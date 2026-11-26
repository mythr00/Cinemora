"""Kokoro-82M narration engine (founder decision 2026-11-26).

- The customer catalogue is EXACTLY the genuine Kokoro v1.0
  voice pack installed on disk — a drift guard loads the voices
  file itself, so a padded, renamed or missing voice fails here.
- Voice/language facts derive from the pack: per-language
  defaults and same-language secondaries; French (one voice)
  has no secondary, by fact.
- The engine call pins speed 1.0 (founder rule: no narration
  speed adjustment anywhere) and passes the voice's own
  espeak language.
- Caller-side text normalisation: "St." before a capitalised
  name reads "Saint" (the investigation's pronunciation finding;
  misaki bracket overrides are NOT used — kokoro-onnx reads them
  aloud as gibberish).
- Voice/language coherence in production config: an English
  voice on a Spanish production is refused before charging; a
  voiceless-language production projects the language's own
  primary voice into the flat run options.
- Two REAL end-to-end syntheses through generate_narration
  (English primary voice, Spanish voice) with measured part
  durations — the contract timing/captions consume.
"""

from __future__ import annotations

import numpy as np
import pytest

from services import narration, production_config


def test_catalogue_matches_the_installed_voice_pack_exactly():
    pack_ids = set(
        np.load(str(narration.KOKORO_VOICES_FILE)).files
    )
    assert set(narration.KOKORO_VOICE_IDS) == pack_ids
    assert set(narration.VOICE_CATALOGUE) == pack_ids | {"default"}
    assert len(pack_ids) == 54


def test_voice_counts_by_language_are_the_packs_own():
    counts = {}
    for meta in narration.KOKORO_VOICE_META.values():
        counts[meta["language"]] = counts.get(meta["language"], 0) + 1
    assert counts == {
        "en": 28,  # 20 US + 8 UK
        "es": 3,
        "fr": 1,
        "hi": 4,
        "it": 2,
        "ja": 5,
        "pt": 3,
        "zh": 8,
    }


def test_language_defaults_and_secondaries():
    assert narration.default_voice_for_language("en") == "af_heart"
    assert narration.default_voice_for_language("es") == "ef_dora"
    assert narration.default_voice_for_language("zh") == "zf_xiaobei"
    assert narration.default_voice_for_language("de") is None
    assert narration.secondary_voice_for("af_heart") == "am_michael"
    assert narration.secondary_voice_for("ef_dora") == "em_alex"
    # The secondary is a same-LANGUAGE contrast (the English
    # preference order applies across US/UK voices alike).
    assert narration.secondary_voice_for("bf_emma") == "am_michael"
    # French has exactly one voice: no secondary exists, honestly.
    assert narration.secondary_voice_for("ff_siwis") is None
    assert narration.voice_language("zf_xiaoxiao") == "zh"
    assert narration.voice_language("pf_dora") == "pt"
    # Unknown ids resolve to the default voice (the module's
    # standing fallback semantics), whose language is English.
    assert narration.voice_language("bogus") == "en"


def test_engine_call_pins_speed_and_voice_language(monkeypatch, tmp_path):
    recorded = {}

    class FakeEngine:
        def create(self, text, voice=None, speed=None, lang=None):
            recorded.update(
                {"text": text, "voice": voice, "speed": speed, "lang": lang}
            )
            return np.zeros(2400, dtype=np.float32), 24000

    monkeypatch.setattr(narration, "_kokoro", lambda: FakeEngine())
    out = tmp_path / "part.wav"
    narration._run_kokoro("Hola, mundo.", out, voice_id="ef_dora")
    assert recorded["voice"] == "ef_dora"
    assert recorded["speed"] == 1.0
    assert recorded["lang"] == "es"
    assert out.exists()


def test_st_abbreviation_reads_as_saint_for_synthesis_only():
    assert (
        narration._normalize_for_synthesis("He died in St. Louis in 1904.")
        == "He died in Saint Louis in 1904."
    )
    # Lowercase "st." and a sentence-final "St." are untouched.
    assert (
        narration._normalize_for_synthesis("The word st. appears here.")
        == "The word st. appears here."
    )
    assert (
        narration._normalize_for_synthesis("He lives on Fifth St.")
        == "He lives on Fifth St."
    )


def test_config_refuses_voice_language_mismatch():
    config = production_config.clean_config(
        {"narration": {"language": "es", "voice_id": "af_heart"}}
    )
    verdict = production_config.validate_config(config)
    codes = {e["code"] for e in verdict["errors"]}
    assert "voice_language_mismatch" in codes


def test_config_accepts_matching_voice_and_projects_language_default():
    spanish = production_config.clean_config(
        {"narration": {"language": "es"}}
    )
    verdict = production_config.validate_config(spanish)
    assert verdict["errors"] == []
    flat = production_config.flat_options(spanish)
    # A voiceless Spanish production narrates with the Spanish
    # primary voice, never the English default.
    assert flat["voice_id"] == "ef_dora"
    assert flat["language"] == "es"

    english = production_config.clean_config(
        {"narration": {"language": "en"}}
    )
    flat_en = production_config.flat_options(english)
    assert "voice_id" not in flat_en  # English default path unchanged


@pytest.mark.parametrize(
    "voice_id, text",
    [
        ("af_heart", "The inquiry opened at dawn, and the city waited."),
        ("ef_dora", "La investigación comenzó al amanecer, y la ciudad esperó."),
    ],
)
def test_real_synthesis_through_generate_narration(tmp_path, voice_id, text):
    out = tmp_path / "narration.wav"
    meta = narration.generate_narration(
        text=text, output_path=str(out), voice_id=voice_id
    )
    assert meta["engine"] == "kokoro"
    assert meta["voice_id_used"] == voice_id
    assert meta["voice_fallback"] is False
    manifest = narration.load_parts_manifest_data(
        out.parent / "narration_parts" / "parts_manifest.json"
    )
    assert manifest is not None
    assert manifest["parts"]
    assert all(
        part["measured_duration_seconds"] > 0
        for part in manifest["parts"]
    )
    assert out.exists() and out.stat().st_size > 0
