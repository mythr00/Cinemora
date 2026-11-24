"""Product knowledge retrieval (design assistant/04 §2.6).

The curated pack in `knowledge/*.md` is the ONLY product-knowledge
source the assistant serves — authored from the real UI (routes,
tabs, labels), versioned with the repo, reviewed like code. Each
file carries a small header (id / title / link / keywords) and a
markdown body. Retrieval is section-level keyword scoring; no
embeddings in v1, per the design.

The navigation route table also lives here: links are built from
this fixed table in code, so the assistant cannot invent a URL.
"""

from __future__ import annotations

import re
from pathlib import Path

_PACK_DIR = Path(__file__).resolve().parent / "knowledge"

# The fixed route table (design 01 §8). "{id}" is replaced with a
# project id the caller owns — tools validate ownership before a
# project link is produced.
ROUTES = {
    "dashboard": "/studio",
    "projects": "/projects",
    "create": "/create",
    "assistant": "/assistant",
    "project": "/projects/{id}",
    "plan": "/projects/{id}/plan",
    "assets": "/projects/{id}/assets",
    "graphics": "/projects/{id}/graphics",
    "edit": "/projects/{id}/edit",
    "certificate": "/projects/{id}/certificate",
}

# Feature name (normalised) -> pack section id.
_FEATURE_ALIASES = {
    "asset manager": "asset-manager",
    "assets": "asset-manager",
    "plan": "plan-review",
    "plan review": "plan-review",
    "editor": "editor",
    "edit": "editor",
    "certificate": "certificate",
    "graphics": "graphics",
    "motion graphics": "graphics",
    "production": "production-flow",
    "production flow": "production-flow",
    "runs": "runs-and-status",
    "run status": "runs-and-status",
    "retry": "runs-and-status",
    "cancel": "runs-and-status",
    "assistant": "assistant-help",
    "genres": "catalogues",
    "voices": "catalogues",
    "overview": "overview",
    "navigation": "overview",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


class Section:
    def __init__(self, section_id, title, link, keywords, body):
        self.id = section_id
        self.title = title
        self.link = link
        self.keywords = keywords
        self.body = body

    def view(self) -> dict:
        return {
            "section_id": self.id,
            "title": self.title,
            "link": self.link,
            "content": self.body,
        }


def _load_pack() -> list[Section]:
    sections = []
    for path in sorted(_PACK_DIR.glob("*.md")):
        header: dict[str, str] = {}
        body_lines = []
        in_header = True
        for line in path.read_text(encoding="utf-8").splitlines():
            if in_header and ":" in line and not line.startswith("#"):
                key, _, value = line.partition(":")
                if key.strip() in ("id", "title", "link", "keywords"):
                    header[key.strip()] = value.strip()
                    continue
            in_header = False
            body_lines.append(line)
        if "id" not in header:
            continue
        sections.append(
            Section(
                section_id=header["id"],
                title=header.get("title", header["id"]),
                link=header.get("link", "/studio"),
                keywords=[
                    word.strip()
                    for word in header.get("keywords", "").split(",")
                    if word.strip()
                ],
                body="\n".join(body_lines).strip(),
            )
        )
    return sections


_SECTIONS: list[Section] | None = None


def sections() -> list[Section]:
    global _SECTIONS
    if _SECTIONS is None:
        _SECTIONS = _load_pack()
    return _SECTIONS


def search(query: str, limit: int = 3) -> list[dict]:
    """Keyword-scored section retrieval. A section scores for
    keyword-phrase hits (strong), title hits, then body token
    overlap; zero-score sections are never returned — an empty
    result is reported as empty, not padded."""
    query_tokens = set(_tokens(query))
    query_text = (query or "").lower()
    scored = []
    for section in sections():
        score = 0
        for keyword in section.keywords:
            if keyword.lower() in query_text:
                score += 5
            elif set(_tokens(keyword)) & query_tokens:
                score += 2
        title_tokens = set(_tokens(section.title))
        score += 3 * len(title_tokens & query_tokens)
        body_tokens = set(_tokens(section.body))
        score += len(body_tokens & query_tokens)
        if score > 0:
            scored.append((score, section))
    scored.sort(key=lambda item: (-item[0], item[1].id))
    return [section.view() for _score, section in scored[:limit]]


def feature_section(feature: str) -> dict | None:
    key = " ".join(_tokens(feature)).strip()
    section_id = _FEATURE_ALIASES.get(key)
    if section_id is None:
        # Fall back to a search over the feature words.
        results = search(feature, limit=1)
        return results[0] if results else None
    for section in sections():
        if section.id == section_id:
            return section.view()
    return None


def build_link(destination: str, project_id: str | None = None) -> str | None:
    """A URL from the fixed route table, or None when the
    destination is unknown (never an invented URL)."""
    template = ROUTES.get((destination or "").strip().lower())
    if template is None:
        return None
    if "{id}" in template:
        if not project_id:
            return None
        return template.replace("{id}", str(project_id))
    return template
