"""Phase 1 — PostgreSQL multi-worker validation of the claim /
lease / heartbeat / resume paths.

Phase 0 found every production record to date was produced on
SQLite and PostgreSQL concurrency was UNMEASURED — the one
concurrency failure on record (Hyatt run 3's lost claim) was a
SQLite "database is locked" burst. These tests run the ledger's
concurrency-critical paths against a REAL PostgreSQL server:
atomic claims under thread contention, ownership-enforced
heartbeats, first-finisher-wins finishes, and the Phase 1
stale -> requeue -> resume cycle racing itself.

They run only when CINEMORA_TEST_POSTGRES_URL points at a
throwaway database (the suite's SQLite default must never be
repointed); the module skips — loudly — otherwise. The database
is expected to be disposable: the fixture creates the full schema
and drops the rows it created.

Validated 2026-11-26 against PostgreSQL 16.4 (results recorded
in the Phase 1 implementation report).
"""

import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

PG_URL = os.environ.get("CINEMORA_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason=(
        "CINEMORA_TEST_POSTGRES_URL not set — PostgreSQL "
        "multi-worker validation needs a real server"
    ),
)


@pytest.fixture()
def pg_ledger(monkeypatch):
    """Rebind the ledger's session factory to a real PostgreSQL
    engine for the test's duration."""
    import database
    import models  # noqa: F401  (registers every table)
    from sqlalchemy.orm import sessionmaker

    engine = database.create_app_engine(PG_URL)
    database.Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)

    from models import Project
    from services import run_ledger

    pid = str(uuid.uuid4())
    db = factory()
    try:
        db.add(
            Project(
                id=pid,
                script="Phase 1 Postgres test script.",
                category="Documentary",
                status="created",
            )
        )
        db.commit()
    finally:
        db.close()
    yield run_ledger, pid, factory
    db = factory()
    try:
        from models import AssetDecision, ProductionRun

        db.query(AssetDecision).filter(
            AssetDecision.project_id == pid
        ).delete()
        db.query(ProductionRun).filter(
            ProductionRun.project_id == pid
        ).delete()
        db.query(Project).filter(Project.id == pid).delete()
        db.commit()
    finally:
        db.close()
        engine.dispose()


def _script(tag):
    return f"PG phase1 {tag} {uuid.uuid4().hex}. One sentence."


def _set_heartbeat(factory, run_ledger, run_id, when):
    from models import ProductionRun

    db = factory()
    try:
        row = db.get(ProductionRun, str(run_id))
        progress = dict(row.progress or {})
        progress["heartbeat_at"] = when.isoformat()
        row.progress = progress
        db.commit()
    finally:
        db.close()


def test_pg_concurrent_claims_single_winner(pg_ledger):
    run_ledger, pid, _factory = pg_ledger
    run_id = run_ledger.submit_production(pid, _script("race"))["run_id"]
    results = []
    barrier = threading.Barrier(8)

    def _claim(index):
        barrier.wait(timeout=30)
        results.append(
            run_ledger.claim_run(run_id, worker_id=f"pg-racer-{index}")
        )

    threads = [threading.Thread(target=_claim, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    winners = [r for r in results if r is not None]
    assert len(results) == 8
    assert len(winners) == 1
    run = run_ledger.get_run(run_id)
    assert run["status"] == "running"
    assert run["progress"]["worker_id"].startswith("pg-racer-")


def test_pg_heartbeat_ownership_enforced(pg_ledger):
    run_ledger, pid, _factory = pg_ledger
    run_id = run_ledger.submit_production(pid, _script("owner"))["run_id"]
    assert run_ledger.claim_run(run_id, worker_id="pg-owner") is not None
    # The owner beats; a foreign worker's beat is refused and a
    # foreign claim on the running run fails.
    assert run_ledger.touch_run(run_id, worker_id="pg-owner") is True
    assert run_ledger.touch_run(run_id, worker_id="pg-intruder") is False
    assert run_ledger.claim_run(run_id, worker_id="pg-intruder") is None
    assert (
        run_ledger.get_run(run_id)["progress"]["worker_id"] == "pg-owner"
    )


def test_pg_finish_first_finisher_wins(pg_ledger):
    run_ledger, pid, _factory = pg_ledger
    run_id = run_ledger.submit_production(pid, _script("finish"))["run_id"]
    assert run_ledger.claim_run(run_id, worker_id="pg-owner") is not None
    outcomes = {}
    barrier = threading.Barrier(2)

    def _finish(status):
        barrier.wait(timeout=30)
        outcomes[status] = run_ledger.finish_run(run_id, status)

    threads = [
        threading.Thread(target=_finish, args=(status,))
        for status in ("completed", "failed")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(outcomes.values()) == [False, True]
    final = run_ledger.get_run(run_id)["status"]
    assert outcomes[final] is True


def test_pg_stale_resume_cycle(pg_ledger):
    run_ledger, pid, factory = pg_ledger
    run_id = run_ledger.submit_production(pid, _script("resume"))["run_id"]
    assert run_ledger.claim_run(run_id, worker_id="pg-dead") is not None
    _set_heartbeat(
        factory, run_ledger, run_id,
        datetime.now(timezone.utc) - timedelta(seconds=3600),
    )
    assert run_ledger.reconcile_stale_runs(stale_after_seconds=180) >= 1
    run = run_ledger.get_run(run_id)
    assert run["status"] == "queued"
    assert run["options"]["resume_count"] == 1
    # A different worker picks the SAME run up and finishes it.
    assert run_ledger.claim_run(run_id, worker_id="pg-next") is not None
    assert run_ledger.finish_run(run_id, "completed") is True
    assert run_ledger.get_run(run_id)["status"] == "completed"


def test_pg_concurrent_reconcile_requeues_once(pg_ledger):
    run_ledger, pid, factory = pg_ledger
    run_id = run_ledger.submit_production(pid, _script("sweep"))["run_id"]
    assert run_ledger.claim_run(run_id, worker_id="pg-dead") is not None
    _set_heartbeat(
        factory, run_ledger, run_id,
        datetime.now(timezone.utc) - timedelta(seconds=3600),
    )
    counts = []
    barrier = threading.Barrier(2)

    def _sweep():
        barrier.wait(timeout=30)
        counts.append(
            run_ledger.reconcile_stale_runs(stale_after_seconds=180)
        )

    threads = [threading.Thread(target=_sweep) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    # Two reconcilers racing the same orphan: the conditional
    # UPDATE's rowcount guard means the run is transitioned exactly
    # once in total (each sweep also sees other tests' orphans in
    # a shared database, so the pin is on THIS run's end state).
    run = run_ledger.get_run(run_id)
    assert run["status"] == "queued"
    assert run["options"]["resume_count"] == 1
