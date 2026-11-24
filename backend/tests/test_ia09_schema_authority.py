"""IA-09 — Alembic is the sole schema authority; no import-time
create_all in main.py.

Before IA-09, main.py ran Base.metadata.create_all(bind=engine)
at import, competing with the migration chain (0001-0005) as
schema authority. IA-09:

- removes that call: importing main creates NO tables and runs
  NO schema check;
- adds database.check_schema() — a read-only verification that
  the configured database is at the migration head — wired into
  serving startup ONLY via main's FastAPI lifespan, so the
  TestClient-without-`with` pattern the rest of the suite uses
  (which never fires lifespan in this Starlette version) is
  unaffected, and tests' own explicit fixture create_all calls
  remain their own setup;
- semantics (database.check_schema docstring carries the full
  rationale): missing tables -> hard failure; stamped revision
  behind head -> hard failure; all tables present but unstamped
  (legacy create_all database) -> loud WARNING + `alembic stamp
  head` guidance, startup proceeds; stamped at head -> OK.

Models vs migrations verdict at implementation time (verified
programmatically, not assumed): a fresh `alembic upgrade head`
SQLite database compared against Base.metadata — same 4 tables,
same column names/types/nullability per table, same indexes
(including the 0005 partial unique index
uq_project_revisions_single_root), same PKs/FKs, and Alembic's
own autogenerate compare_metadata reported ZERO differences.
The only delta is cosmetic column ORDER on `projects` (0003
appended title/options after the 0001 columns; the model declares
them earlier) — order is not schema identity, the ORM addresses
columns by name, and no migration is warranted for it. Hence NO
migration 0006: there is no model<->migration gap to close.
"""

import logging
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import main
from database import (
    Base,
    SchemaVersionError,
    check_schema,
    engine,
    migration_head,
)

BACKEND = Path(__file__).resolve().parent.parent
HEAD = "0009_assistant"


def _alembic_cfg():
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    return cfg


@pytest.fixture()
def adhoc_db(tmp_path, monkeypatch):
    """A throwaway SQLite file + an engine bound to it, with
    DATABASE_URL pointed at it for Alembic runs (migrations/env.py
    reads DATABASE_URL at run time)."""
    db_path = tmp_path / "ia09.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    adhoc_engine = create_engine(f"sqlite:///{db_path}")
    yield db_path, adhoc_engine
    adhoc_engine.dispose()


def _insert_project(db_path, project_id):
    conn = sqlite3.connect(str(db_path))
    try:
        # Phase 8: projects carry a NOT NULL owner on migrated
        # databases (0007), so seed the owning user first. The
        # create_all flavour of this helper's database has the same
        # users table (models), so one code path serves both.
        owner_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO users (id, email, password_hash, role, status,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (owner_id, f"{owner_id}@example.invalid", "x", "user",
             "active", "2026-11-22 00:00:00"),
        )
        conn.execute(
            "INSERT INTO projects (id, script, category, status, created_at,"
            " owner_id) VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, "A script.", "Documentary", "created",
             "2026-11-20 00:00:00", owner_id),
        )
        conn.commit()
    finally:
        conn.close()


