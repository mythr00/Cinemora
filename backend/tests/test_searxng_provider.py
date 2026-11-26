"""SearXNG primary + Serper fallback (Master Implementation
2026-11-26).

Hermetic: HTTP is stubbed at requests.get (SearXNG) and
requests.post (Serper), exactly as the Phase 4c suite stubs it;
no network, no real instance. Real-instance behaviour is covered
by the validation evidence in the master-implementation audit
directory, not here.

Pins:
- SearXNG normalisation into the established result shapes,
  with duplicate-URL removal and retained source metadata.
- Routing: SearXNG primary when configured; Serper fallback on
  failure AND on an unusable (empty) result; fallback events
  recorded; circuit breaker stops hammering a down instance.
- CINEMORA_SEARCH_PROVIDER=serper restores the exact pre-2026
  behaviour even when a SearXNG URL exists.
- Provider attribution is recorded in the usage ledger.
"""

import uuid

import pytest
import requests

from services import provider_usage, run_metrics, search_provider


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clean_breaker():
    search_provider._breaker_reset()
    yield
    search_provider._breaker_reset()


@pytest.fixture()
def searxng_env(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "http://searxng.test:8888")
    monkeypatch.setenv("SERPER_API_KEY", "fallback-key")
    monkeypatch.setattr(search_provider, "SERPER_API_KEY", "fallback-key")
    monkeypatch.setenv("SEARXNG_MAX_ATTEMPTS", "1")
    calls = {"get": 0, "post": 0}
    return calls


def _unique():
    return f"searxng-probe-{uuid.uuid4()}"


def _stub_serper(monkeypatch, calls, payload=None):
    def _post(*a, **k):
        calls["post"] += 1
        return _Response(200, payload if payload is not None else {
            "organic": [{"title": "ST", "link": "https://s.example/1",
                         "snippet": "SS"}],
            "images": [],
            "videos": [],
        })

    monkeypatch.setattr(search_provider.requests, "post", _post)


def _stub_searxng(monkeypatch, calls, payload=None, status=200):
    def _get(url, params=None, headers=None, timeout=None):
        calls["get"] += 1
        if url.endswith("/healthz"):
            return _Response(200, {})
        return _Response(status, payload or {})

    monkeypatch.setattr(search_provider.requests, "get", _get)


def test_searxng_web_success_normalises_and_dedupes(
    monkeypatch, searxng_env
):
    calls = searxng_env
    _stub_searxng(monkeypatch, calls, payload={"results": [
        {"title": "A", "url": "https://a.example/1",
         "content": "alpha", "engine": "duckduckgo"},
        {"title": "A-dup", "url": "https://a.example/1#frag",
         "content": "dup", "engine": "bing"},
        {"title": "B", "url": "https://b.example/2",
         "content": "beta", "engine": "brave"},
        {"title": "NoUrl", "content": "skipped", "engine": "brave"},
    ]})
    _stub_serper(monkeypatch, calls)
    results = search_provider.search_web(_unique())
    assert calls["get"] == 1
    assert calls["post"] == 0  # Serper never called on success
    assert [r["title"] for r in results] == ["A", "B"]
    first = results[0]
    assert first["url"] == "https://a.example/1"
    assert first["snippet"] == "alpha"
    assert first["source"] == "duckduckgo"
    assert first["domain"] == "a.example"
    assert first["category"] == "web"
    assert first["retrieved_at"]


def test_searxng_images_mapping_and_skip_rule(monkeypatch, searxng_env):
    calls = searxng_env
    _stub_searxng(monkeypatch, calls, payload={"results": [
        {"title": "Img", "url": "https://page.example/x",
         "img_src": "https://img.example/x.jpg", "engine": "bing"},
        {"title": "NoImg", "url": "https://page.example/y",
         "engine": "bing"},
    ]})
    _stub_serper(monkeypatch, calls)
    results = search_provider.search_images(_unique())
    assert len(results) == 1
    assert results[0]["image_url"] == "https://img.example/x.jpg"
    assert results[0]["url"] == "https://img.example/x.jpg"
    assert results[0]["source_url"] == "https://page.example/x"
    assert results[0]["category"] == "images"


def test_searxng_videos_mapping(monkeypatch, searxng_env):
    calls = searxng_env
    _stub_searxng(monkeypatch, calls, payload={"results": [
        {"title": "Vid", "url": "https://v.example/1",
         "content": "s", "engine": "youtube",
         "publishedDate": "2026-01-01", "length": "3:12"},
    ]})
    _stub_serper(monkeypatch, calls)
    results = search_provider.search_videos(_unique())
    assert len(results) == 1
    assert results[0]["video_url"] == "https://v.example/1"
    assert results[0]["duration"] == "3:12"
    assert results[0]["date"] == "2026-01-01"


