"""Format intelligence tests (F-1..F-3).

F-1: every scenario carries a format intent (footage / photograph /
document / map / chart / graphic / any) with a confidence. The intent is
inferred conservatively in degraded mode, validated when the LLM
provides it, and defaults to "any" — pure relevance — when no
communicative signal exists.
"""

from services.story_understanding import infer_format_intent, normalize_plan


def test_infer_chart_from_statistics():
    intent, conf = infer_format_intent("The project cost $5 million and employed 2,000 workers.", {})
    assert intent == "chart"
    assert conf >= 0.8


def test_infer_document_from_court():
    intent, _ = infer_format_intent("The court sentenced him to twenty years.", {})
    assert intent == "document"


def test_infer_map_from_geography():
    intent, _ = infer_format_intent("The river formed the border between the two regions.", {})
    assert intent == "map"


def test_infer_footage_from_crowd_action():
    intent, _ = infer_format_intent("Crowds gathered and celebrated in the square.", {})
    assert intent == "footage"


def test_infer_any_when_no_signal():
    intent, conf = infer_format_intent("The cat sat quietly on the mat.", {})
    assert intent == "any"
    assert conf == 0.0


def test_normalize_plan_threads_intent_into_scenarios():
    bible = {"by_id": {}, "alias_index": {}}
    raw = {
        "meaning": "cost",
        "role": "fact",
        "scenarios": [
            {
                "visual": "construction site workers",
                "kind": "event",
                "specificity": "representative",
                "search_terms": ["construction workers"],
            }
        ],
    }
    plan = normalize_plan(raw, "The bridge cost $5 million to build.", bible, None)
    scenario = plan["scenarios"][0]
    assert scenario["format_intent"] == "chart"
    assert scenario["format_intent_confidence"] >= 0.8


def test_normalize_plan_validates_llm_intent():
    bible = {"by_id": {}, "alias_index": {}}
    raw = {
        "meaning": "geography",
        "role": "fact",
        "scenarios": [
            {
                "visual": "river valley aerial",
                "kind": "place",
                "specificity": "representative",
                "search_terms": ["river valley"],
                "format_intent": "map",
            }
        ],
    }
    plan = normalize_plan(raw, "The valley lies east of the mountains.", bible, None)
    assert plan["scenarios"][0]["format_intent"] == "map"

    raw["scenarios"][0]["format_intent"] = "hologram"  # not a real intent
    plan = normalize_plan(raw, "The valley lies east of the mountains.", bible, None)
    assert plan["scenarios"][0]["format_intent"] in {"map", "any"}
