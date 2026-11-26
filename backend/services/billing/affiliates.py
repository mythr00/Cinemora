"""Affiliate programme (Product Improvements item 1).

Founder terms: **12% lifetime commission** on verified
payments by referred customers. The rate is internal platform
configuration (``CINEMORA_AFFILIATE_RATE_PERCENT``, default 12)
— affiliates can read it on their dashboard but never edit it,
and every ledger entry snapshots the rate it accrued at.

Hard rules:
- Attribution happens ONCE, at registration, from the referral
  code the new account carries; it is never rewritten.
- Commission accrues ONLY from verified payment events (the
  Paddle webhook path calls :func:`accrue_for_payment` after a
  payment is recorded from a verified event). No accrual from
  frontend success pages, checkout creation, or admin fiat.
- One accrual per payment, enforced by a UNIQUE idempotency
  key — duplicate webhook deliveries cannot double-accrue.
- Refunds and chargebacks create proportional REVERSAL entries;
  the accrual row is never edited. Reversed amounts are never
  payable.
- Cancellation stops future accrual naturally (no verified
  payments, no accrual). RESUBSCRIBE POLICY (documented):
  lifetime attribution persists, so a resubscription accrues to
  the same affiliate again.
- A suspended affiliate accrues nothing and gains no new
  attributions while suspended.
- NO payout provider is integrated and no real-money payout is
  activated: entries move pending → approved → paid by audited
  admin decision, and the CSV export is the payout worksheet
  until a provider is chosen (founder decision, see report).
"""

from __future__ import annotations

import csv
import io
import os
import secrets
import uuid
from datetime import datetime, timezone

import models

DEFAULT_RATE_PERCENT = 12

PAYOUT_PREFERENCES = ("bank_transfer", "paypal", "wise")


def rate_percent() -> int:
    raw = os.environ.get("CINEMORA_AFFILIATE_RATE_PERCENT")
    if raw:
        try:
            value = int(raw)
            if 0 < value <= 100:
                return value
        except ValueError:
            pass
    return DEFAULT_RATE_PERCENT


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------
# Profiles, clicks, attribution
# ---------------------------------------------------------------------


def profile_for(db, user_id: str) -> models.AffiliateProfile | None:
    return (
        db.query(models.AffiliateProfile)
        .filter_by(user_id=user_id)
        .one_or_none()
    )


def enroll(db, user_id: str) -> models.AffiliateProfile:
    existing = profile_for(db, user_id)
    if existing is not None:
        return existing
    for _ in range(5):
        profile = models.AffiliateProfile(
            id=str(uuid.uuid4()),
            user_id=user_id,
            code="CM-" + secrets.token_hex(4).upper(),
            status="active",
        )
        db.add(profile)
        try:
            db.commit()
            return profile
        except Exception:
            db.rollback()
            if profile_for(db, user_id) is not None:
                return profile_for(db, user_id)
    raise RuntimeError("could not allocate an affiliate code")


def set_payout_preference(
    db, user_id: str, preference: str | None
) -> models.AffiliateProfile:
    if preference is not None and preference not in PAYOUT_PREFERENCES:
        raise ValueError(f"unknown payout preference: {preference}")
    profile = enroll(db, user_id)
    profile.payout_preference = preference
    db.commit()
    return profile


def _profile_by_code(db, code: str) -> models.AffiliateProfile | None:
    if not code:
        return None
    return (
        db.query(models.AffiliateProfile)
        .filter_by(code=code.strip())
        .one_or_none()
    )


def record_click(db, code: str) -> bool:
    profile = _profile_by_code(db, code or "")
    if profile is None or profile.status != "active":
        return False
    db.add(
        models.AffiliateClick(
            id=str(uuid.uuid4()), affiliate_id=profile.id
        )
    )
    db.commit()
    return True


def attribute_registration(
    db, referred_user_id: str, referral_code: str | None
) -> models.AffiliateAttribution | None:
    """Create the lifetime attribution for a new account. Never
    raises for a bad/unknown/self code — registration must not
    depend on the referral being valid."""
    if not referral_code:
        return None
    profile = _profile_by_code(db, referral_code)
    if profile is None or profile.status != "active":
        return None
    if profile.user_id == referred_user_id:
        return None
    existing = (
        db.query(models.AffiliateAttribution)
        .filter_by(referred_user_id=referred_user_id)
        .one_or_none()
    )
    if existing is not None:
        return existing
    attribution = models.AffiliateAttribution(
        id=str(uuid.uuid4()),
        referred_user_id=referred_user_id,
        affiliate_id=profile.id,
        code=profile.code,
        source="registration",
    )
    db.add(attribution)
    db.commit()
    return attribution


def attribution_for(db, user_id: str) -> models.AffiliateAttribution | None:
    return (
        db.query(models.AffiliateAttribution)
        .filter_by(referred_user_id=user_id)
        .one_or_none()
    )


# ---------------------------------------------------------------------
# Ledger: accrual + reversal (verified payment events only)
# ---------------------------------------------------------------------


