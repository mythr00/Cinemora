"""Phase 7 — the administration API (/api/v1/admin).

Covers the acceptance rules from the design package
(cinemora-audit/admin/04 §Acceptance):
- every admin route rejects anonymous callers, ordinary users and
  the wrong credential (route-table test);
- admin figures reconcile with the rows seeded here;
- the writes (run cancel, replacement submission, account suspend /
  reactivate, plan assignment) perform the real operation AND land
  in the audit log with the right actor and target;
- no secret value appears in any admin response;
- administrator sign-ins are audit events.
"""

import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_admin
import api_v1
import main
from database import SessionLocal
from models import (
    AdminAuditLog,
    Base,
    Plan,
    ProductionRun,
    Project,
    User,
    UserSubscription,
)
from services import admin_health, admin_storage
from services import auth as auth_service
from services import run_ledger

OPERATOR_TOKEN = "operator-token-for-admin-tests"
ADMIN_PASSWORD = "admin-correct-horse-42"
USER_PASSWORD = "user-correct-horse-42"


def _uid():
    return str(uuid.uuid4())


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.delenv("CINEMORA_ALLOW_OPERATOR_TOKEN", raising=False)
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=SessionLocal().get_bind())
    fake = fakeredis.FakeRedis(decode_responses=True)
    auth_service.set_redis_client(fake)
    admin_health.set_redis_client(fake)
    admin_storage.invalidate_cache()
    db = SessionLocal()
    try:
        for model in (AdminAuditLog, ProductionRun, Project,
                      UserSubscription, User, Plan):
            db.query(model).delete()
        db.commit()
        db.add_all(
            [
                Plan(id=_uid(), name="free", max_concurrent_runs=1,
                     is_default=True),
                Plan(id=_uid(), name="pro", max_concurrent_runs=3),
            ]
        )
        db.commit()
    finally:
        db.close()
    yield
    auth_service.reset_redis_client()
    admin_health.reset_redis_client()
    db = SessionLocal()
    try:
        for model in (AdminAuditLog, ProductionRun, Project,
                      UserSubscription, User, Plan):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


def _make_user(email, *, role="user", password=USER_PASSWORD):
    db = SessionLocal()
    try:
        user = User(
            id=_uid(), email=email,
            password_hash=auth_service.hash_password(password),
            role=role, status="active",
        )
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


def _session_for(user_id):
    return {"Authorization": f"Bearer {auth_service.create_session(user_id)}"}


@pytest.fixture()
def admin_headers():
    admin_id = _make_user(
        f"admin-{uuid.uuid4().hex[:8]}@example.com",
        role="super_admin", password=ADMIN_PASSWORD,
    )
    return _session_for(admin_id)


@pytest.fixture()
def plain_headers():
    user_id = _make_user(f"user-{uuid.uuid4().hex[:8]}@example.com")
    return _session_for(user_id)


def _audit_rows(action=None):
    db = SessionLocal()
    try:
        q = db.query(AdminAuditLog)
        if action:
            q = q.filter(AdminAuditLog.action == action)
        return [
            (row.actor_identity, row.action, row.target_id, row.result)
            for row in q.all()
        ]
    finally:
        db.close()


# ---------------------------------------------------------------------
# Authorisation matrix
# ---------------------------------------------------------------------

ADMIN_ROUTES = [
    ("GET", "/api/v1/admin/overview", None),
    ("GET", "/api/v1/admin/health", None),
    ("GET", "/api/v1/admin/workers", None),
    ("GET", "/api/v1/admin/runs", None),
    ("GET", "/api/v1/admin/runs/some-run", None),
    ("POST", "/api/v1/admin/runs/some-run/cancel", None),
    ("POST", "/api/v1/admin/runs/some-run/replacement", None),
    ("GET", "/api/v1/admin/providers/usage", None),
    ("GET", "/api/v1/admin/storage", None),
    ("GET", "/api/v1/admin/storage/orphans", None),
    ("GET", "/api/v1/admin/settings", None),
    ("GET", "/api/v1/admin/audit-log", None),
    ("GET", "/api/v1/admin/users", None),
    ("GET", "/api/v1/admin/users/some-user", None),
    ("POST", "/api/v1/admin/users/some-user/status",
     {"status": "disabled"}),
    ("POST", "/api/v1/admin/users/some-user/plan", {"plan_name": "pro"}),
    ("GET", "/api/v1/admin/subscriptions", None),
    ("GET", "/api/v1/admin/projects", None),
    # M9 — AI Assistant administration (Phase 6 hooks).
    ("GET", "/api/v1/admin/assistant/tools", None),
    ("POST", "/api/v1/admin/assistant/tools/some-tool/disable",
     {"reason": "pin"}),
    ("POST", "/api/v1/admin/assistant/tools/some-tool/enable", {}),
    ("GET", "/api/v1/admin/assistant/actions", None),
    ("GET", "/api/v1/admin/assistant/conversations", None),
    ("GET", "/api/v1/admin/assistant/security-events", None),
    # Phase N — Developer API visibility (founder #16).
    ("GET", "/api/v1/admin/developer", None),
]


