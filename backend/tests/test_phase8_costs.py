"""Phase 8 slice 8 — cost engine (§13).

Unknown stays unknown: with no pricing rules every cost is None
with priced_coverage 0. Configured rules price token and
per-call events; events without the measurement a rule needs
stay unpriced; reports label contracted MRR and refuse to
fabricate margins.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import models
from database import Base, SessionLocal, engine
from services.billing import catalogue, costs

Base.metadata.create_all(bind=engine)

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


@pytest.fixture()
def db():
    session = SessionLocal()
    catalogue.ensure_billing_catalogue(session)
    session.commit()
    yield session
    session.close()


def _uniq(base):
    return f"{base}-{uuid.uuid4().hex[:8]}"


def _event(db, **kwargs):
    event = models.ProviderUsageEvent(
        id=str(uuid.uuid4()),
        provider=kwargs.get("provider", "brewkeg"),
        model=kwargs.get("model", "claude-test"),
        prompt_tokens=kwargs.get("prompt_tokens"),
        completion_tokens=kwargs.get("completion_tokens"),
        total_tokens=kwargs.get("total_tokens"),
        run_id=kwargs.get("run_id"),
        created_at=kwargs.get("created_at", NOW),
    )
    db.add(event)
    db.flush()
    return event


def _rule(db, **kwargs):
    rule = models.PricingRule(
        id=str(uuid.uuid4()),
        provider=kwargs.get("provider", "brewkeg"),
        model=kwargs.get("model"),
        unit=kwargs.get("unit", "per_1k_tokens"),
        rate_microusd=kwargs.get("rate_microusd", 3000),
        effective_from=kwargs.get(
            "effective_from", NOW - timedelta(days=30)
        ),
        effective_to=kwargs.get("effective_to"),
        configured_by="admin-test",
    )
    db.add(rule)
    db.flush()
    return rule


def _scoped(result, provider):
    bucket = result["by_provider"].get(
        provider, {"events": 0, "priced_events": 0, "cost_microusd": 0}
    )
    return {
        **result,
        "total_events": bucket["events"],
        "priced_events": bucket["priced_events"],
        "total_cost_microusd": (
            bucket["cost_microusd"] if bucket["priced_events"] else None
        ),
        "pricing_status": (
            "configured" if bucket["priced_events"]
            else "not_configured"
        ),
    }


def test_no_rules_means_unknown_costs(db):
    provider = _uniq("brewkeg")
    _event(db, provider=provider, total_tokens=5000)
    db.commit()
    result = costs.rollup(db)
    result = _scoped(result, provider)
    assert result["total_events"] >= 1
    assert result["priced_events"] == 0
    assert result["total_cost_microusd"] is None
    assert result["pricing_status"] == "not_configured"


def test_token_pricing_and_coverage(db):
    provider = _uniq("brewkeg")
    other = _uniq("search")
    _rule(db, provider=provider, rate_microusd=3000)
    _event(db, provider=provider, total_tokens=2000)  # -> 6000
    _event(db, provider=provider, total_tokens=None)  # unpriced
    _event(db, provider=other, total_tokens=100)  # no rule
    db.commit()
    result = costs.rollup(db)
    bucket = result["by_provider"][provider]
    assert bucket["priced_events"] == 1
    assert bucket["cost_microusd"] == 6000
    assert result["by_provider"][other]["priced_events"] == 0


def test_model_specific_rule_beats_default(db):
    provider = _uniq("brewkeg")
    _rule(db, provider=provider, model=None, rate_microusd=1000)
    _rule(db, provider=provider, model="claude-test",
          rate_microusd=5000)
    _event(db, provider=provider, total_tokens=1000)
    db.commit()
    result = costs.rollup(db)
    assert result["by_provider"][provider]["cost_microusd"] == 5000


def test_effective_window_excludes_old_events(db):
    provider = _uniq("brewkeg")
    _rule(db, provider=provider,
          effective_from=NOW + timedelta(days=1))
    _event(db, provider=provider, total_tokens=1000, created_at=NOW)
    db.commit()
    result = costs.rollup(db)
    assert result["by_provider"][provider]["priced_events"] == 0


def test_per_call_rule(db):
    provider = _uniq("search")
    _rule(db, provider=provider, unit="per_call", rate_microusd=800)
    _event(db, provider=provider)
    _event(db, provider=provider)
    db.commit()
    result = costs.rollup(db)
    assert result["by_provider"][provider]["cost_microusd"] == 1600


def test_financial_report_honesty(db):
    user = models.User(
        id=str(uuid.uuid4()), email=f"{uuid.uuid4()}@e.invalid",
        password_hash="x",
    )
    db.add(user)
    db.flush()
    creator = db.query(models.Plan).filter_by(name="creator").one()
    db.add(
        models.UserSubscription(
            id=str(uuid.uuid4()), user_id=user.id,
            plan_id=creator.id, status="active",
            billing_interval="month",
        )
    )
    db.add(
        models.Payment(
            id=str(uuid.uuid4()), user_id=user.id,
            provider="paddle", provider_transaction_id="txn_rpt_1",
            kind="subscription", status="completed",
            amount_cents=2900, currency="USD",
        )
    )
    db.commit()
    report = costs.financial_report(db)
    assert report["revenue"]["total_cents"] >= 2900
    # Contracted MRR prices the ACTIVE subscription at the
    # catalogue's current Creator price ($29 — restructure
    # 2026-11-26 set $28; founder raised it to $29 on
    # 2026-11-27) — matching the 2900 payment above.
    assert report["mrr_contracted_cents"] >= 2900
    assert report["subscriptions_by_plan"]["creator"] >= 1
    # No pricing rules configured anywhere in this test DB state
    # for these runs: margins stay honestly unknown/partial.
    assert report["gross_margin"]["value"] is None
    assert report["cost_per_finished_minute"]["value"] is None
