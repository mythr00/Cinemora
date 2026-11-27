"""YouTube Script Studio (founder priority #12; gap-audit doc 02).

The audit's central finding: Cinemora had a full script->film
pipeline and NO script authorship — users pasted finished scripts
into POST /projects, and nothing in the codebase wrote or developed
a script. This module is the writing workspace that feeds projects,
built on the machinery that already exists, never a parallel one:

- PERSISTENCE is one artefact per owner,
  ``users/<user_id>/script_studio.json`` under the media root —
  the same artefact pattern as research_studio.json /
  project_versions.json (extend artefacts, do not invent a parallel
  store). A draft is a document: title, category, target length and
  SECTIONS (heading + body). The heading is studio structure; the
  production script is the section bodies joined by blank lines —
  exactly the paragraph blocks the pipeline's analysis consumes.
- METRICS are arithmetic, not vibes: word counts are whitespace
  splits, sentence counts come from script_analyzer.split_sentences,
  and the duration estimate uses the codebase's own recorded rate —
  the create page's ~145 spoken words per minute. Genre-pack pacing
  notes state narration is NOT retimed to pacing targets, so no
  per-pack pace factor is invented here. A target's word budget is
  target_minutes x 145.
- AI ACTIONS run through the provider interface (services.
  llm_provider) only,
  are always explicit and user-triggered, and return PROPOSALS:
  generated text is never written over the user's words by the
  service — the client applies a proposal through the normal update
  path, or discards it. When no provider is configured the actions
  refuse with 503 script_ai_unavailable; a provider failure is a
  502. There is no canned fallback draft anywhere in this module.
  Generation follows the audit's §3 design: outline first (one
  llm_json call carrying per-section word budgets), then one
  llm_text call per section — a long script is never asked of a
  single call.
- ANALYSIS runs the pipeline's own script_analyzer.analyze_script
  (the same Case Bible / sentence-role / scene derivation a
  production runs), with the full planner when the provider is
  configured and the pipeline's own degraded planner when it is
  not; the recorded mode is returned verbatim, never dressed up.
- FACT CHECK extracts the script's facts with the pipeline's own
  script_facts.build_fact_table (every fact basis-stamped
  script_claim) and corroborates them with
  script_facts.corroborate_facts — the Research Studio's engine,
  surfaced here, not duplicated.
- SEND TO PRODUCTION creates a project through the normal creation
  path's own steps (plan script-length enforcement, Project row,
  versioning script snapshot) and records the link on the draft.
  ATTACH writes the assembled script onto an existing project the
  caller owns and snapshots it through versioning, the same way a
  versioning restore sets a script.

Every failure is data or a ScriptError with the v1 shape; this
module never fabricates a draft, a metric or a verdict.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from services import languages as languages_service
from services import llm_provider, media_paths, script_analyzer, script_facts


def language_name(code) -> str:
    return languages_service.name_of(code)
from services.atomic_write import write_json_atomic

STORE_FILENAME = "script_studio.json"

# The codebase's recorded narration rate: the create page shows
# runtime ~= words / 145 ("Based on about 145 spoken words per
# minute"). The studio uses the same number so the two surfaces
# can never disagree.
WORDS_PER_MINUTE = 145

# Audit doc 02 §3 step 4: duration choice 5-60 min. Generation is
# sectioned; the outline is capped so one generation stays a
# bounded number of provider calls.
MIN_TARGET_MINUTES = 5
MAX_TARGET_MINUTES = 60
MAX_OUTLINE_SECTIONS = 12
MAX_SECTIONS = 60
MAX_REVISIONS = 50

DEFAULT_CATEGORY = "Documentary"


class ScriptError(Exception):
    """A domain error with an HTTP status + stable machine code
    (the API layer maps it onto the v1 error shape)."""

    def __init__(self, status_code: int, code: str, message: str,
                 details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _user_dir(user_id) -> Path:
    return media_paths.media_root() / "users" / str(user_id)


# ---------------------------------------------------------------------
# Store (the per-owner script_studio.json artefact)
# ---------------------------------------------------------------------


def _empty_store() -> dict:
    return {"scripts": []}


def load_store(user_id) -> dict:
    path = _user_dir(user_id) / STORE_FILENAME
    if not path.is_file():
        return _empty_store()
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_store()
    if not isinstance(data, dict) or not isinstance(data.get("scripts"), list):
        return _empty_store()
    return data


def save_store(user_id, store: dict) -> dict:
    user_dir = _user_dir(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(user_dir / STORE_FILENAME, store)
    return store


def _find_draft(store: dict, script_id: str) -> dict:
    for draft in store.get("scripts") or []:
        if draft.get("id") == script_id:
            return draft
    raise ScriptError(404, "script_not_found", "No such script draft.")


# ---------------------------------------------------------------------
# Text assembly + metrics (deterministic; the recorded 145 wpm rate)
# ---------------------------------------------------------------------


def assemble_script(draft: dict) -> str:
    """The production script text: section bodies in order, blank
    lines between them. Headings are studio structure and are NOT
    narrated, so they never enter the script a project receives."""
    bodies = []
    for section in draft.get("sections") or []:
        body = str(section.get("body") or "").strip()
        if body:
            bodies.append(body)
    return "\n\n".join(bodies)


def _count_words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


# ---------------------------------------------------------------------
# Permanent script rules (founder directive 2026-11-26): a script
# is natural narration ONLY. It must never contain timestamps or
# timers ([00:30], 00:30 markers), HOOK / INTRODUCTION /
# CONCLUSION headings, chapter headings or numbers, section
# labels, or production instructions. Word count and estimated
# duration are metadata shown separately — never script content.
# Prompts state the rules; this sanitizer ENFORCES them on every
# AI-produced body before it is stored or proposed, so a model
# slip cannot put production furniture into narration.
# ---------------------------------------------------------------------

_TIMESTAMP_LINE = re.compile(
    r"^\[?\(?\d{1,2}:\d{2}(?::\d{2})?\)?\]?$"
)
_TIMESTAMP_INLINE = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]")
_LABEL_LINE = re.compile(
    r"^(HOOK|INTRO|INTRODUCTION|OUTRO|CONCLUSION|EPILOGUE|PROLOGUE)"
    r"\s*[:.\-—]?\s*$",
    re.IGNORECASE,
)
_NUMBERED_HEADING_LINE = re.compile(
    r"^(CHAPTER|SECTION|PART|SCENE|ACT)\s+\d+\s*[:.\-—]?\s*.{0,80}$",
    re.IGNORECASE,
)
_BRACKETED_LINE = re.compile(r"^(\[.+\]|\(.+\))$", re.DOTALL)
_SPEAKER_PREFIX = re.compile(
    r"^(NARRATOR|VOICEOVER|V\.O\.|VO)\s*:\s*", re.IGNORECASE
)

SCRIPT_RULES_TEXT = (
    "The script is natural narration only. Never include "
    "timestamps or timers (no [00:30], no 00:30 markers), no "
    "HOOK, INTRODUCTION or CONCLUSION headings, no chapter "
    "headings or numbers, no section labels, and no production "
    "instructions or stage directions. Word counts and durations "
    "are metadata, never written into the narration."
)


def sanitize_narration_body(text: str) -> tuple[str, int]:
    """Remove production furniture from an AI-produced narration
    body. Returns (clean_text, removed_count). Conservative by
    design: only unambiguous furniture is removed — whole lines
    that are nothing but a timestamp, a structural label or a
    numbered chapter heading; whole lines wrapped in brackets
    (directions); markdown heading lines; inline [mm:ss] markers;
    and NARRATOR:-style speaker prefixes (the spoken text after
    the prefix is kept). Ordinary prose is never altered."""
    if not text:
        return "", 0
    removed = 0
    kept: list[str] = []
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if line:
            if _TIMESTAMP_LINE.match(line):
                removed += 1
                continue
            if _LABEL_LINE.match(line):
                removed += 1
                continue
            if _NUMBERED_HEADING_LINE.match(line):
                removed += 1
                continue
            if line.startswith("#"):
                removed += 1
                continue
            if _BRACKETED_LINE.match(line) and len(line) <= 200:
                removed += 1
                continue
            line = _SPEAKER_PREFIX.sub("", line)
            inline_hits = len(_TIMESTAMP_INLINE.findall(line))
            if inline_hits:
                removed += inline_hits
                line = _TIMESTAMP_INLINE.sub("", line).strip()
                line = re.sub(r"\s{2,}", " ", line)
            if not line:
                continue
        kept.append(line)
    cleaned = "\n".join(kept)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, removed


def section_metrics(section: dict) -> dict:
    body = str(section.get("body") or "")
    words = _count_words(body)
    return {
        "word_count": words,
        "sentence_count": len(script_analyzer.split_sentences(body)) if words else 0,
        "estimated_minutes": round(words / WORDS_PER_MINUTE, 2),
    }


def draft_metrics(draft: dict) -> dict:
    text = assemble_script(draft)
    words = _count_words(text)
    target = draft.get("target_minutes")
    target_words = (
        int(round(float(target) * WORDS_PER_MINUTE)) if target else None
    )
    return {
        "word_count": words,
        "sentence_count": len(script_analyzer.split_sentences(text)) if words else 0,
        "estimated_minutes": round(words / WORDS_PER_MINUTE, 2),
        "words_per_minute": WORDS_PER_MINUTE,
        "target_minutes": target,
        "target_words": target_words,
        "words_vs_target": (words - target_words) if target_words is not None else None,
    }


def _public_draft(draft: dict, *, detail: bool = False) -> dict:
    view = {
        "id": draft.get("id"),
        "title": draft.get("title"),
        "video_title": draft.get("video_title"),
        "language": draft.get("language") or "en",
        "category": draft.get("category"),
        "target_minutes": draft.get("target_minutes"),
        "project_id": draft.get("project_id"),
        "productions": draft.get("productions") or [],
        "created_at": draft.get("created_at"),
        "updated_at": draft.get("updated_at"),
        "metrics": draft_metrics(draft),
        "section_count": len(draft.get("sections") or []),
        "has_analysis": bool(draft.get("analysis")),
        "has_fact_check": bool(draft.get("fact_check")),
    }
    if detail:
        sections = []
        for section in draft.get("sections") or []:
            entry = dict(section)
            entry["metrics"] = section_metrics(section)
            sections.append(entry)
        view["sections"] = sections
        view["generation"] = draft.get("generation")
        view["translated_from"] = draft.get("translated_from")
        view["script_text"] = assemble_script(draft)
        view["analysis"] = draft.get("analysis")
        view["fact_check"] = draft.get("fact_check")
        view["revision_count"] = len(draft.get("revisions") or [])
    return view


# ---------------------------------------------------------------------
# Draft CRUD
# ---------------------------------------------------------------------


def _clean_sections(raw_sections) -> list[dict]:
    if raw_sections is None:
        return [{"id": _new_id("sec"), "heading": "Opening", "body": ""}]
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ScriptError(
            422, "sections_required", "A script needs at least one section."
        )
    if len(raw_sections) > MAX_SECTIONS:
        raise ScriptError(
            422,
            "too_many_sections",
            f"A script may have at most {MAX_SECTIONS} sections.",
        )
    sections = []
    for raw in raw_sections:
        if not isinstance(raw, dict):
            raise ScriptError(422, "invalid_section", "Each section must be an object.")
        sections.append(
            {
                "id": str(raw.get("id") or _new_id("sec")),
                "heading": str(raw.get("heading") or "").strip()[:200],
                "body": str(raw.get("body") or ""),
            }
        )
    return sections


def _clean_target(value):
    if value is None:
        return None
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        raise ScriptError(422, "invalid_target", "Target length must be minutes.")
    if minutes <= 0 or minutes > 600:
        raise ScriptError(
            422, "invalid_target", "Target length must be between 0 and 600 minutes."
        )
    return minutes


def create_draft(user_id, *, title=None, category=None, target_minutes=None,
                 sections=None, video_title=None, language=None) -> dict:
    store = load_store(user_id)
    draft = {
        "id": _new_id("script"),
        "title": (str(title).strip() if title else "") or "Untitled script",
        # Item 3: the public video title — distinct from the
        # working title/topic, editable, carried into production.
        "video_title": (
            str(video_title).strip()[:300] if video_title else None
        ),
        # Item 6: the script's language (narration text language),
        # chosen before generation; carried into production.
        "language": (
            str(language).strip().lower() if language else "en"
        ),
        "category": (str(category).strip() if category else "") or DEFAULT_CATEGORY,
        "target_minutes": _clean_target(target_minutes),
        "sections": _clean_sections(sections),
        "project_id": None,
        "productions": [],
        "revisions": [],
        "analysis": None,
        "fact_check": None,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    store["scripts"].insert(0, draft)
    save_store(user_id, store)
    return _public_draft(draft, detail=True)


def list_drafts(user_id) -> dict:
    store = load_store(user_id)
    return {"scripts": [_public_draft(d) for d in store.get("scripts") or []]}


def get_draft(user_id, script_id: str) -> dict:
    store = load_store(user_id)
    return _public_draft(_find_draft(store, script_id), detail=True)


def _snapshot(draft: dict, source: str) -> dict:
    return {
        "id": _new_id("rev"),
        "at": _now_iso(),
        "source": source,
        "title": draft.get("title"),
        "video_title": draft.get("video_title"),
        "category": draft.get("category"),
        "target_minutes": draft.get("target_minutes"),
        "sections": [
            {"id": s.get("id"), "heading": s.get("heading"), "body": s.get("body")}
            for s in draft.get("sections") or []
        ],
    }


def update_draft(user_id, script_id: str, *, title=None, category=None,
                 target_minutes="__unset__", sections=None,
                 video_title="__unset__",
                 source: str = "edit") -> dict:
    store = load_store(user_id)
    draft = _find_draft(store, script_id)
    changed = False
    if sections is not None:
        new_sections = _clean_sections(sections)
        if new_sections != draft.get("sections"):
            changed = True
            pending_sections = new_sections
        else:
            pending_sections = None
    else:
        pending_sections = None
    meta_changed = (
        (video_title != "__unset__"
         and (str(video_title).strip() if video_title else None)
         != draft.get("video_title"))
        or (title is not None and str(title).strip() != (draft.get("title") or ""))
        or (category is not None and str(category).strip() != (draft.get("category") or ""))
        or (
            target_minutes != "__unset__"
            and _clean_target(target_minutes) != draft.get("target_minutes")
        )
    )
    if changed or meta_changed:
        # Snapshot the PREVIOUS state before applying, so every
        # revision is a state the user can return to (the
        # versioning pattern: history is recorded, never implied).
        revisions = draft.setdefault("revisions", [])
        revisions.append(_snapshot(draft, source))
        del revisions[:-MAX_REVISIONS]
    if pending_sections is not None:
        draft["sections"] = pending_sections
        # Text changed: any earlier analysis/fact-check describes a
        # script that no longer exists. Clear them rather than let
        # stale results pose as current.
        draft["analysis"] = None
        draft["fact_check"] = None
    if video_title != "__unset__":
        draft["video_title"] = (
            str(video_title).strip()[:300] if video_title else None
        )
    if title is not None:
        draft["title"] = str(title).strip() or draft["title"]
    if category is not None:
        draft["category"] = str(category).strip() or draft["category"]
    if target_minutes != "__unset__":
        draft["target_minutes"] = _clean_target(target_minutes)
    draft["updated_at"] = _now_iso()
    save_store(user_id, store)
    return _public_draft(draft, detail=True)


def delete_draft(user_id, script_id: str) -> dict:
    store = load_store(user_id)
    draft = _find_draft(store, script_id)
    store["scripts"] = [d for d in store["scripts"] if d.get("id") != script_id]
    save_store(user_id, store)
    return {"deleted": draft.get("id")}


def list_revisions(user_id, script_id: str) -> dict:
    store = load_store(user_id)
    draft = _find_draft(store, script_id)
    revisions = []
    for revision in draft.get("revisions") or []:
        entry = {k: v for k, v in revision.items() if k != "sections"}
        entry["metrics"] = draft_metrics(
            {"sections": revision.get("sections") or [],
             "target_minutes": revision.get("target_minutes")}
        )
        revisions.append(entry)
    return {"revisions": revisions}


def restore_revision(user_id, script_id: str, revision_id: str) -> dict:
    store = load_store(user_id)
    draft = _find_draft(store, script_id)
    target = None
    for revision in draft.get("revisions") or []:
        if revision.get("id") == revision_id:
            target = revision
            break
    if target is None:
        raise ScriptError(404, "revision_not_found", "No such script revision.")
    # Restoring is itself an edit: the current state is snapshotted
    # first, so a restore can itself be undone.
    revisions = draft.setdefault("revisions", [])
    revisions.append(_snapshot(draft, "pre_restore"))
    del revisions[:-MAX_REVISIONS]
    draft["title"] = target.get("title") or draft["title"]
    draft["video_title"] = target.get("video_title")
    draft["category"] = target.get("category") or draft["category"]
    draft["target_minutes"] = target.get("target_minutes")
    draft["sections"] = _clean_sections(target.get("sections") or [])
    draft["analysis"] = None
    draft["fact_check"] = None
    draft["updated_at"] = _now_iso()
    save_store(user_id, store)
    return _public_draft(draft, detail=True)


# ---------------------------------------------------------------------
# AI actions (llm_provider only; proposals, never silent overwrites)
# ---------------------------------------------------------------------


def ai_available() -> bool:
    return llm_provider.is_configured()


def _require_ai():
    if not ai_available():
        raise ScriptError(
            503,
            "script_ai_unavailable",
            "Script AI is unavailable: no LLM provider is configured "
            "on this deployment.",
        )


def _is_not_configured(error) -> bool:
    """Phase 4b: the typed not-configured provider failure (with the
    pre-D-01 message shape kept as a fallback for stub callables)."""
    if isinstance(error, llm_provider.LLMProviderError):
        return error.kind == "not_configured"
    return "BREWKEG_API_KEY is not configured" in str(error)


def _ai_call(fn, *args, **kwargs):
    """Run one provider call, mapping provider failures onto the
    studio's error shape. An unconfigured provider is a 503 (the
    capability is absent); a provider that answered badly or not at
    all is a 502 (the capability failed). Neither is ever masked by
    canned text."""
    _require_ai()
    try:
        return fn(*args, **kwargs)
    except RuntimeError as error:
        if _is_not_configured(error):
            raise ScriptError(
                503,
                "script_ai_unavailable",
                "Script AI is unavailable: no LLM provider is configured "
                "on this deployment.",
            ) from error
        raise ScriptError(
            502, "script_ai_failed", f"The AI provider failed: {error}"
        ) from error


_WRITER_SYSTEM = (
    "You are a documentary scriptwriter for a faceless YouTube "
    "documentary channel. Write original narration prose: concrete, "
    "factual in tone, no filler, no generic AI phrasing, no "
    "self-reference, no markdown, no stage directions. Write only "
    "the narration text you are asked for. " + SCRIPT_RULES_TEXT
)


def _research_context_block(record: dict) -> str:
    """The channel-research evidence a draft is developed from,
    shaped for the generation prompts: the analysed videos (with
    the basis each rests on), the patterns the analysis found,
    and the original ideas it proposed. Patterns and structure
    lessons only — the studied channel's subjects and expression
    are never material to copy."""
    analysis = record.get("analysis") or {}
    channel = record.get("channel") or {}
    lines = [
        "Channel research the user gathered and chose to develop "
        f"from — channel: {channel.get('title') or 'unknown'}.",
    ]
    videos = analysis.get("videos") or []
    if videos:
        lines.append("Analysed videos (basis = what was actually accessible):")
        for video in videos[:3]:
            lines.append(
                f"- {video.get('title') or 'Untitled'} "
                f"(basis: {video.get('basis') or 'metadata'})"
            )
    patterns = analysis.get("patterns") or []
    if patterns:
        lines.append("Patterns found in those videos:")
        for item in patterns[:6]:
            if isinstance(item, dict) and item.get("pattern"):
                lines.append(f"- {item['pattern']}")
    ideas = analysis.get("ideas") or []
    if ideas:
        lines.append("Original ideas the analysis proposed:")
        for idea in ideas[:6]:
            if isinstance(idea, dict) and idea.get("title"):
                lines.append(
                    f"- {idea['title']}: {idea.get('angle') or ''}"
                )
    lines.append(
        "Apply the patterns and structural lessons where they "
        "genuinely fit this topic; do not copy the studied "
        "channel's subjects, scripts or distinctive expression."
    )
    return "\n".join(lines)


def _outline_prompt(topic: str, angle: str, target_minutes: float,
                    category: str, language: str = "en",
                    research_context: str = "") -> str:
    target_words = int(round(target_minutes * WORDS_PER_MINUTE))
    return (
        f"Topic: {topic}\n"
        f"Angle: {angle or 'the most compelling truthful angle the facts support'}\n"
        f"Category: {category}\n"
        f"Target runtime: {target_minutes:g} minutes "
        f"(about {target_words} words of narration at "
        f"{WORDS_PER_MINUTE} words per minute).\n\n"
        + (
            f"Language: write every heading and intent in "
            f"{language_name(language)}.\n\n"
            if language != "en"
            else ""
        )
        + (f"{research_context}\n\n" if research_context else "")
        + "Design the structure of an ORIGINAL documentary script on "
        "this topic: an opening hook section, the story sections in "
        "order, and a closing section. Give each section a short "
        "heading, a one-sentence intent (what it must establish), "
        "and a word_budget. The budgets must sum to about the "
        f"target. At most {MAX_OUTLINE_SECTIONS} sections.\n\n"
        'Return JSON: {"sections": [{"heading": str, "intent": str, '
        '"word_budget": int}]}'
    )


def _section_prompt(topic: str, heading: str, intent: str,
                    word_budget: int, position: str,
                    language: str = "en") -> str:
    language_line = (
        f"Write the narration in {language_name(language)}.\n"
        if language != "en"
        else ""
    )
    return (
        f"Documentary topic: {topic}\n"
        f"Section ({position}): {heading}\n"
        f"What this section must establish: {intent}\n"
        + language_line
        + f"Write about {word_budget} words of narration for this "
        "section only. Plain prose paragraphs, nothing else. "
        + SCRIPT_RULES_TEXT
    )


def generate_draft(user_id, *, topic: str, angle: str = "",
                   category: str = "", target_minutes=None,
                   language: str = "en",
                   research_id: str | None = None) -> dict:
    """Draft-from-brief (audit doc 02 §3 steps 4-5): outline via
    llm_json, then one llm_text call per section under its recorded
    word budget. The result is a NEW draft — generation never
    touches an existing draft's words."""
    topic = str(topic or "").strip()
    if not topic:
        raise ScriptError(422, "topic_required", "A topic or brief is required.")
    # Item 6: the script language is chosen before generation.
    # Unknown languages refuse; every known language can be
    # WRITTEN (narration support is a separate, production-side
    # fact enforced at run submission).
    language = str(language or "en").strip().lower()
    if not languages_service.is_known(language):
        raise ScriptError(
            422,
            "language_unknown",
            f"Language {language!r} is not supported for script "
            "generation.",
        )
    minutes = _clean_target(target_minutes) or 10.0
    if minutes < MIN_TARGET_MINUTES or minutes > MAX_TARGET_MINUTES:
        raise ScriptError(
            422,
            "target_out_of_range",
            f"Generated scripts target {MIN_TARGET_MINUTES}–"
            f"{MAX_TARGET_MINUTES} minutes (audit doc 02 §3).",
        )
    category = str(category or "").strip() or DEFAULT_CATEGORY
    # Channel research: a supplied research_id must name a real
    # record belonging to this user, and its analysis genuinely
    # informs the outline — the link is not metadata-only.
    research_context = ""
    research_channel = None
    if research_id:
        from services import channel_research

        try:
            record = channel_research.get_research(user_id, research_id)
        except channel_research.ChannelResearchError as error:
            raise ScriptError(
                error.status_code, error.code, error.message
            ) from error
        research_context = _research_context_block(record)
        research_channel = (record.get("channel") or {}).get("title")
    outline = _ai_call(
        llm_provider.complete_json,
        "You design documentary script structures. Techniques and "
        "structure only — you never copy another creator's script "
        "or distinctive expression.",
        _outline_prompt(
            topic, str(angle or "").strip(), minutes, category,
            language,
            research_context=research_context,
        ),
        max_tokens=3000,
        usage_context={
            "user_id": user_id,
            "context": "script_studio_draft",
        },
    )
    raw_sections = (outline or {}).get("sections") if isinstance(outline, dict) else None
    if not raw_sections or not isinstance(raw_sections, list):
        raise ScriptError(
            502,
            "script_ai_failed",
            "The AI provider returned no usable outline; no draft was created.",
        )
    plan = []
    for raw in raw_sections[:MAX_OUTLINE_SECTIONS]:
        if not isinstance(raw, dict):
            continue
        heading = str(raw.get("heading") or "").strip()
        if not heading:
            continue
        try:
            budget = int(raw.get("word_budget") or 0)
        except (TypeError, ValueError):
            budget = 0
        plan.append(
            {
                "heading": heading[:200],
                "intent": str(raw.get("intent") or "").strip(),
                "word_budget": budget if budget > 0 else 150,
            }
        )
    if not plan:
        raise ScriptError(
            502,
            "script_ai_failed",
            "The AI provider returned an empty outline; no draft was created.",
        )
    sections = []
    removed_total = 0
    total = len(plan)
    for index, item in enumerate(plan):
        position = (
            "opening" if index == 0
            else "closing" if index == total - 1
            else f"{index + 1} of {total}"
        )
        body = _ai_call(
            llm_provider.complete_text,
            _WRITER_SYSTEM,
            _section_prompt(topic, item["heading"], item["intent"],
                            item["word_budget"], position, language),
            max_tokens=8000,
            usage_context={
                "user_id": user_id,
                "context": "script_studio_draft",
            },
        )
        clean_body, body_removed = sanitize_narration_body(
            str(body or "")
        )
        removed_total += body_removed
        sections.append(
            {"id": _new_id("sec"), "heading": item["heading"], "body": clean_body}
        )
    view = create_draft(
        user_id,
        title=topic[:300],
        category=category,
        target_minutes=minutes,
        sections=sections,
        language=language,
    )
    generation = {
        "topic": topic,
        "angle": str(angle or "").strip(),
        "language": language,
        # Channel research: the research record this script was
        # developed from, so its references stay attached to the
        # draft (and visible beside it) permanently.
        "research_id": research_id,
        "research_channel": research_channel,
        "outline": plan,
        "basis": "llm_generated_from_user_brief",
        "at": _now_iso(),
        "script_rules_sanitised": removed_total,
    }
    store = load_store(user_id)
    record = _find_draft(store, view["id"])
    record["generation"] = generation
    # Item 3: every generated script automatically receives a
    # compelling video title, distinct from the working topic.
    # Title generation is best-effort: a title failure must never
    # destroy a completed script — the draft then simply carries
    # no video title until one is set.
    video_title = _generate_video_title(
        user_id, topic=topic, category=category, sections=sections
    )
    if video_title:
        record["video_title"] = video_title
    save_store(user_id, store)
    view["generation"] = generation
    view["video_title"] = record.get("video_title")
    return view


