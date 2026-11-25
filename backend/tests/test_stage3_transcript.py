"""Stage 3B — transcript-interval verification (review §2.4).

verify_candidate judges metadata; this layer judges the spoken
words inside the segment that was actually chosen. The transcript
text overlapping [clip_start, clip_start + duration] must contain a
subject/topical token from the scenario, or an otherwise-accepted
candidate is demoted (never upgraded) in the pick flow. All
transcript/network access is mocked: the window helper is fed a
fake transcript through the shared _get_youtube_transcript loader,
and the pick flow is fed a fake window function.
"""

import uuid
from pathlib import Path

import pytest

from services import media_pipeline as mp
from services import provider_health
from services import story_understanding as story


@pytest.fixture(autouse=True)
def _clean_provider_state():
    # The pick flow consults provider_health.is_backed_off before
    # every fetch. Since Phase 8.4 that backoff is SHARED Redis
    # state: another test file's simulated 429s (test_run11 trips
    # youtube.com) leave this file's fetch hosts backed off
    # platform-wide for minutes, refusing every candidate before
    # any request — and provider_health.reset() deliberately does
    # not clear the shared backoff. Isolate through the Phase 8
    # test seam, as test_metadata_absence_bypass.py does: a
    # fakeredis client per test, plus the production per-run
    # reset of the failed-download set and observation table.
    import fakeredis

    provider_health.set_redis_client(
        fakeredis.FakeRedis(decode_responses=True)
    )
    mp.reset_media_caches()
    yield
    mp.reset_media_caches()
    provider_health.reset_redis_client()


def _bible():
    return story.hydrate_bible(
        {
            "entities": [
                {
                    "id": "berlin_wall",
                    "canonical": "Berlin Wall",
                    "aliases": ["the Wall"],
                    "type": "object",
                }
            ]
        }
    )


_SCENARIO = {
    "subject_id": "berlin_wall",
    "specificity": "representative",
    "search_terms": ["crowds crossing checkpoint"],
    "visual": "crowds of people crossing through an opened checkpoint",
    "era": "1989",
}

_MATCH_TEXT = (
    "Thousands of people gathered as crowds crossed through the "
    "checkpoint in Berlin that night and the celebrations continued "
    "across the whole city until morning"
)
_MISMATCH_TEXT = (
    "Today we are baking bread in the kitchen and sharing simple "
    "recipes with fresh butter and warm milk for everyone at home"
)


# ---------------------------------------------------------------------------
# the check function itself
# ---------------------------------------------------------------------------


def test_check_match_when_subject_token_in_window():
    assert (
        mp.check_transcript_interval(_MATCH_TEXT, _SCENARIO, _bible())
        == "interval_transcript_match"
    )


def test_check_mismatch_when_no_subject_or_topical_token():
    assert (
        mp.check_transcript_interval(_MISMATCH_TEXT, _SCENARIO, _bible())
        == "transcript_mismatch"
    )


def test_check_no_transcript_is_neutral():
    assert mp.check_transcript_interval(None, _SCENARIO, _bible()) == "no_transcript"
    assert mp.check_transcript_interval("", _SCENARIO, _bible()) == "no_transcript"


def test_check_tiny_window_is_neutral():
    # Two content tokens (< 5) cannot convict or clear a segment.
    assert (
        mp.check_transcript_interval("Berlin Wall", _SCENARIO, _bible())
        == "no_transcript"
    )


def test_check_without_scenario_terms_is_neutral():
    assert mp.check_transcript_interval(_MISMATCH_TEXT, {}, {}) == "no_transcript"


# ---------------------------------------------------------------------------
# the window-text helper (fake transcript through the shared loader)
# ---------------------------------------------------------------------------

_FAKE_TRANSCRIPT = {
    "events": [
        {"tStartMs": 0, "dDurationMs": 4000, "segs": [{"utf8": "Intro music and titles"}]},
        {
            "tStartMs": 100000,
            "dDurationMs": 5000,
            "segs": [{"utf8": "the Berlin Wall"}, {"utf8": " fell tonight"}],
        },
        {
            "tStartMs": 105000,
            "dDurationMs": 5000,
            "segs": [{"utf8": "crowds crossing the checkpoint"}],
        },
        {"tStartMs": 200000, "dDurationMs": 5000, "segs": [{"utf8": "later unrelated segment"}]},
    ]
}


