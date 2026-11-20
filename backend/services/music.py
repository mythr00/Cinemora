import json
import subprocess
from pathlib import Path


# ==================================================
# SETTINGS
# ==================================================

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


# NOTE (Stage H, M5/D9): the parallel add_background_music
# implementation that used to live here was removed — it had no
# production callers (only its own __main__ CLI) and duplicated
# the prepare_soundtrack mix. There is exactly ONE mix
# implementation now: prepare_soundtrack below.


# ==================================================
# LOUDNESS MEASUREMENT (Stage 3H)
# ==================================================

def measure_loudness_lufs(audio_file):
    """Integrated loudness of an audio file in LUFS, measured with
    ffmpeg's ebur128 filter (parsed from its stderr summary).
    Returns a float, or None when measurement fails for any reason
    — an unmeasured loudness is reported as unknown, never faked."""
    import re

    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-i", str(audio_file),
                "-af", "ebur128",
                "-f", "null", "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
        )
    except Exception as error:
        print(f"Loudness measurement failed: {error}")
        return None

    match = None
    for match in re.finditer(
        r"Integrated loudness:\s*\n?\s*I:\s*(-?[\d.]+)\s*LUFS",
        result.stderr or "",
    ):
        pass  # keep the LAST (summary) match
    if match is None:
        simple = re.findall(
            r"I:\s*(-?[\d.]+)\s*LUFS", result.stderr or ""
        )
        if not simple:
            return None
        try:
            return float(simple[-1])
        except ValueError:
            return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


# ==================================================
# STAGE H — MUSIC CONTRACT, IDENTITY, MEASUREMENT
# ==================================================
#
# One contract everywhere (M3): a production's music is described
# by the options keys "music_file" (path or absent), "music_enabled"
# (bool; explicit False disables music entirely) and "music_volume"
# (0.0-2.0). The legacy "music" bool is honoured as an alias for
# "music_enabled" so no caller is silently ignored. Everywhere a
# soundtrack is recorded, its STATE is one of:
#
#   applied                a track was mixed under the narration
#   not_requested          nobody asked for music; narration only
#   disabled               music was explicitly turned off
#   requested_but_missing  a track was requested but the file was
#                          not there; the film degrades to narration
#                          only and the record SAYS SO (D8)
#   failed                 a track was supplied but could not be
#                          mixed (corrupt/unreadable); the run
#                          fails loudly (the deliberate D10
#                          asymmetry: missing degrades, corrupt fails)
#
# Identity (M6) records only what is actually known about a track:
# file name/path, content sha256, probed duration/codec/rate/
# channels, and how the file arrived (source_type). The licence is
# "unknown" unless a record elsewhere says otherwise — never
# invented, never upgraded.

import hashlib
import os

MUSIC_STATES = (
    "applied",
    "not_requested",
    "disabled",
    "requested_but_missing",
    "failed",
)

# Source classifications for an applied track.
SOURCE_OPERATOR_PLACED = "operator_placed"      # options.music_file
SOURCE_LEGACY_PROJECT_FILE = "legacy_project_file"  # background.mp3
SOURCE_USER_SUPPLIED = "user_supplied"          # editor upload
SOURCE_PROJECT_FILE = "project_file"            # editor project file

# M10 detection threshold, applied to the narration-GAP difference
# statistic (see assess_audio_provenance). Calibrated empirically
# on this system's own chain (Piper narration -> soundtrack -> AAC
# mux): a narration-only export's gap difference median measured
# -89.6 dBFS; the same narration with a sine bed at volume 0.2
# measured -52.5 dBFS. -65 dBFS sits between them with >12 dB of
# margin on both sides. (A single global difference RMS does NOT
# separate the two — codec noise on speech transients reaches
# -35 dBFS per window and the ducked bed dilutes to ~-48 dBFS
# globally — which is why the gap statistic exists.)
MUSIC_GAP_THRESHOLD_DBFS = -65.0


def file_sha256(path):
    """sha256 hex of a file's bytes, or None when unreadable."""
    try:
        hasher = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        return None


