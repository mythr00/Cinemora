import json
import os
import re
import subprocess
import sys
from pathlib import Path


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

def escape_drawtext(text):
    """
    Escape text for safe embedding inside a single-quoted
    FFmpeg drawtext `text='...'` value.

    The only correct way to embed a literal apostrophe is:

        it's  ->  it'\\''s
    """
    text = str(text)
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
    text = escape_drawtext(graphic["text"])

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
        f"text='{text}':"
        "fontcolor=white:"
        "fontsize=28:"
        "x=92:"
        "y=88:"
        f"enable='between(t\\,{start}\\,{end})'"
    )


def add_entity_graphic(filters, graphic):
    start = graphic["start"]
    end = graphic["end"]
    text = escape_drawtext(graphic["text"])
    subtext = escape_drawtext(graphic.get("subtext", ""))

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
        f"text='{text}':"
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
            f"text='{subtext}':"
            "fontcolor=white@0.62:"
            "fontsize=21:"
            "x=105:"
            "y=h-122:"
            f"enable='between(t\\,{start}\\,{end})'"
        )


def add_date_graphic(filters, graphic):
    start = graphic["start"]
    end = graphic["end"]
    text = escape_drawtext(graphic["text"])

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
        f"text='{text}':"
        "fontcolor=white:"
        "fontsize=27:"
        "x=95:"
        "y=186:"
        f"enable='between(t\\,{start}\\,{end})'"
    )


