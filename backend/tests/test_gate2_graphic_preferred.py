"""Gate 2 / UV-05 — graphic-preferred routing at the atmospheric tier.

T4 atmospheric is the ladder's last search tier; T5 graphics are
reached only through missing_visuals. For a beat whose own plan
data marks it inherently explanatory (a statistic, a process), a
generic atmospheric clip is the weakest support — weaker than the
designed graphic the graphics path produces (Unified Visual Audit
UV-05; founder Scenario D / Annex A). When such a beat's walk
reaches the atmospheric tier (only possible with zero verified
candidates from T1–T3), the atmospheric spend is skipped — no
queries, no downloads, no ledger charge — the attempt records
outcome "graphics_preferred" with the plan signal, and the beat's
missing_visuals entry carries reason "graphic_preferred" (jobs
builds graphic targets from missing_visuals regardless of reason).

Signals are read only from plan-carried data (tier_ladder.
graphic_preferred_signal): primary intent graphic_explanation, a
plan-authored statistic/diagram graphic_fallback, or a
chart/graphic scenario format intent. Map/timeline fallbacks —
which the deterministic derivation stamps on ordinary placed /
timed beats — are NOT signals.
"""

import uuid
from pathlib import Path

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import media_pipeline as mp
from services import story_understanding as story
from services import tier_ladder
from services.visual_registry import VisualRegistry


GILGO_S9 = (
    "For months, investigators studied his phones, his movements, "
    "and his family travel records."
)
S9_CONCEPTS = {
    "activities": ["reviewing records", "reviewing phones",
                   "reviewing travel records"],
    "objects": ["phones", "travel records"],
    "settings": [], "place_types": [], "mood": [],
    "era_terms": ["2010s"],
}
EXPLANATORY_INTENTS = {
    "primary_visual_intent": "graphic_explanation",
    "secondary_visual_intents": [],
    "intent_queries": {},
    "graphic_fallback": {"allowed": True, "kind": "statistic"},
    "intents_source": "llm_plan",
}
ORDINARY_INTENTS = {
    "primary_visual_intent": "human_activity",
    "secondary_visual_intents": [],
    "intent_queries": {},
    # The deterministic derivation stamps exactly this on any
    # timed beat — it must NOT count as an explanatory signal.
    "graphic_fallback": {"allowed": True, "kind": "timeline"},
    "intents_source": "llm_plan",
}


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


def _junk(host="junk.example.org"):
    return {
        "url": f"https://{host}/cooking",
        "title": "Cooking show highlights",
        "snippet": "recipes and kitchen fun",
        "provider": "youtube",
    }


def _fixture(intents):
    sentence = {
        "sentence_number": 9, "text": GILGO_S9, "role": "action",
        "fallback_concepts": S9_CONCEPTS, "concepts_source": "llm_plan",
        "visual_intents": intents,
    }
    beat = {
        "beat_id": "beat_s9", "sentence_numbers": [9],
        "text": GILGO_S9, "sentences": [sentence],
        "subject_id": None, "place": None,
    }
    scenario = {
        "sentence_number": 9, "specificity": "representative",
        "kind": "person_activity", "subject": "",
        "search_terms": ["phone records"],
        "visual": "analysts reviewing phone records",
        "queries": ["analysts reviewing phone records"],
    }
    need = {
        "beat_id": "beat_s9", "shot_index": 1,
        "shot_type": "person_activity", "specificity": "representative",
        "visual_category": "contextual", "description": "",
        "required_subject_id": None, "required_subject": "",
        "required_type": "person", "era": "2010s", "place": "",
        "event": "", "visual_requirement": "", "forbidden": [],
        "priority": 2, "tier": "direct",
        "fallback_concepts": S9_CONCEPTS, "concepts_source": "llm_plan",
        "visual_intents": intents,
        "queries": ["analysts reviewing phone records"],
    }
    return beat, scenario, need


