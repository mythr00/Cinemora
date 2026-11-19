"""Revision root uniqueness: one revision-0 root per project (Stage 6, D1).

The Stage 6 adversarial verification found that concurrent
`editor.create_revision_zero` / `get_editor` calls race: creation
was check-then-insert with no uniqueness guard, so N racing
threads created N revision-0 rows (parent_revision_id IS NULL)
for one project — the chain gained multiple roots and "latest
revision" became whichever root was written last. No data was
corrupted (every root imports the same run identically), but the
chain's root must be unique by construction, not by luck.

Fix: a PARTIAL unique index on project_revisions(project_id)
covering only the root rows (parent_revision_id IS NULL):

- SQLite and PostgreSQL both enforce partial indexes, so the
  guarantee holds on either database, under any interleaving —
  the race loser gets an integrity error at insert time and
  editor.create_revision_zero returns the winner's root.
- Branching is untouched: child rows have parent_revision_id set,
  so any number of children per parent (and the per-parent
  revision_number = parent + 1 scheme) remain legal.
- Non-root rows and all existing data are unaffected; the index
  is additive. Downgrade drops only the index.

Note: creating this index on a database that ALREADY contains
duplicate roots (written before this fix) fails loudly rather
than deleting rows — resolving such duplicates is a data
decision, not a migration's.

Revision ID: 0005_revision_root_uniqueness
Revises: 0004_editor_revisions
Create Date: 2026-11-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_revision_root_uniqueness"
down_revision: Union[str, None] = "0004_editor_revisions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mirrors the Index declared on models.ProjectRevision
# (uq_project_revisions_single_root).
_WHERE = sa.text("parent_revision_id IS NULL")


def upgrade() -> None:
    op.create_index(
        "uq_project_revisions_single_root",
        "project_revisions",
        ["project_id"],
        unique=True,
        sqlite_where=_WHERE,
        postgresql_where=_WHERE,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_project_revisions_single_root",
        table_name="project_revisions",
    )
