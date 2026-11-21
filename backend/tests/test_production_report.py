"""Phase 3 acceptance tests: quality verdicts, corrected metrics,
mutually exclusive outcome accounting, zero-vs-unknown, and the
reconciling production report (synthetic + stored artefacts).

Stored-profile tests read the two protected E2E baselines' artefacts
(checks only — no production is ever re-run). They skip ONLY when
the artefacts are genuinely absent.
"""

from pathlib import Path

import pytest

from services.asset_report import build_asset_report
from services.final_video_auditor import _audit_verdict
from services.production_report import (
    build_production_report,
    build_production_report_for_project,
)
from services.quality_verdict import (
    THRESHOLDS,
    composite_quality_verdict,
    evaluate_metric,
)
from services.visual_quality import measure_visual_quality


# ---------------------------------------------------------------------------
# Stored artefact location
# ---------------------------------------------------------------------------

def _stored_project(name):
    candidates = []
    here = Path(__file__).resolve()
    for parent in (here.parents[2], here.parents[3]):
        candidates.append(parent / "cinemora-audit" / "e2e-media" / "media" / name)
    candidates.append(Path.home() / "workspace" / "cinemora-audit" / "e2e-media" / "media" / name)
    for candidate in candidates:
        if (candidate / "asset_report.json").exists() and (candidate / "timeline.json").exists():
            return candidate
    return None


@pytest.fixture(scope="module")
def kohberger_dir():
    path = _stored_project("e2e-truecrime-kohberger")
    if path is None:
        pytest.skip("stored Kohberger artefacts genuinely absent")
    return path


@pytest.fixture(scope="module")
def berlin_dir():
    path = _stored_project("e2e-five-minute-berlin")
    if path is None:
        pytest.skip("stored Berlin artefacts genuinely absent")
    return path


# ---------------------------------------------------------------------------
# Band exclusivity + threshold labels
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,value,band", [
    ("direct_share", 0.50, "PASS"),
    ("direct_share", 0.499, "QUALITY_WARNING"),
    ("direct_share", 0.30, "QUALITY_WARNING"),
    ("direct_share", 0.299, "FAIL"),
    ("genuine_coverage_acceptable", 0.55, "PASS"),
    ("genuine_coverage_acceptable", 0.35, "QUALITY_WARNING"),
    ("genuine_coverage_acceptable", 0.349, "FAIL"),
    ("repeated_item_rate", 0.40, "PASS"),
    ("repeated_item_rate", 0.41, "QUALITY_WARNING"),
    ("repeated_item_rate", 0.65, "QUALITY_WARNING"),
    ("repeated_item_rate", 0.66, "FAIL"),
    ("unique_assets_per_minute", 3.0, "PASS"),
    ("unique_assets_per_minute", 1.5, "QUALITY_WARNING"),
    ("unique_assets_per_minute", 1.49, "FAIL"),
    ("variety_ratio", 0.50, "PASS"),
    ("variety_ratio", 0.25, "QUALITY_WARNING"),
    ("variety_ratio", 0.249, "FAIL"),
    ("rotation_share", 0.50, "PASS"),
    ("rotation_share", 0.51, "QUALITY_WARNING"),
    ("positional_share", 0.25, "PASS"),
    ("positional_share", 0.26, "QUALITY_WARNING"),
    ("single_provider_footage_share", 0.80, "PASS"),
    ("single_provider_footage_share", 0.81, "QUALITY_WARNING"),
])
def test_band_boundaries_land_in_exactly_one_band(name, value, band):
    evaluation = evaluate_metric(name, value)
    assert evaluation["band"] == band
    assert evaluation["value"] == value


def test_band_sweep_never_ambiguous():
    for name in THRESHOLDS:
        if name == "rotation_dominance":
            continue
        for step in range(0, 101):
            evaluation = evaluate_metric(name, step / 100.0)
            assert evaluation["band"] in ("PASS", "QUALITY_WARNING", "FAIL")


def test_unknown_value_has_no_band():
    evaluation = evaluate_metric("direct_share", None)
    assert evaluation["band"] is None
    assert evaluation["basis"] == "PROVISIONAL"


