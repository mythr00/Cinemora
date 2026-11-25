"""QA Report (Phase F, founder priority #8) — the unified
Documentary Quality Report over recorded production artefacts.

The report is ASSEMBLY ONLY (services/qa_report.py): verdicts and
metrics pass through exactly as the production recorded them, a
section whose artefact was never written reports "not_recorded",
an unparseable artefact reports "unavailable", and nothing is
recomputed. These tests write artefact fixtures shaped exactly
like a real production's (modelled on the stored Container run
fde5732c: production_report.json / visual_quality.json /
asset_report.json / delivery.json in the run's artefact directory)
and assert the API surfaces those recorded values — a quality
QUALITY_WARNING and a reliability UNKNOWN stay exactly that, the
per-metric thresholds recorded with the verdict are shown, and the
per-sentence drill-down joins only recorded listings.

Ownership: another user's project is a 404 on every QA route,
including the export.
"""

import json
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from database import SessionLocal, engine
from models import Base, Plan, Project, User, UserSubscription
from services import auth as auth_service
from services import media_paths, run_ledger

TOKEN = "qa-report-token"
PASSWORD = "qa-report-password-1"
HEADERS = {"X-API-Key": TOKEN}


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")  # E-21: explicit opt-in
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(fakeredis.FakeRedis(decode_responses=True))
    db = SessionLocal()
    try:
        if db.query(Plan).filter_by(name="free").one_or_none() is None:
            db.add(
                Plan(
                    id=str(uuid.uuid4()),
                    name="free",
                    max_concurrent_runs=1,
                    max_runs_per_period=100,
                    period="month",
                    priority_tier=0,
                    is_default=True,
                )
            )
            db.commit()
    finally:
        db.close()
    yield
    auth_service.reset_redis_client()


