"""Phase 8 slice 4 — generation billing hooks (§§5, 6, 16, 18).

End-to-end through the REAL choke points (run_ledger
.submit_production / finish_run / request_cancel /
submit_revision_render): reservation, entitlement consumption,
settlement, full restoration on failure/cancel, fail-closed
rejection, the admin zero-charge path, the legacy-plan exemption,
and the §18 allowance proof — every plan delivers exactly its
advertised number of 15-minute documentaries on entitlements.
"""

import uuid
from datetime import datetime, timezone

import pytest

import models
from database import Base, SessionLocal, engine
from services import run_ledger
from services.billing import catalogue, credits, periods
from services.billing.generation_billing import BillingRejected

Base.metadata.create_all(bind=engine)

SCRIPT_15MIN = " ".join(["word"] * 2175)  # 900s at 145 wpm
SCRIPT_30MIN = " ".join(["word"] * 4350)  # 1800s


def _make_user(db, plan_name=None, *, status="active",
               period_bounds=True):
    user = models.User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@example.invalid",
        password_hash="x",
    )
    db.add(user)
    db.flush()
    if plan_name is not None:
        plan = db.query(models.Plan).filter_by(name=plan_name).one()
        now = datetime.now(timezone.utc)
        start, end = periods.month_bounds(now) if period_bounds else (None, None)
        db.add(
            models.UserSubscription(
                id=str(uuid.uuid4()),
                user_id=user.id,
                plan_id=plan.id,
                status=status,
                current_period_start=start,
                current_period_end=end,
            )
        )
    db.flush()
    return user


def _make_project(db, owner_id, script=SCRIPT_15MIN):
    project = models.Project(
        id=str(uuid.uuid4()), script=script, owner_id=owner_id
    )
    db.add(project)
    db.flush()
    return project


@pytest.fixture()
def db():
    session = SessionLocal()
    catalogue.ensure_billing_catalogue(session)
    session.commit()
    yield session
    session.close()


def _wallet(db, user_id):
    account = credits.get_or_create_account(db, user_id)
    db.commit()
    return credits.balances(account)


def _reservation_for(db, run_id):
    return (
        db.query(models.GenerationReservation)
        .filter_by(run_id=run_id)
        .one_or_none()
    )


def test_entitlement_generation_settles_free(db):
    user = _make_user(db, "creator")
    project = _make_project(db, user.id)
    db.commit()

    submission = run_ledger.submit_production(
        project.id, project.script, options={}
    )
    assert submission and not submission["deduplicated"]
    reservation = _reservation_for(db, submission["run_id"])
    assert reservation is not None
    assert reservation.entitlement_reserved is True
    assert reservation.credits_reserved == 0

    assert run_ledger.finish_run(submission["run_id"], "completed")
    db.expire_all()
    reservation = _reservation_for(db, submission["run_id"])
    assert reservation.status == "settled"
    assert reservation.charged_credits == 0
    wallet = _wallet(db, user.id)
    assert wallet["subscription"]["available"] == 1350
    assert wallet["lifetime_spent"] == 0


def test_long_generation_charges_excess_minutes_only(db):
    user = _make_user(db, "creator")
    project = _make_project(db, user.id, script=SCRIPT_30MIN)
    db.commit()
    submission = run_ledger.submit_production(
        project.id, project.script, options={}
    )
    reservation = _reservation_for(db, submission["run_id"])
    # Entitlement covers 15 min; the other 15 are reserved in
    # credits at the platform rate (84).
    assert reservation.entitlement_reserved is True
    assert reservation.credits_reserved == 84
    assert run_ledger.finish_run(
        submission["run_id"], "completed",
        execution={"duration_seconds": 1800},
    )
    db.expire_all()
    reservation = _reservation_for(db, submission["run_id"])
    assert reservation.status == "settled"
    assert reservation.charged_credits == 84
    wallet = _wallet(db, user.id)
    assert wallet["subscription"]["available"] == 1350 - 84
    assert wallet["lifetime_spent"] == 84


