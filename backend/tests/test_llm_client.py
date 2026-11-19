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
