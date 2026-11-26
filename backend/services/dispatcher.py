"""The fair dispatcher + retry sweep + stage watchdog (Phase 8.4;
design 03 §3.2, §4.1, §4.3).

THE BACKLOG LIVES IN THE LEDGER, not in RQ (03 §3.1): submissions
create `queued` run rows; this dispatcher promotes them into RQ
when capacity allows. RQ holds only about as many jobs as there
are workers, which is what makes fairness, Redis-loss recovery
(the dispatcher simply re-promotes from `queued` rows) and
per-user queue depth (admin) all the same ledger query.

Promotion discipline:
- Per-owner cap: the owner's plan `max_concurrent_runs` (no plan
  resolves -> uncapped, the pre-plan-system behaviour).
- Platform cap: CINEMORA_PLATFORM_MAX_RUNNING (default:
  CINEMORA_WORKER_COUNT, default 2 — about one in-flight run per
  worker, so promotion order IS start order).
- Least-served-first: among eligible owners, fewest running, then
  least-recently dispatched, then oldest queued run. Within an
  owner, strict FIFO. A user who submits 10 productions cannot
  starve a user who submits 1 (03 §3.2).
- Priority: owners on a priority-tier plan promote into the
  priority lane, but while standard runs wait, priority promotions
  may use at most platform_cap - 1 slots (the reserve — "priority"
  must never become a denial of service against everyone else).
- Interactive jobs (analysis phase, revision renders) do NOT pass
  through here: they enqueue directly onto the priority lane at
  submission, as they did before 8.4 — a user is actively waiting
  on them and they are short.

A short Redis mutex serialises dispatch passes across processes
(best-effort: if Redis is down the pass runs unlocked — the
ledger's atomic claim still guarantees no double execution, so a
dispatcher race degrades to a wasted claim attempt, never a
duplicate production).

Also here:
- classify_failure + retry_sweep: ledger-level retries for
  TRANSIENT failure classes only (max 2), as new runs riding
  stage-resume adoption (03 §4.3). Deterministic failures
  (coverage gate, validation) are never auto-retried.
- watchdog_sweep: flags runs whose current stage exceeds its
  budget as stuck_suspected — flagged, never killed (03 §4.1).

Everything is fail-soft: a dispatcher problem must never break a
submission or a worker.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

try:
    from services import logging_setup
except ImportError:  # pragma: no cover - flat-layout fallback
    import logging_setup

# B-13 (Phase 5c-1): dispatcher events ride the structured
# cinemora logger tree.
logger = logging_setup.get_logger("dispatcher")

ACTIVE_STATUSES = ("queued", "running", "awaiting_approval")
MAX_AUTO_RETRIES = 2
_RETRY_BACKOFFS = (60, 300)  # seconds, by attempt number (1-based)

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


def platform_cap() -> int:
    raw = os.getenv("CINEMORA_PLATFORM_MAX_RUNNING")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    try:
        return max(1, int(os.getenv("CINEMORA_WORKER_COUNT", "2")))
    except ValueError:
        return 2


def _dispatch_mutex():
    """Best-effort cross-process dispatch serialisation."""
    try:
        client = _redis()
        token = f"{os.getpid()}:{time.time()}"
        if client.set("cinemora:dispatcher:mutex", token, nx=True, ex=10):
            return client, token
        return None, None
    except Exception:
        return None, None


def _release_mutex(client, token):
    if client is None:
        return
    try:
        if client.get("cinemora:dispatcher:mutex") == token:
            client.delete("cinemora:dispatcher:mutex")
    except Exception:
        pass


def _parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------


def dispatch_once() -> list[tuple[str, str | None]]:
    """One promotion pass. Returns [(run_id, job_id)] promoted.
    Fail-soft: returns [] on any ledger problem."""
    client, token = _dispatch_mutex()
    if client is not None and token is None:
        return []  # another pass is in progress
    try:
        return _dispatch_pass()
    except Exception:
        logger.warning("dispatcher pass failed", exc_info=True)
        return []
    finally:
        _release_mutex(client, token)


def _dispatch_pass() -> list[tuple[str, str | None]]:
    import jobs
    from database import SessionLocal
    from models import ProductionRun, Project
    from services import plans as plans_service
    from services import run_ledger

    db = SessionLocal()
    try:
        _clear_stale_dispatches(db)
        running_rows = (
            db.query(ProductionRun)
            .filter(ProductionRun.status == "running")
            .all()
        )
        running_by_owner: dict = {}
        for row in running_rows:
            running_by_owner[row.owner_id] = (
                running_by_owner.get(row.owner_id, 0) + 1
            )
        running_total = len(running_rows)

        backlog = (
            db.query(ProductionRun)
            .filter(ProductionRun.status == "queued")
            .order_by(ProductionRun.created_at)
            .all()
        )
        now = datetime.now(timezone.utc)
        waiting = []  # (run, project, plan, is_priority)
        for run in backlog:
            options = run.options or {}
            if options.get("dispatched_job_id"):
                continue  # already promoted; waiting on a worker
            not_before = _parse_iso(options.get("retry_not_before"))
            if not_before and not_before > now:
                continue  # retry backoff hasn't elapsed
            if options.get("kind") == "revision_render":
                continue  # interactive: enqueued directly at submit
            project = db.get(Project, run.project_id)
            if project is None:
                continue
            plan = (
                plans_service.resolve_effective_plan(db, run.owner_id)
                if run.owner_id
                else None
            )
            is_priority = bool(plan and plan.priority_tier >= 1)
            waiting.append((run, project, plan, is_priority))

        if not waiting:
            return []

        cap = platform_cap()
        standard_waiting = any(not p for *_x, p in waiting)
        # Least-recently dispatched per owner, for the tiebreak.
        last_dispatch: dict = {}
        for run, _project, _plan, _prio in waiting:
            stamp = _parse_iso((run.options or {}).get("dispatched_at"))
            owner = run.owner_id
            if stamp and (owner not in last_dispatch or stamp < last_dispatch[owner]):
                last_dispatch[owner] = stamp

        promoted: list[tuple[str, str | None]] = []
        # Local tallies: promoted runs count as running for the
        # rest of this pass (their jobs are about to execute).
        while running_total < cap:
            candidates = []
            for run, project, plan, is_priority in waiting:
                owner_cap = (
                    plan.max_concurrent_runs
                    if plan is not None
                    else None
                )
                if (
                    owner_cap is not None
                    and running_by_owner.get(run.owner_id, 0) >= owner_cap
                ):
                    continue
                if is_priority and standard_waiting and running_total >= cap - 1:
                    continue  # the reserve: priority never takes the last slot
                candidates.append((run, project, plan, is_priority))
            if not candidates:
                break
            candidates.sort(
                key=lambda item: (
                    running_by_owner.get(item[0].owner_id, 0),
                    last_dispatch.get(item[0].owner_id)
                    or datetime.min.replace(tzinfo=timezone.utc),
                    item[0].created_at,
                )
            )
            run, project, plan, is_priority = candidates[0]
            job_id = jobs.enqueue_promoted_run(run, project, is_priority)
            if job_id is None:
                # Enqueue failed; jobs already marked the run failed.
                waiting = [w for w in waiting if w[0].id != run.id]
                continue
            run_ledger.mark_dispatched(run.id, job_id)
            logging_setup.log_event(
                logger,
                logging.INFO,
                "run_promoted",
                run_id=run.id,
                job_id=job_id,
                project_id=run.project_id,
                priority=is_priority,
            )
            promoted.append((run.id, job_id))
            running_by_owner[run.owner_id] = (
                running_by_owner.get(run.owner_id, 0) + 1
            )
            running_total += 1
            waiting = [w for w in waiting if w[0].id != run.id]
        return promoted
    finally:
        db.close()


def _job_failure_detail(job) -> str:
    """The terminal error line of a failed RQ job: the last
    non-empty line of its exception info (the exception class +
    message), bounded. Falls back to a generic label — a failed
    job must never read as a detail-less failure."""
    exc_info = getattr(job, "exc_info", None)
    if exc_info:
        lines = [line.strip() for line in str(exc_info).splitlines() if line.strip()]
        if lines:
            return lines[-1][:500]
    return "RQ job failed without recorded exception info"


def _job_is_failed(job) -> bool:
    failed = getattr(job, "is_failed", None)
    if failed is not None:
        return bool(failed)
    try:
        return job.get_status() == "failed"
    except Exception:
        return False


def _job_is_finished(job) -> bool:
    finished = getattr(job, "is_finished", None)
    if finished is not None:
        return bool(finished)
    try:
        return job.get_status() == "finished"
    except Exception:
        return False


#: RQ job statuses in which the job will never execute its run:
#: it is not queued for a worker, not started, and not terminal in
#: the failed/finished senses the sweep already handles. A run
#: whose dispatched job sits in one of these states is wedged
#: exactly like the missing-job case (Phase 0 wedge audit): no
#: enqueue path in the codebase produces them today, but the sweep
#: previously treated ANY existing job as live and left such runs
#: stamped forever.
_INERT_JOB_STATUSES = frozenset(
    {"deferred", "scheduled", "canceled", "cancelled", "stopped"}
)


def _job_status(job):
    """The job's RQ status string, or None when it cannot be
    determined (an undeterminable status is judged as live —
    the sweep only acts on positive evidence)."""
    try:
        return str(job.get_status())
    except Exception:
        return None


def _run_shows_execution_started(row) -> bool:
    """True when the ledger records that this run's execution
    actually began. The atomic claim (run_ledger._claim_row)
    stamps started_at AND the presence payload (progress
    worker_id / claimed_at) in the same UPDATE that moves the
    run out of "queued", and finish_run records the execution
    snapshot — so any of those present means the run's own
    lifecycle owns its state and the stale-dispatch sweep must
    leave it alone."""
    if getattr(row, "started_at", None) is not None:
        return True
    if getattr(row, "execution", None) is not None:
        return True
    progress = getattr(row, "progress", None) or {}
    return bool(progress.get("claimed_at") or progress.get("worker_id"))


def _clear_dispatch_stamp(db, row) -> None:
    """Drop a queued run's dispatched_job_id / dispatched_at
    stamp so the promotion pass treats it as waiting again."""
    options = dict(row.options or {})
    options.pop("dispatched_job_id", None)
    options.pop("dispatched_at", None)
    row.options = options
    db.commit()


def _clear_stale_dispatches(db) -> None:
    """Reconcile queued runs against their dispatched RQ jobs.

    - Job no longer exists (Redis restart, job expired): the stamp
      is cleared so the run can be promoted again — the ledger,
      not RQ, is the queue of record.
    - Job FAILED terminally (Gate 2 field fix, 2026-11-26): the
      run is marked FAILED with the job's error recorded on it.
      Before this, Job.fetch succeeded for failed jobs (RQ keeps
      them), so the sweep treated them as "a worker will take it"
      forever, _dispatch_pass skipped the run for its stamp, and
      the run sat "queued" indefinitely with the error nowhere —
      reproduced live when run_full_pipeline died instantly on a
      PermissionError before it could claim/fail its own run.
      finish_run owns the terminal write (first-finisher-wins,
      billing settlement included); the failure is conclusive, so
      no freshness grace applies to it.
    - Job FINISHED but the run was never claimed (F-4 field fix,
      2026-11-26): the B-02 unclaimed-execution refusal working
      as designed — the job's claim lost (there: a SQLite
      "database is locked" burst) and the job returned
      deduplicated_execution without ever executing the run.
      The run itself did not fail, so it is NOT marked failed:
      the stamp is cleared exactly like the missing-job case and
      the run is re-promoted (the dispatcher's bounded retry
      sweep governs repeated failures). Before this, a finished
      job was neither missing nor failed, so the run sat
      invisibly "queued" until the job's 500-second RQ result
      record expired and the job became "missing" (8m27s in the
      field). No freshness grace applies: a finished job is
      terminal in RQ, its claim attempts are over, and the claim
      itself is atomic on the run's status, so clearing the
      stamp cannot race a live claim by that job. The unwedge
      fires ONLY when the run shows no execution ever started
      (_run_shows_execution_started); a finished job whose run
      WAS claimed/executing is left alone — that run's own
      finish path owns its state.
    - Job INERT (Phase 1 wedge catch-all): the job exists but is
      deferred / scheduled / canceled / stopped — no worker will
      ever take it. After the same 2-minute freshness grace as
      the missing-job case, the stamp is cleared and the run is
      re-promoted (again only when no execution ever started)."""
    from models import ProductionRun
    from services import run_ledger

    rows = (
        db.query(ProductionRun)
        .filter(ProductionRun.status == "queued")
        .all()
    )
    failed_runs: list[tuple[str, str]] = []
    for row in rows:
        options = row.options or {}
        job_id = options.get("dispatched_job_id")
        if not job_id:
            continue
        job = None
        try:
            from rq.job import Job

            import jobs

            job = Job.fetch(job_id, connection=jobs.redis_connection)
        except Exception:
            job = None
        if job is not None:
            if _job_is_failed(job):
                failed_runs.append((row.id, _job_failure_detail(job)))
                continue
            if _job_is_finished(job) and not _run_shows_execution_started(
                row
            ):
                # F-4: finished without ever claiming this run —
                # clear the stamp so the pass below re-promotes
                # it, exactly like the missing-job case.
                _clear_dispatch_stamp(db, row)
                logging_setup.log_event(
                    logger,
                    logging.WARNING,
                    "run_unwedged_finished_job",
                    run_id=row.id,
                    job_id=job_id,
                )
                continue
            if _job_status(job) in _INERT_JOB_STATUSES:
                # Phase 1 wedge catch-all: the job exists but is
                # in a state where no worker will ever take it
                # (deferred / scheduled / canceled / stopped).
                # Judged with the same 2-minute freshness grace as
                # a missing job — a just-dispatched job's status
                # may still be settling — then treated exactly
                # like the missing-job case: clear the stamp and
                # let the pass re-promote the run. A run whose
                # execution started is left alone regardless: its
                # own lifecycle owns its state.
                if _run_shows_execution_started(row):
                    continue
                dispatched_at = _parse_iso(options.get("dispatched_at"))
                if dispatched_at and (
                    datetime.now(timezone.utc) - dispatched_at
                ) < timedelta(minutes=2):
                    continue  # too fresh to judge an inert job
                _clear_dispatch_stamp(db, row)
                logging_setup.log_event(
                    logger,
                    logging.WARNING,
                    "run_unwedged_inert_job",
                    run_id=row.id,
                    job_id=job_id,
                    job_status=_job_status(job),
                )
            continue  # job exists and is live, or owns the run's state
        dispatched_at = _parse_iso(options.get("dispatched_at"))
        if dispatched_at and (
            datetime.now(timezone.utc) - dispatched_at
        ) < timedelta(minutes=2):
            continue  # too fresh to judge a missing job
        _clear_dispatch_stamp(db, row)
    for run_id, detail in failed_runs:
        if run_ledger.finish_run(
            run_id, "failed", error=f"dispatched job failed: {detail}"
        ):
            logging_setup.log_event(
                logger,
                logging.WARNING,
                "run_failed_dispatched_job",
                run_id=run_id,
                error=detail,
            )


# ---------------------------------------------------------------------
# Failure classification + retry sweep (03 §4.3)
# ---------------------------------------------------------------------

_TRANSIENT_MARKERS = (
    "interrupted",      # reconciler's orphan mark (crash/timeout)
    "enqueue failed",   # the run never started
    "provider_backed_off",
    "429",
    "temporarily",
    "connection reset",
    "connection aborted",
)

_DETERMINISTIC_MARKERS = (
    "coverage",         # coverage-gate failure: same input, same result
    "validation",
    "invalid",
)


def classify_failure(run: dict) -> str | None:
    """'transient' | 'deterministic' | None (unknown -> no retry).
    Conservative by design: only known-transient classes retry."""
    error = str(run.get("error") or "").lower()
    if not error:
        return None
    if any(marker in error for marker in _DETERMINISTIC_MARKERS):
        return "deterministic"
    if any(marker in error for marker in _TRANSIENT_MARKERS):
        return "transient"
    return None


def retry_sweep() -> list[str]:
    """Create retry runs for recently failed transient runs.
    Returns the new run ids. Runs in the worker's periodic thread."""
    from database import SessionLocal
    from models import ProductionRun, Project
    from services import run_ledger

    created: list[str] = []
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        failed = (
            db.query(ProductionRun)
            .filter(
                ProductionRun.status == "failed",
                ProductionRun.finished_at.isnot(None),
            )
            .order_by(ProductionRun.finished_at.desc())
            .limit(50)
            .all()
        )
        for run in failed:
            finished = run.finished_at
            if finished is not None:
                if finished.tzinfo is None:
                    finished = finished.replace(tzinfo=timezone.utc)
                if finished < cutoff:
                    continue
            run_dict = run_ledger.get_run(run.id) or {}
            if classify_failure(run_dict) != "transient":
                continue
            options = run.options or {}
            attempt = int(options.get("retry_attempt") or 0)
            if attempt >= MAX_AUTO_RETRIES:
                continue
            # A retry child already exists? (options.retry_of == run.id)
            siblings = (
                db.query(ProductionRun)
                .filter(ProductionRun.project_id == run.project_id)
                .all()
            )
            if any(
                (s.options or {}).get("retry_of") == run.id for s in siblings
            ):
                continue
            project = db.get(Project, run.project_id)
            if project is None:
                continue
            # Only retry the script that actually failed: if the
            # project's script changed since, the user's own new
            # submission governs (and stage adoption keys on the
            # hash either way).
            if run_ledger.script_sha256(project.script) != run.script_hash:
                continue
            backoff = _RETRY_BACKOFFS[min(attempt, len(_RETRY_BACKOFFS) - 1)]
            retry_options = {
                **options,
                "retry_of": run.id,
                "retry_attempt": attempt + 1,
                "retry_not_before": (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=backoff)
                ).isoformat(),
            }
            retry_options.pop("dispatched_job_id", None)
            retry_options.pop("dispatched_at", None)
            submission = run_ledger.submit_production(
                run.project_id, project.script, options=retry_options
            )
            if submission and not submission.get("deduplicated"):
                created.append(submission["run_id"])
        return created
    except Exception:
        logger.warning("retry sweep failed", exc_info=True)
        return created
    finally:
        db.close()


