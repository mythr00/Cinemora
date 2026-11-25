"""Developer API (founder priority #16; gap-audit doc 06 Phase N).

The honesty + security rules under test:

- A key secret is shown exactly once (the creation response);
  lists and reads carry only the prefix, and the database stores
  only the secret's SHA-256. Webhook signing secrets likewise.
- Keys authenticate ONLY on /api/public/v1; sessions and the
  legacy operator token do not work there, and the operator
  token + sessions keep working on /api/v1 exactly as before
  (the dual-auth window is untouched).
- Scopes are enforced server-side per route: a read key cannot
  write (403 insufficient_scope); revoked and expired keys fail
  closed (401) immediately.
- The public surface rides the api_v1 handlers: another user's
  project/run is a 404 through it, never a 403, and never listed.
- Per-key rate limits come from the codebase limiter; usage
  counters are written from real requests and match on both the
  public /usage surface and the account list.
- The docs payload is generated from the live public route
  table: every documented endpoint exists with its scope.
- Webhooks fire from the run ledger's finish path, are signed
  with HMAC-SHA256 keyed by the stored secret hash, retry
  transport/5xx failures up to 3 attempts, record every attempt,
  and refuse private/loopback destinations.
"""

import hashlib
import hmac as hmac_module
import json
import uuid
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_public
import api_v1
import jobs
import main
from database import SessionLocal, engine
from models import ApiKey, Base, Plan, ProductionRun, User
from services import admin_audit
from services import auth as auth_service
from services import run_ledger, webhooks

TOKEN = "test-operator-token"
PASSWORD = "correct-horse-9"

SCRIPT_BODY = (
    "On April 26, 1956, Malcolm McLean loaded 58 containers onto "
    "the Ideal X at Port Newark. The ship sailed for Houston."
)


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")  # E-21: explicit opt-in
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    api_public.public_limiter.reset()
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


def _register(client, email):
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(user):
    return {"Authorization": f"Bearer {user['session_token']}"}


@pytest.fixture()
def users(client):
    suffix = uuid.uuid4().hex[:8]
    a = _register(client, f"dev-a-{suffix}@example.com")
    b = _register(client, f"dev-b-{suffix}@example.com")
    return a, b


