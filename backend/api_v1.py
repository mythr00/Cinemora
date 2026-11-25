"""API v1 — the studio contract (Stage 4).

Mounted from main.py under /api/v1. The six legacy routes in
main.py are untouched and keep their original shapes; everything in
this module follows the v1 contract documented in
~/workspace/cinemora-audit/API_CONTRACT.md:

- Account auth (AUD-11, Phase 8.2): every route except
  GET /api/v1/health requires an authenticated principal —
  a server-side session (cookie or Bearer, services/auth.py) or,
  only when a deployment deliberately opens the dual-auth window
  with CINEMORA_ALLOW_OPERATOR_TOKEN=1 (closed by default, E-21),
  the legacy X-API-Key operator token
  (constant-time compare), which resolves to the founder principal.
  When
  neither is available the API fails CLOSED: 503
  "api_not_configured" when the token is unconfigured, 401
  otherwise, never open. Project/run routes are owner-scoped:
  another user's resource is a 404, never a 403 (design 02 §4.1).
- Consistent error shape: {"error": {"code", "message"}} for every
  error raised under /api/v1 (auth, validation, domain errors).
- Script payloads are capped at 60,000 characters (422 beyond).
- POST routes are rate limited via a Redis-backed sliding window
  (B-12; in-process fallback only when Redis is unreachable,
  logged loudly) (default 30/min per
  token identity, CINEMORA_API_RATE_LIMIT overrides).
- Media is served ONLY through recorded artefacts (the plan
  document's media index): an opaque key resolves to a file that a
  run actually recorded, verified to live under the project's media
  directory. Raw paths and bare UUIDs serve nothing.

The plan-approval gate (require_plan_approval) works at the run
lifecycle boundary: submission parks the run after an analysis-only
phase (status "awaiting_approval", plan.json persisted), and
POST /plan/approve releases the SAME run into the full pipeline,
whose analysis stage reuses the persisted story-understanding cache
instead of re-analysing.
"""

from __future__ import annotations

import copy
import hmac
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

import jobs
from database import SessionLocal
from models import Plan, Project, User, UserPreference, UserSubscription
from services import admin_audit
from services import auth as auth_service
from services import plans as plans_service
from services import (
    asset_library,
    collaboration,
    editor,
    editor_locks,
    editor_studio,
    genre_packs,
    localization,
    media_paths,
    narration,
    plan_document,
    production_config,
    project_intelligence,
    qa_report,
    research_studio,
    run_ledger,
    script_studio,
    templates as templates_service,
    thumbnail_studio,
    versioning,
    voice_documentary,
)
from services.atomic_write import write_json_atomic

MAX_SCRIPT_CHARS = 60_000

VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

router = APIRouter()


# ---------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------


class ApiError(Exception):
    """A v1 domain error: HTTP status + stable machine code."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def register_error_handlers(app):
    """Map errors under /api/v1 to the v1 shape; every other path
    keeps FastAPI's default shapes exactly (the legacy routes and
    their tests depend on them)."""

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, exc.message),
        )

    # Phase 8 billing rejections (insufficient credits, past-due
    # subscription, billing unavailable) raised from the run
    # ledger choke point, below every route — one handler maps
    # them for every surface that can submit a generation.
    from services.billing.generation_billing import BillingRejected

    @app.exception_handler(BillingRejected)
    async def _billing_rejected_handler(request: Request, exc: BillingRejected):
        if request.url.path.startswith(("/api/v1", "/api/public/")):
            return JSONResponse(
                status_code=exc.status_code,
                content=_error_body(exc.error_code, str(exc)),
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith(("/api/v1", "/api/public/")):
            message = exc.detail if isinstance(exc.detail, str) else "Request failed."
            return JSONResponse(
                status_code=exc.status_code,
                content=_error_body("http_error", message),
                headers=exc.headers,
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        if request.url.path.startswith(("/api/v1", "/api/public/")):
            errors = exc.errors()
            first = errors[0] if errors else {}
            location = ".".join(str(part) for part in first.get("loc", []))
            message = first.get("msg", "Request validation failed.")
            if location:
                message = f"{location}: {message}"
            return JSONResponse(
                status_code=422,
                content=_error_body("validation_error", message),
            )
        return JSONResponse(
            status_code=422, content={"detail": exc.errors()}
        )


# ---------------------------------------------------------------------
# Auth + rate limiting (AUD-11)
# ---------------------------------------------------------------------


def _operator_token_open() -> bool:
    """The dual-auth window (design 02 §1.4; E-21/B-05): CLOSED by
    default — the legacy operator token resolves to the founder
    super_admin principal ONLY when the deployment deliberately
    opens the window with CINEMORA_ALLOW_OPERATOR_TOKEN=1 (the
    local preview launcher does exactly that, explicitly). Any
    other value, or the variable being unset, keeps it closed."""
    return os.environ.get("CINEMORA_ALLOW_OPERATOR_TOKEN", "0") == "1"


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization") or ""
    if header.startswith("Bearer "):
        return header[len("Bearer "):].strip() or None
    return None


def _session_token_from(request: Request) -> str | None:
    # Deliberate credentials win over ambient ones: an explicit
    # Authorization header first, the session cookie second. A
    # client holding both (a browser that also runs API tooling, a
    # test client with a populated cookie jar) must get the
    # identity it asked for, not whichever cookie is ambient.
    return _bearer_token(request) or request.cookies.get(
        auth_service.SESSION_COOKIE
    )


def _principal_for_session_token(token: str):
    """Resolve a session token to its Principal, or None (unknown /
    expired / revoked token, disabled account, or the accounts
    tables unavailable in a legacy fixture DB)."""
    user_id = auth_service.resolve_session(token)
    if not user_id:
        return None
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is not None and user.status == "active":
            return auth_service.principal_for_user(user, "session")
    except Exception:
        db.rollback()
    finally:
        db.close()
    return None


def require_user(request: Request) -> auth_service.Principal:
    """Resolve the request's principal (Phase 8.2).

    Order: (1) Bearer session token, (2) the legacy operator token
    while the dual-auth window is open -> the founder principal,
    (3) session cookie. Disabled accounts are rejected on every
    path. Fails closed with the pre-8.2 contract: 503
    api_not_configured when the operator token is unconfigured and
    no session resolves, 401 otherwise.
    """
    # Credential precedence: deliberate credentials before ambient
    # ones — (1) Bearer session token, (2) the operator X-API-Key
    # while the dual-auth window is open, (3) the session cookie.
    # A request presenting a valid deliberate credential gets that
    # identity even when an ambient cookie for someone else rides
    # along (shared browsers, test cookie jars). A deliberate
    # credential that is PRESENT but invalid (revoked session,
    # disabled account, retired/wrong operator token) fails the
    # request: silently downgrading it to the ambient cookie's
    # identity would turn "your session was revoked" into "you are
    # suddenly someone else", which is never the caller's intent.
    bearer = _bearer_token(request)
    if bearer:
        principal = _principal_for_session_token(bearer)
        if principal is not None:
            return principal
        raise ApiError(
            401,
            "unauthorized",
            "The presented session is invalid, expired or revoked.",
        )

    configured = os.environ.get("CINEMORA_API_TOKEN") or ""
    provided = request.headers.get("X-API-Key") or ""
    if provided and configured:
        if _operator_token_open() and hmac.compare_digest(
            provided, configured
        ):
            db = SessionLocal()
            try:
                return auth_service.resolve_operator_principal(db)
            finally:
                db.close()
        raise ApiError(
            401,
            "unauthorized",
            "The presented X-API-Key is invalid or retired.",
        )
    # An unconfigured server answers 503 to everything (the
    # pre-8.2 fail-closed contract), whatever was presented.

    cookie = request.cookies.get(auth_service.SESSION_COOKIE)
    if cookie:
        principal = _principal_for_session_token(cookie)
        if principal is not None:
            return principal

    if not configured:
        raise ApiError(
            503,
            "api_not_configured",
            "The API is not configured: CINEMORA_API_TOKEN is not set "
            "on the server, so /api/v1 is closed.",
        )
    raise ApiError(
        401,
        "unauthorized",
        "Sign in (session) or provide a valid X-API-Key header.",
    )


def require_admin(
    principal: auth_service.Principal = Depends(require_user),
) -> auth_service.Principal:
    """require_user + the Phase 7 role ladder. Admin routes are the
    one place a non-owner may see across tenants, so they answer
    403 (not 404) to authenticated non-admins."""
    if not principal.is_admin:
        raise ApiError(
            403, "forbidden", "This endpoint requires an administrator."
        )
    return principal


def _require_admin_for_diagnostic_surface(
    principal: auth_service.Principal,
) -> None:
    """FS-B (FS-7/FS-9/FS-10, enforcement model FS-14): the
    diagnostic project surfaces — the Asset Decisions ledger
    detail, the Delivery & Provenance certificate, and the QA
    reports — are ADMIN-ONLY. Callers invoke this AFTER
    _load_project_for_diagnostic_surface (the Phase 1a choke
    point for non-admins), so the status convention for project
    data is preserved exactly: a principal with no grant on the
    project still gets the tenant-private 404, and only a granted
    principal (owner or accepted member) who is not an
    administrator gets 403 — the require_admin convention for
    admin-only resources.
    Admin and super_admin principals pass unchanged (the operator
    principal resolves to super_admin, so it passes too).

    Developer-API principals (auth_method "api_key") are exempt:
    the public API carries its own authorization model (key
    scopes + the same ownership choke point) and is a kept user
    surface (FS-DISCOVERY A-8), so its handlers calling these
    functions in-process keep their existing behaviour. Session
    principals calling in-process (the Assistant's certificate
    read tool) are NOT exempt — the same data must not leak
    around the gate through another user surface (the FS-A
    fail-closed precedent). Pipeline/service-layer callers never
    pass through these HTTP handlers at all and are unaffected.
    """
    if principal.is_admin:
        return
    if getattr(principal, "auth_method", None) == "api_key":
        return
    raise ApiError(
        403, "forbidden", "This endpoint requires an administrator."
    )


def _load_project_for_diagnostic_surface(
    db, project_id: str, principal: auth_service.Principal
):
    """Project load for the FS-B diagnostic surfaces, pairing with
    _require_admin_for_diagnostic_surface. Admin principals load
    cross-tenant (existence-only 404): the admin API already
    operates cross-tenant by design (api_admin.py), the register's
    framing is "admin diagnostics", and the frontend shows the
    admin tabs by account role alone — an admin who could see the
    tab but never open another tenant's diagnostics would be a
    broken surface (FS-DISCOVERY §3.1 note, option (a)). Every
    non-admin principal loads through the Phase 1a choke point
    unchanged, so tenant privacy (404 for non-granted principals)
    is untouched for ordinary users."""
    if principal.is_admin:
        project = db.get(Project, str(project_id))
        if project is None:
            raise ApiError(
                404, "project_not_found", f"No project {project_id}."
            )
        return project
    return _get_owned_project_or_404(db, project_id, principal)


# ---------------------------------------------------------------------
# FS-C (FS-1/FS-5/FS-6/FS-11/FS-13, enforcement model FS-14): the
# presentational internal surfaces — Visual Plan, Research & Source
# Library, Versioning & Recovery, project/account Intelligence,
# and the account Library & Templates pages — are ADMIN-ONLY,
# exactly like the FS-B diagnostic surfaces: they leave the
# ordinary-user product completely (tabs, nav, direct URL, API)
# while nothing is deleted and the restriction stays trivially
# restorable. The semantics are the FS-B pair's, reused verbatim:
# non-granted principals keep the Phase 1a tenant-private 404,
# granted non-admins get 403, admins load cross-tenant, and
# developer-API principals (auth_method "api_key") keep their own
# scope+ownership model (A-8) — the public API's versions read
# delegates to the v1 handler in-process and must not be broken
# by a session-UI gate. Service-layer functions are never gated:
# the pipeline's own snapshot writes, research-correction hooks
# and editor library imports call services directly and are
# unaffected.
# ---------------------------------------------------------------------


def _require_admin_for_presentational_surface(
    principal: auth_service.Principal,
) -> None:
    """FS-C admin check for the presentational surfaces: the FS-B
    diagnostic convention, shared implementation."""
    _require_admin_for_diagnostic_surface(principal)


def _load_project_for_presentational_surface(
    db, project_id: str, principal: auth_service.Principal
):
    """Project load for the FS-C presentational surfaces: the FS-B
    diagnostic loader (Phase 1a choke point for non-admins,
    cross-tenant existence-only load for admins)."""
    return _load_project_for_diagnostic_surface(db, project_id, principal)


def _require_project_for_presentational_surface(
    project_id: str, principal, *, write: bool = False
) -> None:
    """Load + admin-check in one step for the FS-C project-scoped
    surfaces whose handlers previously called
    _require_owned_project. `write` is accepted for call-site
    symmetry with the choke point: every non-admin is refused by
    the admin check regardless, so the read/write distinction no
    longer changes the outcome (a granted viewer previously got
    the choke point's read-only 403; they now get the admin-only
    403 — same status, stricter rule)."""
    db = SessionLocal()
    try:
        _load_project_for_presentational_surface(db, project_id, principal)
        _require_admin_for_presentational_surface(principal)
    finally:
        db.close()


# The decision-ledger fields the kept Editor flow consumes
# (frontend/components/editor/asset-panel.tsx): a ledger row's id
# (the reuse handle — the editor backend re-reads the decision
# server-side), its decision type, provider, sentence, evidence
# class, verification and rights labels. Everything else a ledger
# row carries — source_url and content_hash (provenance detail),
# asset_key, scene/interval placement, the previous-decision
# chain, revision tagging, recorded timestamps — is FS-7
# diagnostic detail and is stripped for non-admin principals.
_EDITOR_LEDGER_FIELDS = (
    "id",
    "decision_type",
    "provider",
    "sentence_number",
    "evidence_class",
    "verification",
    "rights",
)


def _editor_ledger_row(row: dict) -> dict:
    """The non-admin view of one asset-decision ledger row: only
    the fields the Editor's asset library needs to offer reuse of
    a recorded asset (FS-B field restriction of GET .../assets —
    the endpoint is shared between the admin-only Assets tab and
    the kept Editor flow, so it is restricted by field, never
    closed, for non-admins)."""
    return {field: row.get(field) for field in _EDITOR_LEDGER_FIELDS}


def require_api_token(request: Request) -> str:
    """Back-compat shim for the legacy routes in main.py (IA-08):
    same resolution as require_user, returning the identity string
    those routes have always ignored."""
    principal = require_user(request)
    return principal.email


_rate_logger = logging.getLogger(__name__)

# B-12: how long a failed Redis probe suppresses further probes.
# During the suppression window the limiter runs on its in-memory
# fallback (and says so loudly, once per probe failure).
_RATE_LIMIT_REDIS_RETRY_SECONDS = 5.0

_rate_limit_redis_client = None
_rate_limit_redis_client_set = False


def set_rate_limit_redis_client(client) -> None:
    """Test seam: inject the Redis client the limiters share
    (fakeredis in tests). Pass None to restore lazy creation."""
    global _rate_limit_redis_client, _rate_limit_redis_client_set
    _rate_limit_redis_client = client
    _rate_limit_redis_client_set = True


def reset_rate_limit_redis_client() -> None:
    global _rate_limit_redis_client, _rate_limit_redis_client_set
    _rate_limit_redis_client = None
    _rate_limit_redis_client_set = False


def _shared_rate_limit_redis():
    global _rate_limit_redis_client, _rate_limit_redis_client_set
    if not _rate_limit_redis_client_set:
        from redis import Redis

        _rate_limit_redis_client = Redis.from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        _rate_limit_redis_client_set = True
    return _rate_limit_redis_client


class _RateLimiter:
    """Sliding-window limiter for POST routes (60s window).

    B-12: DISTRIBUTED by default — hits live in a Redis sorted
    set per (scope, identity), so every API process sharing the
    Redis enforces ONE combined limit (the pre-5b in-process
    deque let multi-worker deployments multiply the limit by the
    worker count). The algorithm is the same sliding window:
    prune hits older than 60s, refuse at the limit, else record.

    FALLBACK: when Redis is unreachable the limiter degrades to
    the original in-process deque and logs the degradation
    LOUDLY (error level) — a silent fallback would hide exactly
    the multi-worker bypass this change exists to close. Limits
    and thresholds are unchanged in both modes.
    """

    WINDOW_SECONDS = 60.0

    def __init__(self, scope: str = "api", redis_client=None):
        self._scope = scope
        self._redis_client = redis_client
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()
        self._redis_down_until = 0.0
        self._redis_keys: set[str] = set()
        self.degraded = False  # True while running on the fallback

    # -- Redis plumbing ------------------------------------------------

    def _redis(self):
        if self._redis_client is not None:
            return self._redis_client
        return _shared_rate_limit_redis()

    def set_redis_client(self, client) -> None:
        self._redis_client = client
        self._redis_down_until = 0.0

    def _redis_available(self) -> bool:
        return time.monotonic() >= self._redis_down_until

    def _note_redis_failure(self, exc: Exception) -> None:
        self.degraded = True
        self._redis_down_until = (
            time.monotonic() + _RATE_LIMIT_REDIS_RETRY_SECONDS
        )
        _rate_logger.error(
            "RATE LIMITER DEGRADED: Redis unavailable for scope "
            "%s (%s); falling back to PER-PROCESS in-memory limits "
            "— multi-worker deployments will NOT share one limit "
            "until Redis recovers.",
            self._scope,
            exc,
        )

    def _key(self, identity: str) -> str:
        return f"ratelimit:{self._scope}:{identity}"

    def _redis_check(self, identity: str, effective: int) -> None:
        client = self._redis()
        key = self._key(identity)
        now = time.time()
        client.zremrangebyscore(key, 0, now - self.WINDOW_SECONDS)
        count = client.zcard(key)
        if count >= effective:
            raise ApiError(
                429,
                "rate_limited",
                "Too many write requests in the last minute; "
                "retry shortly.",
            )
        member = f"{now}:{uuid4().hex}"
        client.zadd(key, {member: now})
        client.expire(key, int(self.WINDOW_SECONDS) + 60)
        with self._lock:
            self._redis_keys.add(key)
        self.degraded = False

    # -- In-memory fallback (the pre-5b behaviour) ---------------------

    def _memory_check(self, identity: str, effective: int) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[identity]
            while hits and now - hits[0] > self.WINDOW_SECONDS:
                hits.popleft()
            if len(hits) >= effective:
                raise ApiError(
                    429,
                    "rate_limited",
                    "Too many write requests in the last minute; "
                    "retry shortly.",
                )
            hits.append(now)

    # -- Public surface (unchanged contract) ---------------------------

    def _limit(self) -> int:
        try:
            return max(1, int(os.environ.get("CINEMORA_API_RATE_LIMIT", "30")))
        except ValueError:
            return 30

    def check(self, identity: str, limit: int | None = None) -> None:
        effective = self._limit() if limit is None else limit
        if self._redis_available():
            try:
                self._redis_check(identity, effective)
                return
            except ApiError:
                raise
            except Exception as exc:
                self._note_redis_failure(exc)
        self._memory_check(identity, effective)

    def peek(self, identity: str) -> int:
        """Current in-window hit count for an identity, without
        recording a hit (Developer API usage surface, Phase N)."""
        if self._redis_available():
            try:
                client = self._redis()
                key = self._key(identity)
                client.zremrangebyscore(
                    key, 0, time.time() - self.WINDOW_SECONDS
                )
                return int(client.zcard(key))
            except Exception as exc:
                self._note_redis_failure(exc)
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(identity)
            if not hits:
                return 0
            while hits and now - hits[0] > self.WINDOW_SECONDS:
                hits.popleft()
            return len(hits)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            keys = list(self._redis_keys)
            self._redis_keys.clear()
        if keys and self._redis_available():
            try:
                self._redis().delete(*keys)
            except Exception:
                pass


rate_limiter = _RateLimiter(scope="api")
auth_rate_limiter = _RateLimiter(scope="auth")


def _auth_rate_limit(request: Request, email: str) -> None:
    """Stricter limiter for the anonymous auth endpoints, keyed by
    account + IP (design 02 §1.2)."""
    try:
        limit = max(
            1, int(os.environ.get("CINEMORA_AUTH_RATE_LIMIT", "10"))
        )
    except ValueError:
        limit = 10
    client = request.client.host if request.client else "unknown"
    auth_rate_limiter.check(f"auth:{client}:{email}", limit=limit)


def post_guard(
    principal: auth_service.Principal = Depends(require_user),
) -> auth_service.Principal:
    """Auth + rate limit, for POST routes. The limiter is keyed by
    user id now that principals are people (01 §1.3)."""
    rate_limiter.check(principal.user_id)
    return principal


# ---------------------------------------------------------------------
# Accounts (Phase 8.2): registration, login, sessions, reset, admin
# ---------------------------------------------------------------------


class RegisterIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    display_name: str | None = Field(default=None, max_length=200)
    invite_code: str | None = None


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class ResetRequestIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class ResetConfirmIn(BaseModel):
    token: str = Field(min_length=1)
    new_password: str = Field(min_length=1, max_length=1024)


def _user_view(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
        "email_verified_at": user.email_verified_at,
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
    }


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=auth_service.SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        # Secure by default (E-21/B-07): only an explicit
        # CINEMORA_COOKIE_SECURE=0 (local http development) turns
        # the Secure flag off.
        secure=os.environ.get("CINEMORA_COOKIE_SECURE", "1") != "0",
        path="/",
        max_age=auth_service.SESSION_TTL_SECONDS,
    )


def _registration_gate(body: RegisterIn) -> None:
    mode = os.environ.get("CINEMORA_REGISTRATION", "open").strip().lower()
    if mode == "closed":
        raise ApiError(
            403,
            "registration_closed",
            "Registration is closed on this deployment.",
        )
    if mode == "invite":
        expected = os.environ.get("CINEMORA_INVITE_CODE") or ""
        provided = body.invite_code or ""
        if not expected or not hmac.compare_digest(provided, expected):
            raise ApiError(
                403,
                "invalid_invite_code",
                "An invite code is required to register.",
            )


def _assign_default_plan(db, user: User) -> None:
    """New accounts land on the default plan (02 §1.3) with an empty
    preferences document. Tolerant of unseeded databases (tests):
    no default plan -> no subscription row; plan resolution in
    services/plans.py defines what that means at enforcement time."""
    plan = db.query(Plan).filter(Plan.is_default.is_(True)).first()
    if plan is not None:
        db.add(
            UserSubscription(
                id=str(uuid4()),
                user_id=user.id,
                plan_id=plan.id,
                status="active",
                current_period_start=datetime.now(timezone.utc),
            )
        )
    db.add(
        UserPreference(
            user_id=user.id,
            document={"studio": {}, "assistant": {}, "notifications": {}},
        )
    )


@router.post("/auth/register", status_code=201)
def register(body: RegisterIn, request: Request, response: Response):
    email = auth_service.normalize_email(body.email)
    _auth_rate_limit(request, email)
    _registration_gate(body)
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise ApiError(422, "invalid_email", "That email address looks invalid.")
    policy_error = auth_service.password_policy_error(body.password)
    if policy_error:
        raise ApiError(422, "weak_password", policy_error)
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == email).one_or_none()
        if existing is not None:
            raise ApiError(
                409, "email_taken", "An account with that email already exists."
            )
        user = User(
            id=str(uuid4()),
            email=email,
            password_hash=auth_service.hash_password(body.password),
            display_name=body.display_name,
            role="user",
            status="active",
        )
        db.add(user)
        db.flush()
        _assign_default_plan(db, user)
        db.commit()
        db.refresh(user)
        token = auth_service.create_session(user.id)
        _set_session_cookie(response, token)
        # E-24: the browser credential is the HttpOnly cookie ONLY.
        # The session token never appears in a response body here;
        # non-browser clients exchange credentials at POST
        # /auth/token (see auth_token below).
        return {"user": _user_view(user)}
    finally:
        db.close()


# A real Argon2id hash of a throwaway password, computed once at
# import: login verifies against it when the account doesn't exist,
# so response timing doesn't enumerate registered emails.
_DUMMY_HASH = auth_service.hash_password("timing-equalisation-dummy")


def _perform_login(email: str, password: str):
    """The ONE credential check behind both sign-in surfaces
    (browser login and the API-client token exchange): verify the
    password (timing-equalised for unknown accounts), refuse
    disabled accounts, stamp last_login_at and audit administrator
    sign-ins. Returns (user_view, user_id); raises ApiError with
    the login contract's codes otherwise."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).one_or_none()
        password_ok = auth_service.verify_password(
            user.password_hash if user is not None else _DUMMY_HASH,
            password,
        )
        if user is None or not password_ok:
            raise ApiError(
                401, "invalid_credentials", "Email or password is incorrect."
            )
        if user.status != "active":
            raise ApiError(
                403,
                "account_disabled",
                "This account is disabled; contact the operator.",
            )
        user.last_login_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(user)
        if user.role in ("admin", "super_admin"):
            # Phase 7: administrator sign-ins are audit events
            # (best-effort — the audit writer never fails a login).
            admin_audit.record(
                auth_service.principal_for_user(user, "session"),
                "admin.sign_in",
                target_type="account",
                target_id=user.id,
            )
        return _user_view(user), user.id
    finally:
        db.close()


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    email = auth_service.normalize_email(body.email)
    _auth_rate_limit(request, email)
    user_view, user_id = _perform_login(email, body.password)
    token = auth_service.create_session(user_id)
    _set_session_cookie(response, token)
    # E-24: cookie only — no session token in the body (see
    # register and /auth/token).
    return {"user": user_view}


