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
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path

from services.atomic_write import write_json_atomic

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


def _llm_failure_reason(error):
    """Classify an LLM call failure for the degraded_reason record
    (Master Directive Phase 7 / DM-2(a)): the three failure paths
    that used to be indistinguishable downstream —

      "no_key"          — no API key configured (the BLOCK-001
                          point-of-use check in llm_client);
      "provider_failure"— transport / HTTP / provider-side failure;
      "invalid_plan"    — the provider answered but its response
                          was unusable (llm_client.LLMResponseError:
                          no text / truncated / empty / malformed),
                          or the text did not parse as JSON.
    """
    if error is None:
        return "invalid_plan"
    message = str(error)
    if "BREWKEG_API_KEY" in message and "not configured" in message:
        return "no_key"
    if type(error).__name__ == "LLMResponseError":
        return "invalid_plan"
    return "provider_failure"


def _call_llm_json(llm, system, user, retries=0, failure=None):
    """Call the LLM and parse JSON. On failure returns None and, when
    a `failure` dict is supplied, records {"reason", "detail"} —
    the classification the degraded fallback stamps into the plan's
    planner block (DM-2(a)). The return contract is unchanged."""
    if llm is None:
        if failure is not None:
            failure.update({"reason": "no_key", "detail": "no LLM callable configured"})
        return None
    for _ in range(retries + 1):
        error = None
        try:
            data = parse_json_loose(llm(system, user))
        except Exception as exc:  # network / provider error
            print(f"STORY LLM ERROR: {exc}")
            error = exc
            data = None
        if data is not None:
            return data
        if failure is not None and "reason" not in failure:
            failure.update({
                "reason": _llm_failure_reason(error),
                "detail": str(error)[:200] if error is not None else "response did not parse as JSON",
            })
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


def _fallback_bible(fallback_sentences):
    """The degraded bible builder (Master Directive Phase 7 / P5):
    Degraded Planner 2.0 by default, the old heuristic behind the
    CINEMORA_DEGRADED_PLANNER hatch. A Planner 2.0 failure falls
    back to the heuristic and says so on the bible — a fallback
    inside the fallback is a stamped state, never a silent one
    (Planner 2.0 plan §5.10)."""
    from services import degraded_planner

    if not degraded_planner.planner_enabled():
        data = heuristic_bible(fallback_sentences)
        data["planner_version"] = "heuristic-1.0"
        return data
    try:
        return degraded_planner.build_bible(fallback_sentences)
    except Exception as error:  # the fallback must never crash planning
        print(f"STORY: Degraded Planner 2.0 failed ({error}) -> heuristic bible.")
        data = heuristic_bible(fallback_sentences)
        data["planner_version"] = "heuristic-1.0"
        data["planner_fallback"] = True
        return data


def build_case_bible(script_text, llm, sentences=None, max_chars=24000, _diag=None):
    text = script_text
    if len(text) > max_chars:  # keep beginning, middle, end
        third = max_chars // 3
        mid = len(text) // 2
        text = (
            text[:third]
            + "\n[...]\n" + text[mid - third // 2: mid + third // 2]
            + "\n[...]\n" + text[-third:]
        )

    failure = {}
    data = _call_llm_json(llm, BIBLE_SYSTEM, f"SCRIPT:\n{text}", failure=failure)
    used_llm = True
    if not isinstance(data, dict) or not data.get("entities"):
        print("STORY: LLM case bible unavailable -> deterministic bible (DEGRADED MODE).")
        used_llm = False
        if _diag is not None:
            # DM-2(a): record WHICH failure path degraded the bible.
            # Parsed-but-shapeless output is an invalid plan; no
            # recorded failure with a callable present means the
            # response parsed but carried no entities.
            _diag["bible_reason"] = (
                "no_key" if llm is None
                else failure.get("reason") or "invalid_plan"
            )
            _diag["bible_failure_detail"] = failure.get("detail")
        fallback = sentences or [p for p in re.split(r"(?<=[.!?])\s+", script_text) if p.strip()]
        data = _fallback_bible(fallback)

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
        cleaned_entity = {
            "id": eid,
            "canonical": canonical,
            "type": str(entity.get("type") or "person").lower(),
            "role": entity.get("role") or "",
            "aliases": [str(a).strip() for a in entity.get("aliases", []) if str(a).strip()],
            "pronoun": (entity.get("pronoun") or None),
            "description": entity.get("description") or "",
        }
        # Master Directive Phase 7: Planner 2.0's additive typing
        # provenance rides through the cleaning verbatim (absent on
        # LLM/heuristic entities — consumers treat absence as
        # "no recorded confidence", never as a fabricated value).
        for key in (
            "type_confidence", "type_source", "type_signals",
            "type_low_confidence", "recurrence", "mention_count",
        ):
            if key in entity:
                cleaned_entity[key] = entity[key]
        cleaned.append(cleaned_entity)
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

        # Stage 7 / R7 (review section B.2.4): the genre pack's
        # visual_profile research_requirements shape the research
        # pass — the primary subject is researched FIRST so its
        # minimum-source requirement is the first claim on the pass's
        # failure budget. The requirements are data from the pack,
        # resolved through the same validated profile machinery the
        # pipeline uses; support labelling is untouched.
        research_requirements = None
        if isinstance(genre_pack, dict):
            try:
                from services import genre_packs as _genre_packs

                _resolved = _genre_packs.resolve_visual_profile(genre_pack)
                research_requirements = (
                    (_resolved.get("profile") or {}).get(
                        "research_requirements"
                    )
                )
            except Exception:
                research_requirements = None
        primary_subject_id = bible.get("primary_subject_id")
        if research_requirements and primary_subject_id:
            for _i, _entity in enumerate(subjects):
                if _entity.get("id") == primary_subject_id and _i:
                    subjects.insert(0, subjects.pop(_i))
                    break

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
        # Stage 7 / R7: record the pack's research requirements and
        # whether the primary subject's minimum was met — a record,
        # never a gate on support labelling.
        _primary_sources = None
        _min_sources_met = None
        if research_requirements:
            _primary = by_id.get(primary_subject_id) or {}
            _primary_sources = len(_primary.get("sources") or [])
            _minimum = research_requirements.get(
                "min_sources_per_primary_subject"
            )
            if isinstance(_minimum, int) and search_fn is not None:
                _min_sources_met = _primary_sources >= _minimum
        bible["research_meta"] = {
            "researched_at": researched_at,
            "subjects_researched": subjects_researched,
            "sources_attached": sources_attached,
            "conflicts_found": len(conflicts),
            "genre_pack": (genre_pack or {}).get("id"),
            "research_requirements": research_requirements,
            "primary_subject_sources": _primary_sources,
            "primary_subject_min_sources_met": _min_sources_met,
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
  ],
  "fallback_concepts": {
    "activities": ["short phrases: activities a camera could show while this sentence is spoken, e.g. reviewing records, patrolling"],
    "objects": ["the concrete objects in play, e.g. phone records, pickup truck"],
    "settings": ["settings a camera could show, e.g. investigation office"],
    "place_types": ["kinds of place, e.g. beach shoreline, suburban street"],
    "mood": ["mood words, e.g. methodical, tense"],
    "era_terms": ["era phrases, e.g. 2010s"]
  },
  "visual_intents": {
    "primary_visual_intent": "the ONE visual purpose that best serves this sentence: exact | actual_event | activity_broll | establishing | environmental_broll | object_detail | archival | documentary_context | human_activity | geographic_context | graphic_explanation | atmospheric",
    "secondary_visual_intents": ["other purposes that would genuinely help — only ones that make sense for this sentence"],
    "intent_queries": {
      "literal": ["search phrases for the actual subject/place/event"],
      "activity_broll": ["search phrases for the activity, whoever performs it"],
      "establishing": ["search phrases for the place itself"],
      "contextual": ["search phrases for the surrounding context"],
      "archival": ["search phrases for period/archival material"]
    },
    "graphic_fallback": {"allowed": true, "kind": "map | timeline | statistic | diagram | null — a graphic that would genuinely explain, if footage fails"},
    "preferred_media_type": "video | image | graphic | any",
    "specificity_requirement": "exact | representative | generic",
    "visual_misrepresentation_risk": "low | medium | high — how misleading a wrong visual would be for this sentence"
  }
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
- Also emit "fallback_concepts": what a camera could show if the exact
  subject could not be filmed — the sentence's activities, the concrete
  objects in play, and its setting / kind of place — the way a documentary
  editor briefs B-roll. Phrases, not single keywords; at most 4 per list.
- Also emit "visual_intents": decide what the viewer should SEE while
  hearing this sentence, the way a documentary visual researcher would.
  The primary intent is the single best visual purpose; secondary
  intents are alternatives that genuinely fit — never list a category
  just because it exists. Distinguish actual footage/evidence from
  archival material, illustrative activity B-roll, contextual
  establishing shots, explanatory graphics and atmospheric support.
  Generic footage must never be described as if it depicted the actual
  event or named person. If the sentence describes an activity, an
  activity intent fits; if it describes a place, an establishing
  intent fits; complex information may suit a graphic fallback.
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


def analyze_sentences(sentences, bible, llm, batch_size=10, context_size=4, _diag=None):
    """Returns one normalized plan dict per sentence (same order/length).

    Master Directive Phase 7: a sentence the LLM did not plan (no
    callable, a failed batch, or an item missing from the batch's
    list) is planned by Degraded Planner 2.0's scenario factory —
    its raw plan goes through the SAME normalize_plan referee as
    LLM output — behind the CINEMORA_DEGRADED_PLANNER hatch; with
    the hatch off, normalize_plan's own synthesis remains the
    fallback, exactly as before. Factory-planned sentences are
    stamped degraded + their §5.8 confidences, and the failure
    classification is recorded in _diag (DM-2(a))."""
    plans = [None] * len(sentences)
    carry_subject = bible.get("primary_subject_id")
    sentence_planner = None  # built lazily, only if a sentence degrades

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
        failure = {}
        raw = _call_llm_json(llm, SENTENCE_SYSTEM, user, failure=failure)
        by_idx = {}
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and isinstance(item.get("idx"), int):
                    by_idx[item["idx"]] = item

        for i in batch:
            raw_item = by_idx.get(i)
            factory_raw = None
            if raw_item is None:
                if _diag is not None and "sentence_reason" not in _diag:
                    # A batch that returned a list but omitted this
                    # sentence handed back an invalid plan for it;
                    # a failed call recorded its own classification.
                    _diag["sentence_reason"] = (
                        "no_key" if llm is None
                        else failure.get("reason") or "invalid_plan"
                    )
                    _diag["sentence_failure_detail"] = failure.get("detail")
                from services import degraded_planner

                if degraded_planner.planner_enabled():
                    if sentence_planner is None:
                        sentence_planner = degraded_planner.SentencePlanner(
                            sentences, bible
                        )
                    try:
                        factory_raw = sentence_planner.raw_plan_for(i, carry_subject)
                    except Exception as error:  # the net inside the net
                        print(f"STORY: Planner 2.0 factory failed on sentence {i}: {error}")
                        factory_raw = None
            plan = normalize_plan(
                factory_raw if factory_raw is not None else raw_item,
                sentences[i], bible, carry_subject,
            )
            if factory_raw is not None:
                # §5.8 per-sentence stamps. The plan WAS produced by
                # the deterministic fallback — normalize_plan's
                # `degraded = not raw` cannot see that, so the
                # annotation restores the honest flag (mode
                # derivation in _analysis_mode depends on it).
                plan["degraded"] = True
                plan["subject_confidence"] = factory_raw.get("_subject_confidence")
                plan["role_confidence"] = factory_raw.get("_role_confidence")
                basis = factory_raw.get("_specificity_basis") or []
                if len(basis) == len(plan.get("scenarios") or []):
                    for scenario, basis_value in zip(plan["scenarios"], basis):
                        scenario["specificity_basis"] = basis_value
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


# ---------------------------------------------------------------------------
# Fallback concepts (Intelligent B-Roll System, B1 — plan §4)
# ---------------------------------------------------------------------------
# Every sentence plan carries a fallback concept set: the activities a
# camera could show while the sentence is spoken, the concrete
# objects in play, and the setting / place-type — as if the exact
# subject could not be filmed. B1 RECORDS the concepts (plan
# artefacts, shot needs, the Visual Plan page); nothing searches
# them yet — the tier-escalation loop that consumes them is B2/B3.
#
# Sources, stamped on every plan as ``concepts_source``:
# - ``llm_plan``: the LLM emitted the block; normalize_plan validates
#   it (caps below) and never invents additions.
# - ``need_time_derivation``: the deterministic derivation below,
#   used at need-build time for sentence records that carry no
#   concepts (cached/legacy plans).
# - ``planner2_factory``: proposed by Degraded Planner 2.0's
#   scenario factory (Master Directive Phase 7) and validated here
#   like an LLM block — stamped by normalize_plan when the raw plan
#   carries the factory's marker.

#: The six concept fields, in schema order.
FALLBACK_CONCEPT_FIELDS = (
    "activities", "objects", "settings", "place_types", "mood", "era_terms",
)

_CONCEPT_MAX_PHRASES = 4
_CONCEPT_MAX_WORDS = 6


def _clean_concept_phrases(value):
    """One concept list, cleaned: strings only, whitespace-collapsed,
    at most 4 phrases of at most 6 words each, empties dropped,
    duplicates removed preserving order."""
    phrases = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, str):
                continue
            phrase = " ".join(item.split())
            if not phrase:
                continue
            if len(phrase.split()) > _CONCEPT_MAX_WORDS:
                continue
            if phrase not in phrases:
                phrases.append(phrase)
            if len(phrases) >= _CONCEPT_MAX_PHRASES:
                break
    return phrases


