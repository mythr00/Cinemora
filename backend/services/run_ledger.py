"""Production run + asset decision ledger (Stage 2, AUD-07/09/10).

The database is the run's system of record:

- ``production_runs`` — one row per production run: lifecycle status,
  the full sha256 of the script (the dedup key), options, the run
  manifest pointer, the finishing run_metrics execution snapshot and
  the cooperative-cancellation flag.
- ``asset_decisions`` — the APPEND-ONLY decision ledger: every
  selected / still / rotated / generated / missing / rejected /
  swapped visual decision of a run, with its segment-evidence class,
  the verbatim verification verdict and the rights classification.
  Rows are never updated or deleted; a swap supersedes an earlier
  decision by pointing at it (previous_decision_id).

FAIL-SOFT CONTRACT: every function in this module that touches the
database logs and swallows database errors (returning None / False /
0), exactly like jobs._set_project_status. The ledger is the run's
record, never a pipeline dependency — a database outage must not be
able to fail a production. The only exception raised deliberately is
RunCancelled, which carries no database involvement.

Schema authority: migrations/versions/0002_run_ledger.py (AUD-09).
The ORM models in models.py mirror that migration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


class RunCancelled(Exception):
    """Raised at a stage boundary when cancellation was requested for
    the run (2B cooperative cancellation). jobs.fail_pipeline turns it
    into the terminal run status "cancelled"."""


ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def _now():
    return datetime.now(timezone.utc)


def _new_uuid():
    return str(uuid.uuid4())


def _session():
    """A fresh SessionLocal session (lazy import, like jobs)."""
    from database import SessionLocal

    return SessionLocal()


def script_sha256(script) -> str:
    """Full sha256 of the exact script text — the ledger/manifest
    identity. (jobs.script_hash_for is a 16-char truncated display
    hash and is deliberately NOT reused here.)"""
    return hashlib.sha256(str(script or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------
# Worker presence — identity, ownership, heartbeat (Stage 3, R2)
# ---------------------------------------------------------------------
# PIPE-005 / DATA-005: before this stage a run carried no record of
# WHICH worker owned it and no liveness signal at all — the only
# timestamps were started_at / finished_at, so a run whose worker
# died mid-production sat in "running" until the next worker process
# started, and that worker's startup reconciliation failed EVERY
# running run unconditionally, including runs a second, live worker
# was actively processing.
#
# Presence is recorded in the run row's existing free-form
# ``progress`` JSON (schema authority 0002 — no migration needed):
#
#     {"worker_id": ..., "claimed_at": ..., "heartbeat_at": ...}
#
# - worker_id: the claiming process's identity (current_worker_id).
# - claimed_at: when the atomic claim stamped ownership.
# - heartbeat_at: refreshed by touch_run — at stage boundaries and
#   by the heartbeat thread start_heartbeat installs for the run's
#   execution. reconcile_stale_runs(stale_after_seconds=...) treats
#   a running run as orphaned ONLY when this liveness evidence (or
#   its started_at fallback, for rows that predate presence) is
#   older than the threshold.

_WORKER_ID = None
_WORKER_ID_LOCK = threading.Lock()

# Defaults: a beat every 30s; a run is stale after 180s without one
# (six missed beats). Both are environment-tunable for tests and for
# deployments on slower storage.
def _env_float(name, default):
    """Parse a float env var, falling back to the default on ANY
    malformed value. C-15 review fix: the previous bare float(...)
    at import time meant one typo'd env value crashed the import of
    this module — and with it every process that touches the ledger
    (worker AND API) — with no record of why the process died."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    return value if value > 0 else float(default)


HEARTBEAT_INTERVAL_SECONDS = _env_float(
    "CINEMORA_RUN_HEARTBEAT_SECONDS", 30
)
STALE_AFTER_SECONDS = _env_float("CINEMORA_RUN_STALE_SECONDS", 180)


def current_worker_id() -> str:
    """This process's worker identity: hostname:pid:random.

    Generated lazily once per process, so every run a process
    claims carries the same owner id, and two processes — including
    two on one host — never share one.
    """
    global _WORKER_ID
    if _WORKER_ID is None:
        with _WORKER_ID_LOCK:
            if _WORKER_ID is None:
                _WORKER_ID = (
                    f"{socket.gethostname()}:{os.getpid()}:"
                    f"{uuid.uuid4().hex[:8]}"
                )
    return _WORKER_ID


def _presence_payload(worker_id=None):
    """The progress-JSON presence record stamped at claim/begin."""
    now = _now().isoformat()
    return {
        "worker_id": worker_id or current_worker_id(),
        "claimed_at": now,
        "heartbeat_at": now,
    }


