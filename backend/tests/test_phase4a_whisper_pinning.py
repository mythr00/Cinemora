"""Phase 4a (D-04) — Whisper weights are pinned, not a first-use
surprise download.

services.captions previously built ``WhisperModel("base")`` with no
revision: a fresh deployment downloaded whatever the Hugging Face
repo's default branch pointed at that day. The loader must now
receive the configured model id + revision — the weights the local
stack actually resolved, verified 2026-11-25 from the local HF
cache (``models--Systran--faster-whisper-base/refs/main`` and its
snapshot directory name agree).

The loader is mocked throughout: no model is downloaded or loaded
in these tests.
"""

import re
from pathlib import Path

from services import captions

BACKEND_DIR = Path(__file__).resolve().parent.parent

EXPECTED_MODEL_ID = "Systran/faster-whisper-base"
EXPECTED_REVISION = "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"


def test_whisper_pin_defaults_are_the_verified_local_weights():
    assert captions.WHISPER_MODEL_ID == EXPECTED_MODEL_ID
    assert captions.WHISPER_MODEL_REVISION == EXPECTED_REVISION
    # A revision pin is only deterministic if it is a commit hash,
    # not a movable branch/tag name.
    assert re.fullmatch(r"[0-9a-f]{40}", captions.WHISPER_MODEL_REVISION)


def test_loader_receives_the_configured_model_and_revision(monkeypatch):
    import faster_whisper

    captured = {}

    class FakeModel:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    monkeypatch.setattr(captions, "_WHISPER_MODEL", None)

    model = captions._get_whisper_model()

    assert isinstance(model, FakeModel)
    assert captured["args"] == (captions.WHISPER_MODEL_ID,)
    assert captured["kwargs"]["revision"] == captions.WHISPER_MODEL_REVISION
    # The rest of the loader contract is unchanged (D-04 is a
    # pinning change, not a behaviour change).
    assert captured["kwargs"]["device"] == captions.DEVICE
    assert captured["kwargs"]["compute_type"] == captions.COMPUTE_TYPE
    monkeypatch.setattr(captions, "_WHISPER_MODEL", None)


def test_whisper_pin_is_documented_in_env_example():
    text = (BACKEND_DIR / ".env.example").read_text(encoding="utf-8")
    assert f"CINEMORA_WHISPER_MODEL={EXPECTED_MODEL_ID}" in text
    assert f"CINEMORA_WHISPER_REVISION={EXPECTED_REVISION}" in text