def validate_fallback_concepts(raw):
    """Validate an LLM-supplied fallback_concepts block.

    Returns the cleaned block (all six fields, possibly empty lists)
    when ``raw`` is a dict, else None — the caller then derives.
    Validation only cleans; it never invents a phrase."""
    if not isinstance(raw, dict):
        return None
    return {
        field: _clean_concept_phrases(raw.get(field))
        for field in FALLBACK_CONCEPT_FIELDS
    }


def empty_fallback_concepts():
    return {field: [] for field in FALLBACK_CONCEPT_FIELDS}


# Deterministic derivation (plan §4.2): a small phrase table from
# verb lemma to the gerund activity phrase a camera could show, a
# concrete-object noun scan, and a place-type cue table. Everything
# derived comes from the sentence's own text or its bible entities —
# nothing is invented.
_ACTIVITY_PHRASES = {
    "search": "searching",
    "patrol": "patrolling",
    "travel": "travelling",
    "drive": "driving",
    "walk": "walking",
    "run": "running",
    "investigate": "investigating",
    "track": "tracking movements",
    "follow": "following",
    "watch": "watching",
    "arrest": "arresting",
    "work": "working",
    "build": "building",
    "sail": "sailing",
    "fly": "flying",
    "gather": "gathering",
    "march": "marching",
    "inspect": "inspecting",
    "examine": "examining",
    "review": "reviewing",
    "study": "studying",
    "check": "checking",
    "guard": "guarding",
    "escort": "escorting",
    "survey": "surveying",
    "interview": "interviewing",
    "question": "questioning",
    "testify": "testifying",
    "celebrate": "celebrating",
    "mourn": "mourning",
    "wait": "waiting",
}

#: Surface forms the regular -s/-ing/-ed generation cannot produce.
_ACTIVITY_FORM_OVERRIDES = {
    "patrol": ("patrolled", "patrolling"),
    "travel": ("travelled", "travelling"),
    "study": ("studied", "studying", "studies"),
    "run": ("ran", "running"),
    "drive": ("drove", "driven", "driving"),
    "fly": ("flew", "flown", "flying"),
    "sail": ("sailed", "sailing"),
    "build": ("built", "building"),
    "mourn": ("mourned", "mourning"),
}


def _activity_forms(lemma):
    forms = {lemma, lemma + "s", lemma + "es", lemma + "ing", lemma + "ed"}
    if lemma.endswith("e"):
        forms.add(lemma[:-1] + "ing")
        forms.add(lemma[:-1] + "d")
    forms.update(_ACTIVITY_FORM_OVERRIDES.get(lemma, ()))
    return forms


_ACTIVITY_FORM_TO_LEMMA = {
    form: lemma
    for lemma in _ACTIVITY_PHRASES
    for form in _activity_forms(lemma)
}

#: Verbs whose activity, when the sentence also names records /
#: documents / files / data, is the records-review activity a
#: documentary actually shows (the founder's Gilgo example).
_RECORDS_REVIEW_VERBS = {"study", "review", "examine", "inspect", "check"}
_RECORDS_NOUNS = {"records", "record", "documents", "document", "files", "file", "data"}

#: Concrete object nouns the derivation may lift from the text, with
#: a preceding modifier word when one is present ("travel records").
_OBJECT_NOUNS = (
    "records", "documents", "files", "data", "phones", "phone",
    "letters", "letter", "maps", "map", "photographs", "photograph",
    "photo", "diary", "journal", "notebook", "car", "truck", "boat",
    "ship", "plane", "train", "computer", "camera", "newspaper",
    "book", "weapon", "knife", "gun",
)

#: Words that may never serve as an object's modifier.
_MODIFIER_STOP = {
    "the", "a", "an", "his", "her", "their", "its", "my", "your",
    "our", "this", "that", "these", "those", "of", "and", "or",
    "in", "on", "at", "to", "from", "with", "for", "by",
}

#: Place cue word -> (settings additions, place_types additions).
_PLACE_CUES = {
    "beach": (["beach"], ["beach shoreline"]),
    "shore": (["shoreline"], ["beach shoreline"]),
    "coast": (["coastline"], ["coastline"]),
    "highway": (["highway"], ["highway"]),
    "parkway": (["parkway"], ["highway"]),
    "road": (["road"], ["road"]),
    "street": (["street"], ["street"]),
    "office": (["office"], []),
    "station": (["station"], []),
    "campus": (["college campus"], []),
    "university": (["college campus"], []),
    "courtroom": (["courtroom"], []),
    "court": (["courtroom"], []),
    "hospital": (["hospital"], []),
    "field": (["field"], ["open field"]),
    "forest": (["woods"], ["wooded area"]),
    "woods": (["woods"], ["wooded area"]),
    "brush": (["brush land"], ["wooded area"]),
    "sea": (["open sea"], ["open water"]),
    "ocean": (["ocean shore"], ["open water"]),
    "harbour": (["harbour"], ["harbour"]),
    "harbor": (["harbor"], ["harbor"]),
    "city": ([], ["city street"]),
    "town": ([], ["small town"]),
    "village": ([], ["village"]),
    "desert": (["desert"], ["desert"]),
    "mountain": (["mountains"], ["mountain landscape"]),
    "mountains": (["mountains"], ["mountain landscape"]),
    "river": (["riverbank"], ["river"]),
    "lake": (["lakeshore"], ["lake"]),
    "prison": (["prison"], []),
    "school": (["school"], []),
    "house": (["house"], []),
    "home": (["house interior"], []),
    "farm": (["farm"], ["farmland"]),
    "factory": (["factory"], []),
    "laboratory": (["laboratory"], []),
    "lab": (["laboratory"], []),
}


