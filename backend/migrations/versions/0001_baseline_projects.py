"""Baseline: existing production schema (single `projects` table).

This migration captures the schema exactly as models.py defines it today and
as `Base.metadata.create_all` has been creating it in production. It changes
nothing about the schema; it establishes the Alembic baseline (AUD-09) so all
future model changes ship as reviewed migrations instead of silent
create_all no-ops.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("script", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("projects")