def _generate_video_title(user_id, *, topic: str, category: str,
                          sections: list[dict]) -> str | None:
    """One AI call for the video title (item 3). The title is the
    documentary's public name — compelling and specific, never a
    restatement of the topic, never inside the narration. Returns
    None on any failure or unusable answer."""
    opening = ""
    for section in sections:
        if str(section.get("body") or "").strip():
            opening = str(section["body"]).strip()[:600]
            break
    prompt = (
        f"Documentary topic (working brief, NOT the title): {topic}\n"
        f"Category: {category}\n"
        f"Opening narration excerpt: {opening}\n\n"
        "Write ONE compelling, specific title for this documentary "
        "video — the title a viewer would see. It must be "
        "interesting on its own, truthful to the subject, and "
        "clearly different from the topic wording above. No "
        "quotation marks, no clickbait deception, at most 100 "
        "characters. Return only the title."
    )
    try:
        raw = _ai_call(
            llm_provider.complete_text,
            "You write documentary video titles.",
            prompt,
            max_tokens=120,
            usage_context={
                "user_id": user_id,
                "context": "script_studio_title",
            },
        )
    except Exception:
        return None
    title = str(raw or "").strip().strip('"').strip()
    title = title.splitlines()[0].strip() if title else ""
    if not title or len(title) > 300:
        return None
    if title.lower() == str(topic or "").strip().lower():
        return None
    return title


