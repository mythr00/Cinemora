#!/usr/bin/env bash
# Provision the Kokoro-82M narration model (kokoro-v1.0.onnx,
# 311 MB) beside this script, from the pinned kokoro-onnx
# model-files-v1.0 release, with SHA-256 verification.
#
# Why a fetch script: the model exceeds GitHub's 100 MB per-file
# limit, so — unlike the Piper voice models and the Kokoro voices
# file (voices-v1.0.bin, 28 MB, tracked) — it is NOT in git. Run
# this once per fresh checkout / deployment host; the Docker
# image performs the same verified download at build time (see
# Dockerfile). Idempotent: an existing file with the right
# checksum is left untouched.
set -euo pipefail

cd "$(dirname "$0")"

MODEL="kokoro-v1.0.onnx"
MODEL_URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
MODEL_SHA256="7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"

VOICES="voices-v1.0.bin"
VOICES_URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
VOICES_SHA256="bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"

fetch_verified() {
    local file="$1" url="$2" sha="$3"
    if [ -f "$file" ] && echo "$sha  $file" | sha256sum -c - >/dev/null 2>&1; then
        echo "$file: already present, checksum OK"
        return 0
    fi
    echo "$file: downloading from $url"
    curl -fsSL -o "$file.tmp" "$url"
    echo "$sha  $file.tmp" | sha256sum -c -
    mv "$file.tmp" "$file"
    echo "$file: installed, checksum OK"
}

fetch_verified "$MODEL" "$MODEL_URL" "$MODEL_SHA256"
# The voices file is git-tracked; fetch only if a checkout lacks it.
if [ ! -f "$VOICES" ]; then
    fetch_verified "$VOICES" "$VOICES_URL" "$VOICES_SHA256"
else
    echo "$VOICES: already present (git-tracked)"
fi
