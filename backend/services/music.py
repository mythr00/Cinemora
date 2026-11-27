import json
import shutil
import subprocess
from pathlib import Path


# ==================================================
# SETTINGS (built-in defaults)
# ==================================================
#
# These constants are the DEFAULTS of the Stage J mix
# configuration (resolve_mix_config below): a genre pack's
# mix_preset beats them, an env override beats the preset, and an
# explicit run/editor option beats everything. They are kept as
# module constants because existing callers import them.

# Background music base volume.
# 0.10 = 10%
MUSIC_VOLUME = 0.10

# How strongly the music is reduced when narration plays.
DUCK_RATIO = 8

# How quickly music ducks down.
DUCK_ATTACK = 20

# How quickly music comes back up.
DUCK_RELEASE = 1200

# Sidechain threshold (was a literal in the mix graph pre-Stage J).
DUCK_THRESHOLD = 0.03

# Stage J (P4) mix defaults: bed fades and the crossfade used when
# a track shorter than the film is looped.
FADE_IN_SECONDS = 1.5
FADE_OUT_SECONDS = 2.5
LOOP_CROSSFADE_SECONDS = 1.0


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
#
# Stage J recalibration (P4): the statistic is now GAIN-MATCHED
# (audio_difference_analysis estimates the candidate's gain over
# the narration's loud windows and differences against the scaled
# reference), because Stage J normalises the soundtrack — a pure
# gain change otherwise reads as content (measured failure: a
# narration-only editor export's RAW gap difference was -51.8
# dBFS, above this threshold). Re-measured on the new chain,
# final MP4s: narration-only -86.6 dBFS; sine bed at volume 0.20
# -50.2; at the default 0.10 -56.2; at 0.05 -62.3 (still detected,
# 2.7 dB of margin at half the default volume — a bed that quiet
# is an edge probe, not a production setting). The threshold is
# UNCHANGED: the noise side keeps >21 dB of margin and every
# production volume (presets 0.08-0.10, default 0.10) keeps >8 dB.
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