def _project_rows(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT id, script FROM projects").fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Head derivation
# ---------------------------------------------------------------------


def test_migration_head_is_0009():
    assert migration_head() == HEAD


# ---------------------------------------------------------------------
# Migrated-to-head database: check passes, records preserved
# ---------------------------------------------------------------------


def test_migrated_head_passes_and_preserves_records(adhoc_db):
    db_path, adhoc_engine = adhoc_db
    command.upgrade(_alembic_cfg(), "head")
    project_id = str(uuid.uuid4())
    _insert_project(db_path, project_id)

    result = check_schema(bind=adhoc_engine)

    assert result == {
        "status": "ok",
        "current_revision": HEAD,
        "expected_head": HEAD,
        "missing_tables": [],
    }
    # The check is read-only: the pre-existing row is intact.
    assert _project_rows(db_path) == [(project_id, "A script.")]


def test_app_startup_succeeds_against_migrated_head(adhoc_db, monkeypatch):
    # Serving startup (entering TestClient runs the lifespan) must
    # succeed when the check passes. The lifespan checks the APP
    # engine, so point main's check at the migrated ad-hoc engine
    # for this test — the wiring under test is main -> check_schema.
    _db_path, adhoc_engine = adhoc_db
    command.upgrade(_alembic_cfg(), "head")
    monkeypatch.setattr(
        main, "check_schema", lambda: check_schema(bind=adhoc_engine)
    )
    with TestClient(main.app, raise_server_exceptions=False) as client:
        assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------
# Behind head / incomplete: loud hard failure naming the fix
# ---------------------------------------------------------------------


def test_upgrade_to_0003_only_fails_check(adhoc_db):
    db_path, adhoc_engine = adhoc_db
    command.upgrade(_alembic_cfg(), "0003_project_title_options")

    with pytest.raises(SchemaVersionError) as excinfo:
        check_schema(bind=adhoc_engine)
    message = str(excinfo.value)
    # Names what is missing, the expected head, and the fix command.
    assert "project_revisions" in message
    assert HEAD in message
    assert "alembic upgrade head" in message


def test_stamped_behind_head_with_complete_schema_fails(adhoc_db):
    # All tables present (upgraded to head) but the stamp rewound
    # to 0003: the pure behind-head branch — hard failure naming
    # the current revision, the expected head and the fix.
    _db_path, adhoc_engine = adhoc_db
    cfg = _alembic_cfg()
    command.upgrade(cfg, "head")
    command.stamp(cfg, "0003_project_title_options")

    with pytest.raises(SchemaVersionError) as excinfo:
        check_schema(bind=adhoc_engine)
    message = str(excinfo.value)
    assert "0003_project_title_options" in message
    assert HEAD in message
    assert "alembic upgrade head" in message


def test_empty_database_fails(adhoc_db):
    _db_path, adhoc_engine = adhoc_db

    with pytest.raises(SchemaVersionError) as excinfo:
        check_schema(bind=adhoc_engine)
    message = str(excinfo.value)
    assert "projects" in message
    assert HEAD in message
    assert "alembic upgrade head" in message


def test_startup_refuses_to_serve_when_check_fails(monkeypatch):
    def failing_check():
        raise SchemaVersionError(
            "IA-09 schema check FAILED: stamped behind head. "
            "Fix: run `alembic upgrade head`."
        )

    monkeypatch.setattr(main, "check_schema", failing_check)
    with pytest.raises(SchemaVersionError, match="alembic upgrade head"):
        with TestClient(main.app, raise_server_exceptions=False):
            pass


# ---------------------------------------------------------------------
# Unstamped legacy database (create_all-shaped): warn, don't fail
# ---------------------------------------------------------------------


def test_create_all_unstamped_db_warns_and_passes(adhoc_db):
    db_path, adhoc_engine = adhoc_db
    # Explicit create_all — the shape of every legacy database and
    # of the suite's own test fixtures. Allowed as test setup; the
    # point under test is how check_schema treats the result.
    import models  # noqa: F401  (register tables on Base.metadata)

    Base.metadata.create_all(bind=adhoc_engine)
    project_id = str(uuid.uuid4())
    _insert_project(db_path, project_id)

    # Capture the warning with a handler attached directly to the
    # database logger. Two in-suite quirks shape this: pytest's
    # caplog did not observe this logger's records, and Alembic's
    # env.py fileConfig() (run by the migration tests earlier in
    # this process) disables pre-existing loggers not named in
    # alembic.ini — including "database" — so re-enable it just
    # for the capture and restore the flag afterwards. (Serving
    # never runs Alembic in-process, so production is unaffected.)
    records = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collect(level=logging.WARNING)
    db_logger = logging.getLogger("database")
    old_level = db_logger.level
    was_disabled = db_logger.disabled
    db_logger.addHandler(handler)
    db_logger.setLevel(logging.WARNING)
    db_logger.disabled = False
    try:
        result = check_schema(bind=adhoc_engine)
    finally:
        db_logger.removeHandler(handler)
        db_logger.setLevel(old_level)
        db_logger.disabled = was_disabled

    assert result["status"] == "unstamped_legacy"
    assert result["current_revision"] is None
    assert result["expected_head"] == HEAD
    warnings = [r for r in records if r.levelno >= logging.WARNING]
    assert warnings, "expected a loud schema warning for an unstamped DB"
    assert "alembic stamp head" in warnings[0].getMessage()
    # Read-only again: the row survives the check.
    assert _project_rows(db_path) == [(project_id, "A script.")]


# ---------------------------------------------------------------------
# Import-time behaviour: no create_all, no check on plain requests
# ---------------------------------------------------------------------


def test_importing_main_creates_no_tables(tmp_path):
    # Fresh interpreter, fresh DATABASE_URL: importing main must
    # succeed (no startup check at import) and create NO tables
    # (no import-time create_all). Jobs' import-time config needs
    # the same dummy env conftest provides.
    db_path = tmp_path / "import.db"
    env = dict(os.environ)
    env.update(
        DATABASE_URL=f"sqlite:///{db_path}",
        REDIS_URL="redis://localhost:6379/15",
        BREWKEG_API_KEY="test-dummy-key",
        SERPER_API_KEY="test-dummy-key",
        SOURCE_INTEL_DB=str(tmp_path / "source_intel.sqlite3"),
        PYTHONPATH=str(BACKEND),
    )
    proc = subprocess.run(
        [sys.executable, "-c", "import main; print('imported')"],
        cwd=str(BACKEND),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "imported" in proc.stdout
    tables = set()
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
    assert tables == set()


def test_plain_testclient_requests_do_not_run_the_check(monkeypatch):
    # The pattern the whole suite uses: TestClient WITHOUT `with`
    # never fires lifespan/startup in this Starlette version, so
    # existing tests against fixture-built databases are untouched
    # by IA-09. Pin that behaviour — if a future Starlette starts
    # firing startup per-request, this test fails first and loudly.
    calls = []
    monkeypatch.setattr(main, "check_schema", lambda: calls.append(1))
    client = TestClient(main.app, raise_server_exceptions=False)
    assert client.get("/health").status_code == 200
    assert calls == []


def test_startup_check_passes_for_fixture_created_global_db(monkeypatch):
    # The global test database, built by an explicit create_all
    # exactly like the other API test modules' fixtures: unstamped
    # -> warning path -> startup succeeds and the app serves.
    Base.metadata.create_all(bind=engine)
    calls = []
    real_check = main.check_schema

    def counting_check():
        calls.append(1)
        return real_check()

    monkeypatch.setattr(main, "check_schema", counting_check)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        assert client.get("/health").status_code == 200
    assert calls == [1]
