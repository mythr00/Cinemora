"""Master Directive Phase 3 — the reporting items: AM-016
(segment-keyed failed-download guard), AM-021 (per-query
diagnostics), AM-022 (discovery degraded), AM-022b (reliance over
stills, bases stated), D-B1 (the founder B-roll band, report only)
and FP-4 (beat sufficiency, measured).

Reporting honesty rules throughout: unknown is null/unknown, never
zero; a degraded fact is recorded only when its cause is proven;
an over-band run is REPORTED over — nothing here gates.
"""

import uuid
from pathlib import Path

import pytest

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import genre_packs
from services import media_pipeline as mp
from services import story_understanding as story
from services.asset_report import build_asset_report
from services.production_report import build_production_report
from services.visual_quality import measure_visual_quality


# ---------------------------------------------------------------------------
# AM-016: the failed-download guard, keyed by segment
# ---------------------------------------------------------------------------

def _fetch(candidates, downloader, failures, tmp_path):
    registry = mp.get_registry(f"p3-{uuid.uuid4()}")
    return mp._uve_fetch_first(
        candidates, downloader, str(tmp_path), 1, 1,
        registry, set(), failures=failures,
    )


def test_segment_failure_does_not_poison_other_segments(tmp_path):
    mp._FAILED_DOWNLOAD_URLS.clear()
    url = "https://media.example.org/long-video"
    calls = []

    def fail_segment_a(candidate, scene_dir, sentence_number, asset_index):
        calls.append("a")
        candidate["_attempted_segment_id"] = "seg_aaa"
        candidate["_download_error"] = "download_failed"
        return None

    failures = []
    asset = _fetch([{"url": url, "provider": "youtube"}],
                   fail_segment_a, failures, tmp_path)
    assert asset is None
    # The failure is recorded under the SEGMENT key (with the
    # segment named on the failure record) — the source key is
    # NOT poisoned.
    assert "seg:seg_aaa" in mp._FAILED_DOWNLOAD_URLS
    assert mp.canonical_source_key(url) not in mp._FAILED_DOWNLOAD_URLS
    assert failures[-1]["segment_id"] == "seg_aaa"

    def succeed_segment_b(candidate, scene_dir, sentence_number,
                          asset_index):
        calls.append("b")
        candidate["_attempted_segment_id"] = "seg_bbb"
        path = Path(scene_dir) / "clip.mp4"
        path.write_bytes(b"fake")
        asset = dict(candidate)
        asset.update({
            "file_path": str(path), "path": str(path),
            "type": "video", "visual_type": "video",
            "segment_id": "seg_bbb",
        })
        return asset

    # A different segment of the SAME source is attempted, not
    # skipped: one dead window no longer poisons the source.
    asset = _fetch([{"url": url, "provider": "youtube"}],
                   succeed_segment_b, failures, tmp_path)
    assert calls == ["a", "b"]
    assert asset is not None
    mp._FAILED_DOWNLOAD_URLS.clear()


def test_failed_segment_candidate_is_skipped_without_download(tmp_path):
    mp._FAILED_DOWNLOAD_URLS.clear()
    mp._FAILED_DOWNLOAD_URLS.add("seg:seg_aaa")
    calls = []

    def downloader(candidate, scene_dir, sentence_number, asset_index):
        calls.append(candidate["url"])
        return None

    failures = []
    candidate = {
        "url": "https://media.example.org/long-video",
        "provider": "youtube",
        "segment_id": "seg_aaa",
    }
    asset = _fetch([candidate], downloader, failures, tmp_path)
    assert asset is None
    assert calls == []  # never re-requested
    assert failures[-1]["reason"] == "previously_failed_this_run"
    mp._FAILED_DOWNLOAD_URLS.clear()


def test_source_key_fallback_intact_without_segment(tmp_path):
    """An attempt with no segment identity (the historical shape)
    still records — and honours — the canonical source key."""
    mp._FAILED_DOWNLOAD_URLS.clear()
    calls = []

    def failing(candidate, scene_dir, sentence_number, asset_index):
        calls.append(candidate["url"])
        candidate["_download_error"] = "download_failed"
        return None

    failures = []
    first = {"url": "https://www.youtube.com/watch?v=abc123",
             "provider": "youtube"}
    _fetch([first], failing, failures, tmp_path)
    assert mp.canonical_source_key(first["url"]) in mp._FAILED_DOWNLOAD_URLS
    # The same source, respelled, is skipped without a download.
    respelled = {"url": "https://youtu.be/abc123", "provider": "youtube"}
    _fetch([respelled], failing, failures, tmp_path)
    assert calls == ["https://www.youtube.com/watch?v=abc123"]
    assert failures[-1]["reason"] == "previously_failed_this_run"
    mp._FAILED_DOWNLOAD_URLS.clear()


