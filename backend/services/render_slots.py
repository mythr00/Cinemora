"""Render slots: a Redis counting semaphore for final encodes
(Phase 8.4; design 03 §6.2).

The final ffmpeg encode is the CPU-saturating stage: N workers ×
unbounded ffmpeg = N encodes at 1/N speed, timing out together.
Retrieval/narration/graphics overlap freely; final encodes contend
for CINEMORA_RENDER_CONCURRENCY slots (default 1 on this host
class, formula in 03 §6.1).

Implementation: one Redis key per slot (`render_slot:<i>`), claimed
with SET NX + a lease (default 180 s) that a renewal thread keeps
alive while the render runs; release deletes the key only when it
still carries our token (checked non-atomically — the renewal
thread makes expiry-while-held a practical impossibility, and the
worst case of the residual race is one extra concurrent encode,
never corruption). If Redis is unreachable the render proceeds
unbounded (fail-open): a Redis outage must not stop productions,
and the pre-8.4 behaviour was unbounded anyway.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_redis_client = None

LEASE_SECONDS = 180
RENEW_EVERY_SECONDS = 60


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


def render_concurrency() -> int:
    try:
        return max(1, int(os.getenv("CINEMORA_RENDER_CONCURRENCY", "1")))
    except ValueError:
        return 1


def _try_claim(client, token: str) -> int | None:
    for slot in range(render_concurrency()):
        key = f"render_slot:{slot}"
        if client.set(key, token, nx=True, ex=LEASE_SECONDS):
            return slot
    return None


@contextmanager
def render_slot(timeout: float = 3600.0):
    """Hold one render slot for the duration of the block. Yields
    the slot index, or None when running unbounded (Redis down) —
    callers proceed either way."""
    token = uuid.uuid4().hex
    slot = None
    client = None
    stop = threading.Event()
    try:
        try:
            client = _redis()
            deadline = time.monotonic() + timeout
            while slot is None:
                slot = _try_claim(client, token)
                if slot is not None:
                    break
                if time.monotonic() >= deadline:
                    logger.warning(
                        "render_slots: no slot within %.0fs; "
                        "rendering unbounded rather than failing",
                        timeout,
                    )
                    break
                time.sleep(2.0)
        except Exception:
            logger.warning(
                "render_slots: Redis unavailable; rendering unbounded",
                exc_info=True,
            )
            client = None
            slot = None

        if client is not None and slot is not None:
            key = f"render_slot:{slot}"

            def _renew():
                while not stop.wait(RENEW_EVERY_SECONDS):
                    try:
                        if client.get(key) == token:
                            client.expire(key, LEASE_SECONDS)
                    except Exception:
                        pass

            threading.Thread(
                target=_renew, name="render-slot-renewal", daemon=True
            ).start()
        yield slot
    finally:
        stop.set()
        if client is not None and slot is not None:
            try:
                key = f"render_slot:{slot}"
                if client.get(key) == token:
                    client.delete(key)
            except Exception:
                pass
