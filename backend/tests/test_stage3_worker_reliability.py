"""Stage 3 (R2) — worker reliability: presence, ownership,
singleton, stale-job safety, submission mutex, TEST-002.

Phase 8.4 addendum: the singleton-era tests in this file were
REPLACED (per the migration plan, doc 04 §3) by tests asserting
the multi-worker properties — no singleton gate remains, worker
startup reconciliation is heartbeat-aware and spares a foreign
worker's live run, the worker listens on both lanes
priority-first, two real workers boot side by side, and a
Redis-down submission waits in the ledger backlog instead of
failing.

What this stage proved broken at HEAD 768a554 and fixed:

- PIPE-005: a run carried NO liveness signal — no heartbeat, no
  owner — so a dead worker's run sat in "running" until a restart,
  and startup reconciliation (the only reconciler) failed EVERY
  running run, including a live second worker's (DATA-005/FP-16).
- DATA-005: nothing stopped a second worker consuming the queue.
- INT-103 (ledger half): submit_production's dedup ignored runs
  parked in awaiting_approval, and the dedup check-then-insert
  raced across processes.
- TEST-002: the queue/worker/Redis system had NO integration
  tests — queues were stubbed everywhere. The TEST-002 section
  below runs a REAL SimpleWorker against a REAL Redis (the
  conftest REDIS_URL, DB 15) and boots the real worker.py against
  a migrated database. Those tests skip — loudly, with the reason
  — when no Redis server is reachable, so the suite stays green
  in Redis-less environments; they are not mocked substitutes.

The unit sections use the suite's shared SQLite test database,
following the test_stage2_ledger fixture pattern.
"""

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import jobs  # noqa: F401  (ensures backend modules import in suite order)
import worker
from database import SessionLocal, engine
from models import AssetDecision, Base, ProductionRun, Project
from services import run_ledger


BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")


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
                script="Stage 3 test script.",
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
    return f"Stage 3 {tag} script {uuid.uuid4().hex}. One sentence."


def _set_heartbeat(run_id, when):
    """Directly rewrite a run's recorded heartbeat (test control
    over liveness evidence — simulates a worker that stopped
    beating `when`)."""
    db = SessionLocal()
    try:
        row = db.get(ProductionRun, str(run_id))
        progress = dict(row.progress or {})
        progress["heartbeat_at"] = when.isoformat()
        row.progress = progress
        db.commit()
    finally:
        db.close()


def _run_count(project_id):
    db = SessionLocal()
    try:
        return (
            db.query(ProductionRun)
            .filter(ProductionRun.project_id == str(project_id))
            .count()
        )
    finally:
        db.close()


# ---------------------------------------------------------------------
# Presence: identity, ownership, heartbeat
# ---------------------------------------------------------------------


def test_worker_identity_is_stable_within_a_process():
    first = run_ledger.current_worker_id()
    assert first == run_ledger.current_worker_id()
    host, pid, suffix = first.rsplit(":", 2)
    assert host and int(pid) == os.getpid() and len(suffix) == 8


def test_claim_records_ownership_and_heartbeat(project_id):
    submission = run_ledger.submit_production(
        project_id, _unique_script("claim")
    )
    run_id = submission["run_id"]
    # A queued run has no owner yet: ownership begins at the claim.
    assert run_ledger.get_run(run_id)["progress"] is None

    claimed = run_ledger.claim_run(run_id)
    assert claimed is not None and claimed["status"] == "running"

    run = run_ledger.get_run(run_id)
    progress = run["progress"]
    assert progress["worker_id"] == run_ledger.current_worker_id()
    assert progress["claimed_at"]
    assert progress["heartbeat_at"]

    presence = run_ledger.run_presence(run)
    assert presence["worker_id"] == run_ledger.current_worker_id()
    assert presence["live"] is True
    assert presence["age_seconds"] is not None
    assert presence["age_seconds"] < run_ledger.STALE_AFTER_SECONDS


