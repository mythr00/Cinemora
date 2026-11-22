"""IA-06 — catalogue endpoints for genres and voices.

GET /api/v1/catalogues/genres and GET /api/v1/catalogues/voices
serve the REAL sources of truth under the standard v1 contract
(same X-API-Key auth and {error:{code,message}} shape as every
other v1 route):

- genres: services.genre_packs.available_packs() — the pack JSON
  documents (general / history / true_crime) with their real
  id/label/description and pack fields, nothing invented;
- voices: services.narration.VOICE_CATALOGUE (Stage 3J) — exactly
  the installed voice(s): "en_US-lessac-medium" plus its "default"
  alias, both backed by the one installed Piper model.

There is deliberately no music catalogue: the music library is
founder-gated (P3) and these routes must not activate it — the
genre view does not even carry the packs' music metadata fields.
"""

import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from database import engine
from models import AssetDecision, Base, ProductionRun, Project
from services import genre_packs, narration

TOKEN = "ia06-test-token"
HEADERS = {"X-API-Key": TOKEN}
GENRES_URL = "/api/v1/catalogues/genres"
VOICES_URL = "/api/v1/catalogues/voices"


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    api_v1.rate_limiter.reset()
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Project.__table__,
            ProductionRun.__table__,
            AssetDecision.__table__,
        ],
    )
    yield


def _assert_error_shape(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]


# ---------------------------------------------------------------------
# Auth — same behaviour as every other v1 route
# ---------------------------------------------------------------------


@pytest.mark.parametrize("url", [GENRES_URL, VOICES_URL])
def test_catalogues_require_token(client, url):
    missing = client.get(url)
    _assert_error_shape(missing, 401, "unauthorized")


@pytest.mark.parametrize("url", [GENRES_URL, VOICES_URL])
def test_catalogues_reject_wrong_token(client, url):
    wrong = client.get(url, headers={"X-API-Key": "nope"})
    _assert_error_shape(wrong, 401, "unauthorized")


def test_catalogues_fail_closed_when_unconfigured(client, monkeypatch):
    monkeypatch.delenv("CINEMORA_API_TOKEN", raising=False)
    response = client.get(GENRES_URL, headers=HEADERS)
    _assert_error_shape(response, 503, "api_not_configured")


# ---------------------------------------------------------------------
# Genres — the real pack data
# ---------------------------------------------------------------------


def test_genres_catalogue_matches_pack_data(client):
    response = client.get(GENRES_URL, headers=HEADERS)
    assert response.status_code == 200, response.text
    genres = response.json()["genres"]

    # Exactly the real pack ids — no more, no fewer (five packs
    # since Transformation Phase 2.1/2.2 added engineering and
    # industrial).
    assert {g["id"] for g in genres} == {
        "history", "true_crime", "general", "engineering", "industrial",
    }
    assert len(genres) == 5

    for entry in genres:
        pack = genre_packs.load_pack(entry["id"])
        assert pack is not None
        # id/name/description are the pack document's own values
        # (the pack's human-readable name is its "label" field).
        assert entry["name"] == pack["label"]
        assert entry["label"] == pack["label"]
        assert entry["description"] == pack["description"]
        assert entry["name"]
        assert entry["description"]
        # Relevant pack fields, verbatim from the pack data.
        assert entry["style_family"] == pack["style_family"]
        assert entry["graphics_motifs"] == pack["graphics_motifs"]
        assert entry["format_preferences"] == pack["format_preferences"]
        assert entry["entity_handling"] == pack["entity_handling"]
        assert entry["research_query_templates"] == pack[
            "research_query_templates"
        ]
        assert entry["pacing"] == pack["pacing"]


def test_genres_catalogue_style_families_are_truthful(client):
    genres = {
        g["id"]: g
        for g in client.get(GENRES_URL, headers=HEADERS).json()["genres"]
    }
    assert genres["history"]["style_family"] == "history"
    assert genres["true_crime"]["style_family"] == "true_crime"
    assert genres["general"]["style_family"] is None


def test_genres_catalogue_carries_no_music_activation(client):
    # P3 is founder-gated: no music library route exists and the
    # genre view does not smuggle the packs' music metadata in.
    for entry in client.get(GENRES_URL, headers=HEADERS).json()["genres"]:
        assert "music_mood" not in entry
        assert "mix_preset" not in entry
        assert "tracks" not in entry
        assert "library" not in entry
    missing = client.get("/api/v1/catalogues/music", headers=HEADERS)
    assert missing.status_code == 404


# ---------------------------------------------------------------------
# Voices — exactly the installed catalogue
# ---------------------------------------------------------------------


def test_voices_catalogue_matches_installed_voices(client):
    response = client.get(VOICES_URL, headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["default_voice_id"] == narration.DEFAULT_VOICE_ID == "default"
    voices = body["voices"]

    # Exactly the real catalogue ids (Stage 3J: the installed
    # voices plus the "default" alias; Phase 2.4 installed the
    # second voice, en_US-ryan-medium) — nothing invented,
    # nothing missing.
    assert {v["id"] for v in voices} == set(narration.VOICE_CATALOGUE)
    assert {v["id"] for v in voices} == {
        "en_US-lessac-medium", "en_US-ryan-medium", "default",
    }
    assert len(voices) == 3

    by_id = {v["id"]: v for v in voices}
    canonical = by_id["en_US-lessac-medium"]
    assert canonical["is_default"] is False
    assert canonical["alias_of"] is None
    assert canonical["model"] == "en_US-lessac-medium.onnx"
    assert canonical["model_file"] == "en_US-lessac-medium.onnx"
    assert canonical["installed"] is True

    default = by_id["default"]
    assert default["is_default"] is True
    # The alias is reported as an alias of the real voice, backed by
    # the same installed model — never dressed up as a second voice.
    assert default["alias_of"] == "en_US-lessac-medium"
    assert default["model"] == canonical["model"]
    assert default["installed"] is True

    # Every catalogue entry is backed by files that actually exist.
    for entry in voices:
        model, config = narration.VOICE_CATALOGUE[entry["id"]]
        assert entry["model_file"] == model.name
        assert entry["config_file"] == config.name
        assert entry["installed"] == bool(model.exists() and config.exists())
