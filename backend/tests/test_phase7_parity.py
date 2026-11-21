"""Phase 7 — P4 parity harness: Planner 2.0 vs the stored LLM
golden plans (Berlin five-minute, Berlin multi-source, Kohberger
true-crime — the three llm-mode story_understanding caches).

The harness MEASURES; the gates asserted here are exactly the ones
the offline (KB-disabled) cascade verifiably meets: closed-class
entities = 0 (hard), search-terms coverage = 100%, scenarios per
sentence, entities per sentence, plan place/time presence and
record locations presence at or above the plan's floors, and
primary-subject agreement on the two full-length scripts. The
remaining measures (subject agreement, type accuracy and its
trusted coverage, role distance, no-search recall, exact share)
are computed and exposed on the report for the Phase 7 register
proposal — several are expected to miss their plan targets while
founder decisions D-1/D-2 are unmade, and the harness records that
gap as evidence rather than tuning constants against one script.
"""

import json
from pathlib import Path

import pytest

from services import script_analyzer
from services import story_understanding as story

FIXTURES = Path(__file__).parent / "fixtures" / "golden_plans"
GOLDEN = ("berlin_five_minute", "berlin_multisource", "kohberger_truecrime")
NO_SEARCH_ROLES = {"transition", "reflection", "hook", "conclusion", "motivation"}
CLOSED_CLASS = {
    "what", "who", "whom", "whose", "which", "when", "where", "why",
    "how", "their", "the",
}


def _norm(text):
    return story._norm(text)


def _align(new_entities, llm_entities):
    """New entity id -> LLM entity id: exact normalised-form match
    first, then token-subset containment either way (a shorter
    surface of the same name)."""
    llm_forms = {}
    for entity in llm_entities:
        for form in [entity["canonical"], *(entity.get("aliases") or [])]:
            llm_forms.setdefault(_norm(form), entity["id"])
    mapping = {}
    for entity in new_entities:
        for form in [entity["canonical"], *(entity.get("aliases") or [])]:
            if _norm(form) in llm_forms:
                mapping[entity["id"]] = llm_forms[_norm(form)]
                break
    token_forms = [(set(_norm(form).split()), lid) for form, lid in llm_forms.items()]
    for entity in new_entities:
        if entity["id"] in mapping:
            continue
        best = None
        for form in [entity["canonical"], *(entity.get("aliases") or [])]:
            tokens = set(_norm(form).split())
            if not tokens:
                continue
            for ltokens, lid in token_forms:
                if tokens <= ltokens or ltokens <= tokens:
                    diff = abs(len(tokens) - len(ltokens))
                    if best is None or diff < best[0]:
                        best = (diff, lid)
        if best is not None:
            mapping[entity["id"]] = best[1]
    return mapping