def add_source_graphic(filters, graphic):
    start = graphic["start"]
    end = graphic["end"]
    text = escape_drawtext(graphic["text"])

    filters.append(
        "drawtext="
        "font='Arial':"
        "expansion=none:"
        f"text='{text}':"
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
    with open(plan_file, "w", encoding="utf-8") as file:
        json.dump(plan, file, indent=2, ensure_ascii=False)
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
    filter_script.write_text(filter_complex, encoding="utf-8")

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
    text = escape_drawtext(shorten(text, 64))
    font = _fontfile(
        GRAPHIC_FONT_BOLD_FILE if bold else GRAPHIC_FONT_FILE
    )
    result = (
        f"drawtext={font}text='{text}':"
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


_STAT_VALUE_RE = re.compile(
    r"^\s*(?P<prefix>\$\s?)?"
    r"(?P<number>\d[\d,]*(?:\.\d+)?)"
    r"(?P<suffix>\s*(?:million|billion|trillion|tons?|feet|miles?|years?|vehicles?))?"
    r"\s*$",
    re.I,
)


def _parse_stat_value(value):
    """Split a verbatim stat headline ('$5 million', '12,000 feet')
    into prefix / numeric value / suffix for count-up animation.

    Returns None when the value does not parse cleanly — the caller
    then renders the headline statically, exactly as before. The
    original string is always preserved for the final frame.
    """
    match = _STAT_VALUE_RE.match(clean_text(value))
    if not match:
        return None
    number_text = match.group("number")
    try:
        number = float(number_text.replace(",", ""))
    except ValueError:
        return None
    decimals = len(number_text.split(".")[1]) if "." in number_text else 0
    return {
        "prefix": match.group("prefix") or "",
        "number_text": number_text,
        "number": number,
        "decimals": decimals,
        "grouped": "," in number_text,
        "suffix": match.group("suffix") or "",
        # eif can only print a plain integer; anything with grouping
        # commas or decimals uses stepped layers instead.
        "eif_ok": decimals == 0 and "," not in number_text,
    }


def _format_stat_step(parsed, fraction):
    """Format an intermediate count-up value with the same grouping
    and decimal places as the verbatim final value."""
    value = parsed["number"] * fraction
    if parsed["decimals"]:
        body = f"{value:,.{parsed['decimals']}f}"
    elif parsed["grouped"]:
        body = f"{value:,.0f}"
    else:
        body = f"{value:.0f}"
    return f"{parsed['prefix']}{body}{parsed['suffix']}"


def _countup_headline_filters(headline, x, y, fontsize, duration):
    """Drawtext filters that animate the headline value counting up
    from a low fraction to the VERBATIM final value.

    - Plain integers: one drawtext whose numeric part is an ffmpeg
      %{eif:...} expression clamped at the final value (continuous).
    - Grouped/decimal values: four stepped drawtext layers
      (25/50/75/100%) with enable windows; the last layer is the
      original string, character for character.
    - Unparseable values: the original static headline.
    """
    parsed = _parse_stat_value(headline)
    if parsed is None or parsed["number"] <= 0:
        return [
            _ffmpeg_text_filter(
                headline, str(x), str(y), fontsize, GRAPHIC_TEXT, 1.0,
                bold=True,
            )
        ]

    ramp = max(0.8, duration * 0.8)

    if parsed["eif_ok"]:
        whole = int(parsed["number"])
        expr = rf"%{{eif\:{whole}*min(1\,t/{ramp:.3f})\:d}}"
        animated = f"{parsed['prefix']}{expr}{parsed['suffix']}"
        font = _fontfile(GRAPHIC_FONT_BOLD_FILE)
        return [
            "drawtext="
            f"{font}text='{escape_drawtext(animated)}':"
            f"x={x}:y={y}:"
            f"fontsize={fontsize}:"
            f"fontcolor={GRAPHIC_TEXT}@1.0:"
            "borderw=2:"
            "bordercolor=black@0.45:"
            "shadowx=3:"
            "shadowy=3:"
            "shadowcolor=black@0.65"
        ]

    filters = []
    fractions = (0.25, 0.50, 0.75, 1.0)
    for step, fraction in enumerate(fractions):
        start = ramp * step / len(fractions)
        if fraction >= 1.0:
            text = headline  # verbatim final value
            enable = f"gte(t,{start:.3f})"
        else:
            text = _format_stat_step(parsed, fraction)
            end = ramp * (step + 1) / len(fractions)
            enable = f"between(t,{start:.3f},{end:.3f})"
        filters.append(
            _ffmpeg_text_filter(
                text, str(x), str(y), fontsize, GRAPHIC_TEXT, 1.0,
                bold=True, enable=enable,
            )
        )
    return filters


def _render_stat_graphic(
    output_file,
    headline,
    subtext="",
    duration=4.0,
):
    duration = _graphic_duration(0, duration, 3.0, 8.0)

    filters = _graphic_background()

    filters += [
        "drawbox="
        "x=100:"
        "y=120:"
        "w=1720:"
        "h=840:"
        f"color={GRAPHIC_PANEL}@0.92:"
        "t=fill",

        "drawbox="
        "x=100:"
        "y=120:"
        "w=1720:"
        "h=5:"
        "color=white@0.85:"
        "t=fill",

        "drawbox="
        "x=145:"
        "y=185:"
        "w=7:"
        "h=650:"
        "color=white@0.85:"
        "t=fill",

        _ffmpeg_text_filter(
            "DATA / VERIFIED",
            "190",
            "235",
            24,
            GRAPHIC_TEXT,
            0.65,
        ),
    ]

    # Stage B: the headline value counts up to its verbatim final
    # value instead of sitting static from frame 0.
    filters += _countup_headline_filters(headline, 190, 445, 100, duration)

    if subtext:
        filters += [
            _ffmpeg_text_filter(
                subtext,
                "195",
                "535",
                30,
                "white",
                0.72,
            )
        ]

    # Animated analytical bars.
    filters += [
        "drawbox="
        "x=195:"
        "y=670:"
        "w=1250:"
        "h=3:"
        "color=white@0.25:"
        "t=fill",

        "drawbox="
        "x=195:"
        "y=670:"
        "w='min(1250,1250*t/4)':"
        "h=3:"
        "color=white@0.9:"
        "t=fill",

        "drawbox="
        "x=195:"
        "y=720:"
        "w=760:"
        "h=8:"
        "color=white@0.12:"
        "t=fill",

        "drawbox="
        "x=195:"
        "y=720:"
        "w='min(760,760*t/4)':"
        "h=8:"
        "color=white@0.65:"
        "t=fill",

        _ffmpeg_text_filter(
            "CINEMORA / ANALYSIS",
            "195",
            "805",
            21,
            GRAPHIC_TEXT,
            0.45,
        ),
    ]

    command = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i",
        f"color=c={GRAPHIC_BG_STAT}:s=1920x1080:r={FPS}",
        "-t", f"{duration:.3f}",
        "-vf", ",".join(filters),
        "-an",
        "-c:v", "libx264",
        "-preset", PRESET,
        "-crf", str(CRF),
        "-pix_fmt", "yuv420p",
        str(output_file),
    ]

    run_ffmpeg(command)


_MONTH_NUMBERS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_event_date(when):
    """Parse a fractional year (year + month/12) from an event's
    `when` string ('November 1989', '9 November 1989', '1989').
    Returns None when no year can be found."""
    text = clean_text(when).lower()
    year_match = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", text)
    if not year_match:
        return None
    value = float(year_match.group(1))
    for name, number in _MONTH_NUMBERS.items():
        if re.search(rf"\b{name}\b", text):
            value += (number - 1) / 12.0
            break
    return value


def _timeline_node_positions(usable, duration):
    """Stage B: node x positions PROPORTIONAL to the events' actual
    dates when at least two distinct dates parse; otherwise the
    historical even spacing. Returns (positions, spacing_mode) where
    spacing_mode is 'proportional' or 'even_fallback'. The reveal
    time of node i is when the scan line reaches its x position."""
    line_start = 120.0
    line_span = 1680.0
    dates = [_parse_event_date(event.get("when", "")) for event in usable]
    parsed = [d for d in dates if d is not None]

    if len(usable) > 1 and len(parsed) >= 2 and (max(parsed) - min(parsed)) > 0:
        low, high = min(parsed), max(parsed)
        positions = []
        for index, date in enumerate(dates):
            if date is None:
                # Unparseable events keep their ordinal slot.
                fraction = index / max(len(usable) - 1, 1)
            else:
                fraction = (date - low) / (high - low)
            # Nodes are 70px wide starting at x; keep the fraction
            # inside the line span with the original 70px inset.
            positions.append(190 + fraction * 1540)
        mode = "proportional"
    else:
        spacing = 1540 / max(len(usable) - 1, 1)
        positions = [190 + index * spacing for index in range(len(usable))]
        mode = "even_fallback"

    reveals = [
        duration * ((x - line_start) / line_span) for x in positions
    ]
    return positions, mode, reveals


def _render_timeline_graphic(
    output_file,
    events,
    duration=6.0,
):
    """Render the chronology graphic. Returns the spacing mode used
    ('proportional' or 'even_fallback') so the caller can record it
    on the asset record."""
    duration = _graphic_duration(0, duration, 4.0, 10.0)

    filters = _graphic_background()

    filters += [
        _ffmpeg_text_filter(
            "CHRONOLOGY",
            "120",
            "150",
            28,
            GRAPHIC_TEXT,
            0.65,
        ),
        "drawbox="
        "x=120:"
        "y=500:"
        "w=1680:"
        "h=4:"
        "color=white@0.35:"
        "t=fill",
    ]

    usable = events[:6]

    if not usable:
        usable = [
            {
                "when": "NOW",
                "what": "EVENT",
            }
        ]

    positions, spacing_mode, reveals = _timeline_node_positions(
        usable, duration
    )

    for index, event in enumerate(usable):
        x = int(positions[index])

        when = shorten(
            event.get("when", ""),
            20,
        )

        what = shorten(
            event.get("what", ""),
            34,
        )

        # Stage B: the node and its labels appear when the scan line
        # reaches them, not all from frame 0.
        enable = f"gte(t,{reveals[index]:.3f})"

        # Timeline node.
        filters += [
            f"drawbox="
            f"x={x}:"
            f"y=465:"
            f"w=70:"
            f"h=70:"
            f"color=white@0.08:"
            f"t=fill:"
            f"enable='{enable}'",

            f"drawbox="
            f"x={x + 12}:"
            f"y=477:"
            f"w=46:"
            f"h=46:"
            f"color=white@0.92:"
            f"t=fill:"
            f"enable='{enable}'",

            _ffmpeg_text_filter(
                when,
                str(x - 20),
                "405",
                27,
                GRAPHIC_TEXT,
                0.85,
                enable=enable,
            ),

            _ffmpeg_text_filter(
                what,
                str(max(80, x - 45)),
                "585",
                22,
                GRAPHIC_TEXT,
                0.68,
                enable=enable,
            ),
        ]

    # Moving scan line gives the timeline actual motion. It sweeps
    # the line across the graphic's own duration so node reveals
    # stay synchronised with it.
    filters += [
        "drawbox="
        f"x='120+1680*t/{duration:.3f}':"
        "y=495:"
        "w=8:"
        "h=14:"
        "color=white@0.95:"
        "t=fill",
    ]

    command = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i",
        f"color=c={GRAPHIC_BG_TIMELINE}:s=1920x1080:r={FPS}",
        "-t", f"{duration:.3f}",
        "-vf", ",".join(filters),
        "-an",
        "-c:v", "libx264",
        "-preset", PRESET,
        "-crf", str(CRF),
        "-pix_fmt", "yuv420p",
        str(output_file),
    ]

    run_ffmpeg(command)
    return spacing_mode


