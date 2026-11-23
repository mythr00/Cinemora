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

import json

import degraded_fixtures as fx

from services import media_pipeline as mp
from services import story_understanding as story
from services.asset_report import build_asset_report, build_rights_manifest
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

# ---------------------------------------------------------------- test 1

def test_01_exact_candidate_beats_generic_candidate():
    """Required test 1 (visual-quality ranking; DM-04/DM-12 family):
    B02 exact event beat — the candidate matching subject+event+
    place+era ranks first and is selected; the M04 generic same-topic
    candidate (topical overlap only) is rejected, and even the
    maximum format-intent bonus (0.10, at confidence 1.0 for the
    favoured format) cannot close the relevance gap."""
    exact = fx.candidate()
    generic = fx.MISLEADING["M04"]()

    scored = mp._uve_verify_all(
        [generic, exact], fx.scenario(), fx.bible(),
        shot_need=fx.shot_need(),
    )
    by_url = {c["url"]: c for c in scored}
    # Ranked first by the verifier's own ordering.
    assert scored[0]["url"] == exact["url"]
    exact_score = by_url[exact["url"]]["universal_score"]
    generic_item = by_url[generic["url"]]
    # Metadata-absence bypass (run-6345 fix, 2026-11-23): M04's
    # gate failures are absence-only (missing_person), so the gate
    # no longer terminates it — the scorer's exact-specificity
    # verdict (missing_required_subject) is the recorded rejection.
    # The gate's failure remains on the record as diagnostics, and
    # the selection outcome this test exists to pin is unchanged.
    assert generic_item["reject_reason"] == "missing_required_subject"
    assert generic_item["rejection_stage"] == "scoring"
    assert "missing_person" in (generic_item.get("gate_failures") or [])
    assert (generic_item.get("verification") or {}).get("accepted") is False
    # The bounded format-intent bonus (<= 0.10, here at its maximum:
    # footage intent, confidence 1.0, generic is a video) still
    # leaves the generic candidate below the exact one.
    assert generic_item["universal_score"] + 0.10 < exact_score

    picked = mp._uve_pick(
        scored, fx.scenario(), VisualRegistry("t1"), set(),
        shot_need=fx.shot_need(),
    )
    assert [p["url"] for p in picked] == [exact["url"]]


# ---------------------------------------------------------------- test 2

def test_02_correct_entity_beats_keyword_only_candidate():
    """Required test 2 (visual-quality ranking; DM-12): B01 named
    person, exact. A keyword-only candidate with HIGHER topical
    overlap (every event/place token, no person) is rejected —
    missing_person at the gate, missing_required_subject at the
    verifier — while the entity candidate, with lower topical
    contribution, is the one selected."""
    entity = fx.candidate(
        title="Bryan Kohberger in Moscow, Idaho, 2022",
        snippet="Bryan Kohberger Moscow, Idaho 2022",
        description="Bryan Kohberger Moscow 2022",
        url="https://www.youtube.com/watch?v=ENTITY0001",
        video_url="https://www.youtube.com/watch?v=ENTITY0001",
    )
    keyword_only = fx.candidate(
        title="Arrest police house Moscow 2022 full coverage",
        snippet="Police arrest outside the house in Moscow in 2022 arrest police house",
        description="arrest police house Moscow 2022",
        url="https://www.youtube.com/watch?v=KEYWORD001",
        video_url="https://www.youtube.com/watch?v=KEYWORD001",
    )

    entity_verdict = story.verify_candidate(entity, fx.scenario(), fx.bible())
    keyword_verdict = story.verify_candidate(
        keyword_only, fx.scenario(), fx.bible()
    )
    # The keyword candidate is topically STRONGER — and still rejected.
    assert keyword_verdict["contributions"]["topical"] > \
        entity_verdict["contributions"]["topical"]
    assert keyword_verdict["accepted"] is False
    assert keyword_verdict["rejection_reason"] == "missing_required_subject"
    assert entity_verdict["accepted"] is True

    scored = mp._uve_verify_all(
        [keyword_only, entity], fx.scenario(), fx.bible(),
        shot_need=fx.shot_need(),
    )
    by_url = {c["url"]: c for c in scored}
    # Metadata-absence bypass (run-6345 fix, 2026-11-23): the gate
    # failure (missing_person, absence-only) no longer terminates
    # the candidate, so the recorded primary is the verifier's
    # missing_required_subject — the same verdict asserted directly
    # above; the gate failure rides on the record as diagnostics.
    assert by_url[keyword_only["url"]]["reject_reason"] == (
        "missing_required_subject"
    )
    assert "missing_person" in (
        by_url[keyword_only["url"]].get("gate_failures") or []
    )

    picked = mp._uve_pick(
        scored, fx.scenario(), VisualRegistry("t2"), set(),
        shot_need=fx.shot_need(),
    )
    assert [p["url"] for p in picked] == [entity["url"]]


