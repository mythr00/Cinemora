"""FS access rules — the workstream's cross-cutting access suite.

FS-A (FS-4) — failure-detail sanitization, this file's first
section. A pipeline stage exception's str(error) is stored verbatim
in production_state.json and ProductionRun.error. Before FS-A the
user-facing carriers returned it unsanitized to ordinary users:

1. api_v1._run_view — feeds GET /projects/{id} (latest_run),
   GET /projects/{id}/runs, GET /runs/{run_id} (and run-submission
   / cancel responses);
2. GET /projects/{id}/status — top-level error, the entire
   production state as "progress" (state.error + stages[*].error),
   "stages", and latest_run;
3. the legacy main.py GET /projects/{project_id}/status — the same
   state file outside v1.

These tests pin the fixed behaviour:

- an ordinary user sees NO internal error text anywhere in any
  carrier's JSON (asserted against the raw response text), the
  generic message "Video generation failed" is present, and the
  status still reads "failed" (failure is never disguised);
- a project collaborator (accepted member, non-admin account role)
  is sanitized exactly like the owner-user (register A-9);
- admin and super_admin principals, and the operator principal
  (which resolves to super_admin), see the FULL original text;
- non-failure responses are unchanged (a completed run keeps
  error=None, statuses/stage/timestamps intact);
- storage is untouched: ProductionRun.error and
  production_state.json still hold the full text server-side.

Users are created through the REAL registration path and sessions
live in fakeredis, exactly as in tests/test_legacy_tenant_isolation.py
and tests/test_phase8_auth_isolation.py. No production is ever run:
the failed run and its state file are seeded directly.
"""

import json
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
from auth_helpers import with_session_token
import main
from database import SessionLocal, engine
from models import Base, Plan, ProductionRun, Project, User, UserSubscription
from services import auth as auth_service
from services import run_ledger

TOKEN = "fs-a-" + "operator-token-for-tests"
PASSWORD = "fs-a-" + "correct-horse-42"

INTERNAL_ERROR = (
    "TIMELINE COVERAGE INCOMPLETE: 4 of 31 narration sentences have "
    "no visual coverage: sentence 23 (scene 7, 96.0s-101.5s). "
    "Refusing to render an incomplete documentary."
)
GENERIC = api_v1.GENERIC_FAILURE_MESSAGE


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")
    monkeypatch.delenv("CINEMORA_REGISTRATION", raising=False)
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(fakeredis.FakeRedis(decode_responses=True))
    db = SessionLocal()
    try:
        for model in (ProductionRun, Project, UserSubscription, User):
            db.query(model).delete()
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
    db = SessionLocal()
    try:
        for model in (ProductionRun, Project, UserSubscription, User, Plan):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _uid():
    return str(uuid.uuid4())


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _register(client, label):
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    body = with_session_token(response)
    return {
        "email": email,
        "id": body["user"]["id"],
        "headers": _bearer(body["session_token"]),
    }


def _make_user(role):
    """A DB-created account with an explicit account role (the
    test_admin_api pattern), plus a real session for it."""
    db = SessionLocal()
    try:
        user = User(
            id=_uid(),
            email=f"{role}-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=auth_service.hash_password(PASSWORD),
            role=role,
            status="active",
        )
        db.add(user)
        db.commit()
        user_id, email = user.id, user.email
    finally:
        db.close()
    return {
        "email": email,
        "id": user_id,
        "headers": _bearer(auth_service.create_session(user_id)),
    }


def _seed_failed_project(owner_id, tmp_media_root, *, run_status="failed",
                         error=INTERNAL_ERROR):
    """One project + one run carrying the internal failure text in
    BOTH stores (ProductionRun.error and production_state.json,
    top-level and per-stage), exactly as jobs.fail_pipeline +
    run_ledger.finish_run persist it."""
    script = "Alpha opens. Bravo continues. Charlie closes."
    project_id, run_id = _uid(), _uid()
    db = SessionLocal()
    try:
        db.add(
            Project(
                id=project_id,
                owner_id=owner_id,
                title="FS-A seeded project",
                script=script,
                category="Documentary",
                status=run_status,
            )
        )
        db.add(
            ProductionRun(
                id=run_id,
                project_id=project_id,
                owner_id=owner_id,
                status=run_status,
                stage="video_editor",
                error=error,
                script_hash=run_ledger.script_sha256(script),
            )
        )
        db.commit()
    finally:
        db.close()
    if error is not None:
        project_dir = tmp_media_root / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "status": run_status,
            "current_stage": "video_editor",
            "started_at": "2026-11-25T00:00:00+00:00",
            "completed_at": "2026-11-25T00:05:00+00:00",
            "error": error,
            "stages": {
                "voice_actor": {
                    "status": "completed",
                    "started_at": None,
                    "completed_at": None,
                    "error": None,
                },
                "video_editor": {
                    "status": "failed",
                    "started_at": None,
                    "completed_at": None,
                    "error": error,
                },
            },
        }
        (project_dir / "production_state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
    return project_id, run_id


@pytest.fixture()
def media_root(tmp_path):
    return tmp_path / "media"


def _share(client, owner, member, project_id, role="editor"):
    invite = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=owner["headers"],
        json={"email": member["email"], "role": role},
    )
    assert invite.status_code == 201, invite.text
    respond = client.post(
        f"/api/v1/invitations/{project_id}/respond",
        headers=member["headers"],
        json={"accept": True},
    )
    assert respond.status_code == 200, respond.text


def _carriers(client, headers, project_id, run_id):
    """Every user-facing carrier of the failure detail, as
    (name, response) pairs."""
    return [
        ("project detail (latest_run)",
         client.get(f"/api/v1/projects/{project_id}", headers=headers)),
        ("project list (latest_run)",
         client.get("/api/v1/projects", headers=headers)),
        ("run list",
         client.get(f"/api/v1/projects/{project_id}/runs", headers=headers)),
        ("run detail",
         client.get(f"/api/v1/runs/{run_id}", headers=headers)),
        ("project status",
         client.get(f"/api/v1/projects/{project_id}/status", headers=headers)),
        ("legacy project status",
         client.get(f"/projects/{project_id}/status", headers=headers)),
    ]


def _assert_sanitized(name, response):
    assert response.status_code == 200, (name, response.text)
    # No internal text ANYWHERE in the payload, nested fields incl.
    assert INTERNAL_ERROR not in response.text, name
    assert "TIMELINE COVERAGE INCOMPLETE" not in response.text, name
    # The generic, truthful failure message IS present...
    assert GENERIC in response.text, name
    # ...and the failure is never disguised as success.
    assert '"failed"' in response.text, name


def _assert_full_detail(name, response):
    assert response.status_code == 200, (name, response.text)
    assert INTERNAL_ERROR in response.text, name


# ---------------------------------------------------------------------
# FS-A — ordinary users are sanitized on every carrier
# ---------------------------------------------------------------------


