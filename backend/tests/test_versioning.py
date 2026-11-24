"""Versioning (Phase G, founder priority #9) — script version
history with list/diff/restore, the unified version timeline over
the recorded histories, and project snapshots with restore
(gap-audit doc 04 §3.2/§3.3).

The honesty rules under test (services/versioning.py):

- A script version's text exists ONLY where it was recorded: the
  store snapshots the API writes at project creation and run
  submission, or the project's current script field under its own
  hash. A historical version known only by its run-record hash
  reports text_state "not_recorded", refuses restore with 422,
  and never has a diff reconstructed for it.
- Timeline changes come from the records themselves: a run's
  script_changed / options_changed compare its recorded hash and
  options against the previous run's; a revision's changes are
  the op-log entries its parent does not carry.
- A snapshot captures script + plan.json (verbatim) + the
  revision pointer; restore sets the script field, writes the
  plan document back byte-for-byte, and BRANCHES the pointed
  revision — the original chain is untouched and the branch is a
  normal renderable revision.
- Ownership: another user's project is a 404 on every route.
"""

import json
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import jobs
import main
from database import SessionLocal, engine
from models import Base, Plan, Project, User
from services import auth as auth_service
from services import media_paths, run_ledger

TOKEN = "versioning-token"
PASSWORD = "versioning-password-1"
HEADERS = {"X-API-Key": TOKEN}

SCRIPT_A = "Alpha opens the story.\nBravo continues it.\nCharlie closes it."
SCRIPT_B = "Alpha opens the story.\nBravo continues, revised.\nDelta extends it.\nCharlie closes it."


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

    def _fake_enqueue(project_id, script, options=None, run_id=None):
        if run_id is not None:
            return "job-released"
        merged = {"entrypoint": "enqueue_full_pipeline"}
        if isinstance(options, dict):
            merged.update(options)
        submission = run_ledger.submit_production(
            project_id, script, options=merged
        )
        return submission["run_id"] if submission else "job-unrecorded"

    monkeypatch.setattr(jobs, "enqueue_full_pipeline", _fake_enqueue)
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
    finally:
        db.close()
    yield
    auth_service.reset_redis_client()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _create_project(client, script=SCRIPT_A, headers=HEADERS):
    response = client.post(
        "/api/v1/projects", headers=headers, json={"script": script}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _get(client, path, headers=HEADERS):
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _set_project_script(project_id, script):
    db = SessionLocal()
    try:
        row = db.get(Project, project_id)
        row.script = script
        db.commit()
    finally:
        db.close()


def _submit_run_api(client, project_id, options=None):
    response = client.post(
        f"/api/v1/projects/{project_id}/runs",
        headers=HEADERS,
        json={"options": options} if options else None,
    )
    assert response.status_code == 202, response.text
    return response.json()["run"]["run_id"]


def _completed_run(project_id, script, options=None):
    submission = run_ledger.submit_production(
        project_id, script, options=options
    )
    run_id = submission["run_id"]
    run_ledger.claim_run(run_id)
    run_ledger.finish_run(run_id, "completed")
    return run_id


def _entries(view, kind):
    return [e for e in view["entries"] if e["kind"] == kind]


def _write_timeline(project_id):
    timeline = {
        "scenes": [
            {
                "scene_number": 1,
                "items": [
                    {
                        "sentence_index": 0,
                        "start": 0,
                        "end": 4,
                        "duration": 4,
                        "sentence": "Alpha opens the story.",
                        "type": "video",
                        "file_path": "/nonexistent/clip-a.mp4",
                        "assignment": "sentence",
                    },
                    {
                        "sentence_index": 1,
                        "start": 4,
                        "end": 8,
                        "duration": 4,
                        "sentence": "Bravo continues it.",
                        "type": "video",
                        "file_path": "/nonexistent/clip-b.mp4",
                        "assignment": "sentence",
                    },
                ],
            }
        ]
    }
    project_dir = media_paths.project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "timeline.json").write_text(
        json.dumps(timeline), encoding="utf-8"
    )


