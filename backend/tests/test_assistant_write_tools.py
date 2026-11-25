"""Phase 6.5 — write operations and confirmations (acceptance
scenarios 8, 9, 13 from design 10).

The RQ hop inside jobs.enqueue_full_pipeline is replaced with a
fake that performs the real ledger submission (the function's own
contract) — everything else is real: the v1 route functions the
tools delegate to, plan admission, the ledger, the confirmation
state machine.
"""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_assistant
import api_v1
from auth_helpers import with_session_token
import jobs
import main
from database import SessionLocal, engine
from models import (
    AssistantAction,
    AssistantConversation,
    AssistantMessage,
    Base,
    Plan,
    ProductionRun,
    Project,
    User,
    UserSubscription,
)
from services import auth as auth_service
from services import media_paths, run_ledger
from services.assistant import llm as assistant_llm
from services.assistant import orchestrator

TOKEN = "test-token"
PASSWORD = "alpha-correct-horse-42"
SCRIPT = "A short script. Two sentences only. Nothing more."


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")  # E-21: window is closed by default; tests opt in explicitly
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    api_assistant._message_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(
        fakeredis.FakeRedis(decode_responses=True)
    )

    def _fake_enqueue(project_id, script, options=None, run_id=None):
        if run_id is not None:
            return "job-released"
        merged = {"entrypoint": "enqueue_full_pipeline"}
        if isinstance(options, dict):
            merged.update(options)
        submission = run_ledger.submit_production(
            project_id, script, options=merged
        )
        return submission["run_id"] if submission else "job-unrecorded"

    monkeypatch.setattr(jobs, "enqueue_full_pipeline", _fake_enqueue)
    db = SessionLocal()
    try:
        for model in (
            AssistantAction,
            AssistantMessage,
            AssistantConversation,
            ProductionRun,
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
                    max_concurrent_runs=2,
                    max_runs_per_period=20,
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
            ProductionRun,
            Project,
            UserSubscription,
            User,
            Plan,
        ):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def user(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"write-{uuid.uuid4().hex[:8]}@example.com",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 201, response.text
    return with_session_token(response)


def _create_project(client, user, title="Write Test Film"):
    response = client.post(
        "/api/v1/projects",
        headers=_bearer(user["session_token"]),
        json={"title": title, "script": SCRIPT},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _seed_run(owner_id, project_id, *, status, stage=None, error=None):
    run_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(
            ProductionRun(
                id=run_id,
                project_id=project_id,
                owner_id=owner_id,
                status=status,
                stage=stage,
                error=error,
                script_hash=run_ledger.script_sha256(SCRIPT),
                options={"entrypoint": "enqueue_full_pipeline"},
                created_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            )
        )
        db.commit()
    finally:
        db.close()
    return run_id


def _stub_llm(monkeypatch, responses):
    queue = list(responses)

    def _complete(system_prompt, user_payload):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(assistant_llm, "complete_json", _complete)
    monkeypatch.setattr(orchestrator.llm, "complete_json", _complete)


def _conversation(client, user):
    response = client.post(
        "/api/v1/assistant/conversations",
        headers=_bearer(user["session_token"]),
        json={},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _propose(client, user, conversation_id, text="Do it"):
    response = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/messages",
        headers=_bearer(user["session_token"]),
        json={"content": text},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _confirm(client, user, action_id):
    return client.post(
        f"/api/v1/assistant/actions/{action_id}/confirm",
        headers=_bearer(user["session_token"]),
    )


def _promote_to_admin(user):
    """FS-C follow-up: plan approve/generate/decisions endpoints are
    admin-only, and the assistant write tools delegate to them — so
    the acting user in the plan-approval tests must be an admin (the
    FS-B/C precedent in test_fs_access_rules: a DB-created account
    with an explicit account role). The session token stays valid;
    the role is read from the account on each request."""
    db = SessionLocal()
    try:
        row = db.query(User).filter_by(id=user["user"]["id"]).one()
        row.role = "admin"
        db.commit()
    finally:
        db.close()


def _run_count(project_id):
    db = SessionLocal()
    try:
        return (
            db.query(ProductionRun)
            .filter(ProductionRun.project_id == project_id)
            .count()
        )
    finally:
        db.close()


# ---------------------------------------------------------------------
# Confirmed create + submit produce real, UI-indistinguishable records
# ---------------------------------------------------------------------


def test_confirmed_create_project(client, user, monkeypatch):
    conversation = _conversation(client, user)
    _stub_llm(
        monkeypatch,
        [
            {
                "tool": "create_project",
                "arguments": {
                    "title": "Assistant Made This",
                    "script": SCRIPT,
                    "category": "Documentary",
                },
            }
        ],
    )
    body = _propose(client, user, conversation["id"], "Create my project")
    pending = body["pending_action"]
    assert pending["action_class"] == "write"
    # Nothing exists yet.
    listed = client.get(
        "/api/v1/projects", headers=_bearer(user["session_token"])
    )
    assert listed.json()["projects"] == []

    confirmed = _confirm(client, user, pending["id"])
    assert confirmed.status_code == 200, confirmed.text
    action = confirmed.json()["action"]
    assert action["status"] == "executed"
    created = action["result"]
    assert created["title"] == "Assistant Made This"
    listed = client.get(
        "/api/v1/projects", headers=_bearer(user["session_token"])
    )
    assert [p["id"] for p in listed.json()["projects"]] == [created["id"]]


def test_confirmed_submit_run(client, user, monkeypatch):
    project = _create_project(client, user)
    conversation = _conversation(client, user)
    _stub_llm(
        monkeypatch,
        [
            {
                "tool": "submit_production_run",
                "arguments": {"project_id": project["id"]},
            }
        ],
    )
    body = _propose(client, user, conversation["id"], "Produce it")
    assert _run_count(project["id"]) == 0
    confirmed = _confirm(client, user, body["pending_action"]["id"])
    action = confirmed.json()["action"]
    assert action["status"] == "executed"
    assert action["result"]["run"]["status"] == "queued"
    assert _run_count(project["id"]) == 1


def test_confirmed_plan_decision_and_preference(client, user, monkeypatch):
    _promote_to_admin(user)  # FS-C: plan decisions are admin-only
    project = _create_project(client, user)
    conversation = _conversation(client, user)
    _stub_llm(
        monkeypatch,
        [
            {
                "tool": "post_plan_decision",
                "arguments": {
                    "project_id": project["id"],
                    "sentence_number": 1,
                    "action": "approve",
                },
            }
        ],
    )
    body = _propose(client, user, conversation["id"], "Approve sentence 1")
    confirmed = _confirm(client, user, body["pending_action"]["id"])
    assert confirmed.json()["action"]["status"] == "executed"
    decisions_file = (
        media_paths.project_dir(project["id"]) / "plan_decisions.json"
    )
    records = json.loads(decisions_file.read_text(encoding="utf-8"))
    assert records["decisions"][0]["sentence_number"] == 1
    assert records["decisions"][0]["action"] == "approve"

    _stub_llm(
        monkeypatch,
        [
            {
                "tool": "save_preference",
                "arguments": {"key": "default_genre", "value": "true crime"},
            }
        ],
    )
    body = _propose(client, user, conversation["id"], "Remember my genre")
    confirmed = _confirm(client, user, body["pending_action"]["id"])
    assert confirmed.json()["action"]["status"] == "executed"
    assert confirmed.json()["action"]["result"]["preferences"] == {
        "default_genre": "true crime"
    }


# ---------------------------------------------------------------------
# Scenario 9 — retry: eligibility, pending, confirm, real new run
# ---------------------------------------------------------------------


def test_scenario_9_retry_failed_production(client, user, monkeypatch):
    project = _create_project(client, user)
    failed_run = _seed_run(
        user["user"]["id"],
        project["id"],
        status="failed",
        stage="asset_manager",
        error="Coverage gate failed.",
    )
    conversation = _conversation(client, user)
    _stub_llm(
        monkeypatch,
        [
            {
                "tool": "retry_eligible_stage",
                "arguments": {"run_id": failed_run},
            }
        ],
    )
    body = _propose(client, user, conversation["id"], "Retry that production")
    pending = body["pending_action"]
    assert pending["tool_name"] == "retry_eligible_stage"
    eligibility = pending["result"]["eligibility"]
    assert eligibility["eligible"] is True
    assert eligibility["script_unchanged"] is True
    # Nothing submitted before confirmation.
    assert _run_count(project["id"]) == 1

    confirmed = _confirm(client, user, pending["id"])
    action = confirmed.json()["action"]
    assert action["status"] == "executed"
    assert action["result"]["retry_of_run_id"] == failed_run
    assert action["result"]["run"]["status"] == "queued"
    assert action["result"]["run"]["run_id"] != failed_run
    assert _run_count(project["id"]) == 2


def test_retry_ineligible_run_fails_at_confirm(client, user, monkeypatch):
    project = _create_project(client, user)
    running_run = _seed_run(
        user["user"]["id"], project["id"], status="running", stage="asset_manager"
    )
    conversation = _conversation(client, user)
    _stub_llm(
        monkeypatch,
        [
            {
                "tool": "retry_eligible_stage",
                "arguments": {"run_id": running_run},
            }
        ],
    )
    body = _propose(client, user, conversation["id"], "Retry it anyway")
    pending = body["pending_action"]
    assert pending["result"]["eligibility"]["eligible"] is False
    confirmed = _confirm(client, user, pending["id"])
    action = confirmed.json()["action"]
    assert action["status"] == "failed"
    assert action["result"]["code"] == "retry_not_eligible"
    assert _run_count(project["id"]) == 1


# ---------------------------------------------------------------------
# Scenario 13 — cancel is gated until the separate confirm call
# ---------------------------------------------------------------------


def test_scenario_13_cancel_requires_confirmation(client, user, monkeypatch):
    project = _create_project(client, user)
    running_run = _seed_run(
        user["user"]["id"], project["id"], status="running", stage="asset_manager"
    )
    conversation = _conversation(client, user)
    _stub_llm(
        monkeypatch,
        [{"tool": "cancel_run", "arguments": {"run_id": running_run}}],
    )
    body = _propose(client, user, conversation["id"], "Cancel that run")
    pending = body["pending_action"]
    assert pending["action_class"] == "consequential"
    assert "consequential" in body["message"]["content"]
    # The proposal alone changed nothing.
    run = run_ledger.get_run(running_run)
    assert run["cancel_requested"] is False
    assert run["status"] == "running"

    # Reject leaves the run untouched.
    rejected = client.post(
        f"/api/v1/assistant/actions/{pending['id']}/reject",
        headers=_bearer(user["session_token"]),
    )
    assert rejected.json()["action"]["status"] == "rejected"
    assert run_ledger.get_run(running_run)["cancel_requested"] is False

    # A fresh proposal, confirmed, is the only path that cancels.
    _stub_llm(
        monkeypatch,
        [{"tool": "cancel_run", "arguments": {"run_id": running_run}}],
    )
    body = _propose(client, user, conversation["id"], "Cancel it, really")
    confirmed = _confirm(client, user, body["pending_action"]["id"])
    assert confirmed.json()["action"]["status"] == "executed"
    assert run_ledger.get_run(running_run)["cancel_requested"] is True


def test_approve_plan_releases_awaiting_run(client, user, monkeypatch):
    _promote_to_admin(user)  # FS-C: plan approval is admin-only
    project = _create_project(client, user)
    awaiting = _seed_run(
        user["user"]["id"],
        project["id"],
        status="awaiting_approval",
        stage="script_analysis",
    )
    conversation = _conversation(client, user)
    _stub_llm(
        monkeypatch,
        [
            {
                "tool": "approve_plan",
                "arguments": {"project_id": project["id"]},
            }
        ],
    )
    body = _propose(client, user, conversation["id"], "Approve the plan")
    confirmed = _confirm(client, user, body["pending_action"]["id"])
    action = confirmed.json()["action"]
    assert action["status"] == "executed"
    assert action["result"]["released"] is True
    assert action["result"]["run"]["run_id"] == awaiting