def _validated_music_sections(options):
    """Phase 9 (§13.2/§13.6): music placement — "throughout"
    (the historical behaviour: the bed runs the whole film) or
    "sections" with explicit time ranges in seconds. Returns
    (placement, sections) with sections a cleaned list of
    {"start": float, "end": float}, sorted and non-overlapping.
    Raises ValueError on any malformed input — a placement the
    mix cannot honour must fail loudly, never silently degrade
    to throughout."""
    options = options or {}
    placement = options.get("music_placement")
    if placement is None:
        return "throughout", []
    placement = str(placement).strip().lower()
    if placement == "throughout":
        return "throughout", []
    if placement != "sections":
        raise ValueError(
            f"Option music_placement must be 'throughout' or "
            f"'sections', got {options.get('music_placement')!r}."
        )
    raw = options.get("music_sections")
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            "Option music_sections must be a non-empty list of "
            "{start, end} ranges when music_placement is 'sections'."
        )
    if len(raw) > 50:
        raise ValueError("Option music_sections may carry at most 50 ranges.")
    sections = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"Music section must be an object, got {entry!r}.")
        try:
            start = float(entry.get("start"))
            end = float(entry.get("end"))
        except (TypeError, ValueError):
            raise ValueError(
                f"Music section start/end must be numbers, got {entry!r}."
            ) from None
        if start < 0 or end <= start or end > 86400:
            raise ValueError(
                f"Music section must satisfy 0 <= start < end <= 86400, "
                f"got {entry!r}."
            )
        sections.append({"start": start, "end": end})
    sections.sort(key=lambda s: s["start"])
    for previous, current in zip(sections, sections[1:]):
        if current["start"] < previous["end"]:
            raise ValueError(
                f"Music sections must not overlap, got {previous!r} "
                f"and {current!r}."
            )
    return "sections", sections


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
      placement    Phase 9: "throughout" or "sections"
      sections     Phase 9: validated [{start, end}] ranges
                   (empty for "throughout")

    Rules (M1): an explicit disable beats EVERYTHING, including an
    explicit music_file and a stray background.mp3; an explicit
    music_file beats the legacy file; the legacy project
    background.mp3 is picked up only when nothing was decided
    explicitly."""
    options = options or {}
    project_dir = Path(project_dir)
    placement, sections = _validated_music_sections(options)
    decision = {
        "file": None,
        "requested": False,
        "disabled": False,
        "decided_by": "none",
        "source_type": None,
        "music_volume": _validated_volume(options, "music_volume"),
        "narration_volume": _validated_volume(options, "narration_volume"),
        "placement": placement,
        "sections": sections,
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
        "mix": None,
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
    """Configurable loudness target: the Stage H measurement
    reference AND (Stage J, P4) the loudness prepare_soundtrack
    normalises the mixed soundtrack to."""
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


def final_audio_record(media_file, soundtrack_record=None):
    """The M9 record for one exported file: measured levels plus
    the configured targets they were measured against. Stage J
    (P4): when the soundtrack record is supplied, the record also
    carries whether the soundtrack was NORMALISED to the target
    (and the normalisation detail) — the export record states the
    target, the measured result and the normalised flag together,
    so a reader never has to guess whether a level was aimed or
    accidental."""
    levels = measure_audio_levels(media_file)
    record = {
        "file_name": Path(media_file).name,
        "integrated_lufs": levels["integrated_lufs"],
        "true_peak_dbtp": levels["true_peak_dbtp"],
        "target_lufs": target_lufs(),
        "max_true_peak_dbtp": max_true_peak_dbtp(),
    }
    if isinstance(soundtrack_record, dict):
        mix = soundtrack_record.get("mix") or {}
        normalisation = mix.get("normalisation") or {}
        record["normalised"] = bool(normalisation.get("applied"))
        record["normalisation"] = normalisation or None
    return record


# --------------------------------------------------
# Soundtrack validation (founder mandate 2026-11-27:
# CINEMATIC MUSIC LIBRARY, "Audio validation")
# --------------------------------------------------
# The pre-completion checks the mandate names: the music bed
# covers the full documentary duration, no unintended silent
# sections, no abrupt loop boundaries, no clipping, narration
# intelligible, final audio duration == final video duration
# (the soundtrack is built to the narration's duration, which
# the timeline renders 1:1). Every check is MEASURED from the
# actual files; a measurement that cannot be taken is recorded
# as None and makes the validation incomplete — never passed
# silently.

def detect_silence_spans(media_file, *, noise_db=-45.0, min_duration=1.0):
    """Silent spans of a media file via ffmpeg silencedetect:
    a list of {"start", "end", "duration"} dicts (seconds).
    Returns None when the measurement itself fails."""
    import re

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-i", str(media_file),
                "-af",
                f"silencedetect=noise={noise_db}dB:d={min_duration}",
                "-f", "null", "-",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=600,
        )
    except Exception as error:
        print(f"Silence detection failed: {error}")
        return None
    spans = []
    starts = [
        float(m) for m in re.findall(
            r"silence_start: ([0-9.]+)", result.stderr or "")
    ]
    ends = [
        (float(e), float(d)) for e, d in re.findall(
            r"silence_end: ([0-9.]+) \| silence_duration: ([0-9.]+)",
            result.stderr or "")
    ]
    for start, (end, duration) in zip(starts, ends):
        spans.append({"start": start, "end": end, "duration": duration})
    if len(starts) > len(ends):
        probe = probe_audio_file(media_file) or {}
        total = probe.get("duration_seconds")
        if total:
            start = starts[-1]
            spans.append({
                "start": start, "end": float(total),
                "duration": float(total) - start,
            })
    return spans


def measure_max_sample_step(media_file):
    """The largest absolute sample-to-sample step in the audio
    (mono mixdown, native rate), as a 0..1 fraction of full
    scale. A hard-cut loop seam or a click shows up here as a
    step far beyond the material's own steps. Returns None when
    the measurement fails."""
    import array

    try:
        process = subprocess.Popen(
            [
                "ffmpeg", "-v", "error", "-i", str(media_file),
                "-ac", "1", "-f", "s16le", "-",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
    except Exception as error:
        print(f"Sample-step measurement failed: {error}")
        return None
    max_step = 0
    previous = None
    remainder = b""
    assert process.stdout is not None
    while True:
        chunk = process.stdout.read(1 << 20)
        if not chunk:
            break
        data = remainder + chunk
        usable = len(data) - (len(data) % 2)
        remainder = data[usable:]
        samples = array.array("h")
        samples.frombytes(data[:usable])
        for value in samples:
            if previous is not None:
                step = abs(value - previous)
                if step > max_step:
                    max_step = step
            previous = value
    process.wait()
    if process.returncode != 0:
        return None
    return max_step / 32768.0


def assess_bed_continuity(bed_file, track_file):
    """Loop-boundary abruptness: the shaped bed's largest sample
    step against the source track's own largest step. A properly
    crossfaded loop introduces no step beyond the material's own
    (a small tolerance covers resampling); a hard restart does."""
    bed_step = measure_max_sample_step(bed_file)
    track_step = measure_max_sample_step(track_file)
    if bed_step is None or track_step is None:
        return {"measured": False, "passed": None}
    limit = track_step * 1.5 + 0.02
    return {
        "measured": True,
        "bed_max_step": round(bed_step, 4),
        "track_max_step": round(track_step, 4),
        "limit": round(limit, 4),
        "passed": bed_step <= limit,
    }


def validate_soundtrack(soundtrack_file, narration_file, *,
                        music_applied):
    """The mandate's pre-completion audio validation, measured
    on the prepared soundtrack against the narration it was
    mixed from. Music-specific checks are enforced only when
    music was applied; the rest always run. `passed` requires
    every enforced check; `complete` says every measurement
    could actually be taken."""
    checks = {"music_applied": bool(music_applied)}
    try:
        soundtrack_probe = probe_audio_file(soundtrack_file) or {}
        narration_probe = probe_audio_file(narration_file) or {}
        soundtrack_duration = soundtrack_probe.get("duration_seconds")
        narration_duration = narration_probe.get("duration_seconds")
        checks["soundtrack_duration_seconds"] = soundtrack_duration
        checks["narration_duration_seconds"] = narration_duration
        if soundtrack_duration and narration_duration:
            delta = abs(soundtrack_duration - narration_duration)
            checks["duration_delta_seconds"] = round(delta, 3)
            checks["duration_match"] = delta <= 0.25
        else:
            checks["duration_delta_seconds"] = None
            checks["duration_match"] = None

        levels = measure_audio_levels(soundtrack_file)
        ceiling = max_true_peak_dbtp()
        checks["true_peak_dbtp"] = levels["true_peak_dbtp"]
        checks["max_true_peak_dbtp"] = ceiling
        checks["no_clipping"] = (
            levels["true_peak_dbtp"] is not None
            and levels["true_peak_dbtp"] <= ceiling + 0.1
        )
        checks["integrated_lufs"] = levels["integrated_lufs"]
        checks["target_lufs"] = target_lufs()
        loudness_in_band = (
            levels["integrated_lufs"] is not None
            and target_lufs() - 6.0
            <= levels["integrated_lufs"]
            <= target_lufs() + 3.0
        )

        spans = detect_silence_spans(soundtrack_file)
        if spans is None or not soundtrack_duration:
            checks["longest_internal_silence_seconds"] = None
            checks["no_unintended_silence"] = None
        else:
            internal = [
                span["duration"] for span in spans
                if span["start"] >= 3.0
                and span["end"] <= soundtrack_duration - 3.0
            ]
            longest = max(internal) if internal else 0.0
            checks["longest_internal_silence_seconds"] = round(longest, 3)
            checks["no_unintended_silence"] = (
                longest <= 2.0 if music_applied else None
            )

        if music_applied:
            provenance = assess_audio_provenance(
                "applied", str(soundtrack_file), str(narration_file)
            )
            checks["music_detected_in_narration_gaps"] = provenance[
                "music_detected"
            ]
            checks["provenance_consistent"] = provenance["consistent"]
        else:
            checks["music_detected_in_narration_gaps"] = None
            checks["provenance_consistent"] = None

        # Narration intelligibility is a PROXY check (labelled as
        # such): the mix sits in the loudness band the narration
        # was normalised to, and — when music is applied — the
        # provenance cross-check confirms the narration's gaps
        # carry the bed while its speech stays the dominant
        # signal (the difference analysis behind M10).
        intelligible = loudness_in_band and (
            checks["provenance_consistent"] is not False
        )
        checks["narration_intelligibility_proxy"] = intelligible

        enforced = [
            checks["duration_match"],
            checks["no_clipping"],
            checks["narration_intelligibility_proxy"],
        ]
        if music_applied:
            enforced += [
                checks["no_unintended_silence"],
                checks["music_detected_in_narration_gaps"],
                checks["provenance_consistent"],
            ]
        checks["complete"] = all(value is not None for value in enforced)
        checks["passed"] = bool(
            checks["complete"] and all(enforced)
        )
    except Exception as error:  # validation must never kill a mix
        checks["error"] = f"{type(error).__name__}: {error}"
        checks["complete"] = False
        checks["passed"] = False
    return checks


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
                                for reporting continuity — RAW: a
                                soundtrack-wide gain change moves
                                it, which is why it never decides)
      gap_difference_rms_dbfs   median difference RMS over the
                                narration's QUIET windows, computed
                                against the GAIN-MATCHED reference
                                (see below) — where the reference
                                is near-silent, any energy in the
                                difference is the music bed
                                (unducked there) plus negligible
                                codec noise on silence
      reference_gain_db         the candidate's overall gain
                                relative to the reference, estimated
                                over the narration's LOUD windows
      detection_basis           "narration_gaps" when >=3 windows
                                had reference RMS < -42 dBFS, else
                                "quietest_windows" (the quietest
                                10% of windows), else None
      quiet_window_count        windows the statistic rests on

    Gain matching (Stage J recalibration): Stage J normalises the
    soundtrack, so the exported narration sits at a different
    LEVEL than the reference narration it was mixed from. A raw
    difference then measures the gain, not the content — on the
    new chain a narration-only export's raw gap difference
    measured -51.8 dBFS (its quiet windows are low-level TTS
    noise, not digital silence, and (gain-1) x that noise is
    large), which the Stage H detector would have called music.
    The gap statistic therefore differences the candidate against
    the reference scaled by the gain estimated over loud windows
    (where the narration dominates both files; a ducked bed
    biases the estimate by well under a decibel). A music bed is
    additive energy the scaled reference cannot explain, so it
    still registers; a pure gain change now differences to codec
    noise, as it should.

    Values are None when either file cannot be decoded."""
    import array
    import math
    import tempfile

    empty = {
        "difference_rms_dbfs": None,
        "gap_difference_rms_dbfs": None,
        "reference_gain_db": None,
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
            # Pass 1: per-window levels + the raw global difference.
            total_sq = 0.0
            total_count = 0
            levels = []  # (reference_rms, candidate_rms) per window
            for start in range(0, n - window + 1, window):
                diff_sq = 0.0
                ref_sq = 0.0
                cand_sq = 0.0
                for x, y in zip(
                    cand[start:start + window], ref[start:start + window]
                ):
                    diff = x - y
                    diff_sq += diff * diff
                    ref_sq += y * y
                    cand_sq += x * x
                total_sq += diff_sq
                total_count += window
                levels.append(
                    (math.sqrt(ref_sq / window), math.sqrt(cand_sq / window))
                )
            if total_count == 0 or not levels:
                return empty
            global_db = _db(math.sqrt(total_sq / total_count))
            # Gain estimate: median candidate/reference RMS ratio
            # over the narration's loud windows.
            ratios = sorted(
                c / r for r, c in levels if _db(r) > -30.0 and r > 0
            )
            gain = ratios[len(ratios) // 2] if len(ratios) >= 3 else 1.0
            # Pass 2: the gain-matched difference per window.
            windows = []  # (reference_db, matched_difference_db)
            for index, (ref_rms, _cand_rms) in enumerate(levels):
                start = index * window
                diff_sq = 0.0
                for x, y in zip(
                    cand[start:start + window], ref[start:start + window]
                ):
                    diff = x - gain * y
                    diff_sq += diff * diff
                windows.append(
                    (_db(ref_rms), _db(math.sqrt(diff_sq / window)))
                )
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
                "reference_gain_db": round(20 * math.log10(gain), 2)
                if gain > 0 else None,
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
                "reference_gain_db": None,
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
        "reference_gain_db": analysis.get("reference_gain_db"),
        "detection_basis": analysis["detection_basis"],
        "quiet_window_count": analysis["quiet_window_count"],
        "threshold_dbfs": MUSIC_GAP_THRESHOLD_DBFS,
        "consistent": consistent,
        "method": "final_audio_vs_narration_gap_difference_gain_matched",
    }


# ==================================================
# STAGE J (P4) — MIX CONFIGURATION, BED, NORMALISATION
# ==================================================
#
# Professional mixing on top of the Stage H contract. Every mix
# parameter is resolved ONCE by resolve_mix_config with a single
# precedence, highest first:
#
#   explicit option (run options / editor call)
#   > env override (CINEMORA_MUSIC_* / CINEMORA_DUCK_*)
#   > genre-pack mix_preset (DATA in services/genre_packs)
#   > built-in default (the module constants above)
#
# The resolved config is recorded verbatim in the soundtrack
# record's "mix" section, so provenance describes the mix that
# actually happened — parameters are never buried literals again.

_MIX_OPTION_KEYS = {
    "music_volume": "music_volume",
    "fade_in_seconds": "music_fade_in_seconds",
    "fade_out_seconds": "music_fade_out_seconds",
    "loop_crossfade_seconds": "music_loop_crossfade_seconds",
    "duck_threshold": "duck_threshold",
    "duck_ratio": "duck_ratio",
    "duck_attack_ms": "duck_attack_ms",
    "duck_release_ms": "duck_release_ms",
}

_MIX_ENV_KEYS = {
    "fade_in_seconds": "CINEMORA_MUSIC_FADE_IN_SECONDS",
    "fade_out_seconds": "CINEMORA_MUSIC_FADE_OUT_SECONDS",
    "loop_crossfade_seconds": "CINEMORA_MUSIC_LOOP_CROSSFADE_SECONDS",
    "duck_threshold": "CINEMORA_DUCK_THRESHOLD",
    "duck_ratio": "CINEMORA_DUCK_RATIO",
    "duck_attack_ms": "CINEMORA_DUCK_ATTACK_MS",
    "duck_release_ms": "CINEMORA_DUCK_RELEASE_MS",
}

_MIX_PRESET_KEYS = (
    "music_volume", "fade_in_seconds", "fade_out_seconds", "duck_ratio",
)

_MIX_DEFAULTS = {
    "music_volume": MUSIC_VOLUME,
    "fade_in_seconds": FADE_IN_SECONDS,
    "fade_out_seconds": FADE_OUT_SECONDS,
    "loop_crossfade_seconds": LOOP_CROSSFADE_SECONDS,
    "duck_threshold": DUCK_THRESHOLD,
    "duck_ratio": DUCK_RATIO,
    "duck_attack_ms": DUCK_ATTACK,
    "duck_release_ms": DUCK_RELEASE,
}


def _mix_float(value, key):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Mix parameter {key} must be a number, got {value!r}.")
    if number < 0:
        raise ValueError(f"Mix parameter {key} must be >= 0, got {number}.")
    return number


def resolve_mix_config(options=None, genre_pack=None, music_volume=None):
    """Resolve the full Stage J mix configuration.

    options: the run/editor options dict (explicit values win).
    genre_pack: a genre pack dict, a pack id, or None.
    music_volume: an already-validated explicit bed volume (the
    jobs path passes resolve_music_request's result here); when
    None, options["music_volume"] is consulted, then the preset,
    then the default.

    Returns the config dict prepare_soundtrack consumes; the same
    dict (minus nothing) is what lands in the record's mix
    section, with per-parameter sources for music_volume and a
    preset descriptor."""
    options = options or {}
    pack = None
    pack_id = None
    if isinstance(genre_pack, dict):
        pack = genre_pack
        pack_id = pack.get("id")
    elif genre_pack:
        pack_id = str(genre_pack)
        try:
            from services import genre_packs as _packs

            pack = _packs.load_pack(pack_id)
        except Exception:
            pack = None
    preset = (pack or {}).get("mix_preset") or {}
    if not isinstance(preset, dict):
        preset = {}

    config = {}
    for key, default in _MIX_DEFAULTS.items():
        value = None
        if key == "music_volume" and music_volume is not None:
            value = _mix_float(music_volume, key)
            config["music_volume_source"] = "option"
        elif key in _MIX_OPTION_KEYS and options.get(_MIX_OPTION_KEYS[key]) is not None:
            value = _mix_float(options[_MIX_OPTION_KEYS[key]], key)
            if key == "music_volume":
                config["music_volume_source"] = "option"
        if value is None and key in _MIX_ENV_KEYS:
            env_value = _env_float(_MIX_ENV_KEYS[key], None)
            if env_value is not None:
                value = _mix_float(env_value, key)
                if key == "music_volume":
                    config["music_volume_source"] = "env"
        if value is None and key in _MIX_PRESET_KEYS and preset.get(key) is not None:
            value = _mix_float(preset[key], key)
            if key == "music_volume":
                config["music_volume_source"] = "genre_preset"
        if value is None:
            value = default
            if key == "music_volume":
                config["music_volume_source"] = "default"
        config[key] = value
    if config["music_volume"] > 2.0:
        raise ValueError(
            f"Mix parameter music_volume must be between 0.0 and 2.0, "
            f"got {config['music_volume']}."
        )
    # Phase 9: placement rides the mix config so the ONE mix
    # implementation (and its record) sees the same sections the
    # request resolved. Invalid sections raise here, at resolve
    # time, exactly as they do in resolve_music_request.
    placement, sections = _validated_music_sections(options)
    config["music_placement"] = placement
    config["music_sections"] = sections
    normalise_env = os.environ.get("CINEMORA_MUSIC_NORMALISE")
    if options.get("music_normalise") is not None:
        normalise = bool(options["music_normalise"])
    elif normalise_env is not None and normalise_env.strip() != "":
        normalise = normalise_env.strip().lower() not in ("0", "false", "no")
    else:
        normalise = True
    config["normalise"] = normalise
    # Narration denoise (Lane 4 prototype): option wins, then env,
    # default OFF — see _DENOISE_FILTER's note.
    denoise_env = os.environ.get("CINEMORA_DENOISE_NARRATION")
    if options.get("denoise_narration") is not None:
        denoise = bool(options["denoise_narration"])
    elif denoise_env is not None and denoise_env.strip() != "":
        denoise = denoise_env.strip().lower() not in ("0", "false", "no")
    else:
        denoise = False
    config["denoise_narration"] = denoise
    config["target_lufs"] = target_lufs()
    config["max_true_peak_dbtp"] = max_true_peak_dbtp()
    config["preset"] = {
        "genre_pack": pack_id,
        "applied": bool(preset),
        "name": pack_id if preset else "default",
    }
    return config


def mix_fingerprint(mix):
    """The audio-shaping subset of a mix config/record section, as
    a comparable tuple. The editor's soundtrack-reuse guard uses
    it: a production soundtrack may be copied into a revision only
    when the mix that SHAPED it matches the mix the revision would
    build — a different fade/duck/normalisation shape is a
    different soundtrack, however identical the file selection."""
    if not isinstance(mix, dict):
        return None
    ducking = mix.get("ducking") or {}

    def _num(value):
        try:
            return round(float(value), 4)
        except (TypeError, ValueError):
            return None

    return (
        _num(mix.get("music_volume")),
        _num(mix.get("fade_in_seconds")),
        _num(mix.get("fade_out_seconds")),
        _num(mix.get("loop_crossfade_seconds")),
        _num(ducking.get("threshold", mix.get("duck_threshold"))),
        _num(ducking.get("ratio", mix.get("duck_ratio"))),
        _num(ducking.get("attack_ms", mix.get("duck_attack_ms"))),
        _num(ducking.get("release_ms", mix.get("duck_release_ms"))),
        _num(mix.get("target_lufs")),
        _num(mix.get("max_true_peak_dbtp")),
        bool((mix.get("normalisation") or {}).get("applied"))
        if "normalisation" in mix else bool(mix.get("normalise", True)),
    )


def _loudnorm_analysis(audio_file):
    """Full loudnorm analysis of a file: input_i / input_tp /
    input_lra / input_thresh / target_offset, or None when the
    analysis cannot be parsed (silence and decode failures land
    here — an honest None, never invented numbers)."""
    import re

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-i", str(audio_file),
                "-af", "loudnorm=print_format=json",
                "-f", "null", "-",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=600,
        )
        match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", result.stderr or "")
        if not match:
            return None
        data = json.loads(match.group(0))
        analysis = {}
        for key in ("input_i", "input_tp", "input_lra",
                    "input_thresh", "target_offset"):
            try:
                analysis[key] = float(data[key])
            except (KeyError, TypeError, ValueError):
                return None
        return analysis
    except Exception as error:
        print(f"Loudnorm analysis failed: {error}")
        return None


