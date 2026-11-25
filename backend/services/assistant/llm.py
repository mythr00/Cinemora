"""LLM adapter for the assistant (design assistant/04 §2.3).

The single seam between the orchestrator and the model. Wraps the
platform provider interface (`services.llm_provider`) — so provider
specifics (endpoint shape, JSON recovery, model choice) stay behind
the platform's LLM layer and the assistant is model-independent:
if the provider later grows native tool-use, this is the only file
that changes.

Unavailability is a typed state, never an exception leaking into a
half-answer: no configured key or a provider failure raises
AssistantUnavailable, which the API turns into the honest
`state: "unavailable"` response (degraded honesty, mirroring the
Degraded Planner's recorded modes). The provider failure's typed
kind (Phase 4b: billing / auth / rate_limit / upstream /
not_configured) rides on the exception as `provider_error_kind`
for diagnostics — a 402 quota failure is distinguishable from an
outage, and neither is ever answered with canned content.
"""

from __future__ import annotations

from services import llm_provider


class AssistantUnavailable(Exception):
    """The model cannot be reached; the assistant must say so."""

    def __init__(self, reason: str, message: str, provider_error_kind=None):
        super().__init__(message)
        self.reason = reason  # llm_not_configured | llm_unavailable
        self.message = message
        # Phase 4b: the typed provider failure class behind the
        # unavailability (llm_provider.LLMProviderError.kind), for
        # diagnostics; None when the failure was not a provider one.
        self.provider_error_kind = provider_error_kind


class ProtocolError(Exception):
    """The provider answered, but not in the action protocol."""


def is_configured() -> bool:
    return llm_provider.is_configured()


def complete_json(system_prompt: str, user_payload: str) -> dict:
    """One protocol step: returns the model's JSON object.

    Raises AssistantUnavailable (no key / provider failure) or
    ProtocolError (answer was not a JSON object)."""
    if not is_configured():
        raise AssistantUnavailable(
            "llm_not_configured",
            "The assistant's language model is not configured on "
            "this deployment (no BrewKeg key), so the assistant "
            "cannot answer right now.",
            provider_error_kind="not_configured",
        )
    try:
        result = llm_provider.complete_json(
            system_prompt, user_payload, max_tokens=4000
        )
    except llm_provider.LLMProviderError as error:
        if error.kind == "not_configured":
            raise AssistantUnavailable(
                "llm_not_configured",
                "The assistant's language model is not configured on "
                "this deployment (no BrewKeg key), so the assistant "
                "cannot answer right now.",
                provider_error_kind=error.kind,
            ) from error
        raise AssistantUnavailable(
            "llm_unavailable",
            f"The assistant's language model could not be reached: "
            f"{type(error).__name__}.",
            provider_error_kind=error.kind,
        ) from error
    except Exception as error:
        raise AssistantUnavailable(
            "llm_unavailable",
            f"The assistant's language model could not be reached: "
            f"{type(error).__name__}.",
            provider_error_kind=getattr(error, "kind", None),
        ) from error
    if not isinstance(result, dict):
        raise ProtocolError(
            f"Model returned a {type(result).__name__}, not a JSON object."
        )
    return result
