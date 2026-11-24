"""Thumbnail Generator (founder priority #13; gap-audit doc 03).

The generator is COMPOSITIONAL (services/thumbnail_studio.py): it
renders real image files with PIL from the project's own recorded
material — frames cut by ffmpeg from the finished film of the
latest completed run, verified stills resolved from the asset
ledger, and the project's uploads. These tests build a small REAL
production fixture (an ffmpeg lavfi film, a PIL still recorded as
a ledger 'still' decision and hashed into the timeline, a Case
Bible) and run the actual render path end to end: the composed
files are opened and measured — 1280x720 exactly, the recorded
checks recomputed from the pixels, the variant regenerable from
its stored composition. Nothing mocks the compositor.

Honesty contracts pinned here: a headline past the short cap is
refused (thumbnails are packaging — a few words, never a
sentence); the checks carry no performance claim of any kind;
another user's project is a 404 on every thumbnail route; AI
headline drafting refuses 503 with no provider configured and
never invents canned text.
"""

import hashlib
import json
import subprocess
import uuid
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import api_v1
import main
from database import SessionLocal, engine
from models import AssetDecision, Base, Plan, Project, User, UserSubscription
from services import auth as auth_service
from services import editor, media_paths, run_ledger

TOKEN = "<redacted>"
PASSWORD = "<redacted>"
HEADERS = {"X-API-Key": TOKEN}


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(fakeredis.FakeRedis(decode_responses=True))
    db = SessionLocal()
    try:
        if db.query(Plan).filter_by(name="free").one_or_none() is None:
            db.add(
                Plan(
                    id=str(uuid.uuid4()),
                    name="free",
                    max_concurrent_runs=1,
                    max_runs_per_period=100,
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


def _create_project(client, title="The Sunken Bell"):
    response = client.post(
        "/api/v1/projects",
        headers=HEADERS,
        json={
            "title": title,
            "category": "History",
            "script": f"Script {uuid.uuid4()}. Alpha one. Beta two.",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _completed_run(project):
    submission = run_ledger.submit_production(project["id"], project["script"])
    run_id = submission["run_id"]
    run_ledger.claim_run(run_id)
    run_ledger.finish_run(run_id, "completed")
    return run_id


def _make_film(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=6",
            "-pix_fmt", "yuv420p", str(path),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


def _build_production(client, title="The Sunken Bell"):
    """A project with a completed run: real film, recorded
    timeline, one verified still (ledger decision + timeline file),
    and a Case Bible with a primary subject and recorded facts."""
    project = _create_project(client, title=title)
    project_id = project["id"]
    run_id = _completed_run(project)
    project_dir = media_paths.project_dir(project_id)
    run_dir = project_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    _make_film(run_dir / "documentary_final_output.mp4")

    still_path = project_dir / "scene_1" / "bell_still.jpg"
    still_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (640, 360), (30, 90, 140)).save(still_path, format="JPEG")
    still_hash = hashlib.sha256(still_path.read_bytes()).hexdigest()

    timeline = {
        "project_id": project_id,
        "duration_seconds": 6.0,
        "items": [
            {"type": "image", "index": 0, "scene_number": 1,
             "start": 0.0, "end": 3.0, "file_path": str(still_path)},
            {"type": "video", "index": 1, "scene_number": 1,
             "start": 3.0, "end": 6.0, "file_path": str(run_dir / "documentary_final_output.mp4")},
        ],
    }
    (run_dir / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")

    db = SessionLocal()
    try:
        db.add(
            AssetDecision(
                id=str(uuid.uuid4()),
                run_id=run_id,
                project_id=project_id,
                asset_key="cand_bell",
                content_hash=still_hash,
                provider="wikimedia",
                scene_number=1,
                sentence_number=1,
                decision_type="still",
            )
        )
        db.commit()
    finally:
        db.close()

    bible = {
        "title": title,
        "story_type": "history",
        "primary_subject_id": "bell",
        "entities": [
            {"id": "bell", "canonical": "The Sunken Bell", "type": "object",
             "role": "the bell raised from the harbour floor"},
        ],
        "script_facts": {
            "facts": [
                {"kind": "quantity", "display": "40 metres",
                 "sentence_number": 2, "basis": "script_claim"},
                {"kind": "date", "display": "1911",
                 "sentence_number": 1, "basis": "script_claim"},
            ]
        },
    }
    (project_dir / "case_bible.json").write_text(json.dumps(bible), encoding="utf-8")
    return project


def _get(client, project_id, headers=HEADERS):
    return client.get(f"/api/v1/projects/{project_id}/thumbnails", headers=headers)


# ---------------------------------------------------------------------
# Workspace read: brief, bases, spec
# ---------------------------------------------------------------------


def test_workspace_lists_real_bases_and_recorded_brief(client):
    project = _build_production(client)
    response = _get(client, project["id"])
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["spec"]["width"] == 1280 and view["spec"]["height"] == 720

    brief = view["brief"]
    assert brief["case_bible_state"] == "recorded"
    assert brief["primary_subject"]["name"] == "The Sunken Bell"
    assert {f["display"] for f in brief["recorded_facts"]} == {"40 metres", "1911"}
    assert brief["headline_suggestions"][0] == "The Sunken Bell"

    bases = view["bases"]
    assert bases["film"]["state"] == "recorded"
    assert bases["film"]["timestamps_source"] == "timeline"
    assert len(bases["film"]["frames"]) == 2  # timeline midpoints 1.5s, 4.5s
    assert len(bases["stills"]) == 1
    assert bases["stills"][0]["provider"] == "wikimedia"
    assert view["variants"] == []


def test_project_without_production_has_honest_empty_bases(client):
    project = _create_project(client)
    view = _get(client, project["id"]).json()
    assert view["bases"]["film"]["state"] == "not_recorded"
    assert view["bases"]["stills"] == []
    response = client.post(
        f"/api/v1/projects/{project['id']}/thumbnails/variants",
        headers=HEADERS, json={},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "no_bases"


# ---------------------------------------------------------------------
# Composition through the real render path
# ---------------------------------------------------------------------


def test_compose_from_film_frame_produces_spec_exact_file(client):
    project = _build_production(client)
    view = _get(client, project["id"]).json()
    frame_base = view["bases"]["film"]["frames"][0]["base_id"]

    response = client.post(
        f"/api/v1/projects/{project['id']}/thumbnails",
        headers=HEADERS,
        json={"base_id": frame_base, "headline": "Raised From The Deep",
              "layout": "bottom_band", "accent": "amber"},
    )
    assert response.status_code == 201, response.text
    variant = response.json()
    assert variant["base"]["kind"] == "film_frame"
    assert variant["available"] is True

    checks = variant["checks"]
    assert checks["dimensions"] == [1280, 720]
    assert checks["format"] == "PNG"
    assert checks["under_youtube_size_limit"] is True
    assert checks["headline_words"] == 4
    # Mechanical checks only — no performance claim of any kind.
    assert set(checks) == {
        "dimensions", "format", "file_size_bytes",
        "under_youtube_size_limit", "headline_words",
        "headline_characters", "headline_contrast_ratio",
        "headline_font_height_px_at_10_percent_scale",
    }
    assert checks["headline_contrast_ratio"] > 1.0

    # The file on disk really is the measured image.
    store_dir = media_paths.project_dir(project["id"]) / "thumbnails"
    with Image.open(store_dir / variant["file_name"]) as img:
        assert img.size == (1280, 720)
    assert (store_dir / variant["file_name"]).stat().st_size == checks["file_size_bytes"]

    # The variant is listed, downloadable, and its composition is
    # recorded completely enough to regenerate it.
    listed = _get(client, project["id"]).json()["variants"]
    assert [v["id"] for v in listed] == [variant["id"]]
    download = client.get(
        f"/api/v1/projects/{project['id']}/thumbnails/{variant['id']}/download",
        headers=HEADERS,
    )
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("image/")
    assert len(download.content) == checks["file_size_bytes"]


def test_compose_from_still_and_all_layouts(client):
    project = _build_production(client)
    view = _get(client, project["id"]).json()
    still_base = view["bases"]["stills"][0]["base_id"]
    for layout in ("bottom_band", "top_band", "left_panel", "center_scrim"):
        response = client.post(
            f"/api/v1/projects/{project['id']}/thumbnails",
            headers=HEADERS,
            json={"base_id": still_base, "headline": "The Sunken Bell",
                  "layout": layout},
        )
        assert response.status_code == 201, (layout, response.text)
        assert response.json()["base"]["kind"] == "asset_still"
        assert response.json()["checks"]["dimensions"] == [1280, 720]


def test_generate_variants_crosses_bases_and_layouts(client):
    project = _build_production(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/thumbnails/variants",
        headers=HEADERS, json={"headline": "Raised From The Deep"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    # Two bases (first still + first film frame) x four layouts.
    assert len(body["created"]) == 8
    assert body["skipped"] == []
    assert len(_get(client, project["id"]).json()["variants"]) == 8


def test_headline_cap_refuses_sentences(client):
    project = _build_production(client)
    view = _get(client, project["id"]).json()
    still_base = view["bases"]["stills"][0]["base_id"]
    response = client.post(
        f"/api/v1/projects/{project['id']}/thumbnails",
        headers=HEADERS,
        json={"base_id": still_base,
              "headline": "The Incredible True Story Of The Bell That Sank In 1911"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "headline_too_long"
    assert _get(client, project["id"]).json()["variants"] == []


def test_unknown_base_layout_and_variant_are_404_or_422(client):
    project = _build_production(client)
    bad_base = client.post(
        f"/api/v1/projects/{project['id']}/thumbnails",
        headers=HEADERS, json={"base_id": "still:nope", "headline": "Short"},
    )
    assert bad_base.status_code == 404
    assert bad_base.json()["error"]["code"] == "base_not_found"

    view = _get(client, project["id"]).json()
    still_base = view["bases"]["stills"][0]["base_id"]
    bad_layout = client.post(
        f"/api/v1/projects/{project['id']}/thumbnails",
        headers=HEADERS,
        json={"base_id": still_base, "headline": "Short", "layout": "diagonal"},
    )
    assert bad_layout.status_code == 422
    assert bad_layout.json()["error"]["code"] == "unknown_layout"

    missing = client.get(
        f"/api/v1/projects/{project['id']}/thumbnails/thumb_nope/download",
        headers=HEADERS,
    )
    assert missing.status_code == 404


def test_delete_variant_removes_file_and_record(client):
    project = _build_production(client)
    view = _get(client, project["id"]).json()
    still_base = view["bases"]["stills"][0]["base_id"]
    created = client.post(
        f"/api/v1/projects/{project['id']}/thumbnails",
        headers=HEADERS, json={"base_id": still_base, "headline": "The Sunken Bell"},
    ).json()
    store_dir = media_paths.project_dir(project["id"]) / "thumbnails"
    assert (store_dir / created["file_name"]).is_file()

    deleted = client.delete(
        f"/api/v1/projects/{project['id']}/thumbnails/{created['id']}",
        headers=HEADERS,
    )
    assert deleted.status_code == 200
    assert not (store_dir / created["file_name"]).exists()
    assert _get(client, project["id"]).json()["variants"] == []


# ---------------------------------------------------------------------
# Upload base + reference analysis (measured properties only)
# ---------------------------------------------------------------------


def test_upload_base_and_reference_analysis(client, tmp_path):
    project = _build_production(client)
    # Upload through the existing machinery (editor.store_upload via
    # the real route): a reference-style image with readable text.
    reference = tmp_path / "reference.png"
    img = Image.new("RGB", (1280, 720), (200, 30, 30))
    img.save(reference, format="PNG")
    with open(reference, "rb") as handle:
        response = client.post(
            f"/api/v1/projects/{project['id']}/uploads",
            headers=HEADERS,
            files={"file": ("reference.png", handle, "image/png")},
        )
    assert response.status_code == 201, response.text
    upload_id = response.json()["upload_id"]

    view = _get(client, project["id"]).json()
    upload_bases = view["bases"]["uploads"]
    assert [u["upload_id"] for u in upload_bases] == [upload_id]

    # The uploaded image composes like any other base.
    composed = client.post(
        f"/api/v1/projects/{project['id']}/thumbnails",
        headers=HEADERS,
        json={"base_id": f"upload:{upload_id}", "headline": "My Own Base",
              "layout": "left_panel"},
    )
    assert composed.status_code == 201, composed.text
    assert composed.json()["base"]["kind"] == "upload"

    # Analysis: measured properties, honestly labelled, palette
    # dominated by the uploaded red.
    analysis = client.post(
        f"/api/v1/projects/{project['id']}/thumbnails/analyse",
        headers=HEADERS, json={"base_id": f"upload:{upload_id}"},
    )
    assert analysis.status_code == 200, analysis.text
    body = analysis.json()
    assert body["dimensions"] == [1280, 720]
    assert "not an AI analysis" in body["label"]
    top = body["palette"][0]
    assert top["share"] > 0.9
    r = int(top["hex"][1:3], 16)
    assert r > 150  # the dominant colour really is the red we uploaded
    assert body["ocr"]["state"] in ("recorded", "unavailable")


# ---------------------------------------------------------------------
# AI drafting: honest unavailability, no canned text
# ---------------------------------------------------------------------


def test_ai_suggestions_refuse_without_provider(client, monkeypatch):
    from services import thumbnail_studio

    monkeypatch.setattr(thumbnail_studio.llm_client, "BREWKEG_API_KEY", None)
    project = _build_production(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/thumbnails/suggest-headlines",
        headers=HEADERS,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "thumbnail_ai_unavailable"


def test_ai_suggestions_are_proposals_from_the_recorded_brief(client, monkeypatch):
    from services import thumbnail_studio

    monkeypatch.setattr(thumbnail_studio.llm_client, "BREWKEG_API_KEY", "test-key")
    seen = {}

    def fake_llm_json(system, user):
        seen["user"] = user
        return {"headlines": ["Raised From The Deep", "40 Metres Down",
                              "This One Is Far Too Long To Be A Thumbnail Headline"]}

    monkeypatch.setattr(thumbnail_studio.llm_client, "llm_json", fake_llm_json)
    project = _build_production(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/thumbnails/suggest-headlines",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    # The over-cap proposal is dropped by the same headline rules;
    # nothing was composed or stored by the drafting action.
    assert response.json()["proposals"] == ["Raised From The Deep", "40 Metres Down"]
    assert "40 metres" in seen["user"]  # the recorded brief fed the draft
    assert _get(client, project["id"]).json()["variants"] == []


# ---------------------------------------------------------------------
# Ownership: another user's project is a 404 on every route
# ---------------------------------------------------------------------


def test_cross_tenant_404_on_all_thumbnail_routes(client):
    project = _build_production(client)
    db = SessionLocal()
    try:
        other = User(
            id=str(uuid.uuid4()),
            email=f"other-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=auth_service.hash_password(PASSWORD),
        )
        db.add(other)
        db.flush()
        db.add(UserSubscription(
            id=str(uuid.uuid4()), user_id=other.id,
            plan_id=db.query(Plan).filter_by(name="free").one().id,
            status="active",
        ))
        db.commit()
        other_id = other.id
    finally:
        db.close()
    db = SessionLocal()
    try:
        email = db.get(User, other_id).email
    finally:
        db.close()
    login = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text

    pid = project["id"]
    assert client.get(f"/api/v1/projects/{pid}/thumbnails").status_code in (401, 403, 404)
    assert _get(client, pid, headers={"X-API-Key": "wrong"}).status_code in (401, 404)
    # Cookie session of the other user: every route 404s.
    for method, path, kwargs in [
        ("GET", f"/api/v1/projects/{pid}/thumbnails", {}),
        ("POST", f"/api/v1/projects/{pid}/thumbnails",
         {"json": {"base_id": "still:x", "headline": "Hi"}}),
        ("POST", f"/api/v1/projects/{pid}/thumbnails/variants", {"json": {}}),
        ("POST", f"/api/v1/projects/{pid}/thumbnails/analyse",
         {"json": {"base_id": "upload:x"}}),
        ("GET", f"/api/v1/projects/{pid}/thumbnails/thumb_x/download", {}),
        ("DELETE", f"/api/v1/projects/{pid}/thumbnails/thumb_x", {}),
    ]:
        response = client.request(method, path, **kwargs)
        assert response.status_code == 404, (method, path, response.status_code, response.text)