# ---------------------------------------------------------------- test 3

def test_03_correct_location_beats_unrelated_location():
    """Required test 3 (visual-quality ranking; workstream A place
    leg): B03 location beat, exact. A same-era, same-topic candidate
    about a DIFFERENT bible place (Pullman instead of Moscow) is a
    positive place contradiction / gate rejection, and is not
    selected while the correct-location candidate exists."""
    scenario = fx.scenario(
        subject_id="moscow_idaho", subject="Moscow, Idaho",
        kind="place", event="",
        visual="Moscow Idaho town streets",
        search_terms=["Moscow", "streets", "town"],
    )
    need = fx.shot_need(
        required_subject_id="moscow_idaho",
        required_subject="Moscow, Idaho", required_type="location",
        event="", description="Moscow Idaho town streets",
    )
    correct = fx.candidate(
        title="Moscow, Idaho town streets in 2022",
        snippet="Streets and town views of Moscow, Idaho in 2022, police house",
        description="Moscow streets town 2022",
        url="https://www.youtube.com/watch?v=LOCCORREC1",
        video_url="https://www.youtube.com/watch?v=LOCCORREC1",
    )
    wrong = fx.candidate(
        title="Pullman, Washington town streets in 2022",
        snippet="Streets and town views of Pullman, Washington in 2022, police house",
        description="Pullman streets town 2022",
        url="https://www.youtube.com/watch?v=LOCWRONG01",
        video_url="https://www.youtube.com/watch?v=LOCWRONG01",
    )

    verdict = story.verify_candidate(wrong, scenario, fx.bible())
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "contradictory_place"

    scored = mp._uve_verify_all(
        [wrong, correct], scenario, fx.bible(), shot_need=need,
    )
    by_url = {c["url"]: c for c in scored}
    assert by_url[wrong["url"]]["reject_reason"] in (
        "contradictory_place", "missing_place", "missing_subject",
    )
    assert scored[0]["url"] == correct["url"]

    picked = mp._uve_pick(
        scored, scenario, VisualRegistry("t3"), set(), shot_need=need,
    )
    assert [p["url"] for p in picked] == [correct["url"]]


# ---------------------------------------------------------------- test 4

def test_04_correct_event_beats_same_topic_unrelated_event():
    """Required test 4 (visual-quality ranking; workstream A event
    leg): B02 with two candidates sharing place + era + broad topic
    tokens, differing only in event. The M02 candidate (a campus
    fire/protest in the same city and year) scores lower on topical
    evidence (the event tokens are absent) and is not selected while
    the correct-event candidate is available; a candidate naming a
    different BIBLE event (campus vigil) is a hard
    contradictory_event rejection."""
    scenario = fx.scenario(
        subject_id="kohberger_arrest", subject="Kohberger arrest",
    )
    need = fx.shot_need(
        required_subject_id="kohberger_arrest",
        required_subject="Kohberger arrest", required_type="event",
    )
    correct = fx.candidate()
    unrelated = fx.MISLEADING["M02"]()

    correct_verdict = story.verify_candidate(correct, scenario, fx.bible())
    unrelated_verdict = story.verify_candidate(unrelated, scenario, fx.bible())
    assert correct_verdict["accepted"] is True
    assert unrelated_verdict["accepted"] is False
    assert unrelated_verdict["score"] < correct_verdict["score"]
    assert unrelated_verdict["contributions"]["topical"] < \
        correct_verdict["contributions"]["topical"]

    vigil = fx.candidate(
        title="Campus vigil for the victims, Moscow, Idaho, 2022",
        snippet="A campus vigil in Moscow in 2022, police house gathering",
        description="vigil police house 2022",
        url="https://www.youtube.com/watch?v=WRONGEVEN1",
        video_url="https://www.youtube.com/watch?v=WRONGEVEN1",
    )
    vigil_verdict = story.verify_candidate(vigil, scenario, fx.bible())
    assert vigil_verdict["accepted"] is False
    assert vigil_verdict["rejection_reason"] == "contradictory_event"

    scored = mp._uve_verify_all(
        [unrelated, correct], scenario, fx.bible(), shot_need=need,
    )
    picked = mp._uve_pick(
        scored, scenario, VisualRegistry("t4"), set(), shot_need=need,
    )
    assert [p["url"] for p in picked] == [correct["url"]]