def expand_sections(user_id, script_id: str) -> dict:
    """Expand-outline proposal: one llm_text call per section,
    writing (or expanding) its body toward the section's share of
    the draft's target. Returns a PROPOSAL — the stored draft is
    not modified; the client applies it through the update path."""
    store = load_store(user_id)
    draft = _find_draft(store, script_id)
    sections = draft.get("sections") or []
    if not sections:
        raise ScriptError(422, "sections_required", "The draft has no sections.")
    target_words = draft_metrics(draft)["target_words"]
    per_section = (
        max(80, int(round(target_words / len(sections)))) if target_words
        else 150
    )
    proposed = []
    for section in sections:
        existing = str(section.get("body") or "").strip()
        if existing:
            prompt = (
                f"Documentary: {draft.get('title')}\n"
                f"Section: {section.get('heading') or 'Untitled section'}\n"
                f"Expand this narration to about {per_section} words, "
                "keeping every fact and the author's voice, adding "
                "only connective and contextual prose that the "
                "existing text supports:\n\n" + existing
            )
        else:
            prompt = (
                f"Documentary: {draft.get('title')}\n"
                f"Section: {section.get('heading') or 'Untitled section'}\n"
                f"Write about {per_section} words of narration for "
                "this section, grounded only in what the section "
                "heading and the documentary's subject support. Do "
                "not invent specific dates, names or figures."
            )
        body = _ai_call(
            llm_provider.complete_text, _WRITER_SYSTEM, prompt, max_tokens=8000
        )
        clean_body, _removed = sanitize_narration_body(str(body or ""))
        proposed.append(
            {
                "id": section.get("id"),
                "heading": section.get("heading"),
                "body": clean_body,
                "word_budget": per_section,
            }
        )
    return {
        "script_id": script_id,
        "proposal": {"sections": proposed},
        "basis": "llm_expansion_proposal",
    }


