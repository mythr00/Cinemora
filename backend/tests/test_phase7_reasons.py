"""Phase 7 — P0 honesty: degraded_reason distinguishes the three
failure paths (DM-2(a)).

Before Phase 7 every degraded plan looked the same downstream: the
bible fell back and nobody could tell whether the cause was a
missing API key, a provider failure, or an unusable LLM response.
The planner block now records exactly which path degraded the run.
"""

import json

from services import story_understanding as story

_SENTENCES = [
    "Ada Lovelace designed an engine.",
    "She published notes on it.",
    "The engine was never built.",
]

_BIBLE_JSON = {
    "title": "Ada",
    "story_type": "history",
    "time_period": "1840s",
    "primary_subject_id": "ada",
    "entities": [
        {
            "id": "ada",
            "canonical": "Ada Lovelace",
            "type": "person",
            "role": "main subject",
            "aliases": [],
            "pronoun": "she",
            "description": "Mathematician.",
        }
    ],
    "timeline": [],
}


def _working_llm(system, user):
    if system.startswith("You are a documentary researcher"):
        return json.dumps(_BIBLE_JSON)
    return json.dumps([
        {
            "idx": i,
            "subject_id": "ada",
            "scenarios": [{
                "visual": "Ada Lovelace with the analytical engine",
                "kind": "person_activity",
                "subject_id": "ada",
                "specificity": "representative",
                "search_terms": ["analytical engine"],
            }],
        }
        for i in range(len(_SENTENCES))
    ])


def _plan(llm):
    return story.plan_script(
        " ".join(_SENTENCES), _SENTENCES, llm, cache_path=None
    )


def test_reason_no_key_when_no_llm_configured():
    bible, _plans, mode = _plan(None)
    assert mode == "degraded"
    block = bible["planner"]
    assert block["degraded_reason"] == "no_key"
    assert block["planner_version"] == "degraded-2.0"
    assert block["degraded_sentences"] == len(_SENTENCES)


def test_reason_no_key_when_client_reports_missing_key():
    def llm(system, user):
        # llm_client's BLOCK-001 point-of-use failure, verbatim shape.
        raise RuntimeError("BREWKEG_API_KEY is not configured")

    bible, _plans, mode = _plan(llm)
    assert mode == "degraded"
    assert bible["planner"]["degraded_reason"] == "no_key"


def test_reason_provider_failure_on_transport_error():
    def llm(system, user):
        raise ConnectionError("provider unreachable")

    bible, _plans, mode = _plan(llm)
    assert mode == "degraded"
    assert bible["planner"]["degraded_reason"] == "provider_failure"


def test_reason_invalid_plan_on_unparseable_response():
    def llm(system, user):
        return "I cannot help with that."  # not JSON at all

    bible, _plans, mode = _plan(llm)
    assert mode == "degraded"
    assert bible["planner"]["degraded_reason"] == "invalid_plan"


def test_reason_invalid_plan_on_shapeless_response():
    def llm(system, user):
        # Parses as JSON but carries no bible entities and no plans.
        return json.dumps({"unexpected": True})

    bible, _plans, mode = _plan(llm)
    assert mode == "degraded"
    assert bible["planner"]["degraded_reason"] == "invalid_plan"


def test_llm_mode_records_no_degraded_reason():
    bible, _plans, mode = _plan(_working_llm)
    assert mode == "llm"
    block = bible["planner"]
    assert block["planner_version"] == "llm"
    assert block["degraded_reason"] is None
    assert block["degraded_sentences"] == 0
