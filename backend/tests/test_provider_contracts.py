"""Provider wire contracts (GitHub Skills Programme, Lane 5 — F-03).

The Phase 4c / llm_provider suites pin failure typing and the
thinking-disabled rule. What they do NOT pin — and what silently
breaks productions when a provider changes its wire format — is
the exact request shape and the response field mapping. These
tests pin that surface hermetically (requests stubbed, no
network, no keys: sentinel values only):

* SearXNG: the instance /search URL, the q/format/categories
  query parameters per kind, and the results field mappings
  incl. skip rules and duplicate removal.
* BrewKeg: {base}/messages URL, Bearer header, payload shape,
  and content-block text extraction (thinking blocks skipped,
  text blocks joined, usage/stop_reason passthrough).
"""

import json as _json
import uuid

import pytest

from services import llm_provider, search_provider


class _Response:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload or {}
        # BrewKeg's client rejects an empty body before parsing —
        # a realistic fake carries the JSON text, like the wire.
        self.text = text if text is not None else _json.dumps(self._payload)
        self.headers = {}
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


# --- SearXNG -----------------------------------------------------------

@pytest.fixture()
def searxng_env(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "http://searxng.test:8888")
    seen = {}

    def _capture(response):
        def _get(url, params=None, headers=None, timeout=None):
            seen.update(url=url, params=params, headers=headers,
                        timeout=timeout)
            return response
        return _get

    return seen, _capture


def _unique():
    return f"contract-{uuid.uuid4()}"


def test_searxng_web_request_shape_and_parse(monkeypatch, searxng_env):
    seen, capture = searxng_env
    payload = {"results": [
        {"title": "T1", "url": "https://a.example/1", "content": "S1",
         "engine": "google"},
        {"title": "T2", "url": "https://a.example/2", "content": "S2",
         "engine": "bing"},
    ]}
    monkeypatch.setattr(
        search_provider.requests, "get", capture(_Response(200, payload)))
    query = _unique()
    results = search_provider.search_web(query)
    assert seen["url"] == "http://searxng.test:8888/search"
    assert seen["params"]["q"] == query
    assert seen["params"]["format"] == "json"
    assert seen["params"]["categories"] == "general"
    # No API key exists for SearXNG: no credential header is sent.
    assert "X-API-KEY" not in (seen["headers"] or {})
    assert [
        {k: r[k] for k in ("title", "url", "snippet", "source")}
        for r in results
    ] == [
        {"title": "T1", "url": "https://a.example/1", "snippet": "S1",
         "source": "google"},
        {"title": "T2", "url": "https://a.example/2", "snippet": "S2",
         "source": "bing"},
    ]
    assert results[0]["domain"] == "a.example"
    assert results[0]["category"] == "web"
    assert results[0]["retrieved_at"]


def test_searxng_images_mapping_and_skip_rule(monkeypatch, searxng_env):
    seen, capture = searxng_env
    payload = {"results": [
        {"title": "Img", "img_src": "https://img.example/x.jpg",
         "url": "https://page.example/x", "source": "cdn.example",
         "engine": "bing"},
        {"title": "NoImg", "url": "https://page.example/y",
         "engine": "bing"},
    ]}
    monkeypatch.setattr(
        search_provider.requests, "get", capture(_Response(200, payload)))
    results = search_provider.search_images(_unique())
    assert seen["params"]["categories"] == "images"
    # The entry without img_src is skipped; img_src maps to both
    # image_url and url, url maps to source_url.
    assert len(results) == 1
    assert results[0]["image_url"] == "https://img.example/x.jpg"
    assert results[0]["url"] == "https://img.example/x.jpg"
    assert results[0]["source_url"] == "https://page.example/x"
    assert results[0]["source"] == "cdn.example"


def test_searxng_videos_mapping_and_skip_rule(monkeypatch, searxng_env):
    seen, capture = searxng_env
    payload = {"results": [
        {"title": "Vid", "url": "https://v.example/1", "content": "s",
         "publishedDate": "2026-01-01", "length": "3:12",
         "engine": "youtube"},
        {"title": "NoUrl", "content": "skipped"},
    ]}
    monkeypatch.setattr(
        search_provider.requests, "get", capture(_Response(200, payload)))
    results = search_provider.search_videos(_unique())
    assert seen["params"]["categories"] == "videos"
    assert len(results) == 1
    assert results[0]["video_url"] == "https://v.example/1"
    assert results[0]["url"] == "https://v.example/1"
    assert results[0]["duration"] == "3:12"
    assert results[0]["date"] == "2026-01-01"


# --- BrewKeg -----------------------------------------------------------

_SENTINEL_KEY = "contract" + "-key"


def _provider():
    return llm_provider.BrewKegProvider(
        _SENTINEL_KEY, "https://brewkeg.invalid/", "contract-model"
    )


def test_brewkeg_request_shape(monkeypatch):
    seen = {}

    def _post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, json=json, timeout=timeout)
        return _Response(200, {
            "content": [{"type": "text", "text": "answer"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    monkeypatch.setattr(llm_provider.requests, "post", _post)
    completion = _provider().complete(
        [{"role": "user", "content": "question"}],
        system="be terse", temperature=0.2, max_tokens=1234,
    )
    assert seen["url"] == "https://brewkeg.invalid/messages"
    assert seen["headers"]["Authorization"] == "Bearer contract-key"
    assert seen["headers"]["Content-Type"] == "application/json"
    payload = seen["json"]
    assert payload["model"] == "contract-model"
    assert payload["system"] == "be terse"
    assert payload["messages"] == [{"role": "user", "content": "question"}]
    assert payload["max_tokens"] == 1234
    assert payload["temperature"] == 0.2
    assert payload["thinking"] == {"type": "disabled"}
    assert completion.text == "answer"
    assert completion.stop_reason == "end_turn"
    assert completion.usage == {"input_tokens": 10, "output_tokens": 5}
    assert completion.provider == "brewkeg"


def test_brewkeg_extract_text_skips_thinking_and_joins_text():
    data = {"content": [
        {"type": "thinking", "thinking": "internal reasoning"},
        {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
        {"type": "tool_use", "name": "x"},
    ]}
    assert llm_provider._extract_text(data) == "first\nsecond"
    # The OpenAI-Responses compatibility shape.
    assert llm_provider._extract_text({"output_text": " alt "}) == "alt"
    assert llm_provider._extract_text({"content": []}) == ""


def test_brewkeg_unconfigured_raises_typed_not_configured():
    provider = llm_provider.BrewKegProvider("", "https://brewkeg.invalid", "m")
    with pytest.raises(llm_provider.LLMProviderError) as excinfo:
        provider.complete([{"role": "user", "content": "q"}])
    assert excinfo.value.kind == "not_configured"