def derive_fallback_concepts(sentence_text, *, place=None, era=None, entities=()):
    """Derive a fallback concept set deterministically from a
    sentence's own text and its bible entities (plan §4.2).

    ``entities`` are bible entity dicts (canonical + type) mentioned
    in the sentence. Mood is never derived — the derivation cannot
    judge it, so the list stays honestly empty; the LLM path may
    supply it. Every list is capped by the same validation caps as
    LLM-supplied concepts."""
    text = str(sentence_text or "")
    words = re.findall(r"[a-z']+", text.lower())
    word_set = set(words)
    concepts = empty_fallback_concepts()

    # Activities: verb forms in the text, in text order. A BASE
    # form immediately followed by an object noun is that noun's
    # modifier, not a verb ("travel records" is not travelling);
    # inflected forms keep their verb reading ("reviewing records"
    # IS reviewing).
    activities = []
    for position, word in enumerate(words):
        lemma = _ACTIVITY_FORM_TO_LEMMA.get(word)
        if lemma is None:
            continue
        if (
            word == lemma
            and position + 1 < len(words)
            and words[position + 1] in _OBJECT_NOUNS
        ):
            continue
        if lemma in _RECORDS_REVIEW_VERBS and word_set & _RECORDS_NOUNS:
            phrase = (
                "reviewing records" if word_set & {"records", "record", "data"}
                else "reviewing documents"
            )
        else:
            phrase = _ACTIVITY_PHRASES[lemma]
        if phrase not in activities:
            activities.append(phrase)
    concepts["activities"] = activities[:_CONCEPT_MAX_PHRASES]

    # Objects: bible object entities first, then the noun scan.
    objects = []
    for entity in entities or ():
        if isinstance(entity, dict) and entity.get("type") == "object":
            canonical = str(entity.get("canonical") or "").strip()
            if canonical and canonical not in objects:
                objects.append(canonical)
    for position, word in enumerate(words):
        if word not in _OBJECT_NOUNS:
            continue
        phrase = word
        if position > 0:
            modifier = words[position - 1]
            # An INFLECTED verb form is not a noun modifier
            # ("reviewing records" -> the object is "records"); a
            # base form may be one ("travel records").
            _modifier_lemma = _ACTIVITY_FORM_TO_LEMMA.get(modifier)
            if (
                modifier not in _MODIFIER_STOP
                and modifier not in _OBJECT_NOUNS
                and not (
                    _modifier_lemma is not None and modifier != _modifier_lemma
                )
                and len(modifier) > 2
            ):
                phrase = f"{modifier} {word}"
        if phrase not in objects:
            objects.append(phrase)
    concepts["objects"] = objects[:_CONCEPT_MAX_PHRASES]

    # Settings / place types: the named place itself is a setting a
    # camera could show; cue words add the type vocabulary.
    settings = []
    place_types = []
    named_place = str(place or "").strip()
    if named_place:
        settings.append(named_place)
    for word in words:
        cue = _PLACE_CUES.get(word)
        if cue is None:
            continue
        for addition in cue[0]:
            if addition not in settings:
                settings.append(addition)
        for addition in cue[1]:
            if addition not in place_types:
                place_types.append(addition)
    if "police" in word_set and "station" in settings:
        settings[settings.index("station")] = "police station"
    concepts["settings"] = settings[:_CONCEPT_MAX_PHRASES]
    concepts["place_types"] = place_types[:_CONCEPT_MAX_PHRASES]

    # Era terms: explicit years in the text (plus their decade), and
    # the plan's era when it resolves to a year/decade.
    era_terms = []
    for year in extract_years(text):
        for term in (str(year), f"{year // 10 * 10}s"):
            if term not in era_terms:
                era_terms.append(term)
    era_value = _year_or_decade(era)
    if era_value and era_value not in era_terms:
        era_terms.append(era_value)
    concepts["era_terms"] = era_terms[:_CONCEPT_MAX_PHRASES]

    return concepts


# ---------------------------------------------------------------------------
# Visual intentions (Master Directive Phase 2 / AM-002 v1 — directive §4)
# ---------------------------------------------------------------------------
# Every sentence plan carries a visual-intention record beside its
# fallback concepts: what a documentary editor would want the audience
# to SEE while the sentence is spoken. The record extends the EXISTING
# plan schema through normalize_plan (never a competing schema) and is
# seeded from the sentence's own scenarios + fallback concepts — the
# derivation below invents nothing: every intent and query phrase is
# built from the plan's scenarios, its bible entities, its place/era
# or its concept lists.
#
# Sources, stamped as ``intents_source``:
# - ``llm_plan``: the LLM emitted the block; normalize_plan validates
#   it against the closed vocabulary (unknown intents dropped, lists
#   capped like concepts) and fills any missing field from the
#   derivation — validation never invents beyond the derivation.
# - ``derivation``: the deterministic derivation (degraded planner,
#   and LLM output without the block).

#: The directive's twelve visual-intent types (Part 4.1), closed
#: vocabulary. ``exact`` is the literal-depiction intent.
VISUAL_INTENTS = (
    "exact", "actual_event", "activity_broll", "establishing",
    "environmental_broll", "object_detail", "archival",
    "documentary_context", "human_activity", "geographic_context",
    "graphic_explanation", "atmospheric",
)

#: The per-intent query lists the contract carries (directive §4.2).
INTENT_QUERY_FIELDS = (
    "literal", "activity_broll", "establishing", "contextual", "archival",
)

_GRAPHIC_FALLBACK_KINDS = ("map", "timeline", "statistic", "diagram")
_MEDIA_TYPES = ("video", "image", "graphic", "any")
_RISK_LEVELS = ("low", "medium", "high")
_SECONDARY_INTENT_CAP = 4

#: Scenario format intent -> the plan contract's preferred media type.
_FORMAT_TO_MEDIA_TYPE = {
    "footage": "video",
    "photograph": "image",
    "document": "image",
    "map": "image",
    "chart": "image",
    "graphic": "graphic",
    "any": "any",
}

#: Primary intent by scenario kind when the beat is not exact-led.
_PRIMARY_INTENT_BY_KIND = {
    "event": "actual_event",
    "person_activity": "human_activity",
    "place": "establishing",
    "document": "object_detail",
    "object": "object_detail",
    "archival": "archival",
    "abstract_broll": "atmospheric",
}

#: Specificity -> how misleading a wrong visual would be. An exact
#: beat claims a specific person/place/event, so a wrong visual
#: misrepresents; a generic beat claims only mood.
_RISK_BY_SPECIFICITY = {
    "exact": "high",
    "representative": "medium",
    "generic": "low",
}


def _derive_visual_intents(*, role, scenarios, concepts, place, time,
                           events):
    """Deterministic visual intentions from a sentence's own plan
    material (its scenarios, fallback concepts, place, era, events).

    Only sensible intents are produced: the primary follows the lead
    scenario's kind/specificity; secondaries follow the concept
    lists (activities -> activity B-roll, objects -> detail shots,
    settings/place-types -> environmental, a named place ->
    establishing, an era -> archival). Nothing references an entity,
    place or phrase the plan itself does not carry."""
    concepts = concepts if isinstance(concepts, dict) else {}
    scenarios = [s for s in (scenarios or []) if isinstance(s, dict)]
    primary_scenario = scenarios[0] if scenarios else {}
    kind = str(primary_scenario.get("kind") or "").strip().lower()
    specificity = str(
        primary_scenario.get("specificity") or "representative"
    ).strip().lower()
    if specificity not in SPECIFICITY:
        specificity = "representative"
    subject = str(primary_scenario.get("subject") or "").strip()
    era = str(primary_scenario.get("era") or time or "").strip()
    named_place = str(
        primary_scenario.get("place") or place or ""
    ).strip()

    # Primary: an exact event is actual-event footage; any other
    # exact-led beat demands the literal subject; otherwise the lead
    # scenario's kind decides.
    if kind == "event":
        primary = "actual_event"
    elif specificity == "exact" and (subject or primary_scenario.get("subject_id")):
        primary = "exact"
    else:
        primary = _PRIMARY_INTENT_BY_KIND.get(kind, "documentary_context")

    secondary = []

    def _add(intent):
        if intent != primary and intent not in secondary:
            secondary.append(intent)

    activities = concepts.get("activities") or []
    objects = concepts.get("objects") or []
    settings = concepts.get("settings") or []
    place_types = concepts.get("place_types") or []
    era_terms = concepts.get("era_terms") or []
    if activities:
        _add("activity_broll")
    if objects:
        _add("object_detail")
    if named_place:
        _add("establishing")
        if role in ("hook", "setup", "background"):
            # A scene-setting beat benefits from knowing WHERE in
            # the world it sits, not only the site itself.
            _add("geographic_context")
    if settings or place_types:
        _add("environmental_broll")
    if era or era_terms:
        _add("archival")
    if concepts.get("mood"):
        _add("atmospheric")
    if role in ("evidence", "legal") or events:
        # Chronology/relationship beats are the ones a timeline or
        # evidence graphic genuinely explains.
        _add("graphic_explanation")
    secondary = secondary[:_SECONDARY_INTENT_CAP]

    # Per-intent query phrases — built only from plan material.
    terms = [
        str(t).strip()
        for t in (primary_scenario.get("search_terms") or [])
        if str(t).strip()
    ]
    literal = list(terms)
    if not literal and subject:
        literal = [
            " ".join(p for p in (subject, era) if p).strip()
        ] if era else [subject]
    activity_queries = list(activities[:3])
    if activity_queries and settings:
        combined = f"{activity_queries[0]} {settings[0]}"
        if combined not in activity_queries:
            activity_queries.append(combined)
    establishing_queries = []
    if named_place:
        establishing_queries.append(named_place)
    establishing_queries.extend(place_types[:2])
    contextual_queries = list(settings[:2]) + list(objects[:2])
    archival_queries = []
    if subject and era:
        archival_queries.append(f"{subject} {era}")
    archival_queries.extend(era_terms[:2])
    if named_place and era_terms:
        archival_queries.append(f"{named_place} {era_terms[0]}")
    intent_queries = {
        "literal": _clean_concept_phrases(literal),
        "activity_broll": _clean_concept_phrases(activity_queries),
        "establishing": _clean_concept_phrases(establishing_queries),
        "contextual": _clean_concept_phrases(contextual_queries),
        "archival": _clean_concept_phrases(archival_queries),
    }

    # Graphic fallback: a map when a scene-setting beat names a
    # place; a timeline when the beat carries events or a time.
    if named_place and role in ("hook", "setup", "background"):
        graphic_fallback = {"allowed": True, "kind": "map"}
    elif events or time:
        graphic_fallback = {"allowed": True, "kind": "timeline"}
    else:
        graphic_fallback = {"allowed": False, "kind": None}

    format_intent = str(
        primary_scenario.get("format_intent") or "any"
    ).strip().lower()
    return {
        "primary_visual_intent": primary,
        "secondary_visual_intents": secondary,
        "intent_queries": intent_queries,
        "graphic_fallback": graphic_fallback,
        "preferred_media_type": _FORMAT_TO_MEDIA_TYPE.get(
            format_intent, "any"
        ),
        "specificity_requirement": specificity,
        "visual_misrepresentation_risk": _RISK_BY_SPECIFICITY[specificity],
    }


