"""Phase 8 slice 10 — billing APIs (§§12, 14, 16, 21).

User surface: summary, transactions, payments, notifications
(principal-scoped — a user only ever sees their own records).
Admin surface: plan/package/coupon management, credit
adjustments and promo grants (super_admin, audit-logged),
subscriptions/payments/events views, reports, retention,
pricing rules. Permission ladder enforced: anonymous 401,
plain user 403, admin read-only for commercial mutations.
"""

import uuid
from datetime import datetime, timezone

import fakeredis
import pytest
from fastapi.testclient import TestClient

import main
import models
from database import Base, SessionLocal, engine
from services import auth as auth_service
from services.billing import catalogue, credits, periods

Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    # /api/v1 is closed (503) unless the API token is configured
    # server-side; set it so anonymous requests get the real 401.
    monkeypatch.setenv("CINEMORA_API_TOKEN", "test-operator-token")
    fake = fakeredis.FakeRedis(decode_responses=True)
    auth_service.set_redis_client(fake)
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


def _make_user(role="user"):
    db = SessionLocal()
    try:
        user = models.User(
            id=str(uuid.uuid4()),
            email=f"{uuid.uuid4().hex[:10]}@example.invalid",
            password_hash=auth_service.hash_password("pw"),
            role=role, status="active",
        )
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


def _headers(user_id):
    return {"Authorization": f"Bearer {auth_service.create_session(user_id)}"}


def _subscribe(db, user_id, plan_name="creator"):
    plan = db.query(models.Plan).filter_by(name=plan_name).one()
    now = datetime.now(timezone.utc)
    start, end = periods.month_bounds(now)
    db.add(
        models.UserSubscription(
            id=str(uuid.uuid4()), user_id=user_id, plan_id=plan.id,
            status="active", current_period_start=start,
            current_period_end=end,
        )
    )
    db.commit()
    return plan


def test_user_billing_summary_and_transactions(client):
    user_id = _make_user()
    db = SessionLocal()
    try:
        _subscribe(db, user_id)
        periods.ensure_current_period(db, user_id)
        db.commit()
    finally:
        db.close()

    response = client.get("/api/v1/account/billing",
                          headers=_headers(user_id))
    assert response.status_code == 200
    body = response.json()
    assert body["plan"]["name"] == "creator"
    assert body["plan"]["billing_mode"] == "metered"
    assert body["wallet"]["subscription"]["available"] == 1350
    assert body["period"]["generations"]["remaining"] == 11
    assert len(body["available_plans"]) == 3
    assert len(body["topup_packages"]) == 5
    # Honest unconfigured state (no Paddle env in tests).
    assert body["payment_provider"]["configured"] is False

    response = client.get("/api/v1/account/billing/transactions",
                          headers=_headers(user_id))
    assert response.status_code == 200
    entries = response.json()["entries"]
    assert any(
        e["entry_type"] == "allocation_subscription" for e in entries
    )
    # Anonymous access is refused.
    assert client.get("/api/v1/account/billing").status_code == 401


def test_notifications_flow_and_isolation(client):
    from services.billing import notifications

    owner = _make_user()
    other = _make_user()
    db = SessionLocal()
    try:
        row = notifications.create_notification(
            db, user_id=owner, kind="retention_24h",
            title="Your video will be deleted in 24h",
        )
        db.commit()
        note_id = row.id
    finally:
        db.close()

    response = client.get("/api/v1/account/notifications",
                          headers=_headers(owner))
    assert response.status_code == 200
    assert response.json()["unread_count"] == 1

    # Another user cannot mark it read (404, nothing changes).
    response = client.post(
        f"/api/v1/account/notifications/{note_id}/read",
        headers=_headers(other),
    )
    assert response.status_code == 404

    response = client.post(
        f"/api/v1/account/notifications/{note_id}/read",
        headers=_headers(owner),
    )
    assert response.status_code == 200
    response = client.get("/api/v1/account/notifications",
                          headers=_headers(owner))
    assert response.json()["unread_count"] == 0


