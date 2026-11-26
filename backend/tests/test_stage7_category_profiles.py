"""Stage 7 / R7 — category-aware verification profiles completed +
genre independence as data (Master Architecture Review C.B.2/C.B.3
and B.2.2).

Pins:
- the C.B.2 category derivation (shot_type / role / specificity /
  named-place signals, all from the SHOT, never the candidate);
- the contextual/environmental contract: named-entity ABSENCE
  requirements replaced by an activity/place-TYPE presence floor,
  recorded as no_topical_evidence against the type requirement;
- the evidence contract UNCHANGED (Kohberger-class evidence beats
  are not loosened): the same candidate the contextual profile
  passes to the scorer is rejected under evidence at gate AND
  scorer, exactly as at HEAD;
- universal vetoes firing in every category;
- verdict dimension_results in the C.B.3 vocabulary (not_required
  vs not_evaluated distinguished), the environmental era waiver,
  and profile-bucket separation in visual_quality;
- genre packs' visual_profile as validated DATA: field-by-field
  fallback to general, fail-closed on invalid profiles, the
  assembled-template lint (MG-104), the evidence-floor rule, and
  the research-requirements wiring into the research pass.
"""

import pytest

from services import genre_packs
from services import media_pipeline as mp
from services import story_understanding as story
from services import visual_quality
from services.motion_engine.templates import TEMPLATES


_SOCIAL_HOST = "https://" + "www." + "tik" + "tok" + ".com/@someone/video/1"
_STOCK_HOST = "https://" + "pex" + "els" + ".com/photo/1"


# ---------------------------------------------------------------------------
# C.B.2 — category derivation
# ---------------------------------------------------------------------------

def test_derivation_document_and_object_are_evidence():
    assert story.resolve_visual_category(
        {"kind": "document", "specificity": "representative"}
    ) == "evidence"
    assert story.resolve_visual_category(
        {"shot_type": "object", "specificity": "generic"}
    ) == "evidence"


def test_derivation_evidence_role_is_evidence():
    assert story.resolve_visual_category(
        {"specificity": "representative", "role": "evidence"}
    ) == "evidence"


def test_derivation_environmental_needs_setting_only_place_shot():
    assert story.resolve_visual_category(
        {"shot_type": "place", "specificity": "representative", "place": ""}
    ) == "environmental"
    # A place shot that DOES name its place is contextual (a site,
    # not a setting) — zero drift on stored-production need shapes.
    assert story.resolve_visual_category(
        {
            "shot_type": "place", "specificity": "representative",
            "place": "Moscow, Idaho",
        }
    ) == "contextual"
    # Without a recorded place field the derivation cannot claim
    # "setting only" and stays with the specificity mapping.
    assert story.resolve_visual_category(
        {"shot_type": "place", "specificity": "representative"}
    ) == "contextual"


def test_derivation_abstract_broll_is_atmosphere():
    assert story.resolve_visual_category(
        {"kind": "abstract_broll", "specificity": "representative"}
    ) == "atmosphere"


def test_derivation_stage1_mapping_unchanged():
    assert story.resolve_visual_category({"specificity": "exact"}) == "evidence"
    assert story.resolve_visual_category(
        {"specificity": "representative"}
    ) == "contextual"
    assert story.resolve_visual_category({"specificity": "generic"}) == "atmosphere"
    assert story.resolve_visual_category({}) == "contextual"
    assert story.resolve_visual_category(
        {"specificity": "exact", "visual_category": "environmental"}
    ) == "environmental"


# ---------------------------------------------------------------------------
# C.B.2 — the gate contracts
# ---------------------------------------------------------------------------

_KOHBERGER_CONTEXTUAL_NEED = {
    "required_type": "location",
    "specificity": "representative",
    "shot_type": "place",
    "required_subject": "King Road house",
    "place": "Moscow, Idaho",
    "visual_requirement": "dim bedroom inside a shared student house at night",
    "description": "dim bedroom inside a shared student house at night",
}

_TYPE_MATCH_CANDIDATE = {
    "title": "Quiet student bedroom at night",
    "snippet": "Inside a shared house near campus, lights dim",
}


def test_gate_contextual_passes_type_match_without_named_entities():
    # The Kohberger-class yield case: the candidate's metadata is
    # full of the shot's TYPE tokens and names neither the house nor
    # the town. At HEAD this failed missing_subject/missing_place;
    # under the completed R7 profile the gate passes it to the
    # scorer.
    ok, reason = mp.gate_shot_need(
        _TYPE_MATCH_CANDIDATE, _KOHBERGER_CONTEXTUAL_NEED
    )
    assert ok, f"contextual gate must pass the type match, got {reason}"
    assert mp.gate_all_failures(
        _TYPE_MATCH_CANDIDATE, _KOHBERGER_CONTEXTUAL_NEED
    ) == []


