"""Phase 8.4 — concurrent production infrastructure.

Covers the design's unit-level claims (03 §3–§6):
- run_metrics state is run-scoped: two runs sharing a process
  keep separate counters, and snapshots read per run id.
- provider_health backoff is SHARED through Redis (one worker's
  429 storm backs the scope off for every worker), time-bounded,
  and cleared by any worker's success.
- the platform token bucket enforces its rate across callers.
- the dispatcher promotes least-served-first under the platform
  cap and respects per-owner plan caps + the priority reserve.
- failure classification + the retry sweep: transient failures
  get a stage-resume retry child; deterministic ones never do.
- render slots serialise final encodes.
- the in-pipeline cancellation check raises RunCancelled only
  when the ledger flag is set.
"""

import uuid
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

import jobs
from database import SessionLocal
from models import (
    Base,
    Plan,
    ProductionRun,
    Project,
    User,
    UserSubscription,
)
from services import (
    dispatcher,
    provider_health,
    rate_bucket,
    render_slots,
    run_ledger,
    run_metrics,
)


@pytest.fixture(autouse=True)
def _tables():
    Base.metadata.create_all(bind=SessionLocal().get_bind())
    yield
    db = SessionLocal()
    try:
        db.query(ProductionRun).delete()
        db.query(UserSubscription).delete()
        db.query(Project).delete()
        db.query(User).delete()
        db.query(Plan).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def fake_redis():
    client = fakeredis.FakeRedis(decode_responses=True)
    provider_health.set_redis_client(client)
    rate_bucket.set_redis_client(client)
    render_slots.set_redis_client(client)
    dispatcher.set_redis_client(client)
    yield client
    provider_health.reset_redis_client()
    rate_bucket.reset_redis_client()
    render_slots.reset_redis_client()
    dispatcher.reset_redis_client()
    provider_health.reset()


def _user(email, role="user"):
    db = SessionLocal()
    try:
        user = User(
            id=str(uuid.uuid4()),
            email=email,
            password_hash="x",
            role=role,
            status="active",
        )
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


def _project(owner_id, script="One. Two."):
    db = SessionLocal()
    try:
        project = Project(
            id=str(uuid.uuid4()),
            title="P",
            script=script,
            owner_id=owner_id,
        )
        db.add(project)
        db.commit()
        return project.id
    finally:
        db.close()


def _queued_run(owner_id, project_id, minutes_ago=0, **options):
    db = SessionLocal()
    try:
        run = ProductionRun(
            id=str(uuid.uuid4()),
            project_id=project_id,
            owner_id=owner_id,
            script_hash=run_ledger.script_sha256("One. Two."),
            status="queued",
            options=options or {},
            created_at=datetime.now(timezone.utc)
            - timedelta(minutes=minutes_ago),
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _plan(name, max_concurrent, priority_tier=0):
    db = SessionLocal()
    try:
        plan = Plan(
            id=str(uuid.uuid4()),
            name=name,
            max_concurrent_runs=max_concurrent,
            max_runs_per_period=100,
            period="month",
            max_script_chars=100000,
            max_storage_bytes=10**10,
            max_upload_bytes=10**8,
            priority_tier=priority_tier,
            is_default=False,
        )
        db.add(plan)
        db.commit()
        return plan.id
    finally:
        db.close()


def _subscribe(user_id, plan_id):
    db = SessionLocal()
    try:
        db.add(
            UserSubscription(
                id=str(uuid.uuid4()),
                user_id=user_id,
                plan_id=plan_id,
                status="active",
                current_period_start=datetime.now(timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------
# run_metrics: run-scoped state
# ---------------------------------------------------------------------


def test_run_metrics_two_runs_do_not_corrupt_each_other():
    run_metrics.begin_run("run-a")
    run_metrics.serper_call("search")
    run_metrics.serper_call("images")
    run_metrics.llm_call(100)
    run_metrics.begin_run("run-b")
    run_metrics.serper_call("search")
    run_metrics.llm_call(5)

    snap_a = run_metrics.snapshot("run-a")
    snap_b = run_metrics.snapshot("run-b")
    assert snap_a["serper_calls"]["total"] == 2
    assert snap_a["llm_calls"] == {"total": 1, "total_tokens": 100}
    assert snap_b["serper_calls"]["total"] == 1
    assert snap_b["llm_calls"] == {"total": 1, "total_tokens": 5}
    # The bound snapshot is the most recent run's.
    assert run_metrics.snapshot()["serper_calls"]["total"] == 1


def test_run_metrics_rebind_moves_the_provisional_state():
    key = run_metrics.begin_run()
    run_metrics.serper_call("search")
    run_metrics.rebind_run("real-run-id")
    assert run_metrics.snapshot("real-run-id")["serper_calls"]["total"] == 1
    run_metrics.serper_call("search")
    assert run_metrics.snapshot("real-run-id")["serper_calls"]["total"] == 2
    assert key != "real-run-id"


# ---------------------------------------------------------------------
# provider_health: shared, time-bounded backoff
# ---------------------------------------------------------------------


def test_provider_backoff_is_shared_and_time_bounded(fake_redis):
    url = "https://upload.wikimedia.org/x.jpg"
    scope = provider_health.scope_for_url(url)
    assert not provider_health.is_backed_off(scope)
    # Gate 2 (2026-11-26): Wikimedia scopes trip at their own
    # retry-before-backoff count, not the generic one.
    trip_count = provider_health.trip_count_for_scope(scope)
    for _ in range(trip_count):
        provider_health.record_rate_limit(scope)
    # The backoff now lives in Redis: any other worker reading the
    # same store is backed off WITHOUT making a request.
    assert provider_health.is_backed_off(scope)
    assert fake_redis.get(f"provider:{scope}:backed_off_until")

    # Time-bounded: once the stamp is in the past, the scope is
    # usable again (no production-length poisoning).
    fake_redis.set(f"provider:{scope}:backed_off_until", "1")
    assert not provider_health.is_backed_off(scope)

    # A success from any worker clears the shared state.
    for _ in range(trip_count):
        provider_health.record_rate_limit(scope)
    assert provider_health.is_backed_off(scope)
    provider_health.record_download_outcome(scope, True)
    assert not provider_health.is_backed_off(scope)


# ---------------------------------------------------------------------
# rate_bucket
# ---------------------------------------------------------------------


def test_rate_bucket_enforces_the_platform_rate(fake_redis, monkeypatch):
    monkeypatch.setenv("CINEMORA_SERPER_RPS", "3")
    assert rate_bucket.acquire("serper", timeout=0) is True
    assert rate_bucket.acquire("serper", timeout=0) is True
    assert rate_bucket.acquire("serper", timeout=0) is True
    assert rate_bucket.acquire("serper", timeout=0) is False


# ---------------------------------------------------------------------
# dispatcher: fairness, caps, reserve
# ---------------------------------------------------------------------


@pytest.fixture()
def _promotions(monkeypatch):
    promoted = []

    def fake_enqueue(run, project, is_priority):
        promoted.append((run.id, is_priority))
        return f"job-{run.id[:8]}"

    monkeypatch.setattr(jobs, "enqueue_promoted_run", fake_enqueue)
    return promoted


def test_dispatcher_least_served_first_under_platform_cap(
    fake_redis, _promotions, monkeypatch
):
    monkeypatch.setenv("CINEMORA_PLATFORM_MAX_RUNNING", "2")
    owner_a = _user("a@example.com")
    owner_b = _user("b@example.com")
    project_a = _project(owner_a)
    project_b = _project(owner_b)
    a1 = _queued_run(owner_a, project_a, minutes_ago=30)
    _queued_run(owner_a, project_a, minutes_ago=20)
    _queued_run(owner_a, project_a, minutes_ago=10)
    b1 = _queued_run(owner_b, project_b, minutes_ago=5)

    promoted = dispatcher.dispatch_once()
    promoted_ids = [run_id for run_id, _job in promoted]
    # Cap 2: A's oldest first (oldest backlog), then B — B is
    # least-served (0 running) even though A queued earlier.
    assert promoted_ids == [a1, b1]
    assert len(_promotions) == 2


def test_dispatcher_respects_the_per_owner_plan_cap(
    fake_redis, _promotions, monkeypatch
):
    monkeypatch.setenv("CINEMORA_PLATFORM_MAX_RUNNING", "8")
    owner = _user("capped@example.com")
    plan_id = _plan("cap-one", max_concurrent=1)
    _subscribe(owner, plan_id)
    project_id = _project(owner)
    _queued_run(owner, project_id, minutes_ago=20)
    _queued_run(owner, project_id, minutes_ago=10)

    promoted = dispatcher.dispatch_once()
    assert len(promoted) == 1


def test_dispatcher_priority_reserve_protects_standard_runs(
    fake_redis, _promotions, monkeypatch
):
    monkeypatch.setenv("CINEMORA_PLATFORM_MAX_RUNNING", "2")
    vip = _user("vip@example.com")
    plan_id = _plan("vip-plan", max_concurrent=10, priority_tier=1)
    _subscribe(vip, plan_id)
    standard = _user("std@example.com")
    vip_project = _project(vip)
    std_project = _project(standard)
    _queued_run(vip, vip_project, minutes_ago=30)
    _queued_run(vip, vip_project, minutes_ago=20)
    std_run = _queued_run(standard, std_project, minutes_ago=10)

    promoted = dispatcher.dispatch_once()
    promoted_ids = [run_id for run_id, _job in promoted]
    # Cap 2 with a standard run waiting: priority may use at most
    # cap-1 = 1 slot, so the standard run takes the second slot.
    assert std_run in promoted_ids
    assert len(promoted_ids) == 2
    priority_promotions = [p for _r, p in _promotions if p]
    assert len(priority_promotions) == 1


# ---------------------------------------------------------------------
# retries
# ---------------------------------------------------------------------


def test_failure_classification_is_conservative():
    assert dispatcher.classify_failure({"error": "interrupted"}) == "transient"
    assert (
        dispatcher.classify_failure({"error": "coverage gate: 20/23"})
        == "deterministic"
    )
    assert dispatcher.classify_failure({"error": "mystery"}) is None
    assert dispatcher.classify_failure({"error": None}) is None


def test_retry_sweep_retries_transient_only(fake_redis):
    # Item 8 (2026-11-26): ledger-mechanics fixtures run as a
    # staff owner — an ordinary zero-credit tenant is now
    # billing-rejected at submission by design (see
    # tests/test_pi_item8_zero_credit.py).
    owner = _user("retry@example.com", role="super_admin")
    project_id = _project(owner, script="Alpha. Beta.")
    db = SessionLocal()
    try:
        for tag, error in (
            ("transient", "interrupted"),
            ("deterministic", "coverage gate failed"),
        ):
            db.add(
                ProductionRun(
                    id=str(uuid.uuid4()),
                    project_id=project_id,
                    owner_id=owner,
                    script_hash=run_ledger.script_sha256("Alpha. Beta."),
                    status="failed",
                    error=error,
                    options={},
                    created_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                )
            )
        db.commit()
    finally:
        db.close()

    created = dispatcher.retry_sweep()
    assert len(created) == 1
    child = run_ledger.get_run(created[0])
    assert child["status"] == "queued"
    assert child["options"]["retry_attempt"] == 1
    assert child["options"]["retry_of"]
    # A second sweep mints nothing new (the child exists).
    assert dispatcher.retry_sweep() == []


# ---------------------------------------------------------------------
# render slots
# ---------------------------------------------------------------------


def test_render_slots_serialise_encodes(fake_redis, monkeypatch):
    monkeypatch.setenv("CINEMORA_RENDER_CONCURRENCY", "1")
    with render_slots.render_slot(timeout=5) as first:
        assert first == 0
        with render_slots.render_slot(timeout=0.5) as second:
            # No slot within the timeout: proceeds unbounded
            # (None) rather than failing the render.
            assert second is None
    with render_slots.render_slot(timeout=5) as third:
        assert third == 0


# ---------------------------------------------------------------------
# in-pipeline cancellation
# ---------------------------------------------------------------------


def test_claim_run_rides_out_lock_bursts(monkeypatch):
    """Phase 8.6 battery find: under concurrent production the
    SQLite ledger's lock bursts outlasted the claim's 3x50ms
    retry, and a retry child executed unclaimed (no heartbeat,
    invisible to reconciliation). The claim must back off through
    a burst and still win."""
    from sqlalchemy.exc import OperationalError

    # Item 8 (2026-11-26): ledger-mechanics fixtures run as a
    # staff owner — an ordinary zero-credit tenant is now
    # billing-rejected at submission by design (see
    # tests/test_pi_item8_zero_credit.py).
    owner = _user("claimburst@example.com", role="super_admin")
    project_id = _project(owner)
    run = run_ledger.submit_production(project_id, "One. Two.")

    real_claim_row = run_ledger._claim_row
    calls = {"n": 0}

    def flaky_claim_row(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OperationalError("UPDATE", {}, "database is locked")
        return real_claim_row(*args, **kwargs)

    monkeypatch.setattr(run_ledger, "_claim_row", flaky_claim_row)
    claimed = run_ledger.claim_run(run["run_id"])
    assert claimed is not None
    assert claimed["status"] == "running"
    assert calls["n"] == 3


def test_pipeline_cancellation_check(monkeypatch):
    from services import media_pipeline, run_context

    # Item 8 (2026-11-26): ledger-mechanics fixtures run as a
    # staff owner — an ordinary zero-credit tenant is now
    # billing-rejected at submission by design (see
    # tests/test_pi_item8_zero_credit.py).
    owner = _user("cancel@example.com", role="super_admin")
    project_id = _project(owner)
    run = run_ledger.submit_production(project_id, "One. Two.")
    run_ledger.claim_run(run["run_id"])

    class _Ctx:
        run_id = run["run_id"]

    monkeypatch.setattr(run_context, "current_context", lambda: _Ctx())
    # Not requested: no raise.
    media_pipeline._raise_if_cancel_requested()
    run_ledger.request_cancel(run["run_id"])
    with pytest.raises(run_ledger.RunCancelled):
        media_pipeline._raise_if_cancel_requested()
    # No context at all: no raise.
    monkeypatch.setattr(run_context, "current_context", lambda: None)
    media_pipeline._raise_if_cancel_requested()