def test_real_downloader_short_circuits_failed_segment(
        monkeypatch, tmp_path):
    """In _uve_download_video_candidate itself: a segment already
    failed this run is not re-downloaded, while a DIFFERENT
    segment of the same source proceeds to the downloader."""
    from services import segment_index

    url = "https://media.example.org/long-video"
    monkeypatch.setattr(
        mp, "_segment_start_detailed",
        lambda u, **kw: (0.0, "whole_asset"),
    )
    seg_id = segment_index.segment_id_for(
        mp.canonical_source_key(url), 0.0, mp.DEFAULT_CLIP_DURATION)
    mp._FAILED_DOWNLOAD_URLS.clear()
    mp._FAILED_DOWNLOAD_URLS.add(f"seg:{seg_id}")
    downloads = []
    monkeypatch.setattr(
        mp, "download_video_clip",
        lambda *a, **kw: downloads.append(a) or False,
    )
    candidate = {"url": url, "provider": "youtube",
                 "matched_query": "q"}
    result = mp._uve_download_video_candidate(
        candidate, tmp_path, 1, 1)
    assert result is None
    assert candidate["_download_error"] == "previously_failed_this_run"
    assert candidate["_segment_previously_failed"] is True
    assert candidate["_attempted_segment_id"] == seg_id
    assert downloads == []  # the downloader was never invoked

    # A different segment (start 120s) is a different guard entry:
    # the download IS attempted (and fails on its own merits).
    monkeypatch.setattr(
        mp, "_segment_start_detailed",
        lambda u, **kw: (120.0, "whole_asset"),
    )
    candidate2 = {"url": url, "provider": "youtube",
                  "matched_query": "q"}
    result = mp._uve_download_video_candidate(
        candidate2, tmp_path, 1, 1)
    assert result is None
    assert candidate2["_download_error"] == "download_failed"
    assert len(downloads) == 1
    mp._FAILED_DOWNLOAD_URLS.clear()


# ---------------------------------------------------------------------------
# AM-021: query provenance at build_shot_needs + diagnostics end-to-end
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


def test_build_shot_needs_consumes_intent_queries_with_sources():
    sentence = {
        "text": "For months, investigators studied his phones.",
        "role": "action",
        "visual_intents": {
            "primary_visual_intent": "human_activity",
            "intent_queries": {
                "activity_broll": [
                    "investigators reviewing records",
                    # normalised duplicate of the composed query —
                    # must NOT be added twice.
                    "Analysts  reviewing phone records",
                ],
            },
        },
        "scenarios": [{
            "visual": "analysts reviewing phone records",
            "kind": "person_activity", "specificity": "representative",
            "subject_id": None, "search_terms": ["phone records"],
            "queries": ["analysts reviewing phone records"],
            # normalize_plan stamps this on the queries it composes.
            "queries_source": "composed",
        }],
    }
    beat = {
        "beat_id": "beat_x", "searchable": True,
        "sentences": [sentence], "sentence_numbers": [9],
    }
    needs = mp.build_shot_needs(beat, _bible())
    assert len(needs) == 1
    need = needs[0]
    assert need["queries"] == [
        "analysts reviewing phone records",
        "investigators reviewing records",
    ]
    assert need["query_sources"] == [
        {"query": "analysts reviewing phone records",
         "source": "composed"},
        {"query": "investigators reviewing records",
         "source": "intent_queries"},
    ]


