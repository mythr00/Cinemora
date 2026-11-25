"""Editor Studio (founder priority #5) — backend verification.

Covers the two operations the Studio adds to the revision editor
(set_transition, assign_graphic) and the Studio read models in
services/editor_studio.py (multitrack projection, media pool,
frame-accurate preview, waveforms), at service level and through
the real api_v1 routes. Everything runs against the Stage 6
synthetic production fixture: real clips, real narration, a real
motion-engine graphic, a real ledger.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import stage6_fixture as fx

PROJECT = "proj-studio"
API_PROJECT = "proj-studio-api"
TOKEN = "studio-test-token"
AUTH = {"X-API-Key": TOKEN}


@pytest.fixture(scope="module")
def production(tmp_path_factory):
    root = tmp_path_factory.mktemp("studio-media")
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
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")  # E-21: explicit opt-in
    from main import app

    return TestClient(app, raise_server_exceptions=False)


def _rev0(project_id):
    from services import editor

    editor.get_editor(project_id)
    for view in editor.list_revisions(project_id):
        if view["revision_number"] == 0:
            return editor.get_revision(project_id, view["revision_id"])
    raise AssertionError("no revision 0")


def _items(doc, kind=None):
    items = doc["tracks"]["visual"]
    if kind is not None:
        items = [i for i in items if i["kind"] == kind]
    return items


def _apply(project_id, revision_id, operations):
    from services import editor

    return editor.apply_operations(project_id, revision_id, operations)


# ---------------------------------------------------------------------
# set_transition
# ---------------------------------------------------------------------


def test_set_transition_applies_to_new_revision(production):
    _view0, doc0 = _rev0(PROJECT)
    clip_b = next(
        i for i in _items(doc0) if i["sentence_number"] == 2
    )
    _view1, doc1 = _apply(
        PROJECT,
        doc0["revision_id"],
        [
            {
                "type": "set_transition",
                "item_id": clip_b["item_id"],
                "transition": "dissolve",
                "reason": "time_passage",
                "window_frames": 18,
            }
        ],
    )
    changed = next(
        i for i in _items(doc1) if i["item_id"] == clip_b["item_id"]
    )
    assert changed["transition"] == "dissolve"
    assert changed["transition_reason"] == "time_passage"
    assert changed["transition_window_frames"] == 18
    # The parent revision is untouched (the chain is the history).
    original = next(
        i for i in _items(doc0) if i["item_id"] == clip_b["item_id"]
    )
    assert original["transition"] == "cut"


def test_set_transition_cut_clears_window(production):
    _view0, doc0 = _rev0(PROJECT)
    clip_b = next(i for i in _items(doc0) if i["sentence_number"] == 2)
    _v1, doc1 = _apply(
        PROJECT,
        doc0["revision_id"],
        [
            {
                "type": "set_transition",
                "item_id": clip_b["item_id"],
                "transition": "fade",
                "window_frames": 12,
            }
        ],
    )
    item1 = next(i for i in _items(doc1) if i["item_id"] == clip_b["item_id"])
    _v2, doc2 = _apply(
        PROJECT,
        doc1["revision_id"],
        [
            {
                "type": "set_transition",
                "item_id": item1["item_id"],
                "transition": "cut",
            }
        ],
    )
    item2 = next(i for i in _items(doc2) if i["item_id"] == clip_b["item_id"])
    assert item2["transition"] == "cut"
    assert "transition_window_frames" not in item2


def test_set_transition_first_item_refused(production):
    from services import editor

    _view0, doc0 = _rev0(PROJECT)
    first = _items(doc0)[0]
    with pytest.raises(editor.EditorError) as error:
        _apply(
            PROJECT,
            doc0["revision_id"],
            [
                {
                    "type": "set_transition",
                    "item_id": first["item_id"],
                    "transition": "fade",
                }
            ],
        )
    assert error.value.code == "no_incoming_boundary"


def test_set_transition_match_cut_refused(production):
    from services import editor

    _view0, doc0 = _rev0(PROJECT)
    clip_b = next(i for i in _items(doc0) if i["sentence_number"] == 2)
    with pytest.raises(editor.EditorError) as error:
        _apply(
            PROJECT,
            doc0["revision_id"],
            [
                {
                    "type": "set_transition",
                    "item_id": clip_b["item_id"],
                    "transition": "match_cut",
                }
            ],
        )
    assert error.value.code == "transition_needs_evidence"


def test_set_transition_validation(production):
    from services import editor

    _view0, doc0 = _rev0(PROJECT)
    clip_b = next(i for i in _items(doc0) if i["sentence_number"] == 2)
    before = len(editor.list_revisions(PROJECT))
    for op, code in (
        ({"transition": "spin"}, "unknown_transition"),
        ({"transition": "fade", "reason": "drama"}, "unknown_transition_reason"),
        ({"transition": "fade", "window_frames": 0}, "window_frames_out_of_range"),
        ({"transition": "fade", "window_frames": "wide"}, "window_frames_invalid"),
    ):
        with pytest.raises(editor.EditorError) as error:
            _apply(
                PROJECT,
                doc0["revision_id"],
                [{"type": "set_transition", "item_id": clip_b["item_id"], **op}],
            )
        assert error.value.code == code, op
    # Every refusal was atomic: no revision was created.
    assert len(editor.list_revisions(PROJECT)) == before


def test_set_transition_on_gap_refused(production):
    from services import editor

    _view0, doc0 = _rev0(PROJECT)
    clip_b = next(i for i in _items(doc0) if i["sentence_number"] == 2)
    _v1, doc1 = _apply(
        PROJECT,
        doc0["revision_id"],
        [
            {
                "type": "remove_clip",
                "item_id": clip_b["item_id"],
                "gap_policy": "leave_gap",
            }
        ],
    )
    gap = next(i for i in _items(doc1) if i["kind"] == "gap")
    with pytest.raises(editor.EditorError) as error:
        _apply(
            PROJECT,
            doc1["revision_id"],
            [
                {
                    "type": "set_transition",
                    "item_id": gap["item_id"],
                    "transition": "fade",
                }
            ],
        )
    assert error.value.code == "cannot_set_transition_on_gap"


# ---------------------------------------------------------------------
# assign_graphic
# ---------------------------------------------------------------------


def test_assign_graphic_new_inputs_rerenders(production):
    from services import run_ledger

    _view0, doc0 = _rev0(PROJECT)
    graphic = _items(doc0, "graphic")[0]
    view1, doc1 = _apply(
        PROJECT,
        doc0["revision_id"],
        [
            {
                "type": "assign_graphic",
                "item_id": graphic["item_id"],
                "inputs": {"percent": 75, "label": "FUEL", "sublabel": "LEFT"},
            }
        ],
    )
    item = _items(doc1, "graphic")[0]
    assert item["graphic"]["inputs"]["percent"] == 75
    assert item["graphic"]["inputs"]["label"] == "FUEL"
    assert item["classification"] == "generated_graphic"
    assert "graphic_assigned" in item["flags"]
    assert "editor_media" in item["asset"]["file_path"]
    assert Path(item["asset"]["file_path"]).is_file()
    rows = run_ledger.list_decisions(production["run_id"], 1000, 0)[0]
    generated = [
        r
        for r in rows
        if r["decision_type"] == "generated"
        and r["revision_id"] == view1["revision_id"]
    ]
    assert len(generated) == 1


def test_assign_graphic_unknown_template_refused_atomically(production):
    from services import editor

    _view0, doc0 = _rev0(PROJECT)
    graphic = _items(doc0, "graphic")[0]
    before = len(editor.list_revisions(PROJECT))
    with pytest.raises(editor.EditorError) as error:
        _apply(
            PROJECT,
            doc0["revision_id"],
            [
                {
                    "type": "assign_graphic",
                    "item_id": graphic["item_id"],
                    "template": "no_such_template",
                    "inputs": {"percent": 10},
                }
            ],
        )
    assert error.value.code == "unknown_template"
    assert len(editor.list_revisions(PROJECT)) == before


def test_assign_graphic_validation(production):
    from services import editor

    _view0, doc0 = _rev0(PROJECT)
    graphic = _items(doc0, "graphic")[0]
    clip = _items(doc0, "clip")[0]
    with pytest.raises(editor.EditorError) as error:
        _apply(
            PROJECT,
            doc0["revision_id"],
            [{"type": "assign_graphic", "item_id": graphic["item_id"]}],
        )
    assert error.value.code == "nothing_to_assign"
    with pytest.raises(editor.EditorError) as error:
        _apply(
            PROJECT,
            doc0["revision_id"],
            [
                {
                    "type": "assign_graphic",
                    "item_id": graphic["item_id"],
                    "inputs": ["not", "a", "dict"],
                }
            ],
        )
    assert error.value.code == "graphic_inputs_invalid"
    with pytest.raises(editor.EditorError) as error:
        _apply(
            PROJECT,
            doc0["revision_id"],
            [
                {
                    "type": "assign_graphic",
                    "item_id": clip["item_id"],
                    "inputs": {"percent": 1},
                }
            ],
        )
    assert error.value.code == "not_a_graphic"


# ---------------------------------------------------------------------
# Studio view / media pool
# ---------------------------------------------------------------------


def test_studio_view_projects_multitrack_model(production):
    from services import editor_studio

    _view0, doc0 = _rev0(PROJECT)
    studio = editor_studio.studio_view(PROJECT, doc0["revision_id"])
    assert [t["id"] for t in studio["tracks"]] == [
        "video",
        "graphics",
        "captions",
        "narration",
        "music",
    ]
    tracks = {t["id"]: t for t in studio["tracks"]}
    # Sentence 1 carries a clip + a still on the video track;
    # sentence 3's graphic sits on the graphics track.
    video_names = [
        i["asset"]["file_name"] for i in tracks["video"]["items"]
    ]
    assert "clip_a.mp4" in video_names and "still.png" in video_names
    assert [i["asset"]["file_name"] for i in tracks["graphics"]["items"]] == [
        "graphic.mp4"
    ]
    graphic = tracks["graphics"]["items"][0]
    assert graphic["graphic"]["template"] == "data_gauge"
    # Timeline media is recorded in the media index, so it is
    # servable — the key must be present, not guessed.
    clip = next(
        i for i in tracks["video"]["items"]
        if i["asset"]["file_name"] == "clip_a.mp4"
    )
    assert clip["asset"]["media_key"]
    assert len(tracks["narration"]["sentences"]) == 3
    assert tracks["captions"]["segments"]
    assert studio["duration_seconds"] > 0
    # Catalogues describe what the renderer actually honours.
    assert "dissolve" in studio["catalogues"]["transitions"]
    assert "match_cut" not in studio["catalogues"]["transitions"]
    assert "data_gauge" in studio["catalogues"]["graphic_templates"]


def test_media_pool_lists_recorded_assets(production):
    from services import editor_studio

    _view0, doc0 = _rev0(PROJECT)
    pool = editor_studio.media_pool(PROJECT, doc0["revision_id"])
    by_name = {e["file_name"]: e for e in pool["items"]}
    assert pool["total"] >= 4
    clip = by_name["clip_a.mp4"]
    assert clip["available"] is True
    assert clip["in_timeline"] is True
    assert clip["classification"] == "verified_footage"
    assert clip["media_key"]
    graphic = by_name["graphic.mp4"]
    assert graphic["classification"] == "generated_graphic"
    # Pagination is honest about the total.
    page = editor_studio.media_pool(
        PROJECT, doc0["revision_id"], limit=2, offset=0
    )
    assert page["total"] == pool["total"] and len(page["items"]) == 2


# ---------------------------------------------------------------------
# Frame preview + waveforms
# ---------------------------------------------------------------------


def test_preview_frame_from_video_and_image(production):
    from services import editor_studio

    _view0, doc0 = _rev0(PROJECT)
    clip = next(
        i for i in _items(doc0) if i["asset"]["file_path"].endswith("clip_a.mp4")
    )
    path, content_type = editor_studio.preview_frame(
        PROJECT, doc0["revision_id"], (clip["start"] + clip["end"]) / 2
    )
    assert content_type == "image/jpeg"
    assert Path(path).read_bytes()[:2] == b"\xff\xd8"
    still = next(i for i in _items(doc0) if i["asset"]["type"] == "image")
    path2, content_type2 = editor_studio.preview_frame(
        PROJECT, doc0["revision_id"], (still["start"] + still["end"]) / 2
    )
    assert str(path2).endswith("still.png")
    assert content_type2 == "image/png"


def test_preview_frame_covers_gap_with_holdover(production):
    from services import editor_studio

    _view0, doc0 = _rev0(PROJECT)
    clip_b = next(i for i in _items(doc0) if i["sentence_number"] == 2)
    _v1, doc1 = _apply(
        PROJECT,
        doc0["revision_id"],
        [
            {
                "type": "remove_clip",
                "item_id": clip_b["item_id"],
                "gap_policy": "leave_gap",
            }
        ],
    )
    gap = next(i for i in _items(doc1) if i["kind"] == "gap")
    # The gap previews as the render covers it: the previous
    # genuine visual (the still) held over the gap window.
    path, content_type = editor_studio.preview_frame(
        PROJECT, doc1["revision_id"], (gap["start"] + gap["end"]) / 2
    )
    assert str(path).endswith("still.png")
    assert content_type == "image/png"


def test_waveforms_are_real_peaks(production):
    from services import editor_studio

    _view0, doc0 = _rev0(PROJECT)
    wave = editor_studio.waveform(PROJECT, doc0["revision_id"], "narration")
    assert wave["duration_seconds"] > 0
    assert len(wave["peaks"]) == wave["bucket_count"]
    assert all(0.0 <= p <= 1.0 for p in wave["peaks"])
    # Peaks are normalised to the file's own maximum, which is
    # reported separately — real narration is not silence.
    assert wave["peak"] > 0
    assert max(wave["peaks"]) == 1.0
    cached = editor_studio.waveform(PROJECT, doc0["revision_id"], "narration")
    assert cached == wave


def test_waveform_music_and_unknown_track(production):
    from services import editor, editor_studio

    _view0, doc0 = _rev0(PROJECT)
    if not (doc0["tracks"]["music"].get("file")):
        _v, doc0 = _apply(
            PROJECT,
            doc0["revision_id"],
            [{"type": "set_music", "music_file": "music.wav"}],
        )
    wave = editor_studio.waveform(PROJECT, doc0["revision_id"], "music")
    assert wave["track"] == "music"
    assert max(wave["peaks"]) > 0  # the fixture tone is audible
    with pytest.raises(editor.EditorError) as error:
        editor_studio.waveform(PROJECT, doc0["revision_id"], "sfx")
    assert error.value.code == "unknown_track"


# ---------------------------------------------------------------------
# API level (the frontend's exact routes)
# ---------------------------------------------------------------------


def _api_rev0(client):
    response = client.get(f"/api/v1/projects/{API_PROJECT}/editor", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()["document"]


def test_studio_endpoints_over_http(client):
    doc = _api_rev0(client)
    rid = doc["revision_id"]
    base = f"/api/v1/projects/{API_PROJECT}/revisions/{rid}/studio"

    response = client.get(base, headers=AUTH)
    assert response.status_code == 200, response.text
    assert [t["id"] for t in response.json()["tracks"]] == [
        "video",
        "graphics",
        "captions",
        "narration",
        "music",
    ]

    response = client.get(f"{base}/pool", headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json()["total"] >= 4

    response = client.get(f"{base}/frame", params={"t": 0.5}, headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content[:2] == b"\xff\xd8"

    response = client.get(
        f"{base}/waveform", params={"track": "narration"}, headers=AUTH
    )
    assert response.status_code == 200, response.text
    assert response.json()["peaks"]


def test_studio_operations_over_http(client):
    doc = _api_rev0(client)
    rid = doc["revision_id"]
    clip_b = next(
        i for i in doc["tracks"]["visual"] if i["sentence_number"] == 2
    )
    response = client.post(
        f"/api/v1/projects/{API_PROJECT}/revisions/{rid}/operations",
        headers=AUTH,
        json={
            "operations": [
                {
                    "type": "set_transition",
                    "item_id": clip_b["item_id"],
                    "transition": "fade",
                    "reason": "scene_boundary",
                }
            ]
        },
    )
    assert response.status_code == 201, response.text
    new_rid = response.json()["revision_id"]
    reread = client.get(
        f"/api/v1/projects/{API_PROJECT}/revisions/{new_rid}", headers=AUTH
    )
    assert reread.status_code == 200, reread.text
    item = next(
        i
        for i in reread.json()["document"]["tracks"]["visual"]
        if i["item_id"] == clip_b["item_id"]
    )
    assert item["transition"] == "fade"


def test_studio_unknown_project_and_revision_404(client):
    response = client.get(
        "/api/v1/projects/no-such-project/revisions/x/studio", headers=AUTH
    )
    assert response.status_code == 404
    doc = _api_rev0(client)
    assert doc["revision_id"]
    response = client.get(
        f"/api/v1/projects/{API_PROJECT}/revisions/no-such-revision/studio",
        headers=AUTH,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "revision_not_found"


def test_studio_rejects_non_owner_session(client, monkeypatch):
    """A signed-in user who does not own the project gets 404 —
    the Phase 8 rule, exercised on the Studio surface."""
    import fakeredis

    from database import SessionLocal
    from models import User
    from services import auth as auth_service

    fake = fakeredis.FakeRedis(decode_responses=True)
    auth_service.set_redis_client(fake)
    try:
        db = SessionLocal()
        try:
            user = User(
                id="studio-stranger",
                email="studio-stranger@example.com",
                password_hash=auth_service.hash_password("studio-password-1"),
                role="user",
                status="active",
            )
            db.add(user)
            db.commit()
        finally:
            db.close()
        headers = {
            "Authorization": f"Bearer {auth_service.create_session('studio-stranger')}"
        }
        doc = _api_rev0(client)
        response = client.get(
            f"/api/v1/projects/{API_PROJECT}/revisions/"
            f"{doc['revision_id']}/studio",
            headers=headers,
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "project_not_found"
    finally:
        auth_service.reset_redis_client()
        db = SessionLocal()
        try:
            db.query(User).filter(User.id == "studio-stranger").delete()
            db.commit()
        finally:
            db.close()
