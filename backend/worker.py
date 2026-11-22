"""The Cinemora worker process (Phase 8.4 rewrite).

One invocation = one worker process. The launcher starts
CINEMORA_WORKER_COUNT of them (default 2 for this host class).
What changed from the single-worker era, and why (design 03 §7.1):

- NO worker singleton. The Stage-3 singleton existed because two
  workers corrupted each other — through shared artefact paths
  (fixed by DATA-004 run-scoped artefacts), duplicate execution
  (fixed by the ledger's atomic claims), and startup
  reconciliation failing the other worker's live runs (fixed
  below). With those causes gone, the singleton was pure
  throughput loss, so it is deleted, not bypassed.
- Startup reconciliation is HEARTBEAT-AWARE ONLY. The old total
  startup reconciliation ("any running run is an orphan") was
  sound only under the singleton; a second worker starting now
  must never fail the first worker's live runs. Runs with fresh
  heartbeats are left alone; genuinely orphaned runs are still
  reclaimed by the same call the periodic reconciler makes.
- Forking Worker on POSIX (a crashed production kills a work
  horse, not the worker); SimpleWorker remains the off-POSIX
  fallback it always was.
- Two queues, priority first: video_generation_priority
  (interactive + priority-tier work), then video_generation.
- The dispatcher loop runs as a thread here (leader-elected in
  Redis, so exactly one worker dispatches at a time): it promotes
  the ledger backlog as capacity frees, sweeps transient-failure
  retries, and flags suspected-stuck stages.
"""

import os
import threading
import time

from redis import Redis
from rq import Queue, SimpleWorker

import jobs


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

redis_conn = Redis.from_url(REDIS_URL)

# Queue contract (PIPE-004 lineage): the worker listens on exactly
# the queues jobs.py produces to — priority lane first, so a worker
# becoming free takes interactive work before standard work.
queues = [
    Queue(jobs.PRIORITY_QUEUE_NAME, connection=redis_conn),
    Queue(jobs.QUEUE_NAME, connection=redis_conn),
]


def ensure_schema_ready():
    """BLOCK-002: verify the database schema before the worker
    starts consuming jobs — the same gate the API applies in its
    lifespan (database.check_schema, IA-09).

    Semantics match the API exactly: a SchemaVersionError (missing
    tables, behind/unknown stamp) is FATAL — SystemExit(1) with the
    gate's own clear reason; the unstamped-legacy result is a loud
    warning and startup proceeds, exactly as serving does. Returns
    the check_schema result dict on success.
    """
    from database import SchemaVersionError, check_schema

    try:
        result = check_schema()
    except SchemaVersionError as error:
        print(f"ERROR: worker cannot start: {error}")
        raise SystemExit(1) from error
    if result.get("status") == "unstamped_legacy":
        print(
            "WARNING: database has no alembic_version stamp "
            "(legacy schema) — proceeding as the API does; see "
            "database.check_schema for the stamping guidance."
        )
    else:
        print(
            "Schema check OK: database at Alembic head "
            f"{result.get('expected_head')}."
        )
    return result


def _start_periodic_reconciler():
    """Daemon thread reclaiming orphaned runs WITHOUT a worker
    restart (Stage 3, R2 — the FP-09 minimal correction).

    Every 60s it runs the HEARTBEAT-AWARE reconciliation: only
    runs whose liveness evidence is older than the ledger's stale
    threshold are failed. Runs any worker is executing beat their
    heartbeats as they go, so they are never reclaimed from under
    their worker; a run whose worker died is reclaimed within a
    threshold of its last beat instead of waiting for a restart
    that might never come. Safe to run in EVERY worker process:
    the reconciliation itself is atomic per run."""
    from services import run_ledger

    def _reconcile():
        while True:
            time.sleep(60)
            try:
                reconciled = run_ledger.reconcile_stale_runs(
                    stale_after_seconds=run_ledger.STALE_AFTER_SECONDS
                )
                if reconciled:
                    print(
                        f"Periodic reconciliation: {reconciled} "
                        "orphaned run(s) marked interrupted."
                    )
            except Exception as error:
                print(
                    "WARNING: periodic stale-run reconciliation "
                    f"failed: {error}"
                )

    thread = threading.Thread(
        target=_reconcile, name="worker-stale-run-reconciler",
        daemon=True,
    )
    thread.start()
    return thread


def _start_dispatcher():
    """The fair dispatcher's loop as a daemon thread (Phase 8.4).
    Leader election inside services.dispatcher means several
    workers can start this safely; only the leader's cycles act."""
    from services import dispatcher

    return dispatcher.start_dispatcher_thread()


def build_worker():
    """The RQ worker for this platform: forking where supported
    (job isolation — a segfaulting production kills a horse, not
    the worker), SimpleWorker elsewhere."""
    if os.name == "posix":
        from rq import Worker

        return Worker(queues, connection=redis_conn)
    return SimpleWorker(queues, connection=redis_conn)


if __name__ == "__main__":
    print("========================================")
    print("DOCUMENTARY STUDIO WORKER")
    print("========================================")
    print("Redis:", REDIS_URL)
    print("Queues (priority first):")
    for _queue in queues:
        print(f" - {_queue.name}")
    print()

    # BLOCK-002: schema gate, before any job is consumed (and
    # before reconciliation touches the ledger). A schema that is
    # not ready stops the worker loudly here instead of degrading
    # silently mid-production.
    ensure_schema_ready()

    # Phase 8.4: startup reconciliation is HEARTBEAT-AWARE ONLY.
    # Other workers may be mid-production right now; their runs
    # have fresh heartbeats and are left strictly alone. Only runs
    # whose liveness evidence is older than the stale threshold
    # are orphans. Fail-soft: a ledger problem must never stop the
    # worker from starting.
    try:
        from services import run_ledger

        reconciled = run_ledger.reconcile_stale_runs(
            stale_after_seconds=run_ledger.STALE_AFTER_SECONDS
        )
        if reconciled:
            print(f"Reconciled {reconciled} interrupted run(s).")
    except Exception as error:
        print(f"WARNING: stale-run reconciliation skipped: {error}")

    # Orphaned runs are also reclaimed periodically (heartbeat-
    # aware) instead of only at restarts, in every worker.
    _start_periodic_reconciler()

    # The dispatcher loop (leader-elected): backlog promotion,
    # transient-failure retries, stuck-stage watchdog.
    _start_dispatcher()

    worker = build_worker()
    worker.work(with_scheduler=False)