def test_window_text_concatenates_overlapping_entries(monkeypatch):
    monkeypatch.setattr(mp, "_get_youtube_transcript", lambda url: _FAKE_TRANSCRIPT)
    text = mp._transcript_window_text("https://youtube.com/watch?v=x", 100, 8)
    assert "Berlin Wall" in text and "fell tonight" in text
    assert "crowds crossing the checkpoint" in text
    assert "Intro music" not in text and "unrelated" not in text


def test_window_text_empty_when_nothing_overlaps(monkeypatch):
    monkeypatch.setattr(mp, "_get_youtube_transcript", lambda url: _FAKE_TRANSCRIPT)
    assert mp._transcript_window_text("https://youtube.com/watch?v=x", 500, 8) == ""


def test_window_text_none_when_no_transcript(monkeypatch):
    monkeypatch.setattr(mp, "_get_youtube_transcript", lambda url: None)
    assert mp._transcript_window_text("https://youtube.com/watch?v=x", 100, 8) is None


def test_window_helper_reuses_the_shared_transcript_cache():
    url = "https://youtube.com/watch?v=stage3b-cache-test"
    mp._youtube_transcript_cache[url] = _FAKE_TRANSCRIPT
    try:
        text = mp._transcript_window_text(url, 100, 8)
    finally:
        mp._youtube_transcript_cache.pop(url, None)
    assert text is not None and "Berlin Wall" in text


# ---------------------------------------------------------------------------
# pick flow: demotion inside _uve_fetch_first
# ---------------------------------------------------------------------------


def _asset(tmp_path, name, url, **over):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 1024)
    asset = {
        "url": url,
        "video_url": url,
        "file_path": str(path),
        "path": str(path),
        "type": "video",
        "visual_type": "video",
        "clip_start": 100,
        "clip_duration": 8,
        "duration_seconds": 8,
        "segment_evidence": "verified_segment",
        "verification": {"accepted": True, "score": 0.9, "rejection_reason": ""},
        "title": name,
        "provider": "youtube",
    }
    asset.update(over)
    return asset


def _fetch(monkeypatch, tmp_path, assets, window_for_url):
    monkeypatch.setattr(mp, "_transcript_window_text", window_for_url)
    candidates = [
        {"url": a["url"], "candidate_id": f"cand_{i}", "provider": "youtube"}
        for i, a in enumerate(assets)
    ]
    by_url = {a["url"]: a for a in assets}

    def downloader(candidate, scene_dir, sentence_number, asset_index):
        return by_url[candidate["url"]]

    failures = []
    registry = mp.get_registry(f"stage3b-{uuid.uuid4()}")
    result = mp._uve_fetch_first(
        candidates,
        downloader,
        str(tmp_path),
        1,
        1,
        registry,
        set(),
        failures=failures,
        transcript_scenario=_SCENARIO,
        transcript_bible=_bible(),
    )
    return result, failures


def test_mismatched_candidate_is_demoted_and_next_pick_tried(monkeypatch, tmp_path):
    bad = _asset(tmp_path, "bad.mp4", "https://youtube.com/watch?v=bad")
    good = _asset(tmp_path, "good.mp4", "https://youtube.com/watch?v=good")
    result, failures = _fetch(
        monkeypatch,
        tmp_path,
        [bad, good],
        lambda url, start, duration=8: _MISMATCH_TEXT if "bad" in url else _MATCH_TEXT,
    )
    assert result is good
    # The demoted asset carries the stamp and the rejection.
    assert bad["verification"]["accepted"] is False
    assert bad["verification"]["rejection_reason"] == "transcript_mismatch"
    assert bad["verification"]["transcript_check"] == "transcript_mismatch"
    assert not Path(bad["file_path"]).exists()  # downloaded file removed
    # The surviving asset is stamped as matched, verdict untouched.
    assert good["verification"]["accepted"] is True
    assert good["verification"]["transcript_check"] == "interval_transcript_match"
    assert Path(good["file_path"]).exists()
    # The demotion is recorded, not silent.
    assert any(
        f["reason"] == "transcript_mismatch"
        and f["stage"] == "transcript_verification"
        for f in failures
    )


def test_all_mismatched_returns_none(monkeypatch, tmp_path):
    bad = _asset(tmp_path, "bad.mp4", "https://youtube.com/watch?v=bad")
    result, failures = _fetch(
        monkeypatch, tmp_path, [bad], lambda url, start, duration=8: _MISMATCH_TEXT
    )
    assert result is None
    assert bad["verification"]["accepted"] is False


def test_no_transcript_leaves_verdict_untouched(monkeypatch, tmp_path):
    asset = _asset(tmp_path, "a.mp4", "https://youtube.com/watch?v=a")
    result, _ = _fetch(
        monkeypatch, tmp_path, [asset], lambda url, start, duration=8: None
    )
    assert result is asset
    assert asset["verification"]["accepted"] is True
    assert asset["verification"]["transcript_check"] == "no_transcript"


