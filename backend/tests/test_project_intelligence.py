"""Project Intelligence (founder priority #10; gap-audit doc 05
§2, doc 01 §1.12) — the assembly layer over the persistent
project record (services/project_intelligence.py).

Everything shown is either RECORDED (bible entities, run rows, QA
verdicts via the Phase F assembly, provider funnels from asset
reports) or DERIVED with its derivation labelled (entities shared
across Case Bibles by normalised name + recorded type; entities
mentioned in recorded timeline text). These tests write fixtures
shaped like the real records (modelled on the stored Container
production) and assert the surfaces reproduce those records
exactly — sums are recountable, verdicts pass through unaltered,
and a rule with no evidence produces no recommendation.

Ownership: another user's project is a 404, and another user's
data enters NO account aggregate — not even a count.
"""

import json
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from database import SessionLocal, engine
from models import Base, Plan
from services import auth as auth_service
from services import media_paths, project_intelligence, run_ledger

TOKEN = "project-intelligence-token"
PASSWORD = "project-intelligence-password"
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


def _create_project(client, headers=HEADERS, category="Documentary", title=None):
    body = {
        "script": f"Script {uuid.uuid4()}. Alpha one. Beta two.",
        "category": category,
    }
    if title:
        body["title"] = title
    response = client.post("/api/v1/projects", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _completed_run(project):
    submission = run_ledger.submit_production(project["id"], project["script"])
    run_id = submission["run_id"]
    run_ledger.claim_run(run_id)
    run_ledger.finish_run(run_id, "completed")
    return run_id


def _write_bible(project_id, bible):
    project_dir = media_paths.project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "case_bible.json").write_text(
        json.dumps(bible), encoding="utf-8"
    )


def _write_run_artefact(project_id, run_id, filename, payload):
    run_dir = media_paths.project_dir(project_id) / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / filename).write_text(json.dumps(payload), encoding="utf-8")


def _entity(entity_id, canonical, type_, **extra):
    return {
        "id": entity_id,
        "canonical": canonical,
        "type": type_,
        "role": extra.get("role"),
        "aliases": extra.get("aliases", []),
        "description": extra.get("description"),
        "sources": [],
        "support": extra.get("support", "script_asserted"),
    }


def _bible(entities, timeline=None, primary=None):
    return {
        "title": "Fixture",
        "story_type": "documentary",
        "time_period": "1950s",
        "primary_subject_id": primary,
        "entities": entities,
        "timeline": timeline or [],
        "script_facts": {"facts": [], "record": None},
        "mode": "recorded",
        "conflicts": [],
    }


def _production_report(quality_verdict):
    return {
        "schema": "cinemora.production_report/1",
        "verdicts": {
            "integrity": {"verdict": "PASS", "failures": []},
            "quality": {"verdict": quality_verdict, "metrics": {}},
            "reliability": {"verdict": "UNKNOWN"},
        },
        "reconciliation": {"all_reconciled": True, "failures": [], "checks": []},
        "questions": [],
    }


def _asset_report(funnel=None, **extra):
    report = {
        "coverage": {"total_sentences": 10, "covered_sentences": 10,
                     "uncovered_sentences": []},
        "missing_visual_count": 0,
        "repeated_footage": 0,
        "unverified_assets": 0,
        "low_confidence_selections": 0,
        "download_failures": [],
        "discovery_failures": [],
        "providers": {"funnel": funnel or {}, "single_provider_reliance": {}},
    }
    report.update(extra)
    return report


# ---------------------------------------------------------------------
# Per-project: honest absence
# ---------------------------------------------------------------------


def test_project_intelligence_no_records(client):
    project = _create_project(client)
    response = client.get(
        f"/api/v1/projects/{project['id']}/intelligence", headers=HEADERS
    )
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["record"]["bible_recorded"] is False
    assert view["entities"] == []
    assert view["quality"]["state"] == "not_recorded"
    assert view["quality"]["quality"] is None
    assert view["connections"]["shared_entities"] == []
    assert view["connections"]["timeline_mentions"] == []
    recs = view["recommendations"]
    assert recs["findings_total"] == 0
    states = {rule["rule"]: rule["state"] for rule in recs["rules"]}
    assert states["run_quality"] == "not_recorded"
    assert states["shared_entity_coverage"] == "not_recorded"
    assert states["underused_verified_providers"] == "not_recorded"


