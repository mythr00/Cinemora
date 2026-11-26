#!/bin/sh
# Cinemora backend entrypoint (Gate 2 field fix, 2026-11-26).
#
# Field failure: on the codespace Docker deployment the /data
# volume arrived owned by uid 1000 while the app runs as uid
# 10001 (cinemora), so run_full_pipeline died instantly with
# PermissionError creating the project directory under the media
# root. The image's build-time `chown -R cinemora /data` cannot
# help: a mounted volume's ownership replaces the image's.
#
# This entrypoint starts as root, guarantees every writable path
# the image steers under /data exists and is owned by the app
# user, then drops privileges with gosu and execs the real
# command (default: python supervise.py) AS the app user — the
# processes that run are unchanged, only their starting
# filesystem is repaired first.
#
# When the container is started as a non-root user directly
# (e.g. a host that forces the image's app uid), there is nothing
# to chown and the command simply runs as that user, exactly as
# before this entrypoint existed.
set -e

APP_USER="cinemora"
MEDIA_ROOT="${CINEMORA_MEDIA_ROOT:-/data/media}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$MEDIA_ROOT"
    # The media root is the field-proven failure point: repair it
    # recursively so pre-existing project directories created
    # under a different owner become writable too, not just the
    # root itself.
    chown -R "$APP_USER:$APP_USER" "$MEDIA_ROOT"
    # The other /data-steered writable paths (Dockerfile ENV):
    # their parent directories only — their contents are created
    # by the app itself once the parents are writable.
    for extra in \
        "$(dirname "${SOURCE_INTEL_DB:-/data/source-intel/source_intel.sqlite3}")" \
        "${HF_HOME:-/data/hf-cache}" \
        "${TMPDIR:-/data/tmp}"; do
        mkdir -p "$extra"
        chown "$APP_USER:$APP_USER" "$extra"
    done
    exec gosu "$APP_USER" "$@"
fi

exec "$@"
