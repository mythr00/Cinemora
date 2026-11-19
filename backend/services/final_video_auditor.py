from pathlib import Path
import json
import subprocess


def _ffprobe(args):
    result = subprocess.run(
        ["ffprobe", "-v", "error", *args],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "ffprobe failed:\n"
            + result.stderr.strip()
        )

    return result.stdout.strip()


def probe_final_video(video_file):
    video_file = Path(video_file)

    if not video_file.exists():
        raise FileNotFoundError(
            f"Final video not found: {video_file}"
        )

    if video_file.stat().st_size <= 0:
        raise RuntimeError(
            f"Final video is empty: {video_file}"
        )

    duration_text = _ffprobe([
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_file),
    ])

    streams_text = _ffprobe([
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,r_frame_rate",
        "-of",
        "json",
        str(video_file),
    ])

    try:
        duration = float(duration_text)
    except ValueError:
        raise RuntimeError(
            f"Invalid final video duration: {duration_text}"
        )

    data = json.loads(streams_text)

    video_stream = None
    audio_stream = None

    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and video_stream is None:
            video_stream = stream

        if stream.get("codec_type") == "audio" and audio_stream is None:
            audio_stream = stream

    if video_stream is None:
        raise RuntimeError(
            "Final video contains no video stream."
        )

    if audio_stream is None:
        raise RuntimeError(
            "Final video contains no audio stream."
        )

    width = video_stream.get("width")
    height = video_stream.get("height")

    if width != 1920 or height != 1080:
        raise RuntimeError(
            f"Final video resolution is {width}x{height}; "
            "expected 1920x1080."
        )

    return {
        "path": str(video_file),
        "size_bytes": video_file.stat().st_size,
        "duration_seconds": duration,
        "video_codec": video_stream.get("codec_name"),
        "audio_codec": audio_stream.get("codec_name"),
        "width": width,
        "height": height,
        "frame_rate": video_stream.get("r_frame_rate"),
    }


def validate_final_video(
    video_file,
    expected_duration=None,
    max_duration_delta=5.0,
    asset_report=None,
):
    result = probe_final_video(video_file)

    # AUD-23: the auditor consumes the Asset Manager report. A technically
    # valid file is NOT the same as genuine documentary footage — when the
    # report shows substitutions, the audit says so, in the result and in
    # the log, instead of passing the video as 100% genuine.
    if asset_report is not None:
        substitutions = asset_report.get("substitutions") or {}
        result["asset_report"] = {
            "total_accepted_assets": asset_report.get("total_accepted_assets"),
            "verified_moving_footage": asset_report.get("verified_moving_footage"),
            "verified_images": asset_report.get("verified_images"),
            "unknown_licensing": asset_report.get("unknown_licensing"),
            "substitutions": substitutions,
        }
        result["genuine_footage_only"] = bool(asset_report.get("genuine_footage_only"))
        if not result["genuine_footage_only"]:
            result["genuineness_warnings"] = [
                f"{name}={count}"
                for name, count in substitutions.items()
                if count
            ]
            print()
            print("!" * 70)
            print(
                "ASSET GENUINENESS: NOT 100% GENUINE FOOTAGE — "
                + ", ".join(result["genuineness_warnings"])
            )
            print("!" * 70)

    if expected_duration is not None:
        expected_duration = float(expected_duration)

        delta = abs(
            result["duration_seconds"] - expected_duration
        )

        result["expected_duration_seconds"] = expected_duration
        result["duration_delta_seconds"] = delta

        if delta > max_duration_delta:
            raise RuntimeError(
                "Final video duration mismatch: "
                f"{result['duration_seconds']:.3f}s vs "
                f"expected {expected_duration:.3f}s "
                f"(delta {delta:.3f}s)."
            )

    print()
    print("=" * 70)
    print("FINAL VIDEO AUDIT PASSED")
    print("=" * 70)
    print(f"Video: {result['path']}")
    print(f"Size: {result['size_bytes']} bytes")
    print(f"Duration: {result['duration_seconds']:.3f}s")

    if expected_duration is not None:
        print(
            f"Expected: {result['expected_duration_seconds']:.3f}s"
        )
        print(
            f"Duration delta: "
            f"{result['duration_delta_seconds']:.3f}s"
        )

    print(
        f"Video: {result['video_codec']} "
        f"{result['width']}x{result['height']}"
    )
    print(f"Frame rate: {result['frame_rate']}")
    print(f"Audio: {result['audio_codec']}")

    return result