def tighten_passage(user_id, script_id: str, *, section_id: str,
                    text: str | None = None) -> dict:
    """Tighten proposal for one passage: returns the tightened text
    beside the original. The stored draft is not modified."""
    store = load_store(user_id)
    draft = _find_draft(store, script_id)
    section = None
    for candidate in draft.get("sections") or []:
        if candidate.get("id") == section_id:
            section = candidate
            break
    if section is None:
        raise ScriptError(404, "section_not_found", "No such section in this draft.")
    original = str(text if text is not None else section.get("body") or "").strip()
    if not original:
        raise ScriptError(422, "text_required", "There is no passage text to tighten.")
    prompt = (
        "Tighten this documentary narration passage: cut filler and "
        "repetition, sharpen the sentences, keep every fact, name, "
        "date and figure exactly as written, and keep the author's "
        "voice. Return only the tightened passage.\n\n" + original
    )
    tightened = _ai_call(
        llm_provider.complete_text, _WRITER_SYSTEM, prompt, max_tokens=8000
    )
    clean_tightened, _removed = sanitize_narration_body(
        str(tightened or "")
    )
    return {
        "script_id": script_id,
        "proposal": {
            "section_id": section_id,
            "original": original,
            "tightened": clean_tightened,
            "original_words": _count_words(original),
            "tightened_words": _count_words(clean_tightened),
        },
        "basis": "llm_tighten_proposal",
    }


