"""Brief Stage 7b — regression tests for the three backend defects
the Stage 7 editor acceptance exposed (register S7-D1, S7-D2 and
the media-index gap).

- S7-D1: services/editor.py's soundtrack stage called
  renderer.run_command, which services/renderer.py does not define
  (its helper is run_ffmpeg) — ANY revision render with narration
  volume != 1.0 crashed. Stage 6 tests only ever set MUSIC volume,
  so the branch was never reached. Fixed by calling run_ffmpeg and
  treating its False return as an honest render failure.
- S7-D2: GET /projects/{id}/certificate and /assets selected their
  run via run_ledger.latest_run_for, which returns the newest
  ProductionRun of ANY kind — after a revision render (itself a
  ProductionRun with options.kind="revision_render") both described
  the empty render run. Fixed with
  run_ledger.latest_production_run_for, used by those two endpoints
  only; the other latest_run_for callers (submission dedup, project
  view/status) keep any-kind semantics deliberately.
- Media index: plan_document.build_media_index indexed only
  production artefacts, so recorded revision exports and uploads
  404'd on GET /projects/{id}/media/{key} even though the editor
  derives their keys client-side (sha256 of the project-relative
  path). The index now also trusts paths RECORDED in revision
  documents' exports lists and in the upload index — nothing else.

Everything runs against the Stage 6 synthetic production fixture
(real artefacts, real renders) through the real FastAPI app, in
the request shapes the Stage 7 editor UI sends.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import stage6_fixture as fx

TOKEN = "stage7b-test-token"
AUTH = {"X-API-Key": TOKEN}


class SyncQueue:
    """Executes enqueued jobs immediately (worker stand-in)."""

    def enqueue(self, fn, *args, **kwargs):
        from types import SimpleNamespace

        kwargs.pop("at_front", None)
        fn(*args, **kwargs)
        return SimpleNamespace(id="job-sync")


class RecordingQueue:
    """Records enqueued jobs without executing them."""

    def __init__(self):
        self.calls = []

    def enqueue(self, fn, *args, **kwargs):
        from types import SimpleNamespace

        self.calls.append((fn, args, kwargs))
        return SimpleNamespace(id="job-recorded")


@pytest.fixture(scope="module")
def production(tmp_path_factory):
    root = tmp_path_factory.mktemp("stage7b-media")
    os.environ["CINEMORA_MEDIA_ROOT"] = str(root)
    built = fx.build_synthetic_production(root, "proj-stage7b")
    built["root"] = root
    return built


@pytest.fixture()
def client(monkeypatch, production):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    import jobs

    monkeypatch.setattr(jobs, "video_queue", SyncQueue())
    from main import app

    return TestClient(app, raise_server_exceptions=False)


def _rev0(client, project_id):
    # GET /editor ensures revision 0 exists but returns the LATEST
    # revision (other tests move the chain forward); address
    # revision 0 explicitly through the list, like the Stage 6
    # tests do.
    response = client.get(
        f"/api/v1/projects/{project_id}/editor", headers=AUTH
    )
    assert response.status_code == 200, response.text
    listing = client.get(
        f"/api/v1/projects/{project_id}/revisions", headers=AUTH
    )
    assert listing.status_code == 200, listing.text
    for view in listing.json()["revisions"]:
        if view["revision_number"] == 0:
            return view
    raise AssertionError("no revision 0")


def _apply(client, project_id, revision_id, operations):
    response = client.post(
        f"/api/v1/projects/{project_id}/revisions/{revision_id}/operations",
        json={"operations": operations},
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _render(client, project_id, revision_id):
    response = client.post(
        f"/api/v1/projects/{project_id}/revisions/{revision_id}/render",
        json={"allow_gaps": False},
        headers=AUTH,
    )
    assert response.status_code == 202, response.text
    return response.json()


def _revision(client, project_id, revision_id):
    response = client.get(
        f"/api/v1/projects/{project_id}/revisions/{revision_id}",
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _probe_duration(path):
    from services import media_probe

    probe = media_probe.probe_video(path)
    assert probe, f"no probe for {path}"
    return probe["duration_seconds"]


# ---------------------------------------------------------------------
# S7-D1 — narration volume != 1.0 renders (and exports)
# ---------------------------------------------------------------------


def test_render_with_narration_and_music_volume_exports(client, production):
    project_id = production["project_id"]
    rev0 = _rev0(client, project_id)
    view1 = _apply(
        client,
        project_id,
        rev0["revision_id"],
        [{"type": "set_volumes", "narration": 0.5, "music": 0.5}],
    )
    _render(client, project_id, view1["revision_id"])

    after = _revision(client, project_id, view1["revision_id"])
    assert after["status"] == "exported"
    doc = after["document"]
    assert len(doc["exports"]) == 1
    export_file = Path(doc["exports"][0]["file"])
    assert export_file.is_file()
    # The volume adjustment really ran: the adjusted narration is a
    # recorded artefact of this render's work directory.
    work = production["project_dir"] / "editor_renders" / view1["revision_id"]
    assert (work / "narration_adjusted.wav").is_file()
    assert abs(_probe_duration(export_file) - doc["duration_seconds"]) < 0.75


def test_volume_adjustment_failure_records_failed_render(
    client, production, monkeypatch
):
    """run_ffmpeg reports failure by returning False; the render
    must record failed honestly (status, error, no export) rather
    than crash or silently proceed — the failure-handling contract
    S7-D1's fix must preserve."""
    import jobs
    from services import editor, renderer

    project_id = production["project_id"]
    rev0 = _rev0(client, project_id)
    view1 = _apply(
        client,
        project_id,
        rev0["revision_id"],
        [{"type": "set_volumes", "narration": 0.5}],
    )

    monkeypatch.setattr(jobs, "video_queue", RecordingQueue())
    submitted = editor.request_render(project_id, view1["revision_id"])
    run_id = submitted["run_id"]

    real_run_ffmpeg = renderer.run_ffmpeg

    def failing_adjustment(command, timeout=renderer.FFMPEG_TIMEOUT):
        if any("narration_adjusted" in str(part) for part in command):
            return False
        return real_run_ffmpeg(command, timeout)

    monkeypatch.setattr(renderer, "run_ffmpeg", failing_adjustment)
    outcome = editor.run_revision_render(
        project_id, view1["revision_id"], run_id
    )
    assert outcome["status"] == "failed"
    assert "volume" in outcome["error"].lower()

    after = _revision(client, project_id, view1["revision_id"])
    assert after["status"] == "failed"
    assert after["document"]["exports"] == []
    assert "volume" in after["document"]["last_render_error"].lower()