def test_second_claim_cannot_take_over_a_live_run(project_id):
    submission = run_ledger.submit_production(
        project_id, _unique_script("takeover")
    )
    run_id = submission["run_id"]
    assert run_ledger.claim_run(run_id) is not None
    owner_before = run_ledger.get_run(run_id)["progress"]["worker_id"]

    # Neither this process again nor a different worker identity
    # can claim a run that is already running — and the recorded
    # owner is not overwritten by the attempt.
    assert run_ledger.claim_run(run_id) is None
    assert (
        run_ledger.claim_run(run_id, worker_id="someone-else:1:deadbeef")
        is None
    )
    assert (
        run_ledger.get_run(run_id)["progress"]["worker_id"]
        == owner_before
    )


def test_begin_run_stamps_presence(project_id):
    run_id = run_ledger.begin_run(project_id, _unique_script("begin"))
    run = run_ledger.get_run(run_id)
    assert run["progress"]["worker_id"] == run_ledger.current_worker_id()
    assert run_ledger.run_presence(run)["live"] is True


def test_touch_run_refreshes_only_for_the_owner(project_id):
    run_id = run_ledger.begin_run(project_id, _unique_script("touch"))
    stale = datetime.now(timezone.utc) - timedelta(seconds=600)
    _set_heartbeat(run_id, stale)
    assert run_ledger.run_presence(run_ledger.get_run(run_id))[
        "live"
    ] is False

    # A foreign worker's beat is refused and changes nothing: it
    # can neither keep the run alive nor adopt it.
    assert (
        run_ledger.touch_run(run_id, worker_id="other:9:beefbeef")
        is False
    )
    assert run_ledger.run_presence(run_ledger.get_run(run_id))[
        "live"
    ] is False

    # The owner's beat refreshes liveness.
    assert run_ledger.touch_run(run_id) is True
    assert run_ledger.run_presence(run_ledger.get_run(run_id))[
        "live"
    ] is True


def test_touch_run_stops_at_terminal_status(project_id):
    run_id = run_ledger.begin_run(project_id, _unique_script("term"))
    run_ledger.finish_run(run_id, "completed")
    assert run_ledger.touch_run(run_id) is False


def test_heartbeat_thread_beats_and_stops_at_finish(project_id):
    run_id = run_ledger.begin_run(project_id, _unique_script("beat"))
    _set_heartbeat(
        run_id, datetime.now(timezone.utc) - timedelta(seconds=600)
    )
    thread = run_ledger.start_heartbeat(run_id, interval_seconds=0.05)
    assert thread is not None
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if run_ledger.run_presence(run_ledger.get_run(run_id))[
                "live"
            ]:
                break
            time.sleep(0.05)
        assert run_ledger.run_presence(run_ledger.get_run(run_id))[
            "live"
        ] is True
    finally:
        run_ledger.finish_run(run_id, "completed")
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_run_presence_reports_stale_evidence():
    old = (
        datetime.now(timezone.utc) - timedelta(seconds=3600)
    ).isoformat()
    presence = run_ledger.run_presence(
        {"progress": {"worker_id": "w:1:abcd1234", "heartbeat_at": old}}
    )
    assert presence["worker_id"] == "w:1:abcd1234"
    assert presence["live"] is False
    assert presence["age_seconds"] > 3500

    empty = run_ledger.run_presence({"progress": None})
    assert empty["live"] is False
    assert empty["age_seconds"] is None


# ---------------------------------------------------------------------
# Stale-job safety: heartbeat-aware reconciliation
# ---------------------------------------------------------------------


def test_reconcile_heartbeat_aware_spares_live_runs(project_id):
    live_id = run_ledger.begin_run(project_id, _unique_script("live"))
    reconciled = run_ledger.reconcile_stale_runs(
        stale_after_seconds=180
    )
    # The live run (fresh heartbeat) is never touched. (The count
    # may include stale runs left by other tests in the shared
    # suite database, so it is not asserted exactly.)
    assert reconciled >= 0
    assert run_ledger.get_run(live_id)["status"] == "running"


