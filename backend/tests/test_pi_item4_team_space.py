"""Product Improvements item 4 — workspace-level Team Space.

GET /team/overview aggregates the account's whole collaboration
graph (owned + shared projects, people directory, invitations
in) and is available with NO production and NO completed video
anywhere — team membership never depends on a finished video.
The per-project enforcement model is unchanged.
"""

from __future__ import annotations

import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from auth_helpers import with_session_token
from database import SessionLocal, engine
from models import Base, Plan, ProjectMember, User
from services import auth as auth_service

TOKEN = "pi-item4-token"
PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(
        fakeredis.FakeRedis(decode_responses=True)
    )
    db = SessionLocal()
    try:
        db.query(ProjectMember).delete()
        db.query(User).delete()
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


def _user(client, label):
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    account = with_session_token(response)
    return {
        "email": email,
        "id": account["user"]["id"],
        "headers": {"Authorization": f"Bearer {account['session_token']}"},
    }


def _create_project(client, headers):
    response = client.post(
        "/api/v1/projects",
        headers=headers,
        json={"script": "Alpha opens. Bravo continues. Charlie closes."},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_team_overview_empty_account(client):
    """A brand-new account — no projects, no videos — gets a
    valid, empty Team Space."""
    user = _user(client, "solo")
    response = client.get("/api/v1/team/overview", headers=user["headers"])
    assert response.status_code == 200, response.text
    assert response.json() == {
        "projects": [],
        "people": [],
        "invitations_in": [],
    }


def test_team_overview_full_graph_without_any_video(client):
    owner = _user(client, "owner")
    editor = _user(client, "editor")
    viewer = _user(client, "viewer")
    project_id = _create_project(client, owner["headers"])
    # NOTE: the project has never been produced — no runs, no
    # outputs, no completed video. Team flows must work anyway.

    invite = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=owner["headers"],
        json={"email": editor["email"], "role": "editor"},
    )
    assert invite.status_code == 201, invite.text
    client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=owner["headers"],
        json={"email": viewer["email"], "role": "viewer"},
    )

    # The invitee sees the pending invitation in Team Space
    # before responding.
    viewer_view = client.get(
        "/api/v1/team/overview", headers=viewer["headers"]
    ).json()
    assert len(viewer_view["invitations_in"]) == 1
    assert viewer_view["invitations_in"][0]["project_id"] == project_id
    assert viewer_view["projects"] == []

    accepted = client.post(
        f"/api/v1/invitations/{project_id}/respond",
        headers=editor["headers"],
        json={"accept": True},
    )
    assert accepted.status_code == 200, accepted.text

    owner_view = client.get(
        "/api/v1/team/overview", headers=owner["headers"]
    ).json()
    assert len(owner_view["projects"]) == 1
    entry = owner_view["projects"][0]
    assert entry["your_role"] == "owner"
    assert [m["user"]["email"] for m in entry["members"]] == [editor["email"]]
    # The still-pending viewer invitation is visible to the owner.
    assert [i["user"]["email"] for i in entry["invitations"]] == [
        viewer["email"]
    ]
    people = {p["user"]["email"]: p for p in owner_view["people"]}
    assert set(people) == {owner["email"], editor["email"]}
    assert people[editor["email"]]["projects"][0]["role"] == "editor"

    # The accepted editor sees the shared project as theirs too,
    # but NOT the owner's outgoing invitation list.
    editor_view = client.get(
        "/api/v1/team/overview", headers=editor["headers"]
    ).json()
    assert len(editor_view["projects"]) == 1
    assert editor_view["projects"][0]["your_role"] == "editor"
    assert editor_view["projects"][0]["invitations"] == []


def test_team_overview_requires_auth(client):
    response = client.get("/api/v1/team/overview")
    assert response.status_code == 401
