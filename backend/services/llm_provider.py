"""LLM provider architecture (Master Project Directive Phase 4b / D-01).

ONE seam between Cinemora and any large-language-model provider:

- :class:`LLMProvider` is the interface every consumer codes
  against: ``complete(messages, ...) -> Completion`` plus the
  ``complete_text`` / ``complete_json`` conveniences built on it.
  A Completion carries the text, the provider-reported usage and
  the model that answered — nothing wire-shaped ever leaves this
  module.
- :class:`BrewKegProvider` is the single concrete provider: the
  BrewKeg gateway, Anthropic-shaped wire format (``POST /messages``,
  ``content[]`` text blocks, thinking disabled), configured by env
  exactly as the pre-D-01 client was (``BREWKEG_API_KEY``,
  ``BREWKEG_BASE_URL``, ``BREWKEG_MODEL``). Switching providers is
  now a new class behind this interface, not a rewrite of every
  consumer.
- Failures are TYPED (Phase 4b failure honesty): provider-side
  failures raise :class:`LLMProviderError` with a ``kind`` —
  ``not_configured`` / ``billing`` (HTTP 402) / ``auth`` (401/403) /
  ``rate_limit`` (429, after the bounded transport retries) /
  ``upstream`` (5xx after retries, other HTTP, transport errors).
  A provider that answered but whose answer is unusable raises
  :class:`LLMResponseError` (``no_text`` / ``truncated`` / ``empty``
  / ``malformed`` / ``invalid_json``). Neither is ever converted
  into fabricated content by this layer or its consumers.

``services.llm_client`` remains as a thin compatibility shim over
this module; new code imports this module directly.
"""

import json
import os
import time
from dataclasses import dataclass, field

import requests
from dotenv import load_dotenv


load_dotenv(override=True)


# ---------------------------------------------------------------------
# Configuration (env-driven, identical to the pre-D-01 client)
# ---------------------------------------------------------------------

BREWKEG_API_KEY = os.getenv("BREWKEG_API_KEY")

BREWKEG_BASE_URL = os.getenv(
    "BREWKEG_BASE_URL",
    "https://brewkeg.dev/v1",
).rstrip("/")

BREWKEG_MODEL = os.getenv(
    "BREWKEG_MODEL",
    "gpt-5-mini",
)

# Bounded per-request timeout. Legitimate planner calls complete in
# well under a minute with thinking disabled (see BrewKegProvider);
# the old timeout=None could hang a production job forever.
REQUEST_TIMEOUT_SECONDS = 300


# ---------------------------------------------------------------------
# Typed failures
# ---------------------------------------------------------------------