def _render_process_graphic(
    output_file,
    steps,
    duration=5.0,
):
    duration = _graphic_duration(0, duration, 3.5, 9.0)

    filters = _graphic_background()

    filters += [
        _ffmpeg_text_filter(
            "PROCESS / TRANSFORMATION",
            "120",
            "145",
            27,
            GRAPHIC_TEXT,
            0.65,
        )
    ]

    usable = steps[:5]

    if not usable:
        usable = ["START", "CHANGE", "RESULT"]

    spacing = 1500 / max(len(usable), 1)

    for index, step in enumerate(usable):
        x = int(170 + index * spacing)

        label = shorten(
            step if isinstance(step, str)
            else step.get("label", ""),
            25,
        )

        # Stage B: steps reveal one at a time, in narration order,
        # instead of all being visible from frame 0.
        reveal = duration * 0.55 * index / max(len(usable) - 1, 1)
        enable = f"gte(t,{reveal:.3f})"

        # Outer card.
        filters += [
            f"drawbox="
            f"x={x}:"
            f"y=405:"
            f"w=250:"
            f"h=170:"
            f"color={GRAPHIC_PANEL_PROCESS}@0.94:"
            f"t=fill:"
            f"enable='{enable}'",

            f"drawbox="
            f"x={x}:"
            f"y=405:"
            f"w=250:"
            f"h=4:"
            f"color=white@0.8:"
            f"t=fill:"
            f"enable='{enable}'",

            _ffmpeg_text_filter(
                f"{index + 1:02d}",
                str(x + 20),
                "430",
                23,
                GRAPHIC_TEXT,
                0.45,
                enable=enable,
            ),

            _ffmpeg_text_filter(
                label,
                str(x + 20),
                "490",
                25,
                GRAPHIC_TEXT,
                0.92,
                bold=True,
                enable=enable,
            ),
        ]

        if index < len(usable) - 1:
            arrow_x = x + 265
            # Stage B: the connector is a drawn chevron in the
            # bundled font (the old code rendered a literal "?"
            # because no arrow glyph was available). It appears
            # together with the step it points FROM.
            filters += [
                _ffmpeg_text_filter(
                    ">",
                    str(arrow_x),
                    "460",
                    48,
                    GRAPHIC_TEXT,
                    0.8,
                    bold=True,
                    enable=enable,
                )
            ]

    # Animated horizontal sweep.
    filters += [
        "drawbox="
        f"x='120+1500*t/{duration:.3f}':"
        "y=620:"
        "w=220:"
        "h=3:"
        "color=white@0.65:"
        "t=fill"
    ]

    command = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i",
        f"color=c={GRAPHIC_BG_PROCESS}:s=1920x1080:r={FPS}",
        "-t", f"{duration:.3f}",
        "-vf", ",".join(filters),
        "-an",
        "-c:v", "libx264",
        "-preset", PRESET,
        "-crf", str(CRF),
        "-pix_fmt", "yuv420p",
        str(output_file),
    ]

    run_ffmpeg(command)