# ---------------------------------------------------------------------
# Analysis + fact check (the pipeline's own machinery, surfaced)
# ---------------------------------------------------------------------


def _run_analysis(text: str, script_id: str) -> dict:
    llm = "auto" if ai_available() else None
    return script_analyzer.analyze_script(
        text, project_id=f"script_studio:{script_id}", llm=llm
    )


def _analysis_view(analysis: dict) -> dict:
    bible = analysis.get("bible") or {}
    scenes = []
    for scene in analysis.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        scenes.append(
            {
                "scene_number": scene.get("scene_number"),
                "sentence_count": scene.get("sentence_count"),
                "sentence_numbers": [
                    s.get("sentence_number")
                    for s in scene.get("sentences") or []
                    if isinstance(s, dict)
                ],
                "text": scene.get("text"),
            }
        )
    return {
        "mode": analysis.get("mode"),
        "sentence_count": analysis.get("sentence_count"),
        "sentences": [
            {
                "sentence_number": s.get("sentence_number"),
                "text": s.get("text"),
                "role": s.get("role"),
                "subject_id": s.get("subject_id"),
                "degraded": bool(s.get("degraded")),
            }
            for s in analysis.get("sentences") or []
            if isinstance(s, dict)
        ],
        "scenes": scenes,
        "bible": {
            "title": bible.get("title"),
            "story_type": bible.get("story_type"),
            "tone": bible.get("tone"),
            "primary_subject_id": bible.get("primary_subject_id"),
            "entities": [
                {
                    "canonical": e.get("canonical"),
                    "type": e.get("type"),
                    "aliases": e.get("aliases") or [],
                }
                for e in bible.get("entities") or []
                if isinstance(e, dict)
            ],
        },
        "entities": analysis.get("entities") or [],
        "locations": analysis.get("locations") or [],
        "dates": analysis.get("dates") or [],
    }


