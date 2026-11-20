"""Quality Recovery Phase 1 — verification-integrity acceptance tests.

Historical filename kept per the programme brief; these tests describe
contradiction rejection and banned-provider rejection in the
verification chain. Required-test numbers and canonical DM IDs are in
each docstring (test specification: DEGRADED_MODE_TEST_SPECIFICATION.md;
canonical numbering: QUALITY_RECOVERY_RECONCILIATION.md §2.2).

The retired degraded mode stays retired: nothing here lowers a
threshold (0.55 / 0.34 / 0.22 are untouched), admits a rejected
candidate, or substitutes unrelated media.
"""

import degraded_fixtures as fx

from services import media_pipeline as mp
from services import story_understanding as story
from services.visual_registry import VisualRegistry


# ---------------------------------------------------------------- test 5

def test_05_wrong_identity_rejected_at_generic_and_exact():
    """Required test 5 (DM-04 sibling / workstream A): M01 names a
    different bible person with full topical overlap + era. The old
    arithmetic scored it 0.25 >= the generic 0.22 threshold; it must
    now be a hard contradictory_person rejection at BOTH
    specificities, and never enter the pick pool."""
    for specificity in ("generic", "exact"):
        verdict = story.verify_candidate(
            fx.MISLEADING["M01"](),
            fx.scenario(specificity=specificity),
            fx.bible(),
        )
        assert verdict["accepted"] is False, specificity
        assert verdict["rejection_reason"] == "contradictory_person", specificity

    scored = mp._uve_verify_all(
        [fx.MISLEADING["M01"]()], fx.scenario(), fx.bible(),
        shot_need=fx.shot_need(),
    )
    picked = mp._uve_pick(
        scored, fx.scenario(), VisualRegistry("t5"), set(),
        shot_need=fx.shot_need(),
    )
    assert picked == []


def test_05_companion_naming_required_and_another_person_is_not_rejected():
    """Required test 5 companion: group/event coverage naming the
    required person AND another person is not a contradiction."""
    cand = fx.candidate(
        title="Bryan Kohberger arrest — Kaylee Goncalves case, Moscow 2022",
        snippet="Police arrest Bryan Kohberger in Moscow in 2022; Kaylee Goncalves case",
        description="arrest police house 2022",
    )
    verdict = story.verify_candidate(cand, fx.scenario(), fx.bible())
    assert verdict["rejection_reason"] != "contradictory_person"


def test_05_source_quality_cannot_rescue_a_contradiction():
    """Workstream A: source quality operates only on accepted
    candidates — a prestigious-source wrong-person candidate is still
    rejected before ranking ever sees it."""
    cand = fx.MISLEADING["M01"]()
    cand["source"] = "bbc.com"
    cand["provider"] = "bbc.com"
    verdict = story.verify_candidate(cand, fx.scenario(specificity="generic"), fx.bible())
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "contradictory_person"


# ---------------------------------------------------------------- test 6

