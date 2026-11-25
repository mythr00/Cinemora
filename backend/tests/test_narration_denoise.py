"""Narration denoise prototype (GitHub Skills Programme, Lane 4).

The measured A/B on synthetic material: a tone-burst "voice" over
a white-noise floor. The afftdn pass must lower the noise floor in
the gaps materially while leaving the voice segments' level
essentially intact — that asymmetry is the whole point of a
conservative setting. Plus the config discipline: off by default,
option beats env, and the soundtrack record carries the fact.
"""

import math
import wave

import numpy as np
import pytest

from services import music

SR = 48000


def _synthetic_noisy_voice(path):
    """4s: tone bursts 0-1s and 2-3s; noise everywhere (-30 dBFS)."""
    rng = np.random.default_rng(7)
    t = np.arange(SR * 4) / SR
    noise = rng.normal(0, 0.03, len(t))
    tone = 0.25 * np.sin(2 * np.pi * 440 * t)
    voice = np.where(((t < 1) | ((t >= 2) & (t < 3))), tone, 0.0)
    signal = np.clip(voice + noise, -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((signal * 32767).astype(np.int16).tobytes())
    return path


def _read(path):
    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float64) / 32768.0


def _rms_db(samples):
    rms = float(np.sqrt(np.mean(samples ** 2))) or 1e-12
    return 20 * math.log10(rms)


def _gap_and_voice_db(path):
    samples = _read(path)
    gap = samples[int(1.2 * SR): int(1.9 * SR)]     # noise only
    voice = samples[int(0.2 * SR): int(0.9 * SR)]   # tone + noise
    return _rms_db(gap), _rms_db(voice)


def test_denoise_lowers_noise_floor_and_preserves_voice(tmp_path):
    source = _synthetic_noisy_voice(tmp_path / "noisy.wav")
    cleaned = tmp_path / "cleaned.wav"
    gap_before, voice_before = _gap_and_voice_db(source)
    music.denoise_audio_file(source, cleaned)
    gap_after, voice_after = _gap_and_voice_db(cleaned)
    # The noise floor drops materially (>= 4 dB)...
    assert gap_before - gap_after >= 4.0, (gap_before, gap_after)
    # ...while the voice segment's level is essentially intact.
    assert abs(voice_after - voice_before) <= 2.0, (voice_before, voice_after)


def test_config_resolution_default_off_option_and_env(monkeypatch):
    monkeypatch.delenv("CINEMORA_DENOISE_NARRATION", raising=False)
    assert music.resolve_mix_config()["denoise_narration"] is False
    assert music.resolve_mix_config(
        options={"denoise_narration": True})["denoise_narration"] is True
    monkeypatch.setenv("CINEMORA_DENOISE_NARRATION", "1")
    assert music.resolve_mix_config()["denoise_narration"] is True
    # The explicit option still wins over the env.
    assert music.resolve_mix_config(
        options={"denoise_narration": False})["denoise_narration"] is False


def test_soundtrack_record_carries_denoise_fact(tmp_path):
    narration = _synthetic_noisy_voice(tmp_path / "narration.wav")
    out = tmp_path / "soundtrack.wav"
    config = music.resolve_mix_config(options={"denoise_narration": True})
    record = music.prepare_soundtrack(
        narration, out, mix_config=config,
    )
    assert out.exists()
    denoise = record["mix"]["denoise"]
    assert denoise["applied"] is True
    assert denoise["stage"] == "narration_pre_mix"
    # The filter's nf was set from THIS file's measured floor
    # (the synthetic noise sits at about -30 dBFS).
    assert denoise["measured_floor_db"] is not None
    assert -40.0 < denoise["measured_floor_db"] < -20.0
    assert denoise["filter"].startswith("afftdn=nr=12:nf=")


def test_soundtrack_record_denoise_off_by_default(tmp_path):
    narration = _synthetic_noisy_voice(tmp_path / "narration.wav")
    out = tmp_path / "soundtrack.wav"
    record = music.prepare_soundtrack(narration, out)
    assert record["mix"]["denoise"]["applied"] is False
