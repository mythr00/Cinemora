import atexit
import os
import threading
import time

from redis import Redis
from rq import Queue, SimpleWorker

import jobs


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

redis_conn = Redis.from_url(REDIS_URL)

# PIPE-004: the worker listens on the ONE queue jobs.py produces
# to. A second listener ("video_generation:intermediate") used to
# sit here with no producer anywhere in live code — a job enqueued
# there by any stale client would have been consumed by a path
# expecting different payloads. Removed; this list and jobs.QUEUE_NAME
# are the queue contract.
queues = [
    Queue("video_generation", connection=redis_conn),
]


def ensure_schema_ready():
    """BLOCK-002: verify the database schema before the worker
    starts consuming jobs — the same gate the API applies in its
    lifespan (database.check_schema, IA-09). The worker previously
    never called it, so a worker started against a missing/behind
    schema degraded silently through the fail-soft ledger instead
    of surfacing the problem.

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


# ---------------------------------------------------------------------
# Worker singleton (Stage 3, R2 — DATA-005 / FP-16)
# ---------------------------------------------------------------------
# Before this stage, nothing stopped an operator (or a supervisor's
# restart race) from starting a SECOND worker on the same queue:
# both consumed jobs, same-project runs collided on artefact paths,
# and either worker's startup reconciliation failed the other's
# live runs. The singleton is a Redis lock — Redis is the store
# every worker already shares. The lock carries a TTL and is
# renewed by a daemon thread, so a worker that dies without
# releasing it blocks a replacement only for the TTL, never
# forever; compare-and-renew / compare-and-release Lua scripts
# make sure a worker can only renew or release its OWN lock.

SINGLETON_KEY = "cinemora:worker:singleton"
SINGLETON_TTL_SECONDS = 90
SINGLETON_RENEW_SECONDS = 30

_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
else
    return 0
end
"""

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


def acquire_worker_singleton(connection=None, worker_id=None,
                             key=SINGLETON_KEY):
    """Claim the worker singleton. Returns the worker_id holding
    the lock. Raises SystemExit(1) — loudly, like the schema gate —
    when another worker already holds it or Redis is unreachable:
    a worker that cannot prove it is the only consumer must not
    consume."""
    from services import run_ledger

    connection = connection if connection is not None else redis_conn
    worker_id = worker_id or run_ledger.current_worker_id()
    try:
        acquired = connection.set(
            key, worker_id, nx=True, ex=SINGLETON_TTL_SECONDS
        )
    except Exception as error:
        print(
            "ERROR: worker cannot start: could not reach Redis to "
            f"claim the worker singleton ({error})"
        )
        raise SystemExit(1) from error
    if not acquired:
        try:
            holder = connection.get(key)
            if isinstance(holder, bytes):
                holder = holder.decode("utf-8", "replace")
        except Exception:
            holder = None
        print(
            "ERROR: worker cannot start: another worker already "
            "holds the worker singleton"
            + (f" ({holder})" if holder else "")
            + ". Two workers on one queue corrupt each other's "
            "runs; stop the other worker first."
        )
        raise SystemExit(1)
    print(f"Worker singleton held by {worker_id}.")
    return worker_id


def _renew_singleton(connection, worker_id, key=SINGLETON_KEY):
    """One renewal beat. True while this worker still holds the
    lock; False when the lock was lost (expired and possibly
    re-acquired by another worker)."""
    try:
        result = connection.eval(
            _RENEW_SCRIPT, 1, key, worker_id, SINGLETON_TTL_SECONDS
        )
        return bool(result)
    except Exception as error:
        print(f"WARNING: worker singleton renewal failed: {error}")
        return True  # transient: try again next beat


def _release_singleton(connection, worker_id, key=SINGLETON_KEY):
    try:
        connection.eval(_RELEASE_SCRIPT, 1, key, worker_id)
    except Exception:
        pass


def _start_singleton_keeper(connection, worker_id, key=SINGLETON_KEY):
    """Daemon thread renewing the singleton until the process
    exits; atexit releases it on a clean shutdown. Losing the lock
    is reported loudly — the process keeps running its current job
    (killing it mid-production would be worse), but the operator
    is told a second worker may now exist."""

    def _keep():
        while True:
            time.sleep(SINGLETON_RENEW_SECONDS)
            if not _renew_singleton(connection, worker_id, key=key):
                print(
                    "ERROR: worker singleton LOST — another worker "
                    "may be consuming this queue now."
                )
                return

    thread = threading.Thread(
        target=_keep, name="worker-singleton-keeper", daemon=True
    )
    thread.start()
    atexit.register(_release_singleton, connection, worker_id, key)
    return thread


def _start_periodic_reconciler():
    """Daemon thread reclaiming orphaned runs WITHOUT a worker
    restart (Stage 3, R2 — the FP-09 minimal correction).

    Every 60s it runs the HEARTBEAT-AWARE reconciliation: only
    runs whose liveness evidence is older than the ledger's stale
    threshold are failed. Runs this worker is executing beat their
    heartbeats as they go, so they are never reclaimed from under
    it; a run whose worker died is reclaimed within a threshold of
    its last beat instead of waiting for a restart that might
    never come."""
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


if __name__ == "__main__":
    print("========================================")
    print("DOCUMENTARY STUDIO WORKER")
    print("========================================")
    print("Redis:", REDIS_URL)
    print("Queues:")
    print(" - video_generation")
    print()
    print("Using Windows-compatible SimpleWorker...")
    print()

    # BLOCK-002: schema gate, before any job is consumed (and
    # before reconciliation touches the ledger). A schema that is
    # not ready stops the worker loudly here instead of degrading
    # silently mid-production.
    ensure_schema_ready()

    # Stage 3 (R2): singleton gate, immediately after the schema
    # gate and BEFORE reconciliation. Holding the singleton is the
    # proof that no other worker is consuming, which is what makes
    # the total startup reconciliation below sound.
    _worker_id = acquire_worker_singleton()
    _start_singleton_keeper(redis_conn, _worker_id)

    # Stage 2B (AUD-08) + Stage 3 (R2): stale-run reconciliation.
    # This process now PROVES it is the only worker (singleton
    # above), so any production run still recorded as "running" is
    # an orphan of a dead predecessor — mark it failed /
    # "interrupted" instead of leaving it running forever
    # (total mode: run_ledger.reconcile_stale_runs with no
    # threshold). Fail-soft: a ledger problem must never stop the
    # worker from starting.
    try:
        from services import run_ledger

        reconciled = run_ledger.reconcile_stale_runs()
        if reconciled:
            print(f"Reconciled {reconciled} interrupted run(s).")
    except Exception as error:
        print(f"WARNING: stale-run reconciliation skipped: {error}")

    # Stage 3 (R2): from here on, orphaned runs are also reclaimed
    # periodically (heartbeat-aware) instead of only at restarts.
    _start_periodic_reconciler()

    worker = SimpleWorker(
        queues,
        connection=redis_conn,
    )

    worker.work(with_scheduler=False)