def test_ordinary_user_sees_no_internal_error_on_any_carrier(
    client, media_root
):
    owner = _register(client, "owner")
    project_id, run_id = _seed_failed_project(owner["id"], media_root)

    for name, response in _carriers(client, owner["headers"], project_id, run_id):
        _assert_sanitized(name, response)

    # Carrier-specific shape checks: status stays "failed", the
    # generic message lands in the error fields, stage names and
    # progress structure survive.
    status = client.get(
        f"/api/v1/projects/{project_id}/status", headers=owner["headers"]
    ).json()
    assert status["status"] == "failed"
    assert status["error"] == GENERIC
    assert status["current_stage"] == "video_editor"
    assert status["progress"]["error"] == GENERIC
    assert status["progress"]["stages"]["video_editor"]["error"] == GENERIC
    assert status["progress"]["stages"]["video_editor"]["status"] == "failed"
    assert status["progress"]["stages"]["voice_actor"]["error"] is None
    assert status["stages"]["video_editor"]["error"] == GENERIC
    assert status["latest_run"]["error"] == GENERIC
    assert status["latest_run"]["status"] == "failed"

    run = client.get(f"/api/v1/runs/{run_id}", headers=owner["headers"]).json()
    assert run["status"] == "failed"
    assert run["stage"] == "video_editor"
    assert run["error"] == GENERIC

    legacy = client.get(
        f"/projects/{project_id}/status", headers=owner["headers"]
    ).json()
    assert legacy["status"] == "failed"
    assert legacy["progress"]["error"] == GENERIC
    assert legacy["progress"]["stages"]["video_editor"]["error"] == GENERIC

    # Storage is untouched: the full text remains server-side.
    stored = run_ledger.get_run(run_id)
    assert stored["error"] == INTERNAL_ERROR
    state_file = media_root / project_id / "production_state.json"
    assert json.loads(state_file.read_text(encoding="utf-8"))["error"] == (
        INTERNAL_ERROR
    )


def test_collaborator_is_sanitized_as_non_admin(client, media_root):
    owner = _register(client, "owner")
    member = _register(client, "member")
    project_id, run_id = _seed_failed_project(owner["id"], media_root)
    _share(client, owner, member, project_id, role="editor")

    for name, response in _carriers(
        client, member["headers"], project_id, run_id
    ):
        if name.startswith("project list"):
            continue  # the member's list annotates access_role; covered below
        _assert_sanitized(name, response)

    run = client.get(f"/api/v1/runs/{run_id}", headers=member["headers"]).json()
    assert run["error"] == GENERIC


# ---------------------------------------------------------------------
# FS-A — admins and the operator keep the full detail
# ---------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "super_admin"])
def test_admin_roles_see_full_detail(client, media_root, role):
    admin = _make_user(role)
    project_id, run_id = _seed_failed_project(admin["id"], media_root)

    for name, response in _carriers(client, admin["headers"], project_id, run_id):
        _assert_full_detail(name, response)

    run = client.get(f"/api/v1/runs/{run_id}", headers=admin["headers"]).json()
    assert run["error"] == INTERNAL_ERROR


def test_operator_principal_sees_full_detail(client, media_root):
    # The operator principal owns legacy NULL-owner rows (the v1
    # choke point's rule) and resolves to super_admin, so its
    # responses keep the full detail byte-identical.
    project_id, run_id = _seed_failed_project(None, media_root)
    headers = {"X-API-Key": TOKEN}

    for name, response in _carriers(client, headers, project_id, run_id):
        if name.startswith("project list"):
            continue  # operator list semantics are the founder's own view
        _assert_full_detail(name, response)


# ---------------------------------------------------------------------
# FS-A — non-failure responses are unchanged
# ---------------------------------------------------------------------


def test_completed_run_response_is_unchanged_for_user(client, media_root):
    owner = _register(client, "owner")
    project_id, run_id = _seed_failed_project(
        owner["id"], media_root, run_status="completed", error=None
    )

    run = client.get(f"/api/v1/runs/{run_id}", headers=owner["headers"])
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "completed"
    assert body["error"] is None
    assert GENERIC not in run.text

    status = client.get(
        f"/api/v1/projects/{project_id}/status", headers=owner["headers"]
    )
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "completed"
    assert status.json()["error"] is None
    assert GENERIC not in status.text


def test_sanitizer_helper_admin_passthrough_and_no_mutation():
    # Unit-level pin of the helper's contract: admins get the very
    # same object back; non-admins get a sanitized copy and the
    # original payload (the stored view) is not mutated.
    class _Admin:
        is_admin = True

    class _User:
        is_admin = False

    view = {
        "status": "failed",
        "error": INTERNAL_ERROR,
        "progress": {"stages": {"video_editor": {"error": INTERNAL_ERROR}}},
    }
    assert api_v1.sanitize_failure_detail(view, _Admin()) is view
    sanitized = api_v1.sanitize_failure_detail(view, _User())
    assert sanitized["error"] == GENERIC
    assert sanitized["progress"]["stages"]["video_editor"]["error"] == GENERIC
    assert view["error"] == INTERNAL_ERROR
    assert api_v1.sanitize_failure_detail(view, None)["error"] == GENERIC


# =====================================================================
# FS-B (FS-7/FS-9/FS-10, FS-14) — the diagnostic surfaces are
# admin-only: Asset Decisions ledger detail, Delivery & Provenance
# certificate, QA reports.
#
# Contract pinned here, per surface:
# - an ordinary project OWNER (non-admin account role) is refused
#   the internal detail: certificate + QA routes answer 403 with
#   none of the internal detail in the body; the shared assets
#   endpoint answers 200 with the internal fields stripped (it is
#   field-restricted, not closed — see the shared-flow test);
# - an accepted collaborator (editor member, non-admin) is refused
#   exactly the same way (register A-9: collaboration role does
#   not elevate);
# - a principal with NO grant on the project keeps the Phase 1a
#   tenant-private 404 on every surface (existence stays hidden);
# - admin / super_admin principals and the operator principal
#   (resolves to super_admin) receive the full detail unchanged;
# - shared endpoints keep serving their legitimate user flows for
#   non-admins: GET .../assets still serves the Editor's asset
#   library (the reuse fields), and GET .../outputs still serves
#   the Overview's film/caption delivery panel.
# =====================================================================

from models import AssetDecision  # noqa: E402

SOURCE_URL = "https://provenance.example/internal/ledger-source-9f3"
CONTENT_HASH = "fsb-internal-content-hash-9f3"
QA_MARKER = "FSB_INTERNAL_QA_DETAIL_9F3"


