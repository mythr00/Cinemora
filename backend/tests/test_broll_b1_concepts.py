"""Intelligent B-Roll B1 — visual intent in plans (plan §4, §11 B1).

Every sentence plan carries a fallback concept set — activities /
objects / settings / place_types / mood / era_terms: what a camera
could show if the exact subject could not be filmed. Sources:

- the LLM planner emits the block (SENTENCE_SYSTEM schema addition);
  normalize_plan validates it (<=4 phrases per list, <=6 words per
  phrase, empties dropped, nothing invented) and stamps
  concepts_source="llm_plan";
- the degraded planner (today's heuristic path through the same
  normalize_plan) derives the set deterministically from the
  sentence's own text and bible entities, stamped
  concepts_source="need_time_derivation" — the same derivation
  build_shot_needs runs at need-build time for sentence records
  that carry no concepts (cached/legacy plans).

Concepts are recorded on plans, sentence records, shot needs and
the plan document (the Visual Plan page's data). Nothing searches
them in B1 — the escalation loop is B2.

Genre packs gain broll_vocabulary under visual_profile (data),
with load-time validation and the true_crime care rule (§6.5): no
re-enactment-shaped phrases.
"""

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import genre_packs
from services import media_pipeline as mp
from services import plan_document
from services import story_understanding as story
from services.script_analyzer import analyze_script


GILGO_S1 = (
    "In December 2010, a police officer searching for a missing "
    "woman near Gilgo Beach, Long Island, made a horrifying discovery."
)
GILGO_S9 = (
    "For months, investigators studied his phones, his movements, "
    "and his family travel records."
)


def _bible():
    return story.hydrate_bible({
        "entities": [
            {"id": "rex_heuermann", "canonical": "Rex Heuermann",
             "type": "person", "aliases": ["Heuermann"]},
            {"id": "gilgo_beach", "canonical": "Gilgo Beach",
             "type": "place", "aliases": ["Gilgo"]},
        ],
        "primary_subject_id": "rex_heuermann",
        "title": "Gilgo Beach",
        "story_type": "true_crime",
        "time_period": "2010-2023",
    })


# ---------------------------------------------------------------------------
# Validation of LLM-supplied concepts (§4.1)
# ---------------------------------------------------------------------------

def test_validate_fallback_concepts_caps_and_cleans():
    raw = {
        "activities": [
            "reviewing records", "patrolling", "searching",
            "travelling", "interviewing witnesses", "fifth is dropped",
        ],
        "objects": ["phone records", "", "   ", 42, None],
        "settings": ["one two three four five six seven words"],
        "place_types": ["beach shoreline"],
        "mood": ["tense"],
        "era_terms": ["2010s"],
        "unknown_field": ["dropped"],
    }
    cleaned = story.validate_fallback_concepts(raw)
    assert cleaned["activities"] == [
        "reviewing records", "patrolling", "searching", "travelling",
    ]
    assert cleaned["objects"] == ["phone records"]
    # A 7-word phrase exceeds the 6-word cap and is dropped, not cut.
    assert cleaned["settings"] == []
    assert cleaned["place_types"] == ["beach shoreline"]
    assert "unknown_field" not in cleaned
    assert set(cleaned) == set(story.FALLBACK_CONCEPT_FIELDS)


def test_validate_fallback_concepts_rejects_non_dict():
    assert story.validate_fallback_concepts(None) is None
    assert story.validate_fallback_concepts(["reviewing records"]) is None
    assert story.validate_fallback_concepts("reviewing records") is None


def test_sentence_prompt_carries_concept_schema():
    assert "fallback_concepts" in story.SENTENCE_SYSTEM
    assert "activities" in story.SENTENCE_SYSTEM


# ---------------------------------------------------------------------------
# Deterministic derivation (§4.2) — the plan's worked examples
# ---------------------------------------------------------------------------

def test_derive_gilgo_sentence_9_records_review():
    concepts = story.derive_fallback_concepts(GILGO_S9)
    # The founder's example: "studied ... records" derives the
    # records-review activity a documentary would actually show.
    assert "reviewing records" in concepts["activities"]
    assert "travel records" in concepts["objects"]
    assert "phones" in concepts["objects"]
    # "travel" in "travel records" is a noun modifier, not a verb.
    assert "travelling" not in concepts["activities"]