#: Narration cleanup (GitHub Skills Programme, Lane 4, 2026-11-25).
#: Lane 4 found the pipeline's one genuine audio capability gap:
#: no denoising step exists anywhere, while TTS and recorded
#: narration arrive with hiss/room noise that loudnorm then
#: amplifies to the -14 LUFS target. ffmpeg's afftdn is already
#: in the installed build — no new dependency.
#:
#: MEASURED design (synthetic A/B, tests/test_narration_denoise.py):
#: afftdn's decisive parameter is nf (the noise floor it should
#: expect). At the default nf=-50 it did essentially nothing to a
#: -30 dBFS noise floor (gap RMS -30.45 -> -30.60 dB); with nf set
#: just above the MEASURED floor it removed ~21 dB of floor while
#: moving the voice segment's level by 0.12 dB. So the pass is
#: two-step: measure the narration's floor (10th-percentile
#: windowed RMS of the decoded PCM), then run afftdn with
#: nf = floor + 2 dB and a moderate nr=12. PROTOTYPE posture: off
#: by default, enabled per-run (option denoise_narration) or by
#: env CINEMORA_DENOISE_NARRATION=1, always recorded in the mix
#: section; promotion to a default awaits the same A/B on real
#: narration when film runs resume.
_DENOISE_NR_DB = 12


