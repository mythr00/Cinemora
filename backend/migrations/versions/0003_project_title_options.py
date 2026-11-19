"""Project title + options for the API v1 studio contract (Stage 4).

Adds two nullable columns to the existing `projects` table:

- title: the human title the studio sends at creation (the legacy
  create route never accepted one; the v1 contract does).
- options: the production options chosen at creation (genre,
  voice_id, captions, music_file, require_plan_approval, plus any
  caller keys), persisted so a later run submission can consume
  exactly what the user picked — AUD-06's "options silently
  discarded" break, closed at the schema level for the v1 path.

Both columns are nullable: every pre-existing project row stays
valid and nothing is rewritten. Downgrade drops only these two
columns.

Revision ID: 0003_project_title_options
Revises: 0002_run_ledger
Create Date: 2026-11-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_project_title_options"
down_revision: Union[str, None] = "0002_run_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Matches models.JSONVariant: JSONB on PostgreSQL, JSON elsewhere.
_JSON = sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def upgrade() -> None:
    op.add_column("projects", sa.Column("title", sa.String(length=300), nullable=True))
    op.add_column("projects", sa.Column("options", _JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "options")
    op.drop_column("projects", "title")
