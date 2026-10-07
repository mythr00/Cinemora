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
from pathlib import Path

SENTENCE_ROLES = [
    "hook", "setup", "background", "action", "event", "evidence",
    "motivation", "reflection", "legal", "aftermath", "transition", "conclusion",
]
SCENARIO_KINDS = [
    "person_activity", "place", "document", "event", "object",
    "archival", "abstract_broll",
]
SPECIFICITY = ["exact", "representative", "generic"]

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


def _call_llm_json(llm, system, user, retries=1):
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
    data["mode"] = "llm" if used_llm else "heuristic"
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
        cleaned = _normalize_scenario(scenario, raw, by_id, subject_id)
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
        scenarios.append({
            "visual": f"archival material about {subject_entity['canonical']}" if subject_entity else "neutral documentary b-roll",
            "kind": "archival" if subject_entity else "abstract_broll",
            "subject_id": subject_id,
            "specificity": "exact" if subject_entity else "generic",
            "search_terms": [],
            "event": None,
            "place": (place_entity["canonical"] if place_entity else raw.get("place")),
            "era": _year_or_decade(sentence) or None,
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


def _normalize_scenario(scenario, sentence_raw, by_id, sentence_subject):
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

    return {
        "visual": visual,
        "kind": kind,
        "subject_id": subject_id,
        "specificity": specificity,
        "search_terms": terms[:4],
        "event": scenario.get("event") or None,
        "place": scenario.get("place") or sentence_raw.get("place") or None,
        "era": scenario.get("era") or sentence_raw.get("time") or None,
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
    Returns {"accepted": bool, "score": float, "reasons": [..]}
    """
    text = _norm(" ".join(str(candidate.get(k) or "") for k in ("title", "snippet", "description", "url", "source")))
    by_id = bible.get("by_id", {})
    reasons = []
    score = 0.0

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
            reasons.append(f"mentions {subject['canonical']}")
        elif scenario.get("specificity") == "exact":
            reasons.append(f"missing required subject {subject['canonical']}")

    wanted = set()
    for term in scenario.get("search_terms") or []:
        wanted |= _content_tokens(term)
    wanted |= _content_tokens(scenario.get("visual"))
    if wanted:
        overlap = len(wanted & {_stem(t) for t in text.split()}) / len(wanted)
        score += 0.45 * min(1.0, overlap * 1.5)
        reasons.append(f"visual overlap {overlap:.2f}")

    era = _year_or_decade(scenario.get("era"))
    if era and era[:4] in text:
        score += 0.10
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
            reasons.append(f"names other person {others[0]['canonical']}")

    specificity = scenario.get("specificity", "representative")
    accepted = score >= ACCEPT_THRESHOLD.get(specificity, 0.34)
    if specificity == "exact" and subject and not subject_hit:
        accepted = False
    return {"accepted": accepted, "score": round(score, 3), "reasons": reasons}


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


def plan_script(script_text, sentences, llm, cache_path=None):
    """
    Returns (bible, plans, mode). mode is "llm" or "heuristic".
    Only real LLM results are cached, so a later run with a working LLM is
    never poisoned by an earlier degraded run.
    """
    key = hashlib.sha256((script_text + "\n" + "\n".join(sentences)).encode("utf-8")).hexdigest()[:16]

    if llm is not None and cache_path and Path(cache_path).exists():
        try:
            cached = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            if cached.get("key") == key and cached.get("mode") == "llm":
                print("STORY PLAN: loaded from cache.")
                return hydrate_bible(cached["bible"]), cached["plans"], "llm"
        except Exception:
            pass

    bible = build_case_bible(script_text, llm, sentences=sentences)
    plans = analyze_sentences(sentences, bible, llm)
    mode = bible.get("mode", "heuristic") if llm is not None else "heuristic"
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