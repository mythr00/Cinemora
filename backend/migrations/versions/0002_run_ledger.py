"""Run ledger: production_runs + asset_decisions (Stage 2, AUD-09).

Adds the two ledger tables behind the ProductionRun / AssetDecision
models in models.py:

- production_runs: one row per production run — lifecycle status,
  script hash (dedup), options, run manifest pointer, the finishing
  run_metrics execution snapshot, and the cooperative-cancellation
  flag. Job lifecycle (dedup / claim / cancel / stale reconciliation)
  is built on this table by services/run_ledger.py.
- asset_decisions: the APPEND-ONLY decision ledger — every selected /
  rotated / generated / still / missing / rejected / swapped visual
  decision for a run, with its evidence class, verbatim verification
  verdict and rights classification. Rows are never updated or
  deleted by application code.

This migration is the schema authority for these tables going
forward (AUD-09): the ORM models mirror it exactly. The existing
`projects` table is untouched. Downgrade drops only the two new
tables (asset_decisions first — it references production_runs).

Revision ID: 0002_run_ledger
Revises: 0001_baseline
Create Date: 2026-11-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_run_ledger"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Matches models.JSONVariant: JSONB on PostgreSQL, JSON elsewhere.
_JSON = sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def upgrade() -> None:
    op.create_table(
        "production_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("stage", sa.String(length=100), nullable=True),
        sa.Column("progress", _JSON, nullable=True),
        sa.Column("options", _JSON, nullable=True),
        sa.Column("script_hash", sa.String(length=64), nullable=True),
        sa.Column("manifest_path", sa.String(length=500), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("execution", _JSON, nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_production_runs_project_hash_status",
        "production_runs",
        ["project_id", "script_hash", "status"],
    )
    op.create_index(
        "ix_production_runs_status_created",
        "production_runs",
        ["status", "created_at"],
    )

    op.create_table(
        "asset_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("asset_key", sa.String(length=500), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=200), nullable=True),
        sa.Column("scene_number", sa.Integer(), nullable=True),
        sa.Column("sentence_number", sa.Integer(), nullable=True),
        sa.Column("interval_start", sa.Float(), nullable=True),
        sa.Column("interval_duration", sa.Float(), nullable=True),
        sa.Column("evidence_class", sa.String(length=100), nullable=True),
        sa.Column("verification", _JSON, nullable=True),
        sa.Column("rights", _JSON, nullable=True),
        sa.Column("decision_type", sa.String(length=50), nullable=False),
        sa.Column("previous_decision_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["production_runs.id"]),
        sa.ForeignKeyConstraint(
            ["previous_decision_id"], ["asset_decisions.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_asset_decisions_run", "asset_decisions", ["run_id"])
    op.create_index(
        "ix_asset_decisions_project", "asset_decisions", ["project_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_asset_decisions_project", table_name="asset_decisions")
    op.drop_index("ix_asset_decisions_run", table_name="asset_decisions")
    op.drop_table("asset_decisions")
    op.drop_index(
        "ix_production_runs_status_created", table_name="production_runs"
    )
    op.drop_index(
        "ix_production_runs_project_hash_status",
        table_name="production_runs",
    )
    op.drop_table("production_runs")
