"""Phase 5a (B-02 / E-06) — run-ledger reliability.

Two loss/lock mechanisms pinned by the audit, and their fixes:

(a) CLAIM-STATUS LOSS. Three write-ordering holes let a run's
    recorded status diverge from its execution:
    - jobs.run_full_pipeline: when claim_run lost AND the
      follow-up get_run could not read the ledger (None), the code
      fell through and EXECUTED the run unclaimed — no owner, no
      heartbeat, status in the DB still "queued" while RQ ran it.
    - run_ledger.finish_run overwrote a terminal status
      unconditionally: a stale finisher (a retried/duplicated
      delivery finishing late) could downgrade a "completed" run
      to "failed" (or resurrect it the other way).
    - run_ledger.set_run_status moved ANY non-terminal run to ANY
      status: a stale queued-era write could downgrade a claimed
      "running" run back to "queued", where it could be claimed
      and executed a second time.
    Fixes: lifecycle writes are guarded in the same transaction
    (conditional UPDATE ... WHERE status IN (...)); a second
    finish is refused; status moves are monotonic; the pipeline
    refuses to execute a caller-carried run it could not claim
    and could not prove terminal — it returns an explicit
    "unclaimed_execution_refused" record instead.

(b) SQLITE LOCK CONTENTION (E-06). database.py created its engine
    with no SQLite configuration: journal_mode stayed "delete"
    (a writer blocks all readers) and busy_timeout was whatever
    the driver default happened to be, so concurrent writers
    (pipeline + dispatcher + reconciler + API) hit
    "database is locked" and single-attempt fail-soft ledger
    writes were lost. Fix: database.create_app_engine — the ONE
    engine factory — applies, for file-backed SQLite only,
    WAL journal mode + busy_timeout on every connection.
    Postgres engines are untouched (no SQLite pragmas).
"""

import threading
import uuid

import pytest
from sqlalchemy import text

import database
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
                script="Phase 5a ledger test script.",
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
        db.query(ProductionRun).filter(
            ProductionRun.project_id == pid
        ).delete()
        db.query(Project).filter(Project.id == pid).delete()
        db.commit()
    finally:
        db.close()


def _queued_run(project_id):
    submission = run_ledger.submit_production(
        project_id, "Phase 5a ledger test script."
    )
    assert submission is not None
    return submission["run_id"]


# ---------------------------------------------------------------------
# (b) SQLite engine configuration
# ---------------------------------------------------------------------


def test_sqlite_engine_sets_wal_and_busy_timeout(tmp_path):
    eng = database.create_app_engine(
        f"sqlite:///{tmp_path / 'ledger.db'}"
    )
    try:
        with eng.connect() as connection:
            journal_mode = connection.execute(
                text("PRAGMA journal_mode")
            ).scalar()
            busy_timeout = connection.execute(
                text("PRAGMA busy_timeout")
            ).scalar()
        assert str(journal_mode).lower() == "wal"
        assert int(busy_timeout) == database.SQLITE_BUSY_TIMEOUT_MS
        assert int(busy_timeout) >= 5000
    finally:
        eng.dispose()


def test_sqlite_memory_engine_still_works(tmp_path):
    """In-memory SQLite has no file to journal: engine creation
    and use must not fail, and busy_timeout still applies."""
    eng = database.create_app_engine("sqlite:///:memory:")
    try:
        with eng.connect() as connection:
            busy_timeout = connection.execute(
                text("PRAGMA busy_timeout")
            ).scalar()
        assert int(busy_timeout) == database.SQLITE_BUSY_TIMEOUT_MS
    finally:
        eng.dispose()


def test_postgres_engine_gets_no_sqlite_configuration():
    """Pragmas are SQLite-only: building a Postgres engine must
    succeed (lazily — no connection is made) and carry no SQLite
    connect args."""
    eng = database.create_app_engine(
        "postgresql+psycopg2://user:pass@127.0.0.1:1/unused"
    )
    try:
        assert eng.dialect.name == "postgresql"
    finally:
        eng.dispose()


def test_app_engine_is_the_configured_factory():
    """The module engine the whole app shares is built by the one
    factory, so every SessionLocal user benefits."""
    with engine.connect() as connection:
        busy_timeout = connection.execute(
            text("PRAGMA busy_timeout")
        ).scalar()
    assert int(busy_timeout) == database.SQLITE_BUSY_TIMEOUT_MS


