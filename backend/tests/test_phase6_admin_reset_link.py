"""Phase 6 (A-16) — operator-mediated password reset.

An admin generates a one-time reset link for a user via
POST /api/v1/admin/users/{id}/reset-link, built on the existing
reset machinery (services/auth.issue_reset_token /
consume_reset_token via /auth/password-reset/confirm). Covered:
the admin-only matrix, one-time use through the real confirm flow,
the audit entry, and that neither the token nor the link ever
appears in logs or in the audit detail (B-08 stays fixed).
"""

import logging
import uuid
from urllib.parse import parse_qs, urlparse

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from database import SessionLocal
from models import AdminAuditLog, Base, Plan, Project, ProductionRun, User, UserSubscription
from services import admin_health
from services import auth as auth_service

OPERATOR_TOKEN = "phase6-reset-test-token"
PASSWORD = "initial-password-123"


def _uid():
    return str(uuid.uuid4())


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")
    monkeypatch.delenv("CINEMORA_PUBLIC_BASE_URL", raising=False)
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=SessionLocal().get_bind())
    fake = fakeredis.FakeRedis(decode_responses=True)
    auth_service.set_redis_client(fake)
    admin_health.set_redis_client(fake)
    db = SessionLocal()
    try:
        for model in (AdminAuditLog, ProductionRun, Project, UserSubscription, User, Plan):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()
    yield
    auth_service.reset_redis_client()
    admin_health.reset_redis_client()


def _make_user(email, *, role="user", password=PASSWORD):
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


def _headers(role):
    uid = _make_user(f"{role}-{uuid.uuid4().hex[:8]}@example.com", role=role)
    return _session_for(uid)


def _target():
    return _make_user(f"target-{uuid.uuid4().hex[:8]}@example.com")


def _token_from(link: str) -> str:
    return parse_qs(urlparse(link).query)["token"][0]


def test_anonymous_is_rejected(client):
    target = _target()
    response = client.post(f"/api/v1/admin/users/{target}/reset-link")
    assert response.status_code == 401


def test_ordinary_user_is_forbidden(client):
    target = _target()
    response = client.post(
        f"/api/v1/admin/users/{target}/reset-link", headers=_headers("user")
    )
    assert response.status_code == 403


@pytest.mark.parametrize("role", ["admin", "super_admin"])
def test_admin_roles_can_generate(client, role):
    target = _target()
    response = client.post(
        f"/api/v1/admin/users/{target}/reset-link", headers=_headers(role)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "/reset-password?token=" in body["reset_link"]
    assert body["expires_at"]


def test_unknown_user_is_404(client):
    response = client.post(
        f"/api/v1/admin/users/{_uid()}/reset-link", headers=_headers("admin")
    )
    assert response.status_code == 404


def test_link_is_one_time_via_existing_confirm_flow(client):
    target = _target()
    response = client.post(
        f"/api/v1/admin/users/{target}/reset-link", headers=_headers("admin")
    )
    token = _token_from(response.json()["reset_link"])
    confirm = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "brand-new-password-456"},
    )
    assert confirm.status_code == 200, confirm.text
    again = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "another-password-789"},
    )
    assert again.status_code == 400
    # The new password really signs the user in.
    db = SessionLocal()
    try:
        email = db.get(User, target).email
    finally:
        db.close()
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "brand-new-password-456"},
    )
    assert login.status_code == 200, login.text


def test_new_link_supersedes_previous(client):
    target = _target()
    headers = _headers("admin")
    first = client.post(f"/api/v1/admin/users/{target}/reset-link", headers=headers)
    second = client.post(f"/api/v1/admin/users/{target}/reset-link", headers=headers)
    old_token = _token_from(first.json()["reset_link"])
    confirm = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": old_token, "new_password": "brand-new-password-456"},
    )
    assert confirm.status_code == 400


def test_audit_entry_without_token(client, caplog):
    target = _target()
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            f"/api/v1/admin/users/{target}/reset-link", headers=_headers("admin")
        )
    assert response.status_code == 200
    link = response.json()["reset_link"]
    token = _token_from(link)
    db = SessionLocal()
    try:
        rows = (
            db.query(AdminAuditLog)
            .filter(AdminAuditLog.action == "user.password_reset_link_generated")
            .filter(AdminAuditLog.target_id == target)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].result == "success"
        assert token not in str(rows[0].detail)
        assert link not in str(rows[0].detail)
    finally:
        db.close()
    # B-08: neither the token nor the complete link reaches any log.
    assert token not in caplog.text
    assert link not in caplog.text
