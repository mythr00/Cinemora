"""Phase 6–7 — selection intelligence + advanced treatments
(Motion Graphics Programme, 2026-11-27).

X2: the Visual Director classifies every graphic-candidate beat
by STORYTELLING PROBLEM from recorded data (story-understanding
intents, scenario contracts, bible data, coverage state) — never
from new prose regexes; keyword features survive only as a
labelled fallback. rest resolves to NO graphic recorded as the
deliberate success `no_graphic_by_design`; reconstruction
resolves to a specialist decline and never a fabricated
treatment. The problem + chosen treatment ride on the manifest
entry and the plan-v2 decision, so the choice is auditable.
"""

import json

from services import graphics_pipeline, visual_director
from services.genre_packs import load_pack


def _plan_v1(rows):
    return {
        "version": 1, "project_id": "proj-67", "analysis_mode": "llm",
        "scene_governor": None, "sentences": rows,
    }


def _row(number, scene=1, intent="exact", fallback_allowed=True,
         fallback_kind="statistic", role="statement"):
    return {
        "sentence_number": number,
        "scene_number": scene,
        "role": role,
        "primary_visual_intent": intent,
        "secondary_visual_intents": [],
        "intent_queries": {},
        "graphic_fallback": {
            "allowed": fallback_allowed, "kind": fallback_kind,
        },
        "preferred_media_type": "video",
        "specificity_requirement": "exact",
        "visual_misrepresentation_risk": "low",
        "intents_source": "llm_plan",
        "fallback_concepts": None,
        "concepts_source": None,
        "entry_tier": "direct",
    }


def _outcome(number, scene=1, index=0, covered=False, starved=False,
             text="", scenario=None, document_asset=None):
    outcome = {
        "sentence_number": number,
        "scene_number": scene,
        "sentence_index": index,
        "text": text,
        "scenario": scenario or {},
        "covered": covered,
        "starved": starved,
        "tier": "direct" if covered else None,
        "asset_title": None,
        "asset_file": "/media/a.mp4" if covered else None,
        "scene_photo_count": 0,
        "secondary_video": None,
        "scene_has_identity_photo": False,
    }
    if document_asset is not None:
        outcome["document_asset"] = document_asset
    return outcome


def _decisions(plan_v2):
    return {
        row["sentence_number"]: row["decision"]
        for row in plan_v2["sentences"]
    }


# ---------------------------------------------------------------------------
# Per-problem fixtures
# ---------------------------------------------------------------------------

def test_identification_covered_beat_gets_lower_third():
    """A covered beat introducing a recorded person: problem =
    identification (recorded basis), served by the name-super
    lower third over the footage — not a full-frame graphic."""
    pack = load_pack("general")
    bible = {
        "entities": [{
            "id": "p1", "type": "person",
            "canonical": "Ada Lovelace", "role": "Mathematician",
        }],
        "timeline": [],
    }
    plan = _plan_v1([_row(1)])
    outcomes = {
        1: _outcome(1, covered=True, scenario={
            "subject": "p1", "subject_name": "Ada Lovelace",
        }),
    }
    plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=pack, bible=bible
    )
    decision = _decisions(plan_v2)[1]
    assert decision["treatment"] == "footage"
    assert decision["story_problem"] == "identification"
    assert decision["selection_basis"] == "recorded"
    assert decision["composition"]["pattern"] == "lower_third_card"
    assert decision["composition"]["reason"] == "name_super"
    assert decision["composition"]["label"] == "Ada Lovelace"
    assert manifest["entries"] == []


def test_location_problem_prefers_locator():
    plan = _plan_v1([_row(1, fallback_kind=None)])
    outcomes = {
        1: _outcome(1, starved=True, text="The trail began there.",
                    scenario={"place": "Denver"}),
    }
    plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes
    )
    entry = manifest["entries"][0]
    assert entry["story_problem"] == "location"
    assert entry["selection_basis"] == "recorded"
    assert entry["template"] == "locator"
    assert _decisions(plan_v2)[1]["story_problem"] == "location"


def test_chronology_problem_from_recorded_timeline():
    events = [
        {"when": "1901", "what": "First crossing"},
        {"when": "1904", "what": "Second crossing"},
        {"when": "1911", "what": "Final crossing"},
    ]
    plan = _plan_v1([_row(1, fallback_kind=None)])
    outcomes = {
        1: _outcome(1, starved=True, text="Years passed.",
                    scenario={"timeline": events}),
    }
    _plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes
    )
    entry = manifest["entries"][0]
    assert entry["story_problem"] == "chronology"
    assert entry["selection_basis"] == "recorded"
    assert entry["template"] == "chronology"


