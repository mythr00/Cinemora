"""Master Implementation Part 4 — real cost recording.

The pricing_rules table no longer ships empty for search: the
two search rules whose prices are actually known are seeded
(Serper at its verified Scale rate; SearXNG at $0 marginal), one
usage-ledger event prices as one search call, and everything
without a verified rate (the LLM provider) stays honestly
unknown.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

import models
from database import Base, SessionLocal, engine
from services.billing import costs

Base.metadata.create_all(bind=engine)

_PROVIDERS = ("serper", "searxng", "brewkeg")


@pytest.fixture()
def db():
    session = SessionLocal()
    # The suite shares one throwaway database: clear the rows
    # these tests assert on so file order cannot matter.
    session.query(models.ProviderUsageEvent).filter(
        models.ProviderUsageEvent.provider.in_(_PROVIDERS)
    ).delete(synchronize_session=False)
    session.query(models.PricingRule).filter(
        models.PricingRule.provider.in_(("serper", "searxng")),
        models.PricingRule.unit == "per_search",
    ).delete(synchronize_session=False)
    session.commit()
    yield session
    session.close()


def _event(db, provider, **kwargs):
    event = models.ProviderUsageEvent(
        id=str(uuid.uuid4()),
        provider=provider,
        model=kwargs.get("model"),
        context=kwargs.get("context", "search:web"),
        prompt_tokens=kwargs.get("prompt_tokens"),
        completion_tokens=kwargs.get("completion_tokens"),
        total_tokens=kwargs.get("total_tokens"),
    )
    db.add(event)
    db.flush()
    return event


def test_seed_creates_the_two_search_rules_idempotently(db):
    first = costs.ensure_pricing_rules(db)
    assert first["pricing_rules_created"] == ["serper", "searxng"]
    second = costs.ensure_pricing_rules(db)
    assert second["pricing_rules_created"] == []
    rules = {
        (r.provider, r.unit): r.rate_microusd
        for r in db.query(models.PricingRule).all()
        if r.provider in ("serper", "searxng")
    }
    assert rules[("serper", "per_search")] == 500
    assert rules[("searxng", "per_search")] == 0


def test_seed_never_overwrites_an_admin_rate(db):
    db.add(models.PricingRule(
        id=str(uuid.uuid4()), provider="serper", model=None,
        unit="per_search", rate_microusd=700,
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ))
    db.flush()
    report = costs.ensure_pricing_rules(db)
    assert report["pricing_rules_created"] == ["searxng"]
    rule = (
        db.query(models.PricingRule)
        .filter_by(provider="serper", unit="per_search")
        .one()
    )
    assert rule.rate_microusd == 700


def test_rollup_prices_search_events_and_keeps_llm_unknown(db):
    costs.ensure_pricing_rules(db)
    for _ in range(4):
        _event(db, "serper")
    for _ in range(3):
        _event(db, "searxng")
    _event(db, "brewkeg", context="chat",
           prompt_tokens=1000, completion_tokens=500,
           total_tokens=1500)
    db.commit()
    report = costs.rollup(db)
    providers = report["by_provider"]
    assert providers["serper"] == {
        "events": 4, "priced_events": 4, "cost_microusd": 2000,
    }
    # SearXNG prices at exactly 0 — priced, not unknown.
    assert providers["searxng"] == {
        "events": 3, "priced_events": 3, "cost_microusd": 0,
    }
    # No verified LLM rate is configured: the event counts toward
    # coverage but stays UNPRICED (priced_events 0 — the engine's
    # "unknown" signal; its cost is never invented).
    assert providers["brewkeg"]["events"] == 1
    assert providers["brewkeg"]["priced_events"] == 0
