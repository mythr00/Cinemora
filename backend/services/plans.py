"""Plans, usage accounting and limit enforcement (Phase 8.3;
design 02 §5).

- Usage is DERIVED from the run ledger, never separately metered
  (02 §5.2): concurrency and period counts are queries over
  production_runs by owner; provider consumption aggregates the
  per-run `execution` JSON; storage is a cached walk of the owner's
  project media dirs.
- Enforcement applies only when a plan RESOLVES for the user
  (active subscription, else the default plan). Where no plan
  system is seeded at all (legacy single-operator databases and
  the pre-8.x test fixtures), nothing resolves and nothing is
  enforced — limits are a property of the plan system, and the
  operator-token era had none. On any seeded deployment every
  account resolves: registrations are subscribed to the default
  plan at creation (api_v1._assign_default_plan) and the founder
  holds the founder plan.
- Errors are typed ApiErrors (plan_limit_*) so clients can tell a
  plan rejection from a failure; an over-limit submission creates
  NOTHING (02 §5.3: a queued-but-never-runnable run is a lie).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy import func

import models
from services import media_paths

ACTIVE_RUN_STATUSES = ("queued", "running", "awaiting_approval")

_storage_cache: dict[str, tuple[float, int]] = {}
_STORAGE_TTL_SECONDS = 60.0


def get_active_subscription(db, user_id: str):
    return (
        db.query(models.UserSubscription)
        .filter(
            models.UserSubscription.user_id == user_id,
            models.UserSubscription.status == "active",
        )
        .one_or_none()
    )


def resolve_effective_plan(db, user_id: str):
    """The plan that governs this user right now, or None when no
    plan system is seeded / nothing applies.

    Fail-soft on a database without the accounts tables (legacy
    fixture DBs that create_all only the production tables): no
    plan system exists there, so no plan applies — enforcement is
    a property of the plan system, and an unseeded deployment sees
    zero behaviour change (design 02 §5).

    A principal with NO user row is not a tenant and resolves to
    no plan: that is the synthetic operator principal of the
    dual-auth window (02 §1.4), whose token must keep working
    exactly as it did before accounts existed — resolving it to
    the default tenant plan would throttle the operator with
    free-tier caps (found by the Phase 8 full-suite gate: leaked
    plan rows + the operator's own active runs produced 429s on
    the pre-accounts API contract tests)."""
    try:
        if db.get(models.User, user_id) is None:
            return None
        subscription = get_active_subscription(db, user_id)
        if subscription is not None:
            return db.get(models.Plan, subscription.plan_id)
        return (
            db.query(models.Plan)
            .filter(models.Plan.is_default.is_(True))
            .first()
        )
    except Exception:
        db.rollback()
        return None


# ---------------------------------------------------------------------
# Derived usage
# ---------------------------------------------------------------------


def concurrent_run_count(db, user_id: str) -> int:
    return (
        db.query(func.count(models.ProductionRun.id))
        .filter(
            models.ProductionRun.owner_id == user_id,
            models.ProductionRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .scalar()
        or 0
    )


def period_start_for(db, user_id: str) -> datetime:
    """Current billing-period start: the active subscription's
    period start when recorded, else the first of this calendar
    month (UTC) — plans' only period in v1 is "month" (02 §5.1)."""
    subscription = get_active_subscription(db, user_id)
    if subscription is not None and subscription.current_period_start:
        start = subscription.current_period_start
        return start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def runs_in_period(db, user_id: str, period_start: datetime) -> int:
    return (
        db.query(func.count(models.ProductionRun.id))
        .filter(
            models.ProductionRun.owner_id == user_id,
            models.ProductionRun.created_at >= period_start,
        )
        .scalar()
        or 0
    )


def provider_usage_in_period(db, user_id: str, period_start: datetime) -> dict:
    """Aggregate the per-run execution records (02 §5.2). Serper
    counts exist for every finished run; LLM counts exist once the
    Phase 8.5 choke-point counter lands (absent key = not counted,
    reported as such by the caller, never invented)."""
    rows = (
        db.query(models.ProductionRun.execution)
        .filter(
            models.ProductionRun.owner_id == user_id,
            models.ProductionRun.created_at >= period_start,
            models.ProductionRun.execution.isnot(None),
        )
        .all()
    )
    serper_total = 0
    llm_calls = 0
    llm_tokens = 0
    llm_counted_runs = 0
    for (execution,) in rows:
        if not isinstance(execution, dict):
            continue
        serper = execution.get("serper_calls") or {}
        if isinstance(serper, dict):
            serper_total += int(serper.get("total") or 0)
        llm = execution.get("llm_calls")
        if isinstance(llm, dict):
            llm_counted_runs += 1
            llm_calls += int(llm.get("total") or 0)
            llm_tokens += int(llm.get("total_tokens") or 0)
    return {
        "serper_calls": serper_total,
        "llm_calls": llm_calls,
        "llm_tokens": llm_tokens,
        "llm_counted_runs": llm_counted_runs,
    }