def test_derive_gilgo_sentence_1_searching_and_beach():
    concepts = story.derive_fallback_concepts(GILGO_S1, place="Gilgo Beach")
    assert "searching" in concepts["activities"]
    settings_and_types = concepts["settings"] + concepts["place_types"]
    assert any("beach" in phrase for phrase in settings_and_types)
    assert "2010" in concepts["era_terms"]


def test_derive_never_invents_mood():
    concepts = story.derive_fallback_concepts(GILGO_S9)
    assert concepts["mood"] == []


# ---------------------------------------------------------------------------
# normalize_plan: both planners emit concepts
# ---------------------------------------------------------------------------

def test_normalize_plan_llm_concepts_validated_and_stamped():
    raw = {
        "meaning": "Investigators worked the records.",
        "role": "action",
        "subject_id": "rex_heuermann",
        "scenarios": [{
            "visual": "analysts reviewing phone records",
            "kind": "person_activity",
            "specificity": "representative",
            "subject_id": None,
            "search_terms": ["phone records"],
        }],
        "fallback_concepts": {
            "activities": ["reviewing records"],
            "objects": ["phone records"],
            "settings": ["investigation office"],
            "place_types": [],
            "mood": ["methodical"],
            "era_terms": ["2023"],
        },
    }
    plan = story.normalize_plan(raw, GILGO_S9, _bible(), "rex_heuermann")
    assert plan["concepts_source"] == "llm_plan"
    assert plan["fallback_concepts"]["activities"] == ["reviewing records"]
    assert plan["fallback_concepts"]["mood"] == ["methodical"]


def test_normalize_plan_degraded_derives_concepts():
    plan9 = story.normalize_plan(None, GILGO_S9, _bible(), "rex_heuermann")
    assert plan9["degraded"] is True
    assert plan9["concepts_source"] == "need_time_derivation"
    assert "reviewing records" in plan9["fallback_concepts"]["activities"]

    plan1 = story.normalize_plan(None, GILGO_S1, _bible(), "rex_heuermann")
    assert plan1["concepts_source"] == "need_time_derivation"
    assert "searching" in plan1["fallback_concepts"]["activities"]
    settings_and_types = (
        plan1["fallback_concepts"]["settings"]
        + plan1["fallback_concepts"]["place_types"]
    )
    assert any("beach" in phrase for phrase in settings_and_types)


# ---------------------------------------------------------------------------
# End to end (degraded): sentence records + plan document carry concepts
# ---------------------------------------------------------------------------

def test_analyze_script_degraded_records_concepts(monkeypatch):
    # The research pass is not what this test measures; keep it out
    # (it is fail-soft in production and network-shaped in tests).
    monkeypatch.setattr(
        story, "enrich_bible_with_research",
        lambda bible, *args, **kwargs: bible,
    )
    script = GILGO_S1 + " " + GILGO_S9
    analysis = analyze_script(script, project_id="b1", llm=None)
    assert analysis["mode"] == "degraded"
    records = analysis["sentences"]
    assert len(records) == 2
    for record in records:
        assert record["fallback_concepts"] is not None
        assert record["concepts_source"] == "need_time_derivation"
    assert "searching" in records[0]["fallback_concepts"]["activities"]
    assert "reviewing records" in records[1]["fallback_concepts"]["activities"]

    document = plan_document.plan_from_analysis(analysis, "b1")
    rows = {row["sentence_number"]: row for row in document["sentences"]}
    assert rows[1]["fallback_concepts"] is not None
    assert rows[1]["concepts_source"] == "need_time_derivation"
    assert "searching" in rows[1]["fallback_concepts"]["activities"]


# ---------------------------------------------------------------------------
# Shot needs copy the concepts (§4.2)
# ---------------------------------------------------------------------------

def _sentence_record(number, text, *, concepts=None, source=None):
    record = {
        "sentence_number": number,
        "text": text,
        "role": "action",
        "subject_id": None,
        "scenarios": [{
            "visual": "archival material",
            "kind": "archival",
            "subject_id": None,
            "specificity": "representative",
            "search_terms": [],
            "queries": ["archival material"],
            "place": None,
            "era": None,
        }],
    }
    if concepts is not None:
        record["fallback_concepts"] = concepts
        record["concepts_source"] = source
    return record


