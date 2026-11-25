"""Phase 5b / B-11 — admin failed-job management.

Admins can list / inspect failed jobs (ledger + RQ failed
registry, full diagnostics), requeue a failed run through the
existing dispatch path (idempotent for active runs), and purge a
dead job's queue record without touching project/run data. All
routes are admin-only; writes land in the admin audit log.
"""

import uuid
from datetime import datetime, timezone

import fakeredis
import pytest
from fastapi.testclient import TestClient

import jobs
import main
from database import SessionLocal
from models import (
    AdminAuditLog,
    Base,
    Plan,
    ProductionRun,
    Project,
    User,
    UserSubscription,
)
from services import admin_health, admin_jobs
from services import auth as auth_service
from services import run_ledger

OPERATOR_TOKEN = "test-operator-token-p5b-jobs"


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    import api_v1

    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=SessionLocal().get_bind())
    fake_text = fakeredis.FakeRedis(decode_responses=True)
    auth_service.set_redis_client(fake_text)
    admin_health.set_redis_client(fake_text)
    fake_queue = fakeredis.FakeRedis(decode_responses=False)
    admin_jobs.set_redis_client(fake_queue)
    db = SessionLocal()
    try:
        for model in (AdminAuditLog, ProductionRun, Project,
                      UserSubscription, User, Plan):
            db.query(model).delete()
        db.commit()
        db.add(Plan(id=str(uuid.uuid4()), name="free",
                    max_concurrent_runs=1, is_default=True))
        db.commit()
    finally:
        db.close()
    yield fake_queue
    auth_service.reset_redis_client()
    admin_health.reset_redis_client()
    admin_jobs.reset_redis_client()


def _headers(role="user"):
    db = SessionLocal()
    try:
        user = User(
            id=str(uuid.uuid4()),
            email=f"{role}-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=auth_service.hash_password("pw-12345678"),
            role=role, status="active",
        )
        db.add(user)
        db.commit()
        uid = user.id
    finally:
        db.close()
    return {"Authorization": f"Bearer {auth_service.create_session(uid)}"}


@pytest.fixture()
def admin_headers():
    return _headers("super_admin")


@pytest.fixture()
def plain_headers():
    return _headers("user")


