"""Localization Studio (founder priority #14; gap-audit doc 01
§4, roadmap Phase L sub-phase 1).

The honesty rules under test (services/localization.py):

- Scope is the audit's sub-phase 1: translated captions +
  translated script, English narration retained and labelled.
  Dubbing is reported unavailable with its recorded reasons (the
  narration adapter's installed voices are English-only); no dub
  route exists.
- Translation runs through llm_client.llm_json in bounded,
  verified batches: no provider -> 503 localization_unavailable;
  an incomplete batch -> 502 with nothing written; a "translation"
  that returns the source text unchanged -> 502, never labelled
  translated. There is no canned translation.
- Timing integrity is proven against real files: captions are
  written through the single caption writer (services.captions)
  and the written .srt is parsed back here — segment count and
  every start/end must equal the source's exactly; only the text
  changes, and English word timings are not carried over.
- Overflow is flagged, never truncated: a translation too dense
  for its unchanged window appears in the artefact's warnings
  with its measured characters-per-second.
- The translated script becomes a real Script Studio draft
  (sections = translated paragraphs) plus the as-generated text
  file; ownership: another user's project is a 404 on every
  localization route.
"""

import json
import re
import uuid
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from database import SessionLocal, engine
from models import Base, Plan, ProductionRun, User, UserSubscription
from services import auth as auth_service
from services import captions as captions_service
from services import llm_client, localization, media_paths

TOKEN = "loc-test-token-7f3a"
PASSWORD = "loc-test-password-7f3a"
HEADERS = {"X-API-Key": TOKEN}

SOURCE_SEGMENTS = [
    {"start": 0.0, "end": 2.5, "text": "On April 26, 1956, a ship changed trade forever.",
     "words": [{"word": "On", "start": 0.0, "end": 0.2}]},
    {"start": 2.5, "end": 6.5, "text": "Malcolm McLean watched his containers loaded at Port Newark.",
     "words": [{"word": "Malcolm", "start": 2.5, "end": 2.9}]},
    # A very short window: any real translation of this line is
    # dense for 0.4s, so the reading-speed warning must fire.
    {"start": 6.5, "end": 6.9, "text": "It worked.",
     "words": [{"word": "It", "start": 6.5, "end": 6.7}]},
]

SCRIPT = (
    "On April 26, 1956, Malcolm McLean loaded 58 containers onto "
    "the Ideal X at Port Newark.\n\n"
    "The ship sailed for Houston, and the age of the container "
    "began."
)


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")  # E-21: explicit opt-in
    monkeypatch.setenv("CINEMORA_COOKIE_SECURE", "0")  # E-21: TestClient speaks plain http; cookie-jar flows need the local-dev opt-out
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    # Deterministic AI availability: no provider unless a test
    # explicitly configures the stub.
    monkeypatch.setattr(llm_client, "BREWKEG_API_KEY", None)
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
                    max_concurrent_runs=2,
                    max_runs_per_period=100,
                    period="month",
                    priority_tier=0,
                    is_default=True,
                )
            )
            db.commit()
        # Hermeticity (shared suite DB): terminate leaked active
        # runs from other suites so plan admission starts clean.
        from datetime import datetime, timezone

        leaked = (
            db.query(ProductionRun)
            .filter(
                ProductionRun.status.in_(
                    ("queued", "running", "awaiting_approval")
                )
            )
            .all()
        )
        for row in leaked:
            row.status = "failed"
            row.error = "test-suite cleanup of a leaked active run"
            row.finished_at = datetime.now(timezone.utc)
        if leaked:
            db.commit()
    finally:
        db.close()
    yield
    auth_service.reset_redis_client()


