import json
import os
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import Base, engine, get_db
from models import Project

from jobs import (
    enqueue_test_job,
    enqueue_script_analysis,
    enqueue_full_pipeline,
)

from services import media_paths

from api_v1 import register_error_handlers
from api_v1 import router as api_v1_router


app = FastAPI(
    title="Documentary Studio API",
    version="0.3.0",
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


# Stage 4: the /api/v1 studio contract (api_v1.py). The legacy
# routes below are unchanged; the v1 error handlers only reshape
# errors for /api/v1 paths.
app.include_router(api_v1_router, prefix="/api/v1")
register_error_handlers(app)


Base.metadata.create_all(bind=engine)


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
    music: bool = True


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
# TEST JOB
# =========================================================

@app.post("/projects/{project_id}/test-job")
def start_test_job(project_id: str):
    job_id = enqueue_test_job(project_id)

    return {
        "project_id": project_id,
        "job_id": job_id,
        "status": "queued",
    }


# =========================================================
# START FULL DOCUMENTARY PIPELINE
# =========================================================

@app.post("/projects/{project_id}/analyze")
def analyze_project(
    project_id: str,
    db: Session = Depends(get_db),
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

    return {
        "project_id": project.id,
        "job_id": job_id,
        "status": "pipeline_queued",
        "message": "Full documentary production pipeline queued.",
    }