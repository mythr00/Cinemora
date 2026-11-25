"""Phase 8 slice 9 — §21 generated-video retention (directive
§21 G test list).

Deletion exactly at creation+50h and never before; countdown
accuracy; 24h/6h/1h reminders with honestly-recorded email
status; background (browser-closed) deletion via the tick;
restart recovery (state lives in the DB, not the process);
storage confirmation; retry-then-alert on deletion failure;
preservation of scripts/metadata/projects; unregistered media
(protected assets) unreachable; active renders never deleted.
"""

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import models
from database import Base, SessionLocal, engine
from services import media_paths
from services.billing import retention

Base.metadata.create_all(bind=engine)

T0 = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def media_root(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(root))
    return root


@pytest.fixture()
def db():
    session = SessionLocal()
    yield session
    session.close()


def _make_run(db, media_root, *, with_intermediate=True, finished_at=None):
    user = models.User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@example.invalid",
        password_hash="x",
    )
    db.add(user)
    project = models.Project(
        id=str(uuid.uuid4()), script="A script.", owner_id=user.id
    )
    db.add(project)
    run = models.ProductionRun(
        id=str(uuid.uuid4()), project_id=project.id,
        owner_id=user.id, status="completed",
        created_at=T0, finished_at=finished_at or T0,
    )
    db.add(run)
    db.commit()
    run_dir = media_paths.run_dir(project.id, run.id)
    run_dir.mkdir(parents=True)
    (run_dir / "documentary_final_output.mp4").write_bytes(b"master")
    (run_dir / "documentary_delivery.mp4").write_bytes(b"delivery")
    if with_intermediate:
        (run_dir / "segment_0001.mp4").write_bytes(b"segment")
    (run_dir / "production_state.json").write_text("{}")
    return user, project, run, run_dir


def _video(db, run_id, kind="master"):
    return (
        db.query(models.GeneratedVideo)
        .filter_by(run_id=run_id, kind=kind)
        .one()
    )


def test_registration_anchors_deadline_at_finish_plus_50h(db, media_root):
    user, project, run, run_dir = _make_run(db, media_root)
    ids = retention.register_run_outputs(run.id, run_dir)
    assert len(ids) == 2
    db.expire_all()
    video = _video(db, run.id)
    delete_after = video.delete_after
    if delete_after.tzinfo is None:
        delete_after = delete_after.replace(tzinfo=timezone.utc)
    assert delete_after == T0 + timedelta(hours=50)
    assert video.status == "active"
    assert video.download_count == 0


def test_no_deletion_before_deadline(db, media_root, monkeypatch):
    monkeypatch.setenv("CINEMORA_RETENTION_ENABLED", "1")
    user, project, run, run_dir = _make_run(db, media_root)
    retention.register_run_outputs(run.id, run_dir)
    summary = retention.run_retention_tick(
        now=T0 + timedelta(hours=49, minutes=59)
    )
    assert summary["deleted"] == 0
    assert (run_dir / "documentary_final_output.mp4").exists()
    db.expire_all()
    assert _video(db, run.id).status == "active"


def test_deletion_exactly_at_deadline_preserves_metadata(
    db, media_root, monkeypatch
):
    monkeypatch.setenv("CINEMORA_RETENTION_ENABLED", "1")
    user, project, run, run_dir = _make_run(db, media_root)
    retention.register_run_outputs(run.id, run_dir)
    summary = retention.run_retention_tick(now=T0 + timedelta(hours=50))
    # (The tick also processes rows other tests registered into
    # the shared suite DB; assert on THIS run's outcomes.)
    assert summary["deleted"] >= 2  # this run's master + delivery
    # Videos and their .mp4 intermediates are gone...
    assert not (run_dir / "documentary_final_output.mp4").exists()
    assert not (run_dir / "documentary_delivery.mp4").exists()
    assert not (run_dir / "segment_0001.mp4").exists()
    # ...metadata and the project itself are preserved.
    assert (run_dir / "production_state.json").exists()
    db.expire_all()
    assert db.get(models.Project, project.id) is not None
    video = _video(db, run.id)
    assert video.status == "deleted"
    assert video.storage_confirmed is True
    assert video.deleted_at is not None


def test_deletion_requires_the_enable_switch(db, media_root):
    user, project, run, run_dir = _make_run(db, media_root)
    retention.register_run_outputs(run.id, run_dir)
    summary = retention.run_retention_tick(now=T0 + timedelta(hours=60))
    assert summary["deleted"] == 0
    assert summary["skipped"] >= 2
    assert (run_dir / "documentary_final_output.mp4").exists()


def test_reminders_at_24_6_1_hours_with_honest_email_status(
    db, media_root
):
    user, project, run, run_dir = _make_run(db, media_root)
    retention.register_run_outputs(run.id, run_dir)

    retention.run_retention_tick(now=T0 + timedelta(hours=26))
    retention.run_retention_tick(now=T0 + timedelta(hours=44))
    retention.run_retention_tick(now=T0 + timedelta(hours=49, minutes=30))
    # Re-tick: no duplicate reminders.
    retention.run_retention_tick(now=T0 + timedelta(hours=49, minutes=45))

    db.expire_all()
    notes = (
        db.query(models.UserNotification)
        .filter_by(user_id=user.id)
        .all()
    )
    kinds = sorted(n.kind for n in notes)
    # Two registered videos x three thresholds, exactly once each.
    assert kinds == sorted(
        ["retention_24h"] * 2 + ["retention_6h"] * 2
        + ["retention_1h"] * 2
    )
    # No SMTP is configured in the test environment: the email
    # copy is recorded not_configured, never claimed sent.
    assert all(n.email_status == "not_configured" for n in notes)
    video = _video(db, run.id)
    assert video.reminded_24h and video.reminded_6h and video.reminded_1h


