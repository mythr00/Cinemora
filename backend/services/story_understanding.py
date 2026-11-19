"""
Universal story-understanding layer for the documentary pipeline.

SCRIPT -> CASE BIBLE -> SENTENCE MEANING/ROLE -> VISUAL SCENARIOS
       -> SEARCH INTENT -> SEARCH QUERIES -> CANDIDATE VERIFICATION

Design rules
  * The LLM does the *understanding* (who/what/why/what to show).
  * Code does the *enforcement*: entities can only come from the case bible,
    queries are composed from structured fields (never from raw sentence
    words), and candidates are verified against the scenario.
  * No story-specific words, names or stopword lists. The only fixed list is
    the closed grammatical class of pronouns used as a fallback.

Plug-in point: pass any callable  llm(system: str, user: str) -> str
that returns the model's text (JSON expected). Works with Grok, Claude,
OpenAI, a local model, etc.
"""

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Stage 3A: the research pass resolves services.search_provider's
# search_web LAZILY at call time (see _resolve_search_web), so this
# module never imports the provider at module load and tests can patch
# either services.search_provider.search_web or this attribute.
search_web = None

SENTENCE_ROLES = [
    "hook", "setup", "background", "action", "event", "evidence",
    "motivation", "reflection", "legal", "aftermath", "transition", "conclusion",
]
SCENARIO_KINDS = [
    "person_activity", "place", "document", "event", "object",
    "archival", "abstract_broll",
]
SPECIFICITY = ["exact", "representative", "generic"]

# F-1 (format intelligence): the visual FORMAT that would best communicate
# a sentence. This is a ranking signal for the Asset Manager's unified
# selection — it never mandates a format and never fabricates an asset.
FORMAT_INTENTS = [
    "footage", "photograph", "document", "map", "chart", "graphic", "any",
]

_CHART_RE = re.compile(
    r"(\$\s?\d|\d[\d,.]*\s?(percent|%)|\b(million|billion|trillion)\b|"
    r"\b(statistics|casualties|death toll|population|unemployment|inflation)\b)",
    re.IGNORECASE,
)
_DOCUMENT_RE = re.compile(
    r"\b(court|trial|verdict|sentenced|signed|treaty|decree|official record|"
    r"document|letter (stating|saying)|report (stated|found)|law (stated|said))\b",
    re.IGNORECASE,
)
_MAP_RE = re.compile(
    r"\b(border|boundary|river|coast|island|peninsula|territory|region|"
    r"located|north of|south of|east of|west of|map)\b",
    re.IGNORECASE,
)
_GRAPHIC_RE = re.compile(
    r"\b(how (it|the|a)|works by|process|mechanism|cycle|step by step|system)\b",
    re.IGNORECASE,
)
_FOOTAGE_RE = re.compile(
    r"\b(crowd|marched|attacked|opened|fired|celebrated|speech|ceremony|"
    r"gathered|stormed|parade|protest|riots?|battle|explosion)\b",
    re.IGNORECASE,
)


def infer_format_intent(sentence_text, scenario=None):
    """Heuristic format intent for a sentence (degraded mode + fallback).

    Returns (intent, confidence). Deliberately conservative: strong,
    specific communicative signals (numbers -> chart, records -> document,
    geography -> map) outrank generic ones; no signal -> ("any", 0.0),
    which leaves selection to pure relevance.
    """
    scenario = scenario or {}
    kind = str(scenario.get("kind") or "").lower()
    text = f"{sentence_text or ''} {scenario.get('visual') or ''}"

    if kind == "document" or _DOCUMENT_RE.search(text):
        return "document", 0.85
    if _CHART_RE.search(text):
        return "chart", 0.9
    if _MAP_RE.search(text):
        return "map", 0.6
    if _GRAPHIC_RE.search(text):
        return "graphic", 0.4
    if _FOOTAGE_RE.search(text):
        return "footage", 0.6
    if kind == "archival":
        return "photograph", 0.5
    return "any", 0.0

