"""Real media probing for the Asset Manager (AUD-17).

Before this module, a downloaded "video" was considered valid if the file
existed and was at least 10,000 bytes, and every asset was stamped with a
fabricated ``duration_seconds = 8`` regardless of the file's real contents.
Corrupt-but-large files passed verification and silently became placeholder
clips at render time.

This module is the single place that answers "what is actually in this
file?" using ffprobe (video) and Pillow (images):

- ``probe_video(path)`` -> dict with the REAL duration, resolution, fps,
  codec and audio presence, or ``None`` when the file is missing, unreadable,
  has no video stream, or reports no usable duration.
- ``probe_audio(path)`` -> dict with the REAL duration, codec, sample rate
  and channels, or ``None`` when the file is missing, unreadable, has no
  audio stream, or reports no usable duration (Stage 5 / R4: the probing
  authority for narration part durations).
- ``probe_image(path)`` -> dict with real dimensions/format, or ``None``.
- ``extract_sample_frames(path, ...)`` -> the sampled frame files of a
  video (Phase 1.1 frame-level verification's frame source), or an
  empty list when no frame could be sampled.

Nothing here fabricates values: unknown stays ``None`` and callers must
treat ``None`` as "not a verified asset".
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from PIL import Image, UnidentifiedImageError

_FFPROBE_TIMEOUT_SECONDS = 30


def _parse_fps(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        if "/" in raw:
            num, den = raw.split("/", 1)
            den_f = float(den)
            if den_f == 0:
                return None
            return round(float(num) / den_f, 3)
        return round(float(raw), 3)
    except (TypeError, ValueError):
        return None


def probe_video(path) -> dict | None:
    """Return real stream facts for a video file, or None if unverifiable."""
    path = Path(path)
    if not path.exists() or not path.is_file():
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        return None

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return None

    duration = None
    fmt = data.get("format") or {}
    for raw in (fmt.get("duration"), video.get("duration")):
        try:
            if raw is not None:
                duration = float(raw)
                break
        except (TypeError, ValueError):
            continue
    if duration is None or duration <= 0:
        return None

    try:
        width = int(video.get("width") or 0) or None
        height = int(video.get("height") or 0) or None
    except (TypeError, ValueError):
        width = height = None

    return {
        "duration_seconds": round(duration, 3),
        "width": width,
        "height": height,
        "fps": _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        "codec": video.get("codec_name"),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def probe_audio(path) -> dict | None:
    """Return real stream facts for an audio file, or None if
    unverifiable (Stage 5 / R4, annex D2.1).

    The audio sibling of probe_video: narration parts are WAV, which
    probe_video correctly refuses (it requires a video stream). Same
    discipline — ffprobe facts or None, nothing fabricated: the file
    must exist, carry an audio stream, and report a usable duration.
    This is the single probing authority for narration durations;
    narration._audio_duration delegates here so the old WAV-header
    reader no longer stands as a second authority.
    """
    path = Path(path)
    if not path.exists() or not path.is_file():
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        return None

    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        return None

    duration = None
    fmt = data.get("format") or {}
    for raw in (fmt.get("duration"), audio.get("duration")):
        try:
            if raw is not None:
                duration = float(raw)
                break
        except (TypeError, ValueError):
            continue
    if duration is None or duration <= 0:
        return None

    try:
        sample_rate = int(audio.get("sample_rate") or 0) or None
    except (TypeError, ValueError):
        sample_rate = None
    try:
        channels = int(audio.get("channels") or 0) or None
    except (TypeError, ValueError):
        channels = None

    return {
        "duration_seconds": round(duration, 3),
        "codec": audio.get("codec_name"),
        "sample_rate": sample_rate,
        "channels": channels,
    }


_FFMPEG_TIMEOUT_SECONDS = 60

#: Where in a video the sampled frames are taken (fractions of the
#: probed duration). The opening and closing extremes are avoided on
#: purpose: fades and end cards live there, and the frame stage wants
#: the frames a viewer actually watches.
SAMPLE_FRAME_FRACTIONS = (0.12, 0.5, 0.88)


def extract_sample_frames(path, output_dir, duration_seconds=None,
                          fractions=SAMPLE_FRAME_FRACTIONS, width=640):
    """Extract sampled frames of a video file as PNGs (Phase 1.1).

    The frame-level verification stage (services.frame_verification)
    judges what a candidate actually LOOKS like; this is its only
    frame source, kept here so every "what is in this file" answer
    still comes from the probe path. Frames are decoded at a bounded
    width (analysis never needs more) and written into ``output_dir``
    as ``sample_<i>.png``; the caller owns cleanup.

    Returns the list of frame paths actually written (possibly
    shorter than ``fractions`` when a seek lands past the end), or
    an empty list when the file is missing/unreadable or ffmpeg
    fails — an empty list means "no frames could be sampled", never
    a fabricated frame.
    """
    path = Path(path)
    output_dir = Path(output_dir)
    if not path.exists() or not path.is_file():
        return []
    if duration_seconds is None:
        probe = probe_video(path)
        if probe is None:
            return []
        duration_seconds = probe["duration_seconds"]
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError):
        return []
    if duration <= 0:
        return []
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return []
    written = []
    for index, fraction in enumerate(fractions):
        at = min(max(duration * float(fraction), 0.0), max(duration - 0.05, 0.0))
        target = output_dir / f"sample_{index}.png"
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-ss", f"{at:.3f}",
                    "-i", str(path),
                    "-frames:v", "1",
                    "-vf", f"scale={int(width)}:-2",
                    str(target),
                ],
                capture_output=True,
                text=True,
                timeout=_FFMPEG_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and target.exists() and target.stat().st_size > 0:
            written.append(target)
    return written


def probe_image(path) -> dict | None:
    """Return real dimensions/format for an image file, or None."""
    path = Path(path)
    if not path.exists() or not path.is_file():
        return None
    try:
        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img:
            width, height = img.size
            fmt = img.format
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    if not width or not height:
        return None
    return {"width": width, "height": height, "format": fmt}