def _parse_timestamp(value):
    """Parse an ISO timestamp / datetime into an aware datetime.

    Naive values are read as UTC (SQLite round-trips DateTime
    columns without tzinfo). Returns None for anything unparseable.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def run_presence(run) -> dict:
    """Liveness view of a run dict (as returned by get_run).

    Returns {"worker_id", "heartbeat_at", "age_seconds", "live"}:
    the recorded owner, the freshest liveness timestamp (the
    heartbeat when present, else started_at, else created_at —
    the fallback chain rows predating presence are judged by),
    its age in seconds at call time, and whether that evidence is
    fresher than STALE_AFTER_SECONDS. A run with NO liveness
    evidence at all reports age_seconds=None and live=False.
    """
    run = run or {}
    progress = run.get("progress") or {}
    worker_id = progress.get("worker_id")
    heartbeat = _parse_timestamp(progress.get("heartbeat_at"))
    if heartbeat is None:
        heartbeat = _parse_timestamp(run.get("started_at"))
    if heartbeat is None:
        heartbeat = _parse_timestamp(run.get("created_at"))
    if heartbeat is None:
        return {
            "worker_id": worker_id,
            "heartbeat_at": None,
            "age_seconds": None,
            "live": False,
        }
    age = max(0.0, (_now() - heartbeat).total_seconds())
    return {
        "worker_id": worker_id,
        "heartbeat_at": heartbeat.isoformat(),
        "age_seconds": age,
        "live": age <= STALE_AFTER_SECONDS,
    }


# ---------------------------------------------------------------------
# Run lifecycle — creation / finishing (2A)
# ---------------------------------------------------------------------


def _insert_run(project_id, script_text, options, status):
    """Insert a production_runs row; returns the row as a dict, or
    None when the database is unavailable (fail-soft)."""
    try:
        from models import ProductionRun, Project

        run_id = _new_uuid()
        now = _now()
        digest = script_sha256(script_text)
        # Phase 8.1: the run carries a denormalised copy of its
        # project's owner (design 02 §4.1) — this insert is the one
        # choke point every run creation funnels through (full
        # pipeline, analysis phase, revision renders), so ownership
        # is stamped here and nowhere else. Fail-soft like the rest
        # of the module: an unresolvable project leaves owner None
        # rather than blocking the run.
        owner_id = None
        try:
            owner_db = _session()
            try:
                project_row = owner_db.get(Project, str(project_id))
                if project_row is not None:
                    owner_id = project_row.owner_id
            finally:
                owner_db.close()
        except Exception:
            owner_id = None
        row = ProductionRun(
            id=run_id,
            project_id=str(project_id),
            owner_id=owner_id,
            status=status,
            stage=None,
            # Stage 3 (R2): a run inserted directly in "running"
            # (begin_run) is owned by this process from birth —
            # stamp its presence so liveness tooling never has to
            # guess. Queued runs are stamped at claim time instead.
            progress=_presence_payload() if status == "running" else None,
            options=options or {},
            script_hash=digest,
            manifest_path=None,
            error=None,
            execution=None,
            cancel_requested=False,
            created_at=now,
            started_at=now if status == "running" else None,
            finished_at=None,
        )
        db = _session()
        try:
            db.add(row)
            db.commit()
        finally:
            db.close()
        # Built from locals: the ORM instance is expired/detached
        # once the session closes and must not be touched again.
        return {
            "run_id": run_id,
            "project_id": str(project_id),
            "status": status,
            "script_hash": digest,
            "deduplicated": False,
        }
    except Exception as error:
        print(f"WARNING: run ledger insert failed: {error}")
        return None


def begin_run(project_id, script_text, options=None):
    """Create a run row in status "running" and return its run_id
    (None when the ledger is unavailable — the run proceeds anyway)."""
    created = _insert_run(project_id, script_text, options, "running")
    return created["run_id"] if created else None


def finish_run(run_id, status, error=None, execution=None,
               manifest_path=None, stage=None):
    """Move a run to a terminal status. Returns True on success.

    B-02 (Phase 5a): the finish is a CONDITIONAL update guarded
    on the run still being non-terminal, in the same transaction
    — the FIRST finisher wins. Previously the row was loaded and
    overwritten unconditionally, so a stale finisher (a retried
    or duplicated delivery finishing late, after another delivery
    already completed the run) could downgrade a "completed" run
    to "failed" or resurrect a failed run as completed, and the
    claim's recorded status was lost. A second finish now returns
    False and changes nothing."""
    if not run_id:
        return False
    try:
        from models import ProductionRun
        from sqlalchemy import update

        values = {"status": status, "finished_at": _now()}
        if stage is not None:
            values["stage"] = stage
        if error is not None:
            values["error"] = str(error)
        if execution is not None:
            values["execution"] = execution
        if manifest_path is not None:
            values["manifest_path"] = str(manifest_path)
        db = _session()
        try:
            result = db.execute(
                update(ProductionRun)
                .where(
                    ProductionRun.id == str(run_id),
                    ProductionRun.status.notin_(TERMINAL_STATUSES),
                )
                .values(**values)
            )
            db.commit()
            if result.rowcount != 1:
                return False
        finally:
            db.close()
        # Developer API webhooks (Phase N): fan the terminal status
        # out to the project owner's subscribed endpoints. The
        # service is fail-soft and delivers on its own thread; the
        # extra guard here keeps even an import failure from ever
        # disturbing a production finish.
        try:
            from services import webhooks

            webhooks.notify_run_finished(str(run_id), status)
        except Exception:
            pass
        return True
    except Exception as ledger_error:
        print(f"WARNING: run ledger finish failed: {ledger_error}")
        return False


# B-02 (Phase 5a): the lifecycle transitions set_run_status may
# perform, as {current_status: (allowed targets)}. Monotonic in
# the claim sense: nothing ever moves a run BACK to "queued"
# once it has been claimed ("running"), and nothing moves a run
# out of a terminal status (finish_run owns terminal writes and
# guards them itself). Before this map existed, set_run_status
# moved ANY non-terminal run to ANY status — a stale queued-era
# write could downgrade a claimed, executing run back to
# "queued", where claim_next_run would hand it to a second
# worker for a duplicate execution, and the claim's recorded
# status was effectively lost.
_STATUS_TRANSITIONS = {
    "queued": ("awaiting_approval", "running", "cancelled"),
    "awaiting_approval": ("queued", "running", "cancelled"),
    "running": ("awaiting_approval", "cancelled"),
}


def set_run_status(run_id, status, stage=None):
    """Move a non-terminal run to a lifecycle status (Stage 4).

    Used by the plan-approval gate: an analysis-phase run is parked
    in "awaiting_approval" until the studio releases it. Terminal
    runs are never moved by this helper (finish_run owns those
    transitions), and moves are MONOTONIC (B-02): the conditional
    UPDATE only fires from a current status that may legitimately
    precede the target (see _STATUS_TRANSITIONS), so a stale
    write can never downgrade a claimed/running run. Fail-soft
    like everything in this module.
    """
    if not run_id or not status:
        return False
    target = str(status)
    if target in TERMINAL_STATUSES:
        return False
    allowed_from = tuple(
        current
        for current, targets in _STATUS_TRANSITIONS.items()
        if target in targets
    )
    if not allowed_from:
        return False
    try:
        from models import ProductionRun
        from sqlalchemy import update

        values = {"status": target}
        if stage is not None:
            values["stage"] = str(stage)
        db = _session()
        try:
            result = db.execute(
                update(ProductionRun)
                .where(
                    ProductionRun.id == str(run_id),
                    ProductionRun.status.in_(allowed_from),
                )
                .values(**values)
            )
            db.commit()
            return result.rowcount == 1
        finally:
            db.close()
    except Exception as error:
        print(f"WARNING: run status update failed: {error}")
        return False


def latest_run_for(project_id, script_hash=None):
    """The most recent run dict for a project (optionally restricted
    to one script hash), or None. Read-only; fail-soft."""
    try:
        from models import ProductionRun

        db = _session()
        try:
            query = db.query(ProductionRun).filter(
                ProductionRun.project_id == str(project_id)
            )
            if script_hash:
                query = query.filter(
                    ProductionRun.script_hash == str(script_hash)
                )
            row = query.order_by(ProductionRun.created_at.desc()).first()
            if row is None:
                return None
            return get_run(row.id)
        finally:
            db.close()
    except Exception:
        return None


def latest_production_run_for(project_id, script_hash=None):
    """The most recent PRODUCTION run dict for a project, or None.

    Revision renders are ProductionRuns too (options.kind =
    "revision_render", Stage 6), so latest_run_for returns a render
    run once any revision has been rendered (S7-D2). Surfaces that
    describe the production itself — the provenance certificate and
    the asset ledger listing — must select through here so a render
    can never displace the production they describe. Read-only;
    fail-soft, like latest_run_for."""
    try:
        from models import ProductionRun

        db = _session()
        try:
            query = db.query(ProductionRun).filter(
                ProductionRun.project_id == str(project_id)
            )
            if script_hash:
                query = query.filter(
                    ProductionRun.script_hash == str(script_hash)
                )
            rows = query.order_by(ProductionRun.created_at.desc()).all()
            for row in rows:
                options = row.options or {}
                if options.get("kind") == "revision_render":
                    continue
                return get_run(row.id)
            return None
        finally:
            db.close()
    except Exception:
        return None


def list_runs_for(project_id):
    """All run dicts for a project, newest first (fail-soft: [])."""
    try:
        from models import ProductionRun

        db = _session()
        try:
            rows = (
                db.query(ProductionRun)
                .filter(ProductionRun.project_id == str(project_id))
                .order_by(ProductionRun.created_at.desc())
                .all()
            )
            ids = [row.id for row in rows]
        finally:
            db.close()
        return [run for run in (get_run(i) for i in ids) if run]
    except Exception:
        return []


def mark_dispatched(run_id, job_id):
    """Stamp a queued run as promoted by the dispatcher (Phase 8.4):
    records the RQ job id + dispatch time in the run's options so
    the backlog query can tell promoted runs from waiting ones.
    Fail-soft like everything in this module."""
    if not run_id:
        return False
    try:
        from models import ProductionRun

        db = _session()
        try:
            row = db.get(ProductionRun, str(run_id))
            if row is None or row.status != "queued":
                return False
            options = dict(row.options or {})
            options["dispatched_job_id"] = str(job_id) if job_id else None
            options["dispatched_at"] = _now().isoformat()
            row.options = options
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        return False


def flag_stuck(run_id, stage, budget_seconds):
    """Flag a running run as suspected-stuck in its progress record
    (Phase 8.4 stage watchdog, design 03 §4.1). Flagged, never
    killed: a slow render and a hung render look identical from
    outside. Idempotent per stage. Fail-soft."""
    if not run_id:
        return False
    try:
        from models import ProductionRun

        db = _session()
        try:
            row = db.get(ProductionRun, str(run_id))
            if row is None or row.status != "running":
                return False
            progress = dict(row.progress or {})
            existing = progress.get("stuck_suspected") or {}
            if existing.get("stage") == stage:
                return False
            progress["stuck_suspected"] = {
                "stage": stage,
                "budget_seconds": budget_seconds,
                "flagged_at": _now().isoformat(),
            }
            row.progress = progress
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        return False


def set_run_stage(run_id, stage):
    """Record the run's current stage (observability only)."""
    if not run_id:
        return False
    try:
        from models import ProductionRun

        db = _session()
        try:
            row = db.get(ProductionRun, str(run_id))
            if row is None or row.status in TERMINAL_STATUSES:
                return False
            row.stage = str(stage)
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        return False


def get_run(run_id):
    """The run row as a plain dict, or None."""
    if not run_id:
        return None
    try:
        from models import ProductionRun

        db = _session()
        try:
            row = db.get(ProductionRun, str(run_id))
            if row is None:
                return None
            return {
                "run_id": row.id,
                "project_id": row.project_id,
                "owner_id": row.owner_id,
                "status": row.status,
                "stage": row.stage,
                "progress": row.progress,
                "options": row.options,
                "script_hash": row.script_hash,
                "manifest_path": row.manifest_path,
                "error": row.error,
                "execution": row.execution,
                "cancel_requested": bool(row.cancel_requested),
                "created_at": row.created_at,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
            }
        finally:
            db.close()
    except Exception:
        return None


# ---------------------------------------------------------------------
# Job lifecycle — dedup / claim / cancel / reconcile (2B)
# ---------------------------------------------------------------------


# Statuses that make a submission a duplicate of an existing run.
# "awaiting_approval" is included beyond ACTIVE_STATUSES (Stage 3,
# R2 / INT-103): a plan-gated run parked at the approval gate is
# still THE active run for its project+script — the v1 route has
# always guarded this itself (api_v1 checks all three statuses
# before submitting), but a direct ledger caller could mint a
# second run for a parked script and the two would collide on the
# project's artefacts. The ledger now enforces what the route
# assumed. ACTIVE_STATUSES itself is unchanged: other callers
# (revision-render dedup) rely on its exact membership.
DEDUP_STATUSES = ACTIVE_STATUSES + ("awaiting_approval",)


