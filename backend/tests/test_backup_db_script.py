"""E-09: the backup script must produce a manifest-recorded snapshot
whose restore drill actually verifies (integrity + Alembic version +
row counts), and must detect a tampered backup. Hermetic: runs on a
tiny throwaway SQLite database in tmp_path, never the preview DB.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backup_db.py"
_spec = importlib.util.spec_from_file_location("backup_db", _SCRIPT)
backup_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup_db)


def _make_source(path: Path) -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
    conn.execute("INSERT INTO alembic_version VALUES ('0011_developer_api')")
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, title TEXT)")
    conn.executemany(
        "INSERT INTO projects VALUES (?, ?)",
        [("p1", "One"), ("p2", "Two"), ("p3", "Three")],
    )
    conn.commit()
    conn.close()
    return path


def test_create_and_verify_roundtrip(tmp_path):
    source = _make_source(tmp_path / "source.db")
    backup_path, manifest_path = backup_db.create_backup(source, tmp_path / "backups")
    assert backup_path.exists() and manifest_path.exists()

    result = backup_db.verify_backup(backup_path, manifest_path)
    assert result["ok"], result["problems"]
    assert result["alembic_version"] == "0011_developer_api"
    assert result["row_counts"]["projects"] == 3
    assert result["total_rows"] == 4


def test_verify_detects_tampering(tmp_path):
    source = _make_source(tmp_path / "source.db")
    backup_path, manifest_path = backup_db.create_backup(source, tmp_path / "backups")

    conn = sqlite3.connect(str(backup_path))
    conn.execute("INSERT INTO projects VALUES ('p4', 'Sneaky')")
    conn.commit()
    conn.close()

    result = backup_db.verify_backup(backup_path, manifest_path)
    assert not result["ok"]
    assert any("projects" in problem for problem in result["problems"])
