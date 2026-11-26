"""Run status separation (Phase 4).

Adds the technical/release status columns to production_runs:
technical_status, release_status and the release_findings JSON
document (services/release_status.py). All three are nullable —
NULL means "not evaluated" (pre-Phase-4 runs and runs that never
reached the final audit) and is never backfilled.

Revision ID: 0016_run_status_separation
Revises: 0015_affiliates
Create Date: 2026-11-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_run_status_separation"
down_revision: Union[str, None] = "0015_affiliates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Matches models.JSONVariant: JSONB on PostgreSQL, JSON elsewhere.
_JSON = sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def upgrade() -> None:
    op.add_column(
        "production_runs",
        sa.Column("technical_status", sa.String(50), nullable=True),
    )
    op.add_column(
        "production_runs",
        sa.Column("release_status", sa.String(50), nullable=True),
    )
    op.add_column(
        "production_runs",
        sa.Column("release_findings", _JSON, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("production_runs", "release_findings")
    op.drop_column("production_runs", "release_status")
    op.drop_column("production_runs", "technical_status")
