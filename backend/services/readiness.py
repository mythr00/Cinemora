"""Liveness + readiness (Phase 5b / B-15).

Two different questions, two different surfaces:

- LIVENESS (public): "is the HTTP process alive?" — answers
  ``{"status": "ok"}`` and NOTHING else. No dependency state, no
  version, no diagnostics: ordinary users and anonymous callers
  must never see internal detail (Workstream FS rules).
- READINESS (admin-only, api_admin): "can this deployment
  actually serve productions?" — each dependency is probed for
  real and reported individually with its latency:

  * database — a real ``SELECT 1`` through the caller's session
    (not a schema/config assertion);
  * redis — a real PING through the existing queue Redis client
    seam (services.admin_health's client, which is configured
    from the same REDIS_URL as jobs' queue connection);
  * workers — heartbeat evidence only: the RQ worker registry
    (workers heartbeat into Redis) PLUS the Phase 3b worker
    lifecycle event store (services.worker_events — the durable
    JSONL record of worker_start / worker_spawned /
    worker_died / worker_exit). When NEITHER source shows a live
    worker the check reports ``unknown`` — never a fabricated
    ``ok``;
  * version — app version / commit from the environment when the
    deployment provides them (CINEMORA_APP_VERSION /
    CINEMORA_GIT_COMMIT / GIT_COMMIT), else the FastAPI app
    version, with commit reported as None rather than invented.

Overall status: ``ready`` when every check is ok, ``degraded``
when a non-critical check is not ok (e.g. no worker evidence),
``not_ready`` when a CRITICAL check (database, redis) fails.
Every probe fails soft into its own error/unknown state — one
dead dependency never masks another's result.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

# A lifecycle "start" event counts as live-worker evidence only
# while it is fresh and no later exit/death for the same worker
# supersedes it.
WORKER_EVIDENCE_WINDOW_SECONDS = 900

CRITICAL_CHECKS = ("database", "redis")


def liveness() -> dict:
    """The entire public liveness contract."""
    return {"status": "ok"}


def _timed(fn) -> tuple[dict, float]:
    started = time.monotonic()
    try:
        detail = fn()
    except Exception as exc:  # probe failure IS the result
        detail = {"status": "error", "error": str(exc)[:300]}
    return detail, round((time.monotonic() - started) * 1000, 1)


def _database_check(db) -> dict:
    from sqlalchemy import text

    if db is None:
        from database import SessionLocal

        session = SessionLocal()
        try:
            session.execute(text("SELECT 1")).scalar()
        finally:
            session.close()
    else:
        db.execute(text("SELECT 1")).scalar()
    return {"status": "ok", "query": "SELECT 1"}


def _redis_check() -> dict:
    from services import admin_health

    pong = admin_health._redis().ping()
    return {"status": "ok" if pong else "error", "ping": bool(pong)}


def _lifecycle_worker_evidence() -> dict:
    """Live workers according to the Phase 3b lifecycle store:
    a fresh worker_start/worker_spawned with no later
    worker_exit/worker_died for the same worker identity."""
    from services import worker_events

    events = worker_events.read_events(limit=500)
    now = datetime.now(timezone.utc)
    live: dict[str, dict] = {}
    dead: set[str] = set()
    for event in events:
        kind = event.get("event")
        worker_id = event.get("worker_id") or event.get("name")
        key = str(worker_id or event.get("pid") or "?")
        if kind in ("worker_start", "worker_spawned"):
            live[key] = event
            dead.discard(key)
        elif kind in ("worker_exit", "worker_died", "web_died"):
            live.pop(key, None)
            dead.add(key)
    fresh = []
    for key, event in live.items():
        try:
            stamp = datetime.fromisoformat(str(event.get("ts")))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age = (now - stamp).total_seconds()
        except (TypeError, ValueError):
            continue
        if age <= WORKER_EVIDENCE_WINDOW_SECONDS:
            fresh.append(
                {
                    "worker_id": key,
                    "event": event.get("event"),
                    "ts": event.get("ts"),
                    "age_seconds": round(age, 1),
                    "hostname": event.get("hostname"),
                }
            )
    return {"live_workers": fresh, "events_seen": len(events)}


def _workers_check() -> dict:
    from services import admin_health

    rq_workers: list = []
    rq_error = None
    try:
        component = admin_health._workers_component()
        rq_workers = component.get("workers") or []
        if component.get("status") == "error":
            rq_error = component.get("error")
    except Exception as exc:
        rq_error = str(exc)[:300]
    try:
        lifecycle = _lifecycle_worker_evidence()
    except Exception as exc:
        lifecycle = {"live_workers": [], "events_seen": 0,
                     "error": str(exc)[:300]}
    live = [
        {
            "name": w.get("name"),
            "state": w.get("state"),
            "last_heartbeat": w.get("last_heartbeat"),
            "source": "rq_registry",
        }
        for w in rq_workers
    ] + [
        {**entry, "source": "worker_lifecycle_events"}
        for entry in lifecycle.get("live_workers", [])
    ]
    if live:
        return {
            "status": "ok",
            "present": True,
            "workers": live,
            "rq_registry_error": rq_error,
            "lifecycle_events_seen": lifecycle.get("events_seen"),
        }
    # Truthful absence: neither heartbeat source shows a worker.
    return {
        "status": "unknown",
        "present": False,
        "workers": [],
        "note": (
            "No live worker evidence: the RQ worker registry is "
            "empty/unreachable and the worker lifecycle event "
            "store holds no fresh worker_start. This is reported "
            "as unknown, not ok."
        ),
        "rq_registry_error": rq_error,
        "lifecycle_events_seen": lifecycle.get("events_seen"),
    }


def _version_check() -> dict:
    version = (
        os.getenv("CINEMORA_APP_VERSION")
        or os.getenv("APP_VERSION")
        or "0.3.0"  # main.app FastAPI version
    )
    commit = (
        os.getenv("CINEMORA_GIT_COMMIT")
        or os.getenv("GIT_COMMIT")
        or os.getenv("COMMIT_SHA")
        or None
    )
    return {"status": "ok", "version": version, "commit": commit}


def readiness(db=None) -> dict:
    """Admin readiness report: every check individually timed."""
    checks: dict[str, dict] = {}
    for name, probe in (
        ("database", lambda: _database_check(db)),
        ("redis", _redis_check),
        ("workers", _workers_check),
        ("version", _version_check),
    ):
        detail, latency_ms = _timed(probe)
        detail = dict(detail)
        detail["latency_ms"] = latency_ms
        detail["critical"] = name in CRITICAL_CHECKS
        checks[name] = detail
    if any(checks[name]["status"] != "ok" for name in CRITICAL_CHECKS):
        overall = "not_ready"
    elif any(check["status"] != "ok" for check in checks.values()):
        overall = "degraded"
    else:
        overall = "ready"
    return {
        "status": overall,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }
