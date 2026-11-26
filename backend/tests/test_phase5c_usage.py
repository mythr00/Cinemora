"""Phase 5c-1 (B-10) — provider usage ledger + cost visibility.

Covers:
- every BrewKegProvider.chat() (complete) call records an
  append-only provider_usage_events row with the usage the
  provider actually reported (both Anthropic-shaped
  input/output_tokens and OpenAI-shaped prompt/completion/total
  spellings);
- caller context lands when supplied and stays NULL when not —
  never fabricated; unreported tokens stay NULL;
- recording failures NEVER break the LLM call;
- the admin aggregate's math (provider+model+day groups and
  totals), cost rendered null with pricing_status
  "not_configured", and the search section labelled
  process-local;
- GET /api/v1/admin/usage is admin-only (anonymous 401,
  ordinary user 403, admin 200);
- migration 0012 upgrades/downgrades cleanly on SQLite.
"""

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

import main
from database import Base, SessionLocal
from models import ProviderUsageEvent, User
from services import auth as auth_service
from services import llm_provider, provider_usage

BACKEND = Path(__file__).resolve().parent.parent

USER_PASSWORD = "user-password-5c1"
OPERATOR_TOKEN = "operator-token-5c1"


def _uid():
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    monkeypatch.setenv("CINEMORA_API_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")
    Base.metadata.create_all(bind=SessionLocal().get_bind())
    fake = fakeredis.FakeRedis(decode_responses=True)
    auth_service.set_redis_client(fake)
    db = SessionLocal()
    try:
        db.query(ProviderUsageEvent).delete()
        db.query(User).delete()
        db.commit()
    finally:
        db.close()
    yield
    auth_service.reset_redis_client()
    db = SessionLocal()
    try:
        db.query(ProviderUsageEvent).delete()
        db.query(User).delete()
        db.commit()
    finally:
        db.close()


def _events():
    db = SessionLocal()
    try:
        return db.query(ProviderUsageEvent).all()
    finally:
        db.close()


def _stub_request(monkeypatch, data):
    def fake_request(self, system_prompt, user_prompt, **kwargs):
        return data

    monkeypatch.setattr(
        llm_provider.BrewKegProvider, "_request", fake_request
    )


ANTHROPIC_RESPONSE = {
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 100, "output_tokens": 50},
}


# ---------------------------------------------------------------------
# Recording on provider calls
# ---------------------------------------------------------------------


def test_complete_records_usage_with_context(monkeypatch):
    _stub_request(monkeypatch, ANTHROPIC_RESPONSE)
    provider = llm_provider.BrewKegProvider(api_key="test-key")
    completion = provider.complete(
        [{"role": "user", "content": "hi"}],
        usage_context={
            "user_id": "user-1",
            "project_id": "project-1",
            "run_id": "run-1",
            "context": "unit_test",
        },
    )
    assert completion.text == "hello"

    events = _events()
    assert len(events) == 1
    event = events[0]
    assert event.provider == "brewkeg"
    assert event.model == provider.model
    assert event.prompt_tokens == 100
    assert event.completion_tokens == 50
    # Total derived only because BOTH halves were reported.
    assert event.total_tokens == 150
    assert event.user_id == "user-1"
    assert event.project_id == "project-1"
    assert event.run_id == "run-1"
    assert event.context == "unit_test"


def test_complete_records_openai_shaped_usage(monkeypatch):
    _stub_request(
        monkeypatch,
        {
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
            },
        },
    )
    provider = llm_provider.BrewKegProvider(api_key="test-key")
    provider.complete([{"role": "user", "content": "hi"}])
    (event,) = _events()
    assert (event.prompt_tokens, event.completion_tokens) == (7, 3)
    # The provider-reported total wins over any recomputation.
    assert event.total_tokens == 10


def test_complete_without_context_or_usage_records_nulls(monkeypatch):
    _stub_request(
        monkeypatch,
        {
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
        },
    )
    provider = llm_provider.BrewKegProvider(api_key="test-key")
    provider.complete([{"role": "user", "content": "hi"}])
    (event,) = _events()
    # The call is recorded; everything unknown stays NULL.
    assert event.prompt_tokens is None
    assert event.completion_tokens is None
    assert event.total_tokens is None
    assert event.user_id is None
    assert event.project_id is None
    assert event.run_id is None
    assert event.context is None


def test_recording_failure_never_breaks_the_call(monkeypatch):
    _stub_request(monkeypatch, ANTHROPIC_RESPONSE)

    def exploding_record(**kwargs):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(
        provider_usage, "record_usage", exploding_record
    )
    provider = llm_provider.BrewKegProvider(api_key="test-key")
    completion = provider.complete([{"role": "user", "content": "hi"}])
    assert completion.text == "hello"