# ---------------------------------------------------------------------
# Per-project: recorded entities + verdicts pass through
# ---------------------------------------------------------------------


def test_project_intelligence_recorded_entities_and_verdicts(client):
    project = _create_project(client, title="Harbour Story")
    _write_bible(
        project["id"],
        _bible(
            [
                _entity("ideal_x", "Ideal X", "object", role="ship"),
                _entity("malcom", "Malcom McLean", "person"),
            ],
            primary="ideal_x",
        ),
    )
    run_id = _completed_run(project)
    _write_run_artefact(
        project["id"], run_id, "production_report.json",
        _production_report("QUALITY_WARNING"),
    )
    _write_run_artefact(project["id"], run_id, "asset_report.json", _asset_report())

    view = client.get(
        f"/api/v1/projects/{project['id']}/intelligence", headers=HEADERS
    ).json()
    assert view["record"]["bible_recorded"] is True
    assert view["record"]["runs"]["production"] == 1
    by_id = {e["entity_id"]: e for e in view["entities"]}
    assert by_id["ideal_x"]["is_primary_subject"] is True
    assert by_id["malcom"]["is_primary_subject"] is False
    assert view["connections"]["primary_subject_id"] == "ideal_x"
    # The recorded verdict passes through unaltered.
    assert view["quality"]["state"] == "recorded"
    assert view["quality"]["quality"] == "QUALITY_WARNING"
    assert view["quality"]["integrity"] == "PASS"
    assert view["quality"]["reliability"] == "UNKNOWN"


# ---------------------------------------------------------------------
# Entity connections: shared across projects (derived, labelled)
# ---------------------------------------------------------------------


def test_shared_entities_match_on_name_and_type(client):
    a = _create_project(client, title="Film A")
    b = _create_project(client, title="Film B")
    c = _create_project(client, title="Film C")
    _write_bible(a["id"], _bible([_entity("e1", "Malcom McLean", "person")]))
    _write_bible(b["id"], _bible([_entity("e9", "malcom mclean", "person")]))
    # Same name, different recorded type: NOT a match.
    _write_bible(c["id"], _bible([_entity("e3", "Malcom McLean", "organisation")]))

    account = client.get("/api/v1/intelligence", headers=HEADERS).json()
    shared = account["entity_connections"]
    assert len(shared) == 1
    entry = shared[0]
    assert entry["canonical"] == "Malcom McLean"
    assert entry["project_count"] == 2
    assert {ap["project_id"] for ap in entry["appearances"]} == {a["id"], b["id"]}
    assert "derived" in entry["derivation"]

    view_a = client.get(
        f"/api/v1/projects/{a['id']}/intelligence", headers=HEADERS
    ).json()
    mine = view_a["connections"]["shared_entities"]
    assert len(mine) == 1
    assert mine[0]["other_projects"][0]["project_id"] == b["id"]
    # Project C shares nothing.
    view_c = client.get(
        f"/api/v1/projects/{c['id']}/intelligence", headers=HEADERS
    ).json()
    assert view_c["connections"]["shared_entities"] == []


def test_timeline_mentions_are_derived_and_labelled(client):
    project = _create_project(client)
    _write_bible(
        project["id"],
        _bible(
            [
                _entity("ideal_x", "Ideal X", "object", aliases=["the Ideal X"]),
                _entity("mclean", "Malcom McLean", "person"),
            ],
            timeline=[
                {
                    "when": "April 26, 1956",
                    "what": "The Ideal X left Newark carrying 58 steel boxes.",
                    "support": "script_asserted",
                }
            ],
        ),
    )
    view = client.get(
        f"/api/v1/projects/{project['id']}/intelligence", headers=HEADERS
    ).json()
    mentions = view["connections"]["timeline_mentions"]
    assert len(mentions) == 1
    assert mentions[0]["entity_id"] == "ideal_x"
    assert mentions[0]["events"][0]["when"] == "April 26, 1956"
    assert mentions[0]["events"][0]["matched_name"] == "Ideal X"
    assert "derived" in mentions[0]["derivation"]


