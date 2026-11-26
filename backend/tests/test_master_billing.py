"""Master Implementation — pricing restructure proofs
(founder directive 2026-11-26 ~10:25 WAT).

The counting rule, stated in services/billing/catalogue.py and
enforced at the run-ledger choke point:

- every delivered video counts as exactly 1 toward the plan's
  monthly count (11 / 31 / 61), regardless of duration;
- credits always charge the actual delivered minutes at the
  tier rate — so for videos <= 15 minutes the COUNT binds
  first, and for longer videos the WALLET binds first.

Plus: settlement charges delivered (not estimated) duration and
releases the difference; the count consumption is atomic across
sessions; script/thumbnail overflow prices are exactly 3 / 5
and included allowances never touch the wallet. (Quality
rejection finishes runs as "failed" at the quality gate, so it
follows the same full-restoration contract proven here and in
test_phase8_generation_billing.py.)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

import models
from database import Base, SessionLocal, engine
from services import run_ledger
from services.billing import catalogue, credits, operations, periods
from services.billing.generation_billing import BillingRejected

Base.metadata.create_all(bind=engine)

SCRIPT_5MIN = " ".join(["word"] * 725)    # 300s at 145 wpm
SCRIPT_15MIN = " ".join(["word"] * 2175)  # 900s
SCRIPT_30MIN = " ".join(["word"] * 4350)  # 1800s


@pytest.fixture()
def db():
    session = SessionLocal()
    catalogue.ensure_billing_catalogue(session)
    session.commit()
    yield session
    session.close()


def _user_on(db, plan_name="creator"):
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


def _project(db, owner_id, script):
    project = models.Project(
        id=str(uuid.uuid4()), script=script, owner_id=owner_id
    )
    db.add(project)
    db.commit()
    return project


def _available(db, user_id):
    account = credits.get_or_create_account(db, user_id)
    db.commit()
    return credits.total_available(account)


def _deliver(db, user, script, seconds):
    project = _project(db, user.id, script)
    submission = run_ledger.submit_production(
        project.id, project.script, options={}
    )
    assert run_ledger.finish_run(
        submission["run_id"], "completed",
        execution={"duration_seconds": seconds},
    )
    db.expire_all()
    return submission


def test_count_binds_first_for_short_videos(db):
    # 11 x 5-minute videos on Creator cost 11 x 30 = 330 of the
    # 990 credits — yet the 12th is refused: the COUNT binds.
    user = _user_on(db)
    for _ in range(11):
        _deliver(db, user, SCRIPT_5MIN, 300)
    assert _available(db, user.id) == 990 - 330
    period = periods.current_period(db, user.id)
    assert period.generations_used == 11
    project = _project(db, user.id, SCRIPT_5MIN)
    with pytest.raises(BillingRejected) as excinfo:
        run_ledger.submit_production(
            project.id, project.script, options={}
        )
    assert excinfo.value.error_code == "video_limit_reached"


def test_wallet_binds_first_for_long_videos(db):
    # 30-minute videos on Creator cost 180 each: five fit the
    # wallet (900), the sixth is refused on credits with only
    # 5 of the 11 count units used — the WALLET binds.
    user = _user_on(db)
    for _ in range(5):
        _deliver(db, user, SCRIPT_30MIN, 1800)
    assert _available(db, user.id) == 990 - 900
    period = periods.current_period(db, user.id)
    assert period.generations_used == 5
    project = _project(db, user.id, SCRIPT_30MIN)
    with pytest.raises(BillingRejected) as excinfo:
        run_ledger.submit_production(
            project.id, project.script, options={}
        )
    assert excinfo.value.error_code == "insufficient_credits"
    db.expire_all()
    period = periods.current_period(db, user.id)
    assert period.generations_used == 5  # refused attempt took none


def test_settlement_charges_delivered_not_estimated(db):
    # Reserved on a 15-minute estimate (90); delivered 10 minutes
    # -> charged 60, the undelivered 30 released back.
    user = _user_on(db)
    project = _project(db, user.id, SCRIPT_15MIN)
    submission = run_ledger.submit_production(
        project.id, project.script, options={}
    )
    reservation = (
        db.query(models.GenerationReservation)
        .filter_by(run_id=submission["run_id"])
        .one()
    )
    assert reservation.credits_reserved == 90
    assert run_ledger.finish_run(
        submission["run_id"], "completed",
        execution={"duration_seconds": 600},
    )
    db.expire_all()
    reservation = (
        db.query(models.GenerationReservation)
        .filter_by(run_id=submission["run_id"])
        .one()
    )
    assert reservation.status == "settled"
    assert reservation.charged_credits == 60
    assert _available(db, user.id) == 990 - 60


def test_count_consumption_is_atomic_across_sessions(db):
    # Two sessions hold stale snapshots of the same period with
    # one unit left; exactly one consumption may succeed. The
    # guard lives in the conditional UPDATE, not in Python state.
    user = _user_on(db)
    period = periods.ensure_current_period(db, user.id)
    period.generations_used = period.generations_granted - 1
    db.commit()
    period_id = period.id

    session_a = SessionLocal()
    session_b = SessionLocal()
    try:
        stale_a = session_a.get(models.BillingPeriod, period_id)
        stale_b = session_b.get(models.BillingPeriod, period_id)
        assert periods.consume_allowance(
            session_a, stale_a, "generations"
        ) is True
        session_a.commit()
        assert periods.consume_allowance(
            session_b, stale_b, "generations"
        ) is False
        session_b.rollback()
    finally:
        session_a.close()
        session_b.close()
    db.expire_all()
    period = periods.current_period(db, user.id)
    assert period.generations_used == period.generations_granted


def test_thumbnail_overflow_prices_and_wallet_separation(db):
    user = _user_on(db)
    # Included thumbnails (11) never touch the wallet.
    receipt = operations.begin_operation(
        user.id, "thumbnails", "compose"
    )
    assert receipt["via"] == "allowance"
    assert receipt["credits_charged"] == 0
    assert _available(db, user.id) == 990
    # Beyond the allowance: a thumbnail generation costs exactly
    # 5 credits; the headline assist costs exactly 3.
    period = periods.ensure_current_period(db, user.id)
    period.thumbnails_used = period.thumbnails_granted
    db.commit()
    receipt = operations.begin_operation(
        user.id, "thumbnails", "compose"
    )
    assert receipt["via"] == "credits"
    assert receipt["credits_charged"] == 5
    assert _available(db, user.id) == 990 - 5
    receipt = operations.begin_operation(
        user.id, "thumbnails", "headlines"
    )
    assert receipt["credits_charged"] == 3
    assert _available(db, user.id) == 990 - 5 - 3


def test_every_script_operation_costs_three_beyond_allowance(db):
    user = _user_on(db)
    period = periods.ensure_current_period(db, user.id)
    period.scripts_used = period.scripts_granted
    db.commit()
    for operation in catalogue.SCRIPT_OPERATION_CREDITS:
        receipt = operations.begin_operation(
            user.id, "scripts", operation
        )
        assert receipt["via"] == "credits", operation
        assert receipt["credits_charged"] == 3, operation
        operations.refund_operation(user.id, receipt)
    assert _available(db, user.id) == 990  # all refunded
