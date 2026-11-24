"""Phase 6.2 — assistant core infrastructure.

Covers: migration 0009 (up/down), conversation CRUD + owner
isolation at the API, the orchestrator protocol battery (final
answers, malformed output, unknown tools, bad arguments, step
cap), the honest unavailable state, and the confirmation state
machine driven through two test-only registry tools (a READ and a
WRITE) so the machine is proven independently of product tools.
"""

import sqlite3
import uuid
from pathlib import Path

import fakeredis
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import BaseModel

import api_assistant
import api_v1
import main
from database import SessionLocal, engine
from models import (
    AssistantAction,
    AssistantConversation,
    AssistantMessage,
    Base,
    Plan,
    Project,
    User,
    UserSubscription,
)
from services import auth as auth_service
from services.assistant import actions as actions_svc
from services.assistant import llm as assistant_llm
from services.assistant import orchestrator, registry

BACKEND = Path(__file__).resolve().parent.parent
TOKEN = "test-token"
PASSWORD = "alpha-correct-horse-42"


# ---------------------------------------------------------------------
# Test-only tools (unique names; product assertions elsewhere are
# containment-based, so these never pollute them)
# ---------------------------------------------------------------------


class _EchoArgs(BaseModel):
    model_config = {"extra": "forbid"}
    text: str


def _echo_handler(principal, args):
    return {"echo": args.text, "for_user": principal.user_id}


def _write_handler(principal, args):
    return {"written": args.text, "for_user": principal.user_id}


if registry.get("test_echo_read") is None:
    registry.register(
        registry.ToolSpec(
            name="test_echo_read",
            family="test",
            action_class=registry.READ,
            label="Echo (test)",
            summary="Test-only read tool.",
            input_model=_EchoArgs,
            handler=_echo_handler,
        )
    )
