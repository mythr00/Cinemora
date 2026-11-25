"""Subscription lifecycle job (Phase 8; directive §6).

Runs hourly from the dispatcher's leader-elected loop (exactly
one worker executes it). Responsibilities:

- Period rollover for active metered subscriptions whose benefit
  period has ended: close the old period, expire its remaining
  subscription credits (unless rollover is enabled via
  CINEMORA_SUBSCRIPTION_CREDIT_ROLLOVER=1), apply any scheduled
  plan change (pending_plan_id — downgrades land here, §15),
  advance the bounds by one month, and open the new period with
  a fresh allocation. Annual subscriptions roll over monthly
  too: the payment period is annual, benefit periods monthly.
- Cancellation at period end (cancel_at_period_end): the
  subscription ends, the period closes, remaining subscription
  credits expire. Nothing else is deleted — projects, scripts
  and history are untouched by plan changes (§15).
- Past-due expiry: a past_due subscription whose period has
  ended without a successful payment becomes expired.
- Purchased/promo lot expiry: allocations carrying an expires_at
  (purchased credits valid 12 months; promo credits their
  disclosed expiry) are expired FIFO from the ledger once past
  due — lot remainders are reconstructed deterministically from
  the immutable ledger.
- Reservation reconciliation: any reservation still 'active'
  whose run is terminal is settled/restored (the backstop for a
  settlement that failed inside finish_run).

Every step is idempotent (unique period per user+start, keyed
ledger entries), so a double tick or a leader change mid-tick
cannot double-allocate or double-expire.
"""

from __future__ import annotations

import calendar
import os
from datetime import datetime, timedelta, timezone

import models
from database import SessionLocal
from services.billing import credits
from services.billing import periods as periods_service

LOT_ENTRY_TYPES = ("allocation_purchase", "allocation_promo")


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _add_month(value: datetime) -> datetime:
    month = value.month + 1
    year = value.year + (1 if month > 12 else 0)
    month = month - 12 if month > 12 else month
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def rollover_enabled() -> bool:
    return os.environ.get(
        "CINEMORA_SUBSCRIPTION_CREDIT_ROLLOVER", ""
    ).strip() in ("1", "true", "yes")


def _close_period(db, period, *, expire_credits: bool, reason: str):
    if period is None or period.status != "open":
        return 0
    expired = 0
    if expire_credits:
        expired = credits.expire_bucket(
            db,
            period.user_id,
            "subscription",
            idempotency_key=f"expire:{period.id}",
            reason=reason,
            billing_period_id=period.id,
        )
    period.status = "closed"
    db.flush()
    return expired


def _rollover_subscription(db, subscription, plan, now) -> str:
    """Advance one active subscription past its period end.
    Returns the action taken."""
    old_period = (
        db.query(models.BillingPeriod)
        .filter_by(
            user_id=subscription.user_id,
            period_start=_aware(subscription.current_period_start),
        )
        .one_or_none()
    )

    if subscription.cancel_at_period_end:
        _close_period(
            db, old_period,
            expire_credits=True, reason="subscription cancelled",
        )
        subscription.status = "cancelled"
        subscription.cancel_at_period_end = False
        db.flush()
        return "cancelled_at_period_end"

    _close_period(
        db, old_period,
        expire_credits=not rollover_enabled(),
        reason="period rollover",
    )

    if subscription.pending_plan_id:
        subscription.plan_id = subscription.pending_plan_id
        subscription.pending_plan_id = None

    old_end = _aware(subscription.current_period_end)
    candidate_start, candidate_end = periods_service.month_bounds(now)
    if candidate_start > _aware(subscription.current_period_start):
        new_start, new_end = candidate_start, candidate_end
    else:
        new_start, new_end = old_end, _add_month(old_end)
    subscription.current_period_start = new_start
    subscription.current_period_end = new_end
    db.flush()

    periods_service.ensure_current_period(db, subscription.user_id, now=now)
    return "rolled_over"


def _expire_past_due(db, subscription, now) -> str:
    period = periods_service.current_period(db, subscription.user_id)
    _close_period(
        db, period, expire_credits=True,
        reason="subscription expired (past due)",
    )
    subscription.status = "expired"
    db.flush()
    return "expired_past_due"


