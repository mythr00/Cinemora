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

import os

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


@router.get("/usage")
def usage(days: int = 30, principal=Depends(admin_guard)):
    """B-10 (Phase 5c-1): provider usage visibility — LLM token
    totals from the append-only provider_usage_events ledger,
    grouped by provider + model + day. Cost is null with
    pricing_status "not_configured": no price table exists, and
    no figure is invented (the Phase 8 cost engine will price
    these events later). The search section exposes the search
    provider's in-process counters, labelled process-local —
    they are not a platform-wide ledger."""
    from services import provider_usage

    db = SessionLocal()
    try:
        return provider_usage.aggregate_usage(db, days=days)
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
    # Phase 8 billing: an operator replacement is a zero-charge
    # submission (recorded with source=admin, never billed to the
    # owner) — the admin surface must not become a billing bypass
    # for user submissions, and must not bill users for operator
    # recovery actions either.
    options["billing_source"] = "admin"
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


@router.post("/users/{user_id}/reset-link")
def generate_reset_link(
    user_id: str,
    principal=Depends(_write_guard),
):
    """Operator-mediated password reset (Phase 6, A-16).

    Email delivery is founder-gated (D-09), so a locked-out user is
    helped out of band instead: an admin generates a one-time reset
    link here and delivers it to the user through a channel they
    control. The link is built from the EXISTING reset machinery
    (services/auth.issue_reset_token — single-use, SHA-256 stored,
    1 h expiry; the confirm flow at /auth/password-reset/confirm
    consumes it) and is returned ONCE, in this response only.

    The token and the complete link must NEVER be logged (B-08):
    nothing here writes them to a log, and the audit entry records
    only the action, the target and the expiry — admin_audit's
    secret-key denylist is a second line of defence, not the first.
    Generating a new link supersedes any outstanding one (the
    stored hash is replaced), exactly like a fresh reset request.
    """
    db = SessionLocal()
    try:
        user = db.get(User, str(user_id))
        if user is None:
            raise ApiError(404, "user_not_found", f"No user {user_id}.")
        token = auth_service.issue_reset_token(db, user)
        base = os.environ.get("CINEMORA_PUBLIC_BASE_URL", "")
        link = f"{base}/reset-password?token={token}"
        expires_at = user.password_reset_expires_at
        admin_audit.record(
            principal,
            "user.password_reset_link_generated",
            target_type="user",
            target_id=user.id,
            detail={
                "delivery": "operator_out_of_band",
                "expires_at": str(expires_at) if expires_at else None,
            },
        )
        return {"reset_link": link, "expires_at": expires_at}
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


# ---------------------------------------------------------------------
# Phase 8 — Monetization administration (directive §12)
#
# Plans, top-up packages, coupons, customer credit management,
# subscriptions, payments, webhook events, financial reports,
# pricing rules and the §21 retention registry. Reads: any admin.
# Commercial mutations (prices, packages, coupons, credit
# adjustments, pricing rules): super_admin, matching this
# router's account-consequential tier — and every mutation is
# written to the admin audit log in the same flow.
# ---------------------------------------------------------------------

import uuid as _uuid

import models as _models
from services.billing import account_view as _account_view
from services.billing import costs as _billing_costs
from services.billing import credits as _billing_credits
from services.billing import retention as _billing_retention


class BillingPlanIn(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, max_length=50)
    price_monthly_cents: int | None = Field(default=None, ge=0)
    price_annual_cents: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=3)
    monthly_credits: int | None = Field(default=None, ge=0)
    included_generations: int | None = Field(default=None, ge=0)
    script_allowance: int | None = Field(default=None, ge=0)
    thumbnail_allowance: int | None = Field(default=None, ge=0)
    billing_mode: str | None = Field(
        default=None, pattern="^(legacy|metered)$"
    )
    is_public: bool | None = None
    max_concurrent_runs: int | None = Field(default=None, ge=1)
    max_runs_per_period: int | None = Field(default=None, ge=0)
    max_script_chars: int | None = Field(default=None, ge=0)
    priority_tier: int | None = Field(default=None, ge=0, le=1)
    features: dict | None = None


