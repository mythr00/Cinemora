"""Templates & production presets (founder priority #15;
gap-audit doc 01 §5, doc 06 Phase M).

The honesty rules under test (services/templates.py):

- Built-ins are DERIVED from the real genre packs: one per pack,
  the pack label as category, the pack id as the genre option,
  provenance naming the pack document. They are read-only —
  editing or deleting one refuses; copying one makes an editable
  user template.
- The field audit is enforced at save time: an option key the
  pipeline does not consume is refused (unknown_option), the
  retired narration_speed is refused by name (retired_option —
  founder ruling 2026-11-22), and invalid values/ranges refuse
  with the creation model's own bounds.
- Save-from-project FILTERS the project's stored options through
  the audit and reports every dropped key with its reason;
  save-from-draft copies structure only — headings, never the
  draft's narration bodies.
- Applying a template goes through the normal creation paths:
  apply-project creates a real project (via ProjectCreateIn +
  the shared creation helper) carrying the template's category
  and options; apply-draft creates a real Script Studio draft.
- Ownership: user templates live in the caller's preferences
  document, so another user's template id is a 404 on every
  route and never appears in their list.
"""

import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import jobs
import main
from database import SessionLocal, engine
from models import Base, Plan, ProductionRun, Project
from services import auth as auth_service
from services import genre_packs, llm_client, run_ledger

TOKEN = "templates-test-token"
PASSWORD = "templates-test-password"

SCRIPT_BODY = (
    "On April 26, 1956, Malcolm McLean loaded 58 containers onto "
    "the Ideal X at Port Newark. The ship sailed for Houston."
)


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")  # E-21: explicit opt-in
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setattr(llm_client, "BREWKEG_API_KEY", None)
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


def _register(client, email):
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(user):
    return {"Authorization": f"Bearer {user['session_token']}"}


@pytest.fixture()
def users(client):
    suffix = uuid.uuid4().hex[:8]
    a = _register(client, f"tpl-a-{suffix}@example.com")
    b = _register(client, f"tpl-b-{suffix}@example.com")
    return a, b


