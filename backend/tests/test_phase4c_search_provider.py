"""Phase 4c / D-08 — search-provider consolidation.

- services.search_provider is the sole live Serper client; the
  duplicate services.asset_finder client is proven dead (zero
  importers — asserted below against the module's absence) and
  removed.
- Serper failures are typed (SearchProviderError kinds following
  the llm_provider taxonomy style): not_configured / quota /
  auth / rate_limit / upstream.
- HTTP is stubbed at requests.post, exactly as the pre-existing
  Serper tests stub it; no network.
"""

import importlib
import uuid

import pytest
import requests

from services import search_provider


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._payload


def _stub_post(monkeypatch, response):
    monkeypatch.setattr(
        search_provider.requests, "post", lambda *a, **k: response
    )


def _unique_query():
    return f"phase4c-probe-{uuid.uuid4()}"


def test_asset_finder_dead_client_is_gone():
    # D-08 census: asset_finder had zero importers/callers in the
    # whole repo (backend, frontend, workflows, docs) and zero test
    # dependence; it is removed, not deprecated in place.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("services.asset_finder")


def test_missing_key_is_typed_not_configured(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.setattr(search_provider, "SERPER_API_KEY", None)
    with pytest.raises(search_provider.SearchProviderError) as excinfo:
        search_provider.check_api_key()
    assert excinfo.value.kind == "not_configured"
    # Backwards compatibility: still a RuntimeError, as before.
    assert isinstance(excinfo.value, RuntimeError)


def test_auth_failure_is_typed_and_not_retried(monkeypatch):
    calls = {"n": 0}

    def _post(*a, **k):
        calls["n"] += 1
        return _Response(401, text="invalid key")

    monkeypatch.setattr(search_provider.requests, "post", _post)
    with pytest.raises(search_provider.SearchProviderError) as excinfo:
        search_provider._post_with_retry("search", _unique_query())
    assert excinfo.value.kind == "auth"
    assert excinfo.value.status_code == 401
    assert calls["n"] == 1  # non-retryable: raised at the first response


def test_quota_failure_is_typed(monkeypatch):
    _stub_post(monkeypatch, _Response(402, text="quota exhausted"))
    with pytest.raises(search_provider.SearchProviderError) as excinfo:
        search_provider._post_with_retry("search", _unique_query())
    assert excinfo.value.kind == "quota"
    assert excinfo.value.status_code == 402


def test_rate_limit_after_bounded_retries_is_typed(monkeypatch):
    monkeypatch.setattr(search_provider, "SERPER_MAX_ATTEMPTS", 2)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    calls = {"n": 0}

    def _post(*a, **k):
        calls["n"] += 1
        return _Response(429, text="slow down")

    monkeypatch.setattr(search_provider.requests, "post", _post)
    with pytest.raises(search_provider.SearchProviderError) as excinfo:
        search_provider._post_with_retry("search", _unique_query())
    assert excinfo.value.kind == "rate_limit"
    assert excinfo.value.status_code == 429
    assert calls["n"] == 2  # bounded attempts, then the typed raise


def test_upstream_after_bounded_retries_is_typed(monkeypatch):
    monkeypatch.setattr(search_provider, "SERPER_MAX_ATTEMPTS", 2)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    _stub_post(monkeypatch, _Response(503, text="unavailable"))
    with pytest.raises(search_provider.SearchProviderError) as excinfo:
        search_provider._post_with_retry("videos", _unique_query())
    assert excinfo.value.kind == "upstream"
    assert excinfo.value.status_code == 503


def test_transport_failure_is_typed_upstream(monkeypatch):
    monkeypatch.setattr(search_provider, "SERPER_MAX_ATTEMPTS", 1)

    def _post(*a, **k):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(search_provider.requests, "post", _post)
    with pytest.raises(search_provider.SearchProviderError) as excinfo:
        search_provider._post_with_retry("search", _unique_query())
    assert excinfo.value.kind == "upstream"
    assert excinfo.value.status_code is None


def test_success_path_and_result_shape_unchanged(monkeypatch):
    _stub_post(monkeypatch, _Response(200, payload={
        "organic": [{"title": "T", "link": "https://example.com/x",
                     "snippet": "S"}],
    }))
    results = search_provider.search_web(_unique_query())
    assert results == [{
        "title": "T", "url": "https://example.com/x",
        "snippet": "S", "source": "google_serper",
    }]


def test_request_uses_call_time_key_header(monkeypatch):
    seen = {}

    def _post(url, headers=None, json=None, timeout=None):
        seen["headers"] = headers
        return _Response(200, payload={"organic": []})

    monkeypatch.setenv("SERPER_API_KEY", "call-time-key")
    monkeypatch.setattr(search_provider.requests, "post", _post)
    search_provider.search_web(_unique_query())
    assert seen["headers"]["X-API-KEY"] == "call-time-key"