def _run_pass(monkeypatch, tmp_path, *, beat, scenario, need,
              registry=None):
    calls = []

    def _search(scenario_, cache, pid, need_):
        calls.append((scenario_, need_))
        return ([_junk()], [], [])

    monkeypatch.setattr(mp, "_uve_scenario_search", _search)
    scene_stats = {
        "candidates_considered": 0, "candidates_rejected": 0,
        "downloads_failed": 0, "providers": {}, "kinds": {},
    }
    asset = mp._uve_search_and_pick(
        scenario, need,
        search_cache={}, project_id="t", bible=_bible(),
        registry=registry or VisualRegistry(), used_keys=set(),
        scene_dir=tmp_path, scene_stats=scene_stats,
        beat=beat, beat_videos=[], beat_images=[],
        scenario_index=1, queries=need.get("queries") or [],
        review_records=[],
    )
    return asset, scene_stats, calls


# ---------------------------------------------------------------------------
# The signal predicate (pure, plan-carried data only)
# ---------------------------------------------------------------------------

def test_signal_primary_graphic_explanation():
    signal = tier_ladder.graphic_preferred_signal(
        {"visual_intents": EXPLANATORY_INTENTS}
    )
    assert signal == {"signal": "primary_visual_intent",
                      "kind": "statistic"}


def test_signal_plan_authored_statistic_or_diagram_fallback():
    for kind in ("statistic", "diagram"):
        signal = tier_ladder.graphic_preferred_signal({
            "visual_intents": {
                "primary_visual_intent": "human_activity",
                "graphic_fallback": {"allowed": True, "kind": kind},
            }
        })
        assert signal == {"signal": "graphic_fallback", "kind": kind}


def test_signal_format_intent_chart_or_graphic():
    signal = tier_ladder.graphic_preferred_signal(
        {"visual_intents": ORDINARY_INTENTS},
        {"format_intent": "chart", "format_intent_confidence": 0.9},
    )
    assert signal == {"signal": "format_intent", "kind": "statistic"}
    signal = tier_ladder.graphic_preferred_signal(
        {},
        {"format_intent": "graphic", "format_intent_confidence": 0.4},
    )
    assert signal == {"signal": "format_intent", "kind": "diagram"}


def test_no_signal_for_ordinary_beats():
    # Derived map/timeline fallbacks are not explanatory signals.
    for kind in ("map", "timeline"):
        assert tier_ladder.graphic_preferred_signal({
            "visual_intents": {
                "primary_visual_intent": "establishing",
                "graphic_fallback": {"allowed": True, "kind": kind},
            }
        }) is None
    # A disallowed fallback, a geographic format intent, a weak
    # chart stamp, and no data at all: no signal.
    assert tier_ladder.graphic_preferred_signal({
        "visual_intents": {
            "graphic_fallback": {"allowed": False, "kind": "statistic"},
        }
    }) is None
    assert tier_ladder.graphic_preferred_signal(
        {}, {"format_intent": "map", "format_intent_confidence": 0.6}
    ) is None
    assert tier_ladder.graphic_preferred_signal(
        {}, {"format_intent": "chart", "format_intent_confidence": 0.2}
    ) is None
    assert tier_ladder.graphic_preferred_signal({}) is None
    assert tier_ladder.graphic_preferred_signal(None) is None


# ---------------------------------------------------------------------------
# (a) Explanatory beat: no atmospheric spend, graphics routing
# ---------------------------------------------------------------------------

def test_explanatory_beat_skips_atmospheric_and_routes_to_graphics(
    monkeypatch, tmp_path
):
    beat, scenario, need = _fixture(EXPLANATORY_INTENTS)
    registry = VisualRegistry()
    asset, scene_stats, calls = _run_pass(
        monkeypatch, tmp_path,
        beat=beat, scenario=scenario, need=need, registry=registry,
    )
    assert asset is None
    attempts = scene_stats["coverage_passes"][0]["tier_attempts"]
    assert [a["tier"] for a in attempts] == [
        "related", "activity_broll", "atmospheric",
    ]
    assert attempts[0]["outcome"] == "pool_exhausted"
    assert attempts[1]["outcome"] == "pool_exhausted"
    atmospheric = attempts[2]
    assert atmospheric["outcome"] == "graphics_preferred"
    assert atmospheric["queries"] == []
    assert atmospheric["graphic_preferred"] == {
        "signal": "primary_visual_intent", "kind": "statistic",
    }
    # No atmospheric search ever executed: every search call was
    # for the direct/related/activity tiers.
    searched_tiers = {
        (n or {}).get("tier") for _s, n in calls
    }
    assert "atmospheric" not in searched_tiers
    # The ledger spent only the T2/T3 queries — the skip consumed
    # no searches and no downloads.
    ledger = tier_ladder.ledger_for(registry)
    spent = sum(len(a["queries"]) for a in attempts[:2])
    assert ledger.searches == spent
    assert ledger.downloads == 0


