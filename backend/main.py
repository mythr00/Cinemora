import json
import os
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import Base, check_schema, engine, get_db
from models import Project

from jobs import enqueue_full_pipeline

from services import media_paths

# IA-08: the legacy routes below reuse API v1's auth verbatim —
# require_api_token is imported (api_v1 does not import main, so
# there is no cycle) so legacy and v1 share the ONE token, the ONE
# env var (CINEMORA_API_TOKEN) and the ONE fail-closed semantics
# (503 unconfigured / 401 missing-or-wrong). No second token or
# config exists. ApiError is imported so the deprecated test-job
# route can answer in the v1 error shape.
from api_v1 import ApiError, register_error_handlers, require_api_token
from api_v1 import router as api_v1_router


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


# Stage 4: the /api/v1 studio contract (api_v1.py). The v1 error
# handlers only reshape HTTP/validation errors for /api/v1 paths.
# IA-08: the legacy routes below keep their paths and response
# shapes, but every route that reads or writes project data now
# requires the same X-API-Key auth as v1 (require_api_token);
# GET / and GET /health stay open (no project data, like v1 health).
app.include_router(api_v1_router, prefix="/api/v1")
register_error_handlers(app)


# IA-09: there is deliberately NO Base.metadata.create_all here
# any more. Alembic (migrations/versions) is the sole schema
# authority; the import-time create_all competed with the
# migration chain and silently papered over unmigrated databases.
# Serving startup verifies the schema instead — see _lifespan
# above and database.check_schema. (Base/engine stay imported for
# compatibility with anything that reaches them via this module.)


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


# =========================================================
# CREATE PROJECT
# =========================================================

@app.post("/projects")
def create_project(
    project_data: ProjectCreate,
    db: Session = Depends(get_db),
    _auth: str = Depends(require_api_token),
):
    project_id = str(uuid4())

    project = Project(
        id=project_id,
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
    _auth: str = Depends(require_api_token),
):
    statement = select(Project).where(
        Project.id == project_id
    )

    project = db.execute(statement).scalar_one_or_none()

    if project is None:
        raise HTTPException(
            status_code=404,
            detail="Project not found",
        )

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
    _auth: str = Depends(require_api_token),
):
    statement = select(Project).where(
        Project.id == project_id
    )

    project = db.execute(statement).scalar_one_or_none()

    if project is None:
        raise HTTPException(
            status_code=404,
            detail="Project not found",
        )

    # The DB row carries the run's headline status (written back by
    # jobs as the pipeline transitions); the persisted production
    # state — when a run has written one — carries the per-stage
    # progress. The state file is written atomically (MA-11) and no
    # longer contains run internals (MA-09), so it is safe to serve.
    progress = None
    state_file = media_paths.project_dir(project_id) / "production_state.json"
    if state_file.exists():
        try:
            progress = json.loads(
                state_file.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            progress = None

    return {
        "id": project.id,
        "status": project.status,
        "category": project.category,
        "created_at": project.created_at,
        "progress": progress,
    }


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
    _auth: str = Depends(require_api_token),
):
    """Enqueue the FULL documentary production pipeline for a project.

    Deprecated legacy alias: despite the /analyze path, this starts
    a complete production run (voice, assets, motion, sound, edit),
    exactly like POST /api/v1/projects/{project_id}/runs.
    """
    statement = select(Project).where(
        Project.id == project_id
    )

    project = db.execute(statement).scalar_one_or_none()

    if project is None:
        raise HTTPException(
            status_code=404,
            detail="Project not found",
        )

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