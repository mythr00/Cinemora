"""Master Directive Phase 3 — intent-aware ranking, the AM-004
video preference, and the eligibility/ranking separation proof.

Architecture rule under test (Phase 1): ranking orders the
ALREADY-VERIFIED pool only. The intent signals are bounded
(INTENT_FIT_MAX / MISREPRESENTATION_DEMOTION / VIDEO_PREFERENCE_
EPSILON), recorded on the pick, and can never admit, rescue or
re-judge a candidate — the ineligible-never-picked test below gives
the rejected candidate every ranking advantage and it still loses.

Pipeline tests run the real _uve_search_and_pick (verifier, gate,
pick) with only the provider search and the downloader stubbed,
modelled on tests/test_phase2_ladder.py.
"""

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from pathlib import Path

from services import media_pipeline as mp
from services import story_understanding as story
from services.asset_report import build_asset_report
from services.visual_registry import VisualRegistry


# ---------------------------------------------------------------------------
# Harness (modelled on test_phase2_ladder._run_pass)
# ---------------------------------------------------------------------------

def _bible():
    return story.hydrate_bible({
        "entities": [
            {"id": "rex_heuermann", "canonical": "Rex Heuermann",
             "type": "person", "aliases": ["Heuermann"]},
            {"id": "gilgo_beach", "canonical": "Gilgo Beach",
             "type": "place", "aliases": ["Gilgo"]},
        ],
        "primary_subject_id": "rex_heuermann",
        "title": "Gilgo Beach",
        "story_type": "true_crime",
        "time_period": "2010-2023",
    })


def _downloader(fail_markers=()):
    attempts = []

    def download(candidate, scene_dir, sentence_number, asset_index):
        url = str(
            candidate.get("url") or candidate.get("image_url") or ""
        )
        attempts.append(url)
        if any(m in url for m in fail_markers):
            candidate["_download_error"] = "download_failed"
            return None
        path = Path(scene_dir) / f"asset_{len(attempts)}.mp4"
        path.write_bytes(b"fake")
        asset = dict(candidate)
        asset.update({
            "file_path": str(path), "path": str(path),
            "type": "video", "visual_type": "video",
            "segment_id": f"seg_{len(attempts)}",
            "duration_seconds": 5.0,
        })
        return asset

    return download, attempts


def _run_pass(monkeypatch, tmp_path, *, pools, beat, shot_need,
              scenario, bible=None, fail_markers=()):
    def _search(scenario_, cache, pid, need):
        return pools

    monkeypatch.setattr(mp, "_uve_scenario_search", _search)
    downloader, attempts = _downloader(fail_markers)
    monkeypatch.setattr(mp, "_uve_download_any_candidate", downloader)
    scene_stats = {
        "candidates_considered": 0, "candidates_rejected": 0,
        "downloads_failed": 0, "providers": {}, "kinds": {},
    }
    beat_videos, beat_images = [], []
    asset = mp._uve_search_and_pick(
        scenario, shot_need,
        search_cache={}, project_id="t", bible=bible or {},
        registry=VisualRegistry(), used_keys=set(),
        scene_dir=tmp_path, scene_stats=scene_stats,
        beat=beat, beat_videos=beat_videos, beat_images=beat_images,
        scenario_index=1, queries=(shot_need or {}).get("queries") or [],
        review_records=[],
    )
    return asset, scene_stats, beat_videos, beat_images, attempts


def _need(**overrides):
    need = {
        "beat_id": "b1", "shot_index": 1,
        "shot_type": "person_activity", "specificity": "representative",
        "visual_category": "contextual", "description": "",
        "required_subject_id": None, "required_subject": "",
        "required_type": "person", "era": "", "place": "",
        "event": "", "visual_requirement": "", "forbidden": [],
        "priority": 2, "tier": "direct",
        "fallback_concepts": {}, "concepts_source": "test",
        "visual_intents": None,
        "queries": ["records review"],
    }
    need.update(overrides)
    return need


def _intents(primary, **overrides):
    intents = {
        "primary_visual_intent": primary,
        "secondary_visual_intents": [],
        "preferred_media_type": "any",
        "specificity_requirement": "representative",
        "visual_misrepresentation_risk": "low",
        "intent_queries": {},
    }
    intents.update(overrides)
    return intents