class TopupPackageIn(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, max_length=80)
    credits: int | None = Field(default=None, ge=1)
    price_cents: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=3)
    validity_months: int | None = Field(default=None, ge=1, le=120)
    is_active: bool | None = None
    sort_order: int | None = None
    provider_price_id: str | None = Field(default=None, max_length=200)


class CouponIn(BaseModel):
    model_config = {"extra": "forbid"}

    code: str | None = Field(default=None, max_length=64)
    kind: str | None = Field(
        default=None, pattern="^(percent|fixed|credits)$"
    )
    percent_off: int | None = Field(default=None, ge=1, le=100)
    amount_off_cents: int | None = Field(default=None, ge=0)
    credits_grant: int | None = Field(default=None, ge=1)
    promo_expires_at: datetime | None = None
    max_redemptions: int | None = Field(default=None, ge=1)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool | None = None


class CreditAdjustIn(BaseModel):
    model_config = {"extra": "forbid"}

    bucket: str = Field(pattern="^(subscription|purchased|promo)$")
    delta: int = Field(ge=-1_000_000, le=1_000_000)
    reason: str = Field(min_length=3, max_length=500)


class PromoGrantIn(BaseModel):
    model_config = {"extra": "forbid"}

    coupon_id: str = Field(min_length=1, max_length=64)


class PricingRuleIn(BaseModel):
    model_config = {"extra": "forbid"}

    provider: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    unit: str = Field(
        pattern="^(per_1k_tokens|per_call|per_search|per_minute|per_gb_month)$"
    )
    rate_microusd: int = Field(ge=0)
    effective_from: datetime
    effective_to: datetime | None = None


def _apply_fields(row, body: BaseModel, *, skip_none=True):
    changed = {}
    for field, value in body.model_dump().items():
        if skip_none and value is None:
            continue
        if getattr(row, field) != value:
            changed[field] = value
            setattr(row, field, value)
    return changed


