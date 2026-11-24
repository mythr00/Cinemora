"""API v1 — the studio contract (Stage 4).

Mounted from main.py under /api/v1. The six legacy routes in
main.py are untouched and keep their original shapes; everything in
this module follows the v1 contract documented in
~/workspace/cinemora-audit/API_CONTRACT.md:

- Account auth (AUD-11, Phase 8.2): every route except
  GET /api/v1/health requires an authenticated principal —
  a server-side session (cookie or Bearer, services/auth.py) or,
  during the dual-auth window, the legacy X-API-Key operator token
  (constant-time compare), which resolves to the founder principal
  and is retired by setting CINEMORA_ALLOW_OPERATOR_TOKEN=0. When
  neither is available the API fails CLOSED: 503
  "api_not_configured" when the token is unconfigured, 401
  otherwise, never open. Project/run routes are owner-scoped:
  another user's resource is a 404, never a 403 (design 02 §4.1).
- Consistent error shape: {"error": {"code", "message"}} for every
  error raised under /api/v1 (auth, validation, domain errors).
- Script payloads are capped at 60,000 characters (422 beyond).
- POST routes are rate limited in-process (default 30/min per
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

import hmac
import json
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
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

import jobs
from database import SessionLocal
from models import Plan, Project, User, UserPreference, UserSubscription
from services import admin_audit
from services import auth as auth_service
from services import plans as plans_service
from services import (
    asset_library,
    editor,
    editor_studio,
    genre_packs,
    media_paths,
    narration,
    plan_document,
    project_intelligence,
    qa_report,
    research_studio,
    run_ledger,
    versioning,
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

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/v1"):
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
        if request.url.path.startswith("/api/v1"):
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
    """The dual-auth window (design 02 §1.4): the legacy operator
    token keeps working until CINEMORA_ALLOW_OPERATOR_TOKEN=0
    retires it (the preview launcher keeps the window open; the
    SaaS deployment closes it once session login is in use)."""
    return os.environ.get("CINEMORA_ALLOW_OPERATOR_TOKEN", "1") != "0"


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


def require_api_token(request: Request) -> str:
    """Back-compat shim for the legacy routes in main.py (IA-08):
    same resolution as require_user, returning the identity string
    those routes have always ignored."""
    principal = require_user(request)
    return principal.email


class _RateLimiter:
    """In-process sliding-window limiter for POST routes."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _limit(self) -> int:
        try:
            return max(1, int(os.environ.get("CINEMORA_API_RATE_LIMIT", "30")))
        except ValueError:
            return 30

    def check(self, identity: str, limit: int | None = None) -> None:
        effective = self._limit() if limit is None else limit
        now = time.monotonic()
        with self._lock:
            hits = self._hits[identity]
            while hits and now - hits[0] > 60.0:
                hits.popleft()
            if len(hits) >= effective:
                raise ApiError(
                    429,
                    "rate_limited",
                    "Too many write requests in the last minute; "
                    "retry shortly.",
                )
            hits.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


rate_limiter = _RateLimiter()
auth_rate_limiter = _RateLimiter()


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
        secure=os.environ.get("CINEMORA_COOKIE_SECURE") == "1",
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
        return {"user": _user_view(user), "session_token": token}
    finally:
        db.close()


