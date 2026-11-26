"""Phase 8 slice 2 — billing catalogue + credit maths.

Pins the founder's price table (directive §§2, 3, 8, 20), the
duration cost table (§5), and the core §1 guarantee: every
advertised documentary allowance is deliverable — from the
included entitlements by construction, and from the plan's
credits alone at the platform rate.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import models
from database import Base
from services.billing import catalogue


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cat.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_duration_cost_table_matches_design():
    table = {row["minutes"]: row["credits"]
             for row in catalogue.duration_cost_table()}
    assert table == {
        5: 28, 10: 56, 15: 84, 20: 112, 30: 168, 45: 252, 60: 336,
    }


def test_advertised_allowances_are_deliverable():
    # §1/§18: for each plan, (a) the entitlement count equals the
    # advertised documentary count, and (b) the plan's monthly
    # credits ALONE can pay for the advertised count at the
    # platform rate — the allowance never depends on hidden math.
    rate = catalogue.standard_generation_credits()
    advertised = {"creator": (1300, 11), "studio": (3600, 29),
                  "agency": (7200, 59)}
    for spec in catalogue.METERED_PLANS:
        credits, docs = advertised[spec["name"]]
        assert spec["monthly_credits"] == credits
        assert spec["included_generations"] == docs
        assert credits // rate >= docs, spec["name"]


def test_founder_price_table(db):
    catalogue.ensure_billing_catalogue(db)
    db.commit()
    plans = {
        p.name: p for p in db.query(models.Plan).all()
    }
    assert plans["creator"].price_monthly_cents == 2900
    assert plans["studio"].price_monthly_cents == 7900
    assert plans["agency"].price_monthly_cents == 17900
    # Annual = 20% discount on 12 monthly payments, exactly.
    for name, monthly in (("creator", 2900), ("studio", 7900),
                          ("agency", 17900)):
        assert plans[name].price_annual_cents == monthly * 12 * 4 // 5
        assert plans[name].billing_mode == "metered"
        assert plans[name].is_public is True

    packages = {
        p.name: p for p in db.query(models.TopupPackage).all()
    }
    assert (packages["topup-120"].credits,
            packages["topup-120"].price_cents) == (120, 800)
    assert (packages["topup-300"].credits,
            packages["topup-300"].price_cents) == (300, 1800)
    assert (packages["topup-600"].credits,
            packages["topup-600"].price_cents) == (600, 3200)
    assert (packages["topup-1200"].credits,
            packages["topup-1200"].price_cents) == (1200, 5900)
    assert (packages["topup-2400"].credits,
            packages["topup-2400"].price_cents) == (2400, 10900)
    assert all(p.validity_months == 12 for p in packages.values())


def test_catalogue_sync_is_idempotent_and_preserves_limits(db):
    first = catalogue.ensure_billing_catalogue(db)
    db.commit()
    assert len(first["plans_created"]) == 3
    assert len(first["packages_created"]) == 5

    # An operator limit edit must survive a re-sync; a commercial
    # drift must be corrected by it.
    creator = db.query(models.Plan).filter_by(name="creator").one()
    creator.max_concurrent_runs = 7
    creator.price_monthly_cents = 1
    db.commit()

    second = catalogue.ensure_billing_catalogue(db)
    db.commit()
    assert second["plans_created"] == []
    assert second["packages_created"] == []
    updated = {u["name"]: u["fields"] for u in second["plans_updated"]}
    assert updated == {"creator": ["price_monthly_cents"]}
    creator = db.query(models.Plan).filter_by(name="creator").one()
    assert creator.max_concurrent_runs == 7
    assert creator.price_monthly_cents == 2900

    third = catalogue.ensure_billing_catalogue(db)
    assert third["plans_updated"] == []
    assert third["packages_updated"] == []


def test_estimate_seconds_uses_platform_pace():
    script = " ".join(["word"] * 1450)  # 10 minutes at 145 wpm
    assert catalogue.estimate_seconds(script) == pytest.approx(600.0)
    assert catalogue.generation_credit_cost(600.0) == 56
