"""LLM adapter for the assistant (design assistant/04 §2.3).

The single seam between the orchestrator and the model. Wraps
`services/llm_client.llm_json` — the existing BrewKeg client — so
provider specifics (endpoint shape, JSON recovery, model choice)
stay behind the platform's LLM layer and the assistant is
model-independent: if the provider later grows native tool-use,
this is the only file that changes.

Unavailability is a typed state, never an exception leaking into a
half-answer: no configured key or a provider failure raises
AssistantUnavailable, which the API turns into the honest
`state: "unavailable"` response (degraded honesty, mirroring the
Degraded Planner's recorded modes).
"""

from __future__ import annotations

from services import llm_client


class AssistantUnavailable(Exception):
    """The model cannot be reached; the assistant must say so."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason  # llm_not_configured | llm_unavailable
        self.message = message


class ProtocolError(Exception):
    """The provider answered, but not in the action protocol."""


def is_configured() -> bool:
    return bool(llm_client.BREWKEG_API_KEY)


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
        )
    try:
        result = llm_client.llm_json(
            system_prompt, user_payload, max_tokens=4000
        )
    except Exception as error:
        raise AssistantUnavailable(
            "llm_unavailable",
            f"The assistant's language model could not be reached: "
            f"{type(error).__name__}.",
        ) from error
    if not isinstance(result, dict):
        raise ProtocolError(
            f"Model returned a {type(result).__name__}, not a JSON object."
        )
    return result