# ---------------------------------------------------------------------------
# _intent_rank_adjustment — the bounded signals, unit-pinned
# ---------------------------------------------------------------------------

def _verdict(accepted=True, subject=0.0):
    return {
        "accepted": accepted,
        "contributions": {
            "subject": subject, "topical": 0.45, "era": 0.0,
            "wrong_person": 0.0,
        },
    }


def test_intent_fit_video_and_image_bounded():
    need = {"visual_intents": _intents("activity_broll")}
    adj, reasons = mp._intent_rank_adjustment(
        {"verification": _verdict()}, "video", need, {}, 0.0)
    assert adj == mp.INTENT_FIT_MAX == 0.05
    assert reasons == ["intent_fit:video:activity_broll"]
    # The same intent gives an image nothing.
    adj, reasons = mp._intent_rank_adjustment(
        {"verification": _verdict()}, "image", need, {}, 0.0)
    assert adj == 0.0 and reasons == []

    need = {"visual_intents": _intents("object_detail")}
    adj, reasons = mp._intent_rank_adjustment(
        {"verification": _verdict()}, "image", need, {}, 0.0)
    assert adj == mp.INTENT_FIT_MAX
    assert reasons == ["intent_fit:image:object_detail"]


def test_intent_fit_never_double_counts_format_bonus():
    """When the scenario's format intent already rewarded this kind
    (format_bonus > 0), the intent fit must not pay the same signal
    twice."""
    need = {"visual_intents": _intents("activity_broll")}
    adj, reasons = mp._intent_rank_adjustment(
        {"verification": _verdict()}, "video", need,
        {"format_intent": "footage"}, 0.10)
    assert adj == 0.0 and reasons == []


def test_intent_fit_absent_without_intents():
    adj, reasons = mp._intent_rank_adjustment(
        {"verification": _verdict()}, "video", {}, {}, 0.0)
    assert adj == 0.0 and reasons == []
    adj, reasons = mp._intent_rank_adjustment(
        {"verification": _verdict()}, "video",
        {"visual_intents": None}, {}, 0.0)
    assert adj == 0.0 and reasons == []


def test_misrepresentation_demotion_conditions():
    # Primary documentary_context carries no medium fit, so these
    # assertions isolate the demotion alone. (With a fitting
    # primary the two bounded signals compose: fit +0.05 and
    # demotion -0.05 on the same candidate net to 0.0, both
    # recorded in the reasons.)
    exact_high = {"visual_intents": _intents(
        "documentary_context",
        specificity_requirement="exact",
        visual_misrepresentation_risk="high",
    )}
    # Accepted, no subject evidence -> demoted by the bound.
    adj, reasons = mp._intent_rank_adjustment(
        {"verification": _verdict(subject=0.0)}, "video",
        exact_high, {}, 0.0)
    assert adj == -mp.MISREPRESENTATION_DEMOTION == -0.05
    assert reasons == ["misrepresentation_demotion:no_subject_evidence"]
    # Subject evidence present -> no demotion.
    adj, reasons = mp._intent_rank_adjustment(
        {"verification": _verdict(subject=0.45)}, "video",
        exact_high, {}, 0.0)
    assert adj == 0.0 and reasons == []
    # Not exact -> no demotion.
    rep_high = {"visual_intents": _intents(
        "documentary_context",
        specificity_requirement="representative",
        visual_misrepresentation_risk="high",
    )}
    adj, _ = mp._intent_rank_adjustment(
        {"verification": _verdict(subject=0.0)}, "video",
        rep_high, {}, 0.0)
    assert adj == 0.0
    # Exact but low risk -> no demotion.
    exact_low = {"visual_intents": _intents(
        "documentary_context",
        specificity_requirement="exact",
        visual_misrepresentation_risk="low",
    )}
    adj, _ = mp._intent_rank_adjustment(
        {"verification": _verdict(subject=0.0)}, "video",
        exact_low, {}, 0.0)
    assert adj == 0.0
    # A REJECTED verdict is never adjusted (it is not in the pool
    # that gets picked; the demotion must not masquerade as a
    # second verdict on it either).
    adj, reasons = mp._intent_rank_adjustment(
        {"verification": _verdict(accepted=False, subject=0.0)},
        "video", exact_high, {}, 0.0)
    assert adj == 0.0 and reasons == []
    # Composition: a fitting primary on the same candidate pays the
    # fit AND charges the demotion — both bounded, both recorded.
    both = {"visual_intents": _intents(
        "actual_event",
        specificity_requirement="exact",
        visual_misrepresentation_risk="high",
    )}
    adj, reasons = mp._intent_rank_adjustment(
        {"verification": _verdict(subject=0.0)}, "video", both, {}, 0.0)
    assert adj == 0.0
    assert reasons == [
        "intent_fit:video:actual_event",
        "misrepresentation_demotion:no_subject_evidence",
    ]


