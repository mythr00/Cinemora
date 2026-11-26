"""Billing catalogue (Phase 8; founder directive 2026-11-25 §§1–8).

Single source of truth for:
- the platform credit rate (one rate for every plan — plan
  differences are allowances, never the price of a minute);
- the founder-priced metered plans and top-up packages, synced
  idempotently into the plans / topup_packages tables by
  ensure_billing_catalogue() (also run by scripts/seed_founder.py).

Credit model (design: PHASE8-STAGE2-DESIGN.md §1): each metered
plan's advertised documentaries are INCLUDED GENERATION
ENTITLEMENTS — a generation whose measured duration is at most
the standard 15 minutes consumes one entitlement and zero
credits, so the advertised 11 / 29 / 59 documentaries are
deliverable by construction. Credits are the universal overflow
currency: minutes beyond the entitlement's 15, generations with
no entitlement left, and revision renders are charged at the
platform rate. The founder's figures are per-documentary prices
($2.64 / $2.72 / $3.03 per doc across the plans) — coherent as
entitlements, irreconcilable as a flat per-documentary credit
price, which is why no plan-specific credit price exists here.

Platform configuration in this codebase is environment-based
(services/admin_settings.py), so the rate is a code default with
an environment override, labelled applies-on-restart; per-plan
prices, credits and allowances are database rows the admin API
can edit at runtime.
"""

from __future__ import annotations

import math
import os
import uuid

# The standard documentary the founder priced against.
STANDARD_GENERATION_MINUTES = 15
STANDARD_GENERATION_SECONDS = STANDARD_GENERATION_MINUTES * 60

# Narration pace used for pre-generation estimates (the Script
# Studio convention: 145 words per minute).
WORDS_PER_MINUTE = 145

# Script / thumbnail operation credit costs beyond the included
# allowances (directive §7). Founder table prices generations and
# top-ups only; these are the design's engineering defaults and
# are plain configuration, like the rate.
SCRIPT_OPERATION_CREDITS = {
    "draft": 12,
    "expand": 4,
    "tighten": 4,
    "analysis": 4,
    "fact_check": 4,
    "translate": 8,
    "channel_research": 6,
}
THUMBNAIL_OPERATION_CREDITS = {
    "headlines": 2,
    "compose": 1,
}


def standard_generation_credits() -> int:
    """Credits charged for one standard (15-minute) generation.

    Chosen so every plan can deliver its advertised documentaries
    from credits alone if entitlements were ever exhausted early:
    the binding constraint is Agency (5000 / 59 ≈ 84.7), so the
    rate is 84 — at that rate Creator's 1,350 credits alone cover
    16 standard generations and Studio's 2,500 cover 29.
    """
    raw = os.environ.get("CINEMORA_STANDARD_GENERATION_CREDITS")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return 84


def generation_credit_cost(duration_seconds: float) -> int:
    """Credit cost of a generation of the given measured (or
    estimated) duration, at the single platform rate.

    cost = ceil(seconds * rate / 900). At the default rate:
    5 min 28, 10 min 56, 15 min 84, 20 min 112, 30 min 168,
    45 min 252, 60 min 336. A zero/negative duration costs the
    minimum of one credit-second unit — callers pass real
    measured durations; estimates come from estimate_seconds().
    """
    if duration_seconds is None or duration_seconds <= 0:
        duration_seconds = 1.0
    rate = standard_generation_credits()
    return max(
        1,
        math.ceil(duration_seconds * rate / STANDARD_GENERATION_SECONDS),
    )


def estimate_seconds(script_text: str) -> float:
    """Pre-generation duration estimate from script word count at
    the platform narration pace. Used for reservation sizing and
    the Create-page estimate; settlement always uses the measured
    duration."""
    words = len((script_text or "").split())
    return words / WORDS_PER_MINUTE * 60.0


def duration_cost_table() -> list[dict]:
    """The published duration → credits table (directive §5)."""
    return [
        {
            "minutes": minutes,
            "credits": generation_credit_cost(minutes * 60),
        }
        for minutes in (5, 10, 15, 20, 30, 45, 60)
    ]


# ---------------------------------------------------------------------
# Founder-priced catalogue (directive §§2, 3, 8). Annual = monthly
# x 12 x 0.8, charged upfront; benefit periods stay monthly.
# ---------------------------------------------------------------------

