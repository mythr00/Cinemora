import json
import re
from pathlib import Path

try:
    from services import media_paths
except ImportError:  # flat-layout fallback
    import media_paths

try:
    from services.atomic_write import write_json_atomic
except ImportError:  # flat-layout fallback
    from atomic_write import write_json_atomic

try:
    from services import run_context
except ImportError:  # flat-layout fallback
    import run_context

try:
    from services import narration as narration_service
    from services import timing as timing_service
except ImportError:  # flat-layout fallback
    import narration as narration_service
    import timing as timing_service

try:
    from services import segment_index
except ImportError:  # flat-layout fallback
    import segment_index


# ============================================================
# CONFIGURATION
# ============================================================

DEFAULT_DURATION = 10.0
MIN_SENTENCE_DURATION = 2.0

# Quality Recovery Phase 1 (workstream B; DM-02 / DM-07 / VV-02 /
# VV-07 / VV-08): rotation is a bounded, labelled fallback — never an
# unbounded filler. A rotation draw may not push a file's TOTAL
# on-screen uses (direct + graphic + rotation) past the selection
# cap, prefers the least-used eligible file (fair draw — the old
# verbatim `visuals[sentence_index % len(visuals)]` modulo loop and
# its videos-first ordering bias are gone), avoids repeating the
# immediately preceding file, and avoids pushing one file past half
# the scene's runtime — the consecutive/share preferences yield only
# when no other eligible file exists, the use cap never does. When
# every eligible file is at the cap, the sentence stays UNCOVERED:
# no cap violation, no borrowing from ineligible assets, no filler.
TIMELINE_MAX_FILE_USES = 3
TIMELINE_MAX_FILE_RUNTIME_SHARE = 0.5

# Stage 7 / R9 (Architecture Review C.F.3): the screen-time cap is
# rebound from FILE identity to SEGMENT identity — the same window
# of the same source counts once toward the cap however many files
# carry it (re-downloads, respelled URLs), and distinct windows of
# one source each hold their own allowance. The cap VALUE is
# unchanged (3): the identity level changed, not the number — the
# value is a separate founder-visible decision (C.F.3), and the
# Stage 6 measurement of stored timelines under segment identity is
# reported by services.visual_quality's segment_reuse block. The
# per-source SELECTION cap (visual_registry.max_source_uses) is a
# different cap at a different stage and is untouched.
TIMELINE_MAX_SEGMENT_USES = TIMELINE_MAX_FILE_USES


def _pick_rotation_visual(visuals, usage_counts, usage_durations,
                          previous_path, item_duration, scene_duration,
                          spanning_keys=frozenset()):
    """Fair, capped rotation draw. Returns the chosen visual or None
    when the eligible pool is exhausted under the use cap (the caller
    then leaves the sentence uncovered).

    ``spanning_keys`` (coverage fix, founder live test 2): the
    segment-use keys of the CURRENT sentence's own background beat.
    A visual in that set is the beat's backdrop being asked to span
    its own beat — eligible regardless of the cap, because the cap
    governs cross-beat reuse, not a backdrop's service inside the
    beat it was verified for. The preference tiers (consecutive,
    runtime share) still apply to it; only the cap is lifted, and
    only within the owning beat."""
    pool = [
        visual for visual in visuals
        if not visual.get("motion_graphic") and not visual.get("generated")
    ]
    if not pool:
        return None

    def count_of(visual):
        return usage_counts.get(_visual_use_key(visual), 0)

    def duration_of(visual):
        return usage_durations.get(_visual_use_key(visual), 0.0)

    def under_cap(visual):
        return (
            count_of(visual) < TIMELINE_MAX_SEGMENT_USES
            or _visual_use_key(visual) in spanning_keys
        )

    def not_consecutive(visual):
        return _visual_use_key(visual) != previous_path

    def under_share(visual):
        if not scene_duration or scene_duration <= 0:
            return True
        projected = duration_of(visual) + max(float(item_duration or 0.0), 0.0)
        return projected / scene_duration <= TIMELINE_MAX_FILE_RUNTIME_SHARE

    # Least-used first (count, then accumulated duration, then pool
    # order for determinism): stranded eligible assets are drawn
    # before any file is repeated (VV-07).
    ordered = sorted(
        enumerate(pool),
        key=lambda pair: (count_of(pair[1]), duration_of(pair[1]), pair[0]),
    )
    tiers = [
        lambda v: under_cap(v) and not_consecutive(v) and under_share(v),
        lambda v: under_cap(v) and not_consecutive(v),
        lambda v: under_cap(v),
    ]
    for tier in tiers:
        for _, visual in ordered:
            if tier(visual):
                return visual
    return None


# ============================================================
# HELPERS
# ============================================================

def get_visual_path(visual):
    return (
        visual.get("file_path")
        or visual.get("path")
        or ""
    )


