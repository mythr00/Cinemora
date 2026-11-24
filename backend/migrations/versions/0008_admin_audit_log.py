"""Admin audit log (Phase 7; design cinemora-audit/admin/03 §3).

One new table, `admin_audit_log`: the append-only record of
platform-administration actions (admin sign-ins, run cancels and
replacement submissions, account status changes, plan assignments,
failed admin authorisation attempts). No existing table can serve:
the platform's other ledgers are production-domain records
(asset_decisions, production_runs), and nothing recorded API/admin
operations (Phase 7 platform audit §8).

Additive only — no existing table is touched. Downgrade drops the
table (and with it the audit history; that loss is the documented
cost of downgrading past Phase 7).

Revision ID: 0008_admin_audit_log
Revises: 0007_ownership_required
Create Date: 2026-11-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Matches models.JSONVariant: JSONB on PostgreSQL, JSON elsewhere.
_JSON = sa.JSON().with_variant(postgresql.JSONB, "postgresql")

revision: str = "0008_admin_audit_log"
down_revision: Union[str, None] = "0007_ownership_required"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_identity", sa.String(320), nullable=False),
        sa.Column("actor_role", sa.String(20), nullable=False),
        sa.Column("auth_method", sa.String(30), nullable=False),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("detail", _JSON, nullable=True),
        sa.Column("result", sa.String(20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_admin_audit_log_actor_identity", "admin_audit_log",
        ["actor_identity"],
    )
    op.create_index(
        "ix_admin_audit_log_action", "admin_audit_log", ["action"]
    )
    op.create_index(
        "ix_admin_audit_log_target_id", "admin_audit_log", ["target_id"]
    )
    op.create_index(
        "ix_admin_audit_log_created_at", "admin_audit_log", ["created_at"]
    )


def downgrade() -> None:
    op.drop_table("admin_audit_log")
