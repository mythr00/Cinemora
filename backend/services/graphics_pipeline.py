import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from services.atomic_write import write_json_atomic, write_text_atomic


BASE_DIR = Path(__file__).resolve().parent.parent

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


FPS = 30
CRF = 20
PRESET = "ultrafast"


# =========================================================
# SHARED GRAPHIC STYLE (Stage B: one style system for every
# generated graphic — palette + bundled font, so output is
# identical on any machine instead of depending on the host's
# fontconfig default font.)
# =========================================================

GRAPHIC_BG_STAT = "0x07090d"
GRAPHIC_BG_TIMELINE = "0x070a10"
GRAPHIC_BG_PROCESS = "0x090c12"
GRAPHIC_PANEL = "0x111722"
GRAPHIC_PANEL_PROCESS = "0x151b25"
GRAPHIC_TEXT = "white"
GRAPHIC_SCRIM = "0x080b10"

_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
GRAPHIC_FONT_FILE = _FONT_DIR / "DejaVuSans.ttf"
GRAPHIC_FONT_BOLD_FILE = _FONT_DIR / "DejaVuSans-Bold.ttf"


def _fontfile(font_path):
    """drawtext fontfile= fragment for a bundled font ('' if missing)."""
    try:
        if font_path and Path(font_path).exists():
            # Escape the two characters that are special inside an
            # ffmpeg filter option value.
            escaped = str(font_path).replace("\\", "/").replace(":", "\\:")
            return f"fontfile={escaped}:"
    except Exception:
        pass
    return ""


# =========================================================
# TEXT HELPERS
# =========================================================

_DRAWTEXT_SCRATCH_DIR = None


def drawtext_textfile(text):
    """Write literal drawtext content to a scratch file and
    return its path, escaped as a filter option value.

    WHY: inline `text='...'` values cannot carry an apostrophe
    safely through ffmpeg's two-level filter parsing when the
    filter string is passed as a subprocess argument (no shell):
    the historical `'\''` escaping silently corrupts the filter
    — the drawtext then renders its own option string
    ("...:x=342:y=663:fontsize=22:...") across the frame at the
    default position (the Container film's chronology graphic,
    founder defect audit 2026-11-23). `textfile=` sidesteps all
    escaping: the file's bytes ARE the text. Files are tiny,
    content-addressed, and live in a per-process scratch dir.
    """
    global _DRAWTEXT_SCRATCH_DIR
    import hashlib
    import tempfile

    if _DRAWTEXT_SCRATCH_DIR is None:
        _DRAWTEXT_SCRATCH_DIR = Path(
            tempfile.mkdtemp(prefix="cinemora_drawtext_")
        )
    key = hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:20]
    path = _DRAWTEXT_SCRATCH_DIR / f"{key}.txt"
    if not path.exists():
        write_text_atomic(path, str(text), encoding="utf-8")
    return str(path).replace("\\", "/").replace(":", "\\:")


def escape_drawtext(text, programmatic=False):
    """
    Escape text for safe embedding inside a single-quoted
    FFmpeg drawtext `text='...'` value.

    Literal label text (programmatic=False, the default):
      - backslash is escaped FIRST (``\\`` -> ``\\\\``) so the
        escapes added below are not themselves doubled;
      - ``:`` -> ``\\:`` — an unescaped colon splits drawtext
        filter options, which is exactly how a label such as
        "around 4:00 a.m." broke rendering (the colon defect);
      - ``%`` -> ``%%`` — a bare % starts drawtext text expansion;
      - apostrophe per the long-standing convention:
        it's  ->  it'\\''s

    Programmatic strings (programmatic=True) are caller-built
    ffmpeg expressions such as the count-up ``%{eif:...}`` form:
    their ``%``, ``:`` and ``\\`` sequences are MEANINGFUL and
    must pass through untouched, so only the apostrophe rule
    is applied.
    """
    text = str(text)
    if programmatic:
        return text.replace("'", "'\\''")
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("%", "%%")
    text = text.replace("'", "'\\''")
    return text


def clean_text(value):
    if value is None:
        return ""
    value = str(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def shorten(text, maximum=48):
    text = clean_text(text)
    if len(text) <= maximum:
        return text
    return text[: maximum - 3].rstrip() + "..."


def detect_date(text):
    patterns = [
        r"\b(?:19|20)\d{2}\b",
        r"\b\d{1,2}[/-]\d{1,2}[/-](?:19|20)\d{2}\b",
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+(?:19|20)\d{2}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def detect_entity(text):
    """
    Extract a display entity from the CURRENT text only.
    No hardcoded people, companies, wars, or cities.
    """
    text = clean_text(text)
    if not text:
        return None
    try:
        from services.script_analyzer import detect_entities
        entities = detect_entities(text) or []
        if entities:
            return clean_text(entities[0])
    except Exception:
        pass
    return None


def source_label(item):
    source = clean_text(item.get("source", ""))
    if not source:
        return ""
    return f"SOURCE • {source.upper()}"


def labels_from_text(text):
    names = []
    places = []
    try:
        from services.script_analyzer import analyze_sentence
        analysis = analyze_sentence(text or "") or {}
        names = [clean_text(x).upper() for x in (analysis.get("entities") or [])[:3] if clean_text(x)]
        places = [clean_text(x).upper() for x in (analysis.get("locations") or [])[:3] if clean_text(x)]
    except Exception:
        entity = detect_entity(text)
        if entity:
            names = [entity.upper()]
    return names, places


# =========================================================
# FOUNDER DEFECT RULINGS (2026-11-23): what may be burned
# into a graphic's pixels. Provenance (file names, sources,
# providers, URLs, licences) lives in the rights manifest and
# the reports — NEVER on screen. A text-bearing graphic may
# carry only a SHORT DESIGNED payload (a statistic, a date, a
# name, a place, a beat label of a few words) — never the
# narration sentence itself, whole or truncated, and never a
# raw asset title. The helpers below are the single selection
# point for those payloads; templates additionally refuse
# sentence-length payloads so no caller (chain, Director
# manifest, replayed recipe) can reintroduce the defect.
# =========================================================

MAX_PAYLOAD_WORDS = 8
MAX_PAYLOAD_CHARS = 64


def is_short_payload(value):
    """True when `value` is short enough to be a designed
    graphic payload rather than sentence text."""
    value = clean_text(value)
    return bool(value) and len(value) <= MAX_PAYLOAD_CHARS \
        and len(value.split()) <= MAX_PAYLOAD_WORDS


def is_metadata_title(value):
    """True when a string is a raw asset/file title rather than
    displayable text: a Wikimedia-style "File:" name, a bare
    file name with an image extension, or a provider-stamped
    title. Such strings are provenance, not content."""
    value = clean_text(value)
    if not value:
        return False
    lowered = value.lower()
    if lowered.startswith("file:"):
        return True
    if re.search(r"\.(jpe?g|png|tiff?|webp|gif|svg)\b", lowered):
        return True
    if "wikimedia" in lowered or "commons.wikimedia" in lowered:
        return True
    return False


def short_payload_for(text, sentence_record=None):
    """Select ONE short designed payload from a sentence, in
    preference order: a date, a named entity, a place. Returns ""
    when the sentence carries none — the caller then does not
    build a text graphic for the beat at all (the narration
    sentence is never the fallback). (P0 removal, 2026-11-27:
    the statistic preference rode on the removed native stat
    family's extraction; a bare number — above all a year — is
    not a designed payload.)"""
    text = clean_text(text)
    if not text:
        return ""
    date = detect_date(text)
    if date and is_short_payload(date):
        return date
    try:
        from services.script_analyzer import analyze_sentence
        analysis = analyze_sentence(text) or {}
        for key in ("entities", "locations"):
            for value in analysis.get(key) or []:
                value = clean_text(value)
                if is_short_payload(value):
                    return value
    except Exception:
        pass
    entity = detect_entity(text)
    if entity and is_short_payload(entity):
        return entity
    return ""


def photo_person_conflict(asset, sentence_text, bible):
    """The name of a person a photo must not be placed for, or "".

    Founder defect ruling 2026-11-23 (graphics-layer guard): a
    photo whose recorded title identifies a person is never used
    as the visual for a sentence about someone else. Two prongs,
    both judged on the metadata that actually identifies the
    photo (its title):

    1. The title names a person who belongs to NO bible entity
       and the title names no bible person either (the Eleanor
       Roosevelt shape — an identifiable stranger to the story).
       Delegates to story_understanding's unrelated-person scan,
       evaluated on the title.
    2. The sentence names bible person(s) and the title names a
       DIFFERENT bible person (a right-story, wrong-person
       photo).

    Assets whose titles name no person raise no conflict — the
    guard judges only what the metadata can actually identify,
    and says so rather than guessing from pixels."""
    if not isinstance(asset, dict) or not isinstance(bible, dict):
        return ""
    title = clean_text(asset.get("title") or asset.get("asset_title") or "")
    if not title:
        return ""
    try:
        from services import story_understanding as _su
    except Exception:
        return ""
    persons = [
        e for e in (bible.get("entities") or [])
        if isinstance(e, dict) and e.get("type") == "person"
    ]
    if not persons:
        return ""
    unrelated = _su._unrelated_named_person(
        asset, bible, _su._norm(title), False
    )
    if unrelated:
        return unrelated
    sentence_norm = _su._norm(sentence_text or "")
    sentence_persons = {
        _su._norm(e.get("canonical") or "")
        for e in persons
        if _su._entity_mentioned(e, sentence_norm)
    }
    if sentence_persons:
        title_norm = _su._norm(title)
        for entity in persons:
            if _su._norm(entity.get("canonical") or "") in sentence_persons:
                continue
            if _su._entity_mentioned(entity, title_norm):
                return entity.get("canonical") or ""
    return ""


def short_title_for(target, text=""):
    """A short designed title for a graphic: the beat's
    recorded scenario label when one exists, else the
    sentence's first entity/place label, else "" (no title).
    NEVER the sentence text, whole or truncated — a truncated
    sentence as a title duplicates the caption and reads as a
    text card."""
    target = target or {}
    scenario = target.get("scenario") or {}
    event = clean_text(scenario.get("event") or "")
    if event and len(event) <= 60:
        return event
    names, places = labels_from_text(text or target.get("text") or "")
    for value in names + places:
        if value:
            return value[:60]
    return ""


# =========================================================
# FFmpeg
# =========================================================

def run_ffmpeg(command):
    print()
    print("=" * 70)
    print("MOTION DESIGNER • FFMPEG")
    print("=" * 70)
    print(" ".join(str(x) for x in command))
    print()

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    output_lines = []
    for line in process.stdout:
        print(line, end="", flush=True)
        output_lines.append(line)

    process.wait()

    if process.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed with exit code {process.returncode}"
        )


def probe_media_duration(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            f"ffprobe could not read '{path}' "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )

    try:
        return float(result.stdout.strip())
    except ValueError:
        raise RuntimeError(
            f"ffprobe returned a non-numeric duration for '{path}': "
            f"{result.stdout.strip()!r}"
        )


# =========================================================
# GRAPHIC OUTPUT VALIDATION (MG-05) + SKIP VOCABULARY (MG-08)
# =========================================================
#
# Phase 1 of the merged Motion Graphics programme: EVERY production
# graphic family (ffmpeg-native stat/timeline/process, PIL event
# schematic, Motion Engine templates) passes ONE validation contract
# before its asset record may be registered. Before this, only the
# Motion Engine path probed its output (inside core.render_video);
# the native families were registered on trust.
#
# The probe itself reuses services.media_probe.probe_video — the
# codebase's single "what is actually in this file?" authority
# (AUD-17) — plus a full decode pass, which is the only check that
# catches truncated/corrupt output whose headers still parse.

MIN_GRAPHIC_FILE_BYTES = 5000
GRAPHIC_DURATION_TOLERANCE_SECONDS = 0.5
GRAPHIC_FPS_TOLERANCE = 1.5

# Closed skip-reason vocabulary for graphics outcomes (MG-08). Every
# reason names a real code path; the historical fall-through string
# "no_stat_value" — which conflated "no extractable statistic",
# "schematic not eligible" and "nothing else fit" — is retired.
# Reasons considered and deliberately NOT included (no recorded
# production path produces them today): duplicate_decision (target
# dedup is silent normalisation, not a recorded skip),
# missing_geographical_data and missing_narration_timing (no
# production graphics path consumes geography or narration timing
# yet — both are Phase 3/4 territory).
GRAPHIC_SKIP_REASONS = frozenset({
    "no_sentence_text",          # target carried no sentence text
    "no_usable_statistic",       # historical (pre-P0 stat family): extraction found no defensible value — no longer produced
    "range_not_representable",   # range with conflicting endpoint units
    "missing_required_input",    # engine template refused its data
    "unsupported_template",      # template name not in the engine registry
    "missing_document_source",   # document cue but no genuine document asset
    "no_schematic_labels",       # event/place labels absent for a schematic
    "no_schematic_motif",        # no schematic motif fits the labels
    "schematic_cap_reached",     # one schematic per scene already produced
    "no_eligible_graphic_purpose",  # chain exhausted / schematic ineligible
    "comparison_not_rendered",   # comparison present, no template rendered it
    "rendering_failure",         # a renderer raised
    "label_does_not_fit",        # Phase 5 (X3): a label measured against its box does not fit — the graphic is refused, never rendered clipped
    "visual_validation_failed",  # Phase 5 (X3): the rendered frame failed visual inspection (near-empty / edge-clipped / text overrun)
    "validation_failed",         # probed OK but spec mismatch (dims/fps/duration)
    "invalid_graphic_output",    # missing/empty/undecodable/no video stream
    "document_presentation_disabled",  # Phase 6: founder rule — no genuine document source exists
    "stat_cap_reached",          # historical (pre-P0 stat family): the max_stats cap — no longer produced
    "terminal_graphics_decline",  # P0 rewiring: every content family was attempted and none could render from the recorded facts — the beat falls back to footage
})

# Fact-basis classes for rendered factual claims (MG-09). The three
# classes are never conflated: only research_supported — with a
# recorded evidence reference — may carry the VERIFIED stamp.
FACT_BASIS_RESEARCH = "research_supported"
FACT_BASIS_SCRIPT = "script_claim"
FACT_BASIS_ASSET = "verified_asset_metadata"
_STAT_STAMPS = {
    FACT_BASIS_RESEARCH: "DATA / VERIFIED",
    FACT_BASIS_ASSET: "DATA / VERIFIED ASSET",
    FACT_BASIS_SCRIPT: "DATA / SCRIPT CLAIM",
}

# Gate 4 (B6): the _STAT_STAMPS strings above are INTERNAL
# classification/provenance labels. They live on asset records and
# in manifests only — they must NEVER be rendered into pixels.
# Founder rule (2026-11-23, reaffirmed Gate 4): provenance belongs
# in the manifest, not the frame. Renderers refuse any display
# text carrying one of these labels (or a source/provenance label
# of the same class) instead of burning it in.
INTERNAL_DISPLAY_LABELS = frozenset(
    list(_STAT_STAMPS.values())
    + [
        "SCRIPT CLAIM", "VERIFIED ASSET", "DATA / VERIFIED",
        "SOURCE •", "SOURCE:", "PROVENANCE", "FACT BASIS",
        "RESEARCH_SUPPORTED", "SCRIPT_CLAIM", "VERIFIED_ASSET_METADATA",
    ]
)


def _contains_internal_label(text):
    """True when display text carries an internal classification /
    provenance label that must never reach a rendered frame."""
    upper = clean_text(text).upper()
    return any(label in upper for label in INTERNAL_DISPLAY_LABELS)


def validate_graphic_output(path, expected_duration=None, expected_fps=FPS):
    """Validate one rendered graphic file against the production
    contract. Returns a verdict dict:

        {"passed": bool,
         "failure_kind": None | "invalid_graphic_output" | "validation_failed",
         "detail": str, "checks": {...}, "probe": {...} | None}

    Checks: existence, non-zero size, minimum reasonable size,
    ffprobe success, a real video stream, 1920x1080 dimensions, a
    valid frame rate (≈ expected), a valid duration consistent with
    `expected_duration` when one is known, and full decodability.
    File existence alone is never treated as proof of validity.
    """
    checks = {}
    probe = None

    def _fail(kind, detail):
        return {
            "passed": False,
            "failure_kind": kind,
            "detail": detail,
            "checks": checks,
            "probe": probe,
        }

    candidate = Path(path)
    if not candidate.is_file():
        checks["exists"] = False
        return _fail(
            "invalid_graphic_output", f"graphic file does not exist: {path}"
        )
    checks["exists"] = True

    size = candidate.stat().st_size
    checks["size_bytes"] = size
    if size <= 0:
        return _fail("invalid_graphic_output", "graphic file is empty")
    if size < MIN_GRAPHIC_FILE_BYTES:
        return _fail(
            "invalid_graphic_output",
            f"graphic file is implausibly small: {size} bytes "
            f"(< {MIN_GRAPHIC_FILE_BYTES})",
        )

    try:
        from services.media_probe import probe_video

        probe = probe_video(candidate)
    except Exception as error:  # probe machinery itself failed
        checks["probe_error"] = str(error)
        probe = None
    if probe is None:
        return _fail(
            "invalid_graphic_output",
            "ffprobe could not read a video stream from the graphic file",
        )
    checks["has_video_stream"] = True
    checks["width"] = probe.get("width")
    checks["height"] = probe.get("height")
    if probe.get("width") != 1920 or probe.get("height") != 1080:
        return _fail(
            "validation_failed",
            f"graphic dimensions {probe.get('width')}x{probe.get('height')}, "
            "expected 1920x1080",
        )
    fps = probe.get("fps")
    checks["fps"] = fps
    if not fps or fps <= 0:
        return _fail("validation_failed", "graphic has no valid frame rate")
    if expected_fps and abs(float(fps) - float(expected_fps)) > GRAPHIC_FPS_TOLERANCE:
        return _fail(
            "validation_failed",
            f"graphic frame rate {fps}, expected ~{expected_fps}",
        )
    duration = probe.get("duration_seconds")
    checks["duration_seconds"] = duration
    if not duration or duration <= 0:
        return _fail("validation_failed", "graphic has no valid duration")
    if expected_duration:
        tolerance = max(
            GRAPHIC_DURATION_TOLERANCE_SECONDS, 0.1 * float(expected_duration)
        )
        if abs(float(duration) - float(expected_duration)) > tolerance:
            return _fail(
                "validation_failed",
                f"graphic duration {duration}s differs from the expected "
                f"{expected_duration}s by more than {tolerance:.2f}s",
            )

    # Full decode: the only check that catches truncated output
    # whose container headers still parse.
    try:
        decode = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(candidate), "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        checks["decodable"] = decode.returncode == 0
        if decode.returncode != 0:
            return _fail(
                "invalid_graphic_output",
                "graphic does not decode cleanly: "
                + (decode.stderr or "").strip()[:200],
            )
    except (OSError, subprocess.SubprocessError) as error:
        checks["decodable"] = False
        return _fail(
            "invalid_graphic_output", f"graphic decode check failed: {error}"
        )

    return {
        "passed": True,
        "failure_kind": None,
        "detail": "",
        "checks": checks,
        "probe": probe,
    }


# =========================================================
# TIMELINE
# =========================================================

def load_timeline(timeline_file):
    timeline_file = Path(timeline_file)
    if not timeline_file.exists():
        raise FileNotFoundError(f"Timeline not found: {timeline_file}")
    with open(timeline_file, "r", encoding="utf-8-sig") as file:
        return json.load(file)


# =========================================================
# GRAPHICS PLAN
# =========================================================

def build_graphics_plan(data):
    """
    Build overlays from CURRENT narration only.
    Names and places are discovered dynamically.
    """
    scenes = data.get("scenes", [])
    duration = float(data.get("duration_seconds", 0) or 0)
    plan = {
        "project_id": data.get("project_id"),
        "duration_seconds": duration,
        "graphics": [],
    }

    date_re = re.compile(
        r"\b(?:January|February|March|April|May|June|July|"
        r"August|September|October|November|December)"
        r"\s+\d{1,2},\s+\d{4}\b",
        re.I,
    )
    money_re = re.compile(
        r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:million|billion))?"
        r"|\b\d+(?:\.\d+)?\s*(?:million|billion)\s+dollars?\b",
        re.I,
    )

    used_keys = set()
    last_end = -10.0

    def item_narration(item):
        parts = [
            item.get("sentence_text"),
            item.get("matched_sentence"),
            item.get("sentence"),
            item.get("text"),
            item.get("narration"),
            item.get("caption"),
        ]
        return " ".join(str(p) for p in parts if p)

    def add(kind, start, end, text, subtext=""):
        nonlocal last_end
        text = clean_text(text)
        if not text:
            return
        start = float(start)
        end = float(end)
        if end <= start:
            end = start + 2.8
        if end - start > 4.5:
            end = start + 3.2
        if start < last_end + 1.2:
            return
        key = (kind, text)
        if key in used_keys:
            return
        used_keys.add(key)
        last_end = end
        graphic = {
            "type": kind,
            "start": max(0.0, start),
            "end": min(duration or end, end),
            "text": text,
        }
        if subtext:
            graphic["subtext"] = clean_text(subtext)
        plan["graphics"].append(graphic)

    for scene in scenes:
        scene_start = float(scene.get("start", 0.0) or 0.0)
        items = scene.get("items") or scene.get("visuals") or []
        scene_text = " ".join(item_narration(item) for item in items[:8])
        names, places = labels_from_text(scene_text)
        if places:
            add(
                "location",
                scene_start + 0.2,
                scene_start + 3.0,
                places[0],
                scene.get("title") or "",
            )

        for item in items:
            start = float(item.get("start", scene_start) or scene_start)
            text = item_narration(item)
            if not text.strip():
                continue

            date = date_re.search(text)
            if date:
                add("date", start, start + 3.0, date.group(0).upper())

            money = money_re.search(text)
            if money:
                add("money", start, start + 3.0, money.group(0).upper())

            item_names, item_places = labels_from_text(text)
            if item_names:
                add("entity", start, start + 3.2, item_names[0])
            if item_places:
                add("location", start, start + 2.8, item_places[0])

    plan["graphics"].append({
        "type": "progress",
        "start": 0,
        "end": duration,
    })
    print(
        "GRAPHICS PLAN:",
        len(plan["graphics"]),
        "items |",
        {
            g["type"]: sum(1 for x in plan["graphics"] if x["type"] == g["type"])
            for g in plan["graphics"]
        },
    )
    return plan


