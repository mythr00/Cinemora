"""Master Directive Phase 2 — visual intentions (AM-002 v1) and the
tier ladder's pure machinery.

The intent contract extends the EXISTING plan schema through
normalize_plan: every sentence plan carries primary/secondary
visual intentions from the directive's closed 12-type vocabulary,
per-intent query lists, a graphic fallback, a preferred media type,
a specificity requirement and a misrepresentation risk — seeded
from the sentence's scenarios + B1 fallback concepts, never from
invented entities. The LLM path validates; the degraded path
derives deterministically. visual_plan.json v1 is the additive
pre-retrieval artefact.

The tier ladder module owns the D-B2 budgets, the per-production
ledger, per-role entry tiers (D-B3) and derived-scenario
construction per tier — all pinned here as pure functions.
"""

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import media_pipeline as mp
from services import plan_document
from services import story_understanding as story
from services import tier_ladder


GILGO_S9 = (
    "For months, investigators studied his phones, his movements, "
    "and his family travel records."
)


def _bible(story_type="true_crime"):
    return story.hydrate_bible({
        "entities": [
            {"id": "rex_heuermann", "canonical": "Rex Heuermann",
             "type": "person", "aliases": ["Heuermann"]},
            {"id": "gilgo_beach", "canonical": "Gilgo Beach",
             "type": "place", "aliases": ["Gilgo"]},
        ],
        "primary_subject_id": "rex_heuermann",
        "title": "Gilgo Beach",
        "story_type": story_type,
        "time_period": "2010-2023",
    })


def _plan(raw, sentence, bible=None):
    return story.normalize_plan(raw, sentence, bible or _bible(), None)


# ---------------------------------------------------------------------------
# Intent derivation — per genre shape
# ---------------------------------------------------------------------------

def test_true_crime_activity_beat_intents():
    """Gilgo sentence-9 shape: representative person_activity with
    record-review concepts — the primary is the human activity, the
    secondaries are the activity B-roll and the object detail the
    concepts support. Nothing else."""
    raw = {
        "role": "action",
        "scenarios": [{
            "visual": "analysts reviewing phone records",
            "kind": "person_activity", "specificity": "representative",
            "subject_id": None, "search_terms": ["phone records"],
            "place": None, "era": None,
        }],
        "fallback_concepts": {
            "activities": ["reviewing records", "reviewing phones",
                           "reviewing travel records"],
            "objects": ["phones", "travel records"],
            "settings": [], "place_types": [], "mood": [],
            "era_terms": ["2010s"],
        },
    }
    plan = _plan(raw, GILGO_S9)
    intents = plan["visual_intents"]
    assert intents["intents_source"] == "derivation"
    assert intents["primary_visual_intent"] == "human_activity"
    assert "activity_broll" in intents["secondary_visual_intents"]
    assert "object_detail" in intents["secondary_visual_intents"]
    assert "archival" in intents["secondary_visual_intents"]
    # No place was named: no establishing/geographic invention.
    assert "establishing" not in intents["secondary_visual_intents"]
    assert "geographic_context" not in intents["secondary_visual_intents"]
    assert intents["specificity_requirement"] == "representative"
    assert intents["visual_misrepresentation_risk"] == "medium"
    # Queries come only from the plan's own material.
    queries = intents["intent_queries"]
    assert queries["activity_broll"] == [
        "reviewing records", "reviewing phones", "reviewing travel records",
    ]
    assert queries["archival"] == ["2010s"]
    assert queries["establishing"] == []


def test_history_event_beat_primary_is_actual_event():
    bible = story.hydrate_bible({
        "entities": [
            {"id": "berlin_wall", "canonical": "Berlin Wall",
             "type": "place", "aliases": []},
        ],
        "primary_subject_id": "berlin_wall",
        "title": "The Fall of the Berlin Wall",
        "story_type": "history",
        "time_period": "1989",
    })
    raw = {
        "role": "event",
        "scenarios": [{
            "visual": "crowds at the Berlin Wall as it falls",
            "kind": "event", "specificity": "exact",
            "subject_id": "berlin_wall",
            "search_terms": ["berlin wall falls"],
            "place": "Berlin", "era": "1989",
        }],
        "fallback_concepts": {
            "activities": [], "objects": [], "settings": [],
            "place_types": ["city street"], "mood": [],
            "era_terms": ["1989"],
        },
    }
    plan = _plan(raw, "In November 1989, crowds tore down the Berlin Wall.",
                 bible=bible)
    intents = plan["visual_intents"]
    assert intents["primary_visual_intent"] == "actual_event"
    assert intents["specificity_requirement"] == "exact"
    assert intents["visual_misrepresentation_risk"] == "high"
    assert "establishing" in intents["secondary_visual_intents"]