def test_failed_generation_restores_everything(db):
    user = _make_user(db, "creator")
    project = _make_project(db, user.id, script=SCRIPT_30MIN)
    db.commit()
    submission = run_ledger.submit_production(
        project.id, project.script, options={}
    )
    run_id = submission["run_id"]
    assert run_ledger.finish_run(run_id, "failed", error="boom")
    db.expire_all()
    reservation = _reservation_for(db, run_id)
    assert reservation.status == "released"
    wallet = _wallet(db, user.id)
    assert wallet["subscription"]["available"] == 1350
    assert wallet["subscription"]["reserved"] == 0
    period = periods.current_period(db, user.id)
    assert period.generations_used == 0  # entitlement restored


def test_queued_cancel_restores_reservation(db):
    user = _make_user(db, "creator")
    project = _make_project(db, user.id, script=SCRIPT_30MIN)
    db.commit()
    submission = run_ledger.submit_production(
        project.id, project.script, options={}
    )
    assert run_ledger.request_cancel(submission["run_id"])
    db.expire_all()
    reservation = _reservation_for(db, submission["run_id"])
    assert reservation.status == "released"
    assert _wallet(db, user.id)["subscription"]["available"] == 1350


def test_insufficient_credits_rejects_and_charges_nothing(db):
    user = _make_user(db, "creator")
    project = _make_project(db, user.id)
    db.commit()
    # Drain entitlements and wallet via the period + account.
    period = periods.ensure_current_period(db, user.id)
    period.generations_used = period.generations_granted
    account = credits.get_or_create_account(db, user.id)
    account.subscription_available = 10
    db.commit()

    with pytest.raises(BillingRejected) as excinfo:
        run_ledger.submit_production(project.id, project.script,
                                     options={})
    assert excinfo.value.error_code == "insufficient_credits"
    assert excinfo.value.status_code == 402
    run = run_ledger.latest_run_for(
        project.id, run_ledger.script_sha256(project.script)
    )
    assert run["status"] == "failed"
    assert "insufficient_credits" in (run["error"] or "")
    db.expire_all()
    assert _wallet(db, user.id)["subscription"]["available"] == 10


def test_legacy_plan_user_is_not_metered(db):
    # UPDATED by Product Improvements item 8 (founder directive
    # 2026-11-26, "zero credits must mean zero videos"): the
    # legacy-plan exemption now survives ONLY for staff accounts.
    # An ordinary user on a legacy plan (the seeded 'pro') with
    # zero credits is rejected; a staff user on the same plan
    # keeps the historical unmetered behaviour. The full item-8
    # matrix lives in tests/test_pi_item8_zero_credit.py.
    from scripts.seed_founder import ensure_plans

    ensure_plans(db)
    user = _make_user(db, "pro")
    project = _make_project(db, user.id)
    db.commit()
    with pytest.raises(BillingRejected) as excinfo:
        run_ledger.submit_production(
            project.id, project.script, options={}
        )
    assert excinfo.value.error_code == "insufficient_credits"

    staff = _make_user(db, "pro")
    staff.role = "super_admin"
    staff_project = _make_project(db, staff.id)
    db.commit()
    submission = run_ledger.submit_production(
        staff_project.id, staff_project.script, options={}
    )
    assert submission is not None
    assert _reservation_for(db, submission["run_id"]) is None
    assert run_ledger.finish_run(submission["run_id"], "completed")


def test_admin_source_is_zero_charge(db):
    user = _make_user(db, "creator")
    project = _make_project(db, user.id, script=SCRIPT_30MIN)
    db.commit()
    submission = run_ledger.submit_production(
        project.id, project.script,
        options={"billing_source": "admin"},
    )
    reservation = _reservation_for(db, submission["run_id"])
    assert reservation is not None
    assert reservation.source == "admin"
    assert reservation.credits_reserved == 0
    assert reservation.entitlement_reserved is False
    assert run_ledger.finish_run(submission["run_id"], "completed")
    db.expire_all()
    assert _wallet(db, user.id)["lifetime_spent"] == 0


