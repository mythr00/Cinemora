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
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgomp1 \
        ca-certificates \
        curl \
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

# Persistent-data locations. Altare replaces everything outside the app's
# data folder on redeploy, so every writable path is steered under /data:
# production media, the source-intel SQLite cache, the faster-whisper
# model cache ("base", downloaded from Hugging Face on first use) and TMP.
# VERIFY at deploy time that /data is the app's persistent folder and is
# writable by uid 10001 (see DEPLOY_STEPS.md steps 6 and 9); if Altare's
# data folder lives elsewhere, override these four variables in the app
# environment instead of rebuilding.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CINEMORA_MEDIA_ROOT=/data/media \
    SOURCE_INTEL_DB=/data/source-intel/source_intel.sqlite3 \
    HF_HOME=/data/hf-cache \
    TMPDIR=/data/tmp

RUN mkdir -p /data/media /data/tmp /data/hf-cache /data/source-intel \
    && chown -R cinemora:cinemora /data /app

USER cinemora

# Default process = the web API. Altare sets $PORT; the Procfile carries
# the same command (plus the migration step) and wins where Altare reads
# the Procfile, so keep the two in sync.
CMD ["sh", "-c", "exec python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