def _create_project(client, headers=HEADERS):
    response = client.post(
        "/api/v1/projects",
        headers=headers,
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


def _run_dir(project_id, run_id):
    path = media_paths.project_dir(project_id) / "runs" / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(run_dir, filename, payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (run_dir / filename).write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------
# Fixtures shaped like the real Container run fde5732c artefacts.
# ---------------------------------------------------------------------

PRODUCTION_REPORT = {
    "schema": "cinemora.production_report/1",
    "verdicts": {
        "integrity": {
            "verdict": "PASS",
            "failures": [],
            "warnings": [
                "missing_visuals: 11 sentence(s) had no verified "
                "visual (reported, never substituted)"
            ],
        },
        "quality": {
            "verdict": "QUALITY_WARNING",
            "metrics": {
                "direct_share": {
                    "value": 0.38095238095238093,
                    "band": "QUALITY_WARNING",
                    "basis": "PROVISIONAL",
                    "thresholds": (
                        "PASS >= 0.50; QUALITY_WARNING >= 0.30 and "
                        "< 0.50; FAIL < 0.30"
                    ),
                },
                "genuine_coverage_acceptable": {
                    "value": 0.7619047619047619,
                    "band": "PASS",
                    "basis": "PROVISIONAL",
                    "thresholds": (
                        "PASS >= 0.55; QUALITY_WARNING >= 0.35 and "
                        "< 0.55; FAIL < 0.35"
                    ),
                },
            },
            "fail_reasons": [],
            "warning_reasons": ["direct_share=0.381 -> QUALITY_WARNING"],
            "unknown_metrics": [],
        },
        "reliability": {
            "verdict": "UNKNOWN",
            "basis": {"discovery_failures": "PROVISIONAL"},
            "facts": {
                "discovery_failures_count": 0,
                "discovery_failures_state": "unknown",
                "failed_downloads": 9,
                "total_accepted_assets": 25,
            },
        },
    },
    "reconciliation": {
        "checks": [
            {
                "name": "sentence_count",
                "status": "match",
                "detail": "all sources agree on 21 sentences",
                "values": {"timeline_sentence_count_field": 21},
            },
            {
                "name": "assignment_counts",
                "status": "mismatch",
                "detail": "counts disagree",
                "values": {"asset_report": 25, "renderer_audit": 24},
            },
        ],
        "failures": ["assignment_counts"],
        "all_reconciled": False,
    },
    "questions": {
        "q01": {
            "question": "How many sentences does the production have?",
            "answer": 21,
            "state": "answered",
        },
        "q02": {
            "question": "How many sentences got a direct visual?",
            "answer": 5,
            "state": "answered",
        },
    },
    "sources": {"run_ledger": {"state": "unavailable"}},
}

VISUAL_QUALITY = {
    "schema": "cinemora.visual_quality/1",
    "sentence_outcomes": {
        "sentence_count": 3,
        "buckets": {"direct": 1, "approved_graphic": 1, "contextual": 1},
        "shares": {"direct": 0.333},
        "reconciles": True,
        "sentences_without_verified_visual": 1,
        "per_sentence": [
            {"sentence_number": 1, "outcome": "direct"},
            {"sentence_number": 2, "outcome": "approved_graphic"},
            {"sentence_number": 3, "outcome": "contextual"},
        ],
    },
    "tier_accounting": {
        "state": "recorded",
        "by_tier": {
            "direct": {"count": 4, "runtime_seconds": 38.127, "runtime_share": 0.254},
            "related": {"count": 6, "runtime_seconds": 41.32, "runtime_share": 0.276},
            "graphic": {"count": 11, "runtime_seconds": 70.496, "runtime_share": 0.470},
        },
        "broll_runtime_seconds": 0.0,
        "broll_share": 0.0,
        "unstamped_items": 0,
        "band": {
            "state": "recorded",
            "genre_pack": "industrial",
            "max": 0.5,
            "basis": "founder_band_provisional",
            "measured": 0.0,
            "evaluation": "within_band",
        },
    },
    "diversity": {
        "d1_unique_assets": 19,
        "d1_unique_assets_per_minute": 7.6,
        "d2_repeated_items": 2,
        "d2_repeated_item_rate": 0.095,
        "variety_ratio": 0.905,
    },
    "relevance": {
        "direct_assignment_share": 0.381,
        "pipeline_sentence_assignment_accuracy": 0.381,
        "human_adjudicated_relevance": {"value": None, "state": "unknown"},
    },
    "integrity_categories": {
        "metadata_suspected_mismatch": {
            "count": 2,
            "state": "recorded",
            "basis": "metadata",
            "sentence_numbers": [5, 6],
        },
        "frame_level_confirmation": {"count": None, "state": "not_performed"},
    },
    "uncovered_beats": {
        "count": 1,
        "source_field": "asset_report.missing_visuals",
        "sentence_numbers": [2],
    },
    "rejection_reasons": {
        "source": "candidate_records",
        "distribution": {"missing_subject": 366},
        "total": 536,
        "unknown": 0,
    },
    "runtime_share": {
        "graphics": {"seconds": 70.496, "fraction": 0.470},
        "moving": {"seconds": 0.0, "fraction": 0.0},
        "still": {"seconds": 79.447, "fraction": 0.530},
    },
    "genre_expectations": {
        "state": "recorded",
        "genre_pack": "industrial",
        "fields": {"moving_share": {"measured": 0.0, "evaluation": "UNKNOWN"}},
    },
}

ASSET_REPORT = {
    "coverage": {
        "total_sentences": 3,
        "covered_sentences": 3,
        "uncovered_sentences": [],
        "complete": True,
    },
    "tier_breakdown": {
        "state": "recorded",
        "per_sentence": [
            {"scene_number": 1, "sentence_index": 0, "tier": "related"},
            {"scene_number": 1, "sentence_index": 1, "tier": "graphic"},
            {"scene_number": 2, "sentence_index": 2, "tier": "direct"},
        ],
    },
    "missing_visuals": [
        {
            "scene": 1,
            "sentence_number": 2,
            "reason": "unsearched_role",
            "role": "motivation",
            "graphics_outcome": {"outcome": "generated", "family": "motion_engine"},
        }
    ],
    "missing_visual_count": 1,
    "verification_dimensions": {
        "rights": {"recorded_licences": 13, "unknown_licences": 0},
        "visual_frame": {
            "evaluated_assets": 13,
            "rejected_events": 1,
            "watermarked_preview_detected": 0,
            "ocr_engine": "rapidocr",
        },
    },
    "candidate_records": {
        "total": 610,
        "by_outcome": {"rejected": 536, "verified": 81, "accepted": 13},
        "by_stage": {"metadata_gate": 366, "frame_verification": 1},
        "records": [
            {
                "candidate_id": "cand_frame1",
                "scene": 2,
                "provider": "youtube",
                "kind": "video",
                "url": "https://www.youtube.com/watch?v=example",
                "outcome": "rejected",
                "stage": "frame_verification",
                "reason": "social_story_card",
                "primary_rejection_reason": "social_story_card",
                "verdict_reasons": ["story-card layout detected"],
            },
            {
                "candidate_id": "cand_gate1",
                "scene": 1,
                "provider": "archive.org",
                "kind": "video",
                "url": "https://archive.org/details/example",
                "outcome": "rejected",
                "stage": "metadata_gate",
                "reason": "missing_subject",
                "primary_rejection_reason": "missing_subject",
                "verdict_reasons": [],
            },
        ],
    },
    "rejection_funnel": {"events": 536, "distinct_candidates": 529},
    "total_retrieved_assets": 610,
    "candidates_evaluated": 610,
    "total_accepted_assets": 25,
    "total_rejected_assets": 529,
    "verified_moving_footage": 0,
    "verified_images": 9,
    "unverified_assets": 4,
    "failed_downloads": 9,
    "download_attempt_failures": 36,
    "low_confidence_selections": 4,
    "substitutions": {
        "text_cards": 0,
        "placeholder_clips": 0,
        "unverified_assets": 4,
        "generated_graphics": 12,
    },
    "authenticity": {"authentic": False, "verified_genuine_assets": 9},
    "final_audio": {
        "integrated_lufs": -14.94,
        "true_peak_dbtp": -1.2,
        "target_lufs": -14.0,
        "normalised": True,
    },
    "timing_consistency": {
        "divergence_seconds": 0.0,
        "tolerance_seconds": 0.15,
        "within_tolerance": True,
        "timing_source": "measured_part",
    },
    "captions_alignment": {"aligned": 0, "low_alignment": 0, "alignment_failed": 21},
    "unknown_licensing": 0,
    "rights_note": "RIGHTS_POLICY=allow_all: acceptance is based on relevance only.",
    "run_failure": None,
}

DELIVERY = {
    "profile": "delivery_v1_default",
    "status": "delivered",
    "duration_seconds": 149.933333,
    "master_bitrate_kbps": 12708.1,
    "delivery_bitrate_kbps": 1966.4,
    "decode_validated": True,
    "size_ratio": 0.1547,
}


def _write_full_artefacts(project_id, run_id):
    run_dir = _run_dir(project_id, run_id)
    _write(run_dir, "production_report.json", PRODUCTION_REPORT)
    _write(run_dir, "visual_quality.json", VISUAL_QUALITY)
    _write(run_dir, "asset_report.json", ASSET_REPORT)
    _write(run_dir, "delivery.json", DELIVERY)
    return run_dir


def _get(client, path, headers=HEADERS):
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------
# Routing / absence
# ---------------------------------------------------------------------


def test_latest_qa_report_404_without_runs(client):
    project = _create_project(client)
    response = client.get(
        f"/api/v1/projects/{project['id']}/qa-report", headers=HEADERS
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "no_runs"


def test_run_qa_report_404_for_run_of_another_project(client):
    project = _create_project(client)
    other = _create_project(client)
    run_id = _completed_run(other)
    response = client.get(
        f"/api/v1/projects/{project['id']}/runs/{run_id}/qa-report",
        headers=HEADERS,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "run_not_found"


def test_qa_report_not_recorded_without_artefacts(client):
    project = _create_project(client)
    run_id = _completed_run(project)

    report = _get(client, f"/api/v1/projects/{project['id']}/qa-report")
    assert report["schema"] == "cinemora.qa_report/1"
    assert report["run"]["run_id"] == run_id
    assert report["run"]["status"] == "completed"
    assert report["artefacts"] == {
        "production_report.json": "not_recorded",
        "visual_quality.json": "not_recorded",
        "asset_report.json": "not_recorded",
        "delivery.json": "not_recorded",
    }
    # Honest absence: no verdict is shown, and no section invents
    # a value.
    assert report["verdicts"]["state"] == "not_recorded"
    assert report["verdicts"]["verdicts"] is None
    assert report["reconciliation"]["checks"] is None
    assert report["coverage"]["sentence_outcomes"] is None
    assert report["tiers"]["tier_accounting"] is None
    assert report["frame_verification"]["summary"] is None
    assert report["sentences"]["sentences"] == []
    assert report["delivery"]["state"] == "not_recorded"
    assert report["delivery"]["status"] is None


# ---------------------------------------------------------------------
# Recorded values pass through untouched
# ---------------------------------------------------------------------


def test_qa_report_recorded_verdicts_and_metrics(client):
    project = _create_project(client)
    run_id = _completed_run(project)
    _write_full_artefacts(project["id"], run_id)

    report = _get(
        client, f"/api/v1/projects/{project['id']}/runs/{run_id}/qa-report"
    )
    assert report["artefacts"]["production_report.json"] == "recorded"

    verdicts = report["verdicts"]
    assert verdicts["state"] == "recorded"
    assert verdicts["schema"] == "cinemora.production_report/1"
    # Verdicts are NOT laundered: QUALITY_WARNING and UNKNOWN stay.
    assert verdicts["verdicts"]["integrity"]["verdict"] == "PASS"
    assert verdicts["verdicts"]["quality"]["verdict"] == "QUALITY_WARNING"
    assert verdicts["verdicts"]["reliability"]["verdict"] == "UNKNOWN"
    metric = verdicts["verdicts"]["quality"]["metrics"]["direct_share"]
    assert metric["value"] == 0.38095238095238093
    assert metric["band"] == "QUALITY_WARNING"
    assert metric["basis"] == "PROVISIONAL"
    assert metric["thresholds"].startswith("PASS >= 0.50")
    assert verdicts["verdicts"]["quality"]["warning_reasons"] == [
        "direct_share=0.381 -> QUALITY_WARNING"
    ]

    reconciliation = report["reconciliation"]
    assert reconciliation["all_reconciled"] is False
    assert reconciliation["failures"] == ["assignment_counts"]
    # Full recorded detail — the certificate shows name+status only.
    mismatch = reconciliation["checks"][1]
    assert mismatch["detail"] == "counts disagree"
    assert mismatch["values"] == {"asset_report": 25, "renderer_audit": 24}

    questions = report["questions"]["questions"]
    assert [q["id"] for q in questions] == ["q01", "q02"]
    assert questions[0]["answer"] == 21


def test_qa_report_coverage_tiers_and_sentences(client):
    project = _create_project(client)
    run_id = _completed_run(project)
    _write_full_artefacts(project["id"], run_id)

    report = _get(
        client, f"/api/v1/projects/{project['id']}/runs/{run_id}/qa-report"
    )
    coverage = report["coverage"]
    assert coverage["sentence_outcomes"]["buckets"]["approved_graphic"] == 1
    assert coverage["sentence_outcomes"]["reconciles"] is True
    assert coverage["asset_report_coverage"]["complete"] is True
    assert coverage["uncovered_beats"]["sentence_numbers"] == [2]

    tiers = report["tiers"]["tier_accounting"]
    assert tiers["by_tier"]["graphic"]["runtime_share"] == 0.470
    # B-roll share reported against its recorded band.
    assert tiers["band"]["max"] == 0.5
    assert tiers["band"]["measured"] == 0.0
    assert tiers["band"]["evaluation"] == "within_band"

    sentences = report["sentences"]["sentences"]
    assert [s["sentence_number"] for s in sentences] == [1, 2, 3]
    first, second, third = sentences
    assert first["outcome"] == "direct"
    assert first["tier"] == "related"
    assert first["missing_visual"] is None
    # Sentence 2 fell back to a generated graphic, and the record
    # says why.
    assert second["outcome"] == "approved_graphic"
    assert second["tier"] == "graphic"
    assert second["missing_visual"]["reason"] == "unsearched_role"
    assert second["missing_visual"]["graphics_outcome"]["outcome"] == "generated"
    assert third["outcome"] == "contextual"
    assert third["tier"] == "direct"


def test_qa_report_frame_verification_candidates_defects(client):
    project = _create_project(client)
    run_id = _completed_run(project)
    _write_full_artefacts(project["id"], run_id)

    report = _get(
        client, f"/api/v1/projects/{project['id']}/runs/{run_id}/qa-report"
    )
    frame = report["frame_verification"]
    assert frame["summary"]["evaluated_assets"] == 13
    assert frame["summary"]["ocr_engine"] == "rapidocr"
    assert frame["integrity_categories"]["metadata_suspected_mismatch"][
        "sentence_numbers"
    ] == [5, 6]
    # Only the frame-stage rejection is listed; the metadata-gate
    # rejection belongs to the funnel, not this drill-down.
    assert len(frame["rejections"]) == 1
    rejection = frame["rejections"][0]
    assert rejection["candidate_id"] == "cand_frame1"
    assert rejection["reason"] == "social_story_card"

    candidates = report["candidates"]
    assert candidates["totals"]["candidates_evaluated"] == 610
    assert candidates["totals"]["verified_images"] == 9
    assert candidates["funnel"]["by_stage"]["metadata_gate"] == 366
    assert candidates["rejection_reasons"]["distribution"] == {
        "missing_subject": 366
    }

    defects = report["defects"]
    assert defects["substitutions"]["text_cards"] == 0
    assert defects["substitutions"]["generated_graphics"] == 12
    assert defects["visual_quality_error"] is None

    audio = report["audio_captions"]
    assert audio["final_audio"]["integrated_lufs"] == -14.94
    assert audio["timing_consistency"]["within_tolerance"] is True
    assert audio["captions_alignment"]["alignment_failed"] == 21

    rights = report["rights"]
    assert rights["recorded"] == {"recorded_licences": 13, "unknown_licences": 0}

    delivery = report["delivery"]
    assert delivery["state"] == "recorded"
    assert delivery["status"] == "delivered"
    assert delivery["decode_validated"] is True
    assert delivery["duration_seconds"] == 149.933333


def test_qa_report_unavailable_when_production_report_unreadable(client):
    project = _create_project(client)
    run_id = _completed_run(project)
    run_dir = _run_dir(project["id"], run_id)
    _write(run_dir, "production_report.json", "{not json")
    _write(run_dir, "visual_quality.json", VISUAL_QUALITY)

    report = _get(
        client, f"/api/v1/projects/{project['id']}/runs/{run_id}/qa-report"
    )
    assert report["artefacts"]["production_report.json"] == "unavailable"
    assert report["verdicts"]["state"] == "unavailable"
    assert report["verdicts"]["verdicts"] is None
    assert report["reconciliation"]["state"] == "unavailable"
    # The measurement sections are unaffected by the broken report.
    assert report["coverage"]["state"] == "recorded"
    assert report["tiers"]["tier_accounting"]["band"]["evaluation"] == "within_band"


def test_latest_qa_report_describes_latest_production_run(client):
    project = _create_project(client)
    first = _completed_run(project)
    _write_full_artefacts(project["id"], first)
    second = _completed_run(project)
    run_dir = _run_dir(project["id"], second)
    newer = json.loads(json.dumps(PRODUCTION_REPORT))
    newer["verdicts"]["quality"]["verdict"] = "FAIL"
    _write(run_dir, "production_report.json", newer)

    report = _get(client, f"/api/v1/projects/{project['id']}/qa-report")
    assert report["run"]["run_id"] == second
    assert report["verdicts"]["verdicts"]["quality"]["verdict"] == "FAIL"


def test_qa_report_export_matches_run_report(client):
    project = _create_project(client)
    run_id = _completed_run(project)
    _write_full_artefacts(project["id"], run_id)

    response = client.get(
        f"/api/v1/projects/{project['id']}/runs/{run_id}/qa-report/export",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f"qa-report-{project['id']}-{run_id}.json" in disposition
    exported = response.json()
    on_screen = _get(
        client, f"/api/v1/projects/{project['id']}/runs/{run_id}/qa-report"
    )
    assert exported == on_screen


# ---------------------------------------------------------------------
# Ownership (Phase 8): cross-tenant is a 404 everywhere.
# ---------------------------------------------------------------------


def _register(client, email):
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(user):
    return {"Authorization": f"Bearer {user['session_token']}"}


def test_qa_report_cross_tenant_404(client):
    suffix = uuid.uuid4().hex[:8]
    a = _register(client, f"qa-a-{suffix}@example.com")
    b = _register(client, f"qa-b-{suffix}@example.com")
    project = _create_project(client, headers=_bearer(a))
    run_id = _completed_run(project)
    _write_full_artefacts(project["id"], run_id)

    for path in (
        f"/api/v1/projects/{project['id']}/qa-report",
        f"/api/v1/projects/{project['id']}/runs/{run_id}/qa-report",
        f"/api/v1/projects/{project['id']}/runs/{run_id}/qa-report/export",
    ):
        response = client.get(path, headers=_bearer(b))
        assert response.status_code == 404, (path, response.text)
    # FS-B (FS-10/FS-14): the QA report is admin-only — the
    # non-admin owner is now refused with 403 (cross-tenant stays
    # 404 above), and the operator principal still reads it.
    assert client.get(
        f"/api/v1/projects/{project['id']}/qa-report", headers=_bearer(a)
    ).status_code == 403
    assert client.get(
        f"/api/v1/projects/{project['id']}/qa-report", headers=HEADERS
    ).status_code == 200