# ---------------------------------------------------------------------------
# Pipeline: ranking orders by intent fit within the verified pool
# ---------------------------------------------------------------------------

_FIT_SCENARIO = {
    "sentence_number": 1, "specificity": "representative",
    "kind": "person_activity", "subject_id": None,
    "search_terms": ["records"], "visual": "records review",
    "format_intent": "any",
}

_FIT_VIDEO = {
    "url": "https://media.example.org/clip",
    "title": "Records review",
    "snippet": "records review",
    "provider": "youtube",
    "matched_query": "records review",
}

_FIT_IMAGE = {
    "url": "https://media.example.org/still.jpg",
    "image_url": "https://media.example.org/still.jpg",
    "title": "Records review",
    "snippet": "records review",
    "provider": "web",
    "type": "image",
    "matched_query": "records review",
}


def test_pipeline_intent_fit_orders_verified_pool(monkeypatch, tmp_path):
    """Equal universal scores: the primary intent's medium wins.
    activity_broll -> the video is fetched first; object_detail ->
    the still is fetched first. The fit follows the intent, not
    the pool's assembly order."""
    beat = {"beat_id": "b1", "sentence_numbers": [1], "text": "x"}
    pools = ([dict(_FIT_VIDEO)], [], [dict(_FIT_IMAGE)])

    need = _need(visual_intents=_intents(
        "activity_broll", preferred_media_type="video"))
    _asset, _stats, _v, _i, attempts = _run_pass(
        monkeypatch, tmp_path, pools=pools, beat=beat,
        shot_need=need, scenario=dict(_FIT_SCENARIO))
    assert attempts[0] == _FIT_VIDEO["url"]

    need = _need(visual_intents=_intents("object_detail"))
    _asset, _stats, _v, _i, attempts = _run_pass(
        monkeypatch, tmp_path, pools=pools, beat=beat,
        shot_need=need, scenario=dict(_FIT_SCENARIO))
    assert attempts[0] == _FIT_IMAGE["url"]


def test_pipeline_intent_fit_recorded_on_accepted_event(
        monkeypatch, tmp_path):
    beat = {"beat_id": "b1", "sentence_numbers": [1], "text": "x"}
    pools = ([dict(_FIT_VIDEO)], [], [dict(_FIT_IMAGE)])
    need = _need(visual_intents=_intents(
        "activity_broll", preferred_media_type="video"))
    _asset, scene_stats, _v, _i, _a = _run_pass(
        monkeypatch, tmp_path, pools=pools, beat=beat,
        shot_need=need, scenario=dict(_FIT_SCENARIO))
    accepted = [
        r for r in scene_stats["candidate_records"]
        if r.get("outcome") == "accepted"
    ]
    assert len(accepted) == 1
    assert accepted[0]["intent_adjustment"] == mp.INTENT_FIT_MAX
    assert accepted[0]["intent_reasons"] == [
        "intent_fit:video:activity_broll"
    ]


# ---------------------------------------------------------------------------
# Pipeline: misrepresentation demotion on an exact-intent beat
# ---------------------------------------------------------------------------

_DEMOTE_TERMS = ["alpha", "bravo", "charlie", "delta", "echo",
                 "foxtrot", "golf"]

