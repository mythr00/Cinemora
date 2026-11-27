"""Phase 6–7 — advanced treatments (Motion Graphics Programme,
2026-11-27).

B6-advanced: (a) the before_after template wired to a recorded
comparison contract (two recorded states/values with labels) —
refuses without two recorded sides; (b) uncertainty in the chart
family — recorded estimate ranges render as a band/error bars
and estimated values are labelled "estimated" in the source
line, never presented as exact.

D1: the scenario-fed labelled_diagram treatment (see the D1
section below).
"""

import json

from services import graphics_pipeline, visual_director
from services.motion_engine import templates_v4
from services.motion_engine.templates_v3 import line_chart
from services.motion_engine.templates_v5 import labelled_diagram


# ---------------------------------------------------------------------------
# Helpers (the Phase 5 core-treatments manifest shape)
# ---------------------------------------------------------------------------

def _manifest(entries):
    return {"version": 1, "project_id": "p67", "theme": None,
            "entries": entries}


def _entry(manifest_id, scene, index, number, text, templates,
           scenario=None, **extra):
    entry = {
        "manifest_id": manifest_id,
        "target": {
            "scene_number": scene, "sentence_index": index,
            "sentence_number": number,
        },
        "text": text,
        "scenario": scenario or {},
        "purpose": "coverage_fallback",
        "template_candidates": templates,
        "family_candidates": ["event_schematic"],
        "theme": None,
        "fact_basis": "script_claim",
        "evidence_ref": None,
        "verified_data": None,
    }
    entry.update(extra)
    return entry


def _build(tmp_path, entries, bible=None):
    data = {"scenes": [], "case_bible": bible or {}}
    return graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, manifest=_manifest(entries),
    )


# ---------------------------------------------------------------------------
# B6(a) — before_after from a recorded comparison contract
# ---------------------------------------------------------------------------

def test_before_after_states_template_renders(tmp_path):
    out = tmp_path / "ba.mp4"
    path, meta = templates_v4.before_after(
        {
            "states": [
                {"label": "Before", "value": "12"},
                {"label": "After", "value": "30"},
            ],
            "title": "The Change",
        },
        "editorial", out, duration=1.0,
    )
    assert path is not None
    assert meta["inputs"]["states"] is True
    assert meta["inputs"]["before_label"] == "Before"
    assert meta["inputs"]["after_label"] == "After"


def test_before_after_states_template_refuses_one_side(tmp_path):
    path, meta = templates_v4.before_after(
        {"states": [{"label": "Before", "value": "12"}]},
        "editorial", tmp_path / "ba.mp4", duration=1.0,
    )
    assert path is None
    assert meta["reason"] == "missing_pair"


def test_before_after_renders_through_chain_from_contract(tmp_path):
    entries = [_entry(
        "gm_0001", 1, 0, 1, "Everything changed.", ["before_after"],
        scenario={"comparison": {
            "before": {"label": "1900", "value": 12},
            "after": {"label": "1950", "value": 30},
        }},
    )]
    assets = _build(tmp_path, entries)
    assert len(assets) == 1
    record = assets[0]
    assert record["template"] == "before_after"
    states = record["engine_inputs"]["states"]
    assert [s["label"] for s in states] == ["1900", "1950"]
    assert [s["value"] for s in states] == ["12", "30"]


def test_before_after_chain_refuses_single_recorded_side(tmp_path):
    entries = [_entry(
        "gm_0001", 1, 0, 1, "Everything changed.", ["before_after"],
        scenario={"comparison": {
            "before": {"label": "1900", "value": 12},
        }},
    )]
    assets = _build(tmp_path, entries)
    assert assets == []
    manifest = json.loads(
        (tmp_path / "motion_graphics.json").read_text()
    )
    outcome = manifest["targets"]["outcomes"][0]
    assert outcome["outcome"] == "skipped"


def test_before_after_chain_refuses_unlabelled_side(tmp_path):
    entries = [_entry(
        "gm_0001", 1, 0, 1, "Everything changed.", ["before_after"],
        scenario={"comparison": {
            "states": [
                {"label": "1900", "value": 12},
                {"value": 30},
            ],
        }},
    )]
    assert _build(tmp_path, entries) == []


