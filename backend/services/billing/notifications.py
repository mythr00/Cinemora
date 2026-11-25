"""In-app notifications (Phase 8; directive §21 D).

The platform's first notification store. Created for the
retention reminders; the email copy goes through the existing
mailer seam and the outcome is recorded honestly — the mailer
returns "manual" while no SMTP is configured, which is recorded
as email_status="not_configured", never as sent.
"""

from __future__ import annotations

import uuid

import models


def create_notification(
    db,
    *,
    user_id: str,
    kind: str,
    title: str,
    body: str | None = None,
    project_id: str | None = None,
    generated_video_id: str | None = None,
    send_email: bool = False,
    user_email: str | None = None,
) -> models.UserNotification:
    email_status = None
    if send_email:
        email_status = "not_configured"
        if user_email:
            try:
                from services import mailer

                result = mailer.send_email(user_email, title, body or "")
                email_status = "sent" if result == "smtp" else "not_configured"
            except Exception:
                email_status = "failed"
    row = models.UserNotification(
        id=str(uuid.uuid4()),
        user_id=user_id,
        kind=kind,
        title=title,
        body=body,
        project_id=project_id,
        generated_video_id=generated_video_id,
        email_status=email_status,
    )
    db.add(row)
    db.flush()
    return row


def unread_count(db, user_id: str) -> int:
    return (
        db.query(models.UserNotification)
        .filter_by(user_id=user_id, read_at=None)
        .count()
    )
