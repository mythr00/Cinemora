"""Phase 8 slice 1 — billing schema (migration 0013 + models).

Pins:
- the migration chain head is 0013_billing;
- a fresh database upgraded to head carries every Phase 8 table
  with exactly the columns the ORM declares (name-for-name), so
  models and migrations cannot drift apart silently;
- the financial-integrity unique constraints exist (ledger
  idempotency key, one reservation per run, one webhook inbox
  row per provider event, one wallet per user, one redemption
  per user per coupon, one billing period per user per start);
- downgrade to 0012 removes the Phase 8 schema cleanly.
"""

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

import models
from database import Base

BACKEND = Path(__file__).resolve().parent.parent

NEW_TABLES = {
    "credit_accounts",
    "credit_ledger",
    "billing_periods",
    "generation_reservations",
    "topup_packages",
    "coupons",
    "coupon_redemptions",
    "payments",
    "payment_events",
    "pricing_rules",
    "generated_videos",
    "user_notifications",
}


def _alembic_cfg():
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    return cfg


@pytest.fixture()
def migrated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "phase8.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    engine = create_engine(f"sqlite:///{db_path}")
    yield db_path, engine
    engine.dispose()


def _orm_columns(table_name):
    return {c.name for c in Base.metadata.tables[table_name].columns}


def _db_columns(engine, table_name):
    return {c["name"] for c in inspect(engine).get_columns(table_name)}


def test_upgrade_to_head_creates_billing_schema(migrated_db):
    _db_path, engine = migrated_db
    command.upgrade(_alembic_cfg(), "head")

    tables = set(inspect(engine).get_table_names())
    assert NEW_TABLES <= tables

    for table in sorted(NEW_TABLES):
        assert _db_columns(engine, table) == _orm_columns(table), table

    # Extended existing tables match the ORM too.
    for table in ("plans", "user_subscriptions"):
        assert _db_columns(engine, table) == _orm_columns(table), table


def test_billing_unique_constraints_present(migrated_db):
    _db_path, engine = migrated_db
    command.upgrade(_alembic_cfg(), "head")
    insp = inspect(engine)

    def unique_sets(table):
        out = set()
        for uc in insp.get_unique_constraints(table):
            out.add(tuple(sorted(uc["column_names"])))
        # Column-level unique=True surfaces as an index on SQLite.
        for ix in insp.get_indexes(table):
            if ix.get("unique"):
                out.add(tuple(sorted(ix["column_names"])))
        return out

    assert ("idempotency_key",) in unique_sets("credit_ledger")
    assert ("run_id",) in unique_sets("generation_reservations")
    assert ("event_id", "provider") in unique_sets("payment_events")
    assert ("user_id",) in unique_sets("credit_accounts")
    assert ("coupon_id", "user_id") in unique_sets(
        "coupon_redemptions"
    )
    assert ("period_start", "user_id") in unique_sets(
        "billing_periods"
    )
    assert ("provider_transaction_id",) in unique_sets("payments")


def test_downgrade_removes_billing_schema(migrated_db):
    _db_path, engine = migrated_db
    cfg = _alembic_cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0012_provider_usage")

    tables = set(inspect(engine).get_table_names())
    assert not (NEW_TABLES & tables)
    plan_cols = _db_columns(engine, "plans")
    assert "billing_mode" not in plan_cols
    assert "price_monthly_cents" not in plan_cols
    sub_cols = _db_columns(engine, "user_subscriptions")
    assert "billing_interval" not in sub_cols


def test_billing_models_roundtrip(tmp_path):
    # The ORM side: a wallet, a ledger entry and a reservation can
    # be written and read back with defaults applied.
    engine = create_engine(f"sqlite:///{tmp_path / 'orm.db'}")
    Base.metadata.create_all(bind=engine)
    from sqlalchemy.orm import Session

    user_id = str(uuid.uuid4())
    with Session(engine) as db:
        db.add(
            models.User(
                id=user_id, email="b@example.invalid",
                password_hash="x",
            )
        )
        account = models.CreditAccount(
            id=str(uuid.uuid4()), user_id=user_id
        )
        db.add(account)
        db.flush()
        assert account.subscription_available == 0
        assert account.lifetime_spent == 0
        entry = models.CreditLedgerEntry(
            id=str(uuid.uuid4()),
            user_id=user_id,
            account_id=account.id,
            entry_type="allocation_subscription",
            bucket="subscription",
            amount=1350,
            idempotency_key="alloc:test:period",
        )
        db.add(entry)
        reservation = models.GenerationReservation(
            id=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()),
            root_run_id=str(uuid.uuid4()),
            user_id=user_id,
        )
        db.add(reservation)
        db.commit()

    with Session(engine) as db:
        stored = db.query(models.CreditLedgerEntry).one()
        assert stored.amount == 1350
        assert stored.source == "system"
        assert db.query(models.GenerationReservation).one().status \
            == "active"
    engine.dispose()
