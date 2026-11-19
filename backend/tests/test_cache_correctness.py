"""AUD-18 / AUD-19 / AUD-20 regression tests: cache correctness.

- AUD-18: process-global YouTube caches (used segments, metadata,
  permanent failure blacklists) leaked across projects in the long-lived
  worker. reset_media_caches() now clears them per production run.
- AUD-19: a cached "transcript unavailable" verdict was permanent. It now
  expires after a TTL and the caller retries.
- AUD-20: the registry's segment-overlap protection was dead code because
  registration never passed start/end. It now does.
"""

import sqlite3
import time

from services import media_pipeline, source_cache
from services.visual_registry import VisualRegistry


def test_reset_media_caches_clears_cross_project_state():
    url = "https://youtube.com/watch?v=shared"
    media_pipeline._yt_segment_uses[url] = [(0, 8)]
    media_pipeline._youtube_metadata_cache[url] = {"_full": True}
    media_pipeline._youtube_transcript_cache[url] = [{"text": "x"}]
    media_pipeline._yt_info_failed.add(url)
    media_pipeline._yt_transcript_failed.add(url)

    media_pipeline.reset_media_caches()

    assert media_pipeline._yt_segment_uses == {}
    assert media_pipeline._youtube_metadata_cache == {}
    assert media_pipeline._youtube_transcript_cache == {}
    assert media_pipeline._yt_info_failed == set()
    assert media_pipeline._yt_transcript_failed == set()


def test_transcript_unavailable_expires_after_ttl(tmp_path, monkeypatch):
    db = tmp_path / "intel.sqlite3"
    monkeypatch.setenv("SOURCE_INTEL_DB", str(db))
    url = "https://youtube.com/watch?v=ttl-test"

    source_cache.profile_upsert(url, transcript_status="unavailable")
    fresh = source_cache.profile_get(url)
    assert fresh["transcript_status"] == "unavailable"

    # Age the profile beyond the TTL, as if the failure happened long ago.
    stale = time.time() - source_cache.TRANSCRIPT_UNAVAILABLE_TTL_SECONDS - 60
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE source_profile SET analyzed_at=? WHERE url=?", (stale, url))

    aged = source_cache.profile_get(url)
    assert aged["transcript_status"] == "unknown"  # retryable again


def test_registry_segment_overlap_now_engages():
    registry = VisualRegistry(project_id="overlap-test")
    url = "https://youtube.com/watch?v=seg"

    allowed, _ = registry.can_use(url=url, title="t", start=100, end=108)
    assert allowed

    registry.register(path="/p/a.mp4", url=url, title="t", clip_key=f"{url}|100", start=100, end=108)

    # The exact machinery that was dead before AUD-20: the stored segment
    # is now visible, and an overlapping request is refused.
    assert registry.is_segment_used(url, 104, 112)
    allowed, reason = registry.can_use(
        url=url, title="t", start=104, end=112, allow_source_reuse=True
    )
    assert not allowed
    assert reason

    # A non-overlapping segment from the same source remains usable.
    allowed, _ = registry.can_use(
        url=url, title="t", start=200, end=208, allow_source_reuse=True
    )
    assert allowed
