"""AI Product Assistant API (Phase 6; design assistant/05 §2).

Mounted from main.py under /api/v1 beside api_v1 and api_admin.
Auth is the platform's own: api_v1.require_user / post_guard —
a session (cookie or Bearer) or, during the dual-auth window, the
operator token resolving to the founder principal. Every route is
owner-scoped by the principal's user id; another user's
conversation or action is a 404, never a 403 (the Phase 8 rule).

There is deliberately NO endpoint that executes a WRITE directly
from a chat message. Confirmation is a separate, authenticated,
explicit call — that is what makes an unconfirmed destructive
operation structurally impossible rather than prompt-dependent.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

import api_v1
from api_v1 import ApiError, post_guard, require_user
from database import SessionLocal
from models import (
    AssistantAction,
    AssistantConversation,
    AssistantMessage,
)
from services import auth as auth_service
from services.assistant import actions as actions_svc
from services.assistant import orchestrator, registry

# Importing the tools package populates the registry.
from services.assistant import tools as _tools  # noqa: F401

router = APIRouter()

MAX_MESSAGE_CHARS = 8_000

_message_limiter = api_v1._RateLimiter()


def _assistant_message_limit() -> int:
    try:
        return max(
            1,
            int(os.environ.get("CINEMORA_ASSISTANT_MESSAGE_RATE_LIMIT", "10")),
        )
    except ValueError:
        return 10


def _kill_switch():
    """The env kill switch (design 05 §3): when off, the whole
    assistant surface refuses — fail-closed, like the API itself."""
    if not registry.env_enabled():
        raise ApiError(
            503,
            "assistant_disabled",
            "The assistant is disabled on this deployment "
            "(CINEMORA_ASSISTANT_ENABLED).",
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------


class ConversationCreateIn(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=300)


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


# ---------------------------------------------------------------------
# Views + owner-scoped loads
# ---------------------------------------------------------------------


def _conversation_view(conversation) -> dict:
    return {
        "id": conversation.id,
        "title": conversation.title,
        "project_id": conversation.project_id,
        "created_at": conversation.created_at.isoformat()
        if conversation.created_at
        else None,
        "updated_at": conversation.updated_at.isoformat()
        if conversation.updated_at
        else None,
    }


def _message_view(message) -> dict:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "role": message.role,
        "content": message.content,
        "tool_trace": message.tool_trace,
        "created_at": message.created_at.isoformat()
        if message.created_at
        else None,
    }


def _get_owned_conversation_or_404(db, conversation_id, principal):
    conversation = db.get(AssistantConversation, str(conversation_id))
    if conversation is None or conversation.owner_id != principal.user_id:
        raise ApiError(
            404, "conversation_not_found", "No such assistant conversation."
        )
    return conversation


# ---------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------


@router.post("/assistant/conversations", status_code=201)
def create_conversation(
    body: ConversationCreateIn, principal=Depends(post_guard)
):
    _kill_switch()
    db = SessionLocal()
    try:
        if body.project_id:
            # Binding is owner-checked at creation (design 05 §2):
            # a foreign project id is the same 404 as a missing one.
            api_v1._get_owned_project_or_404(db, body.project_id, principal)
        conversation = AssistantConversation(
            id=str(uuid.uuid4()),
            owner_id=principal.user_id,
            title=body.title,
            project_id=body.project_id,
            created_at=_now(),
            updated_at=_now(),
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return _conversation_view(conversation)
    finally:
        db.close()


@router.get("/assistant/conversations")
def list_conversations(principal=Depends(require_user)):
    _kill_switch()
    db = SessionLocal()
    try:
        rows = (
            db.query(AssistantConversation)
            .filter(AssistantConversation.owner_id == principal.user_id)
            .order_by(AssistantConversation.updated_at.desc())
            .all()
        )
        return {"conversations": [_conversation_view(row) for row in rows]}
    finally:
        db.close()


@router.get("/assistant/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str, principal=Depends(require_user)
):
    _kill_switch()
    db = SessionLocal()
    try:
        conversation = _get_owned_conversation_or_404(
            db, conversation_id, principal
        )
        messages = (
            db.query(AssistantMessage)
            .filter(AssistantMessage.conversation_id == conversation.id)
            .order_by(AssistantMessage.created_at.asc())
            .all()
        )
        pending = (
            db.query(AssistantAction)
            .filter(
                AssistantAction.conversation_id == conversation.id,
                AssistantAction.status == actions_svc.PENDING,
            )
            .order_by(AssistantAction.created_at.desc())
            .all()
        )
        return {
            "conversation": _conversation_view(conversation),
            "messages": [_message_view(message) for message in messages],
            "pending_actions": [
                actions_svc.action_view(action) for action in pending
            ],
        }
    finally:
        db.close()


@router.delete("/assistant/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str, principal=Depends(post_guard)
):
    """Owner hard-delete of a conversation and its records
    (design 08 §7: the data is the owner's own)."""
    _kill_switch()
    db = SessionLocal()
    try:
        conversation = _get_owned_conversation_or_404(
            db, conversation_id, principal
        )
        db.query(AssistantAction).filter(
            AssistantAction.conversation_id == conversation.id
        ).delete()
        db.query(AssistantMessage).filter(
            AssistantMessage.conversation_id == conversation.id
        ).delete()
        db.delete(conversation)
        db.commit()
        return {"deleted": True, "id": conversation_id}
    finally:
        db.close()


# ---------------------------------------------------------------------
# Messages (the orchestrator turn)
# ---------------------------------------------------------------------


@router.post("/assistant/conversations/{conversation_id}/messages")
def post_message(
    conversation_id: str,
    body: MessageIn,
    principal=Depends(post_guard),
):
    _kill_switch()
    # Assistant-specific cap on top of the shared write budget, so
    # a chat loop cannot starve production POSTs (design 08 §5).
    _message_limiter.check(
        f"assistant:{principal.user_id}", limit=_assistant_message_limit()
    )
    db = SessionLocal()
    try:
        conversation = _get_owned_conversation_or_404(
            db, conversation_id, principal
        )
        user_message = AssistantMessage(
            id=str(uuid.uuid4()),
            conversation_id=conversation.id,
            role="user",
            content=body.content,
            created_at=_now(),
        )
        db.add(user_message)
        if not conversation.title:
            conversation.title = body.content.strip().splitlines()[0][:80]
        conversation.updated_at = _now()
        db.commit()

        history = [
            {"role": message.role, "content": message.content}
            for message in db.query(AssistantMessage)
            .filter(AssistantMessage.conversation_id == conversation.id)
            .order_by(AssistantMessage.created_at.asc())
            .all()
        ]

        if not registry.assistant_enabled(db):
            outcome = orchestrator.TurnOutcome(
                state="unavailable",
                text=(
                    "The assistant has been disabled by an "
                    "administrator. Your conversation is saved; "
                    "nothing was executed."
                ),
                unavailable_reason="disabled_by_admin",
            )
        else:
            outcome = orchestrator.run_turn(
                db, principal, conversation, history, body.content
            )

        assistant_message = AssistantMessage(
            id=str(uuid.uuid4()),
            conversation_id=conversation.id,
            role="assistant",
            content=outcome.text,
            tool_trace={
                "state": outcome.state,
                "tools": outcome.trace,
                "unavailable_reason": outcome.unavailable_reason,
            },
            created_at=_now(),
        )
        db.add(assistant_message)
        db.commit()
        db.refresh(assistant_message)
        if outcome.pending_action is not None:
            outcome.pending_action.message_id = assistant_message.id
            db.commit()

        return {
            "state": outcome.state,
            "message": _message_view(assistant_message),
            "pending_action": (
                actions_svc.action_view(outcome.pending_action)
                if outcome.pending_action is not None
                else None
            ),
            "tool_results": outcome.tool_results,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------
# Actions: confirm / reject / activity
# ---------------------------------------------------------------------


@router.post("/assistant/actions/{action_id}/confirm")
def confirm_action(action_id: str, principal=Depends(post_guard)):
    _kill_switch()
    db = SessionLocal()
    try:
        action = actions_svc.get_owned_action_or_404(
            db, action_id, principal.user_id
        )
        action = actions_svc.confirm_and_execute(db, action, principal)
        return {"action": actions_svc.action_view(action)}
    finally:
        db.close()


@router.post("/assistant/actions/{action_id}/reject")
def reject_action(action_id: str, principal=Depends(post_guard)):
    _kill_switch()
    db = SessionLocal()
    try:
        action = actions_svc.get_owned_action_or_404(
            db, action_id, principal.user_id
        )
        action = actions_svc.reject(db, action, owner_id=principal.user_id)
        return {"action": actions_svc.action_view(action)}
    finally:
        db.close()


@router.get("/assistant/actions")
def list_actions(
    conversation_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    principal=Depends(require_user),
):
    """The caller's own assistant activity/audit list."""
    _kill_switch()
    db = SessionLocal()
    try:
        query = db.query(AssistantAction).filter(
            AssistantAction.owner_id == principal.user_id
        )
        if conversation_id:
            query = query.filter(
                AssistantAction.conversation_id == conversation_id
            )
        if status:
            query = query.filter(AssistantAction.status == status)
        rows = (
            query.order_by(AssistantAction.created_at.desc())
            .limit(min(max(1, limit), 500))
            .all()
        )
        return {"actions": [actions_svc.action_view(row) for row in rows]}
    finally:
        db.close()


@router.get("/assistant/tools")
def list_tools(principal=Depends(require_user)):
    """The live registry as this caller may use it (classes +
    enablement) — the same data the admin M9 view shows."""
    _kill_switch()
    db = SessionLocal()
    try:
        return {
            "assistant_enabled": registry.assistant_enabled(db),
            "tools": registry.tool_states(db),
        }
    finally:
        db.close()
