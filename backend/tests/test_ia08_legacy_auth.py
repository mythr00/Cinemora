"""IA-08 — legacy main.py routes fail closed behind v1 auth.

Before IA-08 the seven legacy routes in main.py were completely
unauthenticated duplicates of v1 capabilities. IA-08 (conservative
fail-closed choice, no route deletions):

- every legacy route that reads/writes project data — POST
  /projects, GET /projects/{id}, GET /projects/{id}/status,
  POST /projects/{id}/analyze, POST /projects/{id}/test-job —
  requires the SAME X-API-Key auth as /api/v1 (api_v1's
  require_api_token, imported by main: same CINEMORA_API_TOKEN
  env var, same hmac compare, 503 fail-closed when unset);
- GET / and GET /health stay open (no project data, like v1 health);
- POST /projects/{id}/test-job was a fake success over a no-op
  job (jobs.test_job only prints) — it now returns 410 Gone in
  the v1 error shape instead of "queued";
- POST /projects/{id}/analyze keeps its path but its handler is
  honestly named/documented as enqueueing the FULL pipeline and
  its responses carry Deprecation headers;
- API v1 behaviour is unchanged.

Caller evidence behind this (exhaustive grep at implementation
time): no frontend page/component/lib code, no backend script and
no docker-compose/docs entry calls any legacy project route —
frontend/lib/api.ts uses /api/v1 exclusively and the only
legacyRequest callers are quarantined files under
frontend/preserved/ hitting OTHER (already-404) legacy paths.
The only in-repo users of main.app are tests hitting /api/v1,
plus test_stage0_observability calling main.get_project_status
directly in-process (a function call, not HTTP — dependencies
are not evaluated there, so it needs no token and is unchanged).
"""

import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from database import engine
from models import AssetDecision, Base, ProductionRun, Project

TOKEN = "ia08-test-token"
HEADERS = {"X-API-Key": TOKEN}
WRONG = {"X-API-Key": "nope"}


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")  # E-21: explicit opt-in
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


def _create_legacy_project(client):
    response = client.post(
        "/projects",
        headers=HEADERS,
        json={"script": "IA-08 legacy script.", "category": "History"},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


# ---------------------------------------------------------------------
# POST /projects
# ---------------------------------------------------------------------


def test_legacy_create_project_requires_auth(client):
    missing = client.post("/projects", json={"script": "x"})
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"

    wrong = client.post("/projects", headers=WRONG, json={"script": "x"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "unauthorized"

    created = client.post(
        "/projects", headers=HEADERS, json={"script": "x"}
    )
    assert created.status_code == 200
    assert created.json()["status"] == "created"
    assert created.json()["id"]


# ---------------------------------------------------------------------
# GET /projects/{id} + /status
# ---------------------------------------------------------------------


def test_legacy_get_project_requires_auth(client):
    project_id = _create_legacy_project(client)
    url = f"/projects/{project_id}"

    assert client.get(url).status_code == 401
    assert client.get(url, headers=WRONG).status_code == 401

    ok = client.get(url, headers=HEADERS)
    assert ok.status_code == 200
    assert ok.json()["id"] == project_id
    assert ok.json()["script"] == "IA-08 legacy script."

    # Auth runs before the handler: an unknown id with a valid
    # token still reaches handler logic and 404s in the legacy
    # (non-v1) shape.
    missing = client.get("/projects/does-not-exist", headers=HEADERS)
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Project not found"}


def test_legacy_project_status_requires_auth(client):
    project_id = _create_legacy_project(client)
    url = f"/projects/{project_id}/status"

    assert client.get(url).status_code == 401
    assert client.get(url, headers=WRONG).status_code == 401

    ok = client.get(url, headers=HEADERS)
    assert ok.status_code == 200
    assert ok.json()["id"] == project_id
    assert ok.json()["status"] == "created"
    assert ok.json()["progress"] is None


# ---------------------------------------------------------------------
# POST /projects/{id}/analyze — auth + honest full-pipeline enqueue
# ---------------------------------------------------------------------


def test_legacy_analyze_requires_auth_and_enqueues_pipeline(
    client, monkeypatch
):
    project_id = _create_legacy_project(client)
    calls = []

    def fake_enqueue(pid, script, options=None):
        calls.append((pid, script, options))
        return "job-ia08-1"

    # main imported the name directly, so patch it where it is used.
    monkeypatch.setattr(main, "enqueue_full_pipeline", fake_enqueue)
    url = f"/projects/{project_id}/analyze"

    assert client.post(url).status_code == 401
    assert client.post(url, headers=WRONG).status_code == 401
    assert calls == []  # auth rejected before any enqueue happened

    ok = client.post(url, headers=HEADERS)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "pipeline_queued"
    assert body["job_id"] == "job-ia08-1"
    # Redesign programme (2026-11-27, F4): the legacy enqueue now
    # carries the project's stored options ({} here — the legacy
    # creation stored none), exactly as the v1 path does.
    assert calls == [(project_id, "IA-08 legacy script.", {})]
    # Honest deprecation signalling on the response.
    assert ok.headers.get("deprecation") == "true"
    assert "/api/v1/" in (ok.headers.get("x-deprecated") or "")

    missing = client.post("/projects/does-not-exist/analyze", headers=HEADERS)
    assert missing.status_code == 404


# ---------------------------------------------------------------------
# POST /projects/{id}/test-job — 410, never fake success
# ---------------------------------------------------------------------


def test_legacy_test_job_is_gone_not_fake_queued(client):
    project_id = _create_legacy_project(client)
    url = f"/projects/{project_id}/test-job"

    # Same uniform auth pattern as the other legacy project routes.
    assert client.post(url).status_code == 401
    assert client.post(url, headers=WRONG).status_code == 401

    gone = client.post(url, headers=HEADERS)
    assert gone.status_code == 410
    assert gone.json()["error"]["code"] == "gone"
    assert "test-job" in gone.json()["error"]["message"]


# ---------------------------------------------------------------------
# Fail-closed when unconfigured; root/health stay open
# ---------------------------------------------------------------------


def test_legacy_routes_fail_closed_when_token_unset(client, monkeypatch):
    monkeypatch.delenv("CINEMORA_API_TOKEN", raising=False)
    project_id = "anything"
    for method, url in (
        ("POST", "/projects"),
        ("GET", f"/projects/{project_id}"),
        ("GET", f"/projects/{project_id}/status"),
        ("POST", f"/projects/{project_id}/analyze"),
        ("POST", f"/projects/{project_id}/test-job"),
    ):
        response = client.request(method, url, headers=HEADERS, json={})
        assert response.status_code == 503, (method, url, response.text)
        assert response.json()["error"]["code"] == "api_not_configured"


def test_root_and_legacy_health_stay_open(client):
    assert client.get("/").status_code == 200
    assert client.get("/").json()["status"] == "online"
    assert client.get("/health").status_code == 200
    assert client.get("/health").json() == {"status": "healthy"}


# ---------------------------------------------------------------------
# API v1 unaffected
# ---------------------------------------------------------------------


def test_v1_routes_unaffected(client):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/projects").status_code == 401
    assert client.get("/api/v1/projects", headers=WRONG).status_code == 401
    ok = client.get("/api/v1/projects", headers=HEADERS)
    assert ok.status_code == 200
    assert "projects" in ok.json()