def _create_key(client, headers, **overrides):
    payload = {"name": "CI key", "scopes": ["read", "write"]}
    payload.update(overrides)
    response = client.post(
        "/api/v1/account/api-keys", json=payload, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def _key_headers(secret):
    return {"X-API-Key": secret}


def _create_project(client, headers, title="Container story"):
    response = client.post(
        "/api/v1/projects",
        json={"title": title, "script": SCRIPT_BODY, "category": "Industrial"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------
# Key lifecycle: shown once, hashed at rest, owner-scoped
# ---------------------------------------------------------------------


def test_key_secret_shown_once_and_stored_hashed(client, users):
    a, _ = users
    created = _create_key(client, _bearer(a))
    secret = created["secret"]
    assert secret.startswith("cmk_")
    assert created["key_prefix"] == secret[:12]
    assert created["status"] == "active"
    assert created["request_count"] == 0

    listing = client.get("/api/v1/account/api-keys", headers=_bearer(a))
    assert listing.status_code == 200, listing.text
    (listed,) = listing.json()["keys"]
    assert listed["id"] == created["id"]
    assert "secret" not in listed
    assert "key_hash" not in listed

    db = SessionLocal()
    try:
        row = db.get(ApiKey, created["id"])
        assert row.key_hash == hashlib.sha256(secret.encode()).hexdigest()
        # The plaintext secret appears in NO stored column.
        stored = json.dumps(
            {c.name: str(getattr(row, c.name)) for c in row.__table__.columns}
        )
        assert secret not in stored
    finally:
        db.close()


def test_key_creation_validation(client, users):
    a, _ = users
    for payload in (
        {"name": "x", "scopes": ["admin"]},
        {"name": "x", "scopes": []},
        {"name": "x", "scopes": ["read"], "rate_limit_per_minute": 0},
        {"name": "x", "scopes": ["read"], "rate_limit_per_minute": 601},
        {
            "name": "x",
            "scopes": ["read"],
            "expires_at": (
                datetime.now(timezone.utc) - timedelta(days=1)
            ).isoformat(),
        },
    ):
        response = client.post(
            "/api/v1/account/api-keys", json=payload, headers=_bearer(a)
        )
        assert response.status_code == 422, (payload, response.text)


def test_keys_are_owner_scoped(client, users):
    a, b = users
    created = _create_key(client, _bearer(a))
    listing_b = client.get("/api/v1/account/api-keys", headers=_bearer(b))
    assert listing_b.json()["keys"] == []
    revoke_b = client.delete(
        f"/api/v1/account/api-keys/{created['id']}", headers=_bearer(b)
    )
    assert revoke_b.status_code == 404, revoke_b.text
    # A's key still works after B's attempt.
    ok = client.get(
        "/api/public/v1/projects", headers=_key_headers(created["secret"])
    )
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------
# Public surface: auth, scopes, delegation, isolation
# ---------------------------------------------------------------------


def test_public_surface_requires_a_key(client, users):
    a, _ = users
    assert client.get("/api/public/v1/projects").status_code == 401
    bogus = client.get(
        "/api/public/v1/projects", headers=_key_headers("cmk_" + "x" * 43)
    )
    assert bogus.status_code == 401
    # A session bearer is NOT a key.
    session = client.get("/api/public/v1/projects", headers=_bearer(a))
    assert session.status_code == 401
    # The operator token is NOT a key either.
    operator = client.get(
        "/api/public/v1/projects", headers=_key_headers(TOKEN)
    )
    assert operator.status_code == 401


def test_public_reads_delegate_and_isolate(client, users):
    a, b = users
    project_a = _create_project(client, _bearer(a))
    _create_project(client, _bearer(b), title="Someone else's film")
    key = _create_key(client, _bearer(a), scopes=["read"])

    listing = client.get(
        "/api/public/v1/projects", headers=_key_headers(key["secret"])
    )
    assert listing.status_code == 200, listing.text
    ids = [p["id"] for p in listing.json()["projects"]]
    assert ids == [project_a["id"]]

    got = client.get(
        f"/api/public/v1/projects/{project_a['id']}",
        headers=_key_headers(key["secret"]),
    )
    assert got.status_code == 200, got.text
    assert got.json()["id"] == project_a["id"]

    # B's project through A's key: 404, never 403.
    listing_b = client.get(
        "/api/v1/projects", headers=_bearer(b)
    ).json()["projects"]
    other_id = listing_b[0]["id"]
    cross = client.get(
        f"/api/public/v1/projects/{other_id}",
        headers=_key_headers(key["secret"]),
    )
    assert cross.status_code == 404, cross.text
    cross_qa = client.get(
        f"/api/public/v1/projects/{other_id}/qa-report",
        headers=_key_headers(key["secret"]),
    )
    assert cross_qa.status_code == 404, cross_qa.text


def test_public_scope_enforcement(client, users):
    a, _ = users
    project = _create_project(client, _bearer(a))
    read_key = _create_key(client, _bearer(a), scopes=["read"])

    denied = client.post(
        "/api/public/v1/projects",
        json={"title": "Nope", "script": SCRIPT_BODY},
        headers=_key_headers(read_key["secret"]),
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "insufficient_scope"

    denied_run = client.post(
        f"/api/public/v1/projects/{project['id']}/runs",
        json={},
        headers=_key_headers(read_key["secret"]),
    )
    assert denied_run.status_code == 403, denied_run.text

    write_key = _create_key(client, _bearer(a), scopes=["read", "write"])
    created = client.post(
        "/api/public/v1/projects",
        json={"title": "Via API", "script": SCRIPT_BODY},
        headers=_key_headers(write_key["secret"]),
    )
    assert created.status_code == 201, created.text

    submitted = client.post(
        f"/api/public/v1/projects/{project['id']}/runs",
        json={},
        headers=_key_headers(write_key["secret"]),
    )
    assert submitted.status_code == 202, submitted.text
    run_id = submitted.json()["run"]["run_id"]

    fetched = client.get(
        f"/api/public/v1/runs/{run_id}",
        headers=_key_headers(read_key["secret"]),
    )
    assert fetched.status_code == 200, fetched.text

    runs = client.get(
        f"/api/public/v1/projects/{project['id']}/runs",
        headers=_key_headers(read_key["secret"]),
    )
    assert [r["run_id"] for r in runs.json()["runs"]] == [run_id]

    cancelled = client.post(
        f"/api/public/v1/runs/{run_id}/cancel",
        headers=_key_headers(write_key["secret"]),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["cancel_requested"] is True


def test_revoked_and_expired_keys_fail_closed(client, users):
    a, _ = users
    key = _create_key(client, _bearer(a))
    ok = client.get(
        "/api/public/v1/projects", headers=_key_headers(key["secret"])
    )
    assert ok.status_code == 200

    revoked = client.delete(
        f"/api/v1/account/api-keys/{key['id']}", headers=_bearer(a)
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    after = client.get(
        "/api/public/v1/projects", headers=_key_headers(key["secret"])
    )
    assert after.status_code == 401, after.text

    expiring = _create_key(
        client,
        _bearer(a),
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=1)
        ).isoformat(),
    )
    db = SessionLocal()
    try:
        row = db.get(ApiKey, expiring["id"])
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.commit()
    finally:
        db.close()
    expired = client.get(
        "/api/public/v1/projects", headers=_key_headers(expiring["secret"])
    )
    assert expired.status_code == 401, expired.text


def test_per_key_rate_limit_and_usage_recording(client, users):
    a, _ = users
    key = _create_key(
        client, _bearer(a), scopes=["read"], rate_limit_per_minute=3
    )
    headers = _key_headers(key["secret"])
    for _ in range(3):
        response = client.get("/api/public/v1/projects", headers=headers)
        assert response.status_code == 200, response.text
    limited = client.get("/api/public/v1/projects", headers=headers)
    assert limited.status_code == 429, limited.text
    assert limited.json()["error"]["code"] == "rate_limited"

    # A fresh key (separate budget) reports the recorded usage.
    key2 = _create_key(client, _bearer(a), scopes=["read"])
    headers2 = _key_headers(key2["secret"])
    client.get("/api/public/v1/projects", headers=headers2)
    usage = client.get("/api/public/v1/usage", headers=headers2)
    assert usage.status_code == 200, usage.text
    view = usage.json()["key"]
    assert view["id"] == key2["id"]
    assert view["request_count"] == 2  # the list call + this usage call
    assert view["requests_last_minute"] == 2
    assert view["last_used_at"] is not None
    assert view["rate_limit_per_minute"] >= 1

    listing = client.get("/api/v1/account/api-keys", headers=_bearer(a))
    by_id = {k["id"]: k for k in listing.json()["keys"]}
    assert by_id[key["id"]]["request_count"] == 3
    assert by_id[key2["id"]]["request_count"] == 2


def test_dual_auth_window_untouched(client, users):
    a, _ = users
    # Operator token still authenticates /api/v1 as before.
    operator = client.get("/api/v1/projects", headers=_key_headers(TOKEN))
    assert operator.status_code == 200, operator.text
    # Sessions still authenticate /api/v1.
    session = client.get("/api/v1/projects", headers=_bearer(a))
    assert session.status_code == 200, session.text
    # A wrong operator token still fails on /api/v1.
    wrong = client.get(
        "/api/v1/projects", headers=_key_headers("not-the-token")
    )
    assert wrong.status_code == 401, wrong.text


# ---------------------------------------------------------------------
# Docs: generated from the live route table
# ---------------------------------------------------------------------


def test_docs_match_the_live_public_surface(client, users):
    a, _ = users
    # A cookie-less client: the shared module client carries a
    # session cookie from registration, which legitimately
    # authenticates /api/v1 — the docs are account surfaces.
    anonymous = TestClient(main.app, raise_server_exceptions=False)
    assert anonymous.get("/api/v1/developer/docs").status_code == 401
    response = client.get("/api/v1/developer/docs", headers=_bearer(a))
    assert response.status_code == 200, response.text
    docs = response.json()
    documented = {
        (entry["method"], entry["path"], entry["scope"])
        for entry in docs["endpoints"]
    }
    expected = {
        (entry["method"], entry["path"], entry["scope"])
        for entry in api_public.PUBLIC_ROUTES
    }
    assert documented == expected
    live = {
        (method, api_public.BASE_PATH + route.path)
        for route in api_public.router.routes
        for method in (route.methods or set())
    }
    for entry in docs["endpoints"]:
        assert (entry["method"], entry["path"]) in live
        example_path = entry["path"].replace(
            "{project_id}", "PROJECT_ID"
        ).replace("{run_id}", "RUN_ID")
        assert example_path in entry["curl"]
        assert "X-API-Key" in entry["curl"]
    assert docs["auth"]["header"] == "X-API-Key"
    assert docs["rate_limit"]["default_per_minute"] >= 1


# ---------------------------------------------------------------------
# Admin visibility + audit trail
# ---------------------------------------------------------------------


def test_admin_overview_and_audit_trail(client, users):
    a, b = users
    key = _create_key(client, _bearer(a))
    client.get(
        "/api/public/v1/projects", headers=_key_headers(key["secret"])
    )

    # Non-admins are refused.
    denied = client.get("/api/v1/admin/developer", headers=_bearer(b))
    assert denied.status_code == 403, denied.text

    db = SessionLocal()
    try:
        admin = db.query(User).filter_by(email=a["user"]["email"]).one()
        admin.role = "super_admin"
        db.commit()
    finally:
        db.close()

    overview = client.get("/api/v1/admin/developer", headers=_bearer(a))
    assert overview.status_code == 200, overview.text
    body = overview.json()
    mine = [k for k in body["api_keys"] if k["id"] == key["id"]]
    assert len(mine) == 1
    assert mine[0]["owner_email"] == a["user"]["email"]
    assert mine[0]["request_count"] == 1
    assert "key_hash" not in mine[0] and "secret" not in mine[0]
    assert body["totals"]["api_requests_recorded"] >= 1

    audit = client.get(
        "/api/v1/admin/audit-log",
        params={"action": "api_key.created"},
        headers=_bearer(a),
    )
    assert audit.status_code == 200, audit.text
    entries = audit.json()["entries"]
    assert any(
        entry["target_id"] == key["id"] for entry in entries
    ), entries

    client.delete(
        f"/api/v1/account/api-keys/{key['id']}", headers=_bearer(a)
    )
    db = SessionLocal()
    try:
        rows = admin_audit.query(db, action="api_key.revoked")["entries"]
        assert any(r["target_id"] == key["id"] for r in rows)
    finally:
        db.close()


# ---------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------


def _create_webhook(client, headers, **overrides):
    payload = {
        "url": "https://hooks.example.com/cinemora",
        "events": ["run.completed"],
    }
    payload.update(overrides)
    response = client.post(
        "/api/v1/account/webhooks", json=payload, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def _stub_delivery(monkeypatch, statuses):
    """Stub the SSRF guard + transport; returns the captured calls."""
    import services.assistant.fetcher as fetcher

    monkeypatch.setattr(fetcher, "validate_url", lambda url: url)
    calls = []
    outcomes = list(statuses)

    def _post(url, body, headers):
        calls.append({"url": url, "body": body, "headers": headers})
        outcome = outcomes.pop(0) if outcomes else 200
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(webhooks, "_http_post", _post)
    return calls


def test_webhook_registration_validation_and_secrecy(client, users):
    a, b = users
    created = _create_webhook(client, _bearer(a))
    assert created["signing_secret"].startswith("whsec_")
    assert created["status"] == "active"

    listing = client.get("/api/v1/account/webhooks", headers=_bearer(a))
    (listed,) = listing.json()["endpoints"]
    assert "signing_secret" not in listed
    assert "secret_hash" not in listed

    for payload in (
        {"url": "http://127.0.0.1/hook", "events": ["run.completed"]},
        {"url": "http://10.0.0.5/hook", "events": ["run.completed"]},
        {"url": "ftp://example.com/hook", "events": ["run.completed"]},
        {"url": "https://example.com/hook", "events": ["run.unknown"]},
        {"url": "https://user:pw@example.com/hook", "events": ["run.completed"]},
    ):
        response = client.post(
            "/api/v1/account/webhooks", json=payload, headers=_bearer(a)
        )
        assert response.status_code == 422, (payload, response.text)

    # Cross-tenant: B sees nothing of A's endpoint.
    listing_b = client.get("/api/v1/account/webhooks", headers=_bearer(b))
    assert listing_b.json()["endpoints"] == []
    deliveries_b = client.get(
        f"/api/v1/account/webhooks/{created['id']}/deliveries",
        headers=_bearer(b),
    )
    assert deliveries_b.status_code == 404
    disable_b = client.delete(
        f"/api/v1/account/webhooks/{created['id']}", headers=_bearer(b)
    )
    assert disable_b.status_code == 404


def test_webhook_delivery_signing_and_recording(client, users, monkeypatch):
    a, _ = users
    project = _create_project(client, _bearer(a))
    endpoint = _create_webhook(client, _bearer(a))
    calls = _stub_delivery(monkeypatch, [200])

    run_id = run_ledger.begin_run(project["id"], SCRIPT_BODY)
    assert run_id
    webhooks.notify_run_finished(run_id, "completed", wait=True)

    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://hooks.example.com/cinemora"
    assert call["headers"]["X-Cinemora-Event"] == "run.completed"
    payload = json.loads(call["body"])
    assert payload["data"]["run_id"] == run_id
    assert payload["data"]["status"] == "completed"
    expected = hmac_module.new(
        hashlib.sha256(endpoint["signing_secret"].encode()).hexdigest().encode(),
        call["body"],
        hashlib.sha256,
    ).hexdigest()
    assert call["headers"]["X-Cinemora-Signature"] == f"sha256={expected}"
    assert call["headers"]["X-Cinemora-Delivery"] == payload["id"]

    deliveries = client.get(
        f"/api/v1/account/webhooks/{endpoint['id']}/deliveries",
        headers=_bearer(a),
    )
    (record,) = deliveries.json()["deliveries"]
    assert record["response_status"] == 200
    assert record["attempt"] == 1
    assert record["event"] == "run.completed"
    assert record["run_id"] == run_id


def test_webhook_retries_and_event_filtering(client, users, monkeypatch):
    a, _ = users
    project = _create_project(client, _bearer(a))
    failed_only = _create_webhook(
        client, _bearer(a), events=["run.failed"]
    )
    both = _create_webhook(
        client,
        _bearer(a),
        url="https://hooks.example.com/other",
        events=["run.completed", "run.failed"],
    )
    calls = _stub_delivery(
        monkeypatch, [ConnectionError("down"), 500, 200, 400]
    )

    run_id = run_ledger.begin_run(project["id"], SCRIPT_BODY)
    webhooks.notify_run_finished(run_id, "completed", wait=True)

    # Only the `both` endpoint is subscribed to run.completed: two
    # retryable failures then success = 3 attempts, one group.
    assert len(calls) == 3
    deliveries = client.get(
        f"/api/v1/account/webhooks/{both['id']}/deliveries",
        headers=_bearer(a),
    ).json()["deliveries"]
    assert len(deliveries) == 3
    assert {d["attempt"] for d in deliveries} == {1, 2, 3}
    assert len({d["delivery_group"] for d in deliveries}) == 1
    assert {d["response_status"] for d in deliveries} == {None, 500, 200}

    none_for_failed_only = client.get(
        f"/api/v1/account/webhooks/{failed_only['id']}/deliveries",
        headers=_bearer(a),
    ).json()["deliveries"]
    assert none_for_failed_only == []

    # A 4xx is final: exactly one more attempt, no retries.
    run_id2 = run_ledger.begin_run(project["id"], SCRIPT_BODY)
    webhooks.notify_run_finished(run_id2, "failed", wait=True)
    assert len(calls) == 5  # both endpoints subscribed to run.failed
    failed_deliveries = client.get(
        f"/api/v1/account/webhooks/{failed_only['id']}/deliveries",
        headers=_bearer(a),
    ).json()["deliveries"]
    assert len(failed_deliveries) == 1
    assert failed_deliveries[0]["event"] == "run.failed"


def test_webhook_disabled_endpoint_gets_nothing(client, users, monkeypatch):
    a, _ = users
    project = _create_project(client, _bearer(a))
    endpoint = _create_webhook(client, _bearer(a))
    disabled = client.delete(
        f"/api/v1/account/webhooks/{endpoint['id']}", headers=_bearer(a)
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"

    calls = _stub_delivery(monkeypatch, [200])
    run_id = run_ledger.begin_run(project["id"], SCRIPT_BODY)
    webhooks.notify_run_finished(run_id, "completed", wait=True)
    assert calls == []


def test_finish_run_fires_the_webhook_hook(client, users, monkeypatch):
    seen = []
    monkeypatch.setattr(
        webhooks,
        "notify_run_finished",
        lambda run_id, status, **kw: seen.append((run_id, status)),
    )
    a, _ = users
    project = _create_project(client, _bearer(a))
    run_id = run_ledger.begin_run(project["id"], SCRIPT_BODY)
    assert run_ledger.finish_run(run_id, "completed") is True
    assert seen == [(run_id, "completed")]
