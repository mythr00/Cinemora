"""Collaboration (founder priority #11; gap-audit doc 04 §4).

The access layer ABOVE ownership the audit prescribes. Ownership
stays exactly as Phase 8 built it — one `projects.owner_id`, the
founder/operator bridge for legacy rows — and collaboration adds
grants next to it:

- `project_members` rows (migration 0010): an owner invites an
  EXISTING account by email to a project as `editor` or `viewer`.
  Invitations are in-app only — there is no email service
  configured, so nothing here sends or claims to send email; the
  invitee sees the invitation when they sign in and accepts or
  declines it there.
- Roles: owner (singular, from projects.owner_id) > editor
  (production/editor/research writes, no ownership-level actions)
  > viewer (read-only on production surfaces; may still take part
  in discussion — comments are open to every accepted member,
  which is the point of inviting a reviewer). Ownership-level
  actions — inviting, changing roles, removing members — are
  owner-only and enforced here, server-side, on every call.
- Comments: stored records on the project, optionally anchored to
  one editor revision (validated against the project's revision
  chain at creation). Author edits/deletes their own; the owner
  may delete any comment on their project.
- Activity: assembled ONLY from recorded events — production runs
  (run ledger), editor revisions (revision chain), plan decisions
  (plan_decisions.json), membership lifecycle rows, and comments.
  Nothing is synthesised; a source that has no records contributes
  no events.

Fail-soft membership reads: until a database is migrated to 0010,
the project_members table may not exist. Membership lookups catch
the SQLAlchemy error and resolve to "no membership" (owner access
is unaffected — it never touches the new tables), so the API keeps
serving owner traffic against an unmigrated database. Writes to
the collaboration tables themselves are NOT fail-soft: inviting
against an unmigrated database fails loudly rather than pretending
to record a grant that does not exist.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import SQLAlchemyError

from models import ProjectComment, ProjectMember, User

ROLE_OWNER = "owner"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"
MEMBER_ROLES = (ROLE_EDITOR, ROLE_VIEWER)

STATUS_INVITED = "invited"
STATUS_ACCEPTED = "accepted"
STATUS_DECLINED = "declined"
STATUS_REMOVED = "removed"

MAX_COMMENT_CHARS = 5000
ACTIVITY_LIMIT = 200


class CollaborationError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------
# Role resolution (the choke point's grant semantics)
# ---------------------------------------------------------------------


def membership_row(db, project_id: str, user_id: str) -> ProjectMember | None:
    """The membership row for (project, user), any status — or None.
    Fail-soft: an unmigrated database (no project_members table)
    reads as "no membership", never as an error."""
    try:
        return (
            db.query(ProjectMember)
            .filter(
                ProjectMember.project_id == str(project_id),
                ProjectMember.user_id == str(user_id),
            )
            .one_or_none()
        )
    except SQLAlchemyError:
        db.rollback()
        return None


def role_for(db, project, principal) -> str | None:
    """The principal's effective role on a project: "owner",
    "editor", "viewer", or None (no access). Mirrors the legacy
    NULL-owner rule of the Phase 8 choke point: ownerless legacy
    rows belong to the operator-token principal."""
    owner = project.owner_id
    if owner is not None:
        if owner == principal.user_id:
            return ROLE_OWNER
    elif principal.auth_method == "operator_token":
        return ROLE_OWNER
    row = membership_row(db, project.id, principal.user_id)
    if row is not None and row.status == STATUS_ACCEPTED:
        return row.role
    return None


def shared_project_ids(db, user_id: str) -> list[str]:
    """Projects the user is an accepted member of (not owner of).
    Fail-soft like membership_row."""
    try:
        rows = (
            db.query(ProjectMember.project_id)
            .filter(
                ProjectMember.user_id == str(user_id),
                ProjectMember.status == STATUS_ACCEPTED,
            )
            .all()
        )
        return [row[0] for row in rows]
    except SQLAlchemyError:
        db.rollback()
        return []


# ---------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------


def _user_brief(db, user_id: str | None) -> dict | None:
    if not user_id:
        return None
    user = db.get(User, str(user_id))
    if user is None:
        return {"user_id": user_id, "email": None, "display_name": None}
    return {
        "user_id": user.id,
        "email": user.email,
        "display_name": user.display_name,
    }


def member_view(db, row: ProjectMember) -> dict:
    return {
        "user": _user_brief(db, row.user_id),
        "role": row.role,
        "status": row.status,
        "invited_by": _user_brief(db, row.invited_by_id),
        "invited_at": _iso(row.created_at),
        "responded_at": _iso(row.responded_at),
        "removed_at": _iso(row.removed_at),
        "removed_by": _user_brief(db, row.removed_by_id),
    }


def comment_view(db, row: ProjectComment) -> dict:
    return {
        "comment_id": row.id,
        "project_id": row.project_id,
        "revision_id": row.revision_id,
        "author": _user_brief(db, row.author_id),
        "body": row.body,
        "created_at": _iso(row.created_at),
        "edited_at": _iso(row.edited_at),
    }


# ---------------------------------------------------------------------
# Membership lifecycle (owner-only writes are enforced by callers
# passing the already-verified owner project; this layer enforces
# the state machine itself)
# ---------------------------------------------------------------------


def invite_member(db, project, *, inviter_id: str, email: str, role: str):
    if role not in MEMBER_ROLES:
        raise CollaborationError(
            422, "invalid_role", "role must be one of: editor, viewer."
        )
    normalised = (email or "").strip().lower()
    if not normalised:
        raise CollaborationError(
            422, "email_required", "An email address is required."
        )
    invitee = (
        db.query(User).filter(User.email == normalised).one_or_none()
    )
    if invitee is None:
        raise CollaborationError(
            404,
            "account_not_found",
            "No Cinemora account exists for that email address. "
            "Invitations can only be sent to existing accounts, and "
            "no email is sent — the invitation appears in the "
            "account when its owner signs in.",
        )
    if project.owner_id is not None and invitee.id == project.owner_id:
        raise CollaborationError(
            422,
            "cannot_invite_owner",
            "That account owns this project already.",
        )
    if invitee.id == inviter_id and project.owner_id != invitee.id:
        raise CollaborationError(
            422, "cannot_invite_self", "You cannot invite yourself."
        )
    row = membership_row(db, project.id, invitee.id)
    if row is not None and row.status == STATUS_INVITED:
        raise CollaborationError(
            409,
            "already_invited",
            "That account already has a pending invitation to this project.",
        )
    if row is not None and row.status == STATUS_ACCEPTED:
        raise CollaborationError(
            409,
            "already_member",
            "That account is already a member of this project.",
        )
    now = _now()
    if row is None:
        row = ProjectMember(
            id=str(uuid.uuid4()),
            project_id=project.id,
            user_id=invitee.id,
            role=role,
            status=STATUS_INVITED,
            invited_by_id=inviter_id,
            created_at=now,
        )
        db.add(row)
    else:
        # A declined or removed membership is revived by a fresh
        # invitation on the same row (the lifecycle history of the
        # earlier grant stays in the row's timestamps until the new
        # invitation overwrites them; the activity feed reports the
        # current lifecycle only).
        row.role = role
        row.status = STATUS_INVITED
        row.invited_by_id = inviter_id
        row.created_at = now
        row.responded_at = None
        row.removed_at = None
        row.removed_by_id = None
    db.commit()
    db.refresh(row)
    return row


def respond_to_invitation(db, project_id: str, principal, *, accept: bool):
    row = membership_row(db, project_id, principal.user_id)
    if row is None or row.status != STATUS_INVITED:
        raise CollaborationError(
            404,
            "invitation_not_found",
            "No pending invitation to this project for your account.",
        )
    row.status = STATUS_ACCEPTED if accept else STATUS_DECLINED
    row.responded_at = _now()
    db.commit()
    db.refresh(row)
    return row


def change_member_role(db, project, *, user_id: str, role: str):
    if role not in MEMBER_ROLES:
        raise CollaborationError(
            422, "invalid_role", "role must be one of: editor, viewer."
        )
    row = membership_row(db, project.id, user_id)
    if row is None or row.status != STATUS_ACCEPTED:
        raise CollaborationError(
            404, "member_not_found", "That account is not a member of this project."
        )
    row.role = role
    db.commit()
    db.refresh(row)
    return row


def remove_member(db, project, *, user_id: str, removed_by_id: str):
    row = membership_row(db, project.id, user_id)
    if row is None or row.status not in (STATUS_ACCEPTED, STATUS_INVITED):
        raise CollaborationError(
            404, "member_not_found", "That account is not a member of this project."
        )
    row.status = STATUS_REMOVED
    row.removed_at = _now()
    row.removed_by_id = removed_by_id
    db.commit()
    db.refresh(row)
    return row


def pending_invitations(db, user_id: str) -> list[dict]:
    """The account's pending invitations, with the project and
    inviter resolved. Fail-soft like the other membership reads."""
    from models import Project

    try:
        rows = (
            db.query(ProjectMember)
            .filter(
                ProjectMember.user_id == str(user_id),
                ProjectMember.status == STATUS_INVITED,
            )
            .order_by(ProjectMember.created_at.desc())
            .all()
        )
    except SQLAlchemyError:
        db.rollback()
        return []
    out = []
    for row in rows:
        project = db.get(Project, row.project_id)
        out.append(
            {
                "project_id": row.project_id,
                "project_title": project.title if project else None,
                "project_category": project.category if project else None,
                "role": row.role,
                "invited_by": _user_brief(db, row.invited_by_id),
                "invited_at": _iso(row.created_at),
            }
        )
    return out


def members_overview(db, project) -> dict:
    """Owner + accepted members + pending invitations for the
    project's Team surface. History rows (declined/removed) are
    not listed here — they live in the activity feed."""
    owner = _user_brief(db, project.owner_id)
    try:
        rows = (
            db.query(ProjectMember)
            .filter(ProjectMember.project_id == project.id)
            .order_by(ProjectMember.created_at.asc())
            .all()
        )
    except SQLAlchemyError:
        db.rollback()
        rows = []
    return {
        "owner": owner,
        "members": [
            member_view(db, r) for r in rows if r.status == STATUS_ACCEPTED
        ],
        "invitations": [
            member_view(db, r) for r in rows if r.status == STATUS_INVITED
        ],
    }


# ---------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------


def _validate_body(body: str) -> str:
    text = (body or "").strip()
    if not text:
        raise CollaborationError(
            422, "comment_empty", "A comment cannot be empty."
        )
    if len(text) > MAX_COMMENT_CHARS:
        raise CollaborationError(
            422,
            "comment_too_long",
            f"A comment is at most {MAX_COMMENT_CHARS} characters.",
        )
    return text


def create_comment(db, project, *, author_id: str, body: str, revision_id=None):
    text = _validate_body(body)
    if revision_id:
        # The anchor must be a real revision of THIS project.
        from services import editor

        try:
            editor.get_revision(project.id, str(revision_id))
        except Exception as error:
            raise CollaborationError(
                422,
                "revision_not_found",
                "That revision does not exist on this project.",
            ) from error
    row = ProjectComment(
        id=str(uuid.uuid4()),
        project_id=project.id,
        revision_id=str(revision_id) if revision_id else None,
        author_id=author_id,
        body=text,
        created_at=_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_comments(db, project_id: str) -> list[ProjectComment]:
    return (
        db.query(ProjectComment)
        .filter(ProjectComment.project_id == str(project_id))
        .order_by(ProjectComment.created_at.asc())
        .all()
    )


def _comment_or_404(db, project_id: str, comment_id: str) -> ProjectComment:
    row = db.get(ProjectComment, str(comment_id))
    if row is None or row.project_id != str(project_id):
        raise CollaborationError(
            404, "comment_not_found", "No such comment on this project."
        )
    return row


def edit_comment(db, project, *, comment_id: str, author_id: str, body: str):
    row = _comment_or_404(db, project.id, comment_id)
    if row.author_id != author_id:
        raise CollaborationError(
            403,
            "not_comment_author",
            "Only the comment's author can edit it.",
        )
    row.body = _validate_body(body)
    row.edited_at = _now()
    db.commit()
    db.refresh(row)
    return row


def delete_comment(db, project, *, comment_id: str, principal) -> None:
    row = _comment_or_404(db, project.id, comment_id)
    is_owner = project.owner_id == principal.user_id or (
        project.owner_id is None
        and principal.auth_method == "operator_token"
    )
    if row.author_id != principal.user_id and not is_owner:
        raise CollaborationError(
            403,
            "not_comment_author",
            "Only the comment's author or the project owner can delete it.",
        )
    db.delete(row)
    db.commit()


# ---------------------------------------------------------------------
# Activity — recorded events only
# ---------------------------------------------------------------------


def build_activity(db, project, project_dir) -> dict:
    """Assemble the project's activity feed from the records that
    exist: run ledger rows, editor revisions, plan decisions,
    membership lifecycle rows, comments. Each event names its
    source; nothing is inferred beyond what the record states."""
    events: list[dict] = []

    def add(at, kind: str, summary: str, **extra):
        if at is None:
            return
        event = {"at": _iso(at), "kind": kind, "summary": summary}
        event.update(extra)
        events.append(event)

    # Production runs (run ledger).
    try:
        from services import run_ledger

        for run in run_ledger.list_runs_for(project.id):
            add(
                run.get("created_at"),
                "run_submitted",
                f"Production run submitted (status: {run.get('status')}).",
                run_id=run.get("run_id"),
                status=run.get("status"),
            )
            if run.get("finished_at"):
                add(
                    run.get("finished_at"),
                    "run_finished",
                    f"Production run finished (status: {run.get('status')}).",
                    run_id=run.get("run_id"),
                    status=run.get("status"),
                )
    except Exception:
        pass

    # Editor revisions (revision chain).
    try:
        from services import editor

        for view in editor.list_revisions(project.id):
            add(
                view.get("created_at"),
                "revision_created",
                f"Editor revision {view.get('revision_number')} created "
                f"(status: {view.get('status')}).",
                revision_id=view.get("revision_id"),
                revision_number=view.get("revision_number"),
            )
    except Exception:
        pass

    # Plan decisions (plan_decisions.json).
    try:
        from services import plan_document

        for decision in plan_document.load_plan_decisions(project_dir):
            add(
                decision.get("recorded_at"),
                "plan_decision",
                f"Plan decision: {decision.get('action')} on sentence "
                f"{decision.get('sentence_number')}.",
                sentence_number=decision.get("sentence_number"),
                action=decision.get("action"),
            )
    except Exception:
        pass

    # Membership lifecycle (project_members rows).
    try:
        rows = (
            db.query(ProjectMember)
            .filter(ProjectMember.project_id == project.id)
            .all()
        )
    except SQLAlchemyError:
        db.rollback()
        rows = []
    for row in rows:
        member = _user_brief(db, row.user_id)
        label = (member or {}).get("email") or row.user_id
        add(
            row.created_at,
            "member_invited",
            f"{label} invited as {row.role}.",
            user=member,
            role=row.role,
        )
        if row.responded_at and row.status in (
            STATUS_ACCEPTED,
            STATUS_DECLINED,
        ):
            add(
                row.responded_at,
                "invitation_accepted" if row.status == STATUS_ACCEPTED
                else "invitation_declined",
                f"{label} {'accepted' if row.status == STATUS_ACCEPTED else 'declined'} the invitation.",
                user=member,
                role=row.role,
            )
        if row.removed_at:
            add(
                row.removed_at,
                "member_removed",
                f"{label}'s membership ended.",
                user=member,
                role=row.role,
            )

    # Comments (live rows only — a deleted comment leaves no event).
    for row in list_comments(db, project.id):
        author = _user_brief(db, row.author_id)
        label = (author or {}).get("email") or row.author_id
        add(
            row.created_at,
            "comment",
            f"{label} commented.",
            comment_id=row.id,
            author=author,
            revision_id=row.revision_id,
        )

    events.sort(key=lambda e: e["at"] or "", reverse=True)
    return {"events": events[:ACTIVITY_LIMIT]}
