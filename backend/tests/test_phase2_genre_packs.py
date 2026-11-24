"""Transformation Phase 2.1/2.2 — the engineering and industrial
genre packs.

- Both packs load through the same loader as the existing three
  and validate cleanly (their own visual profiles resolve — no
  fail-closed fallback to general).
- Bands: engineering adopts the history <=40% founder band;
  industrial ships <=50% recorded in the data as provisional.
- A scripted engineering topic produces a measurably different
  visual plan under the engineering pack than under general
  (template candidates + affinities from the Director's own
  consumer, plus the resolved category priors).
- Resolution acceptance through the jobs chain (project category
  -> resolve -> asset report), mirroring the Phase 1.2 test.
- Per-pack narration pace (Phase 2.4): the two new packs declare
  0.94; the existing packs declare nothing and resolve to 1.0.
"""

import uuid

import pytest

import jobs
from database import SessionLocal, engine
from models import Base, Project
from services import genre_packs
from services import visual_director
from services.asset_report import build_asset_report

NEW_PACKS = ("engineering", "industrial")
# Phase M (founder #15) added biography / science / investigative
# as data-authored packs; the full-set pin follows the pack system.
PHASE_M_PACKS = ("biography", "science", "investigative")
ALL_PACKS = ("general", "history", "true_crime") + NEW_PACKS + PHASE_M_PACKS

NEUTRAL_SCRIPT = "A quiet film about bread making and village life."


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------

def test_all_packs_load_with_required_fields():
    assert set(genre_packs.PACK_IDS) == set(ALL_PACKS)
    assert [p["id"] for p in genre_packs.available_packs()] == list(
        genre_packs.PACK_IDS
    )
    for pack_id in ALL_PACKS:
        pack = genre_packs.load_pack(pack_id)
        assert pack is not None, pack_id
        assert pack["id"] == pack_id
        for field in (
            "research_query_templates", "entity_handling",
            "format_preferences", "graphics_motifs", "style_family",
            "pacing", "music_mood",
        ):
            assert field in pack, (pack_id, field)


@pytest.mark.parametrize("pack_id", NEW_PACKS)
def test_new_pack_profiles_validate_and_resolve(pack_id):
    info = genre_packs.resolve_genre_pack({"genre": pack_id}, NEUTRAL_SCRIPT)
    assert info["pack_id"] == pack_id
    # The pack's OWN profile is used — no validation failure
    # forced the fail-closed fallback to the general profile.
    assert info["visual_profile_source"] == pack_id
    assert info["visual_profile_failures"] == []
    assert info["visual_theme"] == "editorial"
    assert info["visual_theme_failures"] == []


@pytest.mark.parametrize("pack_id", NEW_PACKS)
def test_new_pack_broll_vocabulary_is_genre_shaped(pack_id):
    pack = genre_packs.load_pack(pack_id)
    vocab = pack["visual_profile"]["broll_vocabulary"]
    activities = " ".join(vocab["activities"]).lower()
    if pack_id == "engineering":
        assert "wreckage" in activities
        assert "inspecting" in activities
        assert "structural" in " ".join(
            pack["research_query_templates"]
        )
    else:
        assert "machinery" in activities
        assert "assembly line" in " ".join(vocab["settings"])
        assert "manufacturing" in " ".join(
            pack["research_query_templates"]
        )


def test_broll_bands_recorded_in_pack_data():
    engineering = genre_packs.broll_share_band(
        genre_packs.load_pack("engineering")
    )
    assert engineering == {"max": 0.4, "basis": "founder_band"}
    industrial = genre_packs.broll_share_band(
        genre_packs.load_pack("industrial")
    )
    assert industrial["max"] == 0.5
    # The industrial band is provisional — recorded as such in
    # the pack data itself, not just in a plan document.
    assert industrial["basis"] == "founder_band_provisional"


# ---------------------------------------------------------------------------
# The visual plan differs measurably from general
# ---------------------------------------------------------------------------

def _candidates_for(pack_id):
    """The Director's template candidates for one rich beat under
    a pack: comparison + percent + place + photos exercises every
    feature-derived candidate, so pack affinities decide the
    order."""
    pack = genre_packs.load_pack(pack_id)
    row = {"primary_visual_intent": "graphic_explanation"}
    outcome = {
        "text": (
            "The walkway carried 200 pounds per square foot, up "
            "40 percent over the 100 pound design load."
        ),
        "scenario": {"place": "the hotel atrium"},
        "scene_photo_count": 3,
    }
    return visual_director._template_candidates(row, outcome, pack)


