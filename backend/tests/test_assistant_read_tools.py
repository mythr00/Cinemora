"""Phase 6.3 — read intelligence verification (acceptance
scenarios 1-5, 10, 11b from design 10).

Seeded through the real registration + project-creation paths;
runs and artefacts are seeded as records (the same shapes the
pipeline writes). Scenario tests run at the API level with the
stub-LLM seam; the evidence-pack, script, recommendations and
cross-owner tests call the tool handlers with real Principals.
"""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

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
    ProductionRun,
    Project,
    User,
    UserSubscription,
)
from services import auth as auth_service
from services import media_paths
from services.assistant import llm as assistant_llm
from services.assistant import orchestrator, registry

TOKEN = "test-token"
PASSWORD = "alpha-correct-horse-42"
SCRIPT_A = "The harbour at dawn. Cranes over the water. A ship arrives."


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.delenv("CINEMORA_ALLOW_OPERATOR_TOKEN", raising=False)
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
                    max_concurrent_runs=1,
                    max_runs_per_period=10,
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


def _register(client, email):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_project(client, user, title, script=SCRIPT_A):
    response = client.post(
        "/api/v1/projects",
        headers=_bearer(user["session_token"]),
        json={"title": title, "script": script},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _seed_run(owner_id, project_id, *, status, stage=None, error=None,
              progress=None, minutes_ago=5):
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
                progress=progress,
                script_hash=hashlib.sha256(SCRIPT_A.encode()).hexdigest(),
                created_at=datetime.now(timezone.utc)
                - timedelta(minutes=minutes_ago),
                started_at=datetime.now(timezone.utc)
                - timedelta(minutes=minutes_ago),
            )
        )
        db.commit()
    finally:
        db.close()
    return run_id


def _write_artefact(project_id, run_id, filename, payload):
    project_dir = media_paths.project_dir(project_id)
    run_dir = project_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / filename).write_text(json.dumps(payload), encoding="utf-8")
    # Legacy project-level copy too: resolve_artefact falls back
    # to it, and older records only ever had this shape.
    (project_dir / filename).write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _principal(user_json):
    return auth_service.Principal(
        user_id=user_json["user"]["id"],
        email=user_json["user"]["email"],
        role=user_json["user"]["role"],
        auth_method="session",
    )


def _call(principal, tool_name, **arguments):
    spec = registry.get(tool_name)
    assert spec is not None, tool_name
    return spec.handler(principal, spec.input_model(**arguments))


@pytest.fixture()
def seeded(client):
    suffix = uuid.uuid4().hex[:8]
    a = _register(client, f"read-a-{suffix}@example.com")
    b = _register(client, f"read-b-{suffix}@example.com")
    older = _create_project(client, a, "Harbour Story")
    newer = _create_project(client, a, "Mountain Story")
    foreign = _create_project(client, b, "Someone Elses Film")
    return {"a": a, "b": b, "older": older, "newer": newer, "foreign": foreign}


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


def _post(client, user, conversation_id, content):
    return client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/messages",
        headers=_bearer(user["session_token"]),
        json={"content": content},
    )


# ---------------------------------------------------------------------
# Scenarios 1-3 (API level, stub-driven)
# ---------------------------------------------------------------------


def test_scenario_1_latest_project(client, seeded, monkeypatch):
    conversation = _conversation(client, seeded["a"])
    _stub_llm(
        monkeypatch,
        [
            {"tool": "list_projects", "arguments": {"limit": 5}},
            {"final": "Your latest project is Mountain Story."},
        ],
    )
    response = _post(
        client, seeded["a"], conversation["id"], "What is my latest project?"
    )
    body = response.json()
    assert body["state"] == "ok"
    projects = body["tool_results"]["list_projects"]["projects"]
    assert projects[0]["id"] == seeded["newer"]["id"]
    assert projects[0]["title"] == "Mountain Story"
    assert body["tool_results"]["list_projects"]["total_count"] == 2


def test_scenario_2_project_count(client, seeded, monkeypatch):
    conversation = _conversation(client, seeded["a"])
    _stub_llm(
        monkeypatch,
        [
            {"tool": "list_projects", "arguments": {}},
            {"final": "You have 2 projects."},
        ],
    )
    response = _post(
        client, seeded["a"], conversation["id"], "How many projects do I have?"
    )
    assert response.json()["tool_results"]["list_projects"]["total_count"] == 2


