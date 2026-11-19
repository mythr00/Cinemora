from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from database import Base


# JSONB on PostgreSQL, plain JSON everywhere else (SQLite in tests).
JSONVariant = JSON().with_variant(JSONB, "postgresql")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
    )

    script: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # Stage 4 (API v1): the studio contract carries a human title and
    # the production options chosen at creation. Both are nullable so
    # every pre-v1 row stays valid; schema authority is
    # migrations/versions/0003_project_title_options.py (AUD-09).
    title: Mapped[str] = mapped_column(
        String(300),
        nullable=True,
    )

    options: Mapped[dict] = mapped_column(
        JSONVariant,
        nullable=True,
    )

    category: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="Documentary",
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="created",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class ProductionRun(Base):
    """One production run of one project (Stage 2 ledger, AUD-07/10).

    The run is the unit of lifecycle: submissions are deduplicated on
    (project_id, script_hash) while a run is active, queued runs are
    claimed oldest-first, and a run's terminal status is recorded here
    instead of living only in a JSON file on the worker's disk.
    Schema authority: migrations/versions/0002_run_ledger.py (AUD-09).
    """

    __tablename__ = "production_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id"),
        nullable=False,
    )

    # queued -> running -> completed / failed / cancelled
    # (awaiting_approval / rendering are reserved for the plan-review
    # studio stage; no code sets them yet).
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="queued"
    )

    stage: Mapped[str] = mapped_column(String(100), nullable=True)

    # Free-form progress detail (e.g. {"stage": ..., "percent": ...});
    # the headline status lives in `status`.
    progress: Mapped[dict] = mapped_column(JSONVariant, nullable=True)

    options: Mapped[dict] = mapped_column(JSONVariant, nullable=True)

    # Full sha256 of the exact script text (jobs.script_hash_for is a
    # truncated display hash and is NOT used here).
    script_hash: Mapped[str] = mapped_column(String(64), nullable=True)

    manifest_path: Mapped[str] = mapped_column(String(500), nullable=True)

    error: Mapped[str] = mapped_column(Text, nullable=True)

    # The run_metrics snapshot taken when the run finished (Stage 0
    # execution block: provider calls, downloads, stage durations).
    execution: Mapped[dict] = mapped_column(JSONVariant, nullable=True)

    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "ix_production_runs_project_hash_status",
            "project_id",
            "script_hash",
            "status",
        ),
        Index("ix_production_runs_status_created", "status", "created_at"),
    )


class AssetDecision(Base):
    """Append-only ledger of visual decisions for a run (Stage 2).

    One row per decision: an accepted asset selected for a sentence, a
    rotation assignment, a generated graphic, a missing visual, a
    rejected candidate, or a review swap (swapped_in / swapped_out,
    linked by previous_decision_id). Rows are NEVER updated or
    deleted — a later decision supersedes an earlier one by pointing
    at it, so the evidence trail cannot be rewritten.
    Schema authority: migrations/versions/0002_run_ledger.py (AUD-09).
    """

    __tablename__ = "asset_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("production_runs.id"),
        nullable=False,
    )

    project_id: Mapped[str] = mapped_column(String(36), nullable=False)

    # Candidate id when the pipeline assigned one, else the content
    # hash, else the file path — the most stable identity available.
    asset_key: Mapped[str] = mapped_column(String(500), nullable=True)

    # sha256 of the media file itself when a local file exists; None
    # for decisions with no file (missing visuals, rejected candidates
    # that were never downloaded, generated graphics before render).
    content_hash: Mapped[str] = mapped_column(String(64), nullable=True)

    source_url: Mapped[str] = mapped_column(Text, nullable=True)

    provider: Mapped[str] = mapped_column(String(200), nullable=True)

    scene_number: Mapped[int] = mapped_column(nullable=True)

    sentence_number: Mapped[int] = mapped_column(nullable=True)

    interval_start: Mapped[float] = mapped_column(Float, nullable=True)

    interval_duration: Mapped[float] = mapped_column(Float, nullable=True)

    # The segment-evidence class (verified_segment,
    # positional_no_contract, ...) exactly as the pipeline stamped it.
    evidence_class: Mapped[str] = mapped_column(String(100), nullable=True)

    # The verification verdict dict, verbatim (never summarised into a
    # boolean — the verdict's reasons are the evidence).
    verification: Mapped[dict] = mapped_column(JSONVariant, nullable=True)

    # The rights classification dict (licence, rights_status, creator,
    # source, retrieved_at) — "unknown" stays "unknown" (VA-09).
    rights: Mapped[dict] = mapped_column(JSONVariant, nullable=True)

    decision_type: Mapped[str] = mapped_column(String(50), nullable=False)

    previous_decision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("asset_decisions.id"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Stage 6 (editor): the revision whose edit produced this row,
    # or NULL for decisions recorded by the original production run.
    # The ledger stays append-only — a revision never edits or
    # deletes the run's rows; it appends its own, tagged here, so a
    # per-revision certificate can select exactly the original
    # decisions plus one revision chain's decisions. Declared last
    # so the ORM column order matches the 0004 ALTER TABLE order.
    revision_id: Mapped[str] = mapped_column(String(36), nullable=True)

    __table_args__ = (
        Index("ix_asset_decisions_run", "run_id"),
        Index("ix_asset_decisions_project", "project_id"),
        Index("ix_asset_decisions_revision", "revision_id"),
    )


class ProjectRevision(Base):
    """One node in a project's editor revision chain (Stage 6).

    A revision is a non-destructive edit of a completed production:
    revision 0 imports the production's timeline into the editor
    document shape; every applied batch of edit operations creates
    the NEXT node (parent_revision_id) instead of mutating its
    parent — the chain is the backend basis for undo/redo, and any
    earlier revision stays reachable and renderable.

    The database row is the index of record; the timeline document
    itself lives in a file under the project media directory
    (revision_<id>.json) — the established pattern: DB = truth for
    records, files for documents/media. The original production's
    artefacts are never modified by the editor.
    Schema authority: migrations/versions/0004_editor_revisions.py.
    """

    __tablename__ = "project_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id"),
        nullable=False,
    )

    # The completed production run this chain edits. Revision
    # decisions are appended to THIS run's ledger (tagged with
    # revision_id), exactly like Stage 4's plan swaps.
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("production_runs.id"),
        nullable=False,
    )

    parent_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("project_revisions.id"),
        nullable=True,
    )

    # 0 for the imported production; +1 per applied operation batch
    # (or per explicit draft creation) along the chain.
    revision_number: Mapped[int] = mapped_column(nullable=False, default=0)

    # draft -> rendering -> rendered -> exported; failed when the
    # latest render run failed its audit or errored (the document
    # itself stays editable — a child batch can be applied to a
    # failed revision like any other).
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="draft"
    )

    # File name (under the project media dir) of the timeline
    # document: revision_<id>.json.
    document_path: Mapped[str] = mapped_column(String(500), nullable=False)

    # sha256 of the document as written at creation — an integrity
    # anchor so later readers can prove a revision document was not
    # rewritten out of band.
    document_hash: Mapped[str] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_project_revisions_project", "project_id"),
        Index("ix_project_revisions_run", "run_id"),
    )