def _write_plan(project_id, *, title="A Story", sentences=("Alpha opens the story.", "Bravo continues it.")):
    plan = {
        "version": 1,
        "project_id": project_id,
        "analysis_mode": "llm",
        "generated_at": "2026-11-24T09:00:00+00:00",
        "bible": {"title": title, "story_type": "history"},
        "scenes": [{"scene_number": 1, "sentence_numbers": [1, 2]}],
        "sentences": [
            {"sentence_number": i + 1, "text": text}
            for i, text in enumerate(sentences)
        ],
    }
    project_dir = media_paths.project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(plan, indent=2)
    (project_dir / "plan.json").write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------------
# Script versions: recording, listing, honest absence
# ---------------------------------------------------------------------


def test_creation_script_is_the_first_recorded_version(client):
    project = _create_project(client)
    view = _get(client, f"/api/v1/projects/{project['id']}/versions")
    assert view["schema"] == "cinemora.project_versions/1"
    assert view["current_script_hash"] == run_ledger.script_sha256(SCRIPT_A)
    assert len(view["script_versions"]) == 1
    version = view["script_versions"][0]
    assert version["is_current"] is True
    assert version["text_state"] == "recorded"
    assert version["text_source"] == "project_created"
    assert version["char_count"] == len(SCRIPT_A)
    assert version["run_ids"] == []

    detail = _get(
        client,
        f"/api/v1/projects/{project['id']}/versions/scripts/"
        f"{version['script_hash']}",
    )
    assert detail["text"] == SCRIPT_A


def test_submitted_scripts_snapshot_and_diff(client):
    project = _create_project(client)
    _submit_run_api(client, project["id"])
    _set_project_script(project["id"], SCRIPT_B)
    _submit_run_api(client, project["id"])

    view = _get(client, f"/api/v1/projects/{project['id']}/versions")
    versions = view["script_versions"]
    assert len(versions) == 2
    by_hash = {v["script_hash"]: v for v in versions}
    a = by_hash[run_ledger.script_sha256(SCRIPT_A)]
    b = by_hash[run_ledger.script_sha256(SCRIPT_B)]
    # Both texts were recorded (creation + submission snapshots),
    # each version is tied to its run, and B is current.
    assert a["text_state"] == "recorded" and a["run_count"] == 1
    assert b["text_state"] == "recorded" and b["is_current"] is True

    # The timeline's script entry for B carries a REAL diff vs A.
    b_entry = _entries(view, "script")[1]
    diff = b_entry["changes"]["text_diff"]
    assert diff["state"] == "recorded"
    assert diff["added_lines"] == 2
    assert diff["removed_lines"] == 1

    # The compare endpoint produces the unified diff itself.
    compared = _get(
        client,
        f"/api/v1/projects/{project['id']}/versions/compare"
        f"?a=script:{a['script_hash']}&b=script:{b['script_hash']}",
    )
    assert compared["diff"]["kind"] == "script_text"
    assert compared["diff"]["state"] == "recorded"
    assert "+Delta extends it." in compared["diff"]["diff"]
    assert "-Bravo continues it." in compared["diff"]["diff"]


