"""IA-04 — editor_media (regenerated graphics) in the media index.

Defect (integration audit F-03): plan_document.build_media_index
trusted timeline items, manifests, pipeline outputs, plan
replacements, revision-document EXPORTS, revision/production music
and the upload index — but never the visual items of a revision
document. The editor's regenerate_graphic operation writes its
render under editor_media/<revision_id>/graphic_<item_id>.mp4 and
records it ONLY as that item's asset.file_path in the new revision
document, so the regenerated graphic was unindexed and the media
route 404'd it (the editor showed its honest thumbnail fallback
instead of the real graphic).

Fix: build_media_index now also trusts revision-document visual
items whose recorded paths live under editor_media/, under the
SAME guards as the exports/uploads sources — the path must be
recorded in a revision document, must resolve under the project
directory, and must exist. Nothing is indexed by scanning the
editor_media/ directory, and visual-item paths outside
editor_media/ are not trusted from this source (production visuals
are indexed from their own recorded artefacts).

Index semantics pinned here (inherited from the existing
exports/music sources): the index is built from ALL
revision_*.json documents on disk for the project, as a union.
Switching the current/latest revision therefore does NOT un-serve
an earlier revision's regenerated graphic — exactly how an earlier
revision's export stays servable after a later revision renders.

These tests mirror the exports/uploads media-index tests in
test_stage7b_backend_fixes.py (unit-level recorded-path guard +
API-level serving through the real FastAPI app) and the Stage 4
media-serving test's lightweight project fixture, plus one
end-to-end test through the real editor regenerate_graphic
operation on the Stage 6 synthetic production.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_v1
import main
import stage6_fixture as fx
from database import engine
from models import AssetDecision, Base, ProductionRun, Project
from services import media_paths, plan_document

TOKEN = "ia04-test-token"
HEADERS = {"X-API-Key": TOKEN}

GRAPHIC_BYTES = b"\x00\x00\x00\x18ftypmp42-ia04-regenerated-graphic-bytes"


# ---------------------------------------------------------------------
# Lightweight fixtures (Stage 4 style: real app, per-test media root)
# ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture()
def light_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")  # E-21: explicit opt-in
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
    return tmp_path


def _create_project(client):
    response = client.post(
        "/api/v1/projects",
        headers=HEADERS,
        json={"script": f"Script {uuid.uuid4()}. Alpha. Beta."},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _project_dir(project_id):
    path = media_paths.project_dir(project_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_revision_doc(project_dir, revision_id, visual_assets):
    """Write a revision_<id>.json in the editor document shape,
    carrying `visual_assets` (file paths or None) as its visual
    items' recorded assets — the shape editor._persist_new_revision
    writes and _op_regenerate_graphic records into."""
    visual = []
    for index, file_path in enumerate(visual_assets):
        visual.append(
            {
                "item_id": f"itm_ia04_{index}",
                "kind": "graphic",
                "sentence_number": index + 1,
                "scene_number": 1,
                "asset": (
                    {"file_path": str(file_path), "type": "video",
                     "generated": True}
                    if file_path is not None
                    else None
                ),
                "graphic": {"template": "data_gauge"},
                "classification": "generated_graphic",
                "flags": ["regenerated"],
            }
        )
    document = {
        "version": 1,
        "revision_id": revision_id,
        "project_id": project_dir.name,
        "revision_number": 1,
        "status": "draft",
        "tracks": {
            "visual": visual,
            "narration": {"sentences": []},
            "music": {},
            "captions": {"segments": []},
        },
        "exports": [],
    }
    path = project_dir / f"revision_{revision_id}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _editor_media_file(project_dir, revision_id, name="graphic_itm_ia04_0.mp4",
                       data=GRAPHIC_BYTES):
    target = project_dir / "editor_media" / revision_id / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def _serve(client, project_id, key):
    return client.get(
        f"/api/v1/projects/{project_id}/media/{key}", headers=HEADERS
    )


# ---------------------------------------------------------------------
# API level: recorded regenerated graphic serves 200, exact bytes
# ---------------------------------------------------------------------


def test_regenerated_graphic_recorded_in_revision_serves_200(client, light_env):
    project = _create_project(client)
    project_dir = _project_dir(project["id"])
    graphic = _editor_media_file(project_dir, "rev-1")
    _write_revision_doc(project_dir, "rev-1", [graphic])

    index = plan_document.build_media_index(project["id"], project_dir)
    key = plan_document.media_key_for(project_dir, graphic)
    assert key in index
    assert index[key]["kind"] == "editor_media"
    assert index[key]["path"] == graphic.resolve()

    served = _serve(client, project["id"], key)
    assert served.status_code == 200, served.text
    assert served.content == GRAPHIC_BYTES
    assert served.headers["content-type"].startswith("video/")

    # Media auth is unchanged: no token still 401s on this source.
    assert (
        client.get(f"/api/v1/projects/{project['id']}/media/{key}").status_code
        == 401
    )


def test_revision_switching_keeps_all_revisions_graphics_servable(
    client, light_env
):
    """Existing index semantics: build_media_index unions ALL
    revision documents on disk (the exports/music sources behave
    the same), so a graphic recorded only in an earlier revision
    stays servable after a later revision — which records a
    different graphic and no longer references the first — becomes
    the latest. Switching revisions never un-serves a recorded
    editor_media file; only removing its record (or the file)
    does."""
    project = _create_project(client)
    project_dir = _project_dir(project["id"])

    first_bytes = b"first-revision-graphic"
    second_bytes = b"second-revision-graphic"
    first = _editor_media_file(
        project_dir, "rev-1", "graphic_first.mp4", first_bytes
    )
    second = _editor_media_file(
        project_dir, "rev-2", "graphic_second.mp4", second_bytes
    )
    _write_revision_doc(project_dir, "rev-1", [first])
    # The later revision's document does not reference `first`.
    _write_revision_doc(project_dir, "rev-2", [second])

    index = plan_document.build_media_index(project["id"], project_dir)
    for path, data in ((first, first_bytes), (second, second_bytes)):
        key = plan_document.media_key_for(project_dir, path)
        assert index[key]["kind"] == "editor_media"
        served = _serve(client, project["id"], key)
        assert served.status_code == 200, served.text
        assert served.content == data


def test_cross_project_isolation(client, light_env):
    project_a = _create_project(client)
    project_b = _create_project(client)
    dir_a = _project_dir(project_a["id"])
    dir_b = _project_dir(project_b["id"])

    graphic_a = _editor_media_file(dir_a, "rev-a")
    _write_revision_doc(dir_a, "rev-a", [graphic_a])
    key_a = plan_document.media_key_for(dir_a, graphic_a)

    # Project B's index does not contain A's graphic, and B's media
    # route 404s A's key even though the file exists on disk.
    index_b = plan_document.build_media_index(project_b["id"], dir_b)
    assert key_a not in index_b
    denied = _serve(client, project_b["id"], key_a)
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "media_not_found"
    assert denied.content != GRAPHIC_BYTES

    # A revision document planted in B that records A's file (an
    # absolute path outside B's project directory) is likewise
    # rejected by the under-project-dir guard.
    _write_revision_doc(dir_b, "rev-b", [graphic_a])
    index_b = plan_document.build_media_index(project_b["id"], dir_b)
    assert key_a not in index_b
    assert _serve(client, project_b["id"], key_a).status_code == 404

    # A's own route still serves it.
    served = _serve(client, project_a["id"], key_a)
    assert served.status_code == 200
    assert served.content == GRAPHIC_BYTES


def test_missing_recorded_editor_media_file_is_honest_404(client, light_env):
    project = _create_project(client)
    project_dir = _project_dir(project["id"])
    recorded = (
        project_dir / "editor_media" / "rev-1" / "graphic_gone.mp4"
    )
    # Recorded in the revision document, but the file is NOT on disk.
    _write_revision_doc(project_dir, "rev-1", [recorded])

    index = plan_document.build_media_index(project["id"], project_dir)
    key = plan_document.media_key_for(project_dir, recorded)
    assert key not in index
    missing = _serve(client, project["id"], key)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "media_not_found"


def test_unrecorded_and_traversal_editor_media_paths_404(client, light_env):
    project = _create_project(client)
    project_dir = _project_dir(project["id"])

    # An editor_media file present on disk but recorded NOWHERE:
    # the index trusts records, not the directory listing.
    stray = _editor_media_file(
        project_dir, "rev-stray", "graphic_stray.mp4", b"unrecorded"
    )
    stray_key = plan_document.media_key_for(project_dir, stray)
    index = plan_document.build_media_index(project["id"], project_dir)
    assert stray_key not in index
    assert _serve(client, project["id"], stray_key).status_code == 404

    # A revision document recording a traversal path that resolves
    # OUTSIDE the project directory is never indexed.
    outside = light_env / "outside.mp4"
    outside.write_bytes(b"must never be served")
    traversal = project_dir / "editor_media" / ".." / ".." / "outside.mp4"
    _write_revision_doc(project_dir, "rev-evil", [traversal])
    index = plan_document.build_media_index(project["id"], project_dir)
    by_name = {entry["file_name"] for entry in index.values()}
    assert "outside.mp4" not in by_name
    assert _serve(client, project["id"], stray_key).status_code == 404

    # A recorded absolute path outside the project dir whose name
    # happens to sit under some other editor_media/ folder is also
    # rejected — editor_media membership is judged relative to THIS
    # project directory, not by path substring.
    foreign = light_env / "other" / "editor_media" / "rev-x" / "graphic.mp4"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_bytes(b"foreign bytes")
    _write_revision_doc(project_dir, "rev-foreign", [foreign])
    index = plan_document.build_media_index(project["id"], project_dir)
    by_name = {entry["file_name"] for entry in index.values()}
    assert "graphic.mp4" not in by_name


# ---------------------------------------------------------------------
# Unit level: the visual-item source is scoped to editor_media/
# ---------------------------------------------------------------------


def test_media_index_trusts_only_editor_media_visual_paths(tmp_path):
    """Mirror of test_stage7b's test_media_index_trusts_only_
    recorded_paths for the new source: a visual item recorded with
    a non-editor_media path inside the project dir is NOT indexed
    via the visual source (it would need its own recorded artefact
    — timeline, manifest, export, upload), a gap item with no asset
    is skipped, and a recorded editor_media path is indexed with
    kind editor_media under its client-derived key."""
    project_dir = tmp_path / "proj-unit"
    project_dir.mkdir()
    graphic = _editor_media_file(project_dir, "rev-1")
    production_clip = project_dir / "clip_a.mp4"
    production_clip.write_bytes(b"production bytes")

    document = {
        "revision_id": "rev-1",
        "exports": [],
        "tracks": {
            "visual": [
                {"item_id": "g1", "kind": "graphic",
                 "asset": {"file_path": str(graphic), "type": "video"}},
                {"item_id": "c1", "kind": "clip",
                 "asset": {"file_path": str(production_clip),
                           "type": "video"}},
                {"item_id": "gap1", "kind": "gap", "asset": None},
            ],
            "music": {},
        },
    }
    (project_dir / "revision_rev-1.json").write_text(json.dumps(document))

    index = plan_document.build_media_index("proj-unit", project_dir)
    key = plan_document.media_key_for(project_dir, graphic)
    assert index[key]["kind"] == "editor_media"
    assert index[key]["path"] == graphic.resolve()
    clip_key = plan_document.media_key_for(project_dir, production_clip)
    assert clip_key not in index


# ---------------------------------------------------------------------
# End to end: the real regenerate_graphic operation's output serves
# ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def production(tmp_path_factory):
    root = tmp_path_factory.mktemp("ia04-media")
    # IA-04 test hygiene: the env override must be restored after this
    # module — a bare os.environ assignment leaked into later test files
    # (it broke test_stage2_robustness's media-root assertion in the full
    # suite). MonkeyPatch.context() restores the previous value on exit.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CINEMORA_MEDIA_ROOT", str(root))
        built = fx.build_synthetic_production(root, "proj-ia04-e2e")
        built["root"] = root
        yield built


@pytest.fixture()
def e2e_env(monkeypatch, production):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")  # E-21: explicit opt-in
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(production["root"]))
    api_v1.rate_limiter.reset()
    return production


def test_regenerate_graphic_end_to_end_serves_via_media_route(
    client, e2e_env, production
):
    from services import editor

    project_id = production["project_id"]
    project_dir = production["project_dir"]

    editor.get_editor(project_id)
    rev0 = next(
        view for view in editor.list_revisions(project_id)
        if view["revision_number"] == 0
    )
    _view0, doc0 = editor.get_revision(project_id, rev0["revision_id"])
    graphic = next(
        item for item in doc0["tracks"]["visual"] if item["kind"] == "graphic"
    )
    view1, doc1 = editor.apply_operations(
        project_id,
        doc0["revision_id"],
        [{"type": "regenerate_graphic", "item_id": graphic["item_id"]}],
    )
    item = next(
        i for i in doc1["tracks"]["visual"] if i["kind"] == "graphic"
    )
    recorded = Path(item["asset"]["file_path"])
    # The recording shape the fix trusts: under editor_media/ in a
    # revision document's visual items (editor.py _OpContext +
    # _op_regenerate_graphic).
    assert "editor_media" in recorded.parts
    assert recorded.is_file()
    assert view1["revision_id"] in recorded.parts

    key = plan_document.media_key_for(project_dir, recorded)
    index = plan_document.build_media_index(project_id, project_dir)
    assert index[key]["kind"] == "editor_media"

    served = _serve(client, project_id, key)
    assert served.status_code == 200, served.text
    assert served.content == recorded.read_bytes()
    assert served.headers["content-type"].startswith("video/")
