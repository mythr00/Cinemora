"""WRITE / CONSEQUENTIAL tools (design assistant/06).

Handlers in this module are ONLY ever invoked by the confirm
executor (services/assistant/actions.py) after the user confirms
the pending action — the orchestrator proposes, it never calls
these. Each handler delegates to the exact route function the v1
API exposes, so a confirmed assistant operation produces records
indistinguishable from UI-created ones (same validation, same
dedupe, same plan enforcement).

`retry_eligible_stage` is named honestly (design 06): it retries
the *production*, resuming adopted stages — the backend has no
single-stage re-run primitive. Its eligibility verdict is computed
by `retry_eligibility` at proposal time (a read) and re-checked at
execution time.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

import api_v1
from api_v1 import ApiError
from database import SessionLocal
from models import UserPreference
from services import media_paths, run_ledger, stage_resume
from services.assistant.registry import (
    CONSEQUENTIAL,
    WRITE,
    ToolSpec,
    register,
)


# ---------------------------------------------------------------------
# retry_eligible_stage — eligibility (read part) + execution
# ---------------------------------------------------------------------


class RunArg(BaseModel):
    model_config = {"extra": "forbid"}
    run_id: str = Field(min_length=1, max_length=64)


def retry_eligibility(principal, args) -> dict:
    """Deterministic eligibility for retrying one run: terminal
    failed/cancelled, the project script unchanged since the run,
    and the stages a new run would adopt (stage_resume's own
    validator, probed exactly as a starting run probes)."""
    run = api_v1._get_owned_run_or_404(args.run_id, principal)
    project_id = run["project_id"]
    db = SessionLocal()
    try:
        project = api_v1._get_owned_project_or_404(db, project_id, principal)
        script = project.script or ""
    finally:
        db.close()
    if run["status"] not in ("failed", "cancelled"):
        return {
            "eligible": False,
            "run_id": args.run_id,
            "status": run["status"],
            "reason": (
                f"The run is {run['status']}; only a failed or "
                "cancelled run can be retried."
            ),
        }
    script_unchanged = run.get("script_hash") == run_ledger.script_sha256(script)
    if not script_unchanged:
        return {
            "eligible": False,
            "run_id": args.run_id,
            "status": run["status"],
            "reason": (
                "The project's script has changed since this run; "
                "a new submission would be a fresh production, not "
                "a retry with stage adoption."
            ),
        }
    plan = stage_resume.find_resume_source(
        media_paths.project_dir(project_id),
        "__eligibility_probe__",
        run["script_hash"],
        run.get("options"),
    )
    # find_resume_source only lists stages that passed artefact
    # validation (it stops the prefix at the first invalid stage),
    # so the plan's stage keys ARE the adoptable stages.
    adoptable = (
        sorted((plan.get("stages") or {}).keys())
        if plan.get("source_run_id")
        else []
    )
    return {
        "eligible": True,
        "run_id": args.run_id,
        "project_id": project_id,
        "status": run["status"],
        "script_unchanged": True,
        "resume_source_run_id": plan.get("source_run_id"),
        "adoptable_stages": adoptable,
        "note": (
            "A confirmed retry submits a new run for the project; "
            "the new run adopts the listed completed stages after "
            "artefact validation and runs the rest fresh."
        ),
    }


def _retry(principal, args):
    verdict = retry_eligibility(principal, args)
    if not verdict["eligible"]:
        raise ApiError(409, "retry_not_eligible", verdict["reason"])
    result = api_v1.submit_run(verdict["project_id"], None, principal)
    return {"retry_of_run_id": args.run_id, "eligibility": verdict, **result}


# ---------------------------------------------------------------------
# ACTIONS family
# ---------------------------------------------------------------------


class CreateProjectArgs(BaseModel):
    model_config = {"extra": "forbid"}
    title: str | None = Field(default=None, max_length=300)
    script: str = Field(min_length=1, max_length=api_v1.MAX_SCRIPT_CHARS)
    category: str = Field(default="Documentary", max_length=100)
    options: dict | None = None


def _create_project(principal, args):
    body = api_v1.ProjectCreateIn(
        title=args.title,
        script=args.script,
        category=args.category,
        options=args.options,
    )
    return api_v1.create_project(body, principal)


class SubmitRunArgs(BaseModel):
    model_config = {"extra": "forbid"}
    project_id: str = Field(min_length=1, max_length=64)
    options: dict | None = None


def _submit_run(principal, args):
    body = api_v1.RunSubmitIn(options=args.options)
    return api_v1.submit_run(args.project_id, body, principal)


class ProjectOnlyArg(BaseModel):
    model_config = {"extra": "forbid"}
    project_id: str = Field(min_length=1, max_length=64)


def _approve_plan(principal, args):
    return api_v1.approve_plan(args.project_id, principal)


class PlanDecisionArgs(BaseModel):
    model_config = {"extra": "forbid"}
    project_id: str = Field(min_length=1, max_length=64)
    sentence_number: int = Field(ge=1)
    action: str = Field(pattern="^(approve|replace|regenerate)$")
    replacement: dict | None = None


def _plan_decision(principal, args):
    body = api_v1.PlanDecisionIn(
        sentence_number=args.sentence_number,
        action=args.action,
        replacement=args.replacement,
    )
    return api_v1.post_plan_decision(args.project_id, body, principal)


class RenderArgs(BaseModel):
    model_config = {"extra": "forbid"}
    project_id: str = Field(min_length=1, max_length=64)
    revision_id: str = Field(min_length=1, max_length=64)
    allow_gaps: bool = False


def _request_render(principal, args):
    body = api_v1.RenderBody(allow_gaps=args.allow_gaps)
    return api_v1.render_revision(
        args.project_id, args.revision_id, body, principal
    )


def _cancel_run(principal, args):
    return api_v1.cancel_run(args.run_id, principal)


class PreferenceArgs(BaseModel):
    model_config = {"extra": "forbid"}
    key: str = Field(min_length=1, max_length=100)
    value: Any = None


def _save_preference(principal, args):
    """Store one explicitly user-stated preference in the Phase 8
    preferences document's assistant section (design 05 §1.5 as
    reconciled with Phase 8's single preferences store)."""
    db = SessionLocal()
    try:
        row = db.get(UserPreference, principal.user_id)
        document = dict((row.document if row else None) or {})
        section = dict(document.get("assistant") or {})
        section[args.key] = args.value
        document["assistant"] = section
        if row is None:
            row = UserPreference(
                user_id=principal.user_id, document=document
            )
            db.add(row)
        else:
            row.document = document
        db.commit()
        return {"saved": {args.key: args.value}, "preferences": section}
    finally:
        db.close()