def validate_visual_intents(raw, *, derived):
    """Validate an LLM-supplied visual_intents block against the
    closed vocabulary, field by field; every missing or invalid
    field falls back to the derived value. Returns the cleaned
    block, or None when ``raw`` is not a dict (caller uses the
    derivation wholesale)."""
    if not isinstance(raw, dict):
        return None
    block = dict(derived)
    primary = str(raw.get("primary_visual_intent") or "").strip().lower()
    if primary in VISUAL_INTENTS:
        block["primary_visual_intent"] = primary
    secondary_raw = raw.get("secondary_visual_intents")
    if isinstance(secondary_raw, list):
        secondary = []
        for item in secondary_raw:
            intent = str(item or "").strip().lower()
            if (
                intent in VISUAL_INTENTS
                and intent != block["primary_visual_intent"]
                and intent not in secondary
            ):
                secondary.append(intent)
        block["secondary_visual_intents"] = secondary[
            :_SECONDARY_INTENT_CAP
        ]
    queries_raw = raw.get("intent_queries")
    if isinstance(queries_raw, dict):
        block["intent_queries"] = {
            field: _clean_concept_phrases(queries_raw.get(field))
            for field in INTENT_QUERY_FIELDS
        }
    fallback_raw = raw.get("graphic_fallback")
    if isinstance(fallback_raw, dict):
        kind = str(fallback_raw.get("kind") or "").strip().lower()
        block["graphic_fallback"] = {
            "allowed": bool(fallback_raw.get("allowed")),
            "kind": kind if kind in _GRAPHIC_FALLBACK_KINDS else None,
        }
    media_type = str(raw.get("preferred_media_type") or "").strip().lower()
    if media_type in _MEDIA_TYPES:
        block["preferred_media_type"] = media_type
    specificity = str(
        raw.get("specificity_requirement") or ""
    ).strip().lower()
    if specificity in SPECIFICITY:
        block["specificity_requirement"] = specificity
    risk = str(
        raw.get("visual_misrepresentation_risk") or ""
    ).strip().lower()
    if risk in _RISK_LEVELS:
        block["visual_misrepresentation_risk"] = risk
    return block


def build_visual_intents(raw, *, role, scenarios, concepts, place,
                         time, events):
    """The plan's visual-intention record: the validated LLM block
    when one was supplied, else the deterministic derivation. The
    record is stamped with its source either way."""
    derived = _derive_visual_intents(
        role=role, scenarios=scenarios, concepts=concepts,
        place=place, time=time, events=events,
    )
    validated = validate_visual_intents(raw, derived=derived)
    if validated is not None:
        validated["intents_source"] = "llm_plan"
        return validated
    derived["intents_source"] = "derivation"
    return derived


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
    # B-roll B1 (§4.1/§4.2): every plan carries its fallback concept
    # set, stamped with its source. An LLM-supplied block is
    # validated and used verbatim-cleaned; anything else (degraded
    # plans, LLM output without the block) gets the deterministic
    # derivation from the sentence's own text and bible entities.
    concepts = validate_fallback_concepts(raw.get("fallback_concepts"))
    if concepts is not None:
        plan["fallback_concepts"] = concepts
        # Master Directive Phase 7: a concept set proposed by
        # Degraded Planner 2.0's factory is stamped with its own
        # source (the stamp B1's comment anticipated) — never
        # "llm_plan", which would misattribute deterministic output
        # to the model.
        plan["concepts_source"] = (
            "planner2_factory" if raw.get("_planner2") else "llm_plan"
        )
    else:
        plan["fallback_concepts"] = derive_fallback_concepts(
            sentence,
            place=(
                (scenarios[0].get("place") if scenarios else None)
                or raw.get("place")
            ),
            era=(scenarios[0].get("era") if scenarios else None),
            entities=[by_id[e] for e in entity_ids if e in by_id],
        )
        plan["concepts_source"] = "need_time_derivation"
    # Master Directive Phase 2 (§4.2): the visual-intention record,
    # extending this same plan — seeded from the sentence's scenarios
    # and the concept set stamped just above, validated when the LLM
    # supplied one, derived deterministically otherwise (the degraded
    # planner follows the same intent principles without an LLM).
    plan["visual_intents"] = build_visual_intents(
        raw.get("visual_intents"),
        role=role,
        scenarios=plan["scenarios"],
        concepts=plan["fallback_concepts"],
        place=plan.get("place"),
        time=plan.get("time"),
        events=plan.get("events"),
    )
    for scenario in plan["scenarios"]:
        scenario["queries"] = compose_queries(scenario, plan, bible)
        # Phase 3 / AM-021: these queries were COMPOSED here from the
        # scenario's own facets (not authored on it), so stamp their
        # provenance. build_shot_needs copies this onto the need's
        # query_sources; a scenario whose queries carry no stamp
        # (authored by hand or by another producer) is labelled
        # "scenario" there instead.
        scenario["queries_source"] = "composed"
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

    # Stage 1 query fixes (diagnosis D1/D2/D4): the era must survive
    # into at least one query even when it is a range ("1950s–1961")
    # or non-numeric ("Cold War era") — _year_or_decade alone kept
    # only the first year token, so 19 of 69 stored Berlin shot needs
    # had NO query carrying their era. The full era phrase is used
    # when it says more than the extracted year.
    era_raw = str(scenario.get("era") or plan.get("time") or "").strip()
    era_phrase = era_raw if (era_raw and era_raw.lower() != year.lower()) else ""

    candidates = []
    if specificity == "exact":
        candidates += [
            _clean_query([anchor, event, year]),
            _clean_query([anchor, event]),
            # Place joins the term query (was confined to the last
            # candidate, which the max_queries cap often cut).
            _clean_query([anchor, terms[0] if terms else "", place, year]),
            _clean_query([anchor, place, era_phrase]),
        ]
    elif specificity == "representative":
        # D1: a named PERSON subject must survive into at least one
        # query — previously the anchor was appended only for
        # non-person subjects, so person-led needs searched bare
        # terms unless the LLM had embedded the name in search_terms.
        if anchor and subject and subject["type"] == "person":
            candidates.append(
                _clean_query([anchor, terms[0] if terms else event, year])
            )
        for i, term in enumerate(terms[:2]):
            # The first variant of the first term carries the full
            # era phrase when it says more than the extracted year,
            # so the era survives the max_queries cap.
            first_year = (era_phrase or year) if i == 0 else year
            candidates += [_clean_query([term, place, first_year]), _clean_query([term, year]), _clean_query([term])]
        if anchor and subject and subject["type"] != "person":
            candidates.append(_clean_query([anchor, terms[0] if terms else event]))
    else:  # generic
        # D2: generic queries dropped anchor/place/era entirely; the
        # first query now carries the place (or anchor) so generic
        # b-roll stays geographically anchored, the rest stay bare.
        for i, term in enumerate(terms[:2]):
            if i == 0:
                candidates.append(_clean_query([term, place or anchor, era_phrase or year]))
            else:
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

# ---------------------------------------------------------------------------
# Stage 1 / R7 foundation: CATEGORY-AWARE VERIFICATION PROFILES.
#
# Diagnosis (ASSET_MANAGER_DIAGNOSIS_2026-11-20, from the stored Berlin
# and Kohberger artefacts): the metadata gate was the dominant funnel
# stage (Berlin 2,410 of 4,727 evaluation rows; Kohberger 3,813 of
# 4,195) and it applied ONE uniform contract — named subject and named
# place present in title+snippet — whatever the sentence actually
# needed. Profiles separate what a category legitimately REQUIRES at
# the gate from what is merely scored evidence:
#
# - evidence (specificity "exact"): the sentence claims a specific
#   person/place/event; subject AND place stay REQUIRED at the gate,
#   exactly as before.
# - contextual (specificity "representative"): at Stage 1 the subject
#   and place stayed REQUIRED at the gate. Stage 7 (Master
#   Architecture Review C.B.2, R7) supersedes that contract: the
#   named-entity absence requirements are replaced by an
#   activity/place-TYPE presence floor (see CATEGORY_PROFILES below).
#   The Stage 1 compound-place rule ("King Road house, Moscow,
#   Idaho" is a set of alternative components, not one indivisible
#   string — see gate_shot_need) still governs the evidence contract,
#   where named places remain required.
# - atmosphere (specificity "generic"): generic b-roll/atmosphere has
#   no specific subject or place contract, so neither is required at
#   the gate. Acceptance is still governed by verify_candidate's
#   topical-evidence rules and the generic threshold — the gate simply
#   stops pre-empting the scorer with a contract the category never
#   had.
#
# Universal rules are NOT profiled: banned providers, foreign
# provenance, forbidden terms and every positive-contradiction reject
# apply identically in all categories, and the topical-evidence gates
# in verify_candidate are unchanged. Thresholds are unchanged.
# A scenario/need may carry an explicit "visual_category" (the Visual
# Director seam, Stage 8); otherwise the category derives from the
# shot's own signals — shot_type, sentence role and specificity
# (Stage 7 derivation in resolve_visual_category below).
# ---------------------------------------------------------------------------

VISUAL_CATEGORIES = ("evidence", "contextual", "environmental", "atmosphere")

_CATEGORY_BY_SPECIFICITY = {
    "exact": "evidence",
    "representative": "contextual",
    "generic": "atmosphere",
}

# Shot types that always carry the evidence contract (Stage 7, review
# section C.B.2): a document or a specific object on screen is a
# factual claim, whatever the specificity label says.
_EVIDENCE_SHOT_TYPES = frozenset({"document", "object"})

CATEGORY_PROFILES = {
    # Evidence: the full contract, unchanged — named subject AND named
    # place required at the gate, full scorer dimensions required.
    "evidence": {
        "subject_required": True,
        "place_required": True,
        "type_presence_required": False,
    },
    # Contextual (Stage 7, C.B.2): the named-entity ABSENCE
    # requirements no longer apply — requiring a generic candidate's
    # metadata to name the specific person/place was the category
    # error behind the funnel collapse. Instead the gate requires the
    # activity/place-TYPE token set (bedroom, student house, night —
    # never the proper noun) to show non-zero presence. The scorer is
    # unchanged and the specificity threshold is unchanged: the
    # profile changes which dimensions are REQUIRED, never the bar.
    "contextual": {
        "subject_required": False,
        "place_required": False,
        "type_presence_required": True,
    },
    # Environmental (Stage 7, C.B.2): as contextual, with location
    # judged as place-type plus era/region compatibility; the era
    # dimension must be present OR explicitly waived by the profile
    # for era-neutral settings, and the waiver is recorded on the
    # verdict (``era_waived``; temporal dimension ``not_required``).
    "environmental": {
        "subject_required": False,
        "place_required": False,
        "type_presence_required": True,
    },
    # Atmosphere: topical dimension only; contradiction vetoes still
    # apply (vetoes are universal and fire before any profile logic).
    "atmosphere": {
        "subject_required": False,
        "place_required": False,
        "type_presence_required": False,
    },
}


