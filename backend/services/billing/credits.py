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
    """Fetch the account FOR UPDATE (Postgres row lock, which
    serialises the read-modify-write cycle there). SQLite cannot
    render FOR UPDATE and its writer serialisation does NOT cover a
    read-check-write spread across statements — on SQLite the
    balance guards live in _apply_bucket_delta's conditional
    UPDATEs, which the database evaluates atomically."""
    query = db.query(models.CreditAccount).filter_by(user_id=user_id)
    if db.bind is not None and db.bind.dialect.name != "sqlite":
        query = query.with_for_update()
    account = query.one_or_none()
    if account is None:
        account = get_or_create_account(db, user_id)
    return account


def _apply_bucket_delta(
    db,
    account: models.CreditAccount,
    bucket: str,
    *,
    available_delta: int = 0,
    reserved_delta: int = 0,
    min_available: int | None = None,
    min_reserved: int | None = None,
) -> bool:
    """Atomically adjust one bucket's counters via a conditional
    UPDATE, refreshing the ORM account on success.

    The balance guard (``min_available`` / ``min_reserved``) and the
    mutation are a single statement, so the database evaluates the
    guard against the newest committed row when it takes the write
    lock — a concurrent transaction cannot slip between the check
    and the write the way it can between an ORM read and a later
    setattr (Phase 9 adversarial tests proved 5 of 8 racing
    reservations could over-reserve one wallet before this).
    Returns False — nothing changed — when the guard fails.
    """
    available_col = getattr(models.CreditAccount, f"{bucket}_available")
    reserved_col = getattr(models.CreditAccount, f"{bucket}_reserved")
    filters = [models.CreditAccount.id == account.id]
    if min_available is not None:
        filters.append(available_col >= min_available)
    if min_reserved is not None:
        filters.append(reserved_col >= min_reserved)
    values = {}
    if available_delta:
        values[f"{bucket}_available"] = available_col + available_delta
    if reserved_delta:
        values[f"{bucket}_reserved"] = reserved_col + reserved_delta
    if not values:
        return True
    updated = (
        db.query(models.CreditAccount)
        .filter(*filters)
        .update(values, synchronize_session=False)
    )
    if updated != 1:
        return False
    db.refresh(account)
    return True


def _bump_lifetime_spent(db, account: models.CreditAccount, amount: int) -> None:
    """Relative UPDATE for the lifetime counter (never an absolute
    setattr computed from a possibly stale read)."""
    if not amount:
        return
    db.query(models.CreditAccount).filter(
        models.CreditAccount.id == account.id
    ).update(
        {
            "lifetime_spent": models.CreditAccount.lifetime_spent + amount,
        },
        synchronize_session=False,
    )
    db.refresh(account)


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
    if not _apply_bucket_delta(
        db, account, bucket, available_delta=amount
    ):
        raise BillingError("grant failed: account row not updated")
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
        if not _apply_bucket_delta(
            db,
            account,
            bucket,
            available_delta=-take,
            reserved_delta=take,
            min_available=take,
        ):
            # A concurrent transaction took the balance first; the
            # guard inside the UPDATE saw the newest committed row.
            # Nothing in this transaction may stand — the caller
            # rolls back, discarding any earlier buckets' moves.
            db.refresh(account)
            raise InsufficientCreditsError(
                needed=amount, available=total_available(account)
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
        if not _apply_bucket_delta(
            db,
            account,
            bucket,
            available_delta=amount,
            reserved_delta=-amount,
            min_reserved=amount,
        ):
            continue
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
        if take > 0 and _apply_bucket_delta(
            db, account, bucket,
            reserved_delta=-take, min_reserved=take,
        ):
            from_reserved[bucket] = take
            remaining -= take

    for bucket in SPEND_ORDER:
        if remaining <= 0:
            break
        take = min(available(account, bucket), remaining)
        if take > 0 and _apply_bucket_delta(
            db, account, bucket,
            available_delta=-take, min_available=take,
        ):
            from_available[bucket] = take
            remaining -= take

    shortfall = remaining
    charged = total_charge - shortfall
    _bump_lifetime_spent(db, account, charged)

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


def spend(
    db,
    user_id: str,
    amount: int,
    *,
    idempotency_key: str,
    reason: str | None = None,
    project_id: str | None = None,
    details: dict | None = None,
    source: str = "user",
) -> dict:
    """Immediately debit `amount` from available balances in
    spend order (for priced operations that have no reservation
    lifecycle — script/thumbnail operations). Returns the split.
    Idempotent on idempotency_key (a replay returns the recorded
    split and moves nothing). Raises InsufficientCreditsError
    without moving anything."""
    if amount <= 0:
        raise BillingError("spend amount must be positive")
    existing = (
        db.query(models.CreditLedgerEntry)
        .filter(
            models.CreditLedgerEntry.idempotency_key.like(
                f"{idempotency_key}:%"
            ),
            models.CreditLedgerEntry.entry_type == "charge",
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
        if not _apply_bucket_delta(
            db, account, bucket,
            available_delta=-take, min_available=take,
        ):
            # Lost a race for this bucket's balance; fail closed —
            # the caller's transaction rollback discards any
            # earlier buckets' moves and entries.
            db.refresh(account)
            raise InsufficientCreditsError(
                needed=amount, available=total_available(account)
            )
        split[bucket] = take
        remaining -= take
        _append_entry(
            db,
            account,
            entry_type="charge",
            bucket=bucket,
            amount=-take,
            idempotency_key=f"{idempotency_key}:{bucket}",
            source=source,
            reason=reason,
            project_id=project_id,
            details=details,
        )
    _bump_lifetime_spent(db, account, amount)
    return split


def expire_amount(
    db,
    user_id: str,
    bucket: str,
    amount: int,
    *,
    idempotency_key: str,
    source: str = "system",
    reason: str | None = None,
    billing_period_id: str | None = None,
) -> int:
    """Expire up to `amount` of a bucket's AVAILABLE balance
    (partial expiry — purchased/promo lot expiry). Never touches
    reserved value. Returns the amount actually expired (0 when
    the key already applied or nothing is available)."""
    if bucket not in BUCKETS:
        raise BillingError(f"unknown bucket {bucket!r}")
    if amount <= 0:
        return 0
    if _existing_by_key(db, idempotency_key) is not None:
        return 0
    account = _lock_account(db, user_id)
    actual = min(amount, available(account, bucket))
    if actual <= 0:
        return 0
    setattr(
        account, f"{bucket}_available",
        available(account, bucket) - actual,
    )
    _append_entry(
        db,
        account,
        entry_type="expire",
        bucket=bucket,
        amount=-actual,
        idempotency_key=idempotency_key,
        source=source,
        reason=reason,
        billing_period_id=billing_period_id,
    )
    return actual


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
    (period-end subscription expiry). Reserved value is never
    expired — it belongs to in-flight generations. Returns the
    amount expired (0 = no entry)."""
    if bucket not in BUCKETS:
        raise BillingError(f"unknown bucket {bucket!r}")
    account = get_or_create_account(db, user_id)
    return expire_amount(
        db,
        user_id,
        bucket,
        available(account, bucket),
        idempotency_key=idempotency_key,
        source=source,
        reason=reason,
        billing_period_id=billing_period_id,
    )


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
