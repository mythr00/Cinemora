from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from database import Base


# JSONB on PostgreSQL, plain JSON everywhere else (SQLite in tests).
JSONVariant = JSON().with_variant(JSONB, "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """A platform account (Phase 8.1, multi-user SaaS migration).

    Schema authority: migrations/versions/0006_users_and_ownership.py.
    Passwords are Argon2id hashes (services/auth.py); the reset-token
    columns hold only the token's SHA-256 plus expiry, never the
    token itself. `role` is the Phase 7 ladder (user < admin <
    super_admin) as one column — roles are never combined, so no
    join table (design 02 §1.1).
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # Stored lowercased; uniqueness is case-insensitive because every
    # write path normalises (services/auth.py / seed script).
    email: Mapped[str] = mapped_column(
        String(320), nullable=False, unique=True, index=True
    )

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    display_name: Mapped[str] = mapped_column(String(200), nullable=True)

    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user"
    )

    # active / disabled — disabled means no login, sessions revoked,
    # data retained (design 02 §1.1).
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )

    email_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    password_reset_token_hash: Mapped[str] = mapped_column(
        String(64), nullable=True
    )

    password_reset_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    last_login_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Plan(Base):
    """A subscription plan record (Phase 8.1; design 02 §5.1).

    Operator-managed seed data (scripts/seed_founder.py ensures the
    founder/free/pro/studio rows); limits are enforced at the points
    named in 02 §5.3 by Phase 8.3. NULL limit = unlimited.
    """

    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    name: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True
    )

    max_concurrent_runs: Mapped[int] = mapped_column(
        nullable=False, default=1
    )

    max_runs_per_period: Mapped[int] = mapped_column(nullable=True)

    period: Mapped[str] = mapped_column(
        String(20), nullable=False, default="month"
    )

    max_script_chars: Mapped[int] = mapped_column(nullable=True)

    max_storage_bytes: Mapped[int] = mapped_column(nullable=True)

    max_upload_bytes: Mapped[int] = mapped_column(nullable=True)

    # 0 = standard lane, 1 = priority lane (design 03 §3.1).
    priority_tier: Mapped[int] = mapped_column(
        nullable=False, default=0
    )

    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class UserSubscription(Base):
    """One user's subscription to a plan (Phase 8.1; design 02 §5.1).

    Exactly ONE active subscription per user, enforced by the
    partial unique index below (both dialects, the 0005 pattern).
    `billing_provider` / `external_id` are the payment seam only:
    no billing integration exists; until the founder approves one,
    every subscription is founder-granted and `status` transitions
    are operator actions.
    """

    __tablename__ = "user_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )

    plan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plans.id"), nullable=False
    )

    # active / cancelled / past_due
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )

    current_period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    current_period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    billing_provider: Mapped[str] = mapped_column(String(50), nullable=True)

    external_id: Mapped[str] = mapped_column(String(200), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index(
            "uq_user_subscriptions_one_active",
            "user_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )


class UserPreference(Base):
    """One user's preferences document (Phase 8.1; design 02 §6).

    A single JSON document with namespaced sections (`studio`,
    `assistant`, `notifications`) — one preferences store for the
    whole platform; the assistant section keeps Phase 6's rule that
    only explicitly user-approved key/values are stored.
    """

    __tablename__ = "user_preferences"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), primary_key=True
    )

    document: Mapped[dict] = mapped_column(JSONVariant, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )


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

    # Phase 8.1 ownership (design 02 §3.1): added nullable by
    # migration 0006, backfilled to the founder, enforced NOT NULL
    # by migration 0007 on migrated databases. The ORM deliberately
    # keeps the column nullable: legacy test fixtures build schemas
    # with create_all and insert ownerless projects directly, and
    # the enforcement that matters lives in the migrated schema +
    # the API layer, which never creates an ownerless project
    # (Phase 8.2 owner choke point). Runs carry a denormalised copy
    # so queue/fairness queries never join (02 §4.1). Declared LAST
    # so the ORM column order matches the migrated schema exactly
    # (0006 appends the column; the schema-authority tests compare
    # column lists, AUD-09).
    owner_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id"),
        nullable=True,
        index=True,
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

    # queued -> running -> completed / failed / cancelled, plus
    # awaiting_approval: the plan-review gate parks an analysed run
    # there (jobs.run_analysis_phase via run_ledger.set_run_status)
    # until plan approval claims it back. "rendering" is NOT a run
    # status — it is the ProjectRevision status a revision carries
    # while its revision-render run (queued -> running -> terminal
    # here) executes (services/editor.py request_render).
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

    # Phase 8.1: denormalised owner copy (the project's owner at
    # submission time) — see Project.owner_id for the enforcement
    # note. Indexed with status for dispatcher fairness queries.
    # Declared last to match the migrated column order (AUD-09).
    owner_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id"),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "ix_production_runs_project_hash_status",
            "project_id",
            "script_hash",
            "status",
        ),
        Index("ix_production_runs_status_created", "status", "created_at"),
        Index(
            "ix_production_runs_owner_status",
            "owner_id",
            "status",
        ),
    )


