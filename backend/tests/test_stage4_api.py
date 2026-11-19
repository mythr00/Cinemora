"""Stage 4 — API v1 contract tests.

Exercises the /api/v1 surface end-to-end through FastAPI's
TestClient (which works in this venv without httpx installed as a
top-level import — starlette's test transport is self-contained):

- auth: 401 without/with a wrong token, 503 fail-closed when
  CINEMORA_API_TOKEN is unset, health open either way;
- project CRUD shapes + 404s in the v1 error shape;
- the 60,000-char script guard (422) and the POST rate limit (429);
- run submission dedup through the API and cancellation;
- plan generation (analysis stubbed) → per-sentence plan shape;
- plan decisions: approve/regenerate recording, a pre-run replace
  (probed, rights unknown), and a post-run replace through Stage 1's
  evidence-safe swap (fresh probe + fresh verdict + unknown rights,
  nothing inherited) with the ledger swapped_out/swapped_in pair;
- the plan-approval gate: gated submission runs analysis only, the
  run parks in awaiting_approval, approve releases the SAME run
  into the full pipeline carrying the regenerate marks;
- assets pagination over ledger decisions;
- media serving by recorded-artefact key only (valid key serves
  bytes; unknown keys and traversal attempts 404);
- the provenance certificate against a fixture ledger;
- migration 0003 (title/options) preserving rows from 0002.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import api_v1
import jobs
import main
from database import SessionLocal, engine
from models import AssetDecision, Base, ProductionRun, Project
from services import media_paths, plan_document, run_ledger

TOKEN = "stage4-test-token"
HEADERS = {"X-API-Key": TOKEN}
BACKEND = Path(__file__).resolve().parent.parent


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


class FakeQueue:
    def __init__(self):
        self.calls = []

    def enqueue(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return SimpleNamespace(id=f"job-{len(self.calls)}")


def _create_project(client, script=None, **extra):
    body = {
        "script": script
        or f"Script {uuid.uuid4()}. Alpha sentence one. Beta sentence two.",
    }
    body.update(extra)
    response = client.post("/api/v1/projects", headers=HEADERS, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _project_dir(project_id):
    path = media_paths.project_dir(project_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------


def test_health_is_open(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_auth_required_and_wrong_token_rejected(client):
    missing = client.get("/api/v1/projects")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"

    wrong = client.get("/api/v1/projects", headers={"X-API-Key": "nope"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "unauthorized"


def test_auth_unconfigured_fails_closed(client, monkeypatch):
    monkeypatch.delenv("CINEMORA_API_TOKEN", raising=False)
    response = client.get("/api/v1/projects", headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "api_not_configured"
    # Health stays open even while the API is closed.
    assert client.get("/api/v1/health").status_code == 200


# ---------------------------------------------------------------------
# Projects CRUD + guards
# ---------------------------------------------------------------------


def test_project_crud_shapes(client):
    created = _create_project(
        client,
        title="The Yangtze Incident",
        category="History",
        options={"genre": "history", "voice_id": "default"},
    )
    assert created["title"] == "The Yangtze Incident"
    assert created["category"] == "History"
    assert created["status"] == "created"
    assert created["options"]["genre"] == "history"
    assert created["script"].startswith("Script ")

    listing = client.get("/api/v1/projects", headers=HEADERS).json()
    assert any(p["id"] == created["id"] for p in listing["projects"])

    detail = client.get(
        f"/api/v1/projects/{created['id']}", headers=HEADERS
    ).json()
    assert detail["id"] == created["id"]
    assert detail["options"]["voice_id"] == "default"

    status = client.get(
        f"/api/v1/projects/{created['id']}/status", headers=HEADERS
    ).json()
    assert status["status"] == "created"
    assert status["latest_run"] is None

    missing = client.get("/api/v1/projects/does-not-exist", headers=HEADERS)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "project_not_found"


def test_script_size_limit(client):
    too_big = client.post(
        "/api/v1/projects",
        headers=HEADERS,
        json={"script": "x" * (api_v1.MAX_SCRIPT_CHARS + 1)},
    )
    assert too_big.status_code == 422
    assert too_big.json()["error"]["code"] == "validation_error"

    at_limit = client.post(
        "/api/v1/projects",
        headers=HEADERS,
        json={"script": "x" * api_v1.MAX_SCRIPT_CHARS},
    )
    assert at_limit.status_code == 201


def test_rate_limit_fires(client, monkeypatch):
    monkeypatch.setenv("CINEMORA_API_RATE_LIMIT", "3")
    for _ in range(3):
        response = client.post(
            "/api/v1/projects",
            headers=HEADERS,
            json={"script": f"Rate probe {uuid.uuid4()}."},
        )
        assert response.status_code == 201
    blocked = client.post(
        "/api/v1/projects",
        headers=HEADERS,
        json={"script": f"Rate probe {uuid.uuid4()}."},
    )
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"


# ---------------------------------------------------------------------
# Runs: submission dedup + cancel
# ---------------------------------------------------------------------


def test_run_submission_dedupes_through_api(client, monkeypatch):
    queue = FakeQueue()
    monkeypatch.setattr(jobs, "video_queue", queue)
    project = _create_project(client)

    first = client.post(
        f"/api/v1/projects/{project['id']}/runs", headers=HEADERS, json={}
    )
    assert first.status_code == 202, first.text
    first_body = first.json()
    assert first_body["deduplicated"] is False
    assert first_body["run"]["status"] == "queued"
    assert len(queue.calls) == 1
    fn, _args, kwargs = queue.calls[0]
    assert fn is jobs.run_full_pipeline
    assert kwargs["run_id"] == first_body["run"]["run_id"]

    second = client.post(
        f"/api/v1/projects/{project['id']}/runs", headers=HEADERS, json={}
    )
    second_body = second.json()
    assert second_body["deduplicated"] is True
    assert second_body["run"]["run_id"] == first_body["run"]["run_id"]
    assert second_body["job_id"] is None
    assert len(queue.calls) == 1  # nothing new was queued

    runs = client.get(
        f"/api/v1/projects/{project['id']}/runs", headers=HEADERS
    ).json()
    assert len(runs["runs"]) == 1


def test_run_cancel(client, monkeypatch):
    queue = FakeQueue()
    monkeypatch.setattr(jobs, "video_queue", queue)
    project = _create_project(client)
    submitted = client.post(
        f"/api/v1/projects/{project['id']}/runs", headers=HEADERS, json={}
    ).json()
    run_id = submitted["run"]["run_id"]

    cancelled = client.post(
        f"/api/v1/runs/{run_id}/cancel", headers=HEADERS
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["cancel_requested"] is True
    assert cancelled.json()["run"]["status"] == "cancelled"

    fetched = client.get(f"/api/v1/runs/{run_id}", headers=HEADERS).json()
    assert fetched["status"] == "cancelled"

    missing = client.post("/api/v1/runs/no-such-run/cancel", headers=HEADERS)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "run_not_found"


# ---------------------------------------------------------------------
# Plan generation + decisions
# ---------------------------------------------------------------------


def _fake_analysis():
    sentences = [
        {
            "sentence_number": 1,
            "text": "The fleet sailed at dawn.",
            "role": "event",
            "subject_id": "fleet",
            "entities": ["Yangtze River"],
            "scenarios": [
                {
                    "visual": "archival photo of the fleet",
                    "kind": "photo",
                    "specificity": "specific",
                    "format_intent": "photo",
                    "search_terms": ["fleet yangtze"],
                }
            ],
        },
        {
            "sentence_number": 2,
            "text": "Shelling began around 4:00 a.m.",
            "role": "event",
            "subject_id": "fleet",
            "entities": [],
            "scenarios": [],
        },
        {
            "sentence_number": 3,
            "text": "The ship escaped downstream.",
            "role": "outcome",
            "subject_id": "fleet",
            "entities": [],
            "scenarios": [],
        },
    ]
    return {
        "mode": "llm",
        "bible": {
            "title": "The Yangtze Incident",
            "story_type": "history",
            "time_period": "1949",
            "primary_subject_id": "fleet",
            "entities": [
                {"id": "fleet", "canonical": "HMS Amethyst"},
                {"id": "river", "canonical": "Yangtze River"},
            ],
        },
        "sentences": sentences,
        "scenes": [
            {"scene_number": 1, "sentences": sentences[:2]},
            {"scene_number": 2, "sentences": sentences[2:]},
        ],
    }


def _stub_analysis(monkeypatch):
    analysis = _fake_analysis()

    def fake_build(script, project_id="", project_dir=None):
        return {
            "analysis": analysis,
            "sentences": analysis["sentences"],
            "scenes": analysis["scenes"],
        }

    monkeypatch.setattr(jobs, "build_production_scenes", fake_build)
    return analysis


def test_plan_generate_and_shape(client, monkeypatch):
    _stub_analysis(monkeypatch)
    project = _create_project(client)

    missing = client.get(
        f"/api/v1/projects/{project['id']}/plan", headers=HEADERS
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "plan_not_found"

    generated = client.post(
        f"/api/v1/projects/{project['id']}/plan/generate", headers=HEADERS
    )
    assert generated.status_code == 200, generated.text
    plan = generated.json()
    assert plan["plan_source"] == "plan_json"
    assert plan["bible"]["title"] == "The Yangtze Incident"
    assert len(plan["sentences"]) == 3
    first = plan["sentences"][0]
    assert first["scene_number"] == 1
    assert first["subject"] == "HMS Amethyst"
    assert first["proposed_visual"]["kind"] == "photo"
    assert first["coverage"] == "uncovered"
    assert plan["sentences"][2]["scene_number"] == 2
    assert plan["summary"]["sentences"] == 3

    # plan.json was persisted and GET plan now serves it.
    assert (_project_dir(project["id"]) / "plan.json").is_file()
    fetched = client.get(
        f"/api/v1/projects/{project['id']}/plan", headers=HEADERS
    )
    assert fetched.status_code == 200


def test_plan_decisions_are_recorded(client, monkeypatch):
    _stub_analysis(monkeypatch)
    project = _create_project(client)
    client.post(
        f"/api/v1/projects/{project['id']}/plan/generate", headers=HEADERS
    )

    approved = client.post(
        f"/api/v1/projects/{project['id']}/plan/decisions",
        headers=HEADERS,
        json={"sentence_number": 1, "action": "approve"},
    )
    assert approved.status_code == 201
    assert approved.json()["outcome"] == "recorded"

    regen = client.post(
        f"/api/v1/projects/{project['id']}/plan/decisions",
        headers=HEADERS,
        json={"sentence_number": 2, "action": "regenerate"},
    )
    assert regen.json()["outcome"] == "marked_for_regeneration"

    bad = client.post(
        f"/api/v1/projects/{project['id']}/plan/decisions",
        headers=HEADERS,
        json={"sentence_number": 1, "action": "explode"},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_action"

    decisions = plan_document.load_plan_decisions(_project_dir(project["id"]))
    assert [d["action"] for d in decisions] == ["approve", "regenerate"]

    plan = client.get(
        f"/api/v1/projects/{project['id']}/plan", headers=HEADERS
    ).json()
    assert plan["sentences"][0]["decisions"][0]["action"] == "approve"
    assert plan["summary"]["decisions_recorded"] == 2


def test_replace_pre_run_records_intent_with_probe(client, monkeypatch, tmp_path):
    _stub_analysis(monkeypatch)
    project = _create_project(client)
    client.post(
        f"/api/v1/projects/{project['id']}/plan/generate", headers=HEADERS
    )
    replacement = tmp_path / "supplied.png"
    Image.new("RGB", (48, 32), (10, 20, 30)).save(replacement)

    response = client.post(
        f"/api/v1/projects/{project['id']}/plan/decisions",
        headers=HEADERS,
        json={
            "sentence_number": 1,
            "action": "replace",
            "replacement": {"file_path": str(replacement)},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["outcome"] == "recorded_pre_run"
    assert body["replacement_probe"]["width"] == 48
    assert body["replacement_rights"]["license"] == "unknown"
    assert body["replacement_rights"]["rights_status"] == "verify_before_publish"


# ---------------------------------------------------------------------
# Plan-approval gate
# ---------------------------------------------------------------------


def test_plan_gate_parks_run_and_approve_releases_it(client, monkeypatch):
    _stub_analysis(monkeypatch)
    queue = FakeQueue()
    monkeypatch.setattr(jobs, "video_queue", queue)
    project = _create_project(
        client, options={"require_plan_approval": True}
    )
    script = project["script"]

    submitted = client.post(
        f"/api/v1/projects/{project['id']}/runs", headers=HEADERS, json={}
    )
    assert submitted.status_code == 202, submitted.text
    body = submitted.json()
    assert body["phase"] == "analysis"
    assert body["deduplicated"] is False
    run_id = body["run"]["run_id"]
    # Only the analysis phase was queued — not the full pipeline.
    assert len(queue.calls) == 1
    assert queue.calls[0][0] is jobs.run_analysis_phase

    # The analysis phase runs (worker side, invoked directly here).
    result = jobs.run_analysis_phase(project["id"], script, run_id=run_id)
    assert result["status"] == "awaiting_approval"
    assert run_ledger.get_run(run_id)["status"] == "awaiting_approval"
    assert (_project_dir(project["id"]) / "plan.json").is_file()

    # Resubmission while awaiting approval deduplicates, queues nothing.
    again = client.post(
        f"/api/v1/projects/{project['id']}/runs", headers=HEADERS, json={}
    ).json()
    assert again["deduplicated"] is True
    assert len(queue.calls) == 1

    # A regenerate mark recorded before approval.
    client.post(
        f"/api/v1/projects/{project['id']}/plan/decisions",
        headers=HEADERS,
        json={"sentence_number": 2, "action": "regenerate"},
    )

    queue.calls.clear()
    approved = client.post(
        f"/api/v1/projects/{project['id']}/plan/approve", headers=HEADERS
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["released"] is True
    assert approved.json()["regenerate_sentences"] == [2]
    assert len(queue.calls) == 1
    fn, _args, kwargs = queue.calls[0]
    assert fn is jobs.run_full_pipeline
    assert kwargs["run_id"] == run_id
    assert kwargs["options"]["plan_approved"] is True
    assert kwargs["options"]["regenerate_sentences"] == [2]

    # The released run claims from awaiting_approval exactly once.
    claimed = run_ledger.claim_run(
        run_id, from_statuses=("awaiting_approval",)
    )
    assert claimed is not None
    assert run_ledger.get_run(run_id)["status"] == "running"

    # Nothing is awaiting approval any more.
    conflict = client.post(
        f"/api/v1/projects/{project['id']}/plan/approve", headers=HEADERS
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "no_awaiting_run"


def test_plan_approve_without_awaiting_run_conflicts(client):
    project = _create_project(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/plan/approve", headers=HEADERS
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_awaiting_run"


# ---------------------------------------------------------------------
# Post-run replace: evidence-safe swap through the API
# ---------------------------------------------------------------------


def _make_video(path, size="640x360", seconds=2):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    subprocess.run(
        [
            ffmpeg, "-y", "-f", "lavfi",
            "-i", f"testsrc=duration={seconds}:size={size}:rate=10",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True, capture_output=True,
    )
    return path


def _completed_run_project(client, tmp_path):
    """A project with artefacts + a completed ledger run whose
    sentence 1 has a chosen (fake-verified) video."""
    project = _create_project(client)
    project_dir = _project_dir(project["id"])
    original = project_dir / "original.mp4"
    _make_video(original, size="320x240", seconds=1)

    timeline = {
        "project_id": project["id"],
        "items": [
            {
                "type": "video",
                "scene_number": 1,
                "sentence_index": 0,
                "sentence": "The fleet sailed at dawn.",
                "file_path": str(original),
                "path": str(original),
                "source": "wikimedia",
                "source_url": "https://commons.wikimedia.org/wiki/File:Original",
                "title": "Original fleet film",
                "assignment": "sentence",
                "relevance_status": "verified_for_sentence",
                "segment_evidence": "verified_segment",
                "verification": {"accepted": True, "marker": "ORIGINAL"},
            }
        ],
    }
    (project_dir / "timeline.json").write_text(
        json.dumps(timeline), encoding="utf-8"
    )
    _fleet = {
        "id": "fleet",
        "canonical": "HMS Amethyst",
        "type": "object",
        "role": "subject",
        "aliases": ["Amethyst", "HMS Amethyst"],
        "pronoun": "it",
        "description": "Royal Navy sloop",
    }
    bible = {
        "title": "The Yangtze Incident",
        "entities": [_fleet],
        "by_id": {"fleet": _fleet},
    }
    cache = {
        "mode": "llm",
        "bible": bible,
        "plans": [
            {
                "sentence": "The fleet sailed at dawn.",
                "subject_id": "fleet",
                "scenarios": [
                    {
                        "visual": "the fleet sailing at dawn",
                        "kind": "place_establishing",
                        "subject_id": "fleet",
                        "specificity": "specific",
                        "search_terms": ["fleet yangtze dawn"],
                    }
                ],
            }
        ],
    }
    (project_dir / "story_understanding_cache.json").write_text(
        json.dumps(cache), encoding="utf-8"
    )
    plan_document.write_plan_document(project_dir, _fake_analysis(), project["id"])

    submission = run_ledger.submit_production(project["id"], project["script"])
    run_id = submission["run_id"]
    assert run_ledger.claim_run(run_id) is not None
    run_ledger.finish_run(run_id, "completed")
    return project, project_dir, run_id


def test_replace_post_run_swaps_with_fresh_evidence(client, tmp_path):
    project, project_dir, run_id = _completed_run_project(client, tmp_path)
    replacement = _make_video(tmp_path / "replacement.mp4")

    response = client.post(
        f"/api/v1/projects/{project['id']}/plan/decisions",
        headers=HEADERS,
        json={
            "sentence_number": 1,
            "action": "replace",
            "replacement": {
                "file_path": str(replacement),
                "title": "Cooking show episode",
            },
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["outcome"] == "swapped"
    refreshed = body["refreshed_asset"]
    # Fresh probe + fresh verdict + honest rights — nothing inherited
    # from the original's fabricated verification.
    assert refreshed["license"] == "unknown"
    assert refreshed["rights_status"] == "verify_before_publish"
    assert refreshed["segment_evidence"] == "none"
    assert refreshed["verification"] is not None
    assert refreshed["verification"].get("marker") != "ORIGINAL"
    assert refreshed["verification_status"] in ("verified", "swapped")

    # The ledger gained exactly the swapped_out / swapped_in pair,
    # linked, with the swapped_in row carrying the fresh records.
    rows, total = run_ledger.list_decisions(run_id, 100, 0)
    swap_rows = [r for r in rows if r["decision_type"].startswith("swapped")]
    assert total == 2 and len(swap_rows) == 2
    out_row = next(r for r in swap_rows if r["decision_type"] == "swapped_out")
    in_row = next(r for r in swap_rows if r["decision_type"] == "swapped_in")
    assert in_row["previous_decision_id"] == out_row["id"]
    assert in_row["rights"]["license"] == "unknown"
    assert in_row["evidence_class"] == "none"

    # And the decision itself is in the plan decisions artefact.
    decisions = plan_document.load_plan_decisions(project_dir)
    assert decisions[-1]["outcome"] == "swapped"


def test_replace_post_run_probe_failure_keeps_original(client, tmp_path):
    project, project_dir, run_id = _completed_run_project(client, tmp_path)
    bogus = tmp_path / "bogus.mp4"
    bogus.write_bytes(b"this is not a video at all")

    response = client.post(
        f"/api/v1/projects/{project['id']}/plan/decisions",
        headers=HEADERS,
        json={
            "sentence_number": 1,
            "action": "replace",
            "replacement": {"file_path": str(bogus)},
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["outcome"] == "swap_failed_probe"
    # No ledger swap rows were written for the failed swap.
    _rows, total = run_ledger.list_decisions(run_id, 100, 0)
    assert total == 0


# ---------------------------------------------------------------------
# Assets pagination + certificate (fixture ledger)
# ---------------------------------------------------------------------


def _seed_decisions(project_id, run_id, count=3):
    from datetime import datetime, timezone

    db = SessionLocal()
    try:
        for index in range(count):
            db.add(
                AssetDecision(
                    id=str(uuid.uuid4()),
                    run_id=run_id,
                    project_id=project_id,
                    asset_key=f"cand-{index}",
                    content_hash=f"hash-{index}",
                    source_url=f"https://example.org/asset-{index}",
                    provider="wikimedia",
                    scene_number=1,
                    sentence_number=index + 1,
                    interval_start=0.0,
                    interval_duration=5.0,
                    evidence_class="whole_asset",
                    verification={"accepted": True, "score": 0.9},
                    rights={
                        "license": "CC BY 4.0",
                        "rights_status": "recorded",
                        "creator": "An Archive",
                        "source": "wikimedia",
                    },
                    decision_type="selected",
                    created_at=datetime(
                        2026, 11, 19, 10, 0, index, tzinfo=timezone.utc
                    ),
                )
            )
        db.commit()
    finally:
        db.close()


def _run_for(project):
    submission = run_ledger.submit_production(project["id"], project["script"])
    run_id = submission["run_id"]
    run_ledger.claim_run(run_id)
    run_ledger.finish_run(run_id, "completed")
    return run_id


def test_assets_pagination(client):
    project = _create_project(client, title="Paginated")
    run_id = _run_for(project)
    _seed_decisions(project["id"], run_id, count=3)

    page1 = client.get(
        f"/api/v1/projects/{project['id']}/assets?limit=2&offset=0",
        headers=HEADERS,
    ).json()
    assert page1["run_id"] == run_id
    assert page1["total"] == 3
    assert len(page1["items"]) == 2

    page2 = client.get(
        f"/api/v1/projects/{project['id']}/assets?limit=2&offset=2",
        headers=HEADERS,
    ).json()
    assert len(page2["items"]) == 1
    assert page2["items"][0]["asset_key"] == "cand-2"


def test_certificate_from_fixture_ledger(client):
    project = _create_project(client, title="Certified Film")
    run_id = _run_for(project)
    _seed_decisions(project["id"], run_id, count=3)

    response = client.get(
        f"/api/v1/projects/{project['id']}/certificate", headers=HEADERS
    )
    assert response.status_code == 200, response.text
    cert = response.json()
    assert cert["project"]["title"] == "Certified Film"
    assert cert["run"]["run_id"] == run_id
    assert cert["summary"]["total"] == 3
    assert len(cert["assets"]) == 3
    row = cert["assets"][0]
    assert row["sentence_number"] == 1
    assert row["evidence_class"] == "whole_asset"
    assert row["rights"]["license"] == "CC BY 4.0"
    assert row["verification"]["accepted"] is True
    disclaimers = " ".join(cert["disclaimers"])
    assert "allow_all" in disclaimers
    assert "NOT cleared" in disclaimers
    assert "generated" in disclaimers.lower()


# ---------------------------------------------------------------------
# Media serving
# ---------------------------------------------------------------------


def test_media_serving_by_recorded_key_only(client, tmp_path):
    project = _create_project(client)
    project_dir = _project_dir(project["id"])
    still = project_dir / "still.png"
    Image.new("RGB", (16, 16), (200, 30, 30)).save(still)
    timeline = {
        "project_id": project["id"],
        "items": [
            {
                "type": "image",
                "scene_number": 1,
                "sentence_index": 0,
                "sentence": "A still.",
                "file_path": str(still),
                "path": str(still),
                "source": "wikimedia",
                "assignment": "sentence",
                "relevance_status": "verified_for_sentence",
                "segment_evidence": "whole_asset",
            }
        ],
    }
    (project_dir / "timeline.json").write_text(
        json.dumps(timeline), encoding="utf-8"
    )
    # A file inside the project dir that NO artefact records.
    unrecorded = project_dir / "unrecorded.txt"
    unrecorded.write_text("secret-ish", encoding="utf-8")

    key = plan_document.media_key_for(project_dir, still)
    served = client.get(
        f"/api/v1/projects/{project['id']}/media/{key}", headers=HEADERS
    )
    assert served.status_code == 200
    assert served.content == still.read_bytes()
    assert served.headers["content-type"].startswith("image/png")

    # Media requires auth too.
    assert (
        client.get(f"/api/v1/projects/{project['id']}/media/{key}").status_code
        == 401
    )

    # Unknown key → 404 in the v1 shape.
    unknown = client.get(
        f"/api/v1/projects/{project['id']}/media/{'0' * 20}", headers=HEADERS
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "media_not_found"

    # The unrecorded file's would-be key serves nothing (it is not
    # in the recorded-artefact index).
    unrecorded_key = plan_document.media_key_for(project_dir, unrecorded)
    assert (
        client.get(
            f"/api/v1/projects/{project['id']}/media/{unrecorded_key}",
            headers=HEADERS,
        ).status_code
        == 404
    )

    # Traversal attempts never escape the project dir. (A bare ".."
    # segment is normalized away by the HTTP client before the
    # request is even sent, so the encoded forms are what actually
    # reach the server — all must 404, and none may serve file
    # bytes.)
    for evil in ("..%2F..%2Fstill.png", "%2e%2e%2f%2e%2e%2fetc", "..%2fsecret"):
        response = client.get(
            f"/api/v1/projects/{project['id']}/media/{evil}", headers=HEADERS
        )
        assert response.status_code == 404, evil
        assert response.content != still.read_bytes()


def test_outputs_shape(client):
    project = _create_project(client)
    project_dir = _project_dir(project["id"])
    (project_dir / "captions.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
    )
    outputs = client.get(
        f"/api/v1/projects/{project['id']}/outputs", headers=HEADERS
    ).json()
    assert outputs["film"]["exists"] is False
    assert outputs["captions"]["srt"]["exists"] is True
    assert outputs["captions"]["srt"]["media_url"].startswith(
        f"/api/v1/projects/{project['id']}/media/"
    )
    assert outputs["certificate_url"].endswith("/certificate")


# ---------------------------------------------------------------------
# Migration 0003
# ---------------------------------------------------------------------


def test_migration_0003_preserves_rows_and_adds_columns(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "migration3.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))

    command.upgrade(cfg, "0002_run_ledger")
    project_id = str(uuid.uuid4())
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO projects (id, script, category, status, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (project_id, "A script.", "History", "created", "2026-11-19T00:00:00"),
        )
        conn.commit()
        columns_before = [
            row[1] for row in conn.execute("PRAGMA table_info(projects)")
        ]
    finally:
        conn.close()
    assert "title" not in columns_before and "options" not in columns_before

    command.upgrade(cfg, "head")
    conn = sqlite3.connect(str(db_path))
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(projects)")]
        row = conn.execute(
            "SELECT script, title, options FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
    finally:
        conn.close()
    assert "title" in columns and "options" in columns
    assert row == ("A script.", None, None)

    command.downgrade(cfg, "0002_run_ledger")
    conn = sqlite3.connect(str(db_path))
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(projects)")]
        row = conn.execute(
            "SELECT script FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    finally:
        conn.close()
    assert "title" not in columns and "options" not in columns
    assert row == ("A script.",)
