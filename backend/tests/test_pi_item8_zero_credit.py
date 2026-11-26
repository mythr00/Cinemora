"""Product Improvements item 8 (founder directive 2026-11-26):
zero credits must mean zero videos.

Covers the bypass found in the Phase A audit: registration
assigns every new account an active subscription to the seeded
legacy "free" plan (billing_mode='legacy'), and the generation
billing hooks used to treat every non-metered plan as free
admission — an ordinary user with zero credits generated without
any reservation. Also covers the worker side: a claimed run with
no live reservation must be refused, and the caller-less
begin_run path must reserve or refuse.

Rules under test:
- ordinary tenant + legacy/missing plan + no credits -> 402
  insufficient_credits, run failed, nothing reserved or charged;
- ordinary tenant + legacy plan + enough held credits -> the
  generation proceeds on a credits-only reservation and settles
  at full price; failure restores in full;
- staff (admin/super_admin) keep the operator exemption;
- ownerless projects keep legacy behaviour;
- concurrent submissions cannot spend the same credits twice.
"""

import threading
import uuid
from datetime import datetime, timezone

import pytest

import models
from database import Base, SessionLocal, engine
from services import run_ledger
from services.billing import catalogue, credits
from services.billing.generation_billing import BillingRejected

Base.metadata.create_all(bind=engine)

SCRIPT_15MIN = " ".join(["word"] * 2175)  # 900s at 145 wpm -> 84 credits
COST_15MIN = catalogue.generation_credit_cost(900)


