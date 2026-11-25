"""Launcher for panel hosts that run a single Python file (e.g. the
Nodaroo/Pterodactyl "python generic" egg, whose startup runs app.py).

What it does, in order:
1. Loads KEY=VALUE pairs from ``cinemora.env`` in the repository root if
   that file exists. The file is never committed — on the server it is
   created by hand in the panel's file manager and is the only place the
   database/Redis/API secrets live.
2. Fills in non-secret defaults for a hosted launch (paths stay inside the
   server's home directory, which is the persistent disk there). Real
   environment variables and cinemora.env always win over these defaults.
3. Runs the Alembic migrations from backend/ (which read DATABASE_URL from
   the environment), exactly like the Docker image's entrypoint.
4. Serves the FastAPI app with uvicorn on 0.0.0.0:$SERVER_PORT (the panel's
   allocated port), falling back to $PORT, then 25585.
5. Starts CINEMORA_WORKER_COUNT worker processes (B-03/B-04) as children
   of this launcher and supervises them for the life of the web server:
   a monitor thread restarts any worker that dies, and all workers are
   terminated when the web server exits. Panel limitation: the panel
   sees and manages only this one process — if the panel kills or
   restarts app.py, the children die with it (they are in its process
   tree and are terminated in the finally block below); there is no
   independent panel-level restart policy for the workers, and a hard
   SIGKILL of app.py can orphan children until the panel reaps the
   container. See DEPLOYMENT-RUNBOOK.md.
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(ROOT, "backend")
DATA = os.path.join(ROOT, "data")

# 1. Local env file (secrets live only on the server, never in git).
_env_file = os.path.join(ROOT, "cinemora.env")
if os.path.isfile(_env_file):
    with open(_env_file, encoding="utf-8") as _fh:
        for _line in _fh:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _, _value = _line.partition("=")
            os.environ.setdefault(
                _key.strip(), _value.strip().strip('"').strip("'")
            )

# 2. Non-secret defaults for a hosted launch.
os.environ.setdefault("RIGHTS_POLICY", "allow_all")
os.environ.setdefault("CINEMORA_FOUNDER_EMAIL", "mirthdraught@gmail.com")
os.environ.setdefault("CINEMORA_WORKER_COUNT", "2")
os.environ.setdefault("CINEMORA_RENDER_CONCURRENCY", "1")
os.environ.setdefault("CINEMORA_MEDIA_ROOT", os.path.join(DATA, "media"))
os.environ.setdefault(
    "SOURCE_INTEL_DB", os.path.join(DATA, "source-intel", "source_intel.sqlite3")
)
os.environ.setdefault("HF_HOME", os.path.join(DATA, "hf-cache"))
os.environ.setdefault("CINEMORA_CORS_ORIGINS", "https://cinemora-chi.vercel.app")
os.environ.setdefault("CINEMORA_COOKIE_SECURE", "1")
# E-21/B-05: the operator-token dual-auth window stays CLOSED on a
# hosted launch unless the operator deliberately opens it by setting
# CINEMORA_ALLOW_OPERATOR_TOKEN=1 in cinemora.env / the environment.
os.environ.setdefault("CINEMORA_ALLOW_OPERATOR_TOKEN", "0")
os.environ.setdefault(
    "CINEMORA_PUBLIC_BASE_URL", "https://cinemora-chi.vercel.app"
)

for _path in (
    os.environ["CINEMORA_MEDIA_ROOT"],
    os.path.dirname(os.environ["SOURCE_INTEL_DB"]),
    os.environ["HF_HOME"],
):
    os.makedirs(_path, exist_ok=True)

# 3. Run from the backend directory, like the Docker image does.
os.chdir(BACKEND)
sys.path.insert(0, BACKEND)

# 4. Database migrations (backend/migrations/env.py reads DATABASE_URL).
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

command.upgrade(Config(os.path.join(BACKEND, "alembic.ini")), "head")

# 5. Workers (B-03/B-04): CINEMORA_WORKER_COUNT real worker.py child
# processes, spawned and supervised by the shared WorkerPool from
# backend/supervise.py — the same code the Docker image runs. The
# child environment carries the count back to each worker, so the
# dispatcher's promotion cap, the admin display and the actual
# process count cannot diverge.
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from supervise import WorkerPool, build_child_env, worker_count_from_env  # noqa: E402

_worker_count = worker_count_from_env(os.environ)
_pool = WorkerPool(
    _worker_count,
    spawn=subprocess.Popen,
    env=build_child_env(os.environ, _worker_count),
    cwd=BACKEND,
)
_pool.start()
print(f"app.py: started {_worker_count} worker process(es)", flush=True)

_stop_monitor = threading.Event()


def _monitor_workers():
    while not _stop_monitor.wait(2.0):
        restarted = _pool.restart_dead()
        if restarted:
            print(
                f"app.py: restarted {restarted} dead worker process(es)",
                flush=True,
            )


_monitor_thread = threading.Thread(
    target=_monitor_workers, name="app-worker-monitor", daemon=True
)
_monitor_thread.start()

# 6. Serve. Workers live exactly as long as the web server: when
# uvicorn returns (or raises), the monitor stops and every worker is
# terminated before this process exits.
import uvicorn  # noqa: E402

_port = int(os.environ.get("SERVER_PORT") or os.environ.get("PORT") or "25585")
try:
    uvicorn.run("main:app", host="0.0.0.0", port=_port)
finally:
    _stop_monitor.set()
    _pool.terminate_all()
