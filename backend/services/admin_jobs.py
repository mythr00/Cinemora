"""Admin failed/dead job management (Phase 5b / B-11).

Two real sources, both read as-is:

- the run LEDGER (production_runs with status "failed") — the
  durable record of failed productions;
- the RQ failed-job registries of both production queues — the
  dead queue records workers left behind.

Operations:

- list / inspect: merged views over both sources. This surface
  is admin-only (api_admin guard), so entries carry FULL
  diagnostics (error text, RQ exception info) — the FS
  sanitisation that applies to tenant surfaces does not apply
  here (Workstream FS: admins see full diagnostics).
- requeue: a FAILED ledger run is resubmitted through the exact
  studio dispatch path the admin replacement endpoint already
  uses — jobs.enqueue_full_pipeline, which records the new run
  in the ledger (owner stamped from the project by the ledger
  choke point), deduplicates against active runs, and promotes
  through services.dispatcher with its plan/platform admission
  rules. Requeueing a run that is already queued / running /
  awaiting approval is an idempotent no-op.
- purge: removes ONLY the RQ failed-registry record (and its job
  payload). It never deletes or mutates the production run row,
  the project, or any project data — the ledger stays as the
  historical record of the failure.

Redis access uses the QUEUE connection (jobs.redis_connection —
RQ job payloads are pickled, so the text-decoding admin-health
client cannot read them), behind this module's own test seam.
Every RQ read fails soft into an empty registry view with the
error surfaced on the response, never a fabricated job list.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running", "awaiting_approval")

_redis_client = None


def set_redis_client(client) -> None:
    """Test seam: inject a Redis-compatible client (fakeredis,
    decode_responses=False to match the queue connection)."""
    global _redis_client
    _redis_client = client


def reset_redis_client() -> None:
    global _redis_client
    _redis_client = None


def _redis():
    global _redis_client
    if _redis_client is None:
        import jobs

        _redis_client = jobs.redis_connection
    return _redis_client


def _registries():
    """[(queue_name, FailedJobRegistry)] for both lanes."""
    from rq import Queue
    from rq.registry import FailedJobRegistry

    from services import admin_health

    client = _redis()
    out = []
    for name in admin_health.QUEUE_NAMES:
        queue = Queue(name, connection=client)
        out.append((name, FailedJobRegistry(queue=queue)))
    return out


def _job_error(job) -> str | None:
    try:
        result = job.latest_result()
        if result is not None and getattr(result, "exc_string", None):
            return result.exc_string
    except Exception:
        pass
    return getattr(job, "exc_info", None)


def _rq_failed_entries() -> tuple[list[dict], str | None]:
    entries: list[dict] = []
    try:
        from rq.job import Job

        for queue_name, registry in _registries():
            for job_id in registry.get_job_ids():
                try:
                    job = Job.fetch(job_id, connection=_redis())
                except Exception:
                    continue
                kwargs = job.kwargs or {}
                entries.append(
                    {
                        "id": job.id,
                        "source": "rq_failed_registry",
                        "job_id": job.id,
                        "run_id": kwargs.get("run_id"),
                        "project_id": kwargs.get("project_id")
                        or (job.args[0] if job.args else None),
                        "queue": queue_name,
                        "status": "failed",
                        "func_name": job.func_name,
                        "error": _job_error(job),
                        "enqueued_at": job.enqueued_at.isoformat()
                        if job.enqueued_at
                        else None,
                        "ended_at": job.ended_at.isoformat()
                        if job.ended_at
                        else None,
                    }
                )
    except Exception as exc:
        return [], str(exc)[:300]
    return entries, None


def _ledger_failed_entries(db, limit: int, offset: int) -> tuple[list[dict], int]:
    from models import ProductionRun

    query = db.query(ProductionRun).filter(
        ProductionRun.status == "failed"
    )
    total = query.count()
    rows = (
        query.order_by(ProductionRun.finished_at.desc())
        .offset(max(0, offset))
        .limit(max(1, min(limit, 200)))
        .all()
    )
    entries = [
        {
            "id": row.id,
            "source": "ledger",
            "job_id": (row.options or {}).get("dispatched_job_id"),
            "run_id": row.id,
            "project_id": row.project_id,
            "owner_id": row.owner_id,
            "queue": None,
            "status": row.status,
            "stage": row.stage,
            "error": row.error,
            "created_at": row.created_at.isoformat()
            if row.created_at
            else None,
            "finished_at": row.finished_at.isoformat()
            if row.finished_at
            else None,
        }
        for row in rows
    ]
    return entries, total


def list_failed_jobs(db, limit: int = 50, offset: int = 0) -> dict:
    ledger_entries, ledger_total = _ledger_failed_entries(db, limit, offset)
    rq_entries, rq_error = _rq_failed_entries()
    # RQ entries whose run is already represented by a ledger entry
    # on this page are not duplicated; the rest ride along.
    ledger_run_ids = {e["run_id"] for e in ledger_entries}
    extra = [
        e
        for e in rq_entries
        if e["run_id"] not in ledger_run_ids
        and e["job_id"] not in {x["job_id"] for x in ledger_entries}
    ]
    return {
        "total": ledger_total + len(extra),
        "jobs": ledger_entries + extra,
        "rq_failed_registry_error": rq_error,
    }


def get_failed_job(db, identifier: str) -> dict | None:
    """One failed job by run id or RQ job id, from either source."""
    from services import run_ledger

    run = run_ledger.get_run(identifier)
    if run is not None and run["status"] == "failed":
        from models import ProductionRun

        row = db.get(ProductionRun, run["run_id"])
        if row is not None:
            entry = {
                "id": row.id,
                "source": "ledger",
                "job_id": (row.options or {}).get("dispatched_job_id"),
                "run_id": row.id,
                "project_id": row.project_id,
                "owner_id": row.owner_id,
                "queue": None,
                "status": row.status,
                "stage": row.stage,
                "error": row.error,
                "progress": row.progress,
                "options": row.options,
                "execution": row.execution,
                "created_at": row.created_at.isoformat()
                if row.created_at
                else None,
                "started_at": row.started_at.isoformat()
                if row.started_at
                else None,
                "finished_at": row.finished_at.isoformat()
                if row.finished_at
                else None,
            }
            return entry
    rq_entries, _ = _rq_failed_entries()
    for entry in rq_entries:
        if entry["job_id"] == identifier or entry["run_id"] == identifier:
            return entry
    return None


def requeue_failed_run(run_id: str) -> dict:
    """Resubmit a failed run through the existing dispatch path.

    Returns a result dict; raises LookupError for an unknown run
    and ValueError for a non-failed terminal run. Active runs are
    an idempotent no-op (requeued=False, already_active=True).
    """
    import jobs
    from database import SessionLocal
    from models import Project
    from services import run_ledger

    run = run_ledger.get_run(run_id)
    if run is None:
        raise LookupError(f"No run {run_id}.")
    if run["status"] in ACTIVE_STATUSES:
        return {
            "requeued": False,
            "already_active": True,
            "run_id": run_id,
            "status": run["status"],
            "new_run_id": run_id,
        }
    if run["status"] != "failed":
        raise ValueError(
            f"Run {run_id} is {run['status']}; only failed runs "
            "can be requeued."
        )
    db = SessionLocal()
    try:
        project = db.get(Project, run["project_id"])
    finally:
        db.close()
    if project is None:
        raise LookupError(f"No project {run['project_id']}.")
    options = dict(run.get("options") or {})
    options.pop("dispatched_job_id", None)
    options.pop("dispatched_at", None)
    job_or_run = jobs.enqueue_full_pipeline(
        project.id, project.script, options=options or None
    )
    new_run = run_ledger.latest_run_for(
        project.id, run_ledger.script_sha256(project.script)
    )
    return {
        "requeued": True,
        "already_active": False,
        "run_id": run_id,
        "status": "failed",
        "new_run_id": new_run["run_id"] if new_run else None,
        "job_id": job_or_run if job_or_run != (
            new_run["run_id"] if new_run else None
        ) else None,
        "deduplicated": bool(
            new_run and new_run["run_id"] == job_or_run
        ),
    }


def purge_dead_job(identifier: str, db=None) -> dict | None:
    """Remove ONLY the RQ failed-registry record for a dead job.

    `identifier` is an RQ job id, or a run id whose dispatched job
    id is resolved from the ledger options. Returns the purge
    result, or None when no queue record exists. Project and run
    rows are never touched.
    """
    from rq.job import Job

    job_ids = [identifier]
    if db is not None:
        from models import ProductionRun

        row = db.get(ProductionRun, str(identifier))
        if row is not None:
            dispatched = (row.options or {}).get("dispatched_job_id")
            if dispatched:
                job_ids.append(dispatched)
    client = _redis()
    for queue_name, registry in _registries():
        for job_id in job_ids:
            try:
                if job_id not in registry.get_job_ids():
                    continue
                job = Job.fetch(job_id, connection=client)
                registry.remove(job, delete_job=True)
                return {
                    "purged": True,
                    "job_id": job_id,
                    "queue": queue_name,
                }
            except Exception:
                continue
    return None
