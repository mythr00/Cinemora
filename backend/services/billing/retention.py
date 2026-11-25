"""Generated-video retention (Phase 8; founder directive §21).

Every successfully generated video is registered at completion
with delete_after = created_at + 50 hours exactly. A background
sweeper (dispatcher leader loop) deletes at the deadline —
never before — and claims deletion only after verifying the
file no longer exists (storage_confirmed).

Safety rails, in order:
1. The sweeper deletes ONLY rows in generated_videos. The
   registry starts empty and only completion hooks add rows, so
   pre-existing media — including the protected local films —
   is unreachable.
2. Deletion additionally requires CINEMORA_RETENTION_ENABLED=1
   (reminders and registration work regardless; they delete
   nothing).
3. Every path is re-derived from the media root at deletion
   time and containment-checked; a row whose path escapes the
   media root is refused and reported, never followed.
4. Only video files are removed (the registered file and, for
   run outputs, the run directory's other .mp4 intermediates).
   Scripts, manifests, captions, thumbnails, research and
   metadata are preserved — a project is never deleted because
   its video expired.

Reminders fire at 24h / 6h / 1h remaining as in-app
notifications; the email copy is attempted through the mailer
seam and recorded honestly (not_configured while no SMTP
exists). Downloads are tracked on the registry row and never
extend the deadline.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import update

import models
from database import SessionLocal
from services import media_paths
from services.billing import notifications

RETENTION_HOURS = 50
MAX_DELETE_ATTEMPTS = 3

_WARNING_HEADLINE = "YOUR VIDEO WILL BE PERMANENTLY DELETED IN 50 HOURS."


def retention_enabled() -> bool:
    return os.environ.get("CINEMORA_RETENTION_ENABLED", "").strip() in (
        "1",
        "true",
        "yes",
    )


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _session():
    return SessionLocal()


# ---------------------------------------------------------------------
# Registration (completion hooks)
# ---------------------------------------------------------------------


def _register(db, *, user_id, project_id, run_id, revision_id, kind,
              abs_path: Path, created_at):
    root = media_paths.media_root()
    try:
        relative = abs_path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        print(
            "WARNING: retention registration skipped for "
            f"{abs_path}: outside the media root"
        )
        return None
    relative_str = relative.as_posix()
    existing = (
        db.query(models.GeneratedVideo)
        .filter_by(relative_path=relative_str)
        .one_or_none()
    )
    if existing is not None:
        return existing
    anchor = _aware(created_at) or datetime.now(timezone.utc)
    row = models.GeneratedVideo(
        id=str(uuid.uuid4()),
        user_id=user_id,
        project_id=str(project_id),
        run_id=str(run_id) if run_id else None,
        revision_id=str(revision_id) if revision_id else None,
        kind=kind,
        media_key=None,
        relative_path=relative_str,
        size_bytes=abs_path.stat().st_size if abs_path.exists() else None,
        created_at=anchor,
        delete_after=anchor + timedelta(hours=RETENTION_HOURS),
        status="active",
    )
    db.add(row)
    db.flush()
    return row


def register_run_outputs(run_id, artefact_dir) -> list[str]:
    """Register a completed production run's master + delivery
    videos. Called from jobs at the completion point (fail-soft:
    registration problems never fail a production)."""
    registered: list[str] = []
    if not run_id:
        return registered
    db = _session()
    try:
        run = db.get(models.ProductionRun, str(run_id))
        if run is None:
            return registered
        project = db.get(models.Project, run.project_id)
        owner_id = run.owner_id or (project.owner_id if project else None)
        if not owner_id:
            return registered
        artefact_dir = Path(artefact_dir)
        for filename, kind in (
            ("documentary_final_output.mp4", "master"),
            ("documentary_delivery.mp4", "delivery"),
        ):
            path = artefact_dir / filename
            if path.exists():
                row = _register(
                    db,
                    user_id=owner_id,
                    project_id=run.project_id,
                    run_id=run.id,
                    revision_id=None,
                    kind=kind,
                    abs_path=path,
                    created_at=run.finished_at,
                )
                if row is not None:
                    registered.append(row.id)
        db.commit()
        return registered
    except Exception as error:
        try:
            db.rollback()
        except Exception:
            pass
        print(f"WARNING: retention registration failed: {error}")
        return registered
    finally:
        db.close()


def register_revision_outputs(project_id, revision_id, run_id,
                              export: dict) -> list[str]:
    """Register a completed editor revision export (final +
    delivery). Fail-soft like register_run_outputs."""
    registered: list[str] = []
    db = _session()
    try:
        project = db.get(models.Project, str(project_id))
        if project is None or not project.owner_id:
            return registered
        final_path = Path(export.get("file") or "")
        candidates = [(final_path, "revision_final")]
        delivery_path = (
            media_paths.project_dir(project_id)
            / f"revision_{revision_id}_delivery.mp4"
        )
        candidates.append((delivery_path, "revision_delivery"))
        for path, kind in candidates:
            if path and str(path) != "" and path.exists():
                row = _register(
                    db,
                    user_id=project.owner_id,
                    project_id=project_id,
                    run_id=run_id,
                    revision_id=revision_id,
                    kind=kind,
                    abs_path=path,
                    created_at=datetime.now(timezone.utc),
                )
                if row is not None:
                    registered.append(row.id)
        db.commit()
        return registered
    except Exception as error:
        try:
            db.rollback()
        except Exception:
            pass
        print(f"WARNING: revision retention registration failed: {error}")
        return registered
    finally:
        db.close()


# ---------------------------------------------------------------------
# Downloads + user-facing view
# ---------------------------------------------------------------------


def record_download_for_path(abs_path) -> None:
    """Track a served generated video's download (fail-soft;
    called from serve_media). Downloads never extend the
    deadline."""
    try:
        root = media_paths.media_root()
        relative = Path(abs_path).resolve().relative_to(root.resolve())
    except Exception:
        return
    db = _session()
    try:
        row = (
            db.query(models.GeneratedVideo)
            .filter_by(relative_path=relative.as_posix())
            .one_or_none()
        )
        if row is None:
            return
        now = datetime.now(timezone.utc)
        row.download_count = int(row.download_count or 0) + 1
        if row.first_downloaded_at is None:
            row.first_downloaded_at = now
        row.last_downloaded_at = now
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def retention_view(db, project_id: str) -> dict | None:
    """The §21 warning data for a project's latest generated
    video: exact expiry, live remaining seconds, deletion and
    download status."""
    row = (
        db.query(models.GeneratedVideo)
        .filter_by(project_id=str(project_id))
        .order_by(models.GeneratedVideo.created_at.desc())
        .first()
    )
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    delete_after = _aware(row.delete_after)
    remaining = (
        max(0, int((delete_after - now).total_seconds()))
        if delete_after
        else 0
    )
    return {
        "video_id": row.id,
        "kind": row.kind,
        "status": row.status,
        "headline": _WARNING_HEADLINE,
        "created_at": _aware(row.created_at).isoformat()
        if row.created_at
        else None,
        "delete_after": delete_after.isoformat() if delete_after else None,
        "seconds_remaining": remaining if row.status == "active" else 0,
        "downloaded": (row.download_count or 0) > 0,
        "download_count": row.download_count or 0,
        "first_downloaded_at": _aware(row.first_downloaded_at).isoformat()
        if row.first_downloaded_at
        else None,
        "last_downloaded_at": _aware(row.last_downloaded_at).isoformat()
        if row.last_downloaded_at
        else None,
        "deleted_at": _aware(row.deleted_at).isoformat()
        if row.deleted_at
        else None,
        "storage_confirmed": bool(row.storage_confirmed),
    }


# ---------------------------------------------------------------------
# Sweeper (dispatcher leader loop)
# ---------------------------------------------------------------------


def _send_reminder(db, row, label: str, flag: str) -> None:
    user = db.get(models.User, row.user_id)
    delete_after = _aware(row.delete_after)
    notifications.create_notification(
        db,
        user_id=row.user_id,
        kind=f"retention_{label}",
        title=(
            "Your generated video will be permanently deleted "
            f"in {label.replace('_', ' ')}"
        ),
        body=(
            f"{_WARNING_HEADLINE}\n\n"
            "One of your generated videos reaches its 50-hour "
            "retention deadline at "
            f"{delete_after.isoformat() if delete_after else ''}. "
            "Download it before then to keep a copy. Your project, "
            "script and history are not affected."
        ),
        project_id=row.project_id,
        generated_video_id=row.id,
        send_email=True,
        user_email=user.email if user else None,
    )
    setattr(row, flag, True)


def _delete_row_files(db, row) -> None:
    """Delete one registered video (+ run-dir .mp4 intermediates
    for run outputs) and confirm storage. Raises on failure."""
    root = media_paths.media_root().resolve()
    target = (root / row.relative_path).resolve()
    if target != root and root not in target.parents:
        raise ValueError(
            f"refusing to delete outside the media root: {target}"
        )
    targets = [target]
    if row.kind in ("master", "delivery") and row.run_id:
        run_dir = target.parent
        if run_dir.exists():
            targets.extend(
                sorted(p for p in run_dir.glob("*.mp4") if p != target)
            )
    for path in targets:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    if target.exists():
        raise OSError(f"file still exists after deletion: {target}")
    row.status = "deleted"
    row.deleted_at = datetime.now(timezone.utc)
    row.storage_confirmed = True
    row.last_delete_error = None


def run_retention_tick(now: datetime | None = None) -> dict:
    """One sweeper pass: fire due reminders; delete expired
    videos when (and only when) retention is enabled. Never
    raises."""
    now = now or datetime.now(timezone.utc)
    summary = {"reminded": 0, "deleted": 0, "failed": 0, "skipped": 0}
    db = _session()
    try:
        active = (
            db.query(models.GeneratedVideo)
            .filter_by(status="active")
            .all()
        )
        for row in active:
            delete_after = _aware(row.delete_after)
            if delete_after is None:
                continue
            remaining = (delete_after - now).total_seconds()
            try:
                if remaining <= 24 * 3600 and not row.reminded_24h:
                    _send_reminder(db, row, "24h", "reminded_24h")
                    summary["reminded"] += 1
                if remaining <= 6 * 3600 and not row.reminded_6h:
                    _send_reminder(db, row, "6h", "reminded_6h")
                    summary["reminded"] += 1
                if remaining <= 1 * 3600 and not row.reminded_1h:
                    _send_reminder(db, row, "1h", "reminded_1h")
                    summary["reminded"] += 1
                db.commit()
            except Exception:
                db.rollback()

            if remaining > 0:
                continue
            if not retention_enabled():
                summary["skipped"] += 1
                continue
            # Claim the row so two sweepers can never delete the
            # same video twice.
            claimed = db.execute(
                update(models.GeneratedVideo)
                .where(
                    models.GeneratedVideo.id == row.id,
                    models.GeneratedVideo.status == "active",
                )
                .values(status="deleting")
            )
            db.commit()
            if claimed.rowcount != 1:
                continue
            try:
                _delete_row_files(db, row)
                db.commit()
                summary["deleted"] += 1
            except Exception as error:
                db.rollback()
                row = db.get(models.GeneratedVideo, row.id)
                if row is not None:
                    row.delete_attempts = int(row.delete_attempts or 0) + 1
                    row.last_delete_error = str(error)[:500]
                    row.status = (
                        "delete_failed"
                        if row.delete_attempts >= MAX_DELETE_ATTEMPTS
                        else "active"
                    )
                    db.commit()
                    if row.status == "delete_failed":
                        _alert_admin_delete_failed(row)
                summary["failed"] += 1
        return summary
    except Exception as error:
        print(f"WARNING: retention tick failed: {error}")
        return summary
    finally:
        db.close()


def _alert_admin_delete_failed(row) -> None:
    try:
        from services import admin_audit

        admin_audit.record(
            "system",
            "retention.delete_failed",
            target_type="generated_video",
            target_id=row.id,
            detail={
                "project_id": row.project_id,
                "user_id": row.user_id,
                "attempts": row.delete_attempts,
                "error": row.last_delete_error,
            },
            result="failure",
            error=row.last_delete_error,
        )
    except Exception:
        pass


def storage_summary(db) -> dict:
    """§21(F) admin storage aggregates over the registry."""
    rows = db.query(models.GeneratedVideo).all()
    now = datetime.now(timezone.utc)
    summary = {
        "total_videos": len(rows),
        "total_bytes": sum(r.size_bytes or 0 for r in rows),
        "active": 0,
        "deleted": 0,
        "delete_failed": 0,
        "expired_awaiting_deletion": 0,
        "failed_deletions": [],
        "per_user": {},
        "per_project": {},
    }
    for row in rows:
        if row.status == "active":
            summary["active"] += 1
            if _aware(row.delete_after) and _aware(row.delete_after) <= now:
                summary["expired_awaiting_deletion"] += 1
        elif row.status == "deleted":
            summary["deleted"] += 1
        elif row.status == "delete_failed":
            summary["delete_failed"] += 1
            summary["failed_deletions"].append(
                {
                    "video_id": row.id,
                    "project_id": row.project_id,
                    "user_id": row.user_id,
                    "attempts": row.delete_attempts,
                    "error": row.last_delete_error,
                }
            )
        user_bucket = summary["per_user"].setdefault(
            row.user_id, {"videos": 0, "bytes": 0}
        )
        user_bucket["videos"] += 1
        user_bucket["bytes"] += row.size_bytes or 0
        project_bucket = summary["per_project"].setdefault(
            row.project_id, {"videos": 0, "bytes": 0}
        )
        project_bucket["videos"] += 1
        project_bucket["bytes"] += row.size_bytes or 0
    return summary
