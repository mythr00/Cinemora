"""Phase 5a (B-01 / C-03) — caption word-timing root cause + fix.

ROOT CAUSE (reproduced against the installed stack,
faster-whisper 1.2.1 x PyAV 19.0.2): faster-whisper's
``decode_audio`` calls ``av.open(input, metadata_errors="ignore")``
and PyAV 19's ``av.open`` rejects the ``metadata_errors`` keyword —
``TypeError: open() got an unexpected keyword argument
'metadata_errors'``. EVERY non-array input form (path string AND
open file object) routes through that decode, so
``model.transcribe(str(path))`` — the call services/captions made —
could never produce timings on this stack; build_captions caught
the exception and silently degraded every production to
proportional/provisional captions. The numpy-array input form
bypasses decode_audio entirely and works (the workaround
services/transcription already used; captions never received it).

The fix: captions decodes the file itself (PyAV directly, the same
s16/mono/16 kHz decode transcription uses — now implemented once,
in captions, and shared) and hands the model the float32 array.
The path form remains only as a fallback for a stack where the
array decode itself fails. The result records which input form
produced the timings ("timing_source") and a structured log line
says the same.

Verification level: REAL word-level timestamps from the REAL
pinned model on the tracked speech fixture (backend/test.wav —
"This is a test of the documentary narration system."), composited
at known offsets (speech + 2.0 s silence + speech), plus mocked-
model unit tests pinning the input form. The real-model test
skips when the pinned weights are not already in the local HF
cache (it never downloads).
"""

import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from services import captions

BACKEND_DIR = Path(__file__).resolve().parent.parent
SPEECH_FIXTURE = BACKEND_DIR / "test.wav"


# ---------------------------------------------------------------------
# Root-cause probe, encoded: the pinned stack rejects path decode.
# ---------------------------------------------------------------------


def _pinned_stack():
    import importlib.metadata as metadata

    try:
        return (
            metadata.version("faster-whisper") == "1.2.1"
            and metadata.version("av") == "19.0.2"
        )
    except Exception:
        return False


@pytest.mark.skipif(
    not _pinned_stack(),
    reason="root-cause probe is specific to the pinned "
    "faster-whisper 1.2.1 x PyAV 19.0.2 stack",
)
def test_path_decode_is_rejected_on_the_pinned_stack(tmp_path):
    """faster-whisper's own decode_audio (the path/file-object
    route) raises the metadata_errors TypeError on this stack —
    the exact failure that silently degraded captions."""
    from faster_whisper.audio import decode_audio

    wav = tmp_path / "tone.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 16000)

    with pytest.raises(TypeError, match="metadata_errors"):
        decode_audio(str(wav))
    with open(wav, "rb") as handle:
        with pytest.raises(TypeError, match="metadata_errors"):
            decode_audio(handle)


# ---------------------------------------------------------------------
# Mocked-model unit tests: the working input form is what is passed.
# ---------------------------------------------------------------------


class _FakeWord:
    def __init__(self, word, start, end):
        self.word = word
        self.start = start
        self.end = end


class _FakeModel:
    """Records the exact object transcribe() was called with."""

    def __init__(self):
        self.received = None

    def transcribe(self, audio, **kwargs):
        self.received = audio
        assert kwargs.get("word_timestamps") is True
        segment = SimpleNamespace(
            start=0.0,
            end=1.0,
            text=" Hello world",
            words=[
                _FakeWord(" Hello", 0.0, 0.4),
                _FakeWord(" world", 0.4, 1.0),
            ],
        )
        info = SimpleNamespace(
            language="en", language_probability=0.99, duration=1.0
        )
        return iter([segment]), info


def _install_fake_model(monkeypatch):
    model = _FakeModel()
    monkeypatch.setattr(captions, "_get_whisper_model", lambda: model)
    return model