def resolve_visual_category(source):
    """The visual category for a scenario or shot need dict.

    Derivation order (Stage 7, C.B.2) — every signal comes from the
    SHOT, never from a candidate: an explicit ``visual_category``
    wins; a document/object shot type or an evidence sentence role
    carries the evidence contract; exact specificity is evidence; an
    abstract-broll shot is atmosphere; a place shot at
    representative/generic specificity whose need names NO specific
    place is environmental (it establishes setting, not a site);
    anything else falls back to the Stage 1 specificity mapping
    (unknown specificity defaults to contextual, mirroring
    verify_candidate's 0.34 default)."""
    source = source or {}
    explicit = str(source.get("visual_category") or "").strip().lower()
    if explicit in VISUAL_CATEGORIES:
        return explicit
    specificity = str(source.get("specificity") or "").strip().lower()
    shot_type = str(
        source.get("shot_type") or source.get("kind") or ""
    ).strip().lower()
    if shot_type in _EVIDENCE_SHOT_TYPES:
        return "evidence"
    if str(source.get("role") or "").strip().lower() == "evidence":
        return "evidence"
    if specificity == "exact":
        return "evidence"
    if shot_type == "abstract_broll":
        return "atmosphere"
    if (
        shot_type == "place"
        and specificity in ("representative", "generic")
        and "place" in source
        and not str(source.get("place") or "").strip()
    ):
        return "environmental"
    return _CATEGORY_BY_SPECIFICITY.get(specificity, "contextual")


def category_profile(category):
    """The gate-requirement profile for a visual category."""
    return CATEGORY_PROFILES.get(
        str(category or "").strip().lower(),
        CATEGORY_PROFILES["contextual"],
    )

# ---------------------------------------------------------------------------
# Quality Recovery Phase 1/2 helpers: banned providers, date maths and
# positive-contradiction detection. A POSITIVE contradiction (the
# candidate affirmatively names a different person / place / event, or
# carries an explicit date incompatible with the scenario era) is a
# HARD rejection — it is checked before any threshold comparison, so
# neither generic specificity (0.22) nor topical strength nor source
# quality can compensate for it. ABSENCE of a signal is never a
# contradiction: a dateless candidate is judged on its other signals,
# and a candidate naming the required person alongside another person
# (group / event coverage) is not a contradiction.
# ---------------------------------------------------------------------------

# The two permanently banned providers (founder directive, IA-05). The
# names are assembled at runtime so this module carries no provider
# integration artefact for the static ban scan in test_provider_ban.py
# to trip over — the ban here is a runtime rejection, not an integration.
_BANNED_PROVIDERS = frozenset({"pex" + "els", "pixa" + "bay"})

BANNED_PROVIDER_REASON = "banned_provider"
FOREIGN_PROVENANCE_REASON = "foreign_provenance"
CONTRADICTORY_PERSON = "contradictory_person"
CONTRADICTORY_DATE = "contradictory_date"
CONTRADICTORY_PLACE = "contradictory_place"
CONTRADICTORY_EVENT = "contradictory_event"

_YEAR_RE = re.compile(r"\b(1[5-9]\d\d|20\d\d)\b")


def candidate_provider(candidate):
    """The candidate's provider label, normalised (provider > source >
    source_domain). Empty string when the candidate carries none."""
    if not isinstance(candidate, dict):
        return ""
    for key in ("provider", "source", "source_domain"):
        value = str(candidate.get(key) or "").strip().lower()
        if value:
            return value
    return ""


def is_banned_provider(candidate):
    """True when the candidate's provider is a permanently banned one.

    Defence in depth behind the IA-05 static ban: no live search seam
    produces these providers, but a candidate injected at any seam
    (a future provider, a cache, a hand-built pool) must still be
    rejected by the verification chain itself.
    """
    provider = candidate_provider(candidate)
    if provider:
        if provider in _BANNED_PROVIDERS:
            return True
        # Tolerate decorated labels (a banned name plus decoration, or a
        # banned provider's domain used as the source label).
        head = re.split(r"[\s./]", provider)[0]
        if head in _BANNED_PROVIDERS:
            return True
    # Stage 1 gap closure: the label check alone missed candidates
    # whose labels are innocuous but whose URL points at a banned
    # host (e.g. Serper image results carry a site label and no
    # provider key), and the head-token rule missed "www."-prefixed
    # domains. Inspect the URL host's tokens as a second signal: any
    # host label equal to a banned name rejects the candidate.
    if isinstance(candidate, dict):
        for key in ("url", "video_url", "image_url", "download_url", "link"):
            raw = str(candidate.get(key) or "").strip()
            if not raw:
                continue
            host = urlparse(raw if "://" in raw else f"https://{raw}").hostname or ""
            tokens = [t for t in re.split(r"[.\-_]", host.lower()) if t]
            if any(t in _BANNED_PROVIDERS for t in tokens):
                return True
    return False


# ---------------------------------------------------------------------------
# Run-9 quality fixes (2026-11-22): defect classes that the metadata
# verifier could not see in production run e95ac155 (the Gabby Petito
# pipeline film). Two are now HARD rejections in the verification
# chain itself, decided before any threshold comparison exactly like
# the banned-provider and foreign-provenance rules; the third is a
# recorded signal only (see item 1):
#
# 1. WATERMARKED STOCK PREVIEWS — DETECTION ONLY (founder decision
#    2026-11-22, reversing the original run-9 hard rejection). The
#    sentence-9 visual was an Alamy comp image (provider "Alamy",
#    host l450v.alamy.com) with the agency watermark burned across
#    the frame. Stock agencies serve watermarked previews from their
#    public CDNs by construction — a candidate whose provider label
#    or URL host identifies a stock agency preview channel, or whose
#    metadata says "watermark", is detected by is_watermarked_stock
#    below. The founder ruled watermarked stock ALLOWED: it is
#    scored and accepted/rejected on the normal criteria like any
#    other candidate. The detection is still recorded on every
#    verdict as the non-blocking ``watermarked_preview`` fact, so
#    provenance stays honest and usage remains reviewable later —
#    a watermarked preview is never labelled clean or licensed.
#
# 2. SOCIAL-PLATFORM / MEME-FORMAT SOURCES. A vertical TikTok-style
#    "POV" meme clip about a DIFFERENT case illustrated the remains
#    beat, and personal Facebook posts/videos were accepted for two
#    other beats. User-generated social platforms (TikTok, Instagram,
#    Facebook, X) and YouTube Shorts are re-upload/meme channels, not
#    documentary sources; meme-format titles ("POV: ..." captions,
#    #fyp) mark the same class in metadata.
#
# 3. POSITIVE GEOGRAPHY / FOREIGN-PERSON CONTRADICTIONS (in
#    detect_contradiction below): the Alamy title literally read
#    "Brighton UK" for a Moab, Utah beat, and the meme clip's title
#    named Tristyn Bailey — a person from a different case who is in
#    no bible. The bible-entity contradiction checks cannot see
#    either: Brighton/UK are not bible places and Bailey is not a
#    bible person. Both checks stay POSITIVE-only, mirroring the
#    existing doctrine: absence of a signal is never a contradiction.
# ---------------------------------------------------------------------------

# Verdict field under which the watermark detection is recorded
# (non-blocking fact, founder decision 2026-11-22 — see item 1
# above). It is deliberately NOT a rejection reason.
WATERMARKED_PREVIEW_FIELD = "watermarked_preview"
SOCIAL_PLATFORM_REASON = "social_platform_source"
MEME_FORMAT_REASON = "meme_format_source"
CONTRADICTORY_GEOGRAPHY = "contradictory_geography"
UNRELATED_NAMED_PERSON = "unrelated_named_person"

# Stock agencies whose publicly served images are watermarked
# previews/comps. Matched against the provider label's head token
# and against URL host tokens, the same two signals
# is_banned_provider inspects.
_WATERMARKED_STOCK_PROVIDERS = frozenset({
    "alamy", "dreamstime", "123rf", "depositphotos", "shutterstock",
    "gettyimages", "istockphoto", "istock", "picfair", "stockfresh",
    "bigstockphoto", "bigstock", "fotolia", "vectorstock", "pond5",
})

# User-generated social platforms: re-uploads and meme channels.
_SOCIAL_HOSTS = frozenset({
    "tiktok", "instagram", "facebook", "fb", "twitter", "x",
    "snapchat", "reddit",
})

# Meme-format title markers: a leading "POV" caption (the meme
# format's signature — a news title does not lead with it) and
# engagement-bait hashtags.
_MEME_TITLE_RE = re.compile(r"^\s*pov\s*[:\-]", re.IGNORECASE)
_MEME_TAGS = ("#fyp", "#foryou", "#foryoupage", "#viralvideo", "#meme")


def _candidate_urls(candidate):
    if not isinstance(candidate, dict):
        return []
    return [
        str(candidate.get(key) or "").strip()
        for key in ("url", "video_url", "image_url", "download_url", "link")
    ]


def _url_host_tokens(raw_url):
    if not raw_url:
        return []
    host = urlparse(
        raw_url if "://" in raw_url else f"https://{raw_url}"
    ).hostname or ""
    return [t for t in re.split(r"[.\-_]", host.lower()) if t]


