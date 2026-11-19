"""Stage 3A — research-grounded Case Bible (MA-32, review §2.11).

plan_script now runs a fail-soft research pass after the bible is
built: the most-mentioned subjects are searched through
services.search_provider.search_web (mocked here — never the real
Serper), retrieved sources are attached to bible entities with
timestamps, bible facts (timeline entries + entity descriptions)
are labelled "externally_supported" / "script_asserted", source
disagreements are recorded as conflicts without being resolved,
and every sentence plan gains a "research_ref".
"""

import json
from datetime import datetime

import pytest

from services import search_provider
from services import story_understanding as story


_BIBLE_JSON = {
    "title": "The Fall of the Berlin Wall",
    "story_type": "history",
    "time_period": "1961-1989",
    "primary_subject_id": "berlin_wall",
    "entities": [
        {
            "id": "berlin_wall",
            "canonical": "Berlin Wall",
            "type": "object",
            "role": "main subject",
            "aliases": ["the Wall"],
            "pronoun": "it",
            "description": "Concrete barrier that divided Berlin, erected in 1961.",
        },
        {
            "id": "berlin",
            "canonical": "Berlin",
            "type": "place",
            "role": "location",
            "aliases": [],
            "pronoun": "it",
            "description": "Capital city divided by the wall.",
        },
        {
            "id": "hans_mueller",
            "canonical": "Hans Mueller",
            "type": "person",
            "role": "guard commander",
            "aliases": [],
            "pronoun": "he",
            "description": "Border guard commander at the checkpoint.",
        },
    ],
    "timeline": [
        {"when": "1961", "what": "The Berlin Wall was erected around the city."},
        {"when": "1989", "what": "The Berlin Wall fell and crowds crossed freely."},
    ],
}

_SENTENCES = [
    "The Berlin Wall divided the city of Berlin for decades.",
    "Hans Mueller commanded the guards at the checkpoint.",
    "In 1989 the Berlin Wall fell and crowds crossed freely.",
]
_SCRIPT = " ".join(_SENTENCES)
_PLAN_SUBJECTS = ["berlin", "hans_mueller", "berlin_wall"]


def _fake_llm(bible_json=None):
    bible_json = bible_json or _BIBLE_JSON

    def llm(system, user):
        if system.startswith("You are a documentary researcher"):
            return json.dumps(bible_json)
        return json.dumps(
            [
                {
                    "idx": i,
                    "subject_id": _PLAN_SUBJECTS[i],
                    "scenarios": [
                        {
                            "visual": "archival material at the Berlin Wall",
                            "kind": "archival",
                            "subject_id": _PLAN_SUBJECTS[i],
                            "specificity": "representative",
                            "search_terms": ["berlin wall"],
                        }
                    ],
                }
                for i in range(len(_SENTENCES))
            ]
        )

    return llm


def _fake_search(query):
    q = str(query).lower()
    if "hans" in q:
        return []
    if "berlin wall" in q:
        return [
            {
                "title": "Berlin Wall erected in 1961",
                "url": "https://example.org/wall-history",
                "snippet": "The Berlin Wall was erected in 1961 around the city of Berlin, dividing families overnight.",
                "source": "google_serper",
            },
            {
                "title": "The night the wall fell",
                "url": "https://example.org/wall-fall",
                "snippet": "On 9 November 1989 the Berlin Wall fell and crowds crossed freely into the west.",
                "source": "google_serper",
            },
        ]
    if "berlin" in q:
        return [
            {
                "title": "Berlin profile",
                "url": "https://example.org/berlin",
                "snippet": "Berlin, the capital city, was divided by the wall for decades.",
                "source": "google_serper",
            }
        ]
    return []


def _run(monkeypatch, search=_fake_search, llm=None, bible_json=None):
    monkeypatch.setattr(story, "search_web", search)
    return story.plan_script(
        _SCRIPT, _SENTENCES, _fake_llm(bible_json) if llm is None else llm
    )


# ---------------------------------------------------------------------------
# sources + research_meta
# ---------------------------------------------------------------------------


def test_bible_gains_timestamped_sources_and_meta(monkeypatch):
    bible, plans, mode = _run(monkeypatch)
    by_id = bible["by_id"]

    wall_sources = by_id["berlin_wall"]["sources"]
    assert [s["url"] for s in wall_sources] == [
        "https://example.org/wall-history",
        "https://example.org/wall-fall",
    ]
    meta = bible["research_meta"]
    for source in wall_sources:
        assert source["title"] and source["snippet"]
        assert source["retrieved_at"] == meta["researched_at"]
        datetime.fromisoformat(source["retrieved_at"])  # parses as ISO

    assert meta["subjects_researched"] == 3
    assert meta["sources_attached"] == 3  # 2 wall + 1 berlin + 0 hans
    assert meta["conflicts_found"] == len(bible["conflicts"])
    assert by_id["hans_mueller"]["sources"] == []


def test_search_is_routed_through_search_provider(monkeypatch):
    # Patching the provider module itself (not story_understanding)
    # must also work: story_understanding resolves search_web lazily
    # from services.search_provider and never calls Serper directly.
    monkeypatch.setattr(search_provider, "search_web", _fake_search)
    bible, _, _ = story.plan_script(_SCRIPT, _SENTENCES, _fake_llm())
    assert bible["research_meta"]["sources_attached"] == 3


# ---------------------------------------------------------------------------
# fact support labels
# ---------------------------------------------------------------------------