# ---------------------------------------------------------------------
# Account patterns: providers + quality by category (recountable)
# ---------------------------------------------------------------------


def test_provider_aggregate_sums_recorded_funnels(client):
    a = _create_project(client, title="Prov A")
    b = _create_project(client, title="Prov B")
    run_a = _completed_run(a)
    _write_run_artefact(
        a["id"], run_a, "asset_report.json",
        _asset_report(funnel={
            "archive.org": {"discovered": 10, "verified": 3, "rejected": 6,
                            "downloaded": 3, "download_failed": 1},
            "wikimedia": {"discovered": 5, "verified": 5, "rejected": 0,
                          "downloaded": 5, "download_failed": 0},
        }),
    )
    run_b = _completed_run(b)
    _write_run_artefact(
        b["id"], run_b, "asset_report.json",
        _asset_report(funnel={
            "archive.org": {"discovered": 4, "verified": 1, "rejected": 3,
                            "downloaded": 1, "download_failed": 0},
        }),
    )
    account = client.get("/api/v1/intelligence", headers=HEADERS).json()
    providers = {p["provider"]: p for p in account["providers"]["providers"]}
    assert account["providers"]["projects_with_funnel"] >= 2
    archive = providers["archive.org"]
    assert archive["discovered"] == 14
    assert archive["verified"] == 4
    assert archive["rejected"] == 9
    assert archive["download_failed"] == 1
    assert archive["project_count"] == 2
    assert providers["wikimedia"]["verified"] == 5
    assert providers["wikimedia"]["project_count"] == 1


def test_quality_by_category_counts_recorded_verdicts(client):
    a = _create_project(client, category="Engineering", title="Q A")
    b = _create_project(client, category="Engineering", title="Q B")
    c = _create_project(client, category="Engineering", title="Q C")
    for project, verdict in ((a, "PASS"), (b, "QUALITY_WARNING")):
        run_id = _completed_run(project)
        _write_run_artefact(
            project["id"], run_id, "production_report.json",
            _production_report(verdict),
        )
        _write_run_artefact(
            project["id"], run_id, "asset_report.json", _asset_report()
        )
    # C has a run but recorded no production report: counted as a
    # project, not as a verdict.
    _completed_run(c)

    account = client.get("/api/v1/intelligence", headers=HEADERS).json()
    engineering = next(
        row for row in account["quality_by_category"]
        if row["category"] == "Engineering"
    )
    assert engineering["projects"] == 3
    assert engineering["with_recorded_verdict"] == 2
    assert engineering["quality_verdicts"] == {"PASS": 1, "QUALITY_WARNING": 1}
    categories = {row["category"]: row["projects"]
                  for row in account["projects_by_category"]}
    assert categories["Engineering"] == 3


# ---------------------------------------------------------------------
# Recommendations: rules fire only on recorded evidence
# ---------------------------------------------------------------------


def test_run_findings_carry_recorded_evidence():
    report = _asset_report(
        coverage={"total_sentences": 10, "covered_sentences": 7,
                  "uncovered_sentences": [3, 5, 9]},
        repeated_footage=2,
        providers={
            "funnel": {},
            "single_provider_reliance": {
                "warning": True, "provider": "wikimedia",
                "footage_share": None, "footage_base": 0,
                "stills_share": 1.0, "stills_base": 13,
            },
        },
    )
    findings = project_intelligence.run_findings("p1", "r1", report)
    by_name = {f["finding"]: f for f in findings}
    assert by_name["Incomplete visual coverage"]["evidence"][
        "covered_sentences"] == 7
    assert by_name["Repeated footage"]["evidence"]["repeated_footage"] == 2
    reliance = by_name["Single-provider reliance"]
    assert reliance["evidence"]["provider"] == "wikimedia"
    assert reliance["evidence"]["stills_share"] == 1.0
    # A clean report produces no findings at all.
    assert project_intelligence.run_findings("p1", "r1", _asset_report()) == []


