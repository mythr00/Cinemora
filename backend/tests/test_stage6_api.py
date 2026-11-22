"""Stage 6 — editor API contract tests (api_v1 editor endpoints).

Same harness pattern as test_stage4_api.py: throwaway SQLite via
conftest, CINEMORA_API_TOKEN set, the RQ queue stubbed. The editor
endpoints share the v1 fail-closed auth and error shape.
"""

from __future__ import annotations

import io
import os

import pytest
from fastapi.testclient import TestClient

import stage6_fixture as fx

TOKEN = "stage6-test-token"
AUTH = {"X-API-Key": TOKEN}


class SyncQueue:
    def enqueue(self, fn, *args, **kwargs):
        from types import SimpleNamespace

        kwargs.pop("at_front", None)
        # RQ consumes its job-option kwargs itself; the job
        # function never sees them (Phase 8.4 added job_timeout).
        for _opt in ("job_timeout", "job_id", "job_ttl", "result_ttl",
                     "failure_ttl", "depends_on", "job_description"):
            kwargs.pop(_opt, None)
        fn(*args, **kwargs)
        return SimpleNamespace(id="job-sync")


@pytest.fixture(scope="module")
def production(tmp_path_factory):
    root = tmp_path_factory.mktemp("stage6-api-media")
    os.environ["CINEMORA_MEDIA_ROOT"] = str(root)
    return fx.build_synthetic_production(root, "proj-api")


@pytest.fixture()
def client(monkeypatch, production):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    import jobs

    _sync_queue = SyncQueue()
    monkeypatch.setattr(jobs, "video_queue", _sync_queue)
    monkeypatch.setattr(jobs, "video_queue_priority", _sync_queue)
    from main import app

    return TestClient(app, raise_server_exceptions=False)