# A real Argon2id hash of a throwaway password, computed once at
# import: login verifies against it when the account doesn't exist,
# so response timing doesn't enumerate registered emails.
_DUMMY_HASH = auth_service.hash_password("timing-equalisation-dummy")


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    email = auth_service.normalize_email(body.email)
    _auth_rate_limit(request, email)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).one_or_none()
        password_ok = auth_service.verify_password(
            user.password_hash if user is not None else _DUMMY_HASH,
            body.password,
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
        token = auth_service.create_session(user.id)
        _set_session_cookie(response, token)
        return {"user": _user_view(user), "session_token": token}
    finally:
        db.close()


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
                # No SMTP on this deployment: the operator's host
                # log is the delivery channel (02 §1.3) — stated
                # plainly, never dressed up as an email.
                logging.getLogger(__name__).warning(
                    "password reset link for %s (manual delivery): %s",
                    user.email,
                    link,
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


# The Phase 8 account-admin endpoints that used to live here
# (GET /admin/users, POST /admin/users/{user_id}/plan) moved to
# api_admin.py in Phase 7 — same paths and shapes, now behind the
# admin router's guard and recorded in the admin audit log.


# ---------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------


class ProjectOptionsIn(BaseModel):
    model_config = ConfigDict(extra="allow")

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
    db, project_id: str, principal: auth_service.Principal
) -> Project:
    """The owner-scoped load choke point (design 02 §4.1): every
    project-scoped route loads through here. Another user's project
    is indistinguishable from a nonexistent one — 404, never 403 —
    so existence itself stays tenant-private. Legacy rows with a
    NULL owner (pre-8.1 databases that never ran the backfill) are
    visible only to the founder principal who would own them."""
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


def _get_owned_run_or_404(run_id: str, principal: auth_service.Principal):
    """Run-level equivalent: the run dict (run_ledger) with the
    same 404 cross-tenant semantics."""
    run = run_ledger.get_run(run_id)
    if run is None:
        raise ApiError(404, "run_not_found", f"No run {run_id}.")
    owner = run.get("owner_id")
    if owner is None:
        if principal.auth_method != "operator_token":
            raise ApiError(404, "run_not_found", f"No run {run_id}.")
    elif owner != principal.user_id:
        raise ApiError(404, "run_not_found", f"No run {run_id}.")
    return run


def _options_dict(options) -> dict:
    if options is None:
        return {}
    if isinstance(options, BaseModel):
        return options.model_dump(exclude_none=True)
    return dict(options)


def _production_state(project_dir: Path) -> dict | None:
    try:
        return json.loads(
            media_paths.resolve_artefact(
                project_dir, "production_state.json"
            ).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None


def _run_view(run: dict | None) -> dict | None:
    if not run:
        return None
    return {
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


def _film_media_url(project_id: str, project_dir: Path) -> str | None:
    index = plan_document.build_media_index(project_id, project_dir)
    for entry in index.values():
        if entry["file_name"] == plan_document.CANONICAL_OUTPUTS["film"]:
            return f"/api/v1/projects/{project_id}/media/{entry['key']}"
    return None


def _project_view(db, project: Project, *, detail: bool = False) -> dict:
    project_dir = media_paths.project_dir(project.id)
    state = _production_state(project_dir)
    latest = run_ledger.latest_run_for(project.id)
    view = {
        "id": project.id,
        "title": project.title,
        "category": project.category,
        "status": project.status,
        "created_at": project.created_at,
        "script_preview": (project.script or "")[:140],
        "current_stage": (state or {}).get("current_stage"),
        "video_url": _film_media_url(project.id, project_dir)
        if project.status == "completed"
        else None,
        "latest_run": _run_view(latest),
    }
    if detail:
        view["script"] = project.script
        view["options"] = project.options or {}
    return view


# ---------------------------------------------------------------------
# Health (the only unauthenticated v1 route)
# ---------------------------------------------------------------------


@router.get("/health")
def health():
    return {"status": "ok", "service": "cinemora-api", "version": "v1"}


# ---------------------------------------------------------------------
# Catalogues (IA-06) — read-only views over the REAL sources of truth
# ---------------------------------------------------------------------
# Genres come from services.genre_packs (the pack JSON documents in
# services/genre_packs/data/, loaded by genre_packs.available_packs)
# and voices from services.narration.VOICE_CATALOGUE (Stage 3J: the
# catalogue of voices actually installed). Nothing is hardcoded
# here: installing a pack or a voice changes these responses with
# no API change. There is deliberately NO music catalogue route —
# the music library is founder-gated (P3) and stays unactivated;
# the packs' music metadata fields (music_mood / mix_preset) are
# likewise not catalogue data and are not exposed here.


def _genre_view(pack: dict) -> dict:
    import copy

    return {
        "id": pack["id"],
        # The pack documents carry no separate "name" field; their
        # human-readable name IS the "label" field.
        "name": pack.get("label"),
        "label": pack.get("label"),
        "description": pack.get("description"),
        "style_family": pack.get("style_family"),
        "graphics_motifs": copy.deepcopy(pack.get("graphics_motifs") or []),
        "format_preferences": copy.deepcopy(pack.get("format_preferences") or {}),
        "entity_handling": copy.deepcopy(pack.get("entity_handling") or []),
        "research_query_templates": copy.deepcopy(
            pack.get("research_query_templates") or []
        ),
        "pacing": copy.deepcopy(pack.get("pacing") or {}),
    }


@router.get("/catalogues/genres")
def list_genre_catalogue(principal=Depends(require_user)):
    packs = [p for p in genre_packs.available_packs() if p is not None]
    return {"genres": [_genre_view(pack) for pack in packs]}


def _voice_view(voice_id: str, model, config) -> dict:
    # The canonical id for an alias is the non-default catalogue id
    # backed by the same installed model files (today: "default"
    # aliases "en_US-lessac-medium"; both map to the one installed
    # Piper model). Derived from the catalogue itself, never named
    # literally here.
    canonical = None
    for other_id, (other_model, other_config) in narration.VOICE_CATALOGUE.items():
        if other_id == narration.DEFAULT_VOICE_ID:
            continue
        if other_model == model and other_config == config:
            canonical = other_id
            break
    is_default = voice_id == narration.DEFAULT_VOICE_ID
    return {
        "id": voice_id,
        "is_default": is_default,
        "alias_of": None if voice_id == canonical else canonical,
        "model": model.name,
        "model_file": model.name,
        "config_file": config.name,
        "installed": bool(model.exists() and config.exists()),
    }


@router.get("/catalogues/voices")
def list_voice_catalogue(principal=Depends(require_user)):
    voices = [
        _voice_view(voice_id, model, config)
        for voice_id, (model, config) in narration.VOICE_CATALOGUE.items()
    ]
    return {
        "voices": voices,
        "default_voice_id": narration.DEFAULT_VOICE_ID,
    }


# ---------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreateIn, principal=Depends(post_guard)):
    db = SessionLocal()
    try:
        plans_service.enforce_script_length(db, principal.user_id, body.script)
        project = Project(
            id=str(uuid4()),
            owner_id=principal.user_id,
            title=body.title,
            script=body.script,
            category=body.category or "Documentary",
            options=_options_dict(body.options) or None,
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
        return _project_view(db, project, detail=True)
    finally:
        db.close()


@router.get("/projects")
def list_projects(principal=Depends(require_user)):
    db = SessionLocal()
    try:
        # Owner-filtered in SQL, never in Python after the fact
        # (design 02 §4.1).
        projects = (
            db.query(Project)
            .filter(Project.owner_id == principal.user_id)
            .order_by(Project.created_at.desc())
            .all()
        )
        return {"projects": [_project_view(db, p) for p in projects]}
    finally:
        db.close()


@router.get("/projects/{project_id}")
def get_project(project_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
        return _project_view(db, project, detail=True)
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
        return {
            "id": project.id,
            "title": project.title,
            "category": project.category,
            "status": project.status,
            "created_at": project.created_at,
            "current_stage": (state or {}).get("current_stage"),
            "stages": (state or {}).get("stages"),
            "progress": state,
            "latest_run": _run_view(latest),
            "video_url": _film_media_url(project.id, project_dir)
            if project.status == "completed"
            else None,
            "error": error,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------


@router.post("/projects/{project_id}/runs", status_code=202)
def submit_run(project_id: str, body: RunSubmitIn | None = None, principal=Depends(post_guard)):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
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
    script_hash = run_ledger.script_sha256(script)

    if merged.get("require_plan_approval"):
        existing = run_ledger.latest_run_for(project_id, script_hash)
        if existing and existing["status"] in (
            "queued",
            "running",
            "awaiting_approval",
        ):
            return {
                "run": _run_view(existing),
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
                "run": _run_view(run_ledger.get_run(run_id)),
                "deduplicated": True,
                "job_id": None,
                "phase": "analysis",
            }
        job_id = jobs.enqueue_analysis_phase(
            project_id, script, run_id, options=merged
        )
        return {
            "run": _run_view(run_ledger.get_run(run_id)),
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
        "run": _run_view(run),
        "deduplicated": already_active,
        # When the submission deduplicated, enqueue_full_pipeline
        # returns the existing RUN id (its documented contract), not
        # a job id — report no job in that case rather than dress a
        # run id up as one.
        "job_id": None if already_active else job_id,
        "phase": "production",
    }


@router.get("/projects/{project_id}/runs")
def list_project_runs(project_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
    runs = run_ledger.list_runs_for(project_id)
    return {"runs": [_run_view(run) for run in runs]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, principal=Depends(require_user)):
    run = _get_owned_run_or_404(run_id, principal)
    return _run_view(run)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, principal=Depends(post_guard)):
    # Owner check FIRST: a cross-tenant cancel attempt is a 404 and
    # verifiably changes nothing (isolation battery case 3).
    _get_owned_run_or_404(run_id, principal)
    accepted = run_ledger.request_cancel(run_id)
    return {
        "run_id": run_id,
        "cancel_requested": accepted,
        "run": _run_view(run_ledger.get_run(run_id)),
    }


# ---------------------------------------------------------------------
# Production plan + decisions
# ---------------------------------------------------------------------


@router.get("/projects/{project_id}/plan")
def get_plan(project_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
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
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
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
        project = _get_owned_project_or_404(db, project_id, principal)
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
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
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
    job_id = jobs.enqueue_full_pipeline(
        project_id, script, options=options, run_id=awaiting["run_id"]
    )
    return {
        "run": _run_view(run_ledger.get_run(awaiting["run_id"])),
        "job_id": job_id,
        "released": True,
        "regenerate_sentences": marks,
    }


# ---------------------------------------------------------------------
# Assets (ledger decisions for the latest run)
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
        _get_owned_project_or_404(db, project_id, principal)
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
        "certificate_url": f"/api/v1/projects/{project_id}/certificate",
        "plan_url": f"/api/v1/projects/{project_id}/plan",
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
        _get_owned_project_or_404(db, project_id, principal)
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
        _get_owned_project_or_404(db, project_id, principal)
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
        _get_owned_project_or_404(db, project_id, principal)
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
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
    return project_intelligence.build_project_intelligence(
        project_id, principal.user_id
    )


@router.get("/intelligence")
def get_account_intelligence(principal=Depends(require_user)):
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


def _owned_project_dict(project_id: str, principal) -> dict:
    db = SessionLocal()
    try:
        project = _get_owned_project_or_404(db, project_id, principal)
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
    project = _owned_project_dict(project_id, principal)
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
    project = _owned_project_dict(project_id, principal)
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
    _owned_project_dict(project_id, principal)
    return _versioning_call(
        versioning.delete_snapshot, project_id, snapshot_id
    )


@router.post(
    "/projects/{project_id}/versions/snapshots/{snapshot_id}/restore"
)
def restore_snapshot(
    project_id: str, snapshot_id: str, principal=Depends(post_guard)
):
    project = _owned_project_dict(project_id, principal)
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
    project = _owned_project_dict(project_id, principal)
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


def _require_owned_project(project_id: str, principal) -> None:
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
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
    _require_owned_project(project_id, principal)
    return _research_call(research_studio.overview, project_id)


@router.get("/projects/{project_id}/research/bible")
def get_research_bible(project_id: str, principal=Depends(require_user)):
    _require_owned_project(project_id, principal)
    return _research_call(research_studio.bible_view, project_id)


@router.get("/projects/{project_id}/research/sources")
def get_research_sources(
    project_id: str,
    state: str | None = None,
    q: str | None = None,
    principal=Depends(require_user),
):
    _require_owned_project(project_id, principal)
    return _research_call(
        research_studio.list_sources, project_id, state=state, query=q
    )


@router.post("/projects/{project_id}/research/sessions", status_code=201)
def create_research_session(
    project_id: str, body: ResearchSessionIn, principal=Depends(post_guard)
):
    _require_owned_project(project_id, principal)
    return _research_call(research_studio.run_session, project_id, body.topic)


@router.get("/projects/{project_id}/research/sessions/{session_id}")
def get_research_session(
    project_id: str, session_id: str, principal=Depends(require_user)
):
    _require_owned_project(project_id, principal)
    return _research_call(research_studio.get_session, project_id, session_id)


@router.post("/projects/{project_id}/research/sources/{source_id}/state")
def set_research_source_state(
    project_id: str,
    source_id: str,
    body: SourceStateIn,
    principal=Depends(post_guard),
):
    _require_owned_project(project_id, principal)
    return _research_call(
        research_studio.set_source_state, project_id, source_id, body.state
    )


@router.post("/projects/{project_id}/research/sources/external", status_code=201)
def add_research_external_source(
    project_id: str, body: ExternalSourceIn, principal=Depends(post_guard)
):
    _require_owned_project(project_id, principal)
    return _research_call(
        research_studio.add_external_source,
        project_id,
        body.title or "",
        body.url,
    )


@router.post("/projects/{project_id}/research/fact-check")
def run_research_fact_check(project_id: str, principal=Depends(post_guard)):
    _require_owned_project(project_id, principal)
    return _research_call(research_studio.run_fact_check, project_id)


@router.post("/projects/{project_id}/research/entities/{entity_id}/check")
def check_research_entity(
    project_id: str, entity_id: str, principal=Depends(post_guard)
):
    _require_owned_project(project_id, principal)
    return _research_call(research_studio.check_entity, project_id, entity_id)


@router.post("/projects/{project_id}/research/corrections", status_code=201)
def create_research_correction(
    project_id: str, body: CorrectionIn, principal=Depends(post_guard)
):
    _require_owned_project(project_id, principal)
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
    _require_owned_project(project_id, principal)
    _research_call(research_studio.delete_correction, project_id, correction_id)
    return {"deleted": True, "correction_id": correction_id}


@router.get("/projects/{project_id}/research/citations")
def get_research_citations(project_id: str, principal=Depends(require_user)):
    _require_owned_project(project_id, principal)
    return {"citations": _research_call(research_studio.list_citations, project_id)}


@router.post("/projects/{project_id}/research/citations", status_code=201)
def create_research_citation(
    project_id: str, body: CitationIn, principal=Depends(post_guard)
):
    _require_owned_project(project_id, principal)
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
    _require_owned_project(project_id, principal)
    _research_call(research_studio.delete_citation, project_id, citation_id)
    return {"deleted": True, "citation_id": citation_id}


@router.get("/projects/{project_id}/research/bibliography")
def get_research_bibliography(project_id: str, principal=Depends(require_user)):
    _require_owned_project(project_id, principal)
    return _research_call(research_studio.bibliography, project_id)


@router.post("/projects/{project_id}/research/timeline-events", status_code=201)
def create_research_timeline_event(
    project_id: str, body: TimelineEventIn, principal=Depends(post_guard)
):
    _require_owned_project(project_id, principal)
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
    _require_owned_project(project_id, principal)
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
    _require_owned_project(project_id, principal)
    _research_call(research_studio.delete_timeline_event, project_id, event_id)
    return {"deleted": True, "event_id": event_id}


@router.get("/projects/{project_id}/research/summaries")
def get_research_summaries(project_id: str, principal=Depends(require_user)):
    _require_owned_project(project_id, principal)
    return {"summaries": _research_call(research_studio.list_summaries, project_id)}


@router.post("/projects/{project_id}/research/summaries", status_code=201)
def create_research_summary(
    project_id: str, body: SummaryIn, principal=Depends(post_guard)
):
    _require_owned_project(project_id, principal)
    return _research_call(
        research_studio.generate_summary, project_id, body.session_id
    )


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
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
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
    return {"revisions": _editor_call(editor.list_revisions, project_id)}


@router.get("/projects/{project_id}/revisions/{revision_id}")
def get_revision(project_id: str, revision_id: str, principal=Depends(require_user)):
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
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
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
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
        _get_owned_project_or_404(db, project_id, principal)
    finally:
        db.close()
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
        _get_owned_project_or_404(db, project_id, principal)
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
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, kwargs.pop("principal"))
    finally:
        db.close()
    return _editor_call(fn, project_id, *args, **kwargs)


@router.get("/projects/{project_id}/revisions/{revision_id}/studio")
def get_studio(project_id: str, revision_id: str, principal=Depends(require_user)):
    return _studio_call(
        editor_studio.studio_view, project_id, revision_id, principal=principal
    )


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
    db = SessionLocal()
    try:
        return _library_call(
            asset_library.get_asset, db, principal, content_hash
        )
    finally:
        db.close()


@router.get("/library/assets/{content_hash}/media")
def serve_library_media(content_hash: str, principal=Depends(require_user)):
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
    db = SessionLocal()
    try:
        return _library_call(asset_library.list_duplicates, db, principal)
    finally:
        db.close()


@router.get("/library/storage")
def get_library_storage(principal=Depends(require_user)):
    db = SessionLocal()
    try:
        return _library_call(asset_library.storage_view, db, principal)
    finally:
        db.close()


@router.get("/library/folders")
def list_library_folders(principal=Depends(require_user)):
    db = SessionLocal()
    try:
        return _library_call(asset_library.list_folders, db, principal)
    finally:
        db.close()


@router.post("/library/folders", status_code=201)
def create_library_folder(
    body: LibraryFolderIn, principal=Depends(post_guard)
):
    db = SessionLocal()
    try:
        return _library_call(
            asset_library.create_folder, db, principal, body.name
        )
    finally:
        db.close()


@router.delete("/library/folders/{folder_id}")
def delete_library_folder(folder_id: str, principal=Depends(post_guard)):
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
    db = SessionLocal()
    try:
        _get_owned_project_or_404(db, project_id, principal)
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
