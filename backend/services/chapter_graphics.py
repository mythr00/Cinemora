import subprocess
from pathlib import Path


WIDTH = 1920
HEIGHT = 1080

FPS = 30
CRF = 20
PRESET = "ultrafast"


def run_ffmpeg(command):
    print()
    print("Running FFmpeg:")
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


def escape(text):
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def add_chapter_card(
    video_file,
    output_file,
    chapter_number,
    title,
    subtitle=None,
    start=0,
    duration=4,
):
    """
    Adds a cinematic chapter card over the video.
    """

    video_file = Path(video_file)
    output_file = Path(output_file)

    if not video_file.exists():
        raise FileNotFoundError(
            f"Video not found: {video_file}"
        )

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    end = start + duration

    chapter_text = escape(
        f"CHAPTER {chapter_number}"
    )

    title_text = escape(title)

    filters = []

    filters.append(
        "drawbox="
        "x=0:"
        "y=0:"
        "w=iw:"
        "h=ih:"
        "color=black@0.82:"
        "t=fill:"
        f"enable='between(t,{start},{end})'"
    )

    filters.append(
        "drawtext="
        "font='Arial':"
        f"text='{chapter_text}':"
        "fontcolor=white@0.65:"
        "fontsize=30:"
        "x=(w-text_w)/2:"
        "y=390:"
        f"enable='between(t,{start},{end})'"
    )

    filters.append(
        "drawtext="
        "font='Arial':"
        f"text='{title_text}':"
        "fontcolor=white:"
        "fontsize=72:"
        "x=(w-text_w)/2:"
        "y=440:"
        f"enable='between(t,{start},{end})'"
    )

    if subtitle:

        subtitle_text = escape(
            subtitle
        )

        filters.append(
            "drawtext="
            "font='Arial':"
            f"text='{subtitle_text}':"
            "fontcolor=white@0.70:"
            "fontsize=30:"
            "x=(w-text_w)/2:"
            "y=535:"
            f"enable='between(t,{start},{end})'"
        )

    filter_complex = ",".join(filters)

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_file),
        "-vf",
        filter_complex,
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

    run_ffmpeg(command)

    print()
    print("========================================")
    print("CHAPTER CARD COMPLETE")
    print("========================================")
    print(f"Output: {output_file}")

    return str(output_file)


def add_timeline_event(
    video_file,
    output_file,
    event_text,
    year,
    start=0,
    duration=6,
):
    """
    Adds a documentary timeline event.
    """

    video_file = Path(video_file)
    output_file = Path(output_file)

    if not video_file.exists():
        raise FileNotFoundError(
            f"Video not found: {video_file}"
        )

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    end = start + duration

    year_text = escape(year)
    event_text = escape(event_text)

    filter_complex = (
        # horizontal timeline
        "drawbox="
        "x=250:"
        "y=h-260:"
        "w=1420:"
        "h=6:"
        "color=white@0.45:"
        "t=fill:"
        f"enable='between(t,{start},{end})',"

        # timeline marker
        "drawbox="
        "x=940:"
        "y=h-285:"
        "w=12:"
        "h=56:"
        "color=white:"
        "t=fill:"
        f"enable='between(t,{start},{end})',"

        # year
        "drawtext="
        "font='Arial':"
        f"text='{year_text}':"
        "fontcolor=white:"
        "fontsize=42:"
        "x=860:"
        "y=h-360:"
        f"enable='between(t,{start},{end})',"

        # event
        "drawtext="
        "font='Arial':"
        f"text='{event_text}':"
        "fontcolor=white@0.85:"
        "fontsize=30:"
        "x=(w-text_w)/2:"
        "y=h-210:"
        f"enable='between(t,{start},{end})'"
    )

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_file),
        "-vf",
        filter_complex,
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

    run_ffmpeg(command)

    print()
    print("========================================")
    print("TIMELINE EVENT COMPLETE")
    print("========================================")
    print(f"Output: {output_file}")

    return str(output_file)


if __name__ == "__main__":

    project_dir = Path(
        "media/15e0f37d-c3a3-4001-a801-e9f7007cb627"
    )

    input_video = (
        project_dir
        / "documentary_graphics_final.mp4"
    )

    output_video = (
        project_dir
        / "documentary_chapters.mp4"
    )

    add_chapter_card(
        video_file=input_video,
        output_file=output_video,
        chapter_number=1,
        title="THE WARNING",
        subtitle="A Soviet early-warning system detects an incoming attack",
        start=16,
        duration=4,
    )