# ---------------------------------------------------------------- test 8

def _dicts_containing(node, needle):
    """Every dict in a parsed-JSON structure whose own values mention
    `needle` (used to scan a serialised report for one asset's fields
    without assuming a layout)."""
    found = []
    if isinstance(node, dict):
        if any(needle in str(v) for v in node.values()):
            found.append(node)
        for value in node.values():
            found.extend(_dicts_containing(value, needle))
    elif isinstance(node, list):
        for value in node:
            found.extend(_dicts_containing(value, needle))
    return found


def test_08_unknown_rights_stay_unknown_through_report_and_manifest():
    """Required test 8 (rights honesty; AUD-15/VA-09): M07 is fully
    relevant with license "unknown" — it IS selected under allow_all,
    and through build_asset_report + build_rights_manifest the
    licence stays "unknown" in every field that mentions the asset
    (scanned over the serialised artefacts). The Wikimedia control
    asset keeps its real licence verbatim, proving the scan can tell
    the two apart."""
    # Selection leg: unknown rights do not block a relevant asset.
    scored = mp._uve_verify_all(
        [fx.MISLEADING["M07"]()], fx.scenario(), fx.bible(),
        shot_need=fx.shot_need(),
    )
    picked = mp._uve_pick(
        scored, fx.scenario(), VisualRegistry("t8"), set(),
        shot_need=fx.shot_need(),
    )
    assert len(picked) == 1
    assert picked[0]["license"] == "unknown"

    m07_asset = {
        "file_path": "/p/m07.mp4", "path": "/p/m07.mp4", "type": "video",
        "title": "Bryan Kohberger arrest in Moscow, Idaho, 2022",
        "source": "youtube", "provider": "youtube",
        "video_url": "https://www.youtube.com/watch?v=VALIDID001",
        "license": "unknown", "verification_status": "verified",
        "universal_score": 1.0, "creator": "News Channel",
        "retrieved_at": "2026-11-20T08:00:00+00:00",
    }
    control_asset = {
        "file_path": "/p/control.jpg", "path": "/p/control.jpg",
        "type": "image", "title": "File:Moscow Idaho.jpg",
        "source": "wikimedia",
        "source_url": "https://commons.wikimedia.org/wiki/File:Moscow_Idaho.jpg",
        "image_url": "https://upload.wikimedia.org/moscow.jpg",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0",
        "rights_status": "license_recorded",
        "verification_status": "verified", "universal_score": 0.9,
        "creator": "Commons Photographer",
        "retrieved_at": "2026-11-20T08:01:00+00:00",
    }
    scene = {
        "scene_number": 1,
        "videos": [m07_asset], "images": [control_asset],
        "sentences": [
            {"sentence_number": 1, "videos": [m07_asset],
             "images": [control_asset]},
        ],
        "asset_stats": {
            "candidates_considered": 2, "candidates_rejected": 0,
            "downloads_failed": 0,
        },
    }
    report = build_asset_report(
        [scene], renderer_audit={"placeholder_count": 0},
    )
    manifest = build_rights_manifest([scene])

    # Report-level rights split: one unknown, one recorded — separate.
    assert report["rights"] == {"recorded_licences": 1, "unknown_licences": 1}
    assert report["unknown_licensing"] == 1
    dims_rights = report["verification_dimensions"]["rights"]
    assert dims_rights == {"recorded_licences": 1, "unknown_licences": 1}

    by_path = {e["file_path"]: e for e in manifest}
    assert by_path["/p/m07.mp4"]["license"] == "unknown"
    assert by_path["/p/control.jpg"]["license"] == "CC BY 4.0"
    assert by_path["/p/control.jpg"]["license_url"].endswith("/by/4.0")

    # Serialised scan: no dict that mentions the M07 asset (by path
    # or source URL) carries any licence value other than "unknown";
    # the control's real licence appears only on the control's own
    # records.
    serialised = json.loads(json.dumps(
        {"report": report, "manifest": manifest}
    ))
    for needle in ("/p/m07.mp4", "VALIDID001"):
        for record in _dicts_containing(serialised, needle):
            if "license" in record:
                assert record["license"] == "unknown", (needle, record)
    for record in _dicts_containing(serialised, "/p/control.jpg"):
        if "license" in record:
            assert record["license"] == "CC BY 4.0", record