def test_place_hook_beat_establishing_and_geographic():
    raw = {
        "role": "hook",
        "scenarios": [{
            "visual": "Gilgo Beach shoreline",
            "kind": "place", "specificity": "representative",
            "subject_id": "gilgo_beach", "search_terms": ["gilgo beach"],
            "place": "Gilgo Beach", "era": "2010",
        }],
        "fallback_concepts": {
            "activities": ["searching"], "objects": [],
            "settings": ["beach"], "place_types": ["beach shoreline"],
            "mood": [], "era_terms": ["2010s"],
        },
    }
    plan = _plan(raw, "A police officer searched Gilgo Beach in 2010.")
    intents = plan["visual_intents"]
    assert intents["primary_visual_intent"] == "establishing"
    assert "geographic_context" in intents["secondary_visual_intents"]
    assert intents["graphic_fallback"] == {"allowed": True, "kind": "map"}
    assert intents["intent_queries"]["establishing"][0] == "Gilgo Beach"


def test_general_science_object_beat():
    raw = {
        "role": "background",
        "scenarios": [{
            "visual": "the James Webb telescope mirror",
            "kind": "object", "specificity": "representative",
            "subject_id": None, "search_terms": ["telescope mirror"],
            "place": None, "era": None,
        }],
        "fallback_concepts": {
            "activities": [], "objects": ["telescope mirror"],
            "settings": ["observatory"], "place_types": [], "mood": [],
            "era_terms": [],
        },
    }
    plan = _plan(raw, "Its mirror collects light from distant galaxies.",
                 bible=_bible("science"))
    intents = plan["visual_intents"]
    assert intents["primary_visual_intent"] == "object_detail"
    assert "environmental_broll" in intents["secondary_visual_intents"]
    assert intents["graphic_fallback"] == {"allowed": False, "kind": None}


def test_no_invented_entities_in_intent_queries():
    """Every intent query phrase is traceable to the plan's own
    material: scenario terms, concept phrases, the named place, the
    era, or a subject+era / activity+setting combination of those."""
    raw = {
        "role": "action",
        "scenarios": [{
            "visual": "analysts reviewing phone records",
            "kind": "person_activity", "specificity": "representative",
            "subject_id": None, "search_terms": ["phone records"],
            "place": None, "era": None,
        }],
        "fallback_concepts": {
            "activities": ["reviewing records"],
            "objects": ["phones"],
            "settings": ["police station"],
            "place_types": [], "mood": [], "era_terms": ["2010s"],
        },
    }
    plan = _plan(raw, GILGO_S9)
    allowed = {
        "phone records", "reviewing records", "phones",
        "police station", "2010s", "reviewing records police station",
    }
    for phrases in plan["visual_intents"]["intent_queries"].values():
        for phrase in phrases:
            assert phrase in allowed, phrase


# ---------------------------------------------------------------------------
# LLM-supplied intents: validation
# ---------------------------------------------------------------------------

def test_llm_visual_intents_validated_not_trusted():
    raw = {
        "role": "action",
        "scenarios": [{
            "visual": "analysts reviewing phone records",
            "kind": "person_activity", "specificity": "representative",
            "subject_id": None, "search_terms": ["phone records"],
        }],
        "fallback_concepts": {
            "activities": ["reviewing records"], "objects": ["phones"],
            "settings": [], "place_types": [], "mood": [],
            "era_terms": [],
        },
        "visual_intents": {
            "primary_visual_intent": "activity_broll",
            "secondary_visual_intents": [
                "object_detail", "not_a_real_intent", "activity_broll",
            ],
            "intent_queries": {
                "literal": ["phone records"],
                "activity_broll": ["reviewing records"],
                "bogus_field": ["dropped"],
            },
            "graphic_fallback": {"allowed": True, "kind": "diagram"},
            "preferred_media_type": "video",
            "specificity_requirement": "representative",
            "visual_misrepresentation_risk": "low",
        },
    }
    plan = _plan(raw, GILGO_S9)
    intents = plan["visual_intents"]
    assert intents["intents_source"] == "llm_plan"
    assert intents["primary_visual_intent"] == "activity_broll"
    # Unknown intent dropped; the primary is not repeated as secondary.
    assert intents["secondary_visual_intents"] == ["object_detail"]
    assert set(intents["intent_queries"].keys()) == {
        "literal", "activity_broll", "establishing", "contextual",
        "archival",
    }
    assert intents["graphic_fallback"] == {
        "allowed": True, "kind": "diagram",
    }
    assert intents["preferred_media_type"] == "video"


