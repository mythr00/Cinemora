"""Stage 3H (music honesty + mixing) and 3J (voice catalogue).

3H: prepare_soundtrack returns an honest record — mixed/absent,
source basename, measured integrated loudness — and music is
audibly present under narration gaps. Also: asset_report emits the
soundtrack record verbatim under a top-level "soundtrack" key.

3J: the voice catalogue contains ONLY the voices actually
installed (exactly one: en_US-lessac-medium). _run_piper receives
the resolved model; unknown ids fall back honestly and share parts
with the default voice.
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
    # Exactly TWO real voice models are installed in backend/
    # (Phase 2.4 added en_US-ryan-medium as the secondary voice).
    assert set(narration.VOICE_CATALOGUE) == {
        "default", "en_US-lessac-medium", "en_US-ryan-medium",
    }
    for voice_id, (model, config) in narration.VOICE_CATALOGUE.items():
        assert Path(model).exists()
        assert Path(config).exists()
        if voice_id != "default":
            assert Path(model).name == f"{voice_id}.onnx"


def test_resolve_voice_known_and_unknown():
    model, config, resolved, fell_back = narration.resolve_voice(
        "en_US-lessac-medium"
    )
    assert resolved == "en_US-lessac-medium"
    assert fell_back is False

    model2, config2, resolved2, fell_back2 = narration.resolve_voice(
        "totally-bogus-voice"
    )
    assert resolved2 == "default"
    assert fell_back2 is True
    assert model2 == narration.PIPER_MODEL

    _, _, resolved3, fell_back3 = narration.resolve_voice(None)
    assert resolved3 == "default"
    assert fell_back3 is False


class _CapturedPiper:
    """Replaces subprocess.run inside narration._run_piper: records
    the command, writes a real tiny WAV to the -f target.

    Stage 5 (R4): generate_narration now also probes each part
    through media_probe (ffprobe) to record its measured duration,
    and the patch target is the shared subprocess module — so
    non-Piper commands are delegated to the real subprocess.run
    instead of being mistaken for Piper invocations."""

    def __init__(self):
        self.commands = []
        self._real_run = subprocess.run

    def __call__(self, command, **kwargs):
        if "piper" not in command:
            return self._real_run(command, **kwargs)
        self.commands.append(list(command))
        out = Path(command[command.index("-f") + 1])
        with wave.open(str(out), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(22050)
            wav.writeframes(b"\x00\x00" * 500)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def models_used(self):
        models = []
        for command in self.commands:
            piper_at = command.index("piper")
            models.append(command[piper_at + 2])  # arg after "-m"
        return models


@pytest.fixture
def captured_piper(monkeypatch):
    fake = _CapturedPiper()
    monkeypatch.setattr(narration.subprocess, "run", fake)
    return fake


def test_catalogue_voice_reaches_piper_by_name(tmp_path, captured_piper):
    out = tmp_path / "narration.wav"
    meta = generate_narration(
        text="One sentence only.",
        output_path=str(out),
        voice_id="en_US-lessac-medium",
    )
    assert captured_piper.commands, "piper was never invoked"
    expected = str(narration.VOICE_CATALOGUE["en_US-lessac-medium"][0])
    assert all(m == expected for m in captured_piper.models_used())
    assert meta["voice_id_requested"] == "en_US-lessac-medium"
    assert meta["voice_id_used"] == "en_US-lessac-medium"
    assert meta["voice_fallback"] is False
    assert meta["model"] == "en_US-lessac-medium.onnx"


def test_bogus_voice_falls_back_to_default_model(tmp_path, captured_piper):
    out = tmp_path / "narration.wav"
    meta = generate_narration(
        text="One sentence only.",
        output_path=str(out),
        voice_id="no-such-voice",
    )
    assert meta["voice_id_requested"] == "no-such-voice"
    assert meta["voice_id_used"] == "default"
    assert meta["voice_fallback"] is True
    assert meta["model"] == "en_US-lessac-medium.onnx"
    expected = str(narration.VOICE_CATALOGUE["default"][0])
    assert all(m == expected for m in captured_piper.models_used())


def test_part_hash_shared_after_resolution():
    bogus = narration._part_hash("Hello world.", voice_id="no-such-voice")
    default = narration._part_hash("Hello world.", voice_id="default")
    none_voice = narration._part_hash("Hello world.", voice_id=None)
    assert bogus == default == none_voice
    # A different sentence still hashes differently.
    assert narration._part_hash("Other sentence.", voice_id="default") != default