# ---------------------------------------------------------------------
# S7-D2 — certificate + assets keep describing the production
# ---------------------------------------------------------------------


def test_certificate_and_assets_survive_revision_render(client, production):
    project_id = production["project_id"]

    cert_before = client.get(
        f"/api/v1/projects/{project_id}/certificate", headers=AUTH
    )
    assert cert_before.status_code == 200, cert_before.text
    assert cert_before.json()["run"]["run_id"] == production["run_id"]
    assets_before = client.get(
        f"/api/v1/projects/{project_id}/assets", headers=AUTH
    ).json()
    assert assets_before["run_id"] == production["run_id"]
    assert assets_before["total"] > 0

    # Edit + render a revision: this creates a NEWER ProductionRun
    # (the render run) — the exact S7-D2 trigger.
    rev0 = _rev0(client, project_id)
    view1 = _apply(
        client,
        project_id,
        rev0["revision_id"],
        [{"type": "edit_caption", "segment_index": 0,
          "text": "STAGE 7B CERTIFICATE GUARD"}],
    )
    _render(client, project_id, view1["revision_id"])
    rendered = _revision(client, project_id, view1["revision_id"])
    assert rendered["status"] == "exported"

    cert_after = client.get(
        f"/api/v1/projects/{project_id}/certificate", headers=AUTH
    )
    assert cert_after.status_code == 200, cert_after.text
    # Byte-identical: rendering a revision changed neither the run
    # the certificate describes nor a single recorded decision.
    assert cert_after.content == cert_before.content

    assets_after = client.get(
        f"/api/v1/projects/{project_id}/assets", headers=AUTH
    ).json()
    assert assets_after["run_id"] == production["run_id"]
    assert assets_after["total"] == assets_before["total"]

    # The per-revision certificate is unaffected by the scoping.
    revision_cert = client.get(
        f"/api/v1/projects/{project_id}/revisions/"
        f"{view1['revision_id']}/certificate",
        headers=AUTH,
    )
    assert revision_cert.status_code == 200, revision_cert.text
    assert revision_cert.json()["certificate_version"] == 2


def test_latest_production_run_for_skips_render_runs(production):
    from services import run_ledger

    project_id = production["project_id"]
    latest_any = run_ledger.latest_run_for(project_id)
    latest_production = run_ledger.latest_production_run_for(project_id)
    assert latest_production is not None
    assert latest_production["run_id"] == production["run_id"]
    # The any-kind selector is unchanged: after the renders above
    # it still returns the newest run (a revision render).
    assert latest_any is not None
    assert (latest_any.get("options") or {}).get("kind") == "revision_render"
    # A project with no runs at all yields None from both.
    assert run_ledger.latest_production_run_for("no-such-project") is None


