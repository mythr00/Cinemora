"""Users, plans, subscriptions, preferences + ownership columns (Phase 8.1).

The multi-user SaaS migration (founder directive 2026-11-22; design:
cinemora-audit/multiuser/02 §1.1, §3.1, §5.1, §6) starts here. This
revision is purely additive:

- `users` — the account table (Argon2id password hashes, role ladder
  user/admin/super_admin, status active/disabled).
- `plans` — operator-managed plan records (founder/free/pro/studio
  are seeded by scripts/seed_founder.py, not by this migration: seed
  data that an operator tunes does not belong in schema history).
- `user_subscriptions` — one ACTIVE subscription per user, enforced
  by a partial unique index (the 0005 pattern, both dialects).
  `billing_provider` / `external_id` are the payment seam only —
  no billing integration exists or is implied.
- `user_preferences` — one JSON document per user with namespaced
  sections (studio / assistant / notifications), per 02 §6.
- `projects.owner_id` and `production_runs.owner_id` — NULLABLE and
  indexed, with NO foreign key yet. The founder seed + backfill
  (scripts/) populate them; 0007 then adds the FK and enforces
  NOT NULL. Splitting add -> backfill -> enforce keeps every step
  reversible and lets the dual-auth window (02 §1.4) run on the
  intermediate schema.

Asset decisions and project revisions get no owner column: they
scope transitively through their project/run rows (01 §2.1).

Downgrade drops exactly what upgrade created, in reverse order.

Revision ID: 0006_users_and_ownership
Revises: 0005_revision_root_uniqueness
Create Date: 2026-11-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_users_and_ownership"
down_revision: Union[str, None] = "0005_revision_root_uniqueness"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mirrors the partial unique index declared on
# models.UserSubscription (uq_user_subscriptions_one_active).
_ACTIVE = sa.text("status = 'active'")

# Matches models.JSONVariant: JSONB on PostgreSQL, JSON elsewhere.
_JSON = sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("role", sa.String(20), nullable=False, server_default="user"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_reset_token_hash", sa.String(64), nullable=True),
        sa.Column("password_reset_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("max_concurrent_runs", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_runs_per_period", sa.Integer(), nullable=True),
        sa.Column("period", sa.String(20), nullable=False, server_default="month"),
        sa.Column("max_script_chars", sa.Integer(), nullable=True),
        sa.Column("max_storage_bytes", sa.BigInteger(), nullable=True),
        sa.Column("max_upload_bytes", sa.BigInteger(), nullable=True),
        sa.Column("priority_tier", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("name", name="uq_plans_name"),
    )

    op.create_table(
        "user_subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            sa.String(36),
            sa.ForeignKey("plans.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("billing_provider", sa.String(50), nullable=True),
        sa.Column("external_id", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_user_subscriptions_user", "user_subscriptions", ["user_id"]
    )
    op.create_index(
        "uq_user_subscriptions_one_active",
        "user_subscriptions",
        ["user_id"],
        unique=True,
        sqlite_where=_ACTIVE,
        postgresql_where=_ACTIVE,
    )

    op.create_table(
        "user_preferences",
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            primary_key=True,
        ),
        sa.Column("document", _JSON, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # Ownership: nullable, indexed, FK deferred to 0007 (see docstring).
    op.add_column(
        "projects", sa.Column("owner_id", sa.String(36), nullable=True)
    )
    op.create_index("ix_projects_owner", "projects", ["owner_id"])
    op.add_column(
        "production_runs",
        sa.Column("owner_id", sa.String(36), nullable=True),
    )
    op.create_index(
        "ix_production_runs_owner_status",
        "production_runs",
        ["owner_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_production_runs_owner_status", table_name="production_runs"
    )
    op.drop_column("production_runs", "owner_id")
    op.drop_index("ix_projects_owner", table_name="projects")
    op.drop_column("projects", "owner_id")
    op.drop_table("user_preferences")
    op.drop_index(
        "uq_user_subscriptions_one_active", table_name="user_subscriptions"
    )
    op.drop_index(
        "ix_user_subscriptions_user", table_name="user_subscriptions"
    )
    op.drop_table("user_subscriptions")
    op.drop_table("plans")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
