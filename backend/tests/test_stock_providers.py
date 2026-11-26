"""Gate 2 WS4 — Pexels + Pixabay restored as stock discovery providers.

History (kept so it is not lost): on 2026-11-19 the founder imposed a
PERMANENT ban on Pexels and Pixabay (IA-05); the implementations were
removed and tests/test_provider_ban.py pinned the ban statically and
at runtime. On 2026-11-26 the founder LIFTED that ban (Annex B of
~/workspace/cinemora-audit/UNIFIED_VISUAL_PRODUCTION_AUDIT_2026-11-26.md):
both providers are approved again as stock video + photograph
discovery inside the existing Assets Manager. This file REPLACES
tests/test_provider_ban.py (deleted) and pins the restored behaviour:

(a) keyless no-op — no PEXELS_API_KEY / PIXABAY_API_KEY configured
    (the deployment state at restoration time) means every search
    returns [], raises nothing, makes no HTTP call, and discovery
    is unaffected;
(b) mocked-HTTP normalisation for all four search functions,
    including provenance / licence / creator / illustrative-stock
    stamp fields;
(c) story_understanding no longer rejects pexels/pixabay candidates
    as banned_provider, while a social-platform candidate is STILL
    rejected (the social/meme vetoes are a different mechanism and
    remain fully in force);
(d) CINEMORA_DISABLED_PROVIDERS config excludes the stock providers.

Live evidence at restoration time (2026-11-26, keyless requests):
api.pexels.com/videos/search -> HTTP 401 "Missing API key";
pixabay.com/api/videos/ -> HTTP 400 "Invalid or missing API key".
Both endpoints are reachable and require keys — exactly the
degradation expectation the keyless no-op implements.
"""

from __future__ import annotations

import requests

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import pipeline_discovery, stock_providers
from services import story_understanding as story


# ---------------------------------------------------------------------------
# Mocked HTTP plumbing
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error", response=self
            )

    def json(self):
        return self._payload


class _FakeSession:
    """requests.Session stand-in replaying one scripted response."""

    def __init__(self, response):
        self._response = response
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "params": params or {}})
        return self._response


def _mock_http(monkeypatch, payload, status_code=200):
    session = _FakeSession(_FakeResponse(status_code, payload))
    monkeypatch.setattr(stock_providers, "_session", lambda: session)
    return session


def _forbid_http(monkeypatch):
    def _boom():
        raise AssertionError("no HTTP call may be made without a key")

    monkeypatch.setattr(stock_providers, "_session", _boom)


# ---------------------------------------------------------------------------
# (a) Keyless no-op
# ---------------------------------------------------------------------------

