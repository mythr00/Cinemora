"""Admin storage reads (Phase 7; design admin/02 M6 — read-only).

Walks the media tree (services/media_paths.media_root) and joins it
against the projects / production_runs tables: total size,
per-project and per-run sizes, largest artefacts, and the orphan
report (media directories with no database project, projects whose
media directory is missing, run directories of long-terminal runs).

The walk is cached for a short TTL: media grows unbounded and the
report must never become a per-request full-tree scan. Nothing in
this module deletes or moves anything — v1 storage administration
is inspection only (cleanup is a later, consequential-class phase).
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

from services import media_paths

_CACHE_TTL_SECONDS = 60.0
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "root": None, "data": None}

TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def invalidate_cache() -> None:
    with _lock:
        _cache["at"] = 0.0
        _cache["data"] = None
        _cache["root"] = None


def _walk_tree(root) -> dict:
    projects: dict[str, dict] = {}
    largest: list[dict] = []
    total_bytes = 0
    total_files = 0
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            project_bytes = 0
            project_files = 0
            runs: dict[str, int] = {}
            for dirpath, _dirnames, filenames in os.walk(entry):
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        continue
                    project_bytes += size
                    project_files += 1
                    parts = os.path.relpath(path, entry).split(os.sep)
                    if len(parts) >= 2 and parts[0] == "runs":
                        runs[parts[1]] = runs.get(parts[1], 0) + size
                    largest.append(
                        {
                            "path": os.path.relpath(path, root),
                            "project_id": entry.name,
                            "bytes": size,
                        }
                    )
            projects[entry.name] = {
                "bytes": project_bytes,
                "files": project_files,
                "runs": runs,
            }
            total_bytes += project_bytes
            total_files += project_files
    largest.sort(key=lambda item: item["bytes"], reverse=True)
    return {
        "root": str(root),
        "total_bytes": total_bytes,
        "total_files": total_files,
        "projects": projects,
        "largest_artefacts": largest[:20],
    }


def walk(force: bool = False) -> dict:
    """The (cached) media-tree walk."""
    root = media_paths.media_root()
    now = time.monotonic()
    with _lock:
        if (
            not force
            and _cache["data"] is not None
            and _cache["root"] == str(root)
            and now - _cache["at"] < _CACHE_TTL_SECONDS
        ):
            return _cache["data"]
    data = _walk_tree(root)
    with _lock:
        _cache.update(at=now, root=str(root), data=data)
    return data


def summary(db) -> dict:
    """M6 headline: totals + per-project sizes joined with the DB."""
    from models import Project

    data = walk()
    db_projects = {p.id: p for p in db.query(Project).all()}
    projects = []
    for project_id, info in sorted(
        data["projects"].items(), key=lambda kv: kv[1]["bytes"], reverse=True
    ):
        project = db_projects.get(project_id)
        projects.append(
            {
                "project_id": project_id,
                "title": project.title if project else None,
                "status": project.status if project else None,
                "in_database": project is not None,
                "bytes": info["bytes"],
                "files": info["files"],
                "runs": [
                    {"run_id": run_id, "bytes": size}
                    for run_id, size in sorted(
                        info["runs"].items(), key=lambda kv: kv[1],
                        reverse=True,
                    )
                ],
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "media_root": data["root"],
        "total_bytes": data["total_bytes"],
        "total_files": data["total_files"],
        "projects": projects,
        "projects_missing_media": [
            {"project_id": p.id, "title": p.title, "status": p.status}
            for p in db_projects.values()
            if p.id not in data["projects"]
        ],
        "largest_artefacts": data["largest_artefacts"],
    }


def orphans(db, *, old_terminal_days: int = 30) -> dict:
    """M6 orphan report — every entry verifiable by hand from the
    paths it names. Report only; nothing is deleted."""
    from models import ProductionRun, Project

    data = walk()
    db_project_ids = {p.id for p in db.query(Project.id).all()}
    unknown_dirs = sorted(
        pid for pid in data["projects"] if pid not in db_project_ids
    )
    missing_media = sorted(db_project_ids - set(data["projects"]))

    cutoff = datetime.now(timezone.utc).timestamp() - (
        max(1, old_terminal_days) * 86400
    )
    old_terminal = []
    for run in (
        db.query(ProductionRun)
        .filter(ProductionRun.status.in_(TERMINAL_STATUSES))
        .all()
    ):
        finished = run.finished_at
        if finished is None:
            continue
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        if finished.timestamp() >= cutoff:
            continue
        run_dir = media_paths.run_dir(run.project_id, run.id)
        if run_dir.is_dir():
            old_terminal.append(
                {
                    "run_id": run.id,
                    "project_id": run.project_id,
                    "status": run.status,
                    "finished_at": finished.isoformat(),
                    "path": str(run_dir),
                    "bytes": data["projects"]
                    .get(run.project_id, {})
                    .get("runs", {})
                    .get(run.id, 0),
                }
            )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "media_directories_without_project": [
            {
                "project_id": pid,
                "path": str(media_paths.project_dir(pid)),
                "bytes": data["projects"][pid]["bytes"],
            }
            for pid in unknown_dirs
        ],
        "projects_without_media_directory": missing_media,
        "old_terminal_run_directories": old_terminal,
        "old_terminal_threshold_days": old_terminal_days,
    }