def probe_audio_file(path):
    """ffprobe facts for an audio file: duration, codec, sample
    rate, channels — or None when the file cannot be probed as
    audio. Nothing is fabricated: unprobeable stays None."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0 or not result.stdout:
            return None
        data = json.loads(result.stdout)
    except Exception:
        return None
    stream = next(
        (s for s in data.get("streams") or []
         if s.get("codec_type") == "audio"),
        None,
    )
    if stream is None:
        return None
    duration = None
    for raw in ((data.get("format") or {}).get("duration"),
                stream.get("duration")):
        try:
            if raw is not None:
                duration = float(raw)
                break
        except (TypeError, ValueError):
            continue
    try:
        sample_rate = int(stream.get("sample_rate") or 0) or None
    except (TypeError, ValueError):
        sample_rate = None
    try:
        channels = int(stream.get("channels") or 0) or None
    except (TypeError, ValueError):
        channels = None
    return {
        "duration_seconds": round(duration, 3) if duration else None,
        "codec": stream.get("codec_name"),
        "sample_rate": sample_rate,
        "channels": channels,
    }


def describe_music_file(path, source_type):
    """The truthful identity of one music file (M6). Licence is
    "unknown": no code path in this system holds licence evidence
    for an operator/editor-supplied track, and unknown is never
    upgraded."""
    path = Path(path)
    probe = probe_audio_file(path) or {}
    return {
        "file_name": path.name,
        "file_path": str(path),
        "content_sha256": file_sha256(path),
        "duration_seconds": probe.get("duration_seconds"),
        "codec": probe.get("codec"),
        "sample_rate": probe.get("sample_rate"),
        "channels": probe.get("channels"),
        "source_type": source_type,
        "license": "unknown",
        "license_source": None,
    }


def _validated_volume(options, key):
    raw = options.get(key)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"Option {key} must be a number, got {raw!r}.")
    if not (0.0 <= value <= 2.0):
        raise ValueError(f"Option {key} must be between 0.0 and 2.0, got {value}.")
    return value


def resolve_music_request(options, project_dir):
    """Resolve the ONE music contract (M1/M3) into an explicit
    decision. Returns a dict:

      file         path to mix, or None
      requested    True when a caller explicitly asked for a track
      disabled     True when music was explicitly turned off
      decided_by   which input decided: "music_enabled_false",
                   "music_file", "legacy_background_mp3" or "none"
      source_type  classification for the applied track
      music_volume validated options volume or None (default)
      narration_volume validated options volume or None

    Rules (M1): an explicit disable beats EVERYTHING, including an
    explicit music_file and a stray background.mp3; an explicit
    music_file beats the legacy file; the legacy project
    background.mp3 is picked up only when nothing was decided
    explicitly."""
    options = options or {}
    project_dir = Path(project_dir)
    decision = {
        "file": None,
        "requested": False,
        "disabled": False,
        "decided_by": "none",
        "source_type": None,
        "music_volume": _validated_volume(options, "music_volume"),
        "narration_volume": _validated_volume(options, "narration_volume"),
    }
    enabled = options.get("music_enabled")
    if enabled is None and "music" in options:
        enabled = options.get("music")  # legacy bool alias (D9)
    if enabled is not None and not bool(enabled):
        decision["disabled"] = True
        decision["decided_by"] = "music_enabled_false"
        return decision
    music_option = options.get("music_file")
    if music_option:
        decision["file"] = str(music_option)
        decision["requested"] = True
        decision["decided_by"] = "music_file"
        decision["source_type"] = SOURCE_OPERATOR_PLACED
        return decision
    legacy = project_dir / "background.mp3"
    if legacy.is_file():
        decision["file"] = str(legacy)
        decision["decided_by"] = "legacy_background_mp3"
        decision["source_type"] = SOURCE_LEGACY_PROJECT_FILE
    return decision


def failed_music_record(*, requested_file=None, source_type=None,
                        decided_by=None, requested=False, error=None):
    """The record for a soundtrack whose music FAILED to mix
    (state "failed"). The file's hash is still recorded when it can
    be read — a corrupt file is evidence too — while probe facts
    stay None rather than invented."""
    identity = None
    if requested_file:
        path = Path(requested_file)
        identity = {
            "file_name": path.name,
            "file_path": str(path),
            "content_sha256": file_sha256(path),
            "duration_seconds": None,
            "codec": None,
            "sample_rate": None,
            "channels": None,
            "source_type": source_type,
            "license": "unknown",
            "license_source": None,
        }
    return {
        "music": "absent",
        "source": None,
        "loudness_lufs": None,
        "state": "failed",
        "requested": bool(requested),
        "requested_file": str(requested_file) if requested_file else None,
        "decided_by": decided_by,
        "source_type": source_type,
        "music_volume": None,
        "identity": identity,
        "error": str(error) if error is not None else None,
        "status": "soundtrack_failed",
        "output_file": None,
    }


# --------------------------------------------------
# Final-file measurement (M9)
# --------------------------------------------------

def _env_float(name, default):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def target_lufs():
    """Configurable loudness target (measurement reference only in
    this stage — normalisation is Stage J/P4)."""
    return _env_float("CINEMORA_TARGET_LUFS", -14.0)


def max_true_peak_dbtp():
    return _env_float("CINEMORA_MAX_TRUE_PEAK_DBTP", -1.0)


def measure_audio_levels(media_file):
    """Integrated loudness (LUFS) + true peak (dBTP) of a media
    file's audio, via ffmpeg loudnorm's analysis printout. Returns
    {"integrated_lufs": float|None, "true_peak_dbtp": float|None}
    — None values are an honest "could not measure", never faked.
    Falls back to the ebur128 scanner for loudness when loudnorm
    output cannot be parsed."""
    import re

    levels = {"integrated_lufs": None, "true_peak_dbtp": None}
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-i", str(media_file),
                "-af", "loudnorm=print_format=json",
                "-f", "null", "-",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=600,
        )
        match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", result.stderr or "")
        if match:
            data = json.loads(match.group(0))
            for key, field in (("input_i", "integrated_lufs"),
                               ("input_tp", "true_peak_dbtp")):
                try:
                    levels[field] = float(data[key])
                except (KeyError, TypeError, ValueError):
                    pass
    except Exception as error:
        print(f"Audio level measurement failed: {error}")
    if levels["integrated_lufs"] is None:
        levels["integrated_lufs"] = measure_loudness_lufs(media_file)
    return levels


def final_audio_record(media_file):
    """The M9 record for one exported file: measured levels plus
    the configured targets they were measured against (targets are
    reported, not enforced, in this stage)."""
    levels = measure_audio_levels(media_file)
    return {
        "file_name": Path(media_file).name,
        "integrated_lufs": levels["integrated_lufs"],
        "true_peak_dbtp": levels["true_peak_dbtp"],
        "target_lufs": target_lufs(),
        "max_true_peak_dbtp": max_true_peak_dbtp(),
    }


# --------------------------------------------------
# Provenance cross-check (M10)
# --------------------------------------------------

def audio_difference_rms_dbfs(candidate_file, reference_file):
    """RMS of (candidate - reference) in dBFS, both decoded to mono
    48 kHz s16le and compared over their common length. This is the
    empirical test the music audit used on the delivered films: a
    narration-only export differs from its narration by codec noise
    alone (~-55 dBFS); any real music bed raises the difference far
    above that. Returns None when either file cannot be decoded."""
    import array
    import math
    import tempfile

    def _decode(source, target):
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(source),
                "-ac", "1", "-ar", "48000", "-f", "s16le", str(target),
            ],
            capture_output=True, timeout=600,
        )
        return result.returncode == 0 and Path(target).stat().st_size > 0

    try:
        with tempfile.TemporaryDirectory() as tmp:
            cand_raw = Path(tmp) / "candidate.raw"
            ref_raw = Path(tmp) / "reference.raw"
            if not _decode(candidate_file, cand_raw):
                return None
            if not _decode(reference_file, ref_raw):
                return None
            total_sq = 0.0
            count = 0
            with open(cand_raw, "rb") as cand, open(ref_raw, "rb") as ref:
                while True:
                    cand_chunk = cand.read(1 << 20)
                    ref_chunk = ref.read(len(cand_chunk)) if cand_chunk else b""
                    if not cand_chunk or not ref_chunk:
                        break
                    n = min(len(cand_chunk), len(ref_chunk)) // 2
                    if n == 0:
                        break
                    a = array.array("h")
                    a.frombytes(cand_chunk[: n * 2])
                    b = array.array("h")
                    b.frombytes(ref_chunk[: n * 2])
                    for x, y in zip(a, b):
                        diff = x - y
                        total_sq += diff * diff
                    count += n
            if count == 0:
                return None
            rms = math.sqrt(total_sq / count)
            if rms <= 0:
                return -120.0
            return round(20 * math.log10(rms / 32768.0), 2)
    except Exception as error:
        print(f"Audio difference measurement failed: {error}")
        return None


def audio_difference_analysis(candidate_file, reference_file):
    """Windowed difference analysis of candidate vs reference.

    Both files are decoded to mono 48 kHz s16le and compared in
    100 ms windows. Returns a dict:

      difference_rms_dbfs       global difference RMS (the music
                                audit's original statistic, kept
                                for reporting continuity)
      gap_difference_rms_dbfs   median difference RMS over the
                                narration's QUIET windows — where
                                the reference is near-silent, any
                                energy in the difference is the
                                music bed (unducked there) plus
                                negligible codec noise on silence
      detection_basis           "narration_gaps" when >=3 windows
                                had reference RMS < -42 dBFS, else
                                "quietest_windows" (the quietest
                                10% of windows), else None
      quiet_window_count        windows the statistic rests on

    Values are None when either file cannot be decoded."""
    import array
    import math
    import tempfile

    empty = {
        "difference_rms_dbfs": None,
        "gap_difference_rms_dbfs": None,
        "detection_basis": None,
        "quiet_window_count": 0,
    }

    def _decode(source, target):
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(source),
                "-ac", "1", "-ar", "48000", "-f", "s16le", str(target),
            ],
            capture_output=True, timeout=600,
        )
        return result.returncode == 0 and Path(target).stat().st_size > 0

    def _db(rms):
        return 20 * math.log10(rms / 32768.0) if rms > 0 else -120.0

    try:
        with tempfile.TemporaryDirectory() as tmp:
            cand_raw = Path(tmp) / "candidate.raw"
            ref_raw = Path(tmp) / "reference.raw"
            if not _decode(candidate_file, cand_raw):
                return empty
            if not _decode(reference_file, ref_raw):
                return empty
            cand = array.array("h")
            cand.frombytes(cand_raw.read_bytes())
            ref = array.array("h")
            ref.frombytes(ref_raw.read_bytes())
            n = min(len(cand), len(ref))
            cand, ref = cand[:n], ref[:n]
            window = 4800  # 100 ms at 48 kHz
            total_sq = 0.0
            total_count = 0
            windows = []  # (reference_db, difference_db)
            for start in range(0, n - window + 1, window):
                diff_sq = 0.0
                ref_sq = 0.0
                for x, y in zip(
                    cand[start:start + window], ref[start:start + window]
                ):
                    diff = x - y
                    diff_sq += diff * diff
                    ref_sq += y * y
                total_sq += diff_sq
                total_count += window
                windows.append(
                    (
                        _db(math.sqrt(ref_sq / window)),
                        _db(math.sqrt(diff_sq / window)),
                    )
                )
            if total_count == 0 or not windows:
                return empty
            global_db = _db(math.sqrt(total_sq / total_count))
            quiet = [d for r, d in windows if r < -42.0]
            if len(quiet) >= 3:
                statistic_windows = quiet
                basis = "narration_gaps"
            else:
                by_quiet = sorted(windows, key=lambda pair: pair[0])
                take = max(3, math.ceil(len(by_quiet) * 0.10))
                statistic_windows = [d for _r, d in by_quiet[:take]]
                basis = "quietest_windows"
            ordered = sorted(statistic_windows)
            statistic = ordered[len(ordered) // 2]
            return {
                "difference_rms_dbfs": round(global_db, 2),
                "gap_difference_rms_dbfs": round(statistic, 2),
                "detection_basis": basis,
                "quiet_window_count": len(statistic_windows),
            }
    except Exception as error:
        print(f"Audio difference analysis failed: {error}")
        return empty


def assess_audio_provenance(claimed_state, candidate_file, reference_file):
    """M10: do the RECORDS and the AUDIO agree?

    claimed_state is the soundtrack record's state; candidate_file
    is the exported film; reference_file is the exact narration the
    soundtrack was mixed from. "Music detected" means a music bed
    is measurable in the narration's quiet windows, where a bed is
    unducked and codec noise is negligible (see
    audio_difference_analysis and MUSIC_GAP_THRESHOLD_DBFS for the
    calibration). The global difference RMS is reported alongside
    but never decides: on its own it cannot separate a quiet
    ducked bed from codec noise.

    consistent is True/False only when a determination could be
    made; None means "could not verify" (missing files, decode
    failure). States other than "applied" all claim NO music in
    the film and are checked the same way.

    Limits, stated plainly: this detects record inconsistency, not
    musical quality. A track mixed so quietly that its bed sits
    below -65 dBFS in the narration gaps reads as "no music" —
    which is itself a record problem worth flagging, since a bed
    nobody can hear is not the applied soundtrack the records
    describe."""
    claims_music = claimed_state == "applied"
    analysis = {"difference_rms_dbfs": None,
                "gap_difference_rms_dbfs": None,
                "detection_basis": None, "quiet_window_count": 0}
    if candidate_file and reference_file:
        analysis = audio_difference_analysis(candidate_file, reference_file)
    gap = analysis["gap_difference_rms_dbfs"]
    detected = None
    if gap is not None:
        detected = gap > MUSIC_GAP_THRESHOLD_DBFS
    consistent = None
    if detected is not None:
        consistent = bool(detected) == bool(claims_music)
    return {
        "claimed_state": claimed_state,
        "music_detected": detected,
        "difference_rms_dbfs": analysis["difference_rms_dbfs"],
        "gap_difference_rms_dbfs": gap,
        "detection_basis": analysis["detection_basis"],
        "quiet_window_count": analysis["quiet_window_count"],
        "threshold_dbfs": MUSIC_GAP_THRESHOLD_DBFS,
        "consistent": consistent,
        "method": "final_audio_vs_narration_gap_difference",
    }


# ==================================================
# PREPARE STANDALONE SOUNDTRACK
# ==================================================

def prepare_soundtrack(
    narration_file,
    output_file,
    music_file=None,
    music_volume=MUSIC_VOLUME,
    *,
    source_type=None,
    decided_by=None,
    requested=False,
    disabled=False,
):
    """
    Build the documentary soundtrack independently of video.

    This is the Sound Designer stage — the ONE mix implementation
    (M5): looped music at `music_volume`, ducked under the
    narration by a genuine sidechain compressor, summed with the
    narration.

    Output:
        narration + optional background music
        as a standalone WAV soundtrack.

    The Video Editor later muxes this soundtrack into
    the rendered visual timeline.

    The returned record (Stage H) states explicitly which of the
    MUSIC_STATES happened, who decided (decided_by), how an applied
    track arrived (source_type) and the track's truthful identity
    (M6). A requested file that is missing degrades to narration
    only with state "requested_but_missing" (D8); a file that
    exists but cannot be mixed raises — the deliberate D10
    asymmetry, recorded by the caller as state "failed".
    """

    narration_file = Path(narration_file)
    output_file = Path(output_file)

    if not narration_file.exists():
        raise FileNotFoundError(
            f"Narration not found: {narration_file}"
        )

    requested_file = str(music_file) if music_file else None
    missing_requested = False
    if music_file:
        music_file = Path(music_file)
        if not music_file.exists():
            print(
                f"WARNING: music file not found: {music_file}. "
                "Continuing with narration only."
            )
            missing_requested = True
            music_file = None

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    print("\n========================================")
    print("SOUND DESIGNER")
    print("========================================")
    print(f"Narration: {narration_file}")
    print(f"Music: {music_file or 'none'}")
    print(f"Output: {output_file}")

    if music_file:
        filter_complex = (
            f"[0:a]"
            "aresample=48000,"
            "asetpts=N/SR/TB"
            "[voice];"

            f"[1:a]"
            f"volume={music_volume},"
            "aresample=48000,"
            "asetpts=N/SR/TB"
            "[music];"

            "[music][voice]"
            "sidechaincompress="
            "threshold=0.03:"
            f"ratio={DUCK_RATIO}:"
            f"attack={DUCK_ATTACK}:"
            f"release={DUCK_RELEASE}:"
            "makeup=1"
            "[ducked_music];"

            "[voice][ducked_music]"
            "amix="
            "inputs=2:"
            "duration=first:"
            "dropout_transition=2:"
            "normalize=0"
            "[aout]"
        )

        command = [
            "ffmpeg",
            "-y",

            "-i",
            str(narration_file),

            "-stream_loop",
            "-1",

            "-i",
            str(music_file),

            "-filter_complex",
            filter_complex,

            "-map",
            "[aout]",

            "-c:a",
            "pcm_s16le",

            str(output_file),
        ]

    else:
        command = [
            "ffmpeg",
            "-y",

            "-i",
            str(narration_file),

            "-map",
            "0:a:0",

            "-c:a",
            "pcm_s16le",

            str(output_file),
        ]

    run_ffmpeg(command)

    if not output_file.exists():
        raise RuntimeError(
            f"Soundtrack was not created: {output_file}"
        )

    print("\n========================================")
    print("SOUNDTRACK COMPLETE")
    print("========================================")
    print(f"Output: {output_file}")

    # Stage 3H (AUD-28/MA-17) built the honest record; Stage H
    # extends it with the explicit state (M4), the decision source
    # (M1) and the track's truthful identity (M6). The legacy
    # music/source/status/output_file keys are kept for existing
    # consumers.
    loudness = measure_loudness_lufs(output_file)
    if music_file:
        state = "applied"
        identity = describe_music_file(music_file, source_type)
    elif disabled:
        state = "disabled"
        identity = None
    elif missing_requested:
        state = "requested_but_missing"
        identity = None
    else:
        state = "not_requested"
        identity = None
    record = {
        "music": "mixed" if music_file else "absent",
        "source": Path(music_file).name if music_file else None,
        "loudness_lufs": loudness,
        "state": state,
        "requested": bool(requested),
        "requested_file": requested_file,
        "decided_by": decided_by,
        "source_type": source_type,
        "music_volume": music_volume if music_file else None,
        "identity": identity,
        "error": (
            f"requested music file not found: {requested_file}"
            if missing_requested else None
        ),
    }
    record["status"] = "soundtrack_created"
    record["output_file"] = str(output_file)
    print(f"Soundtrack record: {record}")
    return record


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

    # One mix implementation (M5): extract the video's audio as the
    # narration, build the soundtrack with prepare_soundtrack, then
    # mux it back with the production audio muxer.
    import tempfile

    from services.audio_mixer import add_narration

    video_file = Path(args.video)
    output_file = (
        Path(args.output)
        if args.output
        else video_file.parent / "documentary_final.mp4"
    )
    with tempfile.TemporaryDirectory() as tmp:
        narration_wav = Path(tmp) / "narration.wav"
        run_ffmpeg(
            [
                "ffmpeg", "-y", "-i", str(video_file),
                "-vn", "-c:a", "pcm_s16le", str(narration_wav),
            ]
        )
        record = prepare_soundtrack(
            narration_wav,
            Path(tmp) / "soundtrack.wav",
            music_file=args.music,
            music_volume=args.volume,
            source_type=SOURCE_OPERATOR_PLACED,
            decided_by="cli",
            requested=True,
        )
        add_narration(
            str(video_file),
            str(Path(tmp) / "soundtrack.wav"),
            str(output_file),
        )

    print(
        json.dumps(
            record,
            indent=2,
        )
    )
