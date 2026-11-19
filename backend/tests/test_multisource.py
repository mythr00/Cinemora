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

from services import archive_org, licensed_media, media_pipeline, search_provider
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


# ---------------------------------------------------------------------------
# MS-4: independent providers — archive.org, Wikimedia video, and
# failure isolation between providers.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeArchiveClient:
    headers = {}

    def get(self, url, params=None, timeout=None):
        if "advancedsearch" in url:
            return _FakeResponse({
                "response": {"docs": [{
                    "identifier": "berlin-wall-1989",
                    "title": "Berlin Wall falls",
                    "creator": "Archive Contributor",
                    "licenseurl": "https://creativecommons.org/licenses/by-nc-nd/3.0/",
                    "description": "<p>Crowds at the wall</p>",
                }]}
            })
        return _FakeResponse({
            "metadata": {
                "title": "Berlin Wall falls",
                "creator": "Archive Contributor",
                "licenseurl": "https://creativecommons.org/licenses/by-nc-nd/3.0/",
                "description": "<p>Crowds at the wall</p>",
            },
            "files": [
                {"name": "big.mp4", "format": "h.264", "size": 50_000_000},
                {"name": "small.mp4", "format": "h.264", "size": 2_000_000},
                {"name": "thumb.mp4", "format": "Thumbnail", "size": 10_000},
            ],
        })


def test_archive_org_search_maps_metadata_and_licence(monkeypatch):
    monkeypatch.setattr(archive_org, "_session", lambda: _FakeArchiveClient())
    results = archive_org.search_archive_videos("berlin wall", limit=3)
    assert len(results) == 1
    item = results[0]
    assert item["provider"] == "archive.org"
    assert item["license"] == "CC BY-NC-ND 3.0"  # real label from licenseurl
    assert item["license_url"].endswith("by-nc-nd/3.0/")
    assert item["creator"] == "Archive Contributor"
    assert item["video_url"].endswith("/small.mp4")  # smallest usable derivative
    assert item["rights_status"] == "recorded"


def test_archive_org_unknown_licence_stays_unknown():
    assert archive_org.licence_label("") == "unknown"
    assert archive_org.licence_label("https://example.org/some-licence") == "unknown"
    assert archive_org.licence_label("https://creativecommons.org/licenses/by/4.0/") == "CC BY 4.0"


class _FakeSession:
    def get(self, url, params=None, timeout=None):
        return _FakeResponse({
            "query": {"pages": {"1": {
                "title": "File:Berlin Wall.webm",
                "videoinfo": [{
                    "url": "https://upload.wikimedia.org/original.webm",
                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:Berlin_Wall.webm",
                    "derivatives": [
                        {"src": "https://upload.wikimedia.org/big.mp4", "type": "video/mp4", "width": 1920},
                        {"src": "https://upload.wikimedia.org/small.mp4", "type": "video/mp4", "width": 640},
                    ],
                    "extmetadata": {
                        "LicenseShortName": {"value": "CC BY-NC-ND 3.0"},
                        "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-nc-nd/3.0/"},
                        "Artist": {"value": "Commons Filmer"},
                        "ImageDescription": {"value": "Wall crowds"},
                    },
                }],
            }}}
        })


def test_wikimedia_video_discovery_keeps_licence_and_picks_small_derivative(monkeypatch):
    monkeypatch.setattr(licensed_media, "_session", lambda: _FakeSession())
    results = licensed_media.search_wikimedia_videos("berlin wall", limit=3)
    assert len(results) == 1
    item = results[0]
    # NC-ND is NOT silently filtered at discovery — it is labelled.
    assert item["license"] == "CC BY-NC-ND 3.0"
    assert item["download_url"].endswith("/small.mp4")
    assert item["creator"] == "Commons Filmer"


def test_provider_failure_does_not_block_other_providers(monkeypatch):
    """Mandated test 8: one provider raising must not stop discovery."""
    monkeypatch.setattr(
        media_pipeline, "search_videos_multi",
        lambda q, limit=8: [{
            "title": "Vimeo hit", "video_url": "https://vimeo.com/1",
            "url": "https://vimeo.com/1", "source": "vimeo", "provider": "vimeo",
        }],
    )
    monkeypatch.setattr(media_pipeline, "search_web_sources", lambda q, limit=5: [])
    monkeypatch.setattr(media_pipeline, "search_image_sources", lambda q, limit=5: [])
    monkeypatch.setattr(media_pipeline, "search_wikimedia_images", lambda q, limit=3: [])

    def boom(q, limit=3):
        raise RuntimeError("archive.org unreachable")

    monkeypatch.setattr(media_pipeline, "search_archive_org_videos", boom)
    monkeypatch.setattr(media_pipeline, "search_wikimedia_videos", lambda q, limit=3: [])

    scenario = {"queries": ["berlin wall 1989"]}
    videos, web, images = media_pipeline._uve_scenario_search(scenario, {}, "ms4-test")
    assert len(videos) == 1
    assert videos[0]["provider"] == "vimeo"
