"""Phase 5b / B-12 — distributed (Redis-backed) rate limiting.

- Two limiter instances sharing one Redis enforce ONE combined
  limit (the pre-5b per-process deque multiplied the limit by
  the worker count).
- Redis unreachable -> in-memory fallback ONLY, with the
  degradation logged loudly; limits/thresholds unchanged.
- The auth surface is covered end-to-end over HTTP.

The fake Redis below is hand-rolled (only the sorted-set
interface the limiter uses) so these tests do not depend on
fakeredis.
"""

import logging
import uuid

import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from api_v1 import ApiError, _RateLimiter
from database import SessionLocal
from models import Base, User
from services import auth as auth_service


class FakeRedis:
    """Minimal sorted-set Redis fake."""

    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}
        self.expires: dict[str, int] = {}

    def zremrangebyscore(self, key, minimum, maximum):
        zset = self.zsets.get(key, {})
        doomed = [m for m, s in zset.items() if minimum <= s <= maximum]
        for member in doomed:
            del zset[member]
        return len(doomed)

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def zadd(self, key, mapping):
        zset = self.zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if member not in zset:
                added += 1
            zset[member] = score
        return added

    def expire(self, key, seconds):
        self.expires[key] = seconds
        return True

    def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.zsets:
                del self.zsets[key]
                removed += 1
        return removed


class DownRedis:
    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise ConnectionError("redis is down")

        return _raise


def test_two_instances_share_one_combined_limit():
    fake = FakeRedis()
    first = _RateLimiter(scope="auth", redis_client=fake)
    second = _RateLimiter(scope="auth", redis_client=fake)
    identity = "auth:1.2.3.4:someone@example.com"
    first.check(identity, limit=3)
    second.check(identity, limit=3)
    first.check(identity, limit=3)
    # The 4th hit is refused by EITHER instance: one shared limit.
    with pytest.raises(ApiError) as excinfo:
        second.check(identity, limit=3)
    assert excinfo.value.status_code == 429
    assert first.peek(identity) == 3
    assert second.peek(identity) == 3


def test_scopes_do_not_share_limits():
    fake = FakeRedis()
    api = _RateLimiter(scope="api", redis_client=fake)
    auth = _RateLimiter(scope="auth", redis_client=fake)
    api.check("identity", limit=1)
    auth.check("identity", limit=1)  # different scope: still allowed
    with pytest.raises(ApiError):
        api.check("identity", limit=1)


def test_fallback_to_memory_when_redis_down(caplog):
    limiter = _RateLimiter(scope="auth", redis_client=DownRedis())
    with caplog.at_level(logging.ERROR, logger="api_v1"):
        limiter.check("identity", limit=2)
        limiter.check("identity", limit=2)
        with pytest.raises(ApiError) as excinfo:
            limiter.check("identity", limit=2)
    assert excinfo.value.status_code == 429
    assert limiter.degraded is True
    assert any(
        "RATE LIMITER DEGRADED" in record.message
        or "RATE LIMITER DEGRADED" in record.getMessage()
        for record in caplog.records
    )


def test_reset_clears_distributed_hits():
    fake = FakeRedis()
    limiter = _RateLimiter(scope="auth", redis_client=fake)
    limiter.check("identity", limit=5)
    assert limiter.peek("identity") == 1
    limiter.reset()
    assert limiter.peek("identity") == 0


# ---------------------------------------------------------------------
# HTTP: the auth surface enforces the shared limit
# ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    Base.metadata.create_all(bind=SessionLocal().get_bind())
    fake = FakeRedis()
    api_v1.set_rate_limit_redis_client(fake)
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    monkeypatch.setenv("CINEMORA_AUTH_RATE_LIMIT", "3")
    yield fake
    api_v1.reset_rate_limit_redis_client()
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()


def test_auth_login_limit_enforced_via_shared_redis(client):
    email = f"limited-{uuid.uuid4().hex[:8]}@example.com"
    db = SessionLocal()
    try:
        db.add(
            User(
                id=str(uuid.uuid4()),
                email=email,
                password_hash=auth_service.hash_password("pw-12345678"),
                role="user",
                status="active",
            )
        )
        db.commit()
    finally:
        db.close()
    for _ in range(3):
        response = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "wrong-password"},
        )
        assert response.status_code == 401
    blocked = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "wrong-password"},
    )
    assert blocked.status_code == 429