# =========================================================
# FILTER BUILDERS
# =========================================================

def add_scene_marker(filters, graphic):
    start = graphic["start"]
    end = graphic["end"]
    # Literal text via textfile= — inline text= corrupts on
    # apostrophes (see drawtext_textfile).
    text = graphic["text"]

    filters.append(
        "drawbox="
        "x=70:"
        "y=70:"
        "w=240:"
        "h=64:"
        "color=black@0.72:"
        "t=fill:"
        f"enable='between(t\\,{start}\\,{end})'"
    )
    filters.append(
        "drawbox="
        "x=70:"
        "y=134:"
        "w=240:"
        "h=4:"
        "color=white@0.9:"
        "t=fill:"
        f"enable='between(t\\,{start}\\,{end})'"
    )
    filters.append(
        "drawtext="
        "font='Arial':"
        "expansion=none:"
        f"textfile={drawtext_textfile(text)}:"
        "fontcolor=white:"
        "fontsize=28:"
        "x=92:"
        "y=88:"
        f"enable='between(t\\,{start}\\,{end})'"
    )


def add_entity_graphic(filters, graphic):
    start = graphic["start"]
    end = graphic["end"]
    # Literal text via textfile= — inline text= corrupts on
    # apostrophes (see drawtext_textfile).
    text = graphic["text"]
    subtext = graphic.get("subtext", "")

    filters.append(
        "drawbox="
        "x=70:"
        "y=h-190:"
        "w=650:"
        "h=110:"
        "color=black@0.78:"
        "t=fill:"
        f"enable='between(t\\,{start}\\,{end})'"
    )
    filters.append(
        "drawbox="
        "x=70:"
        "y=h-190:"
        "w=8:"
        "h=110:"
        "color=white@0.95:"
        "t=fill:"
        f"enable='between(t\\,{start}\\,{end})'"
    )
    filters.append(
        "drawtext="
        "font='Arial':"
        "expansion=none:"
        f"textfile={drawtext_textfile(text)}:"
        "fontcolor=white:"
        "fontsize=34:"
        "x=105:"
        "y=h-165:"
        f"enable='between(t\\,{start}\\,{end})'"
    )
    if subtext:
        filters.append(
            "drawtext="
            "font='Arial':"
            "expansion=none:"
            f"textfile={drawtext_textfile(subtext)}:"
            "fontcolor=white@0.62:"
            "fontsize=21:"
            "x=105:"
            "y=h-122:"
            f"enable='between(t\\,{start}\\,{end})'"
        )


def add_date_graphic(filters, graphic):
    start = graphic["start"]
    end = graphic["end"]
    # Literal text via textfile= — inline text= corrupts on
    # apostrophes (see drawtext_textfile).
    text = graphic["text"]

    filters.append(
        "drawbox="
        "x=70:"
        "y=170:"
        "w=430:"
        "h=58:"
        "color=black@0.70:"
        "t=fill:"
        f"enable='between(t\\,{start}\\,{end})'"
    )
    filters.append(
        "drawtext="
        "font='Arial':"
        "expansion=none:"
        f"textfile={drawtext_textfile(text)}:"
        "fontcolor=white:"
        "fontsize=27:"
        "x=95:"
        "y=186:"
        f"enable='between(t\\,{start}\\,{end})'"
    )


def add_source_graphic(filters, graphic):
    start = graphic["start"]
    end = graphic["end"]
    # Literal text via textfile= — inline text= corrupts on
    # apostrophes (see drawtext_textfile).
    text = graphic["text"]

    filters.append(
        "drawtext="
        "font='Arial':"
        "expansion=none:"
        f"textfile={drawtext_textfile(text)}:"
        "fontcolor=white@0.55:"
        "fontsize=18:"
        "x=w-text_w-55:"
        "y=h-48:"
        f"enable='between(t\\,{start}\\,{end})'"
    )


def add_progress_graphic(filters, graphic):
    start = graphic["start"]
    end = graphic["end"]
    total_duration = max(float(graphic.get("end", 1)), 0.01)

    filters.append(
        "drawbox="
        "x=0:"
        "y=h-7:"
        "w=iw:"
        "h=7:"
        "color=black@0.55:"
        "t=fill:"
        f"enable='between(t\\,{start}\\,{end})'"
    )
    filters.append(
        "drawbox="
        "x=0:"
        "y=h-7:"
        f"w='iw*(t/{total_duration})':"
        "h=7:"
        "color=white@0.9:"
        "t=fill:"
        f"enable='between(t\\,{start}\\,{end})'"
    )


# =========================================================
# MAIN GRAPHICS RENDER
# =========================================================

def build_graphics(video_file, timeline_file, output_file):
    video_file = Path(video_file)
    timeline_file = Path(timeline_file)
    output_file = Path(output_file)

    if not video_file.exists():
        raise FileNotFoundError(
            f"Motion Designer requires a real video input: {video_file}"
        )
    if not timeline_file.exists():
        raise FileNotFoundError(f"Timeline not found: {timeline_file}")

    data = load_timeline(timeline_file)
    plan = build_graphics_plan(data)
    duration = float(data.get("duration_seconds", 0))
    input_duration = probe_media_duration(video_file)

    if abs(input_duration - duration) > 5.0:
        raise RuntimeError(
            f"video_file '{video_file}' has duration {input_duration:.2f}s, "
            f"but the project's timeline/narration duration is "
            f"{duration:.2f}s. Motion Designer refuses to render against "
            f"a mismatched input."
        )

    print()
    print("=" * 70)
    print("MOTION DESIGNER")
    print("=" * 70)
    print(f"Scenes: {len(data.get('scenes', []))}")
    print(f"Graphics elements: {len(plan['graphics'])}")
    print(f"Duration: {duration:.2f}s")
    print()

    tolerance = 1.0
    max_timestamp = 0.0
    for graphic in plan["graphics"]:
        max_timestamp = max(max_timestamp, float(graphic.get("end", 0)))

    if duration > 0 and max_timestamp > duration + tolerance:
        print(
            "WARNING: a generated graphic has an enable() timestamp "
            f"of {max_timestamp:.2f}s, but the project duration is only "
            f"{duration:.2f}s."
        )

    plan_file = output_file.parent / "graphics_plan.json"
    write_json_atomic(plan_file, plan)
    print(f"Graphics plan: {plan_file}")

    filters = []
    for graphic in plan["graphics"]:
        graphic_type = graphic["type"]
        if graphic_type in {"scene_marker", "location"}:
            add_scene_marker(filters, graphic)
        elif graphic_type == "entity":
            add_entity_graphic(filters, graphic)
        elif graphic_type in {"date", "money"}:
            add_date_graphic(filters, graphic)
        elif graphic_type == "source":
            add_source_graphic(filters, graphic)
        elif graphic_type == "progress":
            add_progress_graphic(filters, graphic)

    if not filters:
        raise RuntimeError("Motion Designer generated zero graphics.")

    filter_complex = "[0:v]" + ",".join(filters) + "[vout]"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    filter_script = output_file.parent / "graphics_filter.ffscript"
    write_text_atomic(filter_script, filter_complex)

    temp_output = output_file.with_name(
        f"{output_file.stem}.tmp{os.getpid()}{output_file.suffix}"
    )

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_file),
        "-/filter_complex",
        str(filter_script),
        "-map",
        "[vout]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        PRESET,
        "-crf",
        str(CRF),
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(temp_output),
    ]

    try:
        run_ffmpeg(command)
        try:
            filter_script.unlink()
        except OSError:
            pass

        if not temp_output.exists():
            raise RuntimeError(
                "Motion Designer finished but no output video was created."
            )

        size = temp_output.stat().st_size
        if size <= 0:
            raise RuntimeError("Motion Designer created an empty output file.")

        try:
            output_duration = probe_media_duration(temp_output)
        except RuntimeError as exc:
            raise RuntimeError(
                "ffmpeg exited successfully but the rendered file failed "
                f"validation: {exc}"
            ) from exc

        if abs(output_duration - duration) > 5.0:
            raise RuntimeError(
                f"Rendered output duration ({output_duration:.2f}s) does "
                f"not match the expected project duration ({duration:.2f}s)."
            )

    except Exception:
        try:
            temp_output.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    temp_output.replace(output_file)

    print()
    print("=" * 70)
    print("MOTION DESIGNER COMPLETE")
    print("=" * 70)
    print(f"Graphics elements: {len(plan['graphics'])}")
    print(f"Output: {output_file}")
    print(f"Size: {size:,} bytes")
    print()
    return str(output_file)


if __name__ == "__main__":
    project_dir = Path("media/3e06bb3d-8c9f-4e37-8d4a-40940d88bce2")
    build_graphics(
        video_file=project_dir / "documentary_captioned.mp4",
        timeline_file=project_dir / "timeline.json",
        output_file=project_dir / "motion_designed.mp4",
    )
# =========================================================
# STANDALONE MOTION GRAPHIC ASSETS
# =========================================================

def _graphic_slug(value):
    value = clean_text(value).lower()
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return value[:60] or "graphic"


def _graphic_duration(start, end, minimum=2.5, maximum=8.0):
    try:
        value = float(end) - float(start)
    except (TypeError, ValueError):
        value = minimum
    return max(minimum, min(maximum, value))


def _ffmpeg_text_filter(
    text,
    x,
    y,
    fontsize=58,
    fontcolor="white",
    alpha=1.0,
    bold=False,
    enable=None,
):
    # Literal text rides via textfile= (see drawtext_textfile):
    # inline text= corrupts on apostrophes and can burn the
    # filter's own option string into the frame.
    text = shorten(text, 64)
    font = _fontfile(
        GRAPHIC_FONT_BOLD_FILE if bold else GRAPHIC_FONT_FILE
    )
    result = (
        f"drawtext={font}textfile={drawtext_textfile(text)}:"
        "expansion=none:"
        f"x={x}:y={y}:"
        f"fontsize={fontsize}:"
        f"fontcolor={fontcolor}@{alpha}:"
        "borderw=2:"
        "bordercolor=black@0.45:"
        "shadowx=3:"
        "shadowy=3:"
        "shadowcolor=black@0.65"
    )
    if enable:
        result += f":enable='{enable}'"
    return result