@router.post("/auth/token")
def auth_token(body: LoginIn, request: Request):
    """The DOCUMENTED credential exchange for non-browser API
    clients (E-24): POST {email, password} and receive the session
    token in the body as a Bearer credential for the Authorization
    header. This is the ONLY auth response that carries a token in
    its body — the browser flows (register/login) deliver the
    session exclusively via the HttpOnly cookie and never expose
    the token to response-body handling (logs, proxies, page
    scripts). Same credential check and same auth rate limiter
    (keyed by account + IP) as /auth/login."""
    email = auth_service.normalize_email(body.email)
    _auth_rate_limit(request, email)
    user_view, user_id = _perform_login(email, body.password)
    token = auth_service.create_session(user_id)
    return {
        "session_token": token,
        "token_type": "bearer",
        "user": user_view,
    }


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    token = _session_token_from(request)
    if token:
        auth_service.revoke_session(token)
    response.delete_cookie(key=auth_service.SESSION_COOKIE, path="/")
    return {"logged_out": True}


@router.get("/auth/me")
def me(principal: auth_service.Principal = Depends(require_user)):
    db = SessionLocal()
    try:
        user = db.get(User, principal.user_id)
        if user is None:
            raise ApiError(401, "unauthorized", "Account no longer exists.")
        return {"user": _user_view(user)}
    finally:
        db.close()


@router.post("/auth/password-reset/request", status_code=202)
def password_reset_request(body: ResetRequestIn, request: Request):
    import logging

    from services import mailer

    email = auth_service.normalize_email(body.email)
    _auth_rate_limit(request, email)
    # The reported mode reflects the deployment's mailer
    # configuration only — identical for known and unknown accounts,
    # so the response cannot enumerate registered emails.
    mode = "smtp" if mailer.smtp_configured() else "manual"
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).one_or_none()
        if user is not None:
            token = auth_service.issue_reset_token(db, user)
            base = os.environ.get("CINEMORA_PUBLIC_BASE_URL", "")
            link = f"{base}/reset-password?token={token}"
            mailer.send_email(
                user.email,
                "Cinemora password reset",
                "Reset your Cinemora password within 1 hour:\n\n"
                f"{link}\n\nIf you did not request this, ignore this email.",
            )
            if mode == "manual":
                # No SMTP on this deployment (B-08/E-24): the raw
                # reset token and the complete reset link must NEVER
                # reach a log — host logs are not a secret-delivery
                # channel. Log only a redacted record: recipient,
                # expiry, and a short non-reversible reference (a
                # prefix of the token's stored SHA-256) so an
                # operator can correlate a support request with this
                # issuance. Delivery itself requires SMTP (or the
                # user re-requesting once SMTP is configured); the
                # token is unrecoverable by design — only its hash
                # is stored.
                import hashlib

                token_ref = hashlib.sha256(token.encode()).hexdigest()[:12]
                logging.getLogger(__name__).warning(
                    "password reset issued for %s (manual delivery; "
                    "token NOT logged): ref sha256:%s, expires %s. "
                    "No email was sent — configure SMTP for delivery.",
                    user.email,
                    token_ref,
                    user.password_reset_expires_at,
                )
    finally:
        db.close()
    return {"delivery": mode}


@router.post("/auth/password-reset/confirm")
def password_reset_confirm(body: ResetConfirmIn):
    policy_error = auth_service.password_policy_error(body.new_password)
    if policy_error:
        raise ApiError(422, "weak_password", policy_error)
    db = SessionLocal()
    try:
        user = auth_service.consume_reset_token(db, body.token, body.new_password)
        if user is None:
            raise ApiError(
                400,
                "invalid_or_expired_token",
                "That reset link is invalid or has expired.",
            )
        return {"reset": True}
    finally:
        db.close()


@router.get("/account/usage")
def account_usage(
    principal: auth_service.Principal = Depends(require_user),
):
    """The caller's plan + derived usage (Phase 8.3; the frontend
    account surface in 8.5 reads exactly this)."""
    db = SessionLocal()
    try:
        return plans_service.usage_summary(db, principal.user_id)
    finally:
        db.close()


# ---------------------------------------------------------------------
# Phase 8 billing: webhooks + checkout (directive §§10, 15)
# ---------------------------------------------------------------------


@router.post("/billing/webhooks/{provider_name}")
async def billing_webhook(provider_name: str, request: Request):
    """Provider webhook receiver. Unauthenticated by design —
    the provider's signature over the raw body IS the
    authentication (services.billing.webhooks). Benefits are
    granted only inside verified processing."""
    from services.billing import webhooks

    raw_body = await request.body()
    status, body = webhooks.handle_webhook(
        provider_name, dict(request.headers), raw_body
    )
    return JSONResponse(status_code=status, content=body)


class CheckoutSubscriptionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1, max_length=64)
    interval: str = Field(default="month", pattern="^(month|year)$")


class CheckoutTopupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_id: str = Field(min_length=1, max_length=64)


class SubscriptionChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1, max_length=64)
    interval: str | None = Field(
        default=None, pattern="^(month|year)$"
    )


@router.post("/account/billing/checkout/subscription")
def billing_checkout_subscription(
    body: CheckoutSubscriptionIn,
    principal: auth_service.Principal = Depends(post_guard),
):
    from services.billing import checkout

    return checkout.create_subscription_checkout(
        principal.user_id, body.plan_id, body.interval
    )


@router.post("/account/billing/checkout/topup")
def billing_checkout_topup(
    body: CheckoutTopupIn,
    principal: auth_service.Principal = Depends(post_guard),
):
    from services.billing import checkout

    return checkout.create_topup_checkout(
        principal.user_id, body.package_id
    )


@router.post("/account/billing/subscription/change")
def billing_subscription_change(
    body: SubscriptionChangeIn,
    principal: auth_service.Principal = Depends(post_guard),
):
    from services.billing import checkout

    return checkout.change_subscription(
        principal.user_id, body.plan_id, body.interval
    )


@router.post("/account/billing/subscription/cancel")
def billing_subscription_cancel(
    principal: auth_service.Principal = Depends(post_guard),
):
    from services.billing import checkout

    return checkout.cancel_subscription(principal.user_id)


@router.get("/account/billing/management-url")
def billing_management_url(
    principal: auth_service.Principal = Depends(require_user),
):
    from services.billing import checkout

    return {"url": checkout.management_url(principal.user_id)}


# The Phase 8 account-admin endpoints that used to live here
# (GET /admin/users, POST /admin/users/{user_id}/plan) moved to
# api_admin.py in Phase 7 — same paths and shapes, now behind the
# admin router's guard and recorded in the admin audit log.


# ---------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------


class ProjectOptionsIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    # FS-2 / FS-D: accepted for old-client compatibility but
    # IGNORED — genre is never a user choice; the pack is
    # backend-derived from the script. The key is stripped before
    # storage (see _strip_user_genre_options).
    genre: str | None = None
    voice_id: str | None = None
    captions: str | None = None  # "burn" | "sidecar" | "off"
    # Stage H (M3): the ONE music contract — the same fields the
    # pipeline (jobs.resolve via services.music.resolve_music_
    # request) and the run manifest use. music_file is an
    # operator-placed server path; music_enabled=False disables
    # music entirely (it beats music_file and any legacy project
    # background.mp3); volumes are 0.0-2.0, mirroring the editor's
    # set_volumes range.
    music_file: str | None = None
    music_enabled: bool | None = None
    music_volume: float | None = Field(default=None, ge=0.0, le=2.0)
    narration_volume: float | None = Field(default=None, ge=0.0, le=2.0)
    # Stage J (P4): mix overrides for services.music.resolve_mix_
    # config — each beats the genre pack's mix_preset and the env
    # overrides; all are optional and recorded in the soundtrack
    # record's mix section when a soundtrack is built.
    music_fade_in_seconds: float | None = Field(default=None, ge=0.0, le=30.0)
    music_fade_out_seconds: float | None = Field(default=None, ge=0.0, le=30.0)
    music_loop_crossfade_seconds: float | None = Field(
        default=None, ge=0.0, le=10.0
    )
    duck_threshold: float | None = Field(default=None, gt=0.0, le=1.0)
    duck_ratio: float | None = Field(default=None, ge=1.0, le=100.0)
    duck_attack_ms: float | None = Field(default=None, ge=0.0, le=10000.0)
    duck_release_ms: float | None = Field(default=None, ge=0.0, le=60000.0)
    require_plan_approval: bool = False
    # Phase 9 (§§13–17): the Pre-Production Configuration Studio
    # document (services.production_config). Cleaned + projected
    # onto the flat keys above at creation; re-resolved and
    # validated at every run submission (§14.2).
    production_config: dict | None = None


class ProjectCreateIn(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    script: str = Field(min_length=1, max_length=MAX_SCRIPT_CHARS)
    category: str = "Documentary"
    options: ProjectOptionsIn | None = None


class RunSubmitIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    options: dict | None = None


class ReplacementIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    file_path: str | None = None
    source_url: str | None = None
    title: str | None = None


class PlanDecisionIn(BaseModel):
    sentence_number: int = Field(ge=1)
    action: str
    replacement: ReplacementIn | None = None


# ---------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_owned_project_or_404(
    db, project_id: str, principal: auth_service.Principal, *, write: bool = False
) -> Project:
    """The project load choke point (design 02 §4.1, extended by
    Collaboration #11): every project-scoped route loads through
    here. Access is the owner's, or an accepted project member's
    (services/collaboration.role_for — the grant layer above
    ownership). A principal with no grant sees another user's
    project as indistinguishable from a nonexistent one — 404,
    never 403 — so existence itself stays tenant-private. Legacy
    rows with a NULL owner (pre-8.1 databases that never ran the
    backfill) are visible only to the founder principal who would
    own them.

    `write=True` is the mutating-route form (every POST/PUT/PATCH/
    DELETE handler passes it): a viewer member is refused with a
    clear 403 project_read_only — they legitimately know the
    project exists, so hiding it would be a lie; editors and the
    owner pass. Reads (write=False) pass for every accepted role."""
    project = db.get(Project, str(project_id))
    if project is None:
        raise ApiError(404, "project_not_found", f"No project {project_id}.")
    role = collaboration.role_for(db, project, principal)
    if role is None:
        raise ApiError(404, "project_not_found", f"No project {project_id}.")
    if write and role == collaboration.ROLE_VIEWER:
        raise ApiError(
            403,
            "project_read_only",
            "Your role on this project is viewer: it is read-only. "
            "Ask the project owner for the editor role to make changes.",
        )
    return project


