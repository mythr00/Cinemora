"""Collaboration (founder priority #11; gap-audit doc 04 §4) —
project membership with backend-enforced roles, in-app
invitations, comments and the recorded-events activity feed.

The boundaries under test:

- A non-member's view of a shared project is the Phase 8 rule:
  404 everywhere, existence never leaks.
- An invited account sees the invitation in-app (GET /invitations)
  and NOWHERE else: no project access until it accepts.
- Viewer: reads pass; every production write is refused 403
  project_read_only; comments are allowed (discussion is the
  point of the role).
- Editor: production writes pass; ownership-level actions
  (invite / role change / removing others) are 404.
- Owner: everything, including member management; membership
  changes take effect on the very next request.
- Comments: author edits/deletes their own; another member cannot
  edit (403) or delete (403); the owner can delete any comment.
- Activity: assembled only from recorded events (runs, revisions,
  plan decisions, membership lifecycle, comments).
"""

import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import jobs
import main
from database import SessionLocal, engine
from models import Base, Plan, Project, User
from services import auth as auth_service
from services import run_ledger

TOKEN = "collab-test-token-0001"
PASSWORD = "collab-correct-horse-42"


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")  # E-21: window is closed by default; tests opt in explicitly
    monkeypatch.delenv("CINEMORA_REGISTRATION", raising=False)
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(fakeredis.FakeRedis(decode_responses=True))

    def _fake_enqueue(project_id, script, options=None, run_id=None):
        if run_id is not None:
            return "job-released"
        merged = {"entrypoint": "enqueue_full_pipeline"}
        if isinstance(options, dict):
            merged.update(options)
        submission = run_ledger.submit_production(
            project_id, script, options=merged
        )
        return submission["run_id"] if submission else "job-unrecorded"

    monkeypatch.setattr(jobs, "enqueue_full_pipeline", _fake_enqueue)
    db = SessionLocal()
    try:
        if db.query(Plan).filter_by(name="free").one_or_none() is None:
            db.add(
                Plan(
                    id=str(uuid.uuid4()),
                    name="free",
                    max_concurrent_runs=2,
                    max_runs_per_period=100,
                    period="month",
                    priority_tier=0,
                    is_default=True,
                )
            )
            db.commit()
        # Hermeticity (shared suite DB): terminate leaked active
        # runs so plan admission starts clean (see test_versioning).
        from datetime import datetime, timezone

        from models import ProductionRun

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
    auth_service.reset_redis_client()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _register(client, email):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _user(client, label):
    suffix = uuid.uuid4().hex[:8]
    email = f"{label}-{suffix}@example.com"
    account = _register(client, email)
    return {
        "email": email,
        "id": account["user"]["id"],
        "headers": _bearer(account["session_token"]),
    }


def _create_project(client, headers):
    response = client.post(
        "/api/v1/projects",
        headers=headers,
        json={"script": "Alpha opens. Bravo continues. Charlie closes."},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _invite(client, owner, project_id, email, role, expect=201):
    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=owner["headers"],
        json={"email": email, "role": role},
    )
    assert response.status_code == expect, response.text
    return response


def _respond(client, member, project_id, accept=True, expect=200):
    response = client.post(
        f"/api/v1/invitations/{project_id}/respond",
        headers=member["headers"],
        json={"accept": accept},
    )
    assert response.status_code == expect, response.text
    return response


def _setup(client):
    """Owner + editor-to-be + viewer-to-be + outsider, one project."""
    owner = _user(client, "owner")
    editor = _user(client, "editor")
    viewer = _user(client, "viewer")
    outsider = _user(client, "outsider")
    project_id = _create_project(client, owner["headers"])
    return owner, editor, viewer, outsider, project_id


def _share(client, owner, member, project_id, role):
    _invite(client, owner, project_id, member["email"], role)
    _respond(client, member, project_id, accept=True)


# ---------------------------------------------------------------------
# Invitation lifecycle
# ---------------------------------------------------------------------


