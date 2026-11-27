"""Channel Research integration + Thumbnail Studio channel
analysis (directives 2026-11-26).

Real-data discipline: extraction is stubbed at the yt-dlp seam
with recorded-shaped payloads; the tests assert that ONLY the
extracted statistics reach the record (nothing invented), that
selection is capped at 3, that the analysis basis is labelled
per video (transcript vs metadata), that ideas flow into the
EXISTING script generation with the research reference kept,
and that channel thumbnail measurement uses the studio's real
measurement core on real image bytes.
"""

from __future__ import annotations

import io
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import api_v1
import main
from auth_helpers import with_session_token
from database import SessionLocal, engine
from models import Base, Plan, User
from services import auth as auth_service, channel_research, llm_provider
from services.billing import credits as credits_service

TOKEN = "pi-channel-token"
PASSWORD = "correct horse battery staple"

CHANNEL_PAYLOAD = {
    "channel_id": "UC123",
    "channel": "Harbour Docs",
    "channel_url": "https://www.youtube.com/@HarbourDocs",
    "channel_follower_count": 42000,
    "entries": [
        {"id": "v1", "title": "Big Ships", "view_count": 900,
         "duration": 600, "upload_date": "20250101"},
        {"id": "v2", "title": "Small Boats", "view_count": 5000,
         "duration": 700, "upload_date": "20250201"},
        {"id": "v3", "title": "Medium Ferries", "view_count": 2000,
         "duration": 800, "upload_date": "20250301"},
    ],
}


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
                    id=str(uuid.uuid4()), name="free",
                    max_concurrent_runs=1, max_runs_per_period=3,
                    period="month", priority_tier=0, is_default=True,
                )
            )
        db.commit()
    finally:
        db.close()
    yield
    auth_service.reset_redis_client()


