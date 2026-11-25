"""Provider wire contracts (GitHub Skills Programme, Lane 5 — F-03).

The Phase 4c / llm_provider suites pin failure typing and the
thinking-disabled rule. What they do NOT pin — and what silently
breaks productions when a provider changes its wire format — is
the exact request shape and the response field mapping. These
tests pin that surface hermetically (requests.post stubbed, no
network, no keys: sentinel values only):

* Serper: per-endpoint URL, {"q": query} payload, and the
  organic/images/videos field mappings incl. skip rules.
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


# --- Serper ------------------------------------------------------------

@pytest.fixture()
def serper_env(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "contract-key")
    seen = {}

    def _capture(response):
        def _post(url, headers=None, json=None, timeout=None):
            seen.update(url=url, headers=headers, json=json, timeout=timeout)
            return response
        return _post

    return seen, _capture


def _unique():
    return f"contract-{uuid.uuid4()}"


def test_serper_web_request_shape_and_parse(monkeypatch, serper_env):
    seen, capture = serper_env
    payload = {"organic": [
        {"title": "T1", "link": "https://a.example/1", "snippet": "S1"},
        {"title": "T2", "link": "https://a.example/2", "snippet": "S2"},
    ]}
    monkeypatch.setattr(
        search_provider.requests, "post", capture(_Response(200, payload)))
    query = _unique()
    results = search_provider.search_web(query)
    assert seen["url"] == "https://google.serper.dev/search"
    assert seen["json"] == {"q": query}
    assert seen["headers"]["X-API-KEY"] == "contract-key"
    assert seen["headers"]["Content-Type"] == "application/json"
    assert results == [
        {"title": "T1", "url": "https://a.example/1", "snippet": "S1",
         "source": "google_serper"},
        {"title": "T2", "url": "https://a.example/2", "snippet": "S2",
         "source": "google_serper"},
    ]


def test_serper_images_mapping_and_skip_rule(monkeypatch, serper_env):
    seen, capture = serper_env
    payload = {"images": [
        {"title": "Img", "imageUrl": "https://img.example/x.jpg",
         "link": "https://page.example/x", "source": "cdn.example"},
        {"title": "NoUrl", "link": "https://page.example/y"},
    ]}
    monkeypatch.setattr(
        search_provider.requests, "post", capture(_Response(200, payload)))
    results = search_provider.search_images(_unique())
    assert seen["url"] == "https://google.serper.dev/images"
    # The entry without imageUrl is skipped; imageUrl maps to both
    # image_url and url, link maps to source_url.
    assert len(results) == 1
    assert results[0]["image_url"] == "https://img.example/x.jpg"
    assert results[0]["url"] == "https://img.example/x.jpg"
    assert results[0]["source_url"] == "https://page.example/x"
    assert results[0]["source"] == "cdn.example"


def test_serper_videos_mapping_and_skip_rule(monkeypatch, serper_env):
    seen, capture = serper_env
    payload = {"videos": [
        {"title": "Vid", "link": "https://v.example/1", "snippet": "s",
         "date": "2026-01-01", "duration": "3:12", "source": "YouTube"},
        {"title": "NoLink", "snippet": "skipped"},
    ]}
    monkeypatch.setattr(
        search_provider.requests, "post", capture(_Response(200, payload)))
    results = search_provider.search_videos(_unique())
    assert seen["url"] == "https://google.serper.dev/videos"
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