def test_invitation_flow_and_no_access_before_accept(client):
    owner, editor, viewer, outsider, project_id = _setup(client)
    _invite(client, owner, project_id, editor["email"], "editor")

    # The invitation is visible in-app to the invitee only.
    invitations = client.get("/api/v1/invitations", headers=editor["headers"])
    assert invitations.status_code == 200
    entries = invitations.json()["invitations"]
    assert len(entries) == 1
    assert entries[0]["project_id"] == project_id
    assert entries[0]["role"] == "editor"
    assert entries[0]["invited_by"]["email"] == owner["email"]
    assert client.get(
        "/api/v1/invitations", headers=viewer["headers"]
    ).json()["invitations"] == []

    # Before accepting: the project does not exist for the invitee.
    response = client.get(
        f"/api/v1/projects/{project_id}", headers=editor["headers"]
    )
    assert response.status_code == 404

    _respond(client, editor, project_id, accept=True)

    response = client.get(
        f"/api/v1/projects/{project_id}", headers=editor["headers"]
    )
    assert response.status_code == 200
    assert response.json()["access_role"] == "editor"

    # The shared project appears in the member's project list.
    listed = client.get("/api/v1/projects", headers=editor["headers"]).json()
    shared = [p for p in listed["projects"] if p["id"] == project_id]
    assert len(shared) == 1 and shared[0]["access_role"] == "editor"

    # Responding twice is a 404 — no pending invitation remains.
    _respond(client, editor, project_id, accept=True, expect=404)


def test_declined_invitation_grants_nothing_and_can_be_reinvited(client):
    owner, editor, viewer, outsider, project_id = _setup(client)
    _invite(client, owner, project_id, viewer["email"], "viewer")
    _respond(client, viewer, project_id, accept=False)
    assert (
        client.get(
            f"/api/v1/projects/{project_id}", headers=viewer["headers"]
        ).status_code
        == 404
    )
    # Re-inviting a declined account revives the row.
    _invite(client, owner, project_id, viewer["email"], "viewer")
    _respond(client, viewer, project_id, accept=True)
    assert (
        client.get(
            f"/api/v1/projects/{project_id}", headers=viewer["headers"]
        ).status_code
        == 200
    )


def test_invite_unknown_account_and_duplicates(client):
    owner, editor, viewer, outsider, project_id = _setup(client)
    # No account for that email: 404, and nothing claims email sent.
    response = _invite(
        client, owner, project_id, "ghost@example.com", "editor", expect=404
    )
    assert response.json()["error"]["code"] == "account_not_found"
    # The owner cannot be invited to their own project.
    _invite(client, owner, project_id, owner["email"], "editor", expect=422)
    # Duplicate pending invitation: 409.
    _invite(client, owner, project_id, editor["email"], "editor")
    _invite(client, owner, project_id, editor["email"], "editor", expect=409)
    # Bad role: 422.
    _invite(client, owner, project_id, viewer["email"], "admin", expect=422)


# ---------------------------------------------------------------------
# Role enforcement matrix
# ---------------------------------------------------------------------