def accrue_for_payment(db, payment: models.Payment):
    """Accrue commission for a payment recorded from a VERIFIED
    provider event. Idempotent per payment. Returns the entry,
    or None when there is nothing to accrue (no attribution,
    suspended affiliate, non-completed payment, zero amount)."""
    if payment is None or payment.status != "completed":
        return None
    if not payment.amount_cents or payment.amount_cents <= 0:
        return None
    key = f"affiliate:payment:{payment.id}"
    existing = (
        db.query(models.CommissionLedgerEntry)
        .filter_by(idempotency_key=key)
        .one_or_none()
    )
    if existing is not None:
        return existing
    attribution = attribution_for(db, payment.user_id)
    if attribution is None:
        return None
    profile = db.get(models.AffiliateProfile, attribution.affiliate_id)
    if profile is None or profile.status != "active":
        return None
    rate = rate_percent()
    commission = (int(payment.amount_cents) * rate) // 100
    entry = models.CommissionLedgerEntry(
        id=str(uuid.uuid4()),
        affiliate_id=profile.id,
        referred_user_id=payment.user_id,
        payment_id=payment.id,
        kind="accrual",
        gross_cents=int(payment.amount_cents),
        rate_percent=rate,
        commission_cents=commission,
        status="pending",
        idempotency_key=key,
    )
    db.add(entry)
    db.commit()
    return entry


def reverse_for_refund(db, payment: models.Payment):
    """Create the proportional reversal for a payment's current
    cumulative refunded amount. Idempotent per (payment,
    refunded total): successive partial refunds create only
    their delta. Full refund/chargeback reverses everything and
    marks the accrual reversed."""
    if payment is None:
        return None
    accrual = (
        db.query(models.CommissionLedgerEntry)
        .filter_by(
            idempotency_key=f"affiliate:payment:{payment.id}",
            kind="accrual",
        )
        .one_or_none()
    )
    if accrual is None:
        return None
    refunded = int(payment.refunded_amount_cents or 0)
    if refunded <= 0 or not payment.amount_cents:
        return None
    key = f"affiliate:reversal:{payment.id}:{refunded}"
    existing = (
        db.query(models.CommissionLedgerEntry)
        .filter_by(idempotency_key=key)
        .one_or_none()
    )
    if existing is not None:
        return existing
    already = (
        db.query(models.CommissionLedgerEntry)
        .filter_by(reversal_of_id=accrual.id, kind="reversal")
        .all()
    )
    already_reversed = sum(int(e.commission_cents) for e in already)
    target = (accrual.commission_cents * refunded) // int(
        payment.amount_cents
    )
    delta = target - already_reversed
    if delta <= 0:
        return None
    entry = models.CommissionLedgerEntry(
        id=str(uuid.uuid4()),
        affiliate_id=accrual.affiliate_id,
        referred_user_id=accrual.referred_user_id,
        payment_id=payment.id,
        kind="reversal",
        gross_cents=refunded,
        rate_percent=accrual.rate_percent,
        commission_cents=delta,
        status="reversed",
        idempotency_key=key,
        reversal_of_id=accrual.id,
    )
    db.add(entry)
    if refunded >= int(payment.amount_cents):
        accrual.status = "reversed"
    db.commit()
    return entry


# ---------------------------------------------------------------------
# Dashboard + admin
# ---------------------------------------------------------------------


def _entry_public(entry: models.CommissionLedgerEntry) -> dict:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "gross_cents": entry.gross_cents,
        "rate_percent": entry.rate_percent,
        "commission_cents": entry.commission_cents,
        "status": entry.status,
        "referred_user_id": entry.referred_user_id,
        "payment_id": entry.payment_id,
        "decision_reason": entry.decision_reason,
        "created_at": entry.created_at.isoformat()
        if entry.created_at
        else None,
    }


def dashboard(db, user_id: str) -> dict:
    profile = profile_for(db, user_id)
    if profile is None:
        return {
            "enrolled": False,
            "rate_percent": rate_percent(),
            "lifetime": True,
        }
    clicks = (
        db.query(models.AffiliateClick)
        .filter_by(affiliate_id=profile.id)
        .count()
    )
    attributions = (
        db.query(models.AffiliateAttribution)
        .filter_by(affiliate_id=profile.id)
        .all()
    )
    entries = (
        db.query(models.CommissionLedgerEntry)
        .filter_by(affiliate_id=profile.id)
        .order_by(models.CommissionLedgerEntry.created_at.desc())
        .all()
    )
    referred_ids = {a.referred_user_id for a in attributions}
    active_customers = 0
    if referred_ids:
        active_customers = (
            db.query(models.UserSubscription)
            .filter(
                models.UserSubscription.user_id.in_(referred_ids),
                models.UserSubscription.status.in_(
                    ("active", "past_due")
                ),
            )
            .count()
        )

    def total(status: str, kind: str = "accrual") -> int:
        return sum(
            int(e.commission_cents)
            for e in entries
            if e.status == status and e.kind == kind
        )

    reversed_total = sum(
        int(e.commission_cents) for e in entries if e.kind == "reversal"
    )
    pending = total("pending") - 0  # reversals tracked separately
    lifetime_earned = sum(
        int(e.commission_cents) for e in entries if e.kind == "accrual"
    ) - reversed_total
    return {
        "enrolled": True,
        "code": profile.code,
        "status": profile.status,
        "rate_percent": rate_percent(),
        "lifetime": True,
        "payout_preference": profile.payout_preference,
        "payout_preferences": list(PAYOUT_PREFERENCES),
        "payout_provider_connected": False,
        "metrics": {
            "link_clicks": clicks,
            "registrations": len(attributions),
            "referred_customers": len(attributions),
            "active_customers": active_customers,
            "conversion_percent": (
                round(100.0 * len(attributions) / clicks, 1)
                if clicks
                else None
            ),
            "lifetime_earnings_cents": lifetime_earned,
            "pending_cents": pending,
            "approved_cents": total("approved"),
            "paid_cents": total("paid"),
            "reversed_cents": reversed_total,
        },
        "recent_activity": [_entry_public(e) for e in entries[:20]],
        "payout_history": [
            _entry_public(e) for e in entries if e.status == "paid"
        ],
    }


