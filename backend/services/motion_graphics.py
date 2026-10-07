import subprocess
from pathlib import Path


FONT = "Arial"
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


def add_title_card(
    video_file,
    output_file,
    title,
    subtitle=None,
    duration=4,
):
    """
    Adds a cinematic documentary title card
    to the beginning of a video.
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

    title_text = escape_drawtext(title)

    filters = []

    filters.append(
        "drawbox="
        "x=0:"
        "y=0:"
        "w=iw:"
        "h=ih:"
        "color=black@0.72:"
        "t=fill:"
        f"enable='between(t,0,{duration})'"
    )

    filters.append(
        "drawtext="
        f"font='{FONT}':"
        f"text='{title_text}':"
        "fontcolor=white:"
        "fontsize=76:"
        "x=(w-text_w)/2:"
        "y=(h-text_h)/2-30:"
        "alpha='if(lt(t,1),t,if(gt(t,3),4-t,1))':"
        f"enable='between(t,0,{duration})'"
    )

    if subtitle:

        subtitle_text = escape_drawtext(
            subtitle
        )

        filters.append(
            "drawtext="
            f"font='{FONT}':"
            f"text='{subtitle_text}':"
            "fontcolor=white@0.75:"
            "fontsize=30:"
            "x=(w-text_w)/2:"
            "y=(h-text_h)/2+70:"
            "alpha='if(lt(t,1),t,if(gt(t,3),4-t,1))':"
            f"enable='between(t,0,{duration})'"
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
    print("TITLE CARD COMPLETE")
    print("========================================")
    print(f"Output: {output_file}")

    return str(output_file)


def add_lower_third(
    video_file,
    output_file,
    text,
    start=0,
    duration=5,
):
    """
    Adds a clean documentary lower-third.
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

    text = escape_drawtext(text)

    end = start + duration

    filter_complex = (
        "drawbox="
        "x=70:"
        "y=h-220:"
        "w=620:"
        "h=90:"
        "color=black@0.72:"
        "t=fill:"
        f"enable='between(t,{start},{end})',"

        "drawtext="
        f"font='{FONT}':"
        f"text='{text}':"
        "fontcolor=white:"
        "fontsize=38:"
        "x=100:"
        "y=h-195:"
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
    print("LOWER THIRD COMPLETE")
    print(f"Output: {output_file}")

    return str(output_file)


def add_date_overlay(
    video_file,
    output_file,
    date_text,
    start=0,
    duration=5,
):
    """
    Adds a documentary-style date/location overlay.
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

    date_text = escape_drawtext(
        date_text
    )

    end = start + duration

    filter_complex = (
        "drawtext="
        f"font='{FONT}':"
        f"text='{date_text}':"
        "fontcolor=white@0.85:"
        "fontsize=32:"
        "x=90:"
        "y=90:"
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
    print("DATE OVERLAY COMPLETE")
    print(f"Output: {output_file}")

    return str(output_file)


def escape_drawtext(text):
    """
    Escape characters that have special meaning
    inside FFmpeg drawtext filters.
    """

    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


if __name__ == "__main__":

    project_dir = Path(
        "media/15e0f37d-c3a3-4001-a801-e9f7007cb627"
    )

    input_video = (
        project_dir
        / "documentary_captioned.mp4"
    )

    output_video = (
        project_dir
        / "documentary_graphics.mp4"
    )

    add_title_card(
        video_file=input_video,
        output_file=output_video,
        title="THE NIGHT THE WORLD ALMOST ENDED",
        subtitle="Stanislav Petrov • September 26, 1983",
        duration=4,
    )