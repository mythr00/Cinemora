"""Phase 8 slice 3 — credit wallet + immutable ledger.

Covers directive §18 items: allocation, reservation, deduction,
refunds/releases, expiration, concurrent duplicate protection
(idempotency), and the rule that balances never go negative and
no operation applies twice.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import models
from database import Base
from services.billing import credits


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'credits.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        user = models.User(
            id=str(uuid.uuid4()), email="c@example.invalid",
            password_hash="x",
        )
        session.add(user)
        session.commit()
        yield session, user.id
    engine.dispose()


def _grant(db, user_id, bucket, amount, key):
    return credits.grant(
        db, user_id, bucket, amount,
        entry_type=f"allocation_{bucket}",
        idempotency_key=key, source="system",
    )


def _account(db, user_id):
    return credits.get_or_create_account(db, user_id)


def test_grant_is_idempotent(db):
    session, user_id = db
    entry, created = _grant(session, user_id, "subscription", 1350,
                            "alloc:1")
    assert created is True
    again, created_again = _grant(session, user_id, "subscription",
                                  1350, "alloc:1")
    assert created_again is False
    assert again.id == entry.id
    account = _account(session, user_id)
    assert account.subscription_available == 1350
    assert session.query(models.CreditLedgerEntry).count() == 1


def test_reserve_spend_order_and_release(db):
    session, user_id = db
    _grant(session, user_id, "subscription", 100, "g1")
    _grant(session, user_id, "promo", 50, "g2")
    _grant(session, user_id, "purchased", 200, "g3")
    session.commit()

    split = credits.reserve(
        session, user_id, 180, reservation_id="r1", run_id="run1"
    )
    # Spend order: subscription -> promo -> purchased.
    assert split == {"subscription": 100, "promo": 50,
                     "purchased": 30}
    account = _account(session, user_id)
    assert credits.total_available(account) == 170
    assert account.purchased_reserved == 30

    # Duplicate reserve for the same reservation moves nothing.
    split2 = credits.reserve(
        session, user_id, 180, reservation_id="r1", run_id="run1"
    )
    assert split2 == split
    assert credits.total_available(_account(session, user_id)) == 170

    released = credits.release_reservation(
        session, user_id, reservation_id="r1"
    )
    assert released == split
    account = _account(session, user_id)
    assert credits.total_available(account) == 350
    assert account.subscription_reserved == 0
    # Release is idempotent too.
    assert credits.release_reservation(
        session, user_id, reservation_id="r1"
    ) == split
    assert credits.total_available(_account(session, user_id)) == 350


def test_reserve_insufficient_moves_nothing(db):
    session, user_id = db
    _grant(session, user_id, "purchased", 40, "g1")
    session.commit()
    with pytest.raises(credits.InsufficientCreditsError):
        credits.reserve(session, user_id, 84, reservation_id="rX")
    session.rollback()
    account = _account(session, user_id)
    assert account.purchased_available == 40
    assert account.purchased_reserved == 0


def test_charge_consumes_reserved_then_available(db):
    session, user_id = db
    _grant(session, user_id, "subscription", 200, "g1")
    session.commit()
    credits.reserve(session, user_id, 56, reservation_id="r1")
    # Actual cost 84: 56 from reserved + 28 overage from available.
    result = credits.charge_reservation(
        session, user_id, reservation_id="r1", total_charge=84
    )
    assert result["charged"] == 84
    assert result["from_reserved"] == {"subscription": 56}
    assert result["from_available"] == {"subscription": 28}
    assert result["shortfall"] == 0
    account = _account(session, user_id)
    assert account.subscription_available == 200 - 56 - 28
    assert account.subscription_reserved == 0
    assert account.lifetime_spent == 84
    # Replay charges nothing twice.
    replay = credits.charge_reservation(
        session, user_id, reservation_id="r1", total_charge=84
    )
    assert replay["replayed"] is True
    assert _account(session, user_id).lifetime_spent == 84


def test_charge_below_reserve_leaves_remainder_reserved(db):
    session, user_id = db
    _grant(session, user_id, "purchased", 500, "g1")
    session.commit()
    credits.reserve(session, user_id, 112, reservation_id="r1")
    result = credits.charge_reservation(
        session, user_id, reservation_id="r1", total_charge=84
    )
    assert result["charged"] == 84
    account = _account(session, user_id)
    assert account.purchased_reserved == 28  # caller releases this
    credits.release_reservation(session, user_id, reservation_id="r1")
    account = _account(session, user_id)
    assert account.purchased_available == 500 - 84
    assert account.purchased_reserved == 0


def test_charge_shortfall_is_recorded_never_negative(db):
    session, user_id = db
    _grant(session, user_id, "purchased", 30, "g1")
    session.commit()
    credits.reserve(session, user_id, 30, reservation_id="r1")
    result = credits.charge_reservation(
        session, user_id, reservation_id="r1", total_charge=84
    )
    assert result["charged"] == 30
    assert result["shortfall"] == 54
    account = _account(session, user_id)
    assert credits.total_available(account) == 0
    entry = (
        session.query(models.CreditLedgerEntry)
        .filter_by(entry_type="charge")
        .one()
    )
    assert entry.details["shortfall_credits"] == 54


def test_expire_bucket_and_admin_adjust(db):
    session, user_id = db
    _grant(session, user_id, "subscription", 300, "g1")
    _grant(session, user_id, "purchased", 90, "g2")
    session.commit()

    expired = credits.expire_bucket(
        session, user_id, "subscription",
        idempotency_key="expire:period1", reason="period end",
    )
    assert expired == 300
    assert credits.expire_bucket(
        session, user_id, "subscription",
        idempotency_key="expire:period1",
    ) == 0
    account = _account(session, user_id)
    assert account.subscription_available == 0
    assert account.purchased_available == 90  # untouched

    entry, created = credits.adjust(
        session, user_id, "purchased", -40,
        actor_id="admin1", reason="goodwill correction",
        idempotency_key="adj:1",
    )
    assert created is True
    assert _account(session, user_id).purchased_available == 50
    with pytest.raises(credits.InsufficientCreditsError):
        credits.adjust(
            session, user_id, "purchased", -100,
            actor_id="admin1", reason="too much",
            idempotency_key="adj:2",
        )
    session.rollback()
    with pytest.raises(credits.BillingError):
        credits.adjust(
            session, user_id, "purchased", 10,
            actor_id="admin1", reason="",
            idempotency_key="adj:3",
        )
    session.rollback()


def test_available_reconstructs_from_ledger(db):
    session, user_id = db
    _grant(session, user_id, "subscription", 1350, "g1")
    _grant(session, user_id, "purchased", 120, "g2")
    session.commit()
    credits.reserve(session, user_id, 200, reservation_id="r1")
    credits.charge_reservation(
        session, user_id, reservation_id="r1", total_charge=150
    )
    credits.release_reservation(session, user_id, reservation_id="r1")
    session.commit()

    acting_on_available = {
        "allocation_subscription", "allocation_purchased",
        "allocation_promo", "release", "refund", "admin_adjustment",
        "reserve", "expire",
    }
    reconstructed = {"subscription": 0, "purchased": 0, "promo": 0}
    for entry in session.query(models.CreditLedgerEntry).all():
        if entry.entry_type in acting_on_available and entry.bucket:
            reconstructed[entry.bucket] += entry.amount
    account = _account(session, user_id)
    for bucket, value in reconstructed.items():
        assert credits.available(account, bucket) == value, bucket
    # And the last entry's snapshot agrees with the account.
    last = (
        session.query(models.CreditLedgerEntry)
        .order_by(models.CreditLedgerEntry.created_at.desc())
        .first()
    )
    assert last.subscription_balance_after == \
        account.subscription_available
