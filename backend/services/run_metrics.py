"""Per-run execution metrics (Stage 0 — Protection & Observability).

A single-process holder for the counts a production run generates:
provider API calls, candidates discovered, downloads, coverage
recovery rounds, stage durations and failures. jobs.run_full_pipeline
calls begin_run() before any stage runs; choke points across the
retrieval stack increment the counters as work actually happens;
jobs passes snapshot() into the asset report as its top-level
"execution" section.

Design rules:
- BEST-EFFORT ONLY. Every public function swallows its own errors —
  a metrics bug must never be able to fail a production run.
- HONEST COSTS. No price table is configured for any provider, so
  monetary_cost_usd is None (unknown) — never 0, never estimated.
- Counts are of REAL work: the Serper counter sits at the HTTP choke
  point, so cache hits are not counted as API calls.
"""

from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()

# Serper endpoint path -> report bucket. "search" is the web endpoint.
_SERPER_KINDS = {"search": "web", "images": "images", "videos": "video"}

_COST_NOTE = "unknown — no price table configured"


def _fresh_state():
    return {
        "started_monotonic": None,
        "serper": {"total": 0, "web": 0, "images": 0, "video": 0},
        "archive_org_searches": 0,
        "wikimedia_searches": 0,
        "candidates_discovered": 0,
        "downloads_attempted": 0,
        "downloads_succeeded": 0,
        "bytes_downloaded": 0,
        "coverage_recovery_rounds": 0,
        "failures": 0,
        "stage_started_at": {},
        "stage_durations": {},
    }


_STATE = _fresh_state()


def begin_run():
    """Reset every counter and start the wall clock. Called once by
    jobs.run_full_pipeline before the first stage runs."""
    global _STATE
    try:
        with _LOCK:
            _STATE = _fresh_state()
            _STATE["started_monotonic"] = time.monotonic()
    except Exception:
        pass


def serper_call(path):
    """One real Serper HTTP request. `path` is the endpoint path used
    by services.search_provider ("search" / "images" / "videos");
    each retry attempt is a real call and is counted."""
    try:
        kind = _SERPER_KINDS.get(str(path or "").strip().lower())
        with _LOCK:
            _STATE["serper"]["total"] += 1
            if kind:
                _STATE["serper"][kind] += 1
    except Exception:
        pass


def archive_org_search():
    """One archive.org advancedsearch invocation."""
    try:
        with _LOCK:
            _STATE["archive_org_searches"] += 1
    except Exception:
        pass


def wikimedia_search():
    """One Wikimedia Commons search invocation (stills or video)."""
    try:
        with _LOCK:
            _STATE["wikimedia_searches"] += 1
    except Exception:
        pass


def candidates_discovered(count):
    """Candidates returned by discovery for one scenario pass (the
    same raw pools the provider/kind funnels in the asset report are
    built from)."""
    try:
        with _LOCK:
            _STATE["candidates_discovered"] += int(count or 0)
    except Exception:
        pass


def download_attempted():
    try:
        with _LOCK:
            _STATE["downloads_attempted"] += 1
    except Exception:
        pass


def download_succeeded(num_bytes=0):
    try:
        with _LOCK:
            _STATE["downloads_succeeded"] += 1
            try:
                _STATE["bytes_downloaded"] += max(int(num_bytes or 0), 0)
            except (TypeError, ValueError):
                pass
    except Exception:
        pass


def coverage_recovery_round():
    """One coverage-recovery round actually executed for a beat."""
    try:
        with _LOCK:
            _STATE["coverage_recovery_rounds"] += 1
    except Exception:
        pass


def stage_started(stage):
    try:
        with _LOCK:
            _STATE["stage_started_at"][str(stage)] = time.monotonic()
    except Exception:
        pass


def _close_stage(stage):
    started = _STATE["stage_started_at"].pop(str(stage), None)
    if started is not None:
        _STATE["stage_durations"][str(stage)] = round(
            time.monotonic() - started, 3
        )


def stage_finished(stage):
    try:
        with _LOCK:
            _close_stage(stage)
    except Exception:
        pass


def stage_failed(stage):
    """A stage failure finalises that stage's duration and counts
    toward the run's failure total."""
    try:
        with _LOCK:
            _close_stage(stage)
            _STATE["failures"] += 1
    except Exception:
        pass


def snapshot():
    """The report-ready "execution" block. Never raises."""
    try:
        with _LOCK:
            started = _STATE["started_monotonic"]
            wall = (
                round(time.monotonic() - started, 3)
                if started is not None
                else None
            )
            attempted = _STATE["downloads_attempted"]
            succeeded = _STATE["downloads_succeeded"]
            return {
                "wall_time_seconds": wall,
                "stage_durations_seconds": dict(_STATE["stage_durations"]),
                "serper_calls": dict(_STATE["serper"]),
                "archive_org_searches": _STATE["archive_org_searches"],
                "wikimedia_searches": _STATE["wikimedia_searches"],
                "candidates_discovered": _STATE["candidates_discovered"],
                "downloads": {
                    "attempted": attempted,
                    "succeeded": succeeded,
                    "failed": attempted - succeeded,
                    "bytes_downloaded": _STATE["bytes_downloaded"],
                },
                "coverage_recovery_rounds": _STATE[
                    "coverage_recovery_rounds"
                ],
                "failures": _STATE["failures"],
                "monetary_cost_usd": None,
                "monetary_cost_note": _COST_NOTE,
            }
    except Exception:
        return {
            "wall_time_seconds": None,
            "stage_durations_seconds": {},
            "serper_calls": {"total": 0, "web": 0, "images": 0, "video": 0},
            "archive_org_searches": 0,
            "wikimedia_searches": 0,
            "candidates_discovered": 0,
            "downloads": {
                "attempted": 0,
                "succeeded": 0,
                "failed": 0,
                "bytes_downloaded": 0,
            },
            "coverage_recovery_rounds": 0,
            "failures": 0,
            "monetary_cost_usd": None,
            "monetary_cost_note": _COST_NOTE,
        }
