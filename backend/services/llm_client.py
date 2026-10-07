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
    "claude-opus-5",
)


if not BREWKEG_API_KEY:
    raise RuntimeError("BREWKEG_API_KEY is not configured")


def _request(
    system_prompt,
    user_prompt,
    *,
    model=None,
    temperature=0.1,
    max_tokens=1200,
):
    payload = {
        "model": model or BREWKEG_MODEL,
        "instructions": str(system_prompt),
        "input": str(user_prompt),
        "max_output_tokens": max_tokens,
    }

    response = requests.post(
        f"{BREWKEG_BASE_URL}/responses",
        headers={
            "Authorization": f"Bearer {BREWKEG_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )

    if not response.ok:
        raise RuntimeError(
            f"BrewKeg request failed "
            f"(HTTP {response.status_code}): "
            f"{response.text[:3000]}"
        )

    if not response.text.strip():
        raise RuntimeError(
            "BrewKeg returned HTTP 200 but an empty response body."
        )

    try:
        return response.json()

    except ValueError as exc:
        raise RuntimeError(
            "BrewKeg returned non-JSON response:\n"
            + response.text[:3000]
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
    max_tokens=1200,
):
    data = _request(
        system_prompt,
        user_prompt,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    text = _extract_text(data)

    if not text:
        raise RuntimeError(
            "BrewKeg returned no text output.\n"
            f"Response keys: {list(data.keys())}\n"
            f"Response: {json.dumps(data)[:3000]}"
        )

    return text


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
            max_tokens=4000,
        )

    return _llm

