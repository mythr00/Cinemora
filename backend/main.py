import json
import os
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import Base, check_schema, engine, get_db
from models import Project

from jobs import enqueue_full_pipeline

from services import media_paths

# IA-08: the legacy routes below reuse API v1's auth verbatim —
# require_api_token / require_user are imported (api_v1 does not
# import main, so there is no cycle) so legacy and v1 share the
# ONE token, the ONE env var (CINEMORA_API_TOKEN) and the ONE
# fail-closed semantics (503 unconfigured / 401 missing-or-wrong).
# No second token or config exists. ApiError is imported so the
# deprecated test-job route can answer in the v1 error shape.
# E-03: the project-data routes additionally resolve the full
# principal (require_user) and enforce v1 ownership — see
# _legacy_project_or_404 below.
from api_v1 import (
    ApiError,
    _get_owned_project_or_404,
    register_error_handlers,
    require_api_token,
    require_user,
    sanitize_failure_detail,
)
from api_v1 import router as api_v1_router
from services import auth as auth_service
from services import plans as plans_service
from api_admin import router as api_admin_router
from api_assistant import router as api_assistant_router
from api_developer import router as api_developer_router
from api_public import router as api_public_router
from services import logging_setup as logging_setup_service


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # IA-09: serving startup verifies the database is at the
    # Alembic migration head (database.check_schema — read-only;
    # missing tables or a behind-head stamp raise and the server
    # refuses to start). Importing this module does NOT run the
    # check, so tests that import main / build TestClient without
    # entering it are unaffected; their explicit fixture
    # create_all calls remain their own test setup.
    check_schema()
    yield


app = FastAPI(
    title="Documentary Studio API",
    version="0.3.0",
    lifespan=_lifespan,
)


# CORS origins come from the environment (Stage 4, AUD-04):
# CINEMORA_CORS_ORIGINS is a comma-separated list, never a wildcard.
# Unset, the historical localhost pair is preserved so the legacy
# routes behave exactly as before.
_cors_origins = [
    origin.strip()
    for origin in os.environ.get("CINEMORA_CORS_ORIGINS", "").split(",")
    if origin.strip()
] or [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# B-13 (Phase 5c-1): structured logging for the cinemora logger
# tree (idempotent) + request-ID middleware — honours a safe
# incoming X-Request-ID or generates one, binds it to the logging
# context for the request, and echoes it in the response header.
logging_setup_service.setup_logging()
app.add_middleware(logging_setup_service.RequestIdMiddleware)


# Stage 4: the /api/v1 studio contract (api_v1.py). The v1 error
# handlers only reshape HTTP/validation errors for /api/v1 paths.
# IA-08: the legacy routes below keep their paths and response
# shapes, but every route that reads or writes project data now
# requires the same X-API-Key auth as v1 (require_api_token);
# GET / and GET /health stay open (no project data, like v1 health).
# E-03: project-data routes also enforce v1 OWNERSHIP (the
# require_user principal through _get_owned_project_or_404).
app.include_router(api_v1_router, prefix="/api/v1")
# Phase 7: the administration API lives under the same /api/v1
# prefix (its router adds /admin), gated by its own role guard.
app.include_router(api_admin_router, prefix="/api/v1")
# Phase 6: the AI Product Assistant API under the same prefix (its
# router adds /assistant), gated by the same principal auth.
app.include_router(api_assistant_router, prefix="/api/v1")
# Phase N (founder #16): the Developer API management surfaces
# (account API keys, webhooks, docs) under the same /api/v1 prefix,
# and the public key-authenticated surface under its own versioned
# prefix. The public router delegates to the api_v1 handlers; its
# errors take the v1 shape (register_error_handlers covers both
# prefixes).
app.include_router(api_developer_router, prefix="/api/v1")
app.include_router(api_public_router, prefix="/api/public/v1")
register_error_handlers(app)


# IA-09: there is deliberately NO Base.metadata.create_all here
# any more. Alembic (migrations/versions) is the sole schema
# authority; the import-time create_all competed with the
# migration chain and silently papered over unmigrated databases.
# Serving startup verifies the schema instead — see _lifespan
# above and database.check_schema. (Base/engine stay imported for
# compatibility with anything that reaches them via this module.)


# =========================================================
# LEGACY OWNERSHIP (E-03 / B-16)
# =========================================================
# IA-08 unified legacy AUTHENTICATION on require_user but not
# AUTHORISATION: the handlers below loaded any project by id for
# any valid principal, and legacy creation set no owner_id. Every
# project load on the legacy surface now goes through the SAME
# choke point as v1 (api_v1._get_owned_project_or_404 — owner, or
# accepted project member, or the operator principal for legacy
# NULL-owner rows; everyone else 404, never 403, so existence
# stays tenant-private). Only the ERROR SHAPE is translated back
# to the legacy contract ({"detail": ...}); the status semantics
# are v1's exactly, including a viewer member's 403
# project_read_only on the mutating route.

def _legacy_project_or_404(
    db: Session,
    project_id: str,
    principal,
    *,
    write: bool = False,
) -> Project:
    if not isinstance(principal, auth_service.Principal):
        # Direct in-process call (tests / internal callers):
        # FastAPI dependencies are not evaluated off-HTTP, so no
        # principal exists — the pre-existing contract for such
        # callers (see test_stage0_observability, which calls
        # get_project_status with an explicit session). Over HTTP
        # this branch is unreachable: require_user always resolves
        # a real Principal or rejects the request first.
        project = db.get(Project, str(project_id))
        if project is None:
            raise HTTPException(
                status_code=404,
                detail="Project not found",
            )
        return project
    try:
        return _get_owned_project_or_404(
            db, project_id, principal, write=write
        )
    except ApiError as error:
        if error.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail="Project not found",
            ) from error
        raise HTTPException(
            status_code=error.status_code,
            detail=error.message,
        ) from error


