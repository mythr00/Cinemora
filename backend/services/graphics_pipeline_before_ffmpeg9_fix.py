import json
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
# TEXT HELPERS
# =========================================================

def escape_drawtext(text):
    """
    Escape text for FFmpeg drawtext.
    """
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("%", "\\%")
    )


def clean_text(value):
    if value is None:
        return ""

    value = str(value)

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

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
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE,
        )

        if match:
            return match.group(0)

    return None


def detect_entity(text):
    """
    Conservative entity extraction from asset metadata.

    This intentionally avoids inventing facts.
    """
    text = clean_text(text)

    if not text:
        return None

    # People / named organizations frequently appearing
    # in documentary asset titles.
    patterns = [
        r"\bSam Bankman-Fried\b",
        r"\bFTX\b",
        r"\bAlameda Research\b",
        r"\bStanislav Petrov\b",
        r"\bJohn F\. Kennedy\b",
        r"\bSoviet Union\b",
        r"\bUnited States\b",
        r"\bCold War\b",
        r"\bCuba\b",
        r"\bMoscow\b",
        r"\bUkraine\b",
        r"\bRussia\b",
        r"\bChina\b",
        r"\bUnited Kingdom\b",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE,
        )

        if match:
            return match.group(0)

    return None


def source_label(item):
    source = clean_text(
        item.get(
            "source",
            "",
        )
    )

    if not source:
        return ""

    source_map = {
        "YouTube": "SOURCE • YOUTUBE",
        "Financial Times": "SOURCE • FINANCIAL TIMES",
        "Bloomberg.com": "SOURCE • BLOOMBERG",
        "National Geographic Documentary Films":
            "SOURCE • NATIONAL GEOGRAPHIC",
        "Reveal News":
            "SOURCE • REVEAL",
        "MasterClass":
            "SOURCE • MASTERCLASS",
        "Columbia University Press":
            "SOURCE • COLUMBIA UNIVERSITY PRESS",
        "Bloomsbury":
            "SOURCE • BLOOMSBURY",
    }

    return source_map.get(
        source,
        f"SOURCE • {source.upper()}",
    )


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

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    print(result.stdout)

    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed with exit code {result.returncode}"
        )


# =========================================================
# TIMELINE
# =========================================================

