"""Transformation Phase 5 (Phase 4.3 follow-up) — metadata-gate name
matching.

Phase 4.3's labelled calibration (576 labelled corpus rows) measured
the gate's absence checks rejecting candidates whose recorded
metadata names the required subject/place in non-literal form:
underscore-compound place strings ("boise_courtroom"), diacritic
variants ("Bernauer Straße" vs "Bernauer Strasse"), and bible
aliases the scorer already honoured but the gate ignored
("Kohberger" for "Bryan Kohberger").

These tests pin the fix's shape:
- folding + non-whitespace tokenisation + discriminative aliases
  satisfy the subject/place checks, with the basis recorded on the
  candidate record;
- the guards hold: a lone given name never satisfies, unrelated
  metadata still rejects, another entity's aliases do not leak;
- the similarity thresholds are untouched.
"""

from services import media_pipeline as mp
from services import story_understanding as story


def _need(**overrides):
    need = {
        "required_subject": "Bryan Kohberger",
        "required_type": "person",
        "place": "boise_courtroom",
        "specificity": "exact",
        "shot_type": "event",
        "subject_aliases": ["Kohberger", "Bryan"],
        "place_aliases": ["Boise courtroom", "courtroom in Boise", "Boise"],
        "subject_entity_type": "person",
    }
    need.update(overrides)
    return need


def _candidate(title):
    return {"title": title, "provider": "example", "url": "https://example.test/x"}


def test_canonical_boise_courtroom_case_passes_with_basis():
    # The Phase 4.3 canonical false reject: surname-only subject
    # mention + the place present only via its bible alias "Boise".
    candidate = _candidate(
        "Idaho judge sets Kohberger trial to begin in June 2025 "
        "| Boise State Public Radio"
    )
    ok, reason = mp.gate_shot_need(candidate, _need())
    assert ok, reason
    basis = candidate["_gate_match_basis"]
    assert basis["subject"]["via"] == "alias"
    assert basis["subject"]["matched"] == "Kohberger"
    assert basis["place"]["via"] == "alias"
    assert basis["place"]["matched"] == "Boise"


def test_underscore_compound_place_matches_spaced_title():
    candidate = _candidate("Hearing held in Boise courtroom on Tuesday")
    need = _need(place_aliases=[], subject_aliases=[])
    ok, reason = mp.gate_shot_need(
        candidate,
        {**need, "required_subject": "", "required_type": "place"},
    )
    assert ok, reason
    basis = candidate["_gate_match_basis"]
    assert basis["place"]["via"] == "tokens"
    assert basis["place"]["tokens"] == ["boise", "courtroom"]


def test_sharp_s_and_diacritic_folding():
    candidate = _candidate(
        "File:Mauergedenkstätte Bernauer Straße, Berlin, 170514, ako.jpg"
    )
    need = _need(
        required_subject="Bernauer Strasse",
        required_type="place",
        place="Bernauer Strasse",
        subject_aliases=[],
        place_aliases=[],
        subject_entity_type="place",
    )
    ok, reason = mp.gate_shot_need(candidate, need)
    assert ok, reason


def test_diacritic_subject_matches_folded_title():
    candidate = _candidate("Günter Schabowski announces new travel rules")
    need = _need(
        required_subject="Gunter Schabowski",
        place="",
        subject_aliases=[],
        place_aliases=[],
    )
    ok, reason = mp.gate_shot_need(candidate, need)
    assert ok, reason


def test_given_name_alias_alone_never_satisfies():
    # "Bryan" is on the need's alias list (the bible records it),
    # but a lone given name is not discriminative evidence.
    candidate = _candidate("Bryan Cranston wins award for stage role")
    ok, reason = mp.gate_shot_need(candidate, _need(place=""))
    assert not ok
    assert reason == "missing_person"


def test_surname_alias_satisfies_person_subject():
    candidate = _candidate("Kohberger sentenced to life in prison")
    ok, reason = mp.gate_shot_need(candidate, _need(place=""))
    assert ok, reason
    assert candidate["_gate_match_basis"]["subject"]["matched"] == "Kohberger"


def test_acronym_alias_satisfies_organization_subject():
    candidate = _candidate("FBI agents execute search warrant")
    need = _need(
        required_subject="Federal Bureau of Investigation",
        required_type="organization",
        place="",
        subject_aliases=["FBI", "FBI agents"],
        place_aliases=[],
        subject_entity_type="organization",
    )
    ok, reason = mp.gate_shot_need(candidate, need)
    assert ok, reason
    assert candidate["_gate_match_basis"]["subject"]["via"] == "alias"


def test_unrelated_metadata_still_rejected():
    candidate = _candidate("Local bakery wins regional bread award")
    ok, reason = mp.gate_shot_need(candidate, _need())
    assert not ok
    assert reason in ("missing_person", "missing_subject", "missing_place")


def test_other_entity_aliases_do_not_leak():
    # The need carries only ITS subject's aliases; a title naming a
    # different person (whose surname is not among them) still fails.
    candidate = _candidate("Madison Mogen scholarship fund announced")
    ok, reason = mp.gate_shot_need(candidate, _need(place=""))
    assert not ok
    assert reason == "missing_person"


def test_place_still_required_when_no_alias_evidence():
    # Subject satisfied via surname; the place is nowhere in the
    # metadata in any recorded form — the absence check must hold.
    candidate = _candidate("Kohberger pleads guilty to four murders")
    ok, reason = mp.gate_shot_need(candidate, _need())
    assert not ok
    assert reason == "missing_place"


def test_compound_place_component_alternative_still_works():
    candidate = _candidate("Students gather in Moscow, Idaho for vigil")
    need = _need(
        required_subject="King Road house",
        required_type="place",
        place="King Road house, Moscow, Idaho",
        subject_aliases=[],
        place_aliases=[],
        subject_entity_type="place",
    )
    # Place satisfied by the second component; the subject (the
    # house itself) is not evidenced, so the gate still rejects —
    # on the subject, not the place.
    failures = mp.gate_all_failures(candidate, need)
    assert "missing_place" not in failures
    assert "missing_subject" in failures


def test_thresholds_unchanged():
    assert story.ACCEPT_THRESHOLD == {
        "exact": 0.55,
        "representative": 0.34,
        "generic": 0.22,
    }


def test_alias_resolution_from_bible_shapes():
    bible = {
        "entities": [
            {
                "id": "bryan_kohberger",
                "canonical": "Bryan Kohberger",
                "type": "person",
                "aliases": ["Kohberger"],
            },
            {
                "id": "boise_courtroom",
                "canonical": "Boise courtroom",
                "type": "place",
                "aliases": ["courtroom in Boise", "Boise"],
            },
        ]
    }
    subject_aliases, place_aliases = mp._resolve_need_aliases(
        bible, bible["entities"][0], "Bryan Kohberger", "boise_courtroom"
    )
    assert "Kohberger" in subject_aliases
    # The id-shaped place resolves to its canonical name first.
    assert place_aliases[0] == "Boise courtroom"
    assert "Boise" in place_aliases
