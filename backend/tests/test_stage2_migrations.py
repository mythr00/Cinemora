"""Stage 2A — migration 0002 (run ledger) behaviour.

Alembic is the schema authority for the ledger tables (AUD-09):
- a fresh database upgrades cleanly to head (projects + both ledger
  tables, columns matching the ORM models exactly);
- a database already at 0001 keeps its projects rows across 0002;
- downgrading to 0001 drops ONLY the two new tables.

Migrations run programmatically against throwaway SQLite files with
DATABASE_URL pointed at them (migrations/env.py reads it at run
time — and, since MA-13, refuses to run without it).
"""

import os
import sqlite3
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

BACKEND = Path(__file__).resolve().parent.parent


def _alembic_cfg():
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    return cfg


def _tables(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()


def _columns(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


@pytest.fixture()
def migration_db(tmp_path, monkeypatch):
    db_path = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    return db_path


def test_fresh_upgrade_creates_ledger_tables_matching_models(migration_db):
    command.upgrade(_alembic_cfg(), "head")

    tables = _tables(migration_db)
    assert {"projects", "production_runs", "asset_decisions"} <= tables

    from models import AssetDecision, ProductionRun

    for model in (ProductionRun, AssetDecision):
        model_columns = [c.name for c in model.__table__.columns]
        assert _columns(migration_db, model.__tablename__) == model_columns


def test_upgrade_from_0001_preserves_projects_rows(migration_db):
    cfg = _alembic_cfg()
    command.upgrade(cfg, "0001_baseline")

    project_id = str(uuid.uuid4())
    conn = sqlite3.connect(str(migration_db))
    try:
        conn.execute(
            "INSERT INTO projects (id, script, category, status, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (project_id, "A script.", "Documentary", "created",
             "2026-11-19 00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(migration_db))
    try:
        rows = conn.execute(
            "SELECT id, script, status FROM projects"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(project_id, "A script.", "created")]
    assert "production_runs" in _tables(migration_db)


def test_downgrade_drops_only_the_new_tables(migration_db):
    cfg = _alembic_cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0001_baseline")

    tables = _tables(migration_db)
    assert "projects" in tables
    assert "production_runs" not in tables
    assert "asset_decisions" not in tables


def test_env_fails_fast_without_database_url(tmp_path, monkeypatch):
    # MA-13: no silent fallback database for migrations.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        command.upgrade(_alembic_cfg(), "head")