def test_llm_invalid_primary_falls_back_to_derivation():
    raw = {
        "role": "action",
        "scenarios": [{
            "visual": "analysts reviewing phone records",
            "kind": "person_activity", "specificity": "representative",
            "subject_id": None, "search_terms": ["phone records"],
        }],
        "visual_intents": {"primary_visual_intent": "banana"},
    }
    plan = _plan(raw, GILGO_S9)
    intents = plan["visual_intents"]
    assert intents["primary_visual_intent"] == "human_activity"
    assert intents["primary_visual_intent"] in story.VISUAL_INTENTS


def test_degraded_plan_derives_intents():
    """No LLM output at all (raw=None): the degraded path still
    produces a complete intent record, deterministically."""
    plan = story.normalize_plan(
        None, "Heuermann lived quietly on Long Island.", _bible(), None,
    )
    intents = plan["visual_intents"]
    assert intents["intents_source"] == "derivation"
    assert intents["primary_visual_intent"] in story.VISUAL_INTENTS
    assert set(intents["intent_queries"].keys()) == {
        "literal", "activity_broll", "establishing", "contextual",
        "archival",
    }
    assert intents["specificity_requirement"] in (
        "exact", "representative", "generic",
    )
    assert intents["visual_misrepresentation_risk"] in (
        "low", "medium", "high",
    )


# ---------------------------------------------------------------------------
# Need carry + visual_plan.json
# ---------------------------------------------------------------------------

def test_shot_need_carries_visual_intents():
    sentence = {
        "text": GILGO_S9, "role": "action",
        "fallback_concepts": {
            "activities": ["reviewing records"], "objects": ["phones"],
            "settings": [], "place_types": [], "mood": [],
            "era_terms": ["2010s"],
        },
        "concepts_source": "llm_plan",
        "visual_intents": {
            "primary_visual_intent": "human_activity",
            "intents_source": "llm_plan",
        },
        # A beat's scenarios live on its sentence records (the
        # pipeline reads them from there via _beat_scenarios).
        "scenarios": [{
            "visual": "analysts reviewing phone records",
            "kind": "person_activity", "specificity": "representative",
            "subject_id": None, "search_terms": ["phone records"],
            "queries": ["analysts reviewing phone records"],
        }],
    }
    beat = {
        "beat_id": "beat_x", "searchable": True,
        "sentences": [sentence], "sentence_numbers": [9],
    }
    needs = mp.build_shot_needs(beat, _bible())
    assert len(needs) == 1
    assert needs[0]["visual_intents"]["primary_visual_intent"] == (
        "human_activity"
    )
    assert needs[0]["tier"] == "direct"