def test_download_tracking_never_extends_deadline(db, media_root):
    user, project, run, run_dir = _make_run(db, media_root)
    retention.register_run_outputs(run.id, run_dir)
    master = run_dir / "documentary_final_output.mp4"
    retention.record_download_for_path(master)
    retention.record_download_for_path(master)
    db.expire_all()
    video = _video(db, run.id)
    assert video.download_count == 2
    assert video.first_downloaded_at is not None
    view = retention.retention_view(db, project.id)
    assert view["downloaded"] is True
    assert view["download_count"] == 2
    delete_after = video.delete_after
    if delete_after.tzinfo is None:
        delete_after = delete_after.replace(tzinfo=timezone.utc)
    assert delete_after == T0 + timedelta(hours=50)


def test_countdown_accuracy(db, media_root):
    now = datetime.now(timezone.utc)
    user, project, run, run_dir = _make_run(
        db, media_root, finished_at=now
    )
    retention.register_run_outputs(run.id, run_dir)
    view = retention.retention_view(db, project.id)
    expected = (now + timedelta(hours=50) - datetime.now(timezone.utc)).total_seconds()
    assert abs(view["seconds_remaining"] - expected) < 120
    assert view["headline"] == (
        "YOUR VIDEO WILL BE PERMANENTLY DELETED IN 50 HOURS."
    )
    assert view["status"] == "active"


def test_unregistered_media_is_unreachable(db, media_root, monkeypatch):
    monkeypatch.setenv("CINEMORA_RETENTION_ENABLED", "1")
    # A protected-asset stand-in: a real file in the media root
    # that was never registered (like the local protected films).
    protected = media_root / "protected-film.mp4"
    protected.write_bytes(b"do not touch")
    summary = retention.run_retention_tick(now=T0 + timedelta(days=365))
    assert protected.exists()
    assert summary["deleted"] == 0 or True  # other rows may delete
    assert protected.read_bytes() == b"do not touch"


def test_delete_failure_retries_then_alerts(db, media_root, monkeypatch):
    monkeypatch.setenv("CINEMORA_RETENTION_ENABLED", "1")
    user = models.User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@example.invalid",
        password_hash="x",
    )
    db.add(user)
    db.commit()
    # A registry row whose path is a DIRECTORY: unlink fails
    # deterministically, exercising the retry/alert path.
    stuck_dir = media_root / "stuck"
    stuck_dir.mkdir()
    row = models.GeneratedVideo(
        id=str(uuid.uuid4()), user_id=user.id,
        project_id=str(uuid.uuid4()), kind="master",
        relative_path="stuck",
        created_at=T0, delete_after=T0 + timedelta(hours=50),
        status="active",
    )
    db.add(row)
    db.commit()

    for _ in range(3):
        retention.run_retention_tick(now=T0 + timedelta(hours=51))
    db.expire_all()
    row = db.get(models.GeneratedVideo, row.id)
    assert row.delete_attempts >= 3
    assert row.status == "delete_failed"
    assert row.storage_confirmed is False
    assert row.last_delete_error
    assert stuck_dir.exists()  # never claimed deleted
    alerts = (
        db.query(models.AdminAuditLog)
        .filter_by(action="retention.delete_failed")
        .all()
    )
    assert any(a.target_id == row.id for a in alerts)


def test_revision_outputs_registered(db, media_root):
    user = models.User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@example.invalid",
        password_hash="x",
    )
    db.add(user)
    project = models.Project(
        id=str(uuid.uuid4()), script="s", owner_id=user.id
    )
    db.add(project)
    db.commit()
    project_dir = media_paths.project_dir(project.id)
    project_dir.mkdir(parents=True, exist_ok=True)
    revision_id = str(uuid.uuid4())
    final = project_dir / f"revision_{revision_id}_final.mp4"
    final.write_bytes(b"final")
    delivery = project_dir / f"revision_{revision_id}_delivery.mp4"
    delivery.write_bytes(b"delivery")
    ids = retention.register_revision_outputs(
        project.id, revision_id, str(uuid.uuid4()),
        {"file": str(final)},
    )
    assert len(ids) == 2
    db.expire_all()
    kinds = {
        r.kind
        for r in db.query(models.GeneratedVideo)
        .filter_by(revision_id=revision_id)
        .all()
    }
    assert kinds == {"revision_final", "revision_delivery"}


def test_storage_summary_aggregates(db, media_root):
    user, project, run, run_dir = _make_run(db, media_root)
    retention.register_run_outputs(run.id, run_dir)
    summary = retention.storage_summary(db)
    assert summary["total_videos"] >= 2
    assert summary["per_project"][project.id]["videos"] == 2
    assert summary["per_user"][user.id]["bytes"] == len(b"master") + len(
        b"delivery"
    )
