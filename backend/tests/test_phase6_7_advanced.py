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

from services import graphics_pipeline
from services.motion_engine import templates_v4
from services.motion_engine.templates_v3 import line_chart


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