def test_reconcile_heartbeat_aware_reclaims_stale_runs(project_id):
    # Phase 1 durability semantics: a heartbeat-stale run is
    # REQUEUED in place for same-run resume (same run row, no
    # terminal write, reservation untouched), NOT failed. The
    # terminal "interrupted" path now applies only once the
    # resume budget is exhausted — pinned in
    # test_phase1_durability.py.
    stale_id = run_ledger.begin_run(project_id, _unique_script("stale"))
    _set_heartbeat(
        stale_id, datetime.now(timezone.utc) - timedelta(seconds=3600)
    )
    reconciled = run_ledger.reconcile_stale_runs(
        stale_after_seconds=180
    )
    assert reconciled >= 1
    run = run_ledger.get_run(stale_id)
    assert run["status"] == "queued"
    assert run["error"] is None
    assert run["finished_at"] is None
    assert run["options"]["resume_count"] == 1
    assert run["progress"]["resume_pending"] is True


def test_reconcile_heartbeat_aware_legacy_rows_use_started_at(
    project_id,
):
    # A row predating presence: no progress at all. Its started_at
    # is the fallback liveness evidence — old means orphaned,
    # recent means leave it alone.
    old_id = run_ledger.begin_run(project_id, _unique_script("legacy"))
    db = SessionLocal()
    try:
        row = db.get(ProductionRun, old_id)
        row.progress = None
        row.started_at = datetime.now(timezone.utc) - timedelta(
            seconds=3600
        )
        db.commit()
    finally:
        db.close()
    run_ledger.reconcile_stale_runs(stale_after_seconds=180)
    # Phase 1: reclaimed means requeued for same-run resume.
    assert run_ledger.get_run(old_id)["status"] == "queued"

    fresh_id = run_ledger.begin_run(
        project_id, _unique_script("legacy-fresh")
    )
    db = SessionLocal()
    try:
        row = db.get(ProductionRun, fresh_id)
        row.progress = None
        db.commit()
    finally:
        db.close()
    run_ledger.reconcile_stale_runs(stale_after_seconds=180)
    assert run_ledger.get_run(fresh_id)["status"] == "running"


def test_reconcile_total_mode_semantics_unchanged(project_id):
    # The no-argument form keeps the AUD-08 contract exactly (the
    # worker-startup mode, now gated by the singleton): every
    # running run is reconciled.
    run_id = run_ledger.begin_run(project_id, _unique_script("total"))
    reconciled = run_ledger.reconcile_stale_runs()
    assert reconciled >= 1
    assert run_ledger.get_run(run_id)["status"] == "failed"


# ---------------------------------------------------------------------
# Submission mutex + dedup completeness
# ---------------------------------------------------------------------


def test_submit_dedup_includes_awaiting_approval(project_id):
    script = _unique_script("parked")
    submission = run_ledger.submit_production(project_id, script)
    run_id = submission["run_id"]
    assert run_ledger.set_run_status(run_id, "awaiting_approval")

    # INT-103: re-submitting the same script while its run is
    # parked at the plan gate must deduplicate onto the parked
    # run, not mint a second production for the project.
    again = run_ledger.submit_production(project_id, script)
    assert again["run_id"] == run_id
    assert again["deduplicated"] is True
    assert _run_count(project_id) == 1


# ---------------------------------------------------------------------
# Worker configuration (PIPE-004)
# ---------------------------------------------------------------------


def test_worker_listens_on_both_lanes_priority_first():
    # Phase 8.4 two-lane contract (design 03 §3.3): the worker
    # drains the priority lane before the standard lane, and the
    # names are exactly the ones jobs.py produces to.
    assert [queue.name for queue in worker.queues] == [
        "video_generation_priority",
        "video_generation",
    ]
    assert jobs.QUEUE_NAME == "video_generation"
    assert jobs.PRIORITY_QUEUE_NAME == "video_generation_priority"


# ---------------------------------------------------------------------
# TEST-002 — real Redis / real worker integration
# ---------------------------------------------------------------------