def test_every_evaluated_metric_carries_a_basis_label():
    measurement = measure_visual_quality(timeline={}, asset_report={}, rights_manifest=[])
    verdict = composite_quality_verdict(measurement, {})
    for name, evaluation in verdict["metrics"].items():
        assert evaluation["basis"] in ("ESTABLISHED", "DERIVED", "PROVISIONAL"), name
        assert evaluation["thresholds"], name


# ---------------------------------------------------------------------------
# Stored profiles
# ---------------------------------------------------------------------------

def test_kohberger_stored_profile(kohberger_dir):
    report = build_production_report_for_project(kohberger_dir)
    assert report["schema"] == "cinemora.production_report/1"
    assert report["verdicts"]["quality"]["verdict"] == "FAIL"
    assert report["verdicts"]["integrity"]["verdict"] == "PASS"
    # B-Roll B0 supersession: q26 (tier / B-roll share) is additive;
    # the report now answers 26 questions. q01–q25 are unchanged.
    assert len(report["questions"]) == 26

    # Re-measure for the metric-level assertions.
    from services.visual_quality import measure_project
    measurement = measure_project(kohberger_dir)
    diversity = measurement["diversity"]
    assert diversity["d1_unique_assets"] == 3
    assert diversity["d1_unique_assets_per_minute"] == pytest.approx(0.915, rel=0.02)
    quality = report["verdicts"]["quality"]
    assert quality["metrics"]["repeated_item_rate"]["band"] == "FAIL"

    outcomes = measurement["sentence_outcomes"]
    assert outcomes["sentence_count"] == 23
    assert sum(outcomes["buckets"].values()) == 23
    assert outcomes["buckets"]["direct"] == 2
    assert outcomes["buckets"]["contextual"] == 21
    assert outcomes["buckets"]["uncovered"] == 0
    assert outcomes["sentences_without_verified_visual"] == 18
    assert outcomes["reconciles"] is True

    reconciliation = report["reconciliation"]
    assert reconciliation["failures"] == []
    assert reconciliation["all_reconciled"] is True
    by_name = {c["name"]: c for c in reconciliation["checks"]}
    assert by_name["repeated_footage"]["status"] in ("match", "explained_difference")


def test_berlin_stored_profile(berlin_dir):
    report = build_production_report_for_project(berlin_dir)
    from services.visual_quality import measure_project
    import json
    measurement = measure_project(berlin_dir)
    asset_report = json.loads((berlin_dir / "asset_report.json").read_text())

    diversity = measurement["diversity"]
    assert diversity["d1_unique_assets"] == 27
    assert diversity["d1_unique_assets_per_minute"] == pytest.approx(4.92, rel=0.02)

    outcomes = measurement["sentence_outcomes"]
    assert sum(outcomes["buckets"].values()) == 43
    assert outcomes["buckets"]["direct"] == 22
    assert outcomes["buckets"]["approved_graphic"] == 3
    assert outcomes["buckets"]["contextual"] == 18

    # Honest computed band: equals the composite of its own metrics,
    # not a forced PASS.
    expected = composite_quality_verdict(measurement, asset_report)
    assert report["verdicts"]["quality"]["verdict"] == expected["verdict"]
    assert report["verdicts"]["quality"]["metrics"]["direct_share"]["band"] == "PASS"
    assert report["verdicts"]["quality"]["metrics"]["repeated_item_rate"]["band"] == "PASS"

    categories = measurement["integrity_categories"]
    assert categories["frame_level_confirmation"]["state"] == "not_performed"
    assert categories["metadata_suspected_mismatch"]["count"] >= 18


# ---------------------------------------------------------------------------
# Zero vs unknown
# ---------------------------------------------------------------------------

def test_provider_failures_zero_vs_unknown():
    legacy = measure_visual_quality(
        timeline={}, asset_report={"discovery_failures": []}, rights_manifest=[]
    )
    assert legacy["provider_failures"]["state"] == "unknown"

    recorded = measure_visual_quality(
        timeline={},
        asset_report={"discovery_failures": [], "discovery_failures_state": "recorded"},
        rights_manifest=[],
    )
    assert recorded["provider_failures"]["state"] == "recorded"
    assert recorded["provider_failures"]["count"] == 0

    absent = measure_visual_quality(timeline={}, asset_report={}, rights_manifest=[])
    assert absent["provider_failures"]["state"] == "unknown"
    assert absent["provider_failures"]["count"] is None


