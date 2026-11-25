"""Provider usage ledger (Phase 5c-1 / B-10).

One new table, no existing table touched:

- `provider_usage_events` — append-only ledger of LLM provider
  calls: provider, model, and the token usage the provider
  actually reported (NULL when it reported none — unknown stays
  unknown). Optional caller context (user / project / run) is
  NULL when the caller had none to pass, never fabricated.

There are deliberately NO cost or price columns: no price table
is configured anywhere in the system. The Phase 8 cost engine
will price these events later from its own configuration; the
admin aggregate renders cost as null with
pricing_status="not_configured" until then.

Additive only. Downgrade drops the table (and with it the usage
history; that loss is the documented cost of downgrading past
this phase).

Revision ID: 0012_provider_usage
Revises: 0011_developer_api
Create Date: 2026-11-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_provider_usage"
down_revision: Union[str, None] = "0011_developer_api"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "provider_usage_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("context", sa.String(120), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
    )
    op.create_index(
        "ix_provider_usage_events_provider",
        "provider_usage_events",
        ["provider"],
    )
    op.create_index(
        "ix_provider_usage_events_created_at",
        "provider_usage_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("provider_usage_events")
