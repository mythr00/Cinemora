"""Collaboration tables (founder priority #11; gap-audit doc 04 §4).

Two new tables, no existing table touched:

- `project_members` — the access layer above ownership: one row
  per (project, account) carrying the member role (editor |
  viewer) and the invitation lifecycle (invited -> accepted |
  declined -> removed). The project owner is never a row; ownership
  stays singular on `projects.owner_id`.
- `project_comments` — member comments on a project, optionally
  anchored to one editor revision, with author and timestamps.

Additive only. Downgrade drops both tables (and with them the
membership and comment history; that loss is the documented cost
of downgrading past this phase).

Revision ID: 0010_collaboration
Revises: 0009_assistant
Create Date: 2026-11-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_collaboration"
down_revision: Union[str, None] = "0009_assistant"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_members",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "invited_by_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "responded_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "removed_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "removed_by_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_project_members_project_id", "project_members", ["project_id"]
    )
    op.create_index(
        "ix_project_members_user_id", "project_members", ["user_id"]
    )
    op.create_index(
        "uq_project_members_project_user",
        "project_members",
        ["project_id", "user_id"],
        unique=True,
    )

    op.create_table(
        "project_comments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("revision_id", sa.String(36), nullable=True),
        sa.Column(
            "author_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "edited_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.create_index(
        "ix_project_comments_project_id", "project_comments", ["project_id"]
    )
    op.create_index(
        "ix_project_comments_author_id", "project_comments", ["author_id"]
    )
    op.create_index(
        "ix_project_comments_created_at", "project_comments", ["created_at"]
    )


def downgrade() -> None:
    op.drop_table("project_comments")
    op.drop_table("project_members")
