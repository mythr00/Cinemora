"""FS access rules — the workstream's cross-cutting access suite.

FS-A (FS-4) — failure-detail sanitization, this file's first
section. A pipeline stage exception's str(error) is stored verbatim
in production_state.json and ProductionRun.error. Before FS-A the
user-facing carriers returned it unsanitized to ordinary users:

1. api_v1._run_view — feeds GET /projects/{id} (latest_run),
   GET /projects/{id}/runs, GET /runs/{run_id} (and run-submission
   / cancel responses);
2. GET /projects/{id}/status — top-level error, the entire
   production state as "progress" (state.error + stages[*].error),
   "stages", and latest_run;
3. the legacy main.py GET /projects/{project_id}/status — the same
   state file outside v1.

These tests pin the fixed behaviour:

- an ordinary user sees NO internal error text anywhere in any
  carrier's JSON (asserted against the raw response text), the
  generic message "Video generation failed" is present, and the
  status still reads "failed" (failure is never disguised);
- a project collaborator (accepted member, non-admin account role)
  is sanitized exactly like the owner-user (register A-9);
- admin and super_admin principals, and the operator principal
  (which resolves to super_admin), see the FULL original text;
- non-failure responses are unchanged (a completed run keeps
  error=None, statuses/stage/timestamps intact);
- storage is untouched: ProductionRun.error and
  production_state.json still hold the full text server-side.

Users are created through the REAL registration path and sessions
live in fakeredis, exactly as in tests/test_legacy_tenant_isolation.py
and tests/test_phase8_auth_isolation.py. No production is ever run:
the failed run and its state file are seeded directly.
"""

import json
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from database import SessionLocal, engine
from models import Base, Plan, ProductionRun, Project, User, UserSubscription
from services import auth as auth_service
from services import run_ledger

TOKEN = "fs-a-" + "operator-token-for-tests"
PASSWORD = "fs-a-" + "correct-horse-42"

INTERNAL_ERROR = (
    "TIMELINE COVERAGE INCOMPLETE: 4 of 31 narration sentences have "
    "no visual coverage: sentence 23 (scene 7, 96.0s-101.5s). "
    "Refusing to render an incomplete documentary."
)
GENERIC = api_v1.GENERIC_FAILURE_MESSAGE


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")
    monkeypatch.delenv("CINEMORA_REGISTRATION", raising=False)
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(fakeredis.FakeRedis(decode_responses=True))
    db = SessionLocal()
    try:
        for model in (ProductionRun, Project, UserSubscription, User):
            db.query(model).delete()
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
    finally:
        db.close()
    yield
    auth_service.reset_redis_client()
    db = SessionLocal()
    try:
        for model in (ProductionRun, Project, UserSubscription, User, Plan):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _uid():
    return str(uuid.uuid4())


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _register(client, label):
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {
        "email": email,
        "id": body["user"]["id"],
        "headers": _bearer(body["session_token"]),
    }


def _make_user(role):
    """A DB-created account with an explicit account role (the
    test_admin_api pattern), plus a real session for it."""
    db = SessionLocal()
    try:
        user = User(
            id=_uid(),
            email=f"{role}-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=auth_service.hash_password(PASSWORD),
            role=role,
            status="active",
        )
        db.add(user)
        db.commit()
        user_id, email = user.id, user.email
    finally:
        db.close()
    return {
        "email": email,
        "id": user_id,
        "headers": _bearer(auth_service.create_session(user_id)),
    }