def test_viewer_is_read_only_but_can_discuss(client):
    owner, editor, viewer, outsider, project_id = _setup(client)
    _share(client, owner, viewer, project_id, "viewer")

    # Reads pass.
    for path in (
        f"/api/v1/projects/{project_id}",
        f"/api/v1/projects/{project_id}/status",
        f"/api/v1/projects/{project_id}/runs",
        f"/api/v1/projects/{project_id}/plan",
        f"/api/v1/projects/{project_id}/members",
        f"/api/v1/projects/{project_id}/comments",
        f"/api/v1/projects/{project_id}/activity",
    ):
        response = client.get(path, headers=viewer["headers"])
        assert response.status_code in (200, 404), (path, response.text)
        if path.endswith("/plan"):
            # No analysis exists yet in this test: 404 plan_not_found
            # is the honest read outcome; anything else must be 200.
            assert response.status_code == 404
        else:
            assert response.status_code == 200, (path, response.text)

    # Production writes: 403 project_read_only.
    response = client.post(
        f"/api/v1/projects/{project_id}/runs",
        headers=viewer["headers"],
        json={},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "project_read_only"
    response = client.post(
        f"/api/v1/projects/{project_id}/plan/decisions",
        headers=viewer["headers"],
        json={"sentence_number": 1, "action": "approve"},
    )
    assert response.status_code == 403
    response = client.post(
        f"/api/v1/projects/{project_id}/revisions",
        headers=viewer["headers"],
        json={},
    )
    assert response.status_code == 403

    # Discussion is open to viewers.
    response = client.post(
        f"/api/v1/projects/{project_id}/comments",
        headers=viewer["headers"],
        json={"body": "Reviewer note: the opening needs work."},
    )
    assert response.status_code == 201, response.text


def test_editor_writes_but_cannot_manage_members(client):
    owner, editor, viewer, outsider, project_id = _setup(client)
    _share(client, owner, editor, project_id, "editor")

    # Editor production write passes (run submission, fake queue).
    response = client.post(
        f"/api/v1/projects/{project_id}/runs",
        headers=editor["headers"],
        json={},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run"]["run_id"]
    run_ledger.claim_run(run_id)
    run_ledger.finish_run(run_id, "completed")

    # The run is stamped with the PROJECT owner's id, and both the
    # owner and the editor can read it; the outsider cannot.
    assert (
        client.get(f"/api/v1/runs/{run_id}", headers=owner["headers"]).status_code
        == 200
    )
    assert (
        client.get(f"/api/v1/runs/{run_id}", headers=editor["headers"]).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/v1/runs/{run_id}", headers=outsider["headers"]
        ).status_code
        == 404
    )

    # Ownership-level actions are 404 for the editor.
    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=editor["headers"],
        json={"email": viewer["email"], "role": "viewer"},
    )
    assert response.status_code == 404
    response = client.patch(
        f"/api/v1/projects/{project_id}/members/{editor['id']}",
        headers=editor["headers"],
        json={"role": "viewer"},
    )
    assert response.status_code == 404
    response = client.delete(
        f"/api/v1/projects/{project_id}/members/{owner['id']}",
        headers=editor["headers"],
    )
    assert response.status_code == 404


def test_non_member_gets_404_everywhere(client):
    owner, editor, viewer, outsider, project_id = _setup(client)
    _share(client, owner, editor, project_id, "editor")
    for method, path in (
        ("GET", f"/api/v1/projects/{project_id}"),
        ("GET", f"/api/v1/projects/{project_id}/members"),
        ("GET", f"/api/v1/projects/{project_id}/comments"),
        ("GET", f"/api/v1/projects/{project_id}/activity"),
        ("POST", f"/api/v1/projects/{project_id}/runs"),
        ("POST", f"/api/v1/projects/{project_id}/comments"),
        ("POST", f"/api/v1/projects/{project_id}/members"),
    ):
        # Valid bodies throughout, so the request reaches the
        # handler and it is the access check that answers.
        body = (
            {"body": "hello"}
            if path.endswith("/comments")
            else {"email": editor["email"], "role": "viewer"}
            if path.endswith("/members")
            else {}
        )
        response = client.request(
            method, path, headers=outsider["headers"], json=body
        )
        assert response.status_code == 404, (method, path, response.text)


