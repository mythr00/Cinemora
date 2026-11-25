"""FS-D (FS-2) — genre pack selection is removed from every user
flow; the documentary treatment is backend-derived from the script.

Before FS-D a user chose a genre pack in the Create / dashboard
forms and the choice reached pack resolution through TWO user-fed
inputs:

1. the create flow's ``options.genre`` (stored on the project and
   honoured by resolve_genre_pack's "option" branch), and
2. a jobs.py backdoor (the run-9 wiring) that injected the
   project's stored ``category`` label into the run options as
   ``project_category``, where it outranked script detection.

FS-D cuts both:

- the frontend sends no genre and no genre-derived category
  (covered by the frontend grep/tsc checks, not here);
- ``api_v1._create_project_record`` strips ``genre``/``genre_pack``
  from user options before storage, and ``submit_run`` strips them
  from the merged run options — payloads carrying them are still
  accepted (old clients keep working), the values are just inert;
- ``jobs._options_for_genre_resolution`` replaces the category
  injection: it returns a copy of the run options with ``genre``,
  ``genre_pack`` and ``project_category`` removed, so BOTH jobs
  resolution sites (analysis phase and full pipeline) always land
  on resolve_genre_pack's detected/default branches — for every
  project, old and new, whatever its stored category/options say.

These tests pin that contract hermetically — project creation via
the real API, resolution via the exact function chain jobs runs.
No production is ever run. Users are created through the real
registration path with sessions in fakeredis, as in
tests/test_fs_access_rules.py.

Detector note (founder-accepted consequence): detect_genre only
distinguishes true_crime / history / general, so scripts a user
once hand-set to e.g. engineering now resolve via the detector
(typically general). That is FS-2's intended behaviour, not a bug
these tests should engineer around.
"""

import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import jobs
import main
from database import SessionLocal, engine
from models import Base, Plan, Project, ProductionRun, User, UserSubscription
from services import auth as auth_service
from services import genre_packs

TOKEN = "fs-d-" + "operator-token-for-tests"
PASSWORD = "fs-d-" + "correct-horse-42"

# Unmistakable true-crime signal: many distinct detector cues
# (murder, victim, suspect, arrested, crime scene, forensic, dna,
# trial, guilty, homicide, investigation, convicted, sentenced,
# detective, police said) and no history cues or years at all.
TRUE_CRIME_SCRIPT = (
    "The detective stood over the victim at the crime scene. "
    "Police said the suspect had been arrested nearby, and forensic "
    "teams recovered dna from the knife. The trial began in spring: "
    "the prosecutor told the jury the defendant was guilty of the "
    "murder, and after the homicide investigation the court "
    "convicted him and sentenced him to life in prison."
)

# Detector-neutral: no crime, war or history markers.
NEUTRAL_SCRIPT = "A quiet film about bread making and village life."


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


def _register(client):
    email = f"fsd-{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {
        "id": body["user"]["id"],
        "headers": {"Authorization": f"Bearer {body['session_token']}"},
    }


def _stored_project(project_id):
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        assert project is not None
        return {
            "category": project.category,
            "options": dict(project.options or {}),
            "script": project.script,
        }
    finally:
        db.close()


def _jobs_resolution(options, script):
    """The exact resolution chain both jobs sites run (analysis
    phase and full pipeline): strip the user-fed genre keys, then
    resolve. Hermetic — no production run."""
    return genre_packs.resolve_genre_pack(
        jobs._options_for_genre_resolution(options), script
    )


