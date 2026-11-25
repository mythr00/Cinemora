"""Phase 1b — secure deployment defaults (E-21 / B-05 / B-07 /
B-08 / E-24).

Proves, against the real API:
1. The operator-token dual-auth window is CLOSED by default: with
   CINEMORA_API_TOKEN set but CINEMORA_ALLOW_OPERATOR_TOKEN unset,
   the operator token does NOT resolve to a principal (401, and no
   founder account is auto-provisioned). With the explicit opt-in
   CINEMORA_ALLOW_OPERATOR_TOKEN=1 it does (founder super_admin).
2. Session auth keeps working while the window is closed.
3. The session cookie carries the Secure flag under deployment
   defaults (env unset), and an explicit CINEMORA_COOKIE_SECURE=0
   (local http development) turns it off.
4. A password reset issued in mailer "manual" mode writes NO raw
   token and NO reset link to any log — only a redacted record with
   a short sha256 reference.
"""

import hashlib
import logging
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
from auth_helpers import with_session_token
import main
from database import SessionLocal, engine
from models import Base, User
from services import auth as auth_service
from services import mailer

OPERATOR_TOKEN = "phase1b-operator-token"
PASSWORD = "phase1b-correct-horse-42"


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.delenv("CINEMORA_ALLOW_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("CINEMORA_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("CINEMORA_REGISTRATION", raising=False)
    monkeypatch.delenv("CINEMORA_SMTP_HOST", raising=False)
    monkeypatch.delenv("CINEMORA_SMTP_FROM", raising=False)
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(
        fakeredis.FakeRedis(decode_responses=True)
    )
    db = SessionLocal()
    try:
        db.query(User).delete()
        db.commit()
    finally:
        db.close()
    yield
    auth_service.reset_redis_client()
    db = SessionLocal()
    try:
        db.query(User).delete()
        db.commit()
    finally:
        db.close()


def _register(client, email):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return response


def _unique_email():
    return f"p1b-{uuid.uuid4().hex[:8]}@example.com"


# ---------------------------------------------------------------------
# Operator-token window (E-21 / B-05)
# ---------------------------------------------------------------------


def test_operator_token_closed_by_default(client):
    response = client.get(
        "/api/v1/auth/me", headers={"X-API-Key": OPERATOR_TOKEN}
    )
    assert response.status_code == 401, response.text
    # The closed window must not even auto-provision the founder.
    db = SessionLocal()
    try:
        assert (
            db.query(User)
            .filter(User.role == "super_admin")
            .count()
            == 0
        )
    finally:
        db.close()


def test_operator_token_with_explicit_opt_in(client, monkeypatch):
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")
    response = client.get(
        "/api/v1/auth/me", headers={"X-API-Key": OPERATOR_TOKEN}
    )
    assert response.status_code == 200, response.text
    user = response.json()["user"]
    assert user["role"] == "super_admin"
    assert user["email"] == auth_service.founder_email()


def test_session_auth_works_with_window_closed(client):
    body = with_session_token(_register(client, _unique_email()))
    me = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {body['session_token']}"},
    )
    assert me.status_code == 200, me.text
    assert me.json()["user"]["email"] == body["user"]["email"]


# ---------------------------------------------------------------------
# Session cookie Secure flag (E-21 / B-07)
# ---------------------------------------------------------------------


def test_session_cookie_secure_by_default(client):
    response = _register(client, _unique_email())
    set_cookie = response.headers.get("set-cookie", "")
    assert "cinemora_session=" in set_cookie
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie


def test_session_cookie_secure_explicit_opt_out(client, monkeypatch):
    monkeypatch.setenv("CINEMORA_COOKIE_SECURE", "0")
    response = _register(client, _unique_email())
    set_cookie = response.headers.get("set-cookie", "")
    assert "cinemora_session=" in set_cookie
    assert "Secure" not in set_cookie


# ---------------------------------------------------------------------
# Password-reset log leakage (B-08 / E-24)
# ---------------------------------------------------------------------


def test_password_reset_manual_mode_logs_no_token(
    client, monkeypatch, caplog
):
    email = _unique_email()
    _register(client, email)

    sent = {}

    def _capture_send(to, subject, body):
        sent.update(to=to, subject=subject, body=body)
        return "manual"

    monkeypatch.setattr(mailer, "send_email", _capture_send)

    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": email},
        )
    assert response.status_code == 202, response.text
    assert response.json() == {"delivery": "manual"}

    # The (undelivered) message body is where the only copy of the
    # raw token legitimately exists in-process; extract it there…
    assert "reset-password?token=" in sent["body"]
    raw_token = sent["body"].split("reset-password?token=", 1)[1]
    raw_token = raw_token.split()[0].strip()
    assert raw_token

    # …and prove neither it nor the reset link reached any log.
    logged = caplog.text
    assert raw_token not in logged
    assert "reset-password?token=" not in logged
    # The redacted record IS logged: recipient + hash-prefix ref.
    assert email in logged
    expected_ref = hashlib.sha256(raw_token.encode()).hexdigest()[:12]
    assert f"sha256:{expected_ref}" in logged


def test_mailer_manual_mode_logs_no_body(client, caplog):
    secret = "do-not-log-this-token-value"
    with caplog.at_level(logging.DEBUG, logger="services.mailer"):
        mode = mailer.send_email(
            "someone@example.com",
            "Cinemora password reset",
            f"Reset link: https://x/reset-password?token={secret}",
        )
    assert mode == "manual"
    assert secret not in caplog.text
    assert "reset-password?token=" not in caplog.text