# Closed grammatical class (not a vocabulary list): only used as a fallback
# to decide that a sentence refers back to the established subject.
PRONOUNS = {"he", "him", "his", "she", "her", "hers", "they", "them", "their", "it", "its"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def parse_json_loose(text):
    if text is None:
        return None
    text = str(text).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        return None
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    if end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s'-]", " ", str(text or "").lower())).strip()


def _tokens(text):
    return re.findall(r"[\w'-]+", str(text or ""))


def _stem(word):
    word = str(word or "").lower()
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _content_tokens(text, min_len=4):
    return {_stem(t) for t in _norm(text).split() if len(t) >= min_len}


def _call_llm_json(llm, system, user, retries=0):
    if llm is None:
        return None
    for _ in range(retries + 1):
        try:
            data = parse_json_loose(llm(system, user))
        except Exception as error:  # network / provider error
            print(f"STORY LLM ERROR: {error}")
            data = None
        if data is not None:
            return data
    return None


# --------------------------------------------------------------------------
# 1. CASE BIBLE
# --------------------------------------------------------------------------

BIBLE_SYSTEM = """You are a documentary researcher. Read the whole script and build a CASE BIBLE:
the real entities and context that every sentence must be interpreted against.

Return ONLY JSON:
{
  "title": "short title of the story",
  "story_type": "true_crime | history | biography | business | finance | disaster | military | other",
  "time_period": "overall era(s) covered",
  "primary_subject_id": "id of the entity the story is mainly about",
  "entities": [
    {
      "id": "short_snake_case_id",
      "canonical": "full proper name exactly as it should be searched",
      "type": "person | place | organization | event | object",
      "role": "e.g. main subject, victim, investigator, location of crimes, court",
      "aliases": ["other names used in the script for the SAME entity"],
      "pronoun": "he | she | they | it | null",
      "description": "one line of who/what this is in this story"
    }
  ],
  "timeline": [{"when": "date or period", "what": "what happened"}]
}

Rules:
- Only include real entities that matter to the story. Never include ordinary
  words, sentence openers, conjunctions, or time words as entities.
- One entity per real-world thing: a person's full name and any short forms
  (first name, last name, nickname) are ONE entity, listed as aliases.
- A place is a place (city, state, house, church), not part of a sentence.
- Do not invent facts that are not supported by the script."""


def build_case_bible(script_text, llm, sentences=None, max_chars=24000):
    text = script_text
    if len(text) > max_chars:  # keep beginning, middle, end
        third = max_chars // 3
        mid = len(text) // 2
        text = (
            text[:third]
            + "\n[...]\n" + text[mid - third // 2: mid + third // 2]
            + "\n[...]\n" + text[-third:]
        )

    data = _call_llm_json(llm, BIBLE_SYSTEM, f"SCRIPT:\n{text}")
    used_llm = True
    if not isinstance(data, dict) or not data.get("entities"):
        print("STORY: LLM case bible unavailable -> heuristic bible (DEGRADED MODE).")
        used_llm = False
        fallback = sentences or [p for p in re.split(r"(?<=[.!?])\s+", script_text) if p.strip()]
        data = heuristic_bible(fallback)

    cleaned = []
    seen = set()
    for entity in data.get("entities", []):
        if not isinstance(entity, dict):
            continue
        canonical = str(entity.get("canonical") or "").strip()
        eid = str(entity.get("id") or _norm(canonical).replace(" ", "_")).strip()
        if not canonical or not eid or eid in seen:
            continue
        seen.add(eid)
        cleaned.append({
            "id": eid,
            "canonical": canonical,
            "type": str(entity.get("type") or "person").lower(),
            "role": entity.get("role") or "",
            "aliases": [str(a).strip() for a in entity.get("aliases", []) if str(a).strip()],
            "pronoun": (entity.get("pronoun") or None),
            "description": entity.get("description") or "",
        })
    data["entities"] = cleaned
    if data.get("primary_subject_id") not in seen:
        data["primary_subject_id"] = cleaned[0]["id"] if cleaned else None

    data["by_id"] = {e["id"]: e for e in cleaned}
    data["alias_index"] = build_alias_index(cleaned)
    # MA-27: one vocabulary end-to-end — the bible is either
    # LLM-produced ("llm") or it fell back ("degraded"). The run-level
    # mode derived in plan_script adds "partial_llm" between them;
    # the old "heuristic" string disagreed with the asset report's
    # "degraded" and is gone.
    data["mode"] = "llm" if used_llm else "degraded"
    return data


def build_alias_index(entities):
    """
    alias (normalized) -> entity id.
    Short forms of person names are derived structurally (first/last token).
    An alias that points at two different entities is dropped as ambiguous.
    """
    index = {}
    ambiguous = set()

    def add(alias, eid):
        key = _norm(alias)
        if len(key) < 3:
            return
        if key in index and index[key] != eid:
            ambiguous.add(key)
        index[key] = eid

    for entity in entities:
        names = {entity["canonical"], *entity["aliases"]}
        if entity["type"] == "person":
            parts = entity["canonical"].split()
            if len(parts) >= 2:
                names.update({parts[0], parts[-1]})
        for name in names:
            add(name, entity["id"])

    for key in ambiguous:
        index.pop(key, None)
    return index


def find_mentions(text, alias_index):
    """
    Longest-match, non-overlapping mentions of bible entities in text.
    'Dennis Rader' yields ONE mention, never 'Dennis' + 'Rader' as well.
    Returns list of entity ids in order of appearance.
    """
    lowered = str(text or "").lower()
    taken = [False] * len(lowered)
    found = []
    for alias in sorted(alias_index, key=len, reverse=True):
        for match in re.finditer(r"(?<![\w])" + re.escape(alias) + r"(?![\w])", lowered):
            a, b = match.span()
            if any(taken[a:b]):
                continue
            for i in range(a, b):
                taken[i] = True
            found.append((a, alias_index[alias]))
    found.sort()
    ordered = []
    for _, eid in found:
        if eid not in ordered:
            ordered.append(eid)
    return ordered


# --------------------------------------------------------------------------
# 1b. RESEARCH-GROUNDED BIBLE (Stage 3A — audit MA-32, review §2.11)
# --------------------------------------------------------------------------
# After the bible is built, a bounded web-research pass grounds it:
# the most-mentioned subjects are searched (via
# services.search_provider.search_web ONLY — never Serper directly),
# the retrieved sources are attached to their bible entities, bible
# facts are labelled "externally_supported" / "script_asserted", and
# disagreements between sources are recorded as conflicts — never
# resolved silently. The whole pass is FAIL-SOFT: a missing provider,
# a raising search, or any unexpected error leaves the bible usable
# with every fact "script_asserted".

_RESEARCH_MAX_SUBJECTS = 12
_RESEARCH_MAX_SOURCES_PER_SUBJECT = 10
_RESEARCH_FAILURE_LIMIT = 3  # total failed queries before giving up

_NUMBER_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+(?:\.\d+)?\b")
_YEAR_RE = re.compile(r"\b(?:1[5-9]\d\d|20\d\d)\b")


def _resolve_search_web():
    """The research search function, resolved at call time.

    A callable patched onto this module's ``search_web`` attribute
    wins; otherwise services.search_provider.search_web is imported
    lazily. Returns None when no provider is importable.
    """
    fn = globals().get("search_web")
    if callable(fn):
        return fn
    try:
        from services.search_provider import search_web as resolved
        return resolved
    except Exception:
        pass
    try:
        import search_provider

        return getattr(search_provider, "search_web", None)
    except Exception:
        return None


def _number_tokens(text):
    """All number tokens in text, comma-normalized ("1,000" -> "1000")."""
    return {raw.replace(",", "") for raw in _NUMBER_RE.findall(str(text or ""))}


def _year_tokens(text):
    return set(_YEAR_RE.findall(str(text or "")))


def _research_queries_for(bible, entity, genre_pack=None):
    """1–2 queries for a subject: its canonical name, plus the name
    with a distinguishing term from the bible (era first, then a
    place from the bible, then the entity's role).

    Stage 5A: when a genre pack is supplied, its
    research_query_templates are formatted per entity
    ({name}/{era}/{place}/{role}) and appended — a template whose
    placeholders cannot all be filled is skipped rather than
    emitting a dangling query. The total stays bounded (<= 5:
    the <= 2 base queries plus at most 3 pack templates)."""
    canonical = str(entity.get("canonical") or "").strip()
    if not canonical:
        return []
    queries = [canonical]
    term = _year_or_decade(bible.get("time_period")) or _year_or_decade(
        entity.get("description")
    )
    if not term:
        for other in bible.get("entities") or []:
            if other.get("type") == "place" and other.get("id") != entity.get("id"):
                term = str(other.get("canonical") or "").strip()
                break
    if not term:
        term = str(entity.get("role") or "").strip()
    if term and term.lower() not in canonical.lower():
        queries.append(f"{canonical} {term}")
    if isinstance(genre_pack, dict):
        era = _year_or_decade(bible.get("time_period")) or _year_or_decade(
            entity.get("description")
        ) or ""
        place = ""
        for other in bible.get("entities") or []:
            if other.get("type") == "place" and other.get("id") != entity.get("id"):
                place = str(other.get("canonical") or "").strip()
                break
        values = {
            "name": canonical,
            "era": era,
            "place": place,
            "role": str(entity.get("role") or "").strip(),
        }
        for template in genre_pack.get("research_query_templates") or []:
            if not isinstance(template, str):
                continue
            needed = set(re.findall(r"\{(\w+)\}", template))
            if any(not values.get(key) for key in needed):
                continue  # unfilled placeholder -> skip, never dangle
            try:
                query = " ".join(template.format(**values).split())
            except (KeyError, IndexError, ValueError):
                continue
            if query and query not in queries:
                queries.append(query)
            if len(queries) >= 5:
                break
    return queries


def _fact_supported(fact_text, sources):
    """A fact is externally supported when at least one retrieved
    snippet for its subject contains the fact's year/number (when the
    fact has one) or a strict majority of its key content tokens."""
    numbers = _number_tokens(fact_text)
    key_tokens = _content_tokens(fact_text)
    for source in sources:
        snippet = str(source.get("snippet") or "")
        if not snippet:
            continue
        if numbers and numbers & _number_tokens(snippet):
            return True
        if key_tokens:
            overlap = len(key_tokens & _content_tokens(snippet))
            if overlap * 2 > len(key_tokens):
                return True
    return False


def _fact_conflict(fact_text, subject, sources):
    """One conflict record when two DIFFERENT sources for the subject
    disagree on the fact's years (or headline numbers) in snippets
    that share the fact's key tokens. Both values are recorded; the
    conflict is never resolved. Returns None when sources agree (or
    there is nothing comparable)."""
    years = _year_tokens(fact_text)
    if not years and not _number_tokens(fact_text):
        return None
    key_tokens = _content_tokens(fact_text)
    # The snippet must share the fact's DISTINCTIVE tokens — the ones
    # beyond the subject's own name. Every snippet about the subject
    # shares the name; requiring more keeps a snippet about a
    # different event in the subject's life from "disagreeing" with
    # this fact just because it mentions another year.
    subject_tokens = _content_tokens(subject.get("canonical"))
    for alias in subject.get("aliases") or []:
        subject_tokens |= _content_tokens(alias)
    share_tokens = (key_tokens - subject_tokens) or key_tokens
    per_source = []
    for source in sources:
        snippet = str(source.get("snippet") or "")
        if not snippet:
            continue
        if share_tokens and not (share_tokens & _content_tokens(snippet)):
            continue
        values = _year_tokens(snippet) if years else _number_tokens(snippet)
        if values:
            per_source.append((str(source.get("url") or ""), values))
    if len(per_source) < 2:
        return None
    all_values = set().union(*(values for _, values in per_source))
    if len(all_values) < 2:
        return None
    if all(values == per_source[0][1] for _, values in per_source):
        return None
    recorded = []
    for value in sorted(all_values):
        url = next(u for u, values in per_source if value in values)
        recorded.append({"value": value, "source_url": url})
    return {
        "subject": subject.get("id"),
        "subject_name": subject.get("canonical"),
        "fact_hint": fact_text[:120],
        "values": recorded,
    }


def _label_bible_facts(bible, by_id):
    """Label every bible fact and collect source conflicts.

    Facts are the bible's timeline entries (when/what) and each
    entity's description claim. The label is stored ON the fact
    entry: timeline entries and entity dicts each gain "support".
    A fact whose related subject was not researched (or has no
    sources) stays "script_asserted". Returns the conflicts list.
    """
    alias_index = bible.get("alias_index") or build_alias_index(
        bible.get("entities") or []
    )
    facts = []
    for entry in bible.get("timeline") or []:
        if not isinstance(entry, dict):
            continue
        text = f"{entry.get('when') or ''} {entry.get('what') or ''}".strip()
        mentions = find_mentions(text, alias_index)
        subject_id = mentions[0] if mentions else bible.get("primary_subject_id")
        facts.append((entry, subject_id, text))
    for entity in bible.get("entities") or []:
        description = str(entity.get("description") or "").strip()
        if description:
            facts.append((entity, entity.get("id"), description))

    conflicts = []
    for entry, subject_id, text in facts:
        subject = by_id.get(subject_id) if subject_id else None
        sources = (subject or {}).get("sources") or []
        entry["support"] = "script_asserted"
        if not sources or subject is None:
            continue
        if _fact_supported(text, sources):
            entry["support"] = "externally_supported"
        conflict = _fact_conflict(text, subject, sources)
        if conflict:
            conflicts.append(conflict)
    return conflicts


def enrich_bible_with_research(bible, script_text, sentences=None, genre_pack=None):
    """Stage 3A research pass over a built bible (mutates and returns it).

    Stage 5A: `genre_pack` (a services.genre_packs pack dict, or
    None) shapes query construction via its
    research_query_templates and is recorded in research_meta.
    None keeps the exact Stage 3A behaviour.

    Subjects are identified across the WHOLE script (alias index +
    find_mentions over the full text and every sentence), ranked by
    mention count, and at most the top 12 are researched with 1–2
    search_web queries each. Retrieved sources are attached to the
    matching entity as entity["sources"] = [{url, title, snippet,
    retrieved_at}]; the bible gains "conflicts" and "research_meta".
    Fail-soft: any failure leaves a valid bible whose facts are all
    "script_asserted".
    """
    if not isinstance(bible, dict):
        return bible
    researched_at = datetime.now(timezone.utc).isoformat()
    entities = bible.get("entities") or []
    by_id = bible.get("by_id") or {e["id"]: e for e in entities}
    for entity in entities:
        if isinstance(entity, dict):
            entity.setdefault("sources", [])
    bible["conflicts"] = []
    bible["research_meta"] = {
        "researched_at": researched_at,
        "subjects_researched": 0,
        "sources_attached": 0,
        "conflicts_found": 0,
        "genre_pack": (genre_pack or {}).get("id"),
    }
    try:
        alias_index = build_alias_index(entities)
        counts = Counter()
        for eid in find_mentions(script_text, alias_index):
            counts[eid] += 1
        for text in sentences or []:
            for eid in find_mentions(text, alias_index):
                counts[eid] += 1
        order = {entity.get("id"): i for i, entity in enumerate(entities)}
        ranked = sorted(
            entities,
            key=lambda e: (-counts.get(e.get("id"), 0), order.get(e.get("id"), 0)),
        )
        subjects = ranked[:_RESEARCH_MAX_SUBJECTS]

        search_fn = _resolve_search_web()
        subjects_researched = 0
        sources_attached = 0
        failures = 0
        if search_fn is not None:
            for entity in subjects:
                if failures >= _RESEARCH_FAILURE_LIMIT:
                    break
                subjects_researched += 1
                seen_urls = {
                    s.get("url") for s in entity.get("sources") or []
                }
                for query in _research_queries_for(bible, entity, genre_pack=genre_pack):
                    if failures >= _RESEARCH_FAILURE_LIMIT:
                        break
                    try:
                        results = search_fn(query) or []
                    except Exception as error:
                        failures += 1
                        print(f"STORY RESEARCH: search failed for {query!r}: {error}")
                        continue
                    for result in results:
                        if not isinstance(result, dict):
                            continue
                        url = str(result.get("url") or result.get("link") or "").strip()
                        if not url or url in seen_urls:
                            continue
                        seen_urls.add(url)
                        entity["sources"].append({
                            "url": url,
                            "title": str(result.get("title") or ""),
                            "snippet": str(result.get("snippet") or ""),
                            "retrieved_at": researched_at,
                        })
                        sources_attached += 1
                        if len(entity["sources"]) >= _RESEARCH_MAX_SOURCES_PER_SUBJECT:
                            break

        conflicts = _label_bible_facts(bible, by_id)
        bible["conflicts"] = conflicts
        bible["research_meta"] = {
            "researched_at": researched_at,
            "subjects_researched": subjects_researched,
            "sources_attached": sources_attached,
            "conflicts_found": len(conflicts),
            "genre_pack": (genre_pack or {}).get("id"),
        }
    except Exception as error:  # research must never crash a production
        print(f"STORY RESEARCH: pass failed ({error}) — facts stay script_asserted.")
        try:
            for entry in bible.get("timeline") or []:
                if isinstance(entry, dict):
                    entry.setdefault("support", "script_asserted")
            for entity in entities:
                if isinstance(entity, dict) and str(entity.get("description") or "").strip():
                    entity.setdefault("support", "script_asserted")
        except Exception:
            pass
    return bible


def _attach_research_refs(plans, bible):
    """Stage 3A sentence coverage: every plan gains "research_ref" —
    the id of the subject the plan is about when that subject has at
    least one attached research source, else "no_external_source"."""
    by_id = (bible or {}).get("by_id") or {}
    for plan in plans or []:
        if not isinstance(plan, dict):
            continue
        ref = "no_external_source"
        candidates = []
        if plan.get("subject_id"):
            candidates.append(plan["subject_id"])
        for eid in plan.get("entity_ids") or []:
            if eid not in candidates:
                candidates.append(eid)
        for eid in candidates:
            entity = by_id.get(eid) or {}
            if entity.get("sources"):
                ref = eid
                break
        plan["research_ref"] = ref
    return plans


# --------------------------------------------------------------------------
# 2. SENTENCE MEANING -> ROLE -> VISUAL SCENARIOS
# --------------------------------------------------------------------------

SENTENCE_SYSTEM = """You are the visual director of a documentary. For each numbered
sentence decide what it MEANS and what should be SHOWN on screen.

You are given the CASE BIBLE (the established entities and context) and the
previous sentences for continuity. Resolve pronouns and references
("he", "they", "this house", "that location", "the suspect", "the victim")
using the bible and previous sentences. Never require the name to be repeated.

Return ONLY a JSON list, one object per sentence:
{
  "idx": <number>,
  "meaning": "one line: what this sentence is actually communicating",
  "role": one of %(roles)s,
  "subject_id": "bible entity id the sentence is about, or null",
  "resolved": {"he": "entity_id", "this house": "entity_id"},
  "events": ["event names such as sentencing, arrest, trial (not dates)"],
  "time": "date/period mentioned or implied, or null",
  "place": "location mentioned or implied, or null",
  "scenarios": [
    {
      "visual": "plain description of ONE shot that would illustrate the meaning",
      "kind": one of %(kinds)s,
      "format_intent": "footage | photograph | document | map | chart | graphic | any — the visual format that would best communicate this sentence (a guide for ranking, not a requirement)",
      "subject_id": "bible entity id or null",
      "specificity": "exact | representative | generic",
      "search_terms": ["2-6 word visual noun phrases a stock/archive search would need"],
      "event": "event name or null",
      "place": "place or null",
      "era": "year/decade or null"
    }
  ]
}

Rules:
- Understand meaning, not keywords. Explain psychology/motivation/legal context
  as what a viewer should SEE (e.g. a person alone, documents, a courtroom),
  not as the abstract words in the sentence.
- If one sentence contains several distinct images, output one scenario per
  image (up to 4). Do not collapse them into one.
- "exact" only when a real, named person/place/event/document must be shown.
  "representative" when a similar real-world shot works. "generic" for mood.
- Entities and subject_ids must come from the bible. Ordinary words, sentence
  openers, and time words are never entities. Dates go in "time"/"era".
- Do not invent facts that the script does not support.""" % {
    "roles": " | ".join(SENTENCE_ROLES),
    "kinds": " | ".join(SCENARIO_KINDS),
}


def _bible_for_prompt(bible):
    return json.dumps({
        "title": bible.get("title"),
        "story_type": bible.get("story_type"),
        "time_period": bible.get("time_period"),
        "primary_subject_id": bible.get("primary_subject_id"),
        "entities": bible.get("entities"),
        "timeline": bible.get("timeline"),
    }, ensure_ascii=False)


def analyze_sentences(sentences, bible, llm, batch_size=10, context_size=4):
    """Returns one normalized plan dict per sentence (same order/length)."""
    plans = [None] * len(sentences)
    carry_subject = bible.get("primary_subject_id")

    for start in range(0, len(sentences), batch_size):
        batch = list(range(start, min(start + batch_size, len(sentences))))
        previous = sentences[max(0, start - context_size):start]

        user = (
            f"CASE BIBLE:\n{_bible_for_prompt(bible)}\n\n"
            f"PREVIOUS SENTENCES (context only):\n"
            + "\n".join(f"- {s}" for s in previous)
            + "\n\nSENTENCES TO ANALYZE:\n"
            + "\n".join(f"{i}: {sentences[i]}" for i in batch)
        )
        raw = _call_llm_json(llm, SENTENCE_SYSTEM, user)
        by_idx = {}
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and isinstance(item.get("idx"), int):
                    by_idx[item["idx"]] = item

        for i in batch:
            plan = normalize_plan(by_idx.get(i), sentences[i], bible, carry_subject)
            plans[i] = plan
            new_subject = plan.get("subject_id")
            if new_subject:
                new_type = bible["by_id"][new_subject]["type"]
                old_type = bible["by_id"].get(carry_subject, {}).get("type")
                # a passing mention of a place/thing must not replace the person
                # the story is following
                if new_type == "person" or carry_subject is None or old_type != "person":
                    carry_subject = new_subject

    return plans


# MA-29: the downstream shot-need contract (media_pipeline
# build_shot_needs / gate_shot_need) branches on a required_type in
# the GATE's vocabulary — "person" triggers the missing-person
# branch, {"location", "organization", "event", "object"} the
# missing-subject branch. Scenarios used to carry only subject_id,
# so consumers derived required_type from the scenario kind and the
# person/location branches were unreachable with production-shaped
# scenarios. The producer now resolves both fields: the subject's
# canonical label and a required_type translated from the bible
# entity type (preferred) or the scenario kind.
_ENTITY_REQUIRED_TYPE = {
    "person": "person",
    "place": "location",
    "organization": "organization",
    "event": "event",
    "object": "object",
}
_KIND_REQUIRED_TYPE = {
    "person_activity": "person",
    "place": "location",
    "event": "event",
    "object": "object",
    "document": "object",
}


def _required_type_for(scenario, subject_entity, kind):
    """Resolve a scenario's required_type in the gate's vocabulary.

    The resolved subject entity's bible type wins; a type supplied
    on the scenario itself is next; the scenario kind is the last
    resort. Returns None when nothing implies a type requirement
    (archival / abstract b-roll scenarios)."""
    if subject_entity:
        mapped = _ENTITY_REQUIRED_TYPE.get(
            str(subject_entity.get("type") or "").lower()
        )
        if mapped:
            return mapped
    supplied = str((scenario or {}).get("required_type") or "").strip().lower()
    if supplied:
        return supplied
    return _KIND_REQUIRED_TYPE.get(kind)


def _pick_subject(mentions, by_id, sentence, carry):
    """No-LLM subject choice: named person > pronoun refers to carried subject > first mention."""
    people = [m for m in mentions if by_id[m]["type"] == "person"]
    if people:
        return people[0]
    if carry and any(t.lower() in PRONOUNS for t in _tokens(sentence)):
        return carry
    if mentions:
        return mentions[0]
    return carry


def normalize_plan(raw, sentence, bible, carry_subject):
    """Enforce structure. Nothing the LLM says can bypass the bible."""
    by_id = bible.get("by_id", {})
    index = bible.get("alias_index", {})
    raw = raw if isinstance(raw, dict) else {}
    degraded = not raw

    mentions = find_mentions(sentence, index)

    subject_id = raw.get("subject_id")
    if subject_id not in by_id:
        subject_id = None
        if not raw:
            subject_id = _pick_subject(mentions, by_id, sentence, carry_subject)
        elif mentions:
            subject_id = mentions[0]
        elif (_tokens(sentence) or [""])[0].lower() in PRONOUNS:
            subject_id = carry_subject

    role = str(raw.get("role") or "").lower()
    if role not in SENTENCE_ROLES:
        role = "background"

    entity_ids = list(mentions)
    if subject_id and subject_id not in entity_ids:
        entity_ids.insert(0, subject_id)
    # A pronoun anywhere in the sentence refers back to the established
    # subject ("..., he could not be executed") even if another entity leads.
    if carry_subject and carry_subject not in entity_ids:
        if any(t.lower() in PRONOUNS for t in _tokens(sentence)):
            entity_ids.append(carry_subject)

    scenarios = []
    for scenario in (raw.get("scenarios") or [])[:4]:
        cleaned = _normalize_scenario(scenario, raw, by_id, subject_id, sentence_text=sentence)
        if cleaned:
            scenarios.append(cleaned)

    if not scenarios:
        # Degraded but safe: one scenario anchored on a bible entity (subject
        # plus any place named in the sentence). Never built from random
        # sentence words.
        place_entity = next(
            (by_id[e] for e in entity_ids
             if e in by_id and by_id[e]["type"] == "place" and e != subject_id),
            None,
        )
        subject_entity = by_id.get(subject_id)
        _intent, _intent_conf = infer_format_intent(
            sentence,
            {"kind": "archival" if subject_entity else "abstract_broll", "visual": ""},
        )
        scenarios.append({
            "visual": f"archival material about {subject_entity['canonical']}" if subject_entity else "neutral documentary b-roll",
            "kind": "archival" if subject_entity else "abstract_broll",
            "subject_id": subject_id,
            # MA-29: same producer contract as _normalize_scenario.
            "subject": (subject_entity["canonical"] if subject_entity else None),
            "required_type": _required_type_for(
                {}, subject_entity, "archival" if subject_entity else "abstract_broll"
            ),
            "specificity": "exact" if subject_entity else "generic",
            "search_terms": [],
            "event": None,
            "place": (place_entity["canonical"] if place_entity else raw.get("place")),
            "era": _year_or_decade(sentence) or None,
            "format_intent": _intent,
            "format_intent_confidence": _intent_conf,
        })
        degraded = True

    plan = {
        "sentence": sentence,
        "meaning": raw.get("meaning") or "",
        "role": role,
        "subject_id": subject_id,
        "entities": [by_id[e]["canonical"] for e in entity_ids if e in by_id],
        "entity_ids": entity_ids,
        "resolved": raw.get("resolved") if isinstance(raw.get("resolved"), dict) else {},
        "events": [str(e) for e in (raw.get("events") or []) if str(e).strip()],
        "time": raw.get("time"),
        "place": raw.get("place"),
        "scenarios": scenarios,
        "degraded": degraded,
    }
    for scenario in plan["scenarios"]:
        scenario["queries"] = compose_queries(scenario, plan, bible)
    return plan


def _normalize_scenario(scenario, sentence_raw, by_id, sentence_subject, sentence_text=""):
    if not isinstance(scenario, dict):
        return None
    visual = str(scenario.get("visual") or "").strip()
    terms = [str(t).strip() for t in (scenario.get("search_terms") or []) if str(t).strip()]
    if not visual and not terms:
        return None

    kind = str(scenario.get("kind") or "").lower()
    if kind not in SCENARIO_KINDS:
        kind = "abstract_broll"
    specificity = str(scenario.get("specificity") or "").lower()
    if specificity not in SPECIFICITY:
        specificity = "representative"

    subject_id = scenario.get("subject_id")
    if subject_id not in by_id:
        subject_id = sentence_subject if specificity == "exact" else None

    if specificity == "exact" and subject_id is None:
        specificity = "representative"  # cannot demand an exact match with no subject

    # F-1: format intent — an LLM-provided value is validated against the
    # closed set; anything else falls back to the heuristic inference.
    format_intent = str(scenario.get("format_intent") or "").lower()
    if format_intent in FORMAT_INTENTS and format_intent != "any":
        format_confidence = 0.8
    else:
        format_intent, format_confidence = infer_format_intent(
            sentence_text or sentence_raw.get("sentence") or "",
            {"kind": kind, "visual": visual},
        )

    # MA-29: emit the resolved subject label and the gate-vocabulary
    # required_type the downstream contract expects. The subject is
    # the bible record this scenario already resolved — never a
    # string re-derived from the sentence.
    subject_entity = by_id.get(subject_id) if subject_id else None
    subject_label = (
        str(subject_entity.get("canonical") or "").strip()
        if subject_entity
        else ""
    ) or str(scenario.get("subject") or "").strip()
    required_type = _required_type_for(scenario, subject_entity, kind)

    return {
        "visual": visual,
        "kind": kind,
        "subject_id": subject_id,
        "subject": subject_label or None,
        "required_type": required_type,
        "specificity": specificity,
        "search_terms": terms[:4],
        "event": scenario.get("event") or None,
        "place": scenario.get("place") or sentence_raw.get("place") or None,
        "era": scenario.get("era") or sentence_raw.get("time") or None,
        "format_intent": format_intent,
        "format_intent_confidence": format_confidence,
    }


# --------------------------------------------------------------------------
# 3. SEARCH INTENT -> SEARCH QUERIES (composed from fields, never raw words)
# --------------------------------------------------------------------------

def _year_or_decade(era):
    match = re.search(r"\b(1[5-9]\d\d|20\d\d)s?\b", str(era or ""))
    return match.group(0) if match else ""


def _clean_query(parts):
    text = " ".join(str(p).strip() for p in parts if p and str(p).strip())
    seen = set()
    words = []
    for word in re.sub(r"\s+", " ", text).strip().split():
        key = word.lower()
        if key in seen:
            continue
        seen.add(key)
        words.append(word)
    return " ".join(words)


def _valid_query(query, alias_index):
    tokens = _tokens(query)
    if not tokens:
        return False
    if len(tokens) == 1:
        # a lone word is only acceptable if it is a known entity alias
        return _norm(query) in alias_index
    return True


def compose_queries(scenario, plan, bible, max_queries=4):
    by_id = bible.get("by_id", {})
    index = bible.get("alias_index", {})
    subject = by_id.get(scenario.get("subject_id"))
    anchor = subject["canonical"] if subject else ""
    terms = scenario.get("search_terms") or []
    event = scenario.get("event") or (plan["events"][0] if plan.get("events") else "")
    place = scenario.get("place") or ""
    year = _year_or_decade(scenario.get("era") or plan.get("time"))
    specificity = scenario.get("specificity")

    candidates = []
    if specificity == "exact":
        candidates += [
            _clean_query([anchor, event, year]),
            _clean_query([anchor, event]),
            _clean_query([anchor, terms[0] if terms else "", year]),
            _clean_query([anchor, place]),
        ]
    elif specificity == "representative":
        for term in terms[:2]:
            candidates += [_clean_query([term, place, year]), _clean_query([term, year]), _clean_query([term])]
        if anchor and subject and subject["type"] != "person":
            candidates.append(_clean_query([anchor, terms[0] if terms else event]))
    else:  # generic
        for term in terms[:2]:
            candidates.append(_clean_query([term]))

    queries = []
    for query in candidates:
        if query and _valid_query(query, index) and query.lower() not in (q.lower() for q in queries):
            queries.append(query)
    return queries[:max_queries]


# --------------------------------------------------------------------------
# 4. CANDIDATE VERIFICATION
# --------------------------------------------------------------------------

ACCEPT_THRESHOLD = {"exact": 0.55, "representative": 0.34, "generic": 0.22}


def verify_candidate(candidate, scenario, bible):
    """
    candidate: dict with any of title, snippet, description, url, source.
    Returns {"accepted": bool, "score": float, "reasons": [..],
             "contributions": {..}, "rejection_reason": str}

    Scoring is deterministic and explainable:
      subject alias hit ............ +0.45
      topical token overlap ........ +0.45 * min(1, overlap * 1.5)
      era year present ............. +0.10
      names a different person ..... -0.30

    VA-04: a subject match ALONE can no longer approve a candidate.
    When the scenario defines topical terms (search_terms / visual),
    the candidate must show SOME topical overlap with them; era or
    subject mentions by themselves are not topical evidence. The
    individual contributions and the final rejection reason are
    recorded on the verdict so every decision is explainable.

    MA-30: the same guarantee holds when the scenario defines NO
    topical terms at all. An empty topical set used to disable the
    VA-04 test entirely (there was no overlap to measure), letting a
    subject-only match (0.45, plus 0.10 for an era year) clear the
    representative/generic thresholds. A subject mention with zero
    topical evidence behind it is rejected as "no_topical_evidence"
    regardless of why the topical set is empty.
    """
    text = _norm(" ".join(str(candidate.get(k) or "") for k in ("title", "snippet", "description", "url", "source")))
    by_id = bible.get("by_id", {})
    reasons = []
    score = 0.0
    contributions = {"subject": 0.0, "topical": 0.0, "era": 0.0, "wrong_person": 0.0}

    subject = by_id.get(scenario.get("subject_id"))
    subject_hit = False
    if subject:
        aliases = {subject["canonical"], *subject["aliases"]}
        if subject["type"] == "person":
            parts = subject["canonical"].split()
            if len(parts) >= 2:
                aliases.add(parts[-1])
        subject_hit = any(_norm(a) and _norm(a) in text for a in aliases)
        if subject_hit:
            score += 0.45
            contributions["subject"] = 0.45
            reasons.append(f"mentions {subject['canonical']}")
        elif scenario.get("specificity") == "exact":
            reasons.append(f"missing required subject {subject['canonical']}")

    wanted = set()
    for term in scenario.get("search_terms") or []:
        wanted |= _content_tokens(term)
    wanted |= _content_tokens(scenario.get("visual"))
    overlap = None
    if wanted:
        overlap = len(wanted & {_stem(t) for t in text.split()}) / len(wanted)
        topical = 0.45 * min(1.0, overlap * 1.5)
        score += topical
        contributions["topical"] = round(topical, 3)
        reasons.append(f"visual overlap {overlap:.2f}")

    era = _year_or_decade(scenario.get("era"))
    if era and era[:4] in text:
        score += 0.10
        contributions["era"] = 0.10
        reasons.append("era match")

    # A named-person shot that names a DIFFERENT bible person and not the
    # subject is almost certainly the wrong photo.
    if subject and not subject_hit:
        others = [
            e for e in bible.get("entities", [])
            if e["id"] != subject["id"] and e["type"] == "person"
            and _norm(e["canonical"]) in text
        ]
        if others:
            score -= 0.3
            contributions["wrong_person"] = -0.3
            reasons.append(f"names other person {others[0]['canonical']}")

    specificity = scenario.get("specificity", "representative")
    accepted = score >= ACCEPT_THRESHOLD.get(specificity, 0.34)
    rejection_reason = ""
    if specificity == "exact" and subject and not subject_hit:
        accepted = False
        rejection_reason = "missing_required_subject"
    elif wanted and overlap == 0:
        # VA-04: no topical evidence at all — a subject mention and/or
        # an era year by themselves do not establish relevance to the
        # visual topic the sentence actually needs.
        accepted = False
        rejection_reason = "no_topical_evidence"
    elif not wanted and subject_hit:
        # MA-30: the scenario yielded no topical terms, so the VA-04
        # overlap test above had nothing to measure and silently
        # lapsed. The only positive evidence here is the subject
        # mention itself (plus at most an era year) — exactly the
        # subject-only approval VA-04 forbids. Reject on the same
        # reason so the guarantee does not depend on the scenario
        # happening to carry topical terms.
        accepted = False
        rejection_reason = "no_topical_evidence"
    elif not accepted:
        rejection_reason = "below_threshold"
    if accepted:
        rejection_reason = ""
    return {
        "accepted": accepted,
        "score": round(score, 3),
        "reasons": reasons,
        "contributions": contributions,
        "rejection_reason": rejection_reason,
    }


# --------------------------------------------------------------------------
# 5. ONE ENTRY POINT (+ cache so re-runs don't re-call the LLM)
# --------------------------------------------------------------------------

def serialize_bible(bible):
    return {k: v for k, v in (bible or {}).items() if k not in ("by_id", "alias_index")}


def hydrate_bible(bible):
    """Rebuild lookup tables from a serialized bible (or make an empty one)."""
    bible = dict(bible or {})
    entities = bible.get("entities") or []
    bible["entities"] = entities
    bible["by_id"] = {e["id"]: e for e in entities}
    bible["alias_index"] = build_alias_index(entities)
    bible.setdefault("primary_subject_id", entities[0]["id"] if entities else None)
    return bible


def _analysis_mode(bible, plans):
    """MA-27: the run's analysis mode, derived from what ACTUALLY
    happened — never from the Case Bible alone.

    "llm"         — the bible AND every sentence plan were
                    LLM-produced (no plan carries the degraded flag).
    "partial_llm" — some of the work was LLM-produced and some fell
                    back: an LLM bible with degraded sentence plans,
                    or a degraded bible whose plans still came from
                    the LLM.
    "degraded"    — everything fell back to deterministic heuristics.

    The previous logic copied the bible's mode onto the whole run,
    so a film whose sentence plans were ALL heuristic fallbacks could
    be reported as analysis_mode="llm" whenever the bible call alone
    had succeeded.
    """
    bible_llm = (bible or {}).get("mode") == "llm"
    plans = plans or []
    llm_plans = sum(1 for plan in plans if not (plan or {}).get("degraded"))
    if bible_llm and llm_plans == len(plans):
        return "llm"
    if not bible_llm and llm_plans == 0:
        return "degraded"
    return "partial_llm"


def plan_script(script_text, sentences, llm, cache_path=None, genre_pack=None):
    """
    Returns (bible, plans, mode). mode is "llm", "partial_llm" or
    "degraded" (MA-27: derived from the bible AND the sentence plans
    via _analysis_mode — see its docstring).
    Only real LLM results are cached, so a later run with a working LLM is
    never poisoned by an earlier degraded run.
    """
    key = hashlib.sha256((script_text + "\n" + "\n".join(sentences)).encode("utf-8")).hexdigest()[:16]

    if llm is not None and cache_path and Path(cache_path).exists():
        try:
            cached = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            if cached.get("key") == key and cached.get("mode") == "llm":
                print("STORY PLAN: loaded from cache.")
                cached_bible = hydrate_bible(cached["bible"])
                cached_plans = cached["plans"]
                # Stage 3A: cached plans were stored with their
                # research_refs; re-derive them anyway so a cache
                # written before Stage 3A still yields full coverage.
                _attach_research_refs(cached_plans, cached_bible)
                return cached_bible, cached_plans, "llm"
        except Exception:
            pass

    bible = build_case_bible(script_text, llm, sentences=sentences)
    plans = analyze_sentences(sentences, bible, llm)
    # Stage 3A: research-ground the bible (both the LLM and the
    # heuristic bible paths land here) and stamp every plan with its
    # research_ref. Runs after sentence analysis so source snippets
    # never leak into the LLM's sentence prompts. Fail-soft by design.
    enrich_bible_with_research(bible, script_text, sentences=sentences, genre_pack=genre_pack)
    _attach_research_refs(plans, bible)
    mode = _analysis_mode(bible, plans)
    if llm is None:
        print("STORY: no LLM configured -> DEGRADED MODE (set LLM_API_KEY for full understanding).")

    if cache_path and mode == "llm":
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(
            json.dumps({"key": key, "mode": mode, "bible": serialize_bible(bible), "plans": plans},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return bible, plans, mode


def describe_plan(index, plan):
    """Readable log line block, replaces the old 'ENTITIES:/EVENT:' output."""
    lines = [
        f"SENTENCE {index + 1}: {plan['sentence']}",
        f"  MEANING : {plan['meaning'] or '(degraded)'}",
        f"  ROLE    : {plan['role']}   SUBJECT: {plan['subject_id']}   ENTITIES: {plan['entities']}",
    ]
    if plan.get("events"):
        lines.append(f"  EVENTS  : {plan['events']}  TIME: {plan.get('time')}  PLACE: {plan.get('place')}")
    for n, scenario in enumerate(plan["scenarios"], 1):
        lines.append(f"  SHOT {n}  : [{scenario['specificity']}/{scenario['kind']}] {scenario['visual']}")
        for query in scenario["queries"]:
            lines.append(f"      query: {query}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 6. NO-LLM FALLBACK (structural, no vocabulary lists)
# --------------------------------------------------------------------------
# Only two tiny closed classes are used: calendar month names and
# prepositions that introduce a location. Everything else is decided by
# capitalization behaviour across the whole script: a word that is capitalized
# only when it starts a sentence ("Within", "There", "When") is never an entity.

MONTHS = {"January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"}
PLACE_PREPOSITIONS = {"in", "at", "near", "from", "to", "across", "outside",
                      "inside", "into", "around", "throughout"}
_EDGE_PUNCT = "\"'\u201c\u201d\u2018\u2019()[]{}:;,.!?"


def capitalized_runs(sentence):
    """Runs of consecutive capitalized words with position context."""
    words = str(sentence or "").split()
    runs = []
    current = []
    start = 0
    prev = ""

    def flush():
        nonlocal current
        if current:
            runs.append({"words": current, "initial": start == 0, "prev": prev})
            current = []

    for i, raw in enumerate(words):
        core = raw.strip(_EDGE_PUNCT)
        is_cap = bool(core) and core[0].isupper() and len(core) > 1 and re.match(r"^[A-Za-z][\w'\u2019-]*$", core)
        if is_cap:
            if not current:
                start = i
                prev = words[i - 1].strip(_EDGE_PUNCT).lower() if i > 0 else ""
            current.append(core)
            if raw[-1:] in ",.;:!?)\u201d\"":
                flush()
        else:
            flush()
    flush()
    return runs


def heuristic_bible(sentences):
    mid_caps = Counter()
    all_runs = []
    for sentence in sentences:
        for run in capitalized_runs(sentence):
            all_runs.append(run)
            words = run["words"][1:] if run["initial"] else run["words"]
            for w in words:
                mid_caps[w.lower()] += 1

    stats = {}
    for run in all_runs:
        words = list(run["words"])
        prev = run["prev"]
        if run["initial"]:
            while words and (words[0] in MONTHS or mid_caps[words[0].lower()] == 0):
                prev = words.pop(0).lower()
        else:
            while words and words[0] in MONTHS:
                prev = words.pop(0).lower()
        while words and words[-1] in MONTHS:
            words.pop()
        if not words:
            continue
        text = " ".join(words)
        entry = stats.setdefault(text, {"count": 0, "mid": 0, "place": 0, "words": words})
        entry["count"] += 1
        if not run["initial"] or len(words) < len(run["words"]) or len(run["words"]) > 1:
            entry["mid"] += 1
        if prev in PLACE_PREPOSITIONS:
            entry["place"] += 1

    candidates = {t: e for t, e in stats.items() if e["mid"] >= 1 or e["count"] >= 2}
    multi = {t: e for t, e in candidates.items() if len(e["words"]) > 1}

    entities = []
    absorbed = set()
    for text, entry in sorted(candidates.items(), key=lambda kv: (-len(kv[1]["words"]), -kv[1]["count"])):
        if text in absorbed:
            continue
        aliases = []
        if len(entry["words"]) > 1:
            for other, other_entry in candidates.items():
                if other != text and len(other_entry["words"]) == 1 and other_entry["words"][0] in entry["words"]:
                    owners = [m for m in multi if other_entry["words"][0] in multi[m]["words"]]
                    if len(owners) == 1:
                        aliases.append(other)
                        absorbed.add(other)
        total = entry["count"] + sum(candidates[a]["count"] for a in aliases)
        place_votes = entry["place"] + sum(candidates[a]["place"] for a in aliases)
        if total and place_votes / total >= 0.5:
            etype = "place"
        elif len(entry["words"]) >= 2:
            etype = "person"
        else:
            etype = "other"
        entities.append({
            "id": re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_"),
            "canonical": text, "type": etype, "role": "", "aliases": aliases,
            "pronoun": None, "description": "", "_count": total,
        })

    entities.sort(key=lambda e: -e["_count"])
    entities = entities[:40]
    primary = next((e["id"] for e in entities if e["type"] == "person"), entities[0]["id"] if entities else None)
    for e in entities:
        e.pop("_count", None)
    return {"title": "", "story_type": "other", "time_period": "",
            "primary_subject_id": primary, "entities": entities, "timeline": []}


def heuristic_entities_in_text(text):
    """Cheap single-text entity guess (no context): names & places only."""
    names, places = [], []
    for run in capitalized_runs(text):
        words = list(run["words"])
        prev = run["prev"]
        while words and words[0] in MONTHS:
            prev = words.pop(0).lower()
        while words and words[-1] in MONTHS:
            words.pop()
        if run["initial"] and len(words) == 1:
            continue
        if not words:
            continue
        value = " ".join(words)
        bucket = places if prev in PLACE_PREPOSITIONS else names
        if value not in bucket and value not in places and value not in names:
            bucket.append(value)
    return names, places