def _seed_production(owner_id, media_root):
    """One completed production: project + completed run + two
    ledger decisions carrying internal provenance detail + a
    recorded production_report.json whose verdict detail carries
    the QA marker."""
    script = "Alpha opens. Bravo continues. Charlie closes."
    project_id, run_id = _uid(), _uid()
    db = SessionLocal()
    try:
        db.add(
            Project(
                id=project_id,
                owner_id=owner_id,
                title="FS-B seeded project",
                script=script,
                category="Documentary",
                status="completed",
            )
        )
        db.add(
            ProductionRun(
                id=run_id,
                project_id=project_id,
                owner_id=owner_id,
                status="completed",
                stage="delivery",
                script_hash=run_ledger.script_sha256(script),
            )
        )
        for idx, decision_type in enumerate(("accepted", "rejected")):
            db.add(
                AssetDecision(
                    id=_uid(),
                    run_id=run_id,
                    project_id=project_id,
                    asset_key=f"ledger/asset-{idx}.mp4",
                    content_hash=CONTENT_HASH,
                    source_url=SOURCE_URL,
                    provider="internal-provider",
                    scene_number=1,
                    sentence_number=idx + 1,
                    interval_start=0.0,
                    interval_duration=5.0,
                    evidence_class="direct",
                    verification={"accepted": decision_type == "accepted"},
                    rights={"license": "internal-licence", "rights_status": "verified"},
                    decision_type=decision_type,
                )
            )
        db.commit()
    finally:
        db.close()
    run_dir = media_root / project_id / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "production_report.json").write_text(
        json.dumps(
            {
                "schema": "cinemora.production_report/1",
                "verdicts": {
                    "quality": {
                        "verdict": "QUALITY_WARNING",
                        "detail": QA_MARKER,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return project_id, run_id


def _gated_paths(project_id, run_id):
    return [
        ("certificate", f"/api/v1/projects/{project_id}/certificate"),
        ("qa latest", f"/api/v1/projects/{project_id}/qa-report"),
        ("qa run", f"/api/v1/projects/{project_id}/runs/{run_id}/qa-report"),
        (
            "qa export",
            f"/api/v1/projects/{project_id}/runs/{run_id}/qa-report/export",
        ),
    ]


def _assert_refused(name, response):
    assert response.status_code == 403, (name, response.status_code, response.text)
    assert SOURCE_URL not in response.text, name
    assert CONTENT_HASH not in response.text, name
    assert QA_MARKER not in response.text, name
    assert "QUALITY_WARNING" not in response.text, name


def test_fsb_owner_user_refused_internal_detail(client, media_root):
    owner = _register(client, "fsb-owner")
    project_id, run_id = _seed_production(owner["id"], media_root)

    for name, path in _gated_paths(project_id, run_id):
        _assert_refused(name, client.get(path, headers=owner["headers"]))

    # Assets (FS-7): field-restricted for non-admins — the internal
    # provenance fields are absent from every row.
    assets = client.get(
        f"/api/v1/projects/{project_id}/assets", headers=owner["headers"]
    )
    assert assets.status_code == 200, assets.text
    assert SOURCE_URL not in assets.text
    assert CONTENT_HASH not in assets.text
    assert "source_url" not in assets.text
    assert "content_hash" not in assets.text
    assert "asset_key" not in assets.text
    assert "previous_decision_id" not in assets.text


def test_fsb_collaborator_refused_internal_detail(client, media_root):
    owner = _register(client, "fsb-owner")
    member = _register(client, "fsb-member")
    project_id, run_id = _seed_production(owner["id"], media_root)
    _share(client, owner, member, project_id, role="editor")

    for name, path in _gated_paths(project_id, run_id):
        _assert_refused(name, client.get(path, headers=member["headers"]))

    assets = client.get(
        f"/api/v1/projects/{project_id}/assets", headers=member["headers"]
    )
    assert assets.status_code == 200, assets.text
    assert SOURCE_URL not in assets.text
    assert "source_url" not in assets.text


def test_fsb_non_member_keeps_phase1a_404(client, media_root):
    owner = _register(client, "fsb-owner")
    stranger = _register(client, "fsb-stranger")
    project_id, run_id = _seed_production(owner["id"], media_root)

    for name, path in _gated_paths(project_id, run_id):
        response = client.get(path, headers=stranger["headers"])
        assert response.status_code == 404, (name, response.status_code)
    assets = client.get(
        f"/api/v1/projects/{project_id}/assets", headers=stranger["headers"]
    )
    assert assets.status_code == 404, assets.text


@pytest.mark.parametrize("role", ["admin", "super_admin"])
def test_fsb_admin_roles_receive_full_detail(client, media_root, role):
    admin = _make_user(role)
    project_id, run_id = _seed_production(admin["id"], media_root)

    for name, path in _gated_paths(project_id, run_id):
        response = client.get(path, headers=admin["headers"])
        assert response.status_code == 200, (name, response.text)
    qa = client.get(
        f"/api/v1/projects/{project_id}/qa-report", headers=admin["headers"]
    )
    assert QA_MARKER in qa.text
    assert "QUALITY_WARNING" in qa.text

    certificate = client.get(
        f"/api/v1/projects/{project_id}/certificate", headers=admin["headers"]
    )
    assert certificate.status_code == 200, certificate.text
    assert SOURCE_URL in certificate.text

    # Assets: admins receive the full ledger rows, unchanged.
    assets = client.get(
        f"/api/v1/projects/{project_id}/assets", headers=admin["headers"]
    )
    assert assets.status_code == 200, assets.text
    assert SOURCE_URL in assets.text
    assert CONTENT_HASH in assets.text
    row = assets.json()["items"][0]
    assert row["source_url"] == SOURCE_URL
    assert row["content_hash"] == CONTENT_HASH
    assert row["asset_key"]


def test_fsb_admin_reads_diagnostics_across_tenants(client, media_root):
    # FS-DISCOVERY §3.1 option (a): admin diagnostics are
    # cross-tenant like the admin API — an admin who does not own
    # (and is not a member of) the project still receives the full
    # detail on every gated surface.
    owner = _register(client, "fsb-owner")
    admin = _make_user("admin")
    project_id, run_id = _seed_production(owner["id"], media_root)

    for name, path in _gated_paths(project_id, run_id):
        response = client.get(path, headers=admin["headers"])
        assert response.status_code == 200, (name, response.text)
    assets = client.get(
        f"/api/v1/projects/{project_id}/assets", headers=admin["headers"]
    )
    assert assets.status_code == 200, assets.text
    assert SOURCE_URL in assets.text


def test_fsb_operator_receives_full_detail(client, media_root):
    # NULL-owner legacy project: the operator principal owns it
    # through the choke point and resolves to super_admin.
    project_id, run_id = _seed_production(None, media_root)
    headers = {"X-API-Key": TOKEN}

    for name, path in _gated_paths(project_id, run_id):
        response = client.get(path, headers=headers)
        assert response.status_code == 200, (name, response.text)
    qa = client.get(f"/api/v1/projects/{project_id}/qa-report", headers=headers)
    assert QA_MARKER in qa.text
    assets = client.get(
        f"/api/v1/projects/{project_id}/assets", headers=headers
    )
    assert SOURCE_URL in assets.text


def test_fsb_shared_endpoints_still_serve_user_flows(client, media_root):
    owner = _register(client, "fsb-owner")
    project_id, run_id = _seed_production(owner["id"], media_root)

    # Shared flow 1 — the classic Editor's asset library
    # (components/editor/asset-panel.tsx) reuses recorded assets
    # through GET .../assets: a non-admin still gets the rows and
    # exactly the reuse fields the panel renders (id as the reuse
    # handle, decision type, provider, sentence, evidence class,
    # verification and rights labels).
    assets = client.get(
        f"/api/v1/projects/{project_id}/assets", headers=owner["headers"]
    )
    assert assets.status_code == 200, assets.text
    body = assets.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    for row in body["items"]:
        assert row["id"]
        assert row["decision_type"] in ("accepted", "rejected")
        assert row["provider"] == "internal-provider"
        assert row["sentence_number"] in (1, 2)
        assert row["evidence_class"] == "direct"
        assert row["verification"] in ({"accepted": True}, {"accepted": False})
        assert row["rights"]["license"] == "internal-licence"

    # Shared flow 2 — the Overview Delivery panel
    # (app/projects/[id]/page.tsx) reads GET .../outputs for the
    # film/caption downloads: unchanged for non-admins.
    outputs = client.get(
        f"/api/v1/projects/{project_id}/outputs", headers=owner["headers"]
    )
    assert outputs.status_code == 200, outputs.text
    body = outputs.json()
    assert body["project_id"] == project_id
    assert "film" in body and "captions" in body

    # Shared flow 3 — the QA page's supporting reads (project +
    # run list) stay available to non-admins; only the report
    # detail itself is gated.
    project = client.get(
        f"/api/v1/projects/{project_id}", headers=owner["headers"]
    )
    assert project.status_code == 200, project.text
    runs = client.get(
        f"/api/v1/projects/{project_id}/runs", headers=owner["headers"]
    )
    assert runs.status_code == 200, runs.text


# =====================================================================
# FS-C (FS-1/FS-5/FS-6/FS-11/FS-13, FS-14) — the presentational
# internal surfaces are admin-only: Visual Plan, Research & Source
# Library, Versioning & Recovery, project + account Intelligence,
# and the account Library & Templates families.
#
# Contract pinned here, per surface:
# - an ordinary project OWNER (non-admin account role) is refused:
#   project-scoped surfaces answer 403 (after the Phase 1a choke
#   point), account-scoped families answer 403 directly, and no
#   internal payload crosses in any refusal body;
# - an accepted collaborator is refused exactly the same way
#   (register A-9);
# - a principal with NO grant on the project keeps the
#   tenant-private 404 on the project-scoped surfaces;
# - admin / super_admin principals and the operator principal
#   receive the surfaces unchanged (admins cross-tenant, per the
#   FS-B convention);
# - the plan-approval gate (A-5): a non-admin-owned project with
#   require_plan_approval set does NOT park at the gate — the run
#   goes straight to the full pipeline — while an admin's gated
#   submission still parks in the analysis phase, and the plan
#   approve POST refuses non-admins;
# - shared endpoints left open keep serving their kept flows: the
#   Graphics tab's only read is the shared editor document
#   endpoint (kept Editor / Editor Studio), which is NOT gated.
# =====================================================================

from types import SimpleNamespace  # noqa: E402

import jobs  # noqa: E402

PLAN_MARKER = "FSC_INTERNAL_PLAN_SENTENCE_7A1"
RESEARCH_MARKER = "FSC_INTERNAL_RESEARCH_ENTITY_7A1"


class _FakeQueue:
    def __init__(self):
        self.calls = []

    def enqueue(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return SimpleNamespace(id=f"job-{len(self.calls)}")


def _seed_fsc_project(owner_id, media_root):
    """One project with a recorded plan document (marker sentence)
    and a Case Bible (marker entity), so the gated surfaces have
    real internal payloads to protect."""
    project_id, run_id = _seed_production(owner_id, media_root)
    project_dir = media_root / project_id
    (project_dir / "plan.json").write_text(
        json.dumps(
            {
                "sentences": [
                    {"sentence_number": 1, "text": PLAN_MARKER}
                ]
            }
        ),
        encoding="utf-8",
    )
    (project_dir / "case_bible.json").write_text(
        json.dumps(
            {
                "title": "FS-C bible",
                "entities": [
                    {"id": "e1", "name": RESEARCH_MARKER, "type": "person"}
                ],
                "facts": [],
            }
        ),
        encoding="utf-8",
    )
    return project_id, run_id


def _fsc_project_paths(project_id):
    return [
        ("plan", f"/api/v1/projects/{project_id}/plan"),
        ("research", f"/api/v1/projects/{project_id}/research"),
        ("research bible", f"/api/v1/projects/{project_id}/research/bible"),
        ("research sources", f"/api/v1/projects/{project_id}/research/sources"),
        ("versions", f"/api/v1/projects/{project_id}/versions"),
        ("intelligence", f"/api/v1/projects/{project_id}/intelligence"),
    ]


def _fsc_account_paths():
    return [
        ("account intelligence", "/api/v1/intelligence"),
        ("library assets", "/api/v1/library/assets"),
        ("library duplicates", "/api/v1/library/duplicates"),
        ("library storage", "/api/v1/library/storage"),
        ("library folders", "/api/v1/library/folders"),
        ("templates", "/api/v1/templates"),
    ]


def _assert_fsc_refused(name, response, status=403):
    assert response.status_code == status, (
        name,
        response.status_code,
        response.text,
    )
    assert PLAN_MARKER not in response.text, name
    assert RESEARCH_MARKER not in response.text, name
    assert SOURCE_URL not in response.text, name


def test_fsc_owner_user_refused_presentational_surfaces(client, media_root):
    owner = _register(client, "fsc-owner")
    project_id, _run_id = _seed_fsc_project(owner["id"], media_root)

    for name, path in _fsc_project_paths(project_id):
        _assert_fsc_refused(name, client.get(path, headers=owner["headers"]))
    for name, path in _fsc_account_paths():
        _assert_fsc_refused(name, client.get(path, headers=owner["headers"]))

    # Writes on the gated families refuse non-admins too.
    writes = [
        ("plan generate", "post", f"/api/v1/projects/{project_id}/plan/generate", {}),
        (
            "plan decisions",
            "post",
            f"/api/v1/projects/{project_id}/plan/decisions",
            {"json": {"sentence_number": 1, "action": "approve"}},
        ),
        ("plan approve", "post", f"/api/v1/projects/{project_id}/plan/approve", {}),
        (
            "research session",
            "post",
            f"/api/v1/projects/{project_id}/research/sessions",
            {"json": {"topic": "T"}},
        ),
        (
            "snapshot create",
            "post",
            f"/api/v1/projects/{project_id}/versions/snapshots",
            {"json": {"name": "S"}},
        ),
        ("template create", "post", "/api/v1/templates", {"json": {"name": "T"}}),
        ("library folder", "post", "/api/v1/library/folders", {"json": {"name": "F"}}),
    ]
    for name, method, path, kwargs in writes:
        response = getattr(client, method)(
            path, headers=owner["headers"], **kwargs
        )
        _assert_fsc_refused(name, response)


def test_fsc_collaborator_refused_and_stranger_404(client, media_root):
    owner = _register(client, "fsc-owner")
    member = _register(client, "fsc-member")
    stranger = _register(client, "fsc-stranger")
    project_id, _run_id = _seed_fsc_project(owner["id"], media_root)
    _share(client, owner, member, project_id, role="editor")

    for name, path in _fsc_project_paths(project_id):
        _assert_fsc_refused(
            f"collaborator {name}", client.get(path, headers=member["headers"])
        )
        response = client.get(path, headers=stranger["headers"])
        assert response.status_code == 404, (name, response.status_code)


@pytest.mark.parametrize("role", ["admin", "super_admin"])
def test_fsc_admin_roles_receive_full_surfaces(client, media_root, role):
    admin = _make_user(role)
    project_id, _run_id = _seed_fsc_project(admin["id"], media_root)

    plan = client.get(
        f"/api/v1/projects/{project_id}/plan", headers=admin["headers"]
    )
    assert plan.status_code == 200, plan.text
    assert PLAN_MARKER in plan.text

    research = client.get(
        f"/api/v1/projects/{project_id}/research", headers=admin["headers"]
    )
    assert research.status_code == 200, research.text
    bible = client.get(
        f"/api/v1/projects/{project_id}/research/bible",
        headers=admin["headers"],
    )
    assert bible.status_code == 200, bible.text
    assert RESEARCH_MARKER in bible.text

    versions = client.get(
        f"/api/v1/projects/{project_id}/versions", headers=admin["headers"]
    )
    assert versions.status_code == 200, versions.text
    assert versions.json()["entries"], versions.text

    intelligence = client.get(
        f"/api/v1/projects/{project_id}/intelligence",
        headers=admin["headers"],
    )
    assert intelligence.status_code == 200, intelligence.text

    for name, path in _fsc_account_paths():
        response = client.get(path, headers=admin["headers"])
        assert response.status_code == 200, (name, response.text)


def test_fsc_admin_reads_presentational_surfaces_across_tenants(
    client, media_root
):
    # The FS-B convention (FS-DISCOVERY §3.1 option (a)): admin
    # surfaces are cross-tenant like the admin API.
    owner = _register(client, "fsc-owner")
    admin = _make_user("admin")
    project_id, _run_id = _seed_fsc_project(owner["id"], media_root)

    for name, path in _fsc_project_paths(project_id):
        response = client.get(path, headers=admin["headers"])
        assert response.status_code == 200, (name, response.text)
    plan = client.get(
        f"/api/v1/projects/{project_id}/plan", headers=admin["headers"]
    )
    assert PLAN_MARKER in plan.text


def test_fsc_operator_receives_full_surfaces(client, media_root):
    project_id, _run_id = _seed_fsc_project(None, media_root)
    headers = {"X-API-Key": TOKEN}

    for name, path in _fsc_project_paths(project_id):
        response = client.get(path, headers=headers)
        assert response.status_code == 200, (name, response.text)
    for name, path in _fsc_account_paths():
        response = client.get(path, headers=headers)
        assert response.status_code == 200, (name, response.text)


def test_fsc_shared_editor_endpoint_not_gated(client, media_root):
    # The Graphics tab's only backend read is GET .../editor — the
    # SAME endpoint the kept classic Editor and Editor Studio load
    # their document through. FS-C gates the Graphics PAGE, not
    # this shared data path: a non-admin owner must not be refused
    # with the admin-only 403 here (the editor's own production
    # gating — completed run / no production — is FS-F's concern
    # and answers in its own codes).
    owner = _register(client, "fsc-owner")
    project_id, _run_id = _seed_production(owner["id"], media_root)

    response = client.get(
        f"/api/v1/projects/{project_id}/editor", headers=owner["headers"]
    )
    assert response.status_code != 403, response.text
    assert "requires an administrator" not in response.text

    # The kept Thumbnails flow (which composes from the project's
    # recorded material, library items included) stays open.
    thumbnails = client.get(
        f"/api/v1/projects/{project_id}/thumbnails", headers=owner["headers"]
    )
    assert thumbnails.status_code == 200, thumbnails.text


def _create_fsc_project(client, headers, options=None):
    body = {
        "title": "FS-C plan gate",
        "script": "Alpha opens. Bravo continues. Charlie closes.",
    }
    if options is not None:
        body["options"] = options
    response = client.post("/api/v1/projects", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_fsc_non_admin_plan_approval_gate_is_bypassed(client, monkeypatch):
    # A-5: an ordinary owner whose project carries
    # require_plan_approval must NOT park at the approval gate —
    # the submission goes straight to the full pipeline.
    queue = _FakeQueue()
    monkeypatch.setattr(jobs, "video_queue", queue)
    monkeypatch.setattr(jobs, "video_queue_priority", queue)
    owner = _register(client, "fsc-gate-owner")
    project = _create_fsc_project(
        client, owner["headers"], options={"require_plan_approval": True}
    )

    submitted = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        headers=owner["headers"],
        json={},
    )
    assert submitted.status_code == 202, submitted.text
    body = submitted.json()
    assert body["phase"] == "production", body
    assert body["run"]["status"] != "awaiting_approval", body
    assert len(queue.calls) == 1
    assert queue.calls[0][0] is jobs.run_full_pipeline


def test_fsc_admin_plan_approval_gate_still_parks(client, monkeypatch):
    # The mechanism is preserved for admins: an admin-owned
    # project with require_plan_approval still parks in the
    # analysis phase, and the approve POST then refuses an
    # ordinary user while an admin passes the gate (409 here: no
    # run is awaiting in this hermetic setup — the gate, not the
    # release, is what this asserts).
    queue = _FakeQueue()
    monkeypatch.setattr(jobs, "video_queue", queue)
    monkeypatch.setattr(jobs, "video_queue_priority", queue)
    admin = _make_user("admin")
    project = _create_fsc_project(
        client, admin["headers"], options={"require_plan_approval": True}
    )

    submitted = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        headers=admin["headers"],
        json={},
    )
    assert submitted.status_code == 202, submitted.text
    body = submitted.json()
    assert body["phase"] == "analysis", body
    assert len(queue.calls) == 1
    assert queue.calls[0][0] is jobs.run_analysis_phase

    owner = _register(client, "fsc-gate-user")
    refused = client.post(
        f"/api/v1/projects/{project['id']}/plan/approve",
        headers=owner["headers"],
    )
    assert refused.status_code in (403, 404), refused.text

    # The admin passes the gate: the answer is the mechanism's own
    # 409 (the analysis phase has not run in this hermetic setup,
    # so no run is awaiting yet), not an authorization refusal.
    admin_answer = client.post(
        f"/api/v1/projects/{project['id']}/plan/approve",
        headers=admin["headers"],
    )
    assert admin_answer.status_code == 409, admin_answer.text
    assert admin_answer.json()["error"]["code"] == "no_awaiting_run"


# =====================================================================
# FS-F (FS-8, FS-14) — Studio gating: ordinary users may enter the
# editing Studio (classic Editor AND Editor Studio) only after
# generation successfully completes AND the finished video exists.
#
# The backend gate (api_v1._require_editor_access) sits in every
# editor-family handler after the Phase 1a ownership choke point:
# - predicate: ANY ProductionRun with status "completed"
#   (run_ledger.list_runs_for — editor.py's own source) AND the
#   canonical film present in the media index
#   (_film_media_url is not None — the same source _project_view
#   and GET .../outputs use for film existence);
# - no completed run (none / queued / running / failed) ->
#   404 no_completed_run, services/editor.py's own semantics;
# - completed run but the finished video artefact missing ->
#   404 no_finished_video;
# - admin / super_admin principals (and the operator principal,
#   which resolves to super_admin) are EXEMPT;
# - collaborators follow the same rule as owners (A-9).
#
# Seeding: DB rows + artefact files in the test media root, as the
# existing editor tests do (stage6_fixture builds the one real
# completed production whose editor must load exactly as before).
# No production is ever run.
# =====================================================================

FILM_NAME = "documentary_final_output.mp4"


def _seed_gate_project(owner_id, media_root, run_statuses=(),
                       project_status="created", film=False):
    """One project + one ProductionRun per status in
    run_statuses (in order), optionally with the canonical
    finished film file present in the project directory."""
    script = "Alpha opens. Bravo continues. Charlie closes."
    project_id = _uid()
    run_ids = []
    db = SessionLocal()
    try:
        db.add(
            Project(
                id=project_id,
                owner_id=owner_id,
                title="FS-F seeded project",
                script=script,
                category="Documentary",
                status=project_status,
            )
        )
        for status in run_statuses:
            run_id = _uid()
            run_ids.append(run_id)
            db.add(
                ProductionRun(
                    id=run_id,
                    project_id=project_id,
                    owner_id=owner_id,
                    status=status,
                    stage="delivery" if status == "completed" else "video_editor",
                    script_hash=run_ledger.script_sha256(script),
                )
            )
        db.commit()
    finally:
        db.close()
    if film:
        project_dir = media_root / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / FILM_NAME).write_bytes(b"fs-f finished film bytes")
    return project_id, run_ids


def _editor_family_paths(project_id, revision_id="rev-fsf-any"):
    """Every gated editor endpoint, both families (classic Editor
    + Editor Studio), as (name, method, path, json) tuples."""
    base = f"/api/v1/projects/{project_id}"
    rev = f"{base}/revisions/{revision_id}"
    return [
        ("editor load", "get", f"{base}/editor", None),
        ("revision create", "post", f"{base}/revisions", {}),
        ("revision list", "get", f"{base}/revisions", None),
        ("revision load", "get", rev, None),
        (
            "revision operations",
            "post",
            f"{rev}/operations",
            {"operations": [{"type": "set_volumes", "music": 0.5}]},
        ),
        ("revision render", "post", f"{rev}/render", {}),
        ("revision exports", "get", f"{rev}/exports", None),
        ("revision certificate", "get", f"{rev}/certificate", None),
        ("studio view", "get", f"{rev}/studio", None),
        ("studio pool", "get", f"{rev}/studio/pool", None),
        ("studio frame", "get", f"{rev}/studio/frame", None),
        ("studio waveform", "get", f"{rev}/studio/waveform", None),
    ]


def _call(client, headers, spec):
    name, method, path, payload = spec
    if payload is None:
        return name, client.get(path, headers=headers)
    return name, client.post(path, headers=headers, json=payload)


def _assert_gate_refusal(name, response, code):
    assert response.status_code == 404, (name, response.status_code, response.text)
    assert response.json()["error"]["code"] == code, (name, response.text)


@pytest.mark.parametrize(
    "run_statuses,project_status",
    [
        ((), "created"),
        (("queued",), "queued"),
        (("running",), "running"),
        (("awaiting_approval",), "awaiting_approval"),
        (("failed",), "failed"),
        (("cancelled",), "cancelled"),
    ],
)
def test_fsf_editor_family_refuses_without_completed_run(
    client, media_root, run_statuses, project_status
):
    owner = _register(client, "fsf-owner")
    project_id, _run_ids = _seed_gate_project(
        owner["id"],
        media_root,
        run_statuses=run_statuses,
        project_status=project_status,
    )

    for spec in _editor_family_paths(project_id):
        name, response = _call(client, owner["headers"], spec)
        _assert_gate_refusal(name, response, "no_completed_run")


def test_fsf_completed_run_without_finished_video_is_refused(
    client, media_root
):
    owner = _register(client, "fsf-owner")
    project_id, _run_ids = _seed_gate_project(
        owner["id"],
        media_root,
        run_statuses=("completed",),
        project_status="completed",
        film=False,
    )

    for spec in _editor_family_paths(project_id):
        name, response = _call(client, owner["headers"], spec)
        _assert_gate_refusal(name, response, "no_finished_video")

    # The gate is a pure entry check: the underlying records are
    # untouched (the completed run still reads back as completed).
    runs = client.get(
        f"/api/v1/projects/{project_id}/runs", headers=owner["headers"]
    )
    assert runs.status_code == 200, runs.text
    assert runs.json()["runs"][0]["status"] == "completed"


def test_fsf_collaborator_follows_the_same_completion_rule(
    client, media_root
):
    owner = _register(client, "fsf-owner")
    member = _register(client, "fsf-member")
    project_id, _run_ids = _seed_gate_project(
        owner["id"],
        media_root,
        run_statuses=("failed",),
        project_status="failed",
    )
    _share(client, owner, member, project_id, role="editor")

    for spec in _editor_family_paths(project_id):
        name, response = _call(client, member["headers"], spec)
        _assert_gate_refusal(f"collaborator {name}", response, "no_completed_run")


def test_fsf_admin_and_operator_bypass_the_gate(client, media_root):
    # Admin-owned project with only a RUNNING run and no film: the
    # gate does not apply — the revision list loads (empty), and a
    # studio load answers with the service's own revision error,
    # never a gate code.
    admin = _make_user("admin")
    project_id, _run_ids = _seed_gate_project(
        admin["id"],
        media_root,
        run_statuses=("running",),
        project_status="running",
    )
    listing = client.get(
        f"/api/v1/projects/{project_id}/revisions", headers=admin["headers"]
    )
    assert listing.status_code == 200, listing.text
    assert listing.json()["revisions"] == []

    studio = client.get(
        f"/api/v1/projects/{project_id}/revisions/rev-none/studio",
        headers=admin["headers"],
    )
    assert studio.status_code == 404, studio.text
    assert studio.json()["error"]["code"] not in (
        "no_completed_run",
        "no_finished_video",
    ), studio.text

    # Operator principal (resolves to super_admin) on a NULL-owner
    # legacy project with only a FAILED run: same bypass.
    project_id, _run_ids = _seed_gate_project(
        None,
        media_root,
        run_statuses=("failed",),
        project_status="failed",
    )
    headers = {"X-API-Key": TOKEN}
    listing = client.get(
        f"/api/v1/projects/{project_id}/revisions", headers=headers
    )
    assert listing.status_code == 200, listing.text
    assert listing.json()["revisions"] == []


def _build_completed_production(media_root, project_id, owner_id):
    """The stage6 synthetic production (the test_editor_e2e_contract
    pattern): a real completed run + finished film + importable
    artefacts, re-owned by the given user."""
    import stage6_fixture as fx

    production = fx.build_synthetic_production(media_root, project_id)
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        project.owner_id = owner_id
        for run in (
            db.query(ProductionRun)
            .filter(ProductionRun.project_id == project_id)
            .all()
        ):
            run.owner_id = owner_id
        db.commit()
    finally:
        db.close()
    return production


def test_fsf_completed_with_video_loads_edits_and_survives_later_failure(
    client, media_root
):
    owner = _register(client, "fsf-owner")
    member = _register(client, "fsf-member")
    admin_member = _make_user("admin")
    project_id = f"proj-fsf-{uuid.uuid4().hex[:8]}"
    production = _build_completed_production(
        media_root, project_id, owner["id"]
    )
    assert (media_root / project_id / FILM_NAME).is_file()
    _share(client, owner, member, project_id, role="editor")
    _share(client, owner, admin_member, project_id, role="editor")

    # (2) Owner load works exactly as before, on both surfaces.
    editor_view = client.get(
        f"/api/v1/projects/{project_id}/editor", headers=owner["headers"]
    )
    assert editor_view.status_code == 200, editor_view.text
    revision_id = editor_view.json()["revision_id"]
    assert editor_view.json()["revision_number"] == 0

    studio = client.get(
        f"/api/v1/projects/{project_id}/revisions/{revision_id}/studio",
        headers=owner["headers"],
    )
    assert studio.status_code == 200, studio.text

    # A collaborator with the editor role loads it too.
    member_view = client.get(
        f"/api/v1/projects/{project_id}/editor", headers=member["headers"]
    )
    assert member_view.status_code == 200, member_view.text

    # Editing works exactly as before: one operation batch creates
    # the next revision, re-readable from the server.
    applied = client.post(
        f"/api/v1/projects/{project_id}/revisions/{revision_id}/operations",
        headers=owner["headers"],
        json={"operations": [{"type": "set_volumes", "music": 0.33}]},
    )
    assert applied.status_code == 201, applied.text
    new_revision_id = applied.json()["revision_id"]
    reread = client.get(
        f"/api/v1/projects/{project_id}/revisions/{new_revision_id}",
        headers=owner["headers"],
    )
    assert reread.status_code == 200, reread.text
    assert reread.json()["document"]["volumes"]["music"] == 0.33

    # The per-revision certificate (an Editor surface) loads.
    certificate = client.get(
        f"/api/v1/projects/{project_id}/revisions/{new_revision_id}/certificate",
        headers=owner["headers"],
    )
    assert certificate.status_code == 200, certificate.text
    assert certificate.json()["certificate_version"] == 2

    # A-10: a LATER failed run (headline project status now
    # "failed") does not lock the owner out — the gate keys on the
    # completed run + film, which still exist.
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        project.status = "failed"
        db.add(
            ProductionRun(
                id=_uid(),
                project_id=project_id,
                owner_id=owner["id"],
                status="failed",
                stage="video_editor",
                error="later run failed",
                script_hash=run_ledger.script_sha256(production["script"]),
            )
        )
        db.commit()
    finally:
        db.close()
    still_open = client.get(
        f"/api/v1/projects/{project_id}/editor", headers=owner["headers"]
    )
    assert still_open.status_code == 200, still_open.text

    # (3)+(4) With the finished video artefact gone, the ordinary
    # owner AND collaborator are refused (no_finished_video) even
    # though revisions exist — while the admin member keeps full
    # support access to the same editor state.
    (media_root / project_id / FILM_NAME).unlink()
    for label, headers in (
        ("owner", owner["headers"]),
        ("collaborator", member["headers"]),
    ):
        refused = client.get(
            f"/api/v1/projects/{project_id}/editor", headers=headers
        )
        _assert_gate_refusal(label, refused, "no_finished_video")
    admin_view = client.get(
        f"/api/v1/projects/{project_id}/editor",
        headers=admin_member["headers"],
    )
    assert admin_view.status_code == 200, admin_view.text


# =====================================================================
# FS-H — the Assistant side channel + backend-generated link targets
#
# The Assistant's service-layer read tools call services/ledgers
# in-process, bypassing the HTTP layer. These tests pin the FS-H
# rule at the tool boundary (the principal the tool receives):
# non-admin sessions get exactly the HTTP restriction (assets →
# retained editor-reuse fields only; research → recorded status
# only; plan → 403; certificate → 403 via the HTTP handler it
# delegates to; failure evidence → simple status; revision chain →
# count only; recommendation links → project Overview), while
# admin principals keep full detail. Ownership runs first, so a
# non-granted principal still gets the tenant-private 404.
# =====================================================================

from models import ProjectRevision  # noqa: E402
from services.assistant import context as assistant_context  # noqa: E402
from services.assistant import registry as assistant_registry  # noqa: E402
from services.assistant import tools_read as _tools_read  # noqa: F401,E402
from services.assistant import tools_search as _tools_search  # noqa: F401,E402

RESEARCH_MARKER = "FSH_SECRET_ENTITY_9F3"


def _principal_for(user, role=None):
    return auth_service.Principal(
        user_id=user["id"],
        email=user["email"],
        role=role or "user",
        auth_method="session",
    )


def _call_tool(principal, tool_name, **arguments):
    spec = assistant_registry.get(tool_name)
    assert spec is not None, tool_name
    return spec.handler(principal, spec.input_model(**arguments))


def _seed_research_artefact(media_root, project_id):
    project_dir = media_root / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "case_bible.json").write_text(
        json.dumps(
            {
                "entities": [{"name": RESEARCH_MARKER}],
                "timeline_events": [{"year": 1901, "event": RESEARCH_MARKER}],
                "script_facts": [RESEARCH_MARKER],
            }
        ),
        encoding="utf-8",
    )

def test_fsh_assistant_assets_field_restricted_for_non_admin(client, media_root):
    owner = _register(client, "fsh-assets-owner")
    admin = _make_user("admin")
    project_id, _run_id = _seed_production(owner["id"], media_root)
    _share(client, owner, admin, project_id, role="editor")

    result = _call_tool(
        _principal_for(owner), "get_project_assets", project_id=project_id
    )
    assert result["total"] == 2
    assert SOURCE_URL not in str(result)
    assert CONTENT_HASH not in str(result)
    for row in result["items"]:
        assert set(row) <= set(api_v1._EDITOR_LEDGER_FIELDS)

    full = _call_tool(
        _principal_for(admin, role="admin"),
        "get_project_assets",
        project_id=project_id,
    )
    assert SOURCE_URL in str(full)

def test_fsh_assistant_research_reduced_and_plan_refused(client, media_root):
    owner = _register(client, "fsh-research-owner")
    admin = _make_user("admin")
    stranger = _register(client, "fsh-research-stranger")
    project_id, _run_id = _seed_production(owner["id"], media_root)
    _share(client, owner, admin, project_id, role="editor")
    _seed_research_artefact(media_root, project_id)

    reduced = _call_tool(
        _principal_for(owner), "get_project_research", project_id=project_id
    )
    assert reduced["recorded"] is True
    assert RESEARCH_MARKER not in str(reduced)
    assert "entities" not in reduced

    full = _call_tool(
        _principal_for(admin, role="admin"),
        "get_project_research",
        project_id=project_id,
    )
    assert RESEARCH_MARKER in str(full)

    with pytest.raises(api_v1.ApiError) as caught:
        _call_tool(
            _principal_for(owner), "get_project_plan", project_id=project_id
        )
    assert caught.value.status_code == 403
    # Ownership still runs first: a stranger gets the 404, not 403.
    with pytest.raises(api_v1.ApiError) as caught:
        _call_tool(
            _principal_for(stranger), "get_project_plan", project_id=project_id
        )
    assert caught.value.status_code == 404

def test_fsh_assistant_certificate_fails_closed_for_non_admin(client, media_root):
    owner = _register(client, "fsh-cert-owner")
    admin = _make_user("admin")
    project_id, _run_id = _seed_production(owner["id"], media_root)
    _share(client, owner, admin, project_id, role="editor")

    with pytest.raises(api_v1.ApiError) as caught:
        _call_tool(
            _principal_for(owner),
            "get_project_certificate",
            project_id=project_id,
        )
    assert caught.value.status_code == 403

    certificate = _call_tool(
        _principal_for(admin, role="admin"),
        "get_project_certificate",
        project_id=project_id,
    )
    assert QA_MARKER in str(certificate)

def test_fsh_assistant_failure_evidence_and_activity_sanitized(client, media_root):
    owner = _register(client, "fsh-errors-owner")
    admin = _make_user("admin")
    project_id, _run_id = _seed_failed_project(owner["id"], media_root)
    _share(client, owner, admin, project_id, role="editor")

    reduced = _call_tool(
        _principal_for(owner), "get_production_errors", project_id=project_id
    )
    assert reduced["run_status"] == "failed"
    assert reduced["error"] == GENERIC
    assert INTERNAL_ERROR not in str(reduced)

    full = _call_tool(
        _principal_for(admin, role="admin"),
        "get_production_errors",
        project_id=project_id,
    )
    assert full["run_error"] == INTERNAL_ERROR

    activity = _call_tool(_principal_for(owner), "get_account_activity", limit=5)
    assert INTERNAL_ERROR not in str(activity)
    assert GENERIC in str(activity)

def test_fsh_assistant_render_status_reduces_revision_chain(client, media_root):
    owner = _register(client, "fsh-render-owner")
    admin = _make_user("admin")
    project_id, run_id = _seed_production(owner["id"], media_root)
    _share(client, owner, admin, project_id, role="editor")
    db = SessionLocal()
    try:
        db.add(
            ProjectRevision(
                id=_uid(),
                project_id=project_id,
                run_id=run_id,
                parent_revision_id=None,
                revision_number=0,
                status="draft",
                document_path="revision.json",
                document_hash="fsh-hash",
            )
        )
        db.commit()
    finally:
        db.close()

    reduced = _call_tool(
        _principal_for(owner), "get_render_status", project_id=project_id
    )
    assert reduced["revision_count"] == 1
    assert "revisions" not in reduced

    full = _call_tool(
        _principal_for(admin, role="admin"),
        "get_render_status",
        project_id=project_id,
    )
    assert len(full["revisions"]) == 1

def test_fsh_assistant_recommendation_links_repointed_for_non_admin(
    client, media_root
):
    owner = _register(client, "fsh-recs-owner")
    admin = _make_user("admin")
    project_id, run_id = _seed_production(owner["id"], media_root)
    _share(client, owner, admin, project_id, role="editor")
    run_dir = media_root / project_id / "runs" / run_id
    (run_dir / "asset_report.json").write_text(
        json.dumps(
            {
                "coverage": {
                    "total_sentences": 11,
                    "covered_sentences": 9,
                    "uncovered_sentences": [4, 7],
                    "complete": False,
                },
                "missing_visual_count": 0,
                "repeated_footage": 0,
                "unverified_assets": 0,
                "low_confidence_selections": 0,
                "download_failures": [],
                "discovery_failures": [],
            }
        ),
        encoding="utf-8",
    )

    result = _call_tool(
        _principal_for(owner),
        "get_project_recommendations",
        project_id=project_id,
    )
    assert result["recommendations"]
    for finding in result["recommendations"]:
        assert finding["link"] == f"/projects/{project_id}"

    full = _call_tool(
        _principal_for(admin, role="admin"),
        "get_project_recommendations",
        project_id=project_id,
    )
    assert any(
        finding["link"].endswith("/assets")
        for finding in full["recommendations"]
    )

def test_fsh_assistant_context_pack_sanitized_for_non_admin(client, media_root):
    owner = _register(client, "fsh-context-owner")
    admin = _make_user("admin")
    project_id, _run_id = _seed_failed_project(owner["id"], media_root)
    _share(client, owner, admin, project_id, role="editor")

    db = SessionLocal()
    try:
        pack = assistant_context.project_context_pack(
            db, _principal_for(owner), project_id
        )
        admin_pack = assistant_context.project_context_pack(
            db, _principal_for(admin, role="admin"), project_id
        )
    finally:
        db.close()
    assert INTERNAL_ERROR not in str(pack)
    assert GENERIC in str(pack)
    assert INTERNAL_ERROR in str(admin_pack)

def test_fsh_assistant_navigation_link_refuses_gated_destinations(
    client, media_root
):
    owner = _register(client, "fsh-nav-owner")
    admin = _make_user("admin")
    project_id, _run_id = _seed_production(owner["id"], media_root)
    _share(client, owner, admin, project_id, role="editor")

    with pytest.raises(api_v1.ApiError) as caught:
        _call_tool(
            _principal_for(owner),
            "generate_product_navigation_link",
            destination="assets",
            project_id=project_id,
        )
    assert caught.value.status_code == 403

    linked = _call_tool(
        _principal_for(admin, role="admin"),
        "generate_product_navigation_link",
        destination="assets",
        project_id=project_id,
    )
    assert linked["url"] == f"/projects/{project_id}/assets"
    # Kept destinations still generate for ordinary users.
    kept = _call_tool(
        _principal_for(owner),
        "generate_product_navigation_link",
        destination="project",
        project_id=project_id,
    )
    assert kept["url"] == f"/projects/{project_id}"

def test_fsh_outputs_payload_hides_admin_only_urls_for_non_admin(
    client, media_root
):
    owner = _register(client, "fsh-outputs-owner")
    admin = _make_user("admin")
    project_id, _run_id = _seed_production(owner["id"], media_root)
    _share(client, owner, admin, project_id, role="editor")

    outputs = client.get(
        f"/api/v1/projects/{project_id}/outputs", headers=owner["headers"]
    )
    assert outputs.status_code == 200, outputs.text
    assert outputs.json()["certificate_url"] is None
    assert outputs.json()["plan_url"] is None
    assert outputs.json()["film"]["exists"] is False  # kept flow intact

    admin_outputs = client.get(
        f"/api/v1/projects/{project_id}/outputs", headers=admin["headers"]
    )
    assert admin_outputs.status_code == 200, admin_outputs.text
    assert admin_outputs.json()["certificate_url"].endswith("/certificate")
    assert admin_outputs.json()["plan_url"].endswith("/plan")
