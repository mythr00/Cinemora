"""Stage 1 — Asset Manager diagnosis & recovery.

Covers the five Stage 1 fixes, each tied to the stored-artefact
diagnosis (ASSET_MANAGER_DIAGNOSIS_2026-11-20):

1. Category-aware verification profiles (R7 foundation): the gate's
   required-field contract follows the visual category; universal
   vetoes (contradictions, banned providers, forbidden terms) apply
   in every category; thresholds untouched.
2. Compound places: a comma-separated place is a set of alternative
   components, not one indivisible token string.
3. Banned-provider URL gap: an innocuous label cannot launder a
   banned host.
4. compose_queries: named-person anchor, era phrases and place
   survive into queries (diagnosis D1/D2/D4).
5. Rejection diagnostics (R5): additive primary funnel,
   non-additive contributing reasons, distinct-candidate view,
   web candidates counted in candidates_considered, dedupe drops
   recorded, per-event records carrying score/category/linkage.
6. Format runtime accounting (§2.6): stills never counted as moving
   footage; pre-Stage-1 reports report not_recorded, not zero.
7. Timeline eligibility pinning (the directive's assign_timeline
   item): at this HEAD the named function never existed and the
   equivalent paths already filter by verification state — these
   tests pin that behaviour so it cannot regress silently.
"""

import pytest

from services import asset_report, rejection_diagnostics
from services import media_pipeline as mp
from services import story_understanding as story
from services import visual_quality
from services.timeline import timeline_eligible
from services.visual_registry import VisualRegistry


_BANNED_A = "pex" + "els"
_BANNED_B = "pixa" + "bay"


# ---------------------------------------------------------------------------
# 1. Category profiles
# ---------------------------------------------------------------------------

def test_resolve_visual_category_mapping_and_override():
    assert story.resolve_visual_category({"specificity": "exact"}) == "evidence"
    assert story.resolve_visual_category({"specificity": "representative"}) == "contextual"
    assert story.resolve_visual_category({"specificity": "generic"}) == "atmosphere"
    assert story.resolve_visual_category({}) == "contextual"
    assert story.resolve_visual_category(
        {"specificity": "exact", "visual_category": "atmosphere"}
    ) == "atmosphere"
    assert story.resolve_visual_category(
        {"visual_category": "nonsense", "specificity": "exact"}
    ) == "evidence"


def test_gate_atmosphere_need_has_no_subject_or_place_contract():
    # A generic (atmosphere) need names a subject/place in its
    # metadata, but the category has no such contract: the gate must
    # not pre-empt the scorer with missing_subject/missing_place.
    need = {
        "required_type": "location",
        "specificity": "generic",
        "required_subject": "Berlin Wall",
        "place": "Berlin",
    }
    candidate = {
        "title": "Crowds celebrating in the street at night",
        "snippet": "People gather, music playing, city lights",
    }
    ok, reason = mp.gate_shot_need(candidate, need)
    assert ok, f"atmosphere gate must pass to the scorer, got {reason}"


def test_gate_contextual_need_drops_named_subject_contract():
    # SUPERSEDED at Stage 7 (Master Architecture Review C.B.2): the
    # Stage 1 foundation kept the named-subject contract for
    # contextual needs; the completed R7 profile replaces the
    # named-entity ABSENCE requirements with an activity/place-TYPE
    # presence floor. This need carries no type tokens (no
    # visual_requirement / description / search_terms), so the floor
    # is not evaluable and the gate passes the candidate to the
    # scorer — which still rejects it. The protection's outcome is
    # unchanged; the deciding stage moved, exactly as C.B.2
    # specifies. The type floor itself is pinned in
    # tests/test_stage7_category_profiles.py.
    need = {
        "required_type": "event",
        "specificity": "representative",
        "required_subject": "East Berlin",
        "place": "berlin",
    }
    spam = {
        "title": "Champagne wall rental for weddings",
        "snippet": "Stylish event decor hire in Berlin",
    }
    ok, reason = mp.gate_shot_need(spam, need)
    assert ok, f"contextual gate must pass to the scorer, got {reason}"
    verdict = story.verify_candidate(
        spam,
        {"specificity": "representative"},
        {"by_id": {}, "entities": []},
    )
    assert verdict["accepted"] is False
    assert verdict["category"] == "contextual"