# ---------------------------------------------------------------------
# Stage watchdog (03 §4.1)
# ---------------------------------------------------------------------


def stage_watchdog_budget() -> int:
    try:
        return max(60, int(os.getenv("CINEMORA_STAGE_WATCHDOG_SECONDS", "3600")))
    except ValueError:
        return 3600


def watchdog_sweep() -> list[str]:
    """Flag running runs whose current stage has exceeded the
    watchdog budget. Returns flagged run ids. Flag only — the job
    timeout remains the hard kill."""
    import json

    from database import SessionLocal
    from models import ProductionRun
    from services import media_paths, run_ledger

    flagged: list[str] = []
    budget = stage_watchdog_budget()
    db = SessionLocal()
    try:
        running = (
            db.query(ProductionRun)
            .filter(ProductionRun.status == "running")
            .all()
        )
        for run in running:
            stage = run.stage
            if not stage:
                continue
            try:
                project_dir = media_paths.project_dir(run.project_id)
                state_path = media_paths.resolve_artefact(
                    project_dir, "production_state.json", run_id=run.id
                )
                state = json.loads(state_path.read_text(encoding="utf-8"))
                record = (state.get("stages") or {}).get(stage) or {}
                started = _parse_iso(record.get("started_at"))
                if started is None:
                    continue
                age = (datetime.now(timezone.utc) - started).total_seconds()
                if age > budget and run_ledger.flag_stuck(
                    run.id, stage, budget
                ):
                    flagged.append(run.id)
            except Exception:
                continue
        return flagged
    except Exception:
        logger.warning("watchdog sweep failed", exc_info=True)
        return flagged
    finally:
        db.close()


