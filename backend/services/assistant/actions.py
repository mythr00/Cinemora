"""Pending actions, the confirmation state machine, and the
tool-execution audit log (design assistant/04 §2.7, 05 §1.4,
08 §3).

One table (`assistant_actions`) serves both purposes because a
pending action and its audit record are the same object's
lifecycle:

    READ execution      -> row lands as executed | failed
    WRITE/CONSEQUENTIAL -> pending -> confirmed -> executed | failed
                        -> pending -> rejected
                        -> pending -> expired (TTL, enforced lazily
                           at read/confirm time from created_at)

Execution re-validates ownership and arguments at execution time —
never only at proposal time — and calls the same handler the tool
registry declares, which for write tools is the same service path
the v1 API routes use. A second confirm of an executed action
returns the recorded result without re-executing (idempotent-safe).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from services.assistant import registry
from services.assistant.redact import redact_value

PENDING = "pending"
CONFIRMED = "confirmed"
REJECTED = "rejected"
EXPIRED = "expired"
EXECUTED = "executed"
FAILED = "failed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_safe(value):
    """Normalise a tool result for the JSON audit columns. Tool
    handlers return the same dicts the v1 API returns, which can
    carry datetimes FastAPI would encode at response time; the
    audit store needs the encoded form."""
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {"unserialisable_result": str(value)[:500]}


def ttl_seconds() -> int:
    try:
        return max(
            30, int(os.environ.get("CINEMORA_ASSISTANT_CONFIRM_TTL_SECONDS", "900"))
        )
    except ValueError:
        return 900


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def is_expired(action) -> bool:
    created = _as_aware(action.created_at)
    if created is None:
        return False
    return _now() > created + timedelta(seconds=ttl_seconds())


def action_view(action) -> dict:
    spec = registry.get(action.tool_name)
    return {
        "id": action.id,
        "conversation_id": action.conversation_id,
        "message_id": action.message_id,
        "tool_name": action.tool_name,
        "action_class": action.action_class,
        "label": spec.label if spec else action.tool_name,
        "arguments": action.arguments or {},
        "status": effective_status(action),
        "result": action.result,
        "created_at": action.created_at.isoformat()
        if action.created_at
        else None,
        "decided_at": action.decided_at.isoformat()
        if action.decided_at
        else None,
        "executed_at": action.executed_at.isoformat()
        if action.executed_at
        else None,
        "expires_at": (
            (_as_aware(action.created_at) + timedelta(seconds=ttl_seconds()))
            .isoformat()
            if action.created_at
            else None
        ),
    }


def effective_status(action) -> str:
    if action.status == PENDING and is_expired(action):
        return EXPIRED
    return action.status


def _new_row(db, *, owner_id, conversation_id, spec, arguments, status):
    from models import AssistantAction

    row = AssistantAction(
        id=str(uuid.uuid4()),
        conversation_id=conversation_id,
        owner_id=owner_id,
        tool_name=spec.name,
        action_class=spec.action_class,
        arguments=redact_value(arguments),
        status=status,
        created_at=_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def record_read(
    db,
    *,
    owner_id,
    conversation_id,
    spec,
    arguments: dict,
    result: dict | None,
    error: str | None,
):
    """Audit row for an executed READ tool call (success or
    failure). Reads are logged with the same rigour as writes —
    the audit accounts for 100% of tool executions."""
    row = _new_row(
        db,
        owner_id=owner_id,
        conversation_id=conversation_id,
        spec=spec,
        arguments=arguments,
        status=EXECUTED if error is None else FAILED,
    )
    row.result = (
        _json_safe(redact_value(result))
        if result is not None
        else {"error": error}
    )
    row.executed_at = _now()
    db.commit()
    db.refresh(row)
    return row


def propose(
    db,
    *,
    owner_id,
    conversation_id,
    spec,
    arguments: dict,
    extra_result: dict | None = None,
):
    """Create the pending action for a WRITE/CONSEQUENTIAL tool.
    Nothing has executed — that is the whole point of the row."""
    row = _new_row(
        db,
        owner_id=owner_id,
        conversation_id=conversation_id,
        spec=spec,
        arguments=arguments,
        status=PENDING,
    )
    if extra_result:
        # e.g. retry eligibility computed at proposal time, shown
        # on the confirmation card.
        row.result = redact_value(extra_result)
        db.commit()
        db.refresh(row)
    return row


def get_owned_action_or_404(db, action_id: str, owner_id: str):
    """Owner-scoped action load: another user's action is a 404,
    exactly like projects and conversations."""
    from api_v1 import ApiError
    from models import AssistantAction

    row = db.get(AssistantAction, str(action_id))
    if row is None or row.owner_id != owner_id:
        raise ApiError(404, "action_not_found", "No such assistant action.")
    return row


def reject(db, action, *, owner_id: str):
    """Reject a pending action. A rejected action can never
    execute; rejecting a settled action is a 409, not a no-op."""
    from api_v1 import ApiError

    if action.owner_id != owner_id:
        raise ApiError(404, "action_not_found", "No such assistant action.")
    status = effective_status(action)
    if status == EXPIRED:
        action.status = EXPIRED
        db.commit()
        raise ApiError(
            409, "action_expired", "This action expired before it was decided."
        )
    if status != PENDING:
        raise ApiError(
            409,
            "action_already_decided",
            f"This action is already {status} and cannot be rejected.",
        )
    action.status = REJECTED
    action.decided_at = _now()
    db.commit()
    db.refresh(action)
    return action


def confirm_and_execute(db, action, principal):
    """The confirm endpoint's executor (`execute_approved_operation`
    in the registry doc — internal, never model-callable).

    Re-validates at execution time: ownership, expiry, the tool
    still being registered and enabled, and the arguments against
    the tool's pydantic schema. Then calls the handler — the same
    code path the v1 API uses — and records the backend's real
    result, success or failure."""
    from api_v1 import ApiError

    if action.owner_id != principal.user_id:
        raise ApiError(404, "action_not_found", "No such assistant action.")
    status = effective_status(action)
    if status == EXECUTED:
        # Idempotent re-confirm: return the recorded outcome, never
        # execute twice.
        return action
    if status == EXPIRED:
        action.status = EXPIRED
        db.commit()
        raise ApiError(
            409,
            "action_expired",
            "This action expired before it was confirmed; ask the "
            "assistant to prepare it again.",
        )
    if status != PENDING:
        raise ApiError(
            409,
            "action_already_decided",
            f"This action is already {status} and cannot be confirmed.",
        )
    spec = registry.get(action.tool_name)
    if spec is None:
        action.status = FAILED
        action.result = {"error": f"Tool {action.tool_name} is no longer registered."}
        action.decided_at = _now()
        db.commit()
        raise ApiError(
            409, "tool_unavailable", "That operation is no longer available."
        )
    if registry.tool_disabled(db, spec.name):
        action.status = FAILED
        action.result = {"error": f"Tool {spec.name} is disabled by an administrator."}
        action.decided_at = _now()
        db.commit()
        raise ApiError(
            409, "tool_disabled", "That operation has been disabled by an administrator."
        )
    try:
        args = spec.input_model.model_validate(action.arguments or {})
    except ValidationError as error:
        action.status = FAILED
        action.result = {"error": f"Stored arguments no longer validate: {error}"}
        action.decided_at = _now()
        db.commit()
        raise ApiError(
            409,
            "arguments_invalid",
            "The stored arguments no longer validate; nothing was executed.",
        ) from error

    action.status = CONFIRMED
    action.decided_at = _now()
    db.commit()
    try:
        result = spec.handler(principal, args)
    except ApiError as error:
        action.status = FAILED
        action.result = redact_value(
            {"error": error.message, "code": error.code}
        )
        action.executed_at = _now()
        db.commit()
        db.refresh(action)
        return action
    except Exception as error:  # the backend's failure, recorded honestly
        action.status = FAILED
        action.result = redact_value(
            {"error": f"{type(error).__name__}: {error}"}
        )
        action.executed_at = _now()
        db.commit()
        db.refresh(action)
        return action
    action.status = EXECUTED
    action.result = _json_safe(
        redact_value(result if isinstance(result, dict) else {"result": result})
    )
    action.executed_at = _now()
    db.commit()
    db.refresh(action)
    return action
