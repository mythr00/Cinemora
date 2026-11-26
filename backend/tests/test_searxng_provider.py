"""SearXNG — the sole search provider (Master Implementation
2026-11-26; founder decision 2026-11-26 removing the former
paid provider completely).

Hermetic: HTTP is stubbed at requests.get; no network, no real
instance. Real-instance behaviour is covered by the validation
evidence in the master-implementation audit directory, not here.

Pins:
- SearXNG normalisation into the established result shapes,
  with duplicate-URL removal and retained source metadata.
- SearXNG is the ONLY provider: every consumer-facing search
  routes to the configured instance; no fallback exists — the
  removed provider's key and the old routing env vars have no
  effect, and the module exposes no fallback machinery.
- Failure surfaces as the typed SearchProviderError (never a
  silent switch, never fabricated results); an empty result set
  from a healthy instance is an honest empty answer; an
  unconfigured deployment raises kind=not_configured.
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
    monkeypatch.setenv("SEARXNG_MAX_ATTEMPTS", "1")
    return {"get": 0, "hosts": []}


def _unique():
    return f"searxng-probe-{uuid.uuid4()}"


def _stub_searxng(monkeypatch, calls, payload=None, status=200):
    def _get(url, params=None, headers=None, timeout=None):
        calls["get"] += 1
        calls["hosts"].append(url)
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
    results = search_provider.search_web(_unique())
    assert calls["get"] == 1
    # The only host ever contacted is the configured instance.
    assert all(
        h.startswith("http://searxng.test:8888") for h in calls["hosts"]
    )
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
    results = search_provider.search_videos(_unique())
    assert len(results) == 1
    assert results[0]["video_url"] == "https://v.example/1"
    assert results[0]["duration"] == "3:12"
    assert results[0]["date"] == "2026-01-01"


def test_failure_surfaces_as_typed_error_no_fallback(
    monkeypatch, searxng_env
):
    calls = searxng_env

    def _get(*a, **k):
        calls["get"] += 1
        raise requests.ConnectionError("instance down")

    monkeypatch.setattr(search_provider.requests, "get", _get)
    run_metrics.begin_run(f"fail-{uuid.uuid4()}")
    with pytest.raises(search_provider.SearchProviderError) as exc:
        search_provider.search_web(_unique())
    assert exc.value.kind == "upstream"
    snap = run_metrics.snapshot()
    assert snap["search_calls"]["searxng_failures"] == 1
    # No other provider exists to record calls against.
    assert set(snap["search_calls"]) == {"searxng", "searxng_failures"}


def test_empty_results_are_an_honest_empty_answer(
    monkeypatch, searxng_env
):
    calls = searxng_env
    _stub_searxng(monkeypatch, calls, payload={"results": []})
    run_metrics.begin_run(f"empty-{uuid.uuid4()}")
    assert search_provider.search_web(_unique()) == []
    snap = run_metrics.snapshot()
    # An empty result is a legitimate answer, not a failure.
    assert snap["search_calls"]["searxng_failures"] == 0


def test_rate_limit_status_types_as_rate_limit(monkeypatch, searxng_env):
    calls = searxng_env
    _stub_searxng(monkeypatch, calls, payload={}, status=429)
    with pytest.raises(search_provider.SearchProviderError) as exc:
        search_provider.search_web(_unique())
    assert exc.value.kind == "rate_limit"
    assert exc.value.status_code == 429


def test_malformed_body_raises(monkeypatch, searxng_env):
    calls = searxng_env
    _stub_searxng(monkeypatch, calls, payload={"unexpected": True})
    with pytest.raises(search_provider.SearchProviderError) as exc:
        search_provider.search_web(_unique())
    assert exc.value.kind == "upstream"


def test_unconfigured_raises_not_configured(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    assert search_provider.searxng_configured() is False
    with pytest.raises(search_provider.SearchProviderError) as exc:
        search_provider.search_web(_unique())
    assert exc.value.kind == "not_configured"


def test_circuit_breaker_stops_hammering(monkeypatch, searxng_env):
    calls = searxng_env

    def _get(*a, **k):
        calls["get"] += 1
        raise requests.ConnectionError("instance down")

    monkeypatch.setattr(search_provider.requests, "get", _get)
    with pytest.raises(search_provider.SearchProviderError):
        search_provider.search_web(_unique())
    assert calls["get"] == 1
    # Breaker is now open: the second query must not touch the
    # instance at all — and still fails honestly (no fallback).
    with pytest.raises(search_provider.SearchProviderError):
        search_provider.search_web(_unique())
    assert calls["get"] == 1


def test_removed_provider_configuration_has_no_effect(
    monkeypatch, searxng_env
):
    # The removed provider's key and the old routing switches are
    # inert: even with all of them set, search routes only to
    # SearXNG, and the module exposes no fallback machinery.
    calls = searxng_env
    monkeypatch.setenv("SERPER_API_KEY", "should-be-ignored")
    monkeypatch.setenv("CINEMORA_SEARCH_PROVIDER", "serper")
    monkeypatch.setenv("CINEMORA_SEARCH_FALLBACK", "1")
    assert not hasattr(search_provider, "_record_fallback")
    assert not hasattr(search_provider, "_serper_search")
    assert not hasattr(search_provider, "_post_with_retry")
    _stub_searxng(monkeypatch, calls, payload={"results": [
        {"title": "X", "url": "https://x.example", "content": "x",
         "engine": "bing"},
    ]})
    results = search_provider.search_web(_unique())
    assert results[0]["title"] == "X"
    assert all(
        h.startswith("http://searxng.test:8888") for h in calls["hosts"]
    )


def test_usage_ledger_records_the_sole_provider(
    monkeypatch, searxng_env
):
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
    search_provider.search_web(_unique())
    providers = [row["provider"] for row in recorded]
    assert providers == ["searxng"]
    assert recorded[0]["context"]["context"] == "search:web"


def test_health_reports_the_sole_provider(monkeypatch, searxng_env):
    calls = searxng_env
    _stub_searxng(monkeypatch, calls, payload={"results": []})
    health = search_provider.search_health()
    assert health["primary"] == "searxng"
    assert set(health) == {"primary", "searxng"}
    assert health["searxng"]["configured"] is True
    assert health["searxng"]["ok"] is True


def test_health_without_searxng_url(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    health = search_provider.search_health()
    assert health["primary"] == "searxng"
    assert health["searxng"]["configured"] is False
    assert health["searxng"]["ok"] is False
