"""Phase 8 slice 4 — generation billing hooks (§§5, 6, 16, 18).

End-to-end through the REAL choke points (run_ledger
.submit_production / finish_run / request_cancel /
submit_revision_render): reservation, settlement, full
restoration on failure/cancel, fail-closed rejection, the admin
zero-charge path, the legacy-plan exemption, and the allowance
proof — every plan delivers exactly its advertised number of
15-minute documentaries.

UPDATED for the pricing restructure (founder directive
2026-11-26 ~10:25 WAT): a run reserves one delivered-video
count unit AND credits for the full estimated duration at the
tier rate; settlement charges the full delivered duration; the
count is a hard cap (video_limit_reached), restored on failure.
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


def test_standard_generation_charges_rate_and_count(db):
    user = _make_user(db, "creator")
    project = _make_project(db, user.id)
    db.commit()

    submission = run_ledger.submit_production(
        project.id, project.script, options={}
    )
    assert submission and not submission["deduplicated"]
    reservation = _reservation_for(db, submission["run_id"])
    assert reservation is not None
    # One delivered-video count unit held, and the FULL estimated
    # duration reserved in credits at the Creator rate (15 x 6).
    assert reservation.entitlement_reserved is True
    assert reservation.credits_reserved == 90

    assert run_ledger.finish_run(submission["run_id"], "completed")
    db.expire_all()
    reservation = _reservation_for(db, submission["run_id"])
    assert reservation.status == "settled"
    assert reservation.charged_credits == 90
    wallet = _wallet(db, user.id)
    assert wallet["subscription"]["available"] == 990 - 90
    assert wallet["lifetime_spent"] == 90
    period = periods.current_period(db, user.id)
    assert period.generations_used == 1  # the delivered video counts


def test_long_generation_charges_full_duration(db):
    user = _make_user(db, "creator")
    project = _make_project(db, user.id, script=SCRIPT_30MIN)
    db.commit()
    submission = run_ledger.submit_production(
        project.id, project.script, options={}
    )
    reservation = _reservation_for(db, submission["run_id"])
    # The whole 30 minutes at the Creator rate (30 x 6 = 180);
    # the count unit is a count, never a 15-minute discount.
    assert reservation.entitlement_reserved is True
    assert reservation.credits_reserved == 180
    assert run_ledger.finish_run(
        submission["run_id"], "completed",
        execution={"duration_seconds": 1800},
    )
    db.expire_all()
    reservation = _reservation_for(db, submission["run_id"])
    assert reservation.status == "settled"
    assert reservation.charged_credits == 180
    wallet = _wallet(db, user.id)
    assert wallet["subscription"]["available"] == 990 - 180
    assert wallet["lifetime_spent"] == 180


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
    assert wallet["subscription"]["available"] == 990
    assert wallet["subscription"]["reserved"] == 0
    period = periods.current_period(db, user.id)
    assert period.generations_used == 0  # count unit restored


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
    assert _wallet(db, user.id)["subscription"]["available"] == 990


def test_video_limit_rejects_even_with_credits(db):
    # The delivered-video count is a hard cap, independent of the
    # wallet: with the count exhausted, a fully funded user is
    # still refused, and nothing is charged or double-consumed.
    user = _make_user(db, "creator")
    project = _make_project(db, user.id)
    db.commit()
    period = periods.ensure_current_period(db, user.id)
    period.generations_used = period.generations_granted
    db.commit()

    with pytest.raises(BillingRejected) as excinfo:
        run_ledger.submit_production(project.id, project.script,
                                     options={})
    assert excinfo.value.error_code == "video_limit_reached"
    assert excinfo.value.status_code == 402
    run = run_ledger.latest_run_for(
        project.id, run_ledger.script_sha256(project.script)
    )
    assert run["status"] == "failed"
    assert "video_limit_reached" in (run["error"] or "")
    db.expire_all()
    assert _wallet(db, user.id)["subscription"]["available"] == 990
    period = periods.current_period(db, user.id)
    assert period.generations_used == period.generations_granted


def test_insufficient_credits_rejects_and_releases_slot(db):
    # Count units remain, but the wallet cannot cover the full
    # estimated duration: refused as insufficient_credits, and
    # the count unit taken during the attempt is rolled back —
    # a refused submission never consumes the allowance.
    user = _make_user(db, "creator")
    project = _make_project(db, user.id)
    db.commit()
    periods.ensure_current_period(db, user.id)
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
    period = periods.current_period(db, user.id)
    assert period.generations_used == 0


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
    assert _wallet(db, user.id)["subscription"]["available"] == 990

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
    # Exactly one charge across the whole chain: the completed
    # retry's full 30 minutes at the Creator rate (30 x 6 = 180).
    assert _wallet(db, user.id)["lifetime_spent"] == 180


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
    # Credits-only at the owner's tier rate (15 min x 6 = 90);
    # a revision render never consumes a delivered-video unit.
    assert reservation.credits_reserved == 90
    assert run_ledger.finish_run(submitted["run_id"], "completed")
    db.expire_all()
    reservation = _reservation_for(db, submitted["run_id"])
    assert reservation.status == "settled"
    assert reservation.charged_credits == 90


@pytest.mark.parametrize(
    "plan_name,docs,std_cost,monthly_credits",
    [
        ("creator", 11, 90, 990),
        ("studio", 31, 75, 2325),
        ("agency", 61, 60, 3660),
    ],
)
def test_advertised_allowance_is_deliverable(
    db, plan_name, docs, std_cost, monthly_credits
):
    # Restructure allowance proof: run exactly the advertised
    # number of standard 15-minute generations. Every one
    # consumes one count unit and settles at the tier rate, so
    # after `docs` videos the count AND the wallet are exhausted
    # together (docs x std_cost == monthly_credits), and the
    # (docs+1)-th submission is refused at the video limit.
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
        assert reservation.credits_reserved == std_cost, (
            plan_name, index,
        )
        assert run_ledger.finish_run(
            submission["run_id"], "completed",
            execution={"duration_seconds": 900},
        )
    db.expire_all()
    wallet = _wallet(db, user.id)
    assert wallet["lifetime_spent"] == monthly_credits
    assert wallet["subscription"]["available"] == 0
    period = periods.current_period(db, user.id)
    assert period.generations_used == docs

    project = _make_project(db, user.id)
    db.commit()
    with pytest.raises(BillingRejected) as excinfo:
        run_ledger.submit_production(
            project.id, project.script, options={}
        )
    assert excinfo.value.error_code == "video_limit_reached"