def test_engineering_plan_differs_from_general():
    eng_candidates, eng_affinities = _candidates_for("engineering")
    gen_candidates, gen_affinities = _candidates_for("general")
    assert eng_candidates != gen_candidates
    assert eng_affinities != gen_affinities
    # The investigation beat's evidence board outranks under
    # engineering; under general it is not affinity-ranked at all.
    assert eng_affinities.get("evidence_board") == 2
    assert "evidence_board" not in gen_affinities
    assert eng_candidates.index("evidence_board") < gen_candidates.index(
        "evidence_board"
    )


def test_industrial_plan_differs_from_general():
    ind_candidates, ind_affinities = _candidates_for("industrial")
    gen_candidates, gen_affinities = _candidates_for("general")
    # The affinity weights the Director orders by differ
    # measurably: industrial doubles the gauge/stat weight of
    # general (process data first), where general leaves the
    # evidence board unranked and industrial ranks the stat
    # family at the top of its affinities.
    assert ind_affinities != gen_affinities
    assert ind_affinities.get("data_gauge") == 2
    assert gen_affinities.get("data_gauge") == 1
    assert ind_affinities.get("stat_comparison") == 2
    assert set(ind_candidates) == set(gen_candidates)


@pytest.mark.parametrize("pack_id", NEW_PACKS)
def test_new_pack_priors_lean_explanatory(pack_id):
    profile = genre_packs.resolve_visual_profile(
        genre_packs.load_pack(pack_id)
    )["profile"]
    general = genre_packs.resolve_visual_profile(
        genre_packs.load_pack("general")
    )["profile"]
    assert (
        profile["category_priors"]["explanatory_graphic"]
        > general["category_priors"]["explanatory_graphic"]
    )
    assert abs(sum(profile["category_priors"].values()) - 1.0) <= 0.01
    # Affinities name only templates with a production assembler.
    assert set(profile["template_affinities"]) <= (
        genre_packs.ASSEMBLED_TEMPLATES
    )
    assert profile["template_affinities"]["outcome_path"] == 3


# ---------------------------------------------------------------------------
# Resolution acceptance (the Phase 1.2 chain, new packs)
# ---------------------------------------------------------------------------

@pytest.fixture()
def project_ids():
    Base.metadata.create_all(engine)
    created = []
    yield created
    db = SessionLocal()
    try:
        for project_id in created:
            row = db.get(Project, project_id)
            if row is not None:
                db.delete(row)
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("pack_id", NEW_PACKS)
def test_project_category_records_new_pack(project_ids, pack_id):
    project_id = str(uuid.uuid4())
    project_ids.append(project_id)
    db = SessionLocal()
    try:
        db.add(Project(
            id=project_id, script=NEUTRAL_SCRIPT,
            title=f"phase2 acceptance {pack_id}", category=pack_id,
        ))
        db.commit()
    finally:
        db.close()

    merged = jobs._options_with_project_category({}, project_id)
    info = genre_packs.resolve_genre_pack(merged, NEUTRAL_SCRIPT)
    assert info["pack_id"] == pack_id
    assert info["source"] == "project_category"
    report = build_asset_report(
        [], genre_pack={"id": info["pack_id"], "source": info["source"]}
    )
    assert report["genre_pack"] == {
        "id": pack_id, "source": "project_category",
    }


# ---------------------------------------------------------------------------
# Narration pace — REMOVED (founder ruling, 2026-11-22)
# ---------------------------------------------------------------------------
# Phase 2.4's per-pack narration_speed (0.94 for engineering /
# industrial) was retired by the founder's voice-speed ruling:
# the voice model is third-party and speed control is
# unsupported, so narration runs at the provider's native rate
# and NO speed adjustment exists anywhere in the system. These
# tests pin the ruling so the control cannot silently return.

def test_no_narration_speed_anywhere_in_packs():
    assert not hasattr(genre_packs, "narration_speed")
    for pack_id in (
        "general", "history", "true_crime", "engineering", "industrial",
    ):
        pack = genre_packs.load_pack(pack_id)
        assert "narration_speed" not in (pack.get("pacing") or {})