def _get_owner_project_or_404(
    db, project_id: str, principal: auth_service.Principal
) -> Project:
    """Owner-ONLY load: ownership-level routes (member management)
    use this, never the grant-aware loader — a member, however
    trusted, administering membership would be privilege
    escalation. Non-owners (members included) get the same 404 as
    any other non-granted principal."""
    project = db.get(Project, str(project_id))
    if project is None:
        raise ApiError(404, "project_not_found", f"No project {project_id}.")
    owner = project.owner_id
    if owner is None:
        if principal.auth_method != "operator_token":
            raise ApiError(
                404, "project_not_found", f"No project {project_id}."
            )
    elif owner != principal.user_id:
        raise ApiError(404, "project_not_found", f"No project {project_id}.")
    return project


def _get_owned_run_or_404(
    run_id: str, principal: auth_service.Principal, *, write: bool = False
):
    """Run-level equivalent: the run dict (run_ledger) with the
    same semantics. A run's recorded owner_id is its PROJECT's
    owner (run_ledger stamps it from the project row), so member
    access resolves through the run's project, exactly like the
    project loader."""
    run = run_ledger.get_run(run_id)
    if run is None:
        raise ApiError(404, "run_not_found", f"No run {run_id}.")
    owner = run.get("owner_id")
    if owner is not None and owner == principal.user_id:
        return run
    if owner is None and principal.auth_method == "operator_token":
        return run
    db = SessionLocal()
    try:
        project = db.get(Project, str(run.get("project_id")))
        if project is not None:
            role = collaboration.role_for(db, project, principal)
            if role is not None:
                if write and role == collaboration.ROLE_VIEWER:
                    raise ApiError(
                        403,
                        "project_read_only",
                        "Your role on this project is viewer: it is "
                        "read-only. Ask the project owner for the "
                        "editor role to make changes.",
                    )
                return run
    finally:
        db.close()
    raise ApiError(404, "run_not_found", f"No run {run_id}.")


def _options_dict(options) -> dict:
    if options is None:
        return {}
    if isinstance(options, BaseModel):
        return options.model_dump(exclude_none=True)
    return dict(options)


# FS-2 / FS-D: the option keys through which a user could once
# choose a genre pack. The create/configure flows no longer offer
# a genre selection, and a user-supplied value under either key
# must have NO effect on the pack a production resolves — the
# documentary treatment is backend-derived from the script
# (services.genre_packs.detect_genre). Payloads carrying the keys
# are still accepted (old clients keep working); the keys are
# stripped wherever user options are stored or merged for a run.
USER_GENRE_OPTION_KEYS = ("genre", "genre_pack")


def _strip_user_genre_options(options) -> dict:
    """A copy of user-supplied options with the FS-2 genre keys
    removed (see USER_GENRE_OPTION_KEYS)."""
    cleaned = dict(options) if isinstance(options, dict) else {}
    for key in USER_GENRE_OPTION_KEYS:
        cleaned.pop(key, None)
    return cleaned