@router.get("/billing/plans")
def billing_plans(principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        plans = db.query(Plan).order_by(Plan.name.asc()).all()
        return {"plans": [_account_view.plan_view(p) for p in plans]}
    finally:
        db.close()


@router.post("/billing/plans", status_code=201)
def billing_plan_create(
    body: BillingPlanIn, principal=Depends(super_admin_guard)
):
    if not body.name:
        raise ApiError(422, "validation_error", "name is required.")
    db = SessionLocal()
    try:
        if db.query(Plan).filter_by(name=body.name).one_or_none():
            raise ApiError(409, "plan_exists", "A plan with that name exists.")
        plan = Plan(id=str(_uuid.uuid4()), name=body.name)
        _apply_fields(plan, body)
        db.add(plan)
        db.commit()
        admin_audit.record(
            principal, "billing.plan_create",
            target_type="plan", target_id=plan.id,
            detail={"name": plan.name},
        )
        return _account_view.plan_view(plan)
    finally:
        db.close()


@router.post("/billing/plans/{plan_id}")
def billing_plan_update(
    plan_id: str, body: BillingPlanIn,
    principal=Depends(super_admin_guard),
):
    db = SessionLocal()
    try:
        plan = db.get(Plan, plan_id)
        if plan is None:
            raise ApiError(404, "plan_not_found", "No such plan.")
        changed = _apply_fields(plan, body)
        db.commit()
        admin_audit.record(
            principal, "billing.plan_update",
            target_type="plan", target_id=plan.id,
            detail={"changed": changed},
        )
        return _account_view.plan_view(plan)
    finally:
        db.close()


@router.get("/billing/topup-packages")
def billing_topup_packages(principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        rows = (
            db.query(_models.TopupPackage)
            .order_by(_models.TopupPackage.sort_order.asc())
            .all()
        )
        return {
            "packages": [
                {
                    "id": p.id, "name": p.name, "credits": p.credits,
                    "price_cents": p.price_cents,
                    "currency": p.currency,
                    "validity_months": p.validity_months,
                    "is_active": p.is_active,
                    "sort_order": p.sort_order,
                    "provider_price_id": p.provider_price_id,
                }
                for p in rows
            ]
        }
    finally:
        db.close()


@router.post("/billing/topup-packages", status_code=201)
def billing_topup_package_create(
    body: TopupPackageIn, principal=Depends(super_admin_guard)
):
    if not body.name or body.credits is None or body.price_cents is None:
        raise ApiError(
            422, "validation_error",
            "name, credits and price_cents are required.",
        )
    db = SessionLocal()
    try:
        package = _models.TopupPackage(
            id=str(_uuid.uuid4()), name=body.name,
            credits=body.credits, price_cents=body.price_cents,
        )
        _apply_fields(package, body)
        db.add(package)
        db.commit()
        admin_audit.record(
            principal, "billing.topup_create",
            target_type="topup_package", target_id=package.id,
            detail={"name": package.name},
        )
        return {"id": package.id, "name": package.name}
    finally:
        db.close()


@router.post("/billing/topup-packages/{package_id}")
def billing_topup_package_update(
    package_id: str, body: TopupPackageIn,
    principal=Depends(super_admin_guard),
):
    db = SessionLocal()
    try:
        package = db.get(_models.TopupPackage, package_id)
        if package is None:
            raise ApiError(404, "package_not_found", "No such package.")
        changed = _apply_fields(package, body)
        db.commit()
        admin_audit.record(
            principal, "billing.topup_update",
            target_type="topup_package", target_id=package.id,
            detail={"changed": changed},
        )
        return {"id": package.id, "changed": changed}
    finally:
        db.close()


@router.get("/billing/coupons")
def billing_coupons(principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        rows = db.query(_models.Coupon).order_by(
            _models.Coupon.created_at.desc()
        ).all()
        return {
            "coupons": [
                {
                    "id": c.id, "code": c.code, "kind": c.kind,
                    "percent_off": c.percent_off,
                    "amount_off_cents": c.amount_off_cents,
                    "credits_grant": c.credits_grant,
                    "promo_expires_at": c.promo_expires_at,
                    "max_redemptions": c.max_redemptions,
                    "redeemed_count": c.redeemed_count,
                    "is_active": c.is_active,
                }
                for c in rows
            ]
        }
    finally:
        db.close()


@router.post("/billing/coupons", status_code=201)
def billing_coupon_create(
    body: CouponIn, principal=Depends(super_admin_guard)
):
    if not body.code or not body.kind:
        raise ApiError(
            422, "validation_error", "code and kind are required."
        )
    db = SessionLocal()
    try:
        if db.query(_models.Coupon).filter_by(code=body.code).one_or_none():
            raise ApiError(409, "coupon_exists", "That code exists.")
        coupon = _models.Coupon(
            id=str(_uuid.uuid4()), code=body.code, kind=body.kind,
            created_by=principal.user_id,
        )
        _apply_fields(coupon, body)
        db.add(coupon)
        db.commit()
        admin_audit.record(
            principal, "billing.coupon_create",
            target_type="coupon", target_id=coupon.id,
            detail={"code": coupon.code, "kind": coupon.kind},
        )
        return {"id": coupon.id, "code": coupon.code}
    finally:
        db.close()


@router.post("/billing/coupons/{coupon_id}")
def billing_coupon_update(
    coupon_id: str, body: CouponIn,
    principal=Depends(super_admin_guard),
):
    db = SessionLocal()
    try:
        coupon = db.get(_models.Coupon, coupon_id)
        if coupon is None:
            raise ApiError(404, "coupon_not_found", "No such coupon.")
        changed = _apply_fields(coupon, body)
        db.commit()
        admin_audit.record(
            principal, "billing.coupon_update",
            target_type="coupon", target_id=coupon.id,
            detail={"changed": changed},
        )
        return {"id": coupon.id, "changed": changed}
    finally:
        db.close()


@router.get("/billing/users/{user_id}")
def billing_user_detail(user_id: str, principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            raise ApiError(404, "user_not_found", "No such user.")
        summary = _account_view.billing_summary(db, user_id)
        summary["user"] = {"id": user.id, "email": user.email,
                            "role": user.role, "status": user.status}
        summary["recent_transactions"] = _account_view.ledger_view(
            db, user_id, limit=20
        )
        return summary
    finally:
        db.close()


@router.post("/billing/users/{user_id}/credits/adjust")
def billing_credit_adjust(
    user_id: str, body: CreditAdjustIn,
    principal=Depends(super_admin_guard),
):
    db = SessionLocal()
    try:
        if db.get(User, user_id) is None:
            raise ApiError(404, "user_not_found", "No such user.")
        try:
            entry, created = _billing_credits.adjust(
                db, user_id, body.bucket, body.delta,
                actor_id=principal.user_id, reason=body.reason,
                idempotency_key=(
                    f"admin-adjust:{_uuid.uuid4().hex}"
                ),
            )
        except _billing_credits.InsufficientCreditsError as exc:
            db.rollback()
            raise ApiError(
                409, "insufficient_credits",
                f"Adjustment exceeds the available balance "
                f"({exc.available}).",
            ) from exc
        db.commit()
        admin_audit.record(
            principal, "billing.credit_adjust",
            target_type="user", target_id=user_id,
            detail={
                "bucket": body.bucket, "delta": body.delta,
                "reason": body.reason, "ledger_entry_id": entry.id,
            },
        )
        account = _billing_credits.get_or_create_account(db, user_id)
        return {
            "adjusted": created,
            "wallet": _billing_credits.balances(account),
        }
    finally:
        db.close()


@router.post("/billing/users/{user_id}/promo-grant")
def billing_promo_grant(
    user_id: str, body: PromoGrantIn,
    principal=Depends(super_admin_guard),
):
    """Grant a credits-kind coupon to one user (the redemption
    path for promo credits; per-user once, cap enforced)."""
    db = SessionLocal()
    try:
        if db.get(User, user_id) is None:
            raise ApiError(404, "user_not_found", "No such user.")
        coupon = db.get(_models.Coupon, body.coupon_id)
        if coupon is None or coupon.kind != "credits" or not coupon.is_active:
            raise ApiError(
                404, "coupon_not_found",
                "No active credits coupon with that id.",
            )
        if (
            coupon.max_redemptions is not None
            and coupon.redeemed_count >= coupon.max_redemptions
        ):
            raise ApiError(
                409, "coupon_exhausted",
                "This coupon has reached its redemption cap.",
            )
        redemption = _models.CouponRedemption(
            id=str(_uuid.uuid4()), coupon_id=coupon.id,
            user_id=user_id,
        )
        db.add(redemption)
        try:
            db.flush()
        except Exception as exc:
            db.rollback()
            raise ApiError(
                409, "coupon_already_redeemed",
                "This user has already redeemed this coupon.",
            ) from exc
        coupon.redeemed_count = int(coupon.redeemed_count or 0) + 1
        entry, _created = _billing_credits.grant(
            db, user_id, "promo", int(coupon.credits_grant or 0),
            entry_type="allocation_promo",
            idempotency_key=f"coupon:{coupon.id}:{user_id}",
            source="admin", actor_id=principal.user_id,
            reason=f"promo coupon {coupon.code}",
            expires_at=coupon.promo_expires_at,
        )
        db.commit()
        admin_audit.record(
            principal, "billing.promo_grant",
            target_type="user", target_id=user_id,
            detail={"coupon_id": coupon.id, "code": coupon.code,
                    "credits": coupon.credits_grant},
        )
        return {"granted": True, "ledger_entry_id": entry.id}
    finally:
        db.close()


@router.get("/billing/subscriptions")
def billing_subscriptions(
    status: str | None = None, principal=Depends(admin_guard)
):
    db = SessionLocal()
    try:
        query = db.query(UserSubscription)
        if status:
            query = query.filter_by(status=status)
        rows = query.order_by(
            UserSubscription.created_at.desc()
        ).limit(200).all()
        plans = {p.id: p.name for p in db.query(Plan).all()}
        users = {u.id: u.email for u in db.query(User).all()}
        return {
            "subscriptions": [
                {
                    **_account_view.subscription_view(s),
                    "user_id": s.user_id,
                    "user_email": users.get(s.user_id),
                    "plan_name": plans.get(s.plan_id),
                }
                for s in rows
            ]
        }
    finally:
        db.close()


@router.get("/billing/payments")
def billing_payments(
    status: str | None = None, principal=Depends(admin_guard)
):
    db = SessionLocal()
    try:
        query = db.query(_models.Payment)
        if status:
            query = query.filter_by(status=status)
        rows = query.order_by(
            _models.Payment.created_at.desc()
        ).limit(200).all()
        users = {u.id: u.email for u in db.query(User).all()}
        return {
            "payments": [
                {
                    "id": p.id, "user_id": p.user_id,
                    "user_email": users.get(p.user_id),
                    "provider": p.provider, "kind": p.kind,
                    "status": p.status,
                    "amount_cents": p.amount_cents,
                    "currency": p.currency,
                    "refunded_amount_cents": p.refunded_amount_cents,
                    "provider_transaction_id":
                        p.provider_transaction_id,
                    "created_at": p.created_at,
                }
                for p in rows
            ]
        }
    finally:
        db.close()


@router.get("/billing/events")
def billing_events(principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        rows = db.query(_models.PaymentEvent).order_by(
            _models.PaymentEvent.created_at.desc()
        ).limit(200).all()
        return {
            "events": [
                {
                    "id": e.id, "provider": e.provider,
                    "event_id": e.event_id,
                    "event_type": e.event_type,
                    "processed_at": e.processed_at,
                    "processing_error": e.processing_error,
                    "created_at": e.created_at,
                }
                for e in rows
            ]
        }
    finally:
        db.close()


# ---------------------------------------------------------------------
# Affiliate programme admin (item 1): review / approve /
# reject-with-reason / mark-paid, suspension, CSV export (the
# payout worksheet — no payout provider is integrated).
# Mutations: super_admin + audit log, like the rest of billing.
# ---------------------------------------------------------------------

from services.billing import affiliates as _affiliates


class AffiliateDecisionIn(BaseModel):
    model_config = {"extra": "forbid"}

    decision: str = Field(pattern="^(approve|reject|paid)$")
    reason: str | None = Field(default=None, max_length=500)


class AffiliateStatusIn(BaseModel):
    model_config = {"extra": "forbid"}

    status: str = Field(pattern="^(active|suspended)$")


@router.get("/affiliates")
def affiliates_overview(principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        return _affiliates.admin_overview(db)
    finally:
        db.close()


@router.get("/affiliates/entries")
def affiliates_entries(
    status: str | None = None, principal=Depends(admin_guard)
):
    db = SessionLocal()
    try:
        return {"entries": _affiliates.list_entries(db, status=status)}
    finally:
        db.close()


@router.post("/affiliates/entries/{entry_id}/decision")
def affiliates_entry_decision(
    entry_id: str, body: AffiliateDecisionIn,
    principal=Depends(super_admin_guard),
):
    db = SessionLocal()
    try:
        try:
            entry = _affiliates.decide_entry(
                db, entry_id, decision=body.decision,
                decided_by=principal.user_id, reason=body.reason,
            )
        except LookupError as error:
            raise ApiError(
                404, "entry_not_found", str(error)
            ) from error
        except ValueError as error:
            raise ApiError(
                422, "decision_invalid", str(error)
            ) from error
        admin_audit.record(
            principal, "affiliate.entry_decision",
            target_type="commission_entry", target_id=entry.id,
            detail={"decision": body.decision,
                    "reason": body.reason},
        )
        return {
            "id": entry.id, "status": entry.status,
            "kind": entry.kind,
            "commission_cents": entry.commission_cents,
        }
    finally:
        db.close()


@router.post("/affiliates/{affiliate_id}/status")
def affiliates_set_status(
    affiliate_id: str, body: AffiliateStatusIn,
    principal=Depends(super_admin_guard),
):
    db = SessionLocal()
    try:
        try:
            profile = _affiliates.set_affiliate_status(
                db, affiliate_id, status=body.status
            )
        except LookupError as error:
            raise ApiError(
                404, "affiliate_not_found", str(error)
            ) from error
        admin_audit.record(
            principal, "affiliate.status",
            target_type="affiliate", target_id=profile.id,
            detail={"status": body.status},
        )
        return {"id": profile.id, "status": profile.status}
    finally:
        db.close()


@router.get("/affiliates/export")
def affiliates_export(principal=Depends(admin_guard)):
    from fastapi.responses import PlainTextResponse

    db = SessionLocal()
    try:
        csv_text = _affiliates.export_csv(db)
    finally:
        db.close()
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition":
                "attachment; filename=affiliate-ledger.csv"
        },
    )


@router.get("/billing/reports")
def billing_reports(days: int = 30, principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        return _billing_costs.financial_report(
            db, days=max(1, min(days, 365))
        )
    finally:
        db.close()


@router.get("/billing/retention")
def billing_retention(principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        return _billing_retention.storage_summary(db)
    finally:
        db.close()


@router.post("/billing/retention/{video_id}/retry")
def billing_retention_retry(
    video_id: str, principal=Depends(_write_guard)
):
    db = SessionLocal()
    try:
        row = db.get(_models.GeneratedVideo, video_id)
        if row is None:
            raise ApiError(404, "video_not_found", "No such video.")
        row.status = "active"
        row.delete_attempts = 0
        row.last_delete_error = None
        db.commit()
        admin_audit.record(
            principal, "billing.retention_retry",
            target_type="generated_video", target_id=video_id,
        )
        return {"video_id": video_id, "status": "active"}
    finally:
        db.close()


@router.get("/billing/pricing-rules")
def billing_pricing_rules(principal=Depends(admin_guard)):
    db = SessionLocal()
    try:
        rows = db.query(_models.PricingRule).order_by(
            _models.PricingRule.provider.asc()
        ).all()
        return {
            "rules": [
                {
                    "id": r.id, "provider": r.provider,
                    "model": r.model, "unit": r.unit,
                    "rate_microusd": r.rate_microusd,
                    "effective_from": r.effective_from,
                    "effective_to": r.effective_to,
                    "configured_by": r.configured_by,
                }
                for r in rows
            ]
        }
    finally:
        db.close()


@router.post("/billing/pricing-rules", status_code=201)
def billing_pricing_rule_create(
    body: PricingRuleIn, principal=Depends(super_admin_guard)
):
    db = SessionLocal()
    try:
        rule = _models.PricingRule(
            id=str(_uuid.uuid4()),
            provider=body.provider, model=body.model,
            unit=body.unit, rate_microusd=body.rate_microusd,
            effective_from=body.effective_from,
            effective_to=body.effective_to,
            configured_by=principal.user_id,
        )
        db.add(rule)
        db.commit()
        admin_audit.record(
            principal, "billing.pricing_rule_create",
            target_type="pricing_rule", target_id=rule.id,
            detail={"provider": rule.provider, "unit": rule.unit,
                    "rate_microusd": rule.rate_microusd},
        )
        return {"id": rule.id}
    finally:
        db.close()


@router.delete("/billing/pricing-rules/{rule_id}")
def billing_pricing_rule_delete(
    rule_id: str, principal=Depends(super_admin_guard)
):
    db = SessionLocal()
    try:
        rule = db.get(_models.PricingRule, rule_id)
        if rule is None:
            raise ApiError(404, "rule_not_found", "No such rule.")
        db.delete(rule)
        db.commit()
        admin_audit.record(
            principal, "billing.pricing_rule_delete",
            target_type="pricing_rule", target_id=rule_id,
        )
        return {"deleted": True}
    finally:
        db.close()