def test_discovery_failures_state_in_build_asset_report():
    with_key = build_asset_report(
        [{"asset_stats": {"discovery_failures": []}, "videos": [], "images": []}]
    )
    assert with_key["discovery_failures_state"] == "recorded"
    without_key = build_asset_report(
        [{"asset_stats": {}, "videos": [], "images": []}]
    )
    assert without_key["discovery_failures_state"] == "not_collected"


# ---------------------------------------------------------------------------
# Mutually exclusive outcome accounting (synthetic)
# ---------------------------------------------------------------------------

def test_outcome_accounting_one_sentence_per_bucket():
    def item(sentence_index, assignment, **extra):
        base = {
            "type": "video", "duration": 5.0,
            "file_path": f"/p/{sentence_index}.mp4", "path": f"/p/{sentence_index}.mp4",
            "source": "src", "assignment": assignment,
            "sentence_index": sentence_index, "scene_number": 1,
        }
        base.update(extra)
        return base

    timeline = {
        "sentence_count": 5,
        "coverage": {"total_sentences": 5, "complete": True, "uncovered_sentences": []},
        "scenes": [{"scene_number": 1, "items": [
            item(0, "sentence"),
            item(1, "motion_graphic"),
            item(2, "holdover"),
            item(3, "rotation"),
            # sentence 5 (index 4) has no item -> uncovered
        ]}],
    }
    report = {"coverage": {"total_sentences": 5}, "missing_visuals": [], "missing_visual_count": 0}
    result = measure_visual_quality(timeline=timeline, asset_report=report, rights_manifest=[])
    outcomes = result["sentence_outcomes"]
    assert outcomes["sentence_count"] == 5
    # Stage 7 / R7 (C.B.3): the buckets gained three profile keys
    # (direct_contextual_profile / direct_environmental_profile /
    # direct_atmosphere_profile) for profile-stamped direct items;
    # this unstamped fixture counts zero in all three.
    assert outcomes["buckets"] == {
        "direct": 1, "approved_graphic": 1, "contextual": 1,
        "editorial_holdover": 1, "uncovered": 1,
        "direct_contextual_profile": 0,
        "direct_environmental_profile": 0,
        "direct_atmosphere_profile": 0,
    }
    assert outcomes["reconciles"] is True
    assert len(outcomes["per_sentence"]) == 5


def test_holdover_flag_counts_as_editorial_holdover():
    timeline = {
        "sentence_count": 1,
        "scenes": [{"scene_number": 1, "items": [{
            "type": "video", "duration": 5.0, "file_path": "/p/a.mp4",
            "assignment": "rotation", "holdover": True, "sentence_index": 0,
        }]}],
    }
    result = measure_visual_quality(timeline=timeline, asset_report={}, rights_manifest=[])
    # holdover outranks contextual in the precedence order
    assert result["sentence_outcomes"]["buckets"]["editorial_holdover"] == 1
    assert result["sentence_outcomes"]["buckets"]["contextual"] == 0


# ---------------------------------------------------------------------------
# Auditor integration: quality never rewrites integrity
# ---------------------------------------------------------------------------

def test_auditor_integration_quality_fail_integrity_passes(kohberger_dir):
    import json
    from services.visual_quality import measure_project
    asset_report = json.loads((kohberger_dir / "asset_report.json").read_text())
    asset_report["visual_quality"] = measure_project(kohberger_dir)
    verdict = _audit_verdict(asset_report)
    assert verdict["quality"]["verdict"] == "FAIL"
    assert verdict["integrity"]["verdict"] == "PASS"
    assert verdict["passed"] is True


def test_production_report_questions_shape(kohberger_dir):
    report = build_production_report_for_project(kohberger_dir)
    # B-Roll B0 supersession: additive q26 extends the shape to q01–q26.
    assert sorted(report["questions"].keys()) == [f"q{i:02d}" for i in range(1, 27)]
    for entry in report["questions"].values():
        assert set(entry.keys()) == {"question", "answer", "state"}
        assert entry["state"] in ("answered", "unknown", "unavailable")
