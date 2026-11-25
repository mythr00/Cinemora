"""Phase 5b / B-15 — liveness + admin readiness.

- Public liveness returns exactly {"status": "ok"} (no internals).
- Admin readiness probes DB (real SELECT 1), Redis (ping),
  workers (RQ registry + Phase 3b lifecycle event store) and
  version, each with latency; up/down combinations are stubbed.
- No live worker evidence -> workers report "unknown", never ok.
- Readiness detail is admin-only.
"""

import json
import uuid
from datetime import datetime, timezone

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from database import SessionLocal
from models import Base, Plan, Project, ProductionRun, User, UserSubscription
from services import admin_health, readiness
from services import auth as auth_service

OPERATOR_TOKEN = "test-operator-token-p5b"


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")
    monkeypatch.setenv(
        "CINEMORA_WORKER_EVENTS_PATH", str(tmp_path / "worker_events.jsonl")
    )
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=SessionLocal().get_bind())
    fake = fakeredis.FakeRedis(decode_responses=True)
    auth_service.set_redis_client(fake)
    admin_health.set_redis_client(fake)
    yield
    auth_service.reset_redis_client()
    admin_health.reset_redis_client()


def _headers(role="user"):
    db = SessionLocal()
    try:
        user = User(
            id=str(uuid.uuid4()),
            email=f"{role}-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=auth_service.hash_password("pw-12345678"),
            role=role, status="active",
        )
        db.add(user)
        db.commit()
        uid = user.id
    finally:
        db.close()
    return {"Authorization": f"Bearer {auth_service.create_session(uid)}"}


class _DownRedis:
    def ping(self):
        raise ConnectionError("redis down")

    def scan_iter(self, match=None):
        raise ConnectionError("redis down")


class _BadDb:
    def execute(self, *args, **kwargs):
        raise RuntimeError("db down")


# ---------------------------------------------------------------------
# Public liveness
# ---------------------------------------------------------------------


def test_public_liveness_exact_body(client):
    for path in ("/health/live", "/api/v1/health/live"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}, path


def test_legacy_health_kept_compatible(client):
    assert client.get("/health").json() == {"status": "healthy"}
    assert client.get("/api/v1/health").json()["status"] == "ok"


# ---------------------------------------------------------------------
# Readiness: access control
# ---------------------------------------------------------------------


def test_readiness_is_admin_only(client):
    assert client.get("/api/v1/admin/readiness").status_code in (401, 403)
    plain = client.get("/api/v1/admin/readiness", headers=_headers("user"))
    assert plain.status_code == 403


# ---------------------------------------------------------------------
# Readiness: up/down combinations (service level, stubbed clients)
# ---------------------------------------------------------------------


def test_readiness_all_up_except_workers_unknown(client):
    report = readiness.readiness(SessionLocal())
    try:
        assert report["checks"]["database"]["status"] == "ok"
        assert report["checks"]["redis"]["status"] == "ok"
        # fakeredis has no registered RQ workers and the lifecycle
        # store is empty: truthful unknown, never a fake ok.
        assert report["checks"]["workers"]["status"] == "unknown"
        assert report["checks"]["workers"]["present"] is False
        assert report["checks"]["version"]["status"] == "ok"
        for check in report["checks"].values():
            assert "latency_ms" in check
        assert report["status"] == "degraded"
    finally:
        pass


def test_readiness_redis_down_is_not_ready():
    admin_health.set_redis_client(_DownRedis())
    report = readiness.readiness(SessionLocal())
    assert report["checks"]["redis"]["status"] == "error"
    assert report["checks"]["database"]["status"] == "ok"
    assert report["status"] == "not_ready"


def test_readiness_db_down_is_not_ready():
    report = readiness.readiness(_BadDb())
    assert report["checks"]["database"]["status"] == "error"
    assert report["status"] == "not_ready"


def test_readiness_worker_evidence_from_lifecycle_store(tmp_path, monkeypatch):
    path = tmp_path / "worker_events.jsonl"
    path.write_text(
        json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": "worker_start",
                "pid": 1234,
                "hostname": "test-host",
                "worker_id": "worker-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CINEMORA_WORKER_EVENTS_PATH", str(path))
    report = readiness.readiness(SessionLocal())
    workers = report["checks"]["workers"]
    assert workers["status"] == "ok"
    assert workers["present"] is True
    assert any(
        w["source"] == "worker_lifecycle_events" for w in workers["workers"]
    )
    assert report["status"] == "ready"


def test_readiness_worker_exit_supersedes_start(tmp_path, monkeypatch):
    path = tmp_path / "worker_events.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    path.write_text(
        json.dumps({"ts": now, "event": "worker_start", "pid": 1,
                    "hostname": "h", "worker_id": "w1"})
        + "\n"
        + json.dumps({"ts": now, "event": "worker_exit", "pid": 1,
                      "hostname": "h", "worker_id": "w1"})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CINEMORA_WORKER_EVENTS_PATH", str(path))
    report = readiness.readiness(SessionLocal())
    assert report["checks"]["workers"]["status"] == "unknown"


def test_readiness_http_admin_happy_path(client):
    response = client.get(
        "/api/v1/admin/readiness", headers=_headers("super_admin")
    )
    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["redis"]["status"] == "ok"
    assert "version" in body["checks"]