def _make_user(db, plan_name=None, *, role="user", status="active"):
    user = models.User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@example.invalid",
        password_hash="x",
        role=role,
    )
    db.add(user)
    db.flush()
    if plan_name is not None:
        plan = db.query(models.Plan).filter_by(name=plan_name).one()
        now = datetime.now(timezone.utc)
        db.add(
            models.UserSubscription(
                id=str(uuid.uuid4()),
                user_id=user.id,
                plan_id=plan.id,
                status=status,
                current_period_start=now,
                current_period_end=None,
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


def _grant_purchased(db, user_id, amount, key):
    credits.grant(
        db, user_id, "purchased", amount,
        entry_type="topup", idempotency_key=key, source="test",
    )
    db.commit()


def _wallet(db, user_id):
    account = credits.get_or_create_account(db, user_id)
    db.commit()
    return credits.balances(account)


def _total_available(db, user_id):
    wallet = _wallet(db, user_id)
    return sum(
        wallet[bucket]["available"]
        for bucket in ("subscription", "promo", "purchased")
    )


def _reservation_for(db, run_id):
    return (
        db.query(models.GenerationReservation)
        .filter_by(run_id=run_id)
        .one_or_none()
    )


@pytest.fixture()
def db():
    session = SessionLocal()
    catalogue.ensure_billing_catalogue(session)
    from scripts.seed_founder import ensure_plans

    ensure_plans(session)
    session.commit()
    yield session
    session.close()


def _latest_run(db, project):
    return run_ledger.latest_run_for(
        project.id, run_ledger.script_sha256(project.script)
    )


# ---------------------------------------------------------------------
# The founder's reproduction: ordinary account, legacy default
# plan, zero credits.
# ---------------------------------------------------------------------


def test_ordinary_user_legacy_free_plan_zero_credits_rejected(db):
    user = _make_user(db, "free")
    project = _make_project(db, user.id)
    db.commit()
    assert _total_available(db, user.id) == 0

    with pytest.raises(BillingRejected) as excinfo:
        run_ledger.submit_production(project.id, project.script,
                                     options={})
    assert excinfo.value.error_code == "insufficient_credits"
    assert excinfo.value.status_code == 402

    run = _latest_run(db, project)
    assert run["status"] == "failed"
    assert "insufficient_credits" in (run["error"] or "")
    assert _reservation_for(db, run["run_id"]) is None
    assert _total_available(db, user.id) == 0
    assert _wallet(db, user.id)["lifetime_spent"] == 0


def test_ordinary_user_no_plan_zero_credits_rejected(db):
    user = _make_user(db, None)
    project = _make_project(db, user.id)
    db.commit()

    with pytest.raises(BillingRejected) as excinfo:
        run_ledger.submit_production(project.id, project.script,
                                     options={})
    assert excinfo.value.error_code == "insufficient_credits"
    assert _latest_run(db, project)["status"] == "failed"


def test_ordinary_user_legacy_plan_partial_credits_rejected(db):
    user = _make_user(db, "free")
    project = _make_project(db, user.id)
    db.commit()
    _grant_purchased(db, user.id, COST_15MIN - 1, f"topup:{user.id}")

    with pytest.raises(BillingRejected):
        run_ledger.submit_production(project.id, project.script,
                                     options={})
    # Nothing moved: the partial balance is intact, no reservation.
    wallet = _wallet(db, user.id)
    assert wallet["purchased"]["available"] == COST_15MIN - 1
    assert wallet["purchased"]["reserved"] == 0
    assert _latest_run(db, project)["status"] == "failed"


# ---------------------------------------------------------------------
# Credits-only path: held credits pay full price on a legacy plan.
# ---------------------------------------------------------------------


def test_ordinary_user_legacy_plan_with_credits_pays_full_price(db):
    user = _make_user(db, "free")
    project = _make_project(db, user.id)
    db.commit()
    _grant_purchased(db, user.id, COST_15MIN, f"topup:{user.id}")

    submission = run_ledger.submit_production(
        project.id, project.script, options={}
    )
    assert submission and not submission["deduplicated"]
    reservation = _reservation_for(db, submission["run_id"])
    assert reservation is not None
    assert reservation.entitlement_reserved is False
    assert reservation.credits_reserved == COST_15MIN

    assert run_ledger.finish_run(submission["run_id"], "completed")
    db.expire_all()
    reservation = _reservation_for(db, submission["run_id"])
    assert reservation.status == "settled"
    assert reservation.charged_credits == COST_15MIN
    wallet = _wallet(db, user.id)
    assert wallet["purchased"]["available"] == 0
    assert wallet["lifetime_spent"] == COST_15MIN


def test_ordinary_user_credits_only_failure_restores_in_full(db):
    user = _make_user(db, "free")
    project = _make_project(db, user.id)
    db.commit()
    _grant_purchased(db, user.id, COST_15MIN, f"topup:{user.id}")

    submission = run_ledger.submit_production(
        project.id, project.script, options={}
    )
    assert run_ledger.finish_run(submission["run_id"], "failed")
    db.expire_all()
    reservation = _reservation_for(db, submission["run_id"])
    assert reservation.status == "released"
    wallet = _wallet(db, user.id)
    assert wallet["purchased"]["available"] == COST_15MIN
    assert wallet["lifetime_spent"] == 0


def test_revision_render_legacy_plan_zero_credits_rejected(db):
    user = _make_user(db, "free")
    project = _make_project(db, user.id)
    db.commit()

    with pytest.raises(BillingRejected) as excinfo:
        run_ledger.submit_revision_render(
            project.id, project.script, revision_id="rev-1",
            source_run_id=str(uuid.uuid4()),
        )
    assert excinfo.value.error_code == "insufficient_credits"


# ---------------------------------------------------------------------
# Exemptions that must survive: staff and ownerless projects.
# ---------------------------------------------------------------------


def test_staff_user_legacy_plan_stays_exempt(db):
    user = _make_user(db, "founder", role="super_admin")
    project = _make_project(db, user.id)
    db.commit()

    submission = run_ledger.submit_production(
        project.id, project.script, options={}
    )
    assert submission is not None
    assert _reservation_for(db, submission["run_id"]) is None
    assert run_ledger.finish_run(submission["run_id"], "completed")


def test_ownerless_project_stays_unmetered(db):
    project = _make_project(db, None)
    db.commit()

    submission = run_ledger.submit_production(
        project.id, project.script, options={}
    )
    assert submission is not None
    assert _reservation_for(db, submission["run_id"]) is None
    assert run_ledger.finish_run(submission["run_id"], "completed")


# ---------------------------------------------------------------------
# Worker side: claim re-validation and the caller-less path.
# ---------------------------------------------------------------------


def test_claim_refuses_unbilled_ordinary_run(db):
    user = _make_user(db, "free")
    project = _make_project(db, user.id)
    db.commit()
    # A queued run inserted directly (the pre-fix bypass shape):
    # no submission, no reservation.
    created = run_ledger._insert_run(
        project.id, project.script, {}, "queued"
    )
    assert created is not None

    claimed = run_ledger.claim_run(created["run_id"])
    assert claimed is None
    run = run_ledger.get_run(created["run_id"])
    assert run["status"] == "failed"
    assert "insufficient_credits" in (run["error"] or "")


def test_claim_allows_reserved_ordinary_run(db):
    user = _make_user(db, "free")
    project = _make_project(db, user.id)
    db.commit()
    _grant_purchased(db, user.id, COST_15MIN, f"topup:{user.id}")
    submission = run_ledger.submit_production(
        project.id, project.script, options={}
    )

    claimed = run_ledger.claim_run(submission["run_id"])
    assert claimed is not None
    assert claimed["status"] == "running"
    assert run_ledger.finish_run(submission["run_id"], "completed")


def test_begin_run_refuses_unbilled_ordinary_project(db):
    user = _make_user(db, "free")
    project = _make_project(db, user.id)
    db.commit()

    with pytest.raises(BillingRejected):
        run_ledger.begin_run(project.id, project.script, options={})
    run = _latest_run(db, project)
    assert run["status"] == "failed"
    assert "insufficient_credits" in (run["error"] or "")


def test_begin_run_staff_project_proceeds(db):
    user = _make_user(db, "founder", role="super_admin")
    project = _make_project(db, user.id)
    db.commit()

    run_id = run_ledger.begin_run(project.id, project.script,
                                  options={})
    assert run_id
    assert run_ledger.finish_run(run_id, "completed")


# ---------------------------------------------------------------------
# Concurrency: the same credits cannot be spent twice.
# ---------------------------------------------------------------------


def test_concurrent_submissions_cannot_double_spend(db):
    user = _make_user(db, "free")
    db.commit()
    _grant_purchased(db, user.id, COST_15MIN, f"topup:{user.id}")
    projects = []
    for _ in range(2):
        session = SessionLocal()
        try:
            project = _make_project(session, user.id)
            session.commit()
            projects.append(project.id)
        finally:
            session.close()

    outcomes = {}

    def _submit(index, project_id):
        try:
            run_ledger.submit_production(
                project_id, SCRIPT_15MIN, options={}
            )
            outcomes[index] = "submitted"
        except BillingRejected:
            outcomes[index] = "rejected"
        except Exception as error:  # pragma: no cover - diagnostic
            outcomes[index] = f"error:{error}"

    threads = [
        threading.Thread(target=_submit, args=(i, pid))
        for i, pid in enumerate(projects)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(outcomes.values()) == ["rejected", "submitted"]
    wallet = _wallet(db, user.id)
    assert wallet["purchased"]["available"] == 0
    assert wallet["purchased"]["reserved"] == COST_15MIN
    assert _total_available(db, user.id) == 0
