"""Phase 9 — adversarial concurrency for generation billing reservations.

Phase 8's final audit (§5) recorded an honest gap: "Not covered by a
dedicated test: concurrent-request reservation races (mitigated by
UNIQUE idempotency keys, reservation run_id uniqueness and row locking
where the dialect supports it; no adversarial concurrency test was
written)."

These tests attack that gap with real threads against the real choke
points — services.billing.credits.reserve and
services.billing.generation_billing.reserve_for_new_run /
settle_for_run — each thread on its own session, released
simultaneously behind a barrier. The assertions are invariants, not
implementation details:

* a wallet can never be reserved past its balance, and never goes
  negative, no matter how many reservations race;
* one reservation_id moves money exactly once, however many times it
  is submitted concurrently;
* one run_id yields exactly one GenerationReservation and consumes
  exactly one entitlement;
* racing runs competing for the last credits produce exactly one
  winner and clean BillingRejected failures for the rest;
* concurrent settlement charges exactly once.

Losers may fail with the domain errors (InsufficientCreditsError /
BillingRejected) or, where a UNIQUE constraint is the mechanism that
stops them, an IntegrityError that rolls their transaction back —
what is asserted is the final state, which must be exact.
"""

import threading
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

import models
from database import Base, SessionLocal, engine
from services.billing import catalogue, credits, periods
from services.billing import generation_billing
from services.billing.generation_billing import BillingRejected

Base.metadata.create_all(bind=engine)

SCRIPT_15MIN = " ".join(["word"] * 2175)  # 900s at 145 wpm
STANDARD_COST = 84  # credits for one standard 15-min generation

THREADS = 8


def _make_user(db, plan_name=None):
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
        start, end = periods.month_bounds(now)
        db.add(
            models.UserSubscription(
                id=str(uuid.uuid4()),
                user_id=user.id,
                plan_id=plan.id,
                status="active",
                current_period_start=start,
                current_period_end=end,
            )
        )
    db.flush()
    return user


def _make_project(db, owner_id):
    project = models.Project(
        id=str(uuid.uuid4()), script=SCRIPT_15MIN, owner_id=owner_id
    )
    db.add(project)
    db.flush()
    return project


def _grant_purchased(db, user_id, amount):
    credits.grant(
        db,
        user_id,
        "purchased",
        amount,
        entry_type="allocation_purchase",
        idempotency_key=f"test-grant:{user_id}:{uuid.uuid4()}",
        source="test",
    )
    db.commit()


def _balances(db, user_id):
    account = credits.get_or_create_account(db, user_id)
    db.commit()
    return credits.balances(account)


def _zero_subscription_bucket(db, user_id):
    """Simulate a user who has already spent this period's
    subscription credits, so contention plays out on the purchased
    bucket alone (period creation allocates subscription credits;
    without this the wallet would hold the plan's full grant)."""
    account = credits.get_or_create_account(db, user_id)
    account.subscription_available = 0
    db.commit()


@pytest.fixture()
def db():
    session = SessionLocal()
    catalogue.ensure_billing_catalogue(session)
    session.commit()
    yield session
    session.close()


