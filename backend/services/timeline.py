import json
import re
from pathlib import Path

try:
    from services import media_paths
except ImportError:  # flat-layout fallback
    import media_paths


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


def _pick_rotation_visual(visuals, usage_counts, usage_durations,
                          previous_path, item_duration, scene_duration):
    """Fair, capped rotation draw. Returns the chosen visual or None
    when the eligible pool is exhausted under the use cap (the caller
    then leaves the sentence uncovered)."""
    pool = [
        visual for visual in visuals
        if not visual.get("motion_graphic") and not visual.get("generated")
    ]
    if not pool:
        return None

    def count_of(visual):
        return usage_counts.get(get_visual_path(visual), 0)

    def duration_of(visual):
        return usage_durations.get(get_visual_path(visual), 0.0)

    def under_cap(visual):
        return count_of(visual) < TIMELINE_MAX_FILE_USES

    def not_consecutive(visual):
        return get_visual_path(visual) != previous_path

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


def visual_exists(visual):
    path = get_visual_path(visual)

    if not path:
        return False

    try:
        return Path(path).exists()
    except Exception:
        return False


def timeline_eligible(visual):
    """MA-36/MA-37: may this visual appear on screen at all?

    ONE standard for every timeline path — per-sentence assignment,
    the rotation pool and the legacy fallback:

    - an asset with a recorded download failure is out;
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


# ============================================================
# NARRATION ALIGNMENT
# ============================================================



def align_sentences_to_segments(
    sentences,
    caption_segments,
    narration_duration,
):
    """
    Align script sentences to Whisper word timestamps.

    Whisper may:
    - combine multiple script sentences into one segment
    - split one script sentence across multiple segments
    - omit punctuation
    - slightly alter wording

    Therefore alignment is performed against the complete
    Whisper word sequence rather than assuming one segment
    equals one script sentence.
    """

    import difflib
    import re

    def normalize_token(value):
        value = str(value or "").lower()

        value = re.sub(
            r"[^a-z0-9']+",
            "",
            value,
        )

        return value

    # ---------------------------------------------------------
    # 1. Normalize script sentences
    # ---------------------------------------------------------

    normalized_sentences = []

    script_tokens = []

    for index, sentence in enumerate(sentences or []):

        if isinstance(sentence, dict):
            sentence_text = str(
                sentence.get("text", "")
            ).strip()
        else:
            sentence_text = str(sentence).strip()

        tokens = [
            normalize_token(token)
            for token in sentence_text.split()
        ]

        tokens = [
            token
            for token in tokens
            if token
        ]

        start_token = len(script_tokens)

        script_tokens.extend(tokens)

        end_token = len(script_tokens)

        normalized_sentences.append(
            {
                "index": index,
                "text": sentence_text,
                "tokens": tokens,
                "token_start": start_token,
                "token_end": end_token,
            }
        )

    # ---------------------------------------------------------
    # 2. Flatten Whisper words
    # ---------------------------------------------------------

    whisper_words = []

    for segment in caption_segments or []:

        for word in segment.get("words", []) or []:

            raw_word = str(
                word.get("word", "")
            ).strip()

            normalized = normalize_token(
                raw_word
            )

            if not normalized:
                continue

            start_time = float(
                word.get("start", 0.0)
            )

            end_time = float(
                word.get(
                    "end",
                    start_time,
                )
            )

            if end_time < start_time:
                end_time = start_time

            whisper_words.append(
                {
                    "text": raw_word,
                    "normalized": normalized,
                    "start": start_time,
                    "end": end_time,
                }
            )

    whisper_tokens = [
        word["normalized"]
        for word in whisper_words
    ]

    total_script_tokens = len(
        script_tokens
    )

    total_whisper_words = len(
        whisper_words
    )

    if not normalized_sentences:
        return []

    if not whisper_words:

        duration = float(
            narration_duration or 0.0
        )

        sentence_duration = (
            duration
            / len(normalized_sentences)
        )

        results = []

        for i, sentence in enumerate(
            normalized_sentences
        ):

            start_time = (
                i * sentence_duration
            )

            end_time = (
                (i + 1)
                * sentence_duration
            )

            results.append(
                {
                    "sentence_index": sentence["index"],
                    "text": sentence["text"],
                    "start": round(
                        start_time,
                        3,
                    ),
                    "end": round(
                        end_time,
                        3,
                    ),
                    "duration": round(
                        max(
                            0.0,
                            end_time - start_time,
                        ),
                        3,
                    ),
                    "word_start": 0,
                    "word_end": 0,
                    "alignment_score": 0.0,
                    "alignment_method": "uniform_fallback",
                }
            )

        return results

    # ---------------------------------------------------------
    # 3. Global token alignment
    # ---------------------------------------------------------

    matcher = difflib.SequenceMatcher(
        None,
        script_tokens,
        whisper_tokens,
        autojunk=False,
    )

    matching_blocks = matcher.get_matching_blocks()

    # Map each script token position to a Whisper word index.
    script_to_whisper = {}

    for block in matching_blocks:

        script_start = block.a
        whisper_start = block.b
        size = block.size

        for offset in range(size):

            script_index = (
                script_start + offset
            )

            whisper_index = (
                whisper_start + offset
            )

            script_to_whisper[
                script_index
            ] = whisper_index

    # ---------------------------------------------------------
    # 4. Interpolate unmatched script tokens
    # ---------------------------------------------------------

    matched_positions = sorted(
        script_to_whisper.keys()
    )

    def estimate_whisper_position(
        script_position,
    ):

        if not matched_positions:
            return int(
                (
                    script_position
                    / max(
                        1,
                        total_script_tokens,
                    )
                )
                * total_whisper_words
            )

        if script_position <= matched_positions[0]:

            return script_to_whisper[
                matched_positions[0]
            ]

        if script_position >= matched_positions[-1]:

            return min(
                total_whisper_words - 1,
                script_to_whisper[
                    matched_positions[-1]
                ]
                + (
                    script_position
                    - matched_positions[-1]
                ),
            )

        left = matched_positions[0]
        right = matched_positions[-1]

        for i in range(
            len(matched_positions) - 1
        ):

            current = matched_positions[i]
            following = matched_positions[i + 1]

            if (
                current
                <= script_position
                <= following
            ):

                left = current
                right = following
                break

        left_whisper = script_to_whisper[
            left
        ]

        right_whisper = script_to_whisper[
            right
        ]

        span_script = (
            right - left
        )

        if span_script <= 0:
            return left_whisper

        ratio = (
            script_position - left
        ) / span_script

        estimate = (
            left_whisper
            + ratio
            * (
                right_whisper
                - left_whisper
            )
        )

        return int(round(estimate))

    # ---------------------------------------------------------
    # 5. Build sentence timing windows
    # ---------------------------------------------------------

    results = []

    previous_end_time = 0.0

    for sentence_position, sentence in enumerate(
        normalized_sentences
    ):

        token_start = sentence[
            "token_start"
        ]

        token_end = sentence[
            "token_end"
        ]

        if token_end <= token_start:

            start_time = previous_end_time
            end_time = start_time

            results.append(
                {
                    "sentence_index": sentence["index"],
                    "text": sentence["text"],
                    "start": round(
                        start_time,
                        3,
                    ),
                    "end": round(
                        end_time,
                        3,
                    ),
                    "duration": 0.0,
                    "word_start": 0,
                    "word_end": 0,
                    "alignment_score": 0.0,
                    "alignment_method": "empty_sentence",
                }
            )

            continue

        mapped_indices = []

        for script_position in range(
            token_start,
            token_end,
        ):

            if script_position in script_to_whisper:

                mapped_indices.append(
                    script_to_whisper[
                        script_position
                    ]
                )

        # -----------------------------------------------------
        # Direct matched words
        # -----------------------------------------------------

        if mapped_indices:

            word_start = min(
                mapped_indices
            )

            word_end = max(
                mapped_indices
            ) + 1

        # -----------------------------------------------------
        # No direct match: estimate from neighboring
        # sentence boundaries.
        # -----------------------------------------------------

        else:

            estimated_start = (
                estimate_whisper_position(
                    token_start
                )
            )

            estimated_end = (
                estimate_whisper_position(
                    max(
                        token_start,
                        token_end - 1,
                    )
                )
                + 1
            )

            word_start = max(
                0,
                min(
                    total_whisper_words - 1,
                    estimated_start,
                ),
            )

            word_end = max(
                word_start + 1,
                min(
                    total_whisper_words,
                    estimated_end,
                ),
            )

        # -----------------------------------------------------
        # Clamp to chronological sequence
        # -----------------------------------------------------

        if results:

            minimum_word = results[-1][
                "word_end"
            ]

            if word_start < minimum_word:

                word_start = minimum_word

            if word_end <= word_start:

                word_end = min(
                    total_whisper_words,
                    word_start + 1,
                )

        if word_start >= total_whisper_words:

            word_start = max(
                0,
                total_whisper_words - 1,
            )

        if word_end > total_whisper_words:

            word_end = total_whisper_words

        selected_words = whisper_words[
            word_start:word_end
        ]

        # -----------------------------------------------------
        # Timestamp calculation
        # -----------------------------------------------------

        if selected_words:

            start_time = selected_words[0][
                "start"
            ]

            end_time = selected_words[-1][
                "end"
            ]

        else:

            start_time = previous_end_time
            end_time = previous_end_time

        if start_time < previous_end_time:

            start_time = previous_end_time

        if end_time < start_time:

            end_time = start_time

        # -----------------------------------------------------
        # Alignment score
        # -----------------------------------------------------

        expected = set(
            sentence["tokens"]
        )

        actual = set(
            word["normalized"]
            for word in selected_words
        )

        if expected:

            alignment_score = (
                len(
                    expected & actual
                )
                / len(expected)
            )

        else:

            alignment_score = 0.0

        results.append(
            {
                "sentence_index": sentence["index"],
                "text": sentence["text"],
                "start": round(
                    start_time,
                    3,
                ),
                "end": round(
                    end_time,
                    3,
                ),
                "duration": round(
                    max(
                        0.0,
                        end_time - start_time,
                    ),
                    3,
                ),
                "word_start": word_start,
                "word_end": word_end,
                "alignment_score": round(
                    alignment_score,
                    4,
                ),
                "alignment_method": "global_token_alignment",
            }
        )

        previous_end_time = end_time

    # ---------------------------------------------------------
    # 6. Final boundary
    # ---------------------------------------------------------

    narration_duration = float(
        narration_duration or 0.0
    )

    if results:

        results[-1]["end"] = round(
            narration_duration,
            3,
        )

        if (
            results[-1]["start"]
            > results[-1]["end"]
        ):

            results[-1]["start"] = results[-1][
                "end"
            ]

        results[-1]["duration"] = round(
            max(
                0.0,
                results[-1]["end"]
                - results[-1]["start"],
            ),
            3,
        )

    # ---------------------------------------------------------
    # 7. Final chronological safety
    # ---------------------------------------------------------

    for i in range(
        1,
        len(results),
    ):

        if (
            results[i]["start"]
            < results[i - 1]["end"]
        ):

            results[i]["start"] = results[
                i - 1
            ]["end"]

        if (
            results[i]["end"]
            < results[i]["start"]
        ):

            results[i]["end"] = results[
                i
            ]["start"]

        results[i]["duration"] = round(
            max(
                0.0,
                results[i]["end"]
                - results[i]["start"],
            ),
            3,
        )

    return results


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

        # Phase 1 rotation accounting: total on-screen uses per file
        # (every assignment kind), accumulated durations, the previous
        # item's file (consecutive rule) and the sentences left
        # uncovered because the eligible pool hit the cap.
        usage_counts = {}
        usage_durations = {}
        previous_path = ""
        uncovered_sentence_indexes = []

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

            usage_counts[path] = usage_counts.get(path, 0) + 1
            usage_durations[path] = (
                usage_durations.get(path, 0.0) + sentence_duration
            )
            previous_path = path

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

    timeline_file = (
        project_dir
        / "timeline.json"
    )

    # --------------------------------------------------------
    # LOAD NARRATION TIMINGS
    # --------------------------------------------------------

    caption_data = load_caption_data(
        project_id,
        output_dir,
    )

    narration_duration = float(
        caption_data[
            "duration"
        ]
    )

    caption_segments = caption_data[
        "segments"
    ]

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
    # ALIGN SCRIPT TO NARRATION
    # --------------------------------------------------------

    sentence_timings = (
        align_sentences_to_segments(
            all_sentences,
            caption_segments,
            narration_duration,
        )
    )

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

        "timing_source":
            "captions.json",

        "alignment":
            {
                "method":
                    "whisper_word_alignment",

                "whisper_segments":
                    len(
                        caption_segments
                    ),

                "script_sentences":
                    len(
                        all_sentences
                    ),

                "aligned_sentences":
                    len(
                        sentence_timings
                    ),
            },
    }

    with open(
        timeline_file,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            result,
            file,
            indent=2,
            ensure_ascii=False,
        )

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
        f"Whisper segments: "
        f"{len(caption_segments)}"
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