def _production_state(project_dir: Path) -> dict | None:
    try:
        return json.loads(
            media_paths.resolve_artefact(
                project_dir, "production_state.json"
            ).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------
# FS-4 / FS-A — failure-detail sanitization (response layer only)
# ---------------------------------------------------------------------
# A pipeline stage exception's str(error) is stored verbatim in
# production_state.json and ProductionRun.error. That text is
# diagnostic (stage-failure explanations, provider/worker errors,
# internal codes) and must never reach a non-admin principal in a
# user-facing response. Storage, backend logging and the admin API
# keep the full text untouched; this helper rewrites ONLY the
# response payload, at the response layer, for non-admin principals.
#
# Rule: admin / super_admin principals (the operator principal
# resolves to super_admin, services/auth.py) get the payload
# byte-identical to the unsanitized one. Everyone else — ordinary
# users AND project collaborators (collaboration role never
# elevates; register ambiguity A-9) — gets every non-empty "error"
# string anywhere in the payload (top level, per-stage entries,
# the embedded production-state "progress" dict, nested run views)
# replaced with GENERIC_FAILURE_MESSAGE, and a dict whose status
# is "failed" but whose "error" is empty gains the generic message
# too, so a failure is always labelled truthfully. Statuses, stage
# names, timestamps and progress structure are preserved exactly:
# a failed run still reads "failed" — failure is never disguised
# as success, only its internal explanation is withheld.
# A missing principal is treated as non-admin (fail closed).

GENERIC_FAILURE_MESSAGE = "Video generation failed"


def _sanitize_failure_node(node) -> None:
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key == "error" and isinstance(value, str) and value:
                node[key] = GENERIC_FAILURE_MESSAGE
            else:
                _sanitize_failure_node(value)
        if node.get("status") == "failed" and "error" in node and not node["error"]:
            node["error"] = GENERIC_FAILURE_MESSAGE
    elif isinstance(node, list):
        for item in node:
            _sanitize_failure_node(item)


def sanitize_failure_detail(view, principal):
    """Return ``view`` with internal failure detail removed for
    non-admin principals; admins get ``view`` itself, unchanged.

    The one reusable FS-4 sanitizer: every user-facing carrier of
    run/project failure detail (run views, project views, project
    status, the legacy main.py status route) passes its payload
    through here so future carriers inherit the rule by doing the
    same. The input is never mutated for non-admins (a deep copy
    is sanitized); admin payloads are returned as-is.
    """
    if view is None:
        return None
    if principal is not None and getattr(principal, "is_admin", False):
        return view
    sanitized = copy.deepcopy(view)
    _sanitize_failure_node(sanitized)
    return sanitized


def _run_view(run: dict | None, principal=None) -> dict | None:
    if not run:
        return None
    view = {
        "run_id": run.get("run_id"),
        "project_id": run.get("project_id"),
        "status": run.get("status"),
        "stage": run.get("stage"),
        "script_hash": run.get("script_hash"),
        "error": run.get("error"),
        "cancel_requested": run.get("cancel_requested"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
    }
    return sanitize_failure_detail(view, principal)


def _film_media_url(project_id: str, project_dir: Path) -> str | None:
    index = plan_document.build_media_index(project_id, project_dir)
    for entry in index.values():
        if entry["file_name"] == plan_document.CANONICAL_OUTPUTS["film"]:
            return f"/api/v1/projects/{project_id}/media/{entry['key']}"
    return None


def _project_view(
    db,
    project: Project,
    *,
    detail: bool = False,
    access_role: str | None = None,
    principal=None,
) -> dict:
    project_dir = media_paths.project_dir(project.id)
    state = _production_state(project_dir)
    latest = run_ledger.latest_run_for(project.id)
    view = {
        "id": project.id,
        "title": project.title,
        "category": project.category,
        "status": project.status,
        "created_at": project.created_at,
        # Collaboration (#11): the CALLER's role on this project
        # ("owner" for their own projects; the member role for
        # shared ones). Present only where the caller resolved it.
        "access_role": access_role,
        "script_preview": (project.script or "")[:140],
        "current_stage": (state or {}).get("current_stage"),
        "video_url": _film_media_url(project.id, project_dir)
        if project.status == "completed"
        else None,
        "latest_run": _run_view(latest, principal),
    }
    if detail:
        view["script"] = project.script
        view["options"] = project.options or {}
    return sanitize_failure_detail(view, principal)


# ---------------------------------------------------------------------
# Health + Catalogues (B-14 step 1): extracted to api_catalogues.py
# and mounted on this router at exactly this position — every route
# path, method, dependency and response is identical to the
# pre-extraction definitions (the factory receives this module's
# require_user, so the auth dependency is the same callable).
# ---------------------------------------------------------------------

from api_catalogues import build_router as _build_catalogues_router

router.include_router(_build_catalogues_router(require_user))


# ---------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------


def _create_project_record(db, principal, *, title, script, category, options):
    """The ONE project-creation path (manual creation and
    template application both land here): plan script-length
    enforcement, the Project row, and the Versioning (§3.2)
    snapshot of the creation script. Callers validate their
    input through ProjectCreateIn first — this helper assumes a
    validated body and applies no second, divergent rule set."""
    plans_service.enforce_script_length(db, principal.user_id, script)
    # FS-2 / FS-D: a user-supplied genre never reaches storage —
    # the pack is derived from the script at production time, so
    # the stored options carry no genre choice to honour later.
    options = _strip_user_genre_options(options)
    project = Project(
        id=str(uuid4()),
        owner_id=principal.user_id,
        title=title,
        script=script,
        category=category or "Documentary",
        options=options or None,
        status="created",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    # Versioning (§3.2): the creation script is the first
    # recorded script version — snapshot its text fail-soft so
    # it stays restorable even after the field is overwritten.
    versioning.try_record_script_snapshot(
        project.id, project.script, "project_created",
        actor=principal.user_id,
    )
    return _project_view(
        db,
        project,
        detail=True,
        access_role=collaboration.ROLE_OWNER,
        principal=principal,
    )


def _config_call(fn, *args, **kwargs):
    """Run a Phase 9 service call, mapping ConfigError onto the
    v1 error shape."""
    try:
        return fn(*args, **kwargs)
    except production_config.ConfigError as error:
        raise ApiError(error.status_code, error.code, error.message) from error


def _prepare_creation_options(db, principal, options) -> dict:
    """Phase 9 at creation: a configuration submitted with the
    project is cleaned (a malformed document refuses 422) and
    projected onto the flat option keys beside it; with no
    submitted configuration, the user's saved defaults (§15)
    seed the project. Validation against the live system is NOT
    run here — a project may legitimately be created before its
    music upload exists; the run-submission gate is where an
    unhonourable configuration refuses."""
    raw = (options or {}).get("production_config")
    if isinstance(raw, dict):
        config = _config_call(production_config.clean_config, raw)
    else:
        config = production_config.load_defaults(db, principal.user_id)
        if config is None:
            return options
    config = production_config.with_honest_preset_label(config)
    prepared = dict(options or {})
    for key in production_config.PROJECTION_OWNED_KEYS:
        prepared.pop(key, None)
    prepared["production_config"] = config
    prepared.update(production_config.flat_options(config))
    return prepared


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreateIn, principal=Depends(post_guard)):
    db = SessionLocal()
    try:
        options = _prepare_creation_options(
            db, principal, _options_dict(body.options)
        )
        return _create_project_record(
            db,
            principal,
            title=body.title,
            script=body.script,
            category=body.category,
            options=options,
        )
    finally:
        db.close()


@router.get("/projects")
def list_projects(principal=Depends(require_user)):
    db = SessionLocal()
    try:
        # Owner-filtered in SQL, never in Python after the fact
        # (design 02 §4.1). Collaboration (#11) adds the projects
        # shared WITH the caller (accepted memberships) as a second
        # SQL-filtered set, each annotated with the caller's role.
        projects = (
            db.query(Project)
            .filter(Project.owner_id == principal.user_id)
            .order_by(Project.created_at.desc())
            .all()
        )
        views = [
            _project_view(
                db, p, access_role=collaboration.ROLE_OWNER, principal=principal
            )
            for p in projects
        ]
        shared_ids = collaboration.shared_project_ids(db, principal.user_id)
        if shared_ids:
            shared = (
                db.query(Project)
                .filter(Project.id.in_(shared_ids))
                .order_by(Project.created_at.desc())
                .all()
            )
            for project in shared:
                if project.owner_id == principal.user_id:
                    continue
                views.append(
                    _project_view(
                        db,
                        project,
                        access_role=collaboration.role_for(db, project, principal),
                        principal=principal,
                    )
                )
        return {"projects": views}
    finally:
        db.close()


@router.get("/projects/{project_id}")
def get_project(project_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
        return _project_view(
            db,
            project,
            detail=True,
            access_role=collaboration.role_for(db, project, principal),
            principal=principal,
        )
    finally:
        db.close()


@router.get("/projects/{project_id}/status")
def get_project_status(project_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
        project_dir = media_paths.project_dir(project.id)
        state = _production_state(project_dir)
        latest = run_ledger.latest_run_for(project.id)
        error = (state or {}).get("error") or (latest or {}).get("error")
        return sanitize_failure_detail(
            {
                "id": project.id,
                "title": project.title,
                "category": project.category,
                "status": project.status,
                "created_at": project.created_at,
                "current_stage": (state or {}).get("current_stage"),
                "stages": (state or {}).get("stages"),
                "progress": state,
                "latest_run": _run_view(latest, principal),
                "video_url": _film_media_url(project.id, project_dir)
                if project.status == "completed"
                else None,
                "error": error,
            },
            principal,
        )
    finally:
        db.close()


# ---------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------


@router.post("/projects/{project_id}/runs", status_code=202)
def submit_run(project_id: str, body: RunSubmitIn | None = None, principal=Depends(post_guard)):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal, write=True)
        script = project.script
        project_options = dict(project.options or {})
    finally:
        db.close()

    # Versioning (§3.2): snapshot the submitted script's exact
    # text (fail-soft, idempotent per hash) so this run's script
    # version stays browsable and restorable after later edits.
    versioning.try_record_script_snapshot(
        project_id, script, "run_submission", actor=principal.user_id
    )

    merged = {**project_options, **((body.options if body else None) or {})}
    # FS-2 / FS-D: a genre supplied with the run — or still stored
    # on a pre-FS-D project — is inert: pack resolution at
    # production time derives from the script alone (jobs strips
    # the same keys again at the resolution seam).
    merged = _strip_user_genre_options(merged)
    # Phase 9 (§14.2/§15): the pre-generation configuration gate.
    # When the merged options carry a production_config document
    # it is re-cleaned, validated against the live system and
    # resolved into the run's option snapshot here — a
    # configuration the pipeline cannot honour refuses the run
    # (422) before anything is queued, and the snapshot the run
    # carries is exactly the configuration it will run with.
    merged = _config_call(
        production_config.resolve_run_options, project, merged
    )
    script_hash = run_ledger.script_sha256(script)

    # FS-C (FS-5, ambiguity A-5): the plan-approval gate applies
    # only when an ADMIN principal administers the flow. The Visual
    # Plan surface is admin-only, so a non-admin owner who stored
    # require_plan_approval (the Create toggle predates FS-C) would
    # park their run in awaiting_approval with no UI able to
    # release it. For non-admin principals the flag is therefore
    # inert at submission: the run takes the ordinary full-pipeline
    # path below (auto-approved at the point the pipeline would
    # wait), and a legacy run already parked by a pre-FS-C
    # submission is not deduplicated against — a fresh full run is
    # queued instead. Admin/operator submissions keep the gate
    # exactly as before, and POST /plan/approve stays available to
    # them (admin-only, below).
    if merged.get("require_plan_approval") and getattr(
        principal, "is_admin", False
    ):
        existing = run_ledger.latest_run_for(project_id, script_hash)
        if existing and existing["status"] in (
            "queued",
            "running",
            "awaiting_approval",
        ):
            return {
                "run": _run_view(existing, principal),
                "deduplicated": True,
                "job_id": None,
                "phase": "analysis",
            }
        # A genuinely new run: plan admission applies (Phase 8.3).
        db = SessionLocal()
        try:
            plans_service.enforce_run_admission(db, principal.user_id)
        finally:
            db.close()
        submission = run_ledger.submit_production(
            project_id, script, options=merged
        )
        if not submission:
            raise ApiError(
                503,
                "ledger_unavailable",
                "The run ledger is unavailable, so a plan-gated run "
                "cannot be recorded; nothing was queued.",
            )
        run_id = submission["run_id"]
        if submission.get("deduplicated"):
            return {
                "run": _run_view(run_ledger.get_run(run_id), principal),
                "deduplicated": True,
                "job_id": None,
                "phase": "analysis",
            }
        job_id = jobs.enqueue_analysis_phase(
            project_id, script, run_id, options=merged
        )
        return {
            "run": _run_view(run_ledger.get_run(run_id), principal),
            "deduplicated": False,
            "job_id": job_id,
            "phase": "analysis",
        }

    existing = run_ledger.latest_run_for(project_id, script_hash)
    already_active = bool(
        existing and existing["status"] in run_ledger.ACTIVE_STATUSES
    )
    if not already_active:
        # A genuinely new run: plan admission applies (Phase 8.3).
        # Deduplicated submissions create nothing and skip it.
        db = SessionLocal()
        try:
            plans_service.enforce_run_admission(db, principal.user_id)
        finally:
            db.close()
    job_id = jobs.enqueue_full_pipeline(
        project_id, script, options=merged or None
    )
    run = run_ledger.latest_run_for(project_id, script_hash)
    return {
        "run": _run_view(run, principal),
        "deduplicated": already_active,
        # When the submission deduplicated, enqueue_full_pipeline
        # returns the existing RUN id (its documented contract), not
        # a job id — report no job in that case rather than dress a
        # run id up as one.
        "job_id": None if already_active else job_id,
        "phase": "production",
    }


@router.get("/projects/{project_id}/generation-estimate")
def generation_estimate(project_id: str, principal=Depends(require_user)):
    """Phase 8 (§5): the pre-generation estimate — what pressing
    Generate will consume for THIS project's script (entitlement
    and/or credits), computed from the script itself."""
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
    from services.billing import operations

    return operations.estimate_for_project(principal.user_id, project_id)


@router.get("/projects/{project_id}/runs")
def list_project_runs(project_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
    runs = run_ledger.list_runs_for(project_id)
    return {"runs": [_run_view(run, principal) for run in runs]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, principal=Depends(require_user)):
    run = _get_owned_run_or_404(run_id, principal)
    return _run_view(run, principal)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, principal=Depends(post_guard)):
    # Owner check FIRST: a cross-tenant cancel attempt is a 404 and
    # verifiably changes nothing (isolation battery case 3).
    _get_owned_run_or_404(run_id, principal, write=True)
    accepted = run_ledger.request_cancel(run_id)
    return {
        "run_id": run_id,
        "cancel_requested": accepted,
        "run": _run_view(run_ledger.get_run(run_id), principal),
    }


# ---------------------------------------------------------------------
# Production plan + decisions
# ---------------------------------------------------------------------


@router.get("/projects/{project_id}/plan")
def get_plan(project_id: str, principal=Depends(require_user)):
    # FS-C (FS-5): the Visual Plan is an admin-only presentational
    # surface — choke point first (non-granted 404), then the
    # admin check (granted non-admin 403).
    db = SessionLocal()
    try:
        project = _load_project_for_presentational_surface(
            db, project_id, principal
        )
        _require_admin_for_presentational_surface(principal)
    finally:
        db.close()
    plan = plan_document.build_plan(
        project.id, media_paths.project_dir(project.id)
    )
    if plan is None:
        raise ApiError(
            404,
            "plan_not_found",
            "No analysis exists for this project yet; generate the "
            "plan first (POST /plan/generate).",
        )
    plan["title"] = project.title
    return plan


@router.post("/projects/{project_id}/plan/generate")
def generate_plan(project_id: str, principal=Depends(post_guard)):
    # FS-C (FS-5): plan generation serves the admin-only Visual
    # Plan surface; same load + admin check as the plan read.
    db = SessionLocal()
    try:
        project = _load_project_for_presentational_surface(
            db, project_id, principal
        )
        _require_admin_for_presentational_surface(principal)
        script = project.script
    finally:
        db.close()
    project_dir = media_paths.project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    try:
        production_data = jobs.build_production_scenes(
            script, project_id=project_id, project_dir=project_dir
        )
    except Exception as error:
        raise ApiError(
            502, "analysis_failed", f"Story analysis failed: {error}"
        ) from error
    plan_document.write_plan_document(
        project_dir, production_data["analysis"], project_id
    )
    plan = plan_document.build_plan(project_id, project_dir)
    if plan is None:
        raise ApiError(
            502,
            "analysis_failed",
            "Analysis completed but no plan document could be assembled.",
        )
    return plan


def _append_plan_decision(project_dir: Path, record: dict) -> dict:
    """Append one decision to plan_decisions.json (atomic rewrite
    via services.atomic_write — the single write path since
    Stage 4 / DATA-101)."""
    path = project_dir / plan_document.DECISIONS_FILENAME
    decisions = plan_document.load_plan_decisions(project_dir)
    decisions.append(record)
    write_json_atomic(path, {"version": 1, "decisions": decisions})
    return record


def _latest_completed_run(project_id: str) -> dict | None:
    for run in run_ledger.list_runs_for(project_id):
        if run["status"] == "completed":
            return run
    return None


def _sentence_swap(project, project_dir: Path, body: PlanDecisionIn, record: dict):
    """Route a replace decision through Stage 1's evidence-safe
    machinery when a completed run has a chosen visual for the
    sentence; otherwise record a plan-level replacement intent.
    Mutates `record` with the outcome; never raises for domain
    failures (they are recorded as outcomes instead)."""
    from services import media_probe

    replacement = body.replacement
    file_path = (replacement.file_path or "").strip() if replacement else ""
    source_url = (replacement.source_url or "").strip() if replacement else ""

    if file_path and not Path(file_path).is_file():
        record["outcome"] = "replacement_file_missing"
        record["outcome_note"] = (
            "The replacement file does not exist on the server; "
            "nothing was probed or swapped."
        )
        return

    completed = _latest_completed_run(project.id)
    plan = plan_document.build_plan(project.id, project_dir)
    sentence_row = None
    if plan:
        for row in plan["sentences"]:
            if row["sentence_number"] == body.sentence_number:
                sentence_row = row
                break

    if completed is None or sentence_row is None or not file_path:
        # Plan-level intent (pre-run, or a URL-only replacement that
        # cannot be probed/downloaded by the swap machinery).
        if file_path:
            suffix = Path(file_path).suffix.lower()
            probe = (
                media_probe.probe_video(file_path)
                if suffix in VIDEO_SUFFIXES
                else media_probe.probe_image(file_path)
            )
            record["replacement_probe"] = probe
            record["replacement_rights"] = {
                "license": "unknown",
                "rights_status": "verify_before_publish",
                "source": "user_supplied",
            }
            record["outcome"] = (
                "recorded_pre_run" if probe else "replacement_probe_failed"
            )
        else:
            record["outcome"] = "recorded_source_url_only"
            record["outcome_note"] = (
                "A source_url replacement is recorded as intent only: "
                "it is not downloaded, probed or verified until a run "
                "consumes it."
            )
        return

    # --- Post-run swap through apply_review_decisions (MA-33 safe) ---
    from services import media_pipeline

    scene_number = sentence_row.get("scene_number")
    items = plan_document._timeline_items(project_dir)
    position = {}
    for scene in plan.get("scenes") or []:
        for index, number in enumerate(scene.get("sentence_numbers") or []):
            position[number] = (scene.get("scene_number"), index)
    item = None
    if body.sentence_number in position:
        want_scene, want_index = position[body.sentence_number]
        for candidate in items:
            if (
                candidate.get("scene_number") == want_scene
                and candidate.get("sentence_index") == want_index
            ):
                item = candidate
                break
    if item is None:
        for candidate in items:
            if candidate.get("sentence") == sentence_row["text"]:
                item = candidate
                break
    if item is None or scene_number is None:
        record["outcome"] = "recorded_no_chosen_visual"
        record["outcome_note"] = (
            "The completed run has no chosen visual for this "
            "sentence, so there is nothing to swap; the replacement "
            "is recorded as intent."
        )
        return

    suffix = Path(str(item.get("file_path") or item.get("path") or "")).suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        bucket = "videos"
    elif suffix in IMAGE_SUFFIXES:
        bucket = "images"
    else:
        bucket = "visuals"
    original_asset = {
        "file_path": item.get("file_path") or item.get("path"),
        "path": item.get("file_path") or item.get("path"),
        "source_url": item.get("source_url"),
        "source": item.get("source"),
        "title": item.get("title"),
    }
    cache = plan_document._read_json(
        project_dir / "story_understanding_cache.json"
    )
    sentence_obj = None
    bible = None
    if isinstance(cache, dict):
        bible = cache.get("bible")
        plans = cache.get("plans") or []
        if 0 < body.sentence_number <= len(plans):
            sentence_obj = plans[body.sentence_number - 1]
    sentence_result = {
        "sentence_number": body.sentence_number,
        "sentence": sentence_obj,
        "images": [dict(original_asset)] if bucket == "images" else [],
        "videos": [dict(original_asset)] if bucket == "videos" else [],
        "visuals": [dict(original_asset)] if bucket == "visuals" else [],
    }
    # apply_review_decisions consumes ONE scene result (scene_number
    # + sentences + scene-level aggregates), exactly as the
    # pipeline's own wrapper passes it.
    result = {
        "scene_number": scene_number,
        "sentences": [sentence_result],
        "beats": [],
        "images": sentence_result["images"],
        "videos": sentence_result["videos"],
        "visuals": sentence_result["visuals"],
    }

    decisions_path = project_dir / f"scene_{scene_number}_decisions.json"
    existing_rows = plan_document._read_json(decisions_path)
    rows = existing_rows if isinstance(existing_rows, list) else []
    rows = [
        row
        for row in rows
        if not (
            isinstance(row, dict)
            and row.get("sentence_number") == body.sentence_number
        )
    ]
    rows.append(
        {
            "sentence_number": body.sentence_number,
            "beat_id": "",
            "approve": False,
            "swap_path": file_path,
            "title": replacement.title if replacement else None,
        }
    )
    # Stage 4 (DATA-101): this was the decisions writer that still
    # used a bare write_text while _append_plan_decision wrote
    # atomically — a crash mid-write truncated the scene decisions
    # the swap machinery consumes. Both now share the single
    # atomic write path (serialisation preserved: indent=2,
    # ensure_ascii default).
    write_json_atomic(decisions_path, rows, ensure_ascii=True)

    try:
        media_pipeline.apply_review_decisions(result, project_dir, bible=bible)
    except Exception as error:
        # The decision is still recorded (with the failure as its
        # outcome) rather than surfacing as a bare 500: a swap that
        # could not even be evaluated changed nothing, and the
        # record must say so.
        record["outcome"] = "swap_failed_error"
        record["outcome_note"] = (
            f"The evidence-safe swap machinery raised: {error}. "
            "The original visual and its evidence are unchanged."
        )
        return
    outcome_rows = result.get("review_decision_outcomes") or []
    outcome_row = next(
        (
            row
            for row in outcome_rows
            if isinstance(row, dict)
            and row.get("sentence_number") == body.sentence_number
        ),
        outcome_rows[-1] if outcome_rows else None,
    )
    outcome = (outcome_row or {}).get("outcome", "no_outcome")
    record["swap_outcome"] = outcome
    refreshed_bucket = sentence_result[bucket]
    refreshed = refreshed_bucket[0] if refreshed_bucket else None
    if outcome == "swapped" and refreshed is not None:
        record["outcome"] = "swapped"
        record["refreshed_asset"] = {
            "file_path": refreshed.get("file_path"),
            "verification_status": refreshed.get("verification_status"),
            "verification": refreshed.get("verification"),
            "rights_status": refreshed.get("rights_status"),
            "license": refreshed.get("license"),
            "segment_evidence": refreshed.get("segment_evidence"),
        }
        record["ledger_decision_id"] = run_ledger.record_swap_decisions(
            completed["run_id"],
            project.id,
            scene_number=scene_number,
            sentence_number=body.sentence_number,
            original=original_asset,
            refreshed=refreshed,
        )
    else:
        record["outcome"] = outcome
        record["outcome_note"] = (
            "The swap did not take effect (see swap_outcome); the "
            "original visual and its evidence are unchanged."
        )


@router.post("/projects/{project_id}/plan/decisions", status_code=201)
def post_plan_decision(project_id: str, body: PlanDecisionIn, principal=Depends(post_guard)):
    if body.action not in ("approve", "replace", "regenerate"):
        raise ApiError(
            422,
            "invalid_action",
            "action must be one of: approve, replace, regenerate.",
        )
    if body.action == "replace" and not (
        body.replacement
        and ((body.replacement.file_path or "").strip()
             or (body.replacement.source_url or "").strip())
    ):
        raise ApiError(
            422,
            "replacement_required",
            "A replace decision needs replacement.file_path or "
            "replacement.source_url.",
        )
    db = SessionLocal()
    try:
        project = _load_project_for_presentational_surface(
            db, project_id, principal
        )
        _require_admin_for_presentational_surface(principal)
    finally:
        db.close()
    project_dir = media_paths.project_dir(project.id)
    record = {
        "sentence_number": body.sentence_number,
        "action": body.action,
        "recorded_at": _now_iso(),
        "replacement": body.replacement.model_dump() if body.replacement else None,
    }
    if body.action == "approve":
        record["outcome"] = "recorded"
    elif body.action == "regenerate":
        record["outcome"] = "marked_for_regeneration"
        record["outcome_note"] = (
            "Honoured by the next run: plan approval carries this "
            "sentence in the run's options as regenerate_sentences."
        )
    else:
        _sentence_swap(project, project_dir, body, record)
    _append_plan_decision(project_dir, record)
    return record


@router.post("/projects/{project_id}/plan/approve")
def approve_plan(project_id: str, principal=Depends(post_guard)):
    # FS-C (FS-5, A-5): plan approval is an admin-only action —
    # the approval mechanism is preserved for admins (and the
    # operator), while non-admin submissions no longer park at
    # the gate at all (see submit_run), so no ordinary user can
    # be stranded awaiting a release only an admin may give.
    db = SessionLocal()
    try:
        project = _load_project_for_presentational_surface(
            db, project_id, principal
        )
        _require_admin_for_presentational_surface(principal)
        script = project.script
    finally:
        db.close()
    awaiting = None
    for run in run_ledger.list_runs_for(project_id):
        if run["status"] == "awaiting_approval":
            awaiting = run
            break
    if awaiting is None:
        raise ApiError(
            409,
            "no_awaiting_run",
            "No run is awaiting plan approval for this project.",
        )
    options = {**(awaiting.get("options") or {}), "plan_approved": True}
    project_dir = media_paths.project_dir(project_id)
    marks = sorted(
        {
            decision["sentence_number"]
            for decision in plan_document.load_plan_decisions(project_dir)
            if decision.get("action") == "regenerate"
            and isinstance(decision.get("sentence_number"), int)
        }
    )
    if marks:
        options["regenerate_sentences"] = marks
    # Phase 9 (§13.1): the plan gate surfaces the configuration's
    # standing warnings at approval time — chiefly the coverage
    # consequence of disabled motion graphics (beats retrieval
    # cannot cover will be reported uncovered; no graphic
    # fallback exists in this run).
    config_warnings = []
    _awaiting_config = production_config.stored_config(
        awaiting.get("options")
    )
    if _awaiting_config is not None:
        config_warnings = production_config.validate_config(
            _awaiting_config
        )["warnings"]
    job_id = jobs.enqueue_full_pipeline(
        project_id, script, options=options, run_id=awaiting["run_id"]
    )
    return {
        "run": _run_view(run_ledger.get_run(awaiting["run_id"]), principal),
        "job_id": job_id,
        "released": True,
        "regenerate_sentences": marks,
        "config_warnings": config_warnings,
    }


# ---------------------------------------------------------------------
# Phase 9 — Pre-Production Configuration Studio (§§13–17)
# ---------------------------------------------------------------------


class ProductionConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: dict


class VoiceTreatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str
    text: str | None = None


class VoiceApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_SCRIPT_CHARS)


@router.get("/config/catalogue")
def get_config_catalogue(principal=Depends(require_user)):
    return production_config.catalogue()


@router.get("/config/defaults")
def get_config_defaults(principal=Depends(require_user)):
    db = SessionLocal()
    try:
        return {
            "defaults": production_config.load_defaults(db, principal.user_id)
        }
    finally:
        db.close()


@router.put("/config/defaults")
def put_config_defaults(
    body: ProductionConfigIn, principal=Depends(post_guard)
):
    db = SessionLocal()
    try:
        saved = _config_call(
            production_config.save_defaults, db, principal.user_id, body.config
        )
        return {"defaults": saved}
    finally:
        db.close()


@router.delete("/config/defaults")
def delete_config_defaults(principal=Depends(post_guard)):
    db = SessionLocal()
    try:
        production_config.clear_defaults(db, principal.user_id)
        return {"defaults": None}
    finally:
        db.close()


@router.get("/projects/{project_id}/config")
def get_project_config(project_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
        return production_config.project_config_view(project)
    finally:
        db.close()


@router.put("/projects/{project_id}/config")
def put_project_config(
    project_id: str,
    body: ProductionConfigIn,
    principal=Depends(post_guard),
):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(
            db, project_id, principal, write=True
        )
        return _config_call(
            production_config.save_project_config, db, project, body.config
        )
    finally:
        db.close()


@router.post("/projects/{project_id}/config/validate")
def validate_project_config(
    project_id: str,
    body: ProductionConfigIn,
    principal=Depends(require_user),
):
    """Pre-generation validation (§15) without storing: the
    document is cleaned (a malformed document refuses 422) and
    validated against the live system; honourability problems
    are REPORTED in the result, not refused."""
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
    config = _config_call(production_config.clean_config, body.config)
    return {
        "config": config,
        "flat_options": production_config.flat_options(config),
        "validation": production_config.validate_config(config),
    }


@router.post("/projects/{project_id}/config/uploads", status_code=201)
def upload_config_file(
    project_id: str,
    kind: str = Form(...),
    file: UploadFile = File(...),
    principal=Depends(post_guard),
):
    """§13.6/§13.4: the user's own music track or voice
    recording, stored as project media (original preserved,
    probed, hashed) and recorded on the configuration."""
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(
            db, project_id, principal, write=True
        )
        record = _config_call(
            production_config.store_config_upload,
            project_id,
            kind,
            file.filename,
            file.file,
        )
        view = _config_call(
            production_config.attach_upload, db, project, kind, record
        )
        return {"upload": record, "config": view}
    finally:
        db.close()


@router.delete("/projects/{project_id}/config/music-upload")
def delete_config_music_upload(
    project_id: str, principal=Depends(post_guard)
):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(
            db, project_id, principal, write=True
        )
        return _config_call(
            production_config.remove_music_upload, db, project
        )
    finally:
        db.close()


@router.get("/projects/{project_id}/config/voice/transcript")
def get_voice_transcript(project_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
    record = voice_documentary.load_transcript(project_id)
    if record is None:
        raise ApiError(
            404,
            "transcript_not_recorded",
            "No voice transcript has been recorded for this project yet.",
        )
    return record


@router.post("/projects/{project_id}/config/voice/transcribe")
def transcribe_voice(project_id: str, principal=Depends(post_guard)):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(
            db, project_id, principal, write=True
        )
        return _config_call(
            voice_documentary.transcribe_project_voice, project
        )
    finally:
        db.close()


@router.post("/projects/{project_id}/config/voice/treat")
def treat_voice_script(
    project_id: str,
    body: VoiceTreatIn,
    principal=Depends(post_guard),
):
    """One conversion proposal (§13.4). Operates on the text
    supplied (the review surface's current text) or, when none
    is supplied, on the recorded transcript. Stores nothing."""
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal, write=True)
    finally:
        db.close()
    text = body.text
    if text is None:
        record = voice_documentary.load_transcript(project_id)
        if record is None:
            raise ApiError(
                404,
                "transcript_not_recorded",
                "No voice transcript has been recorded for this "
                "project yet; transcribe the recording first.",
            )
        text = record.get("text") or ""
    return _config_call(
        voice_documentary.treat_transcript, text, body.mode
    )


@router.post("/projects/{project_id}/config/voice/apply")
def apply_voice_script(
    project_id: str,
    body: VoiceApplyIn,
    principal=Depends(post_guard),
):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(
            db, project_id, principal, write=True
        )
        return _config_call(
            voice_documentary.apply_script, db, project, principal, body.text
        )
    finally:
        db.close()


# ---------------------------------------------------------------------
# Assets (ledger decisions for the latest run)
#
# FS-B (FS-7): the Asset Decisions ledger is an admin-only
# diagnostic surface, but this endpoint is SHARED with the kept
# Editor flow — the classic Editor's asset library
# (frontend/components/editor/asset-panel.tsx) lists ledger rows
# so a user can reuse a recorded asset in a revision. The
# endpoint therefore stays open to granted principals and is
# restricted BY FIELD for non-admins (the FS-A response-layer
# pattern): admins receive every row byte-identical to before;
# non-admins receive only the editor-reuse fields
# (_editor_ledger_row) — no source URLs, content hashes, asset
# keys, decision chains or other provenance/coverage detail.
# ---------------------------------------------------------------------


@router.get("/projects/{project_id}/assets")
def list_assets(
    project_id: str,
    limit: int = 100,
    offset: int = 0,
    principal=Depends(require_user),
):
    db = SessionLocal()
    try:
        _load_project_for_diagnostic_surface(db, project_id, principal)
    finally:
        db.close()
    limit = min(max(1, limit), 500)
    offset = max(0, offset)
    # The asset listing describes the production ledger: a revision
    # render run must never displace it (S7-D2).
    latest = run_ledger.latest_production_run_for(project_id)
    if latest is None:
        return {"run_id": None, "total": 0, "limit": limit, "offset": offset, "items": []}
    rows, total = run_ledger.list_decisions(latest["run_id"], limit, offset)
    if not principal.is_admin:
        rows = [_editor_ledger_row(row) for row in rows]
    return {
        "run_id": latest["run_id"],
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": rows,
    }


# ---------------------------------------------------------------------
# Outputs, media, certificate
# ---------------------------------------------------------------------


def _output_entry(project_id: str, project_dir: Path, index: dict, filename: str):
    path = project_dir / filename
    if not path.is_file():
        return {"exists": False, "filename": filename}
    key = plan_document.media_key_for(project_dir, path)
    entry = {
        "exists": True,
        "filename": filename,
        "size_bytes": path.stat().st_size,
        "media_key": key if key in index else None,
    }
    if entry["media_key"]:
        entry["media_url"] = f"/api/v1/projects/{project_id}/media/{key}"
    return entry


@router.get("/projects/{project_id}/outputs")
def get_outputs(project_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
        # Phase 8 (§21): the retention block for the project's
        # latest generated video (exact expiry + countdown data +
        # download/deletion status), when one is registered.
        from services.billing import retention

        retention_block = retention.retention_view(db, project_id)
    finally:
        db.close()
    project_dir = media_paths.project_dir(project_id)
    index = plan_document.build_media_index(project_id, project_dir)
    names = plan_document.CANONICAL_OUTPUTS
    return {
        "project_id": project_id,
        "film": _output_entry(project_id, project_dir, index, names["film"]),
        "captions": {
            "json": _output_entry(project_id, project_dir, index, names["captions_json"]),
            "srt": _output_entry(project_id, project_dir, index, names["captions_srt"]),
            "vtt": _output_entry(project_id, project_dir, index, names["captions_vtt"]),
        },
        "asset_report": _output_entry(project_id, project_dir, index, names["asset_report"]),
        "rights_manifest": _output_entry(project_id, project_dir, index, names["rights_manifest"]),
        "run_manifest": _output_entry(project_id, project_dir, index, names["run_manifest"]),
        # FS-H: the certificate and plan are admin-only surfaces
        # (FS-9/FS-5) — their URLs must not surface to non-admin
        # session principals through this kept, non-gated payload.
        # Admins and developer-API principals (their own scope
        # model, FS-B precedent) keep the links.
        "certificate_url": (
            f"/api/v1/projects/{project_id}/certificate"
            if principal.is_admin
            or getattr(principal, "auth_method", None) == "api_key"
            else None
        ),
        "plan_url": (
            f"/api/v1/projects/{project_id}/plan"
            if principal.is_admin
            or getattr(principal, "auth_method", None) == "api_key"
            else None
        ),
        "retention": retention_block,
    }


@router.get("/projects/{project_id}/media/{asset_key}")
def serve_media(project_id: str, asset_key: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
    project_dir = media_paths.project_dir(project_id)
    index = plan_document.build_media_index(project_id, project_dir)
    entry = index.get(asset_key)
    if entry is None:
        raise ApiError(
            404,
            "media_not_found",
            "No recorded media exists for that key on this project.",
        )
    # Phase 8 (§21): record downloads of registered generated
    # videos (never extends their deletion deadline). Fail-soft.
    try:
        from services.billing import retention

        retention.record_download_for_path(entry["path"])
    except Exception:
        pass
    return FileResponse(
        entry["path"],
        media_type=entry["content_type"],
        filename=entry["file_name"],
    )


def _read_project_json(project_dir: Path, filename: str, run_id=None):
    # Stage 4 (DATA-004): derived artefacts are run-scoped for new
    # runs — resolve to the serving run's copy when a run is known,
    # else the latest-run view, with the legacy project-level path
    # as fallback. Absence handling is unchanged.
    try:
        return json.loads(
            media_paths.resolve_artefact(
                project_dir, filename, run_id
            ).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None


def _planner_block(project_id: str, run_id=None):
    """Master Directive Phase 7 / P0 (DM-1): the production's
    recorded planner self-audit block, read from the run's
    plan.json exactly as the production flow recorded it — the
    certificate states the planner mode / version / degraded
    reason beside the run it describes. None when the production
    predates the block or the artefact is unreadable (the
    certificate then simply carries no planner fields)."""
    project_dir = media_paths.project_dir(project_id)
    plan = _read_project_json(project_dir, "plan.json", run_id)
    if isinstance(plan, dict) and isinstance(plan.get("planner"), dict):
        return plan["planner"]
    return None


def _quality_section(project_id: str, run_id=None) -> dict:
    """Quality Recovery Phase 5: the production's recorded quality
    report, surfaced on the certificate exactly as the production
    flow recorded it. jobs.py writes production_report.json (the
    three verdicts + cross-artefact reconciliation) and
    visual_quality.json (the measurement) into the production's
    artefact directory — the run's runs/<run_id>/ since Stage 4
    (DATA-004), the project directory for earlier productions —
    and embeds both in the asset report. This endpoint
    recomputes nothing and invents nothing:

    - no production_report.json — a production that predates the
      report, or one whose measurement failed (jobs.py records
      that failure in the asset report instead) -> "not_recorded";
    - the file exists but cannot be parsed -> "unavailable";
    - otherwise -> "recorded": the verdicts pass through untouched
      (PASS / QUALITY_WARNING / FAIL / UNKNOWN, or null where a
      verdict itself was not computable) with a summary of the
      measurement (diversity D1/D2 and the per-sentence outcome
      accounting, without the per-sentence listing).
    """
    empty = {
        "schema": None,
        "verdicts": None,
        "metrics": None,
        "reconciliation": None,
    }
    project_dir = media_paths.project_dir(project_id)
    # Stage 4 (DATA-004): the quality artefacts belong to the run
    # the certificate describes — resolve them in that run's
    # artefact directory (legacy project-level files as fallback),
    # so a later re-run cannot change this run's recorded verdicts.
    if not media_paths.resolve_artefact(
        project_dir, "production_report.json", run_id
    ).is_file():
        return {"state": "not_recorded", **empty}
    report = _read_project_json(
        project_dir, "production_report.json", run_id
    )
    if not isinstance(report, dict):
        return {"state": "unavailable", **empty}
    reconciliation = report.get("reconciliation") or {}
    checks = reconciliation.get("checks") or []
    metrics = None
    measurement = _read_project_json(
        project_dir, "visual_quality.json", run_id
    )
    if isinstance(measurement, dict):
        outcomes = measurement.get("sentence_outcomes") or {}
        timeline_block = measurement.get("timeline") or {}
        metrics = {
            "diversity": measurement.get("diversity"),
            "sentence_outcomes": {
                "sentence_count": outcomes.get("sentence_count"),
                "buckets": outcomes.get("buckets"),
                "shares": outcomes.get("shares"),
                "reconciles": outcomes.get("reconciles"),
                "sentences_without_verified_visual": outcomes.get(
                    "sentences_without_verified_visual"
                ),
            }
            if outcomes
            else None,
            "timeline_item_count": timeline_block.get("timeline_item_count"),
            "max_reuse_count": timeline_block.get("max_reuse_count"),
        }
    return {
        "state": "recorded",
        "schema": report.get("schema"),
        "verdicts": report.get("verdicts"),
        "metrics": metrics,
        "reconciliation": {
            "all_reconciled": reconciliation.get("all_reconciled"),
            "failures": reconciliation.get("failures") or [],
            "checks": [
                {"name": check.get("name"), "status": check.get("status")}
                for check in checks
                if isinstance(check, dict)
            ],
        },
    }


@router.get("/projects/{project_id}/certificate")
def get_certificate(project_id: str, principal=Depends(require_user)):
    # FS-B (FS-9): the Delivery & Provenance certificate — delivery
    # records, provenance and licence-evidence detail — is an
    # admin-only diagnostic surface. The ownership choke point runs
    # first (non-granted principals keep the Phase 1a 404); a
    # granted non-admin then gets 403. The shared GET .../outputs
    # endpoint is NOT gated: the Overview's film/caption downloads
    # are a kept user flow (FS-DISCOVERY §3.1).
    db = SessionLocal()
    try:
        project = _load_project_for_diagnostic_surface(
            db, project_id, principal
        )
        _require_admin_for_diagnostic_surface(principal)
        project_view = {
            "id": project.id,
            "title": project.title,
            "category": project.category,
        }
    finally:
        db.close()
    # Production runs only: after a revision render, the newest run
    # of any kind is the render run, and selecting it here would
    # describe an empty render instead of the production (S7-D2).
    latest = run_ledger.latest_production_run_for(project_id)
    if latest is None:
        raise ApiError(
            404,
            "no_runs",
            "This project has no production runs, so no provenance "
            "certificate exists.",
        )
    # The run certificate describes the ORIGINAL production only:
    # editor-appended decisions (revision_id-tagged, migration 0004)
    # are excluded, so editing a revision can never change this
    # certificate (Stage 6 adversarial defect D3). Per-revision
    # certificates live on the editor endpoints below.
    decisions, _total = run_ledger.list_decisions(
        latest["run_id"], 100_000, 0, include_revision_decisions=False
    )
    counts = run_ledger.decision_counts(
        latest["run_id"], include_revision_decisions=False
    )
    certificate = plan_document.build_certificate(
        project_view, latest, decisions, counts,
        planner=_planner_block(project_id, run_id=latest["run_id"]),
    )
    # Quality Recovery Phase 5: surface the recorded production
    # quality report (Phase 3) alongside the ledger certificate.
    # Additive only — the ledger sections are unchanged, and the
    # quality artefacts are production records the editor never
    # writes, so the original-certificate guarantees (Stage 6 D3)
    # are unaffected.
    certificate["quality"] = _quality_section(
        project_id, run_id=latest["run_id"]
    )
    return certificate


# ---------------------------------------------------------------------------
# QA Report (Phase F, founder priority #8) — the unified Documentary
# Quality Report over the records a production already wrote
# (services/qa_report.py). Assembly only: verdicts and metrics pass
# through exactly as recorded, sections whose artefacts were never
# written report "not_recorded", and nothing is recomputed. Every
# route loads the project through the Phase 8 owner choke point
# first: another user's project — or a run of it — is a 404.
#
# FS-B (FS-10): the QA report READ routes are admin-only diagnostic
# surfaces — after the ownership choke point, a granted non-admin
# principal gets 403 (_require_admin_for_diagnostic_surface). Only
# read access is gated: QA generation and the verdicts' influence
# on production acceptance are pipeline-internal and untouched.
# ---------------------------------------------------------------------------


def _qa_report_for_run(project_id: str, run: dict) -> dict:
    return qa_report.build_qa_report(project_id, run)


def _owned_run_in_project_or_404(project_id: str, run_id: str) -> dict:
    run = run_ledger.get_run(run_id)
    if run is None or run.get("project_id") != project_id:
        raise ApiError(
            404, "run_not_found", f"No run {run_id} in this project."
        )
    return run


@router.get("/projects/{project_id}/qa-report")
def get_latest_qa_report(project_id: str, principal=Depends(require_user)):
    """The QA report for the project's latest PRODUCTION run (the
    same run selection the certificate uses, so a revision render
    can never displace the production the report describes)."""
    db = SessionLocal()
    try:
        _load_project_for_diagnostic_surface(db, project_id, principal)
        _require_admin_for_diagnostic_surface(principal)
    finally:
        db.close()
    latest = run_ledger.latest_production_run_for(project_id)
    if latest is None:
        raise ApiError(
            404,
            "no_runs",
            "This project has no production runs, so no QA report "
            "exists.",
        )
    return _qa_report_for_run(project_id, latest)


@router.get("/projects/{project_id}/runs/{run_id}/qa-report")
def get_run_qa_report(
    project_id: str, run_id: str, principal=Depends(require_user)
):
    db = SessionLocal()
    try:
        _load_project_for_diagnostic_surface(db, project_id, principal)
        _require_admin_for_diagnostic_surface(principal)
    finally:
        db.close()
    run = _owned_run_in_project_or_404(project_id, run_id)
    return _qa_report_for_run(project_id, run)


@router.get("/projects/{project_id}/runs/{run_id}/qa-report/export")
def export_run_qa_report(
    project_id: str, run_id: str, principal=Depends(require_user)
):
    """The same recorded report as a downloadable JSON file —
    generated from the same assembly, so the export can never
    disagree with the on-screen report."""
    db = SessionLocal()
    try:
        _load_project_for_diagnostic_surface(db, project_id, principal)
        _require_admin_for_diagnostic_surface(principal)
    finally:
        db.close()
    run = _owned_run_in_project_or_404(project_id, run_id)
    report = _qa_report_for_run(project_id, run)
    return JSONResponse(
        content=report,
        headers={
            "Content-Disposition": (
                f'attachment; filename="qa-report-{project_id}-{run_id}.json"'
            )
        },
    )


# ---------------------------------------------------------------------------
# Project Intelligence (founder priority #10) — the assembly layer
# over the persistent project record (services/project_intelligence
# .py): entity connections derived from the Case Bibles with their
# derivations labelled, recorded provider/quality patterns across
# the caller's own projects, and deterministic recommendations
# carrying their evidence. The per-project route loads the project
# through the Phase 8 owner choke point first (another user's
# project is a 404); the account route is principal-scoped and
# reads only the caller's own projects, so no other user's data
# can enter any aggregate — not even a count.
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/intelligence")
def get_project_intelligence(project_id: str, principal=Depends(require_user)):
    # FS-C (FS-13): project Intelligence is an admin-only
    # presentational surface — choke point first, then admin.
    db = SessionLocal()
    try:
        _load_project_for_presentational_surface(db, project_id, principal)
        _require_admin_for_presentational_surface(principal)
    finally:
        db.close()
    return project_intelligence.build_project_intelligence(
        project_id, principal.user_id
    )


@router.get("/intelligence")
def get_account_intelligence(principal=Depends(require_user)):
    # FS-C (FS-13): account Intelligence is admin-only. The
    # service stays principal-scoped (an admin sees only their
    # own catalogue's aggregates), exactly as before.
    _require_admin_for_presentational_surface(principal)
    return project_intelligence.build_account_intelligence(principal.user_id)


# ---------------------------------------------------------------------------
# Versioning (Phase G, founder priority #9) — the unified version
# story over the recorded histories (services/versioning.py): script
# versions snapshotted at submission with list/diff/restore, the
# run + revision + plan timeline with per-entry changes, and named
# project snapshots (script + plan + revision pointer) with a
# restore that branches the revision chain instead of mutating it.
# Every route loads the project through the Phase 8 owner choke
# point first: another user's project is a 404 on reads AND writes.
# ---------------------------------------------------------------------------


def _versioning_call(fn, *args, **kwargs):
    """Run a versioning service call, mapping VersioningError onto
    the v1 error shape."""
    try:
        return fn(*args, **kwargs)
    except versioning.VersioningError as error:
        message = error.message
        if error.details:
            message = f"{message} Details: {json.dumps(error.details)}"
        raise ApiError(error.status_code, error.code, message) from error


def _owned_project_dict(project_id: str, principal, *, write: bool = False) -> dict:
    # FS-C (FS-11): Versioning & Recovery is an admin-only
    # presentational surface, so this versioning-only loader pairs
    # the presentational load with the admin check. The pipeline's
    # own snapshot writes go through services/versioning directly
    # and are unaffected. `write` is retained for call-site
    # symmetry; the admin check refuses every non-admin regardless.
    db = SessionLocal()
    try:
        project = _load_project_for_presentational_surface(
            db, project_id, principal
        )
        _require_admin_for_presentational_surface(principal)
        return {
            "id": project.id,
            "script": project.script,
            "title": project.title,
            "category": project.category,
        }
    finally:
        db.close()


class SnapshotCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class VersionLabelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: str = Field(min_length=1, max_length=300)
    name: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


@router.get("/projects/{project_id}/versions")
def get_versions(project_id: str, principal=Depends(require_user)):
    project = _owned_project_dict(project_id, principal)
    return _versioning_call(
        versioning.build_versions_view, project_id, project
    )


@router.get("/projects/{project_id}/versions/scripts/{script_hash}")
def get_script_version(
    project_id: str, script_hash: str, principal=Depends(require_user)
):
    project = _owned_project_dict(project_id, principal)
    return _versioning_call(
        versioning.get_script_version, project_id, project, script_hash
    )


@router.post(
    "/projects/{project_id}/versions/scripts/{script_hash}/restore"
)
def restore_script_version(
    project_id: str, script_hash: str, principal=Depends(post_guard)
):
    project = _owned_project_dict(project_id, principal, write=True)
    return _versioning_call(
        versioning.restore_script_version,
        project_id,
        project,
        script_hash,
        actor=principal.user_id,
    )


@router.get("/projects/{project_id}/versions/compare")
def compare_versions(
    project_id: str, a: str, b: str, principal=Depends(require_user)
):
    project = _owned_project_dict(project_id, principal)
    return _versioning_call(
        versioning.compare_versions, project_id, project, a, b
    )


@router.post(
    "/projects/{project_id}/versions/snapshots", status_code=201
)
def create_snapshot(
    project_id: str,
    body: SnapshotCreateIn,
    principal=Depends(post_guard),
):
    project = _owned_project_dict(project_id, principal, write=True)
    return _versioning_call(
        versioning.create_snapshot,
        project_id,
        project,
        body.name.strip(),
        body.note,
        actor=principal.user_id,
    )


@router.get("/projects/{project_id}/versions/snapshots/{snapshot_id}")
def get_snapshot(
    project_id: str, snapshot_id: str, principal=Depends(require_user)
):
    _owned_project_dict(project_id, principal)
    return _versioning_call(
        versioning.get_snapshot, project_id, snapshot_id
    )


@router.delete("/projects/{project_id}/versions/snapshots/{snapshot_id}")
def delete_snapshot(
    project_id: str, snapshot_id: str, principal=Depends(post_guard)
):
    _owned_project_dict(project_id, principal, write=True)
    return _versioning_call(
        versioning.delete_snapshot, project_id, snapshot_id
    )


@router.post(
    "/projects/{project_id}/versions/snapshots/{snapshot_id}/restore"
)
def restore_snapshot(
    project_id: str, snapshot_id: str, principal=Depends(post_guard)
):
    project = _owned_project_dict(project_id, principal, write=True)
    return _versioning_call(
        versioning.restore_snapshot,
        project_id,
        project,
        snapshot_id,
        actor=principal.user_id,
    )


@router.put("/projects/{project_id}/versions/labels")
def set_version_label(
    project_id: str,
    body: VersionLabelIn,
    principal=Depends(post_guard),
):
    project = _owned_project_dict(project_id, principal, write=True)
    return _versioning_call(
        versioning.set_label,
        project_id,
        project,
        body.entry_id,
        body.name,
        body.note,
        actor=principal.user_id,
    )


# ---------------------------------------------------------------------------
# Research Studio (Phase D, founder priority #6) — the research
# workspace over the existing engine (services/research_studio.py).
# Every route loads the project through the Phase 8 owner choke
# point first: another user's project is a 404 on reads AND writes.
#
# FS-C (FS-6): the Research & Source Library surface is
# ADMIN-ONLY — every route below loads through
# _require_project_for_presentational_surface (choke point, then
# the admin check). services/research_studio.py and the pipeline's
# in-process research hooks (jobs.py's entity-correction pass)
# are untouched.
# ---------------------------------------------------------------------------


def _research_call(fn, *args, **kwargs):
    """Run a research-studio service call, mapping ResearchError
    onto the v1 error shape."""
    try:
        return fn(*args, **kwargs)
    except research_studio.ResearchError as error:
        message = error.message
        if error.details:
            message = f"{message} Details: {json.dumps(error.details)}"
        raise ApiError(error.status_code, error.code, message) from error


def _require_owned_project(project_id: str, principal, *, write: bool = False) -> None:
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal, write=write)
    finally:
        db.close()


class ResearchSessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, max_length=500)


class SourceStateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str


class CorrectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=1)
    field: str
    value: str = Field(min_length=1)


class ExternalSourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    url: str = Field(min_length=1)


class CitationClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    fact_id: str | None = None
    sentence_number: int | None = None
    label: str | None = None


class CitationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: CitationClaimIn
    source_id: str | None = None
    external: ExternalSourceIn | None = None
    note: str | None = None


class TimelineEventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: str = Field(min_length=1)
    what: str = Field(min_length=1)
    source_ids: list[str] = Field(default_factory=list)


class TimelineEventUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: str | None = None
    what: str | None = None
    source_ids: list[str] | None = None


class SummaryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None


@router.get("/projects/{project_id}/research")
def get_research_overview(project_id: str, principal=Depends(require_user)):
    _require_project_for_presentational_surface(project_id, principal)
    return _research_call(research_studio.overview, project_id)


@router.get("/projects/{project_id}/research/bible")
def get_research_bible(project_id: str, principal=Depends(require_user)):
    _require_project_for_presentational_surface(project_id, principal)
    return _research_call(research_studio.bible_view, project_id)


@router.get("/projects/{project_id}/research/sources")
def get_research_sources(
    project_id: str,
    state: str | None = None,
    q: str | None = None,
    principal=Depends(require_user),
):
    _require_project_for_presentational_surface(project_id, principal)
    return _research_call(
        research_studio.list_sources, project_id, state=state, query=q
    )


@router.post("/projects/{project_id}/research/sessions", status_code=201)
def create_research_session(
    project_id: str, body: ResearchSessionIn, principal=Depends(post_guard)
):
    _require_project_for_presentational_surface(project_id, principal, write=True)
    return _research_call(research_studio.run_session, project_id, body.topic)


@router.get("/projects/{project_id}/research/sessions/{session_id}")
def get_research_session(
    project_id: str, session_id: str, principal=Depends(require_user)
):
    _require_project_for_presentational_surface(project_id, principal)
    return _research_call(research_studio.get_session, project_id, session_id)


@router.post("/projects/{project_id}/research/sources/{source_id}/state")
def set_research_source_state(
    project_id: str,
    source_id: str,
    body: SourceStateIn,
    principal=Depends(post_guard),
):
    _require_project_for_presentational_surface(project_id, principal, write=True)
    return _research_call(
        research_studio.set_source_state, project_id, source_id, body.state
    )


@router.post("/projects/{project_id}/research/sources/external", status_code=201)
def add_research_external_source(
    project_id: str, body: ExternalSourceIn, principal=Depends(post_guard)
):
    _require_project_for_presentational_surface(project_id, principal, write=True)
    return _research_call(
        research_studio.add_external_source,
        project_id,
        body.title or "",
        body.url,
    )


@router.post("/projects/{project_id}/research/fact-check")
def run_research_fact_check(project_id: str, principal=Depends(post_guard)):
    _require_project_for_presentational_surface(project_id, principal, write=True)
    return _research_call(research_studio.run_fact_check, project_id)


@router.post("/projects/{project_id}/research/entities/{entity_id}/check")
def check_research_entity(
    project_id: str, entity_id: str, principal=Depends(post_guard)
):
    _require_project_for_presentational_surface(project_id, principal, write=True)
    return _research_call(research_studio.check_entity, project_id, entity_id)


@router.post("/projects/{project_id}/research/corrections", status_code=201)
def create_research_correction(
    project_id: str, body: CorrectionIn, principal=Depends(post_guard)
):
    _require_project_for_presentational_surface(project_id, principal, write=True)
    return _research_call(
        research_studio.add_correction,
        project_id,
        body.entity_id,
        body.field,
        body.value,
    )


@router.delete("/projects/{project_id}/research/corrections/{correction_id}")
def delete_research_correction(
    project_id: str, correction_id: str, principal=Depends(post_guard)
):
    _require_project_for_presentational_surface(project_id, principal, write=True)
    _research_call(research_studio.delete_correction, project_id, correction_id)
    return {"deleted": True, "correction_id": correction_id}


@router.get("/projects/{project_id}/research/citations")
def get_research_citations(project_id: str, principal=Depends(require_user)):
    _require_project_for_presentational_surface(project_id, principal)
    return {"citations": _research_call(research_studio.list_citations, project_id)}


@router.post("/projects/{project_id}/research/citations", status_code=201)
def create_research_citation(
    project_id: str, body: CitationIn, principal=Depends(post_guard)
):
    _require_project_for_presentational_surface(project_id, principal, write=True)
    return _research_call(
        research_studio.add_citation,
        project_id,
        body.claim.model_dump(),
        source_id=body.source_id,
        external=body.external.model_dump() if body.external else None,
        note=body.note,
    )


@router.delete("/projects/{project_id}/research/citations/{citation_id}")
def delete_research_citation(
    project_id: str, citation_id: str, principal=Depends(post_guard)
):
    _require_project_for_presentational_surface(project_id, principal, write=True)
    _research_call(research_studio.delete_citation, project_id, citation_id)
    return {"deleted": True, "citation_id": citation_id}


@router.get("/projects/{project_id}/research/bibliography")
def get_research_bibliography(project_id: str, principal=Depends(require_user)):
    _require_project_for_presentational_surface(project_id, principal)
    return _research_call(research_studio.bibliography, project_id)


@router.post("/projects/{project_id}/research/timeline-events", status_code=201)
def create_research_timeline_event(
    project_id: str, body: TimelineEventIn, principal=Depends(post_guard)
):
    _require_project_for_presentational_surface(project_id, principal, write=True)
    return _research_call(
        research_studio.add_timeline_event,
        project_id,
        body.when,
        body.what,
        body.source_ids,
    )


@router.put("/projects/{project_id}/research/timeline-events/{event_id}")
def update_research_timeline_event(
    project_id: str,
    event_id: str,
    body: TimelineEventUpdateIn,
    principal=Depends(post_guard),
):
    _require_project_for_presentational_surface(project_id, principal, write=True)
    return _research_call(
        research_studio.update_timeline_event,
        project_id,
        event_id,
        when=body.when,
        what=body.what,
        source_ids=body.source_ids,
    )


@router.delete("/projects/{project_id}/research/timeline-events/{event_id}")
def delete_research_timeline_event(
    project_id: str, event_id: str, principal=Depends(post_guard)
):
    _require_project_for_presentational_surface(project_id, principal, write=True)
    _research_call(research_studio.delete_timeline_event, project_id, event_id)
    return {"deleted": True, "event_id": event_id}


@router.get("/projects/{project_id}/research/summaries")
def get_research_summaries(project_id: str, principal=Depends(require_user)):
    _require_project_for_presentational_surface(project_id, principal)
    return {"summaries": _research_call(research_studio.list_summaries, project_id)}


@router.post("/projects/{project_id}/research/summaries", status_code=201)
def create_research_summary(
    project_id: str, body: SummaryIn, principal=Depends(post_guard)
):
    _require_project_for_presentational_surface(project_id, principal, write=True)
    return _research_call(
        research_studio.generate_summary, project_id, body.session_id
    )


# ---------------------------------------------------------------------------
# YouTube Script Studio (founder priority #12) — the writing
# workspace over services/script_studio.py: per-owner draft
# documents, AI proposals through llm_provider, the pipeline's own
# analysis + fact check, and send-to-production through the normal
# project creation path. Drafts live in the caller's own store, so
# another user's draft id is simply absent — a 404, never a leak.
# ---------------------------------------------------------------------------


def _script_call(fn, *args, **kwargs):
    """Run a script-studio service call, mapping ScriptError onto
    the v1 error shape."""
    try:
        return fn(*args, **kwargs)
    except script_studio.ScriptError as error:
        message = error.message
        if error.details:
            message = f"{message} Details: {json.dumps(error.details)}"
        raise ApiError(error.status_code, error.code, message) from error


def _billed_script_call(operation, fn, user_id, *args, **kwargs):
    """Phase 8 (§7): a Script Studio AI operation consumes one
    script allowance unit or debits its credit price BEFORE it
    runs; any failure refunds in full (services.billing
    .operations). Legacy-plan users are unmetered."""
    from services.billing import operations

    receipt = operations.begin_operation(user_id, "scripts", operation)
    try:
        return _script_call(fn, user_id, *args, **kwargs)
    except Exception:
        operations.refund_operation(user_id, receipt)
        raise


class ScriptSectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    heading: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=60_000)


class ScriptDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=300)
    category: str | None = Field(default=None, max_length=120)
    target_minutes: float | None = None
    sections: list[ScriptSectionIn] | None = None


class ScriptDraftUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=300)
    category: str | None = Field(default=None, max_length=120)
    target_minutes: float | None = None
    clear_target: bool = False
    sections: list[ScriptSectionIn] | None = None


class ScriptGenerateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, max_length=2_000)
    angle: str | None = Field(default=None, max_length=2_000)
    category: str | None = Field(default=None, max_length=120)
    target_minutes: float | None = None


class ScriptTightenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str = Field(min_length=1)
    text: str | None = Field(default=None, max_length=60_000)


class ScriptAttachIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)


def _sections_payload(sections):
    if sections is None:
        return None
    return [s.model_dump() for s in sections]


@router.get("/script-studio/scripts")
def list_script_drafts(principal=Depends(require_user)):
    return _script_call(script_studio.list_drafts, principal.user_id)


@router.post("/script-studio/scripts", status_code=201)
def create_script_draft(body: ScriptDraftIn, principal=Depends(post_guard)):
    return _script_call(
        script_studio.create_draft,
        principal.user_id,
        title=body.title,
        category=body.category,
        target_minutes=body.target_minutes,
        sections=_sections_payload(body.sections),
    )


@router.get("/script-studio/scripts/{script_id}")
def get_script_draft(script_id: str, principal=Depends(require_user)):
    return _script_call(script_studio.get_draft, principal.user_id, script_id)


@router.put("/script-studio/scripts/{script_id}")
def update_script_draft(
    script_id: str, body: ScriptDraftUpdateIn, principal=Depends(post_guard)
):
    target = None if body.clear_target else body.target_minutes
    target_arg = target if (body.clear_target or body.target_minutes is not None) else "__unset__"
    return _script_call(
        script_studio.update_draft,
        principal.user_id,
        script_id,
        title=body.title,
        category=body.category,
        target_minutes=target_arg,
        sections=_sections_payload(body.sections),
    )