def _rev0(client, project_id):
    response = client.get(f"/api/v1/projects/{project_id}/editor", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def test_editor_endpoints_require_auth(client, production):
    project_id = production["project_id"]
    assert client.get(f"/api/v1/projects/{project_id}/editor").status_code == 401
    assert client.get(
        f"/api/v1/projects/{project_id}/editor",
        headers={"X-API-Key": "wrong"},
    ).status_code == 401
    assert client.get(
        f"/api/v1/projects/{project_id}/revisions"
    ).status_code == 401
    assert client.post(
        f"/api/v1/projects/{project_id}/uploads",
        files={"file": ("x.mp4", io.BytesIO(b"x"), "video/mp4")},
    ).status_code == 401


def test_editor_fails_closed_without_token(monkeypatch, production):
    monkeypatch.delenv("CINEMORA_API_TOKEN", raising=False)
    from main import app

    unauthenticated = TestClient(app, raise_server_exceptions=False)
    response = unauthenticated.get(
        f"/api/v1/projects/{production['project_id']}/editor",
        headers=AUTH,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "api_not_configured"


def test_get_editor_creates_revision_zero(client, production):
    body = _rev0(client, production["project_id"])
    assert body["revision_number"] == 0
    assert body["document"]["tracks"]["narration"]["sentences"]
    assert len(body["document"]["tracks"]["visual"]) == 4


def test_unknown_project_and_revision_404(client, production):
    response = client.get("/api/v1/projects/nope/editor", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "project_not_found"
    response = client.get(
        f"/api/v1/projects/{production['project_id']}/revisions/nope",
        headers=AUTH,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "revision_not_found"


def test_create_and_list_revisions(client, production):
    project_id = production["project_id"]
    rev0 = _rev0(client, project_id)
    response = client.post(
        f"/api/v1/projects/{project_id}/revisions",
        json={"from_revision_id": rev0["revision_id"]},
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["revision_number"] == rev0["revision_number"] + 1
    assert created["parent_revision_id"] == rev0["revision_id"]

    listing = client.get(
        f"/api/v1/projects/{project_id}/revisions", headers=AUTH
    )
    assert listing.status_code == 200
    ids = {r["revision_id"] for r in listing.json()["revisions"]}
    assert {rev0["revision_id"], created["revision_id"]} <= ids

    fetched = client.get(
        f"/api/v1/projects/{project_id}/revisions/{created['revision_id']}",
        headers=AUTH,
    )
    assert fetched.status_code == 200
    assert fetched.json()["document"]["revision_id"] == created["revision_id"]


def test_operations_endpoint_and_atomicity(client, production):
    project_id = production["project_id"]
    rev0 = _rev0(client, project_id)
    numbers_before = {
        r["revision_id"]
        for r in client.get(
            f"/api/v1/projects/{project_id}/revisions", headers=AUTH
        ).json()["revisions"]
    }
    items = rev0["document"]["tracks"]["visual"]
    clip_a = next(
        i for i in items if i["asset"]["file_path"].endswith("clip_a.mp4")
    )
    response = client.post(
        f"/api/v1/projects/{project_id}/revisions/{rev0['revision_id']}/operations",
        json={"operations": [
            {"type": "trim_clip", "item_id": clip_a["item_id"], "duration": 0.5},
            {"type": "trim_clip", "item_id": "itm_missing", "duration": 0.5},
        ]},
        headers=AUTH,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "item_not_found"
    listing = client.get(
        f"/api/v1/projects/{project_id}/revisions", headers=AUTH
    ).json()["revisions"]
    numbers_after_failure = {r["revision_id"] for r in listing}
    assert numbers_after_failure == numbers_before

    response = client.post(
        f"/api/v1/projects/{project_id}/revisions/{rev0['revision_id']}/operations",
        json={"operations": [
            {"type": "trim_clip", "item_id": clip_a["item_id"], "duration": 0.5},
        ]},
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    assert response.json()["revision_number"] == rev0["revision_number"] + 1
    assert response.json()["parent_revision_id"] == rev0["revision_id"]


def test_render_endpoint_gap_gate_and_exports(client, production):
    project_id = production["project_id"]
    rev0 = _rev0(client, project_id)
    items = rev0["document"]["tracks"]["visual"]
    clip_b = next(
        i for i in items
        if i["sentence_number"] == 2 and i["kind"] != "gap"
    )
    created = client.post(
        f"/api/v1/projects/{project_id}/revisions/{rev0['revision_id']}/operations",
        json={"operations": [
            {"type": "remove_clip", "item_id": clip_b["item_id"],
             "gap_policy": "leave_gap"},
        ]},
        headers=AUTH,
    ).json()
    blocked = client.post(
        f"/api/v1/projects/{project_id}/revisions/{created['revision_id']}/render",
        json={},
        headers=AUTH,
    )
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "unresolved_gaps"

    accepted = client.post(
        f"/api/v1/projects/{project_id}/revisions/{created['revision_id']}/render",
        json={"allow_gaps": True},
        headers=AUTH,
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["render_plan"]["assembly"] is True

    exports = client.get(
        f"/api/v1/projects/{project_id}/revisions/{created['revision_id']}/exports",
        headers=AUTH,
    )
    assert exports.status_code == 200
    assert len(exports.json()["exports"]) == 1
    assert exports.json()["exports"][0]["gaps_exported"]


def test_upload_endpoint(client, production):
    project_id = production["project_id"]
    response = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        files={"file": ("extra.mp4", io.BytesIO(production["clip_c"].read_bytes()),
                        "video/mp4")},
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user_supplied"] is True
    assert body["rights"]["license"] == "unknown"
    assert body["type"] == "video"

    rejected = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        files={"file": ("notes.txt", io.BytesIO(b"not media"), "text/plain")},
        headers=AUTH,
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "upload_unprobeable"


def test_upload_size_cap(client, production, monkeypatch):
    monkeypatch.setenv("CINEMORA_UPLOAD_MAX_BYTES", "1024")
    response = client.post(
        f"/api/v1/projects/{production['project_id']}/uploads",
        files={"file": ("big.mp4", io.BytesIO(production["clip_c"].read_bytes()),
                        "video/mp4")},
        headers=AUTH,
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"


def test_revision_certificate_endpoint(client, production):
    project_id = production["project_id"]
    _rev0(client, project_id)  # ensure revision 0 exists
    revisions = client.get(
        f"/api/v1/projects/{project_id}/revisions", headers=AUTH
    ).json()["revisions"]
    rev0 = next(r for r in revisions if r["revision_number"] == 0)
    response = client.get(
        f"/api/v1/projects/{project_id}/revisions/{rev0['revision_id']}/certificate",
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["certificate_version"] == 2
    assert body["revision"]["revision_id"] == rev0["revision_id"]
    assert len(body["final_revision_assets"]) == 4
    assert any("EDITED REVISION" in d for d in body["disclaimers"])

    # The original run certificate keeps its Stage 4 shape.
    original = client.get(
        f"/api/v1/projects/{project_id}/certificate", headers=AUTH
    )
    assert original.status_code == 200
    assert "revision" not in original.json()