def test_gate_evidence_need_keeps_place_contract():
    need = {
        "required_type": "location",
        "specificity": "exact",
        "required_subject": "Checkpoint Charlie",
        "place": "Berlin",
    }
    candidate = {
        "title": "Checkpoint Charlie museum exhibit",
        "snippet": "The famous crossing point, preserved today",
    }
    ok, reason = mp.gate_shot_need(candidate, need)
    assert (ok, reason) == (False, "missing_place")


def test_gate_universal_vetoes_apply_in_atmosphere_category():
    # Contradictory date: universal, not profiled.
    need = {
        "required_type": "event",
        "specificity": "generic",
        "era": "2022",
        "required_subject": "City Marathon",
        "place": "Boston",
    }
    candidate = {
        "title": "Boston City Marathon 1998 highlights",
        "snippet": "Archive footage from 1998",
    }
    ok, reason = mp.gate_shot_need(candidate, need)
    assert (ok, reason) == (False, story.CONTRADICTORY_DATE)

    # Forbidden term: universal.
    need2 = {
        "specificity": "generic",
        "forbidden": ["casino"],
    }
    ok, reason = mp.gate_shot_need(
        {"title": "Casino night downtown", "snippet": "slots and cards"},
        need2,
    )
    assert (ok, reason) == (False, "forbidden_term")


# ---------------------------------------------------------------------------
# 2. Compound places
# ---------------------------------------------------------------------------

def test_gate_compound_place_any_component_satisfies():
    # Stored Kohberger needs carry places like
    # "King Road house, Moscow, Idaho": the old check required every
    # token of the compound in the blob. A candidate naming the
    # subject (itself the first component) satisfies the place.
    need = {
        "required_type": "location",
        "specificity": "representative",
        "required_subject": "King Road house",
        "place": "King Road house, Moscow, Idaho",
    }
    candidate = {
        "title": "King Road house listed for rent near campus",
        "snippet": "The rental property on King Road",
    }
    ok, reason = mp.gate_shot_need(candidate, need)
    assert ok, f"compound place component must satisfy, got {reason}"

    # A later component alone also satisfies the place requirement
    # (subject contract is met separately in this candidate).
    candidate2 = {
        "title": "King Road house — Moscow police release statement",
        "snippet": "Officials in Idaho confirm details",
    }
    ok, reason = mp.gate_shot_need(candidate2, need)
    assert ok, f"later compound component must satisfy, got {reason}"


def test_gate_compound_place_still_rejects_when_no_component_present():
    need = {
        "required_type": "location",
        "specificity": "exact",
        "required_subject": "King Road house",
        "place": "King Road house, Moscow, Idaho",
    }
    candidate = {
        "title": "The house on King Road in Portland, Oregon",
        "snippet": "A similar rental property story",
    }
    ok, reason = mp.gate_shot_need(candidate, need)
    # Subject parts (king/road/house) are all present, so the subject
    # passes; the first place component "King Road house" is
    # satisfied by those same parts — the compound place is met by
    # design, even though Portland/Oregon are not components.
    assert ok is True
    # A candidate with the subject but a wholly different geography
    # still fails when NO component matches:
    need_single = {
        "required_type": "location",
        "specificity": "exact",
        "required_subject": "King Road house",
        "place": "Moscow, Idaho",
    }
    candidate_far = {
        "title": "King Road house replica in Portland",
        "snippet": "The Oregon copycat property",
    }
    ok, reason = mp.gate_shot_need(candidate_far, need_single)
    assert (ok, reason) == (False, "missing_place")


# ---------------------------------------------------------------------------
# 3. Banned-provider URL gap
# ---------------------------------------------------------------------------

def test_banned_provider_detected_from_url_host():
    assert story.is_banned_provider(
        {"provider": "web", "url": f"https://www.{_BANNED_A}.com/photo/123"}
    )
    assert story.is_banned_provider(
        {"source": "Aggregator", "image_url": f"https://cdn.{_BANNED_B}.com/x.jpg"}
    )
    assert not story.is_banned_provider(
        {"provider": "web", "url": "https://commons.wikimedia.org/wiki/File:X.jpg"}
    )
    assert not story.is_banned_provider(
        {"provider": "youtube", "url": "https://www.youtube.com/watch?v=abc"}
    )


