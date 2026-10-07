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

    worker = SimpleWorker(
        queues,
        connection=redis_conn,
    )

    worker.work(with_scheduler=False)