def test_record_usage_itself_is_fail_soft(monkeypatch):
    import database

    def exploding_session():
        raise RuntimeError("no database")

    monkeypatch.setattr(database, "SessionLocal", exploding_session)
    assert (
        provider_usage.record_usage(
            provider="brewkeg", model="m", usage={"total_tokens": 1}
        )
        is False
    )


# ---------------------------------------------------------------------
# Aggregate math + honest cost/search labelling
# ---------------------------------------------------------------------


def _insert(*, provider, model, prompt, completion, total, when):
    db = SessionLocal()
    try:
        db.add(
            ProviderUsageEvent(
                id=_uid(),
                provider=provider,
                model=model,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                created_at=when,
            )
        )
        db.commit()
    finally:
        db.close()


def test_aggregate_math_and_cost_honesty():
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    _insert(provider="brewkeg", model="m1", prompt=10,
            completion=5, total=15, when=now)
    _insert(provider="brewkeg", model="m1", prompt=20,
            completion=7, total=27, when=now)
    _insert(provider="brewkeg", model="m1", prompt=1,
            completion=2, total=3, when=yesterday)
    _insert(provider="brewkeg", model="m2", prompt=None,
            completion=None, total=None, when=now)

    db = SessionLocal()
    try:
        result = provider_usage.aggregate_usage(db, days=30)
    finally:
        db.close()

    groups = {
        (g["provider"], g["model"], g["day"]): g
        for g in result["llm"]["groups"]
    }
    assert len(groups) == 3
    today_key = next(k for k in groups if k[1] == "m1" and k[2] != str(yesterday.date()))
    today = groups[today_key]
    assert today["calls"] == 2
    assert today["prompt_tokens"] == 30
    assert today["completion_tokens"] == 12
    assert today["total_tokens"] == 42

    totals = result["llm"]["totals"]
    assert totals["calls"] == 4
    assert totals["prompt_tokens"] == 31
    assert totals["completion_tokens"] == 14
    assert totals["total_tokens"] == 45
    # No prices exist anywhere: cost is null, never 0 or estimated.
    assert totals["cost"] is None
    assert totals["pricing_status"] == "not_configured"
    for group in result["llm"]["groups"]:
        assert group["cost"] is None
        assert group["pricing_status"] == "not_configured"

    # Search counters are in-process only — and say so.
    assert result["search"]["provider"] == "searxng"
    assert result["search"]["scope"] == "process_local"
    assert isinstance(result["search"]["calls"], dict)


# ---------------------------------------------------------------------
# Admin-only endpoint matrix
# ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _make_user(email, *, role="user"):
    db = SessionLocal()
    try:
        user = User(
            id=_uid(),
            email=email,
            password_hash=auth_service.hash_password(USER_PASSWORD),
            role=role,
            status="active",
        )
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


def _session_for(user_id):
    return {
        "Authorization": f"Bearer {auth_service.create_session(user_id)}"
    }


def test_usage_endpoint_admin_only(client):
    anonymous = client.get("/api/v1/admin/usage")
    assert anonymous.status_code == 401

    user_id = _make_user(f"user-{uuid.uuid4().hex[:8]}@example.com")
    ordinary = client.get(
        "/api/v1/admin/usage", headers=_session_for(user_id)
    )
    assert ordinary.status_code == 403

    admin_id = _make_user(
        f"admin-{uuid.uuid4().hex[:8]}@example.com", role="admin"
    )
    admin = client.get(
        "/api/v1/admin/usage", headers=_session_for(admin_id)
    )
    assert admin.status_code == 200
    body = admin.json()
    assert body["llm"]["totals"]["cost"] is None
    assert body["llm"]["totals"]["pricing_status"] == "not_configured"
    assert body["search"]["scope"] == "process_local"


# ---------------------------------------------------------------------
# Migration 0012 up/down on SQLite
# ---------------------------------------------------------------------


def _alembic_cfg():
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    return cfg


def test_migration_0012_up_down_sqlite(tmp_path, monkeypatch):
    db_path = tmp_path / "usage_migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        command.upgrade(_alembic_cfg(), "head")
        tables = inspect(engine).get_table_names()
        assert "provider_usage_events" in tables
        columns = {
            column["name"]
            for column in inspect(engine).get_columns(
                "provider_usage_events"
            )
        }
        assert columns == {
            "id", "provider", "model", "prompt_tokens",
            "completion_tokens", "total_tokens", "user_id",
            "project_id", "run_id", "context", "created_at",
        }
        # No price/cost columns exist — by design.
        assert not any("cost" in c or "price" in c for c in columns)

        command.downgrade(_alembic_cfg(), "0011_developer_api")
        assert (
            "provider_usage_events"
            not in inspect(engine).get_table_names()
        )

        command.upgrade(_alembic_cfg(), "head")
        assert (
            "provider_usage_events"
            in inspect(engine).get_table_names()
        )
    finally:
        engine.dispose()
