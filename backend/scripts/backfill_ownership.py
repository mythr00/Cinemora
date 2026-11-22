"""Backfill owner_id onto existing projects and production runs
(Phase 8.1; design 02 §3.2).

Runs BETWEEN migrations 0006 and 0007: 0006 added the nullable
owner columns, this script populates them, 0007 then enforces
NOT NULL + FK.

- Every project's owner becomes the founder user (created by
  scripts/seed_founder.py; this script refuses to run without one).
- Every production run takes the owner OF ITS PROJECT — set from
  the join, then asserted row by row, so a disagreement between a
  run's owner and its project's owner can never slip through.
- Idempotent: only NULL owners are written; a second run verifies
  and changes nothing.
- Asset decisions and revisions need no backfill: they scope
  through their project/run rows.

Prints counts only — never row contents, never credentials.

Usage (from the backend directory):
    DATABASE_URL=sqlite:////path/to.db python scripts/backfill_ownership.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from database import SessionLocal  # noqa: E402
import models  # noqa: E402

FOUNDER_EMAIL_DEFAULT = "mirthdraught@gmail.com"


def main() -> int:
    email = os.environ.get(
        "CINEMORA_FOUNDER_EMAIL", FOUNDER_EMAIL_DEFAULT
    ).strip().lower()

    db = SessionLocal()
    try:
        founder = db.query(models.User).filter_by(email=email).one_or_none()
        if founder is None:
            print(
                f"ERROR: founder user {email} not found — run "
                "scripts/seed_founder.py first."
            )
            return 1

        projects_updated = (
            db.query(models.Project)
            .filter(models.Project.owner_id.is_(None))
            .update(
                {models.Project.owner_id: founder.id},
                synchronize_session=False,
            )
        )

        # Runs take their project's owner (single UPDATE ... FROM
        # via correlated subquery — portable across SQLite/Postgres).
        runs_updated = db.execute(
            text(
                "UPDATE production_runs SET owner_id = ("
                "  SELECT owner_id FROM projects"
                "  WHERE projects.id = production_runs.project_id"
                ") WHERE owner_id IS NULL"
            )
        ).rowcount

        db.commit()

        # Assertions: no NULLs left, and every run agrees with its
        # project, checked row by row (02 §3.2 step 4).
        null_projects = (
            db.query(models.Project)
            .filter(models.Project.owner_id.is_(None))
            .count()
        )
        null_runs = (
            db.query(models.ProductionRun)
            .filter(models.ProductionRun.owner_id.is_(None))
            .count()
        )
        disagreements = 0
        for run in db.query(models.ProductionRun).all():
            project = db.get(models.Project, run.project_id)
            if project is None or run.owner_id != project.owner_id:
                disagreements += 1

        total_projects = db.query(models.Project).count()
        total_runs = db.query(models.ProductionRun).count()
        print(f"projects: {projects_updated} backfilled, {total_projects} total")
        print(f"runs: {runs_updated} backfilled, {total_runs} total")
        print(f"NULL owners remaining — projects: {null_projects}, runs: {null_runs}")
        print(f"run/project owner disagreements: {disagreements}")
        if null_projects or null_runs or disagreements:
            print("ERROR: backfill incomplete — do NOT upgrade to 0007.")
            return 1
        print("backfill verified: safe to upgrade to 0007_ownership_required")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
