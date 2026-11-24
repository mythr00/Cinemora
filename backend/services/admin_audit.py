"""Append-only admin audit log (Phase 7; design admin/03 §3).

One writer, no updater, no deleter: `record()` inserts a row into
`admin_audit_log`; `query()` reads rows back for the M7 viewer.
Every WRITE/CONSEQUENTIAL admin action, every admin sign-in, and
every failed admin authorisation attempt is recorded here with the
acting principal's identity, role and auth method.

`record()` is best-effort by contract: an audit failure must never
fail the action it describes (the same discipline as run_metrics),
so it swallows its own errors after logging them. Callers that need
the entry in the same transaction as a DB effect pass their own
session via `record(..., db=session, commit=False)` and commit
themselves; the default path opens a short session of its own.

`detail` must never carry secrets — callers pass action parameters
and before/after values only, and `record()` additionally drops a
small denylist of key names defensively.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Keys that must never land in an audit detail payload, even if a
# caller passes them by mistake.
_SECRET_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "token",
        "session_token",
        "api_key",
        "x-api-key",
        "authorization",
        "secret",
    }
)


def _clean_detail(detail: dict | None) -> dict | None:
    if not detail:
        return None
    return {
        key: value
        for key, value in detail.items()
        if key.lower() not in _SECRET_KEYS
    }


def record(
    principal,
    action: str,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict | None = None,
    result: str = "success",
    error: str | None = None,
    db=None,
    commit: bool = True,
) -> str | None:
    """Insert one audit row. Returns the row id, or None when the
    write failed (best-effort: the failure is logged, never raised).

    `principal` is a services.auth.Principal (or any object with
    email/role/auth_method attributes); a plain string is accepted
    and treated as the identity of an unauthenticated attempt.
    """
    from database import SessionLocal
    from models import AdminAuditLog

    if isinstance(principal, str) or principal is None:
        identity = principal or "unknown"
        role = "anonymous"
        auth_method = "none"
    else:
        identity = principal.email
        role = principal.role
        auth_method = principal.auth_method

    row = AdminAuditLog(
        id=str(uuid.uuid4()),
        actor_identity=identity,
        actor_role=role,
        auth_method=auth_method,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        detail=_clean_detail(detail),
        result=result,
        error=error,
        created_at=datetime.now(timezone.utc),
    )
    own_session = db is None
    session = db or SessionLocal()
    try:
        session.add(row)
        if commit:
            session.commit()
        else:
            session.flush()
        return row.id
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        logger.warning(
            "admin_audit: failed to record %s (best-effort)", action,
            exc_info=True,
        )
        return None
    finally:
        if own_session:
            session.close()


def query(
    db,
    *,
    actor: str | None = None,
    action: str | None = None,
    target_id: str | None = None,
    result: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Read the log back (M7 viewer): newest first, filterable by
    actor / action / target / result. Returns entries + total."""
    from models import AdminAuditLog

    q = db.query(AdminAuditLog)
    if actor:
        q = q.filter(AdminAuditLog.actor_identity == actor)
    if action:
        q = q.filter(AdminAuditLog.action == action)
    if target_id:
        q = q.filter(AdminAuditLog.target_id == str(target_id))
    if result:
        q = q.filter(AdminAuditLog.result == result)
    total = q.count()
    rows = (
        q.order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc())
        .offset(max(0, offset))
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return {
        "total": total,
        "entries": [
            {
                "id": row.id,
                "actor_identity": row.actor_identity,
                "actor_role": row.actor_role,
                "auth_method": row.auth_method,
                "action": row.action,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "detail": row.detail,
                "result": row.result,
                "error": row.error,
                "created_at": row.created_at.isoformat()
                if row.created_at
                else None,
            }
            for row in rows
        ],
    }