def test_visual_plan_document_rows(tmp_path):
    analysis = {
        "mode": "llm",
        "bible": {"title": "Gilgo", "story_type": "true_crime",
                  "entities": []},
        "scenes": [],
        "sentences": [
            {
                "sentence_number": 1, "text": "A discovery on the beach.",
                "role": "hook", "subject_id": None, "entities": [],
                "scenarios": [],
                "fallback_concepts": {
                    "activities": ["searching"], "objects": [],
                    "settings": ["beach"], "place_types": [],
                    "mood": [], "era_terms": ["2010s"],
                },
                "concepts_source": "llm_plan",
                "visual_intents": {
                    "primary_visual_intent": "establishing",
                    "secondary_visual_intents": ["activity_broll"],
                    "intent_queries": {
                        "literal": [], "activity_broll": ["searching"],
                        "establishing": [], "contextual": ["beach"],
                        "archival": ["2010s"],
                    },
                    "graphic_fallback": {"allowed": True, "kind": "map"},
                    "preferred_media_type": "video",
                    "specificity_requirement": "representative",
                    "visual_misrepresentation_risk": "medium",
                    "intents_source": "llm_plan",
                },
            },
            {
                "sentence_number": 9, "text": GILGO_S9,
                "role": "action", "subject_id": None, "entities": [],
                "scenarios": [],
                "fallback_concepts": None, "concepts_source": None,
                "visual_intents": None,
            },
        ],
    }
    document = plan_document.visual_plan_from_analysis(
        analysis, "proj-1",
        scene_governor={"split_threshold_seconds": 20.0},
    )
    assert document["version"] == 1
    assert document["scene_governor"] == {"split_threshold_seconds": 20.0}
    first, ninth = document["sentences"]
    assert first["primary_visual_intent"] == "establishing"
    assert first["entry_tier"] == "activity_broll"  # hook: D-B3
    assert ninth["entry_tier"] == "direct"
    # A sentence without recorded intents stays honest: nulls, not
    # fabricated intentions.
    assert ninth["primary_visual_intent"] is None

    path = plan_document.write_visual_plan_document(
        tmp_path, analysis, "proj-1",
    )
    assert path is not None and path.name == "visual_plan.json"
    assert path.exists()
    # plan.json rows carry the block too (the plan page's data).
    plan = plan_document.plan_from_analysis(analysis, "proj-1")
    assert plan["sentences"][0]["visual_intents"][
        "primary_visual_intent"
    ] == "establishing"


# ---------------------------------------------------------------------------
# Tier ladder: order, entry tiers, budgets, ledger
# ---------------------------------------------------------------------------

def test_tier_order_and_entry_tiers():
    assert tier_ladder.tiers_after("direct") == (
        "related", "activity_broll", "atmospheric",
    )
    assert tier_ladder.tiers_after("related") == (
        "activity_broll", "atmospheric",
    )
    assert tier_ladder.tiers_after("activity_broll") == ("atmospheric",)
    assert tier_ladder.tiers_after("atmospheric") == ()
    # D-B3: unsearched roles enter at T3, everything else at T1.
    for role in ("hook", "transition", "reflection", "conclusion",
                 "motivation"):
        assert tier_ladder.entry_tier_for_role(role) == "activity_broll"
    for role in ("action", "event", "background", "evidence", None):
        assert tier_ladder.entry_tier_for_role(role) == "direct"


def test_d_b2_budget_constants():
    # The founder-approved D-B2 numbers, exactly.
    assert tier_ladder.TIER_QUERY_BUDGETS == {
        "related": 2, "activity_broll": 3, "atmospheric": 2,
    }
    assert tier_ladder.TIER_DOWNLOAD_ATTEMPTS == 2
    assert tier_ladder.PRODUCTION_MAX_EXTRA_SEARCHES == 40
    assert tier_ladder.PRODUCTION_MAX_EXTRA_DOWNLOADS == 20
    assert tier_ladder.WALL_CLOCK_FRACTION == 0.25


def test_ledger_accounts_and_wall_clock():
    ticks = [1000.0]
    ledger = tier_ladder.EscalationLedger(
        clock=lambda: ticks[0], stage_started_at=0.0,
    )
    assert ledger.can_spend_searches(40)
    ledger.spend_searches(40)
    assert not ledger.can_spend_searches(1)
    assert ledger.summary()["extra_searches"] == 40
    # Wall clock: ACTIVE escalation time beyond 25% of the stage's
    # elapsed time trips the guard; idle time does not.
    ledger.escalation_seconds = 300.0  # stage elapsed 1000 -> cap 250
    assert ledger.wall_clock_exceeded()
    assert not ledger.can_spend_download()

    fresh = tier_ladder.EscalationLedger(
        clock=lambda: ticks[0], stage_started_at=0.0,
    )
    assert not fresh.wall_clock_exceeded()
    fresh.begin_escalation()
    ticks[0] += 10.0
    fresh.end_escalation()
    assert fresh.escalation_seconds == 10.0
    assert not fresh.wall_clock_exceeded()  # 10 <= 25% of 1010
    for _ in range(20):
        assert fresh.can_spend_download()
        fresh.spend_download()
    assert not fresh.can_spend_download()