@router.delete("/script-studio/scripts/{script_id}")
def delete_script_draft(script_id: str, principal=Depends(post_guard)):
    return _script_call(script_studio.delete_draft, principal.user_id, script_id)


@router.get("/script-studio/scripts/{script_id}/revisions")
def list_script_revisions(script_id: str, principal=Depends(require_user)):
    return _script_call(
        script_studio.list_revisions, principal.user_id, script_id
    )


@router.post("/script-studio/scripts/{script_id}/revisions/{revision_id}/restore")
def restore_script_revision(
    script_id: str, revision_id: str, principal=Depends(post_guard)
):
    return _script_call(
        script_studio.restore_revision, principal.user_id, script_id, revision_id
    )


@router.post("/script-studio/scripts/{script_id}/analysis")
def run_script_analysis(script_id: str, principal=Depends(post_guard)):
    return _billed_script_call(
        "analysis", script_studio.run_analysis, principal.user_id,
        script_id,
    )


@router.post("/script-studio/scripts/{script_id}/fact-check")
def run_script_fact_check(script_id: str, principal=Depends(post_guard)):
    return _billed_script_call(
        "fact_check", script_studio.run_fact_check,
        principal.user_id, script_id,
    )


@router.post("/script-studio/ai/draft", status_code=201)
def generate_script_draft(body: ScriptGenerateIn, principal=Depends(post_guard)):
    return _billed_script_call(
        "draft",
        script_studio.generate_draft,
        principal.user_id,
        topic=body.topic,
        angle=body.angle or "",
        category=body.category or "",
        target_minutes=body.target_minutes,
    )


@router.post("/script-studio/scripts/{script_id}/ai/expand")
def expand_script_sections(script_id: str, principal=Depends(post_guard)):
    return _billed_script_call(
        "expand", script_studio.expand_sections, principal.user_id,
        script_id,
    )


@router.post("/script-studio/scripts/{script_id}/ai/tighten")
def tighten_script_passage(
    script_id: str, body: ScriptTightenIn, principal=Depends(post_guard)
):
    return _billed_script_call(
        "tighten",
        script_studio.tighten_passage,
        principal.user_id,
        script_id,
        section_id=body.section_id,
        text=body.text,
    )


@router.post("/script-studio/scripts/{script_id}/send-to-production", status_code=201)
def send_script_to_production(script_id: str, principal=Depends(post_guard)):
    return _script_call(
        script_studio.send_to_production, principal.user_id, script_id
    )


@router.post("/script-studio/scripts/{script_id}/attach")
def attach_script_to_project(
    script_id: str, body: ScriptAttachIn, principal=Depends(post_guard)
):
    _require_owned_project(body.project_id, principal, write=True)
    return _script_call(
        script_studio.attach_to_project,
        principal.user_id,
        script_id,
        body.project_id,
    )


# ---------------------------------------------------------------------------
# Templates & production presets (founder priority #15) — named
# bundles over services/templates.py: built-ins derived live from
# the real genre packs, user templates in the caller's own
# preferences document (another user's id is a 404). Applying a
# template goes through the SAME creation paths as manual work:
# apply-project validates through ProjectCreateIn and creates via
# _create_project_record; apply-draft creates through the Script
# Studio's own create path. Literal routes are declared before
# the {template_id} routes so they can never be shadowed by one.
#
# FS-C (FS-1): the Templates page is an admin-only surface, so
# every route in this family requires an admin principal (403 for
# ordinary users — these are account-scoped routes over the
# caller's own templates, so there is no tenant-private 404 to
# preserve). Consumer evidence for gating the two apply routes
# too: the only frontend callers of apply-draft / apply-project
# are the Templates page itself (grep-verified); Script Studio
# and project creation never call them — they use the service /
# _create_project_record paths in-process, which stay ungated.
# services/templates.py is untouched.
# ---------------------------------------------------------------------------