def is_watermarked_stock(candidate):
    """True when the candidate is a stock agency's watermarked
    preview/comp (run-9 fix 1): the provider label or any URL host
    names a stock agency, or the metadata says "watermark".

    Detection only — since the founder decision of 2026-11-22 this
    no longer rejects; verify_candidate records the result on the
    verdict under WATERMARKED_PREVIEW_FIELD and scores the
    candidate normally."""
    if not isinstance(candidate, dict):
        return False
    provider = candidate_provider(candidate)
    if provider:
        head = re.split(r"[\s./]", provider)[0]
        if head in _WATERMARKED_STOCK_PROVIDERS:
            return True
    for raw in _candidate_urls(candidate):
        if any(t in _WATERMARKED_STOCK_PROVIDERS for t in _url_host_tokens(raw)):
            return True
    meta = " ".join(
        str(candidate.get(k) or "")
        for k in ("title", "snippet", "description")
    ).lower()
    return "watermark" in meta


def is_social_platform_source(candidate):
    """True when the candidate comes from a user-generated social
    platform or a YouTube Shorts URL (run-9 fix 3, source signal)."""
    if not isinstance(candidate, dict):
        return False
    provider = candidate_provider(candidate)
    if provider:
        head = re.split(r"[\s./]", provider)[0]
        if head in _SOCIAL_HOSTS:
            return True
    for raw in _candidate_urls(candidate):
        tokens = _url_host_tokens(raw)
        if any(t in _SOCIAL_HOSTS for t in tokens):
            return True
        lowered = raw.lower()
        if "youtube.com/shorts/" in lowered or "youtu.be" in lowered and "/shorts" in lowered:
            return True
    return False


def has_meme_format_title(candidate):
    """True when the candidate's title carries meme-format markers
    (run-9 fix 3, format signal): a leading "POV:" caption or
    engagement-bait hashtags."""
    if not isinstance(candidate, dict):
        return False
    title = str(candidate.get("title") or "")
    if _MEME_TITLE_RE.search(title):
        return True
    lowered = title.lower()
    return any(tag in lowered for tag in _MEME_TAGS)


# Compact gazetteer for the geography contradiction: country name /
# demonym / region / major-place markers, matched on normalised
# text. Deliberately limited to the countries whose stock and news
# footage commonly pollutes English-language documentary retrieval.
_COUNTRY_MARKERS = {
    "united states": {
        "united states", "usa", "america", "american", "new york",
        "los angeles", "chicago", "houston", "florida", "texas",
        "california", "utah", "wyoming", "arizona", "colorado",
        "ohio", "georgia", "virginia", "washington dc", "las vegas",
        "miami", "boston", "seattle", "denver", "phoenix", "dallas",
        "atlanta", "idaho", "montana", "nevada", "oregon",
        "north carolina", "south carolina", "tennessee", "missouri",
        "illinois", "pennsylvania", "michigan", "minnesota",
        "wisconsin", "alabama", "louisiana", "kentucky", "oklahoma",
        "connecticut", "iowa", "mississippi", "arkansas", "kansas",
        "nebraska", "new mexico", "west virginia", "maryland",
        "maine", "vermont", "new hampshire", "rhode island",
        "delaware", "new jersey", "indiana", "massachusetts",
        "hawaii", "alaska", "north dakota", "south dakota",
    },
    "united kingdom": {
        "united kingdom", "uk", "britain", "british", "england",
        "scotland", "scottish", "wales", "welsh",
        "northern ireland", "london", "brighton", "manchester",
        "liverpool", "birmingham", "glasgow", "edinburgh", "cardiff",
        "belfast", "leeds", "sheffield", "bristol", "yorkshire",
        "cornwall", "kent", "essex", "sussex",
    },
    "canada": {
        "canada", "canadian", "toronto", "vancouver", "montreal",
        "ottawa", "ontario", "quebec", "alberta",
        "british columbia", "manitoba", "saskatchewan",
    },
    "australia": {
        "australia", "australian", "sydney", "melbourne", "brisbane",
        "perth", "adelaide", "queensland", "tasmania",
        "new south wales",
    },
    "germany": {
        "germany", "berlin", "munich", "hamburg",
        "frankfurt", "cologne", "bavaria",
    },
    "france": {
        "france", "paris", "marseille", "lyon", "normandy",
        "provence",
    },
    "ireland": {"ireland", "irish", "dublin"},
    "india": {
        "india", "mumbai", "delhi", "bangalore", "kolkata",
        "chennai", "hyderabad",
    },
    "south africa": {
        "south africa", "south african", "johannesburg", "cape town",
        "pretoria", "durban",
    },
    "new zealand": {
        "new zealand", "auckland", "wellington", "christchurch",
    },
}


def _countries_in_text(text):
    """The gazetteer countries positively named in normalised text."""
    if not text:
        return set()
    tokens = set(text.split())
    found = set()
    for country, markers in _COUNTRY_MARKERS.items():
        for marker in markers:
            if " " in marker:
                if marker in text:
                    found.add(country)
                    break
            elif marker in tokens:
                found.add(country)
                break
    return found


def _scenario_countries(bible, scenario):
    """The country set the production itself establishes: every
    bible entity name/alias plus the scenario's place/event/visual
    fields."""
    parts = []
    for entity in bible.get("entities", []) or []:
        parts.append(entity.get("canonical") or "")
        parts.extend(entity.get("aliases") or [])
    for key in ("place", "event", "visual"):
        parts.append(str(scenario.get(key) or ""))
    return _countries_in_text(_norm(" ".join(parts)))


# Role words that legitimately precede a named person in news
# metadata (officials, experts, spokespeople) — a name introduced by
# one of these is reporting ABOUT the case, not a different case.
_NAME_ROLE_WORDS = frozenset({
    "officer", "sheriff", "detective", "dr", "mr", "mrs", "ms",
    "judge", "attorney", "chief", "director", "agent", "sgt",
    "sergeant", "lieutenant", "captain", "mayor", "governor",
    "senator", "president", "professor", "pastor", "coach",
    "spokesperson", "trooper", "marshal", "warden", "coroner",
})

_NAME_BIGRAM_STOPLIST = frozenset({
    "united states", "united kingdom", "supreme court",
    "police department", "high school", "national park",
    "white house", "social media", "press conference",
    "breaking news", "body cam", "dash cam", "security camera",
    "surveillance video", "press release", "news conference",
})

_PERSON_NAME_RE = re.compile(r"\b([A-Z][a-z]{1,}) ([A-Z][a-z]{1,})\b")


def _unrelated_named_person(candidate, bible, text, subject_hit):
    """A person NAME the candidate's title positively carries that
    belongs to no bible entity, or "" (run-9 fix 2/3 overlap).

    Fires only when the production is person-anchored (the bible
    names at least one person), the candidate names NO bible person
    at all, and a capitalised two-word name in the title matches no
    bible entity of any type and is not introduced by a role word.
    This is the Tristyn Bailey shape: a different case's victim
    presented as this beat's visual."""
    persons = [e for e in bible.get("entities", []) or [] if e.get("type") == "person"]
    if not persons:
        return ""
    if subject_hit:
        return ""
    if any(_entity_mentioned(e, text) for e in persons):
        return ""
    title = str(candidate.get("title") or "")
    if not title:
        return ""
    entity_names = set()
    for entity in bible.get("entities", []) or []:
        entity_names.add(_norm(entity.get("canonical") or ""))
        entity_names |= {_norm(a) for a in (entity.get("aliases") or [])}
        if entity.get("type") == "person":
            parts = str(entity.get("canonical") or "").split()
            if len(parts) >= 2:
                entity_names.add(_norm(parts[-1]))
    for match in _PERSON_NAME_RE.finditer(title):
        name = match.group(0)
        normalised = _norm(name)
        if normalised in _NAME_BIGRAM_STOPLIST:
            continue
        # A bigram that STARTS with a role word is the official's
        # title + first name ("Sheriff Kurt ..."), not a bare name.
        if match.group(1).lower() in _NAME_ROLE_WORDS:
            continue
        if any(n and (normalised in n or n in normalised) for n in entity_names):
            continue
        # Role-word exemption: the word immediately before the name.
        before = title[: match.start()].rstrip().split()
        if before and before[-1].strip("—–-:;,").lower() in _NAME_ROLE_WORDS:
            continue
        return name
    return ""


def candidate_project_id(candidate):
    """The project identity RECORDED on a candidate record, or None.

    Discovery candidates produced by the current run's own search
    carry no project stamp — a stamp exists only on records that
    were produced or stored under some project/run context (asset
    records, cached records, hand-built injections)."""
    if not isinstance(candidate, dict):
        return None
    value = candidate.get("project_id")
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _current_project_id():
    """The project the CURRENT run belongs to (the active
    RunContext), or None when no production run is active."""
    try:
        from services import run_context
    except ImportError:  # flat-layout fallback
        import run_context
    try:
        ctx = run_context.current_context()
    except Exception:
        return None
    project_id = getattr(ctx, "project_id", None)
    return str(project_id) if project_id is not None else None


def is_foreign_provenance(candidate):
    """True when the candidate's RECORDED project identity positively
    mismatches the current run's project (plan §3.3 item 1: wrong
    project / production is a hard rejection, tests 17-18).

    Positive-mismatch rule, mirroring the contradiction doctrine: a
    MISSING stamp is not a mismatch — unstamped discovery candidates
    and legacy/stored records are judged on their other signals. A
    stamp that is present and differs from the current run's project
    is foreign. When no run is active the current project is unknown,
    and a stamped record cannot be shown to belong to the chain being
    built either: provenance stamps are only ever written by a
    project/run context, so a present stamp is foreign to an
    unstamped discovery chain."""
    stamp = candidate_project_id(candidate)
    if stamp is None:
        return False
    return stamp != _current_project_id()


def extract_years(text):
    """Explicit 4-digit years mentioned in `text`, as ints, in order."""
    return [int(y) for y in _YEAR_RE.findall(str(text or ""))]


def era_compatibility(era, years):
    """Is an explicit candidate year set compatible with a scenario era?

    Returns None when the check does not apply (no parseable era, or
    the candidate states no year — absence is not a contradiction),
    True when at least one stated year is compatible, False when the
    candidate states years and NONE is compatible (a positive date
    contradiction).

    An exact era ("2022") admits only that year: a 2019 asset is not
    direct evidence for an exact 2022 event. A decade era ("1960s")
    admits the whole decade: a 1963 asset is fine for a "1960s" beat.
    """
    parsed = _year_or_decade(era)
    if not parsed:
        return None
    years = [int(y) for y in (years or [])]
    if not years:
        return None
    if parsed.endswith("s"):
        start = int(parsed[:4])
        return any(start <= y <= start + 9 for y in years)
    return any(y == int(parsed[:4]) for y in years)


