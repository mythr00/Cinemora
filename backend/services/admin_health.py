"""Admin infrastructure reads (Phase 7; design admin/02 M3/M5).

Composite, per-component health: every component reports its own
status with its own evidence, and a component's failure never
masquerades as another's success (the pre-Phase-7 /health strings
were static and proved only that the HTTP process was alive; they
are left untouched for compatibility and are not used here).

Sources: database.check_schema() for the DB component; a Redis
ping + the RQ worker registry + queue lists for workers/queues
(the run LEDGER remains the durable queue truth — Redis figures
are shown alongside it, divergences visible rather than hidden);
provider backoff keys written by services/provider_health; the
cached media walk for storage. Redis access is lazy and seam-able
(`set_redis_client`) so tests can inject fakeredis; every Redis
read fails soft into that component's error state.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

QUEUE_NAMES = ("video_generation", "video_generation_priority")
JOB_TIMEOUT_SECONDS = 3600  # jobs.py Queue default_timeout

_redis_client = None


def _redis():
    global _redis_client
    if _redis_client is None:
        from redis import Redis

        _redis_client = Redis.from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _redis_client


def set_redis_client(client) -> None:
    """Test seam: inject a Redis-compatible client (fakeredis)."""
    global _redis_client
    _redis_client = client


def reset_redis_client() -> None:
    global _redis_client
    _redis_client = None


def _component(status: str, **detail) -> dict:
    return {"status": status, **detail}


def _database_component() -> dict:
    import database

    try:
        result = database.check_schema()
        return _component(
            "ok",
            current_revision=result.get("current_revision"),
            expected_head=result.get("expected_head"),
            schema_status=result.get("status"),
        )
    except Exception as exc:  # SchemaVersionError or connectivity
        return _component("error", error=str(exc)[:300])


def _redis_component() -> dict:
    try:
        started = time.monotonic()
        pong = _redis().ping()
        return _component(
            "ok",
            ping=pong,
            latency_ms=round((time.monotonic() - started) * 1000, 1),
        )
    except Exception as exc:
        return _component("error", error=str(exc)[:300])


def _workers_component() -> dict:
    """RQ worker registry (Redis-backed). Workers register
    themselves; an unreachable Redis or an empty registry is
    reported as observed, never smoothed over."""
    try:
        from rq import Worker

        workers = Worker.all(connection=_redis())
        return _component(
            "ok" if workers else "absent",
            workers=[
                {
                    "name": w.name,
                    "state": w.state,
                    "queues": [q.name for q in w.queues],
                    "current_job_id": w.get_current_job_id(),
                    "successful_job_count": w.successful_job_count,
                    "failed_job_count": w.failed_job_count,
                    "last_heartbeat": w.last_heartbeat.isoformat()
                    if getattr(w, "last_heartbeat", None)
                    else None,
                    "birth_date": w.birth_date.isoformat()
                    if getattr(w, "birth_date", None)
                    else None,
                }
                for w in workers
            ],
        )
    except Exception as exc:
        return _component("error", error=str(exc)[:300])


def _queues_component(db) -> dict:
    """Ledger counts are primary (they survive Redis loss and match
    what the dispatcher will claim); RQ list lengths ride alongside
    when Redis answers."""
    from models import ProductionRun

    queued = (
        db.query(ProductionRun)
        .filter(ProductionRun.status == "queued")
        .all()
    )
    waiting = [
        r for r in queued if not (r.options or {}).get("dispatched_job_id")
    ]
    running = (
        db.query(ProductionRun)
        .filter(ProductionRun.status == "running")
        .count()
    )
    stuck = (
        db.query(ProductionRun)
        .filter(ProductionRun.status == "running")
        .all()
    )
    stuck_flagged = [
        r.id for r in stuck if (r.progress or {}).get("stuck_suspected")
    ]
    rq_lengths: dict[str, int | None] = {}
    rq_failed: dict[str, int | None] = {}
    try:
        client = _redis()
        for name in QUEUE_NAMES:
            rq_lengths[name] = client.llen(f"rq:queue:{name}")
            rq_failed[name] = client.zcard(f"rq:failed:{name}")
    except Exception:
        rq_lengths = {name: None for name in QUEUE_NAMES}
        rq_failed = {name: None for name in QUEUE_NAMES}
    return _component(
        "ok",
        ledger_queued=len(queued),
        ledger_waiting_undispatched=len(waiting),
        ledger_running=running,
        stuck_suspected_run_ids=stuck_flagged,
        rq_queue_lengths=rq_lengths,
        rq_failed_registry_sizes=rq_failed,
        job_timeout_seconds=JOB_TIMEOUT_SECONDS,
        retry_policy=(
            "The dispatcher's retry sweep creates a retry run for "
            "transient-classified failures only (max 2 attempts, "
            "60s then 300s backoff, services/dispatcher.py); RQ "
            "itself has no automatic retry."
        ),
    )


def _providers_component() -> dict:
    """Provider backoff state, read from the shared Redis keys
    services/provider_health maintains (provider:<scope>:
    backed_off_until)."""
    try:
        client = _redis()
        backed_off = []
        now = time.time()
        for key in client.scan_iter(match="provider:*:backed_off_until"):
            raw = client.get(key)
            try:
                until = float(raw)
            except (TypeError, ValueError):
                continue
            if until > now:
                scope = key[len("provider:"):-len(":backed_off_until")]
                backed_off.append(
                    {"scope": scope, "backed_off_until": until}
                )
        return _component("ok", backed_off_scopes=backed_off)
    except Exception as exc:
        return _component("error", error=str(exc)[:300])


def _storage_component() -> dict:
    from services import admin_storage, media_paths

    try:
        data = admin_storage.walk()
        root = media_paths.media_root()
        disk = shutil.disk_usage(root if root.exists() else root.parent)
        return _component(
            "ok",
            media_total_bytes=data["total_bytes"],
            media_total_files=data["total_files"],
            disk_free_bytes=disk.free,
            disk_total_bytes=disk.total,
            disk_note=(
                "Disk figures are this host's filesystem totals; "
                "media size is measured by walking the media tree."
            ),
        )
    except Exception as exc:
        return _component("error", error=str(exc)[:300])


def _public_site_component() -> dict:
    url = os.environ.get("CINEMORA_PUBLIC_URL") or ""
    if not url:
        return _component(
            "not_configured",
            note="CINEMORA_PUBLIC_URL is not set; no public "
            "reachability check is possible.",
        )
    try:
        import requests

        response = requests.head(url, timeout=5, allow_redirects=True)
        return _component(
            "ok" if response.status_code < 500 else "error",
            url=url,
            http_status=response.status_code,
        )
    except Exception as exc:
        return _component("error", url=url, error=str(exc)[:300])


def composite_health(db) -> dict:
    """M5: every component, individually evidenced."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "components": {
            "api": _component("ok", note="This response is served by "
                              "the API process itself."),
            "database": _database_component(),
            "redis": _redis_component(),
            "workers": _workers_component(),
            "queues": _queues_component(db),
            "providers": _providers_component(),
            "storage": _storage_component(),
            "public_site": _public_site_component(),
        },
    }


def worker_overview(db) -> dict:
    """M3: worker presence + queue state in one read."""
    health = composite_health(db)
    return {
        "generated_at": health["generated_at"],
        "workers": health["components"]["workers"],
        "queues": health["components"]["queues"],
        "providers": health["components"]["providers"],
    }
