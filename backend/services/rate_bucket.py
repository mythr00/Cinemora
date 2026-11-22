"""Platform token buckets at the provider choke points (Phase 8.4;
design 03 §5).

Provider rate limits are PLATFORM resources: one Serper key and one
BrewKeg key serve every tenant, so the limit must be enforced where
all workers can see it — Redis — not per process (01 §4.5). The
bucket is a fixed window: at most `limit` acquisitions per
`window_seconds` per scope, counted with INCR + EXPIRE at the
window's first hit (atomic per command; the benign race is two
workers both seeing count == limit at a window edge, bounded and
harmless at these rates).

FAIL-OPEN by design: if Redis is unreachable the acquisition
succeeds. The per-process semaphores that predate this module stay
in place as the local bound, and a provider outage plan that also
takes Redis down should degrade to yesterday's behaviour, not to a
full stop. Every failure is counted in-process for observability.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_fail_open_count = 0

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


def _limit_for(scope: str) -> tuple[int, float]:
    """(max acquisitions, window seconds) for a scope, from env:
    CINEMORA_<SCOPE>_RPS (per-second rate, fractional allowed via
    a longer window). Defaults are deliberately generous — the
    bucket is a coordination ceiling, not a throttle: Serper 20/s,
    LLM 10/s platform-wide."""
    raw = os.getenv(f"CINEMORA_{scope.upper()}_RPS")
    if raw:
        try:
            rps = float(raw)
            if rps >= 1:
                return int(rps), 1.0
            if rps > 0:
                return 1, 1.0 / rps
        except ValueError:
            pass
    return (20 if scope == "serper" else 10), 1.0


def acquire(scope: str, timeout: float = 30.0) -> bool:
    """Block until a token is available (or timeout). Returns False
    only on timeout; Redis errors fail open (True)."""
    global _fail_open_count
    limit, window = _limit_for(scope)
    deadline = time.monotonic() + timeout
    while True:
        try:
            client = _redis()
            bucket = int(time.time() / window)
            key = f"ratebucket:{scope}:{bucket}"
            count = client.incr(key)
            if count == 1:
                client.expire(key, int(window) + 2)
            if count <= limit:
                return True
        except Exception:
            with _LOCK:
                _fail_open_count += 1
            logger.warning(
                "rate_bucket: Redis unavailable; failing open for %s",
                scope,
                exc_info=True,
            )
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.25, window / 4 or 0.05))


def fail_open_count() -> int:
    with _LOCK:
        return _fail_open_count