def test_recommendation_shared_entity_coverage(client):
    target = _create_project(client, title="Target")
    other1 = _create_project(client, title="Other 1")
    other2 = _create_project(client, title="Other 2")
    _write_bible(target["id"], _bible([_entity("t1", "Harbour Crane", "object")]))
    for project in (other1, other2):
        _write_bible(
            project["id"],
            _bible([_entity("x1", "Sea-Land Service", "organisation")]),
        )
    view = client.get(
        f"/api/v1/projects/{target['id']}/intelligence", headers=HEADERS
    ).json()
    rule = next(
        r for r in view["recommendations"]["rules"]
        if r["rule"] == "shared_entity_coverage"
    )
    assert len(rule["findings"]) == 1
    evidence = rule["findings"][0]["evidence"]
    assert evidence["canonical"] == "Sea-Land Service"
    assert evidence["project_count"] == 2
    assert {p["project_id"] for p in evidence["projects"]} == {
        other1["id"], other2["id"]
    }


def test_recommendation_underused_verified_provider(client):
    target = _create_project(client, title="Target P")
    other = _create_project(client, title="Other P")
    run_t = _completed_run(target)
    _write_run_artefact(
        target["id"], run_t, "asset_report.json",
        _asset_report(funnel={
            "archive.org": {"discovered": 6, "verified": 0, "rejected": 6,
                            "downloaded": 0, "download_failed": 0},
        }),
    )
    run_o = _completed_run(other)
    _write_run_artefact(
        other["id"], run_o, "asset_report.json",
        _asset_report(funnel={
            "archive.org": {"discovered": 9, "verified": 4, "rejected": 5,
                            "downloaded": 4, "download_failed": 0},
        }),
    )
    view = client.get(
        f"/api/v1/projects/{target['id']}/intelligence", headers=HEADERS
    ).json()
    rule = next(
        r for r in view["recommendations"]["rules"]
        if r["rule"] == "underused_verified_providers"
    )
    assert len(rule["findings"]) == 1
    evidence = rule["findings"][0]["evidence"]
    assert evidence["provider"] == "archive.org"
    assert evidence["verified_in_other_projects"] == 4
    assert evidence["verified_in_this_project"] == 0
    # The other project itself gets no such finding for archive.org.
    view_o = client.get(
        f"/api/v1/projects/{other['id']}/intelligence", headers=HEADERS
    ).json()
    rule_o = next(
        r for r in view_o["recommendations"]["rules"]
        if r["rule"] == "underused_verified_providers"
    )
    assert rule_o["findings"] == []


# ---------------------------------------------------------------------
# Ownership: 404 across tenants; no foreign data in aggregates
# ---------------------------------------------------------------------


def _register(client, email):
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(user):
    return {"Authorization": f"Bearer {user['session_token']}"}


def test_intelligence_cross_tenant_404_and_isolation(client):
    suffix = uuid.uuid4().hex[:8]
    a = _register(client, f"pi-a-{suffix}@example.com")
    b = _register(client, f"pi-b-{suffix}@example.com")
    project = _create_project(client, headers=_bearer(a), title="A only")
    _write_bible(
        project["id"], _bible([_entity("e1", "Secret Entity", "person")])
    )
    run_id = _completed_run(project)
    _write_run_artefact(
        project["id"], run_id, "asset_report.json",
        _asset_report(funnel={
            "secret-provider": {"discovered": 3, "verified": 3, "rejected": 0,
                                "downloaded": 3, "download_failed": 0},
        }),
    )

    response = client.get(
        f"/api/v1/projects/{project['id']}/intelligence", headers=_bearer(b)
    )
    assert response.status_code == 404, response.text

    account_b = client.get("/api/v1/intelligence", headers=_bearer(b)).json()
    assert account_b["totals"]["projects"] == 0
    assert account_b["entity_connections"] == []
    assert account_b["providers"]["providers"] == []
    assert all(
        row["project_id"] != project["id"] for row in account_b["projects"]
    )

    # The owner sees their own data.
    account_a = client.get("/api/v1/intelligence", headers=_bearer(a)).json()
    assert account_a["totals"]["projects"] == 1
    providers = {p["provider"] for p in account_a["providers"]["providers"]}
    assert "secret-provider" in providers
