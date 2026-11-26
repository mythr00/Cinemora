"""Gate 2 field-evidence fixes (2026-11-26).

Two defects reproduced on the live codespace deployment
(pre-Gate-2 code, HEAD 018278c):

1. A dispatched RQ job that fails terminally (there:
   run_full_pipeline died instantly with PermissionError creating
   the project directory, before it could claim or fail its own
   run) left the run "queued" forever: the dispatcher's stale-
   dispatch sweep fetched the job, RQ still held it as a FAILED
   job, and the sweep read "exists" as "a worker will take it",
   while the promotion pass skipped the run for its stamp.
   The sweep now marks such a run FAILED with the job's error
   recorded on the run (run.error — the field the API exposes).

2. The Docker image could not guarantee its media root was
   writable by the app user when a mounted volume replaced the
   image's ownership. The image now starts through
   entrypoint.sh, which repairs the /data-steered paths as root
   and drops to the app user via gosu before exec'ing the real
   command (static checks — no Docker on the test machine).
"""

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis
import pytest

import jobs
from database import SessionLocal
from models import Base, Plan, ProductionRun, Project, User, UserSubscription
from services import dispatcher, run_ledger

BACKEND_DIR = Path(__file__).resolve().parent.parent


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
    dispatcher.set_redis_client(client)
    yield client
    dispatcher.reset_redis_client()


def _project():
    db = SessionLocal()
    try:
        project = Project(
            id=str(uuid.uuid4()), title="P", script="One. Two.", owner_id=None
        )
        db.add(project)
        db.commit()
        return project.id
    finally:
        db.close()