def build_motion_graphics_assets(
    data,
    output_dir,
    mode="balanced",
    targets=None,
    max_stats=None,
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
    Targeted sentences get a graphic FIRST and are never blocked by
    the untargeted stat cap; a targeted sentence with no defensible
    graphic content is skipped and the skip is recorded in the
    manifest (no invented content, no text cards).

    `max_stats` caps the number of UNTARGETED stat graphics
    (None = the historical per-mode default: 3 in balanced mode,
    uncapped in heavy mode).
    """

    if mode in {"off", "none", "disabled"}:
        return []

    if max_stats is None:
        # Historical defaults: balanced was capped at 3; heavy was
        # uncapped (kept effectively uncapped here).
        max_stats = 3 if mode == "balanced" else 1_000_000

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    case_bible = data.get("case_bible") or {}
    timeline_events = case_bible.get("timeline") or []
    scenes = data.get("scenes") or []

    assets = []
    index = 0

    def register_asset(
        path,
        graphic_type,
        purpose,
        duration,
        text="",
        subtext="",
    ):
        nonlocal index
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
        }

        assets.append(record)

    # -----------------------------------------------------
    # STAGE A: normalise targets and choose anchors for the
    # timeline / process graphics. Every generated graphic must
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
            }
        )

    target_skips = []

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

    if target_list:
        timeline_anchor = target_list[0]
        process_anchor = (
            target_list[1] if len(target_list) > 1 else None
        )
    else:
        # No uncovered sentences: anchor the chronology to the first
        # scene's first sentence so it can still reach the film, and
        # do not anchor the process graphic (it summarises the same
        # events; one anchored summary graphic is enough).
        timeline_anchor = _first_scene_anchor()
        process_anchor = None

    def _stamp_anchor(record, anchor):
        if anchor:
            record["scene_number"] = anchor.get("scene_number")
            record["sentence_index"] = anchor.get("sentence_index")
            record["sentence_number"] = anchor.get("sentence_number")

    # -----------------------------------------------------
    # STORY TIMELINE
    # -----------------------------------------------------

    if timeline_events and mode in {"balanced", "heavy"}:
        path = output_dir / "motion_graphic_timeline_001.mp4"
        try:
            spacing_mode = _render_timeline_graphic(
                path,
                timeline_events,
                duration=6.0,
            )
            register_asset(
                path,
                "timeline",
                "chronology",
                6.0,
                "STORY TIMELINE",
            )
            _stamp_anchor(assets[-1], timeline_anchor)
            assets[-1]["timeline_spacing"] = (
                spacing_mode or "even_fallback"
            )
        except Exception as error:
            # AUD-21: one failing graphic must not kill the whole job.
            print(f"MOTION GRAPHIC SKIPPED (timeline): {error}")

    # -----------------------------------------------------
    # NUMERICAL / SCALE GRAPHICS
    # -----------------------------------------------------

    money_re = re.compile(
        r"\$\s?\d[\d,]*(?:\.\d+)?"
        r"(?:\s*(?:million|billion|trillion))?",
        re.I,
    )

    number_re = re.compile(
        r"\b\d[\d,]*(?:\.\d+)?\s*"
        r"(?:tons?|feet|miles?|years?|vehicles?)\b",
        re.I,
    )

    def _make_stat(scene_number, sentence_index, sentence_number,
                   text, targeted):
        """Render + register one stat graphic. Returns True when an
        asset was created, False when the sentence carries no
        defensible stat value."""
        match = money_re.search(text) or number_re.search(text)
        if not match:
            return False

        value = clean_text(match.group(0))

        path = (
            output_dir
            / f"motion_graphic_stat_{index + 1:03d}.mp4"
        )

        try:
            _render_stat_graphic(
                path,
                value.upper(),
                shorten(text, 72),
                duration=4.0,
            )
        except Exception as error:
            # AUD-21: skip this graphic; never fail the whole job.
            print(f"MOTION GRAPHIC SKIPPED (stat): {error}")
            return False

        register_asset(
            path,
            "stat",
            "quantitative_explanation",
            4.0,
            value.upper(),
            text,
        )

        assets[-1]["scene_number"] = scene_number
        assets[-1]["sentence_index"] = sentence_index
        assets[-1]["sentence_number"] = (
            sentence_number or sentence_index + 1
        )
        assets[-1]["targeted"] = bool(targeted)
        return True

    covered = set()
    targeted_generated = 0

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
            continue
        if _make_stat(
            target["scene_number"],
            target["sentence_index"],
            target["sentence_number"],
            target["text"],
            targeted=True,
        ):
            covered.add(key)
            targeted_generated += 1
        else:
            target_skips.append(
                {
                    "scene_number": target["scene_number"],
                    "sentence_index": target["sentence_index"],
                    "sentence_number": target["sentence_number"],
                    "reason": "no_stat_value",
                }
            )

    # Untargeted stats, capped by max_stats (Stage A: parameterised;
    # historically a hardcoded 3 in balanced mode).
    stats_created = 0

    for scene in scenes:
        if stats_created >= max_stats:
            break
        scene_number = scene.get("scene_number")

        for sentence_index, sentence in enumerate(
            scene.get("sentences") or []
        ):
            if stats_created >= max_stats:
                break
            if not isinstance(sentence, dict):
                continue
            if (scene_number, sentence_index) in covered:
                continue

            text = clean_text(
                sentence.get("text")
                or sentence.get("sentence_text")
                or ""
            )

            if not text:
                continue

            if _make_stat(
                scene_number,
                sentence_index,
                sentence.get("sentence_number"),
                text,
                targeted=False,
            ):
                covered.add((scene_number, sentence_index))
                stats_created += 1
    # -----------------------------------------------------
    # PROCESS / TRANSFORMATION GRAPHIC
    # -----------------------------------------------------

    if mode in {"balanced", "heavy"}:
        steps = []

        for event in timeline_events[:5]:
            label = clean_text(event.get("when", ""))
            if label:
                steps.append(label)

        if len(steps) >= 3:
            path = (
                output_dir
                / "motion_graphic_process_001.mp4"
            )

            try:
                _render_process_graphic(
                    path,
                    steps,
                    duration=5.0,
                )

                register_asset(
                    path,
                    "process",
                    "process_or_transformation",
                    5.0,
                    "PROCESS",
                )
                _stamp_anchor(assets[-1], process_anchor)
            except Exception as error:
                # AUD-21: skip this graphic; never fail the whole job.
                print(f"MOTION GRAPHIC SKIPPED (process): {error}")

    manifest = output_dir / "motion_graphics.json"

    manifest.write_text(
        json.dumps(
            {
                "mode": mode,
                "max_stats": max_stats,
                "count": len(assets),
                "assets": assets,
                "targets": {
                    "requested": len(target_list),
                    "generated": targeted_generated,
                    "skipped": target_skips,
                },
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(
        f"MOTION GRAPHICS ASSETS: {len(assets)} generated"
    )

    return assets


