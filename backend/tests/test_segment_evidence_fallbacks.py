"""VA-03 / VA-06 regression tests: segment-evidence fallbacks and labels.

- A positional (no-contract) segment is labelled positional_no_contract,
  never verified_segment.
- Provider-metadata duration is a bounded fallback for the whole-asset
  determination when yt-dlp / remote probe cannot establish duration.
- Failure causes distinguish a broken information channel from
  genuinely absent evidence. A failed lookup never becomes a segment.
"""

import pytest

from services import archive_org, media_pipeline as mp


@pytest.fixture(autouse=True)
def _clean_segment_state():
    mp._SEGMENT_BASIS.clear()
    mp._SEGMENT_ATTEMPTS.clear()
    mp._SEGMENT_FAILURE_CAUSE.clear()
    mp._SOURCE_DURATION_CACHE.clear()
    yield


PAGE_URL = "https://www.youtube.com/watch?v=abc123XYZ"


def test_positional_segment_is_not_labelled_verified(monkeypatch):
    monkeypatch.setattr(mp, "_youtube_slim", lambda key: {
        "duration": 300, "chapters": [], "title": "Some long video",
        "description": "",
    })
    monkeypatch.setattr(mp, "_youtube_info", lambda key: {
        "duration": 300, "chapters": [], "title": "Some long video",
        "description": "",
    })
    monkeypatch.setattr(mp, "_youtube_transcript_segment", lambda *a, **k: None)
    start, evidence = mp._segment_start_detailed(PAGE_URL)
    assert start == 0
    assert evidence == "positional_no_contract"
    assert mp._SEGMENT_BASIS[PAGE_URL.split("&")[0]] == "positional_no_contract"


def test_chapter_segment_remains_verified(monkeypatch):
    slim = {
        "duration": 300,
        "chapters": [{"title": "Crowds at the Berlin Wall", "start_time": 40}],
        "title": "Berlin 1989", "description": "",
    }
    monkeypatch.setattr(mp, "_youtube_slim", lambda key: slim)
    monkeypatch.setattr(mp, "_youtube_info", lambda key: slim)
    start, evidence = mp._segment_start_detailed(
        PAGE_URL, subject="Berlin Wall", place="berlin", event="", era="1989"
    )
    assert start == 40
    assert evidence == "verified_segment"
    assert mp._SEGMENT_BASIS[PAGE_URL.split("&")[0]] == "chapter"


def test_provider_duration_fallback_whole_asset(monkeypatch):
    # yt-dlp info unavailable; provider metadata says 12s -> whole asset.
    monkeypatch.setattr(mp, "_youtube_slim", lambda key: None)
    start, evidence = mp._segment_start_detailed(
        "https://archive.org/details/some-item",
        provider_duration=12.0, provider_duration_source="archive.org",
    )
    assert (start, evidence) == (0, "whole_asset")
    attempts = mp._SEGMENT_ATTEMPTS["https://archive.org/details/some-item"]
    assert attempts[0]["source"] == "yt_dlp_info"
    assert attempts[0]["outcome"] == "unavailable"
    assert attempts[1]["source"] == "provider_metadata"
    assert attempts[1]["outcome"] == "duration_known"


def test_provider_duration_long_source_still_needs_evidence(monkeypatch):
    monkeypatch.setattr(mp, "_youtube_slim", lambda key: None)
    monkeypatch.setattr(mp, "_youtube_start", lambda *a, **k: None)
    url = "https://archive.org/details/long-item"
    start, evidence = mp._segment_start_detailed(
        url, provider_duration=1800.0, provider_duration_source="archive.org",
    )
    assert (start, evidence) == (None, "none")
    # Duration WAS established (by the provider) but no timed evidence
    # exists: the cause is missing evidence, not a broken channel.
    assert mp._SEGMENT_FAILURE_CAUSE[url] == "no_evidence"


def test_info_unavailable_cause_when_nothing_known(monkeypatch):
    monkeypatch.setattr(mp, "_youtube_slim", lambda key: None)
    monkeypatch.setattr(mp, "_youtube_start", lambda *a, **k: None)
    start, evidence = mp._segment_start_detailed(PAGE_URL, subject="Berlin Wall")
    assert (start, evidence) == (None, "none")
    assert mp._SEGMENT_FAILURE_CAUSE[PAGE_URL] == "info_unavailable"


def test_direct_media_provider_duration_fallback(monkeypatch):
    monkeypatch.setattr(mp, "_remote_duration_seconds", lambda url: None)
    url = "https://upload.wikimedia.org/clip.webm"
    start, evidence = mp._segment_start_detailed(
        url, provider_duration=10.0, provider_duration_source="wikimedia",
    )
    assert (start, evidence) == (0, "whole_asset")


def test_direct_media_long_file_has_no_evidence_path(monkeypatch):
    monkeypatch.setattr(mp, "_remote_duration_seconds", lambda url: 900.0)
    url = "https://example.org/long.mp4"
    start, evidence = mp._segment_start_detailed(url)
    assert (start, evidence) == (None, "none")
    assert (
        mp._SEGMENT_FAILURE_CAUSE[url]
        == "no_evidence_path_for_long_direct_media"
    )


def test_download_candidate_stamps_segment_cause(monkeypatch, tmp_path):
    def fake_detailed(url, **kwargs):
        mp._SEGMENT_ATTEMPTS[url] = [
            {"source": "yt_dlp_info", "outcome": "unavailable"}
        ]
        mp._SEGMENT_FAILURE_CAUSE[url] = "info_unavailable"
        return None, "none"

    monkeypatch.setattr(mp, "_segment_start_detailed", fake_detailed)
    candidate = {
        "url": PAGE_URL, "provider": "youtube",
        "shot_need": {"required_subject": "Berlin Wall"},
    }
    result = mp._uve_download_video_candidate(candidate, tmp_path, 1, 0)
    assert result is None
    assert candidate["_download_error"] == "no_segment_evidence"
    assert candidate["_segment_failure_cause"] == "info_unavailable"
    assert candidate["rejection_stage"] == "segment_evidence"
    assert candidate["_segment_attempts"][0]["source"] == "yt_dlp_info"


def test_archive_org_carries_provider_duration(monkeypatch):
    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_request(method, url, **kwargs):
        if "advancedsearch" in url:
            return _Resp({"response": {"docs": [
                {"identifier": "item1", "title": "T"}]}})
        return _Resp({
            "metadata": {"title": "T", "licenseurl": "", "creator": "C"},
            "files": [
                {"name": "a.mp4", "format": "MPEG4", "size": 500000,
                 "length": "12.5"},
            ],
        })

    monkeypatch.setattr(archive_org, "request_with_retry", fake_request)
    results = archive_org.search_archive_videos("berlin", limit=3)
    assert len(results) == 1
    assert results[0]["duration_seconds"] == 12.5


def test_provider_duration_parsing():
    assert mp._provider_duration_seconds({"duration_seconds": 42})[0] == 42.0
    assert mp._provider_duration_seconds({"duration": "1:30"})[0] == 90.0
    assert mp._provider_duration_seconds({"duration": "0:08"})[0] == 8.0
    assert mp._provider_duration_seconds({"duration": "abc"})[0] is None
    assert mp._provider_duration_seconds({})[0] is None
