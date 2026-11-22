"""Phase 7 — P0 surfacing: the planner block reaches the plan
document, the asset report, the production report (q30) and the
provenance certificate (DM-1 / DM-2(a)).

Every surface states the mode honestly; a surface with no recorded
block says nothing (unknown), never a fabricated "llm".
"""

import json
from pathlib import Path

import pytest

from services import plan_document
from services import story_understanding as story
from services.asset_report import build_asset_report
from services.production_report import build_production_report

FIXTURES = Path(__file__).parent / "fixtures" / "golden_plans"


@pytest.fixture(scope="module")
def degraded_block():
    scripts = json.loads((FIXTURES / "degraded_scripts.json").read_text())
    sentences = scripts["film1_spierer"]["sentences"]
    bible, _plans, mode = story.plan_script(
        " ".join(sentences), sentences, None, cache_path=None
    )
    assert mode == "degraded"
    return bible["planner"]


def test_planner_block_shape(degraded_block):
    assert degraded_block["mode"] == "degraded"
    assert degraded_block["planner_version"] == "degraded-2.0"
    assert degraded_block["degraded_reason"] == "no_key"
    assert degraded_block["degraded_sentences"] == degraded_block["sentences"] == 11
    assert degraded_block["kb"]["network_enabled"] is True
    quality = degraded_block["quality"]
    assert quality["entities"] >= 3
    assert quality["scenarios_per_sentence"] > 1.0
    assert quality["exact_share"] is not None
    # D-1 evidence is recorded on the block (never drives stamps).
    assert quality["specificity_basis"]


def test_plan_document_exposes_the_planner_block(degraded_block):
    document = plan_document.plan_from_analysis(
        {"bible": {}, "sentences": [], "scenes": [], "planner": degraded_block},
        "project-1",
    )
    assert document["planner"]["planner_version"] == "degraded-2.0"
    assert document["planner"]["degraded_reason"] == "no_key"


def test_asset_report_carries_the_planner_block(degraded_block):
    report = build_asset_report([], analysis_mode="degraded", planner=degraded_block)
    assert report["planner"]["mode"] == "degraded"
    assert report["planner"]["degraded_reason"] == "no_key"
    # Without a block the field is None — unstated, never guessed.
    assert build_asset_report([])["planner"] is None


def test_production_report_q30_answers_from_the_block(degraded_block):
    report = build_production_report(
        measurement={},
        asset_report={"planner": degraded_block},
        timeline={},
        rights_manifest=[],
    )
    q30 = report["questions"]["q30"]
    assert q30["state"] == "answered"
    assert q30["answer"]["mode"] == "degraded"
    assert q30["answer"]["planner_version"] == "degraded-2.0"
    assert q30["answer"]["degraded_reason"] == "no_key"
    assert q30["answer"]["degraded_sentences"] == 11


def test_production_report_q30_unknown_without_a_block():
    report = build_production_report(
        measurement={}, asset_report={}, timeline={}, rights_manifest=[]
    )
    assert report["questions"]["q30"]["state"] == "unknown"


def test_certificate_carries_the_planner_block(degraded_block):
    certificate = plan_document.build_certificate(
        {"id": "p1", "title": "Film", "category": "Documentary"},
        {"run_id": "r1", "status": "completed"},
        [],
        {},
        planner=degraded_block,
    )
    run = certificate["run"]
    assert run["analysis_mode"] == "degraded"
    assert run["planner_version"] == "degraded-2.0"
    assert run["degraded_reason"] == "no_key"
    assert run["planner_quality"] == degraded_block["quality"]


def test_certificate_omits_planner_fields_without_a_block():
    certificate = plan_document.build_certificate(
        {"id": "p1", "title": "Film", "category": "Documentary"},
        {"run_id": "r1", "status": "completed"},
        [],
        {},
    )
    assert "planner_version" not in certificate["run"]
    assert "degraded_reason" not in certificate["run"]