def test_past_due_subscription_is_rejected(db):
    user = _make_user(db, "creator", status="past_due")
    project = _make_project(db, user.id)
    db.commit()
    with pytest.raises(BillingRejected) as excinfo:
        run_ledger.submit_production(project.id, project.script,
                                     options={})
    assert excinfo.value.error_code == "subscription_past_due"


def test_retry_after_failure_charges_once(db):
    user = _make_user(db, "creator")
    project = _make_project(db, user.id, script=SCRIPT_30MIN)
    db.commit()
    first = run_ledger.submit_production(
        project.id, project.script, options={}
    )
    assert run_ledger.finish_run(first["run_id"], "failed")
    db.expire_all()
    assert _wallet(db, user.id)["subscription"]["available"] == 1350

    retry = run_ledger.submit_production(
        project.id, project.script,
        options={"retry_of": first["run_id"], "retry_attempt": 1},
    )
    assert retry and not retry["deduplicated"]
    reservation = _reservation_for(db, retry["run_id"])
    assert reservation is not None
    assert reservation.root_run_id == first["run_id"]
    assert run_ledger.finish_run(
        retry["run_id"], "completed",
        execution={"duration_seconds": 1800},
    )
    db.expire_all()
    # Exactly one charge across the whole chain: the 84-credit
    # excess of the completed retry.
    assert _wallet(db, user.id)["lifetime_spent"] == 84


def test_revision_render_charges_credits(db):
    user = _make_user(db, "creator")
    project = _make_project(db, user.id)
    db.commit()
    submitted = run_ledger.submit_revision_render(
        project.id, project.script, str(uuid.uuid4()),
        str(uuid.uuid4()),
    )
    assert submitted is not None
    reservation = _reservation_for(db, submitted["run_id"])
    assert reservation is not None
    assert reservation.entitlement_reserved is False
    assert reservation.credits_reserved == 84
    assert run_ledger.finish_run(submitted["run_id"], "completed")
    db.expire_all()
    reservation = _reservation_for(db, submitted["run_id"])
    assert reservation.status == "settled"
    assert reservation.charged_credits == 84


@pytest.mark.parametrize(
    "plan_name,docs",
    [("creator", 11), ("studio", 29), ("agency", 59)],
)
def test_advertised_allowance_is_deliverable(db, plan_name, docs):
    # §18 allowance proof: run exactly the advertised number of
    # standard 15-minute generations; every one must settle on an
    # entitlement at zero credits, and the (docs+1)-th must fall
    # back to credits at the platform rate.
    user = _make_user(db, plan_name)
    db.commit()
    for index in range(docs):
        project = _make_project(db, user.id)
        db.commit()
        submission = run_ledger.submit_production(
            project.id, project.script, options={}
        )
        reservation = _reservation_for(db, submission["run_id"])
        assert reservation.entitlement_reserved is True, (
            plan_name, index,
        )
        assert run_ledger.finish_run(
            submission["run_id"], "completed",
            execution={"duration_seconds": 900},
        )
    db.expire_all()
    wallet = _wallet(db, user.id)
    assert wallet["lifetime_spent"] == 0

    project = _make_project(db, user.id)
    db.commit()
    submission = run_ledger.submit_production(
        project.id, project.script, options={}
    )
    reservation = _reservation_for(db, submission["run_id"])
    assert reservation.entitlement_reserved is False
    assert reservation.credits_reserved == 84
    assert run_ledger.finish_run(
        submission["run_id"], "completed",
        execution={"duration_seconds": 900},
    )
    db.expire_all()
    assert _wallet(db, user.id)["lifetime_spent"] == 84
