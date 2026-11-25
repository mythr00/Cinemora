"""llm_provider (Phase 4b / D-01) robustness: reasoning-model responses
with no text block. (Pre-D-01 this file tested services.llm_client;
the implementation moved behind the provider interface and the
tests moved with it — llm_client is now a compatibility shim,
covered by test_llm_client_shim_still_delegates below.)

Live evidence (2026-11-19): the BrewKeg proxy's model intermittently
returns a successful response whose content is a single "thinking"
block (having spent the whole output budget reasoning), with no
"text" block. complete_text must retry a bounded number of times instead
of failing the whole story analysis on the first such response.
"""

import pytest

from services import llm_provider


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

    monkeypatch.setattr(llm_provider.BrewKegProvider, "_request", fake_request)
    return calls


def test_thinking_only_response_is_retried(monkeypatch):
    calls = _sequence(monkeypatch, [THINKING_ONLY, WITH_TEXT])
    assert llm_provider.complete_text("sys", "user") == '{"entities": []}'
    assert calls["n"] == 2


def test_persistent_thinking_only_responses_raise(monkeypatch):
    calls = _sequence(monkeypatch, [THINKING_ONLY])
    with pytest.raises(RuntimeError, match="no text output"):
        llm_provider.complete_text("sys", "user")
    assert calls["n"] == 3  # bounded: exactly 3 attempts


def test_text_response_returns_immediately(monkeypatch):
    calls = _sequence(monkeypatch, [WITH_TEXT])
    assert llm_provider.complete_text("sys", "user") == '{"entities": []}'
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

    monkeypatch.setattr(llm_provider.BrewKegProvider, "_request", fake_request)
    assert llm_provider.complete_text("sys", "user") == '{"entities": []}'
    assert budgets == [16000, 32000]  # escalated, not repeated blindly


def test_exhausted_truncation_raises_typed_error(monkeypatch):
    calls = _sequence(monkeypatch, [TRUNCATED_TEXT])
    with pytest.raises(llm_provider.LLMResponseError) as excinfo:
        llm_provider.complete_text("sys", "user")
    assert excinfo.value.kind == "truncated"
    assert calls["n"] == 3


def test_no_text_error_kind_is_explicit(monkeypatch):
    _sequence(monkeypatch, [THINKING_ONLY])
    with pytest.raises(llm_provider.LLMResponseError) as excinfo:
        llm_provider.complete_text("sys", "user")
    assert excinfo.value.kind == "no_text"


def test_empty_content_is_classified_no_text(monkeypatch):
    _sequence(monkeypatch, [{"content": [], "stop_reason": "end_turn"}])
    with pytest.raises(llm_provider.LLMResponseError) as excinfo:
        llm_provider.complete_text("sys", "user")
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

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    assert llm_provider.complete_text("sys", "user") == '{"entities": []}'
    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert captured["timeout"] == llm_provider.REQUEST_TIMEOUT_SECONDS


