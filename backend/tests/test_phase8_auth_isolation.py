"""Phase 8.2 — accounts, sessions and the tenant-isolation battery
(design 02 §4.2, cases 1-7, 9, 10 + positive controls).

Users A and B are created through the REAL registration path;
sessions live in fakeredis (injected via services.auth's seam).
Case 8 (assistant conversations) is Phase 6 surface that does not
exist yet — it is enumerated in 02 §4.2 as post-Phase 6 and is
marked skipped here so the gap is explicit, not silent.
"""

import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from database import SessionLocal, engine
from models import Base, Plan, Project, User, UserSubscription
from services import auth as auth_service
from services import run_ledger

TOKEN = "phase8-" + "operator-token-for-tests"
PASSWORD_A = "alpha-correct-horse-42"
PASSWORD_B = "bravo-correct-horse-42"


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.delenv("CINEMORA_ALLOW_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("CINEMORA_REGISTRATION", raising=False)
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(fakeredis.FakeRedis(decode_responses=True))
    db = SessionLocal()
    try:
        # Clean slate for the account tables this module owns.
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
    # Leave the shared suite database as the module found it: a
    # default plan left behind would silently subject every later
    # module's principals to plan enforcement (Phase 8 gate find).
    db = SessionLocal()
    try:
        db.query(UserSubscription).delete()
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
    return response.json()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def users(client):
    suffix = uuid.uuid4().hex[:8]
    a = _register(client, f"a-{suffix}@example.com", PASSWORD_A)
    b = _register(client, f"b-{suffix}@example.com", PASSWORD_B)
    return a, b


def _a_project(client, a):
    response = client.post(
        "/api/v1/projects",
        headers=_bearer(a["session_token"]),
        json={"script": "Owner A script. One sentence. Another one."},
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------
# Auth flows
# ---------------------------------------------------------------------


def test_register_login_logout_me(client, users):
    a, _b = users
    me = client.get("/api/v1/auth/me", headers=_bearer(a["session_token"]))
    assert me.status_code == 200
    assert me.json()["user"]["email"] == a["user"]["email"]
    assert me.json()["user"]["role"] == "user"

    login = client.post(
        "/api/v1/auth/login",
        json={"email": a["user"]["email"], "password": PASSWORD_A},
    )
    assert login.status_code == 200, login.text
    token2 = login.json()["session_token"]

    out = client.post("/api/v1/auth/logout", headers=_bearer(token2))
    assert out.status_code == 200
    me2 = client.get("/api/v1/auth/me", headers=_bearer(token2))
    assert me2.status_code == 401


def test_register_duplicate_and_weak_password(client, users):
    a, _b = users
    dup = client.post(
        "/api/v1/auth/register",
        json={"email": a["user"]["email"], "password": "another-good-pass-1"},
    )
    assert dup.status_code == 409
    weak = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"weak-{uuid.uuid4().hex[:6]}@example.com",
            "password": "short",
        },
    )
    assert weak.status_code == 422
    assert weak.json()["error"]["code"] == "weak_password"


def test_login_wrong_password_and_unknown_email(client, users):
    a, _b = users
    wrong = client.post(
        "/api/v1/auth/login",
        json={"email": a["user"]["email"], "password": "not-the-password-1"},
    )
    assert wrong.status_code == 401
    ghost = client.post(
        "/api/v1/auth/login",
        json={
            "email": f"ghost-{uuid.uuid4().hex[:6]}@example.com",
            "password": "not-the-password-1",
        },
    )
    assert ghost.status_code == 401
    assert ghost.json()["error"]["code"] == wrong.json()["error"]["code"]


def test_registration_modes(client, monkeypatch):
    monkeypatch.setenv("CINEMORA_REGISTRATION", "closed")
    closed = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"c-{uuid.uuid4().hex[:6]}@example.com",
            "password": "a-perfectly-fine-pass",
        },
    )
    assert closed.status_code == 403
    assert closed.json()["error"]["code"] == "registration_closed"

    monkeypatch.setenv("CINEMORA_REGISTRATION", "invite")
    monkeypatch.setenv("CINEMORA_INVITE_CODE", "open-sesame-42")
    no_code = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"i-{uuid.uuid4().hex[:6]}@example.com",
            "password": "a-perfectly-fine-pass",
        },
    )
    assert no_code.status_code == 403
    with_code = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"i-{uuid.uuid4().hex[:6]}@example.com",
            "password": "a-perfectly-fine-pass",
            "invite_code": "open-sesame-42",
        },
    )
    assert with_code.status_code == 201


