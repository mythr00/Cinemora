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
    r"(?P<suffix>\s*(?:million|billion|trillion)?\s*(?:%|[a-z]+)?)"
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
            f"{font}text='{escape_drawtext(animated, programmatic=True)}':"
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


# Stage 5 (Motion Graphics V2): the scan sweep completes in this
# fraction of the graphic's duration and then rests at the line's
# end. At 1.0 the final node (x=1730) only appeared at t=95.8% of
# the duration — the v1 benchmark's "final node missing at 90%"
# defect. At 0.85 the last reveal lands at ~81%.
TIMELINE_SCAN_FRACTION = 0.85


def _timeline_node_positions(usable, duration):
    """Stage B: node x positions PROPORTIONAL to the events' actual
    dates when at least two distinct dates parse; otherwise the
    historical even spacing. Returns (positions, spacing_mode) where
    spacing_mode is 'proportional' or 'even_fallback'. The reveal
    time of node i is when the scan line reaches its x position;
    the scan sweeps on the compressed TIMELINE_SCAN_FRACTION clock
    so the final node is revealed well before the graphic ends."""
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

    # MA-02: enforce a minimum gap between nodes so clustered
    # dates (the 1961 Berlin cluster is the canonical case) do not
    # draw their nodes — and labels — on top of each other. Order
    # is preserved; positions are only spread, never reordered.
    min_gap = 170.0
    for i in range(1, len(positions)):
        if positions[i] < positions[i - 1] + min_gap:
            positions[i] = positions[i - 1] + min_gap
    if positions and positions[-1] > 1730.0:
        positions[-1] = 1730.0
        for i in range(len(positions) - 2, -1, -1):
            if positions[i] > positions[i + 1] - min_gap:
                positions[i] = positions[i + 1] - min_gap
    if positions and positions[0] < 190.0:
        positions[0] = 190.0
        for i in range(1, len(positions)):
            if positions[i] < positions[i - 1] + min_gap:
                positions[i] = positions[i - 1] + min_gap

    reveals = [
        duration * TIMELINE_SCAN_FRACTION * ((x - line_start) / line_span)
        for x in positions
    ]
    return positions, mode, reveals