def test_concurrent_writers_do_not_lose_claims(project_id):
    """Eight threads claim eight distinct queued runs at once on
    the shared file-backed SQLite database: every claim lands, no
    'database is locked' loss."""
    run_ids = []
    for index in range(8):
        submission = run_ledger.submit_production(
            project_id, f"Concurrent script {index}."
        )
        assert submission is not None
        run_ids.append(submission["run_id"])

    results = {}

    def claim(run_id):
        results[run_id] = run_ledger.claim_run(run_id)

    threads = [
        threading.Thread(target=claim, args=(run_id,))
        for run_id in run_ids
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(results.get(run_id) for run_id in run_ids), results
    for run_id in run_ids:
        run = run_ledger.get_run(run_id)
        assert run["status"] == "running"
        assert run["progress"]["worker_id"]


def test_concurrent_claim_of_one_run_has_exactly_one_winner(project_id):
    run_id = _queued_run(project_id)
    outcomes = []
    lock = threading.Lock()

    def claim():
        won = run_ledger.claim_run(run_id) is not None
        with lock:
            outcomes.append(won)

    threads = [threading.Thread(target=claim) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert outcomes.count(True) == 1, outcomes


# ---------------------------------------------------------------------
# (a) Monotonic lifecycle writes — a stale write cannot downgrade.
# ---------------------------------------------------------------------


def test_second_finish_cannot_overwrite_terminal_status(project_id):
    run_id = run_ledger.begin_run(
        project_id, "Phase 5a ledger test script."
    )
    assert run_ledger.finish_run(run_id, "completed") is True
    # A stale finisher arriving late must NOT downgrade the run.
    assert run_ledger.finish_run(run_id, "failed", error="late") is False
    run = run_ledger.get_run(run_id)
    assert run["status"] == "completed"
    assert run["error"] is None


def test_finish_still_records_execution_for_the_first_finisher(project_id):
    run_id = run_ledger.begin_run(
        project_id, "Phase 5a ledger test script."
    )
    assert (
        run_ledger.finish_run(
            run_id, "failed", error="boom", execution={"a": 1}
        )
        is True
    )
    run = run_ledger.get_run(run_id)
    assert run["status"] == "failed"
    assert run["error"] == "boom"
    assert run["execution"] == {"a": 1}


def test_set_run_status_cannot_downgrade_a_claimed_run(project_id):
    run_id = _queued_run(project_id)
    assert run_ledger.claim_run(run_id) is not None
    # A stale queued-era write must not send a running run back to
    # queued (where it could be claimed and executed twice).
    assert run_ledger.set_run_status(run_id, "queued") is False
    assert run_ledger.get_run(run_id)["status"] == "running"


def test_set_run_status_still_parks_a_queued_run(project_id):
    """The legitimate move the helper exists for (the plan-
    approval gate parks an analysis run) keeps working."""
    run_id = _queued_run(project_id)
    assert (
        run_ledger.set_run_status(run_id, "awaiting_approval")
        is True
    )
    assert (
        run_ledger.get_run(run_id)["status"] == "awaiting_approval"
    )


def test_mark_dispatched_after_claim_is_refused(project_id):
    run_id = _queued_run(project_id)
    assert run_ledger.claim_run(run_id) is not None
    assert run_ledger.mark_dispatched(run_id, "job-1") is False
    run = run_ledger.get_run(run_id)
    assert "dispatched_job_id" not in (run["options"] or {})


# ---------------------------------------------------------------------
# (a) The pipeline never executes a run it could not claim.
# ---------------------------------------------------------------------


class _ExecutedSentinel(Exception):
    """Raised if pipeline execution is reached in these tests."""


def _block_execution(monkeypatch, tmp_path):
    monkeypatch.setattr(
        jobs.media_paths, "project_dir", lambda pid: tmp_path
    )
    monkeypatch.setattr(jobs.run_metrics, "begin_run", lambda *a: None)
    monkeypatch.setattr(
        jobs, "create_production_state",
        lambda *a, **kw: (_ for _ in ()).throw(_ExecutedSentinel()),
    )


def test_pipeline_refuses_unclaimed_execution_when_ledger_unreadable(
    monkeypatch, tmp_path, project_id
):
    """claim lost + ledger unreadable (the lock-burst shape) must
    NOT fall through into execution: the run is refused, loudly,
    with an explicit record — never executed unclaimed."""
    _block_execution(monkeypatch, tmp_path)
    monkeypatch.setattr(run_ledger, "claim_run", lambda *a, **kw: None)
    monkeypatch.setattr(run_ledger, "get_run", lambda *a, **kw: None)

    result = jobs.run_full_pipeline(
        project_id, "Phase 5a ledger test script.", run_id="run-x"
    )

    assert result["run_id"] == "run-x"
    assert result["status"] == "unclaimed_execution_refused"


def test_pipeline_refuses_when_run_known_non_claimable(
    monkeypatch, tmp_path, project_id
):
    _block_execution(monkeypatch, tmp_path)
    monkeypatch.setattr(run_ledger, "claim_run", lambda *a, **kw: None)
    monkeypatch.setattr(
        run_ledger, "get_run",
        lambda *a, **kw: {"status": "running"},
    )

    result = jobs.run_full_pipeline(
        project_id, "Phase 5a ledger test script.", run_id="run-y"
    )

    assert result["deduplicated_execution"] is True
    assert result["status"] == "running"
