"""YouTube Script Studio (founder priority #12; gap-audit doc 02).

The honesty rules under test (services/script_studio.py):

- Metrics are arithmetic over the text at the codebase's recorded
  rate (145 wpm, the create page's number): word counts, sentence
  counts via the pipeline's own splitter, target budgets.
- AI actions are proposals through llm_client only: with no
  provider configured they refuse 503 script_ai_unavailable; a
  provider failure is a 502; a proposal NEVER modifies the stored
  draft until the user applies it through the normal update path.
  Generation is sectioned (outline via llm_json, one llm_text call
  per section) and lands as a NEW draft.
- Analysis is the pipeline's own script_analyzer (degraded mode
  here — no provider in the test env — and the recorded mode says
  so); fact check is script_facts extraction (basis script_claim)
  plus corroboration, which upgrades a fact only on a verified KB
  verdict whose description carries the fact's year.
- Send-to-production creates a real project whose script is the
  assembled section bodies (headings are studio structure and are
  never narrated); attach rewrites an owned project's script and
  refuses another user's project with a 404.
- Ownership: drafts live in the caller's own store, so another
  user's draft id is a 404 on every route.
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
from services import kb_client, llm_client, run_ledger

TOKEN = "script-studio-test-token"
PASSWORD = "script-studio-test-password"

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
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    # Deterministic AI availability: no provider unless a test
    # explicitly configures the stub.
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
    a = _register(client, f"ss-a-{suffix}@example.com")
    b = _register(client, f"ss-b-{suffix}@example.com")
    return a, b


def _create_draft(client, headers, **overrides):
    payload = {
        "title": "The Box That Changed Trade",
        "category": "industrial",
        "target_minutes": 10,
        "sections": [
            {"heading": "The sailing", "body": SCRIPT_BODY},
            {"heading": "Aftermath", "body": "Ports rebuilt themselves around the box."},
        ],
    }
    payload.update(overrides)
    response = client.post(
        "/api/v1/script-studio/scripts", headers=headers, json=payload
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------
# Draft CRUD + metrics
# ---------------------------------------------------------------------


def test_create_get_list_and_metrics(client, users):
    a, _b = users
    headers = _bearer(a)
    draft = _create_draft(client, headers)
    # 21 words in section 1 + 6 in section 2 = 27 words, 3 sentences.
    assert draft["metrics"]["word_count"] == 27
    assert draft["metrics"]["sentence_count"] == 3
    assert draft["metrics"]["words_per_minute"] == 145
    assert draft["metrics"]["estimated_minutes"] == round(27 / 145, 2)
    assert draft["metrics"]["target_words"] == 1450
    assert draft["metrics"]["words_vs_target"] == 27 - 1450
    assert draft["sections"][0]["metrics"]["word_count"] == 21
    # The assembled production script excludes section headings.
    assert draft["script_text"] == SCRIPT_BODY + "\n\nPorts rebuilt themselves around the box."
    assert "The sailing" not in draft["script_text"]

    fetched = client.get(
        f"/api/v1/script-studio/scripts/{draft['id']}", headers=headers
    )
    assert fetched.status_code == 200
    assert fetched.json()["id"] == draft["id"]

    listed = client.get("/api/v1/script-studio/scripts", headers=headers)
    assert listed.status_code == 200
    assert [s["id"] for s in listed.json()["scripts"]] == [draft["id"]]


def test_update_snapshots_revision_and_restore(client, users):
    a, _b = users
    headers = _bearer(a)
    draft = _create_draft(client, headers)
    revised_sections = [
        {"id": draft["sections"][0]["id"], "heading": "The sailing",
         "body": "A completely different opening line."},
        {"id": draft["sections"][1]["id"], "heading": "Aftermath",
         "body": "Ports rebuilt themselves around the box."},
    ]
    updated = client.put(
        f"/api/v1/script-studio/scripts/{draft['id']}",
        headers=headers,
        json={"sections": revised_sections},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision_count"] == 1

    revisions = client.get(
        f"/api/v1/script-studio/scripts/{draft['id']}/revisions",
        headers=headers,
    ).json()["revisions"]
    assert len(revisions) == 1
    assert revisions[0]["metrics"]["word_count"] == 27

    restored = client.post(
        f"/api/v1/script-studio/scripts/{draft['id']}/revisions/"
        f"{revisions[0]['id']}/restore",
        headers=headers,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["script_text"].startswith("On April 26, 1956")
    # The restore itself snapshotted the pre-restore state.
    assert restored.json()["revision_count"] == 2


def test_delete_draft(client, users):
    a, _b = users
    headers = _bearer(a)
    draft = _create_draft(client, headers)
    deleted = client.delete(
        f"/api/v1/script-studio/scripts/{draft['id']}", headers=headers
    )
    assert deleted.status_code == 200
    gone = client.get(
        f"/api/v1/script-studio/scripts/{draft['id']}", headers=headers
    )
    assert gone.status_code == 404


def test_cross_tenant_draft_is_404_everywhere(client, users):
    a, b = users
    draft = _create_draft(client, _bearer(a))
    intruder = _bearer(b)
    base = f"/api/v1/script-studio/scripts/{draft['id']}"
    for method, path in (
        ("GET", base),
        ("PUT", base),
        ("DELETE", base),
        ("GET", base + "/revisions"),
        ("POST", base + "/analysis"),
        ("POST", base + "/fact-check"),
        ("POST", base + "/ai/expand"),
        ("POST", base + "/send-to-production"),
    ):
        response = client.request(method, path, headers=intruder, json={})
        assert response.status_code == 404, (method, path, response.text)
    listed = client.get("/api/v1/script-studio/scripts", headers=intruder)
    assert listed.json()["scripts"] == []


# ---------------------------------------------------------------------
# Analysis + fact check (the pipeline's own machinery)
# ---------------------------------------------------------------------


def test_analysis_uses_pipeline_analyzer_and_records_mode(client, users):
    a, _b = users
    headers = _bearer(a)
    draft = _create_draft(client, headers)
    response = client.post(
        f"/api/v1/script-studio/scripts/{draft['id']}/analysis", headers=headers
    )
    assert response.status_code == 200, response.text
    analysis = response.json()["analysis"]
    # No provider in the test env: the pipeline's degraded planner
    # ran, and the recorded mode says exactly that.
    assert analysis["mode"] == "degraded"
    assert analysis["sentence_count"] == 3
    assert analysis["sentences"][0]["text"].startswith("On April 26, 1956")
    assert analysis["scenes"], "scene derivation produced no scenes"
    canonicals = [e["canonical"] for e in analysis["bible"]["entities"]]
    assert "Malcolm McLean" in canonicals
    fetched = client.get(
        f"/api/v1/script-studio/scripts/{draft['id']}", headers=headers
    ).json()
    assert fetched["has_analysis"] is True
    assert fetched["analysis"]["mode"] == "degraded"


def test_fact_check_corroborates_only_on_verified_kb(client, users, monkeypatch):
    a, _b = users
    headers = _bearer(a)
    draft = _create_draft(client, headers)

    def fake_lookup(name, budget=None):
        return {
            "status": "found",
            "evidence": "verified",
            "description": "Malcolm McLean's first container voyage sailed in 1956.",
            "type_source": "wikidata",
            "confidence": "high",
        }

    monkeypatch.setattr(kb_client, "lookup_entity", fake_lookup)
    response = client.post(
        f"/api/v1/script-studio/scripts/{draft['id']}/fact-check", headers=headers
    )
    assert response.status_code == 200, response.text
    fact_check = response.json()["fact_check"]
    assert fact_check["record"]["dated_events"] == 1
    assert fact_check["corroboration"]["corroborated"] == 1
    dated = [f for f in fact_check["facts"] if f["kind"] == "dated_event"]
    assert dated[0]["basis"] == "research_supported"
    assert dated[0]["source"]["type"] == "kb"
    quantity = [f for f in fact_check["facts"] if f["kind"] == "quantity"]
    assert quantity[0]["basis"] == "script_claim"


def test_fact_check_without_kb_support_stays_script_claim(client, users, monkeypatch):
    a, _b = users
    headers = _bearer(a)
    draft = _create_draft(client, headers)
    monkeypatch.setattr(
        kb_client,
        "lookup_entity",
        lambda name, budget=None: {
            "status": "not_found",
            "evidence": "unverified",
            "description": "",
            "type_source": None,
            "confidence": None,
        },
    )
    response = client.post(
        f"/api/v1/script-studio/scripts/{draft['id']}/fact-check", headers=headers
    )
    assert response.status_code == 200, response.text
    fact_check = response.json()["fact_check"]
    assert fact_check["corroboration"]["corroborated"] == 0
    dated = [f for f in fact_check["facts"] if f["kind"] == "dated_event"]
    assert dated[0]["basis"] == "script_claim"
    assert dated[0]["kb_check"]["status"] == "not_found"


def test_analysis_and_fact_check_refuse_empty_script(client, users):
    a, _b = users
    headers = _bearer(a)
    draft = _create_draft(
        client, headers, sections=[{"heading": "Empty", "body": "  "}]
    )
    for action in ("analysis", "fact-check", "send-to-production"):
        response = client.post(
            f"/api/v1/script-studio/scripts/{draft['id']}/{action}",
            headers=headers,
        )
        assert response.status_code == 422, (action, response.text)
        assert response.json()["error"]["code"] == "script_empty"


# ---------------------------------------------------------------------
# AI actions: unavailable, failure, and proposal semantics
# ---------------------------------------------------------------------


def test_ai_actions_unavailable_without_provider(client, users):
    a, _b = users
    headers = _bearer(a)
    draft = _create_draft(client, headers)
    response = client.post(
        "/api/v1/script-studio/ai/draft",
        headers=headers,
        json={"topic": "The container port", "target_minutes": 10},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "script_ai_unavailable"
    for path, payload in (
        (f"/api/v1/script-studio/scripts/{draft['id']}/ai/expand", None),
        (
            f"/api/v1/script-studio/scripts/{draft['id']}/ai/tighten",
            {"section_id": draft["sections"][0]["id"]},
        ),
    ):
        response = client.post(path, headers=headers, json=payload)
        assert response.status_code == 503, (path, response.text)
        assert response.json()["error"]["code"] == "script_ai_unavailable"


def test_ai_generation_target_out_of_range(client, users):
    a, _b = users
    response = client.post(
        "/api/v1/script-studio/ai/draft",
        headers=_bearer(a),
        json={"topic": "A very long film", "target_minutes": 90},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "target_out_of_range"


def _configure_ai_stub(monkeypatch):
    monkeypatch.setattr(llm_client, "BREWKEG_API_KEY", "test-key")


def test_ai_draft_generation_is_sectioned_and_creates_new_draft(
    client, users, monkeypatch
):
    a, _b = users
    headers = _bearer(a)
    _configure_ai_stub(monkeypatch)
    calls = {"json": 0, "text": 0}

    def fake_json(system, user, **kwargs):
        calls["json"] += 1
        assert "word_budget" in user
        return {
            "sections": [
                {"heading": "Hook", "intent": "Open on the sailing.", "word_budget": 700},
                {"heading": "Fallout", "intent": "Ports change.", "word_budget": 750},
            ]
        }

    def fake_text(system, user, **kwargs):
        calls["text"] += 1
        return f"Generated narration for call {calls['text']}."

    monkeypatch.setattr(llm_client, "llm_json", fake_json)
    monkeypatch.setattr(llm_client, "llm_text", fake_text)
    response = client.post(
        "/api/v1/script-studio/ai/draft",
        headers=headers,
        json={"topic": "The container port", "target_minutes": 10},
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    assert calls == {"json": 1, "text": 2}
    assert [s["heading"] for s in draft["sections"]] == ["Hook", "Fallout"]
    assert draft["sections"][0]["body"] == "Generated narration for call 1."
    assert draft["generation"]["outline"][0]["word_budget"] == 700
    assert draft["generation"]["basis"] == "llm_generated_from_user_brief"
    # It persisted as a normal draft the user can keep editing.
    listed = client.get("/api/v1/script-studio/scripts", headers=headers).json()
    assert [s["id"] for s in listed["scripts"]] == [draft["id"]]


def test_expand_is_a_proposal_until_applied(client, users, monkeypatch):
    a, _b = users
    headers = _bearer(a)
    draft = _create_draft(client, headers)
    _configure_ai_stub(monkeypatch)
    monkeypatch.setattr(
        llm_client, "llm_text", lambda system, user, **kw: "Expanded prose."
    )
    response = client.post(
        f"/api/v1/script-studio/scripts/{draft['id']}/ai/expand", headers=headers
    )
    assert response.status_code == 200, response.text
    proposal = response.json()["proposal"]
    assert [s["body"] for s in proposal["sections"]] == ["Expanded prose."] * 2
    # The stored draft is untouched by the proposal.
    current = client.get(
        f"/api/v1/script-studio/scripts/{draft['id']}", headers=headers
    ).json()
    assert current["script_text"].startswith("On April 26, 1956")
    # Applying goes through the normal update path (the client maps
    # the proposal onto the section shape the update accepts).
    applied = client.put(
        f"/api/v1/script-studio/scripts/{draft['id']}",
        headers=headers,
        json={
            "sections": [
                {"id": s["id"], "heading": s["heading"], "body": s["body"]}
                for s in proposal["sections"]
            ]
        },
    )
    assert applied.status_code == 200
    assert applied.json()["script_text"] == "Expanded prose.\n\nExpanded prose."


def test_tighten_proposal_reports_both_word_counts(client, users, monkeypatch):
    a, _b = users
    headers = _bearer(a)
    draft = _create_draft(client, headers)
    _configure_ai_stub(monkeypatch)
    monkeypatch.setattr(
        llm_client, "llm_text", lambda system, user, **kw: "Tightened text."
    )
    section_id = draft["sections"][0]["id"]
    response = client.post(
        f"/api/v1/script-studio/scripts/{draft['id']}/ai/tighten",
        headers=headers,
        json={"section_id": section_id},
    )
    assert response.status_code == 200, response.text
    proposal = response.json()["proposal"]
    assert proposal["original"] == SCRIPT_BODY
    assert proposal["tightened"] == "Tightened text."
    assert proposal["original_words"] == 21
    assert proposal["tightened_words"] == 2
    current = client.get(
        f"/api/v1/script-studio/scripts/{draft['id']}", headers=headers
    ).json()
    assert current["sections"][0]["body"] == SCRIPT_BODY


def test_ai_provider_failure_is_502_never_a_canned_draft(client, users, monkeypatch):
    a, _b = users
    headers = _bearer(a)
    _configure_ai_stub(monkeypatch)

    def boom(system, user, **kwargs):
        raise RuntimeError("gateway exploded")

    monkeypatch.setattr(llm_client, "llm_json", boom)
    response = client.post(
        "/api/v1/script-studio/ai/draft",
        headers=headers,
        json={"topic": "Anything", "target_minutes": 10},
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "script_ai_failed"
    listed = client.get("/api/v1/script-studio/scripts", headers=headers).json()
    assert listed["scripts"] == []


# ---------------------------------------------------------------------
# Send to production / attach
# ---------------------------------------------------------------------


def test_send_to_production_creates_real_project(client, users):
    a, _b = users
    headers = _bearer(a)
    draft = _create_draft(client, headers)
    response = client.post(
        f"/api/v1/script-studio/scripts/{draft['id']}/send-to-production",
        headers=headers,
    )
    assert response.status_code == 201, response.text
    project_id = response.json()["project_id"]
    project = client.get(f"/api/v1/projects/{project_id}", headers=headers)
    assert project.status_code == 200
    view = project.json()
    assert view["title"] == "The Box That Changed Trade"
    assert view["category"] == "industrial"
    assert view["script"] == (
        SCRIPT_BODY + "\n\nPorts rebuilt themselves around the box."
    )
    updated = client.get(
        f"/api/v1/script-studio/scripts/{draft['id']}", headers=headers
    ).json()
    assert updated["project_id"] == project_id
    assert updated["productions"][0]["via"] == "send_to_production"


def test_attach_rewrites_owned_project_and_refuses_foreign(client, users):
    a, b = users
    headers_a, headers_b = _bearer(a), _bearer(b)
    draft = _create_draft(client, headers_a)
    own_project = client.post(
        "/api/v1/projects",
        headers=headers_a,
        json={"title": "Mine", "script": "Original script.", "category": "industrial"},
    ).json()
    foreign_project = client.post(
        "/api/v1/projects",
        headers=headers_b,
        json={"title": "Theirs", "script": "Their script.", "category": "industrial"},
    ).json()

    refused = client.post(
        f"/api/v1/script-studio/scripts/{draft['id']}/attach",
        headers=headers_a,
        json={"project_id": foreign_project["id"]},
    )
    assert refused.status_code == 404

    attached = client.post(
        f"/api/v1/script-studio/scripts/{draft['id']}/attach",
        headers=headers_a,
        json={"project_id": own_project["id"]},
    )
    assert attached.status_code == 200, attached.text
    view = client.get(
        f"/api/v1/projects/{own_project['id']}", headers=headers_a
    ).json()
    assert view["script"].startswith("On April 26, 1956")
    # The foreign project's script is untouched.
    foreign = client.get(
        f"/api/v1/projects/{foreign_project['id']}", headers=headers_b
    ).json()
    assert foreign["script"] == "Their script."


def test_project_row_written_by_send_to_production(client, users):
    a, _b = users
    headers = _bearer(a)
    draft = _create_draft(client, headers)
    project_id = client.post(
        f"/api/v1/script-studio/scripts/{draft['id']}/send-to-production",
        headers=headers,
    ).json()["project_id"]
    db = SessionLocal()
    try:
        row = db.get(Project, project_id)
        assert row is not None
        assert row.status == "created"
        assert row.script.startswith("On April 26, 1956")
    finally:
        db.close()