def _visual_use_key(visual):
    """The identity the screen-time cap counts (Stage 7 / R9).

    - a video counts by SEGMENT: its stamped segment_id, else the
      identity reconstructed from its recorded source window
      (canonical source + clip_start + duration) — never by file, so
      the same window re-downloaded under another name still counts
      once, and a different window of the same source counts apart;
    - a still counts by CANONICAL ASSET (its source), as the reuse
      accounting has always identified stills;
    - a generated graphic counts by file, as before (it is not a
      segment of any source);
    - anything without source identity falls back to its file path,
      the pre-Stage-7 behaviour for identity-less records.
    """
    if not isinstance(visual, dict):
        return ""
    path = get_visual_path(visual)
    if visual.get("motion_graphic") or visual.get("generated"):
        return f"file:{path}"
    segment_id = segment_index.segment_id_for_asset(visual)
    if segment_id:
        if get_visual_type(visual) == "video":
            return f"segment:{segment_id}"
        return f"asset:{segment_id}"
    return f"file:{path}"


def visual_exists(visual):
    path = get_visual_path(visual)

    if not path:
        return False

    try:
        return Path(path).exists()
    except Exception:
        return False


def _recorded_stamp(record, key):
    """A provenance stamp recorded on an asset record, normalised
    to a non-empty string, or None when the record carries none."""
    value = record.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _foreign_provenance(visual):
    """Plan §3.3 item 1 (test 18): True when the asset's RECORDED
    run or project provenance positively mismatches the current
    run — the asset was produced under another production run's
    contract, and its verification does not transfer to this run.

    Positive-mismatch rule, mirroring the contradiction doctrine:
    a MISSING stamp is not a mismatch. Pipeline-produced assets and
    legacy/stored records carry no run stamp and are judged by the
    other eligibility rules exactly as before; only a stamp that is
    present AND different from the current run's identity (the
    active RunContext) marks the asset foreign."""
    try:
        ctx = run_context.current_context()
    except Exception:
        return False
    if ctx is None:
        return False
    run_stamp = _recorded_stamp(visual, "run_id")
    if run_stamp is not None:
        current_run = getattr(ctx, "run_id", None)
        if current_run is None or run_stamp != str(current_run):
            return True
    project_stamp = _recorded_stamp(visual, "project_id")
    if project_stamp is not None:
        current_project = getattr(ctx, "project_id", None)
        if current_project is None or project_stamp != str(current_project):
            return True
    return False


def timeline_eligible(visual):
    """MA-36/MA-37: may this visual appear on screen at all?

    ONE standard for every timeline path — per-sentence assignment,
    the rotation pool and the legacy fallback:

    - an asset with a recorded download failure is out;
    - an asset whose recorded run/project provenance belongs to a
      different production run is out (plan §3.3 item 1): its
      verification was earned under another run's contract and does
      not transfer;
    - a generated motion graphic is eligible as its own honestly
      labelled class (it never claims to be verified footage);
    - any other asset needs a verification record carrying an
      explicit acceptance. A rejected asset is out (VA-16), and an
      asset with NO verification record is out too: a missing
      verdict means unverified — never "verified by silence"
      (MA-37). Before this helper, the rotation pool was filtered
      by file existence only, so rejected, failed and unreviewed
      assets in the scene aggregates reached the screen (MA-36).
    """
    if not isinstance(visual, dict):
        return False

    if visual.get("_download_error"):
        return False

    if _foreign_provenance(visual):
        return False

    if visual.get("motion_graphic") or visual.get("generated"):
        return True

    verification = visual.get("verification")

    if not isinstance(verification, dict):
        return False

    return verification.get("accepted") is True