def test_route_table_matches_registered_admin_routes(client):
    # FastAPI 0.165 keeps included routers lazy in app.routes, so
    # the admin router's own route table is the authoritative list;
    # it is mounted at /api/v1 (main.py), its prefix adds /admin.
    registered = set()
    for route in api_admin.router.routes:
        path = "/api/v1" + getattr(route, "path", "")
        for method in route.methods or set():
            if method in ("GET", "POST"):
                registered.add((method, path))
    expected = {
        (method, path.replace("some-run", "{run_id}")
         .replace("some-user", "{user_id}")
         .replace("some-tool", "{name}"))
        for method, path, _ in ADMIN_ROUTES
    }
    assert registered == expected


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
def test_admin_routes_reject_anonymous_and_plain_users(
    client, plain_headers, method, path, body
):
    kwargs = {"json": body} if body is not None else {}
    anonymous = client.request(method, path, **kwargs)
    assert anonymous.status_code in (401, 403), (method, path)
    plain = client.request(method, path, headers=plain_headers, **kwargs)
    assert plain.status_code == 403, (method, path, plain.text)
    wrong_key = client.request(
        method, path, headers={"X-API-Key": "not-the-token"}, **kwargs
    )
    assert wrong_key.status_code == 401, (method, path)


def test_denied_attempts_are_audited(client, plain_headers):
    client.get("/api/v1/admin/overview", headers=plain_headers)
    rows = _audit_rows("admin.auth_denied")
    assert rows and rows[0][3] == "failure"


