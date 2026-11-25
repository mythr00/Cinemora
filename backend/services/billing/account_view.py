"""User billing views (Phase 8; directive §14).

Serializers for the account billing surface. Every figure comes
from the real records (plans, subscriptions, periods, wallet,
ledger, payments) — nothing is estimated or fabricated; costs
and balances the system does not know are simply absent.
"""

from __future__ import annotations

import models
from services import plans as plans_service
from services.billing import catalogue, credits, providers
from services.billing import periods as periods_service


def _dt(value):
    if value is None:
        return None
    if value.tzinfo is None:
        from datetime import timezone

        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def plan_view(plan) -> dict | None:
    if plan is None:
        return None
    return {
        "id": plan.id,
        "name": plan.name,
        "billing_mode": plan.billing_mode,
        "price_monthly_cents": plan.price_monthly_cents,
        "price_annual_cents": plan.price_annual_cents,
        "currency": plan.currency,
        "monthly_credits": plan.monthly_credits,
        "included_generations": plan.included_generations,
        "script_allowance": plan.script_allowance,
        "thumbnail_allowance": plan.thumbnail_allowance,
        "max_concurrent_runs": plan.max_concurrent_runs,
        "priority_tier": plan.priority_tier,
        "is_public": plan.is_public,
        "features": plan.features,
    }


def subscription_view(subscription) -> dict | None:
    if subscription is None:
        return None
    return {
        "id": subscription.id,
        "plan_id": subscription.plan_id,
        "status": subscription.status,
        "billing_interval": subscription.billing_interval,
        "billing_provider": subscription.billing_provider,
        "current_period_start": _dt(subscription.current_period_start),
        "current_period_end": _dt(subscription.current_period_end),
        "cancel_at_period_end": bool(subscription.cancel_at_period_end),
        "pending_plan_id": subscription.pending_plan_id,
    }


def period_view(period) -> dict | None:
    if period is None:
        return None
    return {
        "id": period.id,
        "period_start": _dt(period.period_start),
        "period_end": _dt(period.period_end),
        "status": period.status,
        "generations": {
            "granted": period.generations_granted,
            "used": period.generations_used,
            "remaining": periods_service.allowance_remaining(
                period, "generations"
            ),
        },
        "scripts": {
            "granted": period.scripts_granted,
            "used": period.scripts_used,
            "remaining": periods_service.allowance_remaining(
                period, "scripts"
            ),
        },
        "thumbnails": {
            "granted": period.thumbnails_granted,
            "used": period.thumbnails_used,
            "remaining": periods_service.allowance_remaining(
                period, "thumbnails"
            ),
        },
        "credits_allocated": period.credits_allocated,
    }


def billing_summary(db, user_id: str) -> dict:
    plan = plans_service.resolve_effective_plan(db, user_id)
    subscription = plans_service.get_active_subscription(db, user_id)
    period = periods_service.current_period(db, user_id)
    account = credits.get_or_create_account(db, user_id)
    packages = (
        db.query(models.TopupPackage)
        .filter_by(is_active=True)
        .order_by(models.TopupPackage.sort_order.asc())
        .all()
    )
    public_plans = (
        db.query(models.Plan)
        .filter_by(is_public=True)
        .order_by(models.Plan.price_monthly_cents.asc())
        .all()
    )
    return {
        "plan": plan_view(plan),
        "subscription": subscription_view(subscription),
        "period": period_view(period),
        "wallet": credits.balances(account),
        "credits_total_available": credits.total_available(account),
        "available_plans": [plan_view(p) for p in public_plans],
        "topup_packages": [
            {
                "id": p.id,
                "name": p.name,
                "credits": p.credits,
                "price_cents": p.price_cents,
                "currency": p.currency,
                "validity_months": p.validity_months,
            }
            for p in packages
        ],
        "payment_provider": providers.get_provider().public_config(),
        "credit_pricing": {
            "standard_generation_credits":
                catalogue.standard_generation_credits(),
            "duration_cost_table": catalogue.duration_cost_table(),
            "script_operations": catalogue.SCRIPT_OPERATION_CREDITS,
            "thumbnail_operations":
                catalogue.THUMBNAIL_OPERATION_CREDITS,
        },
    }


def ledger_view(db, user_id: str, *, limit: int = 50,
                offset: int = 0) -> dict:
    query = (
        db.query(models.CreditLedgerEntry)
        .filter_by(user_id=user_id)
        .order_by(models.CreditLedgerEntry.created_at.desc())
    )
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return {
        "total": total,
        "entries": [
            {
                "id": e.id,
                "entry_type": e.entry_type,
                "bucket": e.bucket,
                "amount": e.amount,
                "reason": e.reason,
                "source": e.source,
                "project_id": e.project_id,
                "run_id": e.run_id,
                "payment_id": e.payment_id,
                "expires_at": _dt(e.expires_at),
                "created_at": _dt(e.created_at),
                "balances_after": {
                    "subscription": e.subscription_balance_after,
                    "purchased": e.purchased_balance_after,
                    "promo": e.promo_balance_after,
                },
            }
            for e in rows
        ],
    }


def payments_view(db, user_id: str, *, limit: int = 50,
                  offset: int = 0) -> dict:
    query = (
        db.query(models.Payment)
        .filter_by(user_id=user_id)
        .order_by(models.Payment.created_at.desc())
    )
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return {
        "total": total,
        "payments": [
            {
                "id": p.id,
                "provider": p.provider,
                "kind": p.kind,
                "status": p.status,
                "amount_cents": p.amount_cents,
                "currency": p.currency,
                "refunded_amount_cents": p.refunded_amount_cents,
                "plan_id": p.plan_id,
                "topup_package_id": p.topup_package_id,
                "created_at": _dt(p.created_at),
            }
            for p in rows
        ],
    }
