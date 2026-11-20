import os

from redis import Redis
from rq import Queue, SimpleWorker

import jobs


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

redis_conn = Redis.from_url(REDIS_URL)

queues = [
    Queue("video_generation", connection=redis_conn),
    Queue("video_generation:intermediate", connection=redis_conn),
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


if __name__ == "__main__":
    print("========================================")
    print("DOCUMENTARY STUDIO WORKER")
    print("========================================")
    print("Redis:", REDIS_URL)
    print("Queues:")
    print(" - video_generation")
    print(" - video_generation:intermediate")
    print()
    print("Using Windows-compatible SimpleWorker...")
    print()

    # BLOCK-002: schema gate, before any job is consumed (and
    # before reconciliation touches the ledger). A schema that is
    # not ready stops the worker loudly here instead of degrading
    # silently mid-production.
    ensure_schema_ready()

    # Stage 2B (AUD-08): stale-run reconciliation. This process just
    # started, so any production run still recorded as "running" in
    # the ledger is an orphan of a dead worker — mark it failed /
    # "interrupted" instead of leaving it running forever. Fail-soft:
    # a ledger problem must never stop the worker from starting.
    try:
        from services import run_ledger

        reconciled = run_ledger.reconcile_stale_runs()
        if reconciled:
            print(f"Reconciled {reconciled} interrupted run(s).")
    except Exception as error:
        print(f"WARNING: stale-run reconciliation skipped: {error}")

    worker = SimpleWorker(
        queues,
        connection=redis_conn,
    )

    worker.work(with_scheduler=False)