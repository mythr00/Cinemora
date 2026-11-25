"""Provider download health — per-run observations over a SHARED,
Redis-backed backoff state (run-11 coverage fix 2; Phase 8.4).

Run 11's measured funnel: 105 downloads attempted, 16 succeeded.
The failures concentrated by provider scope: google.com/goto
wrappers 5 downloaded / 84 failed, YouTube 0 / 12 (repeated HTTP
429s), while Wikimedia and the direct-host long tail mostly
delivered. The pipeline had no memory of any of this: it kept
handing candidates to scopes that were actively rate-limiting it.

Two layers, deliberately separated (design 03 §5):

- PER-RUN OBSERVATIONS (in-process, this module's table): every
  real download attempt's outcome and every observed 429, per
  provider scope (the fetch URL's host, ``www.`` stripped), so
  per-provider success is a measured fact of the run, available
  via :func:`snapshot` for the production record. ``reset()`` at
  production start clears ONLY this layer.

- SHARED BACKOFF (Redis): ``RATE_LIMIT_TRIP_COUNT`` consecutive
  429s from one scope — counted across ALL workers, because the
  rate limit belongs to the platform's provider account, not to
  one process (01 §4.2) — backs the scope off platform-wide.
  Two semantic changes sharing forced: the backoff is
  TIME-BOUNDED (``BACKOFF_SECONDS``, renewed by further 429s)
  instead of "for the rest of the production", so one tenant's
  run cannot poison a scope for everyone indefinitely; and a
  successful download from ANY worker clears the scope's
  consecutive-429 counter (the scope recovered; everyone's view
  agrees because the state is shared).

:func:`is_backed_off` is consulted before every fetch; a backed-off
scope's candidates are refused with the typed
``provider_backed_off`` reason — no request is made, the candidate
is NOT marked as a failed download, and the draw moves on.

If Redis is unreachable, the module degrades to the pre-8.4
per-process semantics (the local observation table doubles as the
backoff state) — a Redis outage must not blind the pipeline.

Nothing here changes a verdict, a score or a budget: it only
decides whether a fetch is attempted at all, and records why not.
"""

import logging
import os
import threading
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Consecutive HTTP 429s from one provider scope after which the
#: scope is backed off (counted platform-wide since Phase 8.4).
RATE_LIMIT_TRIP_COUNT = 3

#: How long a tripped scope stays backed off without a further
#: 429. Further 429s renew it; a success clears it early.
BACKOFF_SECONDS = 600

#: Gate 2 (footage-discovery directive, 2026-11-26): Wikimedia
#: Commons scopes get retry-before-backoff. In the Container run,
#: Commons throttling produced 17 provider_backed_off refusals —
#: a transient 429 burst silenced the only licence-evidenced
#: source for the rest of the production. Commons callers that go
#: through services.licensed_media now make
#: WIKIMEDIA_RETRY_ATTEMPTS bounded delayed retries (honouring
#: Retry-After, capped at WIKIMEDIA_MAX_RETRY_DELAY_SECONDS so the
#: delay stays inside the existing per-candidate timeout budget)
#: BEFORE a 429 is recorded here at all; and the scope itself
#: trips only at WIKIMEDIA_RATE_LIMIT_TRIP_COUNT consecutive
#: recorded 429s, so one burst that a retry would have ridden out
#: does not trip it. The backoff protection itself is unchanged:
#: a persistent 429 stream still backs the scope off, for the
#: same BACKOFF_SECONDS, with the same shared-state semantics.
WIKIMEDIA_SCOPES = frozenset({
    "commons.wikimedia.org",
    "upload.wikimedia.org",
    "wikimedia.org",
})
WIKIMEDIA_RETRY_ATTEMPTS = 2
WIKIMEDIA_MAX_RETRY_DELAY_SECONDS = 5.0
WIKIMEDIA_RATE_LIMIT_TRIP_COUNT = 6


def is_wikimedia_scope(scope):
    """Whether a provider scope is a Wikimedia Commons scope."""
    return str(scope or "").lower() in WIKIMEDIA_SCOPES


def trip_count_for_scope(scope):
    """Consecutive recorded 429s that back a scope off."""
    if is_wikimedia_scope(scope):
        return WIKIMEDIA_RATE_LIMIT_TRIP_COUNT
    return RATE_LIMIT_TRIP_COUNT


def wikimedia_retry_delay(attempt, retry_after_seconds=None):
    """Bounded delay before a Commons delayed retry (attempt is
    1-based for the failure). A provider-supplied Retry-After is
    honoured but capped at WIKIMEDIA_MAX_RETRY_DELAY_SECONDS;
    without one, a short exponential delay applies, same cap."""
    if retry_after_seconds is not None:
        try:
            return max(
                0.0,
                min(float(retry_after_seconds),
                    WIKIMEDIA_MAX_RETRY_DELAY_SECONDS),
            )
        except (TypeError, ValueError):
            pass
    return min(
        0.5 * (2 ** max(0, int(attempt) - 1)),
        WIKIMEDIA_MAX_RETRY_DELAY_SECONDS,
    )

_LOCK = threading.Lock()
_SCOPES = {}

_redis_client = None


def _redis():
    global _redis_client
    if _redis_client is None:
        from redis import Redis

        _redis_client = Redis.from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
        )
    return _redis_client


def set_redis_client(client) -> None:
    """Test seam: inject a Redis-compatible client (fakeredis)."""
    global _redis_client
    _redis_client = client


