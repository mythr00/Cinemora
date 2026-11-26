"""Phase 8 slice 2 — billing catalogue + credit maths.

Pins the founder's price table and the core guarantee: every
advertised documentary allowance is deliverable. UPDATED for
the pricing restructure (founder directive 2026-11-26 ~10:25
WAT): per-tier per-minute rates, and the exact identity
monthly_credits == videos x rate x 15, so at the standard
15 minutes the delivered-video count and the wallet are
exhausted together.
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
    # Default (entry-tier) rate: 6 credits per finished minute.
    table = {row["minutes"]: row["credits"]
             for row in catalogue.duration_cost_table()}
    assert table == {
        5: 30, 10: 60, 15: 90, 20: 120, 30: 180, 45: 270, 60: 360,
    }
    # Per-tier tables at the Studio (5) and Agency (4) rates.
    studio = {row["minutes"]: row["credits"]
              for row in catalogue.duration_cost_table(5)}
    assert studio[15] == 75 and studio[30] == 150
    agency = {row["minutes"]: row["credits"]
              for row in catalogue.duration_cost_table(4)}
    assert agency[15] == 60 and agency[30] == 120


def test_advertised_allowances_are_deliverable():
    # Restructure identity: for each plan, monthly credits equal
    # EXACTLY the advertised video count x the 15-minute cost at
    # the tier rate (11x90=990, 31x75=2325, 61x60=3660), and the
    # script/thumbnail allowances equal the video count — the
    # allowance never depends on hidden math.
    advertised = {
        "creator": (990, 11, 6),
        "studio": (2325, 31, 5),
        "agency": (3660, 61, 4),
    }
    for spec in catalogue.METERED_PLANS:
        credits, docs, rate = advertised[spec["name"]]
        assert spec["monthly_credits"] == credits
        assert spec["included_generations"] == docs
        assert spec["script_allowance"] == docs
        assert spec["thumbnail_allowance"] == docs
        assert (
            spec["features"]["generation_credits_per_minute"] == rate
        )
        assert credits == docs * rate * 15, spec["name"]


def test_founder_price_table(db):
    catalogue.ensure_billing_catalogue(db)
    db.commit()
    plans = {
        p.name: p for p in db.query(models.Plan).all()
    }
    assert plans["creator"].price_monthly_cents == 2800
    assert plans["studio"].price_monthly_cents == 6300
    assert plans["agency"].price_monthly_cents == 11100
    # Annual = 20% discount on 12 monthly payments, exactly (the
    # pre-existing -20% structure; only the bases moved).
    for name, monthly in (("creator", 2800), ("studio", 6300),
                          ("agency", 11100)):
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
    assert creator.price_monthly_cents == 2800

    third = catalogue.ensure_billing_catalogue(db)
    assert third["plans_updated"] == []
    assert third["packages_updated"] == []


def test_estimate_seconds_uses_platform_pace():
    script = " ".join(["word"] * 1450)  # 10 minutes at 145 wpm
    assert catalogue.estimate_seconds(script) == pytest.approx(600.0)
    # 10 minutes at the default 6 credits/minute.
    assert catalogue.generation_credit_cost(600.0) == 60
    # The same duration at the Agency rate (4/min).
    assert catalogue.generation_credit_cost(600.0, 4) == 40