# =========================================================
# PRODUCTION OPTIONS
# =========================================================

class ProductionOptions(BaseModel):
    deepResearch: bool = True
    realFootage: bool = True
    archiveImages: bool = True
    maps: bool = True
    graphics: bool = True
    captions: bool = True
    # Stage H (D9): the legacy "music: bool" field was removed — it
    # was accepted by this schema and silently discarded (the
    # legacy handler persists no options at all). The live music
    # contract is the v1 API's music_file / music_enabled /
    # music_volume / narration_volume (services.music).


# =========================================================
# PROJECT CREATION
# =========================================================

class ProjectCreate(BaseModel):
    script: str = Field(min_length=1)
    category: str = "Documentary"
    options: ProductionOptions = ProductionOptions()


# =========================================================
# ROOT
# =========================================================

@app.get("/")
def root():
    return {
        "name": "Documentary Studio API",
        "status": "online",
        "version": "0.3.0",
    }


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
def health():
    return {
        "status": "healthy",
    }


@app.get("/health/live")
def health_live():
    """B-15 public liveness: exactly {"status": "ok"} and nothing
    else (the historical /health shape above is kept compatible;
    readiness detail is admin-only at /api/v1/admin/readiness)."""
    from services import readiness as readiness_service

    return readiness_service.liveness()


# =========================================================
# CREATE PROJECT
# =========================================================

@app.post("/projects")
def create_project(
    project_data: ProjectCreate,
    db: Session = Depends(get_db),
    principal: auth_service.Principal = Depends(require_user),
):
    project_id = str(uuid4())

    # B-16: the project belongs to its creator (v1 parity — the
    # row is visible in the creator's v1 list and covered by the
    # ownership choke point), and v1's creation admission rule
    # (plan script-length) applies here too. Fail-soft when no
    # plan system is seeded, exactly as in v1.
    plans_service.enforce_script_length(
        db, principal.user_id, project_data.script
    )

    project = Project(
        id=project_id,
        owner_id=principal.user_id,
        script=project_data.script,
        category=project_data.category,
        status="created",
    )

    db.add(project)
    db.commit()
    db.refresh(project)

    return {
        "id": project.id,
        "status": project.status,
        "message": "Project created successfully.",
    }


# =========================================================
# GET PROJECT
# =========================================================

@app.get("/projects/{project_id}")
def get_project(
    project_id: str,
    db: Session = Depends(get_db),
    principal: auth_service.Principal = Depends(require_user),
):
    # E-03: ownership enforced via the v1 choke point.
    project = _legacy_project_or_404(db, project_id, principal)

    return {
        "id": project.id,
        "script": project.script,
        "category": project.category,
        "status": project.status,
        "created_at": project.created_at,
    }


# =========================================================
# GET PROJECT STATUS (AUD-03: status write-back + progress)
# =========================================================

