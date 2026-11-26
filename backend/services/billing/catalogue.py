"""Billing catalogue (Phase 8; founder directive 2026-11-25 §§1–8).

Single source of truth for:
- the per-tier credit rates (credits per finished minute of
  delivered video — plans differ in rate, allowances AND price);
- the founder-priced metered plans and top-up packages, synced
  idempotently into the plans / topup_packages tables by
  ensure_billing_catalogue() (also run by scripts/seed_founder.py).

Credit model — PRICING RESTRUCTURE (founder directive
2026-11-26 ~10:25 WAT, PRICING_RESTRUCTURE_DIRECTIVE_2026-11-26.md;
supersedes the Master Implementation directive's Part 2 figures
of the same morning and the Phase 8 entitlement model):

- Every delivered video is charged in credits for its ACTUAL
  delivered duration at the subscriber's tier rate (Creator 6,
  Studio 5, Agency 4 credits per finished minute; rounding is
  ceil to the next whole credit on the total, minimum 1).
  Internal stages (research, narration, rendering, ...) are
  never charged separately.
- Independently of the wallet, each plan carries a monthly
  DELIVERED-VIDEO COUNT entitlement (11 / 31 / 61): every
  successfully delivered video counts as exactly 1 toward the
  count, regardless of its duration. The counting rule is
  therefore transparent: for videos of 15 minutes or less the
  count binds first; for longer videos the wallet binds first
  (a 30-minute Creator video costs 180 of the 990 credits but
  still counts as 1 of the 11). A run reserves one count unit
  at submission; failed / cancelled / quality-rejected runs
  restore it, so only delivered videos consume the count.
  The plan figures are exact by construction: 11 x 90 = 990,
  31 x 75 = 2,325, 61 x 60 = 3,660 — at the standard 15 minutes
  the count and the wallet are exhausted together.
- Script and thumbnail allowances are SEPARATE monthly counters
  (11 / 31 / 61 each), tracked apart from the video wallet:
  using an included script or thumbnail never touches video
  credits. Beyond the allowance, a script operation costs 3
  credits and a thumbnail generation 5 credits, success-only.
- Failed / cancelled / quality-rejected generations restore
  credits and the count unit in full (the settlement contract in
  services/billing/generation_billing.py).

Platform configuration in this codebase is environment-based
(services/admin_settings.py), so the default rate is a code
default with an environment override, labelled applies-on-
restart; per-plan prices, credits, allowances and tier rates
are database rows the admin API can edit at runtime (the tier
rate travels in the plan's synced `features`).
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
# allowances (restructure directive 2026-11-26): EVERY script
# operation costs exactly 3 credits; a thumbnail GENERATION
# (compose / variants) costs exactly 5. Headline suggestion is a
# separate optional AI assist at the same 3-credit assist price
# as script operations — it is never bundled into a compose
# charge, so one thumbnail generation is never charged twice.
SCRIPT_OPERATION_CREDITS = {
    "draft": 3,
    "expand": 3,
    "tighten": 3,
    "analysis": 3,
    "fact_check": 3,
    "translate": 3,
    "channel_research": 3,
}
THUMBNAIL_OPERATION_CREDITS = {
    "headlines": 3,
    "compose": 5,
}

# The entry tier's rate: charged to tenants with no metered plan
# (the item-8 credits-only path) and used wherever a single
# platform default rate is needed.
DEFAULT_CREDITS_PER_MINUTE = 6


def standard_generation_credits() -> int:
    """Credits charged for one standard (15-minute) generation at
    the DEFAULT (entry-tier) rate: 15 x 6 = 90. The environment
    override (CINEMORA_STANDARD_GENERATION_CREDITS) still exists
    as a platform escape hatch and re-derives the default rate.
    """
    raw = os.environ.get("CINEMORA_STANDARD_GENERATION_CREDITS")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_CREDITS_PER_MINUTE * STANDARD_GENERATION_MINUTES


def default_credits_per_minute() -> float:
    """The platform default per-minute rate (entry tier)."""
    return standard_generation_credits() / STANDARD_GENERATION_MINUTES


def plan_credits_per_minute(plan) -> int:
    """The per-finished-minute credit rate of a plan, read from
    its synced features (generation_credits_per_minute). Legacy /
    missing plans and unparseable values fall back to the
    platform default rate."""
    features = getattr(plan, "features", None) or {}
    try:
        value = int(features.get("generation_credits_per_minute"))
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return int(default_credits_per_minute())


def generation_credit_cost(
    duration_seconds: float, credits_per_minute: float | None = None
) -> int:
    """Credit cost of a generation of the given measured (or
    estimated) duration at a per-minute rate.

    cost = ceil(seconds x rate / 60), minimum 1 — the single
    documented rounding rule (restructure directive: consistent
    documented rounding; never charge for undelivered duration).
    At the Creator rate (6/min): 5 min 30, 10 min 60, 15 min 90,
    20 min 120, 30 min 180. At Studio (5): 25/50/75/100/150. At
    Agency (4): 20/40/60/80/120. `credits_per_minute=None` uses
    the platform default rate. A zero/negative duration is
    treated as one second — callers pass real measured durations;
    estimates come from estimate_seconds().
    """
    if duration_seconds is None or duration_seconds <= 0:
        duration_seconds = 1.0
    rate = (
        default_credits_per_minute()
        if credits_per_minute is None
        else float(credits_per_minute)
    )
    return max(1, math.ceil(duration_seconds * rate / 60.0))


def estimate_seconds(script_text: str) -> float:
    """Pre-generation duration estimate from script word count at
    the platform narration pace. Used for reservation sizing and
    the Create-page estimate; settlement always uses the measured
    duration."""
    words = len((script_text or "").split())
    return words / WORDS_PER_MINUTE * 60.0


def duration_cost_table(
    credits_per_minute: float | None = None,
) -> list[dict]:
    """The published duration → credits table at one rate
    (default rate when unspecified)."""
    return [
        {
            "minutes": minutes,
            "credits": generation_credit_cost(
                minutes * 60, credits_per_minute
            ),
        }
        for minutes in (5, 10, 15, 20, 30, 45, 60)
    ]


# ---------------------------------------------------------------------
# Founder-priced catalogue (restructure directive 2026-11-26).
# Annual = monthly x 12 x 0.8 (the pre-existing -20% structure,
# unchanged; only the base prices moved), charged upfront;
# benefit periods stay monthly. Effective price per standard
# video: Creator 2800/11 = $2.55, Studio 6300/31 = $2.03,
# Agency 11100/61 = $1.82 (best value).
# ---------------------------------------------------------------------

METERED_PLANS: list[dict] = [
    {
        "name": "creator",
        "price_monthly_cents": 2800,
        "price_annual_cents": 26880,
        "monthly_credits": 990,
        "included_generations": 11,
        "script_allowance": 11,
        "thumbnail_allowance": 11,
        "max_concurrent_runs": 2,
        "priority_tier": 0,
        "features": {
            "team_members": 1,
            "generation_credits_per_minute": 6,
        },
    },
    {
        "name": "studio",
        "price_monthly_cents": 6300,
        "price_annual_cents": 60480,
        "monthly_credits": 2325,
        "included_generations": 31,
        "script_allowance": 31,
        "thumbnail_allowance": 31,
        "max_concurrent_runs": 4,
        "priority_tier": 1,
        "features": {
            "team_members": 5,
            "generation_credits_per_minute": 5,
        },
    },
    {
        "name": "agency",
        "price_monthly_cents": 11100,
        "price_annual_cents": 106560,
        "monthly_credits": 3660,
        "included_generations": 61,
        "script_allowance": 61,
        "thumbnail_allowance": 61,
        "max_concurrent_runs": 8,
        "priority_tier": 1,
        "features": {
            "team_members": 25,
            "generation_credits_per_minute": 4,
        },
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