def test_admin_billing_permissions_and_audit(client):
    admin = _make_user(role="admin")
    super_admin = _make_user(role="super_admin")
    plain = _make_user()

    # Reads: admin OK, plain user 403.
    assert client.get(
        "/api/v1/admin/billing/plans", headers=_headers(admin)
    ).status_code == 200
    assert client.get(
        "/api/v1/admin/billing/plans", headers=_headers(plain)
    ).status_code == 403

    db = SessionLocal()
    try:
        creator = db.query(models.Plan).filter_by(name="creator").one()
        creator_id = creator.id
    finally:
        db.close()

    # Commercial mutation: plain admin is not enough.
    assert client.post(
        f"/api/v1/admin/billing/plans/{creator_id}",
        json={"price_monthly_cents": 3100},
        headers=_headers(admin),
    ).status_code == 403
    response = client.post(
        f"/api/v1/admin/billing/plans/{creator_id}",
        json={"price_monthly_cents": 3100},
        headers=_headers(super_admin),
    )
    assert response.status_code == 200
    assert response.json()["price_monthly_cents"] == 3100

    db = SessionLocal()
    try:
        assert db.query(models.Plan).get(creator_id).price_monthly_cents == 3100
        audits = (
            db.query(models.AdminAuditLog)
            .filter_by(action="billing.plan_update")
            .all()
        )
        assert any(a.target_id == creator_id for a in audits)
        # Restore the catalogue price for other tests.
        catalogue.ensure_billing_catalogue(db)
        db.commit()
    finally:
        db.close()


def test_admin_credit_adjust_and_promo_grant(client):
    super_admin = _make_user(role="super_admin")
    target = _make_user()

    response = client.post(
        f"/api/v1/admin/billing/users/{target}/credits/adjust",
        json={"bucket": "promo", "delta": 25,
              "reason": "goodwill"},
        headers=_headers(super_admin),
    )
    assert response.status_code == 200
    assert response.json()["wallet"]["promo"]["available"] == 25

    response = client.post(
        "/api/v1/admin/billing/coupons",
        json={"code": f"WELCOME-{uuid.uuid4().hex[:6]}",
              "kind": "credits", "credits_grant": 40},
        headers=_headers(super_admin),
    )
    assert response.status_code == 201
    coupon_id = response.json()["id"]

    response = client.post(
        f"/api/v1/admin/billing/users/{target}/promo-grant",
        json={"coupon_id": coupon_id},
        headers=_headers(super_admin),
    )
    assert response.status_code == 200
    # Second redemption of the same coupon by the same user: 409.
    response = client.post(
        f"/api/v1/admin/billing/users/{target}/promo-grant",
        json={"coupon_id": coupon_id},
        headers=_headers(super_admin),
    )
    assert response.status_code == 409

    db = SessionLocal()
    try:
        account = credits.get_or_create_account(db, target)
        assert account.promo_available == 25 + 40
        audits = (
            db.query(models.AdminAuditLog)
            .filter_by(action="billing.credit_adjust")
            .all()
        )
        assert any(a.target_id == target for a in audits)
    finally:
        db.close()


def test_admin_reports_retention_and_rules(client):
    super_admin = _make_user(role="super_admin")
    headers = _headers(super_admin)

    response = client.get("/api/v1/admin/billing/reports",
                          headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert "revenue" in body and "mrr_contracted_cents" in body

    response = client.get("/api/v1/admin/billing/retention",
                          headers=headers)
    assert response.status_code == 200
    assert "total_videos" in response.json()

    response = client.post(
        "/api/v1/admin/billing/pricing-rules",
        json={
            "provider": "brewkeg", "unit": "per_1k_tokens",
            "rate_microusd": 3000,
            "effective_from": "2026-01-01T00:00:00Z",
        },
        headers=headers,
    )
    assert response.status_code == 201
    rule_id = response.json()["id"]
    response = client.get("/api/v1/admin/billing/pricing-rules",
                          headers=headers)
    assert any(r["id"] == rule_id for r in response.json()["rules"])
    response = client.delete(
        f"/api/v1/admin/billing/pricing-rules/{rule_id}",
        headers=headers,
    )
    assert response.status_code == 200