def estimate_noise_floor_db(audio_file):
    """10th-percentile windowed RMS of the decoded mono PCM, in
    dBFS — the level the quietest parts of the file sit at. None
    when the file cannot be decoded/measured."""
    import subprocess as _subprocess

    import numpy as _np

    try:
        proc = _subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(audio_file),
             "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
            capture_output=True, timeout=300,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        samples = _np.frombuffer(proc.stdout, dtype=_np.float32)
        window = 4800  # 100 ms windows
        usable = (len(samples) // window) * window
        if usable == 0:
            return None
        windows = samples[:usable].reshape(-1, window)
        rms = _np.sqrt(_np.mean(windows.astype(_np.float64) ** 2, axis=1))
        floor = float(_np.percentile(rms, 10))
        if floor <= 0:
            return None
        return round(20.0 * float(_np.log10(floor)), 2)
    except Exception as error:
        print(f"Noise floor measurement failed: {error}")
        return None


def narration_denoise_filter(floor_db=None) -> str:
    """The afftdn filter string for a measured floor (nf just
    above it). Without a measurement, afftdn's defaults — the
    honest no-op-ish fallback the A/B measured."""
    if floor_db is None:
        return f"afftdn=nr={_DENOISE_NR_DB}"
    nf = max(-60.0, min(-20.0, float(floor_db) + 2.0))
    return f"afftdn=nr={_DENOISE_NR_DB}:nf={nf:.1f}"


def denoise_audio_file(input_file, output_file):
    """Measure the floor, then run the narration denoise pass
    over a standalone file. Returns the applied record."""
    floor_db = estimate_noise_floor_db(input_file)
    filter_string = narration_denoise_filter(floor_db)
    run_ffmpeg(
        [
            "ffmpeg", "-y", "-i", str(input_file),
            "-af", filter_string,
            "-c:a", "pcm_s16le", str(output_file),
        ]
    )
    return {"filter": filter_string, "measured_floor_db": floor_db}


#: Encode headroom for the loudnorm true-peak target (Gate 4 fix,
#: 2026-11-26). loudnorm normalises the soundtrack WAV, but the
#: LAST audio stage in the chain is the AAC encode in
#: audio_mixer.add_narration (jobs.py / editor mux), and a lossy
#: encode raises inter-sample peaks AFTER loudnorm's measurement.
#: Measured on the Hyatt test production (run cdfcc130): the
#: normalised soundtrack.wav sat at exactly -1.00 dBTP — the
#: ceiling, with zero headroom — and the delivered MP4 measured
#: -0.91 dBTP (+0.09 dB of encode overshoot), failing the C-06
#: radio.audio_levels gate (-1.0 dBTP ceiling) while loudness
#: passed. So loudnorm's TP target is the delivery ceiling MINUS
#: this headroom; the ceiling itself (config / standard test) is
#: unchanged. 1.0 dB is ~10x the observed overshoot and matches
#: common lossy-delivery practice (target -2.0 dBTP for a -1.0
#: ceiling); loudness is unaffected in kind — loudnorm's TP
#: constraint was already the binding one on peaky narration.
ENCODE_TRUE_PEAK_HEADROOM_DB = 1.0


def _normalise_audio(input_file, output_file, config):
    """Normalise input_file to the configured loudness target and
    write output_file. Returns the normalisation record.

    Method: loudnorm TWO-PASS — a measured analysis pass, then the
    linear apply with the measured values (the architecture allows
    two passes: the soundtrack is a standalone file before muxing,
    so nothing forces the single-pass dynamic mode). loudnorm's TP
    target carries ENCODE_TRUE_PEAK_HEADROOM_DB of headroom below
    the delivery ceiling, because the AAC encode at mux time raises
    true peaks after this stage measures them (see the constant).
    The result is then MEASURED; if its true peak still exceeds the
    headroom-adjusted target, an alimiter stage is the backstop
    (never clip silently). Any failure degrades to copying the
    unnormalised mix and RECORDING that — a failed normalisation is
    a provenance fact, not a silent skip and not a reason to kill a
    production whose mix is otherwise sound."""
    loudnorm_tp = (
        config["max_true_peak_dbtp"] - ENCODE_TRUE_PEAK_HEADROOM_DB
    )
    record = {
        "applied": False,
        "method": None,
        "target_lufs": config["target_lufs"],
        "max_true_peak_dbtp": config["max_true_peak_dbtp"],
        "loudnorm_true_peak_dbtp": loudnorm_tp,
        "encode_headroom_db": ENCODE_TRUE_PEAK_HEADROOM_DB,
        "pre_integrated_lufs": None,
        "pre_true_peak_dbtp": None,
        "post_integrated_lufs": None,
        "post_true_peak_dbtp": None,
        "limiter_applied": False,
        "reason": None,
    }
    if not config.get("normalise", True):
        record["reason"] = "disabled_by_configuration"
        shutil.copy2(input_file, output_file)
        return record
    analysis = _loudnorm_analysis(input_file)
    if analysis is None:
        record["reason"] = "measurement_failed"
        shutil.copy2(input_file, output_file)
        return record
    record["pre_integrated_lufs"] = analysis["input_i"]
    record["pre_true_peak_dbtp"] = analysis["input_tp"]
    loudnorm = (
        f"loudnorm=I={config['target_lufs']}:TP={loudnorm_tp}"
        f":LRA=11"
        f":measured_I={analysis['input_i']}"
        f":measured_TP={analysis['input_tp']}"
        f":measured_LRA={analysis['input_lra']}"
        f":measured_thresh={analysis['input_thresh']}"
        f":offset={analysis['target_offset']}"
        f":linear=true"
    )
    try:
        run_ffmpeg(
            [
                "ffmpeg", "-y", "-i", str(input_file),
                "-af", f"{loudnorm},aresample=48000",
                "-c:a", "pcm_s16le", str(output_file),
            ]
        )
    except Exception as error:
        print(f"Loudnorm apply failed, keeping unnormalised mix: {error}")
        record["reason"] = "loudnorm_failed"
        shutil.copy2(input_file, output_file)
        return record
    record["applied"] = True
    record["method"] = "loudnorm_two_pass_linear"
    levels = measure_audio_levels(output_file)
    record["post_integrated_lufs"] = levels["integrated_lufs"]
    record["post_true_peak_dbtp"] = levels["true_peak_dbtp"]
    ceiling = loudnorm_tp
    if (
        levels["true_peak_dbtp"] is not None
        and levels["true_peak_dbtp"] > ceiling + 0.05
    ):
        # Limiter backstop: loudnorm's linear mode can leave peaks
        # above the target on peaky material. alimiter with
        # level=false applies no makeup gain — it only holds the
        # (headroom-adjusted) target, so a backstopped soundtrack
        # keeps the same encode headroom as a clean loudnorm pass —
        # and the result is measured again and recorded.
        limit = min(0.999, 10 ** (ceiling / 20.0))
        limited = Path(output_file).with_name(
            Path(output_file).stem + ".limited.wav"
        )
        try:
            run_ffmpeg(
                [
                    "ffmpeg", "-y", "-i", str(output_file),
                    "-af", f"alimiter=limit={limit:.6f}:level=false",
                    "-c:a", "pcm_s16le", str(limited),
                ]
            )
            shutil.move(str(limited), str(output_file))
            record["limiter_applied"] = True
            levels = measure_audio_levels(output_file)
            record["post_integrated_lufs"] = levels["integrated_lufs"]
            record["post_true_peak_dbtp"] = levels["true_peak_dbtp"]
        except Exception as error:
            print(f"Limiter backstop failed: {error}")
            limited.unlink(missing_ok=True)
    return record


def _build_music_bed(music_file, target_duration, config, work_dir):
    """Build the music bed for one soundtrack: the track shaped to
    the narration's length with the configured fades applied.

    - track >= film: trimmed, never looped (mode "none");
    - track < film, crossfade > 0: looped by self-joining with
      acrossfade (each join doubles the unit, so a short track
      reaches film length in a handful of ffmpeg calls) — the seam
      is a true crossfade, not a hard cut (mode "crossfade");
    - track < film, crossfade == 0: the legacy hard loop
      (mode "hard").

    Returns (bed_path, loop_info). Raises when the track cannot be
    probed — the caller's corrupt-file path (state "failed")."""
    probe = probe_audio_file(music_file)
    if not probe or not probe.get("duration_seconds"):
        raise RuntimeError(
            f"Music file could not be probed as audio: {music_file}"
        )
    track_duration = float(probe["duration_seconds"])
    target = float(target_duration)
    crossfade = float(config["loop_crossfade_seconds"])
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    source = Path(music_file)
    mode = "none"
    effective_crossfade = 0.0
    if track_duration < target:
        # acrossfade needs the crossfade shorter than the unit it
        # joins; cap at 40% of the track so any real track loops.
        effective_crossfade = min(crossfade, track_duration * 0.4)
        if effective_crossfade >= 0.05:
            mode = "crossfade"
            unit = source
            unit_length = track_duration
            joins = 0
            while unit_length < target and joins < 24:
                joined = work_dir / f"bed_join_{joins}.wav"
                run_ffmpeg(
                    [
                        "ffmpeg", "-y",
                        "-i", str(unit), "-i", str(unit),
                        "-filter_complex",
                        "[0:a]aresample=48000[ja];"
                        "[1:a]aresample=48000[jb];"
                        f"[ja][jb]acrossfade=d={effective_crossfade:.3f}"
                        ":c1=tri:c2=tri[jout]",
                        "-map", "[jout]",
                        "-c:a", "pcm_s16le", str(joined),
                    ]
                )
                unit = joined
                unit_length = unit_length * 2 - effective_crossfade
                joins += 1
            source = unit
        else:
            mode = "hard"
            effective_crossfade = 0.0

    fade_in = min(float(config["fade_in_seconds"]), target / 2.0)
    fade_out = min(float(config["fade_out_seconds"]), target - fade_in)
    filters = ["aresample=48000", f"atrim=0:{target:.3f}", "asetpts=N/SR/TB"]
    if fade_in > 0.01:
        filters.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0.01:
        filters.append(
            f"afade=t=out:st={max(0.0, target - fade_out):.3f}:d={fade_out:.3f}"
        )
    bed = work_dir / "music_bed.wav"
    command = ["ffmpeg", "-y"]
    if mode == "hard":
        command += ["-stream_loop", "-1"]
    command += [
        "-i", str(source),
        "-af", ",".join(filters),
        "-c:a", "pcm_s16le", str(bed),
    ]
    run_ffmpeg(command)
    loop_info = {
        "mode": mode,
        "crossfade_seconds": round(effective_crossfade, 3) if mode == "crossfade" else 0.0,
        "track_duration_seconds": round(track_duration, 3),
        "bed_duration_seconds": round(target, 3),
        "fade_in_seconds": round(fade_in, 3),
        "fade_out_seconds": round(fade_out, 3),
    }
    return bed, loop_info


# ==================================================
# PREPARE STANDALONE SOUNDTRACK
# ==================================================

def _mix_to_file(narration_file, mix_file, music_file, mix_config,
                 ducking, bed_work_dir):
    """The mix itself (Stage J): shape the bed, duck it under the
    narration with the genuine sidechain graph, sum. Writes
    mix_file (pre-normalisation) and returns the bed's loop_info
    (None for a narration-only mix). Raises on any ffmpeg failure
    — the caller's corrupt-file path."""
    resolved_volume = mix_config["music_volume"]
    if not music_file:
        if mix_config.get("denoise_narration"):
            applied = denoise_audio_file(narration_file, mix_file)
            mix_config["denoise_measured_floor_db"] = applied[
                "measured_floor_db"
            ]
        else:
            run_ffmpeg(
                [
                    "ffmpeg", "-y",
                    "-i", str(narration_file),
                    "-map", "0:a:0",
                    "-c:a", "pcm_s16le",
                    str(mix_file),
                ]
            )
        return None

    narration_probe = probe_audio_file(narration_file) or {}
    narration_duration = narration_probe.get("duration_seconds")
    bed_file = None
    loop_info = None
    if narration_duration:
        bed_file, loop_info = _build_music_bed(
            music_file,
            float(narration_duration),
            mix_config,
            bed_work_dir,
        )
    # Phase 9 (§13.2): section placement gates the bed to the
    # selected ranges — outside them the bed is silenced BEFORE
    # ducking, so the sidechain and the sum only ever see music
    # where the configuration placed it. The gate is a per-frame
    # volume expression over the bed's own timestamps (asetpts
    # above aligns t=0 with the film's start).
    sections = mix_config.get("music_sections") or []
    gate_filter = ""
    music_label = "[music]"
    if sections:
        expression = "+".join(
            f"between(t\\,{s['start']:g}\\,{s['end']:g})" for s in sections
        )
        gate_filter = (
            "[music]"
            f"volume='if({expression}\\,1\\,0)':eval=frame"
            "[music_gated];"
        )
        music_label = "[music_gated]"
    voice_cleanup = ""
    if mix_config.get("denoise_narration"):
        floor_db = estimate_noise_floor_db(narration_file)
        mix_config["denoise_measured_floor_db"] = floor_db
        voice_cleanup = f"{narration_denoise_filter(floor_db)},"
    
    filter_complex = (
        "[0:a]"
        "aresample=48000,"
        f"{voice_cleanup}"
        "asetpts=N/SR/TB"
        "[voice];"

        "[1:a]"
        f"volume={resolved_volume},"
        "aresample=48000,"
        "asetpts=N/SR/TB"
        "[music];"

        + gate_filter +

        f"{music_label}[voice]"
        "sidechaincompress="
        f"threshold={ducking['threshold']}:"
        f"ratio={ducking['ratio']}:"
        f"attack={ducking['attack_ms']}:"
        f"release={ducking['release_ms']}:"
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
    command = ["ffmpeg", "-y", "-i", str(narration_file)]
    if bed_file is None:
        # Narration duration unknown (never for pipeline-made
        # narration): the legacy hard loop, recorded as such.
        command += ["-stream_loop", "-1"]
        loop_info = {
            "mode": "hard",
            "crossfade_seconds": 0.0,
            "track_duration_seconds": None,
            "bed_duration_seconds": None,
            "fade_in_seconds": 0.0,
            "fade_out_seconds": 0.0,
            "reason": "narration_duration_unknown",
        }
    command += [
        "-i", str(bed_file if bed_file is not None else music_file),
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        "-c:a", "pcm_s16le",
        str(mix_file),
    ]
    run_ffmpeg(command)
    return loop_info


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
    mix_config=None,
    genre_pack=None,
):
    """
    Build the documentary soundtrack independently of video.

    This is the Sound Designer stage — the ONE mix implementation
    (M5): music shaped into a bed (fades; crossfaded loop when the
    track is shorter than the film — Stage J, P4), ducked under
    the narration by a genuine sidechain compressor, summed with
    the narration, and the mix normalised to the configured
    loudness target with a limiter backstop.

    Output:
        narration + optional background music
        as a standalone WAV soundtrack.

    The Video Editor later muxes this soundtrack into
    the rendered visual timeline.

    Mix parameters come from `mix_config` (see
    resolve_mix_config) when supplied; otherwise a config is
    resolved here from the `music_volume` argument and
    `genre_pack`. When both are supplied, mix_config wins.

    The returned record (Stage H) states explicitly which of the
    MUSIC_STATES happened, who decided (decided_by), how an applied
    track arrived (source_type) and the track's truthful identity
    (M6); its "mix" section (Stage J) records the resolved mix
    configuration, the loop/fade shaping and the normalisation
    outcome. A requested file that is missing degrades to
    narration only with state "requested_but_missing" (D8); a file
    that exists but cannot be mixed raises — the deliberate D10
    asymmetry, recorded by the caller as state "failed".
    """

    narration_file = Path(narration_file)
    output_file = Path(output_file)

    if not narration_file.exists():
        raise FileNotFoundError(
            f"Narration not found: {narration_file}"
        )

    if mix_config is None:
        mix_config = resolve_mix_config(
            music_volume=music_volume, genre_pack=genre_pack
        )
    resolved_volume = mix_config["music_volume"]

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
    print(f"Mix config: {mix_config}")

    # The mix is built into a pre-normalisation file first; the
    # delivered soundtrack is the normalised result (or, when
    # normalisation is off/unavailable, a copy of the mix — the
    # record says which). Intermediates are always cleaned up,
    # including on the corrupt-file failure path.
    mix_file = output_file.with_name(output_file.stem + ".mix.wav")
    bed_work_dir = output_file.parent / ".music_bed_work"
    ducking = {
        "threshold": mix_config["duck_threshold"],
        "ratio": mix_config["duck_ratio"],
        "attack_ms": mix_config["duck_attack_ms"],
        "release_ms": mix_config["duck_release_ms"],
    }
    bed_continuity = None
    try:
        loop_info = _mix_to_file(
            narration_file, mix_file, music_file, mix_config,
            ducking, bed_work_dir,
        )
        if not mix_file.exists():
            raise RuntimeError(
                f"Soundtrack mix was not created: {mix_file}"
            )
        # Loop-boundary continuity is measured on the shaped bed
        # itself, while it still exists (the finally below cleans
        # the bed work dir up).
        bed_file = bed_work_dir / "music_bed.wav"
        if (
            music_file
            and bed_file.is_file()
            and loop_info
            and loop_info.get("mode") == "crossfade"
        ):
            bed_continuity = assess_bed_continuity(bed_file, music_file)
        normalisation = _normalise_audio(mix_file, output_file, mix_config)
    finally:
        mix_file.unlink(missing_ok=True)
        shutil.rmtree(bed_work_dir, ignore_errors=True)

    if not output_file.exists():
        raise RuntimeError(
            f"Soundtrack was not created: {output_file}"
        )

    # The mandate's pre-completion audio validation, measured on
    # the delivered soundtrack (see validate_soundtrack). It is
    # recorded on the soundtrack record; the final auditor's
    # provenance verdict remains the pipeline's hard gate.
    validation = validate_soundtrack(
        output_file, narration_file, music_applied=bool(music_file)
    )
    if bed_continuity is not None:
        validation["bed_continuity"] = bed_continuity
        if bed_continuity.get("passed") is not True:
            validation["complete"] = False
            validation["passed"] = False

    print("\n========================================")
    print("SOUNDTRACK COMPLETE")
    print("========================================")
    print(f"Output: {output_file}")

    # Stage 3H (AUD-28/MA-17) built the honest record; Stage H
    # extends it with the explicit state (M4), the decision source
    # (M1) and the track's truthful identity (M6); Stage J adds the
    # "mix" section (P4): the resolved configuration, the bed
    # shaping and the normalisation outcome. The legacy
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
    mix_section = {
        "preset": mix_config["preset"],
        "music_volume": resolved_volume if music_file else None,
        "music_volume_source": mix_config["music_volume_source"],
        "fade_in_seconds": (
            loop_info.get("fade_in_seconds") if loop_info else None
        ),
        "fade_out_seconds": (
            loop_info.get("fade_out_seconds") if loop_info else None
        ),
        "loop_crossfade_seconds": mix_config["loop_crossfade_seconds"],
        # Phase 9: the placement the mix actually applied —
        # "throughout" or the exact section ranges the bed was
        # gated to (empty list for throughout).
        "placement": mix_config.get("music_placement") or "throughout",
        "sections": mix_config.get("music_sections") or [],
        "loop": loop_info,
        "ducking": ducking if music_file else None,
        "gain_staging": {
            # The narration reaches the mix exactly as supplied —
            # jobs/the editor apply any narration_volume BEFORE
            # this stage. The bed's pre-duck level is music_volume;
            # its level in narration gaps is the sidechain's
            # release back to that same level — derived from the
            # ducking, never a second hidden gain.
            "narration": "as_supplied",
            "bed_pre_duck_volume": resolved_volume if music_file else None,
            "bed_in_gaps": "ducking_release_to_pre_duck_level",
        },
        "target_lufs": mix_config["target_lufs"],
        "max_true_peak_dbtp": mix_config["max_true_peak_dbtp"],
        "normalisation": normalisation,
        # Lane 4 prototype: whether the narration chain carried
        # the afftdn cleanup pass, the filter family and the
        # measured floor its nf was set from — provenance facts
        # on every soundtrack record.
        "denoise": {
            "applied": bool(mix_config.get("denoise_narration")),
            "filter": narration_denoise_filter(
                mix_config.get("denoise_measured_floor_db")
            ),
            "measured_floor_db": mix_config.get(
                "denoise_measured_floor_db"
            ),
            "stage": "narration_pre_mix",
        },
    }
    record = {
        "music": "mixed" if music_file else "absent",
        "source": Path(music_file).name if music_file else None,
        "loudness_lufs": loudness,
        "state": state,
        "requested": bool(requested),
        "requested_file": requested_file,
        "decided_by": decided_by,
        "source_type": source_type,
        "music_volume": resolved_volume if music_file else None,
        "identity": identity,
        "mix": mix_section,
        "validation": validation,
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
