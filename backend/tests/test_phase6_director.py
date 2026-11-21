"""Phase 6 — Visual Director Pass-2 reconciliation (AM-002).

The Director is a pure decision module: these tests feed it plain
dicts and assert the two artefacts it produces — visual_plan.json
v2 (exactly one typed decision per sentence) and
graphics_manifest.json (the authoritative targeted-graphics list).
No I/O, no rendering.
"""

from services import visual_director
from services.genre_packs import load_pack


def _plan_v1(rows):
    return {
        "version": 1,
        "project_id": "proj-phase6",
        "analysis_mode": "llm",
        "scene_governor": None,
        "sentences": rows,
    }


def _row(number, scene=1, intent="exact", fallback_allowed=True):
    return {
        "sentence_number": number,
        "scene_number": scene,
        "role": "statement",
        "primary_visual_intent": intent,
        "secondary_visual_intents": [],
        "intent_queries": {},
        "graphic_fallback": {"allowed": fallback_allowed, "kind": "stat"},
        "preferred_media_type": "video",
        "specificity_requirement": "exact",
        "visual_misrepresentation_risk": "low",
        "intents_source": "llm_plan",
        "fallback_concepts": None,
        "concepts_source": None,
        "entry_tier": "direct",
    }


def _outcome(number, scene=1, index=0, covered=False, starved=False,
             text="", tier=None, title=None, scenario=None, photos=0):
    return {
        "sentence_number": number,
        "scene_number": scene,
        "sentence_index": index,
        "text": text,
        "scenario": scenario or {},
        "covered": covered,
        "starved": starved,
        "tier": tier,
        "asset_title": title,
        "asset_file": "/media/asset.mp4" if covered else None,
        "scene_photo_count": photos,
    }


def test_one_typed_decision_per_sentence():
    plan = _plan_v1([_row(1), _row(2), _row(3), _row(4)])
    outcomes = {
        1: _outcome(1, covered=True, tier="direct"),
        2: _outcome(2, index=1, starved=True, text="Exports rose 42 percent."),
        3: _outcome(3, index=2, starved=True, text="Nothing numeric here."),
        4: _outcome(4, index=3),  # no outcome recorded either way
    }
    plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes
    )
    assert plan_v2["version"] == 2
    assert manifest["version"] == 1
    decisions = {
        row["sentence_number"]: row["decision"]
        for row in plan_v2["sentences"]
    }
    assert len(decisions) == 4
    assert decisions[1]["treatment"] == "footage"
    assert decisions[1]["tier"] == "direct"
    assert decisions[2]["treatment"] == "graphic"
    assert decisions[3]["treatment"] == "graphic"
    assert decisions[4]["treatment"] == "unresolved"
    assert decisions[4]["reason"] == "starved_outcome_unrecorded"
    # The manifest holds exactly the graphic decisions, and the
    # decision's manifest_id resolves to the entry.
    assert len(manifest["entries"]) == 2
    entry_ids = {e["manifest_id"] for e in manifest["entries"]}
    assert decisions[2]["manifest_id"] in entry_ids
    assert decisions[3]["manifest_id"] in entry_ids
    assert plan_v2["summary"]["treatments"] == {
        "footage": 1, "graphic": 2, "unresolved": 1,
    }


def test_starved_beat_without_graphic_permission_stays_unresolved():
    plan = _plan_v1([_row(1, intent="exact", fallback_allowed=False)])
    outcomes = {1: _outcome(1, starved=True, text="A quiet street.")}
    plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes
    )
    decision = plan_v2["sentences"][0]["decision"]
    assert decision["treatment"] == "unresolved"
    assert decision["reason"] == "graphic_not_permitted_by_plan"
    assert manifest["entries"] == []


def test_graphic_explanation_intent_permits_graphic():
    plan = _plan_v1([
        _row(1, intent="graphic_explanation", fallback_allowed=False),
    ])
    outcomes = {1: _outcome(1, starved=True, text="Growth hit 7 percent.")}
    _plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes
    )
    assert len(manifest["entries"]) == 1
    assert manifest["entries"][0]["purpose"] == "graphic_explanation"


