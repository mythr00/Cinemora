"""Phase 4c / D-06 + D-07 — capability-claim honesty.

Pins the current truth so no surface can silently grow a claim:

- D-06: there is NO face/person verification module; wrong-person
  protection is metadata-only (its docstring now says so).
- D-07: the narration voice catalogue is exactly the two local
  English Piper voices; uploaded_voice narration is refused
  honestly by the Phase 9 production config.
"""

import importlib

import pytest

from services import narration, production_config, story_understanding


def test_no_face_or_person_verification_module_exists():
    for module in (
        "services.face_verification",
        "services.person_verification",
        "services.face_recognition",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)


def test_wrong_person_protection_is_documented_metadata_only():
    doc = story_understanding.detect_contradiction.__doc__ or ""
    assert "METADATA-ONLY" in doc
    assert "NO face or person" in doc


def test_voice_catalogue_is_exactly_two_local_english_piper_voices():
    ids = set(narration.VOICE_CATALOGUE) - {"default"}
    assert ids == {"en_US-lessac-medium", "en_US-ryan-medium"}


def test_uploaded_voice_narration_still_refused_honestly():
    with pytest.raises(production_config.ConfigError) as caught:
        production_config.clean_config(
            {"narration": {"source": "uploaded_voice"}}
        )
    assert caught.value.code == "narration_source_unsupported"
