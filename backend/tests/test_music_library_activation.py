"""CINEMATIC MUSIC LIBRARY activation (founder mandate
2026-11-27) — the bundled catalogue, the browse/preview routes,
selection through the real configuration seam, the mix through
the real soundtrack stage, and the mandate's audio validation.

Covers the mandate Part 6 checks at the level this environment
can prove without a production job (the film stop is in force):
files present (1), tracks distinct (2), previews work (3),
selection works (4), selection persists through the generation
projection (5), the selected track is measured in the mixed
soundtrack (6), disabling works (7), narration intelligibility
proxy (8), duration handling — loop and trim (9), fades and
loop-boundary continuity (10), concurrent worker access (11),
missing files handled honestly (12).
"""

import hashlib
import json
import math
import struct
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from database import engine
from models import AssetDecision, Base, ProductionRun, Project
from services import music as music_service
from services import music_library, production_config

TOKEN = "music-activation-token"
HEADERS = {"X-API-Key": TOKEN}

BUNDLED_ROOT = (
    Path(__file__).resolve().parent.parent / "cinemora_music"
)
WAYNE_TRACK = "epic-battle-speech-wayne-jones"


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")
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


def _write_wav(path, seconds, *, freq=440, gap=None, rate=22050, amp=0.5):
    """Speech-like fixture: tone bursts with a quiet gap, so the
    provenance measurement has narration gaps to find music in."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = b""
        for i in range(int(rate * seconds)):
            t = i / rate
            silent = gap is not None and gap[0] <= t < gap[1]
            value = 0 if silent else int(
                amp * 32767 * math.sin(2 * math.pi * freq * t))
            frames += struct.pack("<h", value)
        wav.writeframes(frames)
    return path


# -----------------------------------------------------------------
# 1 + 2: files present, tracks distinct
# -----------------------------------------------------------------

def test_bundled_catalogue_loads_verified_and_distinct():
    catalogue = music_library.load_catalogue(
        BUNDLED_ROOT / "catalogue.json",
        asset_root=BUNDLED_ROOT,
        verify_hashes=True,
    )
    assert len(catalogue) >= 1
    ids = [t.track_id for t in catalogue.tracks]
    hashes = [t.content_sha256 for t in catalogue.tracks]
    assert len(ids) == len(set(ids)), "duplicate track_id in library"
    assert len(hashes) == len(set(hashes)), "duplicate track bytes in library"
    manifest = json.loads(
        (BUNDLED_ROOT / "music_library_manifest.json").read_text(
            encoding="utf-8")
    )
    assert manifest["distinct_tracks"] == len(catalogue)
    for track in catalogue.tracks:
        assert (BUNDLED_ROOT / track.assets.full).is_file()
        if track.assets.preview:
            assert (BUNDLED_ROOT / track.assets.preview).is_file()
        assert track.duration_seconds and track.duration_seconds > 0
        assert track.genres, "every library track carries a category"


def test_bundled_library_is_the_default_catalogue(monkeypatch):
    monkeypatch.delenv("CINEMORA_MUSIC_CATALOGUE", raising=False)
    state = production_config.library_state()
    assert state["configured"] is True
    assert state["available"] is True
    wayne = next(
        t for t in state["tracks"] if t["track_id"] == WAYNE_TRACK)
    assert wayne["performer"] == "Wayne Jones"
    assert wayne["category"] == "epic_battles"
    assert wayne["has_preview"] is True
    assert wayne["duration_seconds"] > 100


# -----------------------------------------------------------------
# 3: previews work (and refuse honestly)
# -----------------------------------------------------------------

def test_preview_route_streams_audio(client):
    response = client.get(
        f"/api/v1/catalogues/music/{WAYNE_TRACK}/preview",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("audio/")
    assert len(response.content) > 100_000  # a real 30 s MP3 clip


def test_preview_route_unknown_track_is_404(client):
    response = client.get(
        "/api/v1/catalogues/music/no-such-track/preview",
        headers=HEADERS,
    )
    assert response.status_code == 404


def test_preview_route_requires_auth(client):
    response = client.get(
        f"/api/v1/catalogues/music/{WAYNE_TRACK}/preview")
    assert response.status_code in (401, 403)


def test_music_catalogue_route_lists_tracks(client):
    response = client.get("/api/v1/catalogues/music", headers=HEADERS)
    assert response.status_code == 200, response.text
    library = response.json()["music_library"]
    assert library["available"] is True
    assert WAYNE_TRACK in [t["track_id"] for t in library["tracks"]]


# -----------------------------------------------------------------
# 4 + 5: selection works and persists through the projection
# -----------------------------------------------------------------

def test_selection_resolves_bundled_track_and_projects():
    config = production_config.clean_config(
        {"music": {"enabled": True, "source": "library",
                   "track_id": WAYNE_TRACK}}
    )
    track, path = production_config.resolve_library_track(config)
    assert track.track_id == WAYNE_TRACK
    assert path.is_file()
    flat = production_config.flat_options(config)
    assert flat["music_track_id"] == WAYNE_TRACK
    assert flat["music_file"] == str(path)


def test_selection_never_substitutes_an_unknown_track():
    config = production_config.clean_config(
        {"music": {"enabled": True, "source": "library",
                   "track_id": "not-a-real-track"}}
    )
    with pytest.raises(production_config.ConfigError) as excinfo:
        production_config.resolve_library_track(config)
    assert excinfo.value.code == "music_track_unknown"


# -----------------------------------------------------------------
# 6 + 8 + 9 + 10: the mix — loop case (track shorter than film)
# -----------------------------------------------------------------

def test_library_track_loops_to_cover_longer_narration(tmp_path):
    config = production_config.clean_config(
        {"music": {"enabled": True, "source": "library",
                   "track_id": WAYNE_TRACK, "volume": 0.2}}
    )
    flat = production_config.flat_options(config)
    # The Wayne Jones track is ~111 s; a 125 s narration forces
    # the crossfaded-loop path.
    narration = _write_wav(
        tmp_path / "narration.wav", 125, freq=440, gap=(60.0, 64.0))
    out = tmp_path / "soundtrack.wav"
    record = music_service.prepare_soundtrack(
        str(narration), str(out),
        music_file=flat["music_file"],
        music_volume=flat["music_volume"],
        source_type="library_track",
        decided_by="music_file",
        requested=True,
    )
    assert record["state"] == "applied"
    catalogue = music_library.load_catalogue(
        BUNDLED_ROOT / "catalogue.json", asset_root=BUNDLED_ROOT)
    record_track = catalogue.by_id(WAYNE_TRACK)
    assert record["identity"]["content_sha256"] == (
        record_track.content_sha256)
    assert record["mix"]["loop"]["mode"] == "crossfade"
    assert record["mix"]["loop"]["fade_in_seconds"] > 0
    assert record["mix"]["loop"]["fade_out_seconds"] > 0
    validation = record["validation"]
    assert validation["duration_match"] is True
    assert validation["no_clipping"] is True
    assert validation["music_detected_in_narration_gaps"] is True
    assert validation["narration_intelligibility_proxy"] is True
    assert validation["bed_continuity"]["passed"] is True
    assert validation["passed"] is True


def test_library_track_trims_to_shorter_narration(tmp_path):
    config = production_config.clean_config(
        {"music": {"enabled": True, "source": "library",
                   "track_id": WAYNE_TRACK, "volume": 0.2}}
    )
    flat = production_config.flat_options(config)
    narration = _write_wav(
        tmp_path / "narration.wav", 20, freq=440, gap=(9.0, 11.0))
    out = tmp_path / "soundtrack.wav"
    record = music_service.prepare_soundtrack(
        str(narration), str(out),
        music_file=flat["music_file"],
        music_volume=flat["music_volume"],
        source_type="library_track",
        decided_by="music_file",
        requested=True,
    )
    assert record["state"] == "applied"
    assert record["mix"]["loop"]["mode"] == "none"  # trimmed, not looped
    assert record["validation"]["duration_match"] is True
    assert record["validation"]["passed"] is True


# -----------------------------------------------------------------
# 7: disabling works
# -----------------------------------------------------------------

def test_disabled_music_projects_no_file_and_records_disabled(tmp_path):
    config = production_config.clean_config(
        {"music": {"enabled": False, "source": "library",
                   "track_id": WAYNE_TRACK}}
    )
    flat = production_config.flat_options(config)
    assert "music_file" not in flat
    narration = _write_wav(tmp_path / "narration.wav", 6, freq=440)
    out = tmp_path / "soundtrack.wav"
    record = music_service.prepare_soundtrack(
        str(narration), str(out), disabled=True)
    assert record["state"] == "disabled"
    assert record["validation"]["music_applied"] is False


# -----------------------------------------------------------------
# 11: concurrent workers can access the library
# -----------------------------------------------------------------

def test_concurrent_catalogue_access():
    def _load_and_resolve(_):
        catalogue, root = production_config.load_library()
        config = production_config.clean_config(
            {"music": {"enabled": True, "source": "library",
                       "track_id": WAYNE_TRACK}}
        )
        track, path = production_config.resolve_library_track(config)
        return track.track_id, path.is_file()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_load_and_resolve, range(16)))
    assert all(
        track_id == WAYNE_TRACK and exists
        for track_id, exists in results
    )


# -----------------------------------------------------------------
# 12: missing files are handled gracefully and honestly
# -----------------------------------------------------------------

def test_missing_track_file_is_an_honest_record(tmp_path):
    narration = _write_wav(tmp_path / "narration.wav", 4, freq=440)
    out = tmp_path / "soundtrack.wav"
    record = music_service.prepare_soundtrack(
        str(narration), str(out),
        music_file=str(tmp_path / "gone.mp3"),
        source_type="library_track",
        decided_by="music_file",
        requested=True,
    )
    assert record["state"] == "requested_but_missing"
    assert "gone.mp3" in record["error"]


def test_unavailable_library_refuses_selection(monkeypatch, tmp_path):
    # A deployment with NO library at all: env points at an
    # absent catalogue and the bundled path is patched away.
    monkeypatch.setenv(
        "CINEMORA_MUSIC_CATALOGUE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(
        production_config, "BUNDLED_CATALOGUE_PATH",
        tmp_path / "no-bundled.json")
    state = production_config.library_state()
    assert state["available"] is False
    config = production_config.clean_config(
        {"music": {"enabled": True, "source": "library",
                   "track_id": WAYNE_TRACK}}
    )
    with pytest.raises(production_config.ConfigError) as excinfo:
        production_config.resolve_library_track(config)
    assert excinfo.value.code == "music_library_unavailable"


def test_preview_route_404s_when_track_file_missing(
        client, monkeypatch, tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    catalogue = {
        "catalogue_version": 1,
        "tracks": [{
            "track_id": "ghost-track",
            "content_sha256": hashlib.sha256(b"ghost").hexdigest(),
            "title": "Ghost",
            "source_class": "bundled",
            "assets": {"full": "ghost.mp3", "preview": None},
        }],
    }
    catalogue_file = root / "catalogue.json"
    catalogue_file.write_text(json.dumps(catalogue), encoding="utf-8")
    monkeypatch.setenv("CINEMORA_MUSIC_CATALOGUE", str(catalogue_file))
    response = client.get(
        "/api/v1/catalogues/music/ghost-track/preview", headers=HEADERS)
    assert response.status_code == 404