class LLMProviderError(RuntimeError):
    """The provider could not serve the call at all.

    ``kind`` is one of ``not_configured``, ``billing``, ``auth``,
    ``rate_limit``, ``upstream``; ``status_code`` is the HTTP status
    when one was received (else None). Consumers branch on the kind
    — e.g. a degraded fallback records ``billing`` as its cause —
    instead of parsing message text.
    """

    KINDS = ("not_configured", "billing", "auth", "rate_limit", "upstream")

    def __init__(self, kind, message, status_code=None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code

    @classmethod
    def for_status(cls, provider_name, status_code, body_text):
        if status_code == 402:
            kind = "billing"
        elif status_code in (401, 403):
            kind = "auth"
        elif status_code == 429:
            kind = "rate_limit"
        else:
            kind = "upstream"
        return cls(
            kind,
            f"{provider_name} request failed "
            f"(HTTP {status_code}): {body_text[:3000]}",
            status_code=status_code,
        )


class LLMResponseError(RuntimeError):
    """The provider answered, but the response itself is unusable.

    kind is one of:
      "no_text"      — no text content (e.g. a reasoning-only
                       response that spent its budget thinking);
      "truncated"    — stop_reason reported the token cap, so any
                       text present is incomplete;
      "empty"        — HTTP 200 with an empty body;
      "malformed"    — the body was not JSON;
      "invalid_json" — the text carried no recoverable JSON object.

    Kept distinct from LLMProviderError so callers and reports can
    tell a bad response apart from a provider outage — and so an
    unusable response is never mistaken for a successful one.
    """

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------


@dataclass
class Completion:
    """One completed provider call: text + usage, nothing wire-shaped."""

    text: str
    usage: dict = field(default_factory=dict)
    model: str | None = None
    provider: str = ""
    stop_reason: str | None = None


class LLMProvider:
    """The provider interface. Concrete providers implement
    :meth:`complete`; text/JSON conveniences live here so every
    provider inherits identical response-honesty behaviour."""

    name = "abstract"

    def __init__(self, model=None):
        self._model = model

    # -- configuration -------------------------------------------------

    @property
    def model(self):
        return self._model

    def configured(self) -> bool:
        raise NotImplementedError

    def _require_configured(self):
        raise NotImplementedError

    # -- the one wire method -------------------------------------------

    def complete(self, messages, *, system=None, model=None,
                 temperature=0.1, max_tokens=16000) -> Completion:
        """Run one completion. ``messages`` is a list of
        {\"role\", \"content\"} dicts; ``system`` is the optional
        system prompt. Returns a Completion; raises LLMProviderError
        / LLMResponseError on failure — never returns invented text."""
        raise NotImplementedError

    # -- conveniences built on complete() -------------------------------

    def complete_text(self, system_prompt, user_prompt, *, model=None,
                      temperature=0.1, max_tokens=16000):
        """One system+user call returning plain text.

        A response counts as successful ONLY when it carries text
        and was not truncated at the token cap. Reasoning-only
        responses (no text) and truncated responses are classified
        explicitly via LLMResponseError and retried a bounded number
        of times with an escalating output budget — never silently
        treated as success."""
        budget = max_tokens
        last_error = None

        for _attempt in range(3):
            completion = self.complete(
                [{"role": "user", "content": str(user_prompt)}],
                system=str(system_prompt),
                model=model,
                temperature=temperature,
                max_tokens=budget,
            )

            if completion.text and completion.stop_reason != "max_tokens":
                return completion.text

            if not completion.text:
                last_error = LLMResponseError(
                    "no_text",
                    f"{self.name} returned no text output.\n"
                    f"stop_reason: {completion.stop_reason}\n"
                    f"usage: {completion.usage}",
                )
            else:
                last_error = LLMResponseError(
                    "truncated",
                    f"{self.name} returned a truncated response "
                    f"(stop_reason=max_tokens at a {budget}-token "
                    "budget); the incomplete text was not treated "
                    "as a successful response.",
                )

            budget = min(budget * 2, 64000)

        raise last_error

    def complete_json(self, system_prompt, user_prompt, *, model=None,
                      temperature=0.0, max_tokens=1600):
        """One system+user call returning parsed JSON.

        Handles markdown fences and JSON embedded in surrounding
        model text. Raises LLMResponseError(\"invalid_json\") when
        no JSON can be recovered — the caller decides what that
        means; nothing is fabricated here."""
        json_system = (
            str(system_prompt)
            + "\n\n"
            "CRITICAL OUTPUT RULES:\n"
            "1. Return ONLY the final JSON object.\n"
            "2. Do NOT return your reasoning or thinking.\n"
            "3. Do NOT use markdown fences.\n"
            "4. Do NOT write anything before or after the JSON.\n"
        )

        text = self.complete_text(
            json_system,
            user_prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        text = str(text or "").strip()

        # Remove markdown fences when present.
        if text.startswith("```"):
            lines = text.splitlines()

            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]

            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]

            text = "\n".join(lines).strip()

        # First try the complete response.
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Then recover JSON embedded inside surrounding model text.
        starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]

        if starts:
            start = min(starts)
            end = max(text.rfind("}"), text.rfind("]"))

            if end > start:
                candidate = text[start:end + 1]

                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass

        raise LLMResponseError(
            "invalid_json",
            f"{self.name} returned invalid JSON:\n" + text[:5000],
        )

    def as_callable(self):
        """This provider as a simple ``(system, user) -> text``
        callable (the story planner's long-standing shape)."""

        def _llm(system_prompt, user_prompt):
            return self.complete_text(
                system_prompt,
                user_prompt,
                temperature=0.0,
                max_tokens=16000,
            )

        return _llm


