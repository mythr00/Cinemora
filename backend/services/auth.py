"""Accounts, passwords and sessions (Phase 8.2; design 02 §1).

The identity layer for the multi-user platform:

- Passwords: Argon2id hashes (argon2-cffi). Policy: >= 12 chars,
  not on the embedded common-password blocklist, <= 1024 chars
  (hashing-DoS bound). Only hashes are ever stored.
- Sessions: opaque 256-bit tokens, server-side in Redis
  (`session:<sha256(token)>` -> JSON), sliding 14-day TTL renewed
  on activity, httpOnly cookie on the web path, Bearer header for
  non-browser clients. Only the token's SHA-256 is stored, so a
  Redis dump yields no usable session. Revocation is instant —
  the property that ruled out JWTs (02 §1.2).
- The founder/operator bridge: during the dual-auth window the
  legacy operator token resolves to the founder principal
  (`resolve_operator_principal`). If a database has no super_admin
  yet (fresh dev/test databases), one is auto-provisioned with an
  unguessable password hash so owner_id always references a real
  user; scripts/seed_founder.py --reset-password sets its first
  usable password.
- Password reset: single-use token, SHA-256 + 1 h expiry on the
  user row; delivery via services/mailer.py, whose reported mode
  is truthful (no "email sent" claim when nothing was sent).

Redis access is lazy and seam-able (`set_redis_client`) so tests
can inject fakeredis; session resolution fails CLOSED (returns
None) on any Redis error.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

logger = logging.getLogger(__name__)

SESSION_COOKIE = "cinemora_session"
SESSION_TTL_SECONDS = 14 * 24 * 3600
_SESSION_RENEW_AFTER = 3600  # slide the TTL at most hourly
RESET_TOKEN_TTL = timedelta(hours=1)
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024

FOUNDER_EMAIL_DEFAULT = "mirthdraught@gmail.com"

_hasher = PasswordHasher()

# A short embedded blocklist of the most common passwords (offline
# by design — 02 §1.2 leaves the list source to build time; the
# rule is what matters). Registration rejects these outright.
COMMON_PASSWORDS = frozenset(
    {
        "password", "password1", "password123", "123456", "12345678",
        "123456789", "1234567890", "qwerty", "qwerty123", "letmein",
        "admin", "administrator", "welcome", "welcome123", "iloveyou",
        "monkey", "dragon", "football", "baseball", "sunshine",
        "master", "shadow", "superman", "batman", "trustno1",
        "111111", "000000", "abc123", "1q2w3e4r", "qazwsx",
        "passw0rd", "p@ssw0rd", "changeme", "default", "secret",
        "cinemora", "cinemora123", "documentary", "film1234",
    }
)


@dataclass(frozen=True)
class Principal:
    """The authenticated identity attached to a request."""

    user_id: str
    email: str
    role: str
    auth_method: str  # "session" | "operator_token" | "api_key"

    # Developer API (Phase N): set only when auth_method is
    # "api_key" — the resolved key's id and its granted scopes, so
    # route guards can enforce scope server-side. Sessions and the
    # operator token carry no key and the full scope set implicitly.
    api_key_id: str | None = None
    scopes: tuple = ()

    @property
    def is_admin(self) -> bool:
        return self.role in ("admin", "super_admin")


# ---------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, ValueError):
        return False


def password_policy_error(password: str) -> str | None:
    """None when acceptable, else a human-readable reason."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return (
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        return "Password is too long."
    if password.lower() in COMMON_PASSWORDS:
        return "That password is on the common-password blocklist."
    return None


# ---------------------------------------------------------------------
# Redis (lazy, seam-able)
# ---------------------------------------------------------------------

_redis_client = None


def _redis():
    global _redis_client
    if _redis_client is None:
        from redis import Redis

        _redis_client = Redis.from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
        )
    return _redis_client


def set_redis_client(client) -> None:
    """Test seam: inject a Redis-compatible client (fakeredis)."""
    global _redis_client
    _redis_client = client


def reset_redis_client() -> None:
    global _redis_client
    _redis_client = None


def _session_key(token: str) -> str:
    digest = hashlib.sha256(token.encode()).hexdigest()
    return f"session:{digest}"


def _user_sessions_key(user_id: str) -> str:
    return f"user_sessions:{user_id}"


# ---------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------