def storage_bytes_for_user(db, user_id: str) -> int:
    """Sum of the owner's project media dirs (cached 60 s — the
    walk over a large tree is the expensive part, and a slightly
    stale number is fine for a cap check, 02 §5.3 point 4)."""
    now = time.monotonic()
    cached = _storage_cache.get(user_id)
    if cached and now - cached[0] < _STORAGE_TTL_SECONDS:
        return cached[1]
    total = 0
    project_ids = (
        db.query(models.Project.id)
        .filter(models.Project.owner_id == user_id)
        .all()
    )
    for (project_id,) in project_ids:
        root = media_paths.project_dir(project_id)
        if not root.exists():
            continue
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
    _storage_cache[user_id] = (now, total)
    return total


def usage_summary(db, user_id: str) -> dict:
    plan = resolve_effective_plan(db, user_id)
    period_start = period_start_for(db, user_id)
    return {
        "plan": (
            {
                "name": plan.name,
                "max_concurrent_runs": plan.max_concurrent_runs,
                "max_runs_per_period": plan.max_runs_per_period,
                "period": plan.period,
                "max_script_chars": plan.max_script_chars,
                "max_storage_bytes": plan.max_storage_bytes,
                "max_upload_bytes": plan.max_upload_bytes,
                "priority_tier": plan.priority_tier,
            }
            if plan is not None
            else None
        ),
        "period_start": period_start.isoformat(),
        "concurrent_runs": concurrent_run_count(db, user_id),
        "runs_in_period": runs_in_period(db, user_id, period_start),
        "provider": provider_usage_in_period(db, user_id, period_start),
        "storage_bytes": storage_bytes_for_user(db, user_id),
    }


# ---------------------------------------------------------------------
# Enforcement (typed errors; nothing is created on rejection)
# ---------------------------------------------------------------------


def _api_error(status: int, code: str, message: str):
    # Local import: api_v1 imports this module, not vice versa —
    # raise the shared error type without a circular import.
    from api_v1 import ApiError

    return ApiError(status, code, message)


def enforce_script_length(db, user_id: str, script: str) -> None:
    plan = resolve_effective_plan(db, user_id)
    if plan is None or plan.max_script_chars is None:
        return
    if len(script) > plan.max_script_chars:
        raise _api_error(
            422,
            "plan_limit_script_chars",
            f"Your {plan.name} plan allows scripts up to "
            f"{plan.max_script_chars} characters; this one is "
            f"{len(script)}.",
        )


def enforce_run_admission(db, user_id: str) -> None:
    """Concurrency cap + monthly quota + storage cap, checked
    before a NEW run is created (deduplicated submissions of an
    already-active run never reach here — they create nothing)."""
    plan = resolve_effective_plan(db, user_id)
    if plan is None:
        return
    if concurrent_run_count(db, user_id) >= plan.max_concurrent_runs:
        raise _api_error(
            429,
            "plan_limit_concurrency",
            f"Your {plan.name} plan allows "
            f"{plan.max_concurrent_runs} concurrent production(s); "
            "wait for one to finish or upgrade your plan.",
        )
    if plan.max_runs_per_period is not None:
        start = period_start_for(db, user_id)
        if runs_in_period(db, user_id, start) >= plan.max_runs_per_period:
            raise _api_error(
                429,
                "plan_limit_quota",
                f"Your {plan.name} plan allows "
                f"{plan.max_runs_per_period} productions per month; "
                "the quota resets next period.",
            )
    if plan.max_storage_bytes is not None:
        if storage_bytes_for_user(db, user_id) >= plan.max_storage_bytes:
            raise _api_error(
                429,
                "plan_limit_storage",
                f"Your {plan.name} plan's storage allowance is "
                "exhausted; delete old productions or upgrade.",
            )


def enforce_upload_size(db, user_id: str, size_bytes: int | None) -> None:
    if size_bytes is None:
        return
    plan = resolve_effective_plan(db, user_id)
    if plan is None or plan.max_upload_bytes is None:
        return
    if size_bytes > plan.max_upload_bytes:
        raise _api_error(
            413,
            "plan_limit_upload",
            f"Your {plan.name} plan allows uploads up to "
            f"{plan.max_upload_bytes} bytes; this file is "
            f"{size_bytes}.",
        )