def test_no_segment_evidence_skips_the_check(monkeypatch, tmp_path):
    asset = _asset(
        tmp_path,
        "a.mp4",
        "https://youtube.com/watch?v=a",
        segment_evidence="none",
    )
    result, _ = _fetch(
        monkeypatch, tmp_path, [asset], lambda url, start, duration=8: _MISMATCH_TEXT
    )
    assert result is asset
    assert asset["verification"]["accepted"] is True
    assert "transcript_check" not in asset["verification"]


def test_rejected_candidate_is_never_upgraded(monkeypatch, tmp_path):
    asset = _asset(
        tmp_path,
        "a.mp4",
        "https://youtube.com/watch?v=a",
        verification={"accepted": False, "score": 0.1, "rejection_reason": "below_threshold"},
    )
    result, _ = _fetch(
        monkeypatch, tmp_path, [asset], lambda url, start, duration=8: _MATCH_TEXT
    )
    assert result is asset  # fetch returns what the downloader made…
    # …but the transcript layer did not touch its verdict.
    assert asset["verification"]["accepted"] is False
    assert "transcript_check" not in asset["verification"]


# ---------------------------------------------------------------------------
# full _uve_search_and_pick pass: demotion end to end
# ---------------------------------------------------------------------------


def test_search_and_pick_demotes_mismatched_winner(monkeypatch, tmp_path):
    bad_url = "https://youtube.com/watch?v=badpick"
    good_url = "https://youtube.com/watch?v=goodpick"

    def candidate(url):
        return {
            "title": "Berlin Wall crowds crossing checkpoint 1989",
            "snippet": "Crowds of people crossing through the opened checkpoint at the Berlin Wall",
            "url": url,
            "video_url": url,
            "provider": "youtube",
            "source": "youtube",
            "matched_query": "berlin wall",
        }

    monkeypatch.setattr(
        mp,
        "_uve_scenario_search",
        lambda scenario, cache, pid, shot_need=None: (
            [candidate(bad_url), candidate(good_url)],
            [],
            [],
        ),
    )
    monkeypatch.setattr(
        mp, "_segment_start_detailed", lambda url, **kw: (100, "verified_segment")
    )

    def fake_download(url, output_path, start_time=0, duration=8):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"\x00" * 4096)
        return True

    monkeypatch.setattr(mp, "download_video_clip", fake_download)
    monkeypatch.setattr(mp, "validate_video", lambda path: True)
    monkeypatch.setattr(
        mp,
        "probe_video",
        lambda path: {
            "duration_seconds": 8.0,
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "codec": "h264",
            "has_audio": True,
        },
    )
    monkeypatch.setattr(
        mp,
        "_transcript_window_text",
        lambda url, start, duration=8: _MISMATCH_TEXT if "badpick" in url else _MATCH_TEXT,
    )
    monkeypatch.setattr(mp, "_uve_annotate", lambda asset, scenario, idx, text: asset)

    scenario = dict(_SCENARIO, queries=["berlin wall"], format_intent="any",
                    format_intent_confidence=0.0)
    scene_stats = {
        "candidates_considered": 0,
        "candidates_rejected": 0,
        "downloads_failed": 0,
        "text_cards": 0,
    }
    beat_videos, beat_images = [], []
    result = mp._uve_search_and_pick(
        scenario,
        {},
        search_cache={},
        project_id=f"stage3b-pick-{uuid.uuid4()}",
        bible=_bible(),
        registry=mp.get_registry(f"stage3b-pick-{uuid.uuid4()}"),
        used_keys=set(),
        scene_dir=str(tmp_path),
        scene_stats=scene_stats,
        beat={"sentence_numbers": [1], "text": _SENTENCES_TEXT},
        beat_videos=beat_videos,
        beat_images=beat_images,
        scenario_index=1,
        queries=["berlin wall"],
        review_records=[],
    )
    assert result is not None
    assert result["url"] == good_url
    assert [a["url"] for a in beat_videos] == [good_url]
    assert result["verification"]["transcript_check"] == "interval_transcript_match"
    failures = scene_stats.get("download_failures") or []
    assert any(f["reason"] == "transcript_mismatch" for f in failures)
    # Only the surviving pick's file remains on disk.
    assert len(list(tmp_path.glob("*.mp4"))) == 1


_SENTENCES_TEXT = "In 1989 the Berlin Wall fell and crowds crossed freely."