def test_gate_contextual_rejects_without_any_type_presence():
    candidate = {
        "title": "Sunny beach resort tour",
        "snippet": "Palm trees and ocean waves all day",
    }
    ok, reason = mp.gate_shot_need(candidate, _KOHBERGER_CONTEXTUAL_NEED)
    assert (ok, reason) == (False, "no_topical_evidence")
    # The failure is recorded against the TYPE requirement — never
    # as a missing named entity.
    assert mp.gate_all_failures(
        candidate, _KOHBERGER_CONTEXTUAL_NEED
    ) == ["no_topical_evidence"]


def test_gate_evidence_contract_unchanged_for_same_candidate():
    # The SAME type-matching candidate under an evidence need is
    # rejected exactly as at HEAD: the named subject and the named
    # place are required and both are absent.
    need = {**_KOHBERGER_CONTEXTUAL_NEED, "specificity": "exact"}
    failures = mp.gate_all_failures(_TYPE_MATCH_CANDIDATE, need)
    assert "missing_subject" in failures
    assert "missing_place" in failures
    ok, _reason = mp.gate_shot_need(_TYPE_MATCH_CANDIDATE, need)
    assert ok is False


def test_gate_type_tokens_exclude_the_proper_nouns():
    tokens = mp._gate_type_tokens(_KOHBERGER_CONTEXTUAL_NEED)
    assert "bedroom" in tokens and "student" in tokens and "night" in tokens
    for proper in ("king", "road", "moscow", "idaho"):
        assert proper not in tokens