def test_relationship_problem_from_recorded_relations():
    plan = _plan_v1([_row(1, fallback_kind=None)])
    outcomes = {
        1: _outcome(1, starved=True, text="They were connected.",
                    scenario={"relationships": [
                        {"from": "Ada", "to": "Grace",
                         "label": "mentored"},
                    ]}),
    }
    _plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes
    )
    entry = manifest["entries"][0]
    assert entry["story_problem"] == "relationship"
    assert entry["template"] == "relationship_network"


def test_evidence_problem_from_genuine_document_asset():
    plan = _plan_v1([_row(1, fallback_kind=None)])
    outcomes = {
        1: _outcome(
            1, starved=True, text="The letter stated the terms.",
            document_asset={
                "path": "/media/doc.png", "title": "Letter",
                "source": "archive", "url": "",
            }),
    }
    _plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes
    )
    entry = manifest["entries"][0]
    assert entry["story_problem"] == "evidence"
    assert entry["selection_basis"] == "recorded"
    assert entry["template"] == "document_presentation"


def test_measurement_problem_from_recorded_chart_contract():
    plan = _plan_v1([_row(1, fallback_kind=None)])
    outcomes = {
        1: _outcome(1, starved=True, text="Output grew steadily.",
                    scenario={"chart": {
                        "series": [
                            {"label": "1900", "value": 3},
                            {"label": "1910", "value": 5},
                        ],
                        "units": "tonnes", "source": "Ledger",
                    }}),
    }
    _plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes
    )
    entry = manifest["entries"][0]
    assert entry["story_problem"] == "measurement"
    assert entry["selection_basis"] == "recorded"
    assert "line_chart" in entry["template_candidates"]


def test_comparison_problem_from_recorded_contract():
    plan = _plan_v1([_row(1, fallback_kind=None)])
    outcomes = {
        1: _outcome(1, starved=True, text="Everything changed.",
                    scenario={"comparison": {
                        "before": {"label": "Before", "value": 12},
                        "after": {"label": "After", "value": 30},
                    }}),
    }
    _plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes
    )
    entry = manifest["entries"][0]
    assert entry["story_problem"] == "comparison"
    assert entry["selection_basis"] == "recorded"
    assert entry["template"] == "before_after"


def test_explanation_problem_from_recorded_diagram_contract():
    plan = _plan_v1([_row(1, fallback_kind=None)])
    outcomes = {
        1: _outcome(1, starved=True, text="The pump works simply.",
                    scenario={"diagram": {"parts": [
                        {"name": "Piston"},
                        {"name": "Valve"},
                    ]}}),
    }
    _plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes
    )
    entry = manifest["entries"][0]
    assert entry["story_problem"] == "explanation"
    assert entry["selection_basis"] == "recorded"
    # The labelled_diagram candidacy + template are pinned in
    # test_phase6_7_advanced.py (D1 commit).


def test_rest_beat_with_recorded_data_is_no_graphic_success():
    """Recorded data present (a chart contract) but the scenario
    records the beat as a rest beat: NO manifest entry, and the
    outcome is the deliberate success no_graphic_by_design —
    never a decline or failure."""
    plan = _plan_v1([_row(1, fallback_kind=None)])
    outcomes = {
        1: _outcome(1, starved=True, text="A long quiet followed.",
                    scenario={
                        "rest": True,
                        "chart": {
                            "series": [
                                {"label": "1900", "value": 3},
                                {"label": "1910", "value": 5},
                            ],
                            "units": "tonnes", "source": "Ledger",
                        },
                    }),
    }
    plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes
    )
    assert manifest["entries"] == []
    decision = _decisions(plan_v2)[1]
    assert decision["reason"] == "no_graphic_by_design"
    assert decision["graphic_outcome"] == "no_graphic_by_design"
    assert decision["by_design"] is True
    assert decision["story_problem"] == "rest"
    assert plan_v2["summary"]["no_graphic_by_design"] == 1
    assert plan_v2["summary"]["story_problems"]["rest"] == 1


def test_atmospheric_intent_is_a_recorded_rest_beat():
    plan = _plan_v1([
        _row(1, intent="atmospheric", fallback_kind=None),
    ])
    outcomes = {1: _outcome(1, starved=True, text="Dusk settled.")}
    plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes
    )
    assert manifest["entries"] == []
    assert _decisions(plan_v2)[1]["reason"] == "no_graphic_by_design"


def test_reconstruction_problem_is_specialist_decline():
    """A recorded reconstruction beat: NO manifest entry, NO
    fabricated treatment — a typed specialist decline."""
    plan = _plan_v1([_row(1, fallback_kind=None)])
    outcomes = {
        1: _outcome(1, starved=True,
                    text="The collapse unfolded in seconds.",
                    scenario={"kind": "reconstruction"}),
    }
    plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes
    )
    assert manifest["entries"] == []
    decision = _decisions(plan_v2)[1]
    assert decision["reason"] == "specialist_decline"
    assert decision["story_problem"] == "reconstruction"
    assert decision["decline"]["type"] == "specialist_decline"
    declines = plan_v2["summary"]["specialist_declines"]
    assert len(declines) == 1
    assert declines[0]["problem"] == "reconstruction"


