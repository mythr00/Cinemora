"""Phase 7 foundation: the admin audit log (design admin/03 §3).

Covers migration 0008 (table created at head, dropped on
downgrade), the append-only writer in services/admin_audit.py
(secret scrubbing, actor attribution), and the M7 query filters.
"""

import sqlite3
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from database import SessionLocal
from models import AdminAuditLog, Base
from services import admin_audit
from services.auth import Principal

BACKEND = Path(__file__).resolve().parent.parent

FOUNDER = Principal(
    user_id="founder-id",
    email="mirthdraught@gmail.com",
    role="super_admin",
    auth_method="session",
)


def _alembic_cfg():
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    return cfg


@pytest.fixture(autouse=True)
def _tables():
    Base.metadata.create_all(bind=SessionLocal().get_bind())
    db = SessionLocal()
    try:
        db.query(AdminAuditLog).delete()
        db.commit()
    finally:
        db.close()
    yield


def test_record_and_query_roundtrip():
    row_id = admin_audit.record(
        FOUNDER,
        "run.cancel",
        target_type="run",
        target_id="run-123",
        detail={"previous_status": "running"},
    )
    assert row_id
    db = SessionLocal()
    try:
        out = admin_audit.query(db)
        assert out["total"] == 1
        entry = out["entries"][0]
        assert entry["actor_identity"] == "mirthdraught@gmail.com"
        assert entry["actor_role"] == "super_admin"
        assert entry["auth_method"] == "session"
        assert entry["action"] == "run.cancel"
        assert entry["target_type"] == "run"
        assert entry["target_id"] == "run-123"
        assert entry["detail"] == {"previous_status": "running"}
        assert entry["result"] == "success"
    finally:
        db.close()


def test_record_scrubs_secret_keys_from_detail():
    admin_audit.record(
        FOUNDER,
        "user.plan_assign",
        target_type="user",
        target_id="u1",
        detail={"plan": "pro", "password": "hunter2", "token": "abc"},
    )
    db = SessionLocal()
    try:
        entry = admin_audit.query(db)["entries"][0]
        assert entry["detail"] == {"plan": "pro"}
    finally:
        db.close()


def test_record_anonymous_attempt():
    admin_audit.record(
        None, "admin.auth_denied", result="failure",
        error="not an administrator",
    )
    db = SessionLocal()
    try:
        entry = admin_audit.query(db, result="failure")["entries"][0]
        assert entry["actor_identity"] == "unknown"
        assert entry["actor_role"] == "anonymous"
        assert entry["result"] == "failure"
    finally:
        db.close()


def test_query_filters():
    admin_audit.record(FOUNDER, "run.cancel", target_id="r1")
    admin_audit.record(FOUNDER, "run.submit_replacement", target_id="r2")
    admin_audit.record(FOUNDER, "run.cancel", target_id="r3")
    db = SessionLocal()
    try:
        assert admin_audit.query(db, action="run.cancel")["total"] == 2
        assert admin_audit.query(db, target_id="r2")["total"] == 1
        assert admin_audit.query(db, actor="nobody@example.com")["total"] == 0
    finally:
        db.close()


def test_service_exposes_no_update_or_delete():
    assert not hasattr(admin_audit, "update")
    assert not hasattr(admin_audit, "delete")
    assert not hasattr(admin_audit, "remove")


def test_migration_0008_creates_and_drops_table(tmp_path, monkeypatch):
    db_path = tmp_path / "mig.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(db_path))
    cfg = _alembic_cfg()
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "admin_audit_log" in tables
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(admin_audit_log)")
        }
        assert {
            "id", "actor_identity", "actor_role", "auth_method",
            "action", "target_type", "target_id", "detail", "result",
            "error", "created_at",
        } <= cols
    finally:
        conn.close()
    command.downgrade(cfg, "0007_ownership_required")
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "admin_audit_log" not in tables
    finally:
        conn.close()
    # Keep the shared engine's env untouched for other modules.
    assert uuid.UUID  # silence linter; ids are uuid strings by design
