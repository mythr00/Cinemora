"""Benefit periods, entitlements and allowances (Phase 8;
directive §§2, 6, 7).

A BillingPeriod is one monthly benefit period of one metered
subscription. It is created lazily (first metered use) or by the
lifecycle job at rollover, and creation allocates the plan's
monthly credits into the subscription bucket exactly once
(ledger idempotency key alloc:{subscription_id}:{period_start}).

Entitlement/allowance counters count COMMITTED units: an
entitlement is consumed when a generation reserves it and
released back if that generation does not complete, so two
concurrent submissions can never spend the same entitlement.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

import models
from services import plans as plans_service
from services.billing import credits

_ALLOWANCE_FIELDS = {
    "generations": ("generations_granted", "generations_used"),
    "scripts": ("scripts_granted", "scripts_used"),
    "thumbnails": ("thumbnails_granted", "thumbnails_used"),
}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0,
                        microsecond=0)
    last_day = calendar.monthrange(start.year, start.month)[1]
    end = start.replace(day=last_day, hour=23, minute=59, second=59,
                        microsecond=999999)
    return start, end


def ensure_current_period(db, user_id: str, now: datetime | None = None):
    """Return the open BillingPeriod for the user's active metered
    subscription, creating it (and allocating the period's
    credits) when missing. None when the user has no active
    metered subscription. The caller commits."""
    now = now or datetime.now(timezone.utc)
    subscription = plans_service.get_active_subscription(db, user_id)
    if subscription is None:
        return None
    plan = db.get(models.Plan, subscription.plan_id)
    if plan is None or plan.billing_mode != "metered":
        return None

    if subscription.current_period_start and subscription.current_period_end:
        start = _aware(subscription.current_period_start)
        end = _aware(subscription.current_period_end)
    else:
        start, end = month_bounds(now)
        # Stamp the subscription so every later reader (lifecycle
        # job included) sees the same bounds.
        subscription.current_period_start = start
        subscription.current_period_end = end

    period = (
        db.query(models.BillingPeriod)
        .filter_by(user_id=user_id, period_start=start)
        .one_or_none()
    )
    if period is not None:
        return period

    period = models.BillingPeriod(
        id=str(uuid.uuid4()),
        user_id=user_id,
        subscription_id=subscription.id,
        plan_id=plan.id,
        period_start=start,
        period_end=end,
        generations_granted=plan.included_generations or 0,
        generations_used=0,
        scripts_granted=plan.script_allowance or 0,
        scripts_used=0,
        thumbnails_granted=plan.thumbnail_allowance or 0,
        thumbnails_used=0,
        credits_allocated=0,
        status="open",
    )
    db.add(period)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return (
            db.query(models.BillingPeriod)
            .filter_by(user_id=user_id, period_start=start)
            .one_or_none()
        )

    if plan.monthly_credits:
        _entry, created = credits.grant(
            db,
            user_id,
            "subscription",
            int(plan.monthly_credits),
            entry_type="allocation_subscription",
            idempotency_key=(
                f"alloc:{subscription.id}:{start.isoformat()}"
            ),
            source="system",
            subscription_id=subscription.id,
            billing_period_id=period.id,
            reason="monthly subscription credit allocation",
        )
        if created:
            period.credits_allocated = int(plan.monthly_credits)
    return period


def current_period(db, user_id: str):
    """The user's open period covering now, or None (no creation,
    no allocation)."""
    now = datetime.now(timezone.utc)
    return (
        db.query(models.BillingPeriod)
        .filter(
            models.BillingPeriod.user_id == user_id,
            models.BillingPeriod.status == "open",
            models.BillingPeriod.period_start <= now,
            models.BillingPeriod.period_end >= now,
        )
        .order_by(models.BillingPeriod.period_start.desc())
        .first()
    )


def consume_allowance(db, period, kind: str, count: int = 1) -> bool:
    """Commit `count` units of an allowance. False (nothing
    changed) when fewer than `count` units remain."""
    granted_field, used_field = _ALLOWANCE_FIELDS[kind]
    granted = int(getattr(period, granted_field) or 0)
    used = int(getattr(period, used_field) or 0)
    if used + count > granted:
        return False
    setattr(period, used_field, used + count)
    db.flush()
    return True


def release_allowance(db, period, kind: str, count: int = 1) -> None:
    """Return previously committed units (a generation that did
    not complete; an operation that failed after consuming)."""
    _granted_field, used_field = _ALLOWANCE_FIELDS[kind]
    used = int(getattr(period, used_field) or 0)
    setattr(period, used_field, max(0, used - count))
    db.flush()


def allowance_remaining(period, kind: str) -> int:
    if period is None:
        return 0
    granted_field, used_field = _ALLOWANCE_FIELDS[kind]
    return max(
        0,
        int(getattr(period, granted_field) or 0)
        - int(getattr(period, used_field) or 0),
    )
