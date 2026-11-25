"""Phase 4c / D-05 — similarity-provider seam characterization.

The expected numbers below are HAND-COMPUTED from the documented
formula (subject +0.45 · topical 0.45*min(1, overlap*1.5) · era
+0.10 · wrong-person -0.30, overlap = |wanted ∩ candidate| /
|wanted| over stemmed tokens, _content_tokens keeps tokens of
length >= 4 and _stem maps trailing "ies"->"y" and strips a
trailing "s" on words longer than 4). They are not captured from
the implementation's own output.

Stemming facts used below (hand-derived from _stem's rule —
strip a trailing "s" only when the word is LONGER than 4 chars):
  "patrols" -> "patrol" (len 7 > 4, trailing s stripped)
  "cars" does NOT stem (len 4) — it stays "cars"
  "police", "patrol", "highway", "engine", "analytical" unchanged.
"""

import pytest

from services import similarity_provider, story_understanding as story


PROVIDER = similarity_provider.TokenOverlapSimilarity()


# ---------------------------------------------------------------------
# Seam level: topical overlap
# ---------------------------------------------------------------------

def test_overlap_full_partial_and_none():
    # wanted 4 tokens, candidate carries 2 -> 2/4 = 0.5
    assert PROVIDER.topical_overlap(
        {"police", "patrol", "highway", "court"},
        {"police", "patrol", "station"},
    ) == 0.5
    assert PROVIDER.topical_overlap({"police"}, {"police"}) == 1.0
    assert PROVIDER.topical_overlap({"police"}, {"station"}) == 0.0
    # Empty wanted set is defined as 0.0 (no topical evidence).
    assert PROVIDER.topical_overlap(set(), {"police"}) == 0.0


# ---------------------------------------------------------------------
# Seam level: weighted score
# ---------------------------------------------------------------------

def test_score_components_hand_computed():
    # No evidence at all.
    s = PROVIDER.score()
    assert s.total == 0.0
    assert s.components == {
        "subject": 0.0, "topical": 0.0, "era": 0.0, "wrong_person": 0.0,
    }

    # Subject only: +0.45.
    s = PROVIDER.score(subject_hit=True)
    assert s.total == pytest.approx(0.45)
    assert s.components["subject"] == 0.45

    # Overlap 0.25 -> topical = 0.45 * (0.25 * 1.5) = 0.45 * 0.375
    # = 0.16875 (component recorded rounded to 3dp: 0.169).
    s = PROVIDER.score(topical_overlap=0.25)
    assert s.total == pytest.approx(0.16875)
    assert s.components["topical"] == pytest.approx(0.169)

    # Overlap 1.0 saturates: topical = 0.45 * min(1, 1.5) = 0.45.
    # Subject + topical + era = 0.45 + 0.45 + 0.10 = 1.00.
    s = PROVIDER.score(subject_hit=True, topical_overlap=1.0, era_hit=True)
    assert s.total == pytest.approx(1.0)
    assert s.components == {
        "subject": 0.45, "topical": 0.45, "era": 0.10, "wrong_person": 0.0,
    }

    # Wrong person only: -0.30.
    s = PROVIDER.score(wrong_person_hit=True)
    assert s.total == pytest.approx(-0.30)
    assert s.components["wrong_person"] == -0.30

    # overlap=None means "no topical set was defined" — no topical
    # component is added (distinct from overlap 0.0 in the record:
    # both add 0.0, but verify_candidate's VA-04/MA-30 logic keys
    # off the wanted set, not this value).
    s = PROVIDER.score(subject_hit=True, topical_overlap=None)
    assert s.total == pytest.approx(0.45)


def test_default_provider_is_the_token_overlap_implementation():
    provider = similarity_provider.get_provider()
    assert isinstance(provider, similarity_provider.TokenOverlapSimilarity)
    assert provider.name == "token_overlap"


# ---------------------------------------------------------------------
# Integration: verify_candidate through the seam (identical scores)
# ---------------------------------------------------------------------

_BIBLE_ADA = {
    "by_id": {
        "ada": {
            "id": "ada", "canonical": "Ada Lovelace",
            "type": "person", "aliases": [],
        },
    },
    "entities": [
        {"id": "ada", "canonical": "Ada Lovelace",
         "type": "person", "aliases": []},
    ],
}


def test_verify_candidate_full_match_scores_one():
    # wanted = _content_tokens("analytical engine") = {analytical,
    # engine}; candidate text carries both -> overlap 1.0.
    # subject hit (+0.45) + topical (0.45) + era 1843 present
    # (+0.10) = 1.000, accepted at the exact threshold 0.55.
    verdict = story.verify_candidate(
        {"title": "Ada Lovelace analytical engine 1843"},
        {"subject_id": "ada", "specificity": "exact",
         "search_terms": ["analytical engine"], "visual": "",
         "era": "1843"},
        _BIBLE_ADA,
    )
    assert verdict["score"] == pytest.approx(1.0)
    assert verdict["contributions"] == {
        "subject": 0.45, "topical": 0.45, "era": 0.10, "wrong_person": 0.0,
    }
    assert verdict["accepted"] is True


def test_verify_candidate_partial_overlap_hand_computed():
    # No subject. wanted = {police, patrol} U {police, patrol,
    # highway} = {police, patrol, highway} (3 tokens).
    # Candidate "Police station interview": stemmed tokens include
    # police only of the wanted set -> overlap 1/3.
    # topical = 0.45 * min(1, (1/3)*1.5) = 0.45 * 0.5 = 0.225.
    # 0.225 < representative threshold 0.34 -> below_threshold.
    verdict = story.verify_candidate(
        {"title": "Police station interview"},
        {"subject_id": None, "specificity": "representative",
         "search_terms": ["police patrol"],
         "visual": "police patrol highway", "era": None},
        {"by_id": {}, "entities": []},
    )
    assert verdict["score"] == pytest.approx(0.225)
    assert verdict["contributions"]["topical"] == pytest.approx(0.225)
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "below_threshold"


def test_verify_candidate_stemming_hand_computed():
    # wanted = _content_tokens("police patrols") = {police, patrol}
    # ("patrols", len 7 > 4, stems to "patrol"). Candidate
    # "police patrol chase" stems to {police, patrol, chase} ->
    # overlap 2/2 = 1.0 -> topical 0.45, accepted at
    # representative 0.34 with no subject/era terms.
    verdict = story.verify_candidate(
        {"title": "police patrol chase"},
        {"subject_id": None, "specificity": "representative",
         "search_terms": ["police patrols"], "visual": "", "era": None},
        {"by_id": {}, "entities": []},
    )
    assert verdict["score"] == pytest.approx(0.45)
    assert verdict["accepted"] is True


def test_verify_candidate_short_plural_does_not_stem():
    # Boundary of the same hand-derived rule: "cars" (len 4) does
    # NOT stem, so wanted = {police, cars} and a candidate saying
    # "car" matches only "police" -> overlap 1/2 = 0.5 ->
    # topical = 0.45 * 0.75 = 0.3375 -> recorded score 0.338.
    verdict = story.verify_candidate(
        {"title": "police car chase"},
        {"subject_id": None, "specificity": "representative",
         "search_terms": ["police cars"], "visual": "", "era": None},
        {"by_id": {}, "entities": []},
    )
    assert verdict["score"] == pytest.approx(0.338)
    assert verdict["accepted"] is False
