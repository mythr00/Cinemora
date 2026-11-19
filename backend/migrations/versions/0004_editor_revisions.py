"""Editor revisions: project_revisions + asset_decisions.revision_id (Stage 6).

Adds the self-service editor's persistence:

- project_revisions: one row per node in a project's revision
  chain. Revision 0 imports a completed production's timeline into
  the editor document shape; every applied batch of edit operations
  creates the next node (parent_revision_id) rather than mutating
  its parent — non-destructive editing with the chain as the
  undo/redo basis. The timeline document itself is a file under the
  project media directory (revision_<id>.json); the row is its
  index of record (DB = truth for records, files for documents).
- asset_decisions.revision_id (nullable): tags ledger rows an
  editor revision appended (swaps, uploads, adds, render-time
  records) so a per-revision provenance certificate can select the
  original run decisions (revision_id NULL) plus exactly one
  revision chain's decisions. Existing rows are untouched and stay
  NULL — the original productions' ledgers are byte-identical in
  meaning.

Both changes are additive. Downgrade drops only the new column,
its index and the new table; no production data is rewritten in
either direction.

Revision ID: 0004_editor_revisions
Revises: 0003_project_title_options
Create Date: 2026-11-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_editor_revisions"
down_revision: Union[str, None] = "0003_project_title_options"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Matches models.JSONVariant: JSONB on PostgreSQL, JSON elsewhere.
_JSON = sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def upgrade() -> None:
    op.create_table(
        "project_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("parent_revision_id", sa.String(length=36), nullable=True),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("document_path", sa.String(length=500), nullable=False),
        sa.Column("document_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["production_runs.id"]),
        sa.ForeignKeyConstraint(
            ["parent_revision_id"], ["project_revisions.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_project_revisions_project", "project_revisions", ["project_id"]
    )
    op.create_index(
        "ix_project_revisions_run", "project_revisions", ["run_id"]
    )

    op.add_column(
        "asset_decisions",
        sa.Column("revision_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_asset_decisions_revision", "asset_decisions", ["revision_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_asset_decisions_revision", table_name="asset_decisions")
    op.drop_column("asset_decisions", "revision_id")
    op.drop_index("ix_project_revisions_run", table_name="project_revisions")
    op.drop_index(
        "ix_project_revisions_project", table_name="project_revisions"
    )
    op.drop_table("project_revisions")