# ---------------------------------------------------------------------------
# B6(b) — uncertainty in the chart family
# ---------------------------------------------------------------------------

def _recorded_line_data(**extra):
    data = {
        "points": [
            {"label": "1900", "value": 10, "display": "10"},
            {"label": "1910", "value": 14, "display": "14"},
            {"label": "1920", "value": 19, "display": "19"},
        ],
        "title": "Output",
        "recorded": True,
        "units": "tonnes",
        "source": "Harbour ledger",
    }
    data.update(extra)
    return data


def test_line_chart_renders_uncertainty_band(tmp_path):
    data = _recorded_line_data()
    for point, low, high in zip(
            data["points"], (8, 11, 15), (12, 17, 24)):
        point["low"] = low
        point["high"] = high
    path, meta = line_chart(
        data, "editorial", tmp_path / "line.mp4", duration=1.0)
    assert path is not None
    assert meta["inputs"]["uncertainty"] is True
    # Ranges ARE estimates: the series is labelled estimated.
    assert meta["inputs"]["estimated"] is True


def test_line_chart_estimated_without_ranges_is_labelled(tmp_path):
    data = _recorded_line_data(estimated=True)
    path, meta = line_chart(
        data, "editorial", tmp_path / "line.mp4", duration=1.0)
    assert path is not None
    assert meta["inputs"]["uncertainty"] is False
    assert meta["inputs"]["estimated"] is True


def test_line_chart_exact_series_is_not_labelled_estimated(tmp_path):
    path, meta = line_chart(
        _recorded_line_data(), "editorial",
        tmp_path / "line.mp4", duration=1.0)
    assert path is not None
    assert meta["inputs"]["estimated"] is False
    assert meta["inputs"]["uncertainty"] is False


def test_uncertainty_rides_the_recorded_chart_contract(tmp_path):
    """Through the chain: a scenario chart contract whose points
    carry recorded low/high ranges + an uncertainty note reaches
    the template as engine inputs (band + estimated label)."""
    entries = [_entry(
        "gm_0001", 1, 0, 1, "Output grew.", ["line_chart"],
        scenario={"chart": {
            "series": [
                {"label": "1900", "value": 10, "low": 8, "high": 12},
                {"label": "1910", "value": 14, "low": 11, "high": 17},
                {"label": "1920", "value": 19, "low": 15, "high": 24},
            ],
            "units": "tonnes",
            "source": "Harbour ledger",
            "uncertainty": {"note": "Reconstructed from manifests"},
        }},
    )]
    assets = _build(tmp_path, entries)
    assert len(assets) == 1
    record = assets[0]
    assert record["template"] == "line_chart"
    inputs = record["engine_inputs"]
    assert inputs["uncertainty"]["estimated"] is True
    assert inputs["uncertainty"]["note"] == (
        "Reconstructed from manifests")
    assert inputs["estimated"] is True
    points = inputs["points"]
    assert points[0]["low"] == 8 and points[0]["high"] == 12
    assert record["engine_metadata"]["inputs"]["uncertainty"] is True
    assert record["engine_metadata"]["inputs"]["estimated"] is True


# ---------------------------------------------------------------------------
# D1 — labelled_diagram from the recorded diagram contract
# ---------------------------------------------------------------------------

_PARTS = [
    {"name": "Piston", "description": "Drives the shaft",
     "at": [0.25, 0.4]},
    {"name": "Valve", "description": "Admits steam",
     "at": [0.6, 0.4]},
    {"name": "Flywheel", "description": "Smooths the stroke",
     "at": [0.8, 0.65]},
]


def test_labelled_diagram_renders_recorded_parts(tmp_path):
    path, meta = labelled_diagram(
        {"parts": _PARTS, "title": "Steam Engine"},
        "editorial", tmp_path / "diagram.mp4", duration=1.0)
    assert path is not None
    assert meta["inputs"]["parts"] == 3
    assert meta["inputs"]["positions"] == "recorded"


