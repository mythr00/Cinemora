"""llm_client robustness: reasoning-model responses with no text block.

Live evidence (2026-11-19): the BrewKeg proxy's model intermittently
returns a successful response whose content is a single "thinking"
block (having spent the whole output budget reasoning), with no
"text" block. llm_text must retry a bounded number of times instead
of failing the whole story analysis on the first such response.
"""

import pytest

from services import llm_client


THINKING_ONLY = {
    "content": [{"type": "thinking", "thinking": "reasoning..."}],
    "stop_reason": "max_tokens",
    "usage": {"output_tokens": 16000},
}

WITH_TEXT = {
    "content": [{"type": "text", "text": '{"entities": []}'}],
    "stop_reason": "end_turn",
}


def _sequence(monkeypatch, responses):
    calls = {"n": 0}

    def fake_request(*args, **kwargs):
        calls["n"] += 1
        return responses[min(calls["n"] - 1, len(responses) - 1)]

    monkeypatch.setattr(llm_client, "_request", fake_request)
    return calls


def test_thinking_only_response_is_retried(monkeypatch):
    calls = _sequence(monkeypatch, [THINKING_ONLY, WITH_TEXT])
    assert llm_client.llm_text("sys", "user") == '{"entities": []}'
    assert calls["n"] == 2


def test_persistent_thinking_only_responses_raise(monkeypatch):
    calls = _sequence(monkeypatch, [THINKING_ONLY])
    with pytest.raises(RuntimeError, match="no text output"):
        llm_client.llm_text("sys", "user")
    assert calls["n"] == 3  # bounded: exactly 3 attempts


def test_text_response_returns_immediately(monkeypatch):
    calls = _sequence(monkeypatch, [WITH_TEXT])
    assert llm_client.llm_text("sys", "user") == '{"entities": []}'
    assert calls["n"] == 1


TRUNCATED_TEXT = {
    "content": [{"type": "text", "text": '{"entities": [{"id": "ber'}],
    "stop_reason": "max_tokens",
}


def test_truncated_text_is_not_success_and_budget_escalates(monkeypatch):
    budgets = []

    def fake_request(*args, **kwargs):
        budgets.append(kwargs.get("max_tokens"))
        return TRUNCATED_TEXT if len(budgets) == 1 else WITH_TEXT

    monkeypatch.setattr(llm_client, "_request", fake_request)
    assert llm_client.llm_text("sys", "user") == '{"entities": []}'
    assert budgets == [16000, 32000]  # escalated, not repeated blindly


def test_exhausted_truncation_raises_typed_error(monkeypatch):
    calls = _sequence(monkeypatch, [TRUNCATED_TEXT])
    with pytest.raises(llm_client.LLMResponseError) as excinfo:
        llm_client.llm_text("sys", "user")
    assert excinfo.value.kind == "truncated"
    assert calls["n"] == 3


def test_no_text_error_kind_is_explicit(monkeypatch):
    _sequence(monkeypatch, [THINKING_ONLY])
    with pytest.raises(llm_client.LLMResponseError) as excinfo:
        llm_client.llm_text("sys", "user")
    assert excinfo.value.kind == "no_text"


def test_empty_content_is_classified_no_text(monkeypatch):
    _sequence(monkeypatch, [{"content": [], "stop_reason": "end_turn"}])
    with pytest.raises(llm_client.LLMResponseError) as excinfo:
        llm_client.llm_text("sys", "user")
    assert excinfo.value.kind == "no_text"


class _FakeResponse:
    def __init__(self, *, ok=True, status_code=200, payload=None, text=None):
        self.ok = ok
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else "{}"

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_request_disables_thinking_and_bounds_timeout(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        captured["timeout"] = timeout
        return _FakeResponse(payload=WITH_TEXT)

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    assert llm_client.llm_text("sys", "user") == '{"entities": []}'
    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert captured["timeout"] == llm_client.REQUEST_TIMEOUT_SECONDS


def test_malformed_body_raises_typed_error(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(payload=ValueError("no json"), text="<html>oops")

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    with pytest.raises(llm_client.LLMResponseError) as excinfo:
        llm_client.llm_text("sys", "user")
    assert excinfo.value.kind == "malformed"


def test_provider_http_error_propagates_without_content_retry(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return _FakeResponse(ok=False, status_code=401, text="unauthorized")

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    with pytest.raises(RuntimeError) as excinfo:
        llm_client.llm_text("sys", "user")
    # A genuine provider error: NOT reclassified as a content problem,
    # and not retried at the llm_text level (401 is not retryable).
    assert not isinstance(excinfo.value, llm_client.LLMResponseError)
    assert calls["n"] == 1