def _seed_run(status="failed", error="coverage gate exploded", options=None):
    db = SessionLocal()
    try:
        project = Project(
            id=str(uuid.uuid4()),
            script="A script about failure.",
            category="Documentary",
            status="created",
        )
        db.add(project)
        run = ProductionRun(
            id=str(uuid.uuid4()),
            project_id=project.id,
            status=status,
            stage="video_editor",
            error=error,
            options=options or {},
            script_hash=run_ledger.script_sha256(project.script),
            finished_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()
        return project.id, run.id
    finally:
        db.close()


def _audit_actions():
    db = SessionLocal()
    try:
        return [
            (row.action, row.target_id, row.result)
            for row in db.query(AdminAuditLog).all()
        ]
    finally:
        db.close()


def _make_rq_failed_job(fake_queue, run_id=None, project_id=None):
    from rq import Queue
    from rq.job import Job
    from rq.registry import FailedJobRegistry

    queue = Queue("video_generation", connection=fake_queue)
    job = Job.create(
        func=jobs.run_full_pipeline,
        args=(project_id or "p", "script"),
        kwargs={"run_id": run_id, "project_id": project_id},
        connection=fake_queue,
    )
    job.save()
    FailedJobRegistry(queue=queue).add(job, exc_string="Traceback: boom")
    return job.id


# ---------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------


def test_failed_job_routes_are_admin_only(client, plain_headers):
    _, run_id = _seed_run()
    for method, path in (
        ("GET", "/api/v1/admin/failed-jobs"),
        ("GET", f"/api/v1/admin/failed-jobs/{run_id}"),
        ("POST", f"/api/v1/admin/failed-jobs/{run_id}/requeue"),
        ("DELETE", f"/api/v1/admin/failed-jobs/{run_id}"),
    ):
        assert client.request(method, path).status_code in (401, 403), path
        plain = client.request(method, path, headers=plain_headers)
        assert plain.status_code == 403, (path, plain.text)


# ---------------------------------------------------------------------
# List / inspect
# ---------------------------------------------------------------------


def test_list_and_inspect_failed_jobs(client, admin_headers, _env):
    project_id, run_id = _seed_run()
    _seed_run(status="completed", error=None)
    job_id = _make_rq_failed_job(_env, run_id="rq-only-run",
                                 project_id=project_id)

    listing = client.get("/api/v1/admin/failed-jobs",
                         headers=admin_headers)
    assert listing.status_code == 200
    body = listing.json()
    ids = {entry["id"] for entry in body["jobs"]}
    assert run_id in ids
    assert job_id in ids  # RQ-registry-only dead job is listed too
    ledger_entry = next(e for e in body["jobs"] if e["id"] == run_id)
    # Admins see full diagnostics (FS rules).
    assert ledger_entry["error"] == "coverage gate exploded"

    detail = client.get(f"/api/v1/admin/failed-jobs/{run_id}",
                        headers=admin_headers)
    assert detail.status_code == 200
    assert detail.json()["error"] == "coverage gate exploded"

    missing = client.get("/api/v1/admin/failed-jobs/nope",
                         headers=admin_headers)
    assert missing.status_code == 404


# ---------------------------------------------------------------------
# Requeue
# ---------------------------------------------------------------------


def test_requeue_failed_run_uses_dispatch_path(
    client, admin_headers, monkeypatch
):
    project_id, run_id = _seed_run()
    calls = []

    def _stub_enqueue(pid, script, options=None, run_id=None):
        calls.append((pid, script))
        submission = run_ledger.submit_production(pid, script,
                                                  options=options)
        return submission["run_id"]

    monkeypatch.setattr(jobs, "enqueue_full_pipeline", _stub_enqueue)
    response = client.post(
        f"/api/v1/admin/failed-jobs/{run_id}/requeue",
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requeued"] is True
    assert body["new_run_id"] and body["new_run_id"] != run_id
    assert calls and calls[0][0] == project_id
    new_run = run_ledger.get_run(body["new_run_id"])
    assert new_run["status"] == "queued"
    assert ("job.requeue", run_id, "success") in _audit_actions()


def test_requeue_active_run_is_idempotent_noop(
    client, admin_headers, monkeypatch
):
    _, run_id = _seed_run(status="running", error=None)
    calls = []
    monkeypatch.setattr(
        jobs, "enqueue_full_pipeline",
        lambda *a, **k: calls.append(a),
    )
    response = client.post(
        f"/api/v1/admin/failed-jobs/{run_id}/requeue",
        headers=admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["requeued"] is False
    assert body["already_active"] is True
    assert calls == []  # nothing was dispatched again


def test_requeue_completed_run_conflicts(client, admin_headers):
    _, run_id = _seed_run(status="completed", error=None)
    response = client.post(
        f"/api/v1/admin/failed-jobs/{run_id}/requeue",
        headers=admin_headers,
    )
    assert response.status_code == 409


def test_requeue_unknown_run_404(client, admin_headers):
    response = client.post(
        "/api/v1/admin/failed-jobs/no-such-run/requeue",
        headers=admin_headers,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------


def test_purge_removes_queue_record_only(client, admin_headers, _env):
    project_id, run_id = _seed_run(
        options={"dispatched_job_id": None}
    )
    job_id = _make_rq_failed_job(_env, run_id=run_id,
                                 project_id=project_id)
    # Point the ledger row at the dead queue job.
    db = SessionLocal()
    try:
        row = db.get(ProductionRun, run_id)
        row.options = {"dispatched_job_id": job_id}
        db.commit()
    finally:
        db.close()

    response = client.delete(
        f"/api/v1/admin/failed-jobs/{run_id}", headers=admin_headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["purged"] is True
    assert response.json()["job_id"] == job_id

    # The queue record is gone...
    from rq.registry import FailedJobRegistry
    from rq import Queue

    registry = FailedJobRegistry(
        queue=Queue("video_generation", connection=_env)
    )
    assert job_id not in registry.get_job_ids()
    # ...but the project and the ledger run row are untouched.
    db = SessionLocal()
    try:
        assert db.get(Project, project_id) is not None
        run_row = db.get(ProductionRun, run_id)
        assert run_row is not None and run_row.status == "failed"
    finally:
        db.close()
    assert ("job.purge", job_id, "success") in _audit_actions()


def test_purge_without_queue_record_404_keeps_run(
    client, admin_headers
):
    _, run_id = _seed_run()
    response = client.delete(
        f"/api/v1/admin/failed-jobs/{run_id}", headers=admin_headers
    )
    assert response.status_code == 404
    assert run_ledger.get_run(run_id)["status"] == "failed"
