"""Multi-source retrieval correction tests (MS-1..MS-7).

MS-1: YouTube discovery lock-in removed.
- Non-YouTube Serper video results survive discovery with real provider
  tags (nothing is stamped "youtube" that isn't).
- Any provider, including YouTube, can be disabled without affecting
  the others.
- The per-source usage cap and source-reuse rules in _uve_pick apply to
  every provider equally (they were YouTube-only carve-outs).
"""

import uuid

from services import media_pipeline, search_provider
from services.visual_registry import VisualRegistry


def _mixed_hits(query, limit=15):
    return [
        {"title": "Berlin Wall falls — newsreel", "video_url": "https://www.youtube.com/watch?v=abc123",
         "url": "https://www.youtube.com/watch?v=abc123", "snippet": "Berlin Wall 1989",
         "source": "Archive Channel", "date": "", "duration": ""},
        {"title": "Berlin Wall documentary", "video_url": "https://vimeo.com/12345",
         "url": "https://vimeo.com/12345", "snippet": "Berlin Wall 1989",
         "source": "DocFilms", "date": "", "duration": ""},
        {"title": "Berlin Wall — BBC report", "video_url": "https://www.bbc.com/news/av/world-europe-123",
         "url": "https://www.bbc.com/news/av/world-europe-123", "snippet": "Berlin Wall 1989",
         "source": "BBC", "date": "", "duration": ""},
        {"title": "Berlin Wall 1989 NBC special", "video_url": "https://archive.org/details/berlin-wall-1989",
         "url": "https://archive.org/details/berlin-wall-1989", "snippet": "Berlin Wall 1989",
         "source": "NBC", "date": "", "duration": ""},
    ]


def test_non_youtube_results_survive_discovery(monkeypatch):
    monkeypatch.setattr(search_provider, "search_videos", _mixed_hits)
    monkeypatch.delenv("CINEMORA_DISABLED_PROVIDERS", raising=False)
    results = media_pipeline.search_videos_multi("berlin wall", limit=8)
    providers = {item["provider"] for item in results}
    assert providers == {"youtube", "vimeo", "bbc.com", "archive.org"}
    by_provider = {item["provider"]: item for item in results}
    # Real sources preserved — nothing falsely stamped.
    assert by_provider["vimeo"]["source"] == "vimeo"
    assert by_provider["bbc.com"]["source_domain"] == "bbc.com"
    assert by_provider["archive.org"]["source"] == "archive.org"
    # YouTube creator evidence (channel name) still threads through.
    assert by_provider["youtube"]["creator"] == "Archive Channel"
    # Licences stay honest.
    assert all(item["license"] == "unknown" for item in results)


def test_youtube_can_be_disabled_without_affecting_others(monkeypatch):
    monkeypatch.setattr(search_provider, "search_videos", _mixed_hits)
    monkeypatch.setenv("CINEMORA_DISABLED_PROVIDERS", "youtube")
    results = media_pipeline.search_videos_multi("berlin wall", limit=8)
    providers = {item["provider"] for item in results}
    assert "youtube" not in providers
    assert providers == {"vimeo", "bbc.com", "archive.org"}


def _accepted_candidate(url):
    return {
        "url": url,
        "video_url": url,
        "title": "Berlin Wall documentary footage",
        "snippet": "Berlin Wall 1989",
        "verification": {"accepted": True, "score": 0.9},
        "universal_score": 0.9,
        "relevance_score": 0.9,
    }


def test_usage_cap_applies_to_non_youtube_providers():
    registry = VisualRegistry(project_id=f"test-{uuid.uuid4()}")
    url = "https://vimeo.com/12345"
    for index in range(3):
        registry.register(path=f"/p/{index}.mp4", url=url, title="t")
    candidate = _accepted_candidate(url)
    picked = media_pipeline._uve_pick(
        [candidate], {"visual": "Berlin Wall"}, registry, set(), shot_need={}
    )
    assert picked == []
    assert candidate.get("reject_reason") == "source_usage_limit"


def test_non_youtube_source_reuse_is_allowed():
    registry = VisualRegistry(project_id=f"test-{uuid.uuid4()}")
    candidate = _accepted_candidate("https://archive.org/details/berlin-wall-1989")
    key = media_pipeline._uve_candidate_key(candidate)
    # The candidate's key is already used in this scene — previously only
    # YouTube sources could be re-picked; now every provider can (the
    # segment machinery differentiates the actual footage used).
    picked = media_pipeline._uve_pick(
        [candidate], {"visual": "Berlin Wall"}, registry, {key}, shot_need={}
    )
    assert len(picked) == 1