# A: full topical overlap + era year, NO subject mention.
#    score = 0.45 + 0.10 = 0.55, contributions.subject = 0.
_DEMOTE_A = {
    "url": "https://media.example.org/clip-a",
    "title": "alpha bravo charlie delta echo foxtrot golf 2010",
    "snippet": "alpha bravo charlie delta echo foxtrot golf",
    "provider": "youtube",
    "matched_query": "q",
}
# B: subject mention + one topical token.
#    score = 0.45 + 0.45 * (1/7 * 1.5) ~= 0.546, subject = 0.45.
_DEMOTE_B = {
    "url": "https://media.example.org/clip-b",
    "title": "Rex Heuermann alpha",
    "snippet": "alpha",
    "provider": "youtube",
    "matched_query": "q",
}

_DEMOTE_SCENARIO = {
    "sentence_number": 1, "specificity": "representative",
    "kind": "event", "subject_id": "rex_heuermann",
    "search_terms": list(_DEMOTE_TERMS), "visual": "alpha",
    "era": "2010-2023", "format_intent": "any",
}


def _demote_run(monkeypatch, tmp_path, risk, fail_markers=()):
    beat = {"beat_id": "b1", "sentence_numbers": [1], "text": "x"}
    need = _need(visual_intents=_intents(
        "documentary_context",
        specificity_requirement="exact",
        visual_misrepresentation_risk=risk,
    ))
    return _run_pass(
        monkeypatch, tmp_path,
        pools=([dict(_DEMOTE_A), dict(_DEMOTE_B)], [], []),
        beat=beat, shot_need=need, scenario=dict(_DEMOTE_SCENARIO),
        bible=_bible(), fail_markers=fail_markers)


def test_pipeline_misrepresentation_demotion_reorders(
        monkeypatch, tmp_path):
    """On an exact, high-risk beat the subject-less verified
    candidate (0.55) is demoted below the subject-evidenced one
    (0.546); with the risk low, raw relevance order stands. The
    demotion is the only difference between the two runs."""
    _a, _s, _v, _i, attempts = _demote_run(monkeypatch, tmp_path, "low")
    assert attempts[0] == _DEMOTE_A["url"]

    # High risk, and the promoted candidate's download fails: the
    # fetch order proves the reorder (B attempted first), and the
    # demoted candidate — ultimately the accepted one — carries
    # the demotion on its accepted record.
    _a, scene_stats, _v, _i, attempts = _demote_run(
        monkeypatch, tmp_path, "high", fail_markers=(_DEMOTE_B["url"],))
    assert attempts == [_DEMOTE_B["url"], _DEMOTE_A["url"]]
    accepted = [
        r for r in scene_stats["candidate_records"]
        if r.get("outcome") == "accepted"
    ]
    assert len(accepted) == 1
    assert accepted[0]["intent_adjustment"] == -mp.MISREPRESENTATION_DEMOTION
    assert accepted[0]["intent_reasons"] == [
        "misrepresentation_demotion:no_subject_evidence"
    ]


# ---------------------------------------------------------------------------
# Eligibility stays separate: a high-relevance INELIGIBLE candidate
# is never picked, however ranking-favoured it is
# ---------------------------------------------------------------------------

def test_ineligible_candidate_never_picked_despite_ranking_bait():
    rejected = {
        "url": "https://media.example.org/rejected",
        "title": "Perfect but rejected",
        "provider": "youtube",
        "type": "video",
        "_pool_kind": "video",
        "universal_score": 0.99,
        "_selection_score": 0.99,
        "_intent_adjustment": mp.INTENT_FIT_MAX,
        "_intent_reasons": ["intent_fit:video:activity_broll"],
        "verification": {"accepted": False,
                         "rejection_reason": "below_threshold"},
    }
    accepted = {
        "url": "https://media.example.org/accepted",
        "title": "Modest but verified",
        "provider": "youtube",
        "type": "video",
        "_pool_kind": "video",
        "universal_score": 0.40,
        "_selection_score": 0.40,
        "verification": {"accepted": True},
    }
    need = _need(visual_intents=_intents(
        "activity_broll", preferred_media_type="video"))
    picked = mp._uve_pick(
        [rejected, accepted],
        {"format_intent": "footage"},
        VisualRegistry(), set(), shot_need=need,
    )
    assert [p["url"] for p in picked] == [accepted["url"]]


