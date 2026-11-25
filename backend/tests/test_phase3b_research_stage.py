"""Phase 3b / C-07 — research outcomes are recorded, bounded, visible.

Before this change, enrich_bible_with_research (services.story_
understanding) ran inside script analysis, counted failures in a
local variable, gave up after _RESEARCH_FAILURE_LIMIT=3, and left
only subjects/sources counts in research_meta — a run produced on
an unenriched bible looked identical to a fully researched one.

Now the pass records its own outcome in research_meta (attempts,
failures, limit, per-query errors, outcome, degraded marker), and
jobs records that outcome as the research stage record on the run
state + asset report. These tests pin both halves. No real search:
the provider seam (story.search_web) is stubbed throughout.
"""

import copy

import pytest

import jobs
from services import story_understanding as story
from services.asset_report import build_asset_report


_SCRIPT = "The Berlin Wall divided Berlin. The Berlin Wall fell in 1989."


def _bible():
    return {
        "title": "Walls",
        "primary_subject_id": "wall",
        "entities": [
            {"id": "wall", "canonical": "Berlin Wall", "type": "object",
             "role": "main subject", "aliases": ["the Wall"],
             "description": "Barrier erected in 1961."},
            {"id": "berlin", "canonical": "Berlin", "type": "place",
             "role": "", "aliases": [],
             "description": "City divided in 1961."},
        ],
        "timeline": [
            {"when": "1961", "what": "The Berlin Wall was erected."},
        ],
    }


@pytest.fixture(autouse=True)
def _no_real_search(monkeypatch):
    # Default: a working provider returning one source per query.
    monkeypatch.setattr(
        story, "search_web",
        lambda query: [{
            "url": "https://example.org/wall",
            "title": "Wall",
            "snippet": "The Berlin Wall was erected in 1961.",
        }],
        raising=False,
    )


def test_successful_pass_records_completed_outcome():
    bible = story.enrich_bible_with_research(
        _bible(), _SCRIPT, sentences=[_SCRIPT]
    )
    meta = bible["research_meta"]
    assert meta["outcome"] == "completed"
    assert meta["degraded"] is False
    assert meta["queries_attempted"] > 0
    assert meta["queries_failed"] == 0
    assert meta["failure_limit"] == story._RESEARCH_FAILURE_LIMIT
    assert meta["failure_limit_reached"] is False
    assert meta["errors"] == []
    assert meta["sources_attached"] > 0


def test_failure_limit_give_up_is_recorded_as_degraded(monkeypatch):
    def _boom(query):
        raise ConnectionError("provider down")

    monkeypatch.setattr(story, "search_web", _boom, raising=False)
    bible = story.enrich_bible_with_research(
        _bible(), _SCRIPT, sentences=[_SCRIPT]
    )
    meta = bible["research_meta"]
    # Bounded: exactly the failure limit of attempts, then it stops.
    assert meta["queries_attempted"] == story._RESEARCH_FAILURE_LIMIT
    assert meta["queries_failed"] == story._RESEARCH_FAILURE_LIMIT
    assert meta["failure_limit_reached"] is True
    assert meta["outcome"] == "degraded"
    assert meta["degraded"] is True
    # What failed is recorded, per query.
    assert len(meta["errors"]) == story._RESEARCH_FAILURE_LIMIT
    assert all("ConnectionError" in e["error"] for e in meta["errors"])
    assert all(e["query"] for e in meta["errors"])
    # Fail-soft preserved: facts stay script_asserted, bible usable.
    assert bible["entities"][0]["support"] == "script_asserted"


def test_no_provider_records_unavailable_not_success(monkeypatch):
    monkeypatch.setattr(story, "_resolve_search_web", lambda: None)
    bible = story.enrich_bible_with_research(
        _bible(), _SCRIPT, sentences=[_SCRIPT]
    )
    meta = bible["research_meta"]
    assert meta["outcome"] == "unavailable"
    assert meta["degraded"] is True
    assert meta["queries_attempted"] == 0


def test_stage_record_surfaces_degraded_on_run_state(monkeypatch):
    def _boom(query):
        raise TimeoutError("slow provider")

    monkeypatch.setattr(story, "search_web", _boom, raising=False)
    bible = story.enrich_bible_with_research(
        _bible(), _SCRIPT, sentences=[_SCRIPT]
    )
    state = {"stages": {"script_analysis": {"status": "running"}}}
    record = jobs._record_research_outcome(
        state, {"analysis": {"bible": bible}}
    )
    assert record["stage"] == "research"
    assert record["degraded"] is True
    assert record["outcome"] == "degraded"
    assert record["failure_limit_reached"] is True
    # Visible in BOTH state-level and the stage record.
    assert state["research"] is record
    assert state["stages"]["script_analysis"]["research"] is record


def test_stage_record_from_production_data_shape():
    bible = story.enrich_bible_with_research(
        _bible(), _SCRIPT, sentences=[_SCRIPT]
    )
    record = jobs._research_stage_record({"bible": bible})
    assert record["recorded"] is True
    assert record["outcome"] == "completed"
    assert record["degraded"] is False


def test_stage_record_without_meta_is_not_recorded_not_faked():
    record = jobs._research_stage_record({"bible": {"entities": []}})
    assert record["recorded"] is False
    assert record["outcome"] == "not_recorded"
    assert record["degraded"] is False


def test_asset_report_carries_the_research_record():
    record = {
        "stage": "research", "recorded": True, "outcome": "degraded",
        "degraded": True, "queries_attempted": 3, "queries_failed": 3,
        "failure_limit": 3, "failure_limit_reached": True,
        "errors": [{"query": "Berlin Wall", "error": "TimeoutError: x"}],
    }
    report = build_asset_report([], research=record)
    assert report["research"] == record
    # And without a caller record it stays None (never fabricated).
    assert build_asset_report([])["research"] is None