def test_role_change_and_removal_take_effect_immediately(client):
    owner, editor, viewer, outsider, project_id = _setup(client)
    _share(client, owner, editor, project_id, "editor")

    # Demote editor -> viewer: writes stop on the next request.
    response = client.patch(
        f"/api/v1/projects/{project_id}/members/{editor['id']}",
        headers=owner["headers"],
        json={"role": "viewer"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "viewer"
    response = client.post(
        f"/api/v1/projects/{project_id}/runs",
        headers=editor["headers"],
        json={},
    )
    assert response.status_code == 403

    # Remove: access ends entirely.
    response = client.delete(
        f"/api/v1/projects/{project_id}/members/{editor['id']}",
        headers=owner["headers"],
    )
    assert response.status_code == 200
    assert response.json()["status"] == "removed"
    assert (
        client.get(
            f"/api/v1/projects/{project_id}", headers=editor["headers"]
        ).status_code
        == 404
    )


def test_member_can_leave_and_owner_cannot_be_removed(client):
    owner, editor, viewer, outsider, project_id = _setup(client)
    _share(client, owner, viewer, project_id, "viewer")
    response = client.delete(
        f"/api/v1/projects/{project_id}/members/{viewer['id']}",
        headers=viewer["headers"],
    )
    assert response.status_code == 200
    assert (
        client.get(
            f"/api/v1/projects/{project_id}", headers=viewer["headers"]
        ).status_code
        == 404
    )
    # The owner is not a member row: removing them is a 404.
    response = client.delete(
        f"/api/v1/projects/{project_id}/members/{owner['id']}",
        headers=owner["headers"],
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------


def test_comment_lifecycle_and_permissions(client):
    owner, editor, viewer, outsider, project_id = _setup(client)
    _share(client, owner, editor, project_id, "editor")

    created = client.post(
        f"/api/v1/projects/{project_id}/comments",
        headers=editor["headers"],
        json={"body": "First pass notes."},
    )
    assert created.status_code == 201, created.text
    comment = created.json()
    assert comment["author"]["email"] == editor["email"]
    comment_id = comment["comment_id"]

    # The owner cannot EDIT another member's comment...
    response = client.put(
        f"/api/v1/projects/{project_id}/comments/{comment_id}",
        headers=owner["headers"],
        json={"body": "Owner rewrite."},
    )
    assert response.status_code == 403
    # ...the author can...
    response = client.put(
        f"/api/v1/projects/{project_id}/comments/{comment_id}",
        headers=editor["headers"],
        json={"body": "First pass notes, corrected."},
    )
    assert response.status_code == 200
    assert response.json()["edited_at"] is not None
    # ...another member cannot delete it...
    _share(client, owner, viewer, project_id, "viewer")
    response = client.delete(
        f"/api/v1/projects/{project_id}/comments/{comment_id}",
        headers=viewer["headers"],
    )
    assert response.status_code == 403
    # ...but the owner can delete any comment on their project.
    response = client.delete(
        f"/api/v1/projects/{project_id}/comments/{comment_id}",
        headers=owner["headers"],
    )
    assert response.status_code == 200
    listed = client.get(
        f"/api/v1/projects/{project_id}/comments", headers=owner["headers"]
    ).json()
    assert listed["comments"] == []

    # A comment anchored to a nonexistent revision is refused.
    response = client.post(
        f"/api/v1/projects/{project_id}/comments",
        headers=editor["headers"],
        json={"body": "Anchored nowhere.", "revision_id": "nope"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------
# Activity feed — recorded events only
# ---------------------------------------------------------------------


def test_activity_feed_reflects_recorded_events(client):
    owner, editor, viewer, outsider, project_id = _setup(client)
    _share(client, owner, editor, project_id, "editor")
    _share(client, owner, viewer, project_id, "viewer")
    client.post(
        f"/api/v1/projects/{project_id}/comments",
        headers=editor["headers"],
        json={"body": "Kickoff."},
    )
    client.post(
        f"/api/v1/projects/{project_id}/plan/decisions",
        headers=owner["headers"],
        json={"sentence_number": 1, "action": "approve"},
    )

    feed = client.get(
        f"/api/v1/projects/{project_id}/activity", headers=viewer["headers"]
    )
    assert feed.status_code == 200, feed.text
    kinds = [event["kind"] for event in feed.json()["events"]]
    assert "member_invited" in kinds
    assert "invitation_accepted" in kinds
    assert "comment" in kinds
    assert "plan_decision" in kinds
    # Newest first.
    ats = [event["at"] for event in feed.json()["events"]]
    assert ats == sorted(ats, reverse=True)


def test_members_overview_shape(client):
    owner, editor, viewer, outsider, project_id = _setup(client)
    _share(client, owner, editor, project_id, "editor")
    _invite(client, owner, project_id, viewer["email"], "viewer")
    overview = client.get(
        f"/api/v1/projects/{project_id}/members", headers=editor["headers"]
    ).json()
    assert overview["owner"]["email"] == owner["email"]
    assert overview["your_role"] == "editor"
    assert [m["user"]["email"] for m in overview["members"]] == [editor["email"]]
    assert [i["user"]["email"] for i in overview["invitations"]] == [
        viewer["email"]
    ]
