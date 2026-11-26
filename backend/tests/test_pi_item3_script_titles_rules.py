"""Product Improvements item 3 + the permanent Script Generation
Rules (founder directives 2026-11-26).

- Every AI-generated script automatically receives a compelling
  video title, distinct from the topic/working title, saved on
  the draft, editable, and carried into the production project.
  The title never appears inside the narration.
- Generated narration is natural prose ONLY: no timestamps, no
  HOOK/INTRODUCTION/CONCLUSION headings, no chapter numbers, no
  production instructions. The sanitizer enforces the rules on
  AI output even when the model disobeys the prompt.
"""

from __future__ import annotations

import re
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from auth_helpers import with_session_token
from database import SessionLocal, engine
from models import Base, Plan, Project, User
from services import auth as auth_service, llm_provider
from services.script_studio import sanitize_narration_body

TOKEN = "pi-item3-token"
PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(
        fakeredis.FakeRedis(decode_responses=True)
    )
    db = SessionLocal()
    try:
        db.query(User).delete()
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


def _bearer(user):
    return {"Authorization": f"Bearer {user['session_token']}"}


@pytest.fixture()
def user(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"pi3-{uuid.uuid4().hex[:8]}@example.com",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 201, response.text
    body = with_session_token(response)
    # Item 8 (operations extension): script AI operations charge
    # ordinary accounts their listed credit price.
    from services.billing import credits as credits_service

    db = SessionLocal()
    try:
        credits_service.grant(
            db,
            body["user"]["id"],
            "purchased",
            5000,
            entry_type="purchase",
            idempotency_key=f"pi3-grant-{body['user']['id']}",
            source="test_fixture",
        )
        db.commit()
    finally:
        db.close()
    return body


# ---------------------------------------------------------------------
# Sanitizer unit tests
# ---------------------------------------------------------------------


def test_sanitizer_strips_all_production_furniture():
    raw = (
        "[00:30]\n"
        "HOOK\n"
        "The bridge was never meant to fall.\n"
        "CHAPTER 3: THE WARNING SIGNS\n"
        "[Music fades under]\n"
        "NARRATOR: Engineers warned the city [12:45] for years.\n"
        "## Interlude\n"
        "CONCLUSION:\n"
        "Ordinary narration survives intact."
    )
    clean, removed = sanitize_narration_body(raw)
    assert removed >= 6
    assert "bridge was never meant to fall" in clean
    assert "Engineers warned the city" in clean
    assert "Ordinary narration survives intact." in clean
    for furniture in ("[00:30]", "[12:45]", "HOOK", "CHAPTER",
                      "Music fades", "NARRATOR:", "##", "CONCLUSION"):
        assert furniture not in clean


def test_sanitizer_never_alters_ordinary_prose():
    prose = (
        "In 1981, at 2:47 in the morning, the walkway gave way. "
        "Chapter by chapter, the report later showed neglect. "
        "A [sic] mark in a quote is not a timestamp."
    )
    clean, removed = sanitize_narration_body(prose)
    assert clean == prose
    assert removed == 0


# ---------------------------------------------------------------------
# Generation: title + sanitised bodies, end to end via the API
# ---------------------------------------------------------------------

DIRTY_BODY = (
    "[00:00]\nHOOK\nThe harbour changed the world in a single "
    "morning. [04:12]\n[Cut to aerial footage]"
)
GOOD_TITLE = "The Morning One Ship Rewrote World Trade"


def _stub_generation(monkeypatch, *, title=GOOD_TITLE):
    monkeypatch.setattr(llm_provider, "BREWKEG_API_KEY", "test-key")
    state = {"text": 0}

    def fake_json(system, user, **kwargs):
        return {
            "sections": [
                {"heading": "Opening", "intent": "Open on the ship.",
                 "word_budget": 700},
                {"heading": "Fallout", "intent": "Ports change.",
                 "word_budget": 750},
            ]
        }

    def fake_text(system, user, **kwargs):
        state["text"] += 1
        if "Write ONE compelling" in user:
            return title
        return DIRTY_BODY

    monkeypatch.setattr(llm_provider, "complete_json", fake_json)
    monkeypatch.setattr(llm_provider, "complete_text", fake_text)
    return state


def test_generated_script_gets_title_and_clean_narration(
    client, user, monkeypatch
):
    _stub_generation(monkeypatch)
    response = client.post(
        "/api/v1/script-studio/ai/draft",
        headers=_bearer(user),
        json={"topic": "The container port", "target_minutes": 10},
    )
    assert response.status_code == 201, response.text
    draft = response.json()

    # Item 3: automatic title, distinct from topic and working title.
    assert draft["video_title"] == GOOD_TITLE
    assert draft["video_title"] != draft["title"]
    assert draft["title"] == "The container port"

    # Script rules: bodies are sanitised before storage.
    for section in draft["sections"]:
        assert "[00:00]" not in section["body"]
        assert "[04:12]" not in section["body"]
        assert "HOOK" not in section["body"]
        assert "Cut to aerial" not in section["body"]
        assert "harbour changed the world" in section["body"]
    assert draft["generation"]["script_rules_sanitised"] >= 3

    # The title is metadata: it never enters the narration text.
    assert GOOD_TITLE not in draft["script_text"]
    # Metrics remain metadata alongside the script.
    assert draft["metrics"]["word_count"] > 0
    assert "word_count" not in draft["script_text"]


def test_title_is_editable_and_carried_into_production(
    client, user, monkeypatch
):
    _stub_generation(monkeypatch)
    draft = client.post(
        "/api/v1/script-studio/ai/draft",
        headers=_bearer(user),
        json={"topic": "The container port", "target_minutes": 10},
    ).json()

    edited = client.put(
        f"/api/v1/script-studio/scripts/{draft['id']}",
        headers=_bearer(user),
        json={"video_title": "A Better Public Title"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["video_title"] == "A Better Public Title"

    sent = client.post(
        f"/api/v1/script-studio/scripts/{draft['id']}/send-to-production",
        headers=_bearer(user),
    )
    assert sent.status_code == 201, sent.text
    project_id = sent.json()["project_id"]

    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        assert project is not None
        # The production project carries the VIDEO title.
        assert project.title == "A Better Public Title"
        # …and the project's script is narration only.
        assert "[00:00]" not in project.script
        assert "HOOK" not in project.script
    finally:
        db.close()


def test_title_failure_never_destroys_a_script(client, user, monkeypatch):
    monkeypatch.setattr(llm_provider, "BREWKEG_API_KEY", "test-key")

    def fake_json(system, user, **kwargs):
        return {
            "sections": [
                {"heading": "Opening", "intent": "Open.",
                 "word_budget": 700},
            ]
        }

    def fake_text(system, user, **kwargs):
        if "Write ONE compelling" in user:
            raise RuntimeError("title model down")
        return "Clean narration, no furniture."

    monkeypatch.setattr(llm_provider, "complete_json", fake_json)
    monkeypatch.setattr(llm_provider, "complete_text", fake_text)
    response = client.post(
        "/api/v1/script-studio/ai/draft",
        headers=_bearer(user),
        json={"topic": "Resilient scripts", "target_minutes": 5},
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["video_title"] is None
    assert draft["sections"][0]["body"] == "Clean narration, no furniture."


def test_writer_prompts_state_the_permanent_rules():
    from services import script_studio

    assert "timestamps" in script_studio.SCRIPT_RULES_TEXT
    assert "HOOK" in script_studio.SCRIPT_RULES_TEXT
    assert script_studio.SCRIPT_RULES_TEXT in script_studio._WRITER_SYSTEM
    section_prompt = script_studio._section_prompt(
        "Topic", "Heading", "Intent", 500, "opening"
    )
    assert script_studio.SCRIPT_RULES_TEXT in section_prompt
    # Word count / duration stay metadata: the metrics view
    # exposes them outside the script text.
    assert re.search(r"estimated_minutes", str(script_studio.draft_metrics({
        "sections": [{"body": "One two three."}],
        "target_minutes": 10,
    })))