def compute_parity(name):
    cache = json.loads((FIXTURES / f"{name}.json").read_text())
    llm_bible, llm_plans = cache["bible"], cache["plans"]
    sentences = [p["sentence"] for p in llm_plans]
    bible, plans, mode = story.plan_script(
        " ".join(sentences), sentences, None, cache_path=None
    )
    assert mode == "degraded"

    mapping = _align(bible["entities"], llm_bible["entities"])
    llm_by_id = {e["id"]: e for e in llm_bible["entities"]}
    new_by_id = {e["id"]: e for e in bible["entities"]}
    aligned = [(new_by_id[n], llm_by_id[l]) for n, l in mapping.items()]
    floored = [
        (n, l) for n, l in aligned
        if isinstance(n.get("type_confidence"), (int, float))
        and n["type_confidence"] >= 0.7
    ]

    report = {
        "sentences": len(sentences),
        "closed_class_entities": [
            e["canonical"] for e in bible["entities"]
            if _norm(e["canonical"]) in CLOSED_CLASS
        ],
        "entities_per_sentence": round(len(bible["entities"]) / len(sentences), 3),
        "aligned_entities": len(aligned),
        "type_accuracy_floored": (
            round(sum(1 for n, l in floored if n["type"] == l["type"]) / len(floored), 3)
            if floored else None
        ),
        "type_floored_pairs": len(floored),
        "type_accuracy_all_aligned": (
            round(sum(1 for n, l in aligned if n["type"] == l["type"]) / len(aligned), 3)
            if aligned else None
        ),
        "primary_agree": mapping.get(bible.get("primary_subject_id") or "")
        == llm_bible.get("primary_subject_id"),
    }

    subj_hit = subj_total = 0
    place_hit = place_total = time_hit = time_total = 0
    nosearch_hit = nosearch_total = 0
    roles_new, roles_llm = {}, {}
    scen = terms = queries = exact = scen_total = 0
    for new_plan, llm_plan in zip(plans, llm_plans):
        roles_new[new_plan["role"]] = roles_new.get(new_plan["role"], 0) + 1
        roles_llm[llm_plan["role"]] = roles_llm.get(llm_plan["role"], 0) + 1
        n_subj = mapping.get(new_plan.get("subject_id") or "")
        l_subj = llm_plan.get("subject_id")
        if l_subj is not None or n_subj is not None:
            subj_total += 1
            subj_hit += n_subj == l_subj
        if llm_plan.get("place"):
            place_total += 1
            place_hit += bool(new_plan.get("place"))
        if llm_plan.get("time"):
            time_total += 1
            time_hit += bool(new_plan.get("time"))
        if llm_plan["role"] in NO_SEARCH_ROLES:
            nosearch_total += 1
            nosearch_hit += new_plan["role"] in NO_SEARCH_ROLES
        for scenario in new_plan["scenarios"]:
            scen_total += 1
            terms += bool(scenario.get("search_terms"))
            queries += len(scenario.get("queries") or [])
            exact += scenario.get("specificity") == "exact"
        scen += len(new_plan["scenarios"])
    llm_scen_total = sum(len(p["scenarios"]) for p in llm_plans)
    report.update({
        "subject_agreement": round(subj_hit / subj_total, 3) if subj_total else None,
        "roles_distinct": len(roles_new),
        "role_tv_distance": round(0.5 * sum(
            abs(roles_new.get(r, 0) / len(plans) - roles_llm.get(r, 0) / len(llm_plans))
            for r in set(roles_new) | set(roles_llm)
        ), 3),
        "nosearch_recall": round(nosearch_hit / nosearch_total, 3) if nosearch_total else None,
        "place_presence": round(place_hit / place_total, 3) if place_total else None,
        "time_presence": round(time_hit / time_total, 3) if time_total else None,
        "scenarios_per_sentence": round(scen / len(plans), 3),
        "terms_coverage": round(terms / scen_total, 3) if scen_total else None,
        "queries_per_scenario": round(queries / scen_total, 3) if scen_total else None,
        "exact_share": round(exact / scen_total, 3) if scen_total else None,
        "llm_exact_share": round(
            sum(1 for p in llm_plans for s in p["scenarios"]
                if s.get("specificity") == "exact") / llm_scen_total, 3
        ),
    })
    records = [
        script_analyzer._sentence_record(i, s, plans[i], bible)
        for i, s in enumerate(sentences)
    ]
    loc_hit = loc_total = 0
    for record, llm_plan in zip(records, llm_plans):
        if llm_plan.get("place"):
            loc_total += 1
            loc_hit += bool(record["locations"])
    report["locations_presence"] = round(loc_hit / loc_total, 3) if loc_total else None
    return report


@pytest.fixture(scope="module")
def parity():
    return {name: compute_parity(name) for name in GOLDEN}


# ------------------------------------------------------------------
# Hard gates + verified floors
# ------------------------------------------------------------------

@pytest.mark.parametrize("name", GOLDEN)
def test_parity_closed_class_is_zero(parity, name):
    assert parity[name]["closed_class_entities"] == []


@pytest.mark.parametrize("name", GOLDEN)
def test_parity_search_terms_coverage_is_total(parity, name):
    assert parity[name]["terms_coverage"] == 1.0


@pytest.mark.parametrize("name", GOLDEN)
def test_parity_scenarios_per_sentence(parity, name):
    assert parity[name]["scenarios_per_sentence"] >= 1.8


@pytest.mark.parametrize("name", GOLDEN)
def test_parity_entities_per_sentence(parity, name):
    assert parity[name]["entities_per_sentence"] >= 0.60


@pytest.mark.parametrize("name", GOLDEN)
def test_parity_place_time_and_locations_presence(parity, name):
    assert parity[name]["place_presence"] >= 0.8
    assert parity[name]["time_presence"] >= 0.8
    assert parity[name]["locations_presence"] >= 0.8


@pytest.mark.parametrize("name", ["berlin_five_minute", "kohberger_truecrime"])
def test_parity_primary_subject_agreement(parity, name):
    assert parity[name]["primary_agree"] is True


# ------------------------------------------------------------------
# The harness reports every measure (the proposal reads these)
# ------------------------------------------------------------------

def test_parity_report_is_complete(parity):
    expected = {
        "closed_class_entities", "entities_per_sentence",
        "type_accuracy_floored", "type_accuracy_all_aligned",
        "primary_agree", "subject_agreement", "roles_distinct",
        "role_tv_distance", "nosearch_recall", "place_presence",
        "time_presence", "scenarios_per_sentence", "terms_coverage",
        "queries_per_scenario", "exact_share", "llm_exact_share",
        "locations_presence",
    }
    for name in GOLDEN:
        assert expected <= set(parity[name]), name