def _graphic_background():
    return [
        "format=yuv420p",
        "drawgrid="
        "width=120:"
        "height=120:"
        "thickness=1:"
        "color=white@0.035",
        "drawbox="
        "x=0:"
        "y=0:"
        "w=1920:"
        "h=1080:"
        f"color={GRAPHIC_SCRIM}@0.18:"
        "t=fill",
    ]


_STAT_STOP_NOUNS = {
    "in", "on", "at", "by", "for", "from", "was", "were", "is", "are",
    "had", "has", "have", "after", "before", "during", "when", "while",
    "than", "then", "and", "or", "to", "of", "the", "a", "an", "with",
    "into", "over", "under", "per", "each", "more", "most", "only",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
}

_DOCUMENT_MARKERS = (
    "document", "report", "letter", "filing", "memorandum", "memo",
    "manuscript", "deposition", "affidavit", "transcript",
)


def _percent_value(text):
    match = re.search(
        r"(\d[\d,]*(?:\.\d+)?)\s*(?:%|percent\b)", clean_text(text), re.I
    )
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def extract_comparison(text):
    """Two numeric values + a comparison cue -> comparison items.

    Returns [{"label", "value", "display"}, ...] (2 items) or None.
    Labels are taken from the text segment each value sits in —
    nothing is invented.
    """
    text = clean_text(text)
    if not text:
        return None
    if not re.search(
        r"\b(than|versus|vs\.?|compared|against|while|whereas|double|twice|half)\b",
        text, re.I,
    ):
        return None
    values = []
    for match in re.finditer(
        r"(\d[\d,]*(?:\.\d+)?)\s*(%|percent\b|million|billion)?\s*([A-Za-z][A-Za-z\-]*)?",
        text, re.I,
    ):
        number_text = match.group(1)
        try:
            numeric = float(number_text.replace(",", ""))
        except ValueError:
            continue
        if (1000 <= numeric <= 2100 and "," not in number_text
                and "." not in number_text and not match.group(2)):
            continue  # bare year
        noun = (match.group(3) or "")
        if noun.lower() in _STAT_STOP_NOUNS:
            noun = ""
        scale = match.group(2) or ""
        display = clean_text(
            f"{number_text} {scale} {noun}".replace("percent", "%")
        )
        values.append({
            "value": numeric, "display": display, "noun": noun,
            "start": match.start(),
        })
        if len(values) >= 2:
            break
    if len(values) < 2:
        return None
    items = []
    for idx, entry in enumerate(values):
        start = 0 if idx == 0 else values[idx - 1]["start"] + 1
        segment = text[start:entry["start"]].strip(" ,;—-")
        words = segment.split()
        label = " ".join(words[-4:]) if words else (entry["noun"] or f"Value {idx + 1}")
        items.append({"label": label, "value": entry["value"],
                      "display": entry["display"]})
    return items


def _series_from_text(text):
    """Wave 1 (F1): quantities in a sentence as line-chart
    points — the series analogue of extract_comparison (which
    keeps only the first two). Bare years are excluded (they
    are dates, not measurements — the same rule
    extract_comparison applies).

    Founder defect ruling 2026-11-23: a series must share ONE
    unit. The historical version plotted every number in the
    sentence on one axis regardless of unit — the Container
    film's crane sentence became a "chart" of 80 (metres) → 25
    (moves) → 30 (moves), a fabricated trend across unlike
    quantities. Values are now grouped by their unit (scale +
    noun); only a group of at least three same-unit values is a
    series, and only the largest such group is returned."""
    text = clean_text(text)
    if not text:
        return None
    values = []
    for match in re.finditer(
        r"(\d[\d,]*(?:\.\d+)?)\s*(%|percent\b|million|billion)?\s*([A-Za-z][A-Za-z\-]*)?",
        text, re.I,
    ):
        number_text = match.group(1)
        try:
            numeric = float(number_text.replace(",", ""))
        except ValueError:
            continue
        if (1000 <= numeric <= 2100 and "," not in number_text
                and "." not in number_text and not match.group(2)):
            continue  # bare year
        scale = (match.group(2) or "").lower()
        if scale == "million":
            numeric *= 1_000_000
        elif scale == "billion":
            numeric *= 1_000_000_000
        noun = (match.group(3) or "")
        if noun.lower() in _STAT_STOP_NOUNS:
            noun = ""
        display = clean_text(
            f"{number_text} {match.group(2) or ''} {noun}".replace(
                "percent", "%")
        )
        values.append({
            "start": match.start(), "value": numeric,
            "display": display,
            "unit": f"{scale} {noun.lower()}".strip(),
        })
    groups = {}
    for entry in values:
        if entry["unit"]:
            groups.setdefault(entry["unit"], []).append(entry)
    if not groups:
        return None
    values = max(groups.values(), key=len)
    if len(values) < 3:
        return None
    points = []
    for entry in values:
        # MG-08 (Motion Graphics audit): a point's label is the
        # quantity's own recorded display ("30 moves") — never the
        # prose tail before the number. The historical version
        # labelled points with the last three narration words
        # ("standing more than"), prose fragments that read as
        # chart nonsense. The unit rides along so readouts can
        # name the quantity ("moves") instead of repeating the
        # value as its own label.
        points.append({
            "label": entry["display"], "value": entry["value"],
            "display": entry["display"],
            "unit": entry.get("unit") or "",
        })
    return points


def _record_style(style):
    """The style value as it may ride on an asset record (and from
    there into JSON artefacts): a style NAME passes through, but a
    motion-engine token dict carries font Paths — records store
    text. (Live failure, Petito production 2026-11-21: in manifest
    mode _style_for_target returns the resolved theme token dict,
    which was stamped verbatim onto engine records; the
    motion_graphics.json write died on the font PosixPaths after
    the evidence-board photo-path fix had cleared the first leak
    of the same class.)"""
    if isinstance(style, dict):
        return {
            key: (str(value) if isinstance(value, Path) else value)
            for key, value in style.items()
        }
    return style