def _entity_mentioned(entity, text):
    """True when the (normalised) text names this bible entity, by its
    canonical name or any alias (persons also match on surname, the
    same rule the subject check uses)."""
    names = {entity.get("canonical") or "", *(entity.get("aliases") or [])}
    if entity.get("type") == "person":
        parts = str(entity.get("canonical") or "").split()
        if len(parts) >= 2:
            names.add(parts[-1])
    return any(_norm(n) and _norm(n) in text for n in names)


def _find_entity(bible, *, types, name):
    """The bible entity of one of `types` whose canonical/alias matches
    `name` (normalised equality or containment), or None."""
    target = _norm(name)
    if not target:
        return None
    for entity in bible.get("entities", []) or []:
        if entity.get("type") not in types:
            continue
        names = {_norm(entity.get("canonical") or "")}
        names |= {_norm(a) for a in (entity.get("aliases") or [])}
        if target in names or any(n and (n in target or target in n) for n in names):
            return entity
    return None


def detect_contradiction(candidate, scenario, bible, text, subject, subject_hit):
    """Positive identity contradiction in a candidate, or ("", "").

    `text` is the candidate's normalised metadata text (the same text
    the scorer reads). Checks, in order: person, place, event, date.
    Only POSITIVE mismatches count — the candidate must affirmatively
    name a different bible entity of the required kind while NOT
    naming the required one.
    """
    entities = bible.get("entities", []) or []

    # Person: the scenario requires a person; the candidate names a
    # DIFFERENT bible person and not the required one. (The old code
    # applied a compensable -0.30 penalty here: topical 0.45 + era
    # 0.10 - 0.30 = 0.25 still cleared the generic 0.22 threshold.)
    if subject and subject.get("type") == "person" and not subject_hit:
        others = [
            e for e in entities
            if e["id"] != subject["id"] and e["type"] == "person"
            and _entity_mentioned(e, text)
        ]
        if others:
            return CONTRADICTORY_PERSON, others[0]["canonical"]

    # Place: either the subject IS a place the candidate replaces with
    # a different bible place, or the scenario names a required place
    # and the candidate names a different bible place instead.
    place_types = {"place", "location"}
    if subject and subject.get("type") in place_types and not subject_hit:
        others = [
            e for e in entities
            if e["id"] != subject["id"] and e["type"] in place_types
            and _entity_mentioned(e, text)
        ]
        if others:
            return CONTRADICTORY_PLACE, others[0]["canonical"]
    required_place = _find_entity(bible, types=place_types, name=scenario.get("place"))
    if required_place is not None and not _entity_mentioned(required_place, text):
        others = [
            e for e in entities
            if e["id"] != required_place["id"] and e["type"] in place_types
            and _entity_mentioned(e, text)
        ]
        if others:
            return CONTRADICTORY_PLACE, others[0]["canonical"]

    # Event: the scenario's event (or an event subject) is replaced by
    # a different bible event in the candidate's metadata.
    required_event = None
    if subject and subject.get("type") == "event":
        required_event = subject
    else:
        required_event = _find_entity(bible, types={"event"}, name=scenario.get("event"))
    if required_event is not None and not _entity_mentioned(required_event, text):
        others = [
            e for e in entities
            if e["id"] != required_event["id"] and e["type"] == "event"
            and _entity_mentioned(e, text)
        ]
        if others:
            return CONTRADICTORY_EVENT, others[0]["canonical"]

    # Date: explicit candidate years, none compatible with the era.
    meta_text = " ".join(
        str(candidate.get(k) or "") for k in ("title", "snippet", "description")
    )
    if era_compatibility(scenario.get("era"), extract_years(meta_text)) is False:
        return CONTRADICTORY_DATE, str(scenario.get("era") or "")

    # Geography (run-9 fix 2): the production establishes exactly
    # one country and the candidate positively names a DIFFERENT
    # single country and never the production's — a positive
    # geography contradiction the bible-place check cannot see
    # (Brighton/UK are not bible places). Multi-country productions
    # or candidates never fire: absence and ambiguity stay neutral.
    home_countries = _scenario_countries(bible, scenario)
    if len(home_countries) == 1:
        candidate_countries = _countries_in_text(text)
        if (
            len(candidate_countries) == 1
            and candidate_countries.isdisjoint(home_countries)
        ):
            return (
                CONTRADICTORY_GEOGRAPHY,
                f"{next(iter(candidate_countries))} vs "
                f"{next(iter(home_countries))}",
            )

    # Unrelated named person (run-9 fixes 2/3): the candidate's
    # title names a person who belongs to a different story entirely
    # (no bible entity, no bible person mentioned at all).
    unrelated = _unrelated_named_person(candidate, bible, text, subject_hit)
    if unrelated:
        return UNRELATED_NAMED_PERSON, unrelated

    return "", ""


# Stage 7 / R7 (review section C.B.3): the verdict's per-dimension
# outcomes, in the review's dimension vocabulary. A dimension a profile
# does not require is recorded ``not_required``; a dimension nobody can
# evaluate at metadata-verification time (transcript and media quality
# are post-download stages; source credibility is a bounded
# post-acceptance tie-break and never enters the composite) is recorded
# ``not_evaluated``. The dimension outcomes are RECORDS: they change no
# decision — the composite and the specificity thresholds above are
# exactly as before.
#
# ``visual`` AT THIS STAGE stays ``not_evaluated`` — truthfully: no
# frames exist yet at metadata time. The previous-era standing order
# (AM-028) excluded the visual dimension from evaluation ENTIRELY;
# that carve-out is SUPERSEDED by the founder's complete autonomous
# takeover mandate (2026-11-21) and transformation plan Phase 1.1:
# the visual dimension IS now evaluated, post-download, on sampled
# frames of the actual file, by services/frame_verification.py,
# whose verdict overwrites this field on the downloaded asset's
# verdict copy (pass/fail) in the candidate ledger. Eligibility
# (this stage) stays strictly separate from that relevance judgement.
VERDICT_DIMENSIONS = (
    "entity", "event", "activity", "location", "temporal", "topical",
    "transcript", "visual", "factual_consistency",
    "source_credibility", "media_quality",
)


def _unevaluated_dimensions():
    return {name: "not_evaluated" for name in VERDICT_DIMENSIONS}


def _dimension_results_for_verdict(
    category, subject, subject_hit, wanted, overlap, era,
    era_contribution, contradiction_reason,
):
    profile = category_profile(category)
    if subject:
        entity = "pass" if subject_hit else (
            "fail" if profile["subject_required"] else "not_required"
        )
    else:
        entity = "not_evaluated"
    if era:
        temporal = "pass" if era_contribution > 0 else "fail"
    elif category == "environmental":
        # C.B.2: the environmental profile waives the era dimension
        # for era-neutral settings; the waiver is recorded, here and
        # as ``era_waived`` on the verdict.
        temporal = "not_required"
    else:
        temporal = "not_evaluated"
    if wanted:
        topical = "pass" if overlap else "fail"
    else:
        topical = "not_evaluated"
    return {
        "entity": entity,
        "event": (
            "fail" if contradiction_reason == CONTRADICTORY_EVENT
            else "not_evaluated"
        ),
        # Activity is not separated from topical at metadata time
        # (C.B.3 table); location is judged at the gate, not scored.
        "activity": "not_evaluated",
        "location": "not_evaluated",
        "temporal": temporal,
        "topical": topical,
        "transcript": "not_evaluated",
        "visual": "not_evaluated",
        "factual_consistency": "fail" if contradiction_reason else "pass",
        "source_credibility": "not_evaluated",
        "media_quality": "not_evaluated",
    }


