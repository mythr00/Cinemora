"""Research Studio (founder priority #6; gap-audit Phase D v1).

Covers services/research_studio.py and its api_v1 routes:

- credibility tiers from the published provider+domain rubric;
- research sessions over the EXISTING machinery (services.research
  query angles + kb_client topic verdict), persisted + URL-deduped,
  with the honest 503 when every search fails;
- the Case Bible surface (entities / merged timeline / fact table
  with basis stamps) read exactly as recorded, both fact shapes;
- fact-check: the pipeline's own corroboration upgrades a fact and
  writes the bible back;
- entity KB checks: a type disagreement is recorded as a conflict
  with both sides, never adjudicated;
- entity corrections: overlay in the bible view AND applied to an
  analysed bible by the same function jobs.build_production_scenes
  calls, so a correction changes the next plan;
- citations (fact / statement / external), bibliography, user
  timeline events, cited summaries (and their refusals);
- gaps derived from recorded data only;
- Phase 8 ownership: user B gets 404 on user A's research, reads
  and writes alike.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

import api_v1
import main
from database import SessionLocal, engine
from models import Base, Plan, Project, User, UserSubscription
from services import auth as auth_service
from services import kb_client, llm_client, media_paths, research
from services import research_studio as rs

BACKEND = Path(__file__).resolve().parent.parent
TOKEN = "research-studio-test-token"
PASSWORD = "correct horse battery staple"


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("SOURCE_INTEL_DB", str(tmp_path / "source_intel.db"))
    monkeypatch.delenv("CINEMORA_ALLOW_OPERATOR_TOKEN", raising=False)
    api_v1.rate_limiter.reset()
    api_v1.auth_rate_limiter.reset()
    Base.metadata.create_all(bind=engine)
    auth_service.set_redis_client(fakeredis.FakeRedis(decode_responses=True))
    db = SessionLocal()
    try:
        for model in (Project, UserSubscription, User):
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
    return response.json()


def _bearer(user):
    return {"Authorization": f"Bearer {user['session_token']}"}


@pytest.fixture()
def users(client):
    suffix = uuid.uuid4().hex[:8]
    a = _register(client, f"rs-a-{suffix}@example.com")
    b = _register(client, f"rs-b-{suffix}@example.com")
    return a, b


@pytest.fixture()
def project_id(client, users):
    a, _b = users
    response = client.post(
        "/api/v1/projects",
        headers=_bearer(a),
        json={
            "title": "Harbour Cranes",
            "script": "On April 26, 1956, the Ideal X carried 58 containers.",
            "category": "industrial",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _write_bible(project_id, bible):
    project_dir = media_paths.project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / "case_bible.json"
    path.write_text(json.dumps(bible), encoding="utf-8")
    return path


def _sample_bible():
    return {
        "title": "The Ideal X",
        "story_type": "industrial",
        "story_era": "1950s",
        "mode": "llm",
        "primary_subject_id": "ideal_x",
        "entities": [
            {
                "id": "ideal_x",
                "canonical": "Ideal X",
                "type": "object",
                "role": "ship",
                "aliases": [],
                "description": "Converted tanker.",
            },
            {
                "id": "malcom_mclean",
                "canonical": "Malcom McLean",
                "type": "place",
                "role": "founder",
                "aliases": ["McLean"],
                "description": "",
                "type_low_confidence": True,
            },
        ],
        "timeline": [
            {
                "when": "April 26, 1956",
                "what": "The Ideal X sails with 58 containers.",
                "support": "script_asserted",
                "fact_basis": "script_claim",
                "fact_id": "sf_0001",
                "sentence_number": 1,
            }
        ],
        "script_facts": {
            "facts": [
                {
                    "fact_id": "sf_0001",
                    "kind": "dated_event",
                    "when": "April 26, 1956",
                    "what": "On April 26, 1956, the Ideal X carried 58 containers.",
                    "year": 1956,
                    "sentence_number": 1,
                    "basis": "script_claim",
                    "source": {"type": "script", "sentence_number": 1},
                    "subjects": ["Ideal X"],
                },
                {
                    "fact_id": "sf_0002",
                    "kind": "quantity",
                    "display": "58 containers",
                    "sentence_number": 1,
                    "basis": "script_claim",
                    "source": {"type": "script", "sentence_number": 1},
                },
            ],
            "record": {"sentences": 1, "facts": 2, "dated_events": 1,
                       "dates": 0, "quantities": 1},
        },
    }


def _stub_search(monkeypatch, results_by_query=None, fail=False):
    def _search(query):
        if fail:
            raise RuntimeError("SERPER_API_KEY is missing")
        return list((results_by_query or {}).get(query, []))

    monkeypatch.setattr(research, "search_web", _search)


def _stub_kb(monkeypatch, verdict):
    monkeypatch.setattr(kb_client, "lookup_entity", lambda name, **kw: verdict)


_KB_SHIP = {
    "query": "Ideal X", "status": "verified", "type": "object",
    "confidence": 0.95, "type_source": "wikidata",
    "description": "Ideal X was a 1956 container ship conversion.",
    "evidence": "verified", "aliases": [], "signals": [],
}

_KB_NOTHING = {
    "query": "x", "status": "not_found", "type": None, "confidence": 0.0,
    "type_source": None, "description": "", "evidence": None,
    "aliases": [], "signals": [],
}


# ---------------------------------------------------------------------
# Credibility tiers
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,provider,expected",
    [
        ("https://www.nps.gov/history.htm", "google_serper", "primary_source"),
        ("https://archive.org/details/x", "archive_org", "primary_source"),
        ("https://en.wikipedia.org/wiki/Ideal_X", "google_serper", "institutional"),
        ("https://www.wikidata.org/wiki/Q1", "google_serper", "institutional"),
        ("https://www.mit.edu/news", "google_serper", "institutional"),
        ("https://www.bbc.com/news/x", "google_serper", "established_press"),
        ("https://randomblog.example/post", "google_serper", "web"),
        ("", "google_serper", "unknown"),
        (None, None, "unknown"),
    ],
)
def test_credibility_tier_rubric(url, provider, expected):
    assert rs.credibility_tier(url, provider) == expected


# ---------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------


def test_session_persists_sources_and_dedupes(project_id, monkeypatch):
    results = {
        "Harbour cranes": [
            {"title": "Port history", "url": "https://example.com/port",
             "snippet": "Cranes.", "source": "google_serper"},
            {"title": "NPS record", "url": "https://www.nps.gov/port",
             "snippet": "Official.", "source": "google_serper"},
        ],
    }
    _stub_search(monkeypatch, results)
    _stub_kb(monkeypatch, _KB_NOTHING)

    session = rs.run_session(project_id, "Harbour cranes")
    # Two of the engine's query angles use the bare topic, so the
    # stub serves the same pair twice: 4 raw results, 2 unique URLs.
    assert session["record"]["results"] == 4
    assert session["record"]["new_sources"] == 2
    assert len(session["source_ids"]) == 2
    assert session["kb_verdict"]["status"] == "not_found"
    tiers = {s["url"]: s["tier"] for s in session["sources"]}
    assert tiers["https://www.nps.gov/port"] == "primary_source"
    assert tiers["https://example.com/port"] == "web"

    again = rs.run_session(project_id, "Harbour cranes")
    assert again["record"]["new_sources"] == 0
    assert again["record"]["reused_sources"] == 2
    library = rs.list_sources(project_id)
    assert library["total"] == 2
    assert library["counts"] == {"candidate": 2, "kept": 0, "discarded": 0}


def test_session_search_unavailable_is_honest(project_id, monkeypatch):
    _stub_search(monkeypatch, fail=True)
    _stub_kb(monkeypatch, _KB_NOTHING)
    with pytest.raises(rs.ResearchError) as caught:
        rs.run_session(project_id, "Anything")
    assert caught.value.status_code == 503
    assert caught.value.code == "search_unavailable"
    assert rs.list_sessions(project_id) == []


def test_source_state_transitions(project_id, monkeypatch):
    _stub_search(monkeypatch, {"T": [{"title": "A", "url": "https://a.example/1",
                                      "snippet": "", "source": "google_serper"}]})
    _stub_kb(monkeypatch, _KB_NOTHING)
    session = rs.run_session(project_id, "T")
    source_id = session["sources"][0]["id"]
    kept = rs.set_source_state(project_id, source_id, "kept")
    assert kept["state"] == "kept"
    with pytest.raises(rs.ResearchError):
        rs.set_source_state(project_id, source_id, "bogus")
    with pytest.raises(rs.ResearchError):
        rs.set_source_state(project_id, "src_missing", "kept")


# ---------------------------------------------------------------------
# Bible surface
# ---------------------------------------------------------------------


def test_bible_view_empty_is_honest(project_id):
    view = rs.bible_view(project_id)
    assert view["recorded"] is False
    assert view["entities"] == [] and view["facts"] == []


def test_bible_view_projects_recorded_bible(project_id):
    _write_bible(project_id, _sample_bible())
    view = rs.bible_view(project_id)
    assert view["recorded"] is True
    assert view["title"] == "The Ideal X"
    assert len(view["entities"]) == 2
    assert view["entities"][0]["effective_type"] == "object"
    assert view["timeline"][0]["origin"] == "case_bible"
    assert view["timeline"][0]["fact_basis"] == "script_claim"
    assert len(view["facts"]) == 2
    assert view["fact_record"]["dated_events"] == 1


def test_bible_view_accepts_bare_list_fact_shape(project_id):
    bible = _sample_bible()
    bible["script_facts"] = bible["script_facts"]["facts"]
    _write_bible(project_id, bible)
    view = rs.bible_view(project_id)
    assert len(view["facts"]) == 2
    assert view["fact_record"] is None


# ---------------------------------------------------------------------
# Fact check
# ---------------------------------------------------------------------


def test_fact_check_upgrades_and_writes_back(project_id, monkeypatch):
    path = _write_bible(project_id, _sample_bible())
    _stub_kb(monkeypatch, _KB_SHIP)
    result = rs.run_fact_check(project_id)
    assert result["run"]["record"]["corroborated"] == 1
    assert result["bases"] == {"script_claim": 1, "research_supported": 1}
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    upgraded = next(
        f for f in on_disk["script_facts"]["facts"] if f["fact_id"] == "sf_0001"
    )
    assert upgraded["basis"] == "research_supported"
    assert upgraded["source"]["type"] == "kb"
    assert on_disk["timeline"][0]["fact_basis"] == "research_supported"
    overview = rs.overview(project_id)
    assert overview["counts"]["facts_research_supported"] == 1


def test_fact_check_without_bible_is_404(project_id):
    with pytest.raises(rs.ResearchError) as caught:
        rs.run_fact_check(project_id)
    assert caught.value.status_code == 404


# ---------------------------------------------------------------------
# Entity checks + corrections
# ---------------------------------------------------------------------


def test_entity_check_records_conflict_without_adjudicating(
    project_id, monkeypatch
):
    _write_bible(project_id, _sample_bible())
    # The bible records Malcom McLean as a place; the KB says person.
    _stub_kb(monkeypatch, {**_KB_SHIP, "type": "person", "query": "Malcom McLean"})
    check = rs.check_entity(project_id, "malcom_mclean")
    assert check["conflict"] is True
    assert check["bible_type"] == "place"
    assert check["kb"]["type"] == "person"
    # The bible on disk is untouched — presentation, not adjudication.
    bible = json.loads(
        (media_paths.project_dir(project_id) / "case_bible.json").read_text()
    )
    assert bible["entities"][1]["type"] == "place"
    gaps = rs.compute_gaps(project_id, bible, rs.load_store(project_id))
    assert gaps["entity_type_conflicts"]["count"] == 1
    assert gaps["low_confidence_entity_types"]["entity_ids"] == ["malcom_mclean"]


def test_correction_overlays_view_and_applies_to_next_analysis(project_id):
    _write_bible(project_id, _sample_bible())
    correction = rs.add_correction(project_id, "malcom_mclean", "type", "person")
    assert correction["original"] == "place"

    view = rs.bible_view(project_id)
    entity = next(e for e in view["entities"] if e["id"] == "malcom_mclean")
    assert entity["type"] == "place"  # recorded value preserved
    assert entity["effective_type"] == "person"
    assert entity["corrections"][0]["id"] == correction["id"]

    # The exact function jobs.build_production_scenes calls:
    fresh_bible = _sample_bible()
    applied = rs.apply_entity_corrections(project_id, fresh_bible)
    assert applied and applied[0]["applied"] is True
    corrected = next(
        e for e in fresh_bible["entities"] if e["id"] == "malcom_mclean"
    )
    assert corrected["type"] == "person"
    assert corrected["user_corrections"][0]["original"] == "place"
    stored = rs.load_store(project_id)["entity_corrections"][0]
    assert stored["applications"] == 1 and stored["applied_at"]

    rs.delete_correction(project_id, correction["id"])
    assert rs.load_store(project_id)["entity_corrections"] == []


def test_correction_validation(project_id):
    _write_bible(project_id, _sample_bible())
    with pytest.raises(rs.ResearchError):
        rs.add_correction(project_id, "malcom_mclean", "type", "galaxy")
    with pytest.raises(rs.ResearchError):
        rs.add_correction(project_id, "malcom_mclean", "type", "place")
    with pytest.raises(rs.ResearchError):
        rs.add_correction(project_id, "nobody", "type", "person")
    with pytest.raises(rs.ResearchError):
        rs.add_correction(project_id, "malcom_mclean", "role", "x")


def test_jobs_hook_applies_corrections(monkeypatch, project_id):
    """End-to-end through jobs.build_production_scenes with the
    analyser stubbed: the written Case Bible carries the correction."""
    import jobs

    _write_bible(project_id, _sample_bible())
    rs.add_correction(project_id, "malcom_mclean", "type", "person")

    def _fake_analyze(script, **kwargs):
        return {
            "sentences": [
                {"sentence_number": 1,
                 "text": "On April 26, 1956, the Ideal X carried 58 containers."}
            ],
            "bible": _sample_bible(),
            "scenes": [],
        }

    monkeypatch.setattr(jobs, "analyze_script", _fake_analyze)
    production = jobs.build_production_scenes(
        "On April 26, 1956, the Ideal X carried 58 containers.",
        project_id=project_id,
        project_dir=media_paths.project_dir(project_id),
    )
    entity = next(
        e for e in production["bible"]["entities"] if e["id"] == "malcom_mclean"
    )
    assert entity["type"] == "person"
    on_disk = json.loads(
        (media_paths.project_dir(project_id) / "case_bible.json").read_text()
    )
    assert next(
        e for e in on_disk["entities"] if e["id"] == "malcom_mclean"
    )["type"] == "person"


# ---------------------------------------------------------------------
# Citations + bibliography
# ---------------------------------------------------------------------


def test_citations_and_bibliography(project_id, monkeypatch):
    _write_bible(project_id, _sample_bible())
    _stub_search(monkeypatch, {"T": [{"title": "Port history",
                                      "url": "https://example.com/port",
                                      "snippet": "", "source": "google_serper"}]})
    _stub_kb(monkeypatch, _KB_NOTHING)
    session = rs.run_session(project_id, "T")
    source_id = session["sources"][0]["id"]

    citation = rs.add_citation(
        project_id, {"kind": "fact", "fact_id": "sf_0001"}, source_id=source_id
    )
    assert "April 26, 1956" in citation["claim"]["label"]
    assert citation["source"]["url"] == "https://example.com/port"

    external = rs.add_citation(
        project_id,
        {"kind": "statement", "label": "The ship was a converted tanker."},
        external={"title": "Archive record", "url": "https://archive.org/details/x"},
    )
    assert external["source"]["provider"] == "user_supplied"
    assert external["source"]["tier"] == "primary_source"

    bib = rs.bibliography(project_id)
    assert len(bib["entries"]) == 2
    assert "https://example.com/port" in bib["text"]
    assert "https://archive.org/details/x" in bib["text"]

    with pytest.raises(rs.ResearchError):
        rs.add_citation(project_id, {"kind": "fact", "fact_id": "sf_9999"},
                        source_id=source_id)
    rs.delete_citation(project_id, citation["id"])
    assert len(rs.list_citations(project_id)) == 1


# ---------------------------------------------------------------------
# Timeline events (user-owned layer over the bible timeline)
# ---------------------------------------------------------------------


def test_user_timeline_events_merge_and_validate(project_id):
    _write_bible(project_id, _sample_bible())
    event = rs.add_timeline_event(project_id, "May 1956", "Ports begin refits.")
    view = rs.bible_view(project_id)
    origins = [(e["origin"], e["when"]) for e in view["timeline"]]
    assert ("case_bible", "April 26, 1956") in origins
    assert ("studio", "May 1956") in origins

    updated = rs.update_timeline_event(
        project_id, event["id"], what="Ports begin refits nationwide."
    )
    assert updated["what"].endswith("nationwide.")
    with pytest.raises(rs.ResearchError):
        rs.add_timeline_event(project_id, "June 1956", "X", source_ids=["src_nope"])
    rs.delete_timeline_event(project_id, event["id"])
    view = rs.bible_view(project_id)
    assert all(e["origin"] == "case_bible" for e in view["timeline"])


# ---------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------


def _kept_source(project_id, monkeypatch):
    _stub_search(monkeypatch, {"T": [{"title": "Port history",
                                      "url": "https://example.com/port",
                                      "snippet": "Container history.",
                                      "source": "google_serper"}]})
    _stub_kb(monkeypatch, _KB_NOTHING)
    session = rs.run_session(project_id, "T")
    source_id = session["sources"][0]["id"]
    rs.set_source_state(project_id, source_id, "kept")
    return session, source_id


def test_summary_requires_kept_sources_and_cites_them(project_id, monkeypatch):
    with pytest.raises(rs.ResearchError) as caught:
        rs.generate_summary(project_id)
    assert caught.value.code == "no_kept_sources"

    _session, source_id = _kept_source(project_id, monkeypatch)
    monkeypatch.setattr(
        llm_client, "llm_text",
        lambda system, user, **kw: "A brief summary [1].",
    )
    summary = rs.generate_summary(project_id)
    assert summary["source_ids"] == [source_id]
    assert summary["sources"][0]["url"] == "https://example.com/port"
    assert rs.list_summaries(project_id)[0]["text"] == "A brief summary [1]."


def test_summary_llm_failure_is_502_not_fabrication(project_id, monkeypatch):
    _kept_source(project_id, monkeypatch)

    def _boom(system, user, **kw):
        raise RuntimeError("no key")

    monkeypatch.setattr(llm_client, "llm_text", _boom)
    with pytest.raises(rs.ResearchError) as caught:
        rs.generate_summary(project_id)
    assert caught.value.status_code == 502
    assert rs.list_summaries(project_id) == []


# ---------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------


def test_gaps_derive_from_recorded_data(project_id):
    bible = _sample_bible()
    _write_bible(project_id, bible)
    gaps = rs.compute_gaps(project_id, bible, rs.load_store(project_id))
    assert gaps["unverified_facts"]["count"] == 2
    assert gaps["unchecked_facts"]["fact_ids"] == ["sf_0001"]
    assert gaps["uncorroborated_timeline_events"]["count"] == 1
    assert set(gaps["entities_without_kept_sources"]["entity_ids"]) == {
        "ideal_x", "malcom_mclean"
    }


# ---------------------------------------------------------------------
# API: ownership + happy path
# ---------------------------------------------------------------------


def test_api_cross_tenant_404_and_owner_flow(client, users, project_id, monkeypatch):
    a, b = users
    _write_bible(project_id, _sample_bible())
    base = f"/api/v1/projects/{project_id}/research"

    for method, path, kwargs in [
        ("get", base, {}),
        ("get", f"{base}/bible", {}),
        ("get", f"{base}/sources", {}),
        ("post", f"{base}/sessions", {"json": {"topic": "T"}}),
        ("post", f"{base}/fact-check", {}),
        ("post", f"{base}/corrections",
         {"json": {"entity_id": "malcom_mclean", "field": "type", "value": "person"}}),
    ]:
        response = getattr(client, method)(path, headers=_bearer(b), **kwargs)
        assert response.status_code == 404, (method, path, response.text)

    overview = client.get(base, headers=_bearer(a))
    assert overview.status_code == 200
    assert overview.json()["bible_recorded"] is True
    assert overview.json()["counts"]["entities"] == 2

    _stub_search(monkeypatch, {"Cranes": [{"title": "Port history",
                                           "url": "https://example.com/port",
                                           "snippet": "", "source": "google_serper"}]})
    _stub_kb(monkeypatch, _KB_NOTHING)
    created = client.post(f"{base}/sessions", headers=_bearer(a),
                          json={"topic": "Cranes"})
    assert created.status_code == 201, created.text
    source_id = created.json()["sources"][0]["id"]

    state = client.post(f"{base}/sources/{source_id}/state",
                        headers=_bearer(a), json={"state": "kept"})
    assert state.status_code == 200 and state.json()["state"] == "kept"

    sources = client.get(f"{base}/sources?state=kept", headers=_bearer(a))
    assert sources.json()["total"] == 1

    correction = client.post(
        f"{base}/corrections", headers=_bearer(a),
        json={"entity_id": "malcom_mclean", "field": "type", "value": "person"},
    )
    assert correction.status_code == 201, correction.text

    bible = client.get(f"{base}/bible", headers=_bearer(a))
    entity = next(
        e for e in bible.json()["entities"] if e["id"] == "malcom_mclean"
    )
    assert entity["effective_type"] == "person"

    citation = client.post(
        f"{base}/citations", headers=_bearer(a),
        json={"claim": {"kind": "fact", "fact_id": "sf_0001"},
              "source_id": source_id},
    )
    assert citation.status_code == 201, citation.text
    bib = client.get(f"{base}/bibliography", headers=_bearer(a))
    assert "https://example.com/port" in bib.json()["text"]

    _stub_kb(monkeypatch, _KB_SHIP)
    fact_check = client.post(f"{base}/fact-check", headers=_bearer(a))
    assert fact_check.status_code == 200, fact_check.text
    # The stubbed KB verdict for "Ideal X" describes 1956: sf_0001 upgrades.
    assert fact_check.json()["bases"]["research_supported"] == 1
