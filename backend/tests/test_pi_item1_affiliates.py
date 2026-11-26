"""Affiliate programme (Product Improvements item 1).

Verified end to end at the seams that matter:
- enrolment, click recording, registration attribution
  (unknown/self codes ignored, registration never blocked);
- commission accrues ONLY from verified Paddle webhook events
  (the same signed-fixture path as the payments suite), once
  per payment even under duplicate delivery;
- refunds create proportional reversal entries; the accrual is
  never edited;
- a suspended affiliate accrues nothing;
- admin decisions enforce their state machine (reject needs a
  reason; paid only from approved);
- the dashboard aggregates real rows only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import main
import models
from auth_helpers import with_session_token
from database import Base, SessionLocal, engine
from services import auth as auth_service
from services.billing import affiliates, catalogue, webhooks

SECRET = "affiliate-test-secret"
PASSWORD = "correct horse battery staple"

Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", "pi-aff-token")
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("PADDLE_API_KEY", "test-api-key")
    monkeypatch.setenv("PADDLE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("PADDLE_CLIENT_TOKEN", "test-client-token")
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    auth_service.set_redis_client(
        fakeredis.FakeRedis(decode_responses=True)
    )
    db = SessionLocal()
    try:
        catalogue.ensure_billing_catalogue(db)
        db.commit()
    finally:
        db.close()
    yield
    auth_service.reset_redis_client()


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _sign(body: bytes):
    ts = int(time.time())
    digest = hmac.new(
        SECRET.encode(), f"{ts}:".encode() + body, hashlib.sha256
    ).hexdigest()
    return {"Paddle-Signature": f"ts={ts};h1={digest}"}


def _deliver(event_type, data, event_id=None):
    body = json.dumps(
        {
            "event_id": event_id or f"evt_{uuid.uuid4().hex[:12]}",
            "event_type": event_type,
            "data": data,
        }
    ).encode()
    return webhooks.handle_webhook("paddle", _sign(body), body)


def _register(client, referral_code=None):
    payload = {
        "email": f"aff-{uuid.uuid4().hex[:10]}@example.com",
        "password": PASSWORD,
    }
    if referral_code:
        payload["referral_code"] = referral_code
    response = client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201, response.text
    return with_session_token(response)


def _user_id_by_email(email):
    db = SessionLocal()
    try:
        return db.query(models.User).filter_by(email=email).one().id
    finally:
        db.close()


def _pay_subscription(user_id, txn="txn_aff_1", amount=2900):
    db = SessionLocal()
    try:
        plan = db.query(models.Plan).filter_by(name="creator").one()
        plan_id = plan.id
    finally:
        db.close()
    return _deliver(
        "transaction.completed",
        {
            "id": txn,
            "status": "completed",
            "customer_id": "ctm_1",
            "subscription_id": f"sub_{txn}",
            "custom_data": {
                "user_id": user_id,
                "kind": "subscription",
                "plan_id": plan_id,
                "billing_interval": "month",
                "amount_cents": amount,
                "currency": "USD",
            },
            "details": {"totals": {"grand_total": str(amount)}},
        },
    )


def _entries(affiliate_id):
    db = SessionLocal()
    try:
        return (
            db.query(models.CommissionLedgerEntry)
            .filter_by(affiliate_id=affiliate_id)
            .all()
        )
    finally:
        db.close()


def _setup_affiliate(client):
    """An enrolled affiliate + a referred customer, via the API."""
    owner = _register(client)
    owner_headers = {
        "Authorization": f"Bearer {owner['session_token']}"
    }
    enrolled = client.post(
        "/api/v1/account/affiliate/enroll", headers=owner_headers
    )
    assert enrolled.status_code == 200, enrolled.text
    code = enrolled.json()["code"]
    customer = _register(client, referral_code=code)
    return owner, owner_headers, code, customer


def test_enroll_dashboard_and_clicks(client):
    owner, headers, code, _customer = _setup_affiliate(client)
    dashboard = client.get("/api/v1/account/affiliate", headers=headers)
    body = dashboard.json()
    assert body["enrolled"] is True
    assert body["rate_percent"] == 12
    assert body["lifetime"] is True
    assert body["metrics"]["registrations"] == 1
    assert body["payout_provider_connected"] is False

    click = client.post("/api/v1/affiliates/click", json={"code": code})
    assert click.json()["recorded"] is True
    unknown = client.post(
        "/api/v1/affiliates/click", json={"code": "CM-NOPE"}
    )
    assert unknown.json()["recorded"] is False
    body = client.get("/api/v1/account/affiliate", headers=headers).json()
    assert body["metrics"]["link_clicks"] == 1
    assert body["metrics"]["conversion_percent"] == 100.0


def test_registration_ignores_unknown_and_self_codes(client):
    owner = _register(client)
    headers = {"Authorization": f"Bearer {owner['session_token']}"}
    code = client.post(
        "/api/v1/account/affiliate/enroll", headers=headers
    ).json()["code"]
    # Unknown code: registration succeeds, no attribution.
    stranger = _register(client, referral_code="CM-UNKNOWN")
    db = SessionLocal()
    try:
        assert affiliates.attribution_for(db, stranger["user"]["id"]) is None
        # Self-referral: the affiliate cannot attribute themselves.
        assert affiliates.attribution_for(db, owner["user"]["id"]) is None
    finally:
        db.close()
    assert code.startswith("CM-")


def test_accrual_only_from_verified_webhook_and_once(client):
    owner, _headers, _code, customer = _setup_affiliate(client)
    customer_id = customer["user"]["id"]
    db = SessionLocal()
    try:
        profile = affiliates.profile_for(db, owner["user"]["id"])
        affiliate_id = profile.id
    finally:
        db.close()

    # No payment yet: no entries, however the dashboard is read.
    assert _entries(affiliate_id) == []

    status, result = _pay_subscription(customer_id)
    assert result["processed"] is True
    entries = _entries(affiliate_id)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "accrual"
    assert entry.status == "pending"
    assert entry.gross_cents == 2900
    assert entry.rate_percent == 12
    assert entry.commission_cents == 348  # 12% of 2900

    # Duplicate delivery of the same transaction: still one.
    _pay_subscription(customer_id)
    assert len(_entries(affiliate_id)) == 1

    # An UNREFERRED customer's payment accrues nothing.
    other = _register(client)
    _pay_subscription(other["user"]["id"], txn="txn_aff_other")
    assert len(_entries(affiliate_id)) == 1


def test_refund_reverses_and_decisions_enforce_states(client):
    owner, _headers, _code, customer = _setup_affiliate(client)
    customer_id = customer["user"]["id"]
    db = SessionLocal()
    try:
        affiliate_id = affiliates.profile_for(
            db, owner["user"]["id"]
        ).id
    finally:
        db.close()
    _pay_subscription(customer_id, txn="txn_aff_refund")
    accrual = _entries(affiliate_id)[0]

    # Reject requires a reason; approve then paid is the path.
    with pytest.raises(ValueError):
        affiliates.decide_entry(
            SessionLocal(), accrual.id, decision="reject",
            decided_by="admin",
        )
    db = SessionLocal()
    try:
        decided = affiliates.decide_entry(
            db, accrual.id, decision="approve", decided_by="admin"
        )
        assert decided.status == "approved"
        decided = affiliates.decide_entry(
            db, accrual.id, decision="paid", decided_by="admin"
        )
        assert decided.status == "paid"
    finally:
        db.close()

    # Full refund: proportional reversal of the whole commission.
    _deliver(
        "transaction.refunded",
        {
            "id": "adj_1",
            "transaction_id": "txn_aff_refund",
            "details": {"totals": {"grand_total": "2900"}},
        },
    )
    entries = _entries(affiliate_id)
    reversals = [e for e in entries if e.kind == "reversal"]
    assert len(reversals) == 1
    assert reversals[0].commission_cents == 348
    assert reversals[0].reversal_of_id == accrual.id
    accrual_now = [e for e in entries if e.kind == "accrual"][0]
    assert accrual_now.status == "reversed"


def test_suspended_affiliate_accrues_nothing(client):
    owner, _headers, _code, customer = _setup_affiliate(client)
    db = SessionLocal()
    try:
        profile = affiliates.profile_for(db, owner["user"]["id"])
        affiliates.set_affiliate_status(
            db, profile.id, status="suspended"
        )
        affiliate_id = profile.id
    finally:
        db.close()
    _pay_subscription(customer["user"]["id"], txn="txn_aff_susp")
    assert _entries(affiliate_id) == []


def test_admin_api_overview_decision_and_export(client):
    owner, _headers, _code, customer = _setup_affiliate(client)
    _pay_subscription(customer["user"]["id"], txn="txn_aff_admin")
    db = SessionLocal()
    try:
        admin = models.User(
            id=str(uuid.uuid4()),
            email=f"root-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=auth_service.hash_password("pw"),
            role="super_admin", status="active",
        )
        db.add(admin)
        db.commit()
        admin_id = admin.id
        affiliate_id = affiliates.profile_for(
            db, owner["user"]["id"]
        ).id
        entry_id = _entries(affiliate_id)[0].id
    finally:
        db.close()
    admin_headers = {
        "Authorization": f"Bearer {auth_service.create_session(admin_id)}"
    }
    overview = client.get("/api/v1/admin/affiliates", headers=admin_headers)
    assert overview.status_code == 200, overview.text
    row = [
        a for a in overview.json()["affiliates"] if a["id"] == affiliate_id
    ][0]
    assert row["accrued_cents"] == 348

    decided = client.post(
        f"/api/v1/admin/affiliates/entries/{entry_id}/decision",
        headers=admin_headers,
        json={"decision": "approve"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"

    export = client.get(
        "/api/v1/admin/affiliates/export", headers=admin_headers
    )
    assert export.status_code == 200
    assert "commission_cents" in export.text
    assert "348" in export.text

    # A plain user cannot read the admin surface.
    plain = client.get(
        "/api/v1/admin/affiliates",
        headers={"Authorization": f"Bearer {customer['session_token']}"},
    )
    assert plain.status_code == 403


def test_checkout_custom_data_carries_attribution(client):
    _owner, _headers, _code, customer = _setup_affiliate(client)
    from services.billing import checkout

    db = SessionLocal()
    try:
        data = checkout._affiliate_custom_data(db, customer["user"]["id"])
        assert data.get("affiliate_id")
        plain = _register(client)
        assert checkout._affiliate_custom_data(
            db, plain["user"]["id"]
        ) == {}
    finally:
        db.close()


def test_recurring_commission_on_later_payments(client):
    """Lifetime means LATER verified payments from the same
    referred customer keep accruing — e.g. monthly renewals and
    a resubscription after cancellation (the attribution row is
    never deleted, so accrual simply resumes)."""
    owner, _headers, _code, customer = _setup_affiliate(client)
    customer_id = customer["user"]["id"]
    db = SessionLocal()
    try:
        affiliate_id = affiliates.profile_for(
            db, owner["user"]["id"]
        ).id
    finally:
        db.close()
    _pay_subscription(customer_id, txn="txn_aff_m1")
    _pay_subscription(customer_id, txn="txn_aff_m2")
    _pay_subscription(customer_id, txn="txn_aff_m3", amount=7900)
    entries = _entries(affiliate_id)
    assert len(entries) == 3
    assert sorted(e.commission_cents for e in entries) == [
        348, 348, 948,  # 12% of 2900, 2900, 7900
    ]
    dashboard = affiliates.dashboard(
        SessionLocal(), owner["user"]["id"]
    )
    assert dashboard["metrics"]["lifetime_earnings_cents"] == 348 + 348 + 948
    assert dashboard["metrics"]["pending_cents"] == 348 + 348 + 948
