"""Phase 8: scheduled plan change (directive §15).

One additive column:

- `user_subscriptions.pending_plan_id` — the plan a downgrade
  (or any scheduled change) takes effect with at the next
  benefit-period boundary. The lifecycle job applies it at
  rollover and clears it. NULL = no change scheduled. Upgrades
  never use it: they apply immediately with proration.

Revision ID: 0014_subscription_pending_plan
Revises: 0013_billing
Create Date: 2026-11-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_subscription_pending_plan"
down_revision: Union[str, None] = "0013_billing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Batch mode: SQLite cannot ALTER in an FK constraint
    # directly; on PostgreSQL batch is a passthrough. The FK
    # gets an explicit name (batch recreation requires it).
    with op.batch_alter_table("user_subscriptions") as batch_op:
        batch_op.add_column(
            sa.Column("pending_plan_id", sa.String(36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_user_subscriptions_pending_plan_id",
            "plans",
            ["pending_plan_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("user_subscriptions") as batch_op:
        batch_op.drop_constraint(
            "fk_user_subscriptions_pending_plan_id",
            type_="foreignkey",
        )
        batch_op.drop_column("pending_plan_id")
