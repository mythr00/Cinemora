"""Phase 8.3 — plans and usage limits (design 02 §5.3, stage
acceptance in 04 §8.3).

Enforcement is proven at each point with the typed error AND the
"nothing was created" assertion; usage numbers are checked against
an independent recomputation from the ledger; the founder plan is
the uncapped control. Queueing is stubbed (FakeQueue) — the run
rows the tests count are real ledger rows.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import jobs
import main
from database import SessionLocal, engine
from models import (
    Base,
    Plan,
    ProductionRun,
    Project,
    User,
    UserSubscription,
)
from services import auth as auth_service
from services import media_paths
from services import run_ledger

TOKEN = "phase8-" + "operator-token-for-tests"
PASSWORD = "correct-horse-battery-42"


class FakeQueue:
    def __init__(self):
        self.calls = []

    def enqueue(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return SimpleNamespace(id=f"job-{len(self.calls)}")


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _plans_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")  # E-21: explicit opt-in
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(fakeredis.FakeRedis(decode_responses=True))
    _paired_queue = FakeQueue()
    monkeypatch.setattr(jobs, "video_queue", _paired_queue)
    monkeypatch.setattr(jobs, "video_queue_priority", _paired_queue)
    db = SessionLocal()
    try:
        # FK-safe wipe (children before parents, from the metadata
        # graph): PostgreSQL enforces FKs that SQLite test runs
        # historically tolerated violating (E-07 readiness).
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
        specs = {
            # name: (concurrent, per-month, script, storage, upload, tier, default)
            "founder": (2_147_483_647, None, None, None, None, 1, False),
            "free": (1, 3, 20_000, 2 * 1024**3, 512 * 1024**2, 0, True),
            "tiny": (1, 100, 10, 1, 4, 0, False),
        }
        for name, (conc, quota, script, storage, upload, tier, default) in specs.items():
            db.add(
                Plan(
                    id=str(uuid.uuid4()),
                    name=name,
                    max_concurrent_runs=conc,
                    max_runs_per_period=quota,
                    period="month",
                    max_script_chars=script,
                    max_storage_bytes=storage,
                    max_upload_bytes=upload,
                    priority_tier=tier,
                    is_default=default,
                )
            )
        db.commit()
    finally:
        db.close()
    yield
    auth_service.reset_redis_client()
    # Leave the shared suite database as the module found it: the
    # seeded plans (free is default) must not leak into later
    # modules — with plans present, plan enforcement activates
    # for every principal in the process (Phase 8 gate find).
    db = SessionLocal()
    try:
        # FK-safe wipe (children before parents, from the metadata
        # graph): PostgreSQL enforces FKs that SQLite test runs
        # historically tolerated violating (E-07 readiness).
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
        db.commit()
    finally:
        db.close()


def _user(client, plan=None):
    email = f"u-{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    user = response.json()
    if plan is not None:
        _assign_plan(user["user"]["id"], plan)
    return user


def _assign_plan(user_id, plan_name):
    db = SessionLocal()
    try:
        plan = db.query(Plan).filter_by(name=plan_name).one()
        db.query(UserSubscription).filter_by(
            user_id=user_id, status="active"
        ).update({"status": "cancelled"})
        db.add(
            UserSubscription(
                id=str(uuid.uuid4()),
                user_id=user_id,
                plan_id=plan.id,
                status="active",
                current_period_start=datetime.now(timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()


def _bearer(user):
    return {"Authorization": f"Bearer {user['session_token']}"}


def _project(client, user, script="A short script. Two sentences."):
    response = client.post(
        "/api/v1/projects", headers=_bearer(user), json={"script": script}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _run_count(owner_id):
    db = SessionLocal()
    try:
        return (
            db.query(ProductionRun)
            .filter(ProductionRun.owner_id == owner_id)
            .count()
        )
    finally:
        db.close()


def test_concurrency_cap_rejects_and_creates_nothing(client):
    user = _user(client)  # free plan: 1 concurrent
    project = _project(client, user)
    active = run_ledger.begin_run(project["id"], project["script"])
    assert active

    other = _project(client, user, script="Another script entirely. Yes.")
    before = _run_count(user["user"]["id"])
    response = client.post(
        f"/api/v1/projects/{other['id']}/runs",
        headers=_bearer(user),
        json={},
    )
    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "plan_limit_concurrency"
    assert _run_count(user["user"]["id"]) == before  # nothing created
    run_ledger.finish_run(active, "failed")


def test_dedupe_submission_skips_admission(client):
    user = _user(client)
    project = _project(client, user)
    first = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        headers=_bearer(user),
        json={},
    )
    assert first.status_code == 202, first.text
    # The same script again while active: deduplicated, NOT a 429,
    # even though the user is at the concurrency cap.
    second = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        headers=_bearer(user),
        json={},
    )
    assert second.status_code == 202, second.text
    assert second.json()["deduplicated"] is True


def test_monthly_quota_and_period_rollover(client):
    user = _user(client)  # free plan: 3 runs/month
    project = _project(client, user)
    for i in range(3):
        run_id = run_ledger.begin_run(project["id"], project["script"])
        run_ledger.finish_run(run_id, "completed")
    before = _run_count(user["user"]["id"])
    response = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        headers=_bearer(user),
        json={},
    )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "plan_limit_quota"
    assert _run_count(user["user"]["id"]) == before

    # Moving the subscription's period start past the old runs
    # empties the period (the rollover mechanism) — submissions flow.
    db = SessionLocal()
    try:
        sub = (
            db.query(UserSubscription)
            .filter_by(user_id=user["user"]["id"], status="active")
            .one()
        )
        sub.current_period_start = datetime.now(timezone.utc) + timedelta(days=1)
        db.commit()
    finally:
        db.close()
    allowed = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        headers=_bearer(user),
        json={},
    )
    assert allowed.status_code == 202, allowed.text


def test_script_and_upload_and_storage_caps(client):
    user = _user(client, plan="tiny")  # script 10 chars, upload 4 B, storage 1 B

    script = client.post(
        "/api/v1/projects",
        headers=_bearer(user),
        json={"script": "This script is far longer than ten characters."},
    )
    assert script.status_code == 422
    assert script.json()["error"]["code"] == "plan_limit_script_chars"

    ok = client.post(
        "/api/v1/projects",
        headers=_bearer(user),
        json={"script": "Tiny. Ok."},
    )
    assert ok.status_code == 201, ok.text
    project = ok.json()

    upload = client.post(
        f"/api/v1/projects/{project['id']}/uploads",
        headers=_bearer(user),
        files={"file": ("big.bin", b"12345")},
    )
    assert upload.status_code == 413
    assert upload.json()["error"]["code"] == "plan_limit_upload"

    # Storage: one byte of media already meets the 1-byte cap.
    project_dir = media_paths.project_dir(project["id"])
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "blob.bin").write_bytes(b"x")
    submit = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        headers=_bearer(user),
        json={},
    )
    assert submit.status_code == 429
    assert submit.json()["error"]["code"] == "plan_limit_storage"


def test_founder_plan_is_uncapped_control(client):
    user = _user(client, plan="founder")
    project = _project(client, user)
    for _ in range(3):
        run_id = run_ledger.begin_run(project["id"], project["script"])
        assert run_id  # three concurrent, no cap at 1 or quota at 3
    # A genuinely NEW production (different project + script) goes
    # through admission — and passes, where the free plan 429s.
    other = _project(client, user, script="Founder second film. Go.")
    before = _run_count(user["user"]["id"])
    response = client.post(
        f"/api/v1/projects/{other['id']}/runs",
        headers=_bearer(user),
        json={},
    )
    assert response.status_code == 202, response.text
    assert _run_count(user["user"]["id"]) == before + 1


def test_admin_assign_plan_endpoint(client):
    user = _user(client)
    as_user = client.post(
        f"/api/v1/admin/users/{user['user']['id']}/plan",
        headers=_bearer(user),
        json={"plan_name": "tiny"},
    )
    assert as_user.status_code == 403
    as_admin = client.post(
        f"/api/v1/admin/users/{user['user']['id']}/plan",
        headers={"X-API-Key": TOKEN},
        json={"plan_name": "tiny"},
    )
    assert as_admin.status_code == 200, as_admin.text
    assert as_admin.json()["plan"] == "tiny"
    missing = client.post(
        f"/api/v1/admin/users/{user['user']['id']}/plan",
        headers={"X-API-Key": TOKEN},
        json={"plan_name": "nonexistent"},
    )
    assert missing.status_code == 404


def test_usage_summary_matches_ledger(client):
    user = _user(client)
    project = _project(client, user)
    run_id = run_ledger.begin_run(project["id"], project["script"])
    run_ledger.finish_run(
        run_id,
        "completed",
        execution={
            "serper_calls": {"total": 7, "web": 5, "images": 2, "video": 0},
            "llm_calls": {"total": 3, "total_tokens": 1234},
        },
    )
    usage = client.get("/api/v1/account/usage", headers=_bearer(user))
    assert usage.status_code == 200, usage.text
    body = usage.json()
    assert body["plan"]["name"] == "free"
    assert body["runs_in_period"] == 1
    assert body["concurrent_runs"] == 0
    assert body["provider"]["serper_calls"] == 7
    assert body["provider"]["llm_calls"] == 3
    assert body["provider"]["llm_tokens"] == 1234
    assert body["provider"]["llm_counted_runs"] == 1
