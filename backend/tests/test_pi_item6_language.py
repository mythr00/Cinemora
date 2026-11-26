"""Product Improvements item 6 — language before generation.

- The language catalogue states honest support facts (narration
  exists only where a voice genuinely exists: English today).
- Script generation honours the chosen script language (the
  EN→ES test: prompts direct Spanish, the draft records "es").
- A pasted/own script can be translated into a NEW draft; the
  original is kept unchanged (both versions preserved).
- The project language is authoritative: a production whose
  language has no narration support is refused BEFORE charging
  (no reservation, no wallet movement, no run) with the honest
  reason — never a silent English substitution.
"""

from __future__ import annotations

import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from auth_helpers import with_session_token
from database import SessionLocal, engine
from models import Base, Plan, ProductionRun, User
from services import auth as auth_service, languages, llm_provider
from services.billing import credits as credits_service

TOKEN = "pi-item6-token"
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


def _user(client, *, fund=5000):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"pi6-{uuid.uuid4().hex[:8]}@example.com",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 201, response.text
    body = with_session_token(response)
    if fund:
        db = SessionLocal()
        try:
            credits_service.grant(
                db, body["user"]["id"], "purchased", fund,
                entry_type="purchase",
                idempotency_key=f"pi6-grant-{body['user']['id']}",
                source="test_fixture",
            )
            db.commit()
        finally:
            db.close()
    return {
        "id": body["user"]["id"],
        "headers": {"Authorization": f"Bearer {body['session_token']}"},
    }


def _balance(user_id):
    db = SessionLocal()
    try:
        account = credits_service.get_or_create_account(db, user_id)
        return account.purchased_available + account.subscription_available
    finally:
        db.close()


# ---------------------------------------------------------------------
# Catalogue facts
# ---------------------------------------------------------------------


def test_language_catalogue_states_honest_support():
    catalogue = {row["code"]: row for row in languages.language_catalogue()}
    assert catalogue["en"]["narration_supported"] is True
    assert len(catalogue["en"]["voices"]) >= 2
    for code in ("es", "fr", "de", "ja"):
        assert catalogue[code]["narration_supported"] is False
        assert catalogue[code]["voices"] == []
        assert catalogue[code]["script_supported"] is True
    assert languages.production_block_reason("en") is None
    reason = languages.production_block_reason("es")
    assert "Spanish" in reason and "English" in reason


def test_languages_route_serves_the_catalogue(client):
    user = _user(client)
    response = client.get("/api/v1/catalogues/languages", headers=user["headers"])
    assert response.status_code == 200, response.text
    codes = {row["code"] for row in response.json()["languages"]}
    assert {"en", "es", "fr", "de", "ja", "ko"} <= codes


# ---------------------------------------------------------------------
# EN -> ES script generation + script-language selection
# ---------------------------------------------------------------------


def _stub_writer(monkeypatch, *, spanish=True):
    monkeypatch.setattr(llm_provider, "BREWKEG_API_KEY", "test-key")
    seen = {"prompts": []}

    def fake_json(system, user, **kwargs):
        seen["prompts"].append(user)
        return {
            "sections": [
                {"heading": "Apertura", "intent": "Abrir.",
                 "word_budget": 700},
            ]
        }

    def fake_text(system, user, **kwargs):
        seen["prompts"].append(user)
        if "Write ONE compelling" in user:
            return "El puente que nadie debió cruzar"
        return (
            "El puente se derrumbó una mañana de verano."
            if spanish
            else "The bridge fell one summer morning."
        )

    monkeypatch.setattr(llm_provider, "complete_json", fake_json)
    monkeypatch.setattr(llm_provider, "complete_text", fake_text)
    return seen