def create_session(user_id: str) -> str:
    """Issue a session; returns the raw token (shown to the client
    once, stored only as SHA-256 server-side)."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    payload = json.dumps(
        {"user_id": user_id, "created_at": now, "last_seen_at": now}
    )
    client = _redis()
    client.set(_session_key(token), payload, ex=SESSION_TTL_SECONDS)
    client.sadd(_user_sessions_key(user_id), _session_key(token))
    return token


def resolve_session(token: str) -> str | None:
    """Token -> user_id, sliding the TTL on activity. Any Redis
    error fails closed (None)."""
    if not token:
        return None
    try:
        client = _redis()
        raw = client.get(_session_key(token))
        if raw is None:
            return None
        payload = json.loads(raw)
        now = time.time()
        if now - float(payload.get("last_seen_at", 0)) > _SESSION_RENEW_AFTER:
            payload["last_seen_at"] = now
            client.set(
                _session_key(token),
                json.dumps(payload),
                ex=SESSION_TTL_SECONDS,
            )
        return payload.get("user_id")
    except Exception:
        logger.warning("session resolution failed; treating as anonymous",
                       exc_info=True)
        return None


def revoke_session(token: str) -> None:
    if not token:
        return
    try:
        client = _redis()
        raw = client.get(_session_key(token))
        client.delete(_session_key(token))
        if raw:
            user_id = json.loads(raw).get("user_id")
            if user_id:
                client.srem(_user_sessions_key(user_id), _session_key(token))
    except Exception:
        logger.warning("session revocation failed", exc_info=True)


def revoke_all_sessions(user_id: str) -> int:
    """Revoke every session of a user (password change/reset,
    account disable). Returns how many session keys were deleted."""
    try:
        client = _redis()
        keys = client.smembers(_user_sessions_key(user_id))
        deleted = 0
        for key in keys:
            deleted += client.delete(key)
        client.delete(_user_sessions_key(user_id))
        return deleted
    except Exception:
        logger.warning("revoke_all_sessions failed", exc_info=True)
        return 0


# ---------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def founder_email() -> str:
    return normalize_email(
        os.environ.get("CINEMORA_FOUNDER_EMAIL", FOUNDER_EMAIL_DEFAULT)
    )


def ensure_founder_user(db):
    """The founder (super_admin) row, auto-provisioned if absent.

    Used by the operator-token bridge: a database with no accounts
    yet (fresh dev/test DB) gains a founder with an unguessable
    password hash, so every owner_id can reference a real user.
    scripts/seed_founder.py is the deliberate path on real data and
    sets the first usable password via its password file.
    """
    import models

    founder = (
        db.query(models.User)
        .filter(models.User.role == "super_admin")
        .order_by(models.User.created_at)
        .first()
    )
    if founder is not None:
        return founder
    founder = models.User(
        id=str(uuid.uuid4()),
        email=founder_email(),
        password_hash=hash_password(secrets.token_urlsafe(48)),
        display_name="Founder",
        role="super_admin",
        status="active",
    )
    db.add(founder)
    db.commit()
    db.refresh(founder)
    return founder


# Fixed id for the synthetic operator principal used when the
# accounts tables don't exist yet (legacy fixture databases that
# create_all only the production tables). On any migrated database
# the users table exists and this fallback never fires.
OPERATOR_FALLBACK_ID = "00000000-0000-4000-8000-000000000001"


def resolve_operator_principal(db) -> Principal:
    """Dual-auth window (02 §1.4): the legacy operator token IS the
    founder. The window is CLOSED by default (E-21/B-05) — callers
    (api_v1.require_user) reach this only when the deployment has
    deliberately set CINEMORA_ALLOW_OPERATOR_TOKEN=1."""
    try:
        founder = ensure_founder_user(db)
        return Principal(
            user_id=founder.id,
            email=founder.email,
            role=founder.role,
            auth_method="operator_token",
        )
    except Exception:
        db.rollback()
        logger.warning(
            "operator principal: accounts tables unavailable; "
            "falling back to the synthetic operator principal",
            exc_info=True,
        )
        return Principal(
            user_id=OPERATOR_FALLBACK_ID,
            email=founder_email(),
            role="super_admin",
            auth_method="operator_token",
        )


def principal_for_user(user, auth_method: str) -> Principal:
    return Principal(
        user_id=user.id,
        email=user.email,
        role=user.role,
        auth_method=auth_method,
    )


# ---------------------------------------------------------------------
# Password reset tokens
# ---------------------------------------------------------------------


def issue_reset_token(db, user) -> str:
    """Create a single-use reset token; only its SHA-256 is stored."""
    token = secrets.token_urlsafe(32)
    user.password_reset_token_hash = hashlib.sha256(
        token.encode()
    ).hexdigest()
    user.password_reset_expires_at = datetime.now(timezone.utc) + RESET_TOKEN_TTL
    db.commit()
    return token


def consume_reset_token(db, token: str, new_password: str):
    """Validate + consume a reset token and set the new password.
    Returns the user, or None when the token is unknown/expired."""
    import models

    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    user = (
        db.query(models.User)
        .filter(models.User.password_reset_token_hash == digest)
        .one_or_none()
    )
    if user is None:
        return None
    expires = user.password_reset_expires_at
    if expires is None:
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        return None
    user.password_hash = hash_password(new_password)
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None
    db.commit()
    revoke_all_sessions(user.id)
    return user