def test_operator_token_principal_is_founder_admin(client):
    # The dual-auth window: the operator token resolves to the
    # seeded super_admin founder, who passes the admin guard.
    response = client.get(
        "/api/v1/admin/overview", headers={"X-API-Key": OPERATOR_TOKEN}
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------
# Reads reconcile with seeded data
# ---------------------------------------------------------------------


def _seed_project_run(owner_id, status="failed"):
    db = SessionLocal()
    try:
        project = Project(
            id=_uid(), script="Seeded script.", title="Seeded",
            category="Documentary", status="created", owner_id=owner_id,
        )
        db.add(project)
        run = ProductionRun(
            id=_uid(), project_id=project.id, owner_id=owner_id,
            status=status, stage="asset_manager", error="boom",
            script_hash=run_ledger.script_sha256("Seeded script."),
            execution={
                "serper_calls": {"total": 3, "web": 3, "images": 0,
                                 "video": 0},
                "llm_calls": {"total": 1, "total_tokens": 10},
                "downloads": {"attempted": 0, "succeeded": 0,
                              "failed": 0, "bytes_downloaded": 0},
            },
        )
        db.add(run)
        db.commit()
        return project.id, run.id
    finally:
        db.close()


def test_overview_and_usage_endpoints(client, admin_headers):
    owner = _make_user(f"owner-{uuid.uuid4().hex[:8]}@example.com")
    _seed_project_run(owner)
    overview = client.get("/api/v1/admin/overview", headers=admin_headers)
    assert overview.status_code == 200
    body = overview.json()
    assert body["runs_by_status"].get("failed", 0) >= 1
    assert body["latest_failures"][0]["error"] == "boom"

    usage = client.get(
        "/api/v1/admin/providers/usage", headers=admin_headers
    ).json()
    assert usage["totals"]["serper_calls"]["total"] >= 3
    assert usage["monetary_cost_usd"] is None

    runs = client.get("/api/v1/admin/runs", headers=admin_headers).json()
    assert runs["total"] >= 1


def test_health_endpoint_components(client, admin_headers):
    body = client.get("/api/v1/admin/health", headers=admin_headers).json()
    components = body["components"]
    assert components["database"]["status"] == "ok"
    assert components["redis"]["status"] == "ok"
    assert components["workers"]["status"] == "absent"
    assert components["public_site"]["status"] == "not_configured"


def test_settings_never_leak_secret_values(client, admin_headers, monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "serper-sentinel-secret")
    monkeypatch.setenv("BREWKEG_API_KEY", "brewkeg-sentinel-secret")
    response = client.get("/api/v1/admin/settings", headers=admin_headers)
    assert response.status_code == 200
    assert "serper-sentinel-secret" not in response.text
    assert "brewkeg-sentinel-secret" not in response.text
    credentials = response.json()["groups"]["credentials"]
    serper = [e for e in credentials if e["name"] == "SERPER_API_KEY"][0]
    assert serper["configured"] is True and serper["secret"] is True
    assert "value" not in serper


# ---------------------------------------------------------------------
# Writes: real effects + audit rows
# ---------------------------------------------------------------------


def test_cancel_run_is_real_and_audited(client, admin_headers):
    owner = _make_user(f"owner-{uuid.uuid4().hex[:8]}@example.com")
    _, run_id = _seed_project_run(owner, status="queued")
    response = client.post(
        f"/api/v1/admin/runs/{run_id}/cancel", headers=admin_headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["cancel_requested"] is True
    assert run_ledger.get_run(run_id)["cancel_requested"] is True
    rows = [r for r in _audit_rows("run.cancel") if r[2] == run_id]
    assert rows and rows[0][0].endswith("@example.com")


def test_replacement_submits_new_run_and_audits(
    client, admin_headers, monkeypatch
):
    owner = _make_user(f"owner-{uuid.uuid4().hex[:8]}@example.com")
    project_id, run_id = _seed_project_run(owner, status="failed")

    def fake_enqueue(pid, script, options=None, run_id=None):
        submission = run_ledger.submit_production(
            pid, script, options=options
        )
        return submission["run_id"]

    monkeypatch.setattr(api_admin.jobs, "enqueue_full_pipeline", fake_enqueue)
    response = client.post(
        f"/api/v1/admin/runs/{run_id}/replacement", headers=admin_headers
    )
    assert response.status_code == 200, response.text
    new_run = response.json()["run"]
    assert new_run and new_run["run_id"] != run_id
    assert new_run["status"] == "queued"
    rows = [
        r for r in _audit_rows("run.submit_replacement")
        if r[2] == new_run["run_id"]
    ]
    assert rows

    active = client.post(
        f"/api/v1/admin/runs/{new_run['run_id']}/replacement",
        headers=admin_headers,
    )
    assert active.status_code == 409


def test_suspend_and_reactivate_user(client, admin_headers):
    target_id = _make_user(f"target-{uuid.uuid4().hex[:8]}@example.com")
    target_headers = _session_for(target_id)
    assert client.get("/api/v1/auth/me", headers=target_headers).status_code == 200

    response = client.post(
        f"/api/v1/admin/users/{target_id}/status",
        headers=admin_headers, json={"status": "disabled"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["user"]["status"] == "disabled"
    # Sessions revoked + disabled accounts rejected on every path.
    assert client.get("/api/v1/auth/me", headers=target_headers).status_code in (401, 403)
    rows = [r for r in _audit_rows("user.status_change") if r[2] == target_id]
    assert rows

    response = client.post(
        f"/api/v1/admin/users/{target_id}/status",
        headers=admin_headers, json={"status": "active"},
    )
    assert response.json()["user"]["status"] == "active"


def test_admin_cannot_disable_self(client, admin_headers):
    me = client.get("/api/v1/auth/me", headers=admin_headers).json()["user"]
    response = client.post(
        f"/api/v1/admin/users/{me['id']}/status",
        headers=admin_headers, json={"status": "disabled"},
    )
    assert response.status_code == 409


def test_non_super_admin_cannot_change_status(client):
    plain_admin = _make_user(
        f"pa-{uuid.uuid4().hex[:8]}@example.com", role="admin"
    )
    headers = _session_for(plain_admin)
    target_id = _make_user(f"t-{uuid.uuid4().hex[:8]}@example.com")
    response = client.post(
        f"/api/v1/admin/users/{target_id}/status",
        headers=headers, json={"status": "disabled"},
    )
    assert response.status_code == 403
    # ...but reads are open to the whole admin ladder.
    assert client.get("/api/v1/admin/users", headers=headers).status_code == 200


def test_plan_assignment_still_works_and_is_audited(client, admin_headers):
    target_id = _make_user(f"t-{uuid.uuid4().hex[:8]}@example.com")
    response = client.post(
        f"/api/v1/admin/users/{target_id}/plan",
        headers=admin_headers, json={"plan_name": "pro"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["plan"] == "pro"
    rows = [r for r in _audit_rows("user.plan_assign") if r[2] == target_id]
    assert rows
    detail = client.get(
        f"/api/v1/admin/users/{target_id}", headers=admin_headers
    ).json()
    assert detail["plan"] == "pro"
    assert "usage" in detail


def test_users_search_and_subscriptions_view(client, admin_headers):
    email = f"findme-{uuid.uuid4().hex[:8]}@example.com"
    _make_user(email)
    found = client.get(
        "/api/v1/admin/users", headers=admin_headers,
        params={"q": "findme"},
    ).json()
    assert [u["email"] for u in found["users"]] == [email]
    subs = client.get(
        "/api/v1/admin/subscriptions", headers=admin_headers
    ).json()
    assert {p["name"] for p in subs["plans"]} == {"free", "pro"}
    assert "no payment" in subs["billing_note"].lower()


def test_audit_log_endpoint_returns_entries(client, admin_headers):
    client.post(
        f"/api/v1/admin/runs/{_seed_project_run(_make_user(f'o-{uuid.uuid4().hex[:8]}@example.com'), status='queued')[1]}/cancel",
        headers=admin_headers,
    )
    body = client.get(
        "/api/v1/admin/audit-log", headers=admin_headers,
        params={"action": "run.cancel"},
    ).json()
    assert body["total"] >= 1
    assert body["entries"][0]["action"] == "run.cancel"
    # The viewer is read-only: no write verbs exist on it.
    assert client.post(
        "/api/v1/admin/audit-log", headers=admin_headers, json={}
    ).status_code in (404, 405)


def test_admin_sign_in_is_audited(client):
    email = f"boss-{uuid.uuid4().hex[:8]}@example.com"
    _make_user(email, role="super_admin", password=ADMIN_PASSWORD)
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    rows = [r for r in _audit_rows("admin.sign_in") if r[0] == email]
    assert rows