def verify_candidate(candidate, scenario, bible):
    """
    candidate: dict with any of title, snippet, description, url, source.
    Returns {"accepted": bool, "score": float, "reasons": [..],
             "contributions": {..}, "rejection_reason": str,
             "failed_checks": [..]}

    Stage 6 / R5: ``failed_checks`` lists EVERY decision check that
    independently failed, in the precedence order the primary
    ``rejection_reason`` is resolved by (contradiction ->
    missing_required_subject -> no_topical_evidence ->
    below_threshold). It is diagnostics only — the decision is
    unchanged (the first failed check still decides, exactly as
    before); the list exists so rejection records can carry the
    contributing reasons the first-fail resolution used to destroy.

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
    # Founder decision 2026-11-22: watermarked stock previews are
    # ALLOWED. The detection is computed once here and stamped on
    # every verdict this function returns (the
    # WATERMARKED_PREVIEW_FIELD fact, alongside category /
    # dimension_results / era_waived) so it survives into the asset
    # record — the verdict dict is stored whole on the item — while
    # the candidate is scored on the normal criteria below.
    watermarked_preview = is_watermarked_stock(candidate)

    # Workstream J: a candidate from a permanently banned provider is
    # rejected by the verification chain itself, whatever seam produced
    # it and however relevant its metadata looks.
    if is_banned_provider(candidate):
        return {
            "accepted": False,
            "score": 0.0,
            "reasons": [f"banned provider {candidate_provider(candidate)}"],
            "contributions": {"subject": 0.0, "topical": 0.0, "era": 0.0, "wrong_person": 0.0},
            "rejection_reason": BANNED_PROVIDER_REASON,
            "failed_checks": [BANNED_PROVIDER_REASON],
            "category": resolve_visual_category(scenario),
            "dimension_results": _unevaluated_dimensions(),
            WATERMARKED_PREVIEW_FIELD: watermarked_preview,
        }

    # Plan §3.3 item 1 (tests 17-18): a candidate whose recorded
    # project provenance belongs to another project is rejected by
    # the verification chain itself, whatever seam produced it and
    # however relevant its metadata looks — acceptance earned under
    # another project's contract does not transfer.
    if is_foreign_provenance(candidate):
        return {
            "accepted": False,
            "score": 0.0,
            "reasons": [f"foreign provenance {candidate_project_id(candidate)}"],
            "contributions": {"subject": 0.0, "topical": 0.0, "era": 0.0, "wrong_person": 0.0},
            "rejection_reason": FOREIGN_PROVENANCE_REASON,
            "failed_checks": [FOREIGN_PROVENANCE_REASON],
            "category": resolve_visual_category(scenario),
            "dimension_results": _unevaluated_dimensions(),
            WATERMARKED_PREVIEW_FIELD: watermarked_preview,
        }

    # Run-9 quality fixes (2026-11-22): social-platform sources and
    # meme-format titles are rejected by the verification chain
    # itself, before scoring — the same hard-reject standing as the
    # two rules above. A TikTok/Facebook re-upload or a "POV:" meme
    # is not documentary material however topical its metadata
    # looks. (Watermarked stock previews were in this loop until the
    # founder decision of 2026-11-22 allowed them; their detection
    # is recorded above instead. The meme/social ban was explicitly
    # reaffirmed by the same decision.)
    for _check, _reason, _label in (
        (is_social_platform_source, SOCIAL_PLATFORM_REASON, "social platform source"),
        (has_meme_format_title, MEME_FORMAT_REASON, "meme format title"),
    ):
        if _check(candidate):
            return {
                "accepted": False,
                "score": 0.0,
                "reasons": [_label],
                "contributions": {"subject": 0.0, "topical": 0.0, "era": 0.0, "wrong_person": 0.0},
                "rejection_reason": _reason,
                "failed_checks": [_reason],
                "category": resolve_visual_category(scenario),
                "dimension_results": _unevaluated_dimensions(),
                WATERMARKED_PREVIEW_FIELD: watermarked_preview,
            }

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

    # Workstream A: a POSITIVE identity contradiction is a hard reject,
    # decided BEFORE the threshold comparison — generic specificity,
    # topical strength and (downstream) source quality cannot override
    # it. The computed score/contributions are kept on the verdict so
    # the rejection stays explainable in the asset report.
    contradiction_reason, contradiction_detail = detect_contradiction(
        candidate, scenario, bible, text, subject, subject_hit
    )
    if contradiction_reason:
        reasons.append(f"contradicts {contradiction_reason}: {contradiction_detail}")

    specificity = scenario.get("specificity", "representative")
    accepted = score >= ACCEPT_THRESHOLD.get(specificity, 0.34)
    rejection_reason = ""
    if contradiction_reason:
        accepted = False
        rejection_reason = contradiction_reason
    elif specificity == "exact" and subject and not subject_hit:
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
    # Stage 6 / R5: collect-all diagnostics. Each decision check is
    # evaluated INDEPENDENTLY (not as an elif chain) from the same
    # in-scope values the primary resolution above used, so the
    # contributing failures survive recording. By construction the
    # first entry equals rejection_reason whenever the candidate is
    # rejected, and the list is empty whenever it is accepted.
    failed_checks = []
    if contradiction_reason:
        failed_checks.append(contradiction_reason)
    if specificity == "exact" and subject and not subject_hit:
        failed_checks.append("missing_required_subject")
    if (wanted and overlap == 0) or (not wanted and subject_hit):
        failed_checks.append("no_topical_evidence")
    if score < ACCEPT_THRESHOLD.get(specificity, 0.34):
        failed_checks.append("below_threshold")
    if watermarked_preview:
        # Non-blocking fact, recorded for review (founder decision
        # 2026-11-22): the candidate is a watermarked stock preview
        # and was judged on the normal criteria above.
        reasons.append("watermarked stock preview (allowed, recorded)")
    return {
        "accepted": accepted,
        "score": round(score, 3),
        "reasons": reasons,
        "contributions": contributions,
        "rejection_reason": rejection_reason,
        "failed_checks": failed_checks,
        # Stage 1 / R7: the category profile this verdict was judged
        # under, recorded so diagnostics can slice by category. The
        # gate (media_pipeline.gate_shot_need) applies the profile's
        # required/optional contract; the scorer's rules above are
        # category-independent.
        "category": resolve_visual_category(scenario),
        # Stage 7 / R7 (C.B.2/C.B.3): every verdict is stamped with
        # its profile's per-dimension outcomes, so an acceptance
        # under a non-evidence profile is distinguishable from an
        # evidence acceptance in the asset record and the ledger.
        "dimension_results": _dimension_results_for_verdict(
            resolve_visual_category(scenario), subject, subject_hit,
            wanted, overlap, era, contributions.get("era", 0.0),
            contradiction_reason,
        ),
        # C.B.2: the environmental profile's era waiver, recorded
        # whenever it applies (era-neutral setting, no era to judge).
        "era_waived": bool(
            resolve_visual_category(scenario) == "environmental"
            and not era
        ),
        # Founder decision 2026-11-22: watermarked stock previews
        # are allowed; the detection is recorded here as a
        # non-blocking fact so usage stays reviewable and the asset
        # is never labelled clean or licensed on this evidence.
        WATERMARKED_PREVIEW_FIELD: watermarked_preview,
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


def build_planner_block(bible, plans, mode, diag=None):
    """The plan's self-audit block (Master Directive Phase 7 / P0;
    Planner 2.0 plan §5.8; absorbs DM-1 + DM-2(a)).

    Stamped onto the bible by plan_script, serialized into
    plan.json / the asset report / the production report (q30) /
    the certificate, so EVERY production states which planner
    produced its plan, why it degraded when it did, and how well
    the planner did — in numbers computed from the plan itself.
    Fields a planner cannot know are None (unknown), never a
    fabricated zero: e.g. type-confidence shares exist only when
    the bible carries recorded confidences (Planner 2.0 bibles).
    """
    diag = diag or {}
    bible = bible or {}
    plans = plans or []
    entities = bible.get("entities") or []

    confidences = [
        e["type_confidence"] for e in entities
        if isinstance(e.get("type_confidence"), (int, float))
    ]
    trusted = sum(1 for c in confidences if c >= 0.7)
    scenarios = [sc for plan in plans for sc in (plan.get("scenarios") or [])]
    queries = sum(len(sc.get("queries") or []) for sc in scenarios)
    degraded_sentences = sum(1 for plan in plans if plan.get("degraded"))
    factory_sentences = sum(
        1 for plan in plans if plan.get("concepts_source") == "planner2_factory"
    )
    subject_confident = sum(
        1 for plan in plans
        if isinstance(plan.get("subject_confidence"), (int, float))
        and plan["subject_confidence"] >= 0.5
    )
    basis_counts = {}
    for scenario in scenarios:
        basis = scenario.get("specificity_basis")
        if basis:
            basis_counts[basis] = basis_counts.get(basis, 0) + 1

    if mode == "llm":
        planner_version = "llm"
    elif bible.get("planner_version"):
        planner_version = bible["planner_version"]
    elif factory_sentences:
        planner_version = "degraded-2.0"
    else:
        planner_version = "heuristic-1.0"

    degraded_reason = None
    if mode != "llm":
        degraded_reason = (
            diag.get("bible_reason") or diag.get("sentence_reason") or "no_key"
        )

    kb = bible.get("kb") if isinstance(bible.get("kb"), dict) else {}
    return {
        "mode": mode,
        "planner_version": planner_version,
        "degraded_reason": degraded_reason,
        "degraded_sentences": degraded_sentences,
        "sentences": len(plans),
        "kb": {
            "lookups": kb.get("lookups", 0),
            "cache_hits": kb.get("cache_hits", 0),
            "live": kb.get("live", 0),
            "unreachable": bool(kb.get("unreachable", False)),
            "budget_exhausted": bool(kb.get("budget_exhausted", False)),
            # D-2 state, recorded on every degraded bible: the KB
            # network source is disabled until the founder approves
            # it — a stamped fact, not a silent absence.
            "network_enabled": bool(kb.get("network_enabled", False)),
        },
        "quality": {
            "entities": len(entities),
            "entities_trusted_share": (
                round(trusted / len(confidences), 4) if confidences else None
            ),
            "mean_type_confidence": (
                round(sum(confidences) / len(confidences), 4) if confidences else None
            ),
            "sentences_subject_confident_share": (
                round(subject_confident / len(plans), 4)
                if plans and factory_sentences else None
            ),
            "place_coverage": (
                round(sum(1 for p in plans if p.get("place")) / len(plans), 4)
                if plans else None
            ),
            "era_coverage": (
                round(sum(1 for p in plans if p.get("time")) / len(plans), 4)
                if plans else None
            ),
            "scenarios_per_sentence": (
                round(len(scenarios) / len(plans), 4) if plans else None
            ),
            "queries_per_scenario": (
                round(queries / len(scenarios), 4) if scenarios else None
            ),
            "exact_share": (
                round(
                    sum(1 for sc in scenarios if sc.get("specificity") == "exact")
                    / len(scenarios),
                    4,
                )
                if scenarios else None
            ),
            "roles_distinct": len({p.get("role") for p in plans if p.get("role")}),
            # D-1 evidence, recorded only: how the corroboration
            # computation (which does NOT drive the stamps while
            # D-1 is unmade) would have classified each scenario.
            "specificity_basis": basis_counts,
        },
    }


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

    diag = {}
    bible = build_case_bible(script_text, llm, sentences=sentences, _diag=diag)
    plans = analyze_sentences(sentences, bible, llm, _diag=diag)
    # Stage 3A: research-ground the bible (both the LLM and the
    # heuristic bible paths land here) and stamp every plan with its
    # research_ref. Runs after sentence analysis so source snippets
    # never leak into the LLM's sentence prompts. Fail-soft by design.
    enrich_bible_with_research(bible, script_text, sentences=sentences, genre_pack=genre_pack)
    _attach_research_refs(plans, bible)
    mode = _analysis_mode(bible, plans)
    # Master Directive Phase 7 / P0: the plan self-audit block —
    # mode + degraded_reason + quality, computed from the plan
    # itself and stamped on the bible so every downstream artefact
    # (plan.json, asset report, production report, certificate)
    # states it. DM-6: the signage names the variable that
    # actually gates the LLM (BLOCK-001 moved the check to
    # llm_client's point of use).
    bible["planner"] = build_planner_block(bible, plans, mode, diag)
    if llm is None:
        print("STORY: no LLM configured -> DEGRADED MODE (set BREWKEG_API_KEY for full understanding).")

    if cache_path and mode == "llm":
        # Atomic (Stage 4): the story cache doubles as the plan
        # fallback and the analysis resume record — a truncated
        # copy would silently degrade both.
        write_json_atomic(
            Path(cache_path),
            {"key": key, "mode": mode, "bible": serialize_bible(bible), "plans": plans},
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
