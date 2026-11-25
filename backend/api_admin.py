"""Administration API (Phase 7) — mounted at /api/v1/admin.

Every route is gated by the admin guard below: the caller must be
an authenticated principal on the Phase 7 role ladder (role
`admin` or `super_admin`, services/auth.Principal.is_admin). This
is a separate, backend-enforced scope — admin endpoints see across
tenants by design, so they are NOT sprinkled into the user API:
they live here, behind this guard, and the two account-admin
endpoints Phase 8 landed in api_v1 moved here with them (same
paths, same shapes).

Sensitive operations write to the append-only admin audit log
(services/admin_audit) in the same flow as their effect:
- run.cancel / run.submit_replacement (M2 writes),
- user.status_change (suspend / reactivate; super_admin),
- user.plan_assign (super_admin; Phase 8 founder operation),
- admin.sign_in (recorded by the login route in api_v1),
- admin.auth_denied (failed authorisation attempts, logged by the
  guard so probing is visible).

Reads aggregate the platform's real records only (services/
admin_stats, admin_storage, admin_health, admin_settings): no
figure is fabricated, and monetary cost stays None — no price
table exists (services/run_metrics design rule).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func

import api_v1
import jobs
from api_v1 import ApiError
from database import SessionLocal
from models import (
    ApiKey,
    AssistantAction,
    AssistantConversation,
    AssistantMessage,
    AssistantSecurityEvent,
    AssistantToolOverride,
    Plan,
    Project,
    User,
    UserSubscription,
    WebhookDelivery,
    WebhookEndpoint,
)
from services import (
    admin_audit,
    admin_health,
    admin_settings,
    admin_stats,
    admin_storage,
)
from services.assistant import registry as assistant_registry
from services import auth as auth_service
from services import plans as plans_service
from services import run_ledger

router = APIRouter(prefix="/admin", tags=["admin"])

# Role ladder for this router's writes. Reads: any admin principal.
# Production writes: any admin principal. Account-consequential
# writes (suspend/reactivate, plan assignment): super_admin — the
# founder role, matching the Phase 8 "founder operation" contract.
SUPER_ADMIN = "super_admin"


def admin_guard(
    request: Request,
    principal: auth_service.Principal = Depends(api_v1.require_user),
) -> auth_service.Principal:
    """require_user + the Phase 7 role ladder, with denied attempts
    recorded in the audit log (design admin/03 §3)."""
    if not principal.is_admin:
        admin_audit.record(
            principal,
            "admin.auth_denied",
            target_type="route",
            target_id=request.url.path,
            result="failure",
            error="authenticated principal is not an administrator",
        )
        raise ApiError(
            403, "forbidden", "This endpoint requires an administrator."
        )
    return principal


def super_admin_guard(
    principal: auth_service.Principal = Depends(admin_guard),
) -> auth_service.Principal:
    if principal.role != SUPER_ADMIN:
        raise ApiError(
            403,
            "forbidden",
            "This operation requires the super_admin (founder) role.",
        )
    return principal


def _write_guard(
    principal: auth_service.Principal = Depends(admin_guard),
) -> auth_service.Principal:
    api_v1.rate_limiter.check(principal.user_id)
    return principal


# ---------------------------------------------------------------------
# M1 — overview; M5 — composite health; M3 — workers & queues
# ---------------------------------------------------------------------


@router.get("/overview")
def overview(days: int = 30, principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        return admin_stats.overview(db, days=days)
    finally:
        db.close()


@router.get("/health")
def health(principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        return admin_health.composite_health(db)
    finally:
        db.close()


@router.get("/readiness")
def readiness_check(principal=Depends(admin_guard)):
    """B-15: admin-only readiness — real per-dependency probes
    (DB SELECT 1, Redis ping, worker heartbeat evidence, version)
    each with latency; overall degraded/not_ready when a check
    fails. Detail stays behind this guard (FS rules)."""
    from services import readiness as readiness_service

    db = SessionLocal()
    try:
        return readiness_service.readiness(db)
    finally:
        db.close()


@router.get("/workers")
def workers(principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        return admin_health.worker_overview(db)
    finally:
        db.close()


# ---------------------------------------------------------------------
# M2 — production monitoring (+ the two audited writes)
# ---------------------------------------------------------------------


@router.get("/runs")
def runs(
    status: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    principal=Depends(admin_guard),
):
    db = SessionLocal()
    try:
        return admin_stats.list_runs(
            db, status=status, project_id=project_id,
            limit=limit, offset=offset,
        )
    finally:
        db.close()


@router.get("/runs/{run_id}")
def run_detail(run_id: str, principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        detail = admin_stats.run_detail(db, run_id)
    finally:
        db.close()
    if detail is None:
        raise ApiError(404, "run_not_found", f"No run {run_id}.")
    return detail


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, principal=Depends(_write_guard)):
    run = run_ledger.get_run(run_id)
    if run is None:
        raise ApiError(404, "run_not_found", f"No run {run_id}.")
    accepted = run_ledger.request_cancel(run_id)
    admin_audit.record(
        principal,
        "run.cancel",
        target_type="run",
        target_id=run_id,
        detail={
            "project_id": run["project_id"],
            "previous_status": run["status"],
            "cancel_requested": accepted,
        },
        result="success" if accepted else "failure",
        error=None if accepted else "run was not in a cancellable state",
    )
    return {
        "run_id": run_id,
        "cancel_requested": accepted,
        "run": _run_summary(run_id),
    }


def _run_summary(run_id: str):
    db = SessionLocal()
    try:
        detail = admin_stats.run_detail(db, run_id)
        if detail is None:
            return None
        return {
            key: detail[key]
            for key in ("run_id", "project_id", "status", "stage",
                        "cancel_requested")
        }
    finally:
        db.close()


@router.post("/runs/{run_id}/replacement")
def submit_replacement(run_id: str, principal=Depends(_write_guard)):
    """Submit a NEW run for a terminal run's project (the studio
    submission path, minus tenant checks — the run's owner is
    stamped from the project by the ledger choke point). Stage
    resume adopts completed stages when the script is unchanged.
    This never resurrects the old run."""
    run = run_ledger.get_run(run_id)
    if run is None:
        raise ApiError(404, "run_not_found", f"No run {run_id}.")
    if run["status"] in run_ledger.ACTIVE_STATUSES + ("awaiting_approval",):
        raise ApiError(
            409,
            "run_still_active",
            "The run is still active; a replacement is only for "
            "terminal runs.",
        )
    db = SessionLocal()
    try:
        project = db.get(Project, run["project_id"])
    finally:
        db.close()
    if project is None:
        raise ApiError(
            404, "project_not_found", f"No project {run['project_id']}."
        )
    options = dict(run.get("options") or {})
    options.pop("dispatched_job_id", None)
    options.pop("dispatched_at", None)
    job_or_run = jobs.enqueue_full_pipeline(
        project.id, project.script, options=options or None
    )
    new_run = run_ledger.latest_run_for(
        project.id, run_ledger.script_sha256(project.script)
    )
    admin_audit.record(
        principal,
        "run.submit_replacement",
        target_type="run",
        target_id=new_run["run_id"] if new_run else None,
        detail={
            "replaces_run_id": run_id,
            "project_id": project.id,
            "deduplicated": bool(new_run and new_run["run_id"] == job_or_run),
        },
    )
    return {
        "run": _run_summary(new_run["run_id"]) if new_run else None,
        "replaces_run_id": run_id,
    }


# ---------------------------------------------------------------------
# B-11 — failed / dead job management (services/admin_jobs)
# ---------------------------------------------------------------------


@router.get("/failed-jobs")
def failed_jobs(
    limit: int = 50,
    offset: int = 0,
    principal=Depends(admin_guard),
):
    from services import admin_jobs

    db = SessionLocal()
    try:
        return admin_jobs.list_failed_jobs(db, limit=limit, offset=offset)
    finally:
        db.close()


@router.get("/failed-jobs/{job_id}")
def failed_job_detail(job_id: str, principal=Depends(admin_guard)):
    from services import admin_jobs

    db = SessionLocal()
    try:
        entry = admin_jobs.get_failed_job(db, job_id)
    finally:
        db.close()
    if entry is None:
        raise ApiError(
            404, "failed_job_not_found", f"No failed job {job_id}."
        )
    return entry


@router.post("/failed-jobs/{job_id}/requeue")
def requeue_failed_job(job_id: str, principal=Depends(_write_guard)):
    """Requeue a FAILED production run through the existing
    dispatch path (jobs.enqueue_full_pipeline -> ledger +
    dispatcher admission). Idempotent for active runs. Audited."""
    from services import admin_jobs

    run_id = job_id
    run = run_ledger.get_run(run_id)
    if run is None:
        # The identifier may be an RQ job id whose kwargs carry
        # the run id.
        db = SessionLocal()
        try:
            entry = admin_jobs.get_failed_job(db, job_id)
        finally:
            db.close()
        if entry is not None and entry.get("run_id"):
            run_id = entry["run_id"]
            run = run_ledger.get_run(run_id)
    if run is None:
        raise ApiError(404, "run_not_found", f"No run {job_id}.")
    try:
        result = admin_jobs.requeue_failed_run(run_id)
    except LookupError as exc:
        raise ApiError(404, "run_not_found", str(exc)) from exc
    except ValueError as exc:
        admin_audit.record(
            principal,
            "job.requeue",
            target_type="run",
            target_id=run_id,
            result="failure",
            error=str(exc),
        )
        raise ApiError(409, "run_not_requeueable", str(exc)) from exc
    admin_audit.record(
        principal,
        "job.requeue",
        target_type="run",
        target_id=run_id,
        detail={
            "project_id": run["project_id"],
            "previous_status": run["status"],
            "requeued": result["requeued"],
            "already_active": result["already_active"],
            "new_run_id": result.get("new_run_id"),
        },
        result="success",
    )
    return result


@router.delete("/failed-jobs/{job_id}")
def purge_failed_job(job_id: str, principal=Depends(_write_guard)):
    """Purge a dead job's QUEUE record only (RQ failed registry).
    Project data and the ledger run row are never touched.
    Audited."""
    from services import admin_jobs

    db = SessionLocal()
    try:
        result = admin_jobs.purge_dead_job(job_id, db=db)
    finally:
        db.close()
    if result is None:
        admin_audit.record(
            principal,
            "job.purge",
            target_type="job",
            target_id=job_id,
            result="failure",
            error="no queue record found for this job",
        )
        raise ApiError(
            404,
            "queue_record_not_found",
            f"No dead queue record for {job_id}.",
        )
    admin_audit.record(
        principal,
        "job.purge",
        target_type="job",
        target_id=result["job_id"],
        detail={"queue": result["queue"], "requested_id": job_id},
        result="success",
    )
    return result


# ---------------------------------------------------------------------
# M4 — provider usage; M6 — storage; M8 — settings; M7 — audit log
# ---------------------------------------------------------------------


@router.get("/providers/usage")
def providers_usage(days: int = 30, principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        return admin_stats.provider_usage(db, days=days)
    finally:
        db.close()


@router.get("/storage")
def storage(principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        return admin_storage.summary(db)
    finally:
        db.close()


@router.get("/storage/orphans")
def storage_orphans(
    old_terminal_days: int = 30, principal=Depends(admin_guard)
):
    db = SessionLocal()
    try:
        return admin_storage.orphans(db, old_terminal_days=old_terminal_days)
    finally:
        db.close()


@router.get("/settings")
def settings(principal=Depends(admin_guard)):
    return admin_settings.effective_settings()


@router.get("/audit-log")
def audit_log(
    actor: str | None = None,
    action: str | None = None,
    target_id: str | None = None,
    result: str | None = None,
    limit: int = 50,
    offset: int = 0,
    principal=Depends(admin_guard),
):
    db = SessionLocal()
    try:
        return admin_audit.query(
            db, actor=actor, action=action, target_id=target_id,
            result=result, limit=limit, offset=offset,
        )
    finally:
        db.close()


# ---------------------------------------------------------------------
# Developer API (Phase N, founder #16): platform-wide key + webhook
# visibility. Reads only — key management stays with the owning
# account. Views carry prefixes and recorded usage, never secrets
# or hashes. Issuance/revocation events themselves are in the
# audit log above (api_key.created / api_key.revoked /
# webhook.created / webhook.disabled).
# ---------------------------------------------------------------------


@router.get("/developer")
def developer_overview(principal=Depends(admin_guard)):
    from services import api_keys as api_keys_service

    db = SessionLocal()
    try:
        keys = db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
        key_views = []
        for row in keys:
            owner = db.get(User, row.user_id)
            view = api_keys_service.key_view(row)
            view["owner_email"] = owner.email if owner else None
            key_views.append(view)
        endpoints = (
            db.query(WebhookEndpoint)
            .order_by(WebhookEndpoint.created_at.desc())
            .all()
        )
        endpoint_views = []
        for row in endpoints:
            owner = db.get(User, row.user_id)
            deliveries = (
                db.query(WebhookDelivery)
                .filter(WebhookDelivery.endpoint_id == row.id)
                .count()
            )
            endpoint_views.append(
                {
                    "id": row.id,
                    "owner_email": owner.email if owner else None,
                    "url": row.url,
                    "events": list(row.events or []),
                    "created_at": row.created_at,
                    "disabled_at": row.disabled_at,
                    "status": (
                        "disabled" if row.disabled_at is not None else "active"
                    ),
                    "delivery_count": deliveries,
                }
            )
        return {
            "api_keys": key_views,
            "webhooks": endpoint_views,
            "totals": {
                "api_keys": len(key_views),
                "active_api_keys": sum(
                    1 for view in key_views if view["status"] == "active"
                ),
                "api_requests_recorded": sum(
                    view["request_count"] for view in key_views
                ),
                "webhooks": len(endpoint_views),
                "webhook_deliveries": sum(
                    view["delivery_count"] for view in endpoint_views
                ),
            },
        }
    finally:
        db.close()


# ---------------------------------------------------------------------
# Users & accounts (Phase 8 endpoints, moved here; suspend /
# reactivate is the Phase 7 addition — super_admin, audited)
# ---------------------------------------------------------------------


def _admin_user_view(db, user: User) -> dict:
    subscription = plans_service.get_active_subscription(db, user.id)
    plan_name = None
    if subscription is not None:
        plan = db.get(Plan, subscription.plan_id)
        plan_name = plan.name if plan else None
    from models import ProductionRun

    return {
        **api_v1._user_view(user),
        "plan": plan_name,
        "project_count": db.query(Project)
        .filter(Project.owner_id == user.id)
        .count(),
        "run_count": db.query(ProductionRun)
        .filter(ProductionRun.owner_id == user.id)
        .count(),
    }


@router.get("/users")
def list_users(
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
    principal=Depends(admin_guard),
):
    db = SessionLocal()
    try:
        query = db.query(User).order_by(User.created_at)
        if q:
            query = query.filter(User.email.ilike(f"%{q.strip()}%"))
        total = query.count()
        users = (
            query.offset(max(0, offset)).limit(max(1, min(limit, 200))).all()
        )
        return {
            "total": total,
            "users": [_admin_user_view(db, user) for user in users],
        }
    finally:
        db.close()


@router.get("/users/{user_id}")
def get_user(user_id: str, principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        user = db.get(User, str(user_id))
        if user is None:
            raise ApiError(404, "user_not_found", f"No user {user_id}.")
        view = _admin_user_view(db, user)
        view["usage"] = plans_service.usage_summary(db, user.id)
        return view
    finally:
        db.close()


class UserStatusIn(BaseModel):
    status: str = Field(pattern="^(active|disabled)$")


@router.post("/users/{user_id}/status")
def set_user_status(
    user_id: str,
    body: UserStatusIn,
    principal=Depends(super_admin_guard),
):
    """Suspend (disabled) or reactivate an account. Disabling
    revokes every session immediately (services/auth); the data is
    retained. The founder cannot disable their own account through
    this surface — a self-lockout guard."""
    db = SessionLocal()
    try:
        user = db.get(User, str(user_id))
        if user is None:
            raise ApiError(404, "user_not_found", f"No user {user_id}.")
        if user.id == principal.user_id and body.status == "disabled":
            raise ApiError(
                409,
                "cannot_disable_self",
                "You cannot disable your own account.",
            )
        previous = user.status
        user.status = body.status
        db.commit()
        if body.status == "disabled":
            auth_service.revoke_all_sessions(user.id)
        admin_audit.record(
            principal,
            "user.status_change",
            target_type="user",
            target_id=user.id,
            detail={"from": previous, "to": body.status},
        )
        return {"user": _admin_user_view(db, user)}
    finally:
        db.close()


class PlanAssignIn(BaseModel):
    plan_name: str = Field(min_length=1, max_length=50)


@router.post("/users/{user_id}/plan")
def assign_plan(
    user_id: str,
    body: PlanAssignIn,
    principal=Depends(super_admin_guard),
):
    """Founder operation (Phase 8): move a user onto another plan.
    The previous active subscription is cancelled, a new one starts
    now. No payment is collected — plans are operator-granted
    records."""
    from uuid import uuid4

    db = SessionLocal()
    try:
        user = db.get(User, str(user_id))
        if user is None:
            raise ApiError(404, "user_not_found", f"No user {user_id}.")
        plan = (
            db.query(Plan).filter(Plan.name == body.plan_name).one_or_none()
        )
        if plan is None:
            raise ApiError(
                404, "plan_not_found", f"No plan named {body.plan_name}."
            )
        current = plans_service.get_active_subscription(db, user.id)
        previous_name = None
        if current is not None:
            previous = db.get(Plan, current.plan_id)
            previous_name = previous.name if previous else None
            current.status = "cancelled"
        subscription = UserSubscription(
            id=str(uuid4()),
            user_id=user.id,
            plan_id=plan.id,
            status="active",
            current_period_start=datetime.now(timezone.utc),
        )
        db.add(subscription)
        db.commit()
        admin_audit.record(
            principal,
            "user.plan_assign",
            target_type="user",
            target_id=user.id,
            detail={"from_plan": previous_name, "to_plan": plan.name},
        )
        return {"user": api_v1._user_view(user), "plan": plan.name}
    finally:
        db.close()


# ---------------------------------------------------------------------
# Subscriptions & plans (records only — no payment collection)
# ---------------------------------------------------------------------


@router.get("/subscriptions")
def subscriptions(principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        plans = db.query(Plan).order_by(Plan.name).all()
        subs = (
            db.query(UserSubscription)
            .order_by(UserSubscription.created_at.desc())
            .all()
        )
        users = {u.id: u.email for u in db.query(User).all()}
        plan_names = {p.id: p.name for p in plans}
        active_counts: dict[str, int] = {}
        for sub in subs:
            if sub.status == "active":
                active_counts[sub.plan_id] = (
                    active_counts.get(sub.plan_id, 0) + 1
                )
        return {
            "plans": [
                {
                    "id": plan.id,
                    "name": plan.name,
                    "max_concurrent_runs": plan.max_concurrent_runs,
                    "max_runs_per_period": plan.max_runs_per_period,
                    "period": plan.period,
                    "max_script_chars": plan.max_script_chars,
                    "max_storage_bytes": plan.max_storage_bytes,
                    "max_upload_bytes": plan.max_upload_bytes,
                    "priority_tier": plan.priority_tier,
                    "is_default": plan.is_default,
                    "active_subscribers": active_counts.get(plan.id, 0),
                }
                for plan in plans
            ],
            "subscriptions": [
                {
                    "id": sub.id,
                    "user_id": sub.user_id,
                    "user_email": users.get(sub.user_id),
                    "plan": plan_names.get(sub.plan_id),
                    "status": sub.status,
                    "current_period_start": sub.current_period_start,
                    "current_period_end": sub.current_period_end,
                    "billing_provider": sub.billing_provider,
                    "created_at": sub.created_at,
                }
                for sub in subs
            ],
            "billing_note": (
                "Plans and subscriptions are operator-granted "
                "records. No payment provider is connected and no "
                "payment is collected by the platform."
            ),
        }
    finally:
        db.close()


# ---------------------------------------------------------------------
# Projects (cross-tenant metadata view; content stays in the studio)
# ---------------------------------------------------------------------


@router.get("/projects")
def projects(
    limit: int = 100,
    offset: int = 0,
    principal=Depends(admin_guard),
):
    db = SessionLocal()
    try:
        total = db.query(Project).count()
        rows = (
            db.query(Project)
            .order_by(Project.created_at.desc())
            .offset(max(0, offset))
            .limit(max(1, min(limit, 200)))
            .all()
        )
        owners = {u.id: u.email for u in db.query(User).all()}
        out = []
        for project in rows:
            latest = run_ledger.latest_run_for(project.id)
            out.append(
                {
                    "id": project.id,
                    "title": project.title,
                    "category": project.category,
                    "status": project.status,
                    "owner_id": project.owner_id,
                    "owner_email": owners.get(project.owner_id),
                    "created_at": project.created_at,
                    "latest_run": (
                        {
                            "run_id": latest["run_id"],
                            "status": latest["status"],
                            "stage": latest["stage"],
                        }
                        if latest
                        else None
                    ),
                    "studio_path": f"/projects/{project.id}",
                }
            )
        return {"total": total, "projects": out}
    finally:
        db.close()


# ---------------------------------------------------------------------
# M9 — AI Assistant administration (Phase 6 hooks; admin design M9)
# ---------------------------------------------------------------------


class ToolToggleBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


def _set_tool_override(
    principal, name: str, enabled: bool, reason: str | None
) -> dict:
    if assistant_registry.get(name) is None:
        raise ApiError(
            404, "tool_not_found", f"No assistant tool named {name}."
        )
    api_v1.rate_limiter.check(principal.user_id)
    db = SessionLocal()
    try:
        # One row per scope (the tool name, or "*" globally); the
        # reason lives in the audit record, not on the row.
        row = db.get(AssistantToolOverride, name)
        if row is None:
            row = AssistantToolOverride(scope=name)
            db.add(row)
        row.disabled = not enabled
        row.updated_by = principal.user_id
        db.commit()
    finally:
        db.close()
    admin_audit.record(
        principal,
        "assistant.tool_enable" if enabled else "assistant.tool_disable",
        target_type="assistant_tool",
        target_id=name,
        detail={"reason": reason},
    )
    states = {
        state["name"]: state
        for state in assistant_registry.tool_states()
    }
    return {"tool": states[name]}


@router.get("/assistant/tools")
def assistant_tools(principal=Depends(admin_guard)):
    return {
        "assistant_enabled": assistant_registry.assistant_enabled(),
        "tools": assistant_registry.tool_states(),
    }


@router.post("/assistant/tools/{name}/disable")
def assistant_tool_disable(
    name: str,
    body: ToolToggleBody | None = None,
    principal=Depends(super_admin_guard),
):
    return _set_tool_override(
        principal, name, False, body.reason if body else None
    )


@router.post("/assistant/tools/{name}/enable")
def assistant_tool_enable(
    name: str,
    body: ToolToggleBody | None = None,
    principal=Depends(super_admin_guard),
):
    return _set_tool_override(
        principal, name, True, body.reason if body else None
    )


@router.get("/assistant/actions")
def assistant_actions(
    status: str | None = None,
    limit: int = 100,
    principal=Depends(admin_guard),
):
    """The assistant execution log (design admin M9): every tool
    execution and every proposed/confirmed action, platform-wide."""
    db = SessionLocal()
    try:
        query = db.query(AssistantAction)
        if status:
            query = query.filter(AssistantAction.status == status)
        rows = (
            query.order_by(AssistantAction.created_at.desc())
            .limit(min(max(limit, 1), 500))
            .all()
        )
        owners = {u.id: u.email for u in db.query(User).all()}
        return {
            "actions": [
                {
                    "id": row.id,
                    "owner_id": row.owner_id,
                    "owner_email": owners.get(row.owner_id),
                    "conversation_id": row.conversation_id,
                    "tool_name": row.tool_name,
                    "action_class": row.action_class,
                    "arguments": row.arguments,
                    "status": row.status,
                    # Failures are recorded inside result
                    # ({"error": ..., "code": ...}) by the executor.
                    "result": row.result,
                    "created_at": row.created_at,
                    "decided_at": row.decided_at,
                    "executed_at": row.executed_at,
                }
                for row in rows
            ]
        }
    finally:
        db.close()


@router.get("/assistant/conversations")
def assistant_conversations(
    limit: int = 100, principal=Depends(admin_guard)
):
    db = SessionLocal()
    try:
        rows = (
            db.query(AssistantConversation)
            .order_by(AssistantConversation.updated_at.desc())
            .limit(min(max(limit, 1), 500))
            .all()
        )
        owners = {u.id: u.email for u in db.query(User).all()}
        message_counts = {
            row[0]: row[1]
            for row in db.query(
                AssistantMessage.conversation_id,
                func.count(AssistantMessage.id),
            )
            .group_by(AssistantMessage.conversation_id)
            .all()
        }
        return {
            "conversations": [
                {
                    "id": row.id,
                    "owner_id": row.owner_id,
                    "owner_email": owners.get(row.owner_id),
                    "title": row.title,
                    "project_id": row.project_id,
                    "message_count": message_counts.get(row.id, 0),
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                }
                for row in rows
            ]
        }
    finally:
        db.close()


@router.get("/assistant/security-events")
def assistant_security_events(
    limit: int = 100, principal=Depends(admin_guard)
):
    """The prompt-injection / abuse feed (design admin M9)."""
    db = SessionLocal()
    try:
        rows = (
            db.query(AssistantSecurityEvent)
            .order_by(AssistantSecurityEvent.created_at.desc())
            .limit(min(max(limit, 1), 500))
            .all()
        )
        owners = {u.id: u.email for u in db.query(User).all()}
        return {
            "events": [
                {
                    "id": row.id,
                    "owner_id": row.owner_id,
                    "owner_email": owners.get(row.owner_id),
                    "kind": row.kind,
                    "source": row.source,
                    "detail": row.detail,
                    "created_at": row.created_at,
                }
                for row in rows
            ]
        }
    finally:
        db.close()