# ---------------------------------------------------------------------------
# AM-004: the video preference inside _uve_pick
# ---------------------------------------------------------------------------

def _pool_item(url, kind, score):
    return {
        "url": url,
        "title": f"item {url}",
        "provider": "youtube",
        "type": "video" if kind == "video" else "image",
        "_pool_kind": kind,
        "universal_score": score,
        "_selection_score": score,
        "verification": {"accepted": True},
    }


def test_video_preference_fires_within_epsilon_and_is_recorded():
    still = _pool_item("https://media.example.org/still", "image", 0.80)
    video = _pool_item("https://media.example.org/video", "video", 0.76)
    picked = mp._uve_pick(
        [still, video], {"format_intent": "footage"},
        VisualRegistry(), set(), shot_need=_need(),
    )
    assert [p["url"] for p in picked] == [video["url"], still["url"]]
    assert video.get("video_preference_applied") is True
    # The displaced still is stamped too: the stamp records that
    # the preference changed this candidate's position, in either
    # direction — a selection decision must be explainable from
    # either side of it.
    assert still.get("video_preference_applied") is True


def test_video_preference_never_crosses_the_epsilon():
    still = _pool_item("https://media.example.org/still", "image", 0.90)
    video = _pool_item("https://media.example.org/video", "video", 0.70)
    picked = mp._uve_pick(
        [still, video], {"format_intent": "footage"},
        VisualRegistry(), set(), shot_need=_need(),
    )
    assert [p["url"] for p in picked] == [still["url"], video["url"]]
    assert "video_preference_applied" not in video
    # The bound itself stays small (register: epsilon must stay small).
    assert 0 < mp.VIDEO_PREFERENCE_EPSILON <= 0.05


def test_video_preference_only_when_the_beat_wants_video():
    still = _pool_item("https://media.example.org/still", "image", 0.80)
    video = _pool_item("https://media.example.org/video", "video", 0.76)
    need = _need(visual_intents=_intents("establishing"))
    picked = mp._uve_pick(
        [still, video], {"format_intent": "any"},
        VisualRegistry(), set(), shot_need=need,
    )
    assert [p["url"] for p in picked] == [still["url"], video["url"]]
    assert "video_preference_applied" not in video


def test_video_preference_via_preferred_media_type():
    still = _pool_item("https://media.example.org/still", "image", 0.80)
    video = _pool_item("https://media.example.org/video", "video", 0.77)
    need = _need(visual_intents=_intents(
        "documentary_context", preferred_media_type="video"))
    picked = mp._uve_pick(
        [still, video], {}, VisualRegistry(), set(), shot_need=need,
    )
    assert [p["url"] for p in picked] == [video["url"], still["url"]]
    assert video.get("video_preference_applied") is True


# ---------------------------------------------------------------------------
# AM-004 measurement: the per-kind funnel, accepted stage included
# ---------------------------------------------------------------------------

def test_per_kind_funnel_reaches_the_asset_report(
        monkeypatch, tmp_path):
    beat = {"beat_id": "b1", "sentence_numbers": [1], "text": "x"}
    pools = ([dict(_FIT_VIDEO)], [], [dict(_FIT_IMAGE)])
    need = _need(visual_intents=_intents(
        "activity_broll", preferred_media_type="video"))
    _asset, scene_stats, beat_videos, beat_images, _a = _run_pass(
        monkeypatch, tmp_path, pools=pools, beat=beat,
        shot_need=need, scenario=dict(_FIT_SCENARIO))
    kinds = scene_stats["kinds"]
    assert kinds["video"] == {
        "discovered": 1, "verified": 1, "rejected": 0, "accepted": 1,
    }
    assert kinds["image"]["discovered"] == 1
    assert kinds["image"]["verified"] == 1
    assert kinds["image"]["accepted"] == 0

    report = build_asset_report(
        [{
            "scene_number": 1, "asset_stats": scene_stats,
            "videos": beat_videos, "images": beat_images,
            "sentences": [],
        }],
        {"scenes": [{"items": []}]},
        renderer_audit={"placeholder_count": 0},
    )
    by_kind = report["format_mix"]["by_kind"]
    assert by_kind["video"]["accepted"] == 1
    assert by_kind["image"]["accepted"] == 0