def load_timeline(timeline_file):
    timeline_file = Path(timeline_file)

    if not timeline_file.exists():
        raise FileNotFoundError(
            f"Timeline not found: {timeline_file}"
        )

    with open(
        timeline_file,
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


# =========================================================
# GRAPHICS PLAN
# =========================================================

def build_graphics_plan(data):
    """
    Convert the raw timeline into a deterministic graphics plan.

    The plan is saved so the Motion Designer stage has an
    inspectable artifact instead of hiding everything inside
    one FFmpeg command.
    """

    scenes = data.get(
        "scenes",
        [],
    )

    plan = {
        "project_id": data.get(
            "project_id"
        ),
        "duration_seconds": float(
            data.get(
                "duration_seconds",
                0,
            )
        ),
        "graphics": [],
    }

    project_time = 0.0

    for scene in scenes:

        scene_number = int(
            scene.get(
                "scene_number",
                0,
            )
        )

        duration = float(
            scene.get(
                "duration_seconds",
                0,
            )
        )

        items = scene.get(
            "items",
            scene.get(
                "visuals",
                [],
            ),
        )

        # -------------------------------------------------
        # Scene opener
        # -------------------------------------------------

        if duration >= 3:

            plan["graphics"].append(
                {
                    "type": "scene_marker",
                    "scene_number": scene_number,
                    "start": project_time,
                    "end": min(
                        project_time + 2.4,
                        project_time + duration,
                    ),
                    "text": (
                        f"SCENE {scene_number:02d}"
                    ),
                }
            )

        # -------------------------------------------------
        # Individual visual graphics
        # -------------------------------------------------

        for item in items:

            start = project_time + float(
                item.get(
                    "start",
                    0,
                )
            )

            end = project_time + float(
                item.get(
                    "end",
                    item.get(
                        "duration",
                        1,
                    ),
                )
            )

            item_title = clean_text(
                item.get(
                    "title",
                    "",
                )
            )

            item_source = clean_text(
                item.get(
                    "source",
                    "",
                )
            )

            combined = (
                f"{item_title} "
                f"{item_source}"
            )

            entity = detect_entity(
                combined
            )

            date = detect_date(
                combined
            )

            source = source_label(
                item
            )

            # ---------------------------------------------
            # Entity lower third
            # ---------------------------------------------

            if entity:

                plan["graphics"].append(
                    {
                        "type": "entity",
                        "scene_number": scene_number,
                        "start": max(
                            project_time,
                            start,
                        ),
                        "end": min(
                            project_time + duration,
                            start + 3.5,
                        ),
                        "text": entity.upper(),
                        "subtext": source,
                    }
                )

            # ---------------------------------------------
            # Date card
            # ---------------------------------------------

            if date:

                plan["graphics"].append(
                    {
                        "type": "date",
                        "scene_number": scene_number,
                        "start": start,
                        "end": min(
                            end,
                            start + 3.0,
                        ),
                        "text": date.upper(),
                    }
                )

            # ---------------------------------------------
            # Source/context tag
            # ---------------------------------------------

            if source and not entity:

                plan["graphics"].append(
                    {
                        "type": "source",
                        "scene_number": scene_number,
                        "start": start + 0.15,
                        "end": min(
                            end,
                            start + 2.4,
                        ),
                        "text": source,
                    }
                )

        project_time += duration

    # -----------------------------------------------------
    # Global progress bar
    # -----------------------------------------------------

    plan["graphics"].append(
        {
            "type": "progress",
            "start": 0,
            "end": plan["duration_seconds"],
        }
    )

    return plan


# =========================================================
# FILTER BUILDERS
# =========================================================

def add_scene_marker(filters, graphic):
    start = graphic["start"]
    end = graphic["end"]

    text = escape_drawtext(
        graphic["text"]
    )

    # Animated dark panel.
    filters.append(
        "drawbox="
        "x=70:"
        "y=70:"
        "w=240:"
        "h=64:"
        "color=black@0.72:"
        "t=fill:"
        f"enable='between(t,{start},{end})'"
    )

    # Accent line that grows into place.
    filters.append(
        "drawbox="
        "x=70:"
        "y=134:"
        "w=240:"
        "h=4:"
        "color=white@0.9:"
        "t=fill:"
        f"enable='between(t,{start},{end})'"
    )

    filters.append(
        "drawtext="
        "font='Arial':"
        f"text='{text}':"
        "fontcolor=white:"
        "fontsize=28:"
        "x=92:"
        "y=88:"
        f"enable='between(t,{start},{end})'"
    )


def add_entity_graphic(filters, graphic):
    start = graphic["start"]
    end = graphic["end"]

    text = escape_drawtext(
        graphic["text"]
    )

    subtext = escape_drawtext(
        graphic.get(
            "subtext",
            "",
        )
    )

    # Lower-third panel.
    filters.append(
        "drawbox="
        "x=70:"
        "y=h-190:"
        "w=650:"
        "h=110:"
        "color=black@0.78:"
        "t=fill:"
        f"enable='between(t,{start},{end})'"
    )

    # Accent stripe.
    filters.append(
        "drawbox="
        "x=70:"
        "y=h-190:"
        "w=8:"
        "h=110:"
        "color=white@0.95:"
        "t=fill:"
        f"enable='between(t,{start},{end})'"
    )

    filters.append(
        "drawtext="
        "font='Arial':"
        f"text='{text}':"
        "fontcolor=white:"
        "fontsize=34:"
        "x=105:"
        "y=h-165:"
        f"enable='between(t,{start},{end})'"
    )

    if subtext:

        filters.append(
            "drawtext="
            "font='Arial':"
            f"text='{subtext}':"
            "fontcolor=white@0.62:"
            "fontsize=21:"
            "x=105:"
            "y=h-122:"
            f"enable='between(t,{start},{end})'"
        )


def add_date_graphic(filters, graphic):
    start = graphic["start"]
    end = graphic["end"]

    text = escape_drawtext(
        graphic["text"]
    )

    filters.append(
        "drawbox="
        "x=70:"
        "y=170:"
        "w=430:"
        "h=58:"
        "color=black@0.70:"
        "t=fill:"
        f"enable='between(t,{start},{end})'"
    )

    filters.append(
        "drawtext="
        "font='Arial':"
        f"text='{text}':"
        "fontcolor=white:"
        "fontsize=27:"
        "x=95:"
        "y=186:"
        f"enable='between(t,{start},{end})'"
    )


def add_source_graphic(filters, graphic):
    start = graphic["start"]
    end = graphic["end"]

    text = escape_drawtext(
        graphic["text"]
    )

    filters.append(
        "drawtext="
        "font='Arial':"
        f"text='{text}':"
        "fontcolor=white@0.55:"
        "fontsize=18:"
        "x=w-text_w-55:"
        "y=h-48:"
        f"enable='between(t,{start},{end})'"
    )


def add_progress_graphic(filters, graphic):
    start = graphic["start"]
    end = graphic["end"]

    # Thin documentary-style progress bar.
    filters.append(
        "drawbox="
        "x=0:"
        "y=h-7:"
        "w=iw:"
        "h=7:"
        "color=black@0.55:"
        "t=fill:"
        f"enable='between(t,{start},{end})'"
    )

    # Animated white progress indicator.
    filters.append(
        "drawbox="
        "x=0:"
        "y=h-7:"
        "w='iw*(t/{duration})':"
        "h=7:"
        "color=white@0.9:"
        "t=fill:"
        f"enable='between(t,{start},{end})':"
        .replace(
            "{duration}",
            str(
                max(
                    float(
                        graphic.get(
                            "end",
                            1,
                        )
                    ),
                    0.01,
                )
            ),
        )
    )


# =========================================================
# MAIN GRAPHICS RENDER
# =========================================================

def build_graphics(
    video_file,
    timeline_file,
    output_file,
):
    """
    Render timeline-aware motion graphics onto a real video.

    The function deliberately requires a real video input.
    This prevents the Motion Designer stage from silently
    succeeding without producing anything useful.
    """

    video_file = Path(video_file)
    timeline_file = Path(timeline_file)
    output_file = Path(output_file)

    if not video_file.exists():
        raise FileNotFoundError(
            f"Motion Designer requires a real video input: {video_file}"
        )

    if not timeline_file.exists():
        raise FileNotFoundError(
            f"Timeline not found: {timeline_file}"
        )

    data = load_timeline(
        timeline_file
    )

    plan = build_graphics_plan(
        data
    )

    duration = float(
        data.get(
            "duration_seconds",
            0,
        )
    )

    print()
    print("=" * 70)
    print("MOTION DESIGNER")
    print("=" * 70)
    print(
        f"Scenes: {len(data.get('scenes', []))}"
    )
    print(
        f"Graphics elements: {len(plan['graphics'])}"
    )
    print(
        f"Duration: {duration:.2f}s"
    )
    print()

    # -----------------------------------------------------
    # Save inspectable graphics plan
    # -----------------------------------------------------

    plan_file = output_file.parent / "graphics_plan.json"

    with open(
        plan_file,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            plan,
            file,
            indent=2,
            ensure_ascii=False,
        )

    print(
        f"Graphics plan: {plan_file}"
    )

    # -----------------------------------------------------
    # Build FFmpeg filters
    # -----------------------------------------------------

    filters = []

    for graphic in plan["graphics"]:

        graphic_type = graphic["type"]

        if graphic_type == "scene_marker":
            add_scene_marker(
                filters,
                graphic,
            )

        elif graphic_type == "entity":
            add_entity_graphic(
                filters,
                graphic,
            )

        elif graphic_type == "date":
            add_date_graphic(
                filters,
                graphic,
            )

        elif graphic_type == "source":
            add_source_graphic(
                filters,
                graphic,
            )

        elif graphic_type == "progress":
            add_progress_graphic(
                filters,
                graphic,
            )

    if not filters:
        raise RuntimeError(
            "Motion Designer generated zero graphics."
        )

    filter_complex = ",".join(
        filters
    )

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # -----------------------------------------------------
    # Render
    # -----------------------------------------------------

    # -----------------------------------------------------
    # Windows-safe FFmpeg filter script
    # -----------------------------------------------------
    # The graphics filter can contain thousands of drawtext
    # filters. Passing the entire filter graph through the
    # Windows command line causes WinError 206.
    #
    # Write the filter graph to a file instead.
    # -----------------------------------------------------

    filter_script = output_file.parent / "graphics_filter.ffscript"

    filter_script.write_text(
        filter_complex,
        encoding="utf-8",
    )

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_file),
        "-filter_complex_script",
        str(filter_script),
        "-map",
        "0:v:0",
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
        str(output_file),
    ]

    run_ffmpeg(
        command
    )

    try:
        filter_script.unlink()
    except OSError:
        pass

    if not output_file.exists():
        raise RuntimeError(
            "Motion Designer finished but no output video was created."
        )

    size = output_file.stat().st_size

    if size <= 0:
        raise RuntimeError(
            "Motion Designer created an empty output file."
        )

    print()
    print("=" * 70)
    print("MOTION DESIGNER COMPLETE")
    print("=" * 70)
    print(
        f"Graphics elements: {len(plan['graphics'])}"
    )
    print(
        f"Output: {output_file}"
    )
    print(
        f"Size: {size:,} bytes"
    )
    print()

    return str(output_file)


# =========================================================
# DIRECT TEST
# =========================================================

if __name__ == "__main__":

    project_dir = Path(
        "media/3e06bb3d-8c9f-4e37-8d4a-40940d88bce2"
    )

    build_graphics(
        video_file=(
            project_dir
            / "documentary_captioned.mp4"
        ),
        timeline_file=(
            project_dir
            / "timeline.json"
        ),
        output_file=(
            project_dir
            / "motion_designed.mp4"
        ),
    )