class _SubmissionMutex:
    """Cross-process mutex for submission check-then-insert (R2).

    The dedup contract is "check for an active run, insert if none"
    — two steps that race when two processes submit the same
    project+script concurrently (DATA-005): both checks pass, both
    insert, and two runs of one production exist. The atomic claim
    cannot help there (the runs are different rows). Redis is the
    one store every submitting process already shares (it carries
    the queue itself), so the mutex is a Redis lock keyed by
    project+script.

    Fail-soft, like everything in this module: when Redis is
    unreachable the submission proceeds WITHOUT the lock (with a
    printed warning) — a Redis outage already fails the enqueue
    that follows and marks the run failed; it must not also wedge
    submission itself.
    """

    def __init__(self, key):
        self._key = key
        self._lock = None

    def __enter__(self):
        try:
            from redis import Redis

            connection = Redis.from_url(
                os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            lock = connection.lock(
                self._key, timeout=30, blocking_timeout=10
            )
            if lock.acquire():
                self._lock = lock
            else:
                print(
                    "WARNING: submission mutex not acquired for "
                    f"{self._key}; proceeding without it"
                )
        except Exception as error:
            print(
                "WARNING: submission mutex unavailable "
                f"({error}); proceeding without it"
            )
        return self

    def __exit__(self, *exc):
        if self._lock is not None:
            try:
                self._lock.release()
            except Exception:
                pass
            self._lock = None
        return False


def submit_production(project_id, script_text, options=None):
    """Submit a production, deduplicated (AUD-07).

    If an ACTIVE run (queued, running, or parked in
    awaiting_approval — DEDUP_STATUSES) already exists for the same
    project + same full script hash, that run is returned with
    deduplicated=True and NO new row is created — a double submission
    is one logical run. Otherwise a new "queued" run row is created.
    The check and the insert run under a per-project+script Redis
    mutex (Stage 3, R2) so concurrent submitters cannot both pass
    the check. Returns the run dict, or None when the ledger is
    unavailable.
    """
    digest = script_sha256(script_text)
    with _SubmissionMutex(
        f"cinemora:submit:{project_id}:{digest}"
    ):
        try:
            from models import ProductionRun

            db = _session()
            try:
                existing = (
                    db.query(ProductionRun)
                    .filter(
                        ProductionRun.project_id == str(project_id),
                        ProductionRun.script_hash == digest,
                        ProductionRun.status.in_(DEDUP_STATUSES),
                    )
                    .order_by(ProductionRun.created_at.asc())
                    .first()
                )
                if existing is not None:
                    return {
                        "run_id": existing.id,
                        "project_id": existing.project_id,
                        "status": existing.status,
                        "script_hash": existing.script_hash,
                        "deduplicated": True,
                    }
            finally:
                db.close()
        except Exception as error:
            print(f"WARNING: run ledger dedup check failed: {error}")
            return None
        return _insert_run(project_id, script_text, options, "queued")


def _claim_row(row_id, from_statuses=("queued",), worker_id=None):
    """Atomically move one run to running (conditional UPDATE
    guarded on the expected status — exactly one claimant can win).
    The default claim source is 'queued'; the Stage 4 plan-approval
    release claims from 'awaiting_approval' instead.

    Stage 3 (R2): the SAME atomic UPDATE also stamps the winner's
    presence (progress JSON: worker_id / claimed_at / heartbeat_at),
    so ownership is recorded by the very write that grants it — a
    run can never be running without a recorded owner, and a losing
    claimant can never overwrite the winner's stamp."""
    from models import ProductionRun
    from sqlalchemy import update

    db = _session()
    try:
        result = db.execute(
            update(ProductionRun)
            .where(
                ProductionRun.id == row_id,
                ProductionRun.status.in_(tuple(from_statuses)),
            )
            .values(
                status="running",
                started_at=_now(),
                progress=_presence_payload(worker_id),
            )
        )
        db.commit()
        return result.rowcount == 1
    finally:
        db.close()


def claim_run(run_id, from_statuses=("queued",), worker_id=None):
    """Claim a specific queued run (used when an enqueue carries its
    run_id). Returns the run dict when this caller won the claim,
    else None (already claimed / cancelled / finished / unknown).
    The winner's worker identity (default: this process's) is
    recorded on the run by the claim itself."""
    if not run_id:
        return None
    # SQLite writer-lock contention under concurrent production is
    # bursty (two pipelines + dispatcher + reconciler + API all
    # writing one file): the original 3x50ms retry starved real
    # claims — a retry child executed UNCLAIMED (no heartbeat, no
    # ledger presence) when its claim hit a ~1s lock burst
    # (Phase 8.6 battery find, 2026-11-23). Back off properly:
    # ~3.3s of grace before treating the claim as lost.
    delays = (0.05, 0.15, 0.4, 0.9, 1.8)
    for attempt in range(len(delays) + 1):
        try:
            if _claim_row(
                str(run_id),
                from_statuses=from_statuses,
                worker_id=worker_id,
            ):
                return get_run(run_id)
            return None
        except Exception as error:
            if "locked" in str(error).lower() and attempt < len(delays):
                time.sleep(delays[attempt])
                continue
            print(f"WARNING: run claim failed: {error}")
            return None
    return None


def _touch_run(run_id, worker_id=None):
    """Heartbeat write. Returns "ok" (beat recorded), "done" (the
    run is no longer running, or is owned by a DIFFERENT worker —
    this caller must stop beating for it), or "error" (transient
    ledger problem; the caller should beat again next interval).

    Ownership is enforced here, not just recorded: a heartbeat from
    a worker that does not own the run is refused, so a second
    worker can never keep another worker's run alive — or claim it
    by beating for it.
    """
    if not run_id:
        return "done"
    try:
        from models import ProductionRun

        db = _session()
        try:
            row = db.get(ProductionRun, str(run_id))
            if row is None or row.status != "running":
                return "done"
            progress = dict(row.progress or {})
            owner = progress.get("worker_id")
            caller = worker_id or current_worker_id()
            if owner is not None and owner != caller:
                return "done"
            progress["worker_id"] = caller
            if not progress.get("claimed_at"):
                progress["claimed_at"] = _now().isoformat()
            progress["heartbeat_at"] = _now().isoformat()
            row.progress = progress
            db.commit()
            return "ok"
        finally:
            db.close()
    except Exception as error:
        print(f"WARNING: run heartbeat failed: {error}")
        return "error"


def touch_run(run_id, worker_id=None) -> bool:
    """Record a heartbeat for a running run owned by this worker.

    Returns True when the beat was recorded. False means the run is
    finished/cancelled, owned by another worker, unknown, or the
    ledger is unavailable — callers treat False as "stop beating",
    never as an error worth failing a production over (fail-soft).
    """
    return _touch_run(run_id, worker_id=worker_id) == "ok"


def start_heartbeat(run_id, interval_seconds=None):
    """Start the daemon heartbeat thread for one run's execution.

    Beats every interval (default HEARTBEAT_INTERVAL_SECONDS) until
    the run leaves "running" or another worker owns it. Transient
    ledger errors do NOT stop the thread — a beat is retried at the
    next interval, because a heartbeat that dies on the first DB
    hiccup would leave a live run looking orphaned. The thread is a
    daemon: it never blocks process exit. Returns the thread, or
    None when no run_id was given.
    """
    if not run_id:
        return None
    interval = (
        HEARTBEAT_INTERVAL_SECONDS
        if interval_seconds is None
        else float(interval_seconds)
    )

    def _beat():
        while True:
            time.sleep(interval)
            try:
                outcome = _touch_run(run_id)
            except Exception:
                outcome = "error"
            if outcome == "done":
                return

    thread = threading.Thread(
        target=_beat,
        name=f"run-heartbeat-{run_id}",
        daemon=True,
    )
    thread.start()
    return thread


def claim_next_run():
    """Claim the OLDEST queued run (FIFO — no LIFO starvation) and
    return it, or None when the queue is empty / all claims lost."""
    try:
        from models import ProductionRun

        db = _session()
        try:
            candidates = (
                db.query(ProductionRun.id)
                .filter(ProductionRun.status == "queued")
                .order_by(ProductionRun.created_at.asc())
                .limit(10)
                .all()
            )
        finally:
            db.close()
    except Exception as error:
        print(f"WARNING: run ledger claim scan failed: {error}")
        return None
    for (candidate_id,) in candidates:
        claimed = claim_run(candidate_id)
        if claimed is not None:
            return claimed
    return None


def request_cancel(run_id):
    """Request cooperative cancellation. A queued run is cancelled
    immediately (it will never be claimed); a running run gets the
    cancel_requested flag, which jobs checks at stage boundaries.
    Returns True when a run was updated."""
    if not run_id:
        return False
    try:
        from models import ProductionRun

        db = _session()
        try:
            row = db.get(ProductionRun, str(run_id))
            if row is None or row.status in TERMINAL_STATUSES:
                return False
            row.cancel_requested = True
            if row.status == "queued":
                row.status = "cancelled"
                row.error = "cancelled"
                row.finished_at = _now()
            db.commit()
            return True
        finally:
            db.close()
    except Exception as error:
        print(f"WARNING: run cancel request failed: {error}")
        return False


def is_cancel_requested(run_id):
    """Fail-soft cancellation check used at stage boundaries: any
    ledger problem reads as "not cancelled" so it can never wedge a
    production."""
    run = get_run(run_id)
    if not run:
        return False
    return bool(run["cancel_requested"]) or run["status"] == "cancelled"


def reconcile_stale_runs(stale_after_seconds=None):
    """Mark orphaned "running" runs failed with error "interrupted"
    (AUD-08 recovery). Returns the count reconciled (0 when the
    ledger is unavailable).

    Two modes, deliberately distinct (Stage 3, R2 — PIPE-005 /
    DATA-005):

    - stale_after_seconds=None — TOTAL reconciliation, the original
      AUD-08 semantics: EVERY running run is failed. This is only
      sound for a caller that has PROVEN no other worker is live —
      worker.py's startup, which runs it only after acquiring the
      worker singleton lock: holding that lock is the proof that
      any running row is an orphan of a dead predecessor.

    - stale_after_seconds=N — HEARTBEAT-AWARE reconciliation, the
      only mode a caller without that proof may use (the worker's
      periodic reconciler): a running run is failed ONLY when its
      liveness evidence — heartbeat_at, falling back to started_at
      / created_at for rows predating presence — is older than N
      seconds. A run with a live heartbeat is NEVER touched, so a
      live worker's in-flight production cannot be failed out from
      under it by another process's housekeeping.
    """
    try:
        from models import ProductionRun
        from sqlalchemy import update

        db = _session()
        try:
            if stale_after_seconds is None:
                result = db.execute(
                    update(ProductionRun)
                    .where(ProductionRun.status == "running")
                    .values(
                        status="failed",
                        error="interrupted",
                        finished_at=_now(),
                    )
                )
                db.commit()
                return int(result.rowcount or 0)

            rows = (
                db.query(ProductionRun)
                .filter(ProductionRun.status == "running")
                .all()
            )
            stale_ids = []
            stale_details = []
            for row in rows:
                presence = run_presence(
                    {
                        "progress": row.progress,
                        "started_at": row.started_at,
                        "created_at": row.created_at,
                    }
                )
                age = presence["age_seconds"]
                if age is None or age > float(stale_after_seconds):
                    stale_ids.append(row.id)
                    # C-15: keep the death circumstances for the
                    # forensic record written after the update —
                    # which worker owned the run and when its heart
                    # last beat.
                    stale_details.append({
                        "run_id": row.id,
                        "project_id": row.project_id,
                        "worker_id": presence["worker_id"],
                        "last_heartbeat_at": presence["heartbeat_at"],
                        "heartbeat_age_seconds": age,
                        "stage": row.stage,
                    })
            if not stale_ids:
                return 0
            result = db.execute(
                update(ProductionRun)
                .where(
                    ProductionRun.id.in_(stale_ids),
                    # Re-guard on the status inside the UPDATE: a run
                    # that finished between the scan and the write
                    # must not be overwritten.
                    ProductionRun.status == "running",
                )
                .values(
                    status="failed",
                    error="interrupted",
                    finished_at=_now(),
                )
            )
            db.commit()
            reconciled_count = int(result.rowcount or 0)
            if reconciled_count:
                # C-15 forensics: the NEXT worker death must be
                # diagnosable from records. This event is the
                # durable evidence that these runs' heartbeats were
                # lost — what died (run + owning worker), when its
                # heart last beat, and how stale it was. Fail-soft
                # (record_event never raises).
                from services import worker_events

                worker_events.record_event(
                    "heartbeat_loss",
                    stale_after_seconds=float(stale_after_seconds),
                    reconciled_count=reconciled_count,
                    runs=stale_details,
                )
            return reconciled_count
        finally:
            db.close()
    except Exception as error:
        print(f"WARNING: stale-run reconciliation failed: {error}")
        return 0


# ---------------------------------------------------------------------
# Asset decisions (2A)
# ---------------------------------------------------------------------


def _file_sha256(path, cache):
    """sha256 of a local media file (streamed), or None. Cached per
    record_decisions call — the same file serves many decisions."""
    if not path:
        return None
    key = str(path)
    if key in cache:
        return cache[key]
    digest = None
    try:
        candidate = Path(key)
        if candidate.is_file():
            hasher = hashlib.sha256()
            with open(candidate, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
    except Exception:
        digest = None
    cache[key] = digest
    return digest


def _asset_rights(asset):
    """The rights classification dict for an asset, mirroring
    build_rights_manifest exactly ("unknown" stays "unknown").

    Phase 1 / AM-014: the asset's recorded title rides in the same
    JSON dict — the ledger previously dropped asset titles
    entirely, so the certificate built from it could not name the
    assets it certifies."""
    return {
        "license": asset.get("license") or "unknown",
        "license_url": asset.get("license_url") or "",
        "rights_status": asset.get("rights_status")
        or "verify_before_publish",
        "creator": asset.get("creator") or asset.get("channel") or "",
        "source": asset.get("source") or "",
        "retrieved_at": asset.get("retrieved_at") or "",
        "title": asset.get("title") or "",
    }


def _asset_verification(asset):
    verdict = asset.get("verification")
    if isinstance(verdict, dict):
        return verdict
    status = asset.get("verification_status")
    if status:
        return {"status": status}
    return None


def _verification_with_segment(asset):
    """The asset's verification record for the ledger, with the
    Stage 7 / R8 segment identity stamped into it (review C.D.3:
    segment_id is stamped on the candidate, the asset, the timeline
    item AND the ledger row). The verdict dict is COPIED before
    stamping — the asset's verdict object is shared with the
    pipeline and is never mutated by recording.

    B-roll B0 (§6.1): the asset's tier rides in the same JSON copy,
    so the certificate assembled from the ledger can state it.

    Phase 1 / AM-013: a swap override stamp (a rejected fresh
    verdict applied anyway, with its recorded reason) rides in the
    same copy, so the certificate can surface the override."""
    verification = _asset_verification(asset)
    if not isinstance(verification, dict):
        return verification
    segment_id = asset.get("segment_id") if isinstance(asset, dict) else None
    tier = asset.get("tier") if isinstance(asset, dict) else None
    swap_override = (
        asset.get("swap_override") if isinstance(asset, dict) else None
    )
    if segment_id:
        verification = {**verification, "segment_id": segment_id}
    if tier:
        verification = {**verification, "tier": tier}
    if isinstance(swap_override, dict) and swap_override:
        verification = {**verification, "swap_override": swap_override}
    return verification


def _timeline_items(timeline):
    if not isinstance(timeline, dict):
        return []
    items = timeline.get("items")
    if items:
        return [i for i in items if isinstance(i, dict)]
    collected = []
    for scene in timeline.get("scenes") or []:
        if isinstance(scene, dict):
            collected.extend(
                i for i in (scene.get("items") or []) if isinstance(i, dict)
            )
    return collected


def _foreign_to_run(asset, project_id, own_run_stamps):
    """Plan §3.3 item 1 (test 18, ledger leg): True when an asset in
    a run's scene aggregates carries RECORDED provenance that
    positively mismatches the run being recorded — a project stamp
    naming a different project, or a run stamp absent from the run
    identities this run's own sentence assignments carry
    (own_run_stamps). Such an asset was produced by another project
    or production run; its acceptance does not transfer, so it must
    not be recorded as this run's decision.

    Positive-mismatch rule: a MISSING stamp is not a mismatch.
    Pipeline-produced assets and legacy/stored records carry no
    stamps and are recorded exactly as before; when no sentence
    assignment carries a run stamp, no run identity is established
    and no asset is excluded on run grounds."""
    if not isinstance(asset, dict):
        return False
    project_stamp = asset.get("project_id")
    if project_stamp is not None and str(project_stamp).strip():
        if str(project_stamp).strip() != str(project_id):
            return True
    run_stamp = asset.get("run_id")
    if run_stamp is not None and str(run_stamp).strip():
        if own_run_stamps and str(run_stamp).strip() not in own_run_stamps:
            return True
    return False


def record_decisions(run_id, project_id, asset_report, timeline,
                     processed_scenes=None):
    """Translate a built asset report (+ timeline) into ledger rows.

    Sources, in order:
    - accepted assets (deduped by file path exactly as
      build_asset_report computes its accepted set) -> selected
      (video) / still (image) / generated (graphic or card); an asset
      carrying swapped_from -> swapped_in, preceded by a swapped_out
      row for the replaced asset, linked via previous_decision_id.
      When processed_scenes is not supplied, the accepted set is
      derived from the timeline items + the report's
      segment_retrieval instead — provider/evidence only; rights and
      verification are then recorded as None rather than fabricated.
    - timeline items with assignment == "rotation" -> rotated.
    - report missing_visuals -> missing.
    - report candidate_records with outcome == "rejected" -> rejected.
    - report candidate_records with outcome == "verified" whose
      candidate never reached the film and never failed a download
      -> verified_not_picked (Phase 1 / AM-015).
    - report candidate_records with outcome == "download_failed"
      -> download_failed (Phase 1 / AM-015).

    Idempotent per run: a run that already has decisions is skipped
    (the ledger is append-only; re-recording would duplicate history).
    Returns the number of rows written, or None when the ledger is
    unavailable / inputs are unusable (fail-soft).
    """
    if not run_id or not isinstance(asset_report, dict):
        return None
    try:
        from models import AssetDecision
        from services.asset_report import _is_card, _is_graphic

        db = _session()
        try:
            already = (
                db.query(AssetDecision)
                .filter(AssetDecision.run_id == str(run_id))
                .first()
            )
            if already is not None:
                return 0

            hash_cache: dict = {}
            rows: list[dict] = []

            # ---- accepted assets ----------------------------------
            accepted: list[tuple[dict, object, list]] = []
            if processed_scenes is not None:
                by_path: dict[str, tuple[dict, object, list]] = {}
                # Plan §3.3 item 1 (test 18, ledger leg): the run
                # identities this run's OWN sentence assignments
                # carry, collected across every scene before any
                # aggregate is read — the reference the aggregate
                # guard (_foreign_to_run) judges foreign run stamps
                # against. Empty when no assignment is stamped (the
                # normal pipeline shape), which disables the guard.
                own_run_stamps: set = set()
                for _scene in processed_scenes or []:
                    if not isinstance(_scene, dict):
                        continue
                    _sentences = _scene.get("sentences")
                    if not _sentences:
                        _sentences = (
                            (_scene.get("scene") or {}).get("sentences")
                            or []
                        )
                    for _sentence in _sentences:
                        if not isinstance(_sentence, dict):
                            continue
                        for _asset in (
                            list(_sentence.get("videos") or [])
                            + list(_sentence.get("images") or [])
                        ):
                            if isinstance(_asset, dict):
                                _stamp = _asset.get("run_id")
                                if _stamp is not None and str(_stamp).strip():
                                    own_run_stamps.add(str(_stamp).strip())
                for scene in processed_scenes or []:
                    if not isinstance(scene, dict):
                        continue
                    scene_number = scene.get("scene_number")
                    if scene_number is None:
                        scene_number = (
                            (scene.get("scene") or {}).get("scene_number")
                        )
                    # Sentence records live at the processed-scene
                    # level in current results; older shapes nest them
                    # under scene["scene"]. Accept either.
                    scene_sentences = scene.get("sentences")
                    if not scene_sentences:
                        scene_sentences = (
                            (scene.get("scene") or {}).get("sentences")
                            or []
                        )
                    sentences_by_path: dict[str, list] = {}
                    for sentence in scene_sentences:
                        if not isinstance(sentence, dict):
                            continue
                        number = sentence.get("sentence_number")
                        for asset in (
                            list(sentence.get("videos") or [])
                            + list(sentence.get("images") or [])
                        ):
                            if not isinstance(asset, dict):
                                continue
                            path = str(
                                asset.get("file_path")
                                or asset.get("path")
                                or ""
                            )
                            if path and number is not None:
                                bucket = sentences_by_path.setdefault(
                                    path, []
                                )
                                if number not in bucket:
                                    bucket.append(number)
                    for asset in (
                        list(scene.get("videos") or [])
                        + list(scene.get("images") or [])
                    ):
                        if not isinstance(asset, dict):
                            continue
                        if _foreign_to_run(asset, project_id, own_run_stamps):
                            continue
                        path = str(
                            asset.get("file_path")
                            or asset.get("path")
                            or ""
                        )
                        if path and path not in by_path:
                            by_path[path] = (
                                asset,
                                scene_number,
                                sentences_by_path.get(path, []),
                            )
                accepted = list(by_path.values())
            else:
                seen: dict[str, dict] = {}
                for item in _timeline_items(timeline):
                    path = str(
                        item.get("file_path") or item.get("path") or ""
                    )
                    if path and path not in seen:
                        seen[path] = item
                for path, item in seen.items():
                    pseudo_asset = {
                        "file_path": path,
                        "type": item.get("type"),
                        "source": item.get("source"),
                        "source_url": item.get("source_url"),
                        "video_url": item.get("video_url"),
                        "image_url": item.get("image_url"),
                        "segment_evidence": item.get("segment_evidence"),
                        # Stage 7 / R8 reconstruction consistency:
                        # the item's SOURCE clip offset is its
                        # clip_start; the old reconstruction used
                        # the item's TIMELINE position (start),
                        # which misrecords the interval whenever the
                        # item carries a real clip offset.
                        "clip_start": (
                            item.get("clip_start")
                            if item.get("clip_start") is not None
                            else item.get("start")
                        ),
                        "segment_id": item.get("segment_id"),
                        "duration_seconds": item.get("duration"),
                        "motion_graphic": item.get("assignment")
                        == "motion_graphic",
                    }
                    accepted.append(
                        (pseudo_asset, item.get("scene_number"), [])
                    )

            for asset, scene_number, sentence_numbers in accepted:
                path = str(asset.get("file_path") or asset.get("path") or "")
                content_hash = _file_sha256(path, hash_cache)
                asset_key = (
                    asset.get("candidate_id") or content_hash or path or None
                )
                common = {
                    "run_id": str(run_id),
                    "project_id": str(project_id),
                    "asset_key": asset_key,
                    "content_hash": content_hash,
                    "source_url": (
                        asset.get("source_url")
                        or asset.get("video_url")
                        or asset.get("image_url")
                        or asset.get("url")
                        or None
                    ),
                    "provider": str(
                        asset.get("provider")
                        or asset.get("source")
                        or "unknown"
                    ),
                    "scene_number": scene_number,
                    "sentence_number": (
                        sentence_numbers[0] if sentence_numbers else None
                    ),
                    "interval_start": asset.get("clip_start"),
                    "interval_duration": asset.get("duration_seconds"),
                    "evidence_class": asset.get("segment_evidence"),
                    "verification": _verification_with_segment(asset),
                    "rights": (
                        _asset_rights(asset)
                        if processed_scenes is not None
                        else None
                    ),
                }
                swapped_from = asset.get("swapped_from")
                if isinstance(swapped_from, dict) and swapped_from:
                    original_path = str(
                        swapped_from.get("file_path") or ""
                    )
                    rows.append({
                        **common,
                        "asset_key": (
                            swapped_from.get("candidate_id")
                            or _file_sha256(original_path, hash_cache)
                            or original_path
                            or None
                        ),
                        "content_hash": _file_sha256(
                            original_path, hash_cache
                        ),
                        # The replaced asset's own verdict/rights are
                        # NOT carried onto this row — only its identity
                        # comes from swapped_from; its evidence lives
                        # in its own original decision rows.
                        "verification": None,
                        "rights": None,
                        "evidence_class": None,
                        "decision_type": "swapped_out",
                        "previous_decision_id": None,
                        "_link_to_next": True,
                    })
                    rows.append({
                        **common,
                        "decision_type": "swapped_in",
                        "previous_decision_id": None,
                    })
                elif _is_card(asset) or _is_graphic(asset):
                    rows.append({
                        **common,
                        "decision_type": "generated",
                        "previous_decision_id": None,
                    })
                elif (asset.get("type") or asset.get("visual_type")) == "image":
                    rows.append({
                        **common,
                        "decision_type": "still",
                        "previous_decision_id": None,
                    })
                else:
                    rows.append({
                        **common,
                        "decision_type": "selected",
                        "previous_decision_id": None,
                    })

            # ---- rotation assignments ------------------------------
            sentence_number_by_position: dict = {}
            if processed_scenes is not None:
                for scene in processed_scenes or []:
                    if not isinstance(scene, dict):
                        continue
                    scene_dict = scene.get("scene") or {}
                    scene_number = (
                        scene.get("scene_number")
                        or scene_dict.get("scene_number")
                    )
                    position_sentences = scene.get("sentences")
                    if not position_sentences:
                        position_sentences = (
                            scene_dict.get("sentences") or []
                        )
                    for index, sentence in enumerate(
                        position_sentences
                    ):
                        if isinstance(sentence, dict):
                            sentence_number_by_position[
                                (scene_number, index)
                            ] = sentence.get("sentence_number")
            for item in _timeline_items(timeline):
                if item.get("assignment") != "rotation":
                    continue
                path = str(item.get("file_path") or item.get("path") or "")
                rows.append({
                    "run_id": str(run_id),
                    "project_id": str(project_id),
                    "asset_key": _file_sha256(path, hash_cache) or path or None,
                    "content_hash": _file_sha256(path, hash_cache),
                    "source_url": item.get("source_url") or None,
                    "provider": str(item.get("source") or "unknown"),
                    "scene_number": item.get("scene_number"),
                    "sentence_number": sentence_number_by_position.get(
                        (item.get("scene_number"), item.get("sentence_index"))
                    ),
                    "interval_start": item.get("start"),
                    "interval_duration": item.get("duration"),
                    "evidence_class": item.get("segment_evidence"),
                    "verification": None,
                    "rights": None,
                    "decision_type": "rotated",
                    "previous_decision_id": None,
                })

            # ---- missing visuals ------------------------------------
            for missing in asset_report.get("missing_visuals") or []:
                if not isinstance(missing, dict):
                    continue
                rows.append({
                    "run_id": str(run_id),
                    "project_id": str(project_id),
                    "asset_key": None,
                    "content_hash": None,
                    "source_url": None,
                    "provider": None,
                    "scene_number": missing.get("scene"),
                    "sentence_number": missing.get("sentence_number"),
                    "interval_start": None,
                    "interval_duration": None,
                    "evidence_class": None,
                    "verification": None,
                    "rights": None,
                    "decision_type": "missing",
                    "previous_decision_id": None,
                })

            # ---- rejected candidates --------------------------------
            records = (
                (asset_report.get("candidate_records") or {}).get("records")
                or []
            )
            for record in records:
                if not isinstance(record, dict):
                    continue
                if record.get("outcome") != "rejected":
                    continue
                # Stage 1 / R5: the ledger row carries the full
                # rejection event the report record holds — primary
                # reason, score and category — inside the existing
                # verification JSON column (no migration), and the
                # sentence linkage when the record carries it. Older
                # records without these fields produce the same row
                # shape as before, with None values.
                _sentences = record.get("sentence_numbers") or []
                rows.append({
                    "run_id": str(run_id),
                    "project_id": str(project_id),
                    "asset_key": record.get("candidate_id"),
                    "content_hash": None,
                    "source_url": record.get("url") or None,
                    "provider": str(record.get("provider") or "unknown"),
                    "scene_number": record.get("scene"),
                    "sentence_number": (
                        _sentences[0] if _sentences else None
                    ),
                    "interval_start": None,
                    "interval_duration": None,
                    "evidence_class": None,
                    "verification": {
                        "accepted": False,
                        "stage": record.get("stage"),
                        "reason": record.get("reason"),
                        "primary_rejection_reason": (
                            record.get("primary_rejection_reason")
                            or record.get("reason")
                        ),
                        "score": record.get("score"),
                        "score_basis": record.get("score_basis"),
                        "category": record.get("category"),
                        "kind": record.get("kind"),
                        # Stage 6 / C.A.5: the full event's
                        # diagnostics — contributions, contributing
                        # reasons and specificity — ride in the same
                        # JSON column (no migration). Records built
                        # before Stage 6 yield None for these keys.
                        "contributions": record.get("contributions"),
                        "contributing_reasons": (
                            record.get("contributing_reasons")
                            if record.get("contributing_reasons")
                            is not None
                            else record.get("verdict_reasons")
                        ),
                        "specificity": record.get("specificity"),
                        "matched_query": record.get("matched_query"),
                        # B-roll B0 (§6.1): the tier of the need the
                        # candidate was evaluated for rides in the
                        # same JSON column (None on pre-B0 records).
                        "tier": record.get("tier"),
                        # Phase 1 / AM-007: the verdict's own
                        # per-dimension record (the C.B.3
                        # 11-dimension shape) and its era waiver —
                        # previously lost here even though the
                        # report record carried them (None on
                        # pre-Phase-1 records).
                        "dimension_results": record.get(
                            "dimension_results"
                        ),
                        "era_waived": record.get("era_waived"),
                        # Phase 1 / AM-014: the candidate's title.
                        "title": record.get("title"),
                        # Metadata-absence bypass (run-6345 fix):
                        # the bypass marker, penalty and absence
                        # reasons ride in the same JSON column (None
                        # on records that were never bypassed).
                        "metadata_absence_bypass": record.get(
                            "metadata_absence_bypass"
                        ),
                        "metadata_absence_penalty": record.get(
                            "metadata_absence_penalty"
                        ),
                        "absence_reasons": record.get("absence_reasons"),
                    },
                    "rights": None,
                    "decision_type": "rejected",
                    "previous_decision_id": None,
                })

            # ---- verified but not picked (Phase 1 / AM-015) ------
            # The ledger previously had NO row for a candidate the
            # verifier accepted but selection never used, nor for a
            # verified candidate whose download failed — both event
            # classes existed in the asset report only, so the
            # certificate (built from the ledger alone) inherited
            # the gap. Both classes are recorded now, from the same
            # report records, with the same verification-JSON shape
            # discipline as the rejected rows above. A candidate
            # that reached the film (an "accepted" event) or whose
            # download failed is NOT also "not picked": the classes
            # are disjoint by candidate_id across the whole record
            # set. Idempotency is the run-level guard above (a run
            # with decisions is never re-recorded).
            _accepted_candidate_ids = {
                record.get("candidate_id")
                for record in records
                if isinstance(record, dict)
                and record.get("outcome") == "accepted"
            }
            _download_failed_candidate_ids = {
                record.get("candidate_id")
                for record in records
                if isinstance(record, dict)
                and record.get("outcome") == "download_failed"
            }

            def _candidate_event_verification(record, *, accepted):
                return {
                    "accepted": accepted,
                    "stage": record.get("stage"),
                    "score": record.get("score"),
                    "score_basis": record.get("score_basis"),
                    "category": record.get("category"),
                    "kind": record.get("kind"),
                    "contributions": record.get("contributions"),
                    "contributing_reasons": (
                        record.get("contributing_reasons")
                        if record.get("contributing_reasons")
                        is not None
                        else record.get("verdict_reasons")
                    ),
                    "specificity": record.get("specificity"),
                    "matched_query": record.get("matched_query"),
                    "tier": record.get("tier"),
                    "dimension_results": record.get(
                        "dimension_results"
                    ),
                    "era_waived": record.get("era_waived"),
                    "title": record.get("title"),
                    # Metadata-absence bypass (run-6345 fix): see
                    # the rejected rows above — same JSON column.
                    "metadata_absence_bypass": record.get(
                        "metadata_absence_bypass"
                    ),
                    "metadata_absence_penalty": record.get(
                        "metadata_absence_penalty"
                    ),
                    "absence_reasons": record.get("absence_reasons"),
                    "speculative_frame_confirmed": record.get(
                        "speculative_frame_confirmed"
                    ),
                }

            for record in records:
                if not isinstance(record, dict):
                    continue
                if record.get("outcome") != "verified":
                    continue
                candidate_id = record.get("candidate_id")
                if candidate_id in _accepted_candidate_ids:
                    continue
                if candidate_id in _download_failed_candidate_ids:
                    continue
                _sentences = record.get("sentence_numbers") or []
                rows.append({
                    "run_id": str(run_id),
                    "project_id": str(project_id),
                    "asset_key": candidate_id,
                    "content_hash": None,
                    "source_url": record.get("url") or None,
                    "provider": str(record.get("provider") or "unknown"),
                    "scene_number": record.get("scene"),
                    "sentence_number": (
                        _sentences[0] if _sentences else None
                    ),
                    "interval_start": None,
                    "interval_duration": None,
                    "evidence_class": None,
                    "verification": _candidate_event_verification(
                        record, accepted=True
                    ),
                    "rights": None,
                    "decision_type": "verified_not_picked",
                    "previous_decision_id": None,
                })

            # ---- download failures (Phase 1 / AM-015) -------------
            for record in records:
                if not isinstance(record, dict):
                    continue
                if record.get("outcome") != "download_failed":
                    continue
                _sentences = record.get("sentence_numbers") or []
                # The candidate WAS verified (accepted=True in the
                # shared shape); the failure is the download itself,
                # recorded with its reason and segment cause when
                # the event carries them.
                _verification = {
                    **_candidate_event_verification(
                        record, accepted=True
                    ),
                    "reason": record.get("reason"),
                    "segment_failure_cause": record.get(
                        "segment_failure_cause"
                    ),
                }
                rows.append({
                    "run_id": str(run_id),
                    "project_id": str(project_id),
                    "asset_key": record.get("candidate_id"),
                    "content_hash": None,
                    "source_url": record.get("url") or None,
                    "provider": str(record.get("provider") or "unknown"),
                    "scene_number": record.get("scene"),
                    "sentence_number": (
                        _sentences[0] if _sentences else None
                    ),
                    "interval_start": None,
                    "interval_duration": None,
                    "evidence_class": None,
                    "verification": _verification,
                    "rights": None,
                    "decision_type": "download_failed",
                    "previous_decision_id": None,
                })

            # ---- insert (append-only), resolving swap links ---------
            written = 0
            previous_row_id = None
            for row in rows:
                link = row.pop("_link_to_next", False)
                if previous_row_id is not None:
                    row["previous_decision_id"] = previous_row_id
                    previous_row_id = None
                decision = AssetDecision(
                    id=_new_uuid(),
                    created_at=_now(),
                    **row,
                )
                db.add(decision)
                db.flush()
                if link:
                    previous_row_id = decision.id
                written += 1
            db.commit()
            return written
        finally:
            db.close()
    except Exception as error:
        print(f"WARNING: asset decision recording failed: {error}")
        return None


def decision_counts(run_id, include_revision_decisions=True):
    """Ledger-derived decision counts for a run (parity checks):
    {"by_type": {...}, "rights_recorded": n, "rights_unknown": n}
    where the rights counts cover the accepted real-asset rows
    (selected / still / swapped_in) — the same population
    build_asset_report counts for rights (generated graphics are
    excluded there). None when unavailable.

    Population: by default EVERY decision recorded against the run,
    including editor-appended rows tagged with a revision_id
    (migration 0004). include_revision_decisions=False restricts to
    the run's OWN production decisions (revision_id IS NULL) — the
    population the Stage 4 run certificate must describe, so editor
    activity can never change the original production's certificate
    (Stage 6 adversarial defect D3)."""
    if not run_id:
        return None
    try:
        from models import AssetDecision

        db = _session()
        try:
            query = db.query(AssetDecision).filter(
                AssetDecision.run_id == str(run_id)
            )
            if not include_revision_decisions:
                query = query.filter(AssetDecision.revision_id.is_(None))
            rows = query.all()
        finally:
            db.close()
        by_type: dict[str, int] = {}
        recorded = 0
        unknown = 0
        for row in rows:
            by_type[row.decision_type] = by_type.get(row.decision_type, 0) + 1
            if row.decision_type in ("selected", "still", "swapped_in"):
                licence = (row.rights or {}).get("license")
                if licence and licence != "unknown":
                    recorded += 1
                else:
                    unknown += 1
        return {
            "by_type": by_type,
            "rights_recorded": recorded,
            "rights_unknown": unknown,
            "total": len(rows),
        }
    except Exception:
        return None


def list_decisions(run_id, limit=100, offset=0,
                   include_revision_decisions=True):
    """One page of a run's asset decisions, oldest first (the order
    they were recorded). Returns (rows, total) with rows as plain
    dicts; ([], 0) when the ledger is unavailable.

    Population: by default EVERY decision recorded against the run,
    including editor-appended rows tagged with a revision_id
    (migration 0004). include_revision_decisions=False restricts to
    the run's OWN production decisions (revision_id IS NULL) — the
    population the Stage 4 run certificate must describe, so editor
    activity can never change the original production's certificate
    (Stage 6 adversarial defect D3)."""
    if not run_id:
        return [], 0
    try:
        from models import AssetDecision

        db = _session()
        try:
            query = db.query(AssetDecision).filter(
                AssetDecision.run_id == str(run_id)
            )
            if not include_revision_decisions:
                query = query.filter(AssetDecision.revision_id.is_(None))
            total = query.count()
            rows = (
                query.order_by(AssetDecision.created_at.asc())
                .offset(max(0, int(offset)))
                .limit(max(1, int(limit)))
                .all()
            )
            return (
                [
                    {
                        "id": row.id,
                        "run_id": row.run_id,
                        "project_id": row.project_id,
                        "asset_key": row.asset_key,
                        "content_hash": row.content_hash,
                        "source_url": row.source_url,
                        "provider": row.provider,
                        "scene_number": row.scene_number,
                        "sentence_number": row.sentence_number,
                        "interval_start": row.interval_start,
                        "interval_duration": row.interval_duration,
                        "evidence_class": row.evidence_class,
                        "verification": row.verification,
                        "rights": row.rights,
                        "decision_type": row.decision_type,
                        "previous_decision_id": row.previous_decision_id,
                        "revision_id": row.revision_id,
                        "created_at": row.created_at,
                    }
                    for row in rows
                ],
                total,
            )
        finally:
            db.close()
    except Exception:
        return [], 0


def record_swap_decisions(run_id, project_id, *, scene_number,
                          sentence_number, original, refreshed,
                          revision_id=None):
    """Append the swapped_out / swapped_in pair for a post-run
    review swap (Stage 4 plan decisions, on top of Stage 1's MA-33
    machinery). The swapped_out row carries ONLY the replaced
    asset's identity — its evidence stays in its own original
    decision rows — and the swapped_in row carries the replacement's
    FRESH probe / verdict / rights from the refreshed record the
    caller built via _refresh_swapped_asset. Returns the new
    swapped_in row id, or None (fail-soft).

    Stage 6: when the swap comes from an editor revision, both rows
    are tagged with that revision_id (migration 0004) so the
    per-revision certificate can select them; plan-review swaps
    leave it NULL, exactly as before."""
    if not run_id:
        return None
    try:
        from models import AssetDecision

        hash_cache: dict = {}
        original_path = str(
            (original or {}).get("file_path")
            or (original or {}).get("path")
            or ""
        )
        refreshed_path = str(
            (refreshed or {}).get("file_path")
            or (refreshed or {}).get("path")
            or ""
        )
        db = _session()
        try:
            out_row = AssetDecision(
                id=_new_uuid(),
                run_id=str(run_id),
                project_id=str(project_id),
                asset_key=(
                    (original or {}).get("candidate_id")
                    or _file_sha256(original_path, hash_cache)
                    or original_path
                    or None
                ),
                content_hash=_file_sha256(original_path, hash_cache),
                source_url=(
                    (original or {}).get("source_url")
                    or (original or {}).get("video_url")
                    or (original or {}).get("image_url")
                    or None
                ),
                provider=str(
                    (original or {}).get("provider")
                    or (original or {}).get("source")
                    or "unknown"
                ),
                scene_number=scene_number,
                sentence_number=sentence_number,
                interval_start=(original or {}).get("clip_start"),
                interval_duration=(original or {}).get("duration_seconds"),
                evidence_class=None,
                verification=None,
                rights=None,
                decision_type="swapped_out",
                previous_decision_id=None,
                revision_id=str(revision_id) if revision_id else None,
                created_at=_now(),
            )
            db.add(out_row)
            db.flush()
            in_row = AssetDecision(
                id=_new_uuid(),
                run_id=str(run_id),
                project_id=str(project_id),
                asset_key=(
                    (refreshed or {}).get("candidate_id")
                    or _file_sha256(refreshed_path, hash_cache)
                    or refreshed_path
                    or None
                ),
                content_hash=_file_sha256(refreshed_path, hash_cache),
                source_url=(
                    (refreshed or {}).get("source_url")
                    or (refreshed or {}).get("video_url")
                    or (refreshed or {}).get("image_url")
                    or None
                ),
                provider=str(
                    (refreshed or {}).get("provider")
                    or (refreshed or {}).get("source")
                    or "unknown"
                ),
                scene_number=scene_number,
                sentence_number=sentence_number,
                interval_start=(refreshed or {}).get("clip_start"),
                interval_duration=(refreshed or {}).get("duration_seconds"),
                evidence_class=(refreshed or {}).get("segment_evidence"),
                verification=_verification_with_segment(refreshed or {}),
                rights=_asset_rights(refreshed or {}),
                decision_type="swapped_in",
                previous_decision_id=out_row.id,
                revision_id=str(revision_id) if revision_id else None,
                created_at=_now(),
            )
            db.add(in_row)
            db.commit()
            return in_row.id
        finally:
            db.close()
    except Exception as error:
        print(f"WARNING: swap decision recording failed: {error}")
        return None


# ---------------------------------------------------------------------
# Editor revision decisions + render runs (Stage 6)
# ---------------------------------------------------------------------


def record_editor_decision(run_id, project_id, *, revision_id,
                           decision_type, asset=None, scene_number=None,
                           sentence_number=None,
                           previous_decision_id=None):
    """Append ONE ledger row produced by an editor revision
    (upload, add, graphic regeneration, render-time record). The
    row is tagged with revision_id (migration 0004); the original
    run's rows are never touched. `asset` is the editor's asset
    record (file_path/source_url/provider/segment_evidence/
    verification + the pipeline-shaped rights fields _asset_rights
    consumes). Returns the new row id, or None (fail-soft)."""
    if not run_id or not revision_id:
        return None
    asset = asset or {}
    try:
        from models import AssetDecision

        hash_cache: dict = {}
        path = str(asset.get("file_path") or asset.get("path") or "")
        db = _session()
        try:
            row = AssetDecision(
                id=_new_uuid(),
                run_id=str(run_id),
                project_id=str(project_id),
                asset_key=(
                    asset.get("candidate_id")
                    or asset.get("decision_id")
                    or _file_sha256(path, hash_cache)
                    or path
                    or None
                ),
                content_hash=_file_sha256(path, hash_cache),
                source_url=(
                    asset.get("source_url")
                    or asset.get("video_url")
                    or asset.get("image_url")
                    or asset.get("url")
                    or None
                ),
                provider=str(
                    asset.get("provider") or asset.get("source") or "unknown"
                ),
                scene_number=scene_number,
                sentence_number=sentence_number,
                interval_start=asset.get("clip_start"),
                interval_duration=asset.get("duration_seconds"),
                evidence_class=asset.get("segment_evidence"),
                verification=_verification_with_segment(asset),
                rights=(
                    asset["rights"]
                    if isinstance(asset.get("rights"), dict)
                    else (
                        _asset_rights(asset)
                        if any(
                            key in asset
                            for key in ("license", "rights_status", "creator")
                        )
                        else None
                    )
                ),
                decision_type=str(decision_type),
                previous_decision_id=previous_decision_id,
                revision_id=str(revision_id),
                created_at=_now(),
            )
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()
    except Exception as error:
        print(f"WARNING: editor decision recording failed: {error}")
        return None


def record_music_decision(run_id, project_id, soundtrack_record,
                          revision_id=None):
    """Append the run's (or one revision's) MUSIC decision row
    (Stage H, M7): decision_type "music", carrying the soundtrack
    record's state, the track's content hash and its rights —
    licence "unknown" unless the record itself carries evidence
    (it never does for operator/editor-supplied files; unknown is
    recorded, never upgraded).

    Idempotent per (run_id, revision_id): a run records its
    soundtrack once; a revision records one row per revision.
    Editor rows are tagged with revision_id so the original run's
    certificate (which excludes revision decisions, D3) never sees
    them. Returns the row id, or None (fail-soft, and when there is
    no record to record)."""
    if not run_id or not isinstance(soundtrack_record, dict):
        return None
    try:
        from models import AssetDecision

        identity = soundtrack_record.get("identity") or {}
        state = soundtrack_record.get("state") or "not_requested"
        db = _session()
        try:
            revision_filter = (
                AssetDecision.revision_id == str(revision_id)
                if revision_id
                else AssetDecision.revision_id.is_(None)
            )
            existing = (
                db.query(AssetDecision)
                .filter(
                    AssetDecision.run_id == str(run_id),
                    AssetDecision.decision_type == "music",
                    revision_filter,
                )
                .first()
            )
            if existing is not None:
                return existing.id
            row = AssetDecision(
                id=_new_uuid(),
                run_id=str(run_id),
                project_id=str(project_id),
                asset_key=(
                    identity.get("file_path")
                    or soundtrack_record.get("requested_file")
                    or f"music:{state}"
                ),
                content_hash=identity.get("content_sha256"),
                source_url=None,
                provider=(
                    soundtrack_record.get("source_type")
                    or soundtrack_record.get("decided_by")
                    or "none"
                ),
                scene_number=None,
                sentence_number=None,
                interval_start=None,
                interval_duration=identity.get("duration_seconds"),
                evidence_class=None,
                verification={
                    "status": state,
                    "music_volume": soundtrack_record.get("music_volume"),
                    "soundtrack_loudness_lufs": soundtrack_record.get(
                        "loudness_lufs"
                    ),
                    "decided_by": soundtrack_record.get("decided_by"),
                },
                rights={
                    "license": identity.get("license") or "unknown",
                    "license_url": "",
                    "rights_status": "verify_before_publish",
                    "creator": "",
                    "source": soundtrack_record.get("source_type") or "",
                    "retrieved_at": "",
                },
                decision_type="music",
                previous_decision_id=None,
                revision_id=str(revision_id) if revision_id else None,
                created_at=_now(),
            )
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()
    except Exception as error:
        print(f"WARNING: music decision recording failed: {error}")
        return None


def list_decisions_for_revision(project_id, run_id, revision_ids):
    """The decision trail for one revision chain: the source run's
    ORIGINAL decisions (revision_id NULL) plus every decision the
    chain's revisions appended (revision_id in the chain), oldest
    first. Returns the row list in list_decisions' dict shape;
    [] when unavailable."""
    try:
        from models import AssetDecision
        from sqlalchemy import or_

        chain = [str(r) for r in (revision_ids or [])]
        db = _session()
        try:
            query = db.query(AssetDecision).filter(
                AssetDecision.project_id == str(project_id),
                or_(
                    (AssetDecision.run_id == str(run_id))
                    & (AssetDecision.revision_id.is_(None)),
                    AssetDecision.revision_id.in_(chain) if chain
                    else AssetDecision.revision_id.is_(None),
                ),
            )
            rows = query.order_by(AssetDecision.created_at.asc()).all()
            result = [
                {
                    "id": row.id,
                    "run_id": row.run_id,
                    "project_id": row.project_id,
                    "asset_key": row.asset_key,
                    "content_hash": row.content_hash,
                    "source_url": row.source_url,
                    "provider": row.provider,
                    "scene_number": row.scene_number,
                    "sentence_number": row.sentence_number,
                    "interval_start": row.interval_start,
                    "interval_duration": row.interval_duration,
                    "evidence_class": row.evidence_class,
                    "verification": row.verification,
                    "rights": row.rights,
                    "decision_type": row.decision_type,
                    "previous_decision_id": row.previous_decision_id,
                    "revision_id": row.revision_id,
                    "created_at": row.created_at,
                }
                for row in rows
            ]
            return result
        finally:
            db.close()
    except Exception:
        return []


def counts_for_decisions(rows):
    """decision_counts over an explicit row list (same definitions:
    rights counts cover selected / still / swapped_in rows)."""
    by_type: dict[str, int] = {}
    recorded = 0
    unknown = 0
    for row in rows or []:
        by_type[row["decision_type"]] = by_type.get(row["decision_type"], 0) + 1
        if row["decision_type"] in ("selected", "still", "swapped_in"):
            licence = (row.get("rights") or {}).get("license")
            if licence and licence != "unknown":
                recorded += 1
            else:
                unknown += 1
    return {
        "by_type": by_type,
        "rights_recorded": recorded,
        "rights_unknown": unknown,
        "total": len(rows or []),
    }


def submit_revision_render(project_id, script_text, revision_id,
                           source_run_id):
    """Queue a render run for one editor revision (Stage 6).

    Deduplicated per revision: an ACTIVE render run (queued or
    running) for the same revision is returned instead of queueing
    a second one. The run's options carry kind="revision_render" so
    lifecycle tooling can tell renders from productions. The check
    and the insert run under the same per-project mutex as
    submit_production (Stage 3, R2). Returns the run dict (with
    deduplicated flag), or None when the ledger is unavailable —
    the caller must then refuse the render rather than run it
    unrecorded."""
    with _SubmissionMutex(
        f"cinemora:submit:{project_id}:revision:{revision_id}"
    ):
        try:
            from models import ProductionRun

            db = _session()
            try:
                active = (
                    db.query(ProductionRun)
                    .filter(
                        ProductionRun.project_id == str(project_id),
                        ProductionRun.status.in_(ACTIVE_STATUSES),
                    )
                    .order_by(ProductionRun.created_at.asc())
                    .all()
                )
                for row in active:
                    options = row.options or {}
                    if (
                        options.get("kind") == "revision_render"
                        and str(options.get("revision_id")) == str(revision_id)
                    ):
                        return {
                            "run_id": row.id,
                            "project_id": row.project_id,
                            "status": row.status,
                            "script_hash": row.script_hash,
                            "deduplicated": True,
                        }
            finally:
                db.close()
        except Exception as error:
            print(f"WARNING: revision render dedup check failed: {error}")
            return None
        return _insert_run(
            project_id,
            script_text,
            {
                "kind": "revision_render",
                "revision_id": str(revision_id),
                "source_run_id": str(source_run_id),
                "entrypoint": "editor_render",
            },
            "queued",
        )


# ---------------------------------------------------------------------
# Run manifests (2E)
# ---------------------------------------------------------------------

MANIFEST_FILENAME = "run_manifest.json"

_SECRET_PATTERNS = [
    re.compile(r"bk_live_[A-Za-z0-9_\-]{8,}"),          # BrewKeg live key
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                  # OpenAI-style key
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),         # GitHub fine PAT
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),                 # GitHub classic PAT
    re.compile(r"AKIA[0-9A-Z]{16}"),                     # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),   # PEM private key
    re.compile(r"\b[0-9a-f]{40}\b"),                     # Serper-style hex key
]

