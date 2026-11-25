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

# 5. Serve.
import uvicorn  # noqa: E402

_port = int(os.environ.get("SERVER_PORT") or os.environ.get("PORT") or "25585")
uvicorn.run("main:app", host="0.0.0.0", port=_port)