def test_password_reset_flow(client, users):
    a, _b = users
    request = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": a["user"]["email"]},
    )
    assert request.status_code == 202
    # No SMTP in tests: the honest manual mode is reported.
    assert request.json()["delivery"] == "manual"
    # Unknown accounts get the identical response (no enumeration).
    ghost = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": f"ghost-{uuid.uuid4().hex[:6]}@example.com"},
    )
    assert ghost.status_code == 202
    assert ghost.json() == request.json()

    db = SessionLocal()
    try:
        user = db.query(User).filter_by(email=a["user"]["email"]).one()
        assert user.password_reset_token_hash  # stored as SHA-256 only
        token = auth_service.issue_reset_token(db, user)
    finally:
        db.close()

    bad = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": "bogus-token", "new_password": "brand-new-pass-42"},
    )
    assert bad.status_code == 400
    ok = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "brand-new-pass-42"},
    )
    assert ok.status_code == 200
    # Single use: the same token cannot reset twice.
    again = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "another-new-pass-42"},
    )
    assert again.status_code == 400
    # Old sessions were revoked by the reset; new password logs in.
    me = client.get("/api/v1/auth/me", headers=_bearer(a["session_token"]))
    assert me.status_code == 401
    login = client.post(
        "/api/v1/auth/login",
        json={"email": a["user"]["email"], "password": "brand-new-pass-42"},
    )
    assert login.status_code == 200


def test_disabled_account_cannot_use_session_or_login(client, users):
    a, _b = users
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(email=a["user"]["email"]).one()
        user.status = "disabled"
        db.commit()
    finally:
        db.close()
    me = client.get("/api/v1/auth/me", headers=_bearer(a["session_token"]))
    assert me.status_code == 401
    login = client.post(
        "/api/v1/auth/login",
        json={"email": a["user"]["email"], "password": PASSWORD_A},
    )
    assert login.status_code == 403
    assert login.json()["error"]["code"] == "account_disabled"


def test_operator_token_window_and_retirement(client, users, monkeypatch):
    a, _b = users
    project = _a_project(client, a)
    # The operator token is the FOUNDER principal: on user routes it
    # is scoped like anyone else (founder != A), so A's project 404s.
    as_token = client.get(
        f"/api/v1/projects/{project['id']}", headers={"X-API-Key": TOKEN}
    )
    assert as_token.status_code == 404
    # ...but the founder-admin reads the admin surface with it.
    admin = client.get("/api/v1/admin/users", headers={"X-API-Key": TOKEN})
    assert admin.status_code == 200
    emails = {u["email"] for u in admin.json()["users"]}
    assert a["user"]["email"] in emails

    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "0")
    retired = client.get("/api/v1/admin/users", headers={"X-API-Key": TOKEN})
    assert retired.status_code == 401
    # Sessions are unaffected by the token's retirement.
    me = client.get("/api/v1/auth/me", headers=_bearer(a["session_token"]))
    assert me.status_code == 200


# ---------------------------------------------------------------------
# The isolation battery (02 §4.2)
# ---------------------------------------------------------------------


def test_battery_project_and_subresources(client, users):
    a, b = users
    project = _a_project(client, a)
    pid = project["id"]
    db = SessionLocal()
    try:
        row = db.get(Project, pid)
        assert row is not None and row.owner_id == a["user"]["id"]
    finally:
        db.close()

    # Case 1: every project sub-resource 404s for B.
    for suffix in (
        "",
        "/status",
        "/plan",
        "/assets",
        "/outputs",
        "/certificate",
        "/editor",
        "/revisions",
        "/runs",
    ):
        resp = client.get(
            f"/api/v1/projects/{pid}{suffix}",
            headers=_bearer(b["session_token"]),
        )
        assert resp.status_code == 404, (suffix, resp.status_code, resp.text)

    # Case 2: B's list contains none of A's projects.
    listing = client.get("/api/v1/projects", headers=_bearer(b["session_token"]))
    assert listing.status_code == 200
    assert all(p["id"] != pid for p in listing.json()["projects"])

    # Anonymous: 401 everywhere. (Cookie-free client — the shared
    # client's jar carries B's session, and for B the correct
    # answer is the 404 asserted above, not 401.)
    anon = TestClient(main.app, raise_server_exceptions=False).get(
        f"/api/v1/projects/{pid}"
    )
    assert anon.status_code == 401

    # Positive control: A reads everything.
    for suffix in ("", "/status", "/runs"):
        resp = client.get(
            f"/api/v1/projects/{pid}{suffix}",
            headers=_bearer(a["session_token"]),
        )
        assert resp.status_code == 200, (suffix, resp.text)