class AssetDecision(Base):
    """Append-only ledger of visual decisions for a run (Stage 2).

    One row per decision: an accepted asset selected for a sentence, a
    rotation assignment, a generated graphic, a missing visual, a
    rejected candidate, a verified-but-not-picked candidate or a
    download failure (Phase 1 / AM-015), or a review swap
    (swapped_in / swapped_out, linked by previous_decision_id). Rows
    are NEVER updated or deleted — a later decision supersedes an
    earlier one by pointing at it, so the evidence trail cannot be
    rewritten.
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
    Schema authority: migrations/versions/0004_editor_revisions.py
    (the table); the single-root partial unique index below is
    migrations/versions/0005_revision_root_uniqueness.py — exactly
    ONE revision-0 root (parent_revision_id IS NULL) may exist per
    project, enforced by the database on SQLite and PostgreSQL
    alike, so concurrent revision-0 creation cannot fork the chain
    (Stage 6 adversarial defect D1). Branching below the root
    (several children of one parent) is unaffected and intended.
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
        # Exactly one revision-0 root per project (D1): a partial
        # unique index over the root rows only — children keep
        # parent_revision_id set, so branching is unconstrained.
        Index(
            "uq_project_revisions_single_root",
            "project_id",
            unique=True,
            sqlite_where=text("parent_revision_id IS NULL"),
            postgresql_where=text("parent_revision_id IS NULL"),
        ),
    )

class AdminAuditLog(Base):
    """Append-only record of platform-administration actions
    (Phase 7; design admin/03 §3).

    Written by services/admin_audit.py for every WRITE/CONSEQUENTIAL
    admin action, admin sign-ins, and failed admin authorisation
    attempts. There is deliberately no update or delete path in
    application code and the admin viewer is read-only: this table
    is the platform's administrative memory, kept separate from the
    production `asset_decisions` ledger (different domain).
    Schema authority: migrations/versions/0008_admin_audit_log.py.
    """

    __tablename__ = "admin_audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    actor_identity: Mapped[str] = mapped_column(
        String(320), nullable=False, index=True
    )

    actor_role: Mapped[str] = mapped_column(String(20), nullable=False)

    # "session" | "operator_token" (services.auth Principal).
    auth_method: Mapped[str] = mapped_column(String(30), nullable=False)

    # Typed verb: admin.sign_in, admin.auth_denied, run.cancel,
    # run.submit_replacement, user.status_change, user.plan_assign.
    action: Mapped[str] = mapped_column(
        String(60), nullable=False, index=True
    )

    target_type: Mapped[str] = mapped_column(String(40), nullable=True)

    target_id: Mapped[str] = mapped_column(
        String(64), nullable=True, index=True
    )

    # Action parameters / before-after (never secrets).
    detail: Mapped[dict] = mapped_column(JSONVariant, nullable=True)

    # "success" | "failure"
    result: Mapped[str] = mapped_column(String(20), nullable=False)

    error: Mapped[str] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        index=True,
    )


class AssistantConversation(Base):
    """One AI Product Assistant conversation (Phase 6; design
    assistant/05 §1.2, reconciled with Phase 8: the owner is a real
    users.id, not the design draft's literal "operator").

    Owner-scoped like every Phase 8 surface: another user's
    conversation is indistinguishable from a nonexistent one.
    `project_id` optionally binds the conversation to one of the
    owner's projects for context (validated at creation and again
    whenever context is assembled).
    Schema authority: migrations/versions/0009_assistant.py.
    """

    __tablename__ = "assistant_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    owner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(300), nullable=True)

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )


class AssistantMessage(Base):
    """One message in an assistant conversation (Phase 6; design
    assistant/05 §1.3). `tool_trace` on assistant turns is the
    ordered record of tool calls the answer rests on — the UI's
    provenance strip and the tests' factuality anchor. Tool rows
    store a compact result summary, never a full payload dump.
    Schema authority: migrations/versions/0009_assistant.py.
    """

    __tablename__ = "assistant_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_conversations.id"),
        nullable=False,
        index=True,
    )

    # user | assistant | tool | system_note
    role: Mapped[str] = mapped_column(String(20), nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)

    tool_trace: Mapped[dict] = mapped_column(JSONVariant, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class AssistantAction(Base):
    """The assistant's confirmation + audit record in one (Phase 6;
    design assistant/05 §1.4).

    Every tool execution writes a row: READ tools land directly as
    `executed` (or `failed`); WRITE/CONSEQUENTIAL tools are created
    as `pending` by the orchestrator and can only move to `executed`
    through the authenticated confirm endpoint — the model has no
    path to execution. Rows are never deleted; status transitions
    are the only updates. Arguments and results pass the redaction
    filter before persistence (design assistant/08 §4).
    Schema authority: migrations/versions/0009_assistant.py.
    """

    __tablename__ = "assistant_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_conversations.id"),
        nullable=True,
        index=True,
    )

    message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("assistant_messages.id"), nullable=True
    )

    owner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )

    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # read | write | consequential
    action_class: Mapped[str] = mapped_column(String(20), nullable=False)

    arguments: Mapped[dict] = mapped_column(JSONVariant, nullable=True)

    # pending | confirmed | rejected | expired | executed | failed
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True
    )

    result: Mapped[dict] = mapped_column(JSONVariant, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AssistantToolOverride(Base):
    """Admin M9 control plane for the assistant (Phase 6 + admin
    design 02 §M9): one row per scope — a tool name, or "*" for the
    whole assistant. The orchestrator reads overrides on every turn,
    so disabling a tool (or the assistant) takes effect on the next
    request, as the M9 acceptance test requires. Written only
    through the admin router, which audit-logs every change.
    Schema authority: migrations/versions/0009_assistant.py.
    """

    __tablename__ = "assistant_tool_overrides"

    # Tool name, or "*" for the global assistant switch.
    scope: Mapped[str] = mapped_column(String(100), primary_key=True)

    disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    updated_by: Mapped[str] = mapped_column(String(320), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )


class AssistantSecurityEvent(Base):
    """Prompt-injection rejection feed (Phase 6; design
    assistant/08 §6 + admin M9). One row when retrieved content
    (search result, fetched page, project script read back through
    a tool) carries instruction-shaped text aimed at the assistant.
    The content is treated as data regardless; the row exists so
    the attempt is reviewable in the admin console.
    Schema authority: migrations/versions/0009_assistant.py.
    """

    __tablename__ = "assistant_security_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    owner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )

    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("assistant_conversations.id"),
        nullable=True,
    )

    # search_result | fetched_page | project_script | tool_result
    source: Mapped[str] = mapped_column(String(40), nullable=False)

    # Matched pattern name + a short redacted excerpt.
    detail: Mapped[dict] = mapped_column(JSONVariant, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        index=True,
    )


class ProjectMember(Base):
    """One account's membership of one project (Collaboration,
    founder priority #11; gap-audit doc 04 §4).

    Schema authority: migrations/versions/0010_collaboration.py.

    The access layer ABOVE ownership the audit prescribes: the
    project keeps exactly one owner (`projects.owner_id`); everyone
    else reaches it through a row here. `role` is editor | viewer
    (the owner is never a row). `status` is the invitation
    lifecycle — invited -> accepted | declined, and accepted ->
    removed (by the owner) or left (by the member, recorded the
    same way with `removed_by_id` = the member themselves). Rows
    are never deleted: a removed/declined row is the recorded
    history the activity feed assembles from, and re-inviting
    revives the same row. Uniqueness of (project_id, user_id) is
    the unique index below.
    """

    __tablename__ = "project_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=False, index=True
    )

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )

    # editor | viewer
    role: Mapped[str] = mapped_column(String(20), nullable=False)

    # invited | accepted | declined | removed
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="invited"
    )

    invited_by_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    responded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    removed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    removed_by_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )

    __table_args__ = (
        Index(
            "uq_project_members_project_user",
            "project_id",
            "user_id",
            unique=True,
        ),
    )


class ProjectComment(Base):
    """A member's comment on a project (Collaboration, #11).

    Schema authority: migrations/versions/0010_collaboration.py.

    Anchored to the project, and optionally to one editor revision
    (`revision_id` — the revision chain's id, validated against the
    project's revisions at creation; kept as a plain column, not an
    FK, because revision documents are artefact-backed and their
    table is a chain index, mirroring how plan documents anchor
    per-sentence rows). Author + timestamps are recorded on the
    row; edits set `edited_at`. Deletion is a hard delete by the
    author or the project owner — the comment's existence in the
    activity feed is derived from live rows only.
    """

    __tablename__ = "project_comments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=False, index=True
    )

    revision_id: Mapped[str] = mapped_column(String(36), nullable=True)

    author_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )

    body: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )

    edited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