# ---------------------------------------------------------------------
# Worker-side loop (leader-elected)
# ---------------------------------------------------------------------

LEADER_KEY = "cinemora:dispatcher:leader"
LEADER_TTL_SECONDS = 45

# Phase 8: the subscription lifecycle job runs hourly, only on
# the leader, throttled here (the loop itself cycles in seconds).
_BILLING_TICK_INTERVAL_SECONDS = 3600
_last_billing_tick = 0.0

# Phase 8 (§21): the retention sweeper runs every minute on the
# leader (reminders + deadline deletions; deletions additionally
# require CINEMORA_RETENTION_ENABLED=1 inside the sweeper).
_RETENTION_TICK_INTERVAL_SECONDS = 60
_last_retention_tick = 0.0


def run_dispatcher_loop(stop_event: threading.Event | None = None):
    """The dispatcher's periodic loop, run as a thread in ONE
    worker process (leader lock in Redis, renewed each cycle;
    non-leaders idle). Also runs the retry + watchdog sweeps.
    Submission-time dispatch_once() calls in the API/jobs processes
    cover the common latency; this loop is the backstop that frees
    capacity as runs finish."""
    from services import run_ledger

    stop_event = stop_event or threading.Event()
    identity = f"{os.getpid()}:{threading.get_ident()}"
    while not stop_event.is_set():
        try:
            client = _redis()
            is_leader = bool(
                client.set(LEADER_KEY, identity, nx=True, ex=LEADER_TTL_SECONDS)
            )
            if not is_leader and client.get(LEADER_KEY) == identity:
                client.expire(LEADER_KEY, LEADER_TTL_SECONDS)
                is_leader = True
        except Exception:
            is_leader = True  # Redis down: pass runs unlocked anyway
        if is_leader:
            try:
                dispatch_once()
                retry_sweep()
                watchdog_sweep()
            except Exception:
                logger.warning("dispatcher loop cycle failed", exc_info=True)
            global _last_billing_tick
            now_mono = time.monotonic()
            if now_mono - _last_billing_tick >= _BILLING_TICK_INTERVAL_SECONDS:
                _last_billing_tick = now_mono
                try:
                    from services.billing import lifecycle

                    summary = lifecycle.run_lifecycle_tick()
                    if summary.get("errors"):
                        logger.warning(
                            "billing lifecycle tick errors: %s", summary
                        )
                except Exception:
                    logger.warning(
                        "billing lifecycle tick failed", exc_info=True
                    )
            global _last_retention_tick
            if now_mono - _last_retention_tick >= _RETENTION_TICK_INTERVAL_SECONDS:
                _last_retention_tick = now_mono
                try:
                    from services.billing import retention

                    retention.run_retention_tick()
                except Exception:
                    logger.warning(
                        "retention tick failed", exc_info=True
                    )
        stop_event.wait(15)


def start_dispatcher_thread() -> threading.Thread:
    thread = threading.Thread(
        target=run_dispatcher_loop,
        name="cinemora-dispatcher",
        daemon=True,
    )
    thread.start()
    return thread
