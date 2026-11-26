"""Stage 3H (music honesty + mixing) and 3J (voice catalogue).

3H: prepare_soundtrack returns an honest record — mixed/absent,
source basename, measured integrated loudness — and music is
audibly present under narration gaps. Also: asset_report emits the
soundtrack record verbatim under a top-level "soundtrack" key.

3J: the voice catalogue contains ONLY the voices actually
installed (Kokoro-82M's genuine 54-voice pack plus the "default"
alias, since the 2026-11-26 engine switch; before it, two Piper
voices). The synthesis seam receives the requested voice;
unknown ids fall back honestly and share parts with the default
voice.
"""

import math
import re
import struct
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import music, narration
from services.asset_report import build_asset_report
from services.narration import generate_narration


# ---------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------

def _narration_with_gap(path, rate=22050):
    """4s narration: tone 0-1.5s, silence 1.5-2.5s, tone 2.5-4s."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = b""
        for i in range(rate * 4):
            t = i / rate
            value = (
                int(0.5 * 32767 * math.sin(2 * math.pi * 440 * t))
                if (t < 1.5 or t >= 2.5)
                else 0
            )
            frames += struct.pack("<h", value)
        wav.writeframes(frames)
    return path


@pytest.fixture(scope="module")
def music_file(tmp_path_factory):
    out = tmp_path_factory.mktemp("music") / "tone_music.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=3:sample_rate=48000",
            str(out),
        ],
        check=True, timeout=60,
    )
    return out


def _gap_rms_db(path, start=1.6, length=0.8):
    result = subprocess.run(
        [
            "ffmpeg", "-ss", str(start), "-t", str(length),
            "-i", str(path),
            "-af", "astats=measure_overall=1",
            "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=60,
    )
    matches = re.findall(r"RMS level dB:\s*(-?[\d.]+|-inf)", result.stderr)
    if not matches:
        return -120.0
    value = matches[-1]
    return -120.0 if value == "-inf" else float(value)


# ---------------------------------------------------------------
# 3H: prepare_soundtrack record + mixing
# ---------------------------------------------------------------

def test_prepare_soundtrack_mixed_record_and_loudness(tmp_path, music_file):
    narration_file = _narration_with_gap(tmp_path / "narration.wav")
    out = tmp_path / "soundtrack.wav"
    record = music.prepare_soundtrack(
        str(narration_file), str(out), music_file=str(music_file)
    )
    assert out.exists()
    assert record["music"] == "mixed"
    assert record["source"] == "tone_music.wav"
    assert isinstance(record["loudness_lufs"], float)
    # Legacy keys preserved.
    assert record["status"] == "soundtrack_created"
    assert record["output_file"] == str(out)


def test_music_present_in_narration_gap(tmp_path, music_file):
    narration_file = _narration_with_gap(tmp_path / "narration.wav")
    mixed = tmp_path / "mixed.wav"
    music.prepare_soundtrack(
        str(narration_file), str(mixed), music_file=str(music_file)
    )
    plain = tmp_path / "plain.wav"
    music.prepare_soundtrack(str(narration_file), str(plain))

    mixed_gap = _gap_rms_db(mixed)
    plain_gap = _gap_rms_db(plain)
    # The gap is silent narration: music must be measurable there,
    # and clearly above the no-music soundtrack's (silent) gap.
    assert mixed_gap > -70.0
    assert mixed_gap > plain_gap + 10.0


def test_prepare_soundtrack_absent_record(tmp_path):
    narration_file = _narration_with_gap(tmp_path / "narration.wav")
    out = tmp_path / "soundtrack.wav"
    record = music.prepare_soundtrack(str(narration_file), str(out))
    assert out.exists()
    assert record["music"] == "absent"
    assert record["source"] is None
    assert isinstance(record["loudness_lufs"], float)


def test_prepare_soundtrack_missing_music_file_is_absent(tmp_path):
    narration_file = _narration_with_gap(tmp_path / "narration.wav")
    out = tmp_path / "soundtrack.wav"
    record = music.prepare_soundtrack(
        str(narration_file), str(out), music_file=str(tmp_path / "nope.mp3")
    )
    assert record["music"] == "absent"
    # Stage H (M4/D8) — deliberate update of the Stage 3H pin: a
    # REQUESTED-but-missing file is now distinguishable from
    # never-requested. (Callers that request a file pass
    # requested=True; this bare call keeps the legacy default.)
    assert record["state"] == "requested_but_missing"
    assert record["requested_file"].endswith("nope.mp3")
    assert record["identity"] is None


def test_asset_report_soundtrack_key():
    record = {"music": "mixed", "source": "tone.mp3", "loudness_lufs": -14.0}
    report = build_asset_report([], soundtrack=record)
    assert report["soundtrack"] == record
    # Default keeps old behaviour for other callers.
    assert build_asset_report([])["soundtrack"] is None


def test_measure_loudness_returns_none_on_missing_file(tmp_path):
    assert music.measure_loudness_lufs(tmp_path / "missing.wav") is None


# ---------------------------------------------------------------
# 3J: voice catalogue
# ---------------------------------------------------------------

def test_catalogue_contains_only_installed_voices():
    # Kokoro switch (2026-11-26): the catalogue is the genuine
    # Kokoro v1.0 pack — 54 voices — plus the "default" alias.
    # Every entry is backed by the shared installed assets (one
    # model + one voices file). The retired Piper voices are NOT
    # in the customer catalogue (dataset-licence defect).
    assert set(narration.VOICE_CATALOGUE) == (
        set(narration.KOKORO_VOICE_IDS) | {"default"}
    )
    assert len(narration.VOICE_CATALOGUE) == 55
    assert "en_US-lessac-medium" not in narration.VOICE_CATALOGUE
    assert "en_US-ryan-medium" not in narration.VOICE_CATALOGUE
    for voice_id, (model, config) in narration.VOICE_CATALOGUE.items():
        assert Path(model).exists()
        assert Path(config).exists()
        assert Path(model).name == "kokoro-v1.0.onnx"
        assert Path(config).name == "voices-v1.0.bin"


def test_resolve_voice_known_and_unknown():
    model, config, resolved, fell_back = narration.resolve_voice(
        "af_heart"
    )
    assert resolved == "af_heart"
    assert fell_back is False

    model2, config2, resolved2, fell_back2 = narration.resolve_voice(
        "totally-bogus-voice"
    )
    assert resolved2 == "default"
    assert fell_back2 is True
    assert model2 == narration.KOKORO_MODEL

    _, _, resolved3, fell_back3 = narration.resolve_voice(None)
    assert resolved3 == "default"
    assert fell_back3 is False


class _CapturedKokoro:
    """Replaces narration._run_kokoro: records each call (the
    requested voice and the canonical voice it resolves to) and
    writes a real tiny WAV to the output target, so
    generate_narration's measured-duration probing (Stage 5, R4)
    runs against a genuine file."""

    def __init__(self):
        self.calls = []

    def __call__(self, text, output_file, voice_id=None):
        self.calls.append(
            {
                "text": text,
                "voice_id": voice_id,
                "canonical": narration.canonical_voice_id(voice_id),
            }
        )
        with wave.open(str(output_file), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(b"\x00\x00" * 2400)


@pytest.fixture
def captured_kokoro(monkeypatch):
    fake = _CapturedKokoro()
    monkeypatch.setattr(narration, "_run_kokoro", fake)
    return fake


def test_catalogue_voice_reaches_engine_by_name(tmp_path, captured_kokoro):
    out = tmp_path / "narration.wav"
    meta = generate_narration(
        text="One sentence only.",
        output_path=str(out),
        voice_id="af_heart",
    )
    assert captured_kokoro.calls, "the Kokoro engine was never invoked"
    assert all(
        c["canonical"] == "af_heart" for c in captured_kokoro.calls
    )
    assert meta["engine"] == "kokoro"
    assert meta["voice_id_requested"] == "af_heart"
    assert meta["voice_id_used"] == "af_heart"
    assert meta["voice_fallback"] is False
    assert meta["model"] == "kokoro-v1.0.onnx"


def test_bogus_voice_falls_back_to_default_model(tmp_path, captured_kokoro):
    out = tmp_path / "narration.wav"
    meta = generate_narration(
        text="One sentence only.",
        output_path=str(out),
        voice_id="no-such-voice",
    )
    assert meta["voice_id_requested"] == "no-such-voice"
    assert meta["voice_id_used"] == "default"
    assert meta["voice_fallback"] is True
    assert meta["model"] == "kokoro-v1.0.onnx"
    # The engine synthesised with the default's canonical voice.
    assert all(
        c["canonical"] == narration.PRIMARY_VOICE_ID
        for c in captured_kokoro.calls
    )


def test_part_hash_shared_after_resolution():
    bogus = narration._part_hash("Hello world.", voice_id="no-such-voice")
    default = narration._part_hash("Hello world.", voice_id="default")
    none_voice = narration._part_hash("Hello world.", voice_id=None)
    assert bogus == default == none_voice
    # A different sentence still hashes differently.
    assert narration._part_hash("Other sentence.", voice_id="default") != default
