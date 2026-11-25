"""Compatibility shim (Phase 4b / D-01).

The LLM implementation moved to :mod:`services.llm_provider` —
the provider interface, the typed failure taxonomy and the single
concrete BrewKeg provider. This module re-exports the pre-D-01
names so older imports keep working; new code must import
``services.llm_provider`` directly and code against the provider
interface. No wire-format or HTTP code lives here.
"""

import requests  # noqa: F401  (re-exported: legacy tests patch it here)

from services.llm_provider import (  # noqa: F401
    BREWKEG_API_KEY,
    BREWKEG_BASE_URL,
    BREWKEG_MODEL,
    REQUEST_TIMEOUT_SECONDS,
    BrewKegProvider,
    Completion,
    LLMProvider,
    LLMProviderError,
    LLMResponseError,
    _extract_text,
    _request,
    _require_api_key,
    get_llm,
    get_provider,
)
from services import llm_provider as _provider


def llm_text(system_prompt, user_prompt, **kwargs):
    return _provider.complete_text(system_prompt, user_prompt, **kwargs)


def llm_json(system_prompt, user_prompt, **kwargs):
    return _provider.complete_json(system_prompt, user_prompt, **kwargs)