def run_analysis(user_id, script_id: str) -> dict:
    store = load_store(user_id)
    draft = _find_draft(store, script_id)
    text = assemble_script(draft)
    if not text:
        raise ScriptError(
            422, "script_empty", "Write some script text before analysing it."
        )
    analysis = _run_analysis(text, script_id)
    view = _analysis_view(analysis)
    view["at"] = _now_iso()
    draft["analysis"] = view
    draft["updated_at"] = _now_iso()
    save_store(user_id, store)
    return {"script_id": script_id, "analysis": view}


def run_fact_check(user_id, script_id: str) -> dict:
    store = load_store(user_id)
    draft = _find_draft(store, script_id)
    text = assemble_script(draft)
    if not text:
        raise ScriptError(
            422, "script_empty", "Write some script text before fact-checking it."
        )
    analysis = _run_analysis(text, script_id)
    table = script_facts.build_fact_table(
        analysis.get("sentences") or [], bible=analysis.get("bible")
    )
    facts = table.get("facts") or []
    record = script_facts.corroborate_facts(facts)
    view = {
        "at": _now_iso(),
        "mode": analysis.get("mode"),
        "record": table.get("record"),
        "corroboration": record,
        "facts": facts,
    }
    draft["fact_check"] = view
    draft["updated_at"] = _now_iso()
    save_store(user_id, store)
    return {"script_id": script_id, "fact_check": view}


