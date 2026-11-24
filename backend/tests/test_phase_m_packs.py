"""Phase M (founder #15) — the biography, science and investigative
genre packs, data-authored on the existing pack system (gap-audit
doc 01 §5 / doc 06 Phase M: the audit's three named additions).

The honesty rules under test:

- Each pack loads through the same loader, validates cleanly with
  the REAL load-time validators (visual profile, visual theme,
  source hints) and resolves its OWN profile — no fail-closed
  fallback to general.
- Expectations stay uncalibrated (range None, basis
  "uncalibrated"): no number enters a pack without the R6
  calibration protocol. The B-roll band is recorded as
  founder_band_provisional — a reporting reference, never a gate,
  and never dressed as founder-set for these genres.
- No pack declares a narration speed: pacing is informational
  only (founder ruling 2026-11-22 — narration runs at the
  provider's native rate; the retired per-pack narration_speed
  stays retired).
- Resolution: an explicit genre option names the new packs like
  any other; the conservative detector is unchanged (it still
  detects only history / true_crime — widening it is a planner
  task, not a template task, per the audit).
"""

import pytest

from services import genre_packs

NEW_PACKS = ("biography", "science", "investigative")

NEUTRAL_SCRIPT = "A quiet film about bread making and village life."


@pytest.mark.parametrize("pack_id", NEW_PACKS)
def test_new_pack_loads_with_required_fields(pack_id):
    pack = genre_packs.load_pack(pack_id)
    assert pack is not None, pack_id
    assert pack["id"] == pack_id
    for field in (
        "label", "description", "research_query_templates",
        "entity_handling", "format_preferences", "graphics_motifs",
        "style_family", "pacing", "music_mood", "mix_preset",
        "expectations", "visual_profile", "visual_theme",
        "source_hints",
    ):
        assert field in pack, (pack_id, field)
    assert pack["graphics_motifs"], pack_id
    assert pack["entity_handling"], pack_id
    assert pack["research_query_templates"], pack_id


@pytest.mark.parametrize("pack_id", NEW_PACKS)
def test_new_pack_validates_and_resolves_its_own_profile(pack_id):
    pack = genre_packs.load_pack(pack_id)
    assert genre_packs.validate_visual_profile(
        pack["visual_profile"], pack_id=pack_id
    ) == []
    assert genre_packs.validate_visual_theme(pack["visual_theme"]) == []
    assert genre_packs.validate_source_hints(pack["source_hints"]) == []
    info = genre_packs.resolve_genre_pack({"genre": pack_id}, NEUTRAL_SCRIPT)
    assert info["pack_id"] == pack_id
    assert info["source"] == "option"
    # The pack's OWN profile is used — no validation failure forced
    # the fail-closed fallback to the general profile.
    assert info["visual_profile_source"] == pack_id
    assert info["visual_profile_failures"] == []
    assert info["visual_theme_failures"] == []


@pytest.mark.parametrize("pack_id", NEW_PACKS)
def test_new_pack_expectations_are_uncalibrated(pack_id):
    expectations = genre_packs.load_pack(pack_id)["expectations"]
    for field in (
        "moving_share", "still_share", "document_share",
        "graphic_share", "uncovered_share",
    ):
        assert expectations[field] == {
            "basis": "uncalibrated", "range": None,
        }, (pack_id, field)
    band = genre_packs.broll_share_band(genre_packs.load_pack(pack_id))
    assert band is not None
    assert band["basis"] == "founder_band_provisional"
    assert 0.0 < band["max"] <= 0.5


@pytest.mark.parametrize("pack_id", NEW_PACKS)
def test_new_pack_declares_no_narration_speed(pack_id):
    pack = genre_packs.load_pack(pack_id)
    assert "narration_speed" not in pack
    assert "speed" not in pack["pacing"]
    assert "not retimed" in pack["pacing"]["notes"]


def test_new_pack_care_rules_are_genre_shaped():
    biography = " ".join(
        genre_packs.load_pack("biography")["entity_handling"]
    ).lower()
    assert "never invent biographical detail" in biography
    science = " ".join(
        genre_packs.load_pack("science")["entity_handling"]
    ).lower()
    assert "settled science" in science
    investigative = " ".join(
        genre_packs.load_pack("investigative")["entity_handling"]
    ).lower()
    assert "allegation is never narrated as a verdict" in investigative


def test_detector_unchanged_by_new_packs():
    # The detector still names only history / true_crime; a science
    # script without an explicit genre stays general (the audit:
    # auto-detect widening is a planner task, not a template task).
    info = genre_packs.detect_genre(
        "The laboratory published its experiment results on Tuesday."
    )
    assert info["pack_id"] == "general"
