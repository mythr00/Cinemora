"""API v1 — the studio contract (Stage 4).

Mounted from main.py under /api/v1. The six legacy routes in
main.py are untouched and keep their original shapes; everything in
this module follows the v1 contract documented in
~/workspace/cinemora-audit/API_CONTRACT.md:

- Single-operator token auth (AUD-11): every route except
  GET /api/v1/health requires the X-API-Key header to match the
  CINEMORA_API_TOKEN environment variable (constant-time compare).
  When the variable is unset the API fails CLOSED: 503
  "api_not_configured", never open.
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

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

import jobs
from database import SessionLocal
from models import Project
from services import (
    editor,
    genre_packs,
    media_paths,
    narration,
    plan_document,
    run_ledger,
)

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


def require_api_token(request: Request) -> str:
    """Single-operator auth. Fails closed when unconfigured."""
    configured = os.environ.get("CINEMORA_API_TOKEN") or ""
    if not configured:
        raise ApiError(
            503,
            "api_not_configured",
            "The API is not configured: CINEMORA_API_TOKEN is not set "
            "on the server, so /api/v1 is closed.",
        )
    provided = request.headers.get("X-API-Key") or ""
    if not provided or not hmac.compare_digest(provided, configured):
        raise ApiError(
            401, "unauthorized", "A valid X-API-Key header is required."
        )
    return "operator"


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

    def check(self, identity: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[identity]
            while hits and now - hits[0] > 60.0:
                hits.popleft()
            if len(hits) >= self._limit():
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


def post_guard(identity: str = Depends(require_api_token)) -> str:
    """Auth + rate limit, for POST routes."""
    rate_limiter.check(identity)
    return identity


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


def _get_project_or_404(db, project_id: str) -> Project:
    project = db.get(Project, str(project_id))
    if project is None:
        raise ApiError(404, "project_not_found", f"No project {project_id}.")
    return project


def _options_dict(options) -> dict:
    if options is None:
        return {}
    if isinstance(options, BaseModel):
        return options.model_dump(exclude_none=True)
    return dict(options)


def _production_state(project_dir: Path) -> dict | None:
    try:
        return json.loads(
            (project_dir / "production_state.json").read_text(encoding="utf-8")
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
def list_genre_catalogue(_=Depends(require_api_token)):
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
def list_voice_catalogue(_=Depends(require_api_token)):
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
def create_project(body: ProjectCreateIn, _=Depends(post_guard)):
    db = SessionLocal()
    try:
        project = Project(
            id=str(uuid4()),
            title=body.title,
            script=body.script,
            category=body.category or "Documentary",
            options=_options_dict(body.options) or None,
            status="created",
        )
        db.add(project)
        db.commit()
        db.refresh(project)
        return _project_view(db, project, detail=True)
    finally:
        db.close()


@router.get("/projects")
def list_projects(_=Depends(require_api_token)):
    db = SessionLocal()
    try:
        projects = (
            db.query(Project).order_by(Project.created_at.desc()).all()
        )
        return {"projects": [_project_view(db, p) for p in projects]}
    finally:
        db.close()


@router.get("/projects/{project_id}")
def get_project(project_id: str, _=Depends(require_api_token)):
    db = SessionLocal()
    try:
        project = _get_project_or_404(db, project_id)
        return _project_view(db, project, detail=True)
    finally:
        db.close()


@router.get("/projects/{project_id}/status")
def get_project_status(project_id: str, _=Depends(require_api_token)):
    db = SessionLocal()
    try:
        project = _get_project_or_404(db, project_id)
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
def submit_run(project_id: str, body: RunSubmitIn | None = None, _=Depends(post_guard)):
    db = SessionLocal()
    try:
        project = _get_project_or_404(db, project_id)
        script = project.script
        project_options = dict(project.options or {})
    finally:
        db.close()

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
def list_project_runs(project_id: str, _=Depends(require_api_token)):
    db = SessionLocal()
    try:
        _get_project_or_404(db, project_id)
    finally:
        db.close()
    runs = run_ledger.list_runs_for(project_id)
    return {"runs": [_run_view(run) for run in runs]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, _=Depends(require_api_token)):
    run = run_ledger.get_run(run_id)
    if run is None:
        raise ApiError(404, "run_not_found", f"No run {run_id}.")
    return _run_view(run)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, _=Depends(post_guard)):
    run = run_ledger.get_run(run_id)
    if run is None:
        raise ApiError(404, "run_not_found", f"No run {run_id}.")
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
def get_plan(project_id: str, _=Depends(require_api_token)):
    db = SessionLocal()
    try:
        project = _get_project_or_404(db, project_id)
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
def generate_plan(project_id: str, _=Depends(post_guard)):
    db = SessionLocal()
    try:
        project = _get_project_or_404(db, project_id)
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
    """Append one decision to plan_decisions.json (atomic rewrite)."""
    import tempfile

    path = project_dir / plan_document.DECISIONS_FILENAME
    decisions = plan_document.load_plan_decisions(project_dir)
    decisions.append(record)
    payload = json.dumps(
        {"version": 1, "decisions": decisions}, indent=2, ensure_ascii=False
    )
    project_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(project_dir), prefix=".plan_decisions.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
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
    decisions_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

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
def post_plan_decision(project_id: str, body: PlanDecisionIn, _=Depends(post_guard)):
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
        project = _get_project_or_404(db, project_id)
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
def approve_plan(project_id: str, _=Depends(post_guard)):
    db = SessionLocal()
    try:
        project = _get_project_or_404(db, project_id)
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
    _=Depends(require_api_token),
):
    db = SessionLocal()
    try:
        _get_project_or_404(db, project_id)
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
def get_outputs(project_id: str, _=Depends(require_api_token)):
    db = SessionLocal()
    try:
        _get_project_or_404(db, project_id)
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
def serve_media(project_id: str, asset_key: str, _=Depends(require_api_token)):
    db = SessionLocal()
    try:
        _get_project_or_404(db, project_id)
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


@router.get("/projects/{project_id}/certificate")
def get_certificate(project_id: str, _=Depends(require_api_token)):
    db = SessionLocal()
    try:
        project = _get_project_or_404(db, project_id)
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
    return plan_document.build_certificate(
        project_view, latest, decisions, counts
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
def get_editor(project_id: str, _=Depends(require_api_token)):
    db = SessionLocal()
    try:
        _get_project_or_404(db, project_id)
    finally:
        db.close()
    view, doc = _editor_call(editor.get_editor, project_id)
    return {**view, "document": doc}


@router.post("/projects/{project_id}/revisions", status_code=201)
def create_revision(
    project_id: str,
    body: CreateRevisionBody,
    _=Depends(post_guard),
):
    db = SessionLocal()
    try:
        _get_project_or_404(db, project_id)
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
def list_revisions(project_id: str, _=Depends(require_api_token)):
    db = SessionLocal()
    try:
        _get_project_or_404(db, project_id)
    finally:
        db.close()
    return {"revisions": _editor_call(editor.list_revisions, project_id)}


@router.get("/projects/{project_id}/revisions/{revision_id}")
def get_revision(project_id: str, revision_id: str, _=Depends(require_api_token)):
    db = SessionLocal()
    try:
        _get_project_or_404(db, project_id)
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
    _=Depends(post_guard),
):
    db = SessionLocal()
    try:
        _get_project_or_404(db, project_id)
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
    _=Depends(post_guard),
):
    db = SessionLocal()
    try:
        _get_project_or_404(db, project_id)
    finally:
        db.close()
    return _editor_call(
        editor.request_render,
        project_id,
        revision_id,
        allow_gaps=body.allow_gaps,
    )


@router.get("/projects/{project_id}/revisions/{revision_id}/exports")
def list_exports(project_id: str, revision_id: str, _=Depends(require_api_token)):
    db = SessionLocal()
    try:
        _get_project_or_404(db, project_id)
    finally:
        db.close()
    _view, doc = _editor_call(editor.get_revision, project_id, revision_id)
    return {"exports": doc.get("exports") or []}


@router.post("/projects/{project_id}/uploads", status_code=201)
def upload_file(
    project_id: str,
    file: UploadFile = File(...),
    _=Depends(post_guard),
):
    db = SessionLocal()
    try:
        _get_project_or_404(db, project_id)
    finally:
        db.close()
    return _editor_call(
        editor.store_upload, project_id, file.filename, file.file
    )


@router.get("/projects/{project_id}/revisions/{revision_id}/certificate")
def get_revision_certificate(
    project_id: str, revision_id: str, _=Depends(require_api_token)
):
    db = SessionLocal()
    try:
        project = _get_project_or_404(db, project_id)
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
