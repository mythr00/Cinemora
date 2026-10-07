import json
import subprocess
from pathlib import Path


def run_ffmpeg(command):
    print("\nRunning FFmpeg:")
    print(" ".join(str(x) for x in command))

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


def create_ass_file(
    captions_file,
    output_file,
):
    captions_file = Path(captions_file)
    output_file = Path(output_file)

    with open(
        captions_file,
        "r",
        encoding="utf-8",
    ) as file:
        data = json.load(file)

    lines = []

    lines.append("[Script Info]")
    lines.append("ScriptType: v4.00+")
    lines.append("PlayResX: 1920")
    lines.append("PlayResY: 1080")
    lines.append("ScaledBorderAndShadow: yes")
    lines.append("")

    lines.append("[V4+ Styles]")
    lines.append(
        "Format: Name, Fontname, Fontsize, PrimaryColour, "
        "SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, "
        "Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding"
    )

    lines.append(
        "Style: Default,Arial,58,"
        "&H00FFFFFF,"
        "&H00FFFFFF,"
        "&H00000000,"
        "&H99000000,"
        "1,0,0,0,100,100,0,0,1,3,2,2,80,80,110,1"
    )

    lines.append("")

    lines.append("[Events]")
    lines.append(
        "Format: Layer, Start, End, Style, Name, "
        "MarginL, MarginR, MarginV, Effect, Text"
    )

    for segment in data["segments"]:

        words = segment["words"]

        for index, word in enumerate(words):

            start = word["start"]
            end = word["end"]

            text_parts = []

            start_index = max(
                0,
                index - 3,
            )

            end_index = min(
                len(words),
                index + 4,
            )

            for i in range(
                start_index,
                end_index,
            ):
                current = words[i]["word"]

                if i == index:
                    current = (
                        r"{\c&H00FFFF&}"
                        + current
                        + r"{\c&HFFFFFF&}"
                    )

                text_parts.append(current)

            text = " ".join(text_parts)

            start_time = format_ass_time(start)
            end_time = format_ass_time(end)

            lines.append(
                f"Dialogue: 0,"
                f"{start_time},"
                f"{end_time},"
                f"Default,,"
                f"0,0,0,0,"
                f"{text}"
            )

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with open(
        output_file,
        "w",
        encoding="utf-8-sig",
    ) as file:
        file.write(
            "\n".join(lines)
        )

    print()
    print("ASS CAPTIONS CREATED")
    print(f"Output: {output_file}")


def format_ass_time(seconds):
    hours = int(seconds // 3600)

    minutes = int(
        (seconds % 3600) // 60
    )

    secs = int(
        seconds % 60
    )

    centiseconds = int(
        round(
            (seconds - int(seconds))
            * 100
        )
    )

    if centiseconds >= 100:
        centiseconds = 0
        secs += 1

    if secs >= 60:
        secs = 0
        minutes += 1

    if minutes >= 60:
        minutes = 0
        hours += 1

    return (
        f"{hours}:"
        f"{minutes:02d}:"
        f"{secs:02d}."
        f"{centiseconds:02d}"
    )


def burn_captions(
    video_file,
    ass_file,
    output_file,
):
    video_file = Path(video_file)
    ass_file = Path(ass_file)
    output_file = Path(output_file)

    if not video_file.exists():
        raise FileNotFoundError(
            f"Video not found: {video_file}"
        )

    if not ass_file.exists():
        raise FileNotFoundError(
            f"ASS file not found: {ass_file}"
        )

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # FFmpeg's ASS filter treats ":" as an option separator.
    # Use a path relative to the backend working directory whenever possible.
    try:
        ass_filter_path = ass_file.resolve().relative_to(
            Path.cwd().resolve()
        ).as_posix()
    except ValueError:
        ass_filter_path = ass_file.resolve().as_posix()
        ass_filter_path = ass_filter_path.replace(":", r"\\:")

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_file),
        "-vf",
        f"ass={ass_filter_path}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "20",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(output_file),
    ]

    run_ffmpeg(command)

    print()
    print("========================================")
    print("CAPTIONS BURNED")
    print("========================================")
    print(f"Output: {output_file}")


if __name__ == "__main__":

    project_dir = Path(
        "media/15e0f37d-c3a3-4001-a801-e9f7007cb627"
    )

    captions_json = (
        project_dir / "captions.json"
    )

    ass_file = (
        project_dir / "captions.ass"
    )

    input_video = (
        project_dir / "documentary_final.mp4"
    )

    output_video = (
        project_dir / "documentary_captioned.mp4"
    )

    create_ass_file(
        captions_json,
        ass_file,
    )

    burn_captions(
        input_video,
        ass_file,
        output_video,
    )