def _queued_run(project_id, **options):
    db = SessionLocal()
    try:
        run = ProductionRun(
            id=str(uuid.uuid4()),
            project_id=project_id,
            owner_id=None,
            script_hash=run_ledger.script_sha256("One. Two."),
            status="queued",
            options=options or {},
            created_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


class _FakeJob:
    def __init__(self, failed, exc_info=None):
        self.is_failed = failed
        self.exc_info = exc_info


def _patch_job_fetch(monkeypatch, job_or_exc):
    def fake_fetch(cls, job_id, connection=None):
        if isinstance(job_or_exc, Exception):
            raise job_or_exc
        return job_or_exc

    monkeypatch.setattr("rq.job.Job.fetch", classmethod(fake_fetch))


# ---------------------------------------------------------------------
# Defect 2: dispatched-then-died run must not stay "queued" silently
# ---------------------------------------------------------------------


def test_failed_dispatched_job_fails_the_run_with_error_visible(monkeypatch):
    project_id = _project()
    run_id = _queued_run(
        project_id,
        dispatched_job_id="job-dead",
        dispatched_at=datetime.now(timezone.utc).isoformat(),
    )
    _patch_job_fetch(
        monkeypatch,
        _FakeJob(
            failed=True,
            exc_info=(
                "Traceback (most recent call last):\n"
                "  File \"jobs.py\", line 1639, in run_full_pipeline\n"
                "PermissionError: [Errno 13] Permission denied: "
                "'/data/media/abc'"
            ),
        ),
    )
    db = SessionLocal()
    try:
        dispatcher._clear_stale_dispatches(db)
    finally:
        db.close()
    run = run_ledger.get_run(run_id)
    assert run is not None
    assert run["status"] == "failed"
    assert run["finished_at"] is not None
    assert "PermissionError" in (run["error"] or "")
    assert "dispatched job failed" in (run["error"] or "")


def test_live_dispatched_job_is_left_alone(monkeypatch):
    project_id = _project()
    run_id = _queued_run(
        project_id,
        dispatched_job_id="job-live",
        dispatched_at=datetime.now(timezone.utc).isoformat(),
    )
    _patch_job_fetch(monkeypatch, _FakeJob(failed=False))
    db = SessionLocal()
    try:
        dispatcher._clear_stale_dispatches(db)
    finally:
        db.close()
    run = run_ledger.get_run(run_id)
    assert run["status"] == "queued"
    assert (run["options"] or {}).get("dispatched_job_id") == "job-live"


def test_stale_marker_from_dead_job_does_not_skip_run_forever(
    monkeypatch, fake_redis
):
    """A run whose dispatched job no longer exists at all gets its
    stamp cleared and is promoted by the same dispatch pass."""
    project_id = _project()
    run_id = _queued_run(
        project_id,
        dispatched_job_id="job-gone",
        dispatched_at=(
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat(),
    )
    _patch_job_fetch(monkeypatch, Exception("No such job"))

    promoted = []

    def fake_enqueue(run, project, is_priority):
        promoted.append(run.id)
        return "job-new"

    monkeypatch.setattr(jobs, "enqueue_promoted_run", fake_enqueue)
    result = dispatcher.dispatch_once()
    assert (run_id, "job-new") in result
    assert promoted == [run_id]
    run = run_ledger.get_run(run_id)
    assert (run["options"] or {}).get("dispatched_job_id") == "job-new"


# ---------------------------------------------------------------------
# F-4 (2026-11-26): dispatched job FINISHED without ever claiming
# the run (its claim lost to a SQLite "database is locked" burst;
# the job returned deduplicated_execution — the B-02 refusal
# working as designed). The run is not failed and must not sit
# invisibly "queued" until the job's RQ result record expires:
# the stale-dispatch sweep clears the stamp so the run is
# re-promoted, exactly like the missing-job case.
# ---------------------------------------------------------------------


class _FakeFinishedJob:
    def __init__(self, finished=True, failed=False):
        self.is_finished = finished
        self.is_failed = failed
        self.exc_info = None


def _queued_run_with_execution_markers(project_id, **fields):
    db = SessionLocal()
    try:
        run = ProductionRun(
            id=str(uuid.uuid4()),
            project_id=project_id,
            owner_id=None,
            script_hash=run_ledger.script_sha256("One. Two."),
            status="queued",
            created_at=datetime.now(timezone.utc),
            **fields,
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def test_finished_dispatched_job_without_claim_unwedges_the_run(
    monkeypatch,
):
    project_id = _project()
    run_id = _queued_run(
        project_id,
        dispatched_job_id="job-finished-unclaimed",
        dispatched_at=datetime.now(timezone.utc).isoformat(),
    )
    _patch_job_fetch(monkeypatch, _FakeFinishedJob(finished=True))
    db = SessionLocal()
    try:
        dispatcher._clear_stale_dispatches(db)
    finally:
        db.close()
    run = run_ledger.get_run(run_id)
    # Not failed — nothing about the run itself failed.
    assert run["status"] == "queued"
    assert run["error"] is None
    options = run["options"] or {}
    assert "dispatched_job_id" not in options
    assert "dispatched_at" not in options


def test_finished_dispatched_job_run_is_repromoted_next_pass(
    monkeypatch, fake_redis
):
    """The full F-4 sequence: stamp cleared by the sweep inside
    dispatch_once, and the same pass promotes the run again."""
    project_id = _project()
    run_id = _queued_run(
        project_id,
        dispatched_job_id="job-finished-unclaimed",
        dispatched_at=datetime.now(timezone.utc).isoformat(),
    )
    _patch_job_fetch(monkeypatch, _FakeFinishedJob(finished=True))

    promoted = []

    def fake_enqueue(run, project, is_priority):
        promoted.append(run.id)
        return "job-new"

    monkeypatch.setattr(jobs, "enqueue_promoted_run", fake_enqueue)
    result = dispatcher.dispatch_once()
    assert (run_id, "job-new") in result
    assert promoted == [run_id]
    run = run_ledger.get_run(run_id)
    assert (run["options"] or {}).get("dispatched_job_id") == "job-new"


def test_live_dispatched_job_still_left_alone(monkeypatch):
    project_id = _project()
    run_id = _queued_run(
        project_id,
        dispatched_job_id="job-live",
        dispatched_at=datetime.now(timezone.utc).isoformat(),
    )
    _patch_job_fetch(
        monkeypatch, _FakeFinishedJob(finished=False, failed=False)
    )
    db = SessionLocal()
    try:
        dispatcher._clear_stale_dispatches(db)
    finally:
        db.close()
    run = run_ledger.get_run(run_id)
    assert run["status"] == "queued"
    assert (run["options"] or {}).get("dispatched_job_id") == "job-live"


def test_finished_job_with_execution_started_is_left_alone(monkeypatch):
    """A finished job whose run WAS claimed (the claim stamps
    started_at + presence atomically) is not unwedged: the run's
    own finish path owns that state."""
    project_id = _project()
    run_id = _queued_run_with_execution_markers(
        project_id,
        options={
            "dispatched_job_id": "job-finished-claimed",
            "dispatched_at": datetime.now(timezone.utc).isoformat(),
        },
        started_at=datetime.now(timezone.utc),
        progress={
            "worker_id": "worker-1",
            "claimed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    _patch_job_fetch(monkeypatch, _FakeFinishedJob(finished=True))
    db = SessionLocal()
    try:
        dispatcher._clear_stale_dispatches(db)
    finally:
        db.close()
    run = run_ledger.get_run(run_id)
    assert run["status"] == "queued"
    assert (run["options"] or {}).get(
        "dispatched_job_id"
    ) == "job-finished-claimed"


# ---------------------------------------------------------------------
# Defect 1: image/entrypoint guarantees a writable media root
# ---------------------------------------------------------------------


def test_entrypoint_repairs_media_root_then_drops_privileges():
    entrypoint = (BACKEND_DIR / "entrypoint.sh").read_text(encoding="utf-8")
    dockerfile = (BACKEND_DIR / "Dockerfile").read_text(encoding="utf-8")
    # The entrypoint is the image's entry point and drops to the
    # app user before exec'ing the real command.
    assert 'ENTRYPOINT ["/app/entrypoint.sh"]' in dockerfile
    assert "gosu" in dockerfile
    assert 'exec gosu "$APP_USER" "$@"' in entrypoint
    # It repairs the media root (env-steered, /data/media default)
    # recursively as root before dropping privileges.
    assert 'MEDIA_ROOT="${CINEMORA_MEDIA_ROOT:-/data/media}"' in entrypoint
    assert 'chown -R "$APP_USER:$APP_USER" "$MEDIA_ROOT"' in entrypoint
    # The app user itself is unchanged.
    assert "useradd --uid 10001" in dockerfile
    assert 'APP_USER="cinemora"' in entrypoint