def get_clip_start(visual):
    """Stage 3F (MA-15): the asset's segment offset (seconds into
    the source clip) as a float, defaulting to 0.0. Carried onto
    VIDEO timeline items so the renderer can input-seek to the
    verified segment instead of always starting at t=0."""
    try:
        value = float(visual.get("clip_start") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def get_visual_type(visual):
    visual_type = (
        visual.get("type")
        or ""
    ).lower()

    if visual_type in {
        "video",
        "image",
    }:
        return visual_type

    path = get_visual_path(
        visual
    ).lower()

    if path.endswith(
        (
            ".mp4",
            ".mov",
            ".m4v",
            ".webm",
            ".mkv",
            ".avi",
        )
    ):
        return "video"

    return "image"


def normalize_text(text):
    if not text:
        return ""

    text = str(text).lower()

    text = re.sub(
        r"[^\w\s]",
        " ",
        text,
        flags=re.UNICODE,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def tokenize(text):
    normalized = normalize_text(text)

    if not normalized:
        return []

    return normalized.split()


# ============================================================
# CAPTION LOADING
# ============================================================

def load_caption_data(
    project_id,
    output_dir=None,
):
    if output_dir is None:
        output_dir = media_paths.media_root()
    project_dir = (
        Path(output_dir)
        / str(project_id)
    )

    caption_file = (
        project_dir
        / "captions.json"
    )

    if not caption_file.exists():
        raise FileNotFoundError(
            "Narration captions were not found: "
            f"{caption_file}"
        )

    with open(
        caption_file,
        "r",
        encoding="utf-8",
    ) as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise RuntimeError(
            "captions.json must contain an object."
        )

    segments = data.get(
        "segments",
        [],
    )

    if not isinstance(
        segments,
        list,
    ):
        raise RuntimeError(
            "captions.json segments must be a list."
        )

    valid_segments = []

    for segment in segments:

        if not isinstance(
            segment,
            dict,
        ):
            continue

        words = segment.get(
            "words",
            [],
        )

        if not isinstance(
            words,
            list,
        ):
            words = []

        try:
            start = float(
                segment.get(
                    "start",
                    0,
                )
            )

            end = float(
                segment.get(
                    "end",
                    start,
                )
            )

        except Exception:
            continue

        if end <= start:
            continue

        valid_segments.append({
            "start": start,
            "end": end,
            "text": segment.get(
                "text",
                "",
            ),
            "words": words,
        })

    duration = data.get(
        "duration"
    )

    if duration is None:

        if valid_segments:
            duration = max(
                segment["end"]
                for segment in valid_segments
            )
        else:
            duration = 0.0

    duration = float(
        duration
    )

    return {
        "audio_file":
            data.get(
                "audio_file",
                "",
            ),

        "language":
            data.get(
                "language",
                "",
            ),

        "duration":
            duration,

        "segments":
            valid_segments,
    }


# ============================================================
# SCRIPT SENTENCE EXTRACTION
# ============================================================

def get_scene_sentences(scene):
    sentences = scene.get(
        "sentences",
        [],
    )

    if isinstance(
        sentences,
        list,
    ):
        return [
            str(sentence)
            for sentence in sentences
            if str(sentence).strip()
        ]

    return []


def build_scene_timeline(
    scene_media,
    sentence_timings=None,
):

    scene_number = scene_media.get(
        "scene_number",
        1,
    )

    duration = float(
        scene_media.get(
            "duration_seconds",
            10,
        )
    )

    # MA-36: the scene aggregates are the rotation pool (and the
    # legacy fallback pool). They are held to the same eligibility
    # standard as per-sentence assignments — file existence alone
    # used to be enough, which let rejected/failed/unreviewed
    # assets rotate onto sentences they were never verified for.
    images = [
        image
        for image in scene_media.get(
            "images",
            [],
        )
        if visual_exists(image)
        and timeline_eligible(image)
    ]

    videos = [
        video
        for video in scene_media.get(
            "videos",
            [],
        )
        if visual_exists(video)
        and timeline_eligible(video)
    ]

    visuals = []

    for video in videos:
        visuals.append(video)

    for image in images:
        visuals.append(image)

    # --------------------------------------------------------
    # PER-SENTENCE VERIFIED ASSIGNMENTS (AUD-14)
    # --------------------------------------------------------
    # The media pipeline records, for every sentence, the exact visual it
    # retrieved and verified for THAT sentence (scene_media["sentences"],
    # in scene order — the same order build_project_timeline slices the
    # sentence timings by). Those assignments are authoritative. The
    # previous implementation ignored them and paired the scene's visuals
    # to sentences round-robin, so a clip verified for sentence A could
    # silently illustrate sentence B.
    assigned_by_position = {}

    for position, sentence_result in enumerate(
        scene_media.get("sentences", []) or []
    ):
        if not isinstance(sentence_result, dict):
            continue

        for candidate in (
            list(sentence_result.get("videos") or [])
            + list(sentence_result.get("images") or [])
        ):
            # VA-16 (defence in depth): the pipeline never places a
            # rejected or failed asset into a sentence result, but the
            # timeline does not blindly trust that — an asset carrying
            # an explicit rejection or download error is skipped here
            # too, so no code path can put it on screen.
            # MA-37: timeline_eligible also skips an asset with NO
            # verification record — a missing verdict is unverified,
            # not a silent pass.
            if not timeline_eligible(candidate):
                continue
            if visual_exists(candidate):
                assigned_by_position[position] = candidate
                break

    # If the scene-level aggregates are empty but per-sentence assignments
    # exist, the assignments themselves are the scene's visuals.
    if not visuals and assigned_by_position:
        seen_paths = set()
        for candidate in assigned_by_position.values():
            path = get_visual_path(candidate)
            if path not in seen_paths:
                seen_paths.add(path)
                visuals.append(candidate)

    if not visuals:

        return {
            "scene_number":
                scene_number,

            "duration_seconds":
                duration,

            "items": [],

            "visuals": [],

            "video_sources": [],

            "sentence_timings":
                sentence_timings or [],
        }

    # --------------------------------------------------------
    # SENTENCE-LEVEL WINDOWS
    # --------------------------------------------------------

    if sentence_timings:

        # Sentence timings are ABSOLUTE project times, while `duration`
        # is this scene's LENGTH. The scene's absolute end is therefore
        # first-timing-start + duration. The window math below previously
        # capped visual_end at `duration` itself, which corrupted every
        # scene after the first (items ending before they started, or
        # collapsing to zero length) — a latent cause of timeline /
        # narration duration mismatches in multi-scene productions.
        timeline_end = float(sentence_timings[0]["start"]) + duration

        items = []

        # Phase 1 rotation accounting: total on-screen uses per
        # identity (every assignment kind), accumulated durations,
        # the previous item's identity (consecutive rule) and the
        # sentences left uncovered because the eligible pool hit the
        # cap. Since Stage 7 / R9 the identity is the SEGMENT
        # (_visual_use_key), not the file.
        usage_counts = {}
        usage_durations = {}
        previous_path = ""
        uncovered_sentence_indexes = []

        # Coverage fix (founder live test 2, part b): own-beat
        # spanning. A background beat's accepted asset is the beat's
        # backdrop — using it for a sentence of its OWN beat is one
        # continuous assignment, not reuse, so it is never blocked
        # by, and never charged against, the segment-use cap; the
        # cap (3, unchanged) governs every OTHER use, i.e. cross-
        # beat service. Beat membership is read from the scene
        # record itself: sentence results carry their beat_id, the
        # scene's beats summary carries each beat's role, and a
        # visual's owning beat is the beat whose sentence results
        # contain it. Scenes without that information (legacy or
        # stored shapes) resolve no spanning keys and account
        # exactly as before.
        _beat_roles = {}
        for _beat_info in scene_media.get("beats") or []:
            if isinstance(_beat_info, dict) and _beat_info.get("beat_id"):
                _beat_roles[_beat_info["beat_id"]] = _beat_info.get("role")
        _sentence_beats = {}
        _visual_beats = {}
        for _position, _result in enumerate(
            scene_media.get("sentences") or []
        ):
            if not isinstance(_result, dict):
                continue
            _beat_id = _result.get("beat_id")
            if not _beat_id:
                continue
            _sentence_beats[_position] = _beat_id
            for _cand in (
                list(_result.get("videos") or [])
                + list(_result.get("images") or [])
            ):
                _path = get_visual_path(_cand)
                if _path:
                    _visual_beats.setdefault(_path, _beat_id)

        def _spanning_keys_for(position):
            _beat_id = _sentence_beats.get(position)
            if not _beat_id or _beat_roles.get(_beat_id) != "background":
                return frozenset()
            return frozenset(
                _visual_use_key(_visual)
                for _visual in visuals
                if _visual_beats.get(get_visual_path(_visual)) == _beat_id
            )

        for sentence_index, timing in enumerate(
            sentence_timings
        ):

            start = float(
                timing["start"]
            )

            end = float(
                timing["end"]
            )

            # Extend the visual window through the beginning of
            # the next sentence so narration pauses are preserved.
            if sentence_index + 1 < len(sentence_timings):
                next_start = float(
                    sentence_timings[sentence_index + 1]["start"]
                )

                visual_end = max(
                    end,
                    next_start,
                )

            else:
                visual_end = max(
                    end,
                    timeline_end,
                )

            # Keep visual assets sequential; narration timing remains untouched.
            visual_start = start
            if items:
                visual_start = max(
                    visual_start,
                    float(items[-1]["end"]),
                )

            sentence_duration = max(
                visual_end - visual_start,
                MIN_SENTENCE_DURATION,
            )

            # Visuals must never flash for a fraction of a second.
            # Keep narration timing untouched; extend only the visual window.
            visual_end = min(
                timeline_end,
                visual_start + sentence_duration,
            )
            # The segment keys this sentence's own background beat
            # may span with (empty for every other sentence).
            spanning_keys = _spanning_keys_for(sentence_index)

            # Prefer a motion graphic explicitly assigned to this sentence.
            assigned_graphics = [
                v for v in visuals
                if v.get("motion_graphic")
                and v.get("sentence_index") == sentence_index
            ]
            if sentence_index in assigned_by_position:
                # AUD-14 + AUD-21: the visual the pipeline verified for
                # THIS sentence always wins — including over a generated
                # motion graphic, which previously replaced genuine
                # footage for any sentence containing a number.
                visual = assigned_by_position[sentence_index]
                assignment = "sentence"
            elif assigned_graphics:
                visual = assigned_graphics[0]
                assignment = "motion_graphic"
            else:
                # Phase 1: bounded fair draw instead of the verbatim
                # modulo loop. Pool exhausted under the cap -> the
                # sentence stays uncovered (recorded, no item built).
                visual = _pick_rotation_visual(
                    visuals, usage_counts, usage_durations,
                    previous_path, sentence_duration, duration,
                    spanning_keys=spanning_keys,
                )
                if visual is None:
                    uncovered_sentence_indexes.append(
                        timing["sentence_index"]
                    )
                    continue
                assignment = "rotation"

            visual_type = get_visual_type(
                visual
            )

            path = get_visual_path(
                visual
            )

            # Stage 7 / R9: usage accounting is keyed by SEGMENT
            # identity (_visual_use_key), not by file — every
            # assignment kind (direct, graphic, rotation) consumes
            # the same identity's allowance. Coverage-fix exception:
            # a spanning use (the visual is the sentence's own
            # background beat's backdrop) is NOT charged — the cap
            # it would consume is the cross-beat allowance, and this
            # use is not cross-beat. previous_path still tracks it,
            # so the consecutive-use preference keeps working.
            use_key = _visual_use_key(visual)
            if use_key not in spanning_keys:
                usage_counts[use_key] = usage_counts.get(use_key, 0) + 1
                usage_durations[use_key] = (
                    usage_durations.get(use_key, 0.0) + sentence_duration
                )
            previous_path = use_key

            item = {
                "type":
                    visual_type,

                "index":
                    len(items),

                "scene_number":
                    scene_number,

                "sentence_index":
                    timing[
                        "sentence_index"
                    ],

                "sentence":
                    timing[
                        "text"
                    ],

                "start":
                    round(
                        visual_start,
                        3,
                    ),

                "end":
                    round(
                        visual_end,
                        3,
                    ),

                "duration":
                    round(
                        sentence_duration,
                        3,
                    ),

                "file_path":
                    path,

                "path":
                    path,

                "source_url":
                    visual.get(
                        "source_url",
                        "",
                    ),

                "thumbnail_url":
                    visual.get(
                        "thumbnail_url",
                        "",
                    ),

                "title":
                    visual.get(
                        "title",
                        "",
                    ),

                "source":
                    visual.get(
                        "source",
                        "",
                    ),

                "selection_score":
                    visual.get(
                        "selection_score",
                        80,
                    ),

                "alignment_score":
                    timing.get(
                        "alignment_score",
                        0,
                    ),

                "visual_relation":
                    "sentence_window",

                # How this visual came to illustrate this sentence:
                # "sentence" (pipeline-verified assignment), "rotation"
                # (scene-level fallback) or "motion_graphic". Consumed by
                # the asset report (AUD-16/AUD-23).
                "assignment":
                    assignment,

                # VA-10: what "assignment" means for THIS sentence,
                # stated explicitly so a rotated visual is never
                # mistaken for a sentence-verified one downstream.
                "relevance_status":
                    {
                        "sentence": "verified_for_sentence",
                        "rotation": "not_verified_for_this_sentence",
                        "motion_graphic": "generated_graphic",
                    }.get(assignment, "not_verified_for_this_sentence"),

                # MA-35: the segment-evidence class of the visual
                # actually used, carried onto the item itself. A
                # positional (evidence-free) window is now
                # distinguishable from an evidence-backed one in the
                # timeline — not only in the asset report — and the
                # report/auditor can measure the positional share of
                # assignments from these stamps.
                "segment_evidence":
                    visual.get(
                        "segment_evidence"
                    ),

                "motion":
                    "auto",

                "transition":
                    "cut",
            }

            # Stage 7 / R7+R8: the item carries the segment identity
            # of the visual actually used and the verification
            # category its verdict was judged under, so downstream
            # accounting (visual_quality's profile buckets and
            # segment-reuse measurement) reads stamps rather than
            # reconstructions. Stamped only when the visual carries
            # them — legacy visuals without them stay unstamped and
            # the accounting falls back exactly as before.
            if visual.get("segment_id"):
                item["segment_id"] = visual["segment_id"]
            _item_verdict = visual.get("verification")
            if isinstance(_item_verdict, dict) and _item_verdict.get(
                "category"
            ):
                item["verification_category"] = _item_verdict["category"]
            elif visual.get("visual_category"):
                item["verification_category"] = visual["visual_category"]

            # Phase 2 (MG-03): a generated graphic's identity and
            # regeneration recipe ride onto the timeline item. The
            # item constructor previously copied an explicit field
            # list with NO graphic fields, so the editor import —
            # which reads template / engine_inputs / graphic_recipe
            # from the item — always found None and regeneration
            # always refused. Fields are copied only when present;
            # ordinary footage items are unchanged. The item is
            # also stamped with the graphic identity itself
            # (motion_graphic / generator): real production items
            # previously carried only assignment="motion_graphic",
            # which the editor import does not read, so imported
            # graphics were misclassified as ordinary footage.
            if visual.get("motion_graphic") or visual.get("graphic_recipe"):
                item["motion_graphic"] = True
                _recipe = visual.get("graphic_recipe") or {}
                if (
                    visual.get("family") == "motion_engine"
                    or _recipe.get("family") == "motion_engine"
                ):
                    item["generator"] = "motion_engine"
                for _graphic_field in (
                    "template",
                    "family",
                    "style",
                    "engine_inputs",
                    "engine_metadata",
                    "graphic_recipe",
                ):
                    if visual.get(_graphic_field) is not None:
                        item[_graphic_field] = visual.get(_graphic_field)

            if visual_type == "video":

                item["video_url"] = visual.get(
                    "video_url",
                    "",
                )

                # Stage 3F: segment offset of the visual actually
                # chosen (sentence / rotation / motion-graphic paths
                # all arrive here with `visual` already resolved).
                item["clip_start"] = get_clip_start(visual)

            else:

                item["image_url"] = visual.get(
                    "image_url",
                    "",
                )

                item["width"] = visual.get(
                    "width"
                )

                item["height"] = visual.get(
                    "height"
                )

            items.append(item)

        return {
            "scene_number":
                scene_number,

            "duration_seconds":
                duration,

            "items":
                items,

            "visuals":
                items,

            "video_sources": [
                {
                    "title":
                        video.get(
                            "title",
                            "",
                        ),

                    "video_url":
                        video.get(
                            "video_url",
                            "",
                        ),

                    "source":
                        video.get(
                            "source",
                            "",
                        ),

                    "snippet":
                        video.get(
                            "snippet",
                            "",
                        ),

                    "duration":
                        video.get(
                            "duration",
                            "",
                        ),

                    "thumbnail_url":
                        video.get(
                            "thumbnail_url",
                            "",
                        ),

                    "selection_score":
                        video.get(
                            "selection_score",
                            100,
                        ),
                }
                for video in videos
            ],

            "video_count":
                len(videos),

            "image_count":
                len(images),

            "sentence_timings":
                sentence_timings,

            # Phase 1: sentences no item was built for because the
            # eligible rotation pool hit its caps — visibly uncovered,
            # never silently filled.
            "uncovered_sentence_indexes":
                uncovered_sentence_indexes,
        }

    # --------------------------------------------------------
    # LEGACY FALLBACK
    # --------------------------------------------------------

    video_count = len(
        videos
    )

    image_count = len(
        images
    )

    if video_count > 0:

        video_weight = 1.5
        image_weight = 1.0

        total_weight = (
            video_count * video_weight
            + image_count * image_weight
        )

        video_duration = (
            duration
            * video_weight
            / total_weight
        )

        image_duration = (
            duration
            * image_weight
            / total_weight
        )

    else:

        video_duration = 0

        image_duration = (
            duration / image_count
            if image_count
            else duration
        )

    items = []

    current_time = 0.0

    for video in videos:

        remaining = (
            duration
            - current_time
        )

        if remaining <= 0:
            break

        item_duration = min(
            video_duration,
            remaining,
        )

        path = get_visual_path(
            video
        )

        items.append({
            "type": "video",
            "index": len(items),
            "scene_number": scene_number,
            "start": round(
                current_time,
                3,
            ),
            "end": round(
                current_time
                + item_duration,
                3,
            ),
            "duration": round(
                item_duration,
                3,
            ),
            "file_path": path,
            "path": path,
            "video_url": video.get(
                "video_url",
                "",
            ),
            "source_url": video.get(
                "source_url",
                "",
            ),
            "thumbnail_url": video.get(
                "thumbnail_url",
                "",
            ),
            "title": video.get(
                "title",
                "",
            ),
            "source": video.get(
                "source",
                "",
            ),
            "selection_score": video.get(
                "selection_score",
                100,
            ),
            # MA-35: evidence class travels with the item (see the
            # sentence-window path above).
            "segment_evidence": video.get(
                "segment_evidence"
            ),
            # Stage 3F: segment offset for the renderer (MA-15).
            "clip_start": get_clip_start(video),
        })

        current_time += item_duration

    for image in images:

        remaining = (
            duration
            - current_time
        )

        if remaining <= 0:
            break

        item_duration = min(
            image_duration,
            remaining,
        )

        path = get_visual_path(
            image
        )

        items.append({
            "type": "image",
            "index": len(items),
            "scene_number": scene_number,
            "start": round(
                current_time,
                3,
            ),
            "end": round(
                current_time
                + item_duration,
                3,
            ),
            "duration": round(
                item_duration,
                3,
            ),
            "file_path": path,
            "path": path,
            "image_url": image.get(
                "image_url",
                "",
            ),
            "source_url": image.get(
                "source_url",
                "",
            ),
            "thumbnail_url": image.get(
                "thumbnail_url",
                "",
            ),
            "title": image.get(
                "title",
                "",
            ),
            "source": image.get(
                "source",
                "",
            ),
            "width": image.get(
                "width"
            ),
            "height": image.get(
                "height"
            ),
            "selection_score": image.get(
                "selection_score",
                80,
            ),
            # MA-35: evidence class travels with the item.
            "segment_evidence": image.get(
                "segment_evidence"
            ),
        })

        current_time += item_duration

    if items:

        difference = (
            duration
            - current_time
        )

        if abs(difference) > 0.001:

            items[-1]["duration"] += (
                difference
            )

            items[-1]["end"] = duration

    video_sources = [
        {
            "title":
                video.get(
                    "title",
                    "",
                ),

            "video_url":
                video.get(
                    "video_url",
                    "",
                ),

            "source":
                video.get(
                    "source",
                    "",
                ),

            "snippet":
                video.get(
                    "snippet",
                    "",
                ),

            "duration":
                video.get(
                    "duration",
                    "",
                ),

            "thumbnail_url":
                video.get(
                    "thumbnail_url",
                    "",
                ),

            "selection_score":
                video.get(
                    "selection_score",
                    100,
                ),
        }
        for video in videos
    ]

    return {
        "scene_number":
            scene_number,

        "duration_seconds":
            duration,

        "items":
            items,

        "visuals":
            items,

        "video_sources":
            video_sources,

        "video_count":
            len(videos),

        "image_count":
            len(images),

        "sentence_timings":
            [],
    }


# ============================================================
# BUILD PROJECT TIMELINE
# ============================================================

def build_project_timeline(
    project_id,
    processed_scenes,
    output_dir=None,
    artefact_dir=None,
    parts_manifest=None,
):

    if output_dir is None:
        output_dir = media_paths.media_root()

    project_dir = (
        Path(output_dir)
        / str(project_id)
    )

    project_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Stage 4 (DATA-004): when the caller runs on behalf of a
    # ledger run, timeline.json — a derived artefact read back as
    # truth — is written into the run's artefact directory instead
    # of the shared project directory. Media and captions are still
    # loaded from the project directory (Phase 3 sharing). Callers
    # without a run keep the legacy project-level location.
    timeline_file = (
        Path(artefact_dir) if artefact_dir is not None else project_dir
    ) / "timeline.json"

    # --------------------------------------------------------
    # LOAD THE MEASURED PARTS MANIFEST (Stage 5 / R4)
    #
    # Production sentence windows are MEASURED part durations
    # (services.timing), never reconstructed from captions —
    # the pre-R4 code loaded captions.json here and aligned
    # script sentences to Whisper word timings, degrading to a
    # uniform total/N split when Whisper failed (AUDIO-001).
    # Resolution: an explicit parts_manifest argument (dict, or
    # a path loaded/upgraded by measurement) wins; otherwise the
    # conventional narration_parts/parts_manifest.json under the
    # project directory. No manifest -> timing_unavailable.
    # --------------------------------------------------------

    if isinstance(parts_manifest, dict):
        manifest_data = parts_manifest
    else:
        manifest_path = (
            Path(parts_manifest)
            if parts_manifest is not None
            else project_dir / "narration_parts" / "parts_manifest.json"
        )
        manifest_data = narration_service.load_parts_manifest_data(
            manifest_path
        )

    if manifest_data is None:
        raise timing_service.TimingUnavailableError(
            "timing_unavailable: no narration parts manifest "
            f"for project {project_id} — production sentence "
            "windows require measured part durations "
            "(services.timing) and are never reconstructed "
            "from captions."
        )

    # --------------------------------------------------------
    # COLLECT PRODUCTION SENTENCES
    # --------------------------------------------------------

    all_sentences = []
    for scene_media in processed_scenes:

        scene = scene_media.get(
            "scene",
            {},
        )

        # Prefer the exact sentence records produced by media processing.
        sentences = scene_media.get(
            "sentences",
            [],
        )

        if not sentences:
            sentences = get_scene_sentences(
                scene
            )

        for sentence in sentences:

            if isinstance(sentence, dict):

                sentence_text = str(
                    sentence.get("sentence_text")
                    or sentence.get("text")
                    or sentence.get("sentence")
                    or ""
                ).strip()

            else:

                sentence_text = str(
                    sentence or ""
                ).strip()
            if sentence_text:

                all_sentences.append(
                    sentence_text
                )

    # --------------------------------------------------------
    # MEASURED SENTENCE WINDOWS (Stage 5 / R4)
    # --------------------------------------------------------

    windows = timing_service.sentence_windows(
        manifest_data
    )

    if len(windows) != len(all_sentences):

        raise timing_service.TimingUnavailableError(
            "timing_unavailable: parts manifest describes "
            f"{len(windows)} sentences but the production "
            f"has {len(all_sentences)} — refusing to build "
            "windows on a mismatched clock."
        )

    narration_duration = timing_service.total_duration(
        manifest_data
    )

    sentence_timings = [
        {
            **window,
            "text":
                sentence_text,
        }
        for window, sentence_text in zip(
            windows,
            all_sentences,
        )
    ]

    # --------------------------------------------------------
    # BUILD SCENE TIMELINES
    # --------------------------------------------------------

    project_time = 0.0

    all_items = []

    scene_results = []

    total_visuals = 0

    sentence_cursor = 0

    scene_ranges = []

    for scene_media in processed_scenes:

        scene = scene_media.get(
            "scene",
            {},
        )

        # Prefer the exact sentence records produced by media processing.
        scene_sentences = scene_media.get(
            "sentences",
            [],
        )

        if not scene_sentences:
            scene_sentences = get_scene_sentences(
                scene
            )

        scene_count = len(
            scene_sentences
        )

        scene_ranges.append((
            scene.get(
                "scene_number",
                len(scene_ranges) + 1,
            ),
            sentence_cursor,
            scene_count,
        ))

        scene_sentence_timings = (
            sentence_timings[
                sentence_cursor:
                sentence_cursor
                + scene_count
            ]
        )

        sentence_cursor += scene_count

        if scene_sentence_timings:

            scene_start = float(
                scene_sentence_timings[0][
                    "start"
                ]
            )

            scene_end = float(
                scene_sentence_timings[-1][
                    "end"
                ]
            )

            scene_duration = max(
                scene_end - scene_start,
                MIN_SENTENCE_DURATION,
            )

        else:

            scene_start = project_time

            scene_duration = float(
                scene_media.get(
                    "duration_seconds",
                    scene.get(
                        "duration_seconds",
                        DEFAULT_DURATION,
                    ),
                )
            )

            scene_end = (
                scene_start
                + scene_duration
            )

        scene_media_for_timeline = {
            **scene_media,

            "duration_seconds":
                scene_duration,
        }

        scene_result = (
            build_scene_timeline(
                scene_media_for_timeline,
                sentence_timings=
                    scene_sentence_timings,
            )
        )

        # ----------------------------------------------------
        # PROJECT TIMESTAMPS
        # ----------------------------------------------------

        for item in scene_result[
            "items"
        ]:

            item = {
                **item,

                "project_start":
                    round(
                        item["start"],
                        3,
                    ),

                "project_end":
                    round(
                        item["end"],
                        3,
                    ),
            }

            all_items.append(
                item
            )

        total_visuals += len(
            scene_result[
                "items"
            ]
        )

        scene_results.append({
            **scene_result,

            "start":
                round(
                    scene_start,
                    3,
                ),

            "end":
                round(
                    scene_end,
                    3,
                ),

            "duration_seconds":
                round(
                    scene_duration,
                    3,
                ),
        })

        project_time = max(
            project_time,
            scene_end,
        )

    # --------------------------------------------------------
    # VISUAL COVERAGE VALIDATION
    #
    # Every narration sentence must be covered by at least one
    # timeline item overlapping its aligned window. A scene whose
    # media stage produced no visuals contributes NO items, so its
    # sentences surface here by number instead of vanishing silently
    # and detonating later as an unexplained frame mismatch in the
    # renderer. Coverage is reported, never faked: an uncovered
    # sentence stays uncovered in this record.
    # --------------------------------------------------------

    uncovered_sentences = []

    for position, timing in enumerate(sentence_timings):

        t_start = float(timing["start"])

        t_end = float(timing["end"])

        covered = any(
            float(it.get("project_start", it.get("start", 0))) < t_end - 0.05
            and float(it.get("project_end", it.get("end", 0))) > t_start + 0.05
            for it in all_items
        )

        if not covered:

            uncovered_scene = None

            for s_num, s_start, s_count in scene_ranges:

                if s_start <= position < s_start + s_count:

                    uncovered_scene = s_num

                    break

            uncovered_sentences.append({
                "sentence_number": position + 1,
                "scene_number": uncovered_scene,
                "start": round(t_start, 3),
                "end": round(t_end, 3),
            })

    coverage = {
        "total_sentences": len(sentence_timings),
        "covered_sentences": len(sentence_timings) - len(uncovered_sentences),
        "uncovered_sentences": uncovered_sentences,
        "complete": not uncovered_sentences,
    }

    # --------------------------------------------------------
    # FINAL DURATION MUST EQUAL NARRATION
    # --------------------------------------------------------

    final_duration = round(
        narration_duration,
        3,
    )

    # --------------------------------------------------------
    # TIMELINE RESULT
    # --------------------------------------------------------

    result = {
        "project_id":
            str(project_id),

        "scene_count":
            len(scene_results),

        "visual_count":
            total_visuals,

        "coverage":
            coverage,

        "sentence_count":
            len(sentence_timings),

        "narration_duration_seconds":
            final_duration,

        "duration_seconds":
            final_duration,

        "scenes":
            scene_results,

        "items":
            all_items,

        # Stage 5 (R4): window provenance — every window in this
        # timeline derives from measured narration part durations
        # (services.timing). The pre-R4 record claimed
        # timing_source "captions.json" with a Whisper alignment
        # block; alignment is a captions-only concern now and is
        # recorded in captions.json, not here.
        "timing_source":
            "measured_part",

        "timing":
            {
                "source":
                    "measured_part",

                "parts":
                    len(
                        sentence_timings
                    ),

                "script_sentences":
                    len(
                        all_sentences
                    ),

                "total_duration_seconds":
                    narration_duration,
            },
    }

    # Atomic write (Stage 4 / DATA-101 class): timeline.json is
    # read back as truth by the plan, the editor import and the
    # report builders — it must never be observable half-written.
    write_json_atomic(timeline_file, result)

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("TIMELINE COMPLETE")
    print("=" * 70)

    print(
        f"Scenes: "
        f"{result['scene_count']}"
    )

    print(
        f"Script sentences: "
        f"{result['sentence_count']}"
    )

    print(
        f"Narration parts (measured): "
        f"{len(sentence_timings)}"
    )

    print(
        f"Visuals: "
        f"{result['visual_count']}"
    )

    print(
        f"Narration duration: "
        f"{final_duration} seconds"
    )

    if total_visuals:

        print(
            f"Average visual window: "
            f"{final_duration / total_visuals:.2f}s"
        )

    print(
        f"Saved: "
        f"{timeline_file}"
    )

    print("=" * 70)

    return {
        **result,

        "timeline_file":
            str(
                timeline_file
            ),

        "status":
            "timeline_created",
    }


