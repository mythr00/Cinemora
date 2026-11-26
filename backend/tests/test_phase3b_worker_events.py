"""Phase 3b / C-15 — worker/watchdog death forensics.

The 2026-11-24 stack deaths (six in a day, watchdog dying
repeatedly) were never explained because nothing recorded death
circumstances. These tests pin the in-repo record that makes the
NEXT death diagnosable:

- services.worker_events: structured JSONL lifecycle store
  (record + read round-trip; fail-soft on an unwritable path).
- supervise.WorkerPool: a dead child leaves a "worker_died" event
  carrying slot, pid and the exit code the supervisor saw, before
  it is respawned; spawns leave "worker_spawned".
- run_ledger.reconcile_stale_runs (heartbeat-aware): reclaiming an
  orphaned run leaves a "heartbeat_loss" event naming the run, its
  owning worker, its last heartbeat and the heartbeat's age.
- run_ledger._env_float (C-15 review fix): a malformed heartbeat /
  stale env value falls back to the default instead of crashing
  this module's import — and with it every ledger consumer.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import supervise
from database import Base, SessionLocal, engine
from models import ProductionRun, Project
from services import run_ledger, worker_events


@pytest.fixture(autouse=True)
def _events_file(tmp_path, monkeypatch):
    path = tmp_path / "worker_events.jsonl"
    monkeypatch.setenv("CINEMORA_WORKER_EVENTS_PATH", str(path))
    return path


# ---------------------------------------------------------------------
# The store itself
# ---------------------------------------------------------------------


def test_record_and_read_round_trip(_events_file):
    assert worker_events.record_event(
        "worker_start", worker_id="host:1:abcd", queues=["video_generation"]
    ) is True
    assert worker_events.record_event("worker_exit", exit_kind="returned")
    events = worker_events.read_events()
    assert [e["event"] for e in events] == ["worker_start", "worker_exit"]
    start = events[0]
    assert start["worker_id"] == "host:1:abcd"
    assert start["queues"] == ["video_generation"]
    assert start["pid"] > 0 and start["ts"] and start["hostname"]


def test_read_events_missing_store_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "CINEMORA_WORKER_EVENTS_PATH", str(tmp_path / "nope.jsonl")
    )
    assert worker_events.read_events() == []


def test_record_event_is_fail_soft_on_unwritable_path(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv(
        "CINEMORA_WORKER_EVENTS_PATH", str(blocker / "events.jsonl")
    )
    # Must not raise — forensics never takes a worker down.
    assert worker_events.record_event("worker_start") is False


# ---------------------------------------------------------------------
# Supervisor witness: exit code / signal recorded before respawn
# ---------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, cmd, env=None, cwd=None):
        self.cmd = list(cmd)
        self.pid = 4242
        self.exit_code = None

    def poll(self):
        return self.exit_code

    def die(self, code):
        self.exit_code = code

    def terminate(self):
        if self.exit_code is None:
            self.exit_code = -15

    def kill(self):
        if self.exit_code is None:
            self.exit_code = -9

    def wait(self, timeout=None):
        return self.exit_code

    def send_signal(self, signum):
        pass


def test_supervisor_records_worker_death_with_exit_code(_events_file):
    pool = supervise.WorkerPool(1, spawn=_FakeProcess)
    pool.start()
    pool.workers[0].die(-9)  # SIGKILL — the unexplained-death shape
    assert pool.restart_dead() == 1
    events = worker_events.read_events()
    spawned = [e for e in events if e["event"] == "worker_spawned"]
    died = [e for e in events if e["event"] == "worker_died"]
    assert len(spawned) == 1 and spawned[0]["child_pid"] == 4242
    assert len(died) == 1
    assert died[0]["slot"] == 0
    assert died[0]["child_pid"] == 4242
    assert died[0]["exit_code"] == -9
    assert died[0]["restart_count"] == 1


def test_supervisor_records_nothing_for_live_workers(_events_file):
    pool = supervise.WorkerPool(1, spawn=_FakeProcess)
    pool.start()
    assert pool.restart_dead() == 0
    died = [e for e in worker_events.read_events()
            if e["event"] == "worker_died"]
    assert died == []


# ---------------------------------------------------------------------
# Heartbeat loss: the reconciler records what died
# ---------------------------------------------------------------------


@pytest.fixture()
def _ledger_tables():
    Base.metadata.create_all(
        bind=engine,
        tables=[Project.__table__, ProductionRun.__table__],
    )
    yield


@pytest.fixture()
def project_id(_ledger_tables):
    pid = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(Project(id=pid, script="C-15 test script.",
                       category="Documentary", status="created"))
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


def test_reconcile_records_heartbeat_loss_event(project_id, _events_file):
    run_id = run_ledger.begin_run(
        project_id, f"C-15 stale script {uuid.uuid4().hex}."
    )
    owner = run_ledger.get_run(run_id)["progress"]["worker_id"]
    last_beat = datetime.now(timezone.utc) - timedelta(seconds=3600)
    db = SessionLocal()
    try:
        row = db.get(ProductionRun, run_id)
        progress = dict(row.progress or {})
        progress["heartbeat_at"] = last_beat.isoformat()
        row.progress = progress
        db.commit()
    finally:
        db.close()

    assert run_ledger.reconcile_stale_runs(stale_after_seconds=180) >= 1
    # Phase 1: a first heartbeat loss RESUMES the run (requeued in
    # place) instead of failing it; the forensic event is recorded
    # either way, now carrying the outcome.
    assert run_ledger.get_run(run_id)["status"] == "queued"

    losses = [e for e in worker_events.read_events()
              if e["event"] == "heartbeat_loss"]
    assert losses, "reclaiming an orphaned run must leave a record"
    mine = [e for e in losses
            if any(r["run_id"] == run_id for r in e["runs"])]
    assert len(mine) == 1
    detail = next(r for r in mine[0]["runs"] if r["run_id"] == run_id)
    assert detail["worker_id"] == owner
    assert detail["last_heartbeat_at"] == last_beat.isoformat()
    assert detail["heartbeat_age_seconds"] > 3500
    assert detail["outcome"] == "resumed"
    assert mine[0]["stale_after_seconds"] == 180.0


# ---------------------------------------------------------------------
# C-15 review fix: malformed heartbeat env cannot kill the import
# ---------------------------------------------------------------------


def test_env_float_falls_back_on_malformed_values(monkeypatch):
    monkeypatch.setenv("CINEMORA_TEST_FLOAT", "not-a-number")
    assert run_ledger._env_float("CINEMORA_TEST_FLOAT", 30) == 30.0
    monkeypatch.setenv("CINEMORA_TEST_FLOAT", "")
    assert run_ledger._env_float("CINEMORA_TEST_FLOAT", 30) == 30.0
    monkeypatch.setenv("CINEMORA_TEST_FLOAT", "-5")
    assert run_ledger._env_float("CINEMORA_TEST_FLOAT", 30) == 30.0
    monkeypatch.setenv("CINEMORA_TEST_FLOAT", "45")
    assert run_ledger._env_float("CINEMORA_TEST_FLOAT", 30) == 45.0
    monkeypatch.delenv("CINEMORA_TEST_FLOAT")
    assert run_ledger._env_float("CINEMORA_TEST_FLOAT", 30) == 30.0