_MANIFEST_REQUIRED_KEYS = (
    "run_id",
    "project_id",
    "script_hash",
    "sentence_count",
    "options",
    "providers",
    "rights_policy",
    "verification_thresholds",
    "llm_model",
    "prompt_version",
    "started_at",
    "status",
)


def provider_snapshot():
    """Configuration-only provider snapshot for the manifest: provider
    NAMES and whether their credential is configured (a boolean —
    never the credential itself)."""
    return {
        "serper": {
            "enabled": bool(os.getenv("SERPER_API_KEY")),
            "role": "discovery (web / images / videos)",
        },
        "archive_org": {
            "enabled": True,
            "role": "footage + stills (open collections)",
        },
        "wikimedia_commons": {
            "enabled": True,
            "role": "stills + video (licence-labelled)",
        },
        "youtube": {
            "enabled": True,
            "role": "one equal supplementary source (yt-dlp)",
        },
    }


def build_initial_manifest(run_id, project_id, script_text, options=None,
                           sentence_count=None):
    """The manifest as written at run start (2E). Configuration values
    only — no secrets are ever placed in a manifest."""
    thresholds = None
    llm_model = None
    try:
        from services.story_understanding import ACCEPT_THRESHOLD

        thresholds = dict(ACCEPT_THRESHOLD)
    except Exception:
        thresholds = None
    try:
        from services.llm_provider import BREWKEG_MODEL

        llm_model = BREWKEG_MODEL
    except Exception:
        llm_model = None
    return {
        "manifest_version": 1,
        "run_id": run_id,
        "project_id": str(project_id),
        "script_hash": script_sha256(script_text),
        "sentence_count": sentence_count,
        "options": options or {},
        "providers": provider_snapshot(),
        "rights_policy": os.getenv("RIGHTS_POLICY", "allow_all"),
        "verification_thresholds": thresholds,
        # story_understanding defines no prompt version constant; the
        # honest value is "unversioned" until it does.
        "prompt_version": "unversioned",
        "llm_model": llm_model,
        "started_at": _now().isoformat(),
        "finished_at": None,
        "status": "running",
    }


