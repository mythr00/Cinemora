"""Phase 6 (A-17) — server-persisted Editor Studio track locks.

Locks are editor project state (services/editor_locks), persisted
server-side, carried by the Studio view, set through the API, and
ENFORCED in services/editor.apply_operations: a batch touching a
locked track is rejected whole with 409 track_locked and persists
nothing. Runs against the Stage 6 synthetic production fixture.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import stage6_fixture as fx

PROJECT = "proj-locks"
API_PROJECT = "proj-locks-api"
TOKEN = "stage6-test-token"
AUTH = {"X-API-Key": TOKEN}


@pytest.fixture(scope="module")
def production(tmp_path_factory):
    root = tmp_path_factory.mktemp("locks-media")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CINEMORA_MEDIA_ROOT", str(root))
        built = fx.build_synthetic_production(root, PROJECT)
        built["root"] = root
        yield built


@pytest.fixture(scope="module")
def api_production(production):
    root = production["root"]
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CINEMORA_MEDIA_ROOT", str(root))
        yield fx.build_synthetic_production(root, API_PROJECT)


@pytest.fixture()
def client(monkeypatch, api_production):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")
    from main import app

    return TestClient(app, raise_server_exceptions=False)


def _rev0(project_id):
    from services import editor

    editor.get_editor(project_id)
    for view in editor.list_revisions(project_id):
        if view["revision_number"] == 0:
            return editor.get_revision(project_id, view["revision_id"])
    raise AssertionError("no revision 0")


def _clip(doc, sentence=1):
    return next(
        i
        for i in doc["tracks"]["visual"]
        if i["kind"] == "clip" and i["sentence_number"] == sentence
    )


@pytest.fixture(autouse=True)
def _clean_locks(production):
    from services import editor_locks

    for track in editor_locks.TRACK_IDS:
        editor_locks.set_track_lock(PROJECT, track, False)
        editor_locks.set_track_lock(API_PROJECT, track, False)
    yield


def test_lock_persists_and_defaults_empty(production):
    from services import editor_locks

    assert editor_locks.locked_tracks(PROJECT) == []
    assert editor_locks.set_track_lock(PROJECT, "video", True) == ["video"]
    # A fresh read (as after a page refresh) sees the persisted lock.
    assert editor_locks.locked_tracks(PROJECT) == ["video"]
    assert editor_locks.set_track_lock(PROJECT, "video", False) == []


def test_unknown_track_rejected(production):
    from services import editor_locks

    with pytest.raises(ValueError):
        editor_locks.set_track_lock(PROJECT, "subtitlez", True)


def test_locked_video_track_rejects_clip_ops_atomically(production):
    from services import editor, editor_locks

    _view0, doc0 = _rev0(PROJECT)
    clip = _clip(doc0)
    editor_locks.set_track_lock(PROJECT, "video", True)
    before = len(editor.list_revisions(PROJECT))
    with pytest.raises(editor.EditorError) as excinfo:
        editor.apply_operations(
            PROJECT,
            doc0["revision_id"],
            [{"type": "trim_clip", "item_id": clip["item_id"], "duration": 1.0}],
        )
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "track_locked"
    # Atomic: no new revision was persisted.
    assert len(editor.list_revisions(PROJECT)) == before


def test_unlocked_track_ops_still_apply(production):
    from services import editor, editor_locks

    _view0, doc0 = _rev0(PROJECT)
    clip = _clip(doc0)
    editor_locks.set_track_lock(PROJECT, "music", True)  # unrelated track
    _view1, doc1 = editor.apply_operations(
        PROJECT,
        doc0["revision_id"],
        [{"type": "trim_clip", "item_id": clip["item_id"], "duration": 1.0}],
    )
    changed = next(
        i for i in doc1["tracks"]["visual"] if i["item_id"] == clip["item_id"]
    )
    assert changed["duration"] == pytest.approx(1.0, abs=0.05) or changed


def test_locked_music_rejects_set_volumes_for_music_only(production):
    from services import editor, editor_locks

    _view0, doc0 = _rev0(PROJECT)
    editor_locks.set_track_lock(PROJECT, "music", True)
    with pytest.raises(editor.EditorError) as excinfo:
        editor.apply_operations(
            PROJECT,
            doc0["revision_id"],
            [{"type": "set_volumes", "music": 0.5}],
        )
    assert excinfo.value.code == "track_locked"
    # Narration-only volume change is untouched by the music lock.
    editor.apply_operations(
        PROJECT,
        doc0["revision_id"],
        [{"type": "set_volumes", "narration": 0.5}],
    )


def test_locked_captions_rejects_edit_caption(production):
    from services import editor, editor_locks

    _view0, doc0 = _rev0(PROJECT)
    editor_locks.set_track_lock(PROJECT, "captions", True)
    segments = doc0["tracks"]["captions"]["segments"]
    assert segments, "fixture must carry captions"
    with pytest.raises(editor.EditorError) as excinfo:
        editor.apply_operations(
            PROJECT,
            doc0["revision_id"],
            [
                {
                    "type": "edit_caption",
                    "segment_index": 0,
                    "text": "Edited",
                    "start": segments[0]["start"],
                    "end": segments[0]["end"],
                }
            ],
        )
    assert excinfo.value.code == "track_locked"


# ---------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------


def test_api_lock_roundtrip_and_studio_view(client):
    from services import editor

    editor.get_editor(API_PROJECT)
    put = client.put(
        f"/api/v1/projects/{API_PROJECT}/editor/track-locks/narration",
        json={"locked": True},
        headers=AUTH,
    )
    assert put.status_code == 200, put.text
    assert put.json()["locked_tracks"] == ["narration"]

    got = client.get(
        f"/api/v1/projects/{API_PROJECT}/editor/track-locks", headers=AUTH
    )
    assert got.status_code == 200
    assert got.json()["locked_tracks"] == ["narration"]

    revisions = client.get(f"/api/v1/projects/{API_PROJECT}/revisions", headers=AUTH)
    rev_id = revisions.json()["revisions"][0]["revision_id"]
    studio = client.get(
        f"/api/v1/projects/{API_PROJECT}/revisions/{rev_id}/studio", headers=AUTH
    )
    assert studio.status_code == 200, studio.text
    assert studio.json()["locked_tracks"] == ["narration"]


def test_api_unknown_track_is_422(client):
    response = client.put(
        f"/api/v1/projects/{API_PROJECT}/editor/track-locks/nope",
        json={"locked": True},
        headers=AUTH,
    )
    assert response.status_code == 422


def test_api_locks_require_auth(client):
    response = client.get(f"/api/v1/projects/{API_PROJECT}/editor/track-locks")
    assert response.status_code in (401, 403, 503)


def test_api_operations_on_locked_track_return_409(client):
    from services import editor

    _view0, doc0 = _rev0(API_PROJECT)
    clip = _clip(doc0)
    put = client.put(
        f"/api/v1/projects/{API_PROJECT}/editor/track-locks/video",
        json={"locked": True},
        headers=AUTH,
    )
    assert put.status_code == 200
    response = client.post(
        f"/api/v1/projects/{API_PROJECT}/revisions/{doc0['revision_id']}/operations",
        json={
            "operations": [
                {"type": "trim_clip", "item_id": clip["item_id"], "duration": 1.0}
            ]
        },
        headers=AUTH,
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "track_locked"
