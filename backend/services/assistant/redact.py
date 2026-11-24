"""Secret redaction for everything the assistant touches (design
assistant/08 §4).

Applied at three boundaries: tool results before they enter the
model's context, action arguments/results before they are persisted
to the audit log, and the final answer before it is stored/returned.
Two layers: the VALUES of the credentials this process is actually
configured with (exact-match replacement — a secret that appears
verbatim is the realistic leak), and assignment-shaped patterns
(`api_key = ...`, `Bearer ...`) for secret-shaped text arriving
inside retrieved content.
"""

from __future__ import annotations

import os
import re

_SECRET_ENV_VARS = (
    "BREWKEG_API_KEY",
    "SERPER_API_KEY",
    "CINEMORA_API_TOKEN",
    "CINEMORA_ADMIN_TOKEN",
)

_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|"
    r"password|passwd|credential)s?\b(\s*[:=]\s*)(\S+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}")
REDACTED = "[redacted]"


def _configured_secret_values() -> list[str]:
    values = []
    for name in _SECRET_ENV_VARS:
        value = os.environ.get(name) or ""
        if len(value) >= 8:
            values.append(value)
    # Longest first so a value that contains another is fully masked.
    return sorted(values, key=len, reverse=True)


def redact_text(text: str) -> str:
    if not text:
        return text
    out = str(text)
    for value in _configured_secret_values():
        out = out.replace(value, REDACTED)
    out = _BEARER_RE.sub(f"Bearer {REDACTED}", out)
    out = _ASSIGNMENT_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out
    )
    return out


def redact_value(value):
    """Recursively redact strings inside JSON-shaped data."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value