def _create_template(client, headers, **overrides):
    payload = {
        "name": "Container house style",
        "category": "Industrial",
        "target_minutes": 12,
        "options": {
            "genre": "industrial",
            "voice_id": "default",
            "captions": "burn",
            "caption_sync": "word",
            "music_enabled": False,
            "music_volume": 0.5,
            "narration_volume": 1.0,
            "duck_ratio": 12,
            "require_plan_approval": True,
        },
        "sections": [{"heading": "The sailing"}, {"heading": "Aftermath"}],
    }
    payload.update(overrides)
    response = client.post("/api/v1/templates", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------
# Built-ins: derived from the real packs, read-only
# ---------------------------------------------------------------------


def test_builtins_are_derived_from_the_real_packs(client, users):
    a, _ = users
    response = client.get("/api/v1/templates", headers=_bearer(a))
    assert response.status_code == 200, response.text
    builtins = response.json()["builtins"]
    packs = [p for p in genre_packs.available_packs() if p]
    assert [b["id"] for b in builtins] == [f"builtin:{p['id']}" for p in packs]
    for view, pack in zip(builtins, packs):
        assert view["kind"] == "builtin"
        assert view["name"] == pack["label"]
        assert view["category"] == pack["label"]
        assert view["options"] == {"genre": pack["id"]}
        assert view["source"]["kind"] == "genre_pack"
        assert view["source"]["pack_id"] == pack["id"]
        # Packs record no script structure; built-ins invent none.
        assert view["sections"] == []


def test_builtin_get_and_read_only_guards(client, users):
    a, _ = users
    response = client.get("/api/v1/templates/builtin:science", headers=_bearer(a))
    assert response.status_code == 200, response.text
    assert response.json()["options"] == {"genre": "science"}

    response = client.patch(
        "/api/v1/templates/builtin:science",
        json={"name": "Hacked"},
        headers=_bearer(a),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "template_builtin_read_only"

    response = client.delete(
        "/api/v1/templates/builtin:science", headers=_bearer(a)
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "template_builtin_read_only"

    response = client.get("/api/v1/templates/builtin:western", headers=_bearer(a))
    assert response.status_code == 404, response.text


def test_copy_builtin_makes_an_editable_user_template(client, users):
    a, _ = users
    response = client.post(
        "/api/v1/templates/builtins/biography/copy",
        json={"name": "Life stories, my way"},
        headers=_bearer(a),
    )
    assert response.status_code == 201, response.text
    copied = response.json()
    assert copied["kind"] == "user"
    assert copied["source"] == {"kind": "builtin_copy", "pack_id": "biography"}
    assert copied["options"] == {"genre": "biography"}

    response = client.patch(
        f"/api/v1/templates/{copied['id']}",
        json={"options": {"genre": "biography", "captions": "off"}},
        headers=_bearer(a),
    )
    assert response.status_code == 200, response.text
    assert response.json()["options"]["captions"] == "off"

    response = client.post(
        "/api/v1/templates/builtins/western/copy", json={}, headers=_bearer(a)
    )
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------
# CRUD + the preset round trip (Phase M acceptance)
# ---------------------------------------------------------------------


def test_user_template_round_trip(client, users):
    a, _ = users
    created = _create_template(client, _bearer(a))
    assert created["kind"] == "user"
    assert created["source"] == {"kind": "manual"}
    assert created["sections"] == [
        {"heading": "The sailing"}, {"heading": "Aftermath"},
    ]

    response = client.get(
        f"/api/v1/templates/{created['id']}", headers=_bearer(a)
    )
    assert response.status_code == 200, response.text
    assert response.json() == created

    response = client.patch(
        f"/api/v1/templates/{created['id']}",
        json={"name": "Renamed", "clear_target": True},
        headers=_bearer(a),
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed"
    assert response.json()["target_minutes"] is None
    # Untouched fields survive a partial update.
    assert response.json()["options"]["genre"] == "industrial"

    response = client.get("/api/v1/templates", headers=_bearer(a))
    ids = [t["id"] for t in response.json()["templates"]]
    assert created["id"] in ids

    response = client.delete(
        f"/api/v1/templates/{created['id']}", headers=_bearer(a)
    )
    assert response.status_code == 200, response.text
    response = client.get(
        f"/api/v1/templates/{created['id']}", headers=_bearer(a)
    )
    assert response.status_code == 404, response.text


def test_template_cross_tenant_is_404_everywhere(client, users):
    a, b = users
    created = _create_template(client, _bearer(a))
    for method, url, kwargs in (
        ("GET", f"/api/v1/templates/{created['id']}", {}),
        ("PATCH", f"/api/v1/templates/{created['id']}", {"json": {"name": "X"}}),
        ("DELETE", f"/api/v1/templates/{created['id']}", {}),
        ("POST", f"/api/v1/templates/{created['id']}/apply-draft", {"json": {}}),
        (
            "POST",
            f"/api/v1/templates/{created['id']}/apply-project",
            {"json": {"script": SCRIPT_BODY}},
        ),
    ):
        response = client.request(method, url, headers=_bearer(b), **kwargs)
        assert response.status_code == 404, (method, url, response.text)
    response = client.get("/api/v1/templates", headers=_bearer(b))
    assert all(
        t["id"] != created["id"] for t in response.json()["templates"]
    )


# ---------------------------------------------------------------------
# The field audit
# ---------------------------------------------------------------------


def test_unknown_and_retired_option_keys_are_refused(client, users):
    a, _ = users
    response = client.post(
        "/api/v1/templates",
        json={"name": "Bad", "options": {"aspect_ratio": "16:9"}},
        headers=_bearer(a),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unknown_option"

    # The founder-retired speed setting is refused BY NAME — a
    # template can never reintroduce narration speed control.
    response = client.post(
        "/api/v1/templates",
        json={"name": "Bad", "options": {"narration_speed": 0.94}},
        headers=_bearer(a),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "retired_option"

    response = client.post(
        "/api/v1/templates",
        json={"name": "Bad", "options": {"music": True}},
        headers=_bearer(a),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "retired_option"


def test_option_values_are_validated_with_creation_bounds(client, users):
    a, _ = users
    for options, code in (
        ({"captions": "loud"}, "invalid_option_value"),
        ({"caption_sync": "karaoke"}, "invalid_option_value"),
        ({"music_volume": 2.5}, "invalid_option_value"),
        ({"duck_threshold": 0}, "invalid_option_value"),
        ({"duck_ratio": 0.5}, "invalid_option_value"),
        ({"music_enabled": "yes"}, "invalid_option_value"),
        ({"genre": "western"}, "unknown_genre"),
    ):
        response = client.post(
            "/api/v1/templates",
            json={"name": "Bad", "options": options},
            headers=_bearer(a),
        )
        assert response.status_code == 422, (options, response.text)
        assert response.json()["error"]["code"] == code, options


# ---------------------------------------------------------------------
# Save from a project / from a draft
# ---------------------------------------------------------------------


def test_template_from_project_filters_and_reports_drops(client, users):
    a, _ = users
    response = client.post(
        "/api/v1/projects",
        json={
            "title": "Source project",
            "script": SCRIPT_BODY,
            "category": "Industrial",
            "options": {
                "genre": "industrial",
                "voice_id": "default",
                "captions": "off",
                "snapshot_extra": "recorded but consumed by nothing",
                "narration_speed": 0.94,
            },
        },
        headers=_bearer(a),
    )
    assert response.status_code == 201, response.text
    project = response.json()

    response = client.post(
        "/api/v1/templates/from-project",
        json={"project_id": project["id"]},
        headers=_bearer(a),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    template = body["template"]
    assert template["source"] == {"kind": "project", "project_id": project["id"]}
    assert template["category"] == "Industrial"
    # require_plan_approval: False rides along because the
    # creation path itself stores the model's default — and it IS
    # a consumed key, so the filter keeps it like any other.
    assert template["options"] == {
        "genre": "industrial", "voice_id": "default", "captions": "off",
        "require_plan_approval": False,
    }
    dropped = {d["key"]: d["reason"] for d in body["dropped_options"]}
    assert dropped == {
        "snapshot_extra": "not_consumed_by_pipeline",
        "narration_speed": "retired_option",
    }
    # A project records no section structure; none is invented.
    assert template["sections"] == []

    # Another user's project is a 404, never a template source.
    _, b = users
    response = client.post(
        "/api/v1/templates/from-project",
        json={"project_id": project["id"]},
        headers=_bearer(b),
    )
    assert response.status_code == 404, response.text


def test_template_from_draft_copies_structure_only(client, users):
    a, b = users
    response = client.post(
        "/api/v1/script-studio/scripts",
        json={
            "title": "Drafty",
            "category": "history",
            "target_minutes": 20,
            "sections": [
                {"heading": "Origins", "body": "Secret narration text."},
                {"heading": "The turn", "body": "More secret text."},
            ],
        },
        headers=_bearer(a),
    )
    assert response.status_code == 201, response.text
    draft = response.json()

    response = client.post(
        "/api/v1/templates/from-draft",
        json={"script_id": draft["id"]},
        headers=_bearer(a),
    )
    assert response.status_code == 201, response.text
    template = response.json()
    assert template["source"] == {"kind": "script_draft", "script_id": draft["id"]}
    assert template["category"] == "history"
    assert template["target_minutes"] == 20
    # Headings only — the draft's bodies are the author's script
    # and are never copied into a template.
    assert template["sections"] == [
        {"heading": "Origins"}, {"heading": "The turn"},
    ]

    response = client.post(
        "/api/v1/templates/from-draft",
        json={"script_id": draft["id"]},
        headers=_bearer(b),
    )
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------
# Apply: through the normal creation paths
# ---------------------------------------------------------------------


def test_apply_to_draft_creates_a_real_studio_draft(client, users):
    a, _ = users
    created = _create_template(client, _bearer(a))
    response = client.post(
        f"/api/v1/templates/{created['id']}/apply-draft",
        json={},
        headers=_bearer(a),
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["category"] == "Industrial"
    assert draft["target_minutes"] == 12
    assert [s["heading"] for s in draft["sections"]] == [
        "The sailing", "Aftermath",
    ]
    assert all(s["body"] == "" for s in draft["sections"])

    # A built-in applies too: pack label as category, no sections
    # (the studio's own default opening section applies).
    response = client.post(
        "/api/v1/templates/builtin:science/apply-draft",
        json={},
        headers=_bearer(a),
    )
    assert response.status_code == 201, response.text
    assert response.json()["category"] == "Science"


def test_apply_to_project_uses_the_creation_path(client, users):
    a, _ = users
    created = _create_template(client, _bearer(a))
    response = client.post(
        f"/api/v1/templates/{created['id']}/apply-project",
        json={"title": "From template", "script": SCRIPT_BODY},
        headers=_bearer(a),
    )
    assert response.status_code == 201, response.text
    project = response.json()
    assert project["title"] == "From template"
    assert project["category"] == "Industrial"
    assert project["script"] == SCRIPT_BODY
    # The template's bundle is on the project row, exactly as a
    # manual creation with the same options would store it.
    assert project["options"]["genre"] == "industrial"
    assert project["options"]["captions"] == "burn"
    assert project["options"]["require_plan_approval"] is True
    assert "narration_speed" not in (project["options"] or {})

    db = SessionLocal()
    try:
        row = db.get(Project, project["id"])
        assert row is not None and row.owner_id is not None
    finally:
        db.close()

    # Built-in application: the pack's category + genre option.
    response = client.post(
        "/api/v1/templates/builtin:biography/apply-project",
        json={"script": SCRIPT_BODY},
        headers=_bearer(a),
    )
    assert response.status_code == 201, response.text
    assert response.json()["category"] == "Biography"
    # Exactly what manual creation with genre=biography stores:
    # the bundle plus the creation model's own False default.
    assert response.json()["options"] == {
        "genre": "biography", "require_plan_approval": False,
    }