@app.get("/projects/{project_id}/status")
def get_project_status(
    project_id: str,
    db: Session = Depends(get_db),
    # IA-08: auth is a dependency only — direct in-process callers
    # (tests calling this handler with an explicit db session) are
    # unaffected, since the dependency is not evaluated off-HTTP.
    # E-03: over HTTP the resolved principal is ownership-checked
    # via the v1 choke point (see _legacy_project_or_404).
    principal: auth_service.Principal = Depends(require_user),
):
    project = _legacy_project_or_404(db, project_id, principal)

    # The DB row carries the run's headline status (written back by
    # jobs as the pipeline transitions); the persisted production
    # state — when a run has written one — carries the per-stage
    # progress. The state file is written atomically (MA-11) and no
    # longer contains run internals (MA-09), so it is safe to serve.
    progress = None
    # Stage 4 (DATA-004): the state file is run-scoped for new
    # runs — resolve the latest run's copy, legacy path as fallback.
    state_file = media_paths.resolve_artefact(
        media_paths.project_dir(project_id), "production_state.json"
    )
    if state_file.exists():
        try:
            progress = json.loads(
                state_file.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            progress = None

    # FS-4 / FS-A: the embedded production state carries the raw
    # stage failure text (state.error, stages[*].error) — pass the
    # payload through the SAME v1 response-layer sanitizer so the
    # legacy surface cannot re-leak what v1 withholds. Admins and
    # the operator principal keep the full detail; direct
    # in-process callers (no Principal) are sanitized fail-closed.
    return sanitize_failure_detail(
        {
            "id": project.id,
            "status": project.status,
            "category": project.category,
            "created_at": project.created_at,
            "progress": progress,
        },
        principal if isinstance(principal, auth_service.Principal) else None,
    )


# =========================================================
# TEST JOB — DEPRECATED (IA-08)
# =========================================================
# jobs.test_job is a no-op: it prints five steps and returns
# "completed" without touching the project. This route used to
# dress that up as a successfully queued job. It now answers
# 410 Gone in the v1 error shape instead of fake success. The
# route is NOT deleted (no silent breakage for any caller that
# still probes it), and it requires the same auth as every other
# legacy project route for consistency — a 410 leaks nothing,
# but the legacy surface now has one uniform auth pattern.
# jobs.enqueue_test_job itself is untouched in jobs.py.

@app.post("/projects/{project_id}/test-job")
def start_test_job(
    project_id: str,
    _auth: str = Depends(require_api_token),
):
    raise ApiError(
        410,
        "gone",
        "POST /projects/{project_id}/test-job is deprecated and "
        "removed: it only ever queued a no-op test job. Submit a "
        "real production run via POST /api/v1/projects/{project_id}/runs.",
    )


# =========================================================
# START FULL DOCUMENTARY PIPELINE
# =========================================================
# IA-08: this route was named "analyze" but never merely analysed
# — it enqueues the FULL production pipeline. The path is kept
# (renaming it would break callers); the handler name, summary
# and docstring now say what it does, and responses carry
# Deprecation headers pointing at the v1 runs endpoint.

@app.post(
    "/projects/{project_id}/analyze",
    summary="Deprecated: enqueue a full production run (not analysis-only)",
    deprecated=True,
)
def enqueue_full_pipeline_legacy(
    project_id: str,
    response: Response,
    db: Session = Depends(get_db),
    principal: auth_service.Principal = Depends(require_user),
):
    """Enqueue the FULL documentary production pipeline for a project.

    Deprecated legacy alias: despite the /analyze path, this starts
    a complete production run (voice, assets, motion, sound, edit),
    exactly like POST /api/v1/projects/{project_id}/runs.
    """
    # E-03: enqueueing is a mutation — the v1 choke point in its
    # write form (a viewer member is refused 403 project_read_only,
    # a non-member 404), then v1's run-admission rules (plan
    # concurrency / quota / storage) before anything is queued.
    project = _legacy_project_or_404(
        db, project_id, principal, write=True
    )
    if isinstance(principal, auth_service.Principal):
        plans_service.enforce_run_admission(db, principal.user_id)

    # Start the REAL production pipeline.
    #
    # The worker will execute:
    #
    # 1. Voice Actor
    # 2. Asset Manager
    # 3. Motion Designer
    # 4. Sound Designer
    # 5. Video Editor
    #
    job_id = enqueue_full_pipeline(
        project.id,
        project.script,
    )

    response.headers["Deprecation"] = "true"
    response.headers["X-Deprecated"] = (
        "Use POST /api/v1/projects/{project_id}/runs instead."
    )

    return {
        "project_id": project.id,
        "job_id": job_id,
        "status": "pipeline_queued",
        "message": "Full documentary production pipeline queued.",
    }