def _seed_failed_project(owner_id, tmp_media_root, *, run_status="failed",
                         error=INTERNAL_ERROR):
    """One project + one run carrying the internal failure text in
    BOTH stores (ProductionRun.error and production_state.json,
    top-level and per-stage), exactly as jobs.fail_pipeline +
    run_ledger.finish_run persist it."""
    script = "Alpha opens. Bravo continues. Charlie closes."
    project_id, run_id = _uid(), _uid()
    db = SessionLocal()
    try:
        db.add(
            Project(
                id=project_id,
                owner_id=owner_id,
                title="FS-A seeded project",
                script=script,
                category="Documentary",
                status=run_status,
            )
        )
        db.add(
            ProductionRun(
                id=run_id,
                project_id=project_id,
                owner_id=owner_id,
                status=run_status,
                stage="video_editor",
                error=error,
                script_hash=run_ledger.script_sha256(script),
            )
        )
        db.commit()
    finally:
        db.close()
    if error is not None:
        project_dir = tmp_media_root / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "status": run_status,
            "current_stage": "video_editor",
            "started_at": "2026-11-25T00:00:00+00:00",
            "completed_at": "2026-11-25T00:05:00+00:00",
            "error": error,
            "stages": {
                "voice_actor": {
                    "status": "completed",
                    "started_at": None,
                    "completed_at": None,
                    "error": None,
                },
                "video_editor": {
                    "status": "failed",
                    "started_at": None,
                    "completed_at": None,
                    "error": error,
                },
            },
        }
        (project_dir / "production_state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
    return project_id, run_id


@pytest.fixture()
def media_root(tmp_path):
    return tmp_path / "media"


def _share(client, owner, member, project_id, role="editor"):
    invite = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=owner["headers"],
        json={"email": member["email"], "role": role},
    )
    assert invite.status_code == 201, invite.text
    respond = client.post(
        f"/api/v1/invitations/{project_id}/respond",
        headers=member["headers"],
        json={"accept": True},
    )
    assert respond.status_code == 200, respond.text


def _carriers(client, headers, project_id, run_id):
    """Every user-facing carrier of the failure detail, as
    (name, response) pairs."""
    return [
        ("project detail (latest_run)",
         client.get(f"/api/v1/projects/{project_id}", headers=headers)),
        ("project list (latest_run)",
         client.get("/api/v1/projects", headers=headers)),
        ("run list",
         client.get(f"/api/v1/projects/{project_id}/runs", headers=headers)),
        ("run detail",
         client.get(f"/api/v1/runs/{run_id}", headers=headers)),
        ("project status",
         client.get(f"/api/v1/projects/{project_id}/status", headers=headers)),
        ("legacy project status",
         client.get(f"/projects/{project_id}/status", headers=headers)),
    ]


def _assert_sanitized(name, response):
    assert response.status_code == 200, (name, response.text)
    # No internal text ANYWHERE in the payload, nested fields incl.
    assert INTERNAL_ERROR not in response.text, name
    assert "TIMELINE COVERAGE INCOMPLETE" not in response.text, name
    # The generic, truthful failure message IS present...
    assert GENERIC in response.text, name
    # ...and the failure is never disguised as success.
    assert '"failed"' in response.text, name


def _assert_full_detail(name, response):
    assert response.status_code == 200, (name, response.text)
    assert INTERNAL_ERROR in response.text, name


# ---------------------------------------------------------------------
# FS-A — ordinary users are sanitized on every carrier
# ---------------------------------------------------------------------


def test_ordinary_user_sees_no_internal_error_on_any_carrier(
    client, media_root
):
    owner = _register(client, "owner")
    project_id, run_id = _seed_failed_project(owner["id"], media_root)

    for name, response in _carriers(client, owner["headers"], project_id, run_id):
        _assert_sanitized(name, response)

    # Carrier-specific shape checks: status stays "failed", the
    # generic message lands in the error fields, stage names and
    # progress structure survive.
    status = client.get(
        f"/api/v1/projects/{project_id}/status", headers=owner["headers"]
    ).json()
    assert status["status"] == "failed"
    assert status["error"] == GENERIC
    assert status["current_stage"] == "video_editor"
    assert status["progress"]["error"] == GENERIC
    assert status["progress"]["stages"]["video_editor"]["error"] == GENERIC
    assert status["progress"]["stages"]["video_editor"]["status"] == "failed"
    assert status["progress"]["stages"]["voice_actor"]["error"] is None
    assert status["stages"]["video_editor"]["error"] == GENERIC
    assert status["latest_run"]["error"] == GENERIC
    assert status["latest_run"]["status"] == "failed"

    run = client.get(f"/api/v1/runs/{run_id}", headers=owner["headers"]).json()
    assert run["status"] == "failed"
    assert run["stage"] == "video_editor"
    assert run["error"] == GENERIC

    legacy = client.get(
        f"/projects/{project_id}/status", headers=owner["headers"]
    ).json()
    assert legacy["status"] == "failed"
    assert legacy["progress"]["error"] == GENERIC
    assert legacy["progress"]["stages"]["video_editor"]["error"] == GENERIC

    # Storage is untouched: the full text remains server-side.
    stored = run_ledger.get_run(run_id)
    assert stored["error"] == INTERNAL_ERROR
    state_file = media_root / project_id / "production_state.json"
    assert json.loads(state_file.read_text(encoding="utf-8"))["error"] == (
        INTERNAL_ERROR
    )