def test_template_affinities_order_candidates():
    """AM-008 consumer: the pack's template_affinities reorder the
    Director's template candidates (higher affinity first)."""
    pack = {
        "id": "custom",
        "visual_theme": "editorial",
        "visual_profile": {
            "template_affinities": {"data_gauge": 5, "bar_chart": 1},
            "composition_affinities": {"full_frame": 2},
        },
    }
    plan = _plan_v1([_row(1)])
    # Comparison AND percent features: bar_chart + stat_comparison
    # + data_gauge + ring_badges are all candidates by feature.
    outcomes = {
        1: _outcome(
            1, starved=True,
            text="Exports were 40 percent, up from 25 percent before.",
        ),
    }
    _plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=pack
    )
    entry = manifest["entries"][0]
    assert entry["template_candidates"][0] == "data_gauge"
    assert entry["affinities"] == {"data_gauge": 5, "bar_chart": 1}
    assert entry["template"] == "data_gauge"
    # The pack's visual_theme selected the production theme.
    assert entry["theme"] == "editorial_dark"
    assert "document_presentation" not in entry["template_candidates"]


def test_fact_basis_verified_only_with_full_evidence():
    plan = _plan_v1([_row(1), _row(2), _row(3)])
    outcomes = {
        n: _outcome(n, index=n - 1, starved=True, text=f"Beat {n}.")
        for n in (1, 2, 3)
    }
    evidence = {
        1: {
            "evidence_ref": {"asset_id": "asset-9", "source": "wikimedia"},
            "verified_data": {"display": "1,204", "kind": "count"},
        },
        # evidence_ref without an asset_id: NOT verified.
        2: {
            "evidence_ref": {"source": "wikimedia"},
            "verified_data": {"display": "55", "kind": "count"},
        },
        # verified_data without any evidence_ref: NOT verified.
        3: {"verified_data": {"display": "9", "kind": "count"}},
    }
    _plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, evidence=evidence
    )
    by_number = {
        e["target"]["sentence_number"]: e for e in manifest["entries"]
    }
    assert by_number[1]["fact_basis"] == "verified_asset_metadata"
    assert by_number[1]["evidence_ref"]["asset_id"] == "asset-9"
    # The verified-data path always permits the stat family.
    assert by_number[1]["family_candidates"][0] == "stat"
    assert by_number[2]["fact_basis"] == "script_claim"
    assert by_number[2]["evidence_ref"] is None
    assert by_number[3]["fact_basis"] == "script_claim"
    assert by_number[3]["verified_data"] is None


def test_composition_decision_for_scene_entry_covered_beat():
    """AM-008/AM-011: with the real general pack (lower_third_card
    affinity 1), the FIRST covered beat of a scene whose asset has
    a recorded title gets a composition decision; the second
    covered beat of the same scene does not."""
    pack = load_pack("general")
    plan = _plan_v1([_row(1), _row(2)])
    outcomes = {
        1: _outcome(1, covered=True, title="Ocean Parkway, Long Island"),
        2: _outcome(2, index=1, covered=True, title="Aerial of the coast"),
    }
    plan_v2, _manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=pack
    )
    first = plan_v2["sentences"][0]["decision"]
    second = plan_v2["sentences"][1]["decision"]
    assert first["composition"]["pattern"] == "lower_third_card"
    assert first["composition"]["label"] == "Ocean Parkway, Long Island"
    assert "composition" not in second
    assert plan_v2["summary"]["compositions"] == 1
    assert "composition_affinities" in (
        plan_v2["summary"]["pack_fields_consumed"]
    )


def test_outcomes_adapter_shapes_processed_scenes():
    processed = [
        {
            "scene": {
                "scene_number": 1,
                "sentences": [
                    {"sentence_number": 1, "text": "One.",
                     "scenarios": [{"place": "France"}]},
                    {"sentence_number": 2, "text": "Two.",
                     "scenarios": []},
                ],
            },
            "videos": [
                {
                    "verification": {"accepted": True},
                    "sentence_index": 0,
                    "tier": "related",
                    "title": "Paris street",
                    "file_path": "/media/paris.mp4",
                    "type": "video",
                },
            ],
            "images": [],
            "asset_stats": {
                "missing_visuals": [{"sentence_number": 2}],
            },
        },
    ]
    outcomes = visual_director.outcomes_from_processed_scenes(processed)
    assert outcomes[1]["covered"] is True
    assert outcomes[1]["tier"] == "related"
    assert outcomes[1]["scenario"] == {"place": "France"}
    assert outcomes[2]["starved"] is True
    assert outcomes[2]["covered"] is False
