"""E-03 / B-16 — legacy main.py routes must enforce tenant isolation.

Before this fix, the legacy unprefixed routes in main.py bypassed
the v1 ownership choke point (api_v1._get_owned_project_or_404)
entirely: any valid principal could read, read the status of, or
enqueue a full production on ANY project by id, and legacy
creation set no owner_id (ownerless projects, invisible to their
creator under v1). These tests pin the fixed behaviour:

- cross-user legacy read / status / enqueue -> 404 (v1 semantics:
  existence itself is tenant-private, never 403 for a non-member);
- the owner keeps full legacy access (read, status, enqueue);
- legacy creation assigns owner_id to the creator, and the
  project appears in the creator's v1 project list;
- a pre-existing ownerless (NULL owner) legacy row stays visible
  to the operator/founder principal only — the same rule the v1
  choke point applies — and 404s for ordinary users.

Users A and B are created through the REAL registration path and
sessions live in fakeredis, exactly as in
tests/test_phase8_auth_isolation.py. The pipeline enqueue is
stubbed (the IA-08 pattern): no real production is ever queued.
"""

import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
from auth_helpers import with_session_token
import main
from database import SessionLocal, engine
from models import Base, Plan, Project, User, UserSubscription
from services import auth as auth_service

TOKEN = "legacy-isolation-operator-token"
PASSWORD_A = "alpha-correct-horse-42"
PASSWORD_B = "bravo-correct-horse-42"


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")  # E-21: window is closed by default; tests opt in explicitly
    monkeypatch.delenv("CINEMORA_REGISTRATION", raising=False)
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(fakeredis.FakeRedis(decode_responses=True))
    db = SessionLocal()
    try:
        for model in (Project, User):
            db.query(model).delete()
        if db.query(Plan).filter_by(name="free").one_or_none() is None:
            db.add(
                Plan(
                    id=str(uuid.uuid4()),
                    name="free",
                    max_concurrent_runs=1,
                    max_runs_per_period=3,
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
    db = SessionLocal()
    try:
        db.query(UserSubscription).delete()
        db.query(Project).delete()
        db.query(User).delete()
        db.query(Plan).delete()
        db.commit()
    finally:
        db.close()


def _register(client, email, password):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password},
    )
    assert response.status_code == 201, response.text
    return with_session_token(response)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def users(client):
    suffix = uuid.uuid4().hex[:8]
    a = _register(client, f"a-{suffix}@example.com", PASSWORD_A)
    b = _register(client, f"b-{suffix}@example.com", PASSWORD_B)
    return a, b


def _a_v1_project(client, a):
    response = client.post(
        "/api/v1/projects",
        headers=_bearer(a["session_token"]),
        json={"script": "Owner A script. One sentence. Another one."},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _stub_enqueue(monkeypatch):
    calls = []

    def fake_enqueue(pid, script):
        calls.append((pid, script))
        return "job-legacy-isolation-1"

    monkeypatch.setattr(main, "enqueue_full_pipeline", fake_enqueue)
    return calls


# ---------------------------------------------------------------------
# Cross-user denial on every project-scoped legacy route
# ---------------------------------------------------------------------


def test_legacy_cross_user_read_denied(client, users):
    a, b = users
    pid = _a_v1_project(client, a)

    resp = client.get(f"/projects/{pid}", headers=_bearer(b["session_token"]))
    assert resp.status_code == 404, resp.text
    # Indistinguishable from a project that does not exist at all.
    missing = client.get(
        "/projects/does-not-exist", headers=_bearer(b["session_token"])
    )
    assert missing.status_code == 404
    assert resp.json() == missing.json()


def test_legacy_cross_user_status_denied(client, users):
    a, b = users
    pid = _a_v1_project(client, a)

    resp = client.get(
        f"/projects/{pid}/status", headers=_bearer(b["session_token"])
    )
    assert resp.status_code == 404, resp.text


def test_legacy_cross_user_enqueue_denied(client, users, monkeypatch):
    a, b = users
    pid = _a_v1_project(client, a)
    calls = _stub_enqueue(monkeypatch)

    resp = client.post(
        f"/projects/{pid}/analyze", headers=_bearer(b["session_token"])
    )
    assert resp.status_code == 404, resp.text
    assert calls == []  # nothing was queued on A's project


# ---------------------------------------------------------------------
# The owner keeps full legacy access
# ---------------------------------------------------------------------


def test_legacy_owner_read_status_enqueue_still_works(
    client, users, monkeypatch
):
    a, _b = users
    pid = _a_v1_project(client, a)
    calls = _stub_enqueue(monkeypatch)
    headers = _bearer(a["session_token"])

    got = client.get(f"/projects/{pid}", headers=headers)
    assert got.status_code == 200, got.text
    assert got.json()["id"] == pid
    assert "Owner A script" in got.json()["script"]

    status = client.get(f"/projects/{pid}/status", headers=headers)
    assert status.status_code == 200, status.text
    assert status.json()["id"] == pid

    queued = client.post(f"/projects/{pid}/analyze", headers=headers)
    assert queued.status_code == 200, queued.text
    assert queued.json()["status"] == "pipeline_queued"
    assert queued.json()["job_id"] == "job-legacy-isolation-1"
    assert calls == [(pid, "Owner A script. One sentence. Another one.")]


# ---------------------------------------------------------------------
# Legacy creation assigns ownership (B-16)
# ---------------------------------------------------------------------


def test_legacy_create_assigns_ownership_and_lists_in_v1(client, users):
    a, b = users
    created = client.post(
        "/projects",
        headers=_bearer(a["session_token"]),
        json={"script": "Legacy-created script.", "category": "History"},
    )
    assert created.status_code == 200, created.text
    pid = created.json()["id"]

    db = SessionLocal()
    try:
        row = db.get(Project, pid)
        assert row is not None
        assert row.owner_id == a["user"]["id"]
    finally:
        db.close()

    # The creator sees it in their v1 list; the other user does not.
    a_list = client.get(
        "/api/v1/projects", headers=_bearer(a["session_token"])
    )
    assert a_list.status_code == 200
    assert pid in {p["id"] for p in a_list.json()["projects"]}
    b_list = client.get(
        "/api/v1/projects", headers=_bearer(b["session_token"])
    )
    assert pid not in {p["id"] for p in b_list.json()["projects"]}

    # And the legacy door is closed to B but open to A.
    assert (
        client.get(f"/projects/{pid}", headers=_bearer(b["session_token"]))
        .status_code
        == 404
    )
    assert (
        client.get(f"/projects/{pid}", headers=_bearer(a["session_token"]))
        .status_code
        == 200
    )


# ---------------------------------------------------------------------
# Pre-existing ownerless rows: operator principal only (v1 rule)
# ---------------------------------------------------------------------


def test_legacy_ownerless_row_visible_to_operator_only(client, users):
    _a, b = users
    pid = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(
            Project(
                id=pid,
                script="Pre-8.1 ownerless legacy script.",
                category="Documentary",
                status="created",
                owner_id=None,
            )
        )
        db.commit()
    finally:
        db.close()

    as_b = client.get(f"/projects/{pid}", headers=_bearer(b["session_token"]))
    assert as_b.status_code == 404, as_b.text

    as_operator = client.get(f"/projects/{pid}", headers={"X-API-Key": TOKEN})
    assert as_operator.status_code == 200, as_operator.text
    assert as_operator.json()["id"] == pid