def _timeline_label_rows(positions, close_distance=220.0):
    """MA-02: assign each node a label row. Nodes whose x positions
    are close get DIFFERENT rows so their when/what labels stack
    vertically instead of colliding; distant nodes reuse row 0."""
    rows = []
    last_x_by_row = {}
    for x in positions:
        chosen = None
        for row in sorted(last_x_by_row):
            if x - last_x_by_row[row] >= close_distance:
                chosen = row
                break
        if chosen is None:
            if not last_x_by_row or 0 not in last_x_by_row:
                chosen = 0
            elif all(x - v < close_distance for v in last_x_by_row.values()):
                chosen = max(last_x_by_row) + 1
            else:
                chosen = 0
        rows.append(chosen)
        last_x_by_row[chosen] = x
    return rows


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
    label_rows = _timeline_label_rows(positions)

    for index, event in enumerate(usable):
        x = int(positions[index])
        row = label_rows[index] if index < len(label_rows) else 0
        when_y = 405 - row * 78
        what_y = 585 + row * 78

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
                str(when_y),
                27,
                GRAPHIC_TEXT,
                0.85,
                enable=enable,
            ),

            _ffmpeg_text_filter(
                what,
                str(max(80, x - 45)),
                str(what_y),
                22,
                GRAPHIC_TEXT,
                0.68,
                enable=enable,
            ),
        ]

    # Moving scan line gives the timeline actual motion. It sweeps
    # the line on the compressed TIMELINE_SCAN_FRACTION clock (and
    # rests at the end) so node reveals stay synchronised with it
    # and the final node lands well before the graphic ends.
    sweep = duration * TIMELINE_SCAN_FRACTION
    filters += [
        "drawbox="
        f"x='120+1680*min(1\\,t/{sweep:.3f})':"
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

        if isinstance(step, str):
            label = shorten(step, 25)
            sublabel = ""
        else:
            # MA-01: the step itself is the event's "what"/action
            # text; "when" is only a sublabel underneath it.
            label = shorten(
                step.get("label") or step.get("what") or "", 25
            )
            sublabel = shorten(
                step.get("sublabel") or step.get("when") or "", 25
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

        if sublabel:
            filters += [
                _ffmpeg_text_filter(
                    sublabel,
                    str(x + 20),
                    "532",
                    18,
                    GRAPHIC_TEXT,
                    0.58,
                    enable=enable,
                )
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


# =========================================================
# STAT EXTRACTION (MA-05)
# =========================================================

_STAT_STOP_NOUNS = {
    "in", "on", "at", "by", "for", "from", "was", "were", "is", "are",
    "had", "has", "have", "after", "before", "during", "when", "while",
    "than", "then", "and", "or", "to", "of", "the", "a", "an", "with",
    "into", "over", "under", "per", "each", "more", "most", "only",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
}

_SEQUENCE_MARKERS = (
    "first", "then", "after", "before", "finally", "next", "later",
    "subsequently", "eventually", "meanwhile",
)

_DOCUMENT_MARKERS = (
    "document", "report", "letter", "filing", "memorandum", "memo",
    "manuscript", "deposition", "affidavit", "transcript",
)


def _has_sequence_marker(text):
    lowered = f" {clean_text(text).lower()} "
    return any(f" {marker} " in lowered or f" {marker}," in lowered
               for marker in _SEQUENCE_MARKERS)


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


def extract_stat(text, sentence_record=None):
    """MA-05: extract ONE defensible statistic from a sentence.

    Returns {"display", "kind", ...} or None. Kinds: money,
    percent, count, range. Order: the analysed sentence record's
    `money` field first (structured analysis beats regex), then
    ranges (first value is the headline, the range is recorded),
    percentages, money in text, then plain counts — a number plus
    ANY following noun, not a whitelist of units.
    """
    text = clean_text(text)
    if sentence_record and isinstance(sentence_record, dict):
        money_field = sentence_record.get("money") or []
        if isinstance(money_field, str):
            money_field = [money_field]
        for entry in money_field:
            entry = clean_text(entry)
            if entry:
                return {"display": entry, "kind": "money",
                        "source": "analysis_money_field"}
    if not text:
        return None

    range_match = re.search(
        r"\bbetween\s+(\d[\d,]*(?:\.\d+)?)\s+and\s+(\d[\d,]*(?:\.\d+)?)"
        r"(?:\s+([A-Za-z%]+))?"
        r"|(\d[\d,]*(?:\.\d+)?)\s*(?:-|\u2013| to )\s*(\d[\d,]*(?:\.\d+)?)"
        r"(?:\s+([A-Za-z%]+))?",
        text, re.I,
    )
    if range_match:
        first = range_match.group(1) or range_match.group(4)
        second = range_match.group(2) or range_match.group(5)
        unit = range_match.group(3) or range_match.group(6) or ""
        # A pair of bare 4-digit years is a date span, not a stat.
        if not (len(first.replace(",", "")) == 4
                and len(second.replace(",", "")) == 4
                and "," not in first and "." not in first):
            display = f"{first} {unit}".strip() if unit else first
            return {"display": display, "kind": "range",
                    "range_end": second, "source": "regex_range"}

    percent_match = re.search(
        r"(\d[\d,]*(?:\.\d+)?)\s*(?:%|percent\b)", text, re.I
    )
    if percent_match:
        return {"display": f"{percent_match.group(1)}%", "kind": "percent",
                "source": "regex_percent"}

    money_match = re.search(
        r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:million|billion|trillion))?"
        r"|\b\d+(?:\.\d+)?\s*(?:million|billion|trillion)\s+dollars?\b",
        text, re.I,
    )
    if money_match:
        return {"display": clean_text(money_match.group(0)), "kind": "money",
                "source": "regex_money"}

    for match in re.finditer(
        r"(\d[\d,]*(?:\.\d+)?)\s*(million|billion|trillion)?\s*([A-Za-z][A-Za-z\-]*)",
        text, re.I,
    ):
        noun = (match.group(3) or "").lower()
        if noun in _STAT_STOP_NOUNS or not noun:
            continue
        number_text = match.group(1)
        # A bare year followed by a noun ("in 1961 people ...") is
        # not a count; comma/decimal/scale numbers still count.
        try:
            numeric = float(number_text.replace(",", ""))
        except ValueError:
            continue
        if (1000 <= numeric <= 2100 and "," not in number_text
                and "." not in number_text and not match.group(2)):
            continue
        return {"display": clean_text(match.group(0)), "kind": "count",
                "source": "regex_count"}
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


def build_motion_graphics_assets(
    data,
    output_dir,
    mode="balanced",
    targets=None,
    max_stats=None,
    genre_pack=None,
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
                # MA-05: a target may itself carry the analysed
                # sentence record's money field; when it does not,
                # _make_stat falls back to the scene sentence record.
                "money": target.get("money"),
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

    # MA-06 anchor preference: the chronology graphic anchors to a
    # targeted sentence whose text contains a date when one exists
    # (anchor_reason="chronology_sentence"); otherwise the first
    # target, labelled a chapter graphic. The process graphic
    # prefers a sentence with sequence markers; with exactly ONE
    # target it anchors to that target (MA-03); with zero targets
    # it stays unanchored and is recorded as undelivered in the
    # manifest instead of being silently counted as delivered.
    timeline_anchor_reason = "chapter_graphic"
    if target_list:
        timeline_anchor = None
        for _candidate in target_list:
            if detect_date(_candidate["text"]):
                timeline_anchor = _candidate
                timeline_anchor_reason = "chronology_sentence"
                break
        if timeline_anchor is None:
            timeline_anchor = target_list[0]

        process_anchor = None
        process_anchor_reason = "chapter_graphic"
        for _candidate in target_list:
            if _has_sequence_marker(_candidate["text"]) and (
                _candidate is not timeline_anchor
                or len(target_list) == 1
            ):
                process_anchor = _candidate
                process_anchor_reason = "sequence_sentence"
                break
        if process_anchor is None:
            if len(target_list) > 1:
                process_anchor = target_list[1]
            else:
                process_anchor = target_list[0]
                process_anchor_reason = "single_target"
    else:
        # No uncovered sentences: anchor the chronology to the first
        # scene's first sentence so it can still reach the film, and
        # do not anchor the process graphic (it summarises the same
        # events; one anchored summary graphic is enough).
        timeline_anchor = _first_scene_anchor()
        process_anchor = None
        process_anchor_reason = "no_targets"

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
            assets[-1]["anchor_reason"] = timeline_anchor_reason
            assets[-1]["family"] = "timeline"
            if timeline_anchor is None:
                assets[-1]["delivered"] = False
                undelivered.append({
                    "graphic_type": "timeline",
                    "reason": "no_anchor_available",
                })
            else:
                assets[-1]["delivered"] = True
        except Exception as error:
            # AUD-21: one failing graphic must not kill the whole job.
            print(f"MOTION GRAPHIC SKIPPED (timeline): {error}")

    # -----------------------------------------------------
    # NUMERICAL / SCALE GRAPHICS
    # -----------------------------------------------------

    def _make_stat(scene_number, sentence_index, sentence_number,
                   text, targeted, target=None):
        """Render + register one stat graphic. Returns True when an
        asset was created, False when the sentence carries no
        defensible stat value. MA-05: extraction is extract_stat
        (percent / plain counts / ranges / money, plus the analysed
        sentence record's `money` field when reachable — via the
        target dict itself or the matching scene sentence record
        in `data["scenes"]`). The stat's kind is recorded."""
        sentence_record = _sentences_by_key.get(
            (scene_number, sentence_index)
        )
        record_for_stat = None
        if target and target.get("money"):
            record_for_stat = {"money": target.get("money")}
        elif sentence_record and sentence_record.get("money"):
            record_for_stat = sentence_record
        info = extract_stat(text, sentence_record=record_for_stat)
        if not info:
            return False

        value = clean_text(info["display"])

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
        assets[-1]["family"] = "stat"
        assets[-1]["stat_kind"] = info["kind"]
        assets[-1]["stat_display"] = value
        assets[-1]["stat_source"] = info.get("source", "")
        if info.get("range_end"):
            assets[-1]["stat_range_end"] = info["range_end"]
        return True

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

    def _schematic_eligible(target):
        """Stage 3D per-sentence reachability: eligible when the
        scene is known to hold no verified visual at all (the
        original trigger), OR when the target sentence's OWN
        coverage is empty — no accepted verified visual is linked
        to this sentence — while other sentences in the scene do
        have linked verified coverage. When no verified asset in
        the scene carries any sentence link, per-sentence coverage
        is unknowable and the scene-level rule decides alone."""
        scene_number = target["scene_number"]
        if scene_number not in _verified_by_scene:
            return False
        if _verified_by_scene[scene_number] == 0:
            return True
        key = (scene_number, target["sentence_index"])
        if key in _verified_sentences:
            return False
        return scene_number in _linked_verified_scenes

    def _make_schematic(target):
        """Render + register one event schematic for a target.
        Returns None on success, otherwise the skip reason. When
        the target is not eligible the historical "no_stat_value"
        reason is preserved unchanged."""
        scene_number = target["scene_number"]
        if not _schematic_eligible(target):
            return "no_stat_value"
        if scene_number in _schematic_scenes:
            return "schematic_cap_reached"
        labels = _schematic_labels(target)
        if labels is None:
            return "no_schematic_labels"
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
            return "no_schematic_motif"
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
            return "schematic_render_failed"
        if rendered is None:
            return "no_schematic_motif"

        register_asset(
            path,
            "event_schematic",
            "event_schematic",
            6.0,
            event,
            target["text"],
        )
        assets[-1]["scene_number"] = scene_number
        assets[-1]["sentence_index"] = target["sentence_index"]
        assets[-1]["sentence_number"] = (
            target["sentence_number"] or target["sentence_index"] + 1
        )
        assets[-1]["targeted"] = True
        assets[-1]["family"] = "event_schematic"
        assets[-1]["schematic_motif"] = motif
        _schematic_scenes.add(scene_number)
        return None

    def _make_engine_graphic(target, template_name, engine_data,
                             style="editorial", duration=3.0):
        """Stage 3E integration: render one Motion Engine template
        for a target and register it with the SAME asset shape and
        honesty stamps as the ffmpeg/PIL graphics. Returns True on
        success, False (with the reason printed) otherwise."""
        try:
            from services.motion_engine import templates as _engine

            template_fn = _engine.TEMPLATES[template_name]
        except Exception as error:
            print(f"MOTION GRAPHIC SKIPPED (motion_engine): {error}")
            return False
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
            return False
        if not rendered:
            return False
        register_asset(
            path, template_name, f"motion_engine_{template_name}",
            duration, target["text"], target["text"],
        )
        assets[-1]["scene_number"] = target["scene_number"]
        assets[-1]["sentence_index"] = target["sentence_index"]
        assets[-1]["sentence_number"] = (
            target["sentence_number"] or target["sentence_index"] + 1
        )
        assets[-1]["targeted"] = True
        assets[-1]["family"] = "motion_engine"
        assets[-1]["template"] = template_name
        assets[-1]["engine_metadata"] = metadata
        return True

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
    # order: stat -> chronology/timeline -> process -> event
    # schematic -> Motion Engine templates -> recorded skip.
    # Every outcome (produced or skipped) is recorded in the
    # manifest with family + reason (target_outcomes). Variety:
    # the same family is not chosen for two consecutive targets
    # when an alternative family fits — an alternative that fits
    # is tried first, and the primary family remains the fallback.
    _timeline_asset = next(
        (a for a in assets if a["graphic_type"] == "timeline"), None
    )
    _timeline_anchor_key = (
        (timeline_anchor.get("scene_number"),
         timeline_anchor.get("sentence_index"))
        if timeline_anchor else None
    )
    _timeline_claimed = False
    _process_claimed = False
    _process_available = bool(timeline_events) and len(
        [e for e in timeline_events if clean_text(e.get("what", ""))]
    ) >= 3
    _last_family = None

    def _record_outcome(target, outcome, family, reason):
        target_outcomes.append({
            "scene_number": target["scene_number"],
            "sentence_index": target["sentence_index"],
            "sentence_number": target["sentence_number"],
            "outcome": outcome,
            "family": family,
            "reason": reason,
        })

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
        has_date = detect_date(text) is not None
        has_sequence = _has_sequence_marker(text)
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
                        "percent": percent, "label": shorten(text, 52),
                        "sublabel": text,
                    }, "editorial")
                if comparison:
                    return ("stat_comparison", {
                        "items": comparison, "title": shorten(text, 60),
                    }, "editorial")
                if has_document:
                    return ("document_presentation", {
                        "title": shorten(text, 52),
                        "source_label": "Narration sentence",
                        "date_label": detect_date(text) or "",
                        "body": text,
                        "highlight": "",
                    }, "history")
                return None
            candidates = []
            if percent is not None:
                candidates.append(("data_gauge", {
                    "percent": percent, "label": shorten(text, 52),
                    "sublabel": text,
                }, _style_for("editorial")))
                candidates.append(("ring_badges", {
                    "badges": [{"value": f"{percent:g}%", "label": shorten(text, 44)}],
                }, _style_for("editorial")))
            if comparison:
                candidates.append(("bar_chart", {
                    "items": comparison, "title": shorten(text, 60),
                }, _style_for("editorial")))
                candidates.append(("stat_comparison", {
                    "items": comparison, "title": shorten(text, 60),
                }, _style_for("editorial")))
                candidates.append(("ring_badges", {
                    "badges": _badges_from_items(comparison),
                }, _style_for("editorial")))
            if has_document:
                candidates.append(("document_presentation", {
                    "title": shorten(text, 52),
                    "source_label": "Narration sentence",
                    "date_label": detect_date(text) or "",
                    "body": text,
                    "highlight": "",
                }, _style_for("history")))
            if not candidates:
                return None
            return _order_by_motif(candidates)[0]

        produced_family = None
        produced_reason = ""

        # Variety pre-pass: if the previous target used the stat
        # family and an engine alternative fits this one, try the
        # alternative FIRST (stat remains the fallback below).
        if _last_family == "stat" and comparison is None:
            _alt = _engine_alternative()
            if _alt and _make_engine_graphic(target, _alt[0], _alt[1], style=_alt[2]):
                produced_family = "motion_engine"
                produced_reason = f"variety_alternative:{_alt[0]}"

        # 1) Stat (broadened extraction). A comparison sentence is
        # NOT reduced to a single-value stat card — it falls
        # through to the stat_comparison template below.
        if produced_family is None and comparison is None:
            if _make_stat(
                target["scene_number"],
                target["sentence_index"],
                target["sentence_number"],
                text,
                targeted=True,
                target=target,
            ):
                produced_family = "stat"
                produced_reason = "stat_value"

        # 2) Chronology / timeline: this target is the timeline
        # graphic's MA-06 anchor and the timeline is unclaimed.
        if produced_family is None and has_date and not _timeline_claimed:
            if _timeline_asset is not None and _timeline_anchor_key == key:
                _timeline_claimed = True
                _timeline_asset["targeted"] = True
                produced_family = "timeline"
                produced_reason = "chronology_sentence"

        # 3) Process: sequence markers + process graphic unused.
        # The claim rebinds process_anchor (this loop runs in the
        # enclosing function scope), which the process render
        # stage below stamps onto the process asset.
        if produced_family is None and has_sequence and not _process_claimed:
            if _process_available:
                process_anchor = target
                process_anchor_reason = "sequence_sentence"
                _process_claimed = True
                produced_family = "process"
                produced_reason = "sequence_sentence"

        # 4) Event schematic (conservative trigger, motif-gated).
        _schematic_reason = None
        if produced_family is None:
            _schematic_reason = _make_schematic(target)
            if _schematic_reason is None:
                produced_family = "event_schematic"
                produced_reason = "schematic"

        # 5) Motion Engine templates. Order inside the stage:
        # stat_comparison, document_presentation, then data_gauge
        # (gauge fires here only for a percentage the stat path
        # did not take — e.g. a comparison sentence, or a stat
        # render failure).
        if produced_family is None:
            _candidates = []
            if _pack_id is not None:
                # Stage 5A/5B: pack-ordered candidates, including
                # the v2 bar_chart (multi-value comparisons) and
                # ring_badges; styles come from the pack's family.
                if comparison:
                    _candidates.append(("bar_chart", {
                        "items": comparison, "title": shorten(text, 60),
                    }, _style_for("editorial")))
                    _candidates.append(("stat_comparison", {
                        "items": comparison, "title": shorten(text, 60),
                    }, _style_for("editorial")))
                    _candidates.append(("ring_badges", {
                        "badges": _badges_from_items(comparison),
                    }, _style_for("editorial")))
                if has_document:
                    _candidates.append(("document_presentation", {
                        "title": shorten(text, 52),
                        "source_label": "Narration sentence",
                        "date_label": detect_date(text) or "",
                        "body": text, "highlight": "",
                    }, _style_for("history")))
                if percent is not None:
                    _candidates.append(("data_gauge", {
                        "percent": percent, "label": shorten(text, 52),
                        "sublabel": text,
                    }, _style_for("editorial")))
                    _candidates.append(("ring_badges", {
                        "badges": [{"value": f"{percent:g}%", "label": shorten(text, 44)}],
                    }, _style_for("editorial")))
                _candidates = _order_by_motif(_candidates)
            else:
                if comparison:
                    _candidates.append(("stat_comparison", {
                        "items": comparison, "title": shorten(text, 60),
                    }, "editorial"))
                if has_document:
                    _candidates.append(("document_presentation", {
                        "title": shorten(text, 52),
                        "source_label": "Narration sentence",
                        "date_label": detect_date(text) or "",
                        "body": text, "highlight": "",
                    }, "history"))
                if percent is not None:
                    _candidates.append(("data_gauge", {
                        "percent": percent, "label": shorten(text, 52),
                        "sublabel": text,
                    }, "editorial"))
            for _name, _data, _style in _candidates:
                if _make_engine_graphic(target, _name, _data, style=_style):
                    produced_family = "motion_engine"
                    produced_reason = f"motion_engine:{_name}"
                    break

        if produced_family is not None:
            covered.add(key)
            targeted_generated += 1
            _last_family = produced_family
            _record_outcome(target, "generated", produced_family, produced_reason)
        else:
            # Precise skip reason: a specific schematic reason wins;
            # otherwise the historical no_stat_value.
            _reason = _schematic_reason or "no_stat_value"
            if comparison:
                _reason = "comparison_not_rendered"
            target_skips.append(
                {
                    "scene_number": target["scene_number"],
                    "sentence_index": target["sentence_index"],
                    "sentence_number": target["sentence_number"],
                    "reason": _reason,
                }
            )
            _record_outcome(target, "skipped", None, _reason)

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

        # MA-01: steps are the events' "what"/action text
        # (shortened); "when" is only a sublabel.
        for event in timeline_events[:5]:
            what = clean_text(event.get("what", ""))
            if what:
                steps.append({
                    "label": shorten(what, 28),
                    "what": what,
                    "sublabel": clean_text(event.get("when", "")),
                    "when": clean_text(event.get("when", "")),
                })

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
                assets[-1]["anchor_reason"] = process_anchor_reason
                assets[-1]["family"] = "process"
                assets[-1]["process_steps"] = [
                    s["label"] for s in steps
                ]
                if process_anchor is None:
                    # MA-03: rendered but undeliverable — recorded
                    # as undelivered, not silently counted.
                    assets[-1]["delivered"] = False
                    undelivered.append({
                        "graphic_type": "process",
                        "reason": "no_anchor_no_targets",
                    })
                else:
                    assets[-1]["delivered"] = True
            except Exception as error:
                # AUD-21: skip this graphic; never fail the whole job.
                print(f"MOTION GRAPHIC SKIPPED (process): {error}")

    manifest = output_dir / "motion_graphics.json"

    manifest.write_text(
        json.dumps(
            {
                "mode": mode,
                "max_stats": max_stats,
                "genre_pack": _pack_id,
                "count": len(assets),
                "assets": assets,
                "targets": {
                    "requested": len(target_list),
                    "generated": targeted_generated,
                    "skipped": target_skips,
                    "outcomes": target_outcomes,
                },
                "undelivered": undelivered,
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