def test_build_shot_needs_unstamped_queries_are_scenario_sourced():
    sentence = {
        "text": "The beach at dawn.",
        "role": "establishing",
        "visual_intents": {
            "primary_visual_intent": "establishing",
            "intent_queries": {
                "establishing": ["gilgo beach aerial view"],
            },
        },
        "scenarios": [{
            "visual": "gilgo beach shoreline",
            "kind": "place", "specificity": "representative",
            "subject_id": None, "search_terms": ["gilgo beach"],
            "queries": ["gilgo beach shoreline"],
            # No queries_source stamp: authored, not composed.
        }],
    }
    beat = {
        "beat_id": "beat_y", "searchable": True,
        "sentences": [sentence], "sentence_numbers": [1],
    }
    need = mp.build_shot_needs(beat, _bible())[0]
    assert need["query_sources"] == [
        {"query": "gilgo beach shoreline", "source": "scenario"},
        {"query": "gilgo beach aerial view", "source": "intent_queries"},
    ]


def test_query_diagnostics_end_to_end(monkeypatch, tmp_path):
    """The REAL _uve_scenario_search (provider functions stubbed)
    -> _uve_search_and_pick -> scene stats -> asset report: every
    issued query is recorded with its source and its
    hits / candidates / verified counts."""
    def videos(query, limit=8):
        if "shoreline" in query:
            return [{
                "url": "https://media.example.org/cooking",
                "title": "Cooking show highlights",
                "snippet": "recipes and kitchen fun",
                "provider": "youtube", "matched_query": query,
            }]
        return [{
            "url": "https://media.example.org/discovery",
            "title": "Gilgo Beach discovery",
            "snippet": "gilgo beach",
            "provider": "youtube", "matched_query": query,
        }]

    monkeypatch.setattr(mp, "search_videos_multi", videos)
    monkeypatch.setattr(mp, "search_web_sources", lambda q, limit=5: [])
    monkeypatch.setattr(mp, "search_image_sources", lambda q, limit=5: [])
    monkeypatch.setattr(mp, "search_wikimedia_images", lambda q, limit=3: [])
    monkeypatch.setattr(mp, "search_archive_org_videos", lambda q, limit=3: [])
    monkeypatch.setattr(mp, "search_wikimedia_videos", lambda q, limit=5: [])

    def download(candidate, scene_dir, sentence_number, asset_index):
        path = Path(scene_dir) / "asset.mp4"
        path.write_bytes(b"fake")
        asset = dict(candidate)
        asset.update({
            "file_path": str(path), "path": str(path),
            "type": "video", "visual_type": "video",
            "segment_id": "seg_1", "duration_seconds": 5.0,
        })
        return asset

    monkeypatch.setattr(mp, "_uve_download_any_candidate", download)

    queries = ["gilgo beach discovery", "police searching shoreline"]
    scenario = {
        "sentence_number": 1, "specificity": "representative",
        "kind": "event", "subject_id": None,
        "search_terms": ["gilgo", "beach"], "visual": "gilgo beach",
        "queries": list(queries),
    }
    need = {
        "beat_id": "b1", "shot_index": 1,
        "shot_type": "event", "specificity": "representative",
        "visual_category": "contextual", "description": "",
        "required_subject_id": None, "required_subject": "",
        "required_type": "event", "era": "", "place": "",
        "event": "", "visual_requirement": "", "forbidden": [],
        "priority": 2, "tier": "direct",
        "fallback_concepts": {}, "concepts_source": "test",
        "visual_intents": None,
        "queries": list(queries),
        "query_sources": [
            {"query": queries[0], "source": "composed"},
            {"query": queries[1], "source": "intent_queries"},
        ],
    }
    scene_stats = {
        "candidates_considered": 0, "candidates_rejected": 0,
        "downloads_failed": 0, "providers": {}, "kinds": {},
    }
    beat_videos, beat_images = [], []
    from services.visual_registry import VisualRegistry
    asset = mp._uve_search_and_pick(
        scenario, need,
        search_cache={}, project_id="t", bible={},
        registry=VisualRegistry(), used_keys=set(),
        scene_dir=tmp_path, scene_stats=scene_stats,
        beat={"beat_id": "b1", "sentence_numbers": [1], "text": "x"},
        beat_videos=beat_videos, beat_images=beat_images,
        scenario_index=1, queries=queries, review_records=[],
    )
    assert asset is not None
    diagnostics = scene_stats["query_diagnostics"]
    assert len(diagnostics) == 1
    entry = diagnostics[0]
    assert entry["pass"] == "primary"
    assert entry["queries"] == [
        {"query": queries[0], "source": "composed", "hits": 1,
         "candidates": 1, "verified": 1},
        {"query": queries[1], "source": "intent_queries", "hits": 1,
         "candidates": 1, "verified": 0},
    ]

    report = build_asset_report(
        [{
            "scene_number": 1, "asset_stats": scene_stats,
            "videos": beat_videos, "images": beat_images,
            "sentences": [],
        }],
        {"scenes": [{"items": []}]},
        renderer_audit={"placeholder_count": 0},
    )
    assert report["query_diagnostics"][0]["scene"] == 1
    assert report["query_diagnostics"][0]["queries"] == entry["queries"]


