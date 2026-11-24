"""Per-user Developer API keys (founder priority #16; gap-audit
doc 06 Phase N).

Security model, as implemented here:

- A key secret is `cmk_` + 43 urlsafe characters from
  `secrets.token_urlsafe(32)`. It is returned to its owner exactly
  once, from `create_key`. Only the secret's SHA-256 (`key_hash`,
  the password-reset token pattern in services/auth.py) and its
  first 12 characters (`key_prefix`, the lookup key and the only
  part ever displayed again) are stored.
- Resolution never logs a presented key, compares hashes with
  `hmac.compare_digest`, and fails closed: unknown prefix, hash
  mismatch, revoked key, expired key and disabled owner account
  all resolve to None.
- Scopes are drawn from {"read", "write"} and are enforced by the
  public-API route guards (api_public.py), not by possession of
  the key alone.
- Usage (`request_count`, `last_used_at`) is written by
  `record_use` from real authenticated public-API traffic only.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

KEY_PREFIX = "cmk_"
DISPLAY_PREFIX_LENGTH = 12
VALID_SCOPES = frozenset({"read", "write"})
MAX_KEYS_PER_USER = 25

# The deployment-wide per-key request budget per minute on
# /api/public/v1, used when a key carries no explicit limit.
DEFAULT_RATE_LIMIT_ENV = "CINEMORA_PUBLIC_API_RATE_LIMIT"
DEFAULT_RATE_LIMIT = 60
MIN_RATE_LIMIT = 1
MAX_RATE_LIMIT = 600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; the codebase convention is
    to read them as UTC before comparing (services/auth.py)."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def default_rate_limit() -> int:
    try:
        value = int(os.environ.get(DEFAULT_RATE_LIMIT_ENV, ""))
    except ValueError:
        return DEFAULT_RATE_LIMIT
    return min(MAX_RATE_LIMIT, max(MIN_RATE_LIMIT, value))


def validate_scopes(scopes) -> list[str]:
    """Normalise a requested scope list, or raise ValueError."""
    if not isinstance(scopes, (list, tuple)) or not scopes:
        raise ValueError("At least one scope is required.")
    cleaned = sorted({str(scope).strip() for scope in scopes})
    unknown = [scope for scope in cleaned if scope not in VALID_SCOPES]
    if unknown:
        raise ValueError(
            "Unknown scope(s): "
            + ", ".join(unknown)
            + ". Valid scopes are: read, write."
        )
    return cleaned


def key_view(row) -> dict:
    """The stored, non-secret view of a key — the only shape any
    list/read endpoint ever returns."""
    return {
        "id": row.id,
        "name": row.name,
        "key_prefix": row.key_prefix,
        "scopes": list(row.scopes or []),
        "rate_limit_per_minute": (
            row.rate_limit_per_minute
            if row.rate_limit_per_minute is not None
            else default_rate_limit()
        ),
        "rate_limit_is_default": row.rate_limit_per_minute is None,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
        "last_used_at": row.last_used_at,
        "request_count": row.request_count,
        "status": (
            "revoked"
            if row.revoked_at is not None
            else "expired"
            if row.expires_at is not None
            and _aware(row.expires_at) <= _utcnow()
            else "active"
        ),
    }


def create_key(
    db,
    user_id: str,
    *,
    name: str,
    scopes,
    expires_at: datetime | None = None,
    rate_limit_per_minute: int | None = None,
) -> tuple[object, str]:
    """Create a key for `user_id`. Returns (row, secret) — the
    secret is available ONLY here; nothing stored can recover it."""
    from models import ApiKey

    cleaned_scopes = validate_scopes(scopes)
    if rate_limit_per_minute is not None and not (
        MIN_RATE_LIMIT <= rate_limit_per_minute <= MAX_RATE_LIMIT
    ):
        raise ValueError(
            f"rate_limit_per_minute must be between {MIN_RATE_LIMIT} "
            f"and {MAX_RATE_LIMIT}."
        )
    active = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
        .count()
    )
    if active >= MAX_KEYS_PER_USER:
        raise ValueError(
            f"Key limit reached ({MAX_KEYS_PER_USER} active keys); "
            "revoke a key before creating another."
        )
    secret = KEY_PREFIX + secrets.token_urlsafe(32)
    row = ApiKey(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name=name,
        key_prefix=secret[:DISPLAY_PREFIX_LENGTH],
        key_hash=_hash_secret(secret),
        scopes=cleaned_scopes,
        rate_limit_per_minute=rate_limit_per_minute,
        created_at=_utcnow(),
        expires_at=expires_at,
        request_count=0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, secret


def list_keys(db, user_id: str) -> list:
    from models import ApiKey

    return (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user_id)
        .order_by(ApiKey.created_at.desc())
        .all()
    )


def get_owned_key(db, user_id: str, key_id: str):
    """The caller's own key row, or None (another user's key is
    indistinguishable from a missing one)."""
    from models import ApiKey

    return (
        db.query(ApiKey)
        .filter(ApiKey.id == key_id, ApiKey.user_id == user_id)
        .one_or_none()
    )


def revoke_key(db, user_id: str, key_id: str):
    """Revoke the caller's key (idempotent on an already-revoked
    key). Returns the row, or None when it is not theirs."""
    row = get_owned_key(db, user_id, key_id)
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = _utcnow()
        db.commit()
        db.refresh(row)
    return row


def resolve_key(db, presented: str):
    """Resolve a presented key secret to (key_row, owner_user), or
    None. Fails closed on every anomaly; never logs the secret."""
    if not presented or not presented.startswith(KEY_PREFIX):
        return None
    from models import ApiKey, User

    prefix = presented[:DISPLAY_PREFIX_LENGTH]
    digest = _hash_secret(presented)
    candidates = (
        db.query(ApiKey).filter(ApiKey.key_prefix == prefix).all()
    )
    now = _utcnow()
    for row in candidates:
        if not hmac.compare_digest(row.key_hash, digest):
            continue
        if row.revoked_at is not None:
            return None
        if row.expires_at is not None and _aware(row.expires_at) <= now:
            return None
        user = db.get(User, row.user_id)
        if user is None or user.status != "active":
            return None
        return row, user
    return None


def record_use(db, key_id: str) -> None:
    """Record one authenticated public-API request against a key.
    Best-effort: a usage-write failure must never fail the request
    it describes (the admin_audit discipline)."""
    from models import ApiKey

    try:
        row = db.get(ApiKey, key_id)
        if row is None:
            return
        row.request_count = (row.request_count or 0) + 1
        row.last_used_at = _utcnow()
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "api_keys: failed to record key use (best-effort)",
            exc_info=True,
        )
