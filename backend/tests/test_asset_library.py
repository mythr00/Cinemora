"""Asset Library (founder priority #7; gap-audit Phase E).

Covers services/asset_library.py and its api_v1 routes:

- the library as a VIEW over the ledger: decisions from every
  owned project grouped by content hash, usages carrying their
  verification + rights verbatim, uploads merged in by hash;
- honest edges: records with no resolvable file are counted as
  unkeyed, records whose file is gone stay listed unavailable,
  unknown licences stay unknown, narration is absent (the ledger
  records none);
- browse/search/filters over the recorded fields only;
- folders + tags persisted in the Phase 8 preferences document;
- duplicates surfaced from content hashes with real byte counts;
- storage numbers reconciled against the files on disk (the
  admin_storage walk, owner-filtered);
- cross-project reuse (Phase E acceptance): upload once, import
  into a second project — the target's ledger gains a
  library_reuse decision whose previous_decision_id points at the
  source decision and whose rights are the source's, carried
  verbatim; both projects' revision certificates show the
  provenance;
- Phase 8 ownership: user B gets 404 on user A's assets on reads,
  organisation writes, media and imports alike.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from database import SessionLocal, engine
from models import (
    AssetDecision,
    Base,
    Plan,
    ProductionRun,
    Project,
    User,
    UserPreference,
    UserSubscription,
)
from services import auth as auth_service
from services import media_paths

TOKEN = "test" + "-token"
PASSWORD = "correct" + " horse battery staple"


# ---------------------------------------------------------------------
# Fixtures + seeding
# ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("SOURCE_INTEL_DB", str(tmp_path / "source_intel.db"))
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")  # E-21: window is closed by default; tests opt in explicitly
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(fakeredis.FakeRedis(decode_responses=True))
    db = SessionLocal()
    try:
        for model in (AssetDecision, Project, UserSubscription, User):
            db.query(model).delete()
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


def _register(client, email):
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    # FS-C (FS-1): the Library endpoints are admin-only, so this
    # suite's actors are admin accounts. Library data stays
    # principal-scoped in the service layer, so the cross-tenant
    # 404 assertions below are unchanged in meaning.
    db = SessionLocal()
    try:
        user = db.get(User, body["user"]["id"])
        user.role = "admin"
        db.commit()
    finally:
        db.close()
    return {"id": body["user"]["id"], "session_token": body["session_token"]}


def _bearer(user):
    return {"Authorization": f"Bearer {user['session_token']}"}


@pytest.fixture()
def users(client):
    suffix = uuid.uuid4().hex[:8]
    a = _register(client, f"lib-a-{suffix}@example.com")
    b = _register(client, f"lib-b-{suffix}@example.com")
    return a, b


def _make_clip(path: Path, pattern: str, seconds: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"{pattern}=size=320x240:rate=15",
            "-t", str(seconds), "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, capture_output=True,
    )
    return path


def _make_audio(path: Path, frequency: int = 440, seconds: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"sine=frequency={frequency}:sample_rate=44100",
            "-t", str(seconds), str(path),
        ],
        check=True, capture_output=True,
    )
    return path


def _make_still(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 240), (40, 90, 160)).save(path)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _uid() -> str:
    return str(uuid.uuid4())


RIGHTS_CC_BY = {
    "license": "CC-BY-4.0",
    "license_url": "https://creativecommons.org/licenses/by/4.0/",
    "rights_status": "cleared",
    "creator": "Harbour Archive",
    "source": "archive_org",
    "retrieved_at": "2026-11-20T10:00:00+00:00",
    "title": "Cranes at dawn",
}

RIGHTS_UNKNOWN = {
    "license": "unknown",
    "license_url": "",
    "rights_status": "verify_before_publish",
    "creator": "",
    "source": "user_supplied",
    "retrieved_at": "2026-11-20T10:00:00+00:00",
    "title": "",
}


def _seed_project(owner_id, title, script="A script."):
    db = SessionLocal()
    try:
        project = Project(
            id=_uid(), script=script, title=title,
            category="Documentary", status="completed", owner_id=owner_id,
        )
        db.add(project)
        run = ProductionRun(
            id=_uid(), project_id=project.id, owner_id=owner_id,
            status="completed", stage="completed",
            script_hash=hashlib.sha256(script.encode()).hexdigest(),
        )
        db.add(run)
        db.commit()
        return project.id, run.id
    finally:
        db.close()


def _seed_decision(project_id, run_id, **fields):
    db = SessionLocal()
    try:
        row = AssetDecision(id=_uid(), run_id=run_id, project_id=project_id, **fields)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _write_timeline(project_id, clip_path):
    project_dir = media_paths.project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "timeline.json").write_text(
        json.dumps(
            {
                "scenes": [
                    {
                        "scene_number": 1,
                        "items": [
                            {
                                "sentence_index": 0,
                                "start": 0,
                                "end": 2,
                                "duration": 2,
                                "file_path": str(clip_path),
                                "type": "video",
                                "sentence": "A sentence.",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def library(client, users):
    """User A: projects P1 + P2 sharing one clip (duplicate), plus
    a still, a music decision, a verified-not-picked candidate, an
    upload in P2, and records with no file. User B: project P3 with
    its own clip."""
    a, b = users
    p1, run1 = _seed_project(a["id"], "Harbour Cranes")
    p2, run2 = _seed_project(a["id"], "Depot Story")
    p3, run3 = _seed_project(b["id"], "Bee Keeper")

    dir1 = media_paths.project_dir(p1)
    dir2 = media_paths.project_dir(p2)
    dir3 = media_paths.project_dir(p3)

    clip = _make_clip(dir1 / "clip_a.mp4", "testsrc2")
    clip_copy = dir2 / "clip_a_copy.mp4"
    clip_copy.parent.mkdir(parents=True, exist_ok=True)
    clip_copy.write_bytes(clip.read_bytes())
    still = _make_still(dir1 / "still.png")
    tone = _make_audio(dir1 / "tone.wav")
    alt = _make_clip(dir1 / "alt.mp4", "smptebars")
    upload_clip = _make_clip(dir2 / "uploads" / "upl-1_my_depot.mp4", "testsrc")
    b_clip = _make_clip(dir3 / "bee.mp4", "testsrc2", seconds=3)

    hashes = {
        "clip": _sha256(clip),
        "still": _sha256(still),
        "tone": _sha256(tone),
        "alt": _sha256(alt),
        "upload": _sha256(upload_clip),
        "bee": _sha256(b_clip),
    }

    decisions = {}
    decisions["clip_p1"] = _seed_decision(
        p1, run1,
        asset_key=str(clip), content_hash=hashes["clip"],
        source_url="https://example.org/clip-a", provider="archive_org",
        scene_number=1, sentence_number=1,
        evidence_class="verified_segment",
        verification={"accepted": True, "outcome": "pass"},
        rights=RIGHTS_CC_BY, decision_type="selected",
    )
    decisions["still"] = _seed_decision(
        p1, run1,
        asset_key=str(still), content_hash=hashes["still"],
        provider="wikimedia_commons", scene_number=1, sentence_number=2,
        evidence_class="verified_segment",
        verification={"accepted": True},
        rights={**RIGHTS_UNKNOWN, "source": "wikimedia_commons"},
        decision_type="still",
    )
    decisions["alt"] = _seed_decision(
        p1, run1,
        asset_key=str(alt), content_hash=hashes["alt"],
        source_url="https://example.org/alt", provider="archive_org",
        scene_number=1, sentence_number=3,
        evidence_class="verified_segment",
        verification={"accepted": True},
        rights={
            **RIGHTS_CC_BY,
            "license": "CC0-1.0",
            "title": "Alt angle",
        },
        decision_type="verified_not_picked",
    )
    decisions["tone"] = _seed_decision(
        p1, run1,
        asset_key=str(tone), content_hash=hashes["tone"],
        provider="user_supplied", decision_type="music",
    )
    # Records with no resolvable file and no hash: unkeyed.
    _seed_decision(
        p1, run1, asset_key="candidate-123", content_hash=None,
        provider="serper", verification={"accepted": False},
        decision_type="rejected",
    )
    _seed_decision(p1, run1, asset_key=None, content_hash=None,
                   decision_type="missing")
    # A record whose file is gone: keyed by hash, listed unavailable.
    decisions["gone"] = _seed_decision(
        p1, run1,
        asset_key=str(dir1 / "vanished.mp4"),
        content_hash="f" * 64,
        provider="archive_org", decision_type="selected",
        rights={**RIGHTS_CC_BY, "title": "Vanished clip"},
    )
    decisions["clip_p2"] = _seed_decision(
        p2, run2,
        asset_key=str(clip_copy), content_hash=hashes["clip"],
        source_url="https://example.org/clip-a", provider="archive_org",
        scene_number=1, sentence_number=1,
        evidence_class="verified_segment",
        verification={"accepted": True, "outcome": "pass"},
        rights=RIGHTS_CC_BY, decision_type="selected",
    )
    decisions["bee"] = _seed_decision(
        p3, run3,
        asset_key=str(b_clip), content_hash=hashes["bee"],
        provider="archive_org", verification={"accepted": True},
        rights=RIGHTS_CC_BY, decision_type="selected",
    )

    # P2's upload index: one real upload + one whose file is gone.
    (dir2 / "uploads").mkdir(parents=True, exist_ok=True)
    (dir2 / "uploads" / "index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "uploads": [
                    {
                        "upload_id": "upl-1",
                        "file_path": str(upload_clip),
                        "file_name": upload_clip.name,
                        "type": "video",
                        "source": "user_supplied",
                        "provider": "user_upload",
                        "source_url": None,
                        "title": "My depot footage",
                        "clip_start": 0.0,
                        "source_duration": 2.0,
                        "decision_id": None,
                        "evidence_class": None,
                        "verification": None,
                        "rights": RIGHTS_UNKNOWN,
                        "user_supplied": True,
                        "generated": False,
                        "size_bytes": upload_clip.stat().st_size,
                    },
                    {
                        "upload_id": "upl-gone",
                        "file_path": str(dir2 / "uploads" / "gone.mp4"),
                        "file_name": "gone.mp4",
                        "type": "video",
                        "title": "Lost upload",
                        "rights": RIGHTS_UNKNOWN,
                        "size_bytes": 10,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    _write_timeline(p1, clip)
    _write_timeline(p2, clip_copy)

    return {
        "a": a, "b": b,
        "p1": p1, "p2": p2, "p3": p3,
        "run1": run1, "run2": run2,
        "hashes": hashes, "decisions": decisions,
        "clip_size": clip.stat().st_size,
        "dirs": {"p1": dir1, "p2": dir2, "p3": dir3},
    }


def _items_by_hash(response):
    return {item["content_hash"]: item for item in response.json()["items"]}


# ---------------------------------------------------------------------
# Listing: the ledger view, grouped by content
# ---------------------------------------------------------------------


def test_library_groups_decisions_by_content_hash(client, library):
    response = client.get("/api/v1/library/assets", headers=_bearer(library["a"]))
    assert response.status_code == 200, response.text
    body = response.json()
    items = _items_by_hash(response)
    hashes = library["hashes"]

    clip = items[hashes["clip"]]
    assert clip["usage_count"] == 2  # one usage per project
    assert clip["copies"] == 2 and clip["duplicate"] is True
    assert clip["licence_state"] == "recorded"
    assert clip["rights"]["license"] == "CC-BY-4.0"
    assert clip["title"] == "Cranes at dawn" and clip["title_recorded"] is True
    assert clip["type"] == "video"
    assert {p["id"] for p in clip["projects"]} == {library["p1"], library["p2"]}
    assert clip["media_url"].endswith(f"/library/assets/{hashes['clip']}/media")

    still = items[hashes["still"]]
    assert still["type"] == "image" and still["licence_state"] == "unknown"
    assert still["rights"]["license"] == "unknown"  # never upgraded

    tone = items[hashes["tone"]]
    assert tone["type"] == "audio"
    assert tone["verification_states"] == ["not_recorded"]
    assert tone["rights"] is None  # the music row recorded none

    alt = items[hashes["alt"]]
    assert alt["decision_types"] == ["verified_not_picked"]
    assert alt["title"] == "Alt angle"

    upload = items[hashes["upload"]]
    assert upload["usage_count"] == 0
    assert "upload" in upload["decision_types"]
    assert upload["title"] == "My depot footage"

    gone = items["f" * 64]
    assert gone["available"] is False and gone["media_url"] is None

    # User B's asset is not in user A's library at all.
    assert hashes["bee"] not in items

    summary = body["summary"]
    assert summary["projects_covered"] == 2
    assert summary["unkeyed_records"] == {"decisions": 2, "uploads": 1}


def test_library_search_and_filters(client, library):
    a = _bearer(library["a"])
    hashes = library["hashes"]

    by_query = client.get("/api/v1/library/assets", params={"q": "cranes"}, headers=a)
    assert set(_items_by_hash(by_query)) == {hashes["clip"]}

    by_type = client.get("/api/v1/library/assets", params={"type": "image"}, headers=a)
    assert set(_items_by_hash(by_type)) == {hashes["still"]}

    by_project = client.get(
        "/api/v1/library/assets", params={"project_id": library["p2"]}, headers=a
    )
    assert set(_items_by_hash(by_project)) == {hashes["clip"], hashes["upload"]}

    by_licence = client.get(
        "/api/v1/library/assets", params={"licence": "unknown"}, headers=a
    )
    unknown = _items_by_hash(by_licence)
    assert hashes["still"] in unknown and hashes["tone"] in unknown
    assert hashes["clip"] not in unknown

    by_verification = client.get(
        "/api/v1/library/assets", params={"verification": "accepted"}, headers=a
    )
    accepted = _items_by_hash(by_verification)
    assert hashes["clip"] in accepted and hashes["alt"] in accepted
    assert hashes["tone"] not in accepted

    by_decision = client.get(
        "/api/v1/library/assets",
        params={"decision_type": "verified_not_picked"},
        headers=a,
    )
    assert set(_items_by_hash(by_decision)) == {hashes["alt"]}

    duplicates = client.get(
        "/api/v1/library/assets", params={"duplicates_only": True}, headers=a
    )
    assert set(_items_by_hash(duplicates)) == {hashes["clip"]}

    by_tag_none = client.get(
        "/api/v1/library/assets", params={"tag": "harbour"}, headers=a
    )
    assert by_tag_none.json()["total"] == 0


def test_library_detail_carries_verbatim_provenance(client, library):
    a = _bearer(library["a"])
    digest = library["hashes"]["clip"]
    response = client.get(f"/api/v1/library/assets/{digest}", headers=a)
    assert response.status_code == 200, response.text
    detail = response.json()
    assert len(detail["usages"]) == 2
    for usage in detail["usages"]:
        assert usage["rights"] == RIGHTS_CC_BY
        assert usage["verification"] == {"accepted": True, "outcome": "pass"}
        assert usage["verification_state"] == "accepted"
        assert usage["file_present"] is True
    assert len(detail["copies_detail"]) == 2
    assert detail["duplicate_reclaimable_bytes"] == library["clip_size"]
    assert detail["home_project"]["id"] in {library["p1"], library["p2"]}

    missing = client.get(f"/api/v1/library/assets/{'0' * 64}", headers=a)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "asset_not_found"


# ---------------------------------------------------------------------
# Media serving
# ---------------------------------------------------------------------


def test_library_media_serving_and_missing_file(client, library):
    a = _bearer(library["a"])
    digest = library["hashes"]["clip"]
    response = client.get(f"/api/v1/library/assets/{digest}/media", headers=a)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("video/")
    expected = (library["dirs"]["p1"] / "clip_a.mp4").read_bytes()
    assert response.content == expected

    gone = client.get(f"/api/v1/library/assets/{'f' * 64}/media", headers=a)
    assert gone.status_code == 404
    assert gone.json()["error"]["code"] == "asset_file_missing"

    unknown = client.get(f"/api/v1/library/assets/{'0' * 64}/media", headers=a)
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "asset_not_found"


# ---------------------------------------------------------------------
# Ownership: cross-tenant is 404 everywhere
# ---------------------------------------------------------------------


def test_library_cross_tenant_is_404(client, library):
    b = _bearer(library["b"])
    digest = library["hashes"]["clip"]
    bee = library["hashes"]["bee"]

    listing = client.get("/api/v1/library/assets", headers=b)
    assert set(_items_by_hash(listing)) == {bee}

    assert client.get(f"/api/v1/library/assets/{digest}", headers=b).status_code == 404
    assert client.get(f"/api/v1/library/assets/{digest}/media", headers=b).status_code == 404
    assert client.get(f"/api/v1/library/assets/{bee}/media", headers=b).status_code == 200

    organised = client.put(
        f"/api/v1/library/assets/{digest}/organisation",
        headers=b, json={"tags": ["stolen"]},
    )
    assert organised.status_code == 404

    imported = client.post(
        f"/api/v1/library/assets/{digest}/imports",
        headers=b, json={"project_id": library["p3"]},
    )
    assert imported.status_code == 404

    foreign_target = client.post(
        f"/api/v1/library/assets/{bee}/imports",
        headers=b, json={"project_id": library["p1"]},
    )
    assert foreign_target.status_code == 404
    assert foreign_target.json()["error"]["code"] == "project_not_found"


# ---------------------------------------------------------------------
# Organisation: folders + tags
# ---------------------------------------------------------------------


def test_library_folders_and_tags(client, library):
    a = _bearer(library["a"])
    digest = library["hashes"]["clip"]

    created = client.post("/api/v1/library/folders", headers=a, json={"name": "Harbour"})
    assert created.status_code == 201, created.text
    folder_id = created.json()["id"]

    duplicate = client.post("/api/v1/library/folders", headers=a, json={"name": "harbour"})
    assert duplicate.status_code == 409

    updated = client.put(
        f"/api/v1/library/assets/{digest}/organisation",
        headers=a,
        json={"tags": ["harbour", "hero"], "folder_id": folder_id, "folder_set": True},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["tags"] == ["harbour", "hero"]
    assert updated.json()["folder_id"] == folder_id

    folders = client.get("/api/v1/library/folders", headers=a).json()["folders"]
    assert folders[0]["item_count"] == 1

    in_folder = client.get(
        "/api/v1/library/assets", params={"folder": folder_id}, headers=a
    )
    assert set(_items_by_hash(in_folder)) == {digest}
    unfiled = client.get(
        "/api/v1/library/assets", params={"folder": "unfiled"}, headers=a
    )
    assert digest not in _items_by_hash(unfiled)
    by_tag = client.get(
        "/api/v1/library/assets", params={"tag": "hero"}, headers=a
    )
    assert set(_items_by_hash(by_tag)) == {digest}

    # The organisation lives in the Phase 8 preferences document.
    db = SessionLocal()
    try:
        row = db.get(UserPreference, library["a"]["id"])
        assert row.document["library"]["items"][digest]["tags"] == ["harbour", "hero"]
    finally:
        db.close()

    deleted = client.delete(f"/api/v1/library/folders/{folder_id}", headers=a)
    assert deleted.status_code == 200
    assert deleted.json()["items_unfiled"] == 1
    after = client.get(f"/api/v1/library/assets/{digest}", headers=a).json()
    assert after["folder_id"] is None and after["tags"] == ["harbour", "hero"]

    assert client.delete("/api/v1/library/folders/nope", headers=a).status_code == 404
    bad_folder = client.put(
        f"/api/v1/library/assets/{digest}/organisation",
        headers=a, json={"folder_id": "nope", "folder_set": True},
    )
    assert bad_folder.status_code == 404


# ---------------------------------------------------------------------
# Duplicates + storage
# ---------------------------------------------------------------------


def test_library_duplicates_view(client, library):
    a = _bearer(library["a"])
    response = client.get("/api/v1/library/duplicates", headers=a)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_items"] == 1
    entry = body["duplicates"][0]
    assert entry["content_hash"] == library["hashes"]["clip"]
    assert len(entry["copies"]) == 2
    assert entry["extra_copies"] == 1
    assert entry["reclaimable_bytes"] == library["clip_size"]
    assert body["total_reclaimable_bytes"] == library["clip_size"]


def test_library_storage_reconciles_with_disk(client, library):
    a = _bearer(library["a"])
    response = client.get("/api/v1/library/storage", headers=a)
    assert response.status_code == 200, response.text
    body = response.json()

    def tree_bytes(root: Path) -> int:
        return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())

    expected = {
        pid: tree_bytes(library["dirs"][key])
        for pid, key in ((library["p1"], "p1"), (library["p2"], "p2"))
    }
    projects = {p["project_id"]: p for p in body["projects"]}
    assert set(projects) == {library["p1"], library["p2"]}  # B's project absent
    for pid, size in expected.items():
        assert projects[pid]["bytes"] == size
    assert body["total_bytes"] == sum(expected.values())
    by_type = body["library_by_type"]
    assert by_type["video"]["items"] >= 3  # clip, alt, upload (+ bee excluded)
    assert by_type["image"]["items"] == 1
    assert by_type["audio"]["items"] == 1
    assert body["duplicates"]["reclaimable_bytes"] == library["clip_size"]


# ---------------------------------------------------------------------
# Cross-project reuse: the Phase E acceptance flow
# ---------------------------------------------------------------------


def _revision_certificate(client, headers, project_id):
    editor_view = client.get(f"/api/v1/projects/{project_id}/editor", headers=headers)
    assert editor_view.status_code == 200, editor_view.text
    revision_id = editor_view.json()["revision_id"]
    cert = client.get(
        f"/api/v1/projects/{project_id}/revisions/{revision_id}/certificate",
        headers=headers,
    )
    assert cert.status_code == 200, cert.text
    return cert.json()


def test_import_carries_provenance_into_target_ledger_and_certificate(
    client, library
):
    a = _bearer(library["a"])
    digest = library["hashes"]["alt"]
    source_decision = library["decisions"]["alt"]

    response = client.post(
        f"/api/v1/library/assets/{digest}/imports",
        headers=a, json={"project_id": library["p2"]},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    entry = body["import"]
    assert entry["library_provenance"]["source_decision_id"] == source_decision
    assert entry["library_provenance"]["source_project_id"] == library["p1"]
    assert entry["library_provenance"]["content_hash"] == digest
    assert entry["rights"]["license"] == "CC0-1.0"  # carried verbatim
    assert entry["verification"] == {"accepted": True}
    assert entry["decision_id"]  # the reuse ledger row

    # The target's ledger: one library_reuse row linked to the source.
    db = SessionLocal()
    try:
        rows = (
            db.query(AssetDecision)
            .filter(
                AssetDecision.project_id == library["p2"],
                AssetDecision.decision_type == "library_reuse",
            )
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.previous_decision_id == source_decision
        assert row.content_hash == digest
        assert row.rights["license"] == "CC0-1.0"
        assert row.verification == {"accepted": True}
    finally:
        db.close()

    # The target's per-revision certificate tells the same story.
    certificate = _revision_certificate(client, a, library["p2"])
    assets = certificate["assets"]
    reuse_rows = [r for r in assets if r["decision_type"] == "library_reuse"]
    assert len(reuse_rows) == 1
    assert reuse_rows[0]["previous_decision_id"] == source_decision
    assert reuse_rows[0]["rights"]["license"] == "CC0-1.0"

    # And the library now shows the asset used in both projects.
    detail = client.get(f"/api/v1/library/assets/{digest}", headers=a).json()
    assert detail["usage_count"] == 2
    assert {p["id"] for p in detail["projects"]} == {library["p1"], library["p2"]}


def test_upload_once_use_in_two_projects(client, library, tmp_path):
    """Phase E acceptance, end to end: one upload, two projects,
    provenance + licence visible and correct in both certificates
    (for a user upload the correct licence is 'unknown' — recorded
    as unknown in the home project and carried as unknown)."""
    a = _bearer(library["a"])
    clip = _make_clip(tmp_path / "once.mp4", "smptebars", seconds=2)

    with clip.open("rb") as handle:
        uploaded = client.post(
            "/api/v1/library/uploads",
            headers=a,
            data={"project_id": library["p1"]},
            files={"file": ("once.mp4", handle, "video/mp4")},
        )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    digest = body["content_hash"]
    assert digest == _sha256(clip)
    upload_decision = body["upload"]["decision_id"]
    assert upload_decision

    # It is a library item immediately.
    listing = client.get("/api/v1/library/assets", headers=a)
    assert digest in _items_by_hash(listing)

    imported = client.post(
        f"/api/v1/library/assets/{digest}/imports",
        headers=a, json={"project_id": library["p2"]},
    )
    assert imported.status_code == 201, imported.text
    assert (
        imported.json()["import"]["library_provenance"]["source_decision_id"]
        == upload_decision
    )

    home_cert = _revision_certificate(client, a, library["p1"])
    home_rows = [
        r for r in home_cert["assets"] if r["decision_type"] == "user_supplied"
    ]
    assert len(home_rows) == 1
    assert home_rows[0]["rights"]["license"] == "unknown"

    target_cert = _revision_certificate(client, a, library["p2"])
    reuse_rows = [
        r for r in target_cert["assets"] if r["decision_type"] == "library_reuse"
    ]
    assert len(reuse_rows) == 1
    assert reuse_rows[0]["previous_decision_id"] == upload_decision
    assert reuse_rows[0]["rights"]["license"] == "unknown"


def test_import_contexts_and_failures(client, library):
    a = _bearer(library["a"])
    digest = library["hashes"]["alt"]

    # A brand-new project (no run, no plan) has nothing to attach to.
    created = client.post(
        "/api/v1/projects",
        headers=a,
        json={"title": "Empty", "script": "Nothing yet.", "category": "industrial"},
    )
    assert created.status_code == 201, created.text
    empty_project = created.json()["id"]
    refused = client.post(
        f"/api/v1/library/assets/{digest}/imports",
        headers=a, json={"project_id": empty_project},
    )
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "no_production_context"

    # The vanished file cannot be imported.
    gone = client.post(
        f"/api/v1/library/assets/{'f' * 64}/imports",
        headers=a, json={"project_id": library["p2"]},
    )
    assert gone.status_code == 422
    assert gone.json()["error"]["code"] == "asset_file_missing"

    # A plan-review (pre-run) project accepts the import into its
    # upload index with NO ledger row — the D3 rule uploads follow.
    pre_project, _run = _seed_project(library["a"]["id"], "Awaiting Plan")
    db = SessionLocal()
    try:
        run = db.query(ProductionRun).filter_by(project_id=pre_project).one()
        run.status = "awaiting_approval"
        db.commit()
    finally:
        db.close()
    accepted = client.post(
        f"/api/v1/library/assets/{digest}/imports",
        headers=a, json={"project_id": pre_project},
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["pre_run"] is True
    assert accepted.json()["import"]["decision_id"] is None
    db = SessionLocal()
    try:
        rows = (
            db.query(AssetDecision)
            .filter(
                AssetDecision.project_id == pre_project,
                AssetDecision.decision_type == "library_reuse",
            )
            .all()
        )
        assert rows == []
    finally:
        db.close()
    index = json.loads(
        (media_paths.project_dir(pre_project) / "uploads" / "index.json").read_text()
    )
    assert index["uploads"][0]["library_provenance"]["content_hash"] == digest