# ---------------------------------------------------------------------
# The one concrete provider: BrewKeg (Anthropic-shaped wire format)
# ---------------------------------------------------------------------


class BrewKegProvider(LLMProvider):
    """BrewKeg gateway provider. ALL BrewKeg wire specifics live in
    this class: the ``/messages`` endpoint, the Anthropic-shaped
    payload, the ``content[]`` text-block extraction and the
    ``stop_reason`` vocabulary.

    Explicit constructor arguments win; omitted ones fall back to
    the module-level env configuration AT CALL TIME, so deployment
    config (and test monkeypatching of the module constants) is
    honoured without rebuilding the provider.
    """

    name = "brewkeg"

    def __init__(self, api_key=None, base_url=None, model=None):
        super().__init__(model=model)
        self._api_key = api_key
        self._base_url = base_url

    @property
    def api_key(self):
        return self._api_key if self._api_key is not None else BREWKEG_API_KEY

    @property
    def base_url(self):
        raw = self._base_url if self._base_url is not None else BREWKEG_BASE_URL
        return str(raw).rstrip("/")

    @property
    def model(self):
        return self._model if self._model is not None else BREWKEG_MODEL

    def configured(self) -> bool:
        return bool(self.api_key)

    def _require_configured(self):
        # BLOCK-001: the key is required at first USE, not at import.
        if not self.api_key:
            raise LLMProviderError(
                "not_configured", "BREWKEG_API_KEY is not configured"
            )

    # -- raw request (the wire) -----------------------------------------

    def _request(self, system_prompt, user_prompt, *, model=None,
                 temperature=0.1, max_tokens=16000):
        """One raw BrewKeg request, returning the parsed response
        body. Bounded transport retries on 429/5xx; every other
        failure raises the typed LLMProviderError for its class."""
        self._require_configured()

        payload = {
            "model": model or self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": str(system_prompt),
            "messages": [
                {
                    "role": "user",
                    "content": str(user_prompt),
                }
            ],
            # Root-cause fix (diagnosed live, 2026-11-19): the
            # backing model is a reasoning model. Left enabled, its
            # reasoning is returned as a "thinking" content block
            # that consumes the output budget — responses truncate
            # at max_tokens, sometimes before any text block exists
            # at all. Every caller wants a direct structured answer,
            # so thinking is disabled at the source.
            "thinking": {"type": "disabled"},
        }

        last_response = None

        for attempt in range(4):
            # Phase 8.4: platform token bucket at the choke point
            # (the BrewKeg key is shared by every worker) and the
            # usage counter: every real request is counted, with
            # token usage when the gateway reports it.
            try:
                from services import rate_bucket

                rate_bucket.acquire("llm", timeout=30.0)
            except Exception:
                pass
            try:
                response = requests.post(
                    f"{self.base_url}/messages",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                raise LLMProviderError(
                    "upstream",
                    f"BrewKeg request failed (transport): {exc}",
                ) from exc

            last_response = response

            try:
                from services import run_metrics

                _tokens = 0
                if response.ok:
                    try:
                        _usage = response.json().get("usage") or {}
                        _tokens = int(_usage.get("input_tokens") or 0) + int(
                            _usage.get("output_tokens") or 0
                        )
                    except Exception:
                        _tokens = 0
                run_metrics.llm_call(_tokens)
            except Exception:
                pass

            if response.ok:
                break

            if response.status_code not in {429, 500, 502, 503, 504}:
                raise LLMProviderError.for_status(
                    "BrewKeg", response.status_code, response.text
                )

            if attempt < 3:
                time.sleep(3 * (attempt + 1))

        response = last_response

        if not response.ok:
            error = LLMProviderError.for_status(
                "BrewKeg", response.status_code, response.text
            )
            raise LLMProviderError(
                error.kind,
                f"BrewKeg request failed after 4 attempts "
                f"(HTTP {response.status_code}): {response.text[:3000]}",
                status_code=response.status_code,
            )

        if not response.text.strip():
            raise LLMResponseError(
                "empty",
                "BrewKeg returned HTTP 200 but an empty response body.",
            )

        try:
            return response.json()

        except ValueError as exc:
            raise LLMResponseError(
                "malformed",
                "BrewKeg returned non-JSON response:\n"
                + response.text[:3000],
            ) from exc

    # -- interface -------------------------------------------------------

    def complete(self, messages, *, system=None, model=None,
                 temperature=0.1, max_tokens=16000) -> Completion:
        # The BrewKeg wire carries exactly one user message in every
        # existing consumer flow; the interface's message list is
        # folded into that shape here, inside the provider.
        user_prompt = ""
        for message in messages or []:
            if isinstance(message, dict):
                user_prompt = str(message.get("content") or user_prompt)
        data = self._request(
            system or "",
            user_prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return Completion(
            text=_extract_text(data),
            usage=data.get("usage") or {},
            model=model or self.model,
            provider=self.name,
            stop_reason=data.get("stop_reason"),
        )


def _extract_text(data):
    """Extract text from a BrewKeg response body (wire parsing —
    this module only). BrewKeg's successful format is
    ``{\"content\": [{\"type\": \"text\", \"text\": ...}]}``; an
    OpenAI-Responses compatibility shape is also accepted."""

    content = data.get("content") or []

    parts = []

    for item in content:
        if not isinstance(item, dict):
            continue

        if item.get("type") != "text":
            continue

        value = item.get("text")

        if value:
            parts.append(str(value))

    if parts:
        return "\n".join(parts).strip()

    output_text = data.get("output_text")

    if output_text:
        return str(output_text).strip()

    output = data.get("output") or []

    for item in output:
        if not isinstance(item, dict):
            continue

        for content_item in item.get("content") or []:
            if not isinstance(content_item, dict):
                continue

            if content_item.get("type") in {
                "output_text",
                "text",
            }:
                value = content_item.get("text")

                if value:
                    parts.append(str(value))

    if parts:
        return "\n".join(parts).strip()

    return ""


# ---------------------------------------------------------------------
# The configured default provider + module-level conveniences
# ---------------------------------------------------------------------

_default_provider = None


def get_provider() -> LLMProvider:
    """The deployment's configured provider (BrewKeg, env-driven).
    Constructed lazily; construction never requires the key —
    only an actual call does (BLOCK-001)."""
    global _default_provider
    if _default_provider is None:
        _default_provider = BrewKegProvider()
    return _default_provider


def is_configured() -> bool:
    return get_provider().configured()


def complete_text(system_prompt, user_prompt, **kwargs):
    return get_provider().complete_text(system_prompt, user_prompt, **kwargs)


def complete_json(system_prompt, user_prompt, **kwargs):
    return get_provider().complete_json(system_prompt, user_prompt, **kwargs)


def get_llm():
    """The configured provider as a simple (system, user) -> text
    callable (the story planner's shape)."""
    return get_provider().as_callable()


# Compatibility aliases for the pre-D-01 module-level API (the
# llm_client shim re-exports these; internal helpers used by the
# provider's own tests).


def _request(system_prompt, user_prompt, **kwargs):
    return get_provider()._request(system_prompt, user_prompt, **kwargs)


def _require_api_key():
    get_provider()._require_configured()