def test_explanatory_beat_missing_visuals_carry_graphic_preferred(
    monkeypatch, tmp_path
):
    """Scene level: the routed beat's missing_visuals entry — the
    graphics path's input — carries reason "graphic_preferred"
    with the signal record, not a plain coverage failure."""
    beat, scenario, need = _fixture(EXPLANATORY_INTENTS)
    sentence = {
        **beat["sentences"][0], "scenarios": [scenario],
    }
    beats = [{**beat, "sentences": [sentence], "shot_needs": [need]}]
    monkeypatch.setattr(mp, "build_story_beats", lambda s, b: beats)
    monkeypatch.setattr(mp, "_load_case_bible", lambda s, p: _bible())
    monkeypatch.setattr(
        mp, "_uve_scenario_search",
        lambda scenario_, cache, pid, need_: ([_junk()], [], []),
    )
    scene = {
        "scene_number": 1,
        "project_id": f"gate2-gp-{uuid.uuid4()}",
        "sentences": [sentence],
    }
    result = mp._uve_process_scene_media(scene, str(tmp_path))
    missing = result["asset_stats"]["missing_visuals"]
    assert len(missing) == 1
    entry = missing[0]
    assert entry["sentence_number"] == 9
    assert entry["reason"] == "graphic_preferred"
    assert entry["graphic_preferred"] == {
        "signal": "primary_visual_intent", "kind": "statistic",
    }
    assert any(
        a["outcome"] == "graphics_preferred"
        and a["tier"] == "atmospheric"
        and a["queries"] == []
        for a in entry["tier_attempts"]
    )


# ---------------------------------------------------------------------------
# (b) Non-explanatory beat: atmospheric exactly as before
# ---------------------------------------------------------------------------

def test_ordinary_beat_still_escalates_to_atmospheric(
    monkeypatch, tmp_path
):
    beat, scenario, need = _fixture(ORDINARY_INTENTS)
    registry = VisualRegistry()
    asset, scene_stats, calls = _run_pass(
        monkeypatch, tmp_path,
        beat=beat, scenario=scenario, need=need, registry=registry,
    )
    assert asset is None
    attempts = scene_stats["coverage_passes"][0]["tier_attempts"]
    assert [a["tier"] for a in attempts] == [
        "related", "activity_broll", "atmospheric",
    ]
    atmospheric = attempts[-1]
    assert atmospheric["outcome"] == "pool_exhausted"
    assert 1 <= len(atmospheric["queries"]) <= (
        tier_ladder.TIER_QUERY_BUDGETS["atmospheric"]
    )
    assert not any(
        a["outcome"] == "graphics_preferred" for a in attempts
    )
    searched_tiers = {(n or {}).get("tier") for _s, n in calls}
    assert "atmospheric" in searched_tiers
    ledger = tier_ladder.ledger_for(registry)
    assert ledger.searches == sum(
        len(a["queries"]) for a in attempts
    )


# ---------------------------------------------------------------------------
# (c) The starvation floor is unaffected by the routing
# ---------------------------------------------------------------------------

def test_graphics_routing_spends_nothing_from_the_floor_accounts(
    monkeypatch, tmp_path
):
    beat, scenario, need = _fixture(EXPLANATORY_INTENTS)
    registry = VisualRegistry()
    _run_pass(
        monkeypatch, tmp_path,
        beat=beat, scenario=scenario, need=need, registry=registry,
    )
    ledger = tier_ladder.ledger_for(registry)
    # The skip charged neither account: the deepening sub-account
    # holds only what T2/T3 actually spent beyond their first
    # queries, and a later need's protected first attempt is fully
    # intact.
    assert ledger.deepening_searches <= (
        tier_ladder.DEEPENING_SEARCH_BUDGET
    )
    assert ledger.allocate_searches(3, need_key="later_need") >= 1
    assert ledger.can_spend_download(need_key="later_need")
    summary = ledger.summary()
    assert summary["floor_search_reserve"] == (
        tier_ladder.FLOOR_SEARCH_RESERVE
    )
    assert summary["beyond_floor_download_spends"] == 0
