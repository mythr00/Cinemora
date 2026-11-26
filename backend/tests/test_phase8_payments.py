"""Phase 8 slice 7 — payments (§§10, 15, 16, 18).

Signed-fixture webhook tests (fixtures are constructed in-test
to Paddle's documented payload shapes; no live credentials):
signature verification, the exactly-once inbox, top-up grants
with 12-month expiry, subscription activation, past-due and
cancellation state, refunds without automatic clawback, the
honest unconfigured state, and local plan change/cancel rules.
"""

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import models
from database import Base, SessionLocal, engine
from services.billing import catalogue, checkout, credits, providers, webhooks
from services.billing.generation_billing import BillingRejected

Base.metadata.create_all(bind=engine)

SECRET = "test-webhook-secret"


@pytest.fixture()
def db():
    session = SessionLocal()
    catalogue.ensure_billing_catalogue(session)
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("PADDLE_API_KEY", "test-api-key")
    monkeypatch.setenv("PADDLE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("PADDLE_CLIENT_TOKEN", "test-client-token")


def _sign(body: bytes, secret: str = SECRET, ts: int | None = None):
    ts = ts if ts is not None else int(time.time())
    digest = hmac.new(
        secret.encode(), f"{ts}:".encode() + body, hashlib.sha256
    ).hexdigest()
    return {"Paddle-Signature": f"ts={ts};h1={digest}"}


def _user(db):
    user = models.User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@example.invalid",
        password_hash="x",
    )
    db.add(user)
    db.commit()
    return user


def _event(event_type, data, event_id=None):
    return json.dumps(
        {
            "event_id": event_id or f"evt_{uuid.uuid4().hex[:12]}",
            "event_type": event_type,
            "data": data,
        }
    ).encode()


def _deliver(body: bytes):
    return webhooks.handle_webhook("paddle", _sign(body), body)


def test_unconfigured_provider_refuses_honestly(db, monkeypatch):
    monkeypatch.delenv("PADDLE_API_KEY", raising=False)
    monkeypatch.delenv("PADDLE_WEBHOOK_SECRET", raising=False)
    user = _user(db)
    plan = db.query(models.Plan).filter_by(name="creator").one()
    with pytest.raises(BillingRejected) as excinfo:
        checkout.create_subscription_checkout(user.id, plan.id, "month")
    assert excinfo.value.error_code == "payment_provider_not_configured"
    assert excinfo.value.status_code == 503
    status, body = webhooks.handle_webhook(
        "paddle", {}, b"{}"
    )
    assert status == 503
    assert providers.get_provider().is_configured() is False


def test_signature_verification(db, configured):
    provider = providers.get_provider()
    body = b'{"event_id":"e1","event_type":"x","data":{}}'
    assert provider.verify_webhook(_sign(body), body) is True
    assert provider.verify_webhook(_sign(body), body + b" ") is False
    stale = _sign(body, ts=int(time.time()) - 3600)
    assert provider.verify_webhook(stale, body) is False
    status, _body = webhooks.handle_webhook(
        "paddle", {"Paddle-Signature": "ts=1;h1=bad"}, body
    )
    assert status == 401


def test_topup_webhook_grants_credits_once(db, configured):
    user = _user(db)
    package = (
        db.query(models.TopupPackage).filter_by(name="topup-300").one()
    )
    body = _event(
        "transaction.completed",
        {
            "id": "txn_topup_1",
            "status": "completed",
            "customer_id": "ctm_1",
            "custom_data": {
                "user_id": user.id,
                "kind": "topup",
                "package_id": package.id,
                "amount_cents": 1800,
                "currency": "USD",
            },
            "details": {"totals": {"grand_total": "1800"}},
        },
        event_id="evt_topup_1",
    )
    status, result = _deliver(body)
    assert status == 200 and result["processed"] is True

    db.expire_all()
    account = credits.get_or_create_account(db, user.id)
    assert account.purchased_available == 300
    payment = (
        db.query(models.Payment)
        .filter_by(provider_transaction_id="txn_topup_1")
        .one()
    )
    assert payment.status == "completed"
    assert payment.amount_cents == 1800
    entry = (
        db.query(models.CreditLedgerEntry)
        .filter_by(entry_type="allocation_purchase", user_id=user.id)
        .one()
    )
    assert entry.expires_at is not None
    expires = entry.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    assert expires > datetime.now(timezone.utc) + timedelta(days=300)

    # Duplicate delivery: inbox dedupes, no second grant.
    status, result = _deliver(body)
    assert result["duplicate"] is True
    db.expire_all()
    assert credits.get_or_create_account(db, user.id).purchased_available == 300


