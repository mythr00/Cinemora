"""Phase 8.1 — data layer: users/plans/subscriptions/preferences,
ownership columns, founder seed + backfill, 0007 enforcement.

The seed and backfill are operator SCRIPTS (they must never run
inside a migration), so the end-to-end test drives them as
subprocesses against a scratch database — the exact path the
preview migration took — and asserts counts, enforcement and the
password-file discipline (mode 0600, password never printed).
"""

import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

BACKEND = Path(__file__).resolve().parent.parent


def _alembic_cfg():
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    return cfg


@pytest.fixture()
def scratch_db(tmp_path, monkeypatch):
    db_path = tmp_path / "phase8.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    yield db_path


def _run_script(script: str, db_path: Path, extra_env: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(BACKEND / "scripts" / script)],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _alembic_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _insert_ownerless_project_and_run(db_path: Path) -> tuple[str, str]:
    project_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO projects (id, script, category, status, created_at)"
            " VALUES (?, 'A script.', 'Documentary', 'created',"
            " '2026-11-20 00:00:00')",
            (project_id,),
        )
        conn.execute(
            "INSERT INTO production_runs (id, project_id, status,"
            " cancel_requested, created_at)"
            " VALUES (?, ?, 'completed', 0, '2026-11-20 00:00:00')",
            (run_id, project_id),
        )
        conn.commit()
    finally:
        conn.close()
    return project_id, run_id


def test_0006_adds_identity_tables_and_nullable_owner(scratch_db):
    command.upgrade(_alembic_cfg(), "0006_users_and_ownership")
    engine = create_engine(f"sqlite:///{scratch_db}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert {
            "users",
            "plans",
            "user_subscriptions",
            "user_preferences",
        } <= tables
        project_cols = {
            c["name"]: c for c in inspect(engine).get_columns("projects")
        }
        run_cols = {
            c["name"]: c
            for c in inspect(engine).get_columns("production_runs")
        }
        assert project_cols["owner_id"]["nullable"] is True
        assert run_cols["owner_id"]["nullable"] is True
    finally:
        engine.dispose()


def test_0007_refuses_unbackfilled_owners(scratch_db):
    command.upgrade(_alembic_cfg(), "0006_users_and_ownership")
    _insert_ownerless_project_and_run(scratch_db)
    with pytest.raises(RuntimeError, match="backfill_ownership"):
        command.upgrade(_alembic_cfg(), "0007_ownership_required")


def test_seed_backfill_0007_end_to_end(scratch_db, tmp_path):
    command.upgrade(_alembic_cfg(), "0006_users_and_ownership")
    project_id, run_id = _insert_ownerless_project_and_run(scratch_db)
    password_file = tmp_path / "founder-pw.txt"

    seed = _run_script(
        "seed_founder.py",
        scratch_db,
        {"CINEMORA_FOUNDER_PASSWORD_FILE": str(password_file)},
    )
    assert seed.returncode == 0, seed.stderr
    assert password_file.exists()
    assert oct(password_file.stat().st_mode)[-3:] == "600"
    # The generated password is in the file and NOWHERE in stdout.
    password = [
        line.split(": ", 1)[1]
        for line in password_file.read_text().splitlines()
        if line.startswith("password: ")
    ][0]
    assert len(password) >= 32
    assert password not in seed.stdout

    backfill = _run_script("backfill_ownership.py", scratch_db, {})
    assert backfill.returncode == 0, backfill.stderr
    assert "disagreements: 0" in backfill.stdout

    command.upgrade(_alembic_cfg(), "head")

    conn = sqlite3.connect(str(scratch_db))
    try:
        founder = conn.execute(
            "SELECT id, role, status FROM users"
            " WHERE email = 'mirthdraught@gmail.com'"
        ).fetchone()
        assert founder is not None and founder[1:] == ("super_admin", "active")
        assert conn.execute(
            "SELECT owner_id FROM projects WHERE id = ?", (project_id,)
        ).fetchone()[0] == founder[0]
        assert conn.execute(
            "SELECT owner_id FROM production_runs WHERE id = ?", (run_id,)
        ).fetchone()[0] == founder[0]
        assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 4
        plan_name = conn.execute(
            "SELECT p.name FROM user_subscriptions s"
            " JOIN plans p ON p.id = s.plan_id"
            " WHERE s.user_id = ? AND s.status = 'active'",
            (founder[0],),
        ).fetchone()[0]
        assert plan_name == "founder"
        # NOT NULL is now a database guarantee, not a convention.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO projects (id, script, category, status,"
                " created_at) VALUES (?, 'x', 'Documentary', 'created',"
                " '2026-11-22 00:00:00')",
                (str(uuid.uuid4()),),
            )
    finally:
        conn.close()

    # Idempotency: seed + backfill again change nothing.
    seed2 = _run_script(
        "seed_founder.py",
        scratch_db,
        {"CINEMORA_FOUNDER_PASSWORD_FILE": str(password_file)},
    )
    assert seed2.returncode == 0 and "already existed" in seed2.stdout
    backfill2 = _run_script("backfill_ownership.py", scratch_db, {})
    assert backfill2.returncode == 0
    assert "projects: 0 backfilled" in backfill2.stdout


def test_up_down_up_clean(scratch_db):
    command.upgrade(_alembic_cfg(), "head")
    command.downgrade(_alembic_cfg(), "0005_revision_root_uniqueness")
    engine = create_engine(f"sqlite:///{scratch_db}")
    try:
        assert "users" not in set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    result = _alembic_cli(scratch_db, "upgrade", "head")
    # 0007's precondition passes trivially on an empty database.
    assert result.returncode == 0, result.stderr
    engine = create_engine(f"sqlite:///{scratch_db}")
    try:
        assert "users" in set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