def enrich_manifest(manifest, *, status, execution=None, asset_report=None,
                    stage_outputs=None):
    """Return a finished copy of the manifest (2E): execution
    snapshot, accepted candidate ids, verification decision counts,
    stage output file names, finish time and final status."""
    enriched = dict(manifest or {})
    enriched["status"] = status
    enriched["finished_at"] = _now().isoformat()
    if execution is not None:
        enriched["execution"] = execution
    if isinstance(asset_report, dict):
        records = (
            (asset_report.get("candidate_records") or {}).get("records")
            or []
        )
        enriched["accepted_candidate_ids"] = [
            record.get("candidate_id")
            for record in records
            if isinstance(record, dict)
            and record.get("outcome") == "accepted"
            and record.get("candidate_id")
        ]
        enriched["verification_decisions"] = dict(
            (asset_report.get("candidate_records") or {}).get("by_outcome")
            or {}
        )
        enriched["accepted_assets"] = asset_report.get(
            "total_accepted_assets"
        )
        enriched["missing_visual_count"] = asset_report.get(
            "missing_visual_count"
        )
        # Stage H (M7): the run's music outcome is part of the
        # manifest — state, track identity and licence, plus the
        # final file's measured audio levels when the report has
        # them. Absent keys stay absent for pre-Stage-H reports.
        soundtrack = asset_report.get("soundtrack")
        if isinstance(soundtrack, dict):
            identity = soundtrack.get("identity") or {}
            enriched["music"] = {
                "state": soundtrack.get("state"),
                "decided_by": soundtrack.get("decided_by"),
                "source_type": soundtrack.get("source_type"),
                "file_name": identity.get("file_name"),
                "content_sha256": identity.get("content_sha256"),
                "license": identity.get("license") or "unknown",
            }
        if isinstance(asset_report.get("final_audio"), dict):
            enriched["final_audio"] = asset_report["final_audio"]
    if stage_outputs is not None:
        enriched["stage_outputs"] = list(stage_outputs)
    return enriched