def _template_call(fn, *args, **kwargs):
    """Run a templates service call, mapping TemplateError (and a
    Script Studio error surfacing through apply-to-draft) onto the
    v1 error shape."""
    try:
        return fn(*args, **kwargs)
    except templates_service.TemplateError as error:
        message = error.message
        if error.details:
            message = f"{message} Details: {json.dumps(error.details)}"
        raise ApiError(error.status_code, error.code, message) from error
    except script_studio.ScriptError as error:
        raise ApiError(error.status_code, error.code, error.message) from error


class TemplateSectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: str | None = Field(default=None, max_length=200)


class TemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=120)
    target_minutes: float | None = None
    options: dict | None = None
    sections: list[TemplateSectionIn] | None = None


class TemplateUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=120)
    target_minutes: float | None = None
    clear_target: bool = False
    options: dict | None = None
    sections: list[TemplateSectionIn] | None = None


class TemplateFromProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    name: str | None = Field(default=None, max_length=200)


class TemplateFromDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_id: str = Field(min_length=1)
    name: str | None = Field(default=None, max_length=200)


class TemplateCopyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)


class TemplateApplyDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=300)


class TemplateApplyProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=300)
    script: str = Field(min_length=1, max_length=MAX_SCRIPT_CHARS)


def _template_sections(body_sections):
    if body_sections is None:
        return None
    return [{"heading": s.heading} for s in body_sections]


@router.get("/templates")
def list_templates(principal=Depends(require_user)):
    # FS-C (FS-1): Templates is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _template_call(templates_service.list_templates, db, principal)
    finally:
        db.close()


@router.post("/templates", status_code=201)
def create_template(body: TemplateIn, principal=Depends(post_guard)):
    # FS-C (FS-1): Templates is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _template_call(
            templates_service.create_template,
            db,
            principal,
            name=body.name,
            category=body.category,
            target_minutes=body.target_minutes,
            options=body.options,
            sections=_template_sections(body.sections),
        )
    finally:
        db.close()


@router.post("/templates/from-project", status_code=201)
def create_template_from_project(
    body: TemplateFromProjectIn, principal=Depends(post_guard)
):
    # FS-C (FS-1): Templates is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, body.project_id, principal)
        view, dropped = _template_call(
            templates_service.template_from_project,
            db,
            principal,
            project,
            name=body.name,
        )
        return {"template": view, "dropped_options": dropped}
    finally:
        db.close()


@router.post("/templates/from-draft", status_code=201)
def create_template_from_draft(
    body: TemplateFromDraftIn, principal=Depends(post_guard)
):
    # FS-C (FS-1): Templates is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _template_call(
            templates_service.template_from_draft,
            db,
            principal,
            body.script_id,
            name=body.name,
        )
    finally:
        db.close()


@router.post("/templates/builtins/{pack_id}/copy", status_code=201)
def copy_builtin_template(
    pack_id: str, body: TemplateCopyIn, principal=Depends(post_guard)
):
    # FS-C (FS-1): Templates is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _template_call(
            templates_service.copy_builtin, db, principal, pack_id, name=body.name
        )
    finally:
        db.close()


@router.get("/templates/{template_id}")
def get_template(template_id: str, principal=Depends(require_user)):
    # FS-C (FS-1): Templates is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _template_call(
            templates_service.resolve_template, db, principal, template_id
        )
    finally:
        db.close()


@router.patch("/templates/{template_id}")
def update_template(
    template_id: str, body: TemplateUpdateIn, principal=Depends(post_guard)
):
    # FS-C (FS-1): Templates is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _template_call(
            templates_service.update_template,
            db,
            principal,
            template_id,
            name=body.name,
            category=body.category,
            target_minutes=None if body.clear_target else (
                body.target_minutes if body.target_minutes is not None else "__unset__"
            ),
            options=body.options,
            sections=_template_sections(body.sections),
        )
    finally:
        db.close()


@router.delete("/templates/{template_id}")
def delete_template(template_id: str, principal=Depends(post_guard)):
    # FS-C (FS-1): Templates is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _template_call(
            templates_service.delete_template, db, principal, template_id
        )
    finally:
        db.close()


@router.post("/templates/{template_id}/apply-draft", status_code=201)
def apply_template_to_draft(
    template_id: str, body: TemplateApplyDraftIn, principal=Depends(post_guard)
):
    # FS-C (FS-1): Templates is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _template_call(
            templates_service.apply_to_draft,
            db,
            principal,
            template_id,
            title=body.title,
        )
    finally:
        db.close()


@router.post("/templates/{template_id}/apply-project", status_code=201)
def apply_template_to_project(
    template_id: str, body: TemplateApplyProjectIn, principal=Depends(post_guard)
):
    # FS-C (FS-1): Templates is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        payload = _template_call(
            templates_service.template_creation_payload, db, principal, template_id
        )
        # The creation path's own validation, unchanged: the
        # template's bundle is fed through ProjectCreateIn (whose
        # ProjectOptionsIn field ranges are the create form's) and
        # created by the same helper manual creation uses — plan
        # script-length enforcement included.
        try:
            validated = ProjectCreateIn(
                title=body.title,
                script=body.script,
                category=payload["category"],
                options=ProjectOptionsIn(**payload["options"]),
            )
        except ValidationError as error:
            raise ApiError(
                422,
                "template_options_invalid",
                "The template's options no longer pass the "
                f"creation path's validation: {error.errors()[0]['msg']}",
            ) from error
        return _create_project_record(
            db,
            principal,
            title=validated.title,
            script=validated.script,
            category=validated.category,
            options=_options_dict(validated.options),
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Thumbnail Generator (founder priority #13) — compositional
# thumbnails over the project's own recorded material
# (services/thumbnail_studio.py): film frames, verified stills and
# uploads composed at YouTube's 1280x720 spec with PIL. Every route
# loads the project through the Phase 8 owner choke point first:
# another user's project is a 404 on reads AND writes, and a viewer
# member is refused writes with the choke point's read-only 403.
# ---------------------------------------------------------------------------


def _thumb_call(fn, *args, **kwargs):
    """Run a thumbnail-studio service call, mapping ThumbnailError
    onto the v1 error shape."""
    try:
        return fn(*args, **kwargs)
    except thumbnail_studio.ThumbnailError as error:
        message = error.message
        if error.details:
            message = f"{message} Details: {json.dumps(error.details)}"
        raise ApiError(error.status_code, error.code, message) from error


def _billed_thumb_call(operation, user_id, fn, *args, **kwargs):
    """Phase 8 (§7): a thumbnail AI operation consumes one
    thumbnail allowance unit or debits its credit price BEFORE it
    runs; any failure refunds in full. Legacy-plan users are
    unmetered."""
    from services.billing import operations

    receipt = operations.begin_operation(
        user_id, "thumbnails", operation
    )
    try:
        return _thumb_call(fn, *args, **kwargs)
    except Exception:
        operations.refund_operation(user_id, receipt)
        raise


def _thumb_project(project_id: str, principal, *, write: bool = False):
    db = SessionLocal()
    try:
        return _get_owned_project_or_404(db, project_id, principal, write=write)
    finally:
        db.close()


class ThumbnailComposeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_id: str = Field(min_length=1, max_length=300)
    headline: str | None = Field(default=None, max_length=200)
    layout: str = "bottom_band"
    accent: str = "amber"


class ThumbnailVariantsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str | None = Field(default=None, max_length=200)


class ThumbnailAnalyseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_id: str = Field(min_length=1, max_length=300)


@router.get("/projects/{project_id}/thumbnails")
def get_thumbnails(project_id: str, principal=Depends(require_user)):
    project = _thumb_project(project_id, principal)
    return thumbnail_studio.studio_view(
        project_id, project.title, project.category
    )


@router.post("/projects/{project_id}/thumbnails", status_code=201)
def compose_thumbnail(project_id: str, body: ThumbnailComposeIn,
                      principal=Depends(post_guard)):
    project = _thumb_project(project_id, principal, write=True)
    return _billed_thumb_call(
        "compose",
        principal.user_id,
        thumbnail_studio.compose_thumbnail,
        project_id,
        body.base_id,
        body.headline,
        layout=body.layout,
        accent=body.accent,
        project_title=project.title,
    )


@router.post("/projects/{project_id}/thumbnails/variants", status_code=201)
def generate_thumbnail_variants(project_id: str, body: ThumbnailVariantsIn,
                                principal=Depends(post_guard)):
    project = _thumb_project(project_id, principal, write=True)
    return _billed_thumb_call(
        "compose",
        principal.user_id,
        thumbnail_studio.generate_variants,
        project_id,
        headline=body.headline,
        project_title=project.title,
    )


@router.post("/projects/{project_id}/thumbnails/analyse")
def analyse_thumbnail_base(project_id: str, body: ThumbnailAnalyseIn,
                           principal=Depends(require_user)):
    _thumb_project(project_id, principal)
    return _thumb_call(
        thumbnail_studio.analyse_image, project_id, body.base_id
    )


@router.post("/projects/{project_id}/thumbnails/suggest-headlines")
def suggest_thumbnail_headlines(project_id: str,
                                principal=Depends(post_guard)):
    project = _thumb_project(project_id, principal)
    return _billed_thumb_call(
        "headlines",
        principal.user_id,
        thumbnail_studio.suggest_headlines_ai,
        project_id,
        project_title=project.title,
        project_category=project.category,
    )


@router.get("/projects/{project_id}/thumbnails/{thumb_id}/image")
def get_thumbnail_image(project_id: str, thumb_id: str,
                        principal=Depends(require_user)):
    _thumb_project(project_id, principal)
    path = _thumb_call(thumbnail_studio.variant_path, project_id, thumb_id)
    return FileResponse(path, media_type="image/png" if path.suffix == ".png" else "image/jpeg")


@router.get("/projects/{project_id}/thumbnails/{thumb_id}/download")
def download_thumbnail(project_id: str, thumb_id: str,
                       principal=Depends(require_user)):
    _thumb_project(project_id, principal)
    path = _thumb_call(thumbnail_studio.variant_path, project_id, thumb_id)
    return FileResponse(
        path,
        media_type="image/png" if path.suffix == ".png" else "image/jpeg",
        filename=f"thumbnail-{thumb_id}{path.suffix}",
    )


@router.delete("/projects/{project_id}/thumbnails/{thumb_id}")
def delete_thumbnail(project_id: str, thumb_id: str,
                     principal=Depends(post_guard)):
    _thumb_project(project_id, principal, write=True)
    return _thumb_call(thumbnail_studio.delete_variant, project_id, thumb_id)


# ---------------------------------------------------------------------------
# Localization Studio (founder priority #14) — sub-phase 1 of the
# audit's phasing (services/localization.py): the project's
# recorded captions and its script translated through llm_provider,
# timings preserved exactly and proven against the written file,
# the English narration retained and labelled as retained. There
# is deliberately NO dub route: the narration adapter's installed
# voices are English-only, and the studio view states why. Every
# route loads the project through the Phase 8 owner choke point
# first — another user's project is a 404 on reads AND writes.
# ---------------------------------------------------------------------------


def _loc_call(fn, *args, **kwargs):
    """Run a localization service call, mapping LocalizationError
    onto the v1 error shape."""
    try:
        return fn(*args, **kwargs)
    except localization.LocalizationError as error:
        message = error.message
        if error.details:
            message = f"{message} Details: {json.dumps(error.details)}"
        raise ApiError(error.status_code, error.code, message) from error


def _loc_project(project_id: str, principal, *, write: bool = False):
    db = SessionLocal()
    try:
        return _get_owned_project_or_404(db, project_id, principal, write=write)
    finally:
        db.close()


class LocalizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(min_length=2, max_length=8)


@router.get("/projects/{project_id}/localization")
def get_localization(project_id: str, principal=Depends(require_user)):
    project = _loc_project(project_id, principal)
    return localization.studio_view(
        project_id,
        project_title=project.title,
        script_text=project.script or "",
    )


@router.post("/projects/{project_id}/localization/captions", status_code=201)
def localize_captions(project_id: str, body: LocalizeIn,
                      principal=Depends(post_guard)):
    _loc_project(project_id, principal, write=True)
    return _loc_call(
        localization.translate_captions, project_id, body.language
    )


@router.post("/projects/{project_id}/localization/script", status_code=201)
def localize_script(project_id: str, body: LocalizeIn,
                    principal=Depends(post_guard)):
    project = _loc_project(project_id, principal, write=True)
    return _loc_call(
        localization.translate_script,
        project_id,
        principal.user_id,
        body.language,
        project_title=project.title,
        project_category=project.category or "Documentary",
        script_text=project.script or "",
    )


_LOC_FILE_MEDIA = {
    "srt": "text/plain; charset=utf-8",
    "vtt": "text/vtt; charset=utf-8",
    "json": "application/json",
    "txt": "text/plain; charset=utf-8",
}

_LOC_FILE_SUFFIX = {"srt": ".srt", "vtt": ".vtt", "json": ".json", "txt": ".txt"}


@router.get("/projects/{project_id}/localization/{artefact_id}/download")
def download_localization_artefact(project_id: str, artefact_id: str,
                                   kind: str = "srt",
                                   principal=Depends(require_user)):
    _loc_project(project_id, principal)
    path = _loc_call(
        localization.artefact_file, project_id, artefact_id, kind
    )
    kind_key = str(kind or "").strip().lower()
    return FileResponse(
        path,
        media_type=_LOC_FILE_MEDIA.get(kind_key, "application/octet-stream"),
        filename=f"{artefact_id}{_LOC_FILE_SUFFIX.get(kind_key, '')}",
    )


@router.delete("/projects/{project_id}/localization/{artefact_id}")
def delete_localization_artefact(project_id: str, artefact_id: str,
                                 principal=Depends(post_guard)):
    _loc_project(project_id, principal, write=True)
    return _loc_call(localization.delete_artefact, project_id, artefact_id)


# ---------------------------------------------------------------------------
# Editor (Stage 6) — non-destructive revisions of a finished production
# ---------------------------------------------------------------------------
# The editor works on revision documents (services/editor.py): the
# production's artefacts are imported read-only into revision 0 and
# every edit batch creates the next revision node. Manual editing
# never makes an asset verified: replacements re-earn their verdict
# through the Stage 1 swap machinery, uploads are user_supplied with
# unknown rights, and every asset change appends ledger rows tagged
# with the revision id.


def _require_editor_access(project_id: str, principal) -> None:
    """FS-F (FS-8, FS-14): the Studio gate. Ordinary principals —
    owners and collaborators alike (collaboration role does not
    elevate, register A-9) — may use the editor endpoint family
    ONLY when the project has a completed run AND its finished
    video exists. The predicate keys on the completed RUN plus the
    film artefact (FS-DISCOVERY §6.3 / A-10), never on the headline
    project status: a project whose latest run failed but which
    has an earlier completed run with its film stays editable.

    - Completed run: any ProductionRun for the project with
      status "completed", read through run_ledger.list_runs_for —
      the same source services/editor.py's create_revision_zero
      keys on.
    - Finished video exists: _film_media_url(...) is not None —
      the media index (plan_document.build_media_index) contains
      the canonical film (plan_document.CANONICAL_OUTPUTS["film"],
      resolved through media_paths.resolve_artefact's latest-run
      view). This is the exact source _project_view and
      GET /projects/{id}/outputs (film.exists) use to establish
      that a film exists; no parallel flag is invented.

    Refusal semantics extend services/editor.py's own: no
    completed run at all -> 404 no_completed_run (the service's
    answer for exactly this state, whatever the other runs'
    statuses — none/queued/running/awaiting/failed/cancelled);
    a completed run whose finished video is missing -> 404
    no_finished_video. Admin and super_admin principals are
    EXEMPT (the operator principal resolves to super_admin), so
    support can inspect and repair any project; the Phase 1a
    ownership choke point still runs before this gate, unchanged.
    Service-layer callers never pass through here and are
    unaffected (the pipeline and the library import machinery
    call services/editor.py in-process)."""
    if principal is None or getattr(principal, "is_admin", False):
        return
    runs = run_ledger.list_runs_for(project_id)
    if not any(run.get("status") == "completed" for run in runs):
        raise ApiError(
            404,
            "no_completed_run",
            "This project has no completed production run to edit.",
        )
    project_dir = media_paths.project_dir(project_id)
    if _film_media_url(project_id, project_dir) is None:
        raise ApiError(
            404,
            "no_finished_video",
            "This project has a completed production run, but its "
            "finished video does not exist, so there is nothing "
            "to edit yet.",
        )


def _editor_call(fn, *args, **kwargs):
    """Run an editor service call, mapping EditorError onto the v1
    error shape (status, code, message — details appended)."""
    try:
        return fn(*args, **kwargs)
    except editor.EditorError as error:
        message = error.message
        if error.details:
            message = f"{message} Details: {json.dumps(error.details)}"
        raise ApiError(error.status_code, error.code, message) from error


class CreateRevisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_revision_id: str | None = None
    from_run_id: str | None = None


class OperationsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: list[dict] = Field(min_length=1)
    actor: str = "operator"


class RenderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_gaps: bool = False


@router.get("/projects/{project_id}/editor")
def get_editor(project_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
    _require_editor_access(project_id, principal)
    view, doc = _editor_call(editor.get_editor, project_id)
    return {**view, "document": doc}


@router.post("/projects/{project_id}/revisions", status_code=201)
def create_revision(
    project_id: str,
    body: CreateRevisionBody,
    principal=Depends(post_guard),
):
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal, write=True)
    finally:
        db.close()
    _require_editor_access(project_id, principal)
    view, _doc = _editor_call(
        editor.create_revision,
        project_id,
        from_revision_id=body.from_revision_id,
        from_run_id=body.from_run_id,
    )
    return view


@router.get("/projects/{project_id}/revisions")
def list_revisions(project_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
    _require_editor_access(project_id, principal)
    return {"revisions": _editor_call(editor.list_revisions, project_id)}


@router.get("/projects/{project_id}/revisions/{revision_id}")
def get_revision(project_id: str, revision_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
    _require_editor_access(project_id, principal)
    view, doc = _editor_call(editor.get_revision, project_id, revision_id)
    return {**view, "document": doc}


@router.post(
    "/projects/{project_id}/revisions/{revision_id}/operations",
    status_code=201,
)
def apply_operations(
    project_id: str,
    revision_id: str,
    body: OperationsBody,
    principal=Depends(post_guard),
):
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal, write=True)
    finally:
        db.close()
    _require_editor_access(project_id, principal)
    view, _doc = _editor_call(
        editor.apply_operations,
        project_id,
        revision_id,
        body.operations,
        actor=body.actor,
    )
    return view


@router.post(
    "/projects/{project_id}/revisions/{revision_id}/render",
    status_code=202,
)
def render_revision(
    project_id: str,
    revision_id: str,
    body: RenderBody,
    principal=Depends(post_guard),
):
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal, write=True)
    finally:
        db.close()
    _require_editor_access(project_id, principal)
    return _editor_call(
        editor.request_render,
        project_id,
        revision_id,
        allow_gaps=body.allow_gaps,
    )


@router.get("/projects/{project_id}/revisions/{revision_id}/exports")
def list_exports(project_id: str, revision_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
    _require_editor_access(project_id, principal)
    _view, doc = _editor_call(editor.get_revision, project_id, revision_id)
    return {"exports": doc.get("exports") or []}


@router.post("/projects/{project_id}/uploads", status_code=201)
def upload_file(
    project_id: str,
    file: UploadFile = File(...),
    principal=Depends(post_guard),
):
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal, write=True)
        # Per-plan upload cap replaces the global one where a plan
        # resolves (02 §5.3); editor.store_upload's global cap
        # remains as the second enforcement point downstream.
        file.file.seek(0, 2)
        upload_size = file.file.tell()
        file.file.seek(0)
        plans_service.enforce_upload_size(
            db, principal.user_id, upload_size
        )
    finally:
        db.close()
    return _editor_call(
        editor.store_upload, project_id, file.filename, file.file
    )


@router.get("/projects/{project_id}/revisions/{revision_id}/certificate")
def get_revision_certificate(
    project_id: str, revision_id: str, principal=Depends(require_user)
):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
        project_view = {
            "id": project.id,
            "title": project.title,
            "category": project.category,
        }
    finally:
        db.close()
    _require_editor_access(project_id, principal)
    view, doc = _editor_call(editor.get_revision, project_id, revision_id)
    chain_ids = _editor_call(editor.revision_chain_ids, project_id, revision_id)
    decisions = _editor_call(
        run_ledger.list_decisions_for_revision,
        project_id,
        view["run_id"],
        chain_ids,
    )
    counts = run_ledger.counts_for_decisions(decisions)
    run_view = run_ledger.get_run(view["run_id"]) or {"run_id": view["run_id"]}
    return plan_document.build_revision_certificate(
        project_view,
        run_view,
        view,
        decisions,
        counts,
        editor.final_assets_for_certificate(doc),
        # Stage H (M7/M8): the certificate's music section describes
        # THIS revision's music (its own soundtrack record), never
        # the production's.
        music=editor.music_certificate_data(doc),
    )


# ---------------------------------------------------------------------
# Editor Studio (founder priority #5): the multitrack projection of
# a revision, its media pool, frame-accurate preview of the
# unrendered document, and real audio waveforms. Reads only — edits
# still flow through the operations endpoint above and renders
# through the render endpoint. Ownership is checked exactly like
# every other editor surface (cross-tenant = 404).
# ---------------------------------------------------------------------


def _studio_call(fn, project_id, *args, **kwargs):
    principal = kwargs.pop("principal")
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
    _require_editor_access(project_id, principal)
    return _editor_call(fn, project_id, *args, **kwargs)


@router.get("/projects/{project_id}/revisions/{revision_id}/studio")
def get_studio(project_id: str, revision_id: str, principal=Depends(require_user)):
    return _studio_call(
        editor_studio.studio_view, project_id, revision_id, principal=principal
    )


class TrackLockBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locked: bool


@router.get("/projects/{project_id}/editor/track-locks")
def get_track_locks(project_id: str, principal=Depends(require_user)):
    """Phase 6 (A-17): the project's server-persisted track locks."""
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
    _require_editor_access(project_id, principal)
    return {"locked_tracks": editor_locks.locked_tracks(project_id)}


@router.put("/projects/{project_id}/editor/track-locks/{track_id}")
def set_track_lock(
    project_id: str,
    track_id: str,
    body: TrackLockBody,
    principal=Depends(post_guard),
):
    """Persist one track's lock state (A-17). Enforcement lives in
    services/editor.apply_operations via editor_locks — a locked
    track's operations are rejected 409 track_locked."""
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal, write=True)
    finally:
        db.close()
    _require_editor_access(project_id, principal)
    try:
        locked = editor_locks.set_track_lock(project_id, track_id, body.locked)
    except ValueError as error:
        raise ApiError(422, "unknown_track", str(error)) from error
    return {"locked_tracks": locked}


@router.get("/projects/{project_id}/revisions/{revision_id}/studio/pool")
def get_studio_pool(
    project_id: str,
    revision_id: str,
    limit: int = 100,
    offset: int = 0,
    principal=Depends(require_user),
):
    return _studio_call(
        editor_studio.media_pool,
        project_id,
        revision_id,
        limit=limit,
        offset=offset,
        principal=principal,
    )


@router.get("/projects/{project_id}/revisions/{revision_id}/studio/frame")
def get_studio_frame(
    project_id: str,
    revision_id: str,
    t: float = 0.0,
    principal=Depends(require_user),
):
    path, content_type = _studio_call(
        editor_studio.preview_frame,
        project_id,
        revision_id,
        t,
        principal=principal,
    )
    return FileResponse(path, media_type=content_type)


@router.get("/projects/{project_id}/revisions/{revision_id}/studio/waveform")
def get_studio_waveform(
    project_id: str,
    revision_id: str,
    track: str = "narration",
    principal=Depends(require_user),
):
    return _studio_call(
        editor_studio.waveform,
        project_id,
        revision_id,
        track,
        principal=principal,
    )


# ---------------------------------------------------------------------------
# Asset Library (Phase E, founder priority #7) — the account-level
# media library over the asset ledger + upload indexes
# (services/asset_library.py). Every route is principal-scoped: the
# library is built from the caller's own projects only, and another
# user's asset answers 404 exactly like a nonexistent one.
#
# FS-C (FS-1): the Library page is an admin-only surface, so every
# route in this family requires an admin principal (403 for
# ordinary users — account-scoped routes, no tenant-private 404
# to preserve). Judgement on the shared-data concern: no kept
# frontend flow calls these HTTP routes (grep-verified — the
# classic Editor and Thumbnails read through the project editor /
# media endpoints, not /library/*); the flows that DO consume
# library data — the editor import machinery and the pipeline —
# call services/asset_library.py in-process, and service-layer
# functions are never gated. The project upload route
# (/projects/{id}/uploads) is a separate kept flow and untouched.
# ---------------------------------------------------------------------------


def _library_call(fn, *args, **kwargs):
    """Run an asset-library service call, mapping LibraryError —
    and the EditorError the import path can raise while seeding a
    target revision — onto the v1 error shape."""
    try:
        return fn(*args, **kwargs)
    except asset_library.LibraryError as error:
        message = error.message
        if error.details:
            message = f"{message} Details: {json.dumps(error.details)}"
        raise ApiError(error.status_code, error.code, message) from error
    except editor.EditorError as error:
        message = error.message
        if error.details:
            message = f"{message} Details: {json.dumps(error.details)}"
        raise ApiError(error.status_code, error.code, message) from error


class LibraryFolderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)


class LibraryOrganisationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tags: list[str] | None = None
    folder_id: str | None = None
    folder_set: bool = False


class LibraryImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)


@router.get("/library/assets")
def list_library_assets(
    q: str | None = None,
    project_id: str | None = None,
    type: str | None = None,
    provider: str | None = None,
    licence: str | None = None,
    verification: str | None = None,
    decision_type: str | None = None,
    folder: str | None = None,
    tag: str | None = None,
    duplicates_only: bool = False,
    limit: int = 100,
    offset: int = 0,
    principal=Depends(require_user),
):
    # FS-C (FS-1): the Library is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _library_call(
            asset_library.list_assets,
            db,
            principal,
            q=q,
            project_id=project_id,
            type=type,
            provider=provider,
            licence=licence,
            verification=verification,
            decision_type=decision_type,
            folder=folder,
            tag=tag,
            duplicates_only=duplicates_only,
            limit=limit,
            offset=offset,
        )
    finally:
        db.close()


@router.get("/library/assets/{content_hash}")
def get_library_asset(content_hash: str, principal=Depends(require_user)):
    # FS-C (FS-1): the Library is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _library_call(
            asset_library.get_asset, db, principal, content_hash
        )
    finally:
        db.close()


@router.get("/library/assets/{content_hash}/media")
def serve_library_media(content_hash: str, principal=Depends(require_user)):
    # FS-C (FS-1): the Library is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        path = _library_call(
            asset_library.resolve_media_path, db, principal, content_hash
        )
    finally:
        db.close()
    return FileResponse(
        path,
        media_type=asset_library.media_content_type(path),
        filename=Path(path).name,
    )


@router.put("/library/assets/{content_hash}/organisation")
def set_library_asset_organisation(
    content_hash: str,
    body: LibraryOrganisationIn,
    principal=Depends(post_guard),
):
    # FS-C (FS-1): the Library is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _library_call(
            asset_library.set_item_organisation,
            db,
            principal,
            content_hash,
            tags=body.tags,
            folder_id=body.folder_id if body.folder_set else ...,
        )
    finally:
        db.close()


@router.post("/library/assets/{content_hash}/imports", status_code=201)
def import_library_asset(
    content_hash: str,
    body: LibraryImportIn,
    principal=Depends(post_guard),
):
    # FS-C (FS-1): the Library is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _library_call(
            asset_library.import_asset,
            db,
            principal,
            content_hash,
            body.project_id,
        )
    finally:
        db.close()


@router.get("/library/duplicates")
def list_library_duplicates(principal=Depends(require_user)):
    # FS-C (FS-1): the Library is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _library_call(asset_library.list_duplicates, db, principal)
    finally:
        db.close()


@router.get("/library/storage")
def get_library_storage(principal=Depends(require_user)):
    # FS-C (FS-1): the Library is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _library_call(asset_library.storage_view, db, principal)
    finally:
        db.close()


@router.get("/library/folders")
def list_library_folders(principal=Depends(require_user)):
    # FS-C (FS-1): the Library is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _library_call(asset_library.list_folders, db, principal)
    finally:
        db.close()


@router.post("/library/folders", status_code=201)
def create_library_folder(
    body: LibraryFolderIn, principal=Depends(post_guard)
):
    # FS-C (FS-1): the Library is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _library_call(
            asset_library.create_folder, db, principal, body.name
        )
    finally:
        db.close()


@router.delete("/library/folders/{folder_id}")
def delete_library_folder(folder_id: str, principal=Depends(post_guard)):
    # FS-C (FS-1): the Library is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        return _library_call(
            asset_library.delete_folder, db, principal, folder_id
        )
    finally:
        db.close()


@router.post("/library/uploads", status_code=201)
def upload_to_library(
    project_id: str = Form(...),
    file: UploadFile = File(...),
    principal=Depends(post_guard),
):
    # FS-C (FS-1): the Library is an admin-only surface.
    _require_admin_for_presentational_surface(principal)
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal, write=True)
        # The same per-plan upload cap the project upload route
        # enforces; editor.store_upload's global cap remains the
        # second enforcement point downstream.
        file.file.seek(0, 2)
        upload_size = file.file.tell()
        file.file.seek(0)
        plans_service.enforce_upload_size(db, principal.user_id, upload_size)
        return _library_call(
            asset_library.library_upload,
            db,
            principal,
            project_id,
            file.filename,
            file.file,
        )
    finally:
        db.close()


# ---------------------------------------------------------------------
# Collaboration (founder priority #11; gap-audit doc 04 §4)
# ---------------------------------------------------------------------
# Membership, invitations, comments and the activity feed. The
# enforcement core is NOT here: it is the grant-aware choke point
# above (_get_owned_project_or_404 with write=True on every
# mutating route) plus services/collaboration.py's state machine.
# These routes add the collaboration surfaces themselves.
#
# Invitations are IN-APP ONLY: there is no email service
# configured, so inviting never sends (or claims to send) email —
# the invitation waits in the invitee's account until they sign in
# and accept or decline it (GET /invitations).


def _collab_call(fn, *args, **kwargs):
    """Run a collaboration service call, mapping CollaborationError
    onto the v1 error shape."""
    try:
        return fn(*args, **kwargs)
    except collaboration.CollaborationError as error:
        raise ApiError(error.status_code, error.code, error.message) from error


class MemberInviteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    role: str


class MemberRoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str


class InvitationRespondIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accept: bool


class CommentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=collaboration.MAX_COMMENT_CHARS)
    revision_id: str | None = None


class CommentEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=collaboration.MAX_COMMENT_CHARS)


@router.get("/projects/{project_id}/members")
def get_project_members(project_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
        overview = collaboration.members_overview(db, project)
        overview["your_role"] = collaboration.role_for(db, project, principal)
        return overview
    finally:
        db.close()


@router.post("/projects/{project_id}/members", status_code=201)
def invite_project_member(
    project_id: str, body: MemberInviteIn, principal=Depends(post_guard)
):
    db = SessionLocal()
    try:
        # Ownership-level: the OWNER loader, not the grant loader.
        project = _get_owner_project_or_404(db, project_id, principal)
        row = _collab_call(
            collaboration.invite_member,
            db,
            project,
            inviter_id=principal.user_id,
            email=body.email,
            role=body.role,
        )
        return collaboration.member_view(db, row)
    finally:
        db.close()


@router.patch("/projects/{project_id}/members/{user_id}")
def change_project_member_role(
    project_id: str,
    user_id: str,
    body: MemberRoleIn,
    principal=Depends(post_guard),
):
    db = SessionLocal()
    try:
        project = _get_owner_project_or_404(db, project_id, principal)
        row = _collab_call(
            collaboration.change_member_role,
            db,
            project,
            user_id=user_id,
            role=body.role,
        )
        return collaboration.member_view(db, row)
    finally:
        db.close()


@router.delete("/projects/{project_id}/members/{user_id}")
def remove_project_member(
    project_id: str, user_id: str, principal=Depends(post_guard)
):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
        if user_id != principal.user_id:
            # Removing SOMEONE ELSE is ownership-level.
            _get_owner_project_or_404(db, project_id, principal)
        row = _collab_call(
            collaboration.remove_member,
            db,
            project,
            user_id=user_id,
            removed_by_id=principal.user_id,
        )
        return collaboration.member_view(db, row)
    finally:
        db.close()


@router.get("/invitations")
def list_invitations(principal=Depends(require_user)):
    db = SessionLocal()
    try:
        return {
            "invitations": collaboration.pending_invitations(
                db, principal.user_id
            )
        }
    finally:
        db.close()


@router.post("/invitations/{project_id}/respond")
def respond_to_invitation(
    project_id: str,
    body: InvitationRespondIn,
    principal=Depends(post_guard),
):
    db = SessionLocal()
    try:
        row = _collab_call(
            collaboration.respond_to_invitation,
            db,
            project_id,
            principal,
            accept=body.accept,
        )
        return collaboration.member_view(db, row)
    finally:
        db.close()


@router.get("/projects/{project_id}/comments")
def list_project_comments(project_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
        rows = collaboration.list_comments(db, project.id)
        return {
            "comments": [collaboration.comment_view(db, r) for r in rows]
        }
    finally:
        db.close()


@router.post("/projects/{project_id}/comments", status_code=201)
def create_project_comment(
    project_id: str, body: CommentIn, principal=Depends(post_guard)
):
    # Deliberately the READ form of the loader: discussion is open
    # to every accepted member, viewers included (inviting a
    # reviewer who cannot comment would be pointless). Production
    # writes are what the viewer role makes read-only.
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
        row = _collab_call(
            collaboration.create_comment,
            db,
            project,
            author_id=principal.user_id,
            body=body.body,
            revision_id=body.revision_id,
        )
        return collaboration.comment_view(db, row)
    finally:
        db.close()


@router.put("/projects/{project_id}/comments/{comment_id}")
def edit_project_comment(
    project_id: str,
    comment_id: str,
    body: CommentEditIn,
    principal=Depends(post_guard),
):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
        row = _collab_call(
            collaboration.edit_comment,
            db,
            project,
            comment_id=comment_id,
            author_id=principal.user_id,
            body=body.body,
        )
        return collaboration.comment_view(db, row)
    finally:
        db.close()


@router.delete("/projects/{project_id}/comments/{comment_id}")
def delete_project_comment(
    project_id: str, comment_id: str, principal=Depends(post_guard)
):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
        _collab_call(
            collaboration.delete_comment,
            db,
            project,
            comment_id=comment_id,
            principal=principal,
        )
        return {"deleted": True, "comment_id": comment_id}
    finally:
        db.close()


@router.get("/projects/{project_id}/activity")
def get_project_activity(project_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
        project_dir = media_paths.project_dir(project.id)
        return collaboration.build_activity(db, project, project_dir)
    finally:
        db.close()