def test_historical_version_hash_only_is_honest(client):
    project = _create_project(client)
    # A run recorded directly in the ledger (the pre-feature data
    # path) for a script that is neither snapshotted nor current.
    old_script = "An older script. Never snapshotted anywhere."
    _completed_run(project["id"], old_script)
    old_hash = run_ledger.script_sha256(old_script)

    view = _get(client, f"/api/v1/projects/{project['id']}/versions")
    by_hash = {v["script_hash"]: v for v in view["script_versions"]}
    assert by_hash[old_hash]["text_state"] == "not_recorded"
    assert by_hash[old_hash]["char_count"] is None
    assert by_hash[old_hash]["run_count"] == 1

    detail = _get(
        client,
        f"/api/v1/projects/{project['id']}/versions/scripts/{old_hash}",
    )
    assert detail["text"] is None

    # Restore refuses: there is no recorded text to restore.
    response = client.post(
        f"/api/v1/projects/{project['id']}/versions/scripts/"
        f"{old_hash}/restore",
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "script_text_not_recorded"

    # And no diff is reconstructed for it.
    current_hash = view["current_script_hash"]
    compared = _get(
        client,
        f"/api/v1/projects/{project['id']}/versions/compare"
        f"?a=script:{old_hash}&b=script:{current_hash}",
    )
    assert compared["diff"]["state"] == "not_recorded"
    assert compared["diff"]["diff"] is None


def test_script_version_restore_sets_project_script(client):
    project = _create_project(client)
    _set_project_script(project["id"], SCRIPT_B)
    a_hash = run_ledger.script_sha256(SCRIPT_A)
    response = client.post(
        f"/api/v1/projects/{project['id']}/versions/scripts/"
        f"{a_hash}/restore",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["script_hash"] == a_hash
    detail = _get(client, f"/api/v1/projects/{project['id']}")
    assert detail["script"] == SCRIPT_A
    view = _get(client, f"/api/v1/projects/{project['id']}/versions")
    assert view["restores"][-1]["kind"] == "script_version"


def test_script_version_unknown_hash_404(client):
    project = _create_project(client)
    missing = "0" * 64
    response = client.get(
        f"/api/v1/projects/{project['id']}/versions/scripts/{missing}",
        headers=HEADERS,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "script_version_not_found"


# ---------------------------------------------------------------------
# Run + revision entries and their recorded changes
# ---------------------------------------------------------------------


def test_run_entries_changes_and_verdicts(client):
    project = _create_project(client)
    first = _completed_run(
        project["id"], SCRIPT_A, options={"voice_id": "voice-1"}
    )
    second = _completed_run(
        project["id"],
        SCRIPT_A,
        options={"voice_id": "voice-2", "music_enabled": True},
    )
    # The second run recorded a production report; the first did not.
    run_dir = media_paths.project_dir(project["id"]) / "runs" / second
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "production_report.json").write_text(
        json.dumps(
            {
                "schema": "cinemora.production_report/1",
                "verdicts": {
                    "integrity": {"verdict": "PASS"},
                    "quality": {"verdict": "QUALITY_WARNING"},
                    "reliability": {"verdict": "UNKNOWN"},
                },
            }
        ),
        encoding="utf-8",
    )

    view = _get(client, f"/api/v1/projects/{project['id']}/versions")
    runs = _entries(view, "run")
    assert [r["detail"]["run_id"] for r in runs] == [first, second]
    assert runs[0]["changes"] is None
    changes = runs[1]["changes"]
    assert changes["vs_entry_id"] == f"run:{first}"
    assert changes["script_changed"] is False
    assert changes["options_changed"] == ["music_enabled", "voice_id"]
    # Verdicts pass through as recorded; absence stays absence.
    assert runs[1]["detail"]["verdicts"]["quality"] == "QUALITY_WARNING"
    assert runs[1]["detail"]["verdicts"]["state"] == "recorded"
    assert runs[0]["detail"]["verdicts"]["state"] == "not_recorded"
    assert runs[0]["detail"]["verdicts"]["quality"] is None

    compared = _get(
        client,
        f"/api/v1/projects/{project['id']}/versions/compare"
        f"?a=run:{first}&b=run:{second}",
    )
    assert compared["diff"]["kind"] == "run_fields"
    assert compared["diff"]["options_changed"] == [
        "music_enabled",
        "voice_id",
    ]
    assert compared["diff"]["verdicts"]["b"]["quality"] == "QUALITY_WARNING"


def test_revision_entries_and_compare(client):
    project = _create_project(client)
    run_id = _completed_run(project["id"], SCRIPT_A)
    _write_timeline(project["id"])

    created = client.post(
        f"/api/v1/projects/{project['id']}/revisions",
        headers=HEADERS,
        json={},
    )
    assert created.status_code == 201, created.text
    revision_zero = created.json()["revision_id"]

    applied = client.post(
        f"/api/v1/projects/{project['id']}/revisions/"
        f"{revision_zero}/operations",
        headers=HEADERS,
        json={"operations": [{"type": "set_volumes", "music": 0.2}]},
    )
    assert applied.status_code == 201, applied.text
    revision_one = applied.json()["revision_id"]

    view = _get(client, f"/api/v1/projects/{project['id']}/versions")
    revisions = _entries(view, "revision")
    assert [r["detail"]["revision_number"] for r in revisions] == [0, 1]
    root_ops = revisions[0]["changes"]["operations_applied"]
    assert [op["type"] for op in root_ops] == ["import_production"]
    one_ops = revisions[1]["changes"]["operations_applied"]
    assert [op["type"] for op in one_ops] == ["set_volumes"]
    assert revisions[1]["changes"]["vs_entry_id"] == (
        f"revision:{revision_zero}"
    )
    assert revisions[1]["detail"]["parent_revision_id"] == revision_zero

    compared = _get(
        client,
        f"/api/v1/projects/{project['id']}/versions/compare"
        f"?a=revision:{revision_zero}&b=revision:{revision_one}",
    )
    diff = compared["diff"]
    assert diff["kind"] == "revision_ops"
    assert diff["common_ancestor_revision_id"] == revision_zero
    assert diff["document_hash"]["equal"] is False
    assert [op["type"] for op in diff["ops_only_in_b"]] == ["set_volumes"]
    assert diff["ops_only_in_a"] == []


def test_compare_cross_kind_is_side_by_side(client):
    project = _create_project(client)
    run_id = _completed_run(project["id"], SCRIPT_A)
    current_hash = run_ledger.script_sha256(SCRIPT_A)
    compared = _get(
        client,
        f"/api/v1/projects/{project['id']}/versions/compare"
        f"?a=run:{run_id}&b=script:{current_hash}",
    )
    assert compared["diff"]["kind"] == "side_by_side"
    assert compared["diff"]["state"] == "not_recorded"
    # Both sides carry the same recorded script hash — that much
    # IS comparable and is reported.
    assert compared["diff"]["script_hash"]["equal"] is True


# ---------------------------------------------------------------------
# Snapshots: capture, restore, delete
# ---------------------------------------------------------------------


def test_snapshot_of_young_project_is_honest(client):
    project = _create_project(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/versions/snapshots",
        headers=HEADERS,
        json={"name": "Fresh project", "note": "nothing produced yet"},
    )
    assert response.status_code == 201, response.text
    snapshot = response.json()
    assert snapshot["script"]["script_hash"] == run_ledger.script_sha256(
        SCRIPT_A
    )
    assert snapshot["plan"]["state"] == "not_recorded"
    assert snapshot["revision"]["state"] == "not_recorded"

    restored = client.post(
        f"/api/v1/projects/{project['id']}/versions/snapshots/"
        f"{snapshot['id']}/restore",
        headers=HEADERS,
    )
    assert restored.status_code == 200, restored.text
    outcome = restored.json()
    assert outcome["script"]["state"] == "restored"
    assert outcome["plan"]["state"] == "not_recorded"
    assert outcome["revision"]["state"] == "not_recorded"
    assert len(outcome["not_restored"]) == 3


def test_snapshot_capture_and_restore_round_trip(client):
    project = _create_project(client)
    run_id = _completed_run(project["id"], SCRIPT_A)
    _write_timeline(project["id"])
    plan_text = _write_plan(project["id"])

    created = client.post(
        f"/api/v1/projects/{project['id']}/revisions",
        headers=HEADERS,
        json={},
    )
    revision_zero = created.json()["revision_id"]

    response = client.post(
        f"/api/v1/projects/{project['id']}/versions/snapshots",
        headers=HEADERS,
        json={"name": "Before the rework"},
    )
    assert response.status_code == 201, response.text
    snapshot = response.json()
    assert snapshot["plan"]["state"] == "recorded"
    assert snapshot["revision"]["state"] == "recorded"
    assert snapshot["revision"]["revision_id"] == revision_zero

    detail = _get(
        client,
        f"/api/v1/projects/{project['id']}/versions/snapshots/"
        f"{snapshot['id']}",
    )
    assert detail["plan_document"]["bible"]["title"] == "A Story"
    assert detail["plan_document"]["sentences"][0]["text"] == (
        "Alpha opens the story."
    )

    # Mutate everything the snapshot holds: script, plan, chain.
    _set_project_script(project["id"], SCRIPT_B)
    _write_plan(
        project["id"],
        title="A Different Story",
        sentences=("Entirely new opening.",),
    )
    applied = client.post(
        f"/api/v1/projects/{project['id']}/revisions/"
        f"{revision_zero}/operations",
        headers=HEADERS,
        json={"operations": [{"type": "set_volumes", "music": 0.2}]},
    )
    revision_one = applied.json()["revision_id"]
    revisions_before = _get(
        client, f"/api/v1/projects/{project['id']}/revisions"
    )["revisions"]
    assert len(revisions_before) == 2

    restored = client.post(
        f"/api/v1/projects/{project['id']}/versions/snapshots/"
        f"{snapshot['id']}/restore",
        headers=HEADERS,
    )
    assert restored.status_code == 200, restored.text
    outcome = restored.json()
    assert outcome["script"]["state"] == "restored"
    assert outcome["plan"]["state"] == "restored"
    assert outcome["revision"]["state"] == "branched"
    assert outcome["revision"]["from_revision_id"] == revision_zero
    branch_id = outcome["revision"]["new_revision_id"]
    assert branch_id not in (revision_zero, revision_one)

    # The script field and plan.json are the recorded originals.
    project_view = _get(client, f"/api/v1/projects/{project['id']}")
    assert project_view["script"] == SCRIPT_A
    plan_now = (
        media_paths.project_dir(project["id"]) / "plan.json"
    ).read_text(encoding="utf-8")
    assert plan_now == plan_text

    # The original chain is untouched; the branch hangs off
    # revision 0 and coexists with revision 1.
    revisions_after = _get(
        client, f"/api/v1/projects/{project['id']}/revisions"
    )["revisions"]
    assert len(revisions_after) == 3
    by_id = {r["revision_id"]: r for r in revisions_after}
    assert by_id[branch_id]["parent_revision_id"] == revision_zero
    assert by_id[revision_one]["parent_revision_id"] == revision_zero
    assert by_id[revision_zero]["parent_revision_id"] is None

    # The restore itself is recorded history.
    view = _get(client, f"/api/v1/projects/{project['id']}/versions")
    assert view["restores"][-1]["kind"] == "snapshot"
    assert view["restores"][-1]["new_revision_id"] == branch_id
    snapshot_entries = _entries(view, "snapshot")
    assert [s["detail"]["name"] for s in snapshot_entries] == [
        "Before the rework"
    ]


def test_snapshot_delete(client):
    project = _create_project(client)
    response = client.post(
        f"/api/v1/projects/{project['id']}/versions/snapshots",
        headers=HEADERS,
        json={"name": "Disposable"},
    )
    snapshot = response.json()
    deleted = client.delete(
        f"/api/v1/projects/{project['id']}/versions/snapshots/"
        f"{snapshot['id']}",
        headers=HEADERS,
    )
    assert deleted.status_code == 200, deleted.text
    view = _get(client, f"/api/v1/projects/{project['id']}/versions")
    assert view["snapshots"] == []
    again = client.delete(
        f"/api/v1/projects/{project['id']}/versions/snapshots/"
        f"{snapshot['id']}",
        headers=HEADERS,
    )
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "snapshot_not_found"


def test_compare_two_snapshots_plan_diff(client):
    project = _create_project(client)
    _write_plan(project["id"])
    first = client.post(
        f"/api/v1/projects/{project['id']}/versions/snapshots",
        headers=HEADERS,
        json={"name": "Plan one"},
    ).json()
    _write_plan(
        project["id"],
        sentences=("Alpha opens the story.", "Bravo continues, revised."),
    )
    second = client.post(
        f"/api/v1/projects/{project['id']}/versions/snapshots",
        headers=HEADERS,
        json={"name": "Plan two"},
    ).json()

    compared = _get(
        client,
        f"/api/v1/projects/{project['id']}/versions/compare"
        f"?a=snapshot:{first['id']}&b=snapshot:{second['id']}",
    )
    diff = compared["diff"]
    assert diff["kind"] == "snapshot_components"
    assert diff["script"]["hash_equal"] is True
    plan_diff = diff["plan"]
    assert plan_diff["state"] == "recorded"
    assert plan_diff["sentence_count"] == {"a": 2, "b": 2}
    assert plan_diff["sentence_text_changes"] == [
        {
            "sentence_number": 2,
            "a_text": "Bravo continues it.",
            "b_text": "Bravo continues, revised.",
        }
    ]


# ---------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------


def test_labels_attach_validate_and_clear(client):
    project = _create_project(client)
    run_id = _completed_run(project["id"], SCRIPT_A)
    entry_id = f"run:{run_id}"

    labelled = client.put(
        f"/api/v1/projects/{project['id']}/versions/labels",
        headers=HEADERS,
        json={
            "entry_id": entry_id,
            "name": "The good take",
            "note": "Passed inspection.",
        },
    )
    assert labelled.status_code == 200, labelled.text
    view = _get(client, f"/api/v1/projects/{project['id']}/versions")
    run_entry = _entries(view, "run")[0]
    assert run_entry["label"]["name"] == "The good take"
    assert run_entry["label"]["note"] == "Passed inspection."

    unknown = client.put(
        f"/api/v1/projects/{project['id']}/versions/labels",
        headers=HEADERS,
        json={"entry_id": "run:does-not-exist", "name": "Nope"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "version_entry_not_found"

    cleared = client.put(
        f"/api/v1/projects/{project['id']}/versions/labels",
        headers=HEADERS,
        json={"entry_id": entry_id, "name": None, "note": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["label"] is None
    view = _get(client, f"/api/v1/projects/{project['id']}/versions")
    assert _entries(view, "run")[0]["label"] is None


# ---------------------------------------------------------------------
# Ownership (Phase 8): cross-tenant is a 404 everywhere.
# ---------------------------------------------------------------------


def _register(client, email):
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(user):
    return {"Authorization": f"Bearer {user['session_token']}"}


def test_versioning_cross_tenant_404(client):
    suffix = uuid.uuid4().hex[:8]
    a = _register(client, f"ver-a-{suffix}@example.com")
    b = _register(client, f"ver-b-{suffix}@example.com")
    project = _create_project(client, headers=_bearer(a))
    run_id = _completed_run(project["id"], SCRIPT_A)
    snapshot = client.post(
        f"/api/v1/projects/{project['id']}/versions/snapshots",
        headers=_bearer(a),
        json={"name": "Owner only"},
    ).json()
    script_hash = run_ledger.script_sha256(SCRIPT_A)

    checks = [
        ("GET", f"/api/v1/projects/{project['id']}/versions"),
        (
            "GET",
            f"/api/v1/projects/{project['id']}/versions/scripts/"
            f"{script_hash}",
        ),
        (
            "GET",
            f"/api/v1/projects/{project['id']}/versions/compare"
            f"?a=run:{run_id}&b=script:{script_hash}",
        ),
        (
            "GET",
            f"/api/v1/projects/{project['id']}/versions/snapshots/"
            f"{snapshot['id']}",
        ),
    ]
    for method, path in checks:
        response = client.request(method, path, headers=_bearer(b))
        assert response.status_code == 404, (method, path, response.text)
    for method, path, payload in (
        (
            "POST",
            f"/api/v1/projects/{project['id']}/versions/snapshots",
            {"name": "Intruder"},
        ),
        (
            "POST",
            f"/api/v1/projects/{project['id']}/versions/snapshots/"
            f"{snapshot['id']}/restore",
            None,
        ),
        (
            "POST",
            f"/api/v1/projects/{project['id']}/versions/scripts/"
            f"{script_hash}/restore",
            None,
        ),
        (
            "PUT",
            f"/api/v1/projects/{project['id']}/versions/labels",
            {"entry_id": f"run:{run_id}", "name": "Intruder"},
        ),
        (
            "DELETE",
            f"/api/v1/projects/{project['id']}/versions/snapshots/"
            f"{snapshot['id']}",
            None,
        ),
    ):
        response = client.request(
            method, path, headers=_bearer(b), json=payload
        )
        assert response.status_code == 404, (method, path, response.text)
    # The owner's data is untouched by the attempts.
    view = _get(
        client,
        f"/api/v1/projects/{project['id']}/versions",
        headers=_bearer(a),
    )
    assert len(view["snapshots"]) == 1
    assert view["restores"] == []
