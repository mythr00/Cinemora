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
- Counts are of REAL work: the search counter sits at the HTTP choke
  point, so cache hits are not counted as API calls.
- RUN-SCOPED (Phase 8.4): states live in a dict keyed by run id;
  begin_run(run_id) creates and binds that run's state, counters
  land on the bound run, and snapshot(run_id) reads a specific run
  back. The pre-8.4 holder was one global state — a second run
  starting in the same process (SimpleWorker sequential runs, or a
  late snapshot) silently overwrote the first run's counts. The
  binding itself is still process-global by design: the worker is
  process-per-run (design 03 §1), so exactly one run is bound at a
  time in production; the keyed states make each run's record
  addressable and incorruptible after the fact.
"""

from __future__ import annotations

import threading
import time
import uuid

_LOCK = threading.Lock()

# Search endpoint path -> report bucket. "search" is the web endpoint.
_SEARCH_KINDS = {"search": "web", "images": "images", "videos": "video"}

_COST_NOTE = "unknown — no price table configured"


def _fresh_state():
    return {
        "started_monotonic": None,
        # Search counters (SearXNG is the sole provider since the
        # 2026-11-26 removal): real HTTP attempts per kind plus
        # the failure count, so a run's search cost is visible.
        "search": {
            "searxng": {"total": 0, "web": 0, "images": 0, "video": 0},
            "searxng_failures": 0,
        },
        "llm": {"total": 0, "total_tokens": 0},
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


_STATES: dict[str, dict] = {}
_BOUND_RUN_KEY: str | None = None
_MAX_KEPT_STATES = 8


def _bound_state() -> dict:
    """The currently bound run's state, creating an ad-hoc one when
    nothing is bound (choke points outside a run — unchanged
    pre-8.4 behaviour of counting into a default holder)."""
    global _BOUND_RUN_KEY
    if _BOUND_RUN_KEY is None or _BOUND_RUN_KEY not in _STATES:
        key = f"adhoc-{uuid.uuid4().hex}"
        _STATES[key] = _fresh_state()
        _BOUND_RUN_KEY = key
    return _STATES[_BOUND_RUN_KEY]


def begin_run(run_id=None):
    """Start a fresh state for a run and bind it. Called once by
    jobs.run_full_pipeline before the first stage runs. `run_id`
    may be None when the ledger id isn't known yet — rebind_run()
    renames the binding once it is."""
    global _BOUND_RUN_KEY
    try:
        with _LOCK:
            key = str(run_id) if run_id else f"adhoc-{uuid.uuid4().hex}"
            state = _fresh_state()
            state["started_monotonic"] = time.monotonic()
            _STATES[key] = state
            _BOUND_RUN_KEY = key
            # Bound memory: keep only the most recent states.
            while len(_STATES) > _MAX_KEPT_STATES:
                oldest = next(iter(_STATES))
                if oldest == _BOUND_RUN_KEY:
                    break
                _STATES.pop(oldest, None)
            return key
    except Exception:
        return None


def rebind_run(run_id):
    """Rename the bound state to the ledger run id (jobs learns the
    id at claim time, after begin_run)."""
    global _BOUND_RUN_KEY
    if not run_id:
        return
    try:
        with _LOCK:
            if _BOUND_RUN_KEY and _BOUND_RUN_KEY in _STATES:
                _STATES[str(run_id)] = _STATES.pop(_BOUND_RUN_KEY)
                _BOUND_RUN_KEY = str(run_id)
    except Exception:
        pass


def _state_for(run_id=None) -> dict:
    if run_id is not None:
        state = _STATES.get(str(run_id))
        if state is not None:
            return state
    return _bound_state()


def bound_run_id():
    """The real run id currently bound, or None when the binding
    is an ad-hoc holder (choke points firing outside a run).
    Used by the search choke point to attribute provider-usage
    ledger rows to the run that caused them."""
    try:
        with _LOCK:
            key = _BOUND_RUN_KEY
        if key and not str(key).startswith("adhoc-"):
            return str(key)
    except Exception:
        pass
    return None


def search_call(provider, path):
    """One real search-provider HTTP request (Master
    Implementation 2026-11-26). `provider` is "searxng" (the sole
    provider); `path` is the endpoint kind ("search" / "images" /
    "videos") mapped through the kind table."""
    try:
        kind = _SEARCH_KINDS.get(str(path or "").strip().lower())
        with _LOCK:
            block = _bound_state()["search"].setdefault(
                str(provider),
                {"total": 0, "web": 0, "images": 0, "video": 0},
            )
            block["total"] += 1
            if kind:
                block[kind] += 1
    except Exception:
        pass


def search_provider_failure(provider):
    """The search provider failed a logical call (after its
    bounded retries) and the typed error was raised."""
    try:
        with _LOCK:
            block = _bound_state()["search"]
            key = f"{provider}_failures"
            block[key] = int(block.get(key) or 0) + 1
    except Exception:
        pass


def archive_org_search():
    """One archive.org advancedsearch invocation."""
    try:
        with _LOCK:
            _bound_state()["archive_org_searches"] += 1
    except Exception:
        pass


def wikimedia_search():
    """One Wikimedia Commons search invocation (stills or video)."""
    try:
        with _LOCK:
            _bound_state()["wikimedia_searches"] += 1
    except Exception:
        pass


def candidates_discovered(count):
    """Candidates returned by discovery for one scenario pass (the
    same raw pools the provider/kind funnels in the asset report are
    built from)."""
    try:
        with _LOCK:
            _bound_state()["candidates_discovered"] += int(count or 0)
    except Exception:
        pass


def download_attempted():
    try:
        with _LOCK:
            _bound_state()["downloads_attempted"] += 1
    except Exception:
        pass


def download_succeeded(num_bytes=0):
    try:
        with _LOCK:
            _bound_state()["downloads_succeeded"] += 1
            try:
                _bound_state()["bytes_downloaded"] += max(int(num_bytes or 0), 0)
            except (TypeError, ValueError):
                pass
    except Exception:
        pass


def coverage_recovery_round():
    """One coverage-recovery round actually executed for a beat."""
    try:
        with _LOCK:
            _bound_state()["coverage_recovery_rounds"] += 1
    except Exception:
        pass


def stage_started(stage):
    try:
        with _LOCK:
            _bound_state()["stage_started_at"][str(stage)] = time.monotonic()
    except Exception:
        pass


def _close_stage(stage):
    started = _bound_state()["stage_started_at"].pop(str(stage), None)
    if started is not None:
        _bound_state()["stage_durations"][str(stage)] = round(
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
            _bound_state()["failures"] += 1
    except Exception:
        pass


def llm_call(total_tokens=0):
    """One real BrewKeg LLM request (Phase 8.4: the counter whose
    absence the Phase 7 audit found — llm_client never counted).
    Token usage comes from the gateway response when it reports
    any; unknown usage counts the call with 0 tokens, never an
    estimate."""
    try:
        with _LOCK:
            _bound_state()["llm"]["total"] += 1
            try:
                _bound_state()["llm"]["total_tokens"] += max(
                    int(total_tokens or 0), 0
                )
            except (TypeError, ValueError):
                pass
    except Exception:
        pass


def process_totals() -> dict:
    """Counters summed across every run state this PROCESS still
    holds (Phase 5c-1 / B-10). Process-local by construction: the
    states are in-memory, capped at the most recent runs, and say
    nothing about other processes — callers must label them as
    such. Never raises."""
    try:
        with _LOCK:
            llm = {"total": 0, "total_tokens": 0}
            searxng = {"total": 0, "web": 0, "images": 0, "video": 0}
            searxng_failures = 0
            for state in _STATES.values():
                llm["total"] += int(state["llm"].get("total") or 0)
                llm["total_tokens"] += int(
                    state["llm"].get("total_tokens") or 0
                )
                block = state.get("search") or {}
                sx = block.get("searxng") or {}
                for key in searxng:
                    searxng[key] += int(sx.get(key) or 0)
                searxng_failures += int(block.get("searxng_failures") or 0)
            return {
                "llm_calls": llm,
                "search_calls": {
                    "searxng": searxng,
                    "searxng_failures": searxng_failures,
                },
            }
    except Exception:
        return {
            "llm_calls": {"total": 0, "total_tokens": 0},
            "search_calls": {
                "searxng": {"total": 0, "web": 0, "images": 0, "video": 0},
                "searxng_failures": 0,
            },
        }


def snapshot(run_id=None):
    """The report-ready "execution" block for a run (default: the
    bound run). Never raises."""
    try:
        with _LOCK:
            state = _state_for(run_id)
            started = state["started_monotonic"]
            wall = (
                round(time.monotonic() - started, 3)
                if started is not None
                else None
            )
            attempted = state["downloads_attempted"]
            succeeded = state["downloads_succeeded"]
            search_block = state.get("search") or {}
            return {
                "wall_time_seconds": wall,
                "stage_durations_seconds": dict(state["stage_durations"]),
                "search_calls": {
                    "searxng": dict(
                        search_block.get("searxng")
                        or {"total": 0, "web": 0, "images": 0, "video": 0}
                    ),
                    "searxng_failures": int(
                        search_block.get("searxng_failures") or 0
                    ),
                },
                "llm_calls": dict(state["llm"]),
                "archive_org_searches": state["archive_org_searches"],
                "wikimedia_searches": state["wikimedia_searches"],
                "candidates_discovered": state["candidates_discovered"],
                "downloads": {
                    "attempted": attempted,
                    "succeeded": succeeded,
                    "failed": attempted - succeeded,
                    "bytes_downloaded": state["bytes_downloaded"],
                },
                "coverage_recovery_rounds": state[
                    "coverage_recovery_rounds"
                ],
                "failures": state["failures"],
                "monetary_cost_usd": None,
                "monetary_cost_note": _COST_NOTE,
            }
    except Exception:
        return {
            "wall_time_seconds": None,
            "stage_durations_seconds": {},
            "search_calls": {
                "searxng": {"total": 0, "web": 0, "images": 0, "video": 0},
                "searxng_failures": 0,
            },
            "llm_calls": {"total": 0, "total_tokens": 0},
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