def test_keyless_searches_are_silent_noops(monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    _forbid_http(monkeypatch)

    assert stock_providers.search_pexels_videos("police van", limit=3) == []
    assert stock_providers.search_pexels_images("police van", limit=3) == []
    assert stock_providers.search_pixabay_videos("police van", limit=3) == []
    assert stock_providers.search_pixabay_images("police van", limit=3) == []
    # The pipeline_discovery wrappers are no-ops too.
    assert pipeline_discovery.search_pexels_videos("police van") == []
    assert pipeline_discovery.search_pexels_images("police van") == []
    assert pipeline_discovery.search_pixabay_videos("police van") == []
    assert pipeline_discovery.search_pixabay_images("police van") == []


def test_provider_http_error_degrades_to_empty(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    monkeypatch.setenv("PIXABAY_API_KEY", "test-key")
    _mock_http(monkeypatch, {}, status_code=401)

    assert stock_providers.search_pexels_videos("police van") == []
    assert stock_providers.search_pexels_images("police van") == []
    assert stock_providers.search_pixabay_videos("police van") == []
    assert stock_providers.search_pixabay_images("police van") == []


# ---------------------------------------------------------------------------
# (b) Mocked-HTTP normalisation
# ---------------------------------------------------------------------------

_PEXELS_VIDEO_PAYLOAD = {
    "videos": [{
        "id": 123,
        "url": "https://www.pexels.com/video/police-van-123/",
        "duration": 12,
        "user": {"name": "Jane Camera", "url": "https://www.pexels.com/@jane"},
        "video_files": [
            {"link": "https://videos.pexels.com/123/hd1920.mp4",
             "width": 1920, "height": 1080, "file_type": "video/mp4"},
            {"link": "https://videos.pexels.com/123/hd1280.mp4",
             "width": 1280, "height": 720, "file_type": "video/mp4"},
            {"link": "https://videos.pexels.com/123/sd640.mp4",
             "width": 640, "height": 360, "file_type": "video/mp4"},
        ],
    }]
}

_PEXELS_IMAGE_PAYLOAD = {
    "photos": [{
        "id": 456,
        "url": "https://www.pexels.com/photo/police-van-456/",
        "width": 4000, "height": 3000,
        "photographer": "John Lens",
        "photographer_url": "https://www.pexels.com/@john",
        "alt": "A police van on a city street",
        "src": {"original": "https://images.pexels.com/456/original.jpg",
                "large": "https://images.pexels.com/456/large.jpg"},
    }]
}

_PIXABAY_VIDEO_PAYLOAD = {
    "hits": [{
        "id": 789,
        "pageURL": "https://pixabay.com/videos/police-van-789/",
        "duration": 20,
        "user": "pixuser",
        "user_id": 42,
        "tags": "police, van, street",
        "videos": {
            "large": {"url": "https://cdn.pixabay.com/789/large.mp4",
                      "width": 1920, "height": 1080},
            "medium": {"url": "https://cdn.pixabay.com/789/medium.mp4",
                       "width": 1280, "height": 720},
            "small": {"url": "https://cdn.pixabay.com/789/small.mp4",
                      "width": 640, "height": 360},
        },
    }]
}

_PIXABAY_IMAGE_PAYLOAD = {
    "hits": [{
        "id": 101,
        "pageURL": "https://pixabay.com/photos/police-van-101/",
        "user": "pixuser",
        "user_id": 42,
        "tags": "police, van, street",
        "imageWidth": 4000, "imageHeight": 3000,
        "webformatURL": "https://cdn.pixabay.com/101/web.jpg",
        "largeImageURL": "https://cdn.pixabay.com/101/large.jpg",
    }]
}


def _assert_stock_stamp(candidate, provider, licence):
    assert candidate["provider"] == provider
    assert candidate["source"] == provider
    assert candidate["license"] == licence
    assert candidate["license_url"]
    assert candidate["attribution_required"] is False
    assert candidate["rights_status"] == "recorded"
    assert candidate["creator"]
    assert candidate["creator_url"]
    # Illustrative-stock honesty stamp (Annex B rule 4).
    assert candidate["illustrative_stock"] is True
    assert candidate["provenance"] == "illustrative_stock"
    assert candidate["matched_query"] == "police van"


def test_pexels_videos_normalisation(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    session = _mock_http(monkeypatch, _PEXELS_VIDEO_PAYLOAD)

    results = stock_providers.search_pexels_videos("police van", limit=3)

    assert len(results) == 1
    hit = results[0]
    _assert_stock_stamp(hit, "pexels", "Pexels License")
    assert hit["kind"] == "video" and hit["type"] == "video"
    assert hit["url"] == "https://www.pexels.com/video/police-van-123/"
    assert hit["source_url"] == hit["url"]
    # Best practical file: largest MP4 at or under 720p wide.
    assert hit["video_url"] == "https://videos.pexels.com/123/sd640.mp4"
    assert hit["width"] == 640 and hit["height"] == 360
    assert hit["duration_seconds"] == 12
    assert hit["creator"] == "Jane Camera"
    # The API key went in the Authorization header, not the URL.
    assert session.calls[0]["url"] == "https://api.pexels.com/videos/search"


def test_pexels_images_normalisation(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    _mock_http(monkeypatch, _PEXELS_IMAGE_PAYLOAD)

    results = stock_providers.search_pexels_images("police van", limit=3)

    assert len(results) == 1
    hit = results[0]
    _assert_stock_stamp(hit, "pexels", "Pexels License")
    assert hit["kind"] == "image" and hit["type"] == "image"
    assert hit["image_url"] == "https://images.pexels.com/456/large.jpg"
    assert hit["source_url"] == "https://www.pexels.com/photo/police-van-456/"
    assert hit["title"] == "A police van on a city street"
    assert hit["width"] == 4000 and hit["height"] == 3000
    assert hit["creator"] == "John Lens"


def test_pixabay_videos_normalisation(monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "test-key")
    session = _mock_http(monkeypatch, _PIXABAY_VIDEO_PAYLOAD)

    results = stock_providers.search_pixabay_videos("police van", limit=3)

    assert len(results) == 1
    hit = results[0]
    _assert_stock_stamp(hit, "pixabay", "Pixabay Content License")
    assert hit["kind"] == "video" and hit["type"] == "video"
    assert hit["url"] == "https://pixabay.com/videos/police-van-789/"
    assert hit["video_url"] == "https://cdn.pixabay.com/789/small.mp4"
    assert hit["width"] == 640 and hit["height"] == 360
    assert hit["duration_seconds"] == 20
    assert hit["creator"] == "pixuser"
    assert hit["creator_url"] == "https://pixabay.com/users/pixuser-42/"
    # The API key went as the documented key query parameter.
    assert session.calls[0]["params"]["key"] == "test-key"


def test_pixabay_images_normalisation(monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "test-key")
    _mock_http(monkeypatch, _PIXABAY_IMAGE_PAYLOAD)

    results = stock_providers.search_pixabay_images("police van", limit=3)

    assert len(results) == 1
    hit = results[0]
    _assert_stock_stamp(hit, "pixabay", "Pixabay Content License")
    assert hit["kind"] == "image" and hit["type"] == "image"
    assert hit["image_url"] == "https://cdn.pixabay.com/101/large.jpg"
    assert hit["source_url"] == "https://pixabay.com/photos/police-van-101/"
    assert hit["width"] == 4000 and hit["height"] == 3000
    assert hit["creator"] == "pixuser"


# ---------------------------------------------------------------------------
# (c) Ban lifted; social veto intact
# ---------------------------------------------------------------------------

def test_stock_candidates_are_not_banned():
    for provider in ("pexels", "pixabay"):
        candidate = {
            "title": "Berlin Wall crowds 1989",
            "snippet": "Crowds at the Berlin Wall",
            "provider": provider,
            "url": f"https://www.{provider}.com/video/1",
        }
        assert not story.is_banned_provider(candidate)
        scenario = {
            "specificity": "representative",
            "search_terms": ["berlin wall"],
            "visual": "berlin wall crowds",
        }
        verdict = story.verify_candidate(
            candidate, scenario, {"by_id": {}, "entities": []}
        )
        assert verdict["rejection_reason"] != story.BANNED_PROVIDER_REASON


def test_social_platform_candidate_still_rejected():
    candidate = {
        "title": "Berlin Wall crowds 1989",
        "snippet": "Crowds at the Berlin Wall",
        "provider": "tiktok",
        "url": "https://www.tiktok.com/@someone/video/1",
    }
    assert story.is_social_platform_source(candidate)
    scenario = {
        "specificity": "representative",
        "search_terms": ["berlin wall"],
        "visual": "berlin wall crowds",
    }
    verdict = story.verify_candidate(
        candidate, scenario, {"by_id": {}, "entities": []}
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == story.SOCIAL_PLATFORM_REASON


# ---------------------------------------------------------------------------
# (d) Disabled-providers config + discovery registration
# ---------------------------------------------------------------------------

def test_disabled_providers_config_excludes_stock(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    monkeypatch.setenv("PIXABAY_API_KEY", "test-key")
    monkeypatch.setenv("CINEMORA_DISABLED_PROVIDERS", "pexels, pixabay")
    _forbid_http(monkeypatch)

    assert pipeline_discovery.search_pexels_videos("police van") == []
    assert pipeline_discovery.search_pexels_images("police van") == []
    assert pipeline_discovery.search_pixabay_videos("police van") == []
    assert pipeline_discovery.search_pixabay_images("police van") == []


def test_scenario_search_merges_stock_hits(monkeypatch):
    from services import media_pipeline as mp

    monkeypatch.setattr(mp, "search_videos_multi", lambda q, limit=8: [])
    monkeypatch.setattr(mp, "search_web_sources", lambda q, limit=5: [])
    monkeypatch.setattr(mp, "search_image_sources", lambda q, limit=5: [])
    monkeypatch.setattr(mp, "search_wikimedia_images", lambda q, limit=3: [])
    monkeypatch.setattr(mp, "search_archive_org_videos", lambda q, limit=5: [])
    monkeypatch.setattr(mp, "search_wikimedia_videos", lambda q, limit=5: [])

    def stock_video(provider):
        def _search(query, limit=5):
            return [{
                "title": "stock hit", "provider": provider, "source": provider,
                "video_url": f"https://stock.example/{provider}/{abs(hash(query))}.mp4",
                "url": f"https://stock.example/{provider}/page",
                "illustrative_stock": True, "provenance": "illustrative_stock",
            }]
        return _search

    def stock_image(provider):
        def _search(query, limit=5):
            return [{
                "title": "stock hit", "provider": provider, "source": provider,
                "image_url": f"https://stock.example/{provider}/{abs(hash(query))}.jpg",
                "url": f"https://stock.example/{provider}/img",
                "illustrative_stock": True, "provenance": "illustrative_stock",
            }]
        return _search

    # _resolve_stock_provider prefers the media_pipeline namespace (the
    # established patch-parity convention), so the doubles must be installed
    # on mp, not pipeline_discovery — otherwise the real keyless wrappers run.
    monkeypatch.setattr(
        mp, "search_pexels_videos", stock_video("pexels"))
    monkeypatch.setattr(
        mp, "search_pixabay_videos", stock_video("pixabay"))
    monkeypatch.setattr(
        mp, "search_pexels_images", stock_image("pexels"))
    monkeypatch.setattr(
        mp, "search_pixabay_images", stock_image("pixabay"))

    scenario = {"queries": ["police van street"], "visual": "police van"}
    videos, _web, images = mp._uve_scenario_search(scenario, {}, "gate2-ws4")

    assert {v.get("provider") for v in videos} == {"pexels", "pixabay"}
    assert {i.get("provider") for i in images} == {"pexels", "pixabay"}
    assert all(v["illustrative_stock"] for v in videos)
    assert all(i["illustrative_stock"] for i in images)
