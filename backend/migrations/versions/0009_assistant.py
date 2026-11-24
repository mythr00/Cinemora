"""AI Product Assistant tables (Phase 6; design
cinemora-audit/assistant/05, reconciled with Phase 8).

Five new tables, no existing table touched:

- `assistant_conversations` / `assistant_messages` — persistent,
  owner-scoped conversation history (the design draft's owner was
  the literal "operator"; Phase 8 landed real users first, so the
  owner is a users.id FK, exactly like projects.owner_id).
- `assistant_actions` — the confirmation state machine and the
  tool-execution audit log in one table.
- `assistant_tool_overrides` — the admin M9 control plane
  (per-tool / global disable, honoured on the next request).
- `assistant_security_events` — the prompt-injection rejection
  feed reviewed from the admin console.

Preferences deliberately do NOT get a table here: Phase 8's
`user_preferences` document already reserves an `assistant`
section for exactly this store (models.UserPreference).

Additive only. Downgrade drops the five tables (and with them the
assistant's history; that loss is the documented cost of
downgrading past Phase 6).

Revision ID: 0009_assistant
Revises: 0008_admin_audit_log
Create Date: 2026-11-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Matches models.JSONVariant: JSONB on PostgreSQL, JSON elsewhere.
_JSON = sa.JSON().with_variant(postgresql.JSONB, "postgresql")

revision: str = "0009_assistant"
down_revision: Union[str, None] = "0008_admin_audit_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assistant_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("title", sa.String(300), nullable=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False
        ),
    )
    op.create_index(
        "ix_assistant_conversations_owner_id",
        "assistant_conversations",
        ["owner_id"],
    )

    op.create_table(
        "assistant_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("assistant_conversations.id"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_trace", _JSON, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
    )
    op.create_index(
        "ix_assistant_messages_conversation_id",
        "assistant_messages",
        ["conversation_id"],
    )

    op.create_table(
        "assistant_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("assistant_conversations.id"),
            nullable=True,
        ),
        sa.Column(
            "message_id",
            sa.String(36),
            sa.ForeignKey("assistant_messages.id"),
            nullable=True,
        ),
        sa.Column(
            "owner_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("action_class", sa.String(20), nullable=False),
        sa.Column("arguments", _JSON, nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result", _JSON, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "executed_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.create_index(
        "ix_assistant_actions_conversation_id",
        "assistant_actions",
        ["conversation_id"],
    )
    op.create_index(
        "ix_assistant_actions_owner_id", "assistant_actions", ["owner_id"]
    )
    op.create_index(
        "ix_assistant_actions_status", "assistant_actions", ["status"]
    )

    op.create_table(
        "assistant_tool_overrides",
        sa.Column("scope", sa.String(100), primary_key=True),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.String(320), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False
        ),
    )

    op.create_table(
        "assistant_security_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("assistant_conversations.id"),
            nullable=True,
        ),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("detail", _JSON, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
    )
    op.create_index(
        "ix_assistant_security_events_owner_id",
        "assistant_security_events",
        ["owner_id"],
    )
    op.create_index(
        "ix_assistant_security_events_created_at",
        "assistant_security_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("assistant_security_events")
    op.drop_table("assistant_tool_overrides")
    op.drop_table("assistant_actions")
    op.drop_table("assistant_messages")
    op.drop_table("assistant_conversations")
