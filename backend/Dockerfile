# Cinemora backend — production image for Altare (linux/amd64, non-root).
#
# BUILD CONTEXT = the repo's backend/ directory (this file is designed to be
# copied to backend/Dockerfile at deploy time, together with .dockerignore
# from this folder — see DEPLOY_STEPS.md step 3):
#
#   cd ~/workspace/cinemora/backend
#   cp ~/workspace/cinemora-audit/altare-deploy/Dockerfile .
#   cp ~/workspace/cinemora-audit/altare-deploy/.dockerignore .
#   docker build --platform linux/amd64 -t ghcr.io/mythr00/cinemora-backend:<tag> .
#
# Python 3.12.3 matches the verified development venv (cinemora-venv2) and
# the pinned requirements.txt header ("Verified on Python 3.12.3").

FROM python:3.12.3-slim-bookworm

# System dependencies:
# - ffmpeg + ffprobe: required on PATH by the pipeline (requirements.txt
#   header states this explicitly; PyAV's bundled libs do NOT cover the
#   pipeline's ffmpeg subprocess calls).
# - libgomp1: OpenMP runtime needed by onnxruntime / ctranslate2 wheels.
# - ca-certificates + curl: TLS for provider APIs; curl for healthchecks.
# - Manim (GitHub Skills Programme, 2026-11-25): manimpango + pycairo
#   build from source (no manylinux wheels) -> gcc, pkg-config and the
#   cairo/pango dev packages; the dev packages also pull the runtime
#   libcairo2 / libpango libraries the Manim templates link at render
#   time. fonts-dejavu-core is belt-and-braces (the templates point
#   fontconfig at the bundled DejaVu in assets/fonts regardless).
#   A multi-stage build could shed the toolchain from the final
#   image; the single-stage layout is retained deliberately (E-28).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgomp1 \
        ca-certificates \
        curl \
        gcc \
        pkg-config \
        libcairo2-dev \
        libpango1.0-dev \
        fonts-dejavu-core \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user (Altare requires non-root images).
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin cinemora

WORKDIR /app

# Dependencies first (layer caching). requirements.txt is the repo's pinned,
# verified set; it includes the test tooling (pytest, fakeredis) — accepted
# for the spike, slimming it is a later optimisation.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code. The build context's .dockerignore excludes media/
# (runtime data, 6.2 GB locally, gitignored in the repo), tests/ and caches.
# The two Piper voice models ARE included on purpose: they live at the
# backend root (en_US-lessac-medium.onnx, en_US-ryan-medium.onnx, ~63 MB
# each, git-tracked) and services/narration.py resolves them relative to
# the backend directory, so they must sit beside the code in the image.
COPY . ./

# Kokoro-82M narration assets (production engine since 2026-11-26):
# voices-v1.0.bin (28 MB) is git-tracked and arrives with COPY above,
# but kokoro-v1.0.onnx (311 MB) exceeds GitHub's 100 MB per-file limit
# and is NOT in the repo — fetch it from the pinned kokoro-onnx
# release with SHA-256 verification (same values as
# backend/fetch_kokoro_model.sh, which provisions local checkouts).
RUN curl -fsSL -o kokoro-v1.0.onnx \
      https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx \
    && echo "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5  kokoro-v1.0.onnx" | sha256sum -c -

# Persistent-data locations. Altare replaces everything outside the app's
# data folder on redeploy, so every writable path is steered under /data:
# production media, the source-intel SQLite cache, the faster-whisper
# model cache ("base", downloaded from Hugging Face on first use) and TMP.
# VERIFY at deploy time that /data is the app's persistent folder and is
# writable by uid 10001 (see DEPLOY_STEPS.md steps 6 and 9); if Altare's
# data folder lives elsewhere, override these four variables in the app
# environment instead of rebuilding.
# Security defaults (E-21/B-05/B-07): Secure session cookies ON and
# the operator-token dual-auth window CLOSED in the shipped image.
# A deployment may deliberately open the window by overriding
# CINEMORA_ALLOW_OPERATOR_TOKEN=1 in its own environment.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CINEMORA_COOKIE_SECURE=1 \
    CINEMORA_ALLOW_OPERATOR_TOKEN=0 \
    CINEMORA_SOURCE_EXTRACTION_ENABLED=1 \
    CINEMORA_MEDIA_ROOT=/data/media \
    SOURCE_INTEL_DB=/data/source-intel/source_intel.sqlite3 \
    HF_HOME=/data/hf-cache \
    TMPDIR=/data/tmp \
    CINEMORA_WORKER_COUNT=2 \
    CINEMORA_RENDER_CONCURRENCY=1 \
    CINEMORA_RUN_MIGRATIONS=1

RUN mkdir -p /data/media /data/tmp /data/hf-cache /data/source-intel \
    && chown -R cinemora:cinemora /data /app \
    && chmod +x /app/entrypoint.sh

# Gate 2 field fix (2026-11-26): NO `USER cinemora` here any more.
# A mounted /data volume replaces the image's ownership (the
# codespace deployment's volume arrived owned by uid 1000), and a
# non-root image cannot repair that at runtime — the app died
# with PermissionError on its first media write. The container
# therefore starts as root ONLY long enough for entrypoint.sh to
# create/chown the /data-steered writable paths, after which it
# drops to the cinemora user via gosu and execs the command: the
# web/worker processes run as uid 10001 exactly as before.
ENTRYPOINT ["/app/entrypoint.sh"]

# Default process = the deployment supervisor (B-03/B-04): it runs
# migrations once (CINEMORA_RUN_MIGRATIONS=1), then starts uvicorn
# plus CINEMORA_WORKER_COUNT worker processes (default 2), restarts
# dead workers, forwards signals, and exits if the web process dies.
# Altare sets $PORT, which the supervisor honours for uvicorn.
# Web-only (for hosts that scale web/workers as separate processes):
#   docker run ... python supervise.py --web-only
# or set CINEMORA_WEB_ONLY=1; workers-only: python supervise.py
# --workers-only (that is what the compose `worker` service runs).
CMD ["python", "supervise.py"]