if registry.get("test_pending_write") is None:
    registry.register(
        registry.ToolSpec(
            name="test_pending_write",
            family="test",
            action_class=registry.WRITE,
            label="Write echo (test)",
            summary="Test-only write tool.",
            input_model=_EchoArgs,
            handler=_write_handler,
        )
    )


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.delenv("CINEMORA_ALLOW_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("CINEMORA_ASSISTANT_ENABLED", raising=False)
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    api_assistant._message_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(
        fakeredis.FakeRedis(decode_responses=True)
    )
    db = SessionLocal()
    try:
        for model in (
            AssistantAction,
            AssistantMessage,
            AssistantConversation,
            Project,
            UserSubscription,
            User,
        ):
            db.query(model).delete()
        if db.query(Plan).filter_by(name="free").one_or_none() is None:
            db.add(
                Plan(
                    id=str(uuid.uuid4()),
                    name="free",
                    max_concurrent_runs=1,
                    max_runs_per_period=3,
                    period="month",
                    priority_tier=0,
                    is_default=True,
                )
            )
        db.commit()
    finally:
        db.close()
    yield
    auth_service.reset_redis_client()
    db = SessionLocal()
    try:
        for model in (
            AssistantAction,
            AssistantMessage,
            AssistantConversation,
            UserSubscription,
            User,
            Plan,
        ):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


def _register(client, email):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def users(client):
    suffix = uuid.uuid4().hex[:8]
    a = _register(client, f"core-a-{suffix}@example.com")
    b = _register(client, f"core-b-{suffix}@example.com")
    return a, b


def _stub_llm(monkeypatch, responses):
    """Drive the orchestrator deterministically: each entry is a
    dict the stub returns, or an exception instance to raise."""
    queue = list(responses)

    def _complete(system_prompt, user_payload):
        if not queue:
            raise AssertionError("stub LLM called more times than scripted")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(assistant_llm, "complete_json", _complete)
    # The orchestrator imported the module, not the function — the
    # attribute patch above is the seam either way.
    monkeypatch.setattr(orchestrator.llm, "complete_json", _complete)


def _conversation(client, user):
    response = client.post(
        "/api/v1/assistant/conversations",
        headers=_bearer(user["session_token"]),
        json={},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _post_message(client, user, conversation_id, content="Hello"):
    return client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/messages",
        headers=_bearer(user["session_token"]),
        json={"content": content},
    )


# ---------------------------------------------------------------------
# Migration 0009
# ---------------------------------------------------------------------


def test_migration_0009_creates_and_drops_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "mig.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    command.upgrade(cfg, "head")
    tables = {
        row[0]
        for row in sqlite3.connect(db_path)
        .execute("SELECT name FROM sqlite_master WHERE type='table'")
        .fetchall()
    }
    assert {
        "assistant_conversations",
        "assistant_messages",
        "assistant_actions",
        "assistant_tool_overrides",
        "assistant_security_events",
    } <= tables
    command.downgrade(cfg, "0008_admin_audit_log")
    tables = {
        row[0]
        for row in sqlite3.connect(db_path)
        .execute("SELECT name FROM sqlite_master WHERE type='table'")
        .fetchall()
    }
    assert "assistant_conversations" not in tables
    assert "admin_audit_log" in tables


# ---------------------------------------------------------------------
# Auth + conversation CRUD + isolation
# ---------------------------------------------------------------------


def test_assistant_routes_reject_anonymous(client):
    response = client.get("/api/v1/assistant/conversations")
    assert response.status_code == 401


def test_conversation_crud_and_isolation(client, users):
    a, b = users
    conversation = _conversation(client, a)
    assert conversation["project_id"] is None

    mine = client.get(
        "/api/v1/assistant/conversations",
        headers=_bearer(a["session_token"]),
    )
    assert [c["id"] for c in mine.json()["conversations"]] == [
        conversation["id"]
    ]
    theirs = client.get(
        "/api/v1/assistant/conversations",
        headers=_bearer(b["session_token"]),
    )
    assert theirs.json()["conversations"] == []

    foreign = client.get(
        f"/api/v1/assistant/conversations/{conversation['id']}",
        headers=_bearer(b["session_token"]),
    )
    assert foreign.status_code == 404

    missing = client.get(
        f"/api/v1/assistant/conversations/{uuid.uuid4()}",
        headers=_bearer(a["session_token"]),
    )
    assert missing.status_code == 404

    deleted = client.delete(
        f"/api/v1/assistant/conversations/{conversation['id']}",
        headers=_bearer(a["session_token"]),
    )
    assert deleted.status_code == 200
    gone = client.get(
        f"/api/v1/assistant/conversations/{conversation['id']}",
        headers=_bearer(a["session_token"]),
    )
    assert gone.status_code == 404


def test_conversation_project_binding_is_owner_checked(client, users):
    a, b = users
    project = client.post(
        "/api/v1/projects",
        headers=_bearer(a["session_token"]),
        json={"script": "Owner A script. One sentence. Another one."},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]

    foreign = client.post(
        "/api/v1/assistant/conversations",
        headers=_bearer(b["session_token"]),
        json={"project_id": project_id},
    )
    assert foreign.status_code == 404

    own = client.post(
        "/api/v1/assistant/conversations",
        headers=_bearer(a["session_token"]),
        json={"project_id": project_id},
    )
    assert own.status_code == 201
    assert own.json()["project_id"] == project_id


# ---------------------------------------------------------------------
# Orchestrator protocol battery
# ---------------------------------------------------------------------


def test_turn_final_answer(client, users, monkeypatch):
    a, _b = users
    conversation = _conversation(client, a)
    _stub_llm(monkeypatch, [{"final": "Hello from the stub."}])
    response = _post_message(client, a, conversation["id"], "Hi")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "ok"
    assert body["message"]["content"] == "Hello from the stub."
    assert body["pending_action"] is None

    detail = client.get(
        f"/api/v1/assistant/conversations/{conversation['id']}",
        headers=_bearer(a["session_token"]),
    )
    roles = [m["role"] for m in detail.json()["messages"]]
    assert roles == ["user", "assistant"]
    assert detail.json()["conversation"]["title"] == "Hi"


def test_turn_read_tool_executes_and_audits(client, users, monkeypatch):
    a, _b = users
    conversation = _conversation(client, a)
    _stub_llm(
        monkeypatch,
        [
            {"tool": "test_echo_read", "arguments": {"text": "ping"}},
            {"final": "The echo came back."},
        ],
    )
    response = _post_message(client, a, conversation["id"], "Echo ping")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "ok"
    assert body["tool_results"]["test_echo_read"]["echo"] == "ping"
    trace = body["message"]["tool_trace"]["tools"]
    assert trace[0]["tool"] == "test_echo_read"
    assert trace[0]["status"] == "executed"

    activity = client.get(
        "/api/v1/assistant/actions",
        headers=_bearer(a["session_token"]),
    )
    rows = activity.json()["actions"]
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "test_echo_read"
    assert rows[0]["status"] == "executed"
    assert rows[0]["action_class"] == "read"


def test_turn_unavailable_is_honest(client, users, monkeypatch):
    a, _b = users
    conversation = _conversation(client, a)
    _stub_llm(
        monkeypatch,
        [
            assistant_llm.AssistantUnavailable(
                "llm_not_configured", "The model is not configured."
            )
        ],
    )
    response = _post_message(client, a, conversation["id"], "Hi")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "unavailable"
    assert "not configured" in body["message"]["content"]


def test_turn_protocol_battery(client, users, monkeypatch):
    a, _b = users
    conversation = _conversation(client, a)

    # Unknown tool, corrected once, then a final answer.
    _stub_llm(
        monkeypatch,
        [
            {"tool": "no_such_tool", "arguments": {}},
            {"final": "Recovered."},
        ],
    )
    response = _post_message(client, a, conversation["id"], "Try")
    assert response.json()["state"] == "ok"
    assert response.json()["message"]["content"] == "Recovered."

    # Bad argument types, corrected once, then final.
    _stub_llm(
        monkeypatch,
        [
            {"tool": "test_echo_read", "arguments": {"text": 42}},
            {"final": "Recovered again."},
        ],
    )
    response = _post_message(client, a, conversation["id"], "Try 2")
    assert response.json()["state"] == "ok"

    # Malformed protocol output beyond the correction budget.
    _stub_llm(
        monkeypatch,
        [
            assistant_llm.ProtocolError("not json"),
            assistant_llm.ProtocolError("not json"),
            assistant_llm.ProtocolError("not json"),
        ],
    )
    response = _post_message(client, a, conversation["id"], "Try 3")
    assert response.json()["state"] == "error"

    # Over-step loop: the stub always proposes the same READ; the
    # step cap must end the turn honestly.
    _stub_llm(
        monkeypatch,
        [
            {"tool": "test_echo_read", "arguments": {"text": "x"}}
            for _ in range(30)
        ],
    )
    monkeypatch.setenv("CINEMORA_ASSISTANT_MAX_STEPS", "3")
    response = _post_message(client, a, conversation["id"], "Loop")
    assert response.json()["state"] == "error"
    assert "step limit" in response.json()["message"]["content"]


# ---------------------------------------------------------------------
# Confirmation state machine (through the test WRITE tool)
# ---------------------------------------------------------------------


def test_write_proposal_confirm_reject_expire(client, users, monkeypatch):
    a, b = users
    conversation = _conversation(client, a)

    # Proposal: nothing executes in the turn.
    _stub_llm(
        monkeypatch,
        [
            {
                "tool": "test_pending_write",
                "arguments": {"text": "draft"},
                "say": "I prepared a write.",
            }
        ],
    )
    response = _post_message(client, a, conversation["id"], "Write draft")
    body = response.json()
    assert body["state"] == "ok"
    pending = body["pending_action"]
    assert pending["status"] == "pending"
    assert pending["tool_name"] == "test_pending_write"
    assert "Nothing has been executed yet" in body["message"]["content"]

    # Cross-owner confirm is a 404 and changes nothing.
    foreign = client.post(
        f"/api/v1/assistant/actions/{pending['id']}/confirm",
        headers=_bearer(b["session_token"]),
    )
    assert foreign.status_code == 404

    # Confirm executes exactly once; a second confirm returns the
    # recorded result without re-executing.
    confirmed = client.post(
        f"/api/v1/assistant/actions/{pending['id']}/confirm",
        headers=_bearer(a["session_token"]),
    )
    assert confirmed.status_code == 200, confirmed.text
    action = confirmed.json()["action"]
    assert action["status"] == "executed"
    assert action["result"]["written"] == "draft"
    again = client.post(
        f"/api/v1/assistant/actions/{pending['id']}/confirm",
        headers=_bearer(a["session_token"]),
    )
    assert again.json()["action"]["status"] == "executed"
    assert again.json()["action"]["result"]["written"] == "draft"

    # Reject path: a fresh proposal rejected stays unexecuted.
    _stub_llm(
        monkeypatch,
        [{"tool": "test_pending_write", "arguments": {"text": "nope"}}],
    )
    response = _post_message(client, a, conversation["id"], "Write nope")
    pending2 = response.json()["pending_action"]
    rejected = client.post(
        f"/api/v1/assistant/actions/{pending2['id']}/reject",
        headers=_bearer(a["session_token"]),
    )
    assert rejected.json()["action"]["status"] == "rejected"
    confirm_after_reject = client.post(
        f"/api/v1/assistant/actions/{pending2['id']}/confirm",
        headers=_bearer(a["session_token"]),
    )
    assert confirm_after_reject.status_code == 409

    # Expiry: a stale pending action cannot be confirmed.
    _stub_llm(
        monkeypatch,
        [{"tool": "test_pending_write", "arguments": {"text": "old"}}],
    )
    response = _post_message(client, a, conversation["id"], "Write old")
    pending3 = response.json()["pending_action"]
    db = SessionLocal()
    try:
        row = db.get(AssistantAction, pending3["id"])
        from datetime import datetime, timedelta, timezone

        row.created_at = datetime.now(timezone.utc) - timedelta(
            seconds=actions_svc.ttl_seconds() + 60
        )
        db.commit()
    finally:
        db.close()
    expired = client.post(
        f"/api/v1/assistant/actions/{pending3['id']}/confirm",
        headers=_bearer(a["session_token"]),
    )
    assert expired.status_code == 409
    assert expired.json()["error"]["code"] == "action_expired"


def test_kill_switch_refuses(client, users, monkeypatch):
    a, _b = users
    monkeypatch.setenv("CINEMORA_ASSISTANT_ENABLED", "false")
    response = client.get(
        "/api/v1/assistant/conversations",
        headers=_bearer(a["session_token"]),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "assistant_disabled"
