"""Checkout + subscription self-service (Phase 8; directive
§§10, 15).

Every paid action goes through the provider; local state changes
for paid subscriptions happen in webhook processing, never here
on a frontend signal. The exceptions are deliberate and narrow:

- a DOWNGRADE only schedules pending_plan_id (no benefit is
  granted early; the lifecycle job applies it at the boundary);
- a CANCELLATION only sets cancel_at_period_end (access runs to
  the paid period's end, §15);
- an operator-granted subscription (billing_provider NULL — no
  provider relationship exists) is changed locally, since there
  is no provider state to confirm.

Without provider credentials every paid entry point raises
BillingRejected(503, payment_provider_not_configured).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import models
from database import SessionLocal
from services import plans as plans_service
from services.billing import credits, providers
from services.billing.generation_billing import BillingRejected
from services.billing.lifecycle import _add_month


def _session():
    return SessionLocal()


def _not_configured() -> BillingRejected:
    return BillingRejected(
        "payment_provider_not_configured",
        "Payments are not configured on this deployment yet. "
        "No charge was made.",
        status_code=503,
    )


def _user_email(db, user_id: str) -> str:
    user = db.get(models.User, user_id)
    if user is None:
        raise BillingRejected(
            "user_not_found", "Account not found.", status_code=404
        )
    return user.email


def _success_url() -> str | None:
    base = os.environ.get("CINEMORA_PUBLIC_BASE_URL")
    return f"{base}/billing?checkout=return" if base else None


def create_subscription_checkout(user_id: str, plan_id: str, interval: str) -> dict:
    provider = providers.get_provider()
    if not provider.is_configured():
        raise _not_configured()
    db = _session()
    try:
        plan = db.get(models.Plan, plan_id)
        if plan is None or plan.billing_mode != "metered" or not plan.is_public:
            raise BillingRejected(
                "plan_not_available",
                "That plan is not available for purchase.",
                status_code=404,
            )
        if interval not in ("month", "year"):
            raise BillingRejected(
                "invalid_interval", "interval must be month or year.",
                status_code=422,
            )
        amount = (
            plan.price_annual_cents
            if interval == "year"
            else plan.price_monthly_cents
        )
        custom_data = {
            "user_id": user_id,
            "kind": "subscription",
            "plan_id": plan.id,
            "billing_interval": interval,
            "amount_cents": amount,
            "currency": plan.currency or "USD",
        }
        return provider.create_checkout(
            user_email=_user_email(db, user_id),
            custom_data=custom_data,
            items=[providers.subscription_price_item(plan, interval)],
            success_url=_success_url(),
        )
    finally:
        db.close()


def create_topup_checkout(user_id: str, package_id: str) -> dict:
    provider = providers.get_provider()
    if not provider.is_configured():
        raise _not_configured()
    db = _session()
    try:
        package = db.get(models.TopupPackage, package_id)
        if package is None or not package.is_active:
            raise BillingRejected(
                "package_not_available",
                "That top-up package is not available.",
                status_code=404,
            )
        custom_data = {
            "user_id": user_id,
            "kind": "topup",
            "package_id": package.id,
            "amount_cents": package.price_cents,
            "currency": package.currency or "USD",
        }
        return provider.create_checkout(
            user_email=_user_email(db, user_id),
            custom_data=custom_data,
            items=[providers.topup_price_item(package)],
            success_url=_success_url(),
        )
    finally:
        db.close()


def change_subscription(user_id: str, new_plan_id: str, interval: str | None = None) -> dict:
    """Upgrade immediately (provider proration when the
    subscription is a paid one; pro-rated credit difference
    granted locally on confirmation), downgrade at the period
    boundary via pending_plan_id. Returns the outcome for the
    API to present."""
    db = _session()
    try:
        subscription = plans_service.get_active_subscription(db, user_id)
        if subscription is None:
            raise BillingRejected(
                "no_active_subscription",
                "You do not have an active subscription to change.",
                status_code=409,
            )
        current = db.get(models.Plan, subscription.plan_id)
        target = db.get(models.Plan, new_plan_id)
        if target is None or target.billing_mode != "metered":
            raise BillingRejected(
                "plan_not_available",
                "That plan is not available.",
                status_code=404,
            )
        if current is not None and target.id == current.id:
            return {"changed": False, "reason": "already_on_plan"}

        is_upgrade = (target.price_monthly_cents or 0) > (
            current.price_monthly_cents or 0 if current else 0
        )
        paid = bool(subscription.billing_provider and subscription.external_id)

        if not is_upgrade:
            subscription.pending_plan_id = target.id
            db.commit()
            return {
                "changed": True,
                "effective": "next_period",
                "plan_id": target.id,
            }

        # Upgrade: immediate.
        if paid:
            provider = providers.get_provider()
            if not provider.is_configured():
                raise _not_configured()
            provider.update_subscription(
                subscription.external_id,
                {
                    "items": [
                        providers.subscription_price_item(
                            target, interval or subscription.billing_interval or "month"
                        )
                    ],
                    "proration_billing_mode": "prorated_immediately",
                },
            )
            # Local plan switch is applied by the subscription
            # webhook; the credit difference is granted here
            # pro-rated because the provider has already charged
            # the prorated difference at this point.
            _grant_upgrade_credit_difference(
                db, subscription, current, target
            )
            subscription.plan_id = target.id
            subscription.pending_plan_id = None
            if interval:
                subscription.billing_interval = interval
            db.commit()
            return {
                "changed": True,
                "effective": "immediate",
                "plan_id": target.id,
                "via": "provider",
            }

        # Operator-granted subscription: no provider state exists;
        # apply locally with the same pro-rated credit rule.
        _grant_upgrade_credit_difference(db, subscription, current, target)
        subscription.plan_id = target.id
        subscription.pending_plan_id = None
        if interval:
            subscription.billing_interval = interval
        db.commit()
        return {
            "changed": True,
            "effective": "immediate",
            "plan_id": target.id,
            "via": "local",
        }
    finally:
        db.close()


def _grant_upgrade_credit_difference(db, subscription, current, target):
    """Pro-rated share of the monthly-credit difference for the
    remainder of the current period, granted exactly once per
    period+target (idempotency key)."""
    difference = int(target.monthly_credits or 0) - int(
        current.monthly_credits or 0 if current else 0
    )
    if difference <= 0:
        return
    now = datetime.now(timezone.utc)
    start = subscription.current_period_start
    end = subscription.current_period_end
    fraction = 1.0
    if start and end:
        start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
        total = (end - start).total_seconds()
        remaining = (end - now).total_seconds()
        if total > 0:
            fraction = max(0.0, min(1.0, remaining / total))
    amount = int(difference * fraction)
    if amount <= 0:
        return
    credits.grant(
        db,
        subscription.user_id,
        "subscription",
        amount,
        entry_type="allocation_subscription",
        idempotency_key=(
            f"upgrade:{subscription.id}:"
            f"{start.isoformat() if start else 'nostart'}:{target.id}"
        ),
        source="system",
        subscription_id=subscription.id,
        reason="upgrade credit difference (pro-rated)",
    )


def cancel_subscription(user_id: str, *, immediate: bool = False) -> dict:
    """§15: cancellation keeps access to the end of the paid
    period and deletes nothing. Immediate cancellation exists
    for admin use."""
    db = _session()
    try:
        subscription = plans_service.get_active_subscription(db, user_id)
        if subscription is None:
            raise BillingRejected(
                "no_active_subscription",
                "You do not have an active subscription to cancel.",
                status_code=409,
            )
        if subscription.billing_provider and subscription.external_id:
            provider = providers.get_provider()
            if not provider.is_configured():
                raise _not_configured()
            provider.cancel_subscription(
                subscription.external_id,
                effective_from=(
                    "immediately" if immediate else "next_billing_period"
                ),
            )
        if immediate:
            subscription.status = "cancelled"
            subscription.cancel_at_period_end = False
        else:
            subscription.cancel_at_period_end = True
        db.commit()
        return {
            "cancelled": True,
            "immediate": immediate,
            "access_until": (
                subscription.current_period_end.isoformat()
                if subscription.current_period_end and not immediate
                else None
            ),
        }
    finally:
        db.close()


def management_url(user_id: str) -> str | None:
    provider = providers.get_provider()
    if not provider.is_configured():
        return None
    db = _session()
    try:
        subscription = plans_service.get_active_subscription(db, user_id)
        if subscription is None or not subscription.provider_customer_id:
            return None
        return provider.create_management_url(
            subscription.provider_customer_id
        )
    finally:
        db.close()