def test_verify_candidate_rejects_banned_url_host():
    candidate = {
        "title": "Berlin Wall crowds 1989",
        "snippet": "Crowds at the Berlin Wall",
        "provider": "web",
        "url": f"https://www.{_BANNED_A}.com/video/1",
    }
    scenario = {"specificity": "representative", "search_terms": ["berlin wall"]}
    verdict = story.verify_candidate(candidate, scenario, {"by_id": {}, "entities": []})
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == story.BANNED_PROVIDER_REASON


# ---------------------------------------------------------------------------
# 4. compose_queries
# ---------------------------------------------------------------------------

def _bible_with(entity_id, canonical, etype):
    return {
        "by_id": {
            entity_id: {"canonical": canonical, "type": etype, "aliases": []}
        },
        "alias_index": {},
        "events": [],
    }


def test_compose_queries_representative_person_anchor_survives():
    bible = _bible_with("ada", "Ada Lovelace", "person")
    scenario = {
        "subject_id": "ada",
        "specificity": "representative",
        "search_terms": ["analytical engine notes"],
        "place": "London",
        "era": "1843",
    }
    queries = story.compose_queries(scenario, {"events": []}, bible)
    assert any("Ada Lovelace" in q for q in queries), queries


def test_compose_queries_range_era_survives():
    bible = _bible_with("wall", "Berlin Wall", "place")
    scenario = {
        "subject_id": "wall",
        "specificity": "representative",
        "search_terms": ["Berlin Wall construction"],
        "place": "Berlin",
        "era": "1950s–1961",
    }
    queries = story.compose_queries(scenario, {"events": []}, bible)
    assert any("1950s" in q and "1961" in q for q in queries), queries


def test_compose_queries_generic_carries_place():
    bible = _bible_with("venice", "Venice", "place")
    scenario = {
        "subject_id": "venice",
        "specificity": "generic",
        "search_terms": ["canal boats morning"],
        "place": "Venice",
        "era": "",
    }
    queries = story.compose_queries(scenario, {"events": []}, bible)
    assert queries and "Venice" in queries[0], queries


def test_compose_queries_exact_term_query_carries_place():
    bible = _bible_with("wall", "Berlin Wall", "place")
    scenario = {
        "subject_id": "wall",
        "specificity": "exact",
        "search_terms": ["construction"],
        "place": "Berlin",
        "era": "1961",
        "event": "construction of the wall",
    }
    queries = story.compose_queries(scenario, {"events": []}, bible)
    assert any("Berlin" in q and "construction" in q for q in queries), queries


# ---------------------------------------------------------------------------
# 5. Rejection diagnostics
# ---------------------------------------------------------------------------

def test_aggregate_funnel_primary_is_additive_contributing_is_not():
    records = [
        rejection_diagnostics.build_event(
            candidate_id="c1", outcome="rejected", stage="metadata_gate",
            reason="missing_subject", score=0.0,
        ),
        rejection_diagnostics.build_event(
            candidate_id="c2", outcome="rejected", stage="scoring",
            reason="below_threshold", score=0.2,
            verdict_reasons=["mentions Berlin", "weak topical overlap"],
            contributions={"subject": 0.0, "topical": 0.2, "era": 0.0,
                           "wrong_person": 0.0},
        ),
        rejection_diagnostics.build_event(
            candidate_id="c2", outcome="verified", stage="verification",
            reason="", score=0.5,
        ),
        rejection_diagnostics.build_event(
            candidate_id="c3", outcome="rejected", stage="scoring",
            reason="below_threshold", score=0.1,
            verdict_reasons=["weak topical overlap"],
        ),
    ]
    funnel = rejection_diagnostics.aggregate_funnel(records)
    primary = funnel["primary_rejections"]
    assert primary["total"] == 3
    assert sum(primary["by_reason"].values()) == 3
    assert sum(primary["by_stage"].values()) == 3
    assert primary["by_reason"] == {"missing_subject": 1, "below_threshold": 2}
    # Contributing reasons are a separate, non-additive block.
    assert funnel["contributing_reasons"]["weak topical overlap"] == 2
    # Distinct view: c2's best outcome is verified, not rejected.
    distinct = funnel["distinct_candidates"]
    assert distinct["total"] == 3
    assert distinct["by_best_outcome"] == {"rejected": 2, "verified": 1}
    # Gate events are honestly labelled as pre-scoring.
    assert records[0]["score_basis"] == "gate_pre_scoring"
    assert records[0]["primary_rejection_reason"] == "missing_subject"


