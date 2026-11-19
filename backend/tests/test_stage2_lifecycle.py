"""Stage 2B — job lifecycle on the run ledger.

Dedup (double submission = one logical run), atomic claiming with
exactly one winner, FIFO claim order, cooperative cancellation at a
stage boundary, and stale-run reconciliation (AUD-07/08/10).
"""

import threading
import uuid
from types import SimpleNamespace

import pytest

import jobs
from database import SessionLocal, engine
from models import AssetDecision, Base, ProductionRun, Project
from services import run_ledger


@pytest.fixture(autouse=True)
def _ledger_tables():
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Project.__table__,
            ProductionRun.__table__,
            AssetDecision.__table__,
        ],
    )
    yield


@pytest.fixture()
def project_id():
    pid = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(
            Project(
                id=pid,
                script="Lifecycle test script.",
                category="Documentary",
                status="created",
            )
        )
        db.commit()
    finally:
        db.close()
    yield pid
    db = SessionLocal()
    try:
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


def _unique_script(tag):
    # Unique per test so dedup keys never collide across tests sharing
    # the suite database.
    return f"Script {tag} {uuid.uuid4()}. One sentence."


def test_double_submission_is_one_logical_run(project_id):
    script = _unique_script("dedup")
    first = run_ledger.submit_production(project_id, script)
    second = run_ledger.submit_production(project_id, script)
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert second["run_id"] == first["run_id"]

    # A different script is a different run.
    other = run_ledger.submit_production(project_id, _unique_script("dedup2"))
    assert other["run_id"] != first["run_id"]

    # Once the run is finished, resubmission is a NEW run (retry).
    run_ledger.finish_run(first["run_id"], "failed", error="boom")
    retry = run_ledger.submit_production(project_id, script)
    assert retry["deduplicated"] is False
    assert retry["run_id"] != first["run_id"]


def test_enqueue_dedupes_and_carries_run_id(project_id, monkeypatch):
    calls = []

    class FakeQueue:
        def enqueue(self, fn, *args, **kwargs):
            calls.append((fn, args, kwargs))
            return SimpleNamespace(id=f"job-{len(calls)}")

    monkeypatch.setattr(jobs, "video_queue", FakeQueue())
    script = _unique_script("enqueue")

    first = jobs.enqueue_full_pipeline(project_id, script)
    assert first == "job-1"
    assert len(calls) == 1
    run_id = calls[0][2]["run_id"]
    assert run_id

    # Same project + same script while queued: no second job, the
    # caller gets the existing run id back.
    second = jobs.enqueue_full_pipeline(project_id, script)
    assert second == run_id
    assert len(calls) == 1


def test_concurrent_claims_have_exactly_one_winner(project_id):
    submission = run_ledger.submit_production(
        project_id, _unique_script("claim")
    )
    run_id = submission["run_id"]

    barrier = threading.Barrier(2)
    results = []

    def claimant():
        barrier.wait(timeout=10)
        results.append(run_ledger.claim_run(run_id))

    threads = [threading.Thread(target=claimant) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    winners = [r for r in results if r is not None]
    assert len(results) == 2
    assert len(winners) == 1
    assert winners[0]["run_id"] == run_id
    assert run_ledger.get_run(run_id)["status"] == "running"


def test_claim_next_run_is_fifo(project_id):
    # Isolate the queue: cancel any queued leftovers from other tests
    # sharing the suite database.
    db = SessionLocal()
    try:
        db.query(ProductionRun).filter(
            ProductionRun.status == "queued"
        ).update({"status": "cancelled"})
        db.commit()
    finally:
        db.close()

    submissions = [
        run_ledger.submit_production(project_id, _unique_script(f"fifo{i}"))
        for i in range(3)
    ]
    claimed_order = []
    for _ in range(3):
        claimed = run_ledger.claim_next_run()
        assert claimed is not None
        claimed_order.append(claimed["run_id"])
    assert claimed_order == [s["run_id"] for s in submissions]
    assert run_ledger.claim_next_run() is None


def test_cancel_queued_run_is_immediate(project_id):
    submission = run_ledger.submit_production(
        project_id, _unique_script("cancel-queued")
    )
    assert run_ledger.request_cancel(submission["run_id"]) is True
    run = run_ledger.get_run(submission["run_id"])
    assert run["status"] == "cancelled"
    assert run_ledger.claim_run(submission["run_id"]) is None


def test_cancel_running_run_stops_at_stage_boundary(project_id, tmp_path):
    run_id = run_ledger.begin_run(project_id, _unique_script("cancel-run"))
    assert run_ledger.request_cancel(run_id) is True
    # Flag set, but a running run is not force-killed...
    assert run_ledger.get_run(run_id)["status"] == "running"
    assert run_ledger.is_cancel_requested(run_id) is True

    # ...it stops cooperatively at the next stage boundary.
    state = {"_run_id": run_id}
    with pytest.raises(run_ledger.RunCancelled):
        jobs.run_stage(state, "voice_actor", tmp_path)

    # And fail_pipeline records the terminal status as "cancelled",
    # not "failed".
    with pytest.raises(run_ledger.RunCancelled):
        jobs.fail_pipeline(
            state,
            "voice_actor",
            run_ledger.RunCancelled("cancelled"),
            tmp_path,
        )
    run = run_ledger.get_run(run_id)
    assert run["status"] == "cancelled"
    assert run["error"] == "cancelled"
    assert run["finished_at"] is not None


def test_stale_runs_are_reconciled_as_interrupted(project_id):
    run_id = run_ledger.begin_run(project_id, _unique_script("stale"))
    assert run_ledger.get_run(run_id)["status"] == "running"

    reconciled = run_ledger.reconcile_stale_runs()
    assert reconciled >= 1

    run = run_ledger.get_run(run_id)
    assert run["status"] == "failed"
    assert run["error"] == "interrupted"
    assert run["finished_at"] is not None