def test_06_contradictory_date_rejected_exact():
    """Required test 6 (canonical DM-15): an explicit 2019 date for an
    exact 2022 event is contradictory_date. On the audited HEAD era
    was bonus-only, so this is the defect reproduction landed with
    its fix."""
    verdict = story.verify_candidate(
        fx.MISLEADING["M03"](), fx.scenario(), fx.bible()
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "contradictory_date"


def test_06_missing_date_is_not_a_contradiction():
    """Required test 6 companion: a dateless twin is judged on its
    remaining signals — absence is not a contradiction."""
    cand = fx.candidate(
        title="Bryan Kohberger arrest in Moscow, Idaho",
        snippet="Police arrest Bryan Kohberger outside the house in Moscow",
        description="Arrest police house Moscow",
    )
    verdict = story.verify_candidate(cand, fx.scenario(), fx.bible())
    assert verdict["rejection_reason"] != "contradictory_date"
    assert verdict["accepted"] is True


def test_06_decade_compatible_candidate_is_not_rejected():
    """Required test 6 companion: a 1963 asset is fine for a '1960s'
    beat; a 2019 asset for that decade is a contradiction."""
    scenario = fx.scenario(era="1960s")
    ok = fx.candidate(
        title="Bryan Kohberger arrest in Moscow, 1963",
        snippet="Police arrest Bryan Kohberger outside the house in Moscow in 1963",
        description="Arrest police house Moscow 1963",
    )
    verdict = story.verify_candidate(ok, scenario, fx.bible())
    assert verdict["rejection_reason"] != "contradictory_date"

    bad = fx.candidate(
        title="Bryan Kohberger arrest in Moscow, 2019",
        snippet="Police arrest Bryan Kohberger outside the house in Moscow in 2019",
        description="Arrest police house Moscow 2019",
    )
    verdict = story.verify_candidate(bad, scenario, fx.bible())
    assert verdict["rejection_reason"] == "contradictory_date"
    assert verdict["accepted"] is False


def test_06_gate_rejects_contradictory_date():
    """Workstream A gate leg: gate_shot_need carries the same date
    rule where the shot need carries the era."""
    ok, reason = mp.gate_shot_need(fx.MISLEADING["M03"](), fx.shot_need())
    assert not ok
    assert reason == "contradictory_date"


# ------------------------------------------------------- place / event

def test_contradictory_place_rejected():
    """Workstream A: the candidate positively names a DIFFERENT bible
    place and not the required place."""
    cand = fx.candidate(
        title="Bryan Kohberger arrest in Pullman, Washington, 2022",
        snippet="Police arrest Bryan Kohberger outside the house in Pullman in 2022",
        description="Arrest police house Pullman 2022",
        url="https://www.youtube.com/watch?v=WRONGPLAC1",
        video_url="https://www.youtube.com/watch?v=WRONGPLAC1",
    )
    verdict = story.verify_candidate(cand, fx.scenario(), fx.bible())
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "contradictory_place"


def test_contradictory_event_rejected():
    """Workstream A: the candidate names a different bible event
    (campus vigil) instead of the required event (arrest), with the
    required person present so only the event check can fire."""
    scenario = fx.scenario(subject_id="kohberger_arrest", subject="Kohberger arrest")
    cand = fx.candidate(
        title="Campus vigil for the victims, Moscow, Idaho, 2022",
        snippet="A campus vigil in Moscow in 2022, police house gathering",
        description="vigil police house 2022",
        url="https://www.youtube.com/watch?v=WRONGEVEN1",
        video_url="https://www.youtube.com/watch?v=WRONGEVEN1",
    )
    verdict = story.verify_candidate(cand, scenario, fx.bible())
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "contradictory_event"


# ------------------------------------------------------------------- M11

def test_m11_banned_provider_rejected_by_verification_chain():
    """Workstream J / spec adversary M11: a candidate whose provider
    is permanently banned is rejected with the canonical
    banned_provider outcome — by verify_candidate, by the gate, and
    through the full _uve_verify_all chain (never picked)."""
    cand = fx.MISLEADING["M11"]()

    verdict = story.verify_candidate(cand, fx.scenario(), fx.bible())
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "banned_provider"

    ok, reason = mp.gate_shot_need(cand, fx.shot_need())
    assert not ok
    assert reason == "banned_provider"

    scored = mp._uve_verify_all(
        [cand], fx.scenario(), fx.bible(), shot_need=fx.shot_need()
    )
    assert scored and scored[0]["reject_reason"] == "banned_provider"
    picked = mp._uve_pick(
        scored, fx.scenario(), VisualRegistry("m11"), set(),
        shot_need=fx.shot_need(),
    )
    assert picked == []


def test_m11_banned_provider_variants_rejected():
    """Decorated banned labels are still banned (defence in depth)."""
    for provider in ("Pex" + "els Videos", "pixa" + "bay.com"):
        cand = fx.candidate(provider=provider, source=provider)
        verdict = story.verify_candidate(cand, fx.scenario(), fx.bible())
        assert verdict["rejection_reason"] == "banned_provider", provider
