"""VA-03 / VA-06 regression tests: segment-evidence fallbacks and labels.

- A positional (no-contract) segment is labelled positional_no_contract,
  never verified_segment.
- Provider-metadata duration is a bounded fallback for the whole-asset
  determination when yt-dlp / remote probe cannot establish duration.
- Failure causes distinguish a broken information channel from
  genuinely absent evidence. A failed lookup never becomes a segment.
- Gate 2: a LONG direct-media file (no timed-evidence channel) takes
  the labelled positional-window path instead of hard rejection —
  deterministic windows bounded by the probed / provider duration,
  honestly stamped positional_no_contract in the basis, the attempts
  record and the accepted asset.
"""

import pytest

from services import archive_org, media_pipeline as mp


@pytest.fixture(autouse=True)
def _clean_segment_state():
    mp._SEGMENT_BASIS.clear()
    mp._SEGMENT_ATTEMPTS.clear()
    mp._SEGMENT_FAILURE_CAUSE.clear()
    mp._SOURCE_DURATION_CACHE.clear()
    mp._yt_segment_uses.clear()
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


def test_direct_media_long_file_gets_labelled_positional_window(monkeypatch):
    """Gate 2: a 900s direct file (> 2x the 8s clip) used to be
    hard-rejected (no_evidence_path_for_long_direct_media — the #1
    download-failure class of the Petito run). It now yields a
    positional window, labelled positional_no_contract in the
    returned evidence, the segment basis and the attempts record —
    never upgraded to verified evidence."""
    monkeypatch.setattr(mp, "_remote_duration_seconds", lambda url: 900.0)
    url = "https://example.org/long.mp4"
    start, evidence = mp._segment_start_detailed(url)
    assert (start, evidence) == (0, "positional_no_contract")
    assert mp._SEGMENT_BASIS[url] == "positional_no_contract"
    assert url not in mp._SEGMENT_FAILURE_CAUSE
    attempts = mp._SEGMENT_ATTEMPTS[url]
    positional = [a for a in attempts if a["source"] == "positional_window"]
    assert len(positional) == 1
    assert positional[0]["outcome"] == "positional_no_contract"
    assert positional[0]["duration_seconds"] == 900.0
    assert positional[0]["duration_source"] == "remote_probe"
    assert positional[0]["window_start_seconds"] == 0
    assert positional[0]["window_seconds"] == mp.DEFAULT_CLIP_DURATION


def test_direct_media_positional_windows_walk_the_file(monkeypatch):
    """Windows are deterministic and non-overlapping: successive
    picks of one long source step by the clip length from the
    start of the file, and once every window is used the source
    rejects honestly instead of re-taking a window."""
    monkeypatch.setattr(mp, "_remote_duration_seconds", lambda url: 40.0)
    url = "https://example.org/forty.mp4"
    starts = [
        mp._segment_start_detailed(url)[0] for _ in range(5)
    ]
    assert starts == [0, 8, 16, 24, 32]
    start, evidence = mp._segment_start_detailed(url)
    assert (start, evidence) == (None, "none")
    assert mp._SEGMENT_FAILURE_CAUSE[url] == "positional_windows_exhausted"


def test_direct_media_long_file_honours_provider_duration(monkeypatch):
    """Archive.org shape: the remote probe cannot read the file,
    but the provider's own file-length metadata bounds the
    positional window (the same metadata the whole-asset fallback
    already trusted) — recorded as the duration source."""
    monkeypatch.setattr(mp, "_remote_duration_seconds", lambda url: None)
    url = "https://archive.org/download/some-item/long.mp4"
    start, evidence = mp._segment_start_detailed(
        url, provider_duration=1800.0, provider_duration_source="archive.org",
    )
    assert (start, evidence) == (0, "positional_no_contract")
    positional = [
        a for a in mp._SEGMENT_ATTEMPTS[url]
        if a["source"] == "positional_window"
    ]
    assert len(positional) == 1
    assert positional[0]["duration_source"] == "provider_metadata"
    assert positional[0]["duration_seconds"] == 1800.0


def test_direct_media_unknown_duration_still_rejects(monkeypatch):
    """No probe, no provider metadata: the duration is unknown, so
    no window can be bounded — rejection stands, with the honest
    cause. A positional window is never invented from nothing."""
    monkeypatch.setattr(mp, "_remote_duration_seconds", lambda url: None)
    url = "https://example.org/mystery.mp4"
    start, evidence = mp._segment_start_detailed(url)
    assert (start, evidence) == (None, "none")
    assert mp._SEGMENT_FAILURE_CAUSE[url] == "duration_unknown"


def test_download_candidate_accepts_positional_window_asset(
    monkeypatch, tmp_path
):
    """End to end through _uve_download_video_candidate: a long
    archive file is fetched at the positional window (the existing
    range-seek/trim path receives start_time=0) and the accepted
    asset carries the honesty label — segment_evidence and
    segment_evidence_basis both positional_no_contract, with the
    true source duration recorded."""
    monkeypatch.setattr(mp, "_remote_duration_seconds", lambda url: 900.0)
    fetched = {}

    def fake_clip(url, output_path, start_time=0, duration=8):
        fetched["start_time"] = start_time
        fetched["duration"] = duration
        from pathlib import Path
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"fake")
        return True

    monkeypatch.setattr(mp, "download_video_clip", fake_clip)
    monkeypatch.setattr(mp, "validate_video", lambda path: True)
    monkeypatch.setattr(mp, "probe_video", lambda path: {
        "duration_seconds": 8.0, "width": 1280, "height": 720,
        "fps": 25.0, "codec": "h264", "has_audio": True,
    })
    monkeypatch.setattr(mp, "_video_technical_failures", lambda probe: [])
    candidate = {
        "url": "https://archive.org/download/item/long.mp4",
        "provider": "archive.org", "duration_seconds": 900.0,
        "shot_need": {},
    }
    asset = mp._uve_download_video_candidate(candidate, tmp_path, 3, 0)
    assert asset is not None
    assert fetched["start_time"] == 0
    assert asset["clip_start"] == 0
    assert asset["segment_evidence"] == "positional_no_contract"
    assert asset["segment_evidence_basis"] == "positional_no_contract"
    assert asset["source_duration_seconds"] == 900.0


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
