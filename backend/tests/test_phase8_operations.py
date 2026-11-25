"""Phase 8 slice 5 — priced AI operations + estimates (§§5, 7).

Allowance-first charging, credit fallback at the configured
prices, full refund on failure, no charge for legacy users, and
an estimate that matches what generation billing will actually
consume for the same script.
"""

import uuid
from datetime import datetime, timezone

import pytest

import models
from database import Base, SessionLocal, engine
from services.billing import catalogue, credits, operations, periods
from services.billing.generation_billing import BillingRejected

Base.metadata.create_all(bind=engine)


@pytest.fixture()
def db():
    session = SessionLocal()
    catalogue.ensure_billing_catalogue(session)
    session.commit()
    yield session
    session.close()


def _user_on(db, plan_name):
    plan = db.query(models.Plan).filter_by(name=plan_name).one()
    user = models.User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@example.invalid",
        password_hash="x",
    )
    db.add(user)
    db.flush()
    now = datetime.now(timezone.utc)
    start, end = periods.month_bounds(now)
    db.add(
        models.UserSubscription(
            id=str(uuid.uuid4()), user_id=user.id, plan_id=plan.id,
            status="active", current_period_start=start,
            current_period_end=end,
        )
    )
    db.commit()
    return user


def _available(db, user_id):
    account = credits.get_or_create_account(db, user_id)
    db.commit()
    return credits.total_available(account)


def test_allowance_consumed_first(db):
    user = _user_on(db, "creator")
    receipt = operations.begin_operation(user.id, "scripts", "draft")
    assert receipt["via"] == "allowance"
    assert receipt["credits_charged"] == 0
    db.expire_all()
    period = periods.current_period(db, user.id)
    assert period.scripts_used == 1
    assert _available(db, user.id) == 1350  # wallet untouched


def test_allowance_refunded_on_failure(db):
    user = _user_on(db, "creator")
    receipt = operations.begin_operation(
        user.id, "thumbnails", "headlines"
    )
    assert receipt["via"] == "allowance"
    operations.refund_operation(user.id, receipt)
    db.expire_all()
    period = periods.current_period(db, user.id)
    assert period.thumbnails_used == 0


def test_credits_charged_beyond_allowance_and_refunded(db):
    user = _user_on(db, "creator")
    period = periods.ensure_current_period(db, user.id)
    period.scripts_used = period.scripts_granted
    db.commit()

    receipt = operations.begin_operation(user.id, "scripts", "draft")
    assert receipt["via"] == "credits"
    assert receipt["credits_charged"] == 12  # catalogue price
    assert _available(db, user.id) == 1350 - 12

    operations.refund_operation(user.id, receipt)
    assert _available(db, user.id) == 1350
    db.expire_all()
    account = credits.get_or_create_account(db, user.id)
    assert account.lifetime_spent == 0  # refund un-spends


def test_insufficient_for_operation_rejects(db):
    user = _user_on(db, "creator")
    period = periods.ensure_current_period(db, user.id)
    period.thumbnails_used = period.thumbnails_granted
    account = credits.get_or_create_account(db, user.id)
    account.subscription_available = 1  # headlines cost 2
    db.commit()
    with pytest.raises(BillingRejected) as excinfo:
        operations.begin_operation(user.id, "thumbnails", "headlines")
    assert excinfo.value.error_code == "insufficient_credits"


def test_legacy_user_unmetered(db):
    from scripts.seed_founder import ensure_plans

    ensure_plans(db)
    plan = db.query(models.Plan).filter_by(name="pro").one()
    user = models.User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@example.invalid",
        password_hash="x",
    )
    db.add(user)
    db.flush()
    db.add(
        models.UserSubscription(
            id=str(uuid.uuid4()), user_id=user.id, plan_id=plan.id,
            status="active",
        )
    )
    db.commit()
    assert operations.begin_operation(
        user.id, "scripts", "draft"
    ) is None


def test_estimate_matches_generation_terms(db):
    user = _user_on(db, "creator")
    project = models.Project(
        id=str(uuid.uuid4()),
        script=" ".join(["word"] * 2175),  # exactly 15 minutes
        owner_id=user.id,
    )
    db.add(project)
    db.commit()

    estimate = operations.estimate_for_project(user.id, project.id)
    assert estimate["metered"] is True
    assert estimate["estimated_seconds"] == pytest.approx(900.0)
    assert estimate["will_use_entitlement"] is True
    assert estimate["credits_needed"] == 0
    assert estimate["can_afford"] is True

    # Exhaust entitlements: the estimate must switch to the full
    # credit price of the same script.
    period = periods.ensure_current_period(db, user.id)
    period.generations_used = period.generations_granted
    db.commit()
    estimate = operations.estimate_for_project(user.id, project.id)
    assert estimate["will_use_entitlement"] is False
    assert estimate["credits_needed"] == 84


def test_spend_is_idempotent(db):
    user = _user_on(db, "creator")
    periods.ensure_current_period(db, user.id)
    db.commit()
    split1 = credits.spend(
        db, user.id, 50, idempotency_key="op:test-key",
        reason="test",
    )
    db.commit()
    split2 = credits.spend(
        db, user.id, 50, idempotency_key="op:test-key",
        reason="test",
    )
    assert split1 == split2 == {"subscription": 50}
    assert _available(db, user.id) == 1350 - 50
