import json
import subprocess
from pathlib import Path


# ==================================================
# SETTINGS
# ==================================================

# Final audio bitrate
AUDIO_BITRATE = "192k"

# Background music base volume.
# 0.10 = 10%
MUSIC_VOLUME = 0.10

# How strongly the music is reduced when narration plays.
DUCK_RATIO = 8

# How quickly music ducks down.
DUCK_ATTACK = 20

# How quickly music comes back up.
DUCK_RELEASE = 1200


# ==================================================
# FFMPEG
# ==================================================

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

    return result


# ==================================================
# ADD BACKGROUND MUSIC
# ==================================================

def add_background_music(
    video_file,
    music_file,
    output_file=None,
    music_volume=MUSIC_VOLUME,
):
    """
    Add background music underneath documentary narration.

    The narration remains clear while the music automatically
    becomes quieter whenever narration is playing.

    Input:
        documentary_with_narration.mp4
        background.mp3

    Output:
        documentary_final.mp4
    """

    video_file = Path(video_file)
    music_file = Path(music_file)

    # --------------------------------------------------
    # Check video
    # --------------------------------------------------

    if not video_file.exists():
        raise FileNotFoundError(
            f"Video not found: {video_file}"
        )

    # --------------------------------------------------
    # Check music
    # --------------------------------------------------

    if not music_file.exists():
        raise FileNotFoundError(
            f"Music not found: {music_file}"
        )

    # --------------------------------------------------
    # Default output
    # --------------------------------------------------

    if output_file is None:
        output_file = (
            video_file.parent
            / "documentary_final.mp4"
        )

    output_file = Path(output_file)

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    print("\n========================================")
    print("ADDING BACKGROUND MUSIC")
    print("========================================")

    print(f"Video: {video_file}")
    print(f"Music: {music_file}")
    print(f"Music volume: {music_volume}")
    print(f"Output: {output_file}")

    # ==================================================
    # MASTER VIDEO DURATION
    # ==================================================

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
            "Unable to determine documentary video duration."
        )

    try:
        video_duration = float(
            duration_result.stdout.strip()
        )
    except ValueError:
        raise RuntimeError(
            "Invalid documentary video duration returned by ffprobe."
        )

    if video_duration <= 0:
        raise RuntimeError(
            "Documentary video duration must be greater than zero."
        )

    print(
        f"Master documentary duration: "
        f"{video_duration:.3f}s"
    )

    # ==================================================
    # AUDIO FILTER
    # ==================================================

    filter_complex = (
        # ----------------------------------------------
        # Background music
        # ----------------------------------------------
        f"[1:a]"
        f"volume={music_volume},"
        "aresample=48000,"
        "asetpts=N/SR/TB"
        "[music];"

        # ----------------------------------------------
        # Narration
        # ----------------------------------------------
        "[0:a]"
        "aresample=48000,"
        "asetpts=N/SR/TB"
        "[voice];"

        # ----------------------------------------------
        # Duck music under narration
        # ----------------------------------------------
        "[music][voice]"
        "sidechaincompress="
        "threshold=0.03:"
        f"ratio={DUCK_RATIO}:"
        f"attack={DUCK_ATTACK}:"
        f"release={DUCK_RELEASE}:"
        "makeup=1"
        "[ducked_music];"

        # ----------------------------------------------
        # Mix narration + music
        # ----------------------------------------------
        "[voice][ducked_music]"
        "amix="
        "inputs=2:"
        "duration=first:"
        "dropout_transition=2:"
        "normalize=0"
        "[aout]"
    )

    # ==================================================
    # FFMPEG COMMAND
    # ==================================================

    command = [
        "ffmpeg",
        "-y",

        # Documentary containing narration
        "-i",
        str(video_file),

        # Loop background music
        "-stream_loop",
        "-1",
        "-i",
        str(music_file),

        # Audio processing
        "-filter_complex",
        filter_complex,

        # Keep documentary video
        "-map",
        "0:v:0",

        # Use processed audio
        "-map",
        "[aout]",

        # Do NOT re-encode video
        "-c:v",
        "copy",

        # Encode final audio
        "-c:a",
        "aac",

        "-b:a",
        AUDIO_BITRATE,

        # Preserve the documentary's exact duration.
        "-t",
        f"{video_duration:.3f}",

        # Make MP4 web-friendly
        "-movflags",
        "+faststart",

        str(output_file),
    ]

    # ==================================================
    # RUN
    # ==================================================

    run_ffmpeg(command)

    # ==================================================
    # COMPLETE
    # ==================================================

    print("\n========================================")
    print("BACKGROUND MUSIC COMPLETE")
    print("========================================")

    print(f"Output: {output_file}")

    return {
        "status": "music_added",
        "output_file": str(output_file),
    }


# ==================================================
# COMMAND LINE
# ==================================================

if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Add background music to a documentary "
            "with automatic narration ducking."
        )
    )

    parser.add_argument(
        "video",
        help="Input documentary MP4",
    )

    parser.add_argument(
        "music",
        help="Background music MP3 or WAV",
    )

    parser.add_argument(
        "--output",
        default=None,
        help="Output MP4 path",
    )

    parser.add_argument(
        "--volume",
        type=float,
        default=MUSIC_VOLUME,
        help=(
            "Background music volume from 0.0 to 1.0. "
            "Default: 0.10"
        ),
    )

    args = parser.parse_args()

    result = add_background_music(
        video_file=args.video,
        music_file=args.music,
        output_file=args.output,
        music_volume=args.volume,
    )

    print(
        json.dumps(
            result,
            indent=2,
        )
    )