def test_generate_script_in_spanish(client, monkeypatch):
    user = _user(client)
    seen = _stub_writer(monkeypatch)
    response = client.post(
        "/api/v1/script-studio/ai/draft",
        headers=user["headers"],
        json={
            "topic": "El puente colgante",
            "target_minutes": 5,
            "language": "es",
        },
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["language"] == "es"
    assert draft["generation"]["language"] == "es"
    assert "derrumbar" in draft["script_text"] or "derrumbó" in draft["script_text"]
    # The writer was actually directed to write Spanish.
    assert any("Spanish" in prompt for prompt in seen["prompts"])


def test_generate_script_unknown_language_refused(client, monkeypatch):
    user = _user(client)
    _stub_writer(monkeypatch)
    response = client.post(
        "/api/v1/script-studio/ai/draft",
        headers=user["headers"],
        json={"topic": "Anything", "target_minutes": 5, "language": "xx"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "language_unknown"


# ---------------------------------------------------------------------
# Translation keeps both versions
# ---------------------------------------------------------------------


def test_translate_draft_keeps_original_and_creates_spanish(client, monkeypatch):
    user = _user(client)
    created = client.post(
        "/api/v1/script-studio/scripts",
        headers=user["headers"],
        json={
            "title": "The Bridge",
            "sections": [
                {"heading": "Opening", "body": "The bridge fell at dawn."}
            ],
        },
    )
    assert created.status_code == 201, created.text
    draft = created.json()

    monkeypatch.setattr(llm_provider, "BREWKEG_API_KEY", "test-key")
    monkeypatch.setattr(
        llm_provider,
        "complete_json",
        lambda system, user, **kw: {
            "heading": "Apertura",
            "body": "El puente cayó al amanecer.",
        },
    )
    response = client.post(
        f"/api/v1/script-studio/scripts/{draft['id']}/translate",
        headers=user["headers"],
        json={"language": "es"},
    )
    assert response.status_code == 201, response.text
    translated = response.json()
    assert translated["id"] != draft["id"]
    assert translated["language"] == "es"
    assert translated["sections"][0]["body"] == "El puente cayó al amanecer."
    assert translated["translated_from"]["script_id"] == draft["id"]
    assert translated["translated_from"]["source_language"] == "en"

    original = client.get(
        f"/api/v1/script-studio/scripts/{draft['id']}",
        headers=user["headers"],
    ).json()
    assert original["language"] == "en"
    assert original["sections"][0]["body"] == "The bridge fell at dawn."


# ---------------------------------------------------------------------
# Production language gate: refused BEFORE charging
# ---------------------------------------------------------------------


def _create_project(client, user, **options):
    response = client.post(
        "/api/v1/projects",
        headers=user["headers"],
        json={
            "title": "Language Test",
            "script": "Alpha opens. Bravo continues. Charlie closes.",
            "options": options,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_spanish_project_language_stored_and_run_refused_pre_charge(client):
    user = _user(client)
    project = _create_project(client, user, language="es")
    assert project["options"]["language"] == "es"
    assert (
        project["options"]["production_config"]["narration"]["language"]
        == "es"
    )

    before = _balance(user["id"])
    response = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        headers=user["headers"],
        json={},
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] in (
        "language_narration_unsupported",
        "config_validation_failed",
    )
    assert "Spanish" in error["message"]
    # Nothing charged, nothing queued.
    assert _balance(user["id"]) == before
    db = SessionLocal()
    try:
        runs = (
            db.query(ProductionRun)
            .filter(ProductionRun.project_id == project["id"])
            .count()
        )
    finally:
        db.close()
    assert runs == 0


def test_english_project_unaffected(client):
    user = _user(client)
    project = _create_project(client, user, language="en")
    assert project["options"]["language"] == "en"
    # Admission proceeds past the language gate (the run is
    # accepted by the ledger; workers are not running here, the
    # run simply exists).
    response = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        headers=user["headers"],
        json={},
    )
    assert response.status_code in (200, 201, 202), response.text


def test_unknown_language_refused_at_creation(client):
    user = _user(client)
    response = client.post(
        "/api/v1/projects",
        headers=user["headers"],
        json={
            "title": "Bad Language",
            "script": "Alpha opens. Bravo continues.",
            "options": {"language": "xx"},
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "language_unknown"
