"""Prompt-injection detection over retrieved content (design
assistant/08 §6).

The structural defences live elsewhere (data envelopes, the
registry, the confirmation wall); this module is the *detection*
layer: text arriving from search results, fetched pages, project
scripts and tool results is scanned for instruction-shaped content
aimed at the assistant. A match never changes how the content is
treated — it is data either way — but it writes an
AssistantSecurityEvent row so the attempt lands in the admin M9
injection-rejection feed, and it flags the turn's tool trace.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

# (pattern name, compiled regex). Kept deliberately narrow: phrases
# whose ordinary use in documentary content is rare, so the feed
# stays signal, not noise.
_PATTERNS = (
    (
        "ignore_instructions",
        re.compile(
            r"(?i)ignore\s+(your\s+|the\s+|all\s+|previous\s+|prior\s+)*"
            r"(instructions|rules|directions|prompt)"
        ),
    ),
    (
        "reveal_secret",
        re.compile(
            r"(?i)(reveal|show|print|leak|send|give)\s+(me\s+)?(your\s+|the\s+)?"
            r"(api[\s_-]?key|api[\s_-]?token|token|secret|password|"
            r"system\s+prompt|credentials)"
        ),
    ),
    (
        "cancel_all",
        re.compile(r"(?i)cancel\s+(all|every)\s+(runs|productions|jobs)"),
    ),
    (
        "override_directive",
        re.compile(
            r"(?i)(disregard|override|forget)\s+(your\s+|the\s+|all\s+)?"
            r"(instructions|rules|safety|restrictions|system\s+prompt)"
        ),
    ),
    (
        "role_reassignment",
        re.compile(
            r"(?i)you\s+are\s+now\s+(a|an|the)\s+"
            r"(?!documentary|film|story|narrator)"
        ),
    ),
    (
        "instruction_to_assistant",
        re.compile(
            r"(?i)\b(assistant|ai)\s*[:,]\s*(ignore|reveal|delete|cancel|"
            r"execute|run)\b"
        ),
    ),
)

_EXCERPT_CHARS = 240


def scan_text(text: str) -> list[dict]:
    """Return [{pattern, excerpt}] for each distinct pattern hit."""
    if not text:
        return []
    haystack = str(text)
    hits = []
    for name, pattern in _PATTERNS:
        match = pattern.search(haystack)
        if match:
            start = max(0, match.start() - 60)
            end = min(len(haystack), match.end() + 60)
            hits.append(
                {
                    "pattern": name,
                    "excerpt": haystack[start:end].replace("\n", " ")[
                        :_EXCERPT_CHARS
                    ],
                }
            )
    return hits


def scan_value(value) -> list[dict]:
    """Scan every string inside a JSON-shaped tool result."""
    hits: list[dict] = []
    seen: set[str] = set()

    def _walk(item):
        if isinstance(item, str):
            for hit in scan_text(item):
                if hit["pattern"] not in seen:
                    seen.add(hit["pattern"])
                    hits.append(hit)
        elif isinstance(item, dict):
            for child in item.values():
                _walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                _walk(child)

    _walk(value)
    return hits


def record_events(db, *, owner_id, conversation_id, source, hits) -> int:
    """Persist one security-event row per distinct pattern hit.
    Best-effort in the admin_audit sense: a logging failure must
    never fail the turn, so errors are swallowed after the fact —
    the caller has already treated the content as data."""
    from models import AssistantSecurityEvent
    from services.assistant.redact import redact_value

    count = 0
    try:
        for hit in hits:
            db.add(
                AssistantSecurityEvent(
                    id=str(uuid.uuid4()),
                    owner_id=owner_id,
                    conversation_id=conversation_id,
                    source=source,
                    detail=redact_value(
                        {"pattern": hit["pattern"], "excerpt": hit["excerpt"]}
                    ),
                    created_at=datetime.now(timezone.utc),
                )
            )
            count += 1
        db.commit()
    except Exception:
        db.rollback()
    return count
