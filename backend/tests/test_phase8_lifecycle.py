"""Phase 8 slice 6 — subscription lifecycle (§§6, 15).

Rollover with expiry, rollover-enabled carry-over, cancellation
at period end, past-due expiry, scheduled plan change applied at
the boundary, purchased-lot expiry (FIFO), annual subscriptions
distributing monthly, and reservation reconciliation.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import models
from database import Base, SessionLocal, engine
from services.billing import catalogue, credits, lifecycle, periods

Base.metadata.create_all(bind=engine)

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc)
AFTER = datetime(2026, 11, 2, tzinfo=timezone.utc)


@pytest.fixture()
def db():
    session = SessionLocal()
    catalogue.ensure_billing_catalogue(session)
    session.commit()
    yield session
    session.close()


def _user_with_sub(db, plan_name="creator", **sub_kwargs):
    plan = db.query(models.Plan).filter_by(name=plan_name).one()
    user = models.User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@example.invalid",
        password_hash="x",
    )
    db.add(user)
    db.flush()
    sub = models.UserSubscription(
        id=str(uuid.uuid4()),
        user_id=user.id,
        plan_id=plan.id,
        status=sub_kwargs.pop("status", "active"),
        current_period_start=sub_kwargs.pop(
            "current_period_start", START
        ),
        current_period_end=sub_kwargs.pop(
            "current_period_end", END
        ),
        **sub_kwargs,
    )
    db.add(sub)
    db.commit()
    return user, sub


def _available(db, user_id, bucket="subscription"):
    account = credits.get_or_create_account(db, user_id)
    db.commit()
    return credits.available(account, bucket)


def test_rollover_expires_old_credits_and_allocates_new(db):
    user, sub = _user_with_sub(db)
    periods.ensure_current_period(db, user.id, now=START)
    db.commit()
    credits.spend(db, user.id, 350, idempotency_key="op:spend1")
    db.commit()
    assert _available(db, user.id) == 1000

    summary = lifecycle.run_lifecycle_tick(now=AFTER)
    assert summary["rolled_over"] >= 1
    db.expire_all()
    # Old remainder (1000) expired; the new period allocated a
    # fresh 1350 — not 2350.
    assert _available(db, user.id) == 1350
    sub = db.get(models.UserSubscription, sub.id)
    # SQLite round-trips DateTime(timezone=True) naive; normalise
    # before comparing (the house _aware pattern).
    new_start = sub.current_period_start
    if new_start.tzinfo is None:
        new_start = new_start.replace(tzinfo=timezone.utc)
    assert new_start > START
    open_periods = (
        db.query(models.BillingPeriod)
        .filter_by(user_id=user.id, status="open")
        .all()
    )
    assert len(open_periods) == 1
    assert open_periods[0].generations_granted == 11
    # A second tick changes nothing (idempotent).
    lifecycle.run_lifecycle_tick(now=AFTER)
    db.expire_all()
    assert _available(db, user.id) == 1350


def test_rollover_enabled_carries_credits(db, monkeypatch):
    monkeypatch.setenv("CINEMORA_SUBSCRIPTION_CREDIT_ROLLOVER", "1")
    user, sub = _user_with_sub(db)
    periods.ensure_current_period(db, user.id, now=START)
    db.commit()
    lifecycle.run_lifecycle_tick(now=AFTER)
    db.expire_all()
    assert _available(db, user.id) == 1350 + 1350


def test_cancel_at_period_end(db):
    user, sub = _user_with_sub(db, cancel_at_period_end=True)
    periods.ensure_current_period(db, user.id, now=START)
    db.commit()
    lifecycle.run_lifecycle_tick(now=AFTER)
    db.expire_all()
    sub = db.get(models.UserSubscription, sub.id)
    assert sub.status == "cancelled"
    assert _available(db, user.id) == 0


def test_past_due_expires_after_period_end(db):
    user, sub = _user_with_sub(db, status="past_due")
    periods.ensure_current_period(db, user.id, now=START)
    db.commit()
    lifecycle.run_lifecycle_tick(now=AFTER)
    db.expire_all()
    sub = db.get(models.UserSubscription, sub.id)
    assert sub.status == "expired"
    assert _available(db, user.id) == 0


def test_scheduled_downgrade_applies_at_boundary(db):
    studio = db.query(models.Plan).filter_by(name="studio").one()
    user, sub = _user_with_sub(db, pending_plan_id=studio.id)
    periods.ensure_current_period(db, user.id, now=START)
    db.commit()
    lifecycle.run_lifecycle_tick(now=AFTER)
    db.expire_all()
    sub = db.get(models.UserSubscription, sub.id)
    assert sub.plan_id == studio.id
    assert sub.pending_plan_id is None
    period = periods.current_period(db, user.id)
    # New period grants come from the NEW plan.
    assert period.generations_granted == 29
    assert _available(db, user.id) == 2500


def test_annual_subscription_distributes_monthly(db):
    user, sub = _user_with_sub(db, billing_interval="year")
    periods.ensure_current_period(db, user.id, now=START)
    db.commit()
    lifecycle.run_lifecycle_tick(now=AFTER)
    db.expire_all()
    sub = db.get(models.UserSubscription, sub.id)
    # Benefit period advanced ~one month, not one year.
    assert sub.current_period_start.month in (11,)
    assert sub.current_period_start.year == 2026
    assert _available(db, user.id) == 1350


def test_purchased_lot_expires_fifo(db):
    user, sub = _user_with_sub(db)
    past = datetime(2026, 1, 1, tzinfo=timezone.utc)
    future = datetime(2027, 1, 1, tzinfo=timezone.utc)
    credits.grant(
        db, user.id, "purchased", 100,
        entry_type="allocation_purchase",
        idempotency_key="lot:a", source="webhook", expires_at=past,
    )
    credits.grant(
        db, user.id, "purchased", 100,
        entry_type="allocation_purchase",
        idempotency_key="lot:b", source="webhook",
        expires_at=future,
    )
    credits.spend(db, user.id, 60, idempotency_key="op:spend")
    db.commit()
    assert _available(db, user.id, "purchased") == 140

    lifecycle.run_lifecycle_tick(now=AFTER)
    db.expire_all()
    # FIFO: the 60 spent came from lot A first, so only A's
    # remaining 40 expire; lot B's 100 survive intact.
    assert _available(db, user.id, "purchased") == 100
    # Idempotent: a second tick expires nothing more.
    lifecycle.run_lifecycle_tick(now=AFTER)
    db.expire_all()
    assert _available(db, user.id, "purchased") == 100


def test_stale_reservation_reconciled(db):
    user, sub = _user_with_sub(db)
    periods.ensure_current_period(db, user.id, now=START)
    db.commit()
    project = models.Project(
        id=str(uuid.uuid4()), script="x", owner_id=user.id
    )
    db.add(project)
    run = models.ProductionRun(
        id=str(uuid.uuid4()), project_id=project.id,
        owner_id=user.id, status="failed",
        created_at=START, finished_at=START,
    )
    db.add(run)
    db.flush()
    reservation = models.GenerationReservation(
        id=str(uuid.uuid4()), run_id=run.id, root_run_id=run.id,
        user_id=user.id, project_id=project.id,
        entitlement_reserved=False, credits_reserved=84,
        status="active", source="user",
    )
    db.add(reservation)
    credits.reserve(
        db, user.id, 84, reservation_id=reservation.id,
        run_id=run.id,
    )
    db.commit()
    assert _available(db, user.id) == 1350 - 84

    summary = lifecycle.run_lifecycle_tick(now=START)
    assert summary["reservations_reconciled"] >= 1
    db.expire_all()
    reservation = db.get(
        models.GenerationReservation, reservation.id
    )
    assert reservation.status == "released"
    assert _available(db, user.id) == 1350