def test_fact_support_labels(monkeypatch):
    bible, _, _ = _run(monkeypatch)
    timeline = {entry["when"]: entry for entry in bible["timeline"]}
    # The snippets contain the facts' years -> externally supported.
    assert timeline["1961"]["support"] == "externally_supported"
    assert timeline["1989"]["support"] == "externally_supported"
    by_id = bible["by_id"]
    assert by_id["berlin_wall"]["support"] == "externally_supported"
    assert by_id["berlin"]["support"] == "externally_supported"
    # Hans Mueller was researched but has no sources at all.
    assert by_id["hans_mueller"]["support"] == "script_asserted"


def test_fact_without_matching_snippet_evidence_stays_script_asserted(monkeypatch):
    def search(query):
        if "berlin wall" in str(query).lower():
            return [
                {
                    "title": "Wall tourism",
                    "url": "https://example.org/tours",
                    "snippet": "Guided bicycle tours along the former barrier route run daily in summer.",
                    "source": "google_serper",
                }
            ]
        return []

    bible, _, _ = _run(monkeypatch, search=search)
    timeline = {entry["when"]: entry for entry in bible["timeline"]}
    # Neither the 1961 nor the 1989 year appears in the snippet, and
    # no majority of either fact's key tokens is present either.
    assert timeline["1961"]["support"] == "script_asserted"
    assert timeline["1989"]["support"] == "script_asserted"


# ---------------------------------------------------------------------------
# conflicts
# ---------------------------------------------------------------------------


def test_conflicting_years_produce_one_conflict_with_both_values(monkeypatch):
    bible_json = dict(_BIBLE_JSON)
    bible_json["entities"] = [
        dict(_BIBLE_JSON["entities"][0], description="Concrete barrier dividing the city."),
        _BIBLE_JSON["entities"][1],
        _BIBLE_JSON["entities"][2],
    ]
    bible_json["timeline"] = [
        {"when": "1989", "what": "The Berlin Wall fell and crowds crossed freely."}
    ]

    def search(query):
        if "berlin wall" in str(query).lower():
            return [
                {
                    "title": "Wall fell in 1989",
                    "url": "https://example.org/a",
                    "snippet": "Historians record that the Berlin Wall fell in 1989 as crowds gathered at the checkpoint.",
                    "source": "google_serper",
                },
                {
                    "title": "Wall fell in 1990?",
                    "url": "https://example.org/b",
                    "snippet": "Some accounts claim the Berlin Wall fell in 1990 as crowds gathered at the checkpoint.",
                    "source": "google_serper",
                },
            ]
        return []

    bible, _, _ = _run(monkeypatch, search=search, bible_json=bible_json)
    conflicts = bible["conflicts"]
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["subject"] == "berlin_wall"
    values = {v["value"]: v["source_url"] for v in conflict["values"]}
    assert values == {
        "1989": "https://example.org/a",
        "1990": "https://example.org/b",
    }
    assert bible["research_meta"]["conflicts_found"] == 1
    # The conflict is recorded, never resolved: the fact keeps the
    # support its evidence earns (1989 IS in a snippet).
    assert bible["timeline"][0]["support"] == "externally_supported"


# ---------------------------------------------------------------------------
# research_ref coverage
# ---------------------------------------------------------------------------


def test_every_plan_carries_research_ref(monkeypatch):
    _, plans, _ = _run(monkeypatch)
    assert len(plans) == len(_SENTENCES)
    assert all("research_ref" in plan for plan in plans)
    assert plans[0]["research_ref"] == "berlin"  # subject has sources
    assert plans[1]["research_ref"] == "no_external_source"  # hans: none
    assert plans[2]["research_ref"] == "berlin_wall"


def test_heuristic_path_also_gets_research_coverage(monkeypatch):
    monkeypatch.setattr(story, "search_web", lambda q: [])
    script = "Ada Lovelace designed the Analytical Engine. Ada Lovelace published her notes."
    sentences = [
        "Ada Lovelace designed the Analytical Engine.",
        "Ada Lovelace published her notes.",
    ]
    bible, plans, mode = story.plan_script(script, sentences, None)
    assert mode == "degraded"
    assert "research_meta" in bible
    assert all(plan.get("research_ref") == "no_external_source" for plan in plans)


# ---------------------------------------------------------------------------
# fail-soft + cache round-trip
# ---------------------------------------------------------------------------


def test_search_failure_never_crashes_plan_script(monkeypatch):
    def boom(query):
        raise RuntimeError("serper down")

    bible, plans, mode = _run(monkeypatch, search=boom)
    assert mode == "llm"  # planning itself unaffected
    assert bible["research_meta"]["sources_attached"] == 0
    assert all(
        entry["support"] == "script_asserted" for entry in bible["timeline"]
    )
    assert all(plan["research_ref"] == "no_external_source" for plan in plans)


def test_research_fields_survive_serialize_hydrate_round_trip(monkeypatch):
    bible, _, _ = _run(monkeypatch)
    serialized = story.serialize_bible(bible)
    assert "by_id" not in serialized and "alias_index" not in serialized
    revived = story.hydrate_bible(json.loads(json.dumps(serialized)))
    wall = revived["by_id"]["berlin_wall"]
    assert [s["url"] for s in wall["sources"]] == [
        "https://example.org/wall-history",
        "https://example.org/wall-fall",
    ]
    assert wall["support"] == "externally_supported"
    assert revived["research_meta"]["sources_attached"] == 3
    assert revived["conflicts"] == bible["conflicts"]
    timeline = {entry["when"]: entry for entry in revived["timeline"]}
    assert timeline["1989"]["support"] == "externally_supported"
