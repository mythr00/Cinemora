"""Transformation Phase 2.4 — the second narration voice and
per-pack pace.

- The catalogue carries a real contrasting second voice
  (en_US-ryan-medium), installed and resolvable.
- assign_sentence_voices gives the secondary voice to quoted /
  interview beats ONLY; everything else keeps the primary voice,
  and when nothing qualifies the assignment is None (the exact
  single-voice path).
- A two-voice render at unit level (Piper stubbed at
  narration._run_piper with real WAV writing): parts are
  synthesised with their assigned voices, the parts manifest
  records each part's voice, and the joined narration probes.
- Loudness profile untouched: the delivery loudness target
  constant is unchanged.
"""

import json
import wave
from pathlib import Path

import pytest

from services import music
from services import narration


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

def test_secondary_voice_is_real_and_contrasting():
    assert narration.SECONDARY_VOICE_ID == "en_US-ryan-medium"
    assert narration.SECONDARY_VOICE_ID in narration.VOICE_CATALOGUE
    model, config, resolved, fell_back = narration.resolve_voice(
        narration.SECONDARY_VOICE_ID
    )
    assert resolved == narration.SECONDARY_VOICE_ID
    assert fell_back is False
    assert model.name == "en_US-ryan-medium.onnx"
    assert model != narration.PIPER_MODEL
    assert narration.secondary_voice_available() is True


def test_primary_voice_unchanged():
    model, _config, resolved, _fb = narration.resolve_voice(None)
    assert resolved == narration.DEFAULT_VOICE_ID
    assert model.name == "en_US-lessac-medium.onnx"


# ---------------------------------------------------------------------------
# Assignment: quoted / interview beats only
# ---------------------------------------------------------------------------

QUOTED_SCRIPT = (
    "The inquiry opened in the spring. "
    "The report was published a year later. "
    "The lead inspector told reporters: \"We found the beam "
    "already cut when we arrived on site.\""
)


def test_assignment_marks_only_the_quoted_sentence():
    voices = narration.assign_sentence_voices(QUOTED_SCRIPT)
    assert voices is not None
    assert len(voices) == 3
    assert voices[2] == narration.SECONDARY_VOICE_ID
    assert voices[0] == narration.DEFAULT_VOICE_ID
    assert voices[1] == narration.DEFAULT_VOICE_ID


def test_assignment_none_when_nothing_is_quoted():
    assert narration.assign_sentence_voices(
        "A plain narration sentence. Another plain sentence follows."
    ) is None


def test_assignment_honours_role_markers_and_primary_choice():
    sentences = [
        {"text": "The hearing resumed.", "role": "action"},
        {"text": "I saw the cable snap.", "role": "interview"},
    ]
    voices = narration.assign_sentence_voices(
        sentences, primary_voice_id="en_US-lessac-medium"
    )
    assert voices == [
        "en_US-lessac-medium", narration.SECONDARY_VOICE_ID,
    ]


def test_assignment_none_when_secondary_unavailable():
    assert narration.assign_sentence_voices(
        QUOTED_SCRIPT, secondary_available=False
    ) is None


# ---------------------------------------------------------------------------
# Two-voice render (synthesis stubbed, WAVs real)
# ---------------------------------------------------------------------------

def _write_tiny_wav(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\x00\x00" * 2205)  # 0.1s of silence


@pytest.fixture()
def fake_piper(monkeypatch):
    calls = []

    def fake_run_piper(text, output_file, voice_id=None):
        calls.append({"text": text, "voice_id": voice_id})
        _write_tiny_wav(output_file)

    monkeypatch.setattr(narration, "_run_piper", fake_run_piper)
    return calls


def test_two_voice_render_records_each_parts_voice(tmp_path, fake_piper):
    output = tmp_path / "narration.wav"
    voices = narration.assign_sentence_voices(QUOTED_SCRIPT)
    meta = narration.generate_narration(
        QUOTED_SCRIPT, str(output), sentence_voices=voices
    )
    assert output.exists()
    used = [c["voice_id"] for c in fake_piper]
    assert used[2] == narration.SECONDARY_VOICE_ID
    assert used[0] == narration.DEFAULT_VOICE_ID
    assert meta["voice_ids_used"] == sorted({
        narration.DEFAULT_VOICE_ID, narration.SECONDARY_VOICE_ID,
    })
    manifest = json.loads(
        (tmp_path / "narration_parts" / "parts_manifest.json").read_text()
    )
    part_voices = [p["voice_id"] for p in manifest["parts"]]
    assert part_voices[2] == narration.SECONDARY_VOICE_ID
    assert part_voices[0] == narration.DEFAULT_VOICE_ID
    # Every part carries a measured duration (Stage 5 contract).
    assert all(
        p["measured_duration_seconds"] > 0 for p in manifest["parts"]
    )


def test_single_voice_render_without_assignment(tmp_path, fake_piper):
    output = tmp_path / "narration.wav"
    meta = narration.generate_narration(
        "One plain sentence. And another one here.", str(output)
    )
    assert meta["voice_ids_used"] == [narration.DEFAULT_VOICE_ID]
    manifest = json.loads(
        (tmp_path / "narration_parts" / "parts_manifest.json").read_text()
    )
    assert all(
        p["voice_id"] == narration.DEFAULT_VOICE_ID
        for p in manifest["parts"]
    )


def test_no_speed_parameter_exists_in_the_tts_path(tmp_path, fake_piper):
    # Founder ruling (2026-11-22): voice speed adjustment is
    # removed end to end — the synthesis entry points accept no
    # speed/length-scale parameter at all, so a historical caller
    # passing one fails loudly instead of silently retiming.
    import inspect

    assert "speed" not in inspect.signature(
        narration.generate_narration
    ).parameters
    assert "speed" not in inspect.signature(
        narration._run_piper
    ).parameters
    with pytest.raises(TypeError):
        narration.generate_narration(
            "A technical sentence about load paths.",
            str(tmp_path / "narration.wav"),
            speed=0.94,
        )


# ---------------------------------------------------------------------------
# Loudness profile untouched
# ---------------------------------------------------------------------------

def test_loudness_target_constant_unchanged(monkeypatch):
    monkeypatch.delenv("CINEMORA_TARGET_LUFS", raising=False)
    # The -14 LUFS class delivery target the Phase 2.4 work must
    # not move (measured deliveries land at -14.9 LUFS class).
    assert music.target_lufs() == -14.0