def build_motion_graphics_assets(
    data,
    output_dir,
    mode="balanced",
    targets=None,
    genre_pack=None,
    manifest=None,
    slot_durations=None,
):
    """
    Generate real standalone MP4 motion-graphic assets.

    These are visual assets, not post-render overlays.
    Each returned record can later be inserted into the
    documentary timeline just like normal video footage.

    Stage A (targeting): `targets` is an optional list of uncovered
    sentences — dicts with scene_number, sentence_index (scene-local,
    0-based), sentence_number, text and optional scenario fields —
    typically derived from the Asset Manager's missing-visual records.
    A targeted sentence whose content fits a graphic family is
    served by that family; a targeted sentence no content family
    can serve reaches the T5 coverage guarantee (stage 6 of the
    chain), which — since the P0 rewiring (2026-11-27) — renders
    NOTHING: the guarantee's terminal outcomes are exactly a
    footage fallback (the beat stays uncovered here and the
    jobs/tier ladder serves it with footage) or a typed
    `terminal_graphics_decline` recorded on the skip and the
    outcome. The historical floor graphics (closing card,
    timeline recap, outcome path, archival-title floor) were
    removed completely and the guarantee is never re-pointed at
    a surviving template. Only a target no eligible family can
    render from its recorded facts (or one carrying no text at
    all) is skipped, and the skip is recorded in the manifest
    with its typed reason and the explicit terminal decline.

    Phase 6 (AM-001/D1 + AM-002 Pass-2): graphics are generated
    ONLY for targeted requirements. The untargeted statistics sweep
    and the unconditional process render are REMOVED:
    with no targets there are no graphics, and the process
    graphic renders only when a target claims it through
    the targeted chain. `manifest` (the Visual Director's
    graphics_manifest dict, or its entries list) is the production
    input: when supplied, its entries ARE the targets, each entry
    restricting the chain to its recorded template/family
    candidates and carrying its theme, fact basis and any
    evidence_ref/verified_data (AM-009). No manifest entry, no
    DIRECTED graphic — and since run-11 fix 5a, a caller target
    for an uncovered beat the manifest does not cover is appended
    as an undirected target, so every uncovered beat reaches the
    chain (and its terminal decline) instead of vanishing.
    """

    if mode in {"off", "none", "disabled"}:
        # Redesign programme (2026-11-27): an OFF build still
        # writes its record — an empty one. Without this, a
        # motion_graphics.json left in the shared project
        # directory by an earlier graphics-ON run survived, and
        # this run's outcome annotation and visual-quality
        # measurement described graphics it never made.
        try:
            _off_dir = Path(output_dir)
            _off_dir.mkdir(parents=True, exist_ok=True)
            write_json_atomic(
                _off_dir / "motion_graphics.json",
                {
                    "mode": "off",
                    "count": 0,
                    "assets": [],
                    "targets": {
                        "requested": 0,
                        "generated": 0,
                        "skipped": [],
                        "outcomes": [],
                    },
                    "undelivered": [],
                    "motion_graphics_disabled": True,
                },
            )
        except Exception:
            pass
        return []

    # Phase 6: manifest entries become the target list. Each entry
    # converts to a target dict carrying the Director's recorded
    # restrictions and stamps; the chain below consumes them.
    manifest_entries = None
    if manifest is not None:
        if isinstance(manifest, dict):
            manifest_entries = manifest.get("entries") or []
        elif isinstance(manifest, list):
            manifest_entries = manifest
    if manifest_entries is not None:
        _caller_targets = list(targets or [])
        targets = []
        for entry in manifest_entries:
            if not isinstance(entry, dict):
                continue
            target_ref = entry.get("target") or {}
            targets.append(
                {
                    "scene_number": target_ref.get("scene_number"),
                    "sentence_index": target_ref.get("sentence_index"),
                    "sentence_number": target_ref.get("sentence_number"),
                    "text": entry.get("text") or "",
                    "scenario": entry.get("scenario") or {},
                    "manifest_id": entry.get("manifest_id"),
                    "template_candidates": entry.get(
                        "template_candidates"
                    ),
                    "family_candidates": entry.get("family_candidates"),
                    "fact_basis": entry.get("fact_basis"),
                    "evidence_ref": entry.get("evidence_ref"),
                    "verified_data": entry.get("verified_data"),
                    "theme": entry.get("theme"),
                }
            )
        # Run-11 fix 5a: the manifest is the Director's direction,
        # not the coverage boundary. The caller's targets derive
        # from the Asset Manager's missing_visuals — EVERY beat
        # still uncovered after the final coverage recovery round.
        # A beat the Director did not manifest used to vanish from
        # the graphics stage entirely (run 11 sentence 12: its
        # missing_visuals record carried graphics_outcome null —
        # it never entered the chain, so not even the T5 floor
        # could serve it). Any caller target the manifest does not
        # cover is appended as an undirected target: it walks the
        # same chain, and the closing-card floor is its guarantee.
        _manifest_keys = {
            (t.get("scene_number"), t.get("sentence_number"))
            for t in targets
        }
        for _caller_target in _caller_targets:
            if not isinstance(_caller_target, dict):
                continue
            _key = (
                _caller_target.get("scene_number"),
                _caller_target.get("sentence_number"),
            )
            if _key[1] is None or _key in _manifest_keys:
                continue
            _manifest_keys.add(_key)
            targets.append(_caller_target)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    case_bible = data.get("case_bible") or {}
    timeline_events = case_bible.get("timeline") or []
    scenes = data.get("scenes") or []

    # Phase 6 (AM-024): the production theme, selected by the
    # genre pack's visual_theme through the six-theme design
    # system. Recorded on the graphics manifest and on every asset
    # record; in manifest mode it also supplies the motion-engine
    # style for engine graphics (see _style_for_target). None when
    # no pack selects a theme — legacy style behaviour then applies
    # unchanged.
    from services.motion_engine import themes as _themes_module

    _production_theme = _themes_module.theme_for_pack(genre_pack)
    _production_theme_name = (
        _production_theme.get("name") if _production_theme else None
    )
    if manifest_entries is not None and _production_theme_name is None:
        _manifest_theme = (
            manifest.get("theme") if isinstance(manifest, dict) else None
        )
        _resolved = _themes_module.resolve_theme(_manifest_theme)
        if _resolved is not None:
            _production_theme = _resolved
            _production_theme_name = _resolved.get("name")

    def _style_for_target(target, default):
        """The motion-engine style for one target's graphics. In
        manifest mode the entry's theme (falling back to the
        production theme) supplies a full theme token dict — the
        theme IS the style. Legacy targets keep the historical
        style names/defaults exactly."""
        if target is not None and target.get("manifest_id"):
            theme = _themes_module.resolve_theme(
                target.get("theme") or _production_theme_name
            )
            if theme is not None:
                return theme
            # Phase 9: a custom theme's token dict cannot be
            # re-resolved from its name ("custom") — when the
            # entry's theme IS the production theme, the
            # production token dict itself is the style.
            if (
                _production_theme is not None
                and (target.get("theme") or _production_theme_name)
                == _production_theme_name
            ):
                return _production_theme
        return default

    assets = []
    index = 0
    # MG-05: every graphic that fails post-render validation is
    # recorded here (and in the manifest) with the original failure
    # information — it is NOT registered, the timeline can never see
    # it, and the corresponding sentence stays uncovered.
    graphic_failures = []

    def register_asset(
        path,
        graphic_type,
        purpose,
        duration,
        text="",
        subtext="",
        recipe=None,
        fact_basis=None,
    ):
        """Validate, then register one rendered graphic.

        Returns the asset record on success, None when validation
        fails (the failure is appended to graphic_failures and the
        caller records the skip against its target). Callers must
        stamp anchors/flags onto the RETURNED record — never onto
        assets[-1], which after a refused registration is a
        different graphic.
        """
        nonlocal index
        verdict = validate_graphic_output(
            path, expected_duration=duration
        )
        if not verdict["passed"]:
            graphic_failures.append({
                "graphic_type": graphic_type,
                "purpose": purpose,
                "stage": "validation",
                "file_path": str(path),
                "reason": verdict["failure_kind"],
                "detail": verdict["detail"],
                "checks": verdict["checks"],
            })
            print(
                f"MOTION GRAPHIC REJECTED (validation) "
                f"[{graphic_type}]: {verdict['detail']}"
            )
            return None
        # Phase 5 (X3): VISUAL validation — the technical probe
        # above cannot see layout. A settled frame is inspected
        # for the gross defect classes that shipped in the
        # founder-judged films (near-empty cards, labels clipped
        # at the frame edge, headlines overrunning their boxes).
        # A graphic that fails is NOT registered: the failure is
        # recorded with the typed visual_validation_failed reason
        # and the beat falls back per the chain's rules (footage
        # or the terminal decline — never another graphic chosen
        # just because it renders). An unavailable inspection
        # (frame extraction failed) is recorded on the verdict
        # and does not block: technical validation already passed
        # and a tooling failure must not fabricate a defect.
        from services.motion_engine import frame_inspection

        visual = frame_inspection.inspect_video(path)
        verdict["visual"] = {
            "passed": visual["passed"],
            "defects": visual["defects"],
            "metrics": visual["metrics"],
        }
        if visual["passed"] is False:
            detail = "; ".join(
                defect["detail"] for defect in visual["defects"]
            )
            graphic_failures.append({
                "graphic_type": graphic_type,
                "purpose": purpose,
                "stage": "validation",
                "file_path": str(path),
                "reason": "visual_validation_failed",
                "detail": detail,
                "checks": verdict["checks"],
                "visual_defects": visual["defects"],
            })
            print(
                f"MOTION GRAPHIC REJECTED (visual validation) "
                f"[{graphic_type}]: {detail}"
            )
            return None
        index += 1

        record = {
            "motion_graphic_id": f"mg_{index:04d}",
            "type": "motion_graphic",
            "graphic_type": graphic_type,
            "graphic_style": graphic_type,
            # Format honesty (founder correction): generated in-house —
            # explicitly identified as generated, never as source material.
            "generated": True,
            "visual_format": "graphic",
            "visual_format_evidence": "generated_in_house",
            "purpose": purpose,
            "file_path": str(path),
            "path": str(path),
            "duration": round(float(duration), 3),
            "source": "cinemora_motion_graphics",
            "title": clean_text(text),
            "text": clean_text(text),
            "subtext": clean_text(subtext),
            "included": True,
            # MG-05/MG-07: the validation verdict rides on the
            # record; `delivered` starts False and is flipped True
            # only by the producing family once the graphic is
            # anchored — jobs.py attaches delivered+valid graphics
            # only.
            "validation": verdict,
            "delivered": False,
        }
        if recipe is not None:
            record["graphic_recipe"] = recipe
        if fact_basis is not None:
            record["fact_basis"] = fact_basis

        assets.append(record)
        return record

    def _recipe(
        family, renderer, inputs, duration, purpose,
        scene_number=None, sentence_index=None, sentence_number=None,
        template=None, style=None, source_references=None,
    ):
        """The complete regeneration recipe for one graphic
        (Phase 2 / MG-03): everything render_graphic_from_recipe
        needs to rebuild the graphic without re-reading the script
        or guessing. `template_version` is None because the Motion
        Engine has no versioning concept today — recorded
        explicitly, never invented."""
        return {
            "family": family,
            "renderer": renderer,
            "template": template,
            "template_version": None,
            "inputs": inputs,
            "style": _record_style(style),
            "duration": duration,
            "purpose": purpose,
            "scene_number": scene_number,
            "sentence_index": sentence_index,
            "sentence_number": sentence_number,
            "source_references": source_references or [],
            "regenerable": True,
            "non_regenerable_reason": None,
        }

    # -----------------------------------------------------
    # STAGE A: normalise targets. Every generated graphic must
    # carry a scene/sentence anchor so the jobs.py attach loop
    # can place it — unanchored graphics never reach a film.
    # -----------------------------------------------------

    target_list = []
    seen_targets = set()
    for target in targets or []:
        if not isinstance(target, dict):
            continue
        scene_number = target.get("scene_number")
        sentence_index = target.get("sentence_index")
        if scene_number is None or sentence_index is None:
            continue
        key = (scene_number, sentence_index)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        target_list.append(
            {
                "scene_number": scene_number,
                "sentence_index": sentence_index,
                "sentence_number": target.get("sentence_number"),
                "text": clean_text(
                    target.get("text") or target.get("sentence_text") or ""
                ),
                "scenario": target.get("scenario") or {},
                # Phase 6: Visual Director manifest fields, carried
                # through when the target came from a manifest
                # entry (None/absent for legacy targets — the chain
                # then runs unrestricted, as before).
                "manifest_id": target.get("manifest_id"),
                "template_candidates": target.get("template_candidates"),
                "family_candidates": target.get("family_candidates"),
                "fact_basis": target.get("fact_basis"),
                "evidence_ref": target.get("evidence_ref"),
                "verified_data": target.get("verified_data"),
                "theme": target.get("theme"),
            }
        )

    target_skips = []
    target_outcomes = []
    undelivered = []

    def _first_scene_anchor():
        for scene in scenes:
            if scene.get("scene_number") is not None and (
                scene.get("sentences") or []
            ):
                return {
                    "scene_number": scene.get("scene_number"),
                    "sentence_index": 0,
                    "sentence_number": (
                        (scene.get("sentences") or [{}])[0].get(
                            "sentence_number"
                        )
                        or 1
                    ),
                }
        return None

    # (P0 removal, 2026-11-27: the MA-06 anchor selection that
    # lived here — chronology anchor in R1, process anchor in
    # R6 — was removed completely with both native families,
    # renderers included. Every remaining graphic is produced
    # per-target by the targeted chain below and carries its
    # target's anchor directly. A beat that would have received
    # a removed graphic falls back to footage or a typed
    # decline.)


    # -----------------------------------------------------
    # EVENT SCHEMATIC support: the fallback for a targeted
    # sentence that carries no statistic. Conservative trigger —
    # ALL of: the sentence is a target (above); it has no stat
    # value (decided in the loop below); its scene is KNOWN from
    # the processed scenes the caller already supplies (jobs.py
    # and the E2E harness both pass processed_scenes in `data`)
    # to hold NO genuine verified visual at all; and the
    # sentence's own analysis record provides event + place
    # labels. At most one schematic per scene. Generated graphics
    # themselves never make a scene count as covered here.
    # -----------------------------------------------------

    _verified_by_scene = {}
    # Wave 1: the accepted verified asset dicts per scene — the
    # material the document/profile/newspaper/parallax assemblers
    # build from (same acceptance rule as the counts below).
    _verified_by_scene_assets = {}
    # Per-sentence coverage (Stage 3D): keys of sentences that have
    # an explicitly accepted verified visual LINKED to them (via
    # sentence_index, or sentence_number resolved through the
    # processed scene's sentence list). Only explicit links enter
    # this set — an unlinked verified asset says nothing about
    # which sentence it covers.
    _verified_sentences = set()
    _linked_verified_scenes = set()
    for _ps in data.get("processed_scenes") or []:
        if not isinstance(_ps, dict):
            continue
        _sn = (_ps.get("scene") or {}).get("scene_number")
        if _sn is None:
            continue
        _ps_sentences = (_ps.get("scene") or {}).get("sentences") or []
        _genuine = 0
        for _v in list(_ps.get("videos") or []) + list(
            _ps.get("images") or []
        ):
            if not isinstance(_v, dict):
                continue
            if _v.get("motion_graphic") or _v.get("_download_error"):
                continue
            # MA-37: only an explicit acceptance counts as a genuine
            # verified visual. A missing verification record means
            # unverified — it must not suppress the schematic by
            # pretending the scene is already covered by a verified
            # visual.
            if (_v.get("verification") or {}).get("accepted") is not True:
                continue
            _genuine += 1
            _verified_by_scene_assets.setdefault(_sn, []).append(_v)
            _linked_index = _v.get("sentence_index")
            if _linked_index is None and _v.get("sentence_number") is not None:
                for _i, _s in enumerate(_ps_sentences):
                    if isinstance(_s, dict) and (
                        _s.get("sentence_number") == _v.get("sentence_number")
                    ):
                        _linked_index = _i
                        break
            if _linked_index is not None:
                _verified_sentences.add((_sn, _linked_index))
                _linked_verified_scenes.add(_sn)
        if _sn not in _verified_by_scene:
            _verified_by_scene[_sn] = _genuine

    _sentences_by_key = {}
    for _scene in scenes:
        for _idx, _sentence in enumerate(_scene.get("sentences") or []):
            if isinstance(_sentence, dict):
                _sentences_by_key[
                    (_scene.get("scene_number"), _idx)
                ] = _sentence

    def _schematic_labels(target):
        """Resolve (event, place, era) for a schematic ONLY from
        the target sentence's own analysis record: the target's
        scenario, else the sentence record's first scenario /
        event / locations / dates. Returns None when event or
        place is absent — labels are never invented."""
        scenario = target.get("scenario") or {}
        sentence = _sentences_by_key.get(
            (target["scene_number"], target["sentence_index"]), {}
        )
        _scenarios = sentence.get("scenarios") or []
        first = (
            _scenarios[0]
            if _scenarios and isinstance(_scenarios[0], dict)
            else {}
        )
        event = clean_text(
            scenario.get("event")
            or first.get("event")
            or sentence.get("event")
            or ""
        )
        _locations = sentence.get("locations") or []
        place = clean_text(
            (_locations[0] if _locations else "")
            or scenario.get("place")
            or first.get("place")
            or ""
        )
        _dates = sentence.get("dates") or []
        era = clean_text(
            scenario.get("era")
            or first.get("era")
            or (_dates[0] if _dates else "")
            or ""
        )
        if not event or not place:
            return None
        return event, place, era

    _schematic_scenes = set()

    def _schematic_eligibility(target):
        """Stage 3D per-sentence reachability: eligible when the
        scene is known to hold no verified visual at all (the
        original trigger), OR when the target sentence's OWN
        coverage is empty — no accepted verified visual is linked
        to this sentence — while other sentences in the scene do
        have linked verified coverage. When no verified asset in
        the scene carries any sentence link, per-sentence coverage
        is unknowable and the scene-level rule decides alone.

        Returns (eligible, blocked_reason): blocked_reason is None
        when eligibility could not be decided (coverage unknown —
        the schematic stage then contributes no specific skip
        reason), and "no_eligible_graphic_purpose" when coverage
        positively blocks the schematic. MG-08: ineligibility is
        NEVER again reported as "no_stat_value"."""
        scene_number = target["scene_number"]
        if scene_number not in _verified_by_scene:
            return False, None
        if _verified_by_scene[scene_number] == 0:
            return True, None
        key = (scene_number, target["sentence_index"])
        if key in _verified_sentences:
            return False, "no_eligible_graphic_purpose"
        if scene_number in _linked_verified_scenes:
            return True, None
        return False, "no_eligible_graphic_purpose"

    def _make_schematic(target):
        """Render + register one event schematic for a target.
        Returns (True, None, "") on success, otherwise
        (False, reason, detail) with a typed MG-08 reason."""
        scene_number = target["scene_number"]
        eligible, blocked_reason = _schematic_eligibility(target)
        if not eligible:
            return False, blocked_reason, ""
        if scene_number in _schematic_scenes:
            return False, "schematic_cap_reached", ""
        labels = _schematic_labels(target)
        if labels is None:
            return False, "no_schematic_labels", ""
        event, place, era = labels

        path = (
            output_dir
            / f"motion_graphic_schematic_{index + 1:03d}.mp4"
        )
        # MA-04: no motif for this event -> no graphic.
        motif = None
        try:
            from services.vector_graphics import select_schematic_motif

            motif = select_schematic_motif(event, place, era)
        except Exception:
            motif = None
        if motif is None:
            return False, "no_schematic_motif", ""
        try:
            from services.vector_graphics import render_event_schematic

            rendered = render_event_schematic(
                path,
                event_label=event,
                place_label=place,
                era_label=era,
                duration=6.0,
            )
        except Exception as error:
            # AUD-21: skip this graphic; never fail the whole job.
            print(f"MOTION GRAPHIC SKIPPED (event_schematic): {error}")
            return False, "rendering_failure", str(error)
        if rendered is None:
            return False, "no_schematic_motif", ""

        record = register_asset(
            path,
            "event_schematic",
            "event_schematic",
            6.0,
            event,
            target["text"],
            recipe=_recipe(
                "event_schematic", "pil_schematic",
                {
                    "event_label": event,
                    "place_label": place,
                    "era_label": era,
                    "motif": motif,
                },
                6.0, "event_schematic",
                scene_number=scene_number,
                sentence_index=target["sentence_index"],
                sentence_number=(
                    target["sentence_number"] or target["sentence_index"] + 1
                ),
                source_references=[{
                    "type": "script_sentence_analysis",
                    "scene_number": scene_number,
                    "sentence_number": (
                        target["sentence_number"]
                        or target["sentence_index"] + 1
                    ),
                }],
            ),
        )
        if record is None:
            failure = graphic_failures[-1]
            return False, failure["reason"], failure["detail"]
        record["scene_number"] = scene_number
        record["sentence_index"] = target["sentence_index"]
        record["sentence_number"] = (
            target["sentence_number"] or target["sentence_index"] + 1
        )
        record["targeted"] = True
        record["family"] = "event_schematic"
        record["schematic_motif"] = motif
        record["delivered"] = True
        _schematic_scenes.add(scene_number)
        return True, None, ""

    # -----------------------------------------------------
    # Phase 6 (AM-010): production assemblers for the templates
    # the Stage 3 chain could not reach. Each assembler builds a
    # template's engine data ONLY from recorded material — the
    # target sentence's own analysis, the case bible, or the
    # scene's verified assets — and returns None when that
    # material does not exist. An assembler never invents data:
    # None means the template is not attempted for this target.
    # -----------------------------------------------------

    # -----------------------------------------------------
    # Wave 1 assemblers (Motion Graphics Engine plan §C).
    # Every assembler returns engine data built ONLY from the
    # target's recorded material, or None — the template then
    # stays unproposed for this target and any attempt records
    # the engine's typed refusal.
    # -----------------------------------------------------

    # -----------------------------------------------------
    # UV-01 / UV-12 (Unified Visual Audit): the graphics chain's
    # script-fact intake. ONE normalisation + honesty filter for
    # every consumer below (line/bar/pie charts, dashboard, HUD,
    # holographic panel).
    #
    # UV-01: in production the bible's script_facts is the
    # build_fact_table DICT {facts, record}; the historical
    # consumers iterated it as a bare list, which yields the
    # dict's string keys — the isinstance guard then silently
    # discarded every fact, so charts and HUD readouts never saw
    # a recorded quantity and fell back to regex text-mining.
    # The dict shape is unwrapped here; a bare list (legacy
    # callers/tests) passes through unchanged.
    #
    # UV-12: the bible's research labels have a production
    # consumer now. A quantity fact is excluded from every
    # graphic when the bible's own records mark it conflicted:
    # an explicit label on the fact (conflicted/conflict flags,
    # or a negative support label), or a bible["conflicts"]
    # entry whose recorded disagreeing values contain the
    # fact's value AND whose fact_hint identifies the fact's
    # own sentence (the hint is the conflicted fact's text —
    # for fact-table timeline entries, "<when> <sentence>").
    # A fact with no label is treated exactly as before.
    # Exclusions are recorded in _fact_exclusions and surface
    # in the graphics manifest — never silent.
    # -----------------------------------------------------

    _fact_exclusions = []
    _quantity_facts_cache = {}

    def _script_fact_list():
        table = (case_bible or {}).get("script_facts")
        if isinstance(table, dict):
            facts = table.get("facts")
        else:
            facts = table
        return [f for f in facts or [] if isinstance(f, dict)]

    def _hint_key(text):
        return re.sub(
            r"\s+", " ",
            re.sub(r"[^a-z0-9 ]", " ", str(text or "").lower()),
        ).strip()

    def _value_token(raw):
        text = str(raw or "").replace(",", "").strip()
        try:
            number = float(text)
        except (TypeError, ValueError):
            return text.lower()
        return str(int(number)) if number == int(number) else str(number)

    _conflicts = [
        c for c in ((case_bible or {}).get("conflicts") or [])
        if isinstance(c, dict)
    ]
    _conflict_values = {
        _value_token(v.get("value"))
        for c in _conflicts
        for v in (c.get("values") or [])
        if isinstance(v, dict)
    }
    _conflict_hints = [
        _hint_key(c.get("fact_hint")) for c in _conflicts
    ]
    _sentence_text_by_number = {}
    for _scene in scenes:
        for _sentence in _scene.get("sentences") or []:
            if isinstance(_sentence, dict) and (
                _sentence.get("sentence_number") is not None
            ):
                _sentence_text_by_number[
                    _sentence["sentence_number"]
                ] = _sentence.get("text") or ""

    def _fact_is_conflicted(fact):
        if fact.get("conflicted") or fact.get("conflict"):
            return True
        if str(fact.get("support") or "").strip().lower() in (
            "conflicted", "unsupported", "disputed",
        ):
            return True
        if not _conflict_values:
            return False
        tokens = set()
        if fact.get("value") is not None:
            tokens.add(_value_token(fact.get("value")))
        for match in re.finditer(
            r"\d[\d,]*(?:\.\d+)?",
            clean_text(fact.get("display") or ""),
        ):
            tokens.add(_value_token(match.group(0)))
        if not tokens or not (tokens & _conflict_values):
            return False
        texts = []
        if fact.get("what"):
            texts.append(
                f"{fact.get('when') or ''} {fact.get('what')}"
            )
        sentence_text = _sentence_text_by_number.get(
            fact.get("sentence_number")
        )
        if sentence_text:
            texts.append(sentence_text)
        for text in texts:
            key = _hint_key(text)
            if not key:
                continue
            for hint in _conflict_hints:
                if hint and (
                    key[:40] in hint or hint[:40] in key
                ):
                    return True
        return False

    def _quantity_value_unit(fact):
        """A quantity fact's (value, unit): the recorded fields
        when present; otherwise derived from the fact's own
        display string ("80 metres" -> 80 / "metres") — the
        production fact table (services.script_facts) records
        display + sentence reference but no parsed value/unit,
        so without this derivation the UV-01 shape fix alone
        would still starve every production chart. A display
        with no leading number ("twenty-foot") derives nothing
        and the fact stays unusable, exactly as before."""
        value = fact.get("value")
        unit = clean_text(fact.get("unit") or "").lower()
        if value is None or not unit:
            match = re.match(
                r"^\s*(\d[\d,]*(?:\.\d+)?)\s*(.*)$",
                clean_text(fact.get("display") or ""),
            )
            if match:
                if value is None:
                    value = match.group(1)
                if not unit:
                    unit = match.group(2).strip().lower()
        return value, unit

    def _usable_quantity_facts():
        """The bible's quantity facts minus the UV-12 exclusions,
        computed once per build (assemblers call repeatedly)."""
        if "facts" not in _quantity_facts_cache:
            usable = []
            for fact in _script_fact_list():
                if fact.get("kind") != "quantity":
                    continue
                if _fact_is_conflicted(fact):
                    _fact_exclusions.append({
                        "fact_id": fact.get("fact_id"),
                        "sentence_number": fact.get("sentence_number"),
                        "display": clean_text(
                            fact.get("display") or ""
                        ),
                        "reason": "research_conflict",
                    })
                    continue
                usable.append(fact)
            _quantity_facts_cache["facts"] = usable
        return _quantity_facts_cache["facts"]

    def _fact_quantities():
        """Recorded quantity facts from the Phase 2.3 fact table,
        grouped by unit: {unit: [fact, ...]} in sentence order."""
        grouped = {}
        for fact in _usable_quantity_facts():
            raw_value, unit = _quantity_value_unit(fact)
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            grouped.setdefault(unit, []).append({
                "value": value,
                "display": clean_text(fact.get("display") or ""),
                "sentence_number": fact.get("sentence_number"),
                "date": clean_text(
                    (fact.get("date") or {}).get("display") or ""),
            })
        for facts in grouped.values():
            facts.sort(key=lambda f: f["sentence_number"] or 0)
        return grouped

    def _assemble_line_chart(target):
        grouped = _fact_quantities()
        for unit, facts in grouped.items():
            if len(facts) >= 3:
                return {
                    "points": [
                        {"label": f["date"] or f["display"],
                         "value": f["value"], "display": f["display"]}
                        for f in facts[:8]
                    ],
                    "title": short_title_for(target),
                    "unit": unit,
                }
        points = _series_from_text(target["text"])
        if points:
            return {"points": points, "title": short_title_for(target)}
        return None

    def _assemble_pie_chart(target):
        text = target["text"]
        lowered = text.lower()
        if any(cue in lowered for cue in
               ("share", "accounted for", "made up", "% of", "percent of")):
            items = extract_comparison(text) or _series_from_text(text)
            if items and len(items) >= 2:
                return {"items": items,
                        "title": short_title_for(target)}
        grouped = _fact_quantities()
        for _unit, facts in grouped.items():
            if len(facts) >= 2:
                return {
                    "items": [
                        {"label": f["date"] or f["display"],
                         "value": f["value"], "display": f["display"]}
                        for f in facts[:6]
                    ],
                    "title": short_title_for(target),
                }
        return None

    def _asset_file(asset):
        for key in ("path", "file", "file_path", "local_path"):
            value = asset.get(key)
            if value:
                return value
        return None

    def _section_variant(name):
        def _assemble(target):
            scenario = target.get("scenario") or {}
            layers = scenario.get("layers")
            if not isinstance(layers, list) or not layers:
                return None
            return {
                "layers": layers,
                "title": clean_text(
                    short_title_for(target)),
                "variant": name,
                "markers": scenario.get("markers") or [],
            }
        return _assemble

    def _verified_scene_assets(target):
        return _verified_by_scene_assets.get(target["scene_number"]) or []

    def _assemble_newspaper_montage(target):
        articles = []
        for asset in _verified_scene_assets(target):
            title = clean_text(
                asset.get("title") or asset.get("asset_title") or "")
            # Founder ruling 2026-11-23: no raw metadata text on
            # screen — an asset whose title is a file name is not
            # a headline, and no source label is ever attached.
            # MG-06-residual: a long descriptive archive title
            # that slips the metadata patterns is still caller
            # prose, not a headline — only a short payload (the
            # designed-element shape) may be typeset as one.
            if not title or is_metadata_title(title) \
                    or not is_short_payload(title):
                continue
            articles.append({
                "headline": title,
            })
            if len(articles) >= 3:
                break
        if len(articles) < 2:
            return None
        return {"articles": articles,
                "title": short_title_for(target)}

    def _identity_photo(target):
        for asset in _verified_scene_assets(target):
            if asset.get("evidence_type") == "identity" or asset.get(
                    "asset_role") == "real_person_photo":
                # Founder ruling 2026-11-23: an identity photo of
                # a person the sentence is not about is the
                # wrong-person defect in its purest form — skip it.
                if photo_person_conflict(
                    asset, target.get("text") or "", case_bible
                ):
                    continue
                path = _asset_file(asset)
                if path and os.path.exists(path):
                    return asset, path
        return None, None

    def _assemble_profile_reveal(target):
        asset, path = _identity_photo(target)
        if path is None:
            return None
        sentence = _sentences_by_key.get(
            (target["scene_number"], target["sentence_index"]), {})
        fields = []
        for date in (sentence.get("dates") or [])[:1]:
            fields.append({"label": "Date", "value": clean_text(date)})
        for place in (sentence.get("locations") or [])[:1]:
            fields.append({"label": "Place", "value": clean_text(place)})
        # Founder ruling 2026-11-23: the profile's title is the
        # PERSON the sentence names (a designed payload), never
        # the asset's recorded title (raw metadata), and the
        # subtitle is never a source/provider label. No nameable
        # person in the sentence — no profile card.
        title = clean_text(detect_entity(target["text"]) or "")
        if not title:
            names, _places = labels_from_text(target["text"])
            title = names[0] if names else ""
        if not title:
            return None
        return {
            "photo": path,
            "title": title,
            "fields": fields,
        }

    def _first_scene_photo(target):
        for asset in _verified_scene_assets(target):
            # Founder ruling 2026-11-23: never place a photo whose
            # title identifies a person the target sentence is not
            # about (see photo_person_conflict).
            if photo_person_conflict(
                asset, target.get("text") or "", case_bible
            ):
                continue
            path = _asset_file(asset)
            if path and os.path.exists(path):
                return asset, path
        return None, None

    def _assemble_text_overlay(target):
        # Photo-backed only: a text overlay with no photo behind
        # it is a text card, and text cards are not a visual
        # answer (the chain's floor covers bare beats instead).
        _asset, path = _first_scene_photo(target)
        if path is None:
            return None
        # Founder ruling 2026-11-23: the overlay's text is a
        # SHORT DESIGNED payload selected from the sentence
        # (statistic / date / name / place) — never the sentence
        # itself, which the caption already carries. Position is
        # UPPER so the label never collides with the caption
        # band at the bottom of the frame.
        payload = short_payload_for(
            target["text"],
            sentence_record=_sentences_by_key.get(
                (target["scene_number"], target["sentence_index"])
            ),
        )
        if not payload:
            return None
        return {
            "text": payload,
            "photo": path,
            "position": "upper",
        }

    def _assemble_text_highlight(target):
        # Quoted passages only: the highlight page presents the
        # sentence as a document excerpt, so the sentence must
        # actually carry quoted material — a bare document
        # mention ("the report described...") is not an excerpt
        # and keeps the chain's missing_document_source outcome.
        text = target["text"]
        quoted = re.search(r"[\"“]([^\"”]{3,60})[\"”]", text)
        if quoted is None:
            return None
        # Founder ruling 2026-11-23: the page presents the QUOTED
        # PASSAGE itself — the genuine short excerpt — never the
        # whole narration sentence as a fabricated document page,
        # and no header label (the historical source_label burned
        # an entity name, or the placeholder "Document", onto it).
        return {
            "text": quoted.group(1),
            "highlight": quoted.group(1),
        }

    # Phase 5 (A2): the archival_title KICKER fact-guard. The
    # Coffee film's opening title carried the kicker "PRESENT
    # DAY" over a film that begins in the Ethiopian highlands
    # centuries ago — the kicker was taken verbatim from one
    # recorded field and never checked against the beat's other
    # recorded facts. The guard below: a kicker is produced ONLY
    # from a recorded fact (the sentence record, its scenario,
    # the target scenario, or the case bible), and a candidate
    # is refused when the beat's OTHER recorded temporal facts
    # contradict it. A kicker is never derived from the
    # sentence's prose. When no recorded fact supports a kicker
    # the title renders without one — unless the request made a
    # kicker mandatory (scenario "kicker_required"), in which
    # case the assembler declines outright.
    _PRESENT_DAY_RE = re.compile(
        r"present[\s-]?day|today|nowadays|contemporary|"
        r"modern[\s-]?day|the present", re.IGNORECASE)
    _CENTURY_RE = re.compile(
        r"(\d{1,2})(?:st|nd|rd|th)[\s-]century", re.IGNORECASE)
    _DECADE_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]0)s\b")
    _YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")

    def _era_ranges(text):
        """Year ranges a recorded temporal string spans: exact
        years, decades ("1960s" -> 1960-1969) and centuries
        ("19th century" -> 1801-1900)."""
        ranges = []
        for match in _CENTURY_RE.finditer(text or ""):
            start = (int(match.group(1)) - 1) * 100 + 1
            ranges.append((start, start + 99))
        for match in _DECADE_RE.finditer(text or ""):
            start = int(match.group(1))
            ranges.append((start, start + 9))
        for match in _YEAR_RE.finditer(text or ""):
            year = int(match.group(1))
            if not any(lo <= year <= hi for lo, hi in ranges):
                ranges.append((year, year))
        return ranges

    def _recorded_title_kicker(target, sentence):
        """(kicker, source) from recorded facts only, or
        ("", None) when no recorded fact supports one.

        Candidates in priority order: the sentence record's era,
        its first scenario's era, the target scenario's era, the
        case bible's recorded period; then recorded section
        labels from the same sources; then a recorded place.
        Each candidate must survive corroboration against the
        beat's other recorded temporal facts (sentence dates,
        scenario date, bible period, the other era fields): a
        present-day candidate is refused when the other facts
        place the beat in the past and none supports the
        present; a dated candidate is refused when its period
        does not overlap any other recorded period."""
        scenario = target.get("scenario") or {}
        _scenarios = sentence.get("scenarios") or []
        first = (
            _scenarios[0]
            if _scenarios and isinstance(_scenarios[0], dict)
            else {}
        )
        era_fields = [
            ("sentence", sentence.get("era")),
            ("sentence_scenario", first.get("era")),
            ("scenario", scenario.get("era")),
            ("case_bible", case_bible.get("time_period")
             or case_bible.get("era")),
        ]
        # The beat's recorded temporal facts (for corroboration):
        # every era field plus the recorded dates. Values are
        # (source, text) so a candidate is never corroborated
        # solely by its own field.
        temporal_facts = list(era_fields) + [
            ("sentence_dates", value)
            for value in (sentence.get("dates") or [])
        ] + [("scenario_date", scenario.get("date"))]

        def _corroborated(candidate, source):
            others = [
                clean_text(value) for src, value in temporal_facts
                if src != source and clean_text(value or "")
            ]
            other_ranges = [
                r for value in others for r in _era_ranges(value)
            ]
            if _PRESENT_DAY_RE.search(candidate):
                past = any(hi < 1980 for _lo, hi in other_ranges)
                present = any(
                    _PRESENT_DAY_RE.search(value)
                    or any(lo >= 1980 for lo, _hi in _era_ranges(value))
                    for value in others
                )
                return not (past and not present)
            ranges = _era_ranges(candidate)
            if ranges and other_ranges:
                return any(
                    lo <= o_hi and o_lo <= hi
                    for lo, hi in ranges
                    for o_lo, o_hi in other_ranges
                )
            return True

        candidates = list(era_fields) + [
            ("sentence_scenario", first.get("section")),
            ("scenario", scenario.get("section")),
            ("case_bible", case_bible.get("section")),
            ("sentence", (sentence.get("locations") or [None])[0]),
            ("scenario", scenario.get("place")),
            ("sentence_scenario", first.get("place")),
        ]
        for source, raw in candidates:
            value = clean_text(raw or "")
            if (
                not value
                or not is_short_payload(value)
                or is_metadata_title(value)
            ):
                continue
            if _corroborated(value, source):
                return value, source
        return "", None

    def _assemble_archival_title(target):
        # A title sequence belongs to the film's opening beat
        # only — first scene, first sentence.
        _scene_numbers = [
            s.get("scene_number") for s in scenes
            if isinstance(s, dict) and s.get("scene_number") is not None
        ]
        if (
            target["sentence_index"] != 0
            or not _scene_numbers
            or target["scene_number"] != min(_scene_numbers)
        ):
            return None
        title = clean_text(
            case_bible.get("title")
            or (target.get("scenario") or {}).get("event")
            or detect_entity(target["text"]) or "")
        if not title:
            return None
        sentence = _sentences_by_key.get(
            (target["scene_number"], target["sentence_index"]), {})
        kicker, kicker_source = _recorded_title_kicker(
            target, sentence)
        scenario = target.get("scenario") or {}
        if not kicker and scenario.get("kicker_required"):
            # The request made a kicker mandatory and no recorded
            # fact supports one: decline rather than invent one.
            return None
        return {
            "title": title,
            "kicker": kicker,
            # Provenance of the kicker (which recorded field it
            # came from) rides on the engine inputs / asset
            # record — it is never drawn on the frame.
            "kicker_source": kicker_source,
            "date_label": clean_text(
                detect_date(target["text"]) or ""),
        }

    def _assemble_document_presentation(target):
        """The document-reveal unlock (plan §C F4): engine data
        ONLY when the target's scene holds a verified asset the
        evidence layer classified as a document, the file exists,
        and its provenance is recorded. Anything less returns
        None and the template keeps its recorded refusal."""
        for asset in _verified_scene_assets(target):
            if asset.get("evidence_type") != "document":
                continue
            # MG-07 (Motion Graphics audit): the presented image
            # is a photo fed into a graphic — the same
            # person-conflict check every other photo assembler
            # applies runs here too; a "document" whose title
            # depicts an unrelated named person is refused and
            # the next candidate is tried.
            if photo_person_conflict(
                asset, target.get("text") or "", case_bible
            ):
                continue
            path = _asset_file(asset)
            provenance = clean_text(
                asset.get("provider") or asset.get("source") or "")
            url = clean_text(
                asset.get("source_url") or asset.get("url") or "")
            if not path or not os.path.exists(path):
                continue
            if not provenance and not url:
                continue
            sentence = _sentences_by_key.get(
                (target["scene_number"], target["sentence_index"]), {})
            dates = sentence.get("dates") or []
            return {
                "image": path,
                # Founder ruling 2026-11-23: the presented page
                # carries no asset title and no source label —
                # provenance is recorded in document_source (and
                # the rights manifest), never drawn. A date label
                # from the sentence's own analysis is content and
                # stays.
                "date_label": clean_text(dates[0]) if dates else "",
                "document_source": {
                    "asset_id": asset.get("candidate_id")
                    or asset.get("asset_id"),
                    "source": provenance,
                    "url": url,
                    "path": path,
                },
            }
        return None

    # -----------------------------------------------------
    # Wave 2 assemblers (Motion Graphics Engine plan §C Wave 2).
    # Same contract as Wave 1: engine data built ONLY from the
    # target's recorded material — the Phase 2.3 fact table, the
    # case bible's recorded timeline, the scene's verified
    # assets, or scenario-recorded data contracts (pyramid /
    # checklist / stages / molecule / density points / scene
    # markers / object labels / expansion states / before-after
    # pair — the layers precedent from Wave 1's schematics) —
    # or None, and the template stays unproposed for the target.
    # -----------------------------------------------------

    def _assemble_bubble_chart(target):
        grouped = _fact_quantities()
        for _unit, facts in grouped.items():
            if len(facts) >= 3:
                return {
                    "items": [
                        {"label": f["date"] or f["display"],
                         "value": f["value"], "display": f["display"]}
                        for f in facts[:6]
                    ],
                    "title": short_title_for(target),
                }
        points = _series_from_text(target["text"])
        if points:
            return {"items": points[:6],
                    "title": short_title_for(target)}
        return None

    def _assemble_population_pyramid(target):
        scenario = target.get("scenario") or {}
        pyramid = scenario.get("pyramid")
        groups = None
        left_label = right_label = ""
        if isinstance(pyramid, dict):
            groups = pyramid.get("groups")
            left_label = clean_text(pyramid.get("left_label") or "")
            right_label = clean_text(pyramid.get("right_label") or "")
        elif isinstance(pyramid, list):
            groups = pyramid
        if not isinstance(groups, list):
            return None
        valid = [
            g for g in groups
            if isinstance(g, dict)
            and isinstance(g.get("left"), (int, float))
            and isinstance(g.get("right"), (int, float))
            and clean_text(g.get("label") or "")
        ]
        if len(valid) < 2:
            return None
        return {
            "groups": valid[:9],
            "left_label": left_label,
            "right_label": right_label,
            "title": short_title_for(target),
        }

    def _assemble_animated_dashboard(target):
        items = extract_comparison(target["text"]) or []
        stats = [
            {"label": clean_text(i.get("label") or ""),
             "value": i.get("value"), "display": i.get("display") or ""}
            for i in items
            if isinstance(i.get("value"), (int, float))
            and clean_text(i.get("label") or "")
        ]
        if len(stats) >= 3:
            return {"stats": stats[:6],
                    "title": short_title_for(target)}
        # Fact-table fallback: one card per DISTINCT recorded
        # unit (its first recorded value) — never the same unit
        # twice, which would present one measure as several.
        grouped = _fact_quantities()
        distinct = [
            {"label": unit, "value": facts[0]["value"],
             "display": facts[0]["display"]}
            for unit, facts in grouped.items() if facts
        ]
        if len(distinct) >= 3:
            data = {"stats": distinct[:6],
                    "title": short_title_for(target)}
            for _unit, facts in grouped.items():
                if len(facts) >= 3:
                    data["series"] = [
                        {"label": f["date"] or f["display"],
                         "value": f["value"]}
                        for f in facts[:8]
                    ]
                    break
            return data
        return None

    def _assemble_checklist_graphic(target):
        scenario = target.get("scenario") or {}
        items = scenario.get("checklist")
        if not isinstance(items, list):
            return None
        valid = [
            i for i in items
            if isinstance(i, dict)
            and clean_text(i.get("label") or i.get("text") or "")
        ]
        if not valid:
            return None
        return {"items": valid[:7],
                "title": short_title_for(target)}

    def _assemble_territory_expansion(target):
        scenario = target.get("scenario") or {}
        expansion = scenario.get("expansion")
        states = expansion.get("states") if isinstance(
            expansion, dict) else None
        if not isinstance(states, list) or len(states) < 2:
            return None
        return {"states": states,
                "title": short_title_for(target)}

    def _assemble_satellite_zoom(target):
        _asset, path = _first_scene_photo(target)
        if path is None:
            return None
        scenario = target.get("scenario") or {}
        place = clean_text(
            scenario.get("place") or scenario.get("location") or "")
        focus = scenario.get("focus")
        if not place and not focus:
            return None
        data = {
            "image": path,
            "label": place or clean_text(scenario.get("event") or ""),
        }
        if isinstance(focus, (list, tuple)) and len(focus) >= 2:
            data["focus"] = list(focus)
            data["focus_source"] = "recorded"
        return data

    def _assemble_heatmap(target):
        scenario = target.get("scenario") or {}
        points = scenario.get("density_points")
        if not isinstance(points, list) or len(points) < 3:
            return None
        data = {
            "points": points,
            "title": short_title_for(target),
            "weight_label": clean_text(
                scenario.get("density_label") or ""),
        }
        # Optional base regions from the target's recorded place,
        # resolved EXACTLY against the geography dataset (the
        # dataset survives the P0 removal; only the map RENDERERS
        # were removed). An unresolved place adds no regions —
        # geography is never invented.
        _place = clean_text(
            scenario.get("place")
            or scenario.get("location")
            or ""
        )
        if not _place:
            _sentence = _sentences_by_key.get(
                (target["scene_number"], target["sentence_index"]), {}
            )
            _locations = _sentence.get("locations") or []
            _place = clean_text(_locations[0] if _locations else "")
        if _place:
            try:
                from services.motion_engine import geography

                _match = next(
                    (n for n in geography.country_names()
                     if n.lower() == _place.lower()),
                    None,
                )
            except Exception:
                _match = None
            if _match is not None:
                data["regions"] = [
                    {"country": _match, "label": _match}
                ]
        return data

    def _assemble_classified_file(target):
        # The classified presentation rides on the SAME genuine
        # document asset as document_presentation — no asset, no
        # data, and the document gate's refusal is recorded.
        data = _assemble_document_presentation(target)
        if data is None:
            return None
        data = dict(data)
        data["variant"] = "classified"
        return data

    def _assemble_newspaper_wall(target):
        articles = []
        for asset in _verified_scene_assets(target):
            title = clean_text(
                asset.get("title") or asset.get("asset_title") or "")
            # Founder ruling 2026-11-23: same as the montage —
            # file-name titles are not headlines, and no source
            # label is ever attached to an article card.
            # MG-06-residual: short-payload gate as in the
            # montage — archive-title prose is not a headline.
            if not title or is_metadata_title(title) \
                    or not is_short_payload(title):
                continue
            articles.append({
                "headline": title,
                "date": clean_text(
                    asset.get("date") or asset.get("published") or ""),
            })
            if len(articles) >= 6:
                break
        if len(articles) < 3:
            return None
        return {"articles": articles,
                "title": short_title_for(target)}

    def _assemble_crime_scene_diagram(target):
        scenario = target.get("scenario") or {}
        markers = scenario.get("scene_markers")
        if not isinstance(markers, list) or not markers:
            return None
        return {
            "markers": markers,
            "rooms": scenario.get("rooms") or [],
            "title": clean_text(scenario.get("event") or "")
            or short_title_for(target),
        }

    def _assemble_construction_sequence(target):
        scenario = target.get("scenario") or {}
        stages = scenario.get("stages")
        if not isinstance(stages, list) or len(stages) < 2:
            return None
        return {"stages": stages,
                "title": short_title_for(target)}

    def _assemble_molecular_diagram(target):
        scenario = target.get("scenario") or {}
        molecule = scenario.get("molecule")
        if not isinstance(molecule, dict):
            return None
        atoms = molecule.get("atoms")
        if not isinstance(atoms, list) or len(atoms) < 2:
            return None
        return {
            "molecule": molecule,
            "title": clean_text(molecule.get("name") or "")
            or short_title_for(target),
        }

    def _assemble_hud_overlay(target):
        _asset, path = _first_scene_photo(target)
        if path is None:
            return None
        readouts = []
        number = target.get("sentence_number")
        # UV-01/UV-12: the shared intake — production dict shape
        # unwrapped, conflicted facts excluded, unit derived from
        # the display when the fact records none.
        for fact in _usable_quantity_facts():
            if number is not None and fact.get(
                    "sentence_number") not in (None, number):
                continue
            display = clean_text(fact.get("display") or "")
            _value, unit = _quantity_value_unit(fact)
            # No placeholder labels: a readout without a real
            # unit label is not presented (the historical "VALUE"
            # fallback burned a placeholder word into the frame).
            if display and unit:
                readouts.append({
                    "label": unit,
                    "value": display,
                })
        if not readouts:
            for point in _series_from_text(target["text"]) or []:
                # MG-08: a point's label IS its display (value) —
                # a readout named by its own value is meaningless;
                # name it by the quantity's unit instead, and skip
                # points that carry neither.
                label = clean_text(point.get("label") or "")
                if label and label == clean_text(
                    point.get("display") or ""
                ):
                    label = clean_text(point.get("unit") or "")
                if label and point.get("display"):
                    readouts.append({
                        "label": label,
                        "value": point.get("display") or "",
                    })
        readouts = [r for r in readouts if r["value"]][:5]
        if not readouts:
            return None
        return {"photo": path, "readouts": readouts,
                "title": short_title_for(target)}

    def _assemble_holographic_panel(target):
        items = []
        for entry in extract_comparison(target["text"]) or []:
            label = clean_text(entry.get("label") or "")
            display = clean_text(entry.get("display") or "")
            if label and display:
                items.append({"label": label, "value": display})
        if len(items) < 2:
            grouped = _fact_quantities()
            items = [
                {"label": unit, "value": facts[0]["display"]}
                for unit, facts in grouped.items()
                if facts and facts[0]["display"]
            ]
        if len(items) < 2:
            return None
        return {"items": items[:6],
                "title": short_title_for(target)}

    def _assemble_before_after(target):
        scenario = target.get("scenario") or {}
        before = scenario.get("before_image")
        after = scenario.get("after_image")
        if not before or not after:
            return None
        if not (os.path.exists(str(before))
                and os.path.exists(str(after))):
            return None
        return {
            "before": str(before), "after": str(after),
            "before_label": clean_text(
                scenario.get("before_label") or ""),
            "after_label": clean_text(scenario.get("after_label") or ""),
            "title": short_title_for(target),
        }

    def _assemble_layered_reveal(target):
        photos = []
        for asset in _verified_scene_assets(target):
            if photo_person_conflict(
                asset, target.get("text") or "", case_bible
            ):
                continue
            path = _asset_file(asset)
            if path and os.path.exists(path):
                photos.append({
                    "path": str(path),
                    # Founder ruling 2026-11-23: photo captions
                    # are never the asset's recorded title (raw
                    # file metadata); the layer label stays blank.
                    "caption": "",
                })
            if len(photos) >= 4:
                break
        if len(photos) < 2:
            return None
        return {"photos": photos,
                "title": short_title_for(target)}

    def _assemble_object_labels(target):
        scenario = target.get("scenario") or {}
        labels = scenario.get("object_labels")
        if not isinstance(labels, list) or not labels:
            return None
        _asset, path = _first_scene_photo(target)
        if path is None:
            return None
        return {
            "photo": path, "labels": labels,
            "title": clean_text(scenario.get("event") or "")
            or short_title_for(target),
        }

    #: Assemblers the undirected chain never appends on its own —
    #: these templates are proposed by the Visual Director (or
    #: not at all), never volunteered by the coverage chain.
    _DIRECTOR_ONLY_ASSEMBLERS = {
        "archival_title",
        # Wave 2: these templates draw on production-global pools
        # (the whole case-bible timeline / fact table) or a gated
        # document asset, not on data belonging to the target beat
        # itself. In the undirected chain they would fire on ANY
        # unserved beat and pre-empt the coverage guarantee's
        # floor (the Wave 1 T5 tests pin that floor), so — like
        # archival_title — they are reachable only through a
        # Director manifest entry, where the candidacy conditions
        # in visual_director are their recorded-evidence gate.
        "bubble_chart", "animated_dashboard",
        "holographic_panel", "classified_file",
        # GitHub Skills Programme (2026-11-25): the Manim engine's
        # templates are Director-only for the same reason, plus a
        # cost discipline the others do not need — a Manim render
        # costs ~15-25 s of CPU, so it may only be spent where the
        # Director's recorded-evidence candidacy (a real series /
        # comparison on the record) justifies it, never as an
        # undirected fallback for an unserved beat.
        "manim_line_chart", "manim_bar_chart",
    }

    #: Template name -> assembler. Templates NOT listed here have
    #: no production assembler (see the Phase 6 proposal's audit
    #: table for the per-template disposition).
    _ASSEMBLERS = {
        # Wave 1 (Motion Graphics Engine plan §C).
        "line_chart": _assemble_line_chart,
        # GitHub Skills Programme: the Manim line chart consumes
        # the SAME assembled recorded series as the PIL line chart
        # — one data contract, two render engines.
        "manim_line_chart": _assemble_line_chart,
        # GitHub Skills Programme: the matplotlib line chart rides
        # the same assembled series; unlike Manim it is cheap
        # (in-process, seconds), so it is NOT Director-only — it
        # may also serve the undirected fallback chain.
        "mpl_line_chart": _assemble_line_chart,
        "pie_chart": _assemble_pie_chart,
        "exploded_view": _section_variant("exploded"),
        "mechanical_cutaway": _section_variant("cutaway"),
        "blueprint_section": _section_variant("blueprint"),
        "newspaper_montage": _assemble_newspaper_montage,
        "profile_reveal": _assemble_profile_reveal,
        "text_overlay": _assemble_text_overlay,
        "text_highlight": _assemble_text_highlight,
        "archival_title": _assemble_archival_title,
        "document_presentation": _assemble_document_presentation,
        # Wave 2 (Motion Graphics Engine plan §C Wave 2).
        "bubble_chart": _assemble_bubble_chart,
        "population_pyramid": _assemble_population_pyramid,
        "animated_dashboard": _assemble_animated_dashboard,
        "checklist_graphic": _assemble_checklist_graphic,
        "territory_expansion": _assemble_territory_expansion,
        "satellite_zoom": _assemble_satellite_zoom,
        "heatmap": _assemble_heatmap,
        "classified_file": _assemble_classified_file,
        "newspaper_wall": _assemble_newspaper_wall,
        "crime_scene_diagram": _assemble_crime_scene_diagram,
        "construction_sequence": _assemble_construction_sequence,
        "molecular_diagram": _assemble_molecular_diagram,
        "hud_overlay": _assemble_hud_overlay,
        "holographic_panel": _assemble_holographic_panel,
        "before_after": _assemble_before_after,
        "layered_reveal": _assemble_layered_reveal,
        "object_labels": _assemble_object_labels,
    }

    def _engine_duration_for(target):
        """MG-11 (Motion Graphics audit): an engine graphic
        renders AT its sentence slot's length — the historical
        fixed 3.0s render left longer slots to the renderer's
        tpad freeze (a 3s closing card frozen across a 10.275s
        slot). Templates are progress-based, so rendering at
        slot length fills the slot with the designed animation
        and holds the settled final state by design. The slot
        comes from the narration part's measured duration
        (stamped by the caller / jobs); clamped to the sane
        graphic band, defaulting to the historical 3.0s when no
        slot is recorded."""
        slot = target.get("slot_duration_seconds")
        try:
            slot = float(slot)
        except (TypeError, ValueError):
            return 3.0
        if slot <= 0:
            return 3.0
        return min(10.0, max(2.5, slot))

    def _make_engine_graphic(target, template_name, engine_data,
                             style="editorial", duration=None):
        """Stage 3E integration: render one Motion Engine template
        for a target and register it with the SAME asset shape and
        honesty stamps as the ffmpeg/PIL graphics.

        Returns (True, None, "") on success, else (False, reason,
        detail): an unknown template, a template refusal (its own
        reason preserved in `detail`), a template exception and a
        validation failure are DISTINCT outcomes (MG-08).

        Phase 2 (MG-03): the COMPLETE template inputs are recorded
        on the asset record (`engine_inputs`, a deepcopy) together
        with the style and a full `graphic_recipe`, so the editor
        can regenerate this graphic without reconstructing anything
        from the script. Before this, only an input SUMMARY
        (engine_metadata) survived and the raw data was discarded.
        """
        if duration is None:
            duration = _engine_duration_for(target)
        if template_name in (
            "document_presentation", "classified_file",
        ) and not (
            isinstance(engine_data, dict)
            and engine_data.get("document_source")
        ):
            # Phase 6 founder rule, Wave 1 unlock: the template
            # renders ONLY from a genuine retrieved document asset
            # whose provenance the assembler recorded in
            # engine_data["document_source"]. Every attempt without
            # one is refused explicitly and typed — the chain never
            # feeds it fabricated document content, and a manifest
            # entry naming it without a source is refused the
            # same way (AM-010 discipline unchanged). Wave 2: the
            # classified_file variant rides under the same gate —
            # a redacted presentation of a fabricated document
            # would be worse than none.
            return (
                False,
                "document_presentation_disabled",
                f"{template_name} requires a genuine document "
                "asset with recorded provenance",
            )
        try:
            from services.motion_engine import templates as _engine

            template_fn = _engine.TEMPLATES[template_name]
        except Exception as error:
            print(f"MOTION GRAPHIC SKIPPED (motion_engine): {error}")
            return False, "unsupported_template", str(error)
        path = (
            output_dir
            / f"motion_graphic_{template_name}_{index + 1:03d}.mp4"
        )
        try:
            rendered, metadata = template_fn(
                engine_data, style, path, duration=duration,
                sentence_index=target["sentence_index"],
                scene_number=target["scene_number"],
            )
        except Exception as error:
            print(f"MOTION GRAPHIC SKIPPED (motion_engine {template_name}): {error}")
            return False, "rendering_failure", str(error)
        if not rendered:
            _refusal = (metadata or {}).get("reason", "")
            if _refusal == "label_does_not_fit":
                # Phase 5 (X3): a label that does not fit its box
                # refuses the graphic with its own typed reason —
                # it is never rendered clipped, and the refusal is
                # distinct from missing data.
                return (
                    False,
                    "label_does_not_fit",
                    (metadata or {}).get("detail", "") or _refusal,
                )
            return (
                False,
                "missing_required_input",
                _refusal,
            )
        record = register_asset(
            path, template_name, f"motion_engine_{template_name}",
            duration, target["text"], target["text"],
            recipe=_recipe(
                "motion_engine", "motion_engine",
                copy.deepcopy(engine_data),
                duration, f"motion_engine_{template_name}",
                scene_number=target["scene_number"],
                sentence_index=target["sentence_index"],
                sentence_number=(
                    target["sentence_number"] or target["sentence_index"] + 1
                ),
                template=template_name,
                style=style,
                source_references=[{
                    "type": "script_sentence",
                    "scene_number": target["scene_number"],
                    "sentence_number": (
                        target["sentence_number"]
                        or target["sentence_index"] + 1
                    ),
                }],
            ),
        )
        if record is None:
            failure = graphic_failures[-1]
            return False, failure["reason"], failure["detail"]
        record["scene_number"] = target["scene_number"]
        record["sentence_index"] = target["sentence_index"]
        record["sentence_number"] = (
            target["sentence_number"] or target["sentence_index"] + 1
        )
        record["targeted"] = True
        record["family"] = "motion_engine"
        record["template"] = template_name
        record["style"] = _record_style(style)
        record["engine_inputs"] = copy.deepcopy(engine_data)
        record["engine_metadata"] = metadata
        record["delivered"] = True
        # Phase 6: a manifest-driven graphic carries its manifest
        # identity + theme onto the asset record (and from there
        # onto the timeline item), so the rendered film stays
        # traceable to the Director's decision that ordered it.
        if target.get("manifest_id"):
            record["manifest_id"] = target["manifest_id"]
        _record_theme = target.get("theme") or _production_theme_name
        if _record_theme:
            record["theme"] = _record_theme
        return True, None, ""

    covered = set()
    targeted_generated = 0

    # Stage 5A: genre-pack influence on the targeted chain. The
    # pack (services.genre_packs data) contributes an ordered motif
    # preference list and a motion-engine style family. With no
    # pack, the chain keeps its exact Stage 3 behaviour — same
    # candidate sets, same order, same per-template styles.
    _pack = genre_pack if isinstance(genre_pack, dict) else {}
    _pack_id = _pack.get("id")
    _motifs = [m for m in (_pack.get("graphics_motifs") or []) if isinstance(m, str)]
    _pack_style = _pack.get("style_family") or None

    def _style_for(default):
        return _pack_style or default

    def _order_by_motif(candidates):
        """Stable-sort (name, data, style) candidates by the pack's
        motif preference; unlisted templates keep their relative
        order after the listed ones."""
        if not _motifs:
            return candidates

        def _key(item):
            try:
                return (0, _motifs.index(item[0]))
            except ValueError:
                return (1, 0)

        return sorted(candidates, key=_key)

    def _badges_from_items(items, limit=3):
        return [
            {
                "value": item.get("display") or f"{item.get('value'):g}",
                "label": item.get("label") or "",
            }
            for item in items[:limit]
        ]

    # Stage 3D targeted fallback chain. Each target is routed, in
    # order: event schematic -> Motion Engine templates ->
    # recorded skip. (P0 removal, 2026-11-27: the stat, process
    # and chronology/timeline stages were removed completely.)
    # Every outcome (produced or skipped) is recorded in the
    # manifest with family + reason (target_outcomes). Variety:
    # the same family is not chosen for two consecutive targets
    # when an alternative family fits — an alternative that fits
    # is tried first, and the primary family remains the fallback.
    _last_family = None
    # Wave 1 (§D.3): the no-same-family-twice rule extended to
    # template level within the engine family — the last engine
    # template that actually rendered, so the next target's
    # chain-ordered candidates rotate away from repeating it.
    _last_engine_template = None

    def _record_outcome(target, outcome, family, reason):
        target_outcomes.append({
            "scene_number": target["scene_number"],
            "sentence_index": target["sentence_index"],
            "sentence_number": target["sentence_number"],
            "outcome": outcome,
            "family": family,
            "reason": reason,
            # Phase 6: the Director decision this target answers
            # to, when it came from the graphics manifest.
            "manifest_id": target.get("manifest_id"),
        })

    def _family_permitted(target, family):
        """Phase 6 manifest gating: a manifest target's recorded
        family_candidates restrict which chain families may run
        for it. Legacy targets (no family list) are unrestricted,
        exactly as before."""
        families = target.get("family_candidates")
        return families is None or family in families

    def _feature_data(name, target, comparison, percent, text):
        """Engine data for one template name from the target's own
        recorded material: feature builders for the four historical
        templates, the Phase 6 assemblers for the rest. None when
        the material does not support the template."""
        if name == "document_presentation":
            # Wave 1 unlock: a genuine retrieved document asset
            # (verified, classified document, provenance recorded)
            # assembles real data and the template renders. With
            # no such asset the attempt is still made (with empty
            # data) so _make_engine_graphic's explicit disabled
            # refusal is what gets recorded — never a fabricated
            # document (AM-010 discipline unchanged).
            return _assemble_document_presentation(target) or {}
        if name == "classified_file":
            # Wave 2: the classified variant rides on the same
            # genuine document asset and the same gate — with no
            # asset the attempt is made with empty data so the
            # gate's refusal is recorded, exactly as above.
            return _assemble_classified_file(target) or {}
        if name == "bar_chart" and comparison:
            return {"items": comparison, "title": short_title_for(target)}
        if name == "manim_bar_chart" and comparison:
            # GitHub Skills Programme: identical recorded comparison
            # data as the PIL bar chart — the Manim template itself
            # refuses under 3 items, before any render is spent.
            return {"items": comparison, "title": short_title_for(target)}
        if name == "mpl_bar_chart" and comparison:
            # GitHub Skills Programme: the matplotlib bar chart
            # rides the same recorded comparison data.
            return {"items": comparison, "title": short_title_for(target)}
        if name == "stat_comparison" and comparison:
            return {"items": comparison, "title": short_title_for(target)}
        if name == "data_gauge" and percent is not None:
            return {
                "percent": percent, "label": short_title_for(target, text),
            }
        if name == "ring_badges":
            if comparison:
                return {"badges": _badges_from_items(comparison)}
            if percent is not None:
                return {
                    "badges": [{
                        "value": f"{percent:g}%",
                        "label": short_title_for(target, text),
                    }],
                }
            return None
        assemble = _ASSEMBLERS.get(name)
        if assemble is not None:
            return assemble(target)
        return None

    def _terminal_facts(target, text):
        """Gate 2 (Blocker 2): the facts actually available for one
        target sentence, gathered from every recorded source — the
        sentence text itself, its analysis record (entities /
        locations / dates), its scenario, and the case bible
        (entities, timeline dates, title). Since the P0 rewiring
        these facts no longer feed a terminal sweep; they are
        recorded on the beat's terminal_graphics_decline as the
        evidence of what was available when every content family
        declined. Nothing is invented: a fact enters the record
        only when a source recorded it, and every fact must pass
        the short-payload bar (a designed element — never
        sentence text). Sentence-local sources outrank
        bible-global ones in every list."""
        sentence = _sentences_by_key.get(
            (target["scene_number"], target["sentence_index"])
        ) or {}
        scenario = target.get("scenario") or {}
        facts = {"names": [], "places": [], "dates": [], "labels": []}
        # Provenance per fact value (lower-cased): "beat" when the
        # value is recorded for THIS sentence (its text, its
        # analysis record, or its scenario), "bible" when it comes
        # only from production-global sources (case-bible entities,
        # timeline). Provenance rides on the decline record so a
        # reader can tell beat facts from production-global ones.
        sources = {}

        def _add(kind, value, source="beat"):
            value = clean_text(value or "")
            if (
                value
                and is_short_payload(value)
                and not is_metadata_title(value)
            ):
                sources.setdefault(value.lower(), set()).add(source)
                if value not in facts[kind]:
                    facts[kind].append(value)

        date = detect_date(text)
        if date:
            _add("dates", date)
        for value in sentence.get("dates") or []:
            _add("dates", value)
        _add("dates", scenario.get("era"))
        _add("dates", scenario.get("date"))

        names, places = labels_from_text(text)
        for value in names:
            _add("names", value)
        for value in places:
            _add("places", value)
        for entity in sentence.get("entities") or []:
            if isinstance(entity, dict):
                _add(
                    "names",
                    entity.get("name") or entity.get("canonical"),
                )
            else:
                _add("names", entity)
        for value in sentence.get("locations") or []:
            _add("places", value)
        _add("places", scenario.get("place"))
        _add("labels", scenario.get("event"))

        for entity in (case_bible or {}).get("entities") or []:
            if not isinstance(entity, dict):
                continue
            kind = (
                "places"
                if str(entity.get("type") or "").lower()
                in ("place", "location")
                else "names"
            )
            _add(
                kind, entity.get("canonical") or entity.get("name"),
                source="bible",
            )
        for event in timeline_events or []:
            if isinstance(event, dict):
                _add("dates", event.get("when"), source="bible")

        facts["title"] = short_title_for(target, text)
        # facts["title"] is a BEAT title (the scenario label or the
        # sentence's own first entity/place label, per
        # short_title_for) — never the production title — so record
        # its provenance as such; it must not act as a title
        # exclusion in the sweep.
        if facts["title"]:
            sources.setdefault(
                facts["title"].lower(), set()).add("beat")
        _selected = short_payload_for(text, sentence_record=sentence)
        if _selected:
            # Selected from the sentence text / its own record.
            sources.setdefault(_selected.lower(), set()).add("beat")
        facts["payload"] = (
            _selected
            or (facts["names"] or [None])[0]
            or (facts["dates"] or [None])[0]
            or (facts["places"] or [None])[0]
            or (facts["labels"] or [None])[0]
            or ""
        )
        facts["_sources"] = {
            value: sorted(kinds) for value, kinds in sources.items()
        }
        return facts

    # Gate 4 (B3): repetition guard + quality-defect record.
    # Identical graphic content (same template/family + same
    # designed payload) must not silently serve multiple
    # sentences — Gate 4 found the identical "steel hanger rods"
    # closing card on five sentences. The guard tracks every
    # payload assigned in this build, and any duplication that
    # still occurs is stamped on the record and listed in
    # graphic_quality_defects — explicit data, never silent.
    _assigned_payloads = {}
    _assigned_payload_texts = set()
    graphic_quality_defects = []

    def _payload_key_for_record(record):
        recipe = record.get("graphic_recipe") or {}
        inputs = recipe.get("inputs") or {}
        family = record.get("family") or record.get("graphic_type")
        template = record.get("template") or recipe.get("template")
        payload = {
            k: v for k, v in inputs.items()
            if k not in ("kicker", "fact_basis", "style")
        }
        try:
            blob = json.dumps(payload, sort_keys=True, default=str)
        except (TypeError, ValueError):
            blob = str(payload)
        return f"{family}/{template}:{blob.lower()}"

    def _register_assigned_payload(record):
        """Register one produced record's payload; on a duplicate,
        stamp the record + defect list and return the defect."""
        if record is None:
            return None
        key = _payload_key_for_record(record)
        _inputs = (record.get("graphic_recipe") or {}).get("inputs") or {}
        for _field in ("quote", "headline"):
            if _inputs.get(_field):
                _assigned_payload_texts.add(
                    clean_text(_inputs[_field]).lower())
        first = _assigned_payloads.get(key)
        if first is None:
            _assigned_payloads[key] = record.get("sentence_number")
            return None
        defect = {
            "type": "duplicate_graphic_payload",
            "sentence_number": record.get("sentence_number"),
            "duplicate_of_sentence_number": first,
            "payload_key": key,
        }
        record["quality_defect"] = "duplicate_graphic_payload"
        record["duplicate_of_sentence_number"] = first
        graphic_quality_defects.append(defect)
        return defect

    # MG-11: stamp each target with its sentence slot's measured
    # duration (narration parts, keyed by sentence_number) so
    # engine graphics render at slot length. A target already
    # carrying a slot keeps it.
    if isinstance(slot_durations, dict):
        for target in target_list:
            if target.get("slot_duration_seconds") is None:
                _slot = slot_durations.get(target.get("sentence_number"))
                if _slot is not None:
                    target["slot_duration_seconds"] = _slot

    # Targeted graphics FIRST — they answer a recorded coverage
    # failure, so the untargeted cap must never block them.
    for target in target_list:
        key = (target["scene_number"], target["sentence_index"])
        if not target["text"]:
            target_skips.append(
                {
                    "scene_number": target["scene_number"],
                    "sentence_index": target["sentence_index"],
                    "sentence_number": target["sentence_number"],
                    "reason": "no_sentence_text",
                }
            )
            _record_outcome(target, "skipped", None, "no_sentence_text")
            continue

        text = target["text"]
        comparison = extract_comparison(text)
        percent = _percent_value(text)
        lowered = text.lower()
        has_document = any(m in lowered for m in _DOCUMENT_MARKERS)

        def _engine_alternative():
            """An engine template that fits this sentence, or None.
            Used by the variety rule and the engine stage itself.
            With a genre pack, every fitting template is a candidate
            and the pack's motif order picks the first; without one,
            the Stage 3 first-fit order applies unchanged."""
            if _pack_id is None:
                if percent is not None:
                    return ("data_gauge", {
                        "percent": percent, "label": short_title_for(target, text),
                    }, "editorial")
                if comparison:
                    return ("stat_comparison", {
                        "items": comparison, "title": short_title_for(target),
                    }, "editorial")
                # MG-09: document_presentation is NOT offered for a
                # bare document-marker sentence. The chain has no
                # genuine document asset — feeding it the narration
                # sentence as a fabricated "document" body presented
                # script text as a source document. The refusal is
                # recorded as missing_document_source when the chain
                # exhausts (see the skip-reason priority below). The
                # engine template itself is unchanged and remains
                # available to callers with a genuine document.
                return None
            candidates = []
            if percent is not None:
                candidates.append(("data_gauge", {
                    "percent": percent, "label": short_title_for(target, text),
                }, _style_for("editorial")))
                candidates.append(("ring_badges", {
                    "badges": [{"value": f"{percent:g}%", "label": short_title_for(target, text)}],
                }, _style_for("editorial")))
            if comparison:
                candidates.append(("bar_chart", {
                    "items": comparison, "title": short_title_for(target),
                }, _style_for("editorial")))
                candidates.append(("stat_comparison", {
                    "items": comparison, "title": short_title_for(target),
                }, _style_for("editorial")))
                candidates.append(("ring_badges", {
                    "badges": _badges_from_items(comparison),
                }, _style_for("editorial")))
            # MG-09: no document_presentation candidate — see the
            # no-pack branch above (missing_document_source).
            if not candidates:
                return None
            return _order_by_motif(candidates)[0]

        produced_family = None
        produced_reason = ""

        _engine_reason = None
        _engine_detail = ""
        _terminal_decline = None

        # (P0 removal, 2026-11-27: chain stage 1 — the native
        # stat family — and its variety pre-pass were removed
        # completely: renderer, extraction, the max_stats cap and
        # the recipe dispatch. Genuine statistics are served by
        # the surviving engine chart family from recorded data,
        # with years/dates excluded from statistic candidacy.)

        # (P0 removal, 2026-11-27: chain stage 3 — the native
        # process family — was removed completely: renderer,
        # anchor logic and recipe dispatch.)

        # 4) Event schematic (conservative trigger, motif-gated).
        _schematic_reason = None
        _schematic_detail = ""
        if produced_family is None and _family_permitted(
            target, "event_schematic"
        ):
            _schematic_ok, _schematic_reason, _schematic_detail = (
                _make_schematic(target)
            )
            if _schematic_ok:
                produced_family = "event_schematic"
                produced_reason = "schematic"

        # 5) Motion Engine templates. Order inside the stage:
        # stat_comparison, document_presentation, then data_gauge
        # (gauge fires here only for a percentage the stat path
        # did not take — e.g. a comparison sentence, or a stat
        # render failure).
        if produced_family is None:
            _candidates = []
            _manifest_restricted = (
                target.get("manifest_id")
                and target.get("template_candidates") is not None
            )
            if _manifest_restricted:
                # Phase 6 manifest mode: ONLY the Visual Director's
                # recorded candidates, in the Director's order,
                # with data from the feature builders / assemblers,
                # styled by the entry's theme.
                for _name in target["template_candidates"]:
                    _data = _feature_data(
                        _name, target, comparison, percent, text
                    )
                    if _data is not None:
                        _candidates.append((
                            _name, _data,
                            _style_for_target(
                                target, _style_for("editorial")
                            ),
                        ))
            elif _pack_id is not None:
                # Stage 5A/5B: pack-ordered candidates, including
                # the v2 bar_chart (multi-value comparisons) and
                # ring_badges; styles come from the pack's family.
                if comparison:
                    _candidates.append(("bar_chart", {
                        "items": comparison, "title": short_title_for(target),
                    }, _style_for("editorial")))
                    _candidates.append(("stat_comparison", {
                        "items": comparison, "title": short_title_for(target),
                    }, _style_for("editorial")))
                    _candidates.append(("ring_badges", {
                        "badges": _badges_from_items(comparison),
                    }, _style_for("editorial")))
                # MG-09: no document_presentation candidate (see
                # _engine_alternative — missing_document_source).
                if percent is not None:
                    _candidates.append(("data_gauge", {
                        "percent": percent, "label": short_title_for(target, text),
                    }, _style_for("editorial")))
                    _candidates.append(("ring_badges", {
                        "badges": [{"value": f"{percent:g}%", "label": short_title_for(target, text)}],
                    }, _style_for("editorial")))
            else:
                if comparison:
                    _candidates.append(("stat_comparison", {
                        "items": comparison, "title": short_title_for(target),
                    }, "editorial"))
                # MG-09: no document_presentation candidate (see
                # _engine_alternative — missing_document_source).
                if percent is not None:
                    _candidates.append(("data_gauge", {
                        "percent": percent, "label": short_title_for(target, text),
                    }, "editorial"))
            if not _manifest_restricted:
                # Phase 6 (AM-010): the assembled templates join
                # the legacy candidate list AFTER the historical
                # candidates — a target the historical chain already
                # serves is served exactly as before; the assemblers
                # only reach targets nothing else fitted.
                for _name, _assemble in _ASSEMBLERS.items():
                    if _name in _DIRECTOR_ONLY_ASSEMBLERS:
                        # Reachable only through a Director
                        # manifest entry (via _feature_data): a
                        # title sequence is an editorial decision,
                        # not a coverage answer — the undirected
                        # chain's floor serves starved beats.
                        continue
                    if any(c[0] == _name for c in _candidates):
                        continue
                    _data = _assemble(target)
                    if _data is not None:
                        _candidates.append((
                            _name, _data, _style_for("editorial"),
                        ))
                if _pack_id is not None:
                    _candidates = _order_by_motif(_candidates)
            if (
                not _manifest_restricted
                and _last_engine_template is not None
                and len(_candidates) > 1
                and _candidates[0][0] == _last_engine_template
            ):
                # Template-level variety (§D.3): the previous
                # target's engine template leads this list too —
                # rotate so a different fitting template is tried
                # first. The repeated template stays in the list
                # as a later fallback; nothing is forbidden.
                # Manifest mode is exempt: the Director's recorded
                # order is authoritative there.
                _candidates = _candidates[1:] + _candidates[:1]
            for _name, _data, _style in _candidates:
                _engine_ok, _engine_reason, _engine_detail = (
                    _make_engine_graphic(
                        target, _name, _data, style=_style
                    )
                )
                if _engine_ok:
                    produced_family = "motion_engine"
                    produced_reason = f"motion_engine:{_name}"
                    _last_engine_template = _name
                    break

        # 6) T5 COVERAGE GUARANTEE — REWIRED (P0 removal,
        # 2026-11-27). Every target in this chain IS a coverage
        # requirement — a sentence that exhausted retrieval and
        # the footage tiers with no visual. The historical
        # guarantee manufactured a floor graphic for every such
        # beat (timeline recap -> outcome path -> the Gate 2
        # terminal sweep's closing card, with archival_title as
        # its soft second). The founder's P0 ruling removed those
        # graphics completely, and the guarantee must NOT be
        # re-pointed at any surviving template to recreate a
        # floor. Its terminal outcomes are now exactly two:
        #
        # (a) FOOTAGE FALLBACK — the beat is left unserved by
        #     graphics here, so the jobs/tier-ladder side serves
        #     it with footage (B-roll / atmospheric tiers or an
        #     extended hold) like any other uncovered beat; or
        # (b) a typed TERMINAL_GRAPHICS_DECLINE, recorded on the
        #     skip and outcome records below with the families
        #     the chain attempted and the facts that were
        #     available — an honest stated absence, never a
        #     manufactured graphic.
        #
        # archival_title survives ONLY as a Director-proposed
        # title treatment in the engine stage above (a manifest
        # entry naming it); it is never attempted here as a
        # floor. The Director's candidate lists remain a
        # preference order for the content families, not a veto.
        if produced_family is None:
            _facts = _terminal_facts(target, text)
            _attempted = []
            for _stage_family, _stage_reason in (
                ("event_schematic", _schematic_reason),
                ("motion_engine", _engine_reason),
            ):
                if _stage_reason is not None:
                    _attempted.append(_stage_family)
            _terminal_decline = {
                "reason": "terminal_graphics_decline",
                "attempted_families": _attempted,
                "facts_available": sorted(
                    kind
                    for kind, values in _facts.items()
                    if values and not kind.startswith("_")
                ),
            }

        if produced_family is not None:
            covered.add(key)
            targeted_generated += 1
            _last_family = produced_family
            # Gate 4 (B3): repetition guard + assigned-family
            # honesty. Find this beat's produced record, register
            # its payload (a duplicate is stamped as an explicit
            # quality defect), and — when the Director assigned
            # exactly one family (event_schematic) and a different
            # family actually filled the beat — record the
            # substitution explicitly: a schematic that declined
            # must never silently reappear as a generic card
            # carrying the schematic assignment.
            _produced_record = next(
                (
                    _r for _r in reversed(assets)
                    if _r.get("scene_number") == target["scene_number"]
                    and _r.get("sentence_index")
                    == target["sentence_index"]
                ),
                None,
            )
            _dup_defect = _register_assigned_payload(_produced_record)
            _assigned = target.get("family_candidates") or []
            if (
                _produced_record is not None
                and len(_assigned) == 1
                and _assigned[0] == "event_schematic"
                and produced_family != "event_schematic"
            ):
                _produced_record["assigned_family"] = "event_schematic"
                _produced_record["family_substitution"] = {
                    "assigned": "event_schematic",
                    "delivered": produced_family,
                    "schematic_reason": _schematic_reason,
                }
                graphic_quality_defects.append({
                    "type": "assigned_family_not_delivered",
                    "sentence_number": target["sentence_number"],
                    "assigned_family": "event_schematic",
                    "delivered_family": produced_family,
                    "schematic_reason": _schematic_reason,
                })
            _record_outcome(target, "generated", produced_family, produced_reason)
            if _dup_defect is not None:
                target_outcomes[-1]["quality_defect"] = (
                    "duplicate_graphic_payload"
                )
            if (
                _produced_record is not None
                and _produced_record.get("family_substitution")
            ):
                target_outcomes[-1]["family_substitution"] = (
                    _produced_record["family_substitution"]
                )
        else:
            # MG-08: ONE typed reason from the closed vocabulary,
            # chosen by specificity — an attempted render/validation
            # failure first, then a truthfulness refusal, then the
            # schematic stage's specific reason, then the
            # sentence-shape reasons (comparison / document), then
            # the engine/stat stage outcomes, and only as a last
            # resort the generic no-purpose fall-through. The
            # historical "no_stat_value" conflated all of these;
            # the original failure detail rides along in `detail`.
            _hard_reasons = {
                "rendering_failure", "validation_failed",
                "invalid_graphic_output", "visual_validation_failed",
            }
            # T5 guarantee (stage 6, rewired by the P0 removal):
            # the guarantee no longer renders anything, so it
            # contributes no reason of its own — an unserved beat
            # keeps the deciding stage's pinned MG-08 reason
            # (missing_document_source, ...)
            # and carries the typed terminal_graphics_decline
            # alongside it (recorded below).
            _reason = None
            _detail = ""
            for _cand_reason, _cand_detail in (
                (_engine_reason, _engine_detail),
                (_schematic_reason, _schematic_detail),
            ):
                if _cand_reason in _hard_reasons:
                    _reason, _detail = _cand_reason, _cand_detail
                    break
            if _reason is None and _schematic_reason in (
                "no_schematic_labels", "no_schematic_motif",
                "schematic_cap_reached",
            ):
                _reason, _detail = _schematic_reason, _schematic_detail
            if _reason is None and (
                _schematic_reason == "no_eligible_graphic_purpose"
            ):
                _reason = "no_eligible_graphic_purpose"
            if _reason is None and comparison:
                _reason = "comparison_not_rendered"
            if _reason is None and (
                _engine_reason == "document_presentation_disabled"
            ):
                # Phase 6: the explicit founder-rule refusal wins
                # over the generic document-cue reason — the
                # template WAS attempted and refused as disabled.
                _reason, _detail = _engine_reason, _engine_detail
            if _reason is None and has_document:
                _reason = "missing_document_source"
            if _reason is None and _engine_reason:
                _reason, _detail = _engine_reason, _engine_detail
            if _reason is None:
                _reason = "no_eligible_graphic_purpose"
            if _reason not in GRAPHIC_SKIP_REASONS:
                # Closed-vocabulary guard: an unvocabularied string
                # must never reach the manifest as a "reason"; the
                # original is preserved in the detail.
                _detail = f"unvocabularied reason {_reason!r}: {_detail}"
                _reason = "no_eligible_graphic_purpose"
            target_skips.append(
                {
                    "scene_number": target["scene_number"],
                    "sentence_index": target["sentence_index"],
                    "sentence_number": target["sentence_number"],
                    "reason": _reason,
                    "detail": _detail,
                }
            )
            if _terminal_decline is not None:
                # Gate 2 (Blocker 2): the explicit terminal
                # decline — every content family was attempted
                # against the recorded facts, and none could
                # render. Recorded on BOTH the skip
                # entry and the outcome so the beat's absence of
                # any visual is a stated fact with its evidence,
                # never a silent fall-through.
                target_skips[-1]["terminal_decline"] = _terminal_decline
            _record_outcome(target, "skipped", None, _reason)
            if _terminal_decline is not None:
                target_outcomes[-1]["terminal_decline"] = (
                    _terminal_decline
                )
            # Founder defect audit 2026-11-23: the chain's primary
            # attempt failure is preserved in graphic_failures for
            # UNSERVED beats too — the same record the superseded
            # path writes when the coverage floor serves. Before
            # the floor stopped printing whole sentences it served
            # every beat, so this gap was invisible: an unserved
            # beat's refusals (e.g. the classified_file document
            # gate) reached the target outcome but vanished from
            # the failure audit.
            _prior_reasons = [
                ("event_schematic", _schematic_reason,
                 _schematic_detail),
                ("motion_engine", _engine_reason, _engine_detail),
            ]
            _hard = {
                "rendering_failure", "validation_failed",
                "invalid_graphic_output", "visual_validation_failed",
            }
            _prior = next(
                (p for p in _prior_reasons if p[1] in _hard),
                None,
            ) or next(
                (p for p in _prior_reasons if p[1] is not None),
                None,
            )
            # A hard failure that register_asset already logged
            # at the validation stage (the entry immediately
            # before this one, same reason) is not recorded
            # twice — this append exists for refusals that never
            # reach registration.
            _already_logged = (
                _prior is not None
                and _prior[1] in _hard
                and bool(graphic_failures)
                and graphic_failures[-1].get("stage") == "validation"
                and graphic_failures[-1].get("reason") == _prior[1]
            )
            if _prior is not None and not _already_logged:
                graphic_failures.append({
                    "graphic_type": _prior[0],
                    "purpose": "targeted",
                    "stage": "chain",
                    "file_path": None,
                    "reason": _prior[1],
                    "detail": _prior[2],
                    "scene_number": target["scene_number"],
                    "sentence_index": target["sentence_index"],
                    "sentence_number": target["sentence_number"],
                })
            # A targeted sentence with a recorded outcome — generated
            # OR skipped — is DECIDED: the untargeted sweep below
            # must not silently retry it and double-record the same
            # failure (or turn a refused graphic into an untargeted
            # second attempt).
            covered.add((target["scene_number"], target["sentence_index"]))

    # Phase 6 (AM-001/D1): the untargeted statistics sweep that
    # used to run here — every non-target sentence in the script
    # re-attempted as a stat, capped by max_stats — is REMOVED.
    # D1's rule is absolute: a graphic exists only for a targeted
    # requirement (a manifest entry, or a legacy target). Sentences
    # nobody targeted get no graphic, however stat-shaped they are.

    # Phase 6: every asset record carries the production theme
    # when one was selected (engine records already carry their
    # entry theme; setdefault keeps theirs).
    if _production_theme_name:
        for _asset in assets:
            _asset.setdefault("theme", _production_theme_name)

    manifest = output_dir / "motion_graphics.json"

    write_json_atomic(
        manifest,
        {
            "mode": mode,
            "genre_pack": _pack_id,
            # Phase 6 (AM-002): how this generation was targeted —
            # by the Visual Director's graphics manifest (the
            # production path) or by legacy caller-supplied targets.
            "targeting": {
                "mode": (
                    "graphics_manifest"
                    if manifest_entries is not None
                    else "legacy_targets"
                ),
                "manifest_entries": (
                    len(manifest_entries)
                    if manifest_entries is not None
                    else None
                ),
                "theme": _production_theme_name,
            },
            "count": len(assets),
            "assets": assets,
            "targets": {
                "requested": len(target_list),
                "generated": targeted_generated,
                "skipped": target_skips,
                "outcomes": target_outcomes,
            },
            "undelivered": undelivered,
            # UV-12: quantity facts the bible's own research
            # labels excluded from every graphic in this build,
            # with the reason — recorded, never silent.
            "fact_exclusions": _fact_exclusions,
            # Gate 4 (B3): duplicate payloads and assigned-family
            # substitutions that occurred in this build, as
            # explicit quality defects — never silent repetition.
            "graphic_quality_defects": graphic_quality_defects,
            # MG-05: graphics that were rendered but refused
            # registration (validation) or failed to render —
            # recorded with their original failure information,
            # never silently dropped.
            "graphic_failures": graphic_failures,
        },
    )

    print(
        f"MOTION GRAPHICS ASSETS: {len(assets)} generated"
    )

    return assets