def test_scenario_3_running_documentary_status(client, seeded, monkeypatch):
    run_id = _seed_run(
        seeded["a"]["user"]["id"],
        seeded["older"]["id"],
        status="running",
        stage="asset_manager",
        progress={
            "worker_id": "worker-1",
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    conversation = _conversation(client, seeded["a"])
    _stub_llm(
        monkeypatch,
        [
            {
                "tool": "get_job_status",
                "arguments": {"project_id": seeded["older"]["id"]},
            },
            {"final": "It is gathering assets."},
        ],
    )
    response = _post(
        client,
        seeded["a"],
        conversation["id"],
        "What is the status of my running documentary?",
    )
    result = response.json()["tool_results"]["get_job_status"]
    assert result["run_id"] == run_id
    assert result["status"] == "running"
    assert result["stage"] == "asset_manager"
    assert result["presence"]["worker_id"] == "worker-1"
    assert result["presence"]["live"] is True


# ---------------------------------------------------------------------
# Scenarios 4, 5, 10 (handler level, real principals)
# ---------------------------------------------------------------------


def test_scenario_4_failure_evidence_pack(client, seeded):
    run_id = _seed_run(
        seeded["a"]["user"]["id"],
        seeded["older"]["id"],
        status="failed",
        stage="asset_manager",
        error="Coverage gate failed.",
    )
    _write_artefact(
        seeded["older"]["id"],
        run_id,
        "production_state.json",
        {
            "current_stage": "asset_manager",
            "error": "Coverage gate failed.",
            "stages": {
                "voice_actor": {"status": "completed", "error": None},
                "asset_manager": {
                    "status": "failed",
                    "error": "Only 9 of 23 sentences found verified visuals.",
                },
            },
        },
    )
    pack = _call(
        _principal(seeded["a"]),
        "get_production_errors",
        project_id=seeded["older"]["id"],
    )
    assert pack["run_error"] == "Coverage gate failed."
    assert pack["stage_errors"] == {
        "asset_manager": "Only 9 of 23 sentences found verified visuals."
    }
    assert "do not invent a cause" in pack["evidence_note"]


def test_scenario_5_script_is_exact(client, seeded):
    result = _call(
        _principal(seeded["a"]),
        "get_project_script",
        project_id=seeded["older"]["id"],
    )
    assert result["script"] == SCRIPT_A
    assert result["total_chars"] == len(SCRIPT_A)
    assert result["has_more"] is False
    # Pagination: a later window continues exactly where the first ended.
    page = _call(
        _principal(seeded["a"]),
        "get_project_script",
        project_id=seeded["older"]["id"],
        offset=10,
        limit_chars=100,
    )
    assert page["script"] == SCRIPT_A[10:]
    assert page["has_more"] is False


def test_scenario_10_recommendations_carry_real_figures(client, seeded):
    run_id = _seed_run(
        seeded["a"]["user"]["id"],
        seeded["older"]["id"],
        status="completed",
        minutes_ago=60,
    )
    _write_artefact(
        seeded["older"]["id"],
        run_id,
        "asset_report.json",
        {
            "coverage": {
                "total_sentences": 11,
                "covered_sentences": 9,
                "uncovered_sentences": [4, 7],
                "complete": False,
            },
            "missing_visual_count": 2,
            "repeated_footage": 6,
            "source_reuse": {"distinct_sources": 1},
            "unverified_assets": 0,
            "low_confidence_selections": 0,
            "download_failures": [],
            "discovery_failures": [],
        },
    )
    result = _call(
        _principal(seeded["a"]),
        "get_project_recommendations",
        project_id=seeded["older"]["id"],
    )
    findings = {f["finding"]: f for f in result["recommendations"]}
    assert findings["Incomplete visual coverage"]["evidence"][
        "covered_sentences"
    ] == 9
    assert findings["Incomplete visual coverage"]["evidence"][
        "total_sentences"
    ] == 11
    assert findings["Sentences with no verified visual"]["evidence"][
        "missing_visual_count"
    ] == 2
    assert findings["Repeated footage"]["evidence"]["repeated_footage"] == 6


# ---------------------------------------------------------------------
# Scenario 11b — structural cross-owner refusal
# ---------------------------------------------------------------------


def test_scenario_11b_cross_owner_tools_refuse(client, seeded):
    foreign_principal = _principal(seeded["b"])
    # Every project-scoped read of A's project is the same 404 as
    # a missing project.
    for tool_name, kwargs in (
        ("get_project_details", {}),
        ("get_project_script", {}),
        ("get_project_assets", {}),
        ("get_project_recommendations", {}),
        ("get_production_errors", {}),
    ):
        with pytest.raises(api_v1.ApiError) as caught:
            _call(
                foreign_principal,
                tool_name,
                project_id=seeded["older"]["id"],
                **kwargs,
            )
        assert caught.value.status_code == 404, tool_name
    # Lists and searches never include the foreign project.
    listed = _call(foreign_principal, "list_projects")
    assert [p["id"] for p in listed["projects"]] == [seeded["foreign"]["id"]]
    # "Mountain" matches only A's newer project (title + script);
    # B's own project legitimately contains "harbour" in its
    # script, so that query would match B's own data by design.
    searched = _call(foreign_principal, "search_projects", query="Mountain")
    assert searched["projects"] == []
    # Account tools describe B only.
    profile = _call(foreign_principal, "get_account_profile")
    assert profile["project_count"] == 1
    assert profile["user"]["email"] == seeded["b"]["user"]["email"]


def test_account_tools_describe_the_caller(client, seeded):
    principal = _principal(seeded["a"])
    profile = _call(principal, "get_account_profile")
    assert profile["project_count"] == 2
    assert profile["plan"]["name"] == "free"
    assert profile["providers"]["serper"]["enabled"] is True
    usage = _call(principal, "get_account_usage", days=30)
    assert usage["runs_total"] == 0
    prefs = _call(principal, "get_account_preferences")
    assert prefs == {"preferences": {}}
    activity = _call(principal, "get_account_activity", limit=5)
    assert activity == {"activity": []}
