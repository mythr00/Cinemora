"""Verified webhook processing (Phase 8; directive §§10, 16).

The ONLY path that grants payment benefits. Flow: verify the
provider signature over the raw body -> store the event in the
inbox exactly once (UNIQUE provider+event_id) -> dispatch ->
record processed_at or the processing error on the inbox row.

Handlers trust only server-set custom_data (written by our own
checkout creation) plus verified provider state. Refunds update
the payment record but NEVER claw credits back automatically —
reversing spent value is an audited admin decision (§15).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

import models
from database import SessionLocal
from services.billing import credits, providers
from services.billing import periods as periods_service
from services.billing.lifecycle import _add_month


def handle_webhook(provider_name: str, headers: dict,
                   raw_body: bytes) -> tuple[int, dict]:
    """Transport adapter used by the route. Returns
    (http_status, body)."""
    provider = providers.get_provider()
    if provider.name != provider_name:
        return 404, {"error": "unknown_provider"}
    if not provider.is_configured():
        return 503, {"error": "payment_provider_not_configured"}
    if not provider.verify_webhook(headers, raw_body):
        return 401, {"error": "invalid_signature"}
    try:
        event = provider.parse_event(raw_body)
    except Exception:
        return 400, {"error": "invalid_payload"}
    if not event.get("event_id") or not event.get("event_type"):
        return 400, {"error": "invalid_payload"}
    result = process_event(provider_name, event)
    return 200, result


def process_event(provider_name: str, event: dict) -> dict:
    db = SessionLocal()
    try:
        row = models.PaymentEvent(
            id=str(uuid.uuid4()),
            provider=provider_name,
            event_id=event["event_id"],
            event_type=event["event_type"],
            payload=event.get("data") or {},
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return {"duplicate": True, "processed": False}
        try:
            _dispatch(db, event["event_type"], event.get("data") or {})
            row.processed_at = datetime.now(timezone.utc)
            db.commit()
            return {"duplicate": False, "processed": True}
        except Exception as exc:
            db.rollback()
            row = db.get(models.PaymentEvent, row.id)
            if row is not None:
                row.processing_error = f"{type(exc).__name__}: {exc}"
                db.commit()
            return {
                "duplicate": False,
                "processed": False,
                "error": str(exc),
            }
    finally:
        db.close()


# ---------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------


def _dispatch(db, event_type: str, data: dict) -> None:
    if event_type in ("transaction.completed", "transaction.paid"):
        _on_transaction_paid(db, data)
    elif event_type in (
        "subscription.created",
        "subscription.activated",
        "subscription.updated",
        "subscription.resumed",
    ):
        _on_subscription_state(db, data, status="active")
    elif event_type == "subscription.past_due":
        _on_subscription_state(db, data, status="past_due")
    elif event_type == "subscription.canceled":
        _on_subscription_cancelled(db, data)
    elif event_type == "transaction.payment_failed":
        _on_payment_failed(db, data)
    elif event_type in (
        "transaction.refunded",
        "transaction.partially_refunded",
        "adjustment.created",
    ):
        _on_refund(db, data, event_type)
    # Anything else is recorded in the inbox and deliberately
    # ignored (the provider sends many informational events).


def _custom(data: dict) -> dict:
    return data.get("custom_data") or {}


def _amount_cents(data: dict) -> int | None:
    totals = (data.get("details") or {}).get("totals") or {}
    for key in ("grand_total", "total"):
        value = totals.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return None


def _find_payment(db, provider_name: str, transaction_id: str):
    return (
        db.query(models.Payment)
        .filter_by(
            provider=provider_name,
            provider_transaction_id=transaction_id,
        )
        .one_or_none()
    )


def _on_transaction_paid(db, data: dict) -> None:
    provider_name = providers.get_provider().name
    custom = _custom(data)
    user_id = custom.get("user_id")
    user = db.get(models.User, user_id) if user_id else None
    if user is None:
        raise ValueError("transaction has no resolvable user")
    transaction_id = data.get("id")
    amount = _amount_cents(data)
    if amount is None:
        amount = int(custom.get("amount_cents") or 0)
    kind = custom.get("kind")

    payment = _find_payment(db, provider_name, transaction_id)
    if payment is None:
        payment = models.Payment(
            id=str(uuid.uuid4()),
            user_id=user.id,
            provider=provider_name,
            provider_transaction_id=transaction_id,
            kind=kind or "subscription",
            status="completed",
            amount_cents=amount,
            currency=custom.get("currency") or "USD",
        )
        db.add(payment)
        db.flush()

    if kind == "topup":
        package = db.get(models.TopupPackage, custom.get("package_id"))
        if package is None:
            raise ValueError("topup package not found")
        payment.topup_package_id = package.id
        now = datetime.now(timezone.utc)
        expires = now
        for _ in range(int(package.validity_months or 12)):
            expires = _add_month(expires)
        credits.grant(
            db,
            user.id,
            "purchased",
            int(package.credits),
            entry_type="allocation_purchase",
            idempotency_key=f"{provider_name}:txn:{transaction_id}",
            source="webhook",
            payment_id=payment.id,
            expires_at=expires,
            reason=f"top-up purchase: {package.name}",
        )
        db.commit()
        return

    if kind == "subscription":
        plan = db.get(models.Plan, custom.get("plan_id"))
        if plan is None:
            raise ValueError("subscription plan not found")
        payment.plan_id = plan.id
        _activate_subscription(
            db,
            user=user,
            plan=plan,
            interval=custom.get("billing_interval") or "month",
            external_id=data.get("subscription_id"),
            customer_id=data.get("customer_id"),
            payment=payment,
        )
        db.commit()
        return

    raise ValueError(f"unknown transaction kind {kind!r}")


def _activate_subscription(db, *, user, plan, interval, external_id,
                           customer_id, payment=None):
    """Create or renew the local subscription from verified
    provider state, then open the current benefit period (which
    allocates the monthly credits exactly once)."""
    now = datetime.now(timezone.utc)
    existing = None
    if external_id:
        existing = (
            db.query(models.UserSubscription)
            .filter_by(billing_provider=providers.get_provider().name,
                       external_id=external_id)
            .one_or_none()
        )
    if existing is None:
        existing = (
            db.query(models.UserSubscription)
            .filter_by(user_id=user.id, status="active")
            .one_or_none()
        )
    if existing is not None and (
        external_id is None or existing.external_id == external_id
    ):
        existing.plan_id = plan.id
        existing.status = "active"
        existing.billing_interval = interval
        existing.current_period_start = now
        existing.current_period_end = _add_month(now)
        if external_id:
            existing.external_id = external_id
        if customer_id:
            existing.provider_customer_id = customer_id
        existing.billing_provider = providers.get_provider().name
        subscription = existing
    else:
        if existing is not None:
            existing.status = "cancelled"
            db.flush()
        subscription = models.UserSubscription(
            id=str(uuid.uuid4()),
            user_id=user.id,
            plan_id=plan.id,
            status="active",
            current_period_start=now,
            current_period_end=_add_month(now),
            billing_provider=providers.get_provider().name,
            external_id=external_id,
            billing_interval=interval,
            provider_customer_id=customer_id,
        )
        db.add(subscription)
        db.flush()
    if payment is not None:
        payment.subscription_id = subscription.id
    periods_service.ensure_current_period(db, user.id, now=now)
    return subscription


def _find_subscription(db, data: dict):
    external_id = data.get("id") or data.get("subscription_id")
    if external_id:
        found = (
            db.query(models.UserSubscription)
            .filter_by(external_id=external_id)
            .one_or_none()
        )
        if found is not None:
            return found
    custom = _custom(data)
    user_id = custom.get("user_id")
    if user_id:
        return (
            db.query(models.UserSubscription)
            .filter_by(user_id=user_id, status="active")
            .one_or_none()
        )
    return None


def _on_subscription_state(db, data: dict, *, status: str) -> None:
    subscription = _find_subscription(db, data)
    if subscription is None:
        # A subscription event for a subscription we have not
        # recorded (e.g. the transaction event arrives later) —
        # recorded in the inbox; nothing to apply yet.
        return
    subscription.status = status
    period = data.get("current_billing_period") or {}
    if period.get("starts_at"):
        subscription.current_period_start = _parse_dt(period["starts_at"])
    if period.get("ends_at"):
        subscription.current_period_end = _parse_dt(period["ends_at"])
    scheduled = data.get("scheduled_change")
    if scheduled and scheduled.get("action") == "cancel":
        subscription.cancel_at_period_end = True
    elif scheduled is None:
        subscription.cancel_at_period_end = False
    if data.get("customer_id"):
        subscription.provider_customer_id = data["customer_id"]
    db.commit()


def _on_subscription_cancelled(db, data: dict) -> None:
    subscription = _find_subscription(db, data)
    if subscription is None:
        return
    scheduled = data.get("scheduled_change") or {}
    effective_at = scheduled.get("effective_at")
    if effective_at and _parse_dt(effective_at) > datetime.now(
        timezone.utc
    ):
        subscription.cancel_at_period_end = True
    else:
        subscription.status = "cancelled"
        subscription.cancel_at_period_end = False
    db.commit()


def _on_payment_failed(db, data: dict) -> None:
    subscription = _find_subscription(db, data)
    if subscription is not None and subscription.status == "active":
        subscription.status = "past_due"
        db.commit()


def _on_refund(db, data: dict, event_type: str) -> None:
    provider_name = providers.get_provider().name
    transaction_id = data.get("transaction_id") or data.get("id")
    payment = _find_payment(db, provider_name, transaction_id)
    if payment is None:
        return
    amount = _amount_cents(data)
    if amount is not None:
        payment.refunded_amount_cents = amount
    payment.status = (
        "refunded"
        if payment.refunded_amount_cents >= payment.amount_cents
        and payment.amount_cents > 0
        else "partially_refunded"
    )
    db.commit()


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        parsed
        if parsed.tzinfo
        else parsed.replace(tzinfo=timezone.utc)
    )
