"""Delivery encode profile (Master Directive Phase 8, AM-023).

The founder's live test produced a 191,566,743-byte export for a
45.43 s film (~34 Mbps): the pipeline's only encode is the
archival-grade master (CRF 23 / ultrafast, no bitrate ceiling),
which is the right master and the wrong file to hand a browser.
This module adds the DELIVERY copy beside the master: a
web-playable H.264/AAC encode with faststart, produced from the
finished master without touching it.

AM-023 — the founder's delivery bitrate TARGET — remains OPEN
and is the founder's decision. The numbers below are an
ENGINEERING DEFAULT, chosen to be unremarkable for 1080p web
delivery (CRF 26 with a 10 Mbps ceiling: visually transparent
for documentary material at a fraction of archival size), and
are recorded on every delivery record as `profile` so a later
founder decision can supersede them in exactly one place. They
are not presented anywhere as the founder's choice.

Contract:
* `produce_delivery_copy(master, out_path)` never modifies the
  master, never raises (returns a record dict), and validates
  its output by full decode + duration agreement before calling
  it delivered — a failed or suspect encode is recorded as
  failed and the (partial) output is not presented as a
  delivery copy.
* The record is plain data: {profile, status, master_bytes,
  delivery_bytes, size_ratio, master_bitrate_kbps,
  delivery_bitrate_kbps, duration_seconds, decode_validated}.
"""

from pathlib import Path

#: The profile machinery's default. AM-023 (the founder's target)
#: is OPEN — see the module docstring.
DELIVERY_PROFILE = {
    "name": "delivery_v1_default",
    "video_codec": "libx264",
    "preset": "medium",
    "crf": "26",
    "maxrate_kbps": 10000,
    "bufsize_kbps": 20000,
    "audio_codec": "aac",
    "audio_bitrate_kbps": 160,
    "pixel_format": "yuv420p",
    "movflags": "+faststart",
    "am023_status": "OPEN — engineering default, not a founder decision",
}


def delivery_encode_args(profile=None):
    """The ffmpeg argument fragment for the delivery encode."""
    profile = profile or DELIVERY_PROFILE
    return [
        "-c:v", profile["video_codec"],
        "-preset", profile["preset"],
        "-crf", profile["crf"],
        "-maxrate", f"{profile['maxrate_kbps']}k",
        "-bufsize", f"{profile['bufsize_kbps']}k",
        "-pix_fmt", profile["pixel_format"],
        "-c:a", profile["audio_codec"],
        "-b:a", f"{profile['audio_bitrate_kbps']}k",
        "-movflags", profile["movflags"],
    ]


def _probe_record(path):
    """(bytes, duration_seconds, overall_bitrate_kbps) via the
    renderer's probe, or (None, None, None) when unprobeable."""
    from services import renderer as _renderer

    path = Path(path)
    if not path.exists():
        return None, None, None
    info = _renderer.probe_media(path)
    size = path.stat().st_size
    if not info:
        return size, None, None
    duration = info.get("duration")
    bitrate = None
    if duration:
        bitrate = round(size * 8 / float(duration) / 1000, 1)
    return size, duration, bitrate


def _full_decode_valid(path):
    """Decode the whole file to null; True only when ffmpeg
    decodes every frame without error (RENDER-002 style
    validation, applied to the delivery copy)."""
    from services import renderer as _renderer

    command = [
        "ffmpeg", "-v", "error", "-xerror",
        "-i", str(path), "-f", "null", "-",
    ]
    return _renderer.run_ffmpeg(command, timeout=900) is True


def produce_delivery_copy(master_path, out_path, profile=None):
    """Encode `out_path` as the delivery copy of `master_path`.

    Returns the delivery record (see the module docstring). On
    any failure the record's status is "failed" with the reason,
    and no file is presented as delivered (a partial output is
    removed rather than left to be mistaken for the copy)."""
    from services import renderer as _renderer

    profile = profile or DELIVERY_PROFILE
    master_path = Path(master_path)
    out_path = Path(out_path)
    record = {
        "profile": profile["name"],
        "am023_status": profile["am023_status"],
        "status": "failed",
        "master": str(master_path),
        "delivery": str(out_path),
    }
    try:
        master_bytes, master_duration, master_bitrate = _probe_record(
            master_path
        )
        if master_bytes is None or not master_duration:
            record["reason"] = "master_unprobeable"
            return record
        record.update({
            "master_bytes": master_bytes,
            "master_bitrate_kbps": master_bitrate,
            "duration_seconds": master_duration,
        })
        out_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "ffmpeg", "-y", "-i", str(master_path),
            *delivery_encode_args(profile),
            str(out_path),
        ]
        result = _renderer.run_ffmpeg(command, timeout=1800)
        if result is not True or not out_path.exists():
            record["reason"] = "delivery_encode_failed"
            return record
        delivery_bytes, delivery_duration, delivery_bitrate = (
            _probe_record(out_path)
        )
        record.update({
            "delivery_bytes": delivery_bytes,
            "delivery_bitrate_kbps": delivery_bitrate,
        })
        if delivery_duration is None or abs(
            delivery_duration - master_duration
        ) > 0.5:
            record["reason"] = "delivery_duration_mismatch"
        elif not _full_decode_valid(out_path):
            record["reason"] = "delivery_decode_failed"
        else:
            record["status"] = "delivered"
            record["decode_validated"] = True
            if master_bytes:
                record["size_ratio"] = round(
                    delivery_bytes / master_bytes, 4
                )
            return record
        # A suspect copy is not a delivery copy.
        try:
            out_path.unlink()
        except OSError:
            pass
        return record
    except Exception as error:  # fail-soft: the master stands alone
        record["reason"] = "delivery_exception"
        record["detail"] = str(error)
        return record