# ---------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------

register(ToolSpec(
    name="retry_eligible_stage", family="production", action_class=WRITE,
    label="Retry production",
    summary="Retry a failed or cancelled run: eligibility (terminal state, unchanged script, adoptable stages) is computed first; on confirmation a new run is submitted and adopts completed stages.",
    input_model=RunArg, handler=_retry,
    describe=lambda a: f"Retry the production from run {a.run_id} (a new run, resuming completed stages)",
))
register(ToolSpec(
    name="create_project", family="actions", action_class=WRITE,
    label="Create project",
    summary="Create a new documentary project from a script (same validation as the New production page, including the script length cap and plan limits).",
    input_model=CreateProjectArgs, handler=_create_project,
    describe=lambda a: f"Create project '{a.title or 'Untitled'}' ({a.category})",
))
register(ToolSpec(
    name="submit_production_run", family="actions", action_class=WRITE,
    label="Submit production run",
    summary="Submit a production run for one of the user's projects (dedupe and plan-approval flow identical to the API).",
    input_model=SubmitRunArgs, handler=_submit_run,
    describe=lambda a: f"Submit a production run for project {a.project_id}",
))
register(ToolSpec(
    name="approve_plan", family="actions", action_class=WRITE,
    label="Approve plan",
    summary="Approve the plan of a run parked in awaiting_approval, releasing it into full production.",
    input_model=ProjectOnlyArg, handler=_approve_plan,
    describe=lambda a: f"Approve the plan for project {a.project_id} and release its run",
))
register(ToolSpec(
    name="post_plan_decision", family="actions", action_class=WRITE,
    label="Record plan decision",
    summary="Record a per-sentence plan decision (approve / replace / regenerate) on the user's project.",
    input_model=PlanDecisionArgs, handler=_plan_decision,
    describe=lambda a: f"Record a '{a.action}' decision for sentence {a.sentence_number} of project {a.project_id}",
))
register(ToolSpec(
    name="request_revision_render", family="actions", action_class=WRITE,
    label="Request revision render",
    summary="Request a render of an editor revision (202 semantics, identical to the editor's render button).",
    input_model=RenderArgs, handler=_request_render,
    describe=lambda a: f"Request a render of revision {a.revision_id} for project {a.project_id}",
))
register(ToolSpec(
    name="cancel_run", family="actions", action_class=CONSEQUENTIAL,
    label="Cancel production run",
    summary="Cancel a production run. Consequential: it destroys in-progress work, so it needs explicit confirmation.",
    input_model=RunArg, handler=_cancel_run,
    describe=lambda a: f"Cancel production run {a.run_id}",
))
register(ToolSpec(
    name="save_preference", family="account", action_class=WRITE,
    label="Save preference",
    summary="Save one explicitly user-stated assistant preference (e.g. default genre or voice). Never used for inferred preferences.",
    input_model=PreferenceArgs, handler=_save_preference,
    describe=lambda a: f"Save preference '{a.key}'",
))