def _run_concurrently(fn, count=THREADS):
    """Run fn(index) in `count` threads released at the same instant.

    Returns a list of ("ok", value) / ("error", exception) per thread.
    """
    barrier = threading.Barrier(count)
    results = [None] * count

    def worker(index):
        barrier.wait(timeout=30)
        try:
            results[index] = ("ok", fn(index))
        except Exception as exc:  # noqa: BLE001 - classified by callers
            results[index] = ("error", exc)

    threads = [
        threading.Thread(target=worker, args=(i,)) for i in range(count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert all(result is not None for result in results), (
        "a worker thread did not finish"
    )
    return results


def test_concurrent_reserve_distinct_reservations_single_winner(db):
    """8 reservations of 84 race for a wallet holding exactly 84:
    exactly one wins, the rest are refused, nothing goes negative,
    and the ledger agrees with the account counters."""
    user = _make_user(db)
    db.commit()
    user_id = user.id
    _grant_purchased(db, user_id, STANDARD_COST)

    def attempt(index):
        session = SessionLocal()
        try:
            split = credits.reserve(
                session,
                user_id,
                STANDARD_COST,
                reservation_id=f"race-distinct-{index}",
                run_id=f"run-{index}",
            )
            session.commit()
            return split
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    results = _run_concurrently(attempt)
    winners = [r for r in results if r[0] == "ok"]
    losers = [r for r in results if r[0] == "error"]
    assert len(winners) == 1, f"expected exactly 1 winner: {results}"
    assert all(
        isinstance(exc, credits.InsufficientCreditsError)
        for _, exc in losers
    ), f"losers must be refused for insufficiency: {losers}"

    balances = _balances(db, user_id)
    assert balances["purchased"]["available"] == 0
    assert balances["purchased"]["reserved"] == STANDARD_COST
    reserve_entries = (
        db.query(models.CreditLedgerEntry)
        .filter_by(entry_type="reserve")
        .all()
    )
    assert sum(-entry.amount for entry in reserve_entries) == STANDARD_COST


def test_concurrent_reserve_same_reservation_moves_money_once(db):
    """The same reservation_id submitted from 8 threads at once is
    idempotent in effect: the wallet is debited exactly once and the
    ledger holds exactly one reserve split, whichever thread won."""
    user = _make_user(db)
    db.commit()
    user_id = user.id
    _grant_purchased(db, user_id, 2 * STANDARD_COST)
    reservation_id = f"race-same-{uuid.uuid4()}"

    def attempt(_index):
        session = SessionLocal()
        try:
            split = credits.reserve(
                session,
                user_id,
                STANDARD_COST,
                reservation_id=reservation_id,
                run_id="run-shared",
            )
            session.commit()
            return split
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    results = _run_concurrently(attempt)
    errors = [exc for kind, exc in results if kind == "error"]
    assert all(
        isinstance(exc, (IntegrityError, credits.BillingError))
        for exc in errors
    ), f"unexpected loser error type: {errors}"

    balances = _balances(db, user_id)
    assert balances["purchased"]["available"] == STANDARD_COST
    assert balances["purchased"]["reserved"] == STANDARD_COST
    reserve_entries = (
        db.query(models.CreditLedgerEntry)
        .filter_by(entry_type="reserve", reservation_id=reservation_id)
        .all()
    )
    assert sum(-entry.amount for entry in reserve_entries) == STANDARD_COST


def test_concurrent_duplicate_run_reserves_once(db):
    """6 threads submit the SAME run_id through the real generation
    choke point: exactly one reservation row, exactly one entitlement
    consumed, every other thread fails closed."""
    user = _make_user(db, plan_name="creator")
    project = _make_project(db, user.id)
    db.commit()
    user_id, project_id = user.id, project.id
    run_id = str(uuid.uuid4())

    def attempt(_index):
        return generation_billing.reserve_for_new_run(
            run_id, project_id, SCRIPT_15MIN
        )

    results = _run_concurrently(attempt, count=6)
    winners = [value for kind, value in results if kind == "ok"]
    losers = [exc for kind, exc in results if kind == "error"]
    assert len(winners) == 1, f"expected exactly 1 winner: {results}"
    assert all(isinstance(exc, BillingRejected) for exc in losers), (
        f"duplicates must fail closed as BillingRejected: {losers}"
    )

    rows = (
        db.query(models.GenerationReservation)
        .filter_by(run_id=run_id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].entitlement_reserved is True
    period = periods.ensure_current_period(db, user_id)
    db.commit()
    assert period.generations_used == 1


def test_concurrent_runs_compete_for_last_credits(db):
    """Entitlements exhausted, wallet holding exactly one generation:
    6 distinct runs race through the choke point — exactly one is
    reserved, the rest are refused for insufficient credits, and the
    wallet ends with exactly 84 reserved and 0 available."""
    user = _make_user(db, plan_name="creator")
    project = _make_project(db, user.id)
    db.commit()
    user_id, project_id = user.id, project.id
    period = periods.ensure_current_period(db, user_id)
    period.generations_used = period.generations_granted
    db.commit()
    _zero_subscription_bucket(db, user_id)
    _grant_purchased(db, user_id, STANDARD_COST)

    def attempt(index):
        return generation_billing.reserve_for_new_run(
            str(uuid.uuid4()), project_id, SCRIPT_15MIN
        )

    results = _run_concurrently(attempt, count=6)
    winners = [value for kind, value in results if kind == "ok"]
    losers = [exc for kind, exc in results if kind == "error"]
    assert len(winners) == 1, f"expected exactly 1 winner: {results}"
    assert all(isinstance(exc, BillingRejected) for exc in losers)
    # Losers fail closed: normally insufficient_credits; under a
    # simultaneous burst on SQLite a loser can instead hit the
    # fail-closed billing_unavailable (503) when its transaction
    # loses the write-lock race — nothing is charged either way,
    # which is the invariant asserted here and below.
    assert all(
        exc.error_code
        in ("insufficient_credits", "billing_unavailable")
        for exc in losers
    ), f"losers must fail closed: {losers}"
    assert winners[0]["credits_reserved"] == STANDARD_COST

    balances = _balances(db, user_id)
    assert balances["purchased"]["available"] == 0
    assert balances["purchased"]["reserved"] == STANDARD_COST


def test_concurrent_settle_charges_exactly_once(db):
    """6 threads settle the same completed run at once: the wallet is
    charged exactly 84 exactly once and the reservation ends settled
    with charged_credits == 84."""
    user = _make_user(db, plan_name="creator")
    project = _make_project(db, user.id)
    db.commit()
    user_id, project_id = user.id, project.id
    period = periods.ensure_current_period(db, user_id)
    period.generations_used = period.generations_granted
    db.commit()
    _zero_subscription_bucket(db, user_id)
    _grant_purchased(db, user_id, STANDARD_COST)

    run_id = str(uuid.uuid4())
    summary = generation_billing.reserve_for_new_run(
        run_id, project_id, SCRIPT_15MIN
    )
    assert summary["credits_reserved"] == STANDARD_COST
    run = models.ProductionRun(
        id=run_id,
        project_id=project_id,
        status="completed",
    )
    db.add(run)
    db.commit()

    def attempt(_index):
        generation_billing.settle_for_run(
            run_id, "completed", {"duration_seconds": 900}
        )
        return True

    results = _run_concurrently(attempt, count=6)
    assert all(kind == "ok" for kind, _ in results), (
        f"settle_for_run is fail-soft and must not raise: {results}"
    )

    db.expire_all()
    reservation = (
        db.query(models.GenerationReservation)
        .filter_by(run_id=run_id)
        .one()
    )
    assert reservation.status == "settled"
    assert reservation.charged_credits == STANDARD_COST
    charge_entries = (
        db.query(models.CreditLedgerEntry)
        .filter_by(
            entry_type="charge", reservation_id=reservation.id
        )
        .all()
    )
    assert sum(-entry.amount for entry in charge_entries) == STANDARD_COST
    balances = _balances(db, user_id)
    assert balances["purchased"]["available"] == 0
    assert balances["purchased"]["reserved"] == 0
