import os
import json
import requests
from dotenv import load_dotenv


load_dotenv(override=True)


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
# well under a minute with thinking disabled (see _request); the old
# timeout=None could hang a production job forever on a stuck call.
REQUEST_TIMEOUT_SECONDS = 300


class LLMResponseError(RuntimeError):
    """The provider answered, but the response itself is unusable.

    kind is one of:
      "no_text"   — no text content block (e.g. a reasoning-only
                    response that spent its budget thinking);
      "truncated" — stop_reason reported the token cap, so any text
                    present is incomplete;
      "empty"     — HTTP 200 with an empty body;
      "malformed" — the body was not JSON.

    Kept distinct from transport/HTTP failures (plain RuntimeError
    from _request) so callers and reports can tell a bad response
    apart from a provider outage — and so an unusable response is
    never mistaken for a successful one.
    """

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _require_api_key():
    """BLOCK-001: the BrewKeg key is required at first USE, not at
    import. This check used to sit at module top level, so importing
    llm_client (directly, or transitively via script_analyzer)
    crashed the whole process — including the API, even for
    read-only routes that never call the LLM — whenever the key was
    unset. Importing this module now always succeeds; an actual LLM
    call without a key fails here with the same clear error, before
    any HTTP request is attempted."""
    if not BREWKEG_API_KEY:
        raise RuntimeError("BREWKEG_API_KEY is not configured")


def _request(
    system_prompt,
    user_prompt,
    *,
    model=None,
    temperature=0.1,
    max_tokens=16000,
):
    _require_api_key()

    payload = {
        "model": model or BREWKEG_MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": str(system_prompt),
        "messages": [
            {
                "role": "user",
                "content": str(user_prompt),
            }
        ],
        # Root-cause fix (diagnosed live, 2026-11-19): the backing
        # model is a reasoning model. Left enabled, its reasoning is
        # returned as a "thinking" content block that consumes the
        # output budget — responses truncate at max_tokens, sometimes
        # before any text block exists at all. Every caller of this
        # client wants a direct structured answer (llm_json already
        # instructs "Do NOT return your reasoning"), so thinking is
        # disabled at the source. Verified against the live service:
        # with thinking disabled the same prompts return text-only
        # responses with stop_reason=end_turn in a fraction of the
        # time and output tokens.
        "thinking": {"type": "disabled"},
    }

    import time

    last_response = None

    for attempt in range(4):
        # Phase 8.4: platform token bucket at the choke point (the
        # BrewKeg key is shared by every worker — design 03 §5) and
        # the usage counter the Phase 7 audit found missing: every
        # real request is counted, with token usage when the
        # gateway reports it.
        try:
            from services import rate_bucket

            rate_bucket.acquire("llm", timeout=30.0)
        except Exception:
            pass
        response = requests.post(
            f"{BREWKEG_BASE_URL}/messages",
            headers={
                "Authorization": f"Bearer {BREWKEG_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

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
            raise RuntimeError(
                f"BrewKeg request failed "
                f"(HTTP {response.status_code}): "
                f"{response.text[:3000]}"
            )

        if attempt < 3:
            time.sleep(3 * (attempt + 1))

    response = last_response

    if not response.ok:
        raise RuntimeError(
            f"BrewKeg request failed after 4 attempts "
            f"(HTTP {response.status_code}): "
            f"{response.text[:3000]}"
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


def _extract_text(data):
    """
    Extract text from BrewKeg Responses API.

    BrewKeg's actual successful response format is:

        {
            "content": [
                {
                    "type": "text",
                    "text": "..."
                }
            ]
        }
    """

    # ---------------------------------------------------------
    # 1. BrewKeg actual Responses format
    # ---------------------------------------------------------

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

    # ---------------------------------------------------------
    # 2. OpenAI Responses compatibility
    # ---------------------------------------------------------

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


def llm_text(
    system_prompt,
    user_prompt,
    *,
    model=None,
    temperature=0.1,
    max_tokens=16000,
):
    # A response counts as successful ONLY when it carries text and
    # was not truncated at the token cap. Reasoning-only responses
    # (no text block) and truncated responses are classified
    # explicitly via LLMResponseError and retried a bounded number
    # of times with an escalating output budget — never silently
    # treated as success. (Thinking is disabled in _request, which
    # removes the usual cause; this loop is the safety net for the
    # cases that remain, e.g. a genuinely long answer.)
    budget = max_tokens
    last_error = None

    for _attempt in range(3):
        data = _request(
            system_prompt,
            user_prompt,
            model=model,
            temperature=temperature,
            max_tokens=budget,
        )

        text = _extract_text(data)
        stop_reason = data.get("stop_reason")

        if text and stop_reason != "max_tokens":
            return text

        if not text:
            blocks = [
                item.get("type")
                for item in data.get("content") or []
                if isinstance(item, dict)
            ]
            last_error = LLMResponseError(
                "no_text",
                "BrewKeg returned no text output.\n"
                f"Content blocks: {blocks}\n"
                f"stop_reason: {stop_reason}\n"
                f"usage: {data.get('usage')}\n"
                f"Response: {json.dumps(data)[:3000]}",
            )
        else:
            last_error = LLMResponseError(
                "truncated",
                "BrewKeg returned a truncated response "
                f"(stop_reason=max_tokens at a {budget}-token budget); "
                "the incomplete text was not treated as a successful "
                "response.",
            )

        budget = min(budget * 2, 64000)

    raise last_error


def llm_json(
    system_prompt,
    user_prompt,
    *,
    model=None,
    temperature=0.0,
    max_tokens=1600,
):
    """
    Robust JSON LLM call.

    Handles markdown fences and JSON embedded in surrounding model text.
    If the provider returns reasoning before the JSON, parse_json-style
    extraction still recovers the final JSON object/array.
    """
    json_system = (
        str(system_prompt)
        + "\n\n"
        "CRITICAL OUTPUT RULES:\n"
        "1. Return ONLY the final JSON object.\n"
        "2. Do NOT return your reasoning or thinking.\n"
        "3. Do NOT use markdown fences.\n"
        "4. Do NOT write anything before or after the JSON.\n"
    )

    text = llm_text(
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

    raise RuntimeError(
        "BrewKeg returned invalid JSON:\n"
        + text[:5000]
    )


def get_llm():
    """
    Return the configured LLM as a simple callable compatible
    with the existing story_understanding planner.
    """

    def _llm(system_prompt, user_prompt):
        return llm_text(
            system_prompt,
            user_prompt,
            model=BREWKEG_MODEL,
            temperature=0.0,
            max_tokens=16000,
        )

    return _llm