def test_malformed_body_raises_typed_error(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(payload=ValueError("no json"), text="<html>oops")

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    with pytest.raises(llm_provider.LLMResponseError) as excinfo:
        llm_provider.complete_text("sys", "user")
    assert excinfo.value.kind == "malformed"


def test_provider_http_error_propagates_without_content_retry(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return _FakeResponse(ok=False, status_code=401, text="unauthorized")

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    with pytest.raises(RuntimeError) as excinfo:
        llm_provider.complete_text("sys", "user")
    # A genuine provider error: NOT reclassified as a content problem,
    # and not retried at the complete_text level (401 is not retryable).
    assert not isinstance(excinfo.value, llm_provider.LLMResponseError)
    assert calls["n"] == 1


# ---------------------------------------------------------------------
# Phase 4b / D-01 + failure honesty + D-18 degraded explicitness
# ---------------------------------------------------------------------

from services import llm_client, llm_provider, story_understanding as story
from services.assistant import llm as assistant_llm


def _status_post(status_code, text="provider says no"):
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(ok=False, status_code=status_code, text=text)

    return fake_post


def test_http_402_is_a_typed_billing_error(monkeypatch):
    monkeypatch.setattr(llm_provider.requests, "post", _status_post(402))
    with pytest.raises(llm_provider.LLMProviderError) as excinfo:
        llm_provider.complete_text("sys", "user")
    assert excinfo.value.kind == "billing"
    assert excinfo.value.status_code == 402
    # A billing failure is NOT a response problem and carries no text.
    assert not isinstance(excinfo.value, llm_provider.LLMResponseError)


def test_http_401_is_a_typed_auth_error(monkeypatch):
    monkeypatch.setattr(llm_provider.requests, "post", _status_post(401))
    with pytest.raises(llm_provider.LLMProviderError) as excinfo:
        llm_provider.complete_text("sys", "user")
    assert excinfo.value.kind == "auth"
    assert excinfo.value.status_code == 401


def test_http_429_after_retries_is_a_typed_rate_limit_error(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return _FakeResponse(ok=False, status_code=429, text="slow down")

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    with pytest.raises(llm_provider.LLMProviderError) as excinfo:
        llm_provider.complete_text("sys", "user")
    assert excinfo.value.kind == "rate_limit"
    assert excinfo.value.status_code == 429
    assert calls["n"] == 4  # the pre-D-01 bounded transport retries


def test_http_503_after_retries_is_a_typed_upstream_error(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr(llm_provider.requests, "post", _status_post(503))
    with pytest.raises(llm_provider.LLMProviderError) as excinfo:
        llm_provider.complete_text("sys", "user")
    assert excinfo.value.kind == "upstream"
    assert excinfo.value.status_code == 503


def test_missing_key_is_a_typed_not_configured_error(monkeypatch):
    monkeypatch.setattr(llm_provider, "BREWKEG_API_KEY", "")
    with pytest.raises(llm_provider.LLMProviderError) as excinfo:
        llm_provider.complete_text("sys", "user")
    assert excinfo.value.kind == "not_configured"
    assert "BREWKEG_API_KEY is not configured" in str(excinfo.value)


def test_completion_carries_text_usage_and_model(monkeypatch):
    payload = dict(WITH_TEXT)
    payload["usage"] = {"input_tokens": 11, "output_tokens": 7}

    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(payload=payload)

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    completion = llm_provider.get_provider().complete(
        [{"role": "user", "content": "user"}], system="sys"
    )
    assert completion.text == '{"entities": []}'
    assert completion.usage == {"input_tokens": 11, "output_tokens": 7}
    assert completion.provider == "brewkeg"
    assert completion.model == llm_provider.BREWKEG_MODEL


def test_llm_client_shim_still_delegates(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(payload=WITH_TEXT)

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    assert llm_client.llm_text("sys", "user") == '{"entities": []}'
    assert llm_client.BREWKEG_MODEL == llm_provider.BREWKEG_MODEL
    assert llm_client.LLMProviderError is llm_provider.LLMProviderError


def test_script_studio_maps_402_to_502_never_a_draft(monkeypatch):
    from services import script_studio

    monkeypatch.setattr(llm_provider, "BREWKEG_API_KEY", "test-key")

    def billing(system, user, **kwargs):
        raise llm_provider.LLMProviderError(
            "billing", "BrewKeg request failed (HTTP 402): quota",
            status_code=402,
        )

    with pytest.raises(script_studio.ScriptError) as excinfo:
        script_studio._ai_call(billing, "sys", "user")
    assert excinfo.value.status_code == 502
    assert excinfo.value.code == "script_ai_failed"


def test_assistant_unavailable_carries_billing_kind(monkeypatch):
    def billing(system, user, **kwargs):
        raise llm_provider.LLMProviderError(
            "billing", "BrewKeg request failed (HTTP 402): quota",
            status_code=402,
        )

    monkeypatch.setattr(llm_provider, "BREWKEG_API_KEY", "test-key")
    monkeypatch.setattr(llm_provider, "complete_json", billing)
    with pytest.raises(assistant_llm.AssistantUnavailable) as excinfo:
        assistant_llm.complete_json("sys", "payload")
    assert excinfo.value.reason == "llm_unavailable"
    assert excinfo.value.provider_error_kind == "billing"


_BILLING_SENTENCES = [
    "Ada Lovelace designed an engine.",
    "She published notes on it.",
    "The engine was never built.",
]


def test_402_engages_degraded_planner_with_billing_marker():
    """D-18: a 402 does not silently become the product — the plan
    degrades (the fallback is kept) AND the bible's planner block
    records the degraded marker with its billing cause class."""

    def llm(system, user):
        raise llm_provider.LLMProviderError(
            "billing", "BrewKeg request failed (HTTP 402): quota",
            status_code=402,
        )

    bible, plans, mode = story.plan_script(
        " ".join(_BILLING_SENTENCES), _BILLING_SENTENCES, llm,
        cache_path=None,
    )
    assert mode == "degraded"
    block = bible["planner"]
    assert block["degraded_reason"] == "provider_billing"
    assert block["failure_kind"] == "billing"
    assert block["degraded_sentences"] == len(_BILLING_SENTENCES)
    assert all(plan["degraded"] for plan in plans)


def test_failure_reason_taxonomy_covers_all_provider_kinds():
    cases = {
        "not_configured": "no_key",
        "billing": "provider_billing",
        "auth": "provider_auth",
        "rate_limit": "provider_rate_limit",
        "upstream": "provider_failure",
    }
    for kind, reason in cases.items():
        error = llm_provider.LLMProviderError(kind, "boom")
        assert story._llm_failure_reason(error) == reason, kind
