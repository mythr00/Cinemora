"""Brief Stage 5b — pre-run uploads for plan-review replacement.

Closes the C-04 limitation found in Brief Stage 5: POST
/projects/{id}/uploads used to anchor every upload to the editor
revision chain (store_upload created revision 0), which requires a
COMPLETED production run — so on a project whose first run is
parked in "awaiting_approval" (exactly when plan-review
replacement matters) the upload failed 404 no_completed_run and
replace-via-upload could not complete pre-run.

The fix (services/editor.py::store_upload): when no revision and
no completed run exist but a run is awaiting plan approval, the
upload is stored, probed and indexed in the SAME project upload
index the editor resolves {upload_id} against — without creating
revision 0 (no timeline exists to import) and without a ledger
decision (an untagged row on the awaiting run would leak into the
original run's certificate, the D3 isolation). The record is
marked "pre_run": true with decision_id None.

These tests drive the real API + the real analysis-phase path
(analysis stubbed, as in test_stage4_api.py) and pin:

(a) upload on an awaiting_approval project -> 201, probed,
    user_supplied, licence unknown, NO revision rows created;
(b) a plan replace consuming that upload pre-run ->
    recorded_pre_run with the decision persisted;
(c) after the run completes and revision 0 exists, the same
    upload is listed by the editor's index and consumable by a
    replace_clip operation;
(d) upload on a run-less project -> honest 404
    no_production_context, nothing stored;
(e) corrupt file pre-run -> 422, nothing stored;
(f) the whole flow leaves the original run's certificate and
    ledger decisions untouched.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api_v1
import jobs
import main
from database import SessionLocal, engine
from models import AssetDecision, Base, ProductionRun, Project, ProjectRevision
from services import editor, media_paths, plan_document, run_ledger

TOKEN = "stage5b-test-token"
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
    # Full create_all, the same startup step main.py runs and the
    # shape every other API test file uses: run submission now
    # passes through Phase 8 plan admission + generation billing,
    # which read the accounts/billing tables — the old four-table
    # list made POST /runs 500 on the missing tables in isolation.
    Base.metadata.create_all(bind=engine)
    # Hermeticity (shared suite DB; the test_versioning precedent):
    # suites that submit runs through fake queues leave them
    # active forever, and plan admission counts a principal's
    # active runs — after enough predecessors, this file's
    # submissions are refused with plan_limit_concurrency.
    # Those runs are abandoned leftovers of finished tests;
    # terminate them, as the production reconciler would, so
    # admission starts clean.
    from datetime import datetime, timezone

    db = SessionLocal()
    try:
        leaked = (
            db.query(ProductionRun)
            .filter(
                ProductionRun.status.in_(
                    ("queued", "running", "awaiting_approval")
                )
            )
            .all()
        )
        for row in leaked:
            row.status = "failed"
            row.error = "test-suite cleanup of a leaked active run"
            row.finished_at = datetime.now(timezone.utc)
        if leaked:
            db.commit()
    finally:
        db.close()
    yield


class FakeQueue:
    def __init__(self):
        self.calls = []

    def enqueue(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return SimpleNamespace(id=f"job-{len(self.calls)}")


# ---------------------------------------------------------------------
# Fixture helpers — the awaiting_approval state via the real path
# (same pattern as test_stage4_api.py's plan-gate test)
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

    def fake_build(script, project_id="", project_dir=None, genre_pack=None):
        return {
            "analysis": analysis,
            "sentences": analysis["sentences"],
            "scenes": analysis["scenes"],
        }

    monkeypatch.setattr(jobs, "build_production_scenes", fake_build)
    return analysis


def _create_project(client, **extra):
    body = {
        "script": (
            f"Script {uuid.uuid4()}. The fleet sailed at dawn. "
            "Shelling began around 4:00 a.m. The ship escaped downstream."
        )
    }
    body.update(extra)
    response = client.post("/api/v1/projects", headers=HEADERS, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _awaiting_project(client, monkeypatch):
    """A project whose first run is parked in awaiting_approval,
    reached through the real submit + analysis-phase machinery."""
    _stub_analysis(monkeypatch)
    queue = FakeQueue()
    monkeypatch.setattr(jobs, "video_queue", queue)
    monkeypatch.setattr(jobs, "video_queue_priority", queue)
    project = _create_project(
        client, options={"require_plan_approval": True}
    )
    submitted = client.post(
        f"/api/v1/projects/{project['id']}/runs", headers=HEADERS, json={}
    )
    assert submitted.status_code == 202, submitted.text
    run_id = submitted.json()["run"]["run_id"]
    assert queue.calls and queue.calls[0][0] is jobs.run_analysis_phase
    result = jobs.run_analysis_phase(project["id"], project["script"], run_id=run_id)
    assert result["status"] == "awaiting_approval"
    assert run_ledger.get_run(run_id)["status"] == "awaiting_approval"
    assert (media_paths.project_dir(project["id"]) / "plan.json").is_file()
    return project, run_id


def _make_video(path, size="320x240", seconds=2):
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


def _upload(client, project_id, payload: bytes, name="supplied.mp4"):
    return client.post(
        f"/api/v1/projects/{project_id}/uploads",
        files={"file": (name, io.BytesIO(payload), "video/mp4")},
        headers=HEADERS,
    )


def _revision_rows(project_id):
    db = SessionLocal()
    try:
        return (
            db.query(ProjectRevision)
            .filter(ProjectRevision.project_id == str(project_id))
            .count()
        )
    finally:
        db.close()


# ---------------------------------------------------------------------
# (a) Upload on an awaiting_approval project
# ---------------------------------------------------------------------


def test_upload_on_awaiting_project_stores_without_revision(
    client, monkeypatch, tmp_path
):
    project, run_id = _awaiting_project(client, monkeypatch)
    project_id = project["id"]
    clip = _make_video(tmp_path / "supplied.mp4", seconds=2)

    response = _upload(client, project_id, clip.read_bytes())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["pre_run"] is True
    assert body["decision_id"] is None
    assert body["user_supplied"] is True
    assert body["source"] == "user_supplied"
    assert body["provider"] == "user_upload"
    assert body["type"] == "video"
    assert body["source_duration"] is not None and body["source_duration"] > 0
    assert body["rights"]["license"] == "unknown"
    assert body["rights"]["rights_status"] == "verify_before_publish"
    assert body["rights"]["source"] == "user_supplied"

    # Stored under the project's uploads dir and recorded in the
    # same index the editor resolves {upload_id} against.
    stored = Path(body["file_path"])
    assert stored.is_file()
    assert stored.parent == media_paths.project_dir(project_id) / "uploads"
    indexed = editor.get_upload(project_id, body["upload_id"])
    assert indexed is not None
    assert indexed["file_path"] == body["file_path"]
    assert indexed["pre_run"] is True

    # NO revision was created and the run is untouched.
    assert _revision_rows(project_id) == 0
    listing = client.get(
        f"/api/v1/projects/{project_id}/revisions", headers=HEADERS
    )
    assert listing.json()["revisions"] == []
    assert run_ledger.get_run(run_id)["status"] == "awaiting_approval"
    # And no ledger decision of any kind was recorded for the run.
    _rows, total = run_ledger.list_decisions(run_id, 100, 0)
    assert total == 0


# ---------------------------------------------------------------------
# (b) Plan replace consumes the pre-run upload
# ---------------------------------------------------------------------


def test_plan_replace_consumes_prerun_upload(client, monkeypatch, tmp_path):
    project, _run_id = _awaiting_project(client, monkeypatch)
    project_id = project["id"]
    clip = _make_video(tmp_path / "supplied.mp4", size="320x240", seconds=2)
    upload = _upload(client, project_id, clip.read_bytes()).json()

    response = client.post(
        f"/api/v1/projects/{project_id}/plan/decisions",
        headers=HEADERS,
        json={
            "sentence_number": 1,
            "action": "replace",
            "replacement": {
                "file_path": upload["file_path"],
                "title": upload["title"],
            },
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["outcome"] == "recorded_pre_run"
    assert body["replacement_probe"]["width"] == 320
    assert body["replacement_rights"]["license"] == "unknown"
    assert body["replacement_rights"]["rights_status"] == "verify_before_publish"
    assert body["replacement_rights"]["source"] == "user_supplied"

    # The decision is persisted and surfaces on the plan.
    project_dir = media_paths.project_dir(project_id)
    decisions = plan_document.load_plan_decisions(project_dir)
    recorded = [d for d in decisions if d.get("action") == "replace"]
    assert len(recorded) == 1
    assert recorded[0]["outcome"] == "recorded_pre_run"
    plan = client.get(
        f"/api/v1/projects/{project_id}/plan", headers=HEADERS
    ).json()
    assert plan["sentences"][0]["decisions"][0]["outcome"] == "recorded_pre_run"


# ---------------------------------------------------------------------
# (c) After completion + revision 0, the pre-run upload is usable
# ---------------------------------------------------------------------


def test_prerun_upload_usable_in_editor_after_completion(
    client, monkeypatch, tmp_path
):
    project, run_id = _awaiting_project(client, monkeypatch)
    project_id = project["id"]
    project_dir = media_paths.project_dir(project_id)
    clip = _make_video(tmp_path / "supplied.mp4", seconds=3)
    upload = _upload(client, project_id, clip.read_bytes()).json()
    assert upload["pre_run"] is True

    # The production completes: a timeline lands and the SAME run
    # is finished (the full pipeline's own end state).
    original = _make_video(project_dir / "original.mp4", seconds=2)
    timeline = {
        "scenes": [
            {
                "scene_number": 1,
                "items": [
                    {
                        "type": "video",
                        "scene_number": 1,
                        "sentence_index": 0,
                        "sentence": "The fleet sailed at dawn.",
                        "start": 0,
                        "end": 2.0,
                        "duration": 2.0,
                        "file_path": str(original),
                        "path": str(original),
                        "source": "synthetic",
                        "provider": "synthetic",
                        "source_url": "https://example.test/original.mp4",
                        "title": "original",
                        "assignment": "sentence",
                        "clip_start": 0,
                    }
                ],
            }
        ],
        "duration_seconds": 2.0,
    }
    (project_dir / "timeline.json").write_text(json.dumps(timeline, indent=2))
    assert run_ledger.finish_run(run_id, "completed")

    # Revision 0 is created from the completed run on demand.
    editor_view = client.get(
        f"/api/v1/projects/{project_id}/editor", headers=HEADERS
    )
    assert editor_view.status_code == 200, editor_view.text
    rev0 = editor_view.json()
    assert rev0["revision_number"] == 0

    # The pre-run upload is still in the editor's index, unchanged.
    indexed = editor.get_upload(project_id, upload["upload_id"])
    assert indexed is not None
    assert indexed["pre_run"] is True
    assert indexed["rights"]["license"] == "unknown"

    # And it is consumable: replace the timeline's only clip with
    # it, by upload_id, through the real operations endpoint.
    doc = rev0["document"]
    item = doc["tracks"]["visual"][0]
    applied = client.post(
        f"/api/v1/projects/{project_id}/revisions/{rev0['revision_id']}/operations",
        headers=HEADERS,
        json={
            "operations": [
                {
                    "type": "replace_clip",
                    "item_id": item["item_id"],
                    "asset": {"upload_id": upload["upload_id"]},
                }
            ]
        },
    )
    assert applied.status_code == 201, applied.text
    new_revision_id = applied.json()["revision_id"]
    fetched = client.get(
        f"/api/v1/projects/{project_id}/revisions/{new_revision_id}",
        headers=HEADERS,
    )
    assert fetched.status_code == 200, fetched.text
    new_item = fetched.json()["document"]["tracks"]["visual"][0]
    assert new_item["asset"]["file_path"] == upload["file_path"]
    assert new_item["asset"]["user_supplied"] is True
    assert new_item["classification"] == "user_supplied"
    assert new_item["asset"]["rights"]["license"] == "unknown"


# ---------------------------------------------------------------------
# (d) Run-less project: honest error, nothing stored
# ---------------------------------------------------------------------


def test_upload_on_runless_project_honest_error(client, tmp_path):
    project = _create_project(client)
    project_id = project["id"]
    clip = _make_video(tmp_path / "supplied.mp4", seconds=1)

    response = _upload(client, project_id, clip.read_bytes())
    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == "no_production_context"
    assert "awaiting approval" in error["message"]

    project_dir = media_paths.project_dir(project_id)
    assert editor._uploads_index(project_dir) == []
    uploads_dir = project_dir / "uploads"
    leftovers = list(uploads_dir.iterdir()) if uploads_dir.exists() else []
    assert leftovers == []
    assert _revision_rows(project_id) == 0


# ---------------------------------------------------------------------
# (e) Corrupt file pre-run: 422, nothing stored
# ---------------------------------------------------------------------


def test_corrupt_upload_prerun_rejected_and_not_stored(
    client, monkeypatch, tmp_path
):
    project, _run_id = _awaiting_project(client, monkeypatch)
    project_id = project["id"]
    project_dir = media_paths.project_dir(project_id)

    response = _upload(
        client, project_id, b"this is not media at all", name="broken.mp4"
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "upload_unprobeable"

    assert editor._uploads_index(project_dir) == []
    uploads_dir = project_dir / "uploads"
    leftovers = list(uploads_dir.iterdir()) if uploads_dir.exists() else []
    assert leftovers == []
    assert _revision_rows(project_id) == 0


# ---------------------------------------------------------------------
# (f) The flow leaves the original run's certificate untouched
# ---------------------------------------------------------------------


def test_prerun_flow_leaves_run_certificate_unchanged(
    client, monkeypatch, tmp_path
):
    project, run_id = _awaiting_project(client, monkeypatch)
    project_id = project["id"]

    before = client.get(
        f"/api/v1/projects/{project_id}/certificate", headers=HEADERS
    )
    assert before.status_code == 200, before.text

    clip = _make_video(tmp_path / "supplied.mp4", seconds=2)
    upload = _upload(client, project_id, clip.read_bytes()).json()
    replaced = client.post(
        f"/api/v1/projects/{project_id}/plan/decisions",
        headers=HEADERS,
        json={
            "sentence_number": 1,
            "action": "replace",
            "replacement": {"file_path": upload["file_path"]},
        },
    )
    assert replaced.json()["outcome"] == "recorded_pre_run"

    after = client.get(
        f"/api/v1/projects/{project_id}/certificate", headers=HEADERS
    )
    assert after.status_code == 200, after.text
    # Byte-identical: the upload and the plan decision added no
    # ledger rows and changed no run record the certificate reads.
    assert after.content == before.content
    assert upload["file_name"] not in after.text
    _rows, total = run_ledger.list_decisions(run_id, 100, 0)
    assert total == 0
    counts = run_ledger.decision_counts(
        run_id, include_revision_decisions=False
    )
    assert counts["by_type"] == {}
    assert counts["rights_recorded"] == 0
    assert counts["rights_unknown"] == 0
