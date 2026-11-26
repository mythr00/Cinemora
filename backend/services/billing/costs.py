"""Production cost engine + financial reports (Phase 8;
directive §13).

Costs are computed from the B-10 provider usage ledger and the
pricing_rules table. With no rule configured, a cost is unknown
(None) — never zero, never estimated, never invented (the
platform's standing F-14 rule). Every aggregate reports its
priced coverage (priced_events / total_events) so partial
knowledge is visible instead of silently averaged in.

Master Implementation (2026-11-26, Part 4): the table no longer
ships empty for SEARCH. ensure_pricing_rules() seeds the two
search rules whose prices are actually known (see
SEEDED_PRICING_RULES below). Everything else — the LLM provider's
billed rate above all — stays unseeded and therefore honestly
"unknown" until an admin configures a verified rate; the
profitability reporting labels those components ESTIMATED from
measured usage instead of inventing a ledger price.

Report metrics that cannot be computed from real records return
{"value": None, "status": "unknown", "reason": ...} — the B-10
honesty pattern — rather than a fabricated number.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import models


#: Search cost rule seeded by ensure_pricing_rules (Master
#: Implementation Part 4). SearXNG — the sole search provider
#: since the 2026-11-26 removal — is self-hosted: its MARGINAL
#: cost per search is $0. The instance's infrastructure cost is
#: attributed in the profitability report from measured resource
#: use — never folded into a per-search price, which would
#: misstate both numbers. effective_from is set in the past so
#: every recorded search event prices.
SEEDED_PRICING_RULES = (
    {"provider": "searxng", "model": None, "unit": "per_search",
     "rate_microusd": 0},
)

_SEED_EFFECTIVE_FROM = datetime(2026, 1, 1, tzinfo=timezone.utc)


def ensure_pricing_rules(db) -> dict:
    """Seed the known search pricing rules (idempotent).

    Only fills gaps: an existing rule for the same
    provider/model/unit (e.g. an admin-adjusted rate) is never
    overwritten.
    """
    created: list[str] = []
    for spec in SEEDED_PRICING_RULES:
        exists = (
            db.query(models.PricingRule)
            .filter_by(
                provider=spec["provider"],
                model=spec["model"],
                unit=spec["unit"],
            )
            .first()
        )
        if exists is not None:
            continue
        db.add(models.PricingRule(
            id=str(uuid.uuid4()),
            provider=spec["provider"],
            model=spec["model"],
            unit=spec["unit"],
            rate_microusd=spec["rate_microusd"],
            effective_from=_SEED_EFFECTIVE_FROM,
        ))
        created.append(spec["provider"])
    if created:
        db.flush()
    return {"pricing_rules_created": created}


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _applicable_rule(rules, event):
    """The rule that prices one event: same provider, model match
    preferred over the provider default (model NULL), effective
    at the event's creation time."""
    candidates = []
    for rule in rules:
        if rule.provider != event.provider:
            continue
        if rule.model is not None and rule.model != event.model:
            continue
        start = _aware(rule.effective_from)
        end = _aware(rule.effective_to)
        created = _aware(event.created_at)
        if start and created and created < start:
            continue
        if end and created and created >= end:
            continue
        candidates.append(rule)
    if not candidates:
        return None
    # Model-specific beats provider-default; latest effective wins.
    candidates.sort(
        key=lambda r: (
            r.model is not None,
            _aware(r.effective_from) or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    return candidates[0]


def price_event(rule, event) -> int | None:
    """Micro-USD cost of one usage event under one rule, or None
    when the event lacks the measurement the rule's unit needs
    (unknown stays unknown)."""
    if rule is None:
        return None
    if rule.unit == "per_call":
        return int(rule.rate_microusd)
    if rule.unit == "per_1k_tokens":
        tokens = event.total_tokens
        if tokens is None:
            tokens = (
                (event.prompt_tokens or 0)
                + (event.completion_tokens or 0)
                if (event.prompt_tokens is not None
                    or event.completion_tokens is not None)
                else None
            )
        if tokens is None:
            return None
        return int(tokens * int(rule.rate_microusd) / 1000)
    if rule.unit == "per_search":
        # One usage-ledger event == one search call: the search
        # provider choke point records one event per attempt
        # (Master Implementation Parts 1/4).
        return int(rule.rate_microusd)
    # per_minute / per_gb_month rules price other record types,
    # not usage-ledger events.
    return None


def rollup(db, *, since: datetime | None = None,
           until: datetime | None = None) -> dict:
    """Price the usage ledger over a window."""
    rules = db.query(models.PricingRule).all()
    query = db.query(models.ProviderUsageEvent)
    if since is not None:
        query = query.filter(models.ProviderUsageEvent.created_at >= since)
    if until is not None:
        query = query.filter(models.ProviderUsageEvent.created_at < until)
    events = query.all()

    total_microusd = 0
    priced = 0
    by_provider: dict[str, dict] = {}
    cost_by_run: dict[str, int] = {}
    priced_by_run: dict[str, int] = {}
    for event in events:
        rule = _applicable_rule(rules, event)
        cost = price_event(rule, event)
        bucket = by_provider.setdefault(
            event.provider,
            {"events": 0, "priced_events": 0, "cost_microusd": 0},
        )
        bucket["events"] += 1
        if cost is not None:
            priced += 1
            total_microusd += cost
            bucket["priced_events"] += 1
            bucket["cost_microusd"] += cost
            if event.run_id:
                cost_by_run[event.run_id] = (
                    cost_by_run.get(event.run_id, 0) + cost
                )
                priced_by_run[event.run_id] = (
                    priced_by_run.get(event.run_id, 0) + 1
                )
    return {
        "total_events": len(events),
        "priced_events": priced,
        "unpriced_events": len(events) - priced,
        "priced_coverage": (
            priced / len(events) if events else 0.0
        ),
        "total_cost_microusd": total_microusd if priced else None,
        "total_cost_usd": (
            round(total_microusd / 1_000_000, 4) if priced else None
        ),
        "pricing_status": (
            "configured" if priced else "not_configured"
        ),
        "by_provider": by_provider,
        "cost_by_run": cost_by_run,
        "priced_events_by_run": priced_by_run,
    }


def _unknown(reason: str) -> dict:
    return {"value": None, "status": "unknown", "reason": reason}


def financial_report(db, *, days: int = 30) -> dict:
    """The §13 admin report. Revenue comes only from verified
    payment rows; MRR is labelled contracted (active subscriptions
    x configured plan prices), never presented as collected."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    payments = (
        db.query(models.Payment)
        .filter(models.Payment.status == "completed")
        .all()
    )
    revenue_cents = sum(p.amount_cents for p in payments)
    revenue_period = sum(
        p.amount_cents
        for p in payments
        if _aware(p.created_at) and _aware(p.created_at) >= since
    )

    active_subs = (
        db.query(models.UserSubscription)
        .filter_by(status="active")
        .all()
    )
    mrr_cents = 0
    subs_by_plan: dict[str, int] = {}
    for sub in active_subs:
        plan = db.get(models.Plan, sub.plan_id)
        if plan is None:
            continue
        subs_by_plan[plan.name] = subs_by_plan.get(plan.name, 0) + 1
        if plan.billing_mode != "metered":
            continue
        if sub.billing_interval == "year" and plan.price_annual_cents:
            mrr_cents += plan.price_annual_cents // 12
        elif plan.price_monthly_cents:
            mrr_cents += plan.price_monthly_cents

    cancelled_period = (
        db.query(models.UserSubscription)
        .filter(
            models.UserSubscription.status.in_(("cancelled", "expired")),
        )
        .count()
    )

    accounts = db.query(models.CreditAccount).all()
    credits_outstanding = sum(
        a.subscription_available + a.purchased_available + a.promo_available
        for a in accounts
    )
    charges = (
        db.query(models.CreditLedgerEntry)
        .filter(
            models.CreditLedgerEntry.entry_type == "charge",
            models.CreditLedgerEntry.created_at >= since,
        )
        .all()
    )
    credits_consumed = sum(-e.amount for e in charges if e.amount < 0)

    usage = rollup(db, since=since)

    # Cost per finished minute: priced cost of settled runs over
    # their measured minutes. Unknown until both pricing rules
    # and settled generations with measured durations exist.
    settled = (
        db.query(models.GenerationReservation)
        .filter_by(status="settled")
        .all()
    )
    finished_minutes = sum(
        (r.actual_seconds or 0) / 60.0
        for r in settled
        if r.actual_seconds
    )
    settled_cost = None
    if usage["cost_by_run"] and finished_minutes > 0:
        total = sum(
            usage["cost_by_run"].get(r.run_id, 0) for r in settled
        )
        priced_runs = sum(
            1 for r in settled if r.run_id in usage["cost_by_run"]
        )
        if priced_runs:
            settled_cost = {
                "value": round(total / finished_minutes, 2),
                "unit": "microusd_per_finished_minute",
                "priced_runs": priced_runs,
                "settled_runs": len(settled),
            }
    if settled_cost is None:
        settled_cost = _unknown(
            "no priced provider usage for settled generations yet "
            "(pricing rules unconfigured or no settled runs)"
        )

    failed = (
        db.query(models.GenerationReservation)
        .filter_by(status="released")
        .all()
    )
    failed_cost = None
    if usage["cost_by_run"] and failed:
        failed_cost = {
            "value": sum(
                usage["cost_by_run"].get(r.run_id, 0) for r in failed
            ),
            "unit": "microusd",
            "released_reservations": len(failed),
        }
    if failed_cost is None:
        failed_cost = _unknown(
            "failed-generation provider cost is unpriced "
            "(pricing rules unconfigured)"
        )

    return {
        "period_days": days,
        "revenue": {
            "total_cents": revenue_cents,
            "period_cents": revenue_period,
            "source": "verified payments only",
        },
        "mrr_contracted_cents": mrr_cents,
        "arr_contracted_cents": mrr_cents * 12,
        "active_subscriptions": len(active_subs),
        "subscriptions_by_plan": subs_by_plan,
        "cancelled_or_expired_subscriptions": cancelled_period,
        "arpu_cents": (
            round(revenue_period / len(active_subs))
            if active_subs
            else None
        ),
        "credits_outstanding": credits_outstanding,
        "credits_consumed_period": credits_consumed,
        "provider_usage": {
            key: usage[key]
            for key in (
                "total_events", "priced_events", "unpriced_events",
                "priced_coverage", "total_cost_microusd",
                "total_cost_usd", "pricing_status", "by_provider",
            )
        },
        "cost_per_finished_minute": settled_cost,
        "failed_generation_cost": failed_cost,
        "gross_margin": _unknown(
            "gross margin needs priced production costs; "
            "pricing rules are unconfigured"
        ) if not usage["priced_events"] else {
            "value": None,
            "status": "partial",
            "reason": "priced coverage is partial; margin would "
                      "misstate — see provider_usage.priced_coverage",
        },
    }
