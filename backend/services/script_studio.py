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

from services import llm_provider, media_paths, script_analyzer, script_facts
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
                 sections=None) -> dict:
    store = load_store(user_id)
    draft = {
        "id": _new_id("script"),
        "title": (str(title).strip() if title else "") or "Untitled script",
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
        "category": draft.get("category"),
        "target_minutes": draft.get("target_minutes"),
        "sections": [
            {"id": s.get("id"), "heading": s.get("heading"), "body": s.get("body")}
            for s in draft.get("sections") or []
        ],
    }


def update_draft(user_id, script_id: str, *, title=None, category=None,
                 target_minutes="__unset__", sections=None,
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
        (title is not None and str(title).strip() != (draft.get("title") or ""))
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
    "the narration text you are asked for."
)


def _outline_prompt(topic: str, angle: str, target_minutes: float,
                    category: str) -> str:
    target_words = int(round(target_minutes * WORDS_PER_MINUTE))
    return (
        f"Topic: {topic}\n"
        f"Angle: {angle or 'the most compelling truthful angle the facts support'}\n"
        f"Category: {category}\n"
        f"Target runtime: {target_minutes:g} minutes "
        f"(about {target_words} words of narration at "
        f"{WORDS_PER_MINUTE} words per minute).\n\n"
        "Design the structure of an ORIGINAL documentary script on "
        "this topic: an opening hook section, the story sections in "
        "order, and a closing section. Give each section a short "
        "heading, a one-sentence intent (what it must establish), "
        "and a word_budget. The budgets must sum to about the "
        f"target. At most {MAX_OUTLINE_SECTIONS} sections.\n\n"
        'Return JSON: {"sections": [{"heading": str, "intent": str, '
        '"word_budget": int}]}'
    )


def _section_prompt(topic: str, heading: str, intent: str,
                    word_budget: int, position: str) -> str:
    return (
        f"Documentary topic: {topic}\n"
        f"Section ({position}): {heading}\n"
        f"What this section must establish: {intent}\n"
        f"Write about {word_budget} words of narration for this "
        "section only. Plain prose paragraphs, nothing else."
    )


def generate_draft(user_id, *, topic: str, angle: str = "",
                   category: str = "", target_minutes=None) -> dict:
    """Draft-from-brief (audit doc 02 §3 steps 4-5): outline via
    llm_json, then one llm_text call per section under its recorded
    word budget. The result is a NEW draft — generation never
    touches an existing draft's words."""
    topic = str(topic or "").strip()
    if not topic:
        raise ScriptError(422, "topic_required", "A topic or brief is required.")
    minutes = _clean_target(target_minutes) or 10.0
    if minutes < MIN_TARGET_MINUTES or minutes > MAX_TARGET_MINUTES:
        raise ScriptError(
            422,
            "target_out_of_range",
            f"Generated scripts target {MIN_TARGET_MINUTES}–"
            f"{MAX_TARGET_MINUTES} minutes (audit doc 02 §3).",
        )
    category = str(category or "").strip() or DEFAULT_CATEGORY
    outline = _ai_call(
        llm_provider.complete_json,
        "You design documentary script structures. Techniques and "
        "structure only — you never copy another creator's script "
        "or distinctive expression.",
        _outline_prompt(topic, str(angle or "").strip(), minutes, category),
        max_tokens=3000,
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
                            item["word_budget"], position),
            max_tokens=8000,
        )
        sections.append(
            {"id": _new_id("sec"), "heading": item["heading"], "body": str(body or "").strip()}
        )
    view = create_draft(
        user_id,
        title=topic[:300],
        category=category,
        target_minutes=minutes,
        sections=sections,
    )
    generation = {
        "topic": topic,
        "angle": str(angle or "").strip(),
        "outline": plan,
        "basis": "llm_generated_from_user_brief",
        "at": _now_iso(),
    }
    store = load_store(user_id)
    record = _find_draft(store, view["id"])
    record["generation"] = generation
    save_store(user_id, store)
    view["generation"] = generation
    return view


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
        proposed.append(
            {
                "id": section.get("id"),
                "heading": section.get("heading"),
                "body": str(body or "").strip(),
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
    return {
        "script_id": script_id,
        "proposal": {
            "section_id": section_id,
            "original": original,
            "tightened": str(tightened or "").strip(),
            "original_words": _count_words(original),
            "tightened_words": _count_words(str(tightened or "")),
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
            title=draft.get("title") or "Untitled script",
            script=text,
            category=draft.get("category") or DEFAULT_CATEGORY,
            options=None,
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