def test_gate_vetoes_still_fire_in_contextual():
    # Forbidden term (universal).
    need = {**_KOHBERGER_CONTEXTUAL_NEED, "forbidden": ["casino"]}
    ok, reason = mp.gate_shot_need(
        {"title": "Casino bedroom student night special", "snippet": "x"},
        need,
    )
    assert (ok, reason) == (False, "forbidden_term")
    # Positive person contradiction (universal).
    need2 = {
        "required_type": "person",
        "specificity": "representative",
        "required_subject": "Bryan Kohberger",
        "other_people": ["Ethan Chapin"],
        "visual_requirement": "court hearing room",
    }
    failures = mp.gate_all_failures(
        {"title": "Ethan Chapin memorial service", "snippet": "family"},
        need2,
    )
    assert story.CONTRADICTORY_PERSON in failures
    # Hard source veto (universal). Founder 2026-11-26 (Annex B):
    # the Pexels/Pixabay provider ban is lifted, so the surviving
    # universal veto target is a social-platform source. That veto
    # lives in verify_candidate (the scorer leg of the chain), so it
    # is asserted there, in the same contextual category.
    verdict = story.verify_candidate(
        {"title": "bedroom night", "url": _SOCIAL_HOST},
        {"specificity": "representative"},
        {"by_id": {}, "entities": []},
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == story.SOCIAL_PLATFORM_REASON
    assert verdict["category"] == "contextual"
    # New truth: the restored stock host is NOT vetoed — the same
    # candidate on a Pexels URL carries no banned-provider rejection.
    assert not story.is_banned_provider(
        {"title": "bedroom night", "url": _STOCK_HOST}
    )


# ---------------------------------------------------------------------------
# C.B.3 — scorer behaviour + verdict dimension_results
# ---------------------------------------------------------------------------

def _bible_with(entity):
    return {"by_id": {entity["id"]: entity}, "entities": [entity]}


def test_scorer_evidence_regression_missing_subject():
    bible = _bible_with({
        "id": "s1", "canonical": "Berlin Wall", "aliases": [],
        "type": "location",
    })
    scenario = {
        "specificity": "exact", "subject_id": "s1",
        "visual": "checkpoint charlie border crossing",
        "search_terms": ["berlin wall"],
    }
    verdict = story.verify_candidate(
        {"title": "Cooking show", "snippet": "recipes and kitchen fun"},
        scenario, bible,
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "missing_required_subject"
    assert verdict["category"] == "evidence"
    dims = verdict["dimension_results"]
    assert dims["entity"] == "fail"
    assert dims["topical"] == "fail"
    assert dims["factual_consistency"] == "pass"
    assert dims["visual"] == "not_evaluated"
    assert dims["source_credibility"] == "not_evaluated"
    assert dims["media_quality"] == "not_evaluated"
    assert verdict["era_waived"] is False


def test_scorer_contextual_acceptance_stamps_profile_dimensions():
    bible = _bible_with({
        "id": "u1", "canonical": "University of Idaho", "aliases": [],
        "type": "organization",
    })
    scenario = {
        "specificity": "representative", "subject_id": "u1",
        "visual": "students leaving a lecture hall on campus",
        "search_terms": ["lecture hall campus students"],
    }
    verdict = story.verify_candidate(
        {
            "title": "Students leaving lecture hall",
            "snippet": "Campus students between classes",
        },
        scenario, bible,
    )
    assert verdict["accepted"] is True
    assert verdict["category"] == "contextual"
    dims = verdict["dimension_results"]
    # The subject dimension is not REQUIRED under the contextual
    # profile — recorded not_required, not fail, not silently pass.
    assert dims["entity"] == "not_required"
    assert dims["topical"] == "pass"
    assert dims["factual_consistency"] == "pass"
    assert dims["transcript"] == "not_evaluated"


def test_scorer_environmental_era_waiver_is_recorded():
    scenario = {
        "shot_type": "place", "specificity": "representative",
        "place": "",
        "visual": "quiet residential street in a small town",
        "search_terms": ["residential street"],
    }
    verdict = story.verify_candidate(
        {"title": "Residential street, small town", "snippet": "quiet"},
        scenario, {"by_id": {}, "entities": []},
    )
    assert verdict["category"] == "environmental"
    assert verdict["era_waived"] is True
    assert verdict["dimension_results"]["temporal"] == "not_required"


def test_scorer_veto_verdict_dimensions_all_unevaluated():
    # Veto target: a social-platform host — the hard veto that
    # survives the lifted Pexels/Pixabay ban (founder 2026-11-26).
    verdict = story.verify_candidate(
        {"title": "bedroom", "url": _SOCIAL_HOST},
        {"specificity": "representative"},
        {"by_id": {}, "entities": []},
    )
    assert verdict["accepted"] is False
    assert set(verdict["dimension_results"].values()) == {"not_evaluated"}


# ---------------------------------------------------------------------------
# visual_quality — profile buckets stay separate (C.B.3)
# ---------------------------------------------------------------------------

def test_visual_quality_profile_buckets_are_distinct():
    def _item(number, category_marker):
        item = {
            "type": "video",
            "file_path": f"/media/s{number}.mp4",
            "source_url": "https://www.youtube.com/watch?v=abc123XYZ",
            "assignment": "sentence",
            "sentence_number": number,
            "duration": 5.0,
        }
        if category_marker:
            item["verification_category"] = category_marker
        return item

    timeline = {
        "scenes": [{
            "items": [_item(1, "contextual"), _item(2, None),
                      _item(3, "environmental")],
        }],
        "sentence_count": 3,
    }
    result = visual_quality.measure_visual_quality(
        timeline=timeline, asset_report={}, rights_manifest=[],
    )
    buckets = result["sentence_outcomes"]["buckets"]
    assert buckets["direct"] == 1
    assert buckets["direct_contextual_profile"] == 1
    assert buckets["direct_environmental_profile"] == 1
    assert buckets["contextual"] == 0  # rotation bucket untouched
    assert result["sentence_outcomes"]["reconciles"] is True
    assert result["relevance"]["direct_assignment_share"] == 1.0


# ---------------------------------------------------------------------------
# B.2.2 — visual_profile as validated pack data
# ---------------------------------------------------------------------------

def test_all_packs_carry_valid_visual_profiles():
    for pack in genre_packs.available_packs():
        assert genre_packs.validate_visual_profile(
            pack.get("visual_profile")
        ) == [], pack["id"]
        assert genre_packs.validate_visual_theme(
            pack.get("visual_theme")
        ) == [], pack["id"]
        priors = pack["visual_profile"]["category_priors"]
        assert set(priors) == set(genre_packs.PROFILE_CATEGORIES)
        assert abs(sum(priors.values()) - 1.0) < 0.01
        # Stage 6 expectations blocks stay intact (purely additive).
        assert pack["expectations"]["moving_share"] == {
            "basis": "uncalibrated", "range": None,
        }


def test_assembled_templates_are_real_and_dispatched():
    for name in genre_packs.ASSEMBLED_TEMPLATES:
        assert name in TEMPLATES
    from services import graphics_pipeline
    source = open(graphics_pipeline.__file__, encoding="utf-8").read()
    for name in genre_packs.ASSEMBLED_TEMPLATES:
        assert f'"{name}"' in source


def test_resolve_genre_pack_exposes_profile_and_theme():
    resolved = genre_packs.resolve_genre_pack({"genre": "true_crime"})
    assert resolved["visual_profile_source"] == "true_crime"
    assert resolved["visual_profile_failures"] == []
    assert resolved["visual_theme"] == "true_crime"
    assert resolved["visual_profile"]["category_priors"]["evidence"] == 0.35


def test_visual_profile_field_by_field_fallback():
    general = genre_packs.load_pack("general")
    synthetic = {
        "id": "history",
        "visual_profile": {
            "category_priors": {
                "evidence": 0.5, "contextual": 0.5,
            },
        },
        "visual_theme": "history",
    }
    resolved = genre_packs.resolve_visual_profile(synthetic)
    assert resolved["profile_source"] == "history"
    assert resolved["profile_failures"] == []
    # The declared field wins; every absent field is general's.
    assert resolved["profile"]["category_priors"] == {
        "evidence": 0.5, "contextual": 0.5,
    }
    assert (
        resolved["profile"]["template_affinities"]
        == general["visual_profile"]["template_affinities"]
    )
    assert resolved["visual_theme"] == "history"
    # The cached pack dicts are never mutated by resolution.
    assert "category_priors" not in (general.get("_resolved") or {})


@pytest.mark.parametrize("profile,failure", [
    ({"category_priors": {"evidence": 0.5}}, "category_priors_sum"),
    # Phase 6 supersession (AM-010): territory_map gained a
    # production assembler, so it is no longer an "unassembled"
    # affinity; cross_section remains data-only/unassembled and
    # exercises the same validation branch.
    ({"template_affinities": {"cross_section": 1}},
     "template_affinity_unassembled:cross_section"),
    ({"template_affinities": {"nope_template": 1}},
     "template_affinity_unknown:nope_template"),
    ({"specificity_defaults": {"evidence": "representative"}},
     "specificity_default_evidence_below_exact"),
    ({"bogus_field": 1}, "visual_profile_unknown_fields"),
    ({"research_requirements": {"min_sources_per_primary_subject": -1}},
     "research_requirements_min_sources_invalid"),
])
def test_invalid_visual_profiles_fail_closed_to_general(profile, failure):
    failures = genre_packs.validate_visual_profile(profile)
    assert any(f.startswith(failure) for f in failures), failures
    resolved = genre_packs.resolve_visual_profile(
        {"id": "history", "visual_profile": profile}
    )
    assert resolved["profile_source"] == "general"
    assert resolved["profile_failures"]
    assert (
        resolved["profile"]
        == genre_packs.load_pack("general")["visual_profile"]
    )


def test_invalid_visual_theme_falls_back_to_general():
    assert genre_packs.validate_visual_theme("noir") == [
        "visual_theme_unknown:noir"
    ]
    resolved = genre_packs.resolve_visual_profile(
        {"id": "history", "visual_profile": {}, "visual_theme": "noir"}
    )
    assert resolved["visual_theme"] == "editorial"
    assert resolved["theme_failures"] == ["visual_theme_unknown:noir"]


# ---------------------------------------------------------------------------
# B.2.4 — research requirements shape the research pass
# ---------------------------------------------------------------------------

def test_research_requirements_prioritise_primary_subject(monkeypatch):
    pack = genre_packs.load_pack("true_crime")
    entities = [
        {
            "id": "e_other", "canonical": "Detective Ramos",
            "aliases": [], "type": "person", "sources": [],
            "description": "",
        },
        {
            "id": "e_primary", "canonical": "Ada Lovelace",
            "aliases": [], "type": "person", "sources": [],
            "description": "",
        },
    ]
    bible = {
        "entities": entities,
        "by_id": {e["id"]: e for e in entities},
        "primary_subject_id": "e_primary",
        "timeline": [],
    }
    calls = []

    def fake_search(query):
        calls.append(query)
        # One source total, so the primary's minimum (2) is unmet.
        if len(calls) == 1:
            return [{"url": "https://example.org/a", "title": "A"}]
        return []

    monkeypatch.setattr(story, "_resolve_search_web", lambda: fake_search)
    # The other entity dominates the mentions; without the pack's
    # research requirements it would be researched first.
    script = "Detective Ramos spoke. Detective Ramos left. " * 3
    story.enrich_bible_with_research(
        bible, script, sentences=None, genre_pack=pack
    )
    assert calls and calls[0] == "Ada Lovelace"
    meta = bible["research_meta"]
    assert meta["research_requirements"] == {
        "min_sources_per_primary_subject": 2,
        "prefer_primary_sources": True,
    }
    assert meta["primary_subject_sources"] == 1
    assert meta["primary_subject_min_sources_met"] is False
