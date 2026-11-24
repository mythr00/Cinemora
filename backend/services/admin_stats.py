"""Admin aggregation reads (Phase 7; design admin/02 M1/M2/M4).

Thin, read-only aggregation over the records the platform already
keeps: `projects`, `production_runs` (+ its `execution` JSON
snapshot), `users`, `user_subscriptions`, and the per-run artefact
files resolved through services/media_paths. Every figure traces
to those sources; a window with no data reports zeros / nulls,
never an invented value. Monetary cost is NOT computed here:
run_metrics records monetary_cost_usd as None by design (no price
table exists), and this module surfaces exactly that.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone

TERMINAL_STATUSES = ("completed", "failed", "cancelled")
ACTIVE_STATUSES = ("queued", "running", "awaiting_approval")


def _aware(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _window_start(days: int, now: datetime) -> datetime:
    return now - timedelta(days=max(1, days))


def _run_wall_seconds(run) -> float | None:
    execution = run.execution or {}
    wall = execution.get("wall_time_seconds")
    if isinstance(wall, (int, float)) and wall >= 0:
        return float(wall)
    started, finished = _aware(run.started_at), _aware(run.finished_at)
    if started and finished:
        return max(0.0, (finished - started).total_seconds())
    return None


def overview(db, *, days: int = 30, now: datetime | None = None) -> dict:
    """M1 platform overview: counts and rates over real rows."""
    from models import ProductionRun, Project, User

    now = now or datetime.now(timezone.utc)
    since = _window_start(days, now)

    from sqlalchemy import func

    total_projects = db.query(Project).count()
    total_users = db.query(User).count()
    users_by_status = {
        status: count
        for status, count in db.query(User.status, func.count(User.id))
        .group_by(User.status)
        .all()
    }

    runs = db.query(ProductionRun).all()
    by_status: dict[str, int] = {}
    window_by_status: dict[str, int] = {}
    walls: list[float] = []
    window_walls: list[float] = []
    serper_total_window = 0
    llm_total_window = 0
    latest_failures = []
    for run in runs:
        by_status[run.status] = by_status.get(run.status, 0) + 1
        created = _aware(run.created_at)
        in_window = created is not None and created >= since
        if in_window:
            window_by_status[run.status] = (
                window_by_status.get(run.status, 0) + 1
            )
            execution = run.execution or {}
            serper = execution.get("serper_calls") or {}
            if isinstance(serper.get("total"), (int, float)):
                serper_total_window += int(serper["total"])
            llm = execution.get("llm_calls") or {}
            if isinstance(llm.get("total"), (int, float)):
                llm_total_window += int(llm["total"])
        wall = _run_wall_seconds(run)
        if wall is not None:
            walls.append(wall)
            if in_window:
                window_walls.append(wall)
        if run.status in ("failed", "cancelled"):
            latest_failures.append(run)
    latest_failures.sort(
        key=lambda r: _aware(r.created_at) or datetime.min.replace(
            tzinfo=timezone.utc
        ),
        reverse=True,
    )

    window_terminal = sum(
        window_by_status.get(s, 0) for s in TERMINAL_STATUSES
    )
    window_completed = window_by_status.get("completed", 0)

    return {
        "generated_at": now.isoformat(),
        "window_days": days,
        "projects_total": total_projects,
        "users_total": total_users,
        "users_by_status": users_by_status,
        "runs_total": len(runs),
        "runs_by_status": by_status,
        "runs_window_by_status": window_by_status,
        "completion_rate_window": (
            window_completed / window_terminal if window_terminal else None
        ),
        "median_wall_seconds": statistics.median(walls) if walls else None,
        "median_wall_seconds_window": (
            statistics.median(window_walls) if window_walls else None
        ),
        "serper_calls_window": serper_total_window,
        "llm_calls_window": llm_total_window,
        "active_runs": sum(by_status.get(s, 0) for s in ACTIVE_STATUSES),
        "latest_failures": [
            {
                "run_id": run.id,
                "project_id": run.project_id,
                "status": run.status,
                "stage": run.stage,
                "error": (run.error or "")[:300] or None,
                "created_at": _aware(run.created_at).isoformat()
                if run.created_at
                else None,
            }
            for run in latest_failures[:5]
        ],
    }


def list_runs(
    db,
    *,
    status: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """M2 cross-project run list (newest first) with presence for
    active runs."""
    from models import ProductionRun, Project
    from services import run_ledger

    q = db.query(ProductionRun)
    if status:
        q = q.filter(ProductionRun.status == status)
    if project_id:
        q = q.filter(ProductionRun.project_id == project_id)
    total = q.count()
    rows = (
        q.order_by(ProductionRun.created_at.desc())
        .offset(max(0, offset))
        .limit(max(1, min(limit, 200)))
        .all()
    )
    titles = {
        p.id: p.title
        for p in db.query(Project).filter(
            Project.id.in_([r.project_id for r in rows])
        ).all()
    } if rows else {}
    out = []
    for run in rows:
        entry = {
            "run_id": run.id,
            "project_id": run.project_id,
            "project_title": titles.get(run.project_id),
            "owner_id": run.owner_id,
            "status": run.status,
            "stage": run.stage,
            "error": (run.error or "")[:300] or None,
            "cancel_requested": bool(run.cancel_requested),
            "created_at": _aware(run.created_at).isoformat()
            if run.created_at
            else None,
            "started_at": _aware(run.started_at).isoformat()
            if run.started_at
            else None,
            "finished_at": _aware(run.finished_at).isoformat()
            if run.finished_at
            else None,
            "wall_seconds": _run_wall_seconds(run),
            "presence": None,
        }
        if run.status in ACTIVE_STATUSES:
            entry["presence"] = run_ledger.run_presence(
                run_ledger.get_run(run.id)
            )
        out.append(entry)
    return {"total": total, "runs": out}


def _read_json(path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_detail(db, run_id: str) -> dict | None:
    """M2 run detail: the ledger row, its execution snapshot, and
    bounded summaries of the run's artefact files (metadata only —
    no script or footage content, per design admin/02 cross-cutting
    rule 9)."""
    from models import ProductionRun, Project
    from services import media_paths, run_ledger

    run = db.get(ProductionRun, str(run_id))
    if run is None:
        return None
    project = db.get(Project, run.project_id)
    project_dir = media_paths.project_dir(run.project_id)

    asset_report = _read_json(
        media_paths.resolve_artefact(
            project_dir, "asset_report.json", run_id=run.id
        )
    )
    production_state = _read_json(
        media_paths.resolve_artefact(
            project_dir, "production_state.json", run_id=run.id
        )
    )
    report_summary = None
    if asset_report is not None:
        providers = asset_report.get("providers") or {}
        report_summary = {
            "providers_funnel": providers.get("funnel"),
            "single_provider_reliance": providers.get(
                "single_provider_reliance"
            ),
            "rejection_reasons": asset_report.get("rejection_reasons"),
            "run_failure": asset_report.get("run_failure"),
            "coverage": asset_report.get("coverage"),
        }
    state_summary = None
    if production_state is not None:
        stages = production_state.get("stages") or {}
        state_summary = {
            "current_stage": production_state.get("current_stage"),
            "stages": {
                name: (stage or {}).get("status")
                for name, stage in stages.items()
                if isinstance(stage, dict)
            },
        }
    return {
        "run_id": run.id,
        "project_id": run.project_id,
        "project_title": project.title if project else None,
        "owner_id": run.owner_id,
        "status": run.status,
        "stage": run.stage,
        "error": run.error,
        "cancel_requested": bool(run.cancel_requested),
        "options": run.options,
        "script_hash": run.script_hash,
        "created_at": _aware(run.created_at).isoformat()
        if run.created_at
        else None,
        "started_at": _aware(run.started_at).isoformat()
        if run.started_at
        else None,
        "finished_at": _aware(run.finished_at).isoformat()
        if run.finished_at
        else None,
        "wall_seconds": _run_wall_seconds(run),
        "presence": run_ledger.run_presence(run_ledger.get_run(run.id)),
        "execution": run.execution,
        "asset_report": report_summary,
        "production_state": state_summary,
        "studio_project_path": f"/projects/{run.project_id}",
    }


def provider_usage(db, *, days: int = 30, now: datetime | None = None) -> dict:
    """M4 provider usage: call counts aggregated from the per-run
    `execution` snapshots. Cost is reported exactly as run_metrics
    records it: None (not instrumented), never 0 or estimated."""
    from models import ProductionRun

    now = now or datetime.now(timezone.utc)
    since = _window_start(days, now)
    totals = {
        "serper_calls": {"total": 0, "web": 0, "images": 0, "video": 0},
        "llm_calls": {"total": 0, "total_tokens": 0},
        "archive_org_searches": 0,
        "wikimedia_searches": 0,
        "candidates_discovered": 0,
        "downloads": {
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "bytes_downloaded": 0,
        },
    }
    runs_in_window = 0
    per_run = []
    for run in db.query(ProductionRun).all():
        created = _aware(run.created_at)
        if created is None or created < since:
            continue
        execution = run.execution or {}
        if not execution:
            continue
        runs_in_window += 1
        serper = execution.get("serper_calls") or {}
        for kind in ("total", "web", "images", "video"):
            if isinstance(serper.get(kind), (int, float)):
                totals["serper_calls"][kind] += int(serper[kind])
        llm = execution.get("llm_calls") or {}
        for kind in ("total", "total_tokens"):
            if isinstance(llm.get(kind), (int, float)):
                totals["llm_calls"][kind] += int(llm[kind])
        for key in ("archive_org_searches", "wikimedia_searches",
                    "candidates_discovered"):
            if isinstance(execution.get(key), (int, float)):
                totals[key] += int(execution[key])
        downloads = execution.get("downloads") or {}
        for kind in ("attempted", "succeeded", "failed", "bytes_downloaded"):
            if isinstance(downloads.get(kind), (int, float)):
                totals["downloads"][kind] += int(downloads[kind])
        per_run.append(
            {
                "run_id": run.id,
                "project_id": run.project_id,
                "status": run.status,
                "serper_calls": serper,
                "llm_calls": llm,
                "downloads": downloads,
            }
        )
    per_run.sort(key=lambda e: e["run_id"])
    return {
        "generated_at": now.isoformat(),
        "window_days": days,
        "runs_with_execution": runs_in_window,
        "totals": totals,
        "per_run": per_run,
        "monetary_cost_usd": None,
        "monetary_cost_note": (
            "unknown — no price table configured "
            "(services/run_metrics design rule: never 0, never estimated)"
        ),
    }
