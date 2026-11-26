"""READ tools: account, projects, production (design assistant/06).

Every handler scopes by the caller's user id and loads projects /
runs through api_v1's owner choke points, so a foreign id is the
same 404 as a missing one. Handlers wrap the same service and
route logic the v1 API uses — nothing here re-implements a read
the platform already has, and artefact readers tolerate missing
keys from older runs, reporting "not recorded" instead of failing
(design 09, risk 6).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

import api_v1
from api_v1 import ApiError
from database import SessionLocal
from models import ProductionRun, Project, User, UserPreference
from services import editor, media_paths, plan_document, plans as plans_service
from services import project_intelligence, run_ledger
from services.assistant import boundary
from services.assistant.registry import READ, EmptyArgs, ToolSpec, register


# ---------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------


def _owned_project(principal, project_id: str):
    """(db, project) with the owner choke point applied. Caller
    closes db."""
    db = SessionLocal()
    try:
        project = api_v1._get_owned_project_or_404(db, project_id, principal)
        return db, project
    except Exception:
        db.close()
        raise


def _read_artefact(project_id: str, filename: str, run_id: str | None = None):
    project_dir = media_paths.project_dir(project_id)
    try:
        return json.loads(
            media_paths.resolve_artefact(
                project_dir, filename, run_id
            ).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None


def _latest_production_run(project_id: str) -> dict | None:
    return run_ledger.latest_production_run_for(project_id)


# ---------------------------------------------------------------------
# FS-H (FS-14): the Assistant is a user surface, so its in-process
# reads must apply the SAME restrictions the HTTP layer applies —
# calling a service or ledger function directly must not become a
# side channel around the FS-A/FS-B/FS-C gates. The rule is applied
# HERE, at the tool boundary, with the principal the tool already
# receives; the underlying service functions stay ungated because
# the pipeline itself calls them. Admin/operator principals
# (is_admin) keep full detail. Developer-API principals never
# reach the Assistant (session surface), so no api_key exemption
# applies here.
# ---------------------------------------------------------------------


def _is_admin(principal) -> bool:
    return bool(getattr(principal, "is_admin", False))


def _require_admin_tool(principal) -> None:
    """Refuse an internal-detail tool for a non-admin principal,
    with the HTTP layer's admin-only convention (403). Callers run
    the ownership choke point FIRST, so a non-granted principal
    still gets the tenant-private 404 and only a granted non-admin
    reaches this refusal."""
    if not _is_admin(principal):
        raise ApiError(
            403, "forbidden", "This endpoint requires an administrator."
        )


class ProjectArg(BaseModel):
    model_config = {"extra": "forbid"}
    project_id: str = Field(min_length=1, max_length=64)


# ---------------------------------------------------------------------
# ACCOUNT family
# ---------------------------------------------------------------------


def _account_profile(principal, args):
    db = SessionLocal()
    try:
        user = db.get(User, principal.user_id)
        usage = plans_service.usage_summary(db, principal.user_id)
        project_count = (
            db.query(Project)
            .filter(Project.owner_id == principal.user_id)
            .count()
        )
        run_count = (
            db.query(ProductionRun)
            .filter(ProductionRun.owner_id == principal.user_id)
            .count()
        )
        return {
            "user": {
                "email": user.email if user else principal.email,
                "display_name": user.display_name if user else None,
                "role": user.role if user else principal.role,
                "status": user.status if user else None,
                "member_since": user.created_at.isoformat()
                if user and user.created_at
                else None,
            },
            "plan": usage.get("plan"),
            "subscription_status": (
                "active plan record (see plan)"
                if usage.get("plan")
                else "no plan record resolved"
            ),
            "project_count": project_count,
            "run_count": run_count,
            # Item 2 boundary: provider names/configuration are
            # internal. Staff see the real snapshot; customers get
            # generic capability entries with the same truth.
            "providers": (
                run_ledger.provider_snapshot()
                if boundary.is_staff(principal)
                else boundary.sanitize_value(
                    run_ledger.provider_snapshot()
                )
            ),
        }
    finally:
        db.close()


class UsageArgs(BaseModel):
    model_config = {"extra": "forbid"}
    days: int = Field(default=30, ge=1, le=365)


def _account_usage(principal, args):
    db = SessionLocal()
    try:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        runs = (
            db.query(ProductionRun)
            .filter(
                ProductionRun.owner_id == principal.user_id,
                ProductionRun.created_at >= since,
            )
            .all()
        )
        by_status: dict[str, int] = {}
        execution_totals: dict[str, float] = {}
        for run in runs:
            by_status[run.status] = by_status.get(run.status, 0) + 1
            execution = run.execution or {}
            if isinstance(execution, dict):
                for key, value in execution.items():
                    if isinstance(value, (int, float)) and not isinstance(
                        value, bool
                    ):
                        execution_totals[key] = (
                            execution_totals.get(key, 0.0) + float(value)
                        )
        return {
            "days": args.days,
            "runs_total": len(runs),
            "runs_by_status": by_status,
            "execution_totals": execution_totals,
            "note": (
                "Execution totals sum the numeric metrics each run "
                "recorded; a metric a run did not record contributes "
                "nothing rather than a zero-fill."
            ),
        }
    finally:
        db.close()


def _account_preferences(principal, args):
    db = SessionLocal()
    try:
        row = db.get(UserPreference, principal.user_id)
        document = (row.document if row else None) or {}
        return {"preferences": document.get("assistant") or {}}
    finally:
        db.close()


class ActivityArgs(BaseModel):
    model_config = {"extra": "forbid"}
    limit: int = Field(default=10, ge=1, le=50)


def _account_activity(principal, args):
    db = SessionLocal()
    try:
        rows = (
            db.query(ProductionRun, Project.title)
            .join(Project, Project.id == ProductionRun.project_id)
            .filter(ProductionRun.owner_id == principal.user_id)
            .order_by(ProductionRun.created_at.desc())
            .limit(args.limit)
            .all()
        )
        return {
            "activity": [
                {
                    "run_id": run.id,
                    "project_id": run.project_id,
                    "project_title": title,
                    "status": run.status,
                    "stage": run.stage,
                    # FS-H/FS-A: failure detail is sanitized for
                    # non-admins exactly as on the HTTP carriers.
                    "error": (
                        run.error
                        if _is_admin(principal)
                        else (
                            api_v1.GENERIC_FAILURE_MESSAGE
                            if run.error
                            else run.error
                        )
                    ),
                    "created_at": run.created_at.isoformat()
                    if run.created_at
                    else None,
                    "finished_at": run.finished_at.isoformat()
                    if run.finished_at
                    else None,
                }
                for run, title in rows
            ]
        }
    finally:
        db.close()


# ---------------------------------------------------------------------
# PROJECTS family
# ---------------------------------------------------------------------


class ListProjectsArgs(BaseModel):
    model_config = {"extra": "forbid"}
    limit: int = Field(default=20, ge=1, le=100)


def _list_projects(principal, args):
    db = SessionLocal()
    try:
        projects = (
            db.query(Project)
            .filter(Project.owner_id == principal.user_id)
            .order_by(Project.created_at.desc())
            .limit(args.limit)
            .all()
        )
        total = (
            db.query(Project)
            .filter(Project.owner_id == principal.user_id)
            .count()
        )
        return {
            "total_count": total,
            "projects": [
                api_v1._project_view(db, project) for project in projects
            ],
        }
    finally:
        db.close()


class SearchProjectsArgs(BaseModel):
    model_config = {"extra": "forbid"}
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=100)


def _search_projects(principal, args):
    needle = args.query.lower()
    db = SessionLocal()
    try:
        projects = (
            db.query(Project)
            .filter(Project.owner_id == principal.user_id)
            .order_by(Project.created_at.desc())
            .all()
        )
        matches = [
            project
            for project in projects
            if needle in (project.title or "").lower()
            or needle in (project.category or "").lower()
            or needle in (project.script or "").lower()
        ][: args.limit]
        return {
            "query": args.query,
            "projects": [
                api_v1._project_view(db, project) for project in matches
            ],
        }
    finally:
        db.close()


def _project_details(principal, args):
    db, project = _owned_project(principal, args.project_id)
    try:
        return api_v1._project_view(db, project, detail=True)
    finally:
        db.close()


class ScriptArgs(BaseModel):
    model_config = {"extra": "forbid"}
    project_id: str = Field(min_length=1, max_length=64)
    offset: int = Field(default=0, ge=0)
    limit_chars: int = Field(default=6000, ge=100, le=20000)


def _project_script(principal, args):
    db, project = _owned_project(principal, args.project_id)
    try:
        script = project.script or ""
        window = script[args.offset : args.offset + args.limit_chars]
        return {
            "project_id": project.id,
            "title": project.title,
            "total_chars": len(script),
            "offset": args.offset,
            "script": window,
            "has_more": args.offset + len(window) < len(script),
        }
    finally:
        db.close()


def _project_research(principal, args):
    db, project = _owned_project(principal, args.project_id)
    try:
        project_id = project.id
    finally:
        db.close()
    bible = _read_artefact(project_id, "case_bible.json")
    plan = _read_artefact(project_id, "plan.json")
    if not _is_admin(principal):
        # FS-H (FS-6): research detail is an admin-only
        # presentational surface on HTTP; a non-admin Assistant
        # session gets only the simple recorded/not-recorded
        # status the user product legitimately shows.
        return {
            "project_id": project_id,
            "recorded": bible is not None or plan is not None,
            "note": (
                "Research is recorded for this project."
                if bible is not None or plan is not None
                else "No research artefacts are recorded for this "
                "project yet; research is produced by story "
                "analysis during a production run."
            ),
        }
    if bible is None and plan is None:
        return {
            "project_id": project_id,
            "recorded": False,
            "note": (
                "No research artefacts are recorded for this project "
                "yet; research is produced by story analysis during "
                "a production run."
            ),
        }
    summary: dict = {"project_id": project_id, "recorded": True}
    if isinstance(bible, dict):
        entities = bible.get("entities") or []
        summary["entities"] = [
            entity.get("name") if isinstance(entity, dict) else str(entity)
            for entity in entities[:40]
        ]
        events = bible.get("timeline_events") or bible.get("timeline") or []
        summary["timeline_events"] = events[:30]
        facts = bible.get("script_facts") or []
        summary["script_facts_count"] = len(facts)
        summary["script_facts"] = facts[:20]
    if isinstance(plan, dict) and isinstance(plan.get("planner"), dict):
        summary["planner"] = plan["planner"]
    return summary


def _project_plan(principal, args):
    db, project = _owned_project(principal, args.project_id)
    try:
        project_id = project.id
    finally:
        db.close()
    # FS-H (FS-5): the Visual Plan is admin-only on HTTP (reads
    # included); the Assistant must not become its side channel.
    _require_admin_tool(principal)
    plan = plan_document.build_plan(
        project_id, media_paths.project_dir(project_id)
    )
    if plan is None:
        raise ApiError(
            404,
            "plan_not_found",
            "No analysis exists for this project yet; a plan is "
            "produced by story analysis during production.",
        )
    rows = []
    for row in plan.get("sentences") or []:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "sentence_number": row.get("sentence_number"),
                "scene_number": row.get("scene_number"),
                "text": (row.get("text") or "")[:200],
                "proposed_visual": row.get("proposed_visual"),
                "chosen_visual": row.get("chosen_visual"),
                "decision": row.get("decision"),
            }
        )
    return {
        "project_id": project_id,
        "plan_source": plan.get("plan_source"),
        "planner": plan.get("planner"),
        "sentence_count": len(rows),
        "sentences": rows,
    }


class AssetsArgs(BaseModel):
    model_config = {"extra": "forbid"}
    project_id: str = Field(min_length=1, max_length=64)
    filter: str = Field(default="all", pattern="^(accepted|rejected|missing|all)$")
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


def _project_assets(principal, args):
    db, _project = _owned_project(principal, args.project_id)
    db.close()
    latest = _latest_production_run(args.project_id)
    if latest is None:
        return {
            "project_id": args.project_id,
            "run_id": None,
            "total": 0,
            "items": [],
            "note": "This project has no production runs yet.",
        }
    rows, total = run_ledger.list_decisions(latest["run_id"], 500, 0)
    if not _is_admin(principal):
        # FS-H (FS-7): mirror the HTTP field restriction exactly —
        # non-admins receive only the retained editor-reuse fields
        # (api_v1._editor_ledger_row), never provenance detail.
        rows = [api_v1._editor_ledger_row(row) for row in rows]
    if args.filter != "all":
        rows = [
            row
            for row in rows
            if str(row.get("decision_type") or "").lower() == args.filter
        ]
        total = len(rows)
    window = rows[args.offset : args.offset + args.limit]
    return {
        "project_id": args.project_id,
        "run_id": latest["run_id"],
        "filter": args.filter,
        "total": total,
        "items": window,
    }


def _project_timeline(principal, args):
    db, _project = _owned_project(principal, args.project_id)
    db.close()
    timeline = _read_artefact(args.project_id, "timeline.json")
    if not isinstance(timeline, dict):
        return {
            "project_id": args.project_id,
            "recorded": False,
            "note": "No timeline is recorded for this project yet.",
        }
    items = timeline.get("items") or timeline.get("scenes") or []
    return {
        "project_id": args.project_id,
        "recorded": True,
        "item_count": len(items),
        "total_duration_seconds": timeline.get("total_duration")
        or timeline.get("duration_seconds"),
        "timing_source": timeline.get("timing_source"),
        "coverage": timeline.get("coverage"),
        "items": [
            {
                "scene_number": item.get("scene_number"),
                "sentence_number": item.get("sentence_number"),
                "duration": item.get("duration"),
                "start": item.get("start"),
                "end": item.get("end"),
            }
            for item in items[:100]
            if isinstance(item, dict)
        ],
    }


def _project_output(principal, args):
    # Delegates to the HTTP handler, which now withholds the
    # admin-only certificate/plan URLs from non-admin principals
    # (FS-H) — the film/caption inventory stays a kept user flow.
    return api_v1.get_outputs(args.project_id, principal)


def _project_certificate(principal, args):
    return api_v1.get_certificate(args.project_id, principal)


def _project_recommendations(principal, args):
    """The deterministic recommendations reader (design 02 §H):
    findings are computed from the run's own asset_report figures —
    the model narrates and prioritises, it does not detect. The
    computation itself lives in services.project_intelligence
    (run_findings) so the Assistant and Project Intelligence share
    ONE implementation of the rules."""
    db, _project = _owned_project(principal, args.project_id)
    db.close()
    latest = _latest_production_run(args.project_id)
    if latest is None:
        return {
            "project_id": args.project_id,
            "recommendations": [],
            "note": "No production run exists yet, so there is no evidence to base recommendations on.",
        }
    report = _read_artefact(
        args.project_id, "asset_report.json", run_id=latest["run_id"]
    )
    if not isinstance(report, dict):
        return {
            "project_id": args.project_id,
            "run_id": latest["run_id"],
            "recommendations": [],
            "note": "The latest run recorded no asset report, so no evidence-based recommendations can be made.",
        }
    findings = project_intelligence.run_findings(
        args.project_id, latest["run_id"], report
    )
    if not _is_admin(principal):
        # FS-H: findings' `link` fields point at admin-only
        # surfaces (assets/plan/research). The findings themselves
        # are the user-legitimate summary; their gated deep links
        # must not surface through this non-gated payload, so a
        # non-admin gets the project Overview as the link target.
        findings = [
            {**finding, "link": f"/projects/{args.project_id}"}
            if isinstance(finding, dict) and finding.get("link")
            else finding
            for finding in findings
        ]
    return {
        "project_id": args.project_id,
        "run_id": latest["run_id"],
        "recommendations": findings,
        "note": (
            "Findings are computed from the run's recorded asset "
            "report figures; no figure here is estimated."
            if findings
            else "The latest run's asset report shows no coverage, "
            "repetition, verification or retrieval problems to act on."
        ),
    }


# ---------------------------------------------------------------------
# PRODUCTION family
# ---------------------------------------------------------------------


class JobStatusArgs(BaseModel):
    model_config = {"extra": "forbid"}
    run_id: str | None = Field(default=None, max_length=64)
    project_id: str | None = Field(default=None, max_length=64)


def _job_status(principal, args):
    if args.run_id:
        run = api_v1._get_owned_run_or_404(args.run_id, principal)
    elif args.project_id:
        db, _project = _owned_project(principal, args.project_id)
        db.close()
        run = run_ledger.latest_run_for(args.project_id)
        if run is None:
            return {
                "project_id": args.project_id,
                "run": None,
                "note": "This project has no runs yet.",
            }
    else:
        raise ApiError(
            422, "missing_target", "Provide run_id or project_id."
        )
    view = api_v1._run_view(run)
    view["presence"] = run_ledger.run_presence(run)
    return view


def _pipeline_status(principal, args):
    return api_v1.get_project_status(args.project_id, principal)


class ErrorsArgs(BaseModel):
    model_config = {"extra": "forbid"}
    project_id: str | None = Field(default=None, max_length=64)
    run_id: str | None = Field(default=None, max_length=64)


def _production_errors(principal, args):
    """The troubleshooting evidence pack (design 02 §F): every
    recorded failure signal for one run, assembled verbatim. What
    is NOT in the pack was not recorded — the narration must say
    so rather than speculate."""
    run = None
    project_id = args.project_id
    if args.run_id:
        run = api_v1._get_owned_run_or_404(args.run_id, principal)
        project_id = run["project_id"]
    elif project_id:
        db, _project = _owned_project(principal, project_id)
        db.close()
        run = run_ledger.latest_run_for(project_id)
    else:
        raise ApiError(
            422, "missing_target", "Provide run_id or project_id."
        )
    if run is None:
        return {
            "project_id": project_id,
            "recorded": False,
            "note": "This project has no runs, so no failure evidence exists.",
        }
    if not _is_admin(principal):
        # FS-H (FS-4): the verbatim evidence pack is failure
        # diagnostics — admin-only, exactly as on HTTP. A non-admin
        # Assistant session gets the truthful simple status: the
        # run's status/stage and the generic failure label, never
        # the recorded error text or per-stage errors.
        return {
            "project_id": project_id,
            "run_id": run["run_id"],
            "run_status": run["status"],
            "run_stage": run["stage"],
            "error": (
                api_v1.GENERIC_FAILURE_MESSAGE
                if run["status"] == "failed"
                else None
            ),
            "evidence_note": (
                "Detailed failure diagnostics are available to "
                "administrators; this session sees the run's "
                "status only."
            ),
        }
    pack: dict = {
        "project_id": project_id,
        "run_id": run["run_id"],
        "run_status": run["status"],
        "run_stage": run["stage"],
        "run_error": run["error"],
    }
    state = _read_artefact(project_id, "production_state.json", run_id=run["run_id"])
    if isinstance(state, dict):
        pack["state_error"] = state.get("error")
        pack["stage_errors"] = {
            name: record.get("error")
            for name, record in (state.get("stages") or {}).items()
            if isinstance(record, dict) and record.get("error")
        }
        resume = state.get("resume")
        if isinstance(resume, dict) and resume.get("fallback"):
            pack["resume_fallback"] = resume["fallback"]
    report = _read_artefact(project_id, "asset_report.json", run_id=run["run_id"])
    if isinstance(report, dict):
        pack["run_failure"] = report.get("run_failure")
        pack["discovery_failures"] = report.get("discovery_failures")
    plan = _read_artefact(project_id, "plan.json", run_id=run["run_id"])
    if isinstance(plan, dict) and isinstance(plan.get("planner"), dict):
        pack["planner"] = plan["planner"]
    production_report = _read_artefact(
        project_id, "production_report.json", run_id=run["run_id"]
    )
    if isinstance(production_report, dict):
        pack["production_report_verdicts"] = production_report.get("verdicts")
    pack["evidence_note"] = (
        "Fields absent from this pack were not recorded for this "
        "run; do not invent a cause beyond this evidence."
    )
    return pack


def _worker_status(principal, args):
    db = SessionLocal()
    try:
        rows = (
            db.query(ProductionRun)
            .filter(
                ProductionRun.owner_id == principal.user_id,
                ProductionRun.status.in_(
                    ("queued", "running", "awaiting_approval")
                ),
            )
            .order_by(ProductionRun.created_at.asc())
            .all()
        )
        active = []
        for row in rows:
            run = run_ledger.get_run(row.id)
            active.append(
                {
                    "run_id": row.id,
                    "project_id": row.project_id,
                    "status": row.status,
                    "stage": row.stage,
                    "presence": run_ledger.run_presence(run)
                    if run
                    else None,
                }
            )
    finally:
        db.close()
    queue_depths = None
    try:
        import os

        from redis import Redis
        from rq import Queue

        connection = Redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            socket_connect_timeout=2,
        )
        queue_depths = {
            name: len(Queue(name, connection=connection))
            for name in ("video_generation", "video_generation_priority")
        }
    except Exception:
        queue_depths = None
    return {
        "your_active_runs": active,
        "queue_depths": queue_depths,
        "queue_note": (
            None
            if queue_depths is not None
            else "Queue depth could not be read (Redis unreachable from the API process)."
        ),
    }


def _render_status(principal, args):
    db, _project = _owned_project(principal, args.project_id)
    db.close()
    try:
        revisions = editor.list_revisions(args.project_id)
    except editor.EditorError as error:
        raise ApiError(error.status_code, error.code, error.message) from error
    runs = run_ledger.list_runs_for(args.project_id)
    render_runs = [
        api_v1._run_view(run)
        for run in runs
        if (run.get("options") or {}).get("kind") == "revision_render"
        or run.get("stage") == "video_editor"
        and (run.get("options") or {}).get("revision_id")
    ]
    if not _is_admin(principal):
        # FS-H (FS-11): the revision chain is version-history
        # detail — an admin-only surface on HTTP. A non-admin
        # Assistant session gets the simple status (how many
        # revisions exist, and the render runs, whose views are
        # already failure-sanitized), not the chain itself.
        return {
            "project_id": args.project_id,
            "revision_count": len(revisions),
            "render_runs": render_runs[:10],
            "note": (
                "Revision history detail is available to "
                "administrators; this session sees the revision "
                "count and render status only."
            ),
        }
    return {
        "project_id": args.project_id,
        "revisions": revisions,
        "render_runs": render_runs[:10],
    }


# ---------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------

register(ToolSpec(
    name="get_account_profile", family="account", action_class=READ,
    label="Read account profile",
    summary="The signed-in user's account, plan, project/run counts and provider configuration status.",
    input_model=EmptyArgs, handler=_account_profile,
))
register(ToolSpec(
    name="get_account_usage", family="account", action_class=READ,
    label="Read account usage",
    summary="Run counts by status and recorded execution metrics over the last N days.",
    input_model=UsageArgs, handler=_account_usage,
))
register(ToolSpec(
    name="get_account_preferences", family="account", action_class=READ,
    label="Read assistant preferences",
    summary="The user's explicitly saved assistant preferences (empty until they approve some).",
    input_model=EmptyArgs, handler=_account_preferences,
))
register(ToolSpec(
    name="get_account_activity", family="account", action_class=READ,
    label="Read recent activity",
    summary="The user's most recent production runs across their projects.",
    input_model=ActivityArgs, handler=_account_activity,
))
register(ToolSpec(
    name="list_projects", family="projects", action_class=READ,
    label="List projects",
    summary="List the user's projects, newest first, with total count. Use this to answer 'my latest project' and 'how many projects'.",
    input_model=ListProjectsArgs, handler=_list_projects,
))
register(ToolSpec(
    name="search_projects", family="projects", action_class=READ,
    label="Search projects",
    summary="Find the user's projects whose title, category or script text contains the query.",
    input_model=SearchProjectsArgs, handler=_search_projects,
))
register(ToolSpec(
    name="get_project_details", family="projects", action_class=READ,
    label="Read project details",
    summary="One project's detail: title, category, status, options, full script, latest run, film URL when completed.",
    input_model=ProjectArg, handler=_project_details,
))
register(ToolSpec(
    name="get_project_script", family="projects", action_class=READ,
    label="Read project script",
    summary="A window of the project's stored script text (paginated by offset/limit_chars).",
    input_model=ScriptArgs, handler=_project_script,
    scan_source="project_script",
))
register(ToolSpec(
    name="get_project_research", family="projects", action_class=READ,
    label="Read project research",
    summary="The project's recorded research: Case Bible entities, timeline events, script facts, planner block.",
    input_model=ProjectArg, handler=_project_research,
))
register(ToolSpec(
    name="get_project_plan", family="projects", action_class=READ,
    label="Read project plan",
    summary="The sentence-level visual plan: proposed/chosen visuals and recorded decisions per sentence.",
    input_model=ProjectArg, handler=_project_plan,
))
register(ToolSpec(
    name="get_project_assets", family="projects", action_class=READ,
    label="Read asset decisions",
    summary="The latest production run's asset decision ledger (accepted/rejected/missing), with verification verdicts and rights.",
    input_model=AssetsArgs, handler=_project_assets,
))
register(ToolSpec(
    name="get_project_timeline", family="projects", action_class=READ,
    label="Read project timeline",
    summary="The recorded timeline: item count, durations, timing source, coverage.",
    input_model=ProjectArg, handler=_project_timeline,
))
register(ToolSpec(
    name="get_project_output", family="projects", action_class=READ,
    label="Read project outputs",
    summary="The project's output inventory: film, captions, reports and their media URLs, plus certificate/plan links.",
    input_model=ProjectArg, handler=_project_output,
))
register(ToolSpec(
    name="get_project_certificate", family="projects", action_class=READ,
    label="Read production certificate",
    summary="The production certificate for the latest run, including quality verdicts and the planner block.",
    input_model=ProjectArg, handler=_project_certificate,
))
register(ToolSpec(
    name="get_project_recommendations", family="projects", action_class=READ,
    label="Compute project recommendations",
    summary="Deterministic, evidence-based improvement findings computed from the latest run's asset report (coverage, missing visuals, repeated footage, unverified assets, retrieval failures).",
    input_model=ProjectArg, handler=_project_recommendations,
))
register(ToolSpec(
    name="get_job_status", family="production", action_class=READ,
    label="Read job status",
    summary="One run's status, stage, error and worker presence (heartbeat age, live) — by run_id, or the latest run of a project_id.",
    input_model=JobStatusArgs, handler=_job_status,
))
register(ToolSpec(
    name="get_pipeline_status", family="production", action_class=READ,
    label="Read pipeline status",
    summary="A project's per-stage production state, current stage and latest run.",
    input_model=ProjectArg, handler=_pipeline_status,
))
register(ToolSpec(
    name="get_production_errors", family="production", action_class=READ,
    label="Assemble failure evidence",
    summary="The troubleshooting evidence pack for a run: run/stage errors, recorded run failure, discovery failures, planner degraded reason, resume fallback. Absent fields were not recorded.",
    input_model=ErrorsArgs, handler=_production_errors,
))
register(ToolSpec(
    name="get_worker_status", family="production", action_class=READ,
    label="Read worker status",
    summary="The user's active runs with presence, plus platform queue depths when Redis is reachable.",
    input_model=EmptyArgs, handler=_worker_status,
))
register(ToolSpec(
    name="get_render_status", family="production", action_class=READ,
    label="Read render status",
    summary="A project's editor revision chain and recent revision render runs.",
    input_model=ProjectArg, handler=_render_status,
))