def render_graphic_from_recipe(recipe, out_path, duration=None):
    """Re-render one graphic from its recorded regeneration recipe
    (Phase 2 / MG-03) — the single dispatch used by the editor's
    regenerate_graphic operation, so production and regeneration
    share one rendering path per family.

    `duration` overrides the recipe's recorded duration (the editor
    passes the item's recorded timeline window); the recipe's own
    duration is the default. Returns (path, metadata) on success
    and (None, {"reason": ...}) when a template/renderer refuses
    the recorded inputs; raises on hard render failures and on a
    recipe that is explicitly marked non-regenerable. The caller
    validates the output (validate_graphic_output) and promotes it
    transactionally — this function never touches the previous
    asset.
    """
    if not isinstance(recipe, dict):
        raise ValueError("graphic recipe is not a recorded dict")
    if recipe.get("regenerable") is False:
        raise ValueError(
            "graphic is marked non-regenerable: "
            + str(recipe.get("non_regenerable_reason") or "no reason recorded")
        )
    family = recipe.get("family")
    inputs = copy.deepcopy(recipe.get("inputs") or {})
    effective_duration = (
        float(duration)
        if duration
        else float(recipe.get("duration") or inputs.get("duration") or 3.0)
    )
    out_path = Path(out_path)

    if family == "motion_engine":
        from services.motion_engine import templates as _engine

        template_name = recipe.get("template")
        template_fn = _engine.TEMPLATES.get(template_name)
        if template_fn is None:
            return None, {"reason": f"unknown_template:{template_name}"}
        rendered, metadata = template_fn(
            inputs,
            recipe.get("style") or "editorial",
            out_path,
            duration=effective_duration,
            sentence_index=recipe.get("sentence_index"),
            scene_number=recipe.get("scene_number"),
        )
        if not rendered:
            return None, metadata or {"reason": "template_refused"}
        return str(out_path), metadata or {}

    if family == "event_schematic":
        from services.vector_graphics import render_event_schematic

        rendered = render_event_schematic(
            out_path,
            event_label=inputs.get("event_label", ""),
            place_label=inputs.get("place_label", ""),
            era_label=inputs.get("era_label", ""),
            duration=effective_duration,
        )
        if rendered is None:
            return None, {"reason": "no_schematic_motif"}
        return str(out_path), {"schematic_motif": inputs.get("motif")}

    raise ValueError(f"unknown graphic family in recipe: {family!r}")