# ---------------------------------------------------------------------
# Send to production / attach (the normal project paths)
# ---------------------------------------------------------------------


def translate_draft(user_id, script_id: str, *, language: str) -> dict:
    """Translate a draft into another language as a NEW draft
    (item 6): the original is kept unchanged beside the
    translation, each labelled with its own language — the same
    honesty rule the Localization Studio follows. Translation is
    per section through the provider, and a section whose
    "translation" comes back byte-identical to the source is
    refused rather than passed off as translated."""
    store = load_store(user_id)
    draft = _find_draft(store, script_id)
    source_language = str(draft.get("language") or "en").lower()
    target = str(language or "").strip().lower()
    if not languages_service.is_known(target):
        raise ScriptError(
            422, "language_unknown",
            f"Language {target!r} is not supported for translation.",
        )
    if target == source_language:
        raise ScriptError(
            422, "same_language",
            "The draft is already in that language.",
        )
    sections = draft.get("sections") or []
    if not any(str(sec.get("body") or "").strip() for sec in sections):
        raise ScriptError(
            422, "script_empty",
            "Write some script text before translating it.",
        )
    _require_ai()
    translated_sections = []
    for section in sections:
        body = str(section.get("body") or "")
        heading = str(section.get("heading") or "")
        if not body.strip():
            translated_sections.append(
                {"heading": heading, "body": ""}
            )
            continue
        result = _ai_call(
            llm_provider.complete_json,
            "You translate documentary narration faithfully. You "
            "never summarise, add or omit content.",
            (
                f"Translate this documentary script section from "
                f"{language_name(source_language)} to "
                f"{language_name(target)}. Keep every fact, name, "
                f"date and figure exactly. Return JSON: "
                f'{{"heading": str, "body": str}}\n\n'
                f"Heading: {heading}\n\nBody:\n{body}"
            ),
            max_tokens=8000,
            usage_context={
                "user_id": user_id,
                "context": "script_studio_translate",
            },
        )
        new_body = str((result or {}).get("body") or "").strip()
        new_heading = str((result or {}).get("heading") or "").strip()
        if not new_body:
            raise ScriptError(
                502, "script_ai_failed",
                "The translation came back empty; no draft was created.",
            )
        if new_body == body.strip():
            raise ScriptError(
                502, "script_ai_failed",
                "The translation was identical to the source text; "
                "no draft was created.",
            )
        clean_body, _removed = sanitize_narration_body(new_body)
        translated_sections.append(
            {"heading": new_heading or heading, "body": clean_body}
        )
    view = create_draft(
        user_id,
        title=(
            f"{draft.get('title') or 'Untitled script'} — "
            f"{language_name(target)} script"
        )[:300],
        category=draft.get("category"),
        target_minutes=draft.get("target_minutes"),
        sections=translated_sections,
        video_title=draft.get("video_title"),
        language=target,
    )
    store = load_store(user_id)
    record = _find_draft(store, view["id"])
    record["translated_from"] = {
        "script_id": script_id,
        "source_language": source_language,
        "at": _now_iso(),
    }
    save_store(user_id, store)
    view["translated_from"] = record["translated_from"]
    return view