def _create_project(client, script=SCRIPT):
    response = client.post(
        "/api/v1/projects",
        headers=HEADERS,
        json={
            "title": f"Container Story {uuid.uuid4().hex[:8]}",
            "category": "Industrial",
            "script": script,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _write_source_captions(project_id):
    """Record production captions through the single caption
    writer — the same path that writes a real production's
    captions.json/.srt/.vtt."""
    project_dir = media_paths.project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "language": "en",
        "duration": 6.9,
        "timing_source": "whisper",
        "provisional": False,
        "segments": [dict(segment) for segment in SOURCE_SEGMENTS],
    }
    captions_service.write_edited_captions(data, project_dir / "captions.json")


def _stub_translator(monkeypatch, *, transform=None, drop_indices=()):
    """Configure the llm seam: a deterministic fake translator
    over the real llm_json call shape. Default transform prefixes
    the target language, so translated text always differs from
    the source; tests may override per index."""
    monkeypatch.setattr(llm_client, "BREWKEG_API_KEY", "test-key")
    calls = []

    def fake_llm_json(system, user, **kwargs):
        start = user.index('{"target_language"')
        payload = json.loads(user[start:])
        calls.append(payload)
        translations = []
        for item in payload["items"]:
            if item["index"] in drop_indices:
                continue
            if transform is not None:
                text = transform(item, payload["target_language"])
            else:
                text = f"[{payload['target_language']}] {item['text']}"
            translations.append({"index": item["index"], "text": text})
        return {"translations": translations}

    monkeypatch.setattr(llm_client, "llm_json", fake_llm_json)
    return calls


def _parse_srt(path: Path):
    blocks = []
    text = path.read_text(encoding="utf-8")
    for block in re.split(r"\n\n+", text.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        match = re.match(
            r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)",
            lines[1],
        )
        assert match, lines[1]
        parts = [int(group) for group in match.groups()]
        start = parts[0] * 3600 + parts[1] * 60 + parts[2] + parts[3] / 1000
        end = parts[4] * 3600 + parts[5] * 60 + parts[6] + parts[7] / 1000
        blocks.append(
            {"index": int(lines[0]), "start": start, "end": end,
             "text": "\n".join(lines[2:])}
        )
    return blocks


# ---------------------------------------------------------------------
# Studio view: sources, languages, honest dubbing state
# ---------------------------------------------------------------------


def test_studio_view_reports_sources_languages_and_no_dub(client):
    project = _create_project(client)
    response = client.get(
        f"/api/v1/projects/{project['id']}/localization", headers=HEADERS
    )
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["source_language"] == "en"
    assert view["ai_available"] is False
    assert view["source"]["captions"]["state"] == "not_recorded"
    assert view["source"]["script"]["state"] == "recorded"
    assert view["source"]["script"]["paragraph_count"] == 2
    codes = {entry["code"] for entry in view["languages"]}
    assert {"es", "fr", "de", "ja"} <= codes
    assert "en" not in codes  # English is the source, never a target
    dubbing = view["dubbing"]
    assert dubbing["state"] == "unavailable"
    voice_ids = {voice["voice_id"] for voice in dubbing["voices"]}
    assert voice_ids == {"en_US-lessac-medium", "en_US-ryan-medium"}
    assert "falls back to the English default" in dubbing["reason"]
    assert view["artefacts"] == []


# ---------------------------------------------------------------------
# Caption localisation
# ---------------------------------------------------------------------


def test_translate_captions_refuses_without_provider(client):
    project = _create_project(client)
    _write_source_captions(project["id"])
    response = client.post(
        f"/api/v1/projects/{project['id']}/localization/captions",
        headers=HEADERS,
        json={"language": "fr"},
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "localization_unavailable"
    view = client.get(
        f"/api/v1/projects/{project['id']}/localization", headers=HEADERS
    ).json()
    assert view["artefacts"] == []


def test_translate_captions_without_recorded_captions_is_422(client, monkeypatch):
    _stub_translator(monkeypatch)
    project = _create_project(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/localization/captions",
        headers=HEADERS,
        json={"language": "fr"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "captions_not_recorded"


def test_translate_captions_preserves_timings_through_real_files(client, monkeypatch):
    _stub_translator(monkeypatch)
    project = _create_project(client)
    _write_source_captions(project["id"])
    response = client.post(
        f"/api/v1/projects/{project['id']}/localization/captions",
        headers=HEADERS,
        json={"language": "fr"},
    )
    assert response.status_code == 201, response.text
    artefact = response.json()
    assert artefact["kind"] == "captions"
    assert artefact["language"] == "fr"
    assert artefact["segment_count"] == len(SOURCE_SEGMENTS)
    assert artefact["source"]["kind"] == "production_captions"
    assert sorted(artefact["downloads"]) == ["json", "srt", "vtt"]
    assert all(artefact["files_available"].values())

    artefact_dir = (
        media_paths.project_dir(project["id"])
        / "localization" / artefact["id"]
    )
    document = json.loads(
        (artefact_dir / "captions.json").read_text(encoding="utf-8")
    )
    assert document["language"] == "fr"
    assert document["source_language"] == "en"
    assert document["localization"]["narration_retained"] is True
    segments = document["segments"]
    assert len(segments) == len(SOURCE_SEGMENTS)
    for source, localised in zip(SOURCE_SEGMENTS, segments):
        assert localised["start"] == source["start"]
        assert localised["end"] == source["end"]
        assert localised["text"] == f"[French] {source['text']}"
        # English word timings are not carried into a translation.
        assert "words" not in localised

    # The written .srt parses back through a real SRT reader with
    # identical count and timings.
    blocks = _parse_srt(artefact_dir / "captions.srt")
    assert len(blocks) == len(SOURCE_SEGMENTS)
    for source, block in zip(SOURCE_SEGMENTS, blocks):
        assert block["start"] == pytest.approx(source["start"])
        assert block["end"] == pytest.approx(source["end"])
        assert block["text"] == f"[French] {source['text']}"

    # The short-window segment is flagged for reading speed; the
    # comfortable ones are not. Nothing was truncated.
    warnings = {w["segment_index"]: w for w in artefact["warnings"]}
    assert set(warnings) == {2}
    assert warnings[2]["reason"] == "reading_speed"
    assert warnings[2]["characters_per_second"] > localization.MAX_READING_CPS
    assert segments[2]["text"] == "[French] It worked."

    # Downloads serve the real files.
    download = client.get(
        f"/api/v1/projects/{project['id']}/localization/"
        f"{artefact['id']}/download?kind=srt",
        headers=HEADERS,
    )
    assert download.status_code == 200
    assert "[French] It worked." in download.text


def test_translate_captions_batches_are_bounded(client, monkeypatch):
    calls = _stub_translator(monkeypatch)
    project = _create_project(client)
    many = [
        {"start": float(i * 2), "end": float(i * 2 + 2),
         "text": f"Segment number {i} of the narration."}
        for i in range(60)
    ]
    project_dir = media_paths.project_dir(project["id"])
    project_dir.mkdir(parents=True, exist_ok=True)
    captions_service.write_edited_captions(
        {"language": "en", "duration": 120.0, "segments": many},
        project_dir / "captions.json",
    )
    response = client.post(
        f"/api/v1/projects/{project['id']}/localization/captions",
        headers=HEADERS,
        json={"language": "es"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["segment_count"] == 60
    # 60 segments at the recorded batch bound of 25 = 3 calls.
    assert len(calls) == 3
    assert all(
        len(call["items"]) <= localization.CAPTION_BATCH_SIZE for call in calls
    )


def test_incomplete_translation_batch_is_refused(client, monkeypatch):
    _stub_translator(monkeypatch, drop_indices=(1,))
    project = _create_project(client)
    _write_source_captions(project["id"])
    response = client.post(
        f"/api/v1/projects/{project['id']}/localization/captions",
        headers=HEADERS,
        json={"language": "de"},
    )
    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "localization_failed"
    view = client.get(
        f"/api/v1/projects/{project['id']}/localization", headers=HEADERS
    ).json()
    assert view["artefacts"] == []


def test_source_text_returned_unchanged_is_refused(client, monkeypatch):
    _stub_translator(
        monkeypatch, transform=lambda item, _lang: item["text"]
    )
    project = _create_project(client)
    _write_source_captions(project["id"])
    response = client.post(
        f"/api/v1/projects/{project['id']}/localization/captions",
        headers=HEADERS,
        json={"language": "fr"},
    )
    assert response.status_code == 502, response.text
    assert "not a translation" in response.json()["error"]["message"]
    view = client.get(
        f"/api/v1/projects/{project['id']}/localization", headers=HEADERS
    ).json()
    assert view["artefacts"] == []


def test_unknown_language_is_422(client, monkeypatch):
    _stub_translator(monkeypatch)
    project = _create_project(client)
    _write_source_captions(project["id"])
    response = client.post(
        f"/api/v1/projects/{project['id']}/localization/captions",
        headers=HEADERS,
        json={"language": "xx"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unknown_language"


def test_caption_source_prefers_editor_revision_track(monkeypatch):
    """Source resolution (service level): a non-stale editor
    revision caption track wins over the production captions; a
    stale track does not."""
    from services import editor

    revision_doc = {
        "revision_id": "rev_1",
        "revision_number": 3,
        "tracks": {
            "captions": {
                "stale": False,
                "segments": [
                    {"start": 0.0, "end": 1.5, "text": "Edited caption."}
                ],
            }
        },
    }
    monkeypatch.setattr(
        editor, "latest_revision", lambda pid: ({"id": "rev_1"}, revision_doc)
    )
    source = localization.source_captions("project-x")
    assert source["state"] == "recorded"
    assert source["source"] == "editor_revision"
    assert source["revision_number"] == 3
    assert source["segments"][0]["text"] == "Edited caption."

    revision_doc["tracks"]["captions"]["stale"] = True
    source = localization.source_captions("project-x")
    # Stale track + no production captions on disk for this id.
    assert source["state"] == "not_recorded"


# ---------------------------------------------------------------------
# Script localisation
# ---------------------------------------------------------------------


def test_translate_script_creates_studio_draft_and_record(client, monkeypatch):
    _stub_translator(monkeypatch)
    project = _create_project(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/localization/script",
        headers=HEADERS,
        json={"language": "es"},
    )
    assert response.status_code == 201, response.text
    artefact = response.json()
    assert artefact["kind"] == "script"
    assert artefact["language"] == "es"
    assert artefact["paragraph_count"] == 2
    assert artefact["script_text"].startswith("[Spanish] On April 26, 1956")
    assert artefact["downloads"] == ["txt"]

    # The as-generated text file is real and downloadable.
    artefact_dir = (
        media_paths.project_dir(project["id"])
        / "localization" / artefact["id"]
    )
    assert (artefact_dir / "script.txt").read_text(
        encoding="utf-8"
    ).strip() == artefact["script_text"]
    download = client.get(
        f"/api/v1/projects/{project['id']}/localization/"
        f"{artefact['id']}/download?kind=txt",
        headers=HEADERS,
    )
    assert download.status_code == 200
    assert "[Spanish]" in download.text

    # The draft is a real Script Studio draft whose sections are
    # the translated paragraphs.
    draft = client.get(
        f"/api/v1/script-studio/scripts/{artefact['script_draft_id']}",
        headers=HEADERS,
    )
    assert draft.status_code == 200, draft.text
    bodies = [s["body"] for s in draft.json()["sections"]]
    assert len(bodies) == 2
    assert all(body.startswith("[Spanish]") for body in bodies)
    assert "Spanish script" in draft.json()["title"]


def test_translate_script_refuses_without_provider(client):
    project = _create_project(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/localization/script",
        headers=HEADERS,
        json={"language": "es"},
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "localization_unavailable"


def test_delete_artefact_removes_files_but_keeps_script_draft(client, monkeypatch):
    _stub_translator(monkeypatch)
    project = _create_project(client)
    _write_source_captions(project["id"])
    captions_artefact = client.post(
        f"/api/v1/projects/{project['id']}/localization/captions",
        headers=HEADERS,
        json={"language": "fr"},
    ).json()
    script_artefact = client.post(
        f"/api/v1/projects/{project['id']}/localization/script",
        headers=HEADERS,
        json={"language": "fr"},
    ).json()

    deleted = client.request(
        "DELETE",
        f"/api/v1/projects/{project['id']}/localization/{captions_artefact['id']}",
        headers=HEADERS,
    )
    assert deleted.status_code == 200, deleted.text
    artefact_dir = (
        media_paths.project_dir(project["id"])
        / "localization" / captions_artefact["id"]
    )
    assert not artefact_dir.exists()
    gone = client.get(
        f"/api/v1/projects/{project['id']}/localization/"
        f"{captions_artefact['id']}/download?kind=srt",
        headers=HEADERS,
    )
    assert gone.status_code == 404

    deleted_script = client.request(
        "DELETE",
        f"/api/v1/projects/{project['id']}/localization/{script_artefact['id']}",
        headers=HEADERS,
    )
    assert deleted_script.status_code == 200, deleted_script.text
    assert deleted_script.json()["script_draft_kept"] is True
    draft = client.get(
        f"/api/v1/script-studio/scripts/{script_artefact['script_draft_id']}",
        headers=HEADERS,
    )
    assert draft.status_code == 200  # the user's draft survives


# ---------------------------------------------------------------------
# Ownership: another user's project is a 404 on every route
# ---------------------------------------------------------------------


def test_cross_tenant_404_on_all_localization_routes(client, monkeypatch):
    _stub_translator(monkeypatch)
    project = _create_project(client)
    _write_source_captions(project["id"])
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
    for method, path, kwargs in [
        ("GET", f"/api/v1/projects/{pid}/localization", {}),
        ("POST", f"/api/v1/projects/{pid}/localization/captions",
         {"json": {"language": "fr"}}),
        ("POST", f"/api/v1/projects/{pid}/localization/script",
         {"json": {"language": "fr"}}),
        ("GET", f"/api/v1/projects/{pid}/localization/loc_x/download?kind=srt", {}),
        ("DELETE", f"/api/v1/projects/{pid}/localization/loc_x", {}),
    ]:
        response = client.request(method, path, **kwargs)
        assert response.status_code == 404, (
            method, path, response.status_code, response.text
        )
