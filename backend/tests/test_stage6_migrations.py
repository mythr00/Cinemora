"""Stage 6 — migration 0004 (editor revisions) behaviour, plus
migration 0005 (single-root uniqueness, defect D1).

Same discipline as 0002/0003: fresh install upgrades cleanly to
head with columns matching the ORM models; a database at 0003 keeps
its rows across 0004; downgrading to 0003 drops ONLY the new table
and column. For 0005: fresh install creates the partial unique
index matching the ORM model; a database at 0004 keeps its rows
across 0005; downgrading to 0004 drops ONLY the index; the index
enforces exactly one revision-0 root per project while branching
below the root stays legal.
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
    # Phase 8 path (0006 -> backfill -> 0007): the additive step
    # lands first, then the operator step the migrations are
    # designed around — seed a founder and backfill ownership,
    # exactly as scripts/seed_founder.py + backfill_ownership.py
    # did on the preview database — before 0007 enforces NOT NULL.
    # (0007 refusing an un-backfilled database is covered by the
    # Phase 8.1 tests; this test's subject is row preservation.)
    command.upgrade(cfg, "0006_users_and_ownership")
    conn = sqlite3.connect(migration_db)
    try:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, role, status,"
            " created_at) VALUES ('founder-1', 'founder@example.com',"
            " 'hash', 'super_admin', 'active', '2026-11-22 00:00:00')"
        )
        conn.execute("UPDATE projects SET owner_id = 'founder-1'")
        conn.commit()
    finally:
        conn.close()
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(migration_db)
    try:
        rows = conn.execute(
            "SELECT id, script, owner_id FROM projects"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("p1", "A script.", "founder-1")]
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


# ---------------------------------------------------------------------
# Migration 0005 — single-root partial unique index (defect D1)
# ---------------------------------------------------------------------

ROOT_INDEX = "uq_project_revisions_single_root"


def _indexes(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index'"
            )
        }
    finally:
        conn.close()


def _insert_revision(db_path, revision_id, project_id, parent_id=None):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO project_revisions (id, project_id, run_id,"
            " parent_revision_id, revision_number, status,"
            " document_path, created_at, updated_at) VALUES"
            " (?, ?, 'run-1', ?, 0, 'draft', ?,"
            " '2026-11-19 00:00:00', '2026-11-19 00:00:00')",
            (
                revision_id,
                project_id,
                parent_id,
                f"revision_{revision_id}.json",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_fresh_upgrade_creates_single_root_index_matching_model(migration_db):
    command.upgrade(_alembic_cfg(), "head")
    indexes = _indexes(migration_db)
    assert ROOT_INDEX in indexes
    sql = indexes[ROOT_INDEX].upper()
    assert "UNIQUE" in sql
    assert "PARENT_REVISION_ID IS NULL" in sql
    # ORM parity: the model declares the same index.
    from models import ProjectRevision

    orm_indexes = {ix.name: ix for ix in ProjectRevision.__table__.indexes}
    assert ROOT_INDEX in orm_indexes
    assert orm_indexes[ROOT_INDEX].unique


def test_upgrade_from_0004_preserves_rows_and_adds_index(migration_db):
    cfg = _alembic_cfg()
    command.upgrade(cfg, "0004_editor_revisions")
    assert ROOT_INDEX not in _indexes(migration_db)
    _insert_revision(migration_db, "root-1", "p1")
    _insert_revision(migration_db, "child-1", "p1", parent_id="root-1")
    command.upgrade(cfg, "head")
    assert ROOT_INDEX in _indexes(migration_db)
    conn = sqlite3.connect(migration_db)
    try:
        rows = conn.execute(
            "SELECT id FROM project_revisions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("child-1",), ("root-1",)]


def test_downgrade_0005_drops_only_the_index(migration_db):
    cfg = _alembic_cfg()
    command.upgrade(cfg, "head")
    _insert_revision(migration_db, "root-1", "p1")
    command.downgrade(cfg, "0004_editor_revisions")
    assert ROOT_INDEX not in _indexes(migration_db)
    assert "project_revisions" in _tables(migration_db)
    conn = sqlite3.connect(migration_db)
    try:
        rows = conn.execute("SELECT id FROM project_revisions").fetchall()
    finally:
        conn.close()
    assert rows == [("root-1",)]
    # And back up again cleanly.
    command.upgrade(cfg, "head")
    assert ROOT_INDEX in _indexes(migration_db)


def test_single_root_index_enforced_branching_intact(migration_db):
    command.upgrade(_alembic_cfg(), "head")
    _insert_revision(migration_db, "root-1", "p1")
    # A second root for the SAME project violates the index.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_revision(migration_db, "root-2", "p1")
    # A root for a DIFFERENT project is fine.
    _insert_revision(migration_db, "root-other", "p2")
    # Branching below the root is unconstrained: several children
    # of one parent, each with parent_revision_id set.
    _insert_revision(migration_db, "child-1", "p1", parent_id="root-1")
    _insert_revision(migration_db, "child-2", "p1", parent_id="root-1")
    conn = sqlite3.connect(migration_db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM project_revisions"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 4