def _expire_lots(db, now) -> int:
    """Expire purchased/promo allocation lots past their
    expires_at, FIFO. Returns total credits expired."""
    total = 0
    lots = (
        db.query(models.CreditLedgerEntry)
        .filter(
            models.CreditLedgerEntry.entry_type.in_(LOT_ENTRY_TYPES),
            models.CreditLedgerEntry.expires_at.isnot(None),
            models.CreditLedgerEntry.expires_at <= now,
        )
        .order_by(models.CreditLedgerEntry.expires_at.asc())
        .all()
    )
    for lot in lots:
        # How much of this lot was already consumed? Consumption
        # from the bucket (charges + prior expiries) is attributed
        # FIFO across the bucket's lots in expiry order.
        bucket = lot.bucket
        consumed_before = (
            db.query(models.CreditLedgerEntry)
            .filter(
                models.CreditLedgerEntry.user_id == lot.user_id,
                models.CreditLedgerEntry.bucket == bucket,
                models.CreditLedgerEntry.entry_type.in_(
                    ("allocation_purchase", "allocation_promo")
                ),
                models.CreditLedgerEntry.expires_at.isnot(None),
                models.CreditLedgerEntry.expires_at
                < _aware(lot.expires_at),
            )
            .all()
        )
        prior_lots_total = sum(e.amount for e in consumed_before)
        debits = (
            db.query(models.CreditLedgerEntry)
            .filter(
                models.CreditLedgerEntry.user_id == lot.user_id,
                models.CreditLedgerEntry.bucket == bucket,
                models.CreditLedgerEntry.entry_type.in_(
                    ("charge", "expire")
                ),
            )
            .all()
        )
        total_debits = sum(-e.amount for e in debits if e.amount < 0)
        consumed_from_this = max(
            0, min(lot.amount, total_debits - prior_lots_total)
        )
        remaining = lot.amount - consumed_from_this
        if remaining <= 0:
            continue
        total += credits.expire_amount(
            db,
            lot.user_id,
            bucket,
            remaining,
            idempotency_key=f"lot-expire:{lot.id}",
            reason="credit lot expired",
        )
    return total


def _reconcile_reservations(db) -> int:
    from services.billing import generation_billing

    active = (
        db.query(models.GenerationReservation)
        .filter_by(status="active")
        .limit(200)
        .all()
    )
    settled = 0
    for reservation in active:
        run = db.get(models.ProductionRun, reservation.run_id)
        if run is not None and run.status in (
            "completed", "failed", "cancelled",
        ):
            generation_billing.settle_for_run(run.id, run.status)
            settled += 1
    return settled


def run_lifecycle_tick(now: datetime | None = None) -> dict:
    """One lifecycle pass. Returns a summary dict; never raises
    (the dispatcher loop logs and continues)."""
    now = now or datetime.now(timezone.utc)
    summary = {
        "rolled_over": 0,
        "cancelled_at_period_end": 0,
        "expired_past_due": 0,
        "lots_expired_credits": 0,
        "reservations_reconciled": 0,
        "errors": 0,
    }
    db = SessionLocal()
    try:
        subscriptions = (
            db.query(models.UserSubscription)
            .filter(
                models.UserSubscription.status.in_(
                    ("active", "past_due")
                )
            )
            .all()
        )
        for subscription in subscriptions:
            try:
                plan = db.get(models.Plan, subscription.plan_id)
                if plan is None or plan.billing_mode != "metered":
                    continue
                period_end = _aware(subscription.current_period_end)
                if period_end is None or now < period_end:
                    continue
                if subscription.status == "past_due":
                    action = _expire_past_due(db, subscription, now)
                else:
                    action = _rollover_subscription(
                        db, subscription, plan, now
                    )
                summary[action] = summary.get(action, 0) + 1
                db.commit()
            except Exception:
                db.rollback()
                summary["errors"] += 1
        try:
            summary["lots_expired_credits"] = _expire_lots(db, now)
            db.commit()
        except Exception:
            db.rollback()
            summary["errors"] += 1
        try:
            summary["reservations_reconciled"] = (
                _reconcile_reservations(db)
            )
        except Exception:
            summary["errors"] += 1
        return summary
    finally:
        db.close()
