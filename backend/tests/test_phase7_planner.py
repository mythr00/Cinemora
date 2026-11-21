"""Phase 7 — Degraded Planner 2.0 core behaviour (P1/P2/P3/P5).

Regression fixtures are the two stored degraded-mode scripts from
the Degraded Mode Inspection (film 1: a missing-person script whose
lead the old heuristic typed as a PLACE and whose primary subject
it set to a university; the Apollo 13 script). The planner must
never again produce those failures, must never admit a closed-class
phantom entity (`what`, `their`, ...), and must be deterministic.
"""

import json
from pathlib import Path

import pytest

from services import degraded_planner
from services import story_understanding as story

FIXTURES = Path(__file__).parent / "fixtures" / "golden_plans"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(scope="module")
def scripts():
    return _load("degraded_scripts.json")


@pytest.fixture(scope="module")
def film1(scripts):
    sentences = scripts["film1_spierer"]["sentences"]
    bible, plans, mode = story.plan_script(
        " ".join(sentences), sentences, None, cache_path=None
    )
    return bible, plans, mode


@pytest.fixture(scope="module")
def apollo(scripts):
    sentences = scripts["apollo13"]["sentences"]
    bible, plans, mode = story.plan_script(
        " ".join(sentences), sentences, None, cache_path=None
    )
    return bible, plans, mode


def _by_canonical(bible):
    return {e["canonical"]: e for e in bible["entities"]}


# ------------------------------------------------------------------
# The closed-class screen: phantom entities are impossible
# ------------------------------------------------------------------

CLOSED_CLASS = {
    "what", "who", "whom", "whose", "which", "when", "where", "why",
    "how", "their", "the", "within", "monday",
}


@pytest.mark.parametrize("fixture", ["film1", "apollo"])
def test_no_closed_class_phantom_entities(fixture, request):
    bible, _plans, _mode = request.getfixturevalue(fixture)
    canonicals = {e["canonical"].lower() for e in bible["entities"]}
    assert not (canonicals & CLOSED_CLASS), canonicals & CLOSED_CLASS
    # The two inspection phantoms, by name:
    assert "what" not in canonicals
    assert "their" not in canonicals


def test_closed_class_screen_unit():
    # Post-colon capitals and sentence-initial function words have
    # no admission path, in any position.
    stats = degraded_planner._candidate_stats([
        "One question remains: What happened next?",
        "Within days, Their story spread everywhere.",
        "The Monday meeting changed nothing.",
    ])
    admitted = {text.lower() for text in stats}
    assert "what" not in admitted
    assert "their" not in admitted
    assert "monday" not in admitted
    assert "within" not in admitted


# ------------------------------------------------------------------
# The inspection's headline failures, as regressions
# ------------------------------------------------------------------

def test_film1_lead_is_a_person_and_the_primary_subject(film1):
    bible, _plans, mode = film1
    assert mode == "degraded"
    entities = _by_canonical(bible)
    lauren = entities["Lauren Spierer"]
    # The old heuristic typed her as a place. She is a person.
    assert lauren["type"] == "person"
    assert lauren["type_source"] == "frames"
    # ...and she, not the university, is the story's primary subject.
    assert bible["primary_subject_id"] == lauren["id"]


def test_film1_university_is_an_organization_not_the_primary(film1):
    bible, _plans, _mode = film1
    entities = _by_canonical(bible)
    university = entities["Indiana University"]
    assert university["type"] == "organization"
    assert university["type_confidence"] >= degraded_planner.TYPE_CONFIDENCE_FLOOR
    assert bible["primary_subject_id"] != university["id"]


def test_apollo_primary_is_nasa_typed_organization(apollo):
    bible, _plans, _mode = apollo
    entities = _by_canonical(bible)
    nasa = entities["NASA"]
    assert nasa["type"] == "organization"
    assert nasa["type_confidence"] >= degraded_planner.TYPE_CONFIDENCE_FLOOR
    assert bible["primary_subject_id"] == nasa["id"]


def test_every_entity_carries_typing_provenance(film1, apollo):
    for bible, _plans, _mode in (film1, apollo):
        for entity in bible["entities"]:
            assert isinstance(entity["type_confidence"], (int, float))
            assert entity["type_source"]
            assert entity["type_signals"]
            assert entity["type_low_confidence"] == (
                entity["type_confidence"] < degraded_planner.TYPE_CONFIDENCE_FLOOR
            )