def test_ledger_hangs_off_registry():
    from services.visual_registry import VisualRegistry

    registry = VisualRegistry()
    first = tier_ladder.ledger_for(registry)
    assert tier_ladder.ledger_for(registry) is first
    assert tier_ladder.ledger_for(VisualRegistry()) is not first


# ---------------------------------------------------------------------------
# Tier ladder: derived scenarios
# ---------------------------------------------------------------------------

def _need(**over):
    need = {
        "tier": "direct", "place": "", "era": "2010s",
        "fallback_concepts": {
            "activities": ["reviewing records", "reviewing phones"],
            "objects": ["phones", "travel records"],
            "settings": [], "place_types": [], "mood": [],
            "era_terms": ["2010s"],
        },
        "visual_intents": None,
    }
    need.update(over)
    return need


def test_derived_related_prefers_bible_entities():
    bible = story.hydrate_bible({
        "entities": [
            {"id": "phones_obj", "canonical": "phones",
             "type": "object", "aliases": []},
            {"id": "gilgo_beach", "canonical": "Gilgo Beach",
             "type": "place", "aliases": []},
        ],
        "story_type": "true_crime",
    })
    scenarios = tier_ladder.derived_scenarios_for_tier(
        _need(place="Gilgo Beach"), bible, "related",
    )
    assert len(scenarios) == 2  # the tier's query budget is 2
    assert scenarios[0]["specificity"] == "exact"
    assert scenarios[0]["subject_id"] == "phones_obj"
    assert scenarios[1]["kind"] == "place"
    assert scenarios[1]["specificity"] == "exact"


def test_derived_related_unmatched_objects_are_representative():
    scenarios = tier_ladder.derived_scenarios_for_tier(
        _need(), {}, "related",
    )
    # Both concept objects become representative detail scenarios —
    # and no more than the tier's query budget (2) are produced.
    assert len(scenarios) == 2
    assert scenarios[0]["kind"] == "object"
    assert scenarios[0]["subject"] == "phones"
    assert scenarios[0]["specificity"] == "representative"
    assert scenarios[1]["subject"] == "travel records"


def test_derived_activity_uses_sentence_activities():
    scenarios = tier_ladder.derived_scenarios_for_tier(
        _need(), {}, "activity_broll",
    )
    assert len(scenarios) == 1
    scenario = scenarios[0]
    assert scenario["kind"] == "person_activity"
    assert scenario["specificity"] == "representative"
    assert scenario["subject"] == ""
    assert "reviewing records" in scenario["search_terms"]
    # No activities -> no honest T3 scenario.
    empty = _need(fallback_concepts={
        "activities": [], "objects": ["phones"], "settings": [],
        "place_types": [], "mood": [], "era_terms": [],
    })
    assert tier_ladder.derived_scenarios_for_tier(
        empty, {}, "activity_broll",
    ) == []


def test_derived_atmospheric_from_place_types_and_mood():
    need = _need(fallback_concepts={
        "activities": [], "objects": [], "settings": ["beach"],
        "place_types": ["beach shoreline"], "mood": ["somber"],
        "era_terms": ["2010s"],
    })
    scenarios = tier_ladder.derived_scenarios_for_tier(
        need, {}, "atmospheric",
    )
    assert len(scenarios) == 1
    assert scenarios[0]["specificity"] == "generic"  # no named place
    assert "beach shoreline" in scenarios[0]["search_terms"]
    # A named place upgrades the same material to representative.
    anchored = tier_ladder.derived_scenarios_for_tier(
        _need(place="Gilgo Beach", fallback_concepts=need["fallback_concepts"]),
        {}, "atmospheric",
    )
    assert anchored[0]["specificity"] == "representative"


def test_broll_vocabulary_resolution():
    pack_id, vocabulary = tier_ladder.broll_vocabulary_for(
        {"story_type": "true_crime"}
    )
    assert pack_id == "true_crime"
    assert any(
        "searching" in phrase for phrase in vocabulary["activities"]
    )
    assert "police station" in vocabulary["settings"]
    # Unknown story types fail closed to the general pack.
    pack_id, vocabulary = tier_ladder.broll_vocabulary_for(
        {"story_type": "underwater_basket_weaving"}
    )
    assert pack_id == "general"
    assert vocabulary["activities"]
    pack_id, _ = tier_ladder.broll_vocabulary_for({})
    assert pack_id == "general"