def test_shot_needs_copy_recorded_concepts():
    concepts = story.derive_fallback_concepts(GILGO_S9)
    beats = mp.build_story_beats(
        [_sentence_record(1, GILGO_S9, concepts=concepts,
                          source="llm_plan")],
        {},
    )
    need = beats[0]["shot_needs"][0]
    assert need["fallback_concepts"] == concepts
    assert need["concepts_source"] == "llm_plan"


def test_shot_needs_derive_when_sentence_has_none():
    beats = mp.build_story_beats([_sentence_record(1, GILGO_S9)], {})
    need = beats[0]["shot_needs"][0]
    assert need["concepts_source"] == "need_time_derivation"
    assert "reviewing records" in need["fallback_concepts"]["activities"]


# ---------------------------------------------------------------------------
# Genre-pack broll_vocabulary (§4.3 + §6.5 care rule)
# ---------------------------------------------------------------------------

def test_all_packs_ship_valid_broll_vocabulary():
    for pack_id in genre_packs.PACK_IDS:
        pack = genre_packs.load_pack(pack_id)
        failures = genre_packs.validate_visual_profile(
            pack["visual_profile"], pack_id=pack_id
        )
        assert failures == [], (pack_id, failures)
        resolved = genre_packs.resolve_visual_profile(pack)
        assert resolved["profile_failures"] == []
        vocabulary = resolved["profile"]["broll_vocabulary"]
        assert vocabulary["activities"]
        assert vocabulary["settings"]


def test_pack_without_vocabulary_resolves_to_general_core():
    synthetic = {
        "id": "history",
        "visual_profile": {"profile_version": 1},
        "visual_theme": None,
    }
    resolved = genre_packs.resolve_visual_profile(synthetic)
    general = genre_packs.load_pack("general")
    assert (
        resolved["profile"]["broll_vocabulary"]
        == general["visual_profile"]["broll_vocabulary"]
    )


def test_broll_vocabulary_validation_failures():
    assert genre_packs.validate_broll_vocabulary(None) == []
    assert genre_packs.validate_broll_vocabulary("patrolling") == [
        "broll_vocabulary_not_an_object"
    ]
    failures = genre_packs.validate_broll_vocabulary(
        {"activities": "patrolling", "bogus": []}
    )
    assert "broll_vocabulary_unknown_fields:bogus" in failures
    assert "broll_vocabulary_activities_not_a_list" in failures
    failures = genre_packs.validate_broll_vocabulary(
        {"activities": ["one two three four five six seven"]}
    )
    assert failures == ["broll_vocabulary_activities_phrase_too_long"]
    failures = genre_packs.validate_broll_vocabulary(
        {"activities": [f"phrase {i}" for i in range(13)]}
    )
    assert failures == ["broll_vocabulary_activities_too_many"]


def test_true_crime_care_rule_bars_reenactment_vocabulary():
    failures = genre_packs.validate_broll_vocabulary(
        {"activities": ["strangling the victim"]}, pack_id="true_crime"
    )
    assert failures == [
        "broll_vocabulary_reenactment:strangling the victim"
    ]
    # The investigation-side vocabulary the pack actually ships is
    # clean, and ordinary words never trip the fragment match.
    assert genre_packs.validate_broll_vocabulary(
        {"activities": ["reviewing evidence", "skilled forensic work"]},
        pack_id="true_crime",
    ) == []
    # The care rule is scoped to the true_crime pack's vocabulary.
    assert genre_packs.validate_broll_vocabulary(
        {"activities": ["strangling the victim"]}, pack_id="general"
    ) == []
    # A malformed true_crime profile fails closed at resolution.
    pack = {
        "id": "true_crime",
        "visual_profile": {
            "broll_vocabulary": {"activities": ["attacking a victim"]},
        },
        "visual_theme": None,
    }
    resolved = genre_packs.resolve_visual_profile(pack)
    assert resolved["profile_source"] == "general"
    assert any(
        "reenactment" in failure
        for failure in resolved["profile_failures"]
    )