# ------------------------------------------------------------------
# Scenario factory: real search terms for EVERY scenario (P3)
# ------------------------------------------------------------------

@pytest.mark.parametrize("fixture", ["film1", "apollo"])
def test_factory_emits_search_terms_for_every_scenario(fixture, request):
    _bible, plans, _mode = request.getfixturevalue(fixture)
    assert plans
    for plan in plans:
        assert plan["scenarios"], plan
        for scenario in plan["scenarios"]:
            assert scenario["search_terms"], scenario
            assert scenario["queries"], scenario


def test_factory_plans_are_marked_degraded(film1):
    _bible, plans, _mode = film1
    assert all(plan["degraded"] for plan in plans)
    assert all(
        plan["concepts_source"] == "planner2_factory" for plan in plans
    )


# ------------------------------------------------------------------
# Role classifier: >= 7 distinct roles across a golden script (P2)
# ------------------------------------------------------------------

def test_role_classifier_yields_seven_distinct_roles_on_berlin():
    cache = _load("berlin_five_minute.json")
    sentences = [p["sentence"] for p in cache["plans"]]
    _bible, plans, _mode = story.plan_script(
        " ".join(sentences), sentences, None, cache_path=None
    )
    roles = {plan["role"] for plan in plans}
    assert len(roles) >= 7, roles


# ------------------------------------------------------------------
# Determinism pin (P5) + the escape hatch
# ------------------------------------------------------------------

def _scrub(value):
    """Remove research-pass timestamps/sources: determinism is a
    property of the PLANNER, not of the fail-soft research pass."""
    if isinstance(value, dict):
        return {
            key: _scrub(item)
            for key, item in value.items()
            if key not in ("sources", "research_meta", "retrieved_at",
                           "researched_at", "conflicts")
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def test_planner_is_deterministic(scripts):
    sentences = scripts["film1_spierer"]["sentences"]
    first = story.plan_script(" ".join(sentences), sentences, None, cache_path=None)
    second = story.plan_script(" ".join(sentences), sentences, None, cache_path=None)
    assert _scrub(first[0]) == _scrub(second[0])
    assert _scrub(first[1]) == _scrub(second[1])
    assert first[2] == second[2] == "degraded"


def test_escape_hatch_selects_the_old_heuristic(monkeypatch, scripts):
    monkeypatch.setenv("CINEMORA_DEGRADED_PLANNER", "1")
    sentences = scripts["film1_spierer"]["sentences"]
    bible, plans, mode = story.plan_script(
        " ".join(sentences), sentences, None, cache_path=None
    )
    assert mode == "degraded"
    assert bible["planner_version"] == "heuristic-1.0"
    assert bible["planner"]["planner_version"] == "heuristic-1.0"
    # The heuristic's entities carry no Planner 2.0 typing stamps.
    assert all("type_confidence" not in e for e in bible["entities"])
    # Its synthesis scenarios are the old shape: no factory terms.
    assert all(
        plan["concepts_source"] != "planner2_factory" for plan in plans
    )


def test_hatch_default_is_planner2(monkeypatch):
    monkeypatch.delenv("CINEMORA_DEGRADED_PLANNER", raising=False)
    assert degraded_planner.planner_enabled() is True
    monkeypatch.setenv("CINEMORA_DEGRADED_PLANNER", "legacy")
    assert degraded_planner.planner_enabled() is False


# ------------------------------------------------------------------
# Anti-overfit lint: the planner's source names no golden subject
# ------------------------------------------------------------------

def test_planner_source_names_no_golden_subjects():
    names = (
        "spierer", "kohberger", "bloomington", "schabowski",
        "goncalves", "kernodle", "mogen", "chapin", "heuermann",
        "bernauer", "bornholmer", "mauerspechte", "nikolaikirche",
        "lauren",
    )
    for module in (degraded_planner,):
        source = Path(module.__file__).read_text().lower()
        for name in names:
            assert name not in source, (module.__name__, name)
    from services import kb_client

    source = Path(kb_client.__file__).read_text().lower()
    for name in names:
        assert name not in source, ("kb_client", name)
