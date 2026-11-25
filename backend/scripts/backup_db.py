#!/usr/bin/env python3
"""Consistent, manifest-recorded backup of the local-stack SQLite DB,
with a *tested* restore path (audit finding E-09).

Why this exists
---------------
Before this script, the only "backups" of the preview stack were the
launcher's manual, unencrypted, same-VM ``preview.db.bak-*`` copies
(``cinemora-preview/start-stack.sh``) and no restore had ever been
tested. This script provides, for the LOCAL (SQLite) stack:

  create   Online-safe snapshot via the SQLite backup API (the
           source is opened read-only; the live stack is never
           stopped, locked for writing, or replaced), plus a JSON
           manifest: timestamp, source path, byte size, SHA-256,
           Alembic version, and a row count for every table.
  verify   Restore drill: copies a backup into a TEMPORARY directory
           (the live database is never touched), opens the restored
           copy, runs ``PRAGMA integrity_check`` and compares the
           Alembic version and every table's row count against the
           manifest. Exit code 0 only if everything matches.

Usage
-----
  python scripts/backup_db.py create [--source PATH] [--dest-dir DIR]
  python scripts/backup_db.py verify BACKUP.db [--manifest PATH]
  python scripts/backup_db.py verify --latest [--dest-dir DIR]

Defaults: source ``~/workspace/cinemora-preview/preview.db``,
dest dir ``~/workspace/cinemora-preview/backups/``. Backup files are
named ``cinemora-backup-<UTC timestamp>.db`` with a sibling
``.manifest.json``.

PRODUCTION REQUIREMENT (recommendation — NOT implemented here)
--------------------------------------------------------------
This script covers the local SQLite preview stack only. The
production PostgreSQL deployment (E-07 cutover) requires, before the
first non-founder account is admitted:

  * a SCHEDULED ``pg_dump`` (custom format) of the production
    database, shipped OFF-HOST and encrypted at rest, with a
    retention/pruning policy;
  * a media-tree backup taken as one consistent set with the DB
    backup (E-08/E-09: DB rows reference media files);
  * a periodic RESTORE DRILL of that ``pg_dump`` onto scratch
    infrastructure, verified the same way ``verify`` does here
    (integrity + Alembic head + row counts + a known project
    end-to-end), with the drill result recorded.

None of that exists yet; do not describe production backup as
covered until it is built and drilled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SOURCE = Path.home() / "workspace" / "cinemora-preview" / "preview.db"
DEFAULT_DEST_DIR = Path.home() / "workspace" / "cinemora-preview" / "backups"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inspect(db_path: Path) -> dict:
    """Integrity, Alembic version and per-table row counts of a DB file."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        row_counts = {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }
        alembic_version = None
        if "alembic_version" in tables:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            alembic_version = row[0] if row else None
        return {
            "integrity_check": integrity,
            "alembic_version": alembic_version,
            "row_counts": row_counts,
        }
    finally:
        conn.close()


def create_backup(source: Path, dest_dir: Path) -> tuple[Path, Path]:
    if not source.exists():
        raise SystemExit(f"source database not found: {source}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = dest_dir / f"cinemora-backup-{stamp}.db"
    manifest_path = dest_dir / f"cinemora-backup-{stamp}.manifest.json"

    # Online-safe snapshot: source opened read-only, SQLite backup API
    # copies a transactionally consistent image while the stack runs.
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(backup_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    info = _inspect(backup_path)
    manifest = {
        "kind": "cinemora-sqlite-backup",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source),
        "backup_file": backup_path.name,
        "size_bytes": backup_path.stat().st_size,
        "sha256": _sha256(backup_path),
        **info,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return backup_path, manifest_path


def verify_backup(backup_path: Path, manifest_path: Path) -> dict:
    """Restore into a temp dir and compare against the manifest."""
    manifest = json.loads(manifest_path.read_text())
    problems: list[str] = []

    actual_sha = _sha256(backup_path)
    if actual_sha != manifest["sha256"]:
        problems.append(
            f"sha256 mismatch: manifest {manifest['sha256']} != file {actual_sha}"
        )
    if backup_path.stat().st_size != manifest["size_bytes"]:
        problems.append("size_bytes mismatch with manifest")

    with tempfile.TemporaryDirectory(prefix="cinemora-restore-") as tmp:
        restored = Path(tmp) / "restored.db"
        shutil.copy2(backup_path, restored)  # the restore itself
        info = _inspect(restored)

    if info["integrity_check"] != "ok":
        problems.append(f"integrity_check: {info['integrity_check']}")
    if info["alembic_version"] != manifest["alembic_version"]:
        problems.append(
            f"alembic_version mismatch: manifest {manifest['alembic_version']} "
            f"!= restored {info['alembic_version']}"
        )
    if info["row_counts"] != manifest["row_counts"]:
        for table in sorted(set(info["row_counts"]) | set(manifest["row_counts"])):
            expected = manifest["row_counts"].get(table)
            actual = info["row_counts"].get(table)
            if expected != actual:
                problems.append(
                    f"row count mismatch in {table}: manifest {expected} != restored {actual}"
                )
    return {
        "backup": str(backup_path),
        "manifest": str(manifest_path),
        "ok": not problems,
        "problems": problems,
        "alembic_version": info["alembic_version"],
        "tables": len(info["row_counts"]),
        "total_rows": sum(info["row_counts"].values()),
        "row_counts": info["row_counts"],
    }


def _latest_backup(dest_dir: Path) -> Path:
    candidates = sorted(dest_dir.glob("cinemora-backup-*.db"))
    if not candidates:
        raise SystemExit(f"no backups found in {dest_dir}")
    return candidates[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="snapshot the live DB + manifest")
    p_create.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    p_create.add_argument("--dest-dir", type=Path, default=DEFAULT_DEST_DIR)

    p_verify = sub.add_parser("verify", help="restore-test a backup in a temp dir")
    p_verify.add_argument("backup", type=Path, nargs="?", default=None)
    p_verify.add_argument("--manifest", type=Path, default=None)
    p_verify.add_argument("--latest", action="store_true")
    p_verify.add_argument("--dest-dir", type=Path, default=DEFAULT_DEST_DIR)

    args = parser.parse_args()

    if args.command == "create":
        backup_path, manifest_path = create_backup(args.source, args.dest_dir)
        manifest = json.loads(manifest_path.read_text())
        print(f"backup:   {backup_path}")
        print(f"manifest: {manifest_path}")
        print(
            f"size={manifest['size_bytes']} sha256={manifest['sha256'][:16]}… "
            f"alembic={manifest['alembic_version']} "
            f"tables={len(manifest['row_counts'])} "
            f"rows={sum(manifest['row_counts'].values())} "
            f"integrity={manifest['integrity_check']}"
        )
        return 0

    backup_path = _latest_backup(args.dest_dir) if args.latest else args.backup
    if backup_path is None:
        parser.error("verify needs a backup path or --latest")
    manifest_path = args.manifest or backup_path.with_suffix("").with_suffix(
        ".manifest.json"
    )
    if not manifest_path.exists():
        # cinemora-backup-<stamp>.db -> cinemora-backup-<stamp>.manifest.json
        manifest_path = backup_path.parent / (backup_path.stem + ".manifest.json")
    result = verify_backup(backup_path, manifest_path)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