def send_to_production(user_id, script_id: str) -> dict:
    """Create a project from the draft through the normal creation
    path's own steps: plan script-length enforcement, the Project
    row, and the versioning script snapshot the create route
    records. Returns the new project's identity; the draft keeps
    the link."""
    from database import SessionLocal
    from models import Project
    from services import plans as plans_service
    from services import versioning

    store = load_store(user_id)
    draft = _find_draft(store, script_id)
    text = assemble_script(draft)
    if not text:
        raise ScriptError(
            422, "script_empty", "Write some script text before sending it to production."
        )
    db = SessionLocal()
    try:
        plans_service.enforce_script_length(db, user_id, text)
        project = Project(
            id=str(uuid.uuid4()),
            owner_id=user_id,
            # Item 3: the production carries the video title (the
            # documentary's public name) when one exists; the
            # working title is only the fallback.
            title=(
                draft.get("video_title")
                or draft.get("title")
                or "Untitled script"
            ),
            script=text,
            category=draft.get("category") or DEFAULT_CATEGORY,
            # Item 6: the draft's language becomes the project's
            # authoritative language (flat key; the run gate
            # enforces narration support before charging). The
            # duration the user chose for the script is carried
            # into the project too, so the handoff keeps it.
            options=(
                {
                    **(
                        {"language": draft["language"]}
                        if draft.get("language")
                        else {}
                    ),
                    **(
                        {"target_minutes": draft["target_minutes"]}
                        if draft.get("target_minutes")
                        else {}
                    ),
                }
                or None
            ),
            status="created",
        )
        db.add(project)
        db.commit()
        project_id = project.id
    finally:
        db.close()
    versioning.try_record_script_snapshot(
        project_id, text, "script_studio", actor=user_id
    )
    draft["project_id"] = project_id
    draft.setdefault("productions", []).append(
        {"project_id": project_id, "at": _now_iso(), "via": "send_to_production"}
    )
    draft["updated_at"] = _now_iso()
    save_store(user_id, store)
    return {
        "script_id": script_id,
        "project_id": project_id,
        "draft": _public_draft(draft, detail=True),
    }


def attach_to_project(user_id, script_id: str, project_id: str) -> dict:
    """Write the draft's assembled script onto an existing project
    (ownership is enforced by the route's project guard before this
    runs) and snapshot it through versioning — the same mechanism a
    versioning restore uses to set a project's script."""
    from database import SessionLocal
    from models import Project
    from services import versioning

    store = load_store(user_id)
    draft = _find_draft(store, script_id)
    text = assemble_script(draft)
    if not text:
        raise ScriptError(
            422, "script_empty", "Write some script text before attaching it."
        )
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is None:
            raise ScriptError(404, "project_not_found", "No such project.")
        project.script = text
        db.commit()
    finally:
        db.close()
    versioning.try_record_script_snapshot(
        project_id, text, "script_studio_attach", actor=user_id
    )
    draft["project_id"] = project_id
    draft.setdefault("productions", []).append(
        {"project_id": project_id, "at": _now_iso(), "via": "attach"}
    )
    draft["updated_at"] = _now_iso()
    save_store(user_id, store)
    return {
        "script_id": script_id,
        "project_id": project_id,
        "draft": _public_draft(draft, detail=True),
    }