def test_transcribe_audio_passes_a_decoded_array_not_a_path(
    monkeypatch, tmp_path
):
    audio = tmp_path / "narration.wav"
    audio.write_bytes(b"not-really-audio")
    model = _install_fake_model(monkeypatch)
    sentinel = np.zeros(16000, dtype=np.float32)
    monkeypatch.setattr(
        captions, "_decode_audio_array", lambda path: sentinel
    )

    result = captions.transcribe_audio(
        str(audio), str(tmp_path / "captions.json")
    )

    # The model received the decoded float32 array — NOT the path
    # string the broken stack rejects.
    assert isinstance(model.received, np.ndarray)
    assert model.received is sentinel
    assert result["timing_source"] == "faster_whisper_array_decode"
    assert result["segments"][0]["words"] == [
        {"word": "Hello", "start": 0.0, "end": 0.4},
        {"word": "world", "start": 0.4, "end": 1.0},
    ]


def test_transcribe_audio_falls_back_to_path_when_decode_fails(
    monkeypatch, tmp_path
):
    """The fallback chain survives for genuine failures: when the
    array decode itself cannot run, the legacy path form is tried
    and the result honestly records that it produced the timings."""
    audio = tmp_path / "narration.wav"
    audio.write_bytes(b"not-really-audio")
    model = _install_fake_model(monkeypatch)

    def broken_decode(path):
        raise RuntimeError("no audio stream")

    monkeypatch.setattr(captions, "_decode_audio_array", broken_decode)

    result = captions.transcribe_audio(
        str(audio), str(tmp_path / "captions.json")
    )

    assert model.received == str(audio)
    assert result["timing_source"] == "faster_whisper_path"
    assert result["segments"], "fallback must still yield segments"


def test_decode_is_shared_with_transcription():
    """One decode implementation: transcription's helper produces
    exactly the captions decode for the same file (no divergent
    second copy to drift again — the divergence is what caused
    B-01)."""
    from services import transcription

    via_captions = captions._decode_audio_array(SPEECH_FIXTURE)
    via_transcription = transcription._decode_audio_array(SPEECH_FIXTURE)
    assert via_captions.dtype == np.float32
    assert np.array_equal(via_captions, via_transcription)


# ---------------------------------------------------------------------
# Real model, real word timings at known offsets.
# ---------------------------------------------------------------------


def _model_cached():
    import os

    hf_home = os.environ.get("HF_HOME")
    roots = [Path(hf_home)] if hf_home else [
        Path.home() / ".cache" / "huggingface"
    ]
    return any(
        (root / "hub" / "models--Systran--faster-whisper-base").is_dir()
        for root in roots
    )


requires_cached_model = pytest.mark.skipif(
    not (SPEECH_FIXTURE.is_file() and _model_cached()),
    reason="pinned Whisper weights not in the local HF cache "
    "or speech fixture missing; refusing to download in tests",
)


def _composite_wav(path):
    """speech + 2.0 s of silence + speech, at the fixture's own
    sample rate. Returns (speech_seconds, gap_seconds)."""
    with wave.open(str(SPEECH_FIXTURE), "rb") as source:
        params = source.getparams()
        frames = source.readframes(source.getnframes())
    speech_seconds = params.nframes / params.framerate
    gap_seconds = 2.0
    silence = b"\x00\x00" * int(params.framerate * gap_seconds)
    with wave.open(str(path), "wb") as out:
        out.setparams(params)
        out.writeframes(frames + silence + frames)
    return speech_seconds, gap_seconds


@requires_cached_model
def test_real_model_word_timestamps_at_known_offsets(tmp_path):
    """The primary path on the real pinned model produces REAL
    word-level timestamps: words from the first speech copy land
    early, words from the second copy land after
    speech + gap (within a generous tolerance)."""
    wav = tmp_path / "composite.wav"
    speech_seconds, gap_seconds = _composite_wav(wav)
    second_copy_start = speech_seconds + gap_seconds

    result = captions.transcribe_audio(
        str(wav), str(tmp_path / "captions.json")
    )

    assert result["timing_source"] == "faster_whisper_array_decode"
    words = [
        word
        for segment in result["segments"]
        for word in segment["words"]
    ]
    assert len(words) >= 10, f"expected real words, got {words!r}"
    starts = [word["start"] for word in words]
    assert min(starts) < 1.0, "first copy's words must start early"
    assert max(starts) > second_copy_start - 0.5, (
        "second copy's words must land after the silence gap "
        f"(expected some start > {second_copy_start - 0.5}, "
        f"got max {max(starts)})"
    )
    # Word timings are monotonic and inside the audio duration.
    assert starts == sorted(starts)
    assert max(word["end"] for word in words) <= result["duration"] + 0.5