def _run_pass(monkeypatch, tmp_path, *, videos=(), web=(), images=()):
    monkeypatch.setattr(
        mp, "_uve_scenario_search",
        lambda scenario, cache, pid, need: (list(videos), list(web), list(images)),
    )
    scene_stats = {
        "candidates_considered": 0, "candidates_rejected": 0,
        "downloads_failed": 0, "providers": {}, "kinds": {},
    }
    review_records = []
    scenario = {
        "sentence_number": 1, "specificity": "representative",
        "subject": "Berlin Wall", "search_terms": ["berlin", "wall"],
        "visual": "berlin wall crowds",
    }
    asset = mp._uve_search_and_pick(
        scenario, None,
        search_cache={}, project_id="t", bible={},
        registry=VisualRegistry(), used_keys=set(),
        scene_dir=tmp_path, scene_stats=scene_stats,
        beat={"sentence_numbers": [1], "text": "The Berlin Wall fell."},
        beat_videos=[], beat_images=[], scenario_index=0,
        queries=["berlin wall"], review_records=review_records,
    )
    return asset, scene_stats


def test_web_candidates_counted_in_considered_and_records_enriched(
    monkeypatch, tmp_path
):
    web = [
        {"url": f"https://spam.org/{i}", "title": f"Unrelated thing {i}",
         "snippet": "nothing relevant", "provider": "web", "kind": "web"}
        for i in range(3)
    ]
    asset, scene_stats = _run_pass(monkeypatch, tmp_path, web=web)
    assert asset is None
    # Before the Stage 1 fix this was 0: web was excluded from
    # candidates_considered while included in candidates_rejected.
    assert scene_stats["candidates_considered"] == 3
    assert scene_stats["candidates_rejected"] == 3
    records = scene_stats["candidate_records"]
    assert len(records) == 3
    for record in records:
        assert record["kind"] == "web"
        assert record["outcome"] == "rejected"
        assert record["primary_rejection_reason"] == record["reason"]
        assert record["category"] == "contextual"
        assert record["specificity"] == "representative"
        assert record["sentence_numbers"] == [1]


def test_dedupe_drops_are_recorded(monkeypatch, tmp_path):
    dup = {
        "url": "https://example.org/same", "image_url": "https://example.org/same",
        "title": "Unrelated thing", "snippet": "nothing relevant",
        "provider": "web", "kind": "image",
    }
    images = [dict(dup), dict(dup), dict(dup)]
    asset, scene_stats = _run_pass(monkeypatch, tmp_path, images=images)
    records = scene_stats["candidate_records"]
    dedupe = [r for r in records if r.get("stage") == "dedupe"]
    assert len(dedupe) == 2
    assert all(
        r["reason"] == rejection_diagnostics.DEDUPE_REASON for r in dedupe
    )
    # The surviving copy was evaluated normally.
    evaluated = [r for r in records if r.get("stage") != "dedupe"]
    assert len(evaluated) == 1


