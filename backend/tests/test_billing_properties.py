"""Hypothesis property tests for the billing invariants
(GitHub Skills Programme, Lane 5, 2026-11-25).

Lane 5's top INSTALL: Phase 9 found a real concurrency money bug
by adversarial testing; the credit maths (84 credits per standard
generation, ceil-by-seconds, entitlement/expiry rules) is exactly
the invariant space example-based tests under-cover. These
properties run the REAL credits module against generated inputs:

* the pure rate function matches its documented formula exactly,
  is monotone, and never charges less than one credit;
* wallet conservation — across generated grant/reserve/release/
  charge sequences, no bucket ever goes negative and
  available + reserved + lifetime_spent always equals the total
  granted (nothing is created or destroyed by a movement);
* idempotency — replaying a grant or a reserve under the same key
  moves nothing twice;
* over-reservation refuses cleanly and moves nothing.

In-memory SQLite per example; the Phase 9 atomic-delta fix is
what makes the conservation property hold under the ORM.
"""

import math
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import models
from database import Base
from services.billing import catalogue, credits

SETTINGS = settings(max_examples=30, deadline=None)


@pytest.fixture(autouse=True)
def _default_rate(monkeypatch):
    # The rate function reads an env override; pin the default.
    monkeypatch.delenv("CINEMORA_STANDARD_GENERATION_CREDITS", raising=False)


# --- the pure rate function ------------------------------------------

@given(seconds=st.floats(min_value=0.01, max_value=100_000, allow_nan=False))
@SETTINGS
def test_cost_matches_documented_formula(seconds):
    expected = max(1, math.ceil(seconds * 84 / 900))
    assert catalogue.generation_credit_cost(seconds) == expected


@given(
    a=st.floats(min_value=0.01, max_value=50_000, allow_nan=False),
    b=st.floats(min_value=0.01, max_value=50_000, allow_nan=False),
)
@SETTINGS
def test_cost_is_monotone_and_subadditive(a, b):
    lo, hi = min(a, b), max(a, b)
    assert catalogue.generation_credit_cost(lo) <= catalogue.generation_credit_cost(hi)
    assert (
        catalogue.generation_credit_cost(a + b)
        <= catalogue.generation_credit_cost(a)
        + catalogue.generation_credit_cost(b)
    )


@given(k=st.integers(min_value=1, max_value=200))
@SETTINGS
def test_cost_scales_exactly_at_standard_units(k):
    assert catalogue.generation_credit_cost(900.0 * k) == 84 * k


def test_cost_minimum_and_nonpositive_durations():
    assert catalogue.generation_credit_cost(0) == 1
    assert catalogue.generation_credit_cost(-30) == 1
    assert catalogue.generation_credit_cost(1) == 1


# --- the wallet --------------------------------------------------------

def _fresh_wallet():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = Session(engine)
    user = models.User(
        id=str(uuid.uuid4()), email="p@example.invalid", password_hash="x"
    )
    session.add(user)
    session.commit()
    return session, user.id


def _totals(session, user_id):
    account = credits.get_or_create_account(session, user_id)
    balances = credits.balances(account)
    available = sum(b["available"] for b in balances.values()
                    if isinstance(b, dict))
    reserved = sum(b["reserved"] for b in balances.values()
                   if isinstance(b, dict))
    return available, reserved, balances["lifetime_spent"]


@given(
    granted=st.integers(min_value=1, max_value=5000),
    reserve_frac=st.floats(min_value=0.0, max_value=1.0),
    charge_frac=st.floats(min_value=0.0, max_value=1.0),
)
@SETTINGS
def test_grant_reserve_charge_release_conserves(granted, reserve_frac, charge_frac):
    session, user_id = _fresh_wallet()
    try:
        credits.grant(
            session, user_id, "subscription", granted,
            entry_type="allocation_subscription",
            idempotency_key="g:1", source="system",
        )
        reserved_amount = int(granted * reserve_frac)
        if reserved_amount:
            credits.reserve(
                session, user_id, reserved_amount,
                reservation_id="r:1", source="user",
            )
        charge = int(reserved_amount * charge_frac)
        if charge:
            result = credits.charge_reservation(
                session, user_id, reservation_id="r:1",
                total_charge=charge, source="system",
            )
            assert result["shortfall"] == 0
        credits.release_reservation(
            session, user_id, reservation_id="r:1", source="system",
        )
        available, reserved, spent = _totals(session, user_id)
        assert reserved == 0
        assert spent == charge
        assert available == granted - charge
        assert available + reserved + spent == granted
    finally:
        session.close()


@given(amount=st.integers(min_value=1, max_value=5000))
@SETTINGS
def test_grant_idempotency_never_double_applies(amount):
    session, user_id = _fresh_wallet()
    try:
        for _ in range(3):
            credits.grant(
                session, user_id, "purchased", amount,
                entry_type="allocation_purchased",
                idempotency_key="g:dup", source="system",
            )
        available, reserved, spent = _totals(session, user_id)
        assert (available, reserved, spent) == (amount, 0, 0)
    finally:
        session.close()


@given(
    granted=st.integers(min_value=1, max_value=1000),
    extra=st.integers(min_value=1, max_value=5000),
)
@SETTINGS
def test_over_reservation_refuses_and_moves_nothing(granted, extra):
    session, user_id = _fresh_wallet()
    try:
        credits.grant(
            session, user_id, "subscription", granted,
            entry_type="allocation_subscription",
            idempotency_key="g:1", source="system",
        )
        with pytest.raises(credits.InsufficientCreditsError):
            credits.reserve(
                session, user_id, granted + extra,
                reservation_id="r:too-big", source="user",
            )
        available, reserved, spent = _totals(session, user_id)
        assert (available, reserved, spent) == (granted, 0, 0)
    finally:
        session.close()


@given(
    granted=st.integers(min_value=10, max_value=5000),
    reserve_frac=st.floats(min_value=0.1, max_value=1.0),
)
@SETTINGS
def test_reserve_idempotency_never_double_moves(granted, reserve_frac):
    session, user_id = _fresh_wallet()
    try:
        credits.grant(
            session, user_id, "subscription", granted,
            entry_type="allocation_subscription",
            idempotency_key="g:1", source="system",
        )
        amount = max(1, int(granted * reserve_frac))
        first = credits.reserve(
            session, user_id, amount, reservation_id="r:1", source="user"
        )
        second = credits.reserve(
            session, user_id, amount, reservation_id="r:1", source="user"
        )
        assert first == second
        available, reserved, spent = _totals(session, user_id)
        assert (available, reserved, spent) == (granted - amount, amount, 0)
    finally:
        session.close()
