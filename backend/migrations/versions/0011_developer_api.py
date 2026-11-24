"""Developer API tables (founder priority #16; gap-audit doc 06
Phase N).

Three new tables, no existing table touched:

- `api_keys` — per-user Developer API keys on the Phase 8 user
  model. Only the secret's SHA-256 and a short display/lookup
  prefix are stored; the secret itself is shown once at creation
  and never persists. Revocation is a timestamp.
- `webhook_endpoints` — user-registered run-lifecycle webhooks.
  Only the signing secret's SHA-256 is stored; deliveries sign
  with HMAC-SHA256 keyed by that hash (services/webhooks.py).
- `webhook_deliveries` — one row per delivery attempt, written
  from real traffic only.

Additive only. Downgrade drops all three tables (and with them
the key/webhook history; that loss is the documented cost of
downgrading past this phase).

Revision ID: 0011_developer_api
Revises: 0010_collaboration
Create Date: 2026-11-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_developer_api"
down_revision: Union[str, None] = "0010_collaboration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Matches models.JSONVariant: JSONB on PostgreSQL, JSON elsewhere.
_JSON = sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("scopes", _JSON, nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "revoked_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "last_used_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "request_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"])

    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("events", _JSON, nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "disabled_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.create_index(
        "ix_webhook_endpoints_user_id", "webhook_endpoints", ["user_id"]
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "endpoint_id",
            sa.String(36),
            sa.ForeignKey("webhook_endpoints.id"),
            nullable=False,
        ),
        sa.Column("delivery_group", sa.String(36), nullable=False),
        sa.Column("event", sa.String(40), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_endpoint_id",
        "webhook_deliveries",
        ["endpoint_id"],
    )
    op.create_index(
        "ix_webhook_deliveries_delivery_group",
        "webhook_deliveries",
        ["delivery_group"],
    )
    op.create_index(
        "ix_webhook_deliveries_created_at",
        "webhook_deliveries",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
    op.drop_table("webhook_endpoints")
    op.drop_table("api_keys")