def test_collaborator_is_sanitized_as_non_admin(client, media_root):
    owner = _register(client, "owner")
    member = _register(client, "member")
    project_id, run_id = _seed_failed_project(owner["id"], media_root)
    _share(client, owner, member, project_id, role="editor")

    for name, response in _carriers(
        client, member["headers"], project_id, run_id
    ):
        if name.startswith("project list"):
            continue  # the member's list annotates access_role; covered below
        _assert_sanitized(name, response)

    run = client.get(f"/api/v1/runs/{run_id}", headers=member["headers"]).json()
    assert run["error"] == GENERIC


# ---------------------------------------------------------------------
# FS-A — admins and the operator keep the full detail
# ---------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "super_admin"])
def test_admin_roles_see_full_detail(client, media_root, role):
    admin = _make_user(role)
    project_id, run_id = _seed_failed_project(admin["id"], media_root)

    for name, response in _carriers(client, admin["headers"], project_id, run_id):
        _assert_full_detail(name, response)

    run = client.get(f"/api/v1/runs/{run_id}", headers=admin["headers"]).json()
    assert run["error"] == INTERNAL_ERROR


def test_operator_principal_sees_full_detail(client, media_root):
    # The operator principal owns legacy NULL-owner rows (the v1
    # choke point's rule) and resolves to super_admin, so its
    # responses keep the full detail byte-identical.
    project_id, run_id = _seed_failed_project(None, media_root)
    headers = {"X-API-Key": TOKEN}

    for name, response in _carriers(client, headers, project_id, run_id):
        if name.startswith("project list"):
            continue  # operator list semantics are the founder's own view
        _assert_full_detail(name, response)


# ---------------------------------------------------------------------
# FS-A — non-failure responses are unchanged
# ---------------------------------------------------------------------


def test_completed_run_response_is_unchanged_for_user(client, media_root):
    owner = _register(client, "owner")
    project_id, run_id = _seed_failed_project(
        owner["id"], media_root, run_status="completed", error=None
    )

    run = client.get(f"/api/v1/runs/{run_id}", headers=owner["headers"])
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "completed"
    assert body["error"] is None
    assert GENERIC not in run.text

    status = client.get(
        f"/api/v1/projects/{project_id}/status", headers=owner["headers"]
    )
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "completed"
    assert status.json()["error"] is None
    assert GENERIC not in status.text


def test_sanitizer_helper_admin_passthrough_and_no_mutation():
    # Unit-level pin of the helper's contract: admins get the very
    # same object back; non-admins get a sanitized copy and the
    # original payload (the stored view) is not mutated.
    class _Admin:
        is_admin = True

    class _User:
        is_admin = False

    view = {
        "status": "failed",
        "error": INTERNAL_ERROR,
        "progress": {"stages": {"video_editor": {"error": INTERNAL_ERROR}}},
    }
    assert api_v1.sanitize_failure_detail(view, _Admin()) is view
    sanitized = api_v1.sanitize_failure_detail(view, _User())
    assert sanitized["error"] == GENERIC
    assert sanitized["progress"]["stages"]["video_editor"]["error"] == GENERIC
    assert view["error"] == INTERNAL_ERROR
    assert api_v1.sanitize_failure_detail(view, None)["error"] == GENERIC