# ---------------------------------------------------------------------
# Media index — recorded revision exports + uploads are servable
# ---------------------------------------------------------------------


def test_media_serves_revision_export_and_upload(client, production):
    from services import plan_document

    project_id = production["project_id"]
    project_dir = production["project_dir"]

    rev0 = _rev0(client, project_id)
    view1 = _apply(
        client,
        project_id,
        rev0["revision_id"],
        [{"type": "edit_caption", "segment_index": 0,
          "text": "STAGE 7B MEDIA EXPORT"}],
    )
    _render(client, project_id, view1["revision_id"])
    exports = client.get(
        f"/api/v1/projects/{project_id}/revisions/"
        f"{view1['revision_id']}/exports",
        headers=AUTH,
    ).json()["exports"]
    assert len(exports) == 1
    export_path = Path(exports[0]["file"])
    assert export_path.is_file()

    # The key the editor derives client-side (sha256 of the
    # project-relative path; the export sits at the project root,
    # so the relative path is its file name).
    derived = hashlib.sha256(
        exports[0]["file_name"].encode()
    ).hexdigest()[:20]
    assert derived == plan_document.media_key_for(project_dir, export_path)
    served = client.get(
        f"/api/v1/projects/{project_id}/media/{derived}", headers=AUTH
    )
    assert served.status_code == 200, served.text
    assert served.headers["content-type"].startswith("video/")
    assert len(served.content) == export_path.stat().st_size

    # Uploads resolve through the same recorded-path mechanism.
    clip_bytes = Path(production["clip_a"]).read_bytes()
    uploaded = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        files={"file": ("stage7b-upload.mp4", io.BytesIO(clip_bytes),
                        "video/mp4")},
        headers=AUTH,
    )
    assert uploaded.status_code == 201, uploaded.text
    upload_key = plan_document.media_key_for(
        project_dir, uploaded.json()["file_path"]
    )
    served_upload = client.get(
        f"/api/v1/projects/{project_id}/media/{upload_key}", headers=AUTH
    )
    assert served_upload.status_code == 200, served_upload.text
    assert served_upload.content == clip_bytes

    # Production media keys are unchanged.
    outputs = client.get(
        f"/api/v1/projects/{project_id}/outputs", headers=AUTH
    ).json()
    film = client.get(
        f"/api/v1/projects/{project_id}/media/"
        f"{outputs['film']['media_key']}",
        headers=AUTH,
    )
    assert film.status_code == 200

    # An unrecorded file's key still 404s.
    unknown = hashlib.sha256(b"never-recorded.mp4").hexdigest()[:20]
    missing = client.get(
        f"/api/v1/projects/{project_id}/media/{unknown}", headers=AUTH
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "media_not_found"


def test_media_index_trusts_only_recorded_paths(tmp_path):
    """Unit-level guard check: an export recorded with a path
    OUTSIDE the project directory (traversal) is never indexed,
    and neither is an unrecorded file inside it."""
    from services import plan_document

    project_dir = tmp_path / "proj-unit"
    project_dir.mkdir()
    inside = project_dir / "revision_x_final.mp4"
    inside.write_bytes(b"fake export bytes")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"must never be served")
    stray = project_dir / "stray.mp4"
    stray.write_bytes(b"unrecorded")

    (project_dir / "revision_x.json").write_text(json.dumps({
        "revision_id": "x",
        "exports": [{"file": str(inside), "file_name": inside.name}],
    }))
    (project_dir / "revision_y.json").write_text(json.dumps({
        "revision_id": "y",
        "exports": [{"file": str(outside), "file_name": outside.name}],
    }))
    uploads_dir = project_dir / "uploads"
    uploads_dir.mkdir()
    uploaded_file = uploads_dir / "u1_clip.mp4"
    uploaded_file.write_bytes(b"fake upload bytes")
    (uploads_dir / "index.json").write_text(json.dumps({
        "version": 1,
        "uploads": [{"upload_id": "u1", "file_path": str(uploaded_file)}],
    }))

    index = plan_document.build_media_index("proj-unit", project_dir)
    by_name = {entry["file_name"]: entry for entry in index.values()}
    assert by_name["revision_x_final.mp4"]["kind"] == "revision_export"
    assert by_name["u1_clip.mp4"]["kind"] == "upload"
    assert "outside.mp4" not in by_name
    assert "stray.mp4" not in by_name
    # The client-derived key for the recorded export resolves.
    key = plan_document.media_key_for(project_dir, inside)
    assert index[key]["path"] == inside.resolve()