def test_fallback_on_transport_error(monkeypatch, searxng_env):
    calls = searxng_env

    def _get(*a, **k):
        calls["get"] += 1
        raise requests.ConnectionError("instance down")

    monkeypatch.setattr(search_provider.requests, "get", _get)
    _stub_serper(monkeypatch, calls)
    run_metrics.begin_run(f"fb-{uuid.uuid4()}")
    results = search_provider.search_web(_unique())
    assert results == [{
        "title": "ST", "url": "https://s.example/1",
        "snippet": "SS", "source": "google_serper",
    }]
    snap = run_metrics.snapshot()
    assert snap["search_calls"]["fallback_events"] == 1
    assert snap["search_calls"]["searxng_failures"] == 1
    assert snap["serper_calls"]["total"] == 1


def test_fallback_on_empty_results(monkeypatch, searxng_env):
    calls = searxng_env
    _stub_searxng(monkeypatch, calls, payload={"results": []})
    _stub_serper(monkeypatch, calls)
    run_metrics.begin_run(f"fb-{uuid.uuid4()}")
    results = search_provider.search_web(_unique())
    assert len(results) == 1
    assert results[0]["source"] == "google_serper"
    snap = run_metrics.snapshot()
    assert snap["search_calls"]["fallback_events"] == 1
    # An empty result is unusable, not a provider failure.
    assert snap["search_calls"]["searxng_failures"] == 0


def test_fallback_on_malformed_body(monkeypatch, searxng_env):
    calls = searxng_env
    _stub_searxng(monkeypatch, calls, payload={"unexpected": True})
    _stub_serper(monkeypatch, calls)
    results = search_provider.search_web(_unique())
    assert results[0]["source"] == "google_serper"


def test_fallback_disabled_raises_primary_error(
    monkeypatch, searxng_env
):
    calls = searxng_env
    monkeypatch.setenv("CINEMORA_SEARCH_FALLBACK", "0")

    def _get(*a, **k):
        calls["get"] += 1
        raise requests.ConnectionError("instance down")

    monkeypatch.setattr(search_provider.requests, "get", _get)
    _stub_serper(monkeypatch, calls)
    with pytest.raises(search_provider.SearchProviderError) as exc:
        search_provider.search_web(_unique())
    assert exc.value.kind == "upstream"
    assert calls["post"] == 0


def test_circuit_breaker_stops_hammering(monkeypatch, searxng_env):
    calls = searxng_env

    def _get(*a, **k):
        calls["get"] += 1
        raise requests.ConnectionError("instance down")

    monkeypatch.setattr(search_provider.requests, "get", _get)
    _stub_serper(monkeypatch, calls)
    search_provider.search_web(_unique())
    assert calls["get"] == 1
    # Breaker is now open: the second query must not touch the
    # instance at all, but still be served by the fallback.
    search_provider.search_web(_unique())
    assert calls["get"] == 1
    assert calls["post"] == 2


def test_serper_mode_ignores_searxng(monkeypatch, searxng_env):
    calls = searxng_env
    monkeypatch.setenv("CINEMORA_SEARCH_PROVIDER", "serper")
    _stub_searxng(monkeypatch, calls, payload={"results": [
        {"title": "X", "url": "https://x.example", "content": "x",
         "engine": "bing"},
    ]})
    _stub_serper(monkeypatch, calls)
    results = search_provider.search_web(_unique())
    assert calls["get"] == 0
    assert results[0]["source"] == "google_serper"


def test_usage_ledger_records_each_provider(monkeypatch, searxng_env):
    calls = searxng_env
    recorded = []
    monkeypatch.setattr(
        provider_usage, "record_usage",
        lambda **kw: recorded.append(kw) or True,
    )
    _stub_searxng(monkeypatch, calls, payload={"results": [
        {"title": "A", "url": "https://a.example/1",
         "content": "alpha", "engine": "duckduckgo"},
    ]})
    _stub_serper(monkeypatch, calls)
    search_provider.search_web(_unique())
    providers = [row["provider"] for row in recorded]
    assert providers == ["searxng"]
    assert recorded[0]["context"]["context"] == "search:web"


def test_health_reports_configuration(monkeypatch, searxng_env):
    calls = searxng_env
    _stub_searxng(monkeypatch, calls, payload={"results": []})
    health = search_provider.search_health()
    assert health["primary"] == "searxng"
    assert health["fallback_enabled"] is True
    assert health["searxng"]["configured"] is True
    assert health["searxng"]["ok"] is True
    assert health["serper"]["configured"] is True


def test_health_without_searxng_url(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    health = search_provider.search_health()
    assert health["primary"] == "serper"
    assert health["searxng"]["configured"] is False
    assert health["searxng"]["ok"] is False
