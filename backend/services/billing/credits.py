"""Credit wallet + immutable ledger (Phase 8; directive §4).

The ONLY module that mutates credit balances. Every mutation
moves a CreditAccount row and appends an immutable
CreditLedgerEntry in the same transaction (the caller commits).

Ledger conventions (models.CreditLedgerEntry carries the full
contract): `amount` is signed from the perspective of the pool
the entry type acts on — allocations/releases/refunds are
positive INTO available, reserves are negative FROM available,
charges are negative FROM reserved (the value was already
removed from available at reserve time), expiries are negative
FROM available. The *_balance_after columns snapshot the three
AVAILABLE balances after each entry, so available balances can
be reconstructed from the ledger alone:
    available(bucket) = sum of amount over entries acting on
    available for that bucket (allocations + releases + refunds
    + adjustments - reserves - expiries).

Spend order (soonest-expiring value first): subscription ->
promo -> purchased. Refunds and releases always return value to
the bucket it came from (tracked per reservation from the
reserve entries themselves).

Balances never go negative. A settlement whose overage exceeds
the available balance charges what exists and records the
shortfall on the charge entries' details — the film exists by
then; the loss is the platform's, visible, and never silently
billed later.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError

import models

BUCKETS = ("subscription", "purchased", "promo")
SPEND_ORDER = ("subscription", "promo", "purchased")


class BillingError(Exception):
    """Base class for billing-domain errors."""


class InsufficientCreditsError(BillingError):
    def __init__(self, needed: int, available: int):
        super().__init__(
            f"insufficient credits: needed {needed}, "
            f"available {available}"
        )
        self.needed = needed
        self.available = available


def _new_id() -> str:
    return str(uuid.uuid4())


def get_or_create_account(db, user_id: str) -> models.CreditAccount:
    account = (
        db.query(models.CreditAccount)
        .filter_by(user_id=user_id)
        .one_or_none()
    )
    if account is None:
        account = models.CreditAccount(id=_new_id(), user_id=user_id)
        db.add(account)
        db.flush()
    return account


def _lock_account(db, user_id: str) -> models.CreditAccount:
    """Fetch the account FOR UPDATE (Postgres); SQLite serialises
    writers anyway, and its dialect cannot render FOR UPDATE."""
    query = db.query(models.CreditAccount).filter_by(user_id=user_id)
    if db.bind is not None and db.bind.dialect.name != "sqlite":
        query = query.with_for_update()
    account = query.one_or_none()
    if account is None:
        account = get_or_create_account(db, user_id)
    return account


def available(account: models.CreditAccount, bucket: str) -> int:
    return int(getattr(account, f"{bucket}_available"))


def reserved(account: models.CreditAccount, bucket: str) -> int:
    return int(getattr(account, f"{bucket}_reserved"))


def total_available(account: models.CreditAccount) -> int:
    return sum(available(account, bucket) for bucket in BUCKETS)


def balances(account: models.CreditAccount) -> dict:
    return {
        bucket: {
            "available": available(account, bucket),
            "reserved": reserved(account, bucket),
        }
        for bucket in BUCKETS
    } | {"lifetime_spent": int(account.lifetime_spent)}


def _append_entry(
    db,
    account: models.CreditAccount,
    *,
    entry_type: str,
    bucket: str | None,
    amount: int,
    idempotency_key: str | None = None,
    source: str = "system",
    actor_id: str | None = None,
    reason: str | None = None,
    subscription_id: str | None = None,
    billing_period_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    payment_id: str | None = None,
    reservation_id: str | None = None,
    expires_at=None,
    details: dict | None = None,
) -> models.CreditLedgerEntry:
    entry = models.CreditLedgerEntry(
        id=_new_id(),
        user_id=account.user_id,
        account_id=account.id,
        entry_type=entry_type,
        bucket=bucket,
        amount=amount,
        subscription_balance_after=account.subscription_available,
        purchased_balance_after=account.purchased_available,
        promo_balance_after=account.promo_available,
        subscription_id=subscription_id,
        billing_period_id=billing_period_id,
        project_id=project_id,
        run_id=run_id,
        payment_id=payment_id,
        reservation_id=reservation_id,
        idempotency_key=idempotency_key,
        reason=reason,
        source=source,
        actor_id=actor_id,
        details=details,
        expires_at=expires_at,
    )
    db.add(entry)
    db.flush()
    return entry


def _existing_by_key(db, idempotency_key: str):
    return (
        db.query(models.CreditLedgerEntry)
        .filter_by(idempotency_key=idempotency_key)
        .one_or_none()
    )


def grant(
    db,
    user_id: str,
    bucket: str,
    amount: int,
    *,
    entry_type: str,
    idempotency_key: str,
    source: str,
    actor_id: str | None = None,
    reason: str | None = None,
    subscription_id: str | None = None,
    billing_period_id: str | None = None,
    payment_id: str | None = None,
    expires_at=None,
) -> tuple[models.CreditLedgerEntry, bool]:
    """Credit `amount` into a bucket's available balance.

    Idempotent on idempotency_key: a replay returns the original
    entry with created=False and moves nothing.
    """
    if bucket not in BUCKETS:
        raise BillingError(f"unknown bucket {bucket!r}")
    if amount <= 0:
        raise BillingError("grant amount must be positive")
    existing = _existing_by_key(db, idempotency_key)
    if existing is not None:
        return existing, False
    account = _lock_account(db, user_id)
    setattr(
        account, f"{bucket}_available",
        available(account, bucket) + amount,
    )
    try:
        entry = _append_entry(
            db,
            account,
            entry_type=entry_type,
            bucket=bucket,
            amount=amount,
            idempotency_key=idempotency_key,
            source=source,
            actor_id=actor_id,
            reason=reason,
            subscription_id=subscription_id,
            billing_period_id=billing_period_id,
            payment_id=payment_id,
            expires_at=expires_at,
        )
    except IntegrityError:
        db.rollback()
        existing = _existing_by_key(db, idempotency_key)
        if existing is not None:
            return existing, False
        raise
    return entry, True


def reserve(
    db,
    user_id: str,
    amount: int,
    *,
    reservation_id: str,
    run_id: str | None = None,
    project_id: str | None = None,
    billing_period_id: str | None = None,
    source: str = "user",
) -> dict:
    """Move `amount` from available to reserved across buckets in
    spend order. Returns the per-bucket split.

    Idempotent per reservation: if reserve entries already exist
    for this reservation, their split is returned and nothing
    moves. Raises InsufficientCreditsError without moving
    anything when the total available cannot cover the amount.
    """
    if amount < 0:
        raise BillingError("reserve amount must be >= 0")
    existing = (
        db.query(models.CreditLedgerEntry)
        .filter_by(
            reservation_id=reservation_id, entry_type="reserve"
        )
        .all()
    )
    if existing:
        return {
            entry.bucket: -entry.amount
            for entry in existing
            if entry.bucket
        }
    account = _lock_account(db, user_id)
    total = total_available(account)
    if total < amount:
        raise InsufficientCreditsError(needed=amount, available=total)
    split: dict[str, int] = {}
    remaining = amount
    for bucket in SPEND_ORDER:
        if remaining <= 0:
            break
        take = min(available(account, bucket), remaining)
        if take <= 0:
            continue
        setattr(
            account, f"{bucket}_available",
            available(account, bucket) - take,
        )
        setattr(
            account, f"{bucket}_reserved",
            reserved(account, bucket) + take,
        )
        split[bucket] = take
        remaining -= take
        _append_entry(
            db,
            account,
            entry_type="reserve",
            bucket=bucket,
            amount=-take,
            idempotency_key=f"reserve:{reservation_id}:{bucket}",
            source=source,
            run_id=run_id,
            project_id=project_id,
            billing_period_id=billing_period_id,
            reservation_id=reservation_id,
        )
    return split


def _reservation_split(db, reservation_id: str) -> dict:
    entries = (
        db.query(models.CreditLedgerEntry)
        .filter_by(
            reservation_id=reservation_id, entry_type="reserve"
        )
        .all()
    )
    return {
        entry.bucket: -entry.amount for entry in entries if entry.bucket
    }


def release_reservation(
    db,
    user_id: str,
    *,
    reservation_id: str,
    run_id: str | None = None,
    project_id: str | None = None,
    source: str = "system",
    reason: str | None = None,
) -> dict:
    """Return a reservation's full reserved split to available.
    Idempotent per reservation. Returns the released split."""
    existing = (
        db.query(models.CreditLedgerEntry)
        .filter_by(
            reservation_id=reservation_id, entry_type="release"
        )
        .all()
    )
    if existing:
        return {
            entry.bucket: entry.amount
            for entry in existing
            if entry.bucket
        }
    split = _reservation_split(db, reservation_id)
    if not split:
        return {}
    account = _lock_account(db, user_id)
    released: dict[str, int] = {}
    for bucket in SPEND_ORDER:
        amount = split.get(bucket, 0)
        if amount <= 0:
            continue
        held = reserved(account, bucket)
        amount = min(amount, held)
        if amount <= 0:
            continue
        setattr(
            account, f"{bucket}_reserved", held - amount
        )
        setattr(
            account, f"{bucket}_available",
            available(account, bucket) + amount,
        )
        released[bucket] = amount
        _append_entry(
            db,
            account,
            entry_type="release",
            bucket=bucket,
            amount=amount,
            idempotency_key=f"release:{reservation_id}:{bucket}",
            source=source,
            reason=reason,
            run_id=run_id,
            project_id=project_id,
            reservation_id=reservation_id,
        )
    return released


def charge_reservation(
    db,
    user_id: str,
    *,
    reservation_id: str,
    total_charge: int,
    run_id: str | None = None,
    project_id: str | None = None,
    billing_period_id: str | None = None,
    source: str = "system",
) -> dict:
    """Settle a reservation: consume reserved value up to
    total_charge; draw any overage from available in spend order.

    Returns {"charged": int, "from_reserved": split,
    "from_available": split, "shortfall": int}. Idempotent per
    reservation (existing charge entries are replayed, nothing
    moves twice). Unused reserved value is NOT released here —
    the caller releases the remainder explicitly so the ledger
    shows charge and release as separate, auditable movements.
    """
    existing = (
        db.query(models.CreditLedgerEntry)
        .filter_by(
            reservation_id=reservation_id, entry_type="charge"
        )
        .all()
    )
    if existing:
        charged = sum(-e.amount for e in existing)
        return {
            "charged": charged,
            "from_reserved": {},
            "from_available": {},
            "shortfall": 0,
            "replayed": True,
        }
    if total_charge < 0:
        raise BillingError("charge must be >= 0")
    account = _lock_account(db, user_id)
    split = _reservation_split(db, reservation_id)
    remaining = total_charge
    from_reserved: dict[str, int] = {}
    from_available: dict[str, int] = {}

    for bucket in SPEND_ORDER:
        if remaining <= 0:
            break
        held = min(split.get(bucket, 0), reserved(account, bucket))
        take = min(held, remaining)
        if take > 0:
            setattr(
                account, f"{bucket}_reserved",
                reserved(account, bucket) - take,
            )
            from_reserved[bucket] = take
            remaining -= take

    for bucket in SPEND_ORDER:
        if remaining <= 0:
            break
        take = min(available(account, bucket), remaining)
        if take > 0:
            setattr(
                account, f"{bucket}_available",
                available(account, bucket) - take,
            )
            from_available[bucket] = take
            remaining -= take

    shortfall = remaining
    charged = total_charge - shortfall
    account.lifetime_spent = int(account.lifetime_spent) + charged

    for bucket in SPEND_ORDER:
        amount = from_reserved.get(bucket, 0) + from_available.get(
            bucket, 0
        )
        if amount <= 0:
            continue
        _append_entry(
            db,
            account,
            entry_type="charge",
            bucket=bucket,
            amount=-amount,
            idempotency_key=f"charge:{reservation_id}:{bucket}",
            source=source,
            run_id=run_id,
            project_id=project_id,
            billing_period_id=billing_period_id,
            reservation_id=reservation_id,
            details={
                "from_reserved": from_reserved.get(bucket, 0),
                "from_available": from_available.get(bucket, 0),
                **(
                    {"shortfall_credits": shortfall}
                    if shortfall
                    else {}
                ),
            },
        )
    return {
        "charged": charged,
        "from_reserved": from_reserved,
        "from_available": from_available,
        "shortfall": shortfall,
        "replayed": False,
    }


def expire_bucket(
    db,
    user_id: str,
    bucket: str,
    *,
    idempotency_key: str,
    source: str = "system",
    reason: str | None = None,
    billing_period_id: str | None = None,
) -> int:
    """Zero a bucket's AVAILABLE balance with an expire entry
    (period-end subscription expiry; purchased-lot expiry).
    Reserved value is never expired — it belongs to in-flight
    generations. Returns the amount expired (0 = no entry)."""
    if bucket not in BUCKETS:
        raise BillingError(f"unknown bucket {bucket!r}")
    if _existing_by_key(db, idempotency_key) is not None:
        return 0
    account = _lock_account(db, user_id)
    amount = available(account, bucket)
    if amount <= 0:
        return 0
    setattr(account, f"{bucket}_available", 0)
    _append_entry(
        db,
        account,
        entry_type="expire",
        bucket=bucket,
        amount=-amount,
        idempotency_key=idempotency_key,
        source=source,
        reason=reason,
        billing_period_id=billing_period_id,
    )
    return amount


def adjust(
    db,
    user_id: str,
    bucket: str,
    delta: int,
    *,
    actor_id: str,
    reason: str,
    idempotency_key: str,
) -> tuple[models.CreditLedgerEntry, bool]:
    """Admin manual adjustment (directive §12). Positive delta
    credits the bucket; negative debits it and may not exceed the
    available balance. Idempotent on idempotency_key. The admin
    API layer additionally writes the admin_audit_log row."""
    if bucket not in BUCKETS:
        raise BillingError(f"unknown bucket {bucket!r}")
    if delta == 0:
        raise BillingError("adjustment delta must be non-zero")
    if not reason:
        raise BillingError("admin adjustments require a reason")
    existing = _existing_by_key(db, idempotency_key)
    if existing is not None:
        return existing, False
    account = _lock_account(db, user_id)
    new_balance = available(account, bucket) + delta
    if new_balance < 0:
        raise InsufficientCreditsError(
            needed=-delta, available=available(account, bucket)
        )
    setattr(account, f"{bucket}_available", new_balance)
    entry = _append_entry(
        db,
        account,
        entry_type="admin_adjustment",
        bucket=bucket,
        amount=delta,
        idempotency_key=idempotency_key,
        source="admin",
        actor_id=actor_id,
        reason=reason,
    )
    return entry, True