def decide_entry(
    db,
    entry_id: str,
    *,
    decision: str,
    decided_by: str,
    reason: str | None = None,
) -> models.CommissionLedgerEntry:
    entry = db.get(models.CommissionLedgerEntry, entry_id)
    if entry is None:
        raise LookupError("commission entry not found")
    if entry.kind != "accrual":
        raise ValueError("only accrual entries can be decided")
    if decision == "approve":
        if entry.status != "pending":
            raise ValueError(f"cannot approve a {entry.status} entry")
        entry.status = "approved"
    elif decision == "reject":
        if entry.status not in ("pending", "approved"):
            raise ValueError(f"cannot reject a {entry.status} entry")
        if not reason:
            raise ValueError("rejection requires a reason")
        entry.status = "rejected"
    elif decision == "paid":
        if entry.status != "approved":
            raise ValueError("only approved entries can be marked paid")
        entry.status = "paid"
    else:
        raise ValueError(f"unknown decision: {decision}")
    entry.decided_by = decided_by
    entry.decided_at = _now()
    entry.decision_reason = reason
    db.commit()
    return entry


def set_affiliate_status(
    db, affiliate_id: str, *, status: str
) -> models.AffiliateProfile:
    if status not in ("active", "suspended"):
        raise ValueError(f"unknown affiliate status: {status}")
    profile = db.get(models.AffiliateProfile, affiliate_id)
    if profile is None:
        raise LookupError("affiliate not found")
    profile.status = status
    db.commit()
    return profile


def admin_overview(db) -> dict:
    profiles = db.query(models.AffiliateProfile).all()
    entries = db.query(models.CommissionLedgerEntry).all()
    rows = []
    for profile in profiles:
        own = [e for e in entries if e.affiliate_id == profile.id]
        user = db.get(models.User, profile.user_id)
        rows.append(
            {
                "id": profile.id,
                "user_id": profile.user_id,
                "email": user.email if user else None,
                "code": profile.code,
                "status": profile.status,
                "payout_preference": profile.payout_preference,
                "accrued_cents": sum(
                    int(e.commission_cents)
                    for e in own
                    if e.kind == "accrual"
                ),
                "reversed_cents": sum(
                    int(e.commission_cents)
                    for e in own
                    if e.kind == "reversal"
                ),
                "pending_cents": sum(
                    int(e.commission_cents)
                    for e in own
                    if e.kind == "accrual" and e.status == "pending"
                ),
            }
        )
    return {"affiliates": rows, "rate_percent": rate_percent()}


def list_entries(db, *, status: str | None = None) -> list[dict]:
    query = db.query(models.CommissionLedgerEntry)
    if status:
        query = query.filter_by(status=status)
    return [
        _entry_public(e)
        for e in query.order_by(
            models.CommissionLedgerEntry.created_at.desc()
        ).all()
    ]


def export_csv(db) -> str:
    """The payout worksheet: every ledger entry, oldest first.
    Until a payout provider is chosen (founder decision), this
    export is how payouts are prepared and reconciled."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "entry_id", "affiliate_id", "affiliate_email",
            "referred_user_id", "payment_id", "kind", "gross_cents",
            "rate_percent", "commission_cents", "status",
            "decided_by", "decision_reason", "created_at",
        ]
    )
    entries = (
        db.query(models.CommissionLedgerEntry)
        .order_by(models.CommissionLedgerEntry.created_at.asc())
        .all()
    )
    for entry in entries:
        profile = db.get(models.AffiliateProfile, entry.affiliate_id)
        user = (
            db.get(models.User, profile.user_id) if profile else None
        )
        writer.writerow(
            [
                entry.id, entry.affiliate_id,
                user.email if user else "",
                entry.referred_user_id, entry.payment_id or "",
                entry.kind, entry.gross_cents, entry.rate_percent,
                entry.commission_cents, entry.status,
                entry.decided_by or "", entry.decision_reason or "",
                entry.created_at.isoformat() if entry.created_at else "",
            ]
        )
    return buffer.getvalue()