def test_keyword_fallback_is_labelled_as_such():
    """No recorded data decides: the historical text features
    speak, and the record says the basis was keyword_fallback."""
    plan = _plan_v1([_row(1, fallback_kind=None)])
    outcomes = {
        1: _outcome(1, starved=True,
                    text="Exports rose 42 percent in a year."),
    }
    _plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes
    )
    entry = manifest["entries"][0]
    assert entry["story_problem"] == "measurement"
    assert entry["selection_basis"] == "keyword_fallback"


# ---------------------------------------------------------------------------
# X4 — density & rest governance in the chain
# ---------------------------------------------------------------------------

def _density_targets(numbers, problems=None):
    problems = problems or {}
    return [
        {
            "scene_number": 1,
            "sentence_index": n - 1,
            "sentence_number": n,
            "text": f"Exports rose {40 + n} percent that year.",
            "scenario": {},
            "story_problem": problems.get(n),
        }
        for n in numbers
    ]


def _read_manifest(tmp_path):
    return json.loads(
        (tmp_path / "motion_graphics.json").read_text()
    )


def test_density_budget_caps_rest_and_spacing(tmp_path):
    """A synthetic 10-beat plan, 10 graphic candidates, 6 s per
    beat (one minute total): the budget admits a subset (the
    1st, 3rd, 5th, 7th beats — rest beats and the per-minute cap
    exclude the rest) and EVERY exclusion is recorded with the
    typed density_budget reason."""
    targets = _density_targets(range(1, 11))
    slots = {n: 6.0 for n in range(1, 11)}
    assets = graphics_pipeline.build_motion_graphics_assets(
        {"scenes": [], "case_bible": {}}, tmp_path,
        targets=targets, slot_durations=slots,
    )
    produced = sorted(a["sentence_number"] for a in assets)
    assert produced == [1, 3, 5, 7]
    manifest = _read_manifest(tmp_path)
    density = manifest["density_budget"]
    assert density["max_full_frame_per_minute"] == 4
    assert density["full_frame_produced"] == 4
    assert len(density["exclusions"]) == 6
    rules = {
        e["sentence_number"]: e["rule"] for e in density["exclusions"]
    }
    assert rules[2] == "rest_beat_after_graphic"
    assert rules[4] == "rest_beat_after_graphic"
    assert rules[9] == "per_minute_cap_reached"
    assert rules[10] == "per_minute_cap_reached"
    skips = {
        s["sentence_number"]: s
        for s in manifest["targets"]["skipped"]
    }
    assert set(skips) == {2, 4, 6, 8, 9, 10}
    for skip in skips.values():
        assert skip["reason"] == "density_budget"
        assert skip["density"]["rule"]
    outcomes = {
        o["sentence_number"]: o
        for o in manifest["targets"]["outcomes"]
    }
    assert outcomes[1]["outcome"] == "generated"
    assert outcomes[1]["density"]["start_seconds"] == 0.0
    assert outcomes[2]["reason"] == "density_budget"


def test_density_rest_rule_exempts_evidence_critical_beat(tmp_path):
    """The beat right after a full-frame graphic is rested —
    UNLESS its recorded problem is evidence-critical."""
    targets = _density_targets([1, 2], problems={2: "evidence"})
    slots = {1: 15.0, 2: 15.0}
    assets = graphics_pipeline.build_motion_graphics_assets(
        {"scenes": [], "case_bible": {}}, tmp_path,
        targets=targets, slot_durations=slots,
    )
    assert sorted(a["sentence_number"] for a in assets) == [1, 2]


def test_density_inactive_without_recorded_slot_durations(tmp_path):
    """No recorded timings -> the budget cannot be measured and
    the historical behaviour is unchanged (no density key)."""
    targets = _density_targets([1, 2])
    assets = graphics_pipeline.build_motion_graphics_assets(
        {"scenes": [], "case_bible": {}}, tmp_path, targets=targets,
    )
    assert len(assets) == 2
    manifest = _read_manifest(tmp_path)
    assert "density_budget" not in manifest


def test_unclassifiable_beat_keeps_historical_behaviour():
    """Neither recorded data nor keyword features decide: no
    problem is stamped as decided (basis none) and the beat is
    still manifested for the chain, exactly as before X2."""
    plan = _plan_v1([_row(1, fallback_kind=None)])
    outcomes = {
        1: _outcome(1, starved=True, text="Nothing numeric here."),
    }
    _plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes
    )
    assert len(manifest["entries"]) == 1
    entry = manifest["entries"][0]
    assert entry["story_problem"] is None
    assert entry["selection_basis"] == "none"
