import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker


logger = logging.getLogger(__name__)


load_dotenv()


DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured.")


engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


# =========================================================
# SCHEMA AUTHORITY (IA-09)
# =========================================================
# Alembic is the ONLY schema authority: the migration chain in
# migrations/versions (0001-0005) creates and evolves every table.
# main.py used to run Base.metadata.create_all(bind=engine) at
# import time, which silently created any missing table outside
# the migration chain and let the two authorities drift apart.
# That call is gone. Instead, serving startup (main.py's lifespan)
# calls check_schema() below, which verifies — read-only, it never
# creates, alters or stamps anything — that the configured
# database is at the migration head:
#
# - any model table missing            -> SchemaVersionError
#   (an empty / unmigrated database fails here);
# - alembic_version behind the head    -> SchemaVersionError;
# - all model tables present but NO alembic_version stamp
#   (a legacy database created by the old import-time create_all,
#   or a test database built by an explicit fixture create_all)
#   -> loud WARNING + stamping guidance, startup proceeds: the
#   schema is physically complete, only its provenance is
#   unrecorded, and failing hard would break every pre-Alembic
#   database (and the test fixtures) without protecting anything —
#   the operator action is to verify the schema and stamp it;
# - stamped exactly at head            -> OK.
#
# Importing this module (or main) never runs the check and never
# touches the database; only serving startup does.

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

UPGRADE_FIX = (
    "Fix: run `alembic upgrade head` from the backend directory "
    "with DATABASE_URL pointing at this database."
)


class SchemaVersionError(RuntimeError):
    """The configured database is not at the Alembic migration head."""


def migration_head() -> str:
    """The single head revision of the migration chain on disk."""
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory(str(MIGRATIONS_DIR)).get_heads()
    if len(heads) != 1:
        raise SchemaVersionError(
            "IA-09 schema check: expected exactly one Alembic head "
            f"in {MIGRATIONS_DIR}, found {heads or 'none'} — the "
            "migration chain itself is broken; refusing to start."
        )
    return heads[0]


def check_schema(bind=None) -> dict:
    """Verify the database behind `bind` (default: the app engine)
    is at the Alembic migration head. Read-only.

    Returns {"status": "ok" | "unstamped_legacy",
             "current_revision": str | None,
             "expected_head": str, "missing_tables": []}.
    Raises SchemaVersionError when tables are missing or the
    stamped revision is behind (or unknown to) the head.
    """
    # Register every model table on Base.metadata (local import:
    # models imports Base from here, so a top-level import would
    # be circular; by call time models is importable).
    import models  # noqa: F401

    bind = bind if bind is not None else engine
    head = migration_head()

    present = set(inspect(bind).get_table_names())
    model_tables = set(Base.metadata.tables)
    missing = sorted(model_tables - present)
    if missing:
        raise SchemaVersionError(
            "IA-09 schema check FAILED: database is missing "
            f"table(s): {', '.join(missing)}. Expected Alembic "
            f"head '{head}'. The application no longer creates "
            "tables at import (Alembic is the schema authority). "
            + UPGRADE_FIX
        )

    current = None
    if "alembic_version" in present:
        if hasattr(bind, "connect"):
            with bind.connect() as connection:
                rows = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalars().all()
        else:  # a bare Connection
            rows = bind.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all()
        if len(rows) > 1:
            raise SchemaVersionError(
                "IA-09 schema check FAILED: alembic_version holds "
                f"{len(rows)} revisions ({', '.join(sorted(rows))}); "
                "expected exactly one. Resolve the migration state "
                "manually, then " + UPGRADE_FIX
            )
        current = rows[0] if rows else None

    if current is None:
        logger.warning(
            "IA-09 SCHEMA WARNING: all model tables are present but "
            "this database has NO alembic_version stamp — it was "
            "created outside the migration chain (legacy create_all "
            "database). Serving will continue, but Alembic is the "
            "schema authority: verify the schema matches head '%s', "
            "then run `alembic stamp head` from the backend "
            "directory (or rebuild the database with "
            "`alembic upgrade head`).",
            head,
        )
        return {
            "status": "unstamped_legacy",
            "current_revision": None,
            "expected_head": head,
            "missing_tables": [],
        }

    if current != head:
        raise SchemaVersionError(
            "IA-09 schema check FAILED: database schema revision "
            f"'{current}' is not the Alembic head '{head}' (behind "
            "or unknown to the migration chain). Refusing to serve "
            "against an out-of-date schema. " + UPGRADE_FIX
        )

    return {
        "status": "ok",
        "current_revision": current,
        "expected_head": head,
        "missing_tables": [],
    }