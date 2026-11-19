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