def _user(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"cr-{uuid.uuid4().hex[:8]}@example.com",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 201, response.text
    body = with_session_token(response)
    db = SessionLocal()
    try:
        credits_service.grant(
            db, body["user"]["id"], "purchased", 5000,
            entry_type="purchase",
            idempotency_key=f"cr-grant-{body['user']['id']}",
            source="test_fixture",
        )
        db.commit()
    finally:
        db.close()
    return {"Authorization": f"Bearer {body['session_token']}"}


def _stub_channel(monkeypatch):
    monkeypatch.setattr(
        channel_research, "_run_ytdlp", lambda args: CHANNEL_PAYLOAD
    )


def test_resolve_orders_by_real_view_counts(client, monkeypatch):
    headers = _user(client)
    _stub_channel(monkeypatch)
    response = client.post(
        "/api/v1/script-studio/channel-research",
        headers=headers,
        json={"channel_url": "https://www.youtube.com/@HarbourDocs"},
    )
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["channel"]["title"] == "Harbour Docs"
    assert record["channel"]["subscriber_count"] == 42000
    assert record["ordering_basis"] == "view_count_desc"
    assert [v["id"] for v in record["videos"]] == ["v2", "v3", "v1"]
    assert record["videos"][0]["view_count"] == 5000


def test_resolve_rejects_non_channel_urls(client):
    headers = _user(client)
    response = client.post(
        "/api/v1/script-studio/channel-research",
        headers=headers,
        json={"channel_url": "https://example.com/not-a-channel"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "channel_url_invalid"


def test_analyse_labels_basis_and_caps_selection(client, monkeypatch):
    headers = _user(client)
    return _analyse_for_headers(client, monkeypatch, headers)


def _analyse_for_headers(client, monkeypatch, headers):
    """The analyse flow, parameterised by user so dependent tests
    can act as the SAME user who owns the research record (the
    redesign programme made generate_draft verify ownership)."""
    _stub_channel(monkeypatch)
    record = client.post(
        "/api/v1/script-studio/channel-research",
        headers=headers,
        json={"channel_url": "https://www.youtube.com/@HarbourDocs"},
    ).json()

    def fake_detail(video_id):
        return {
            "id": video_id,
            "title": f"Video {video_id}",
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "view_count": 100,
            "like_count": 5,
            "duration_seconds": 60,
            "upload_date": "20250101",
            "thumbnail": None,
            "description": "A description.",
            "tags": [],
            "transcript": "spoken words" if video_id == "v2" else None,
            "basis": "transcript" if video_id == "v2" else "metadata",
        }

    monkeypatch.setattr(channel_research, "fetch_video_detail", fake_detail)
    monkeypatch.setattr(llm_provider, "BREWKEG_API_KEY", "test-key")
    monkeypatch.setattr(
        llm_provider,
        "complete_json",
        lambda system, user, **kw: {
            "patterns": [
                {"pattern": "Numbers in titles", "basis": "metadata",
                 "evidence": "2 of 3 titles"}
            ],
            "ideas": [
                {"title": "The Last Lighthouse Keeper",
                 "angle": "Automation ends a craft",
                 "why_it_fits": "Single-subject craft story"}
            ],
        },
    )

    too_many = client.post(
        f"/api/v1/script-studio/channel-research/{record['id']}/analyse",
        headers=headers,
        json={"video_ids": ["v1", "v2", "v3", "v1"]},
    )
    assert too_many.status_code == 422

    response = client.post(
        f"/api/v1/script-studio/channel-research/{record['id']}/analyse",
        headers=headers,
        json={"video_ids": ["v2", "v1"]},
    )
    assert response.status_code == 200, response.text
    analysis = response.json()["analysis"]
    assert analysis["basis"] == {"v2": "transcript", "v1": "metadata"}
    assert analysis["patterns"][0]["pattern"] == "Numbers in titles"
    assert analysis["ideas"][0]["title"] == "The Last Lighthouse Keeper"
    return record, analysis


def test_idea_flows_into_existing_generation_with_reference(
    client, monkeypatch
):
    headers = _user(client)
    record, _analysis = _analyse_for_headers(client, monkeypatch, headers)
    # Script generation stubs: outline + sections + title.
    monkeypatch.setattr(
        llm_provider, "complete_json",
        lambda system, user, **kw: {
            "sections": [
                {"heading": "Opening", "intent": "Open.",
                 "word_budget": 700}
            ]
        },
    )
    monkeypatch.setattr(
        llm_provider, "complete_text",
        lambda system, user, **kw: (
            "Un faro solitario."
            if "Write ONE compelling" in user
            else "Narration about a lighthouse keeper."
        ),
    )
    response = client.post(
        "/api/v1/script-studio/ai/draft",
        headers=headers,
        json={
            "topic": "The Last Lighthouse Keeper",
            "angle": "Automation ends a craft",
            "target_minutes": 10,
            "research_id": record["id"],
        },
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["generation"]["research_id"] == record["id"]
    assert draft["generation"]["basis"] == "llm_generated_from_user_brief"


def test_channel_thumbnail_styles_measure_real_images(
    client, monkeypatch
):
    headers = _user(client)
    _stub_channel(monkeypatch)
    # Real image bytes served as the "remote" thumbnails.
    buffer = io.BytesIO()
    Image.new("RGB", (320, 180), (200, 40, 40)).save(buffer, "JPEG")
    payload = buffer.getvalue()

    class _Response:
        content = payload

        def raise_for_status(self):
            return None

    import requests

    monkeypatch.setattr(
        requests, "get", lambda url, **kw: _Response()
    )
    # Point every video at a thumbnail URL.
    for entry in CHANNEL_PAYLOAD["entries"]:
        entry["thumbnail"] = "https://i.ytimg.com/vi/x/hq.jpg"
    response = client.post(
        "/api/v1/thumbnails/channel-analysis",
        headers=headers,
        json={"channel_url": "https://www.youtube.com/@HarbourDocs"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["channel"]["title"] == "Harbour Docs"
    first = body["videos"][0]
    assert first["measurement"] is not None
    assert first["measurement"]["dimensions"] == [320, 180]
    assert first["measurement"]["palette"], "palette measured"
    assert "style reference only" in body["label"]


# ---------------------------------------------------------------------
# Redesign programme (2026-11-27): the pasted-URL forms users
# actually copy, and research genuinely reaching generation.
# ---------------------------------------------------------------------


def test_resolve_normalises_pasted_url_forms(client, monkeypatch):
    headers = _user(client)
    seen_urls = []

    def capturing_run(args):
        seen_urls.append(args[-1])
        return CHANNEL_PAYLOAD

    monkeypatch.setattr(channel_research, "_run_ytdlp", capturing_run)
    pasted_forms = [
        "https://www.youtube.com/@HarbourDocs/videos",
        "https://www.youtube.com/@HarbourDocs?si=share-token",
        "https://www.youtube.com/@HarbourDocs/featured",
        "youtube.com/@HarbourDocs",
        "www.youtube.com/@HarbourDocs/shorts",
        "https://m.youtube.com/@HarbourDocs",
        "@HarbourDocs",
    ]
    for form in pasted_forms:
        response = client.post(
            "/api/v1/script-studio/channel-research",
            headers=headers,
            json={"channel_url": form},
        )
        assert response.status_code == 201, (form, response.text)
    assert seen_urls == [
        "https://www.youtube.com/@HarbourDocs/videos"
    ] * len(pasted_forms)


def test_generate_draft_refuses_unknown_research_id(client, monkeypatch):
    headers = _user(client)
    monkeypatch.setattr(llm_provider, "BREWKEG_API_KEY", "test-key")
    response = client.post(
        "/api/v1/script-studio/ai/draft",
        headers=headers,
        json={
            "topic": "Anything",
            "target_minutes": 10,
            "research_id": "research-does-not-exist",
        },
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "research_not_found"


def test_generate_draft_prompt_carries_research_patterns(
    client, monkeypatch
):
    headers = _user(client)
    record, _analysis = _analyse_for_headers(client, monkeypatch, headers)
    prompts = []

    def capturing_complete_json(system, user, **kw):
        prompts.append(user)
        return {
            "sections": [
                {"heading": "Opening", "intent": "Open.",
                 "word_budget": 700}
            ]
        }

    monkeypatch.setattr(
        llm_provider, "complete_json", capturing_complete_json
    )
    monkeypatch.setattr(
        llm_provider, "complete_text",
        lambda system, user, **kw: "Narration text.",
    )
    response = client.post(
        "/api/v1/script-studio/ai/draft",
        headers=headers,
        json={
            "topic": "The Last Lighthouse Keeper",
            "target_minutes": 10,
            "research_id": record["id"],
        },
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    # The analysis pattern text reached the outline prompt — the
    # research link is functional, not metadata-only.
    assert any("Numbers in titles" in prompt for prompt in prompts)
    assert draft["generation"]["research_id"] == record["id"]
    assert draft["generation"]["research_channel"] == "Harbour Docs"
