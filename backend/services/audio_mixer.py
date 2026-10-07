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
    )

    print(result.stdout)

    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed with exit code {result.returncode}"
        )

    return result


def add_narration(
    video_file,
    narration_file,
    output_file=None,
):
    """
    Add narration audio to a rendered documentary video.
    """

    video_file = Path(video_file)
    narration_file = Path(narration_file)

    if not video_file.exists():
        raise FileNotFoundError(
            f"Video not found: {video_file}"
        )

    if not narration_file.exists():
        raise FileNotFoundError(
            f"Narration not found: {narration_file}"
        )

    if output_file is None:
        output_file = (
            video_file.parent /
            "documentary_with_narration.mp4"
        )

    output_file = Path(output_file)

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    print("\nAdding narration...")
    print(f"Video: {video_file}")
    print(f"Narration: {narration_file}")
    print(f"Output: {output_file}")

    duration_probe = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_file),
    ]

    duration_result = subprocess.run(
        duration_probe,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if duration_result.returncode != 0:
        raise RuntimeError(
            "Unable to determine rendered video duration."
        )

    try:
        video_duration = float(
            duration_result.stdout.strip()
        )
    except ValueError:
        raise RuntimeError(
            "Invalid video duration returned by ffprobe."
        )

    if video_duration <= 0:
        raise RuntimeError(
            "Rendered video duration must be greater than zero."
        )

    print(
        f"Rendered video duration: "
        f"{video_duration:.3f}s"
    )

    command = [
        "ffmpeg",
        "-y",

        "-i",
        str(video_file),

        "-i",
        str(narration_file),

        "-map",
        "0:v:0",

        "-map",
        "1:a:0",

        "-c:v",
        "copy",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-af",
        "apad",

        "-t",
        f"{video_duration:.3f}",

        "-movflags",
        "+faststart",

        str(output_file),
    ]

    run_ffmpeg(command)

    print(
        "\nNARRATION MIX COMPLETE"
    )

    print(
        f"Output: {output_file}"
    )

    return {
        "status": "audio_mixed",
        "output_file": str(output_file),
    }


if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser(
        description="Add narration to documentary video"
    )

    parser.add_argument(
        "video",
        help="Path to documentary MP4",
    )

    parser.add_argument(
        "narration",
        help="Path to narration WAV/MP3",
    )

    parser.add_argument(
        "--output",
        default=None,
        help="Output MP4 path",
    )

    args = parser.parse_args()

    result = add_narration(
        video_file=args.video,
        narration_file=args.narration,
        output_file=args.output,
    )

    print(
        json.dumps(
            result,
            indent=2,
        )
    )