@pytest.fixture()
def redis15():
    """A REAL Redis connection (conftest REDIS_URL, DB 15).

    Skips — never fakes — when no server is reachable: TEST-002
    exists precisely because stubbed queues proved nothing about
    the real system."""
    from redis import Redis

    connection = Redis.from_url(REDIS_URL)
    try:
        connection.ping()
    except Exception as error:
        pytest.skip(
            f"TEST-002 requires a real Redis at {REDIS_URL}: {error}"
        )
    yield connection
    connection.close()


def _record_fifo_marker(label):
    """RQ job body for the FIFO proof: appends its label to a
    Redis list, so execution order is directly observable."""
    from redis import Redis

    Redis.from_url(REDIS_URL).rpush("stage3:fifo:order", label)


def test_no_singleton_gate_remains():
    """Phase 8.4 (design 03 §7.1): the worker singleton is DELETED,
    not bypassed — the multi-worker design's safety comes from
    atomic claims, ownership-checked heartbeats and
    heartbeat-aware reconciliation, so nothing in the worker
    module may refuse a second worker."""
    assert not hasattr(worker, "acquire_worker_singleton")
    assert not hasattr(worker, "SINGLETON_KEY")


def test_startup_reconciliation_spares_a_live_foreign_run(project_id):
    """The exact call worker.py's startup makes (heartbeat-aware
    reconciliation) must leave another worker's live run strictly
    alone — and still reclaim it once its heartbeat goes stale.
    This is the property that replaced the singleton: worker B
    starting can never fail worker A's in-flight production."""
    run = run_ledger.submit_production(project_id, _unique_script("foreign"))
    # The claim itself stamps worker-a's ownership + a fresh
    # heartbeat (see test_claim_records_ownership_and_heartbeat).
    claimed = run_ledger.claim_run(run["run_id"], worker_id="worker-a")
    assert claimed is not None

    reconciled = run_ledger.reconcile_stale_runs(
        stale_after_seconds=run_ledger.STALE_AFTER_SECONDS
    )
    assert reconciled == 0
    assert run_ledger.get_run(run["run_id"])["status"] == "running"

    _set_heartbeat(
        run["run_id"],
        datetime.now(timezone.utc)
        - timedelta(seconds=run_ledger.STALE_AFTER_SECONDS + 120),
    )
    reconciled = run_ledger.reconcile_stale_runs(
        stale_after_seconds=run_ledger.STALE_AFTER_SECONDS
    )
    assert reconciled == 1
    # Phase 1: the orphaned run is requeued for same-run resume,
    # not failed — worker B's startup can still never disturb a
    # LIVE run (asserted above), and a dead worker's run now
    # continues instead of dying.
    assert run_ledger.get_run(run["run_id"])["status"] == "queued"


def test_real_redis_fifo_order_on_the_production_queue(redis15):
    """PIPE-101 residual proof, on the REAL queue jobs.py enqueues
    to: five jobs enqueued in order execute oldest-first under a
    real SimpleWorker consuming the real Redis list."""
    from rq import SimpleWorker

    queue = jobs.video_queue
    assert queue.name == "video_generation"
    queue.empty()
    redis15.delete("stage3:fifo:order")
    labels = [f"fifo-{index}" for index in range(5)]
    try:
        for label in labels:
            queue.enqueue(_record_fifo_marker, label)
        SimpleWorker([queue], connection=redis15).work(burst=True)
        executed = [
            value.decode()
            for value in redis15.lrange("stage3:fifo:order", 0, -1)
        ]
        assert executed == labels
    finally:
        queue.empty()
        redis15.delete("stage3:fifo:order")