def write_manifest(project_dir, manifest):
    """Write run_manifest.json atomically (temp file + os.replace,
    the MA-11 pattern, via services.atomic_write since Stage 4).
    The directory argument is the artefact home chosen by the
    caller — the run's artefact directory for pipeline runs
    (DATA-004). Fail-soft: returns the path or None."""
    from services.atomic_write import write_json_atomic

    try:
        project_dir = Path(project_dir)
        target = project_dir / MANIFEST_FILENAME
        write_json_atomic(target, manifest)
        return target
    except Exception as error:
        print(f"WARNING: run manifest write failed: {error}")
        return None


def _iter_strings(value, key_path=""):
    if isinstance(value, str):
        yield key_path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_strings(item, f"{key_path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _iter_strings(item, f"{key_path}[{index}]")


def validate_manifest(manifest):
    """Structural + secret-hygiene validation of a run manifest.
    Returns a list of problems (empty = valid). The script_hash field
    itself is exempt from the secret scan (it is a hash, and its
    format is checked separately)."""
    problems: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest is not a JSON object"]
    for key in _MANIFEST_REQUIRED_KEYS:
        if key not in manifest:
            problems.append(f"missing required key: {key}")
    digest = manifest.get("script_hash")
    if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
        problems.append("script_hash is not a full sha256 hex digest")
    for key_path, value in _iter_strings(manifest):
        if key_path.endswith("script_hash"):
            continue
        for pattern in _SECRET_PATTERNS:
            if pattern.search(value):
                problems.append(
                    f"secret-shaped value at {key_path or '<root>'} "
                    f"(pattern {pattern.pattern[:24]}...)"
                )
                break
    return problems
