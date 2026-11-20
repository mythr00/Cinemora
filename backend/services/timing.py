"""The one narration timing authority (Stage 5 / R4, AUDIO-001).

Measured part durations are the single source of truth for sentence
windows — for production (timeline.build_project_timeline), for the
editor (run_revision_render's retime block) and for caption event
placement (captions.build_captions). Before this module, production
windows were *reconstructed* from Whisper caption timings and could
degrade to a uniform total÷N split; only the editor measured real
part durations, privately.

This module is small and pure: it performs no I/O and probes
nothing. It consumes a parts manifest (the dict narration persists
as parts_manifest.json, schema v2) whose entries carry
``measured_duration_seconds`` measured at generation time through
services.media_probe.probe_audio, and derives windows by cumulative
sums in part order.

A part with no recorded measurement is a hard error here
(``TimingUnavailableError``, "timing_unavailable"): an unmeasured
window is never estimated, split or otherwise reconstructed. That
is the programme's no-silent-fallback rule applied to the film's
clock — missing/unmeasurable narration fails loudly, exactly as
corrupt narration already did.
"""

from __future__ import annotations

TIMING_SOURCE = "measured_part"

# Annex D2.2.3: divergence between Σ parts and the concatenated
# narration file beyond this tolerance (concat/frame rounding) is
# recorded as a timing warning in the run's asset report.
CONCAT_TOLERANCE_SECONDS = 0.15


class TimingUnavailableError(RuntimeError):
    """Raised when measured sentence timing cannot be produced:
    no parts manifest, a part without a recorded measurement, or a
    manifest whose parts do not form the contiguous 0..n-1 sequence.
    The message always carries the machine-readable
    ``timing_unavailable`` marker (annex D2.2.7)."""


def _manifest_parts(parts_manifest) -> list:
    """Normalise a parts manifest (full dict or bare parts list)
    into its parts entries, sorted by index."""
    if isinstance(parts_manifest, dict):
        parts = parts_manifest.get("parts") or []
    elif isinstance(parts_manifest, (list, tuple)):
        parts = list(parts_manifest)
    else:
        parts = []
    entries = [entry for entry in parts if isinstance(entry, dict)]
    entries.sort(key=lambda entry: entry.get("index", 0))
    return entries


def _measured_duration(entry) -> float:
    raw = entry.get("measured_duration_seconds")
    if raw is None:
        raise TimingUnavailableError(
            "timing_unavailable: narration part "
            f"{entry.get('index')} ({entry.get('file')}) has no "
            "measured_duration_seconds recorded — windows are never "
            "reconstructed from estimates."
        )
    try:
        duration = float(raw)
    except (TypeError, ValueError):
        raise TimingUnavailableError(
            "timing_unavailable: narration part "
            f"{entry.get('index')} ({entry.get('file')}) carries an "
            f"unusable measured_duration_seconds: {raw!r}."
        ) from None
    if duration < 0:
        raise TimingUnavailableError(
            "timing_unavailable: narration part "
            f"{entry.get('index')} ({entry.get('file')}) carries a "
            f"negative measured_duration_seconds: {raw!r}."
        )
    return duration


def sentence_windows(parts_manifest) -> list:
    """Derive the sentence windows from measured part durations.

    Returns [{sentence_index, start, end, duration,
    timing_source: "measured_part"}] — cumulative sums in part
    order, so window i starts exactly where window i-1 ended and
    the total duration is Σ parts. Raises TimingUnavailableError
    when any part is unmeasured or the part indexes are not the
    contiguous 0..n-1 sequence (a missing part would silently
    shift every later window).
    """
    entries = _manifest_parts(parts_manifest)
    if not entries:
        raise TimingUnavailableError(
            "timing_unavailable: no narration parts manifest "
            "available — sentence windows require measured part "
            "durations."
        )
    indexes = [entry.get("index") for entry in entries]
    if indexes != list(range(len(entries))):
        raise TimingUnavailableError(
            "timing_unavailable: narration parts do not form the "
            f"contiguous 0..{len(entries) - 1} sequence "
            f"(found {indexes}) — a missing part would shift every "
            "later window."
        )

    windows = []
    cursor = 0.0
    for entry in entries:
        duration = _measured_duration(entry)
        start = cursor
        end = cursor + duration
        windows.append(
            {
                "sentence_index": int(entry["index"]),
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(duration, 3),
                "timing_source": TIMING_SOURCE,
            }
        )
        cursor = end
    if cursor <= 0:
        raise TimingUnavailableError(
            "timing_unavailable: narration parts measure zero "
            "total duration."
        )
    return windows


def total_duration(parts_manifest) -> float:
    """Total narration duration: Σ measured part durations (3dp).

    Raises TimingUnavailableError on the same conditions as
    sentence_windows — the total is a timing fact, not an estimate.
    """
    entries = _manifest_parts(parts_manifest)
    if not entries:
        raise TimingUnavailableError(
            "timing_unavailable: no narration parts manifest "
            "available — total duration requires measured parts."
        )
    return round(sum(_measured_duration(entry) for entry in entries), 3)


def timing_consistency(parts_total_seconds, concatenated_duration_seconds,
                       tolerance_seconds=CONCAT_TOLERANCE_SECONDS) -> dict:
    """Compare Σ parts against the probed duration of the
    concatenated narration file (annex D2.2.3).

    Pure arithmetic on two measured numbers. The record is written
    into the run's asset report by the caller; a divergence beyond
    tolerance is a recorded warning, never silently absorbed.
    """
    parts_total = float(parts_total_seconds)
    concatenated = float(concatenated_duration_seconds)
    divergence = round(abs(parts_total - concatenated), 3)
    return {
        "parts_total_seconds": round(parts_total, 3),
        "concatenated_duration_seconds": round(concatenated, 3),
        "divergence_seconds": divergence,
        "tolerance_seconds": float(tolerance_seconds),
        "within_tolerance": divergence <= float(tolerance_seconds),
        "timing_source": TIMING_SOURCE,
    }
