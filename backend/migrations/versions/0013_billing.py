"""Phase 8 monetization: credits, subscriptions, payments,
retention (founder directive 2026-11-25, §§1–21).

Additive only:

- `plans` gains the commercial catalogue columns (prices in
  cents, monthly credits, included generations, script/thumbnail
  allowances, billing_mode legacy|metered, features, is_public).
  Existing rows keep billing_mode='legacy' — the pre-monetization
  behaviour — until the catalogue sync seeds the metered plans.
- `user_subscriptions` gains billing_interval,
  cancel_at_period_end and provider_customer_id.
- New tables: credit_accounts, credit_ledger (immutable,
  idempotency-keyed), billing_periods, generation_reservations,
  topup_packages, coupons, coupon_redemptions, payments,
  payment_events (webhook inbox), pricing_rules (ships empty —
  costs stay unknown until configured), generated_videos (§21
  retention registry) and user_notifications.

No existing row is modified and no existing column is altered.
Downgrade drops the new tables and columns (billing history
would be lost — the documented cost of downgrading past this
phase).

Revision ID: 0013_billing
Revises: 0012_provider_usage
Create Date: 2026-11-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_billing"
down_revision: Union[str, None] = "0012_provider_usage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONVariant = sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def upgrade() -> None:
    # --- plans: commercial catalogue ------------------------------
    op.add_column(
        "plans", sa.Column("price_monthly_cents", sa.Integer(),
                           nullable=True)
    )
    op.add_column(
        "plans", sa.Column("price_annual_cents", sa.Integer(),
                           nullable=True)
    )
    op.add_column(
        "plans",
        sa.Column("currency", sa.String(3), nullable=False,
                  server_default="USD"),
    )
    op.add_column(
        "plans", sa.Column("monthly_credits", sa.Integer(),
                           nullable=True)
    )
    op.add_column(
        "plans", sa.Column("included_generations", sa.Integer(),
                           nullable=True)
    )
    op.add_column(
        "plans", sa.Column("script_allowance", sa.Integer(),
                           nullable=True)
    )
    op.add_column(
        "plans", sa.Column("thumbnail_allowance", sa.Integer(),
                           nullable=True)
    )
    op.add_column(
        "plans",
        sa.Column("billing_mode", sa.String(20), nullable=False,
                  server_default="legacy"),
    )
    op.add_column(
        "plans", sa.Column("features", JSONVariant, nullable=True)
    )
    op.add_column(
        "plans",
        sa.Column("is_public", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )

    # --- user_subscriptions: billing lifecycle --------------------
    op.add_column(
        "user_subscriptions",
        sa.Column("billing_interval", sa.String(10), nullable=True),
    )
    op.add_column(
        "user_subscriptions",
        sa.Column("cancel_at_period_end", sa.Boolean(),
                  nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "user_subscriptions",
        sa.Column("provider_customer_id", sa.String(200),
                  nullable=True),
    )

    # --- credit_accounts ------------------------------------------
    op.create_table(
        "credit_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36),
                  sa.ForeignKey("users.id"), nullable=False,
                  unique=True),
        sa.Column("subscription_available", sa.Integer(),
                  nullable=False, server_default="0"),
        sa.Column("subscription_reserved", sa.Integer(),
                  nullable=False, server_default="0"),
        sa.Column("purchased_available", sa.Integer(),
                  nullable=False, server_default="0"),
        sa.Column("purchased_reserved", sa.Integer(),
                  nullable=False, server_default="0"),
        sa.Column("promo_available", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("promo_reserved", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("lifetime_spent", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False),
    )
    op.create_index(
        "ix_credit_accounts_user_id", "credit_accounts", ["user_id"]
    )

    # --- credit_ledger --------------------------------------------
    op.create_table(
        "credit_ledger",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_id", sa.String(36),
                  sa.ForeignKey("credit_accounts.id"),
                  nullable=False),
        sa.Column("entry_type", sa.String(30), nullable=False),
        sa.Column("bucket", sa.String(20), nullable=True),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("subscription_balance_after", sa.Integer(),
                  nullable=True),
        sa.Column("purchased_balance_after", sa.Integer(),
                  nullable=True),
        sa.Column("promo_balance_after", sa.Integer(),
                  nullable=True),
        sa.Column("subscription_id", sa.String(36), nullable=True),
        sa.Column("billing_period_id", sa.String(36), nullable=True),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("payment_id", sa.String(36), nullable=True),
        sa.Column("reservation_id", sa.String(36), nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=True,
                  unique=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source", sa.String(20), nullable=False,
                  server_default="system"),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("details", JSONVariant, nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
    )
    op.create_index(
        "ix_credit_ledger_user_id", "credit_ledger", ["user_id"]
    )
    op.create_index(
        "ix_credit_ledger_account_id", "credit_ledger",
        ["account_id"],
    )
    op.create_index(
        "ix_credit_ledger_entry_type", "credit_ledger",
        ["entry_type"],
    )
    op.create_index(
        "ix_credit_ledger_run_id", "credit_ledger", ["run_id"]
    )
    op.create_index(
        "ix_credit_ledger_created_at", "credit_ledger",
        ["created_at"],
    )

    # --- billing_periods ------------------------------------------
    op.create_table(
        "billing_periods",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("subscription_id", sa.String(36),
                  sa.ForeignKey("user_subscriptions.id"),
                  nullable=False),
        sa.Column("plan_id", sa.String(36),
                  sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("generations_granted", sa.Integer(),
                  nullable=False, server_default="0"),
        sa.Column("generations_used", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("scripts_granted", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("scripts_used", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("thumbnails_granted", sa.Integer(),
                  nullable=False, server_default="0"),
        sa.Column("thumbnails_used", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("credits_allocated", sa.Integer(),
                  nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
        sa.UniqueConstraint(
            "user_id", "period_start",
            name="uq_billing_periods_user_start",
        ),
    )
    op.create_index(
        "ix_billing_periods_user_id", "billing_periods", ["user_id"]
    )
    op.create_index(
        "ix_billing_periods_subscription_id", "billing_periods",
        ["subscription_id"],
    )

    # --- generation_reservations ----------------------------------
    op.create_table(
        "generation_reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False,
                  unique=True),
        sa.Column("root_run_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("billing_period_id", sa.String(36),
                  nullable=True),
        sa.Column("entitlement_reserved", sa.Boolean(),
                  nullable=False, server_default=sa.false()),
        sa.Column("credits_reserved", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("estimated_seconds", sa.Float(), nullable=True),
        sa.Column("actual_seconds", sa.Float(), nullable=True),
        sa.Column("charged_credits", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="active"),
        sa.Column("source", sa.String(20), nullable=False,
                  server_default="user"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True),
                  nullable=True),
    )
    op.create_index(
        "ix_generation_reservations_run_id",
        "generation_reservations", ["run_id"],
    )
    op.create_index(
        "ix_generation_reservations_root_run_id",
        "generation_reservations", ["root_run_id"],
    )
    op.create_index(
        "ix_generation_reservations_user_id",
        "generation_reservations", ["user_id"],
    )
    op.create_index(
        "ix_generation_reservations_project_id",
        "generation_reservations", ["project_id"],
    )
    op.create_index(
        "ix_generation_reservations_status",
        "generation_reservations", ["status"],
    )

    # --- topup_packages -------------------------------------------
    op.create_table(
        "topup_packages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False,
                  unique=True),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False,
                  server_default="USD"),
        sa.Column("validity_months", sa.Integer(), nullable=False,
                  server_default="12"),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("provider_price_id", sa.String(200),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False),
    )

    # --- coupons ---------------------------------------------------
    op.create_table(
        "coupons",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False,
                  unique=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("percent_off", sa.Integer(), nullable=True),
        sa.Column("amount_off_cents", sa.Integer(), nullable=True),
        sa.Column("credits_grant", sa.Integer(), nullable=True),
        sa.Column("promo_expires_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("max_redemptions", sa.Integer(), nullable=True),
        sa.Column("redeemed_count", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("starts_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
    )

    # --- coupon_redemptions ---------------------------------------
    op.create_table(
        "coupon_redemptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("coupon_id", sa.String(36),
                  sa.ForeignKey("coupons.id"), nullable=False),
        sa.Column("user_id", sa.String(36),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("payment_id", sa.String(36), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True),
                  nullable=False),
        sa.UniqueConstraint(
            "coupon_id", "user_id",
            name="uq_coupon_redemptions_once",
        ),
    )
    op.create_index(
        "ix_coupon_redemptions_coupon_id", "coupon_redemptions",
        ["coupon_id"],
    )
    op.create_index(
        "ix_coupon_redemptions_user_id", "coupon_redemptions",
        ["user_id"],
    )

    # --- payments ---------------------------------------------------
    op.create_table(
        "payments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("provider_transaction_id", sa.String(200),
                  nullable=True, unique=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False,
                  server_default="USD"),
        sa.Column("plan_id", sa.String(36),
                  sa.ForeignKey("plans.id"), nullable=True),
        sa.Column("topup_package_id", sa.String(36),
                  sa.ForeignKey("topup_packages.id"), nullable=True),
        sa.Column("subscription_id", sa.String(36),
                  sa.ForeignKey("user_subscriptions.id"),
                  nullable=True),
        sa.Column("coupon_id", sa.String(36),
                  sa.ForeignKey("coupons.id"), nullable=True),
        sa.Column("refunded_amount_cents", sa.Integer(),
                  nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False),
    )
    op.create_index(
        "ix_payments_user_id", "payments", ["user_id"]
    )
    op.create_index(
        "ix_payments_provider", "payments", ["provider"]
    )
    op.create_index("ix_payments_status", "payments", ["status"])
    op.create_index(
        "ix_payments_created_at", "payments", ["created_at"]
    )

    # --- payment_events -------------------------------------------
    op.create_table(
        "payment_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("event_id", sa.String(200), nullable=False),
        sa.Column("event_type", sa.String(120), nullable=False),
        sa.Column("payload", JSONVariant, nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
        sa.UniqueConstraint(
            "provider", "event_id",
            name="uq_payment_events_provider_event",
        ),
    )
    op.create_index(
        "ix_payment_events_created_at", "payment_events",
        ["created_at"],
    )

    # --- pricing_rules (ships empty: costs stay unknown) ----------
    op.create_table(
        "pricing_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("unit", sa.String(30), nullable=False),
        sa.Column("rate_microusd", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("configured_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
    )
    op.create_index(
        "ix_pricing_rules_provider", "pricing_rules", ["provider"]
    )

    # --- generated_videos (§21 retention registry) -----------------
    op.create_table(
        "generated_videos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("revision_id", sa.String(36), nullable=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("media_key", sa.String(120), nullable=True),
        sa.Column("relative_path", sa.String(500), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("delete_after", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="active"),
        sa.Column("deleted_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("storage_confirmed", sa.Boolean(),
                  nullable=False, server_default=sa.false()),
        sa.Column("download_count", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("first_downloaded_at",
                  sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_downloaded_at",
                  sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_attempts", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("last_delete_error", sa.Text(), nullable=True),
        sa.Column("reminded_24h", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("reminded_6h", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("reminded_1h", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )
    op.create_index(
        "ix_generated_videos_user_id", "generated_videos",
        ["user_id"],
    )
    op.create_index(
        "ix_generated_videos_project_id", "generated_videos",
        ["project_id"],
    )
    op.create_index(
        "ix_generated_videos_run_id", "generated_videos",
        ["run_id"],
    )
    op.create_index(
        "ix_generated_videos_created_at", "generated_videos",
        ["created_at"],
    )
    op.create_index(
        "ix_generated_videos_delete_after", "generated_videos",
        ["delete_after"],
    )
    op.create_index(
        "ix_generated_videos_status", "generated_videos",
        ["status"],
    )

    # --- user_notifications ----------------------------------------
    op.create_table(
        "user_notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("generated_video_id", sa.String(36),
                  nullable=True),
        sa.Column("email_status", sa.String(20), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
    )
    op.create_index(
        "ix_user_notifications_user_id", "user_notifications",
        ["user_id"],
    )
    op.create_index(
        "ix_user_notifications_created_at", "user_notifications",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("user_notifications")
    op.drop_table("generated_videos")
    op.drop_table("pricing_rules")
    op.drop_table("payment_events")
    op.drop_table("payments")
    op.drop_table("coupon_redemptions")
    op.drop_table("coupons")
    op.drop_table("topup_packages")
    op.drop_table("generation_reservations")
    op.drop_table("billing_periods")
    op.drop_table("credit_ledger")
    op.drop_table("credit_accounts")

    op.drop_column("user_subscriptions", "provider_customer_id")
    op.drop_column("user_subscriptions", "cancel_at_period_end")
    op.drop_column("user_subscriptions", "billing_interval")

    op.drop_column("plans", "is_public")
    op.drop_column("plans", "features")
    op.drop_column("plans", "billing_mode")
    op.drop_column("plans", "thumbnail_allowance")
    op.drop_column("plans", "script_allowance")
    op.drop_column("plans", "included_generations")
    op.drop_column("plans", "monthly_credits")
    op.drop_column("plans", "currency")
    op.drop_column("plans", "price_annual_cents")
    op.drop_column("plans", "price_monthly_cents")