# ---------------------------------------------------------------------------
# AM-022: discovery degraded — exactly when search is unconfigured
# ---------------------------------------------------------------------------

_EXEC_ZERO = {"search_calls": {"searxng": {"total": 0, "by_purpose": {}, "failures": 0}}}
_EXEC_FIVE = {"search_calls": {"searxng": {"total": 5, "by_purpose": {}, "failures": 0}}}


def test_discovery_degraded_exactly_when_search_unconfigured(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    report = build_asset_report([], execution=_EXEC_ZERO)
    discovery = report["discovery"]
    assert discovery["state"] == "degraded_search_unconfigured"
    assert discovery["degraded"] is True
    assert discovery["reason"] == "search_not_configured"
    assert discovery["fact"] == "discovery degraded: search not configured"
    assert discovery["search_calls"] == 0
    assert discovery["search_configured"] is False

    # Zero calls WITH search configured: a different fact — the run
    # simply never needed search. NOT degraded.
    monkeypatch.setenv("SEARXNG_URL", "http://searxng.test")
    report = build_asset_report([], execution=_EXEC_ZERO)
    discovery = report["discovery"]
    assert discovery["state"] == "recorded"
    assert discovery["degraded"] is False
    assert discovery["reason"] == "no_search_calls"
    assert discovery["fact"] is None

    # Calls happened: not degraded, whatever the configuration.
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    report = build_asset_report([], execution=_EXEC_FIVE)
    assert report["discovery"]["degraded"] is False
    assert report["discovery"]["search_calls"] == 5

    # No execution record at all: unknown, never a fabricated fact.
    report = build_asset_report([])
    assert report["discovery"]["state"] == "unknown"
    assert report["discovery"]["degraded"] is None
    assert report["discovery"]["fact"] is None


def test_production_report_q28_carries_the_discovery_fact(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    asset_report = build_asset_report([], execution=_EXEC_ZERO)
    report = build_production_report(
        measurement={}, asset_report=asset_report,
        timeline={}, rights_manifest=[],
    )
    q28 = report["questions"]["q28"]
    assert q28["state"] == "answered"
    assert q28["answer"]["state"] == "degraded_search_unconfigured"
    assert q28["answer"]["fact"] == "discovery degraded: search not configured"

    bare = build_production_report(
        measurement={}, asset_report={}, timeline={},
        rights_manifest=[],
    )
    assert bare["questions"]["q28"]["state"] == "unknown"


# ---------------------------------------------------------------------------
# AM-022b: reliance over stills, bases stated
# ---------------------------------------------------------------------------

def _scene_with_assets(videos, images):
    return {
        "asset_stats": {
            "candidates_considered": 0, "candidates_rejected": 0,
            "downloads_failed": 0, "providers": {},
        },
        "videos": videos, "images": images, "sentences": [],
    }


def _asset(provider, kind, i):
    return {
        "file_path": f"/p/{provider}-{kind}-{i}.mp4",
        "type": kind, "verification_status": "verified",
        "universal_score": 0.9, "license": "unknown",
        "source": provider, "provider": provider,
    }


def test_reliance_covers_stills_with_stated_base():
    # A stills-only run, all from one provider: the warning fires
    # on the STILLS base, and every figure states its n.
    scene = _scene_with_assets(
        [], [_asset("wikimedia", "image", i) for i in range(4)])
    report = build_asset_report(
        [scene], {"scenes": [{"items": []}]},
        renderer_audit={"placeholder_count": 0})
    reliance = report["providers"]["single_provider_reliance"]
    assert reliance["warning"] is True
    assert reliance["provider"] == "wikimedia"
    assert reliance["stills_share"] == 1.0
    assert reliance["stills_base"] == 4
    assert reliance["stills_provider"] == "wikimedia"
    assert reliance["footage_share"] is None
    assert reliance["footage_base"] == 0
    assert report["providers"]["accepted_stills_by_provider"] == {
        "wikimedia": 4,
    }


def test_reliance_footage_semantics_unchanged_with_bases():
    scene = _scene_with_assets(
        [_asset("youtube", "video", i) for i in range(5)],
        [_asset("wikimedia", "image", 0), _asset("web", "image", 1)],
    )
    report = build_asset_report(
        [scene], {"scenes": [{"items": []}]},
        renderer_audit={"placeholder_count": 0})
    reliance = report["providers"]["single_provider_reliance"]
    assert reliance["warning"] is True
    assert reliance["provider"] == "youtube"
    assert reliance["footage_share"] == 1.0
    assert reliance["footage_base"] == 5
    # Stills are split across two providers: no stills warning.
    assert reliance["stills_share"] == 0.5
    assert reliance["stills_base"] == 2


# ---------------------------------------------------------------------------
# D-B1: the founder band as pack data + report-only comparison
# ---------------------------------------------------------------------------

def test_packs_carry_the_founder_broll_band():
    assert genre_packs.broll_share_band(
        genre_packs.load_pack("true_crime")) == {
            "max": 0.40, "basis": "founder_band"}
    assert genre_packs.broll_share_band(
        genre_packs.load_pack("history"))["max"] == 0.40
    assert genre_packs.broll_share_band(
        genre_packs.load_pack("general"))["max"] == 0.50
    assert genre_packs.broll_share_band(None) is None
    assert genre_packs.broll_share_band({}) is None


def _stamped_timeline(direct_s, broll_s):
    items = [
        {"assignment": "sentence", "scene_number": 1,
         "sentence_index": 0, "file_path": "/x/a.mp4",
         "duration": float(direct_s), "tier": "direct"},
    ]
    if broll_s:
        items.append(
            {"assignment": "sentence", "scene_number": 1,
             "sentence_index": 1, "file_path": "/x/b.mp4",
             "duration": float(broll_s), "tier": "activity_broll"})
    return {
        "items": items,
        "duration_seconds": float(direct_s + broll_s),
        "sentence_count": len(items),
    }


def test_asset_report_band_comparison_reports_over_not_blocked():
    # B-roll share 10/20 = 0.5 against the true_crime band (0.40):
    # the report says over_band — and the report builds normally;
    # nothing is blocked, failed or gated by it.
    timeline = _stamped_timeline(10, 10)
    report = build_asset_report(
        [], timeline_result=timeline,
        genre_pack={"id": "true_crime", "source": "test"})
    band = report["tier_breakdown"]["band"]
    assert band["state"] == "recorded"
    assert band["max"] == 0.40
    assert band["basis"] == "founder_band"
    assert band["evaluation"] == "over_band"
    assert report["run_completed"] is True

    # The same share is within the general band (0.50, inclusive).
    report = build_asset_report(
        [], timeline_result=timeline,
        genre_pack={"id": "general", "source": "test"})
    assert report["tier_breakdown"]["band"]["evaluation"] == "within_band"

    # No pack named: the band is unknown, never a fabricated pass.
    report = build_asset_report([], timeline_result=timeline)
    assert report["tier_breakdown"]["band"]["state"] == "unknown"
    assert report["tier_breakdown"]["band"]["evaluation"] == "unknown"


def test_visual_quality_band_block():
    timeline = _stamped_timeline(10, 10)  # share 0.5
    asset_report = build_asset_report(
        [], timeline_result=timeline,
        genre_pack={"id": "true_crime", "source": "test"})
    measurement = measure_visual_quality(
        timeline=timeline, asset_report=asset_report,
        rights_manifest=[])
    band = measurement["tier_accounting"]["band"]
    assert band["evaluation"] == "over_band"
    assert band["measured"] == pytest.approx(0.5)
    assert band["max"] == 0.40

    timeline = _stamped_timeline(14, 6)  # share 0.3
    asset_report = build_asset_report(
        [], timeline_result=timeline,
        genre_pack={"id": "true_crime", "source": "test"})
    measurement = measure_visual_quality(
        timeline=timeline, asset_report=asset_report,
        rights_manifest=[])
    assert measurement["tier_accounting"]["band"]["evaluation"] == (
        "within_band")

    # Unstamped timeline: the share is unknown and so is the
    # evaluation — never zero, never a pass.
    legacy = {"items": [{
        "assignment": "sentence", "scene_number": 1,
        "sentence_index": 0, "file_path": "/x/a.mp4",
        "duration": 10.0,
    }]}
    measurement = measure_visual_quality(
        timeline=legacy, asset_report=asset_report, rights_manifest=[])
    band = measurement["tier_accounting"]["band"]
    assert band["state"] == "not_recorded"
    assert band["evaluation"] == "unknown"


# ---------------------------------------------------------------------------
# FP-4: beat sufficiency — measured in the pipeline, aggregated in
# the asset report
# ---------------------------------------------------------------------------

def test_beat_sufficiency_recorded_and_aggregated(
        monkeypatch, tmp_path):
    sentences = [
        {"sentence_number": n,
         "text": f"Berlin Wall crowds scene {n}."}
        for n in (1, 2, 3)
    ]
    scenario = {
        "sentence_number": 1, "specificity": "representative",
        "subject": "Berlin Wall", "search_terms": ["berlin", "wall"],
        "visual": "berlin wall crowds",
        "queries": ["berlin wall crowds"],
    }
    need = {
        "beat_id": "beat_fp4", "shot_index": 1,
        "shot_type": "event", "specificity": "representative",
        "visual_category": "contextual", "description": "",
        "required_subject_id": None, "required_subject": "",
        "required_type": "place", "era": "", "place": "",
        "event": "", "visual_requirement": "", "forbidden": [],
        "priority": 2, "tier": "direct",
        "fallback_concepts": {}, "concepts_source": "test",
        "visual_intents": None, "queries": ["berlin wall crowds"],
    }
    beat = {
        "beat_id": "beat_fp4", "subject_id": None, "place": None,
        "searchable": True, "role": "event",
        "sentence_numbers": [1, 2, 3], "sentences": sentences,
        "text": " ".join(s["text"] for s in sentences),
        "shot_needs": [need],
    }
    good = {
        "url": "https://media.example.org/berlin-0",
        "title": "Berlin Wall crowds 0",
        "snippet": "berlin wall crowds",
        "provider": "youtube",
    }

    def fake_search(scenario_, cache, pid, shot_need=None):
        return ([dict(good)], [], [])

    def download(candidate, scene_dir, sentence_number, asset_index):
        path = Path(scene_dir) / "asset.mp4"
        path.write_bytes(b"fake")
        asset = dict(candidate)
        asset.update({
            "file_path": str(path), "path": str(path),
            "type": "video", "visual_type": "video",
            "segment_id": "seg_1", "duration_seconds": 5.0,
        })
        return asset

    monkeypatch.setattr(mp, "build_story_beats", lambda s, b: [beat])
    monkeypatch.setattr(mp, "_load_case_bible", lambda s, p: {})
    monkeypatch.setattr(mp, "_beat_scenarios", lambda b: [scenario])
    monkeypatch.setattr(mp, "_uve_scenario_search", fake_search)
    monkeypatch.setattr(mp, "_uve_download_any_candidate", download)

    scene = {
        "scene_number": 1,
        "project_id": f"p3-fp4-{uuid.uuid4()}",
        "sentences": sentences,
    }
    result = mp._uve_process_scene_media(scene, str(tmp_path))
    sufficiency = result["asset_stats"]["beat_sufficiency"]
    assert sufficiency == [{
        "beat_id": "beat_fp4",
        "sentence_count": 3,
        "distinct_assets": 1,
        "sufficiency_ratio": pytest.approx(1 / 3, abs=1e-4),
        "meets_half": False,
        "searchable": True,
    }]

    report = build_asset_report(
        [{
            "scene_number": 1, "asset_stats": result["asset_stats"],
            "videos": result["videos"], "images": result["images"],
            "sentences": [],
        }],
        {"scenes": [{"items": []}]},
        renderer_audit={"placeholder_count": 0},
    )
    assert report["beat_sufficiency"][0]["scene"] == 1
    assert report["beat_sufficiency"][0]["distinct_assets"] == 1
    assert report["beat_sufficiency_summary"] == {
        "beats": 1, "searchable_beats": 1, "below_half": 1,
    }