def test_subscription_webhook_activates_and_allocates(db, configured):
    user = _user(db)
    plan = db.query(models.Plan).filter_by(name="creator").one()
    body = _event(
        "transaction.completed",
        {
            "id": "txn_sub_1",
            "status": "completed",
            "customer_id": "ctm_9",
            "subscription_id": "sub_123",
            "custom_data": {
                "user_id": user.id,
                "kind": "subscription",
                "plan_id": plan.id,
                "billing_interval": "month",
                "amount_cents": 2800,
                "currency": "USD",
            },
            "details": {"totals": {"grand_total": "2800"}},
        },
        event_id="evt_sub_1",
    )
    status, result = _deliver(body)
    assert result["processed"] is True
    db.expire_all()
    sub = (
        db.query(models.UserSubscription)
        .filter_by(user_id=user.id, status="active")
        .one()
    )
    assert sub.external_id == "sub_123"
    assert sub.provider_customer_id == "ctm_9"
    assert sub.billing_provider == "paddle"
    # The first benefit period allocated the monthly credits.
    account = credits.get_or_create_account(db, user.id)
    assert account.subscription_available == 990


def test_subscription_state_events(db, configured):
    user = _user(db)
    plan = db.query(models.Plan).filter_by(name="creator").one()
    sub = models.UserSubscription(
        id=str(uuid.uuid4()), user_id=user.id, plan_id=plan.id,
        status="active", billing_provider="paddle",
        external_id="sub_777",
    )
    db.add(sub)
    db.commit()

    body = _event(
        "subscription.past_due", {"id": "sub_777"},
        event_id="evt_pd_1",
    )
    _deliver(body)
    db.expire_all()
    assert db.get(models.UserSubscription, sub.id).status == "past_due"

    future = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
    body = _event(
        "subscription.canceled",
        {
            "id": "sub_777",
            "scheduled_change": {
                "action": "cancel", "effective_at": future,
            },
        },
        event_id="evt_cx_1",
    )
    _deliver(body)
    db.expire_all()
    sub = db.get(models.UserSubscription, sub.id)
    # Scheduled cancellation: access continues to period end.
    assert sub.status == "past_due"
    assert sub.cancel_at_period_end is True


def test_refund_updates_payment_without_clawback(db, configured):
    user = _user(db)
    package = (
        db.query(models.TopupPackage).filter_by(name="topup-120").one()
    )
    paid = _event(
        "transaction.completed",
        {
            "id": "txn_ref_1",
            "custom_data": {
                "user_id": user.id, "kind": "topup",
                "package_id": package.id, "amount_cents": 800,
                "currency": "USD",
            },
            "details": {"totals": {"grand_total": "800"}},
        },
        event_id="evt_ref_paid",
    )
    _deliver(paid)
    refund = _event(
        "transaction.refunded",
        {
            "id": "txn_ref_1",
            "details": {"totals": {"grand_total": "800"}},
        },
        event_id="evt_ref_1",
    )
    status, result = _deliver(refund)
    assert result["processed"] is True
    db.expire_all()
    payment = (
        db.query(models.Payment)
        .filter_by(provider_transaction_id="txn_ref_1")
        .one()
    )
    assert payment.status == "refunded"
    assert payment.refunded_amount_cents == 800
    # Policy: no automatic clawback — the credits stay until an
    # admin decides otherwise (audited adjustment).
    account = credits.get_or_create_account(db, user.id)
    assert account.purchased_available == 120


def test_local_upgrade_and_downgrade_rules(db):
    # Operator-granted subscription (no provider relationship):
    # upgrade applies immediately with pro-rated credits,
    # downgrade schedules at the boundary.
    user = _user(db)
    creator = db.query(models.Plan).filter_by(name="creator").one()
    studio = db.query(models.Plan).filter_by(name="studio").one()
    now = datetime.now(timezone.utc)
    sub = models.UserSubscription(
        id=str(uuid.uuid4()), user_id=user.id, plan_id=creator.id,
        status="active", current_period_start=now - timedelta(days=15),
        current_period_end=now + timedelta(days=15),
    )
    db.add(sub)
    db.commit()

    result = checkout.change_subscription(user.id, studio.id)
    assert result["effective"] == "immediate"
    db.expire_all()
    sub = db.get(models.UserSubscription, sub.id)
    assert sub.plan_id == studio.id
    account = credits.get_or_create_account(db, user.id)
    # Half the period remains: ~half of the 1335 difference
    # (Studio 2325 - Creator 990, restructure figures).
    assert 600 < account.subscription_available <= 667

    result = checkout.change_subscription(user.id, creator.id)
    assert result["effective"] == "next_period"
    db.expire_all()
    sub = db.get(models.UserSubscription, sub.id)
    assert sub.plan_id == studio.id  # unchanged until boundary
    assert sub.pending_plan_id == creator.id

    result = checkout.cancel_subscription(user.id)
    assert result["cancelled"] is True
    assert result["access_until"] is not None
    db.expire_all()
    assert db.get(
        models.UserSubscription, sub.id
    ).cancel_at_period_end is True
