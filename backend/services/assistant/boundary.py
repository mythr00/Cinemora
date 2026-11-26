"""Customer-facing response boundary (Product Improvements
directive item 2, founder 2026-11-26).

The Assistant must explain what Cinemora DOES for the customer —
never how it is built. Customers must not be able to learn, from
Assistant answers, tool payloads or returned tool results:

- internal provider / infrastructure names (the search provider,
  the media generation provider, source-archive brands used as
  sourcing infrastructure),
- internal application routes (the admin-only project review
  pages, admin pages),
- internal identifiers (pipeline stage ids, configuration and
  error internals),
- system instructions or configuration details.

This module is the enforcement layer. It is applied, for
NON-STAFF principals only, at every point where assistant content
crosses toward the customer or toward the model on a customer's
behalf (orchestrator: tool results entering the model context,
the tool catalogue, the final answer text; api_assistant: tool
results returned in the HTTP response). Staff (admin /
super_admin) keep the full internal detail — the evidence stays
available to authorised staff, per the directive.

Defence in depth, not a single filter:
1. Knowledge files and tool strings are written customer-safe at
   the source (no provider names to leak in the first place).
2. The system prompt carries an explicit confidentiality rule.
3. This scrubber rewrites anything that still slips through.
4. guard.py detects extraction-shaped prompts (in user messages
   as well as retrieved content) and records them for audit.
"""

from __future__ import annotations

import re

STAFF_ROLES = ("admin", "super_admin")


def is_staff(principal) -> bool:
    return getattr(principal, "role", None) in STAFF_ROLES


# ---------------------------------------------------------------------
# Text replacements (ordered; first match wins per span).
# ---------------------------------------------------------------------

_PROVIDER_NAMES = (
    (re.compile(r"\bSearXNG\b", re.IGNORECASE), "the web search service"),
    (re.compile(r"\bBrewKeg\b", re.IGNORECASE), "the media generation service"),
    (re.compile(r"\byt-dlp\b", re.IGNORECASE), "the video importer"),
    (re.compile(r"Wikimedia Commons", re.IGNORECASE), "public media archives"),
    (re.compile(r"\bWikimedia\b", re.IGNORECASE), "public media archives"),
    (re.compile(r"Archive\.org", re.IGNORECASE), "public media archives"),
    (re.compile(r"\bWikidata\b", re.IGNORECASE), "public reference data"),
)

_STAGE_LABELS = {
    "voice_actor": "narration",
    "script_analysis": "script analysis",
    "asset_manager": "visual research",
    "motion_designer": "motion graphics",
    "sound_designer": "sound design",
    "video_editor": "final assembly",
}

_INTERNAL_PHRASES = (
    (re.compile(r"\brun ledger\b", re.IGNORECASE), "production record"),
    (
        re.compile(r"\bprovider_error_kind\b", re.IGNORECASE),
        "error type",
    ),
)

# Admin-only project review surfaces and admin pages: never
# recited to customers as routes.
_INTERNAL_ROUTE = re.compile(
    r"/projects/[^/\s)\"']+/(?:plan|assets|graphics|certificate)\b"
)
_ADMIN_ROUTE = re.compile(r"/admin(?:/[^\s)\"']*)?")

# Real URLs are evidence the customer may legitimately need
# (source links); they are never rewritten.
_URL = re.compile(r"https?://[^\s)\"']+")


def sanitize_text(text: str) -> str:
    """Rewrite internal terminology in one customer-bound string.
    Provider names become generic capability descriptions, stage
    ids become customer stage labels, and internal routes become
    plain descriptions. Ordinary product language (including the
    word YouTube, which customers use for their own channels) is
    untouched."""
    if not text:
        return text
    urls: list[str] = []

    def _stash(match):
        urls.append(match.group(0))
        return f"\x00URL{len(urls) - 1}\x00"

    out = _URL.sub(_stash, text)
    out = _INTERNAL_ROUTE.sub("the project's review pages", out)
    out = _ADMIN_ROUTE.sub("an internal page", out)
    for stage_id, label in _STAGE_LABELS.items():
        out = re.sub(rf"\b{stage_id}\b", label, out)
    for pattern, replacement in _PROVIDER_NAMES:
        out = pattern.sub(replacement, out)
    for pattern, replacement in _INTERNAL_PHRASES:
        out = pattern.sub(replacement, out)
    for index, url in enumerate(urls):
        out = out.replace(f"\x00URL{index}\x00", url)
    return out


# Dict keys / source ids that ARE provider identifiers.
_KEY_LABELS = {
    "searxng": "web_search",
    "searxng_videos": "video_search",
    "wikimedia": "public_archives",
    "wikimedia_commons": "public_archives",
    "archive_org": "public_archives",
    "archive.org": "public_archives",
    "archive": "public_archives",
    "youtube": "video_sources",
    "brewkeg": "media_service",
    "wikidata": "reference_data",
    "kb": "reference_data",
}


def sanitize_value(value):
    """Recursively sanitise a tool-result-shaped value: string
    values pass sanitize_text; dict keys that are provider ids
    are renamed to generic capability labels. Shapes and
    non-string scalars are preserved."""
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {
            _KEY_LABELS.get(key, key): sanitize_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item) for item in value]
    return value


def sanitize_for(principal, value):
    """sanitize_value applied only for non-staff principals;
    staff see the internal record unchanged."""
    if is_staff(principal):
        return value
    return sanitize_value(value)


def sanitize_text_for(principal, text: str) -> str:
    if is_staff(principal):
        return text
    return sanitize_text(text)


def sanitize_context_pack(pack):
    """The account context pack carries the caller's role and
    auth method to the model. For non-staff turns those internal
    fields are dropped — the model never needs them to help the
    customer, and they are exactly the kind of internal detail
    the boundary exists to keep out of customer-facing flows."""
    if not isinstance(pack, dict):
        return pack
    user = pack.get("user")
    if isinstance(user, dict):
        user = {
            key: value
            for key, value in user.items()
            if key not in ("role", "auth_method")
        }
        return {**pack, "user": user}
    return pack