METERED_PLANS: list[dict] = [
    {
        "name": "creator",
        "price_monthly_cents": 2900,
        "price_annual_cents": 27840,
        "monthly_credits": 1350,
        "included_generations": 11,
        "script_allowance": 30,
        "thumbnail_allowance": 30,
        "max_concurrent_runs": 2,
        "priority_tier": 0,
        "features": {"team_members": 1},
    },
    {
        "name": "studio",
        "price_monthly_cents": 7900,
        "price_annual_cents": 75840,
        "monthly_credits": 2500,
        "included_generations": 29,
        "script_allowance": 90,
        "thumbnail_allowance": 90,
        "max_concurrent_runs": 4,
        "priority_tier": 1,
        "features": {"team_members": 5},
    },
    {
        "name": "agency",
        "price_monthly_cents": 17900,
        "price_annual_cents": 171840,
        "monthly_credits": 5000,
        "included_generations": 59,
        "script_allowance": 300,
        "thumbnail_allowance": 300,
        "max_concurrent_runs": 8,
        "priority_tier": 1,
        "features": {"team_members": 25},
    },
]

# Commercial fields the sync owns on existing rows (prices and
# allowances are catalogue truth; limits below are creation-time
# defaults only, so later admin limit edits are never clobbered).
_PLAN_COMMERCIAL_FIELDS = (
    "price_monthly_cents",
    "price_annual_cents",
    "monthly_credits",
    "included_generations",
    "script_allowance",
    "thumbnail_allowance",
    "billing_mode",
    "currency",
    "is_public",
    "features",
)

TOPUP_PACKAGES: list[dict] = [
    {"name": "topup-120", "credits": 120, "price_cents": 800,
     "sort_order": 1},
    {"name": "topup-300", "credits": 300, "price_cents": 1800,
     "sort_order": 2},
    {"name": "topup-600", "credits": 600, "price_cents": 3200,
     "sort_order": 3},
    {"name": "topup-1200", "credits": 1200, "price_cents": 5900,
     "sort_order": 4},
    {"name": "topup-2400", "credits": 2400, "price_cents": 10900,
     "sort_order": 5},
]

_PACKAGE_COMMERCIAL_FIELDS = (
    "credits",
    "price_cents",
    "validity_months",
    "sort_order",
)


def ensure_billing_catalogue(db) -> dict:
    """Idempotently sync the metered plans and top-up packages.

    Creates missing rows; on existing rows updates only the
    commercial fields (see _PLAN_COMMERCIAL_FIELDS) when they
    differ. Returns a change report; the caller commits.
    """
    import models

    report = {
        "plans_created": [],
        "plans_updated": [],
        "packages_created": [],
        "packages_updated": [],
    }

    for spec in METERED_PLANS:
        plan = (
            db.query(models.Plan).filter_by(name=spec["name"]).one_or_none()
        )
        desired = {
            "price_monthly_cents": spec["price_monthly_cents"],
            "price_annual_cents": spec["price_annual_cents"],
            "monthly_credits": spec["monthly_credits"],
            "included_generations": spec["included_generations"],
            "script_allowance": spec["script_allowance"],
            "thumbnail_allowance": spec["thumbnail_allowance"],
            "billing_mode": "metered",
            "currency": "USD",
            "is_public": True,
            "features": spec["features"],
        }
        if plan is None:
            plan = models.Plan(
                id=str(uuid.uuid4()),
                name=spec["name"],
                max_concurrent_runs=spec["max_concurrent_runs"],
                max_runs_per_period=None,
                period="month",
                max_script_chars=60_000,
                max_storage_bytes=None,
                max_upload_bytes=None,
                priority_tier=spec["priority_tier"],
                is_default=False,
                **desired,
            )
            db.add(plan)
            db.flush()
            report["plans_created"].append(spec["name"])
        else:
            changed = [
                field
                for field in _PLAN_COMMERCIAL_FIELDS
                if getattr(plan, field) != desired[field]
            ]
            if changed:
                for field in changed:
                    setattr(plan, field, desired[field])
                report["plans_updated"].append(
                    {"name": spec["name"], "fields": changed}
                )

    for spec in TOPUP_PACKAGES:
        package = (
            db.query(models.TopupPackage)
            .filter_by(name=spec["name"])
            .one_or_none()
        )
        desired = {
            "credits": spec["credits"],
            "price_cents": spec["price_cents"],
            "validity_months": 12,
            "sort_order": spec["sort_order"],
        }
        if package is None:
            package = models.TopupPackage(
                id=str(uuid.uuid4()),
                name=spec["name"],
                currency="USD",
                is_active=True,
                **desired,
            )
            db.add(package)
            db.flush()
            report["packages_created"].append(spec["name"])
        else:
            changed = [
                field
                for field in _PACKAGE_COMMERCIAL_FIELDS
                if getattr(package, field) != desired[field]
            ]
            if changed:
                for field in changed:
                    setattr(package, field, desired[field])
                report["packages_updated"].append(
                    {"name": spec["name"], "fields": changed}
                )

    return report
