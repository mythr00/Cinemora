"""VA-04 / VA-05 / VA-11 regression tests (Asset Verification Audit fixes).

VA-04: a subject-only match must not approve a candidate.
VA-05: the matched search query must not satisfy the metadata gate.
VA-11: the gate runs once (in _uve_verify_all); _uve_pick must not
       re-run it, and rejection counts must reflect single execution.
"""

import pytest

from services import media_pipeline as mp
from services import story_understanding as story


def _bible():
    return story.hydrate_bible({
        "entities": [
            {"id": "berlin_wall", "canonical": "Berlin Wall", "aliases": ["the Wall"], "type": "object"},
            {"id": "east_berlin", "canonical": "East Berlin", "aliases": [], "type": "location"},
        ]
    })


def _scenario(**over):
    sc = {
        "subject_id": "berlin_wall",
        "specificity": "representative",
        "search_terms": ["crowds crossing checkpoint"],
        "visual": "crowds of people crossing through an opened checkpoint",
        "era": "1989",
    }
    sc.update(over)
    return sc


# ---------------------------------------------------------------- VA-04

def test_subject_only_match_no_longer_passes():
    verdict = story.verify_candidate(
        {"title": "Berlin Wall — a short history", "snippet": "The Berlin Wall explained"},
        _scenario(), _bible(),
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "no_topical_evidence"
    assert verdict["contributions"]["subject"] == 0.45
    assert verdict["contributions"]["topical"] == 0.0


def test_subject_plus_era_only_still_rejected():
    verdict = story.verify_candidate(
        {"title": "Berlin Wall 1989 retrospective", "snippet": "Looking back at 1989"},
        _scenario(), _bible(),
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "no_topical_evidence"


def test_genuinely_relevant_candidate_still_passes():
    verdict = story.verify_candidate(
        {
            "title": "Crowds crossing the checkpoint at the Berlin Wall, 1989",
            "snippet": "Crowds of people crossing through the opened checkpoint in 1989",
        },
        _scenario(), _bible(),
    )
    assert verdict["accepted"] is True
    assert verdict["rejection_reason"] == ""
    assert verdict["contributions"]["topical"] > 0


def test_weak_topical_match_below_threshold():
    verdict = story.verify_candidate(
        {"title": "Checkpoint restaurant review", "snippet": "A checkpoint themed cafe"},
        _scenario(subject_id=None, search_terms=["crowds crossing checkpoint charlie border guards"]),
        _bible(),
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] in ("below_threshold", "no_topical_evidence")


def test_exact_still_requires_subject():
    verdict = story.verify_candidate(
        {"title": "Crowds crossing a checkpoint in 1989", "snippet": "people crossing"},
        _scenario(specificity="exact"), _bible(),
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "missing_required_subject"


# ---------------------------------------------------------------- VA-05

def test_gate_ignores_matched_query_words():
    # Stage 7 supersession (Review C.B.2): a plain representative
    # need is CONTEXTUAL and no longer carries named-entity absence
    # requirements at the gate. VA-05 (the query is not evidence) is
    # category-independent, so the need declares the EVIDENCE
    # category explicitly — the contract under which the gate-level
    # pin applies verbatim.
    need = {
        "required_type": "event",
        "specificity": "representative",
        "visual_category": "evidence",
        "required_subject": "East Berlin",
        "place": "berlin",
    }
    candidate = {
        "title": "Champagne wall rental for weddings",
        "snippet": "Stylish event decor hire",
        # The query contains every required term; the candidate does not.
        "matched_query": "East Berliners crossing Berlin Wall 1989",
    }
    ok, reason = mp.gate_shot_need(candidate, need)
    assert not ok
    assert reason == "missing_subject"


def test_gate_still_uses_candidate_metadata():
    need = {
        "required_type": "event",
        "specificity": "representative",
        "required_subject": "East Berlin",
        "place": "berlin",
    }
    candidate = {
        "title": "East Berliners cross into the West",
        "snippet": "Crowds in Berlin, November 1989",
        "matched_query": "unrelated query words",
    }
    ok, _ = mp.gate_shot_need(candidate, need)
    assert ok


# ---------------------------------------------------------------- VA-11

def test_gate_executes_once_per_candidate(monkeypatch):
    calls = []
    real_gate = mp.gate_shot_need

    def counting_gate(candidate, shot_need):
        calls.append(1)
        return real_gate(candidate, shot_need)

    monkeypatch.setattr(mp, "gate_shot_need", counting_gate)
    candidates = [
        {"title": "Champagne wall rental", "snippet": "weddings", "url": "https://x.example/1"},
        {"title": "Berlin Wall crowds 1989", "snippet": "crowds crossing", "url": "https://x.example/2"},
    ]
    # Evidence category declared explicitly (Stage 7 / C.B.2 — see
    # test_gate_ignores_matched_query_words): both candidates are
    # gate-rejected under the evidence contract, as before.
    need = {"required_type": "event", "specificity": "representative",
            "visual_category": "evidence",
            "required_subject": "East Berlin", "place": "berlin"}
    scored = mp._uve_verify_all(candidates, _scenario(), _bible(), shot_need=need)
    assert len(calls) == 2  # exactly one gate call per candidate

    from services.visual_registry import VisualRegistry
    picked = mp._uve_pick(scored, _scenario(), VisualRegistry(), set(), shot_need=need)
    assert len(calls) == 2  # pick did NOT re-run the gate
    assert picked == []     # gate-rejected / unverified candidates stay out


def test_pick_rejection_counts_single_execution(capsys):
    candidates = [
        {"title": "Champagne wall rental", "snippet": "weddings", "url": "https://x.example/1"},
    ]
    # Evidence category declared explicitly (Stage 7 / C.B.2 — see
    # test_gate_ignores_matched_query_words).
    need = {"required_type": "event", "specificity": "representative",
            "visual_category": "evidence",
            "required_subject": "East Berlin", "place": "berlin"}
    scored = mp._uve_verify_all(candidates, _scenario(), _bible(), shot_need=need)
    from services.visual_registry import VisualRegistry
    mp._uve_pick(scored, _scenario(), VisualRegistry(), set(), shot_need=need)
    out = capsys.readouterr().out
    assert out.count("REJECT missing_subject") == 1  # not printed twice
    rejected = [c for c in scored if c.get("reject_reason")]
    assert len(rejected) == 1 and rejected[0]["rejection_stage"] == "metadata_gate"
