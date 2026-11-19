"""Stage 6 — migration 0004 (editor revisions) behaviour.

Same discipline as 0002/0003: fresh install upgrades cleanly to
head with columns matching the ORM models; a database at 0003 keeps
its rows across 0004; downgrading to 0003 drops ONLY the new table
and column.
"""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command
from alembic.config import Config


@pytest.fixture()
def migration_db(tmp_path, monkeypatch):
    db_path = tmp_path / "stage6_migrations.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    return db_path


def _alembic_cfg():
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "migrations"))
    return cfg


def _tables(db_path):
    conn = sqlite3.connect(db_path)
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
    conn = sqlite3.connect(db_path)
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def test_fresh_upgrade_creates_revision_table_matching_model(migration_db):
    command.upgrade(_alembic_cfg(), "head")
    assert "project_revisions" in _tables(migration_db)
    from models import AssetDecision, ProjectRevision

    assert _columns(migration_db, "project_revisions") == [
        c.name for c in ProjectRevision.__table__.columns
    ]
    assert "revision_id" in _columns(migration_db, "asset_decisions")
    assert _columns(migration_db, "asset_decisions") == [
        c.name for c in AssetDecision.__table__.columns
    ]


def test_upgrade_from_0003_preserves_rows(migration_db):
    cfg = _alembic_cfg()
    command.upgrade(cfg, "0003_project_title_options")
    conn = sqlite3.connect(migration_db)
    try:
        conn.execute(
            "INSERT INTO projects (id, script, category, status, created_at)"
            " VALUES ('p1', 'A script.', 'history', 'completed',"
            " '2026-11-19 00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(migration_db)
    try:
        rows = conn.execute("SELECT id, script FROM projects").fetchall()
    finally:
        conn.close()
    assert rows == [("p1", "A script.")]
    assert "project_revisions" in _tables(migration_db)


def test_downgrade_drops_only_stage6_objects(migration_db):
    cfg = _alembic_cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0003_project_title_options")
    tables = _tables(migration_db)
    assert "project_revisions" not in tables
    assert "projects" in tables
    assert "production_runs" in tables
    assert "asset_decisions" in tables
    assert "revision_id" not in _columns(migration_db, "asset_decisions")
    # And back up again cleanly.
    command.upgrade(cfg, "head")
    assert "project_revisions" in _tables(migration_db)