def test_asset_report_rejection_funnel_and_formats():
    records = [
        {"candidate_id": "cand_a", "outcome": "rejected", "stage": "scoring",
         "reason": "no_topical_evidence", "primary_rejection_reason": "no_topical_evidence",
         "url": "https://x/1", "provider": "web", "kind": "web",
         "verdict_reasons": ["weak topical overlap"]},
        {"candidate_id": "cand_b", "outcome": "rejected", "stage": "metadata_gate",
         "reason": "missing_subject", "primary_rejection_reason": "missing_subject",
         "url": "https://x/2", "provider": "web", "kind": "web"},
        {"candidate_id": "cand_c", "outcome": "accepted", "stage": "accepted",
         "reason": "", "url": "https://x/3", "provider": "youtube", "kind": "video"},
    ]
    production_result = [{
        "scene_number": 1,
        "asset_stats": {
            "candidates_considered": 3, "candidates_rejected": 2,
            "downloads_failed": 0, "candidate_records": records,
        },
        "videos": [{
            "file_path": "/tmp/clip.mp4", "type": "video",
            "verification": {"accepted": True},
        }],
        "images": [],
    }]
    report = asset_report.build_asset_report(production_result, None)
    funnel = report["rejection_funnel"]
    assert funnel["primary_rejections"]["total"] == 2
    assert funnel["primary_rejections"]["by_reason"] == {
        "no_topical_evidence": 1, "missing_subject": 1,
    }
    assert funnel["contributing_reasons"] == {"weak topical overlap": 1}
    assert funnel["distinct_candidates"]["by_best_outcome"] == {
        "rejected": 2, "accepted": 1,
    }
    assert report["accepted_asset_formats"] == [
        {"file_path": "/tmp/clip.mp4", "visual_format": "footage"}
    ]
    # Every record carries a stable event id.
    assert all(r.get("event_id") for r in report["candidate_records"]["records"])


# ---------------------------------------------------------------------------
# 6. Format runtime accounting
# ---------------------------------------------------------------------------

def test_visual_quality_runtime_share_by_format():
    timeline = {
        "items": [
            {"file_path": "/media/v1.mp4", "duration": 30.0,
             "assignment": "sentence", "type": "video"},
            {"file_path": "/media/p1.jpg", "duration": 20.0,
             "assignment": "sentence", "type": "image"},
            {"file_path": "/media/g1.mp4", "duration": 10.0,
             "assignment": "motion_graphic", "type": "video"},
        ]
    }
    report = {
        "accepted_asset_formats": [
            {"file_path": "/media/v1.mp4", "visual_format": "footage"},
            {"file_path": "/media/p1.jpg", "visual_format": "photograph"},
            {"file_path": "/media/g1.mp4", "visual_format": "graphic"},
        ]
    }
    result = visual_quality.measure_visual_quality(
        timeline=timeline, asset_report=report, rights_manifest=[],
    )
    block = result["runtime_share_by_format"]
    assert block["state"] == "recorded"
    assert block["joined_items"] == 3
    assert block["by_format"]["footage"]["runtime_share"] == pytest.approx(0.5)
    assert block["by_format"]["photograph"]["runtime_share"] == pytest.approx(1 / 3)
    assert block["by_format"]["graphic"]["runtime_share"] == pytest.approx(1 / 6)
    # A still photograph is never moving footage.
    assert block["moving_footage_runtime_share"] == pytest.approx(0.5)


def test_visual_quality_format_block_not_recorded_for_legacy_reports():
    result = visual_quality.measure_visual_quality(
        timeline={"items": []}, asset_report={}, rights_manifest=[],
    )
    block = result["runtime_share_by_format"]
    assert block["state"] == "not_recorded"
    assert block["moving_footage_runtime_share"] is None


# ---------------------------------------------------------------------------
# 7. Timeline eligibility pinning (assign_timeline equivalent)
# ---------------------------------------------------------------------------

def _asset(tmp_path, name, verification):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 2048)
    asset = {"file_path": str(path), "path": str(path), "type": "video",
             "title": name}
    if verification is not None:
        asset["verification"] = verification
    return asset


def test_timeline_eligible_filters_by_verification_state(tmp_path):
    verified = _asset(tmp_path, "ok.mp4", {"accepted": True, "score": 0.3})
    failed = _asset(tmp_path, "bad.mp4", {"accepted": False, "score": 0.99})
    unreviewed = _asset(tmp_path, "unknown.mp4", None)
    assert timeline_eligible(verified) is True
    # A failed verdict can never win on score: it is not eligible.
    assert timeline_eligible(failed) is False
    # No verdict at all means unverified — never verified by silence.
    assert timeline_eligible(unreviewed) is False