def test_battery_run_access_and_cancel(client, users):
    a, b = users
    project = _a_project(client, a)
    run_id = run_ledger.begin_run(project["id"], project["script"])
    assert run_id
    run = run_ledger.get_run(run_id)
    assert run["owner_id"] == a["user"]["id"]

    # Case 3: B cannot read or cancel A's run; the attempt changes
    # nothing.
    got = client.get(f"/api/v1/runs/{run_id}", headers=_bearer(b["session_token"]))
    assert got.status_code == 404
    cancel = client.post(
        f"/api/v1/runs/{run_id}/cancel", headers=_bearer(b["session_token"])
    )
    assert cancel.status_code == 404
    after = run_ledger.get_run(run_id)
    assert after["status"] == "running"
    assert after["cancel_requested"] is False

    # Positive control: A reads and cancels.
    got_a = client.get(f"/api/v1/runs/{run_id}", headers=_bearer(a["session_token"]))
    assert got_a.status_code == 200
    cancel_a = client.post(
        f"/api/v1/runs/{run_id}/cancel", headers=_bearer(a["session_token"])
    )
    assert cancel_a.status_code == 200
    run_ledger.finish_run(run_id, "cancelled")


def test_battery_submit_decide_upload_against_foreign_project(client, users):
    a, b = users
    project = _a_project(client, a)
    pid = project["id"]

    # Case 4: submit a run against A's project.
    submit = client.post(
        f"/api/v1/projects/{pid}/runs",
        headers=_bearer(b["session_token"]),
        json={},
    )
    assert submit.status_code == 404

    # Case 6: plan decision against A's project.
    decision = client.post(
        f"/api/v1/projects/{pid}/plan/decisions",
        headers=_bearer(b["session_token"]),
        json={"sentence_number": 1, "action": "approve"},
    )
    assert decision.status_code == 404

    # Case 7: upload into A's project.
    upload = client.post(
        f"/api/v1/projects/{pid}/uploads",
        headers=_bearer(b["session_token"]),
        files={"file": ("x.txt", b"hello")},
    )
    assert upload.status_code == 404


def test_battery_media_door(client, users):
    a, b = users
    project = _a_project(client, a)
    pid = project["id"]
    # Case 5: B asks for media under A's project — the door check
    # fires before any key resolution.
    resp = client.get(
        f"/api/v1/projects/{pid}/media/whatever-key",
        headers=_bearer(b["session_token"]),
    )
    assert resp.status_code == 404
    # Case 10: A asks under B's (nonexistent-for-A) project id with
    # a key shape from A's own project — still closed.
    b_project = client.post(
        "/api/v1/projects",
        headers=_bearer(b["session_token"]),
        json={"script": "Owner B script. One. Two."},
    ).json()
    resp10 = client.get(
        f"/api/v1/projects/{b_project['id']}/media/whatever-key",
        headers=_bearer(a["session_token"]),
    )
    assert resp10.status_code == 404


def test_battery_admin_endpoints(client, users):
    _a, b = users
    # Case 9: role "user" -> 403; anonymous -> 401. The anonymous
    # call needs a cookie-free client: the shared client's jar
    # legitimately carries B's session cookie, and a request with
    # a valid cookie is not anonymous.
    as_user = client.get(
        "/api/v1/admin/users", headers=_bearer(b["session_token"])
    )
    assert as_user.status_code == 403
    anon = TestClient(main.app, raise_server_exceptions=False).get(
        "/api/v1/admin/users"
    )
    assert anon.status_code == 401


@pytest.mark.skip(
    reason="Case 8 (assistant conversations) is Phase 6 surface; "
    "the endpoints do not exist yet. Enumerated in 02 §4.2 as "
    "post-Phase 6 and tested when Phase 6 lands."
)
def test_battery_assistant_conversations():
    pass
