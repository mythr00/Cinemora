"""Ownership required: owner_id NOT NULL + FK to users (Phase 8.1).

Second half of the split begun in 0006 (design: cinemora-audit/
multiuser/02 §3.1). Between the two revisions the founder seed and
the ownership backfill run, populating every existing project and
production run with the founder's user id. This revision then makes
ownership a database guarantee:

- `projects.owner_id`       -> NOT NULL, FK to users.id
- `production_runs.owner_id` -> NOT NULL, FK to users.id

PRECONDITION: no NULL owner_id may remain in either table. On
SQLite the upgrade checks and refuses loudly (naming the counts)
rather than failing mid-recreate with an opaque integrity error;
run scripts/seed_founder.py then scripts/backfill_ownership.py
first. PostgreSQL gets the same guarantee from the constraint
itself.

Batch mode is used so the alteration is a table recreate on SQLite
and a plain ALTER on PostgreSQL — one code path, both dialects.

MODELS NOTE (deliberate, documented in models.py): the ORM keeps
owner_id nullable=True. Legacy test fixtures build databases with
Base.metadata.create_all and insert ownerless projects directly;
the NOT NULL guarantee lives here, in the migrated schema that
real deployments run, and in the application layer (the API never
creates an ownerless project or run — Phase 8.2). check_schema()
gates real deployments on the migration head, so a served database
always carries this enforcement.

Downgrade reverses both alterations (FK dropped, nullability
restored); the backfilled owner values are data and are kept.

Revision ID: 0007_ownership_required
Revises: 0006_users_and_ownership
Create Date: 2026-11-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_ownership_required"
down_revision: Union[str, None] = "0006_users_and_ownership"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _null_owners(bind, table: str) -> int:
    return bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {table} WHERE owner_id IS NULL")
    ).scalar_one()


def upgrade() -> None:
    bind = op.get_bind()
    for table in ("projects", "production_runs"):
        nulls = _null_owners(bind, table)
        if nulls:
            raise RuntimeError(
                f"0007 precondition failed: {table} still has {nulls} "
                "row(s) with owner_id IS NULL. Run "
                "scripts/seed_founder.py and "
                "scripts/backfill_ownership.py before upgrading to "
                "0007_ownership_required."
            )

    with op.batch_alter_table("projects") as batch_op:
        batch_op.alter_column(
            "owner_id", existing_type=sa.String(36), nullable=False
        )
        batch_op.create_foreign_key(
            "fk_projects_owner_id_users", "users", ["owner_id"], ["id"]
        )

    with op.batch_alter_table("production_runs") as batch_op:
        batch_op.alter_column(
            "owner_id", existing_type=sa.String(36), nullable=False
        )
        batch_op.create_foreign_key(
            "fk_production_runs_owner_id_users",
            "users",
            ["owner_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("production_runs") as batch_op:
        batch_op.drop_constraint(
            "fk_production_runs_owner_id_users", type_="foreignkey"
        )
        batch_op.alter_column(
            "owner_id", existing_type=sa.String(36), nullable=True
        )

    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_constraint(
            "fk_projects_owner_id_users", type_="foreignkey"
        )
        batch_op.alter_column(
            "owner_id", existing_type=sa.String(36), nullable=True
        )