def _create(client, headers, script, **extra):
    payload = {"title": "FS-D project", "script": script}
    payload.update(extra)
    response = client.post("/api/v1/projects", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ---------------------------------------------------------------------
# (1) Creation with NO genre succeeds; the pack derives from the
#     script.
# ---------------------------------------------------------------------


def test_create_without_genre_derives_pack_from_script(client):
    owner = _register(client)
    project_id = _create(client, owner["headers"], TRUE_CRIME_SCRIPT)

    stored = _stored_project(project_id)
    # Nothing genre-shaped was stored: there is no user choice on
    # the project for any later path to honour.
    assert "genre" not in stored["options"]
    assert "genre_pack" not in stored["options"]

    info = _jobs_resolution(stored["options"], stored["script"])
    assert info["pack_id"] == "true_crime"
    assert info["source"] == "detected"
    assert info["pack"] is not None
    assert info["pack"]["id"] == "true_crime"


# ---------------------------------------------------------------------
# (2) Creation WITH a conflicting user-supplied genre derives the
#     SAME pack as without it — the user value is ineffective, and
#     the payload is still accepted (old clients keep working).
# ---------------------------------------------------------------------


def test_create_with_conflicting_genre_derives_same_pack(client):
    owner = _register(client)

    baseline_id = _create(client, owner["headers"], TRUE_CRIME_SCRIPT)
    baseline = _stored_project(baseline_id)
    baseline_info = _jobs_resolution(baseline["options"], baseline["script"])

    conflict_id = _create(
        client,
        owner["headers"],
        TRUE_CRIME_SCRIPT,
        category="History",
        options={"genre": "history", "genre_pack": "history"},
    )
    conflict = _stored_project(conflict_id)

    # The supplied genre never reached storage...
    assert "genre" not in conflict["options"]
    assert "genre_pack" not in conflict["options"]
    # ...and resolution derives exactly the pack the genre-less
    # creation derived: the script's, not the user's.
    info = _jobs_resolution(conflict["options"], conflict["script"])
    assert info["pack_id"] == baseline_info["pack_id"] == "true_crime"
    assert info["source"] == baseline_info["source"] == "detected"


def test_strip_helper_keeps_other_options():
    # Unit pin of the API-side strip: only the genre keys go.
    cleaned = api_v1._strip_user_genre_options(
        {"genre": "history", "genre_pack": "history", "voice_id": "v1"}
    )
    assert cleaned == {"voice_id": "v1"}
    assert api_v1._strip_user_genre_options(None) == {}


# ---------------------------------------------------------------------
# (3) Jobs-time resolution for a project whose STORED category (and
#     stored options) name a different pack still derives from the
#     script — the run-9 backdoor is closed for existing projects.
# ---------------------------------------------------------------------


def test_jobs_resolution_ignores_stored_category_and_genre():
    project_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(
            Project(
                id=project_id,
                title="FS-D legacy project",
                script=TRUE_CRIME_SCRIPT,
                # A pre-FS-D project: the stored category is a genre
                # label and the stored options carry the user's
                # genre choice — both used to win over detection.
                category="history",
                options={"genre": "history", "genre_pack": "history"},
                status="created",
            )
        )
        db.commit()
    finally:
        db.close()

    stored = _stored_project(project_id)
    # The stored data itself is untouched (no migration, no
    # rewrite) — it is simply no longer consumed as a selection.
    assert stored["category"] == "history"
    assert stored["options"]["genre"] == "history"

    # The run options a submission would merge from this project,
    # passed through the exact chain jobs runs.
    run_options = dict(stored["options"])
    info = _jobs_resolution(run_options, stored["script"])
    assert info["pack_id"] == "true_crime"
    assert info["source"] == "detected"

    # The seam itself: every user-fed key is gone from the copy,
    # the caller's dict is not mutated, and the run-9 injection
    # helpers no longer exist.
    cleaned = jobs._options_for_genre_resolution(run_options)
    assert "genre" not in cleaned
    assert "genre_pack" not in cleaned
    assert "project_category" not in cleaned
    assert run_options == {"genre": "history", "genre_pack": "history"}
    assert not hasattr(jobs, "_options_with_project_category")
    assert not hasattr(jobs, "_project_category")


# ---------------------------------------------------------------------
# (4) A generic script derives the general pack — no crash, and no
#     None pack can reach the 15 consumer sites.
# ---------------------------------------------------------------------


def test_generic_script_derives_general_pack(client):
    owner = _register(client)
    project_id = _create(client, owner["headers"], NEUTRAL_SCRIPT)
    stored = _stored_project(project_id)

    info = _jobs_resolution(stored["options"], stored["script"])
    assert info["pack_id"] == "general"
    assert info["source"] == "default"
    assert info["pack"] is not None
    assert info["pack"]["id"] == "general"