def test_redis_down_submission_waits_in_the_ledger_backlog(project_id):
    """Redis unavailability at submit time, Phase 8.4 semantics
    (design 03 §3.1): the ledger is the queue of record, so a
    submission whose promotion cannot reach Redis does NOT fail
    and does NOT raise — the run waits in the backlog (status
    queued, no job stamp) for a later dispatch pass. (Pre-8.4
    this test asserted the old direct-enqueue contract: raise +
    run marked failed.)"""
    from redis import Redis
    from rq import Queue

    dead_queue = Queue(
        "video_generation",
        connection=Redis.from_url(
            "redis://127.0.0.1:6399/0",
            socket_connect_timeout=2,
            socket_timeout=2,
        ),
    )
    original = jobs.video_queue
    jobs.video_queue = dead_queue
    try:
        result = jobs.enqueue_full_pipeline(
            project_id, _unique_script("redis-down")
        )
    finally:
        jobs.video_queue = original
    assert result is None  # not promoted — and NOT an exception
    runs = run_ledger.list_runs_for(project_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "queued"
    assert not (runs[0]["options"] or {}).get("dispatched_job_id")


def test_concurrent_submissions_mint_exactly_one_run(
    project_id, redis15
):
    """DATA-005 submission race, against real Redis (the mutex is
    a Redis lock, so this test needs the real server): eight
    concurrent submitters of the same project+script produce ONE
    run row and all observe the same run_id."""
    script = _unique_script("race")
    results = []
    errors = []

    def _submit():
        try:
            results.append(
                run_ledger.submit_production(project_id, script)
            )
        except Exception as error:  # pragma: no cover - failure path
            errors.append(error)

    threads = [
        threading.Thread(target=_submit) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors
    assert len(results) == 8
    assert {result["run_id"] for result in results} == {
        results[0]["run_id"]
    }
    assert (
        sum(1 for result in results if not result["deduplicated"])
        == 1
    )
    assert _run_count(project_id) == 1


def test_worker_boots_against_migrated_database(redis15, tmp_path):
    """BLOCK-002 residual + Phase 8.4 multi-worker boot, end to
    end: the REAL worker.py, as a subprocess, against a database
    built by the REAL Alembic chain and the REAL Redis — passes
    the schema gate and reaches work() readiness (RQ registration
    visible in Redis); a SECOND worker then boots alongside it —
    the exact behaviour the deleted singleton used to refuse —
    and both shut down cleanly on SIGTERM."""
    db_file = tmp_path / "boot.sqlite3"
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_file}"
    env["REDIS_URL"] = REDIS_URL
    env["PYTHONUNBUFFERED"] = "1"

    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert migrated.returncode == 0, migrated.stdout + migrated.stderr

    def _registered_workers():
        try:
            return {
                value.decode() if isinstance(value, bytes) else value
                for value in redis15.smembers("rq:workers")
            }
        except Exception:
            return set()

    baseline_workers = _registered_workers()
    process = subprocess.Popen(
        [sys.executable, "worker.py"],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    second_process = None
    output = []
    try:
        # Wait for work() readiness: the worker's RQ registration
        # appears in Redis (only reachable after the schema gate,
        # reconciliation and work() startup).
        deadline = time.time() + 180
        while time.time() < deadline:
            if process.poll() is not None:
                break
            if len(_registered_workers() - baseline_workers) >= 1:
                break
            time.sleep(0.5)
        assert process.poll() is None, (
            "worker exited during boot; output so far is in the "
            "finally-drained buffer"
        )
        assert len(_registered_workers() - baseline_workers) >= 1, (
            "worker never registered with RQ"
        )

        # A second worker on the same deployment boots ALONGSIDE
        # the first — the multi-worker property Phase 8.4 exists
        # to provide (the singleton used to refuse exactly this).
        second_process = subprocess.Popen(
            [sys.executable, "worker.py"],
            cwd=BACKEND_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + 180
        while time.time() < deadline:
            if second_process.poll() is not None:
                break
            if len(_registered_workers() - baseline_workers) >= 2:
                break
            time.sleep(0.5)
        assert second_process.poll() is None, (
            "second worker exited during boot — multi-worker "
            "startup regressed"
        )
        assert len(_registered_workers() - baseline_workers) >= 2, (
            "second worker never registered with RQ"
        )
    finally:
        for proc in (second_process, process):
            if proc is None:
                continue
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
            try:
                out, _ = proc.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate(timeout=30)
            output.append(out or "")

    combined = "".join(output)
    assert "Schema check OK" in combined
    assert "video_generation_priority" in combined
    # Clean SIGTERM shutdown (RQ warm shutdown returns from work()
    # and the process exits normally).
    assert process.returncode == 0, combined
    assert second_process is not None and second_process.returncode == 0
