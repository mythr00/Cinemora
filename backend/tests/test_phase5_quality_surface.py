"""Quality Recovery Phase 5 — certificate quality surface.

The Phase 3 production flow records production_report.json (the
integrity / quality / reliability verdicts + reconciliation) and
visual_quality.json (the measurement) in the project directory.
The certificate endpoint surfaces those records additively:

- a production that predates the report carries
  quality.state == "not_recorded" — never a fabricated verdict;
- a present-but-unreadable report carries "unavailable";
- a recorded report passes its verdicts through untouched
  (a quality FAIL and a reliability UNKNOWN stay exactly that)
  with a metrics summary (diversity D1/D2 + per-sentence outcome
  accounting) and the reconciliation check summary.

The ledger sections of the certificate are unchanged.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from database import engine
from models import AssetDecision, Base, ProductionRun, Project
from services import media_paths, run_ledger

TOKEN = "phase5-token"
HEADERS = {"X-API-Key": TOKEN}


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    api_v1.rate_limiter.reset()
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Project.__table__,
            ProductionRun.__table__,
            AssetDecision.__table__,
        ],
    )
    yield


def _create_project(client):
    response = client.post(
        "/api/v1/projects",
        headers=HEADERS,
        json={"script": f"Script {uuid.uuid4()}. Alpha one. Beta two."},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _completed_run(project):
    submission = run_ledger.submit_production(project["id"], project["script"])
    run_id = submission["run_id"]
    run_ledger.claim_run(run_id)
    run_ledger.finish_run(run_id, "completed")
    return run_id


def _project_dir(project_id):
    path = media_paths.project_dir(project_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _certificate(client, project_id):
    response = client.get(
        f"/api/v1/projects/{project_id}/certificate", headers=HEADERS
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_certificate_quality_not_recorded_without_artefacts(client):
    project = _create_project(client)
    _completed_run(project)

    cert = _certificate(client, project["id"])
    # The ledger certificate itself is intact.
    assert cert["run"]["status"] == "completed"
    quality = cert["quality"]
    assert quality["state"] == "not_recorded"
    assert quality["verdicts"] is None
    assert quality["metrics"] is None
    assert quality["reconciliation"] is None


def test_certificate_quality_recorded_passes_verdicts_through(client):
    project = _create_project(client)
    _completed_run(project)
    project_dir = _project_dir(project["id"])
    report = {
        "schema": "cinemora.production_report/1",
        "verdicts": {
            "integrity": {"verdict": "PASS", "failures": [], "warnings": []},
            "quality": {
                "verdict": "FAIL",
                "metrics": {},
                "fail_reasons": ["d2_repeated_item_rate=0.87 -> FAIL"],
                "warning_reasons": [],
                "unknown_metrics": [],
            },
            "reliability": {
                "verdict": "UNKNOWN",
                "basis": {},
                "facts": {"discovery_failures_state": "unknown"},
            },
        },
        "reconciliation": {
            "checks": [
                {"name": "sentence_count", "status": "match",
                 "detail": "all sources agree", "values": {}},
                {"name": "assignment_counts", "status": "mismatch",
                 "detail": "counts disagree", "values": {}},
            ],
            "failures": ["assignment_counts"],
            "all_reconciled": False,
        },
        "questions": {},
        "sources": {},
    }
    (project_dir / "production_report.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    measurement = {
        "diversity": {
            "d1_unique_assets": 3,
            "d1_unique_assets_per_minute": 0.915,
            "d2_repeated_items": 20,
            "d2_repeated_item_rate": 0.87,
        },
        "sentence_outcomes": {
            "sentence_count": 23,
            "buckets": {
                "direct": 2,
                "approved_graphic": 0,
                "contextual": 21,
                "editorial_holdover": 0,
                "uncovered": 0,
            },
            "shares": {"direct": 0.087},
            "reconciles": True,
            "sentences_without_verified_visual": 18,
            "per_sentence": [{"sentence_number": 1, "outcome": "direct"}],
        },
        "timeline": {"timeline_item_count": 23, "max_reuse_count": 8},
    }
    (project_dir / "visual_quality.json").write_text(
        json.dumps(measurement), encoding="utf-8"
    )

    cert = _certificate(client, project["id"])
    quality = cert["quality"]
    assert quality["state"] == "recorded"
    assert quality["schema"] == "cinemora.production_report/1"
    # Verdicts pass through untouched — FAIL and UNKNOWN are not
    # laundered into anything softer.
    assert quality["verdicts"]["integrity"]["verdict"] == "PASS"
    assert quality["verdicts"]["quality"]["verdict"] == "FAIL"
    assert quality["verdicts"]["reliability"]["verdict"] == "UNKNOWN"
    metrics = quality["metrics"]
    assert metrics["diversity"]["d1_unique_assets"] == 3
    assert metrics["diversity"]["d1_unique_assets_per_minute"] == 0.915
    outcomes = metrics["sentence_outcomes"]
    assert outcomes["sentence_count"] == 23
    assert outcomes["buckets"]["contextual"] == 21
    assert outcomes["reconciles"] is True
    # The per-sentence listing stays out of the summary payload.
    assert "per_sentence" not in outcomes
    assert metrics["timeline_item_count"] == 23
    assert metrics["max_reuse_count"] == 8
    reconciliation = quality["reconciliation"]
    assert reconciliation["all_reconciled"] is False
    assert reconciliation["failures"] == ["assignment_counts"]
    assert reconciliation["checks"] == [
        {"name": "sentence_count", "status": "match"},
        {"name": "assignment_counts", "status": "mismatch"},
    ]


def test_certificate_quality_unavailable_when_report_unreadable(client):
    project = _create_project(client)
    _completed_run(project)
    project_dir = _project_dir(project["id"])
    (project_dir / "production_report.json").write_text(
        "{not json", encoding="utf-8"
    )

    cert = _certificate(client, project["id"])
    assert cert["quality"]["state"] == "unavailable"
    assert cert["quality"]["verdicts"] is None