def test_labelled_diagram_grid_layout_without_positions(tmp_path):
    parts = [{"name": n} for n in ("Leaf", "Stem", "Root", "Flower")]
    path, meta = labelled_diagram(
        {"parts": parts}, "editorial",
        tmp_path / "diagram.mp4", duration=1.0)
    assert path is not None
    assert meta["inputs"]["positions"] == "ordered_grid"


def test_labelled_diagram_refuses_fewer_than_two_named_parts(tmp_path):
    path, meta = labelled_diagram(
        {"parts": [{"name": "Piston"}, {"description": "unnamed"}]},
        "editorial", tmp_path / "diagram.mp4", duration=1.0)
    assert path is None
    assert meta["reason"] == "insufficient_parts"


def test_labelled_diagram_refuses_overflowing_description(tmp_path):
    parts = [
        {"name": "Piston",
         "description": " ".join(["word"] * 60)},
        {"name": "Valve"},
    ]
    path, meta = labelled_diagram(
        {"parts": parts}, "editorial",
        tmp_path / "diagram.mp4", duration=1.0)
    assert path is None
    assert meta["reason"] == "label_does_not_fit"


def test_labelled_diagram_renders_through_chain(tmp_path):
    entries = [_entry(
        "gm_0001", 1, 0, 1, "The engine works in stages.",
        ["labelled_diagram"],
        scenario={"diagram": {"parts": _PARTS}},
    )]
    assets = _build(tmp_path, entries)
    assert len(assets) == 1
    record = assets[0]
    assert record["template"] == "labelled_diagram"
    names = [p["name"] for p in record["engine_inputs"]["parts"]]
    assert names == ["Piston", "Valve", "Flywheel"]


def test_labelled_diagram_chain_refuses_without_contract(tmp_path):
    entries = [_entry(
        "gm_0001", 1, 0, 1, "The engine works in stages.",
        ["labelled_diagram"], scenario={},
    )]
    assert _build(tmp_path, entries) == []


def test_director_labelled_diagram_candidacy_from_contract():
    plan = {
        "version": 1, "project_id": "p67", "analysis_mode": "llm",
        "scene_governor": None,
        "sentences": [{
            "sentence_number": 1, "scene_number": 1,
            "role": "statement",
            "primary_visual_intent": "exact",
            "secondary_visual_intents": [], "intent_queries": {},
            "graphic_fallback": {"allowed": True, "kind": None},
            "preferred_media_type": "video",
            "specificity_requirement": "exact",
            "visual_misrepresentation_risk": "low",
            "intents_source": "llm_plan", "fallback_concepts": None,
            "concepts_source": None, "entry_tier": "direct",
        }],
    }
    outcomes = {
        1: {
            "sentence_number": 1, "scene_number": 1,
            "sentence_index": 0, "text": "The pump has two parts.",
            "scenario": {"diagram": {"parts": _PARTS}},
            "covered": False, "starved": True, "tier": None,
            "asset_title": None, "asset_file": None,
            "scene_photo_count": 0, "secondary_video": None,
            "scene_has_identity_photo": False,
        },
    }
    _plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan, outcomes)
    entry = manifest["entries"][0]
    assert entry["story_problem"] == "explanation"
    assert entry["template"] == "labelled_diagram"


def test_estimated_flag_without_ranges_rides_the_contract(tmp_path):
    entries = [_entry(
        "gm_0001", 1, 0, 1, "Output grew.", ["line_chart"],
        scenario={"chart": {
            "series": [
                {"label": "1900", "value": 10},
                {"label": "1910", "value": 14},
            ],
            "units": "tonnes",
            "source": "Harbour ledger",
            "estimated": True,
        }},
    )]
    assets = _build(tmp_path, entries)
    assert len(assets) == 1
    inputs = assets[0]["engine_inputs"]
    assert inputs["estimated"] is True
    assert inputs["uncertainty"]["estimated"] is True
    assert assets[0]["engine_metadata"]["inputs"]["estimated"] is True
    assert assets[0]["engine_metadata"]["inputs"]["uncertainty"] is False
