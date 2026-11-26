"""Affiliate programme (Product Improvements item 1).

Four additive tables: affiliate_profiles, affiliate_clicks,
affiliate_attributions, commission_ledger. No existing table is
altered. Commission accrues only from verified payment events;
the ledger is append-only with UNIQUE idempotency keys.

Revision ID: 0015_affiliates
Revises: 0014_subscription_pending_plan
Create Date: 2026-11-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_affiliates"
down_revision: Union[str, None] = "0014_subscription_pending_plan"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "affiliate_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id", sa.String(36),
            sa.ForeignKey("users.id"), nullable=False, unique=True,
        ),
        sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("payout_preference", sa.String(30), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
    )
    op.create_table(
        "affiliate_clicks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "affiliate_id", sa.String(36),
            sa.ForeignKey("affiliate_profiles.id"), nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
    )
    op.create_table(
        "affiliate_attributions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "referred_user_id", sa.String(36),
            sa.ForeignKey("users.id"), nullable=False, unique=True,
        ),
        sa.Column(
            "affiliate_id", sa.String(36),
            sa.ForeignKey("affiliate_profiles.id"), nullable=False,
        ),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
    )
    op.create_table(
        "commission_ledger",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "affiliate_id", sa.String(36),
            sa.ForeignKey("affiliate_profiles.id"), nullable=False,
        ),
        sa.Column(
            "referred_user_id", sa.String(36),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column(
            "payment_id", sa.String(36),
            sa.ForeignKey("payments.id"), nullable=True,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("gross_cents", sa.Integer(), nullable=False),
        sa.Column("rate_percent", sa.Integer(), nullable=False),
        sa.Column("commission_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "idempotency_key", sa.String(200), nullable=False,
            unique=True,
        ),
        sa.Column(
            "reversal_of_id", sa.String(36),
            sa.ForeignKey("commission_ledger.id"), nullable=True,
        ),
        sa.Column("decided_by", sa.String(36), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("decision_reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
    )


def downgrade() -> None:
    op.drop_table("commission_ledger")
    op.drop_table("affiliate_attributions")
    op.drop_table("affiliate_clicks")
    op.drop_table("affiliate_profiles")