def reset_redis_client() -> None:
    global _redis_client
    _redis_client = None


def _consec_key(scope):
    return f"provider:{scope}:consec429"


def _backoff_key(scope):
    return f"provider:{scope}:backed_off_until"


def _shared_trip(scope):
    """Record one 429 in the shared state; returns True when the
    scope is backed off platform-wide after this observation."""
    try:
        client = _redis()
        consec = client.incr(_consec_key(scope))
        client.expire(_consec_key(scope), BACKOFF_SECONDS * 2)
        if consec >= trip_count_for_scope(scope):
            client.set(
                _backoff_key(scope),
                str(time.time() + BACKOFF_SECONDS),
                ex=BACKOFF_SECONDS,
            )
            return True
        return _shared_is_backed_off(scope)
    except Exception:
        logger.warning(
            "provider_health: shared state unavailable; using local",
            exc_info=True,
        )
        return None  # sentinel: caller falls back to local state


def _shared_is_backed_off(scope):
    try:
        raw = _redis().get(_backoff_key(scope))
        if raw is None:
            return False
        return float(raw) > time.time()
    except Exception:
        return None


def _shared_clear(scope):
    try:
        client = _redis()
        client.delete(_consec_key(scope))
        client.delete(_backoff_key(scope))
    except Exception:
        pass


def scope_for_url(url):
    """The provider scope for a fetch URL: its host, lowercased,
    with a leading ``www.`` stripped. Empty string for no URL."""
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _entry(scope):
    return _SCOPES.setdefault(scope, {
        "attempts": 0,
        "succeeded": 0,
        "failed": 0,
        "rate_limited": 0,
        "consecutive_rate_limits": 0,
        "delayed_retries": 0,
        "delayed_retry_seconds": 0.0,
        "backed_off": False,
        "backed_off_reason": None,
    })


def record_download_outcome(scope, ok):
    """Record one real download attempt's outcome for a scope."""
    if not scope:
        return
    with _LOCK:
        entry = _entry(scope)
        entry["attempts"] += 1
        if ok:
            entry["succeeded"] += 1
            entry["consecutive_rate_limits"] = 0
            entry["backed_off"] = False
            entry["backed_off_reason"] = None
        else:
            entry["failed"] += 1
    if ok:
        # A success from any worker clears the shared counter:
        # the scope recovered (design 03 §5).
        _shared_clear(scope)


def record_delayed_retry(scope, delay_seconds):
    """Record one bounded delayed retry performed for a scope
    BEFORE any backoff accounting (Gate 2 retry-before-backoff for
    Wikimedia Commons). Purely observational: it never trips or
    clears a backoff; it makes the retry visible in the per-run
    snapshot / logs exactly like the 429 observations are."""
    if not scope:
        return
    try:
        delay = max(0.0, float(delay_seconds))
    except (TypeError, ValueError):
        delay = 0.0
    with _LOCK:
        entry = _entry(scope)
        entry["delayed_retries"] += 1
        entry["delayed_retry_seconds"] += delay
    logger.info(
        "provider_health: delayed retry for scope %s after %.2fs "
        "(retry-before-backoff)",
        scope, delay,
    )


def record_rate_limit(scope):
    """Record one observed HTTP 429 from a scope. Trips the shared
    backoff at the scope's trip count (RATE_LIMIT_TRIP_COUNT
    consecutive 429s counted platform-wide; Wikimedia Commons
    scopes trip at WIKIMEDIA_RATE_LIMIT_TRIP_COUNT because their
    callers retry with delay before recording — see the Gate 2
    note at the constants); returns True when backed off."""
    if not scope:
        return False
    trip_count = trip_count_for_scope(scope)
    shared = _shared_trip(scope)
    with _LOCK:
        entry = _entry(scope)
        entry["rate_limited"] += 1
        entry["consecutive_rate_limits"] += 1
        if shared is None:
            # Redis unavailable: pre-8.4 local semantics.
            if (
                entry["consecutive_rate_limits"] >= trip_count
                and not entry["backed_off"]
            ):
                entry["backed_off"] = True
                entry["backed_off_reason"] = (
                    f"{entry['consecutive_rate_limits']} consecutive "
                    "HTTP 429 responses"
                )
            return entry["backed_off"]
        if shared:
            entry["backed_off"] = True
            entry["backed_off_reason"] = (
                f"{trip_count}+ consecutive HTTP 429 "
                f"responses platform-wide; backed off for "
                f"{BACKOFF_SECONDS}s"
            )
        return shared


def is_backed_off(scope):
    """Whether the scope is currently backed off (shared state
    first; local per-production state as the outage fallback)."""
    if not scope:
        return False
    shared = _shared_is_backed_off(scope)
    if shared is not None:
        return shared
    with _LOCK:
        entry = _SCOPES.get(scope)
        return bool(entry and entry["backed_off"])


def snapshot():
    """A deep-enough copy of this run's per-scope observations, for
    the production record. `backed_off` reflects the shared state
    at snapshot time when Redis answers."""
    with _LOCK:
        result = {scope: dict(entry) for scope, entry in _SCOPES.items()}
    for scope, entry in result.items():
        shared = _shared_is_backed_off(scope)
        if shared is not None:
            entry["backed_off"] = shared
    return result


def reset():
    """Clear the PER-RUN observation table (called at every
    production start, alongside the other per-run caches). The
    shared Redis backoff is deliberately NOT cleared: it is a
    platform resource with its own time bounds (03 §5)."""
    with _LOCK:
        _SCOPES.clear()
