    def verify_candidate(candidate, scenario, bible):
        text = _norm(
            " ".join(
                str(candidate.get(key) or "")
                for key in (
                    "title",
                    "snippet",
                    "description",
                    "url",
                    "source",
                )
            )
        )

        bible = bible or {}
        by_id = bible.get("by_id", {})
        reasons = []
        score = 0.0

        scenario = scenario or {}

        subject = by_id.get(
            scenario.get("subject_id")
        )

        scenario_subject = str(
            scenario.get("subject")
            or ""
        ).strip()

        subject_hit = False
        place_hit = False
        event_hit = False

        # ---------------------------------------------------------
        # REQUIRED SUBJECT
        # ---------------------------------------------------------
        if subject:
            aliases = {
                subject["canonical"],
                *subject.get("aliases", []),
            }

            if subject["type"] == "person":
                parts = subject["canonical"].split()

                if len(parts) >= 2:
                    aliases.add(parts[-1])

            subject_hit = any(
                _norm(alias) and _norm(alias) in text
                for alias in aliases
            )

            if subject_hit:
                score += 0.45
                reasons.append(
                    f"mentions {subject['canonical']}"
                )
            else:
                reasons.append(
                    f"missing required subject {subject['canonical']}"
                )

        elif scenario_subject:
            subject_parts = [
                _norm(part)
                for part in scenario_subject.split()
                if len(part) > 2
            ]

            subject_norm = _norm(scenario_subject)

            subject_hit = (
                subject_norm in text
                or (
                    bool(subject_parts)
                    and all(
                        part in text
                        for part in subject_parts
                    )
                )
            )

            if subject_hit:
                score += 0.45
                reasons.append(
                    f"mentions scenario subject {scenario_subject}"
                )
            else:
                reasons.append(
                    f"missing scenario subject {scenario_subject}"
                )

        # ---------------------------------------------------------
        # REQUIRED LOCATION
        # ---------------------------------------------------------
        place = str(
            scenario.get("place")
            or scenario.get("location")
            or ""
        ).strip()

        if place:
            place_norm = _norm(place)

            place_parts = [
                _norm(part)
                for part in place.split()
                if len(part) > 2
            ]

            place_hit = (
                place_norm in text
                or (
                    bool(place_parts)
                    and all(
                        part in text
                        for part in place_parts
                    )
                )
            )

            if place_hit:
                score += 0.20
                reasons.append(
                    f"mentions location {place}"
                )
            else:
                reasons.append(
                    f"missing required location {place}"
                )

        # ---------------------------------------------------------
        # EVENT
        # ---------------------------------------------------------
        event = str(
            scenario.get("event")
            or ""
        ).strip()

        if event:
            event_tokens = [
                _norm(token)
                for token in event.split()
                if len(token) > 2
            ]

            event_hit = (
                _norm(event) in text
                or (
                    bool(event_tokens)
                    and all(
                        token in text
                        for token in event_tokens
                    )
                )
            )

            if event_hit:
                score += 0.20
                reasons.append(
                    f"mentions event {event}"
                )
            else:
                reasons.append(
                    f"missing event {event}"
                )

        # ---------------------------------------------------------
        # SEARCH / VISUAL OVERLAP
        #
        # Prefer structured evidence terms.
        # Fall back to the complete visual description only when
        # no structured terms exist.
        # ---------------------------------------------------------
        wanted = set()

        for term in scenario.get("search_terms") or []:
            wanted |= _content_tokens(term)

        structured_terms = []

        for key in (
            "subject",
            "event",
            "place",
            "location",
        ):
            value = str(
                scenario.get(key)
                or ""
            ).strip()

            if value:
                structured_terms.append(value)

        if structured_terms:
            for term in structured_terms:
                wanted |= _content_tokens(term)
        else:
            wanted |= _content_tokens(
                scenario.get("visual")
            )

        if wanted:
            candidate_tokens = {
                _stem(token)
                for token in _tokens(text)
            }

            overlap = (
                len(wanted & candidate_tokens)
                / len(wanted)
            )

            score += 0.35 * min(
                1.0,
                overlap * 1.5,
            )

            reasons.append(
                f"visual overlap {overlap:.2f}"
            )

        # ---------------------------------------------------------
        # ERA
        # ---------------------------------------------------------
        era = _year_or_decade(
            scenario.get("era")
        )

        if era and era[:4] in text:
            score += 0.10
            reasons.append("era match")

        # ---------------------------------------------------------
        # WRONG PERSON PENALTY
        # ---------------------------------------------------------
        if subject and not subject_hit:
            others = []

            for entity in bible.get("entities", []):
                if entity["id"] == subject["id"]:
                    continue

                if entity["type"] != "person":
                    continue

                if _norm(entity["canonical"]) in text:
                    others.append(entity)

            if others:
                score -= 0.30
                reasons.append(
                    "names another bible person"
                )

        # ---------------------------------------------------------
        # SHOT TYPE / SPECIFICITY
        # ---------------------------------------------------------
        shot_type = str(
            scenario.get("shot_type")
            or scenario.get("kind")
            or ""
        ).lower()

        specificity = str(
            scenario.get("specificity")
            or "representative"
        ).lower()

        # ---------------------------------------------------------
        # PERSON-SPECIFIC FOOTAGE
        # ---------------------------------------------------------
        if subject and subject.get("type") == "person":
            if not subject_hit:
                return {
                    "accepted": False,
                    "score": round(max(0.0, score), 3),
                    "reasons": reasons + [
                        "person verification failed"
                    ],
                }

        # ---------------------------------------------------------
        # LOCATION-SPECIFIC FOOTAGE
        # ---------------------------------------------------------
        if place and shot_type in (
            "location",
            "place",
            "person_activity",
            "event",
            "action",
            "archival",
        ):
            if not place_hit and specificity in (
                "exact",
                "representative",
            ):
                return {
                    "accepted": False,
                    "score": round(max(0.0, score), 3),
                    "reasons": reasons + [
                        "location verification failed"
                    ],
                }

        # ---------------------------------------------------------
        # EXACT CONTRACTS
        # ---------------------------------------------------------
        if specificity == "exact":
            if (
                (subject or scenario_subject)
                and not subject_hit
            ):
                return {
                    "accepted": False,
                    "score": round(max(0.0, score), 3),
                    "reasons": reasons + [
                        "exact subject verification failed"
                    ],
                }

            if place and not place_hit:
                return {
                    "accepted": False,
                    "score": round(max(0.0, score), 3),
                    "reasons": reasons + [
                        "exact location verification failed"
                    ],
                }

            if event and not event_hit:
                return {
                    "accepted": False,
                    "score": round(max(0.0, score), 3),
                    "reasons": reasons + [
                        "exact event verification failed"
                    ],
                }

        # ---------------------------------------------------------
        # FINAL SCORE
        # ---------------------------------------------------------
        threshold = ACCEPT_THRESHOLD.get(
            specificity,
            0.50,
        )

        accepted = score >= threshold

        return {
            "accepted": accepted,
            "score": round(max(0.0, score), 3),
            "reasons": reasons,
        }


"""
Universal story-understanding layer for the documentary pipeline.

SCRIPT
  -> CASE BIBLE
  -> SENTENCE MEANING / ROLE
  -> VISUAL SCENARIOS
  -> SEARCH INTENT
  -> SEARCH QUERIES
  -> CANDIDATE VERIFICATION

Design rules
------------
1. The LLM does the actual semantic understanding whenever available.
2. Code enforces structure and prevents hallucinated entities/queries.
3. The fallback mode is UNIVERSAL and must not contain story-specific
   names, places, organizations, crimes, companies, historical figures,
   or niche-specific blacklists.
4. The fallback may use structural language signals, but it must not
   assume that a particular documentary is about crime, business,
   history, war, etc.
5. Queries are built from structured fields, never raw sentence fragments.
6. Entity aliases belong to the same real-world entity.
7. Pronouns are resolved conservatively.
"""

import hashlib
import json
import re
from pathlib import Path


SENTENCE_ROLES = [
    "hook",
    "setup",
    "background",
    "action",
    "event",
    "evidence",
    "motivation",
    "reflection",
    "legal",
    "aftermath",
    "transition",
    "conclusion",
]

SCENARIO_KINDS = [
    "person_activity",
    "place",
    "document",
    "event",
    "object",
    "archival",
    "abstract_broll",
]

SPECIFICITY = [
    "exact",
    "representative",
    "generic",
]

PRONOUNS = {
    "he",
    "him",
    "his",
    "she",
    "her",
    "hers",
    "they",
    "them",
    "their",
    "theirs",
    "it",
    "its",
    "we",
    "us",
    "our",
    "ours",
    "you",
    "your",
    "yours",
}


def parse_json_loose(text):
    if text is None:
        return None

    text = str(text).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text, flags=re.IGNORECASE)

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
    text = str(text or "")
    text = re.sub(r"[^\w\s'-]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text)
    return text.lower().strip()


def _tokens(text):
    return re.findall(r"[\w'-]+", str(text or ""), flags=re.UNICODE)


def _stem(word):
    word = str(word or "").lower()
    if len(word) > 5 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 5 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _content_tokens(text, min_len=4):
    return {
        _stem(token)
        for token in _norm(text).split()
        if len(token) >= min_len
    }


def _call_llm_json(llm, system, user, retries=1):
    if llm is None:
        return None
    for _ in range(retries + 1):
        try:
            result = llm(system, user)
            data = parse_json_loose(result)
            if data is not None:
                return data
        except Exception as error:
            print(f"STORY LLM ERROR: {error}")
    return None


def _clean_text(value):
    value = str(value or "").strip()
    value = value.strip("\"'â€œâ€â€˜â€™.,!?;:()[]{}")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _canonical_without_possessive(value):
    value = _clean_text(value)
    value = re.sub(r"(?:'s|â€™s)$", "", value, flags=re.IGNORECASE)
    return value.strip()


def _string_list(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


BIBLE_SYSTEM = """You are a documentary researcher.

Read the ENTIRE script and build a CASE BIBLE containing the real-world
entities and context needed to understand every sentence.

Return ONLY valid JSON:

{
  "title": "short title",
  "story_type": "true_crime | history | biography | business | finance | disaster | military | science | technology | sports | culture | politics | other",
  "time_period": "overall era or empty string",
  "story_era": "same as time_period or a more specific era",
  "tone": "short tone label supported by the script, or empty string",
  "banned_terms": ["terms that would pull the wrong search results"],
  "banned_visuals": ["visuals that would be anachronistic or off-story"],
  "primary_subject_id": "entity id or null",
  "entities": [
    {
      "id": "short_snake_case_id",
      "canonical": "canonical name exactly as supported by the script",
      "type": "person | place | organization | event | object",
      "role": "role in this story or empty string",
      "aliases": ["other names referring to THE SAME ENTITY"],
      "pronoun": "he | she | they | it | null",
      "description": "short factual description supported by the script",
      "disambiguation": "who/what this is AND is not, enough to search correctly",
      "date_range": "date or range supported by the script, or empty string",
      "location": "place this entity belongs to, or empty string"
    }
  ],
  "timeline": [
    {
      "when": "date or period",
      "what": "event"
    }
  ]
}

STRICT RULES:

- Understand the story before extracting entities.
- Only include real entities that materially matter to the story.
- Never turn ordinary words into entities.
- Never turn sentence openers into entities.
- Never turn adjectives, occupations, generic groups, activities,
  concepts, or descriptions into people.
- A person's full name and surname/first-name references are ONE entity.
- A possessive form such as "John Smith's" is the same entity as
  "John Smith".
- A title or occupation is not automatically a person.
- A location is a place.
- A company, institution, agency, team, church, university, newspaper,
  military branch, etc. is an organization when the script establishes it
  as such.
- Objects, technologies, documents, vehicles, weapons, products,
  machines, etc. can be objects when they are actual story entities.
- Do not invent entities.
- Do not infer facts that are not supported by the script.
- Do not create an entity merely because a phrase is capitalized.
- Resolve aliases into one entity.
- Use the full script context when determining entity type and role.
- disambiguation must distinguish this entity from same-named others.
- date_range and location must be supported by the script or empty.
- banned_visuals / banned_terms only from THIS script, not from a niche stereotype.
"""


def build_case_bible(script_text, llm, sentences=None, max_chars=24000):
    text = str(script_text or "")

    if len(text) > max_chars:
        third = max_chars // 3
        middle = len(text) // 2
        text = (
            text[:third]
            + "\n[...]\n"
            + text[middle - third // 2:middle + third // 2]
            + "\n[...]\n"
            + text[-third:]
        )

    data = _call_llm_json(llm, BIBLE_SYSTEM, f"SCRIPT:\n{text}")
    used_llm = True

    if not isinstance(data, dict):
        print("STORY: LLM case bible unavailable -> structural fallback.")
        used_llm = False
        fallback_sentences = sentences or [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+", str(script_text or ""))
            if part.strip()
        ]
        data = heuristic_bible(fallback_sentences)
    elif not isinstance(data.get("entities"), list):
        data["entities"] = []

    cleaned = []
    seen_ids = set()

    for entity in data.get("entities", []):
        if not isinstance(entity, dict):
            continue

        canonical = _canonical_without_possessive(entity.get("canonical"))
        if not canonical:
            continue

        eid = str(entity.get("id") or _norm(canonical).replace(" ", "_")).strip()
        if not eid or eid in seen_ids:
            continue

        entity_type = str(entity.get("type") or "object").lower().strip()
        if entity_type not in {"person", "place", "organization", "event", "object"}:
            entity_type = "object"

        aliases = []
        for alias in entity.get("aliases", []) or []:
            alias = _canonical_without_possessive(alias)
            if alias and alias.lower() != canonical.lower() and alias not in aliases:
                aliases.append(alias)

        description = str(entity.get("description") or "").strip()
        cleaned.append({
            "id": eid,
            "canonical": canonical,
            "type": entity_type,
            "role": str(entity.get("role") or "").strip(),
            "aliases": aliases,
            "pronoun": entity.get("pronoun") or None,
            "description": description,
            "disambiguation": str(entity.get("disambiguation") or description or "").strip(),
            "date_range": str(entity.get("date_range") or "").strip(),
            "location": str(entity.get("location") or "").strip(),
        })
        seen_ids.add(eid)

    data["entities"] = cleaned

    if data.get("primary_subject_id") not in seen_ids:
        data["primary_subject_id"] = cleaned[0]["id"] if cleaned else None

    data["story_era"] = str(data.get("story_era") or data.get("time_period") or "").strip()
    data["time_period"] = str(data.get("time_period") or data.get("story_era") or "").strip()
    data["tone"] = str(data.get("tone") or "").strip()
    data["banned_terms"] = _string_list(data.get("banned_terms"))
    data["banned_visuals"] = _string_list(data.get("banned_visuals"))
    data["title"] = str(data.get("title") or "").strip()
    data["story_type"] = str(data.get("story_type") or "other").strip() or "other"
    if not isinstance(data.get("timeline"), list):
        data["timeline"] = []

    data["by_id"] = {entity["id"]: entity for entity in cleaned}
    data["alias_index"] = build_alias_index(cleaned)
    data["mode"] = "llm" if used_llm else "heuristic"
    return data


def build_alias_index(entities):
    index = {}
    ambiguous = set()

    def add(alias, entity_id):
        alias = _canonical_without_possessive(alias)
        key = _norm(alias)
        if len(key) < 3:
            return
        existing = index.get(key)
        if existing and existing != entity_id:
            ambiguous.add(key)
            return
        index[key] = entity_id

    for entity in entities:
        canonical = entity["canonical"]
        names = {canonical, *entity.get("aliases", [])}
        if entity["type"] == "person":
            parts = canonical.split()
            if len(parts) >= 2:
                names.add(parts[0])
                names.add(parts[-1])
        for name in names:
            add(name, entity["id"])

    for key in ambiguous:
        index.pop(key, None)
    return index


def find_mentions(text, alias_index):
    lowered = str(text or "").lower()
    taken = [False] * len(lowered)
    found = []

    for alias in sorted(alias_index, key=len, reverse=True):
        pattern = r"(?<![\w])" + re.escape(alias) + r"(?![\w])"
        for match in re.finditer(pattern, lowered, flags=re.IGNORECASE):
            start, end = match.span()
            if any(taken[start:end]):
                continue
            for position in range(start, end):
                taken[position] = True
            found.append((start, alias_index[alias]))

    found.sort()
    result = []
    for _, entity_id in found:
        if entity_id not in result:
            result.append(entity_id)
    return result


SENTENCE_SYSTEM = """You are the visual director of a documentary.

For each numbered sentence, determine:

- what the sentence actually means
- its narrative role
- which CASE BIBLE entity it concerns
- what should literally appear on screen
- what visual search concepts can find that footage

Return ONLY a JSON list.

Each item:

{
  "idx": 0,
  "meaning": "one-line meaning",
  "role": "hook | setup | background | action | event | evidence | motivation | reflection | legal | aftermath | transition | conclusion",
  "subject_id": "case bible entity id or null",
  "resolved": {
    "he": "entity_id",
    "this house": "entity_id"
  },
  "events": [],
  "time": "date/period or null",
  "place": "place or null",
  "scenarios": [
    {
      "visual": "one concrete shot",
      "kind": "person_activity | place | document | event | object | archival | abstract_broll",
      "subject_id": "entity id or null",
      "specificity": "exact | representative | generic",
      "search_terms": [
        "2-6 word concrete visual concepts"
      ],
      "event": "event or null",
      "place": "place or null",
      "era": "year/decade or null"
    }
  ]
}

RULES:

- Use the CASE BIBLE.
- Never invent an entity.
- Resolve pronouns using the established context.
- A person, place, organization or object mentioned in the sentence
  should be used when it is actually relevant to the shot.
- Do not turn ordinary words into entities.
- Search terms must describe things that can literally be photographed,
  filmed, shown in archival footage, shown in a document, or represented
  visually.
- Search terms must be noun phrases or concrete activity phrases.
- Never output narration.
- Never output a sentence fragment copied from the script.
- Never output instructions such as "show this".
- Never use a generic media word as the only search term when a real
  subject/place/object/event is available.
- A psychological or abstract statement must be translated into visible
  evidence or representative visuals.
- A named person should receive exact visuals only when the script supports
  that person being the subject.
- A representative visual is appropriate when exact footage does not exist.
- Do not invent historical photographs, locations or events.
"""


def _bible_for_prompt(bible):
    return json.dumps(
        {
            "title": bible.get("title"),
            "story_type": bible.get("story_type"),
            "time_period": bible.get("time_period"),
            "story_era": bible.get("story_era"),
            "tone": bible.get("tone"),
            "banned_terms": bible.get("banned_terms"),
            "banned_visuals": bible.get("banned_visuals"),
            "primary_subject_id": bible.get("primary_subject_id"),
            "entities": bible.get("entities"),
            "timeline": bible.get("timeline"),
        },
        ensure_ascii=False,
    )


def analyze_sentences(sentences, bible, llm, batch_size=10, context_size=4):
    plans = [None for _ in sentences]
    carry_subject = bible.get("primary_subject_id")

    for start in range(0, len(sentences), batch_size):
        batch = list(range(start, min(start + batch_size, len(sentences))))
        previous = sentences[max(0, start - context_size):start]
        user = (
            f"CASE BIBLE:\n{_bible_for_prompt(bible)}\n\n"
            f"PREVIOUS SENTENCES:\n"
            + "\n".join(f"- {sentence}" for sentence in previous)
            + "\n\nSENTENCES:\n"
            + "\n".join(f"{index}: {sentences[index]}" for index in batch)
        )
        raw = _call_llm_json(llm, SENTENCE_SYSTEM, user)
        by_idx = {}
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and isinstance(item.get("idx"), int):
                    by_idx[item["idx"]] = item

        for index in batch:
            plan = normalize_plan(by_idx.get(index), sentences[index], bible, carry_subject)
            plans[index] = plan
            new_subject = plan.get("subject_id")
            if new_subject:
                new_entity = bible["by_id"].get(new_subject, {})
                old_entity = bible["by_id"].get(carry_subject, {})
                new_type = new_entity.get("type")
                old_type = old_entity.get("type")
                if new_type == "person" or carry_subject is None or old_type != "person":
                    carry_subject = new_subject

    return plans


def _pick_subject(mentions, by_id, sentence, carry):
    people = [
        entity_id
        for entity_id in mentions
        if entity_id in by_id and by_id[entity_id]["type"] == "person"
    ]
    if people:
        return people[0]

    tokens = {token.lower() for token in _tokens(sentence)}
    direct_pronouns = {"he", "him", "his", "she", "her", "hers", "it", "its"}
    group_pronouns = {"they", "them", "their", "theirs"}

    if carry and tokens.intersection(direct_pronouns):
        return carry
    if carry and tokens.intersection(group_pronouns):
        entity = by_id.get(carry, {})
        if entity.get("type") == "person":
            return carry
    if mentions:
        return mentions[0]
    return None


def _heuristic_sentence_intent(sentence):
    text = str(sentence or "").strip()
    if not text:
        return {
            "visual": "documentary archival material",
            "kind": "archival",
            "specificity": "generic",
            "search_terms": ["documentary archival footage", "historical photographs"],
            "event": "",
        }

    location_match = re.search(
        r"\b(?:in|at|near|inside|outside|around|"
        r"from|through|across|within)\s+"
        r"([A-Z][A-Za-z0-9'â€™-]*(?:\s+[A-Z][A-Za-z0-9'â€™-]*){0,4})",
        text,
    )
    if location_match:
        location = _clean_text(location_match.group(1))
        if len(location.split()) >= 1:
            return {
                "visual": f"{location} location footage",
                "kind": "place",
                "specificity": "representative",
                "search_terms": [f"{location} location", "location exterior", "local archival footage"],
                "event": "",
            }

    if re.search(r'["â€œâ€].+?["â€œâ€]', text):
        return {
            "visual": "document or quoted material",
            "kind": "document",
            "specificity": "representative",
            "search_terms": ["historical document", "archival document", "printed records"],
            "event": "",
        }

    if re.search(r"\b(?:1[5-9]\d{2}|20\d{2})\b", text):
        return {
            "visual": "dated archival material",
            "kind": "archival",
            "specificity": "representative",
            "search_terms": ["archival photographs", "historical documents", "dated newspaper"],
            "event": "",
        }

    runs = capitalized_runs(text)
    named = []
    for run in runs:
        value = " ".join(run["words"]).strip()
        if len(value.split()) >= 2:
            named.append(value)
    if named:
        subject = named[0]
        return {
            "visual": f"{subject} documentary footage",
            "kind": "person_activity",
            "specificity": "representative",
            "search_terms": [f"{subject} footage", f"{subject} archival", "documentary interview"],
            "event": "",
        }

    return {
        "visual": "documentary archival material",
        "kind": "archival",
        "specificity": "generic",
        "search_terms": ["documentary archival footage", "historical photographs", "documentary footage"],
        "event": "",
    }


def normalize_plan(raw, sentence, bible, carry_subject):
    by_id = bible.get("by_id", {})
    alias_index = bible.get("alias_index", {})
    raw = raw if isinstance(raw, dict) else {}
    degraded = not bool(raw)
    mentions = find_mentions(sentence, alias_index)
    subject_id = raw.get("subject_id")

    if subject_id not in by_id:
        subject_id = None
        if not raw:
            subject_id = _pick_subject(mentions, by_id, sentence, carry_subject)
        elif mentions:
            subject_id = mentions[0]
        else:
            tokens = {token.lower() for token in _tokens(sentence)}
            if tokens.intersection(PRONOUNS) and carry_subject:
                subject_id = carry_subject

    role = str(raw.get("role") or "").lower().strip()
    if role not in SENTENCE_ROLES:
        role = "background"

    entity_ids = list(mentions)
    if subject_id and subject_id not in entity_ids:
        entity_ids.insert(0, subject_id)

    if carry_subject and carry_subject not in entity_ids:
        tokens = {token.lower() for token in _tokens(sentence)}
        if tokens.intersection(PRONOUNS):
            carry_entity = by_id.get(carry_subject, {})
            if carry_entity:
                entity_ids.append(carry_subject)

    scenarios = []
    for scenario in (raw.get("scenarios") or [])[:4]:
        cleaned = _normalize_scenario(scenario, raw, by_id, subject_id)
        if cleaned:
            scenarios.append(cleaned)

    if not scenarios:
        intent = _heuristic_sentence_intent(sentence)
        place_entities = [entity for entity in by_id.values() if entity.get("type") == "place"]
        unique_place = place_entities[0]["canonical"] if len(place_entities) == 1 else None
        scenarios.append({
            "visual": intent["visual"],
            "kind": intent["kind"],
            "subject_id": subject_id,
            "specificity": intent["specificity"],
            "search_terms": intent["search_terms"],
            "event": intent["event"] or None,
            "place": unique_place,
            "era": _year_or_decade(sentence) or None,
        })
        degraded = True

    plan = {
        "sentence": sentence,
        "meaning": str(raw.get("meaning") or ""),
        "role": role,
        "subject_id": subject_id,
        "entities": [by_id[entity_id]["canonical"] for entity_id in entity_ids if entity_id in by_id],
        "entity_ids": entity_ids,
        "resolved": raw.get("resolved") if isinstance(raw.get("resolved"), dict) else {},
        "events": [str(event) for event in (raw.get("events") or []) if str(event).strip()],
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
    terms = _normalize_search_terms(scenario.get("search_terms") or [], visual)
    if not visual and not terms:
        return None

    kind = str(scenario.get("kind") or "").lower().strip()
    if kind not in SCENARIO_KINDS:
        kind = "abstract_broll"

    specificity = str(scenario.get("specificity") or "").lower().strip()
    if specificity not in SPECIFICITY:
        specificity = "representative"

    subject_id = scenario.get("subject_id")
    if subject_id not in by_id:
        subject_id = sentence_subject if specificity == "exact" else None
    if specificity == "exact" and subject_id is None:
        specificity = "representative"

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


def _year_or_decade(value):
    match = re.search(r"\b(?:1[5-9]\d{2}|20\d{2})\b", str(value or ""))
    return match.group(0) if match else ""


def _clean_query(parts):
    text = " ".join(str(part).strip() for part in parts if part and str(part).strip())
    text = re.sub(r"\s+", " ", text).strip()
    words = []
    seen = set()
    for word in text.split():
        key = word.lower()
        if key in seen:
            continue
        seen.add(key)
        words.append(word)
    return " ".join(words)


def _search_term_is_visual(term):
    text = re.sub(r"\s+", " ", str(term or "")).strip()
    if not text:
        return False
    tokens = _tokens(text)
    if len(tokens) < 2 or len(tokens) > 8:
        return False
    normalized = _norm(text)
    if re.match(
        r"^(when|while|where|because|although|after|before|as|since|if|and|but|so|"
        r"that|which|who|what|how)\b",
        normalized,
    ):
        return False
    if re.search(
        r"\b(show|shows|showing|shown|tell|tells|telling|explains|explaining|"
        r"means|meaning|according|describes|describing|reveals|revealing)\b",
        normalized,
    ):
        return False
    return bool(_content_tokens(text))


def _normalize_search_terms(raw_terms, visual=""):
    terms = []
    if isinstance(raw_terms, str):
        raw_terms = [raw_terms]
    for raw in raw_terms or []:
        term = re.sub(r"\s+", " ", str(raw or "")).strip(" ,.;:!?")
        if not term or not _search_term_is_visual(term):
            continue
        cleaned = _clean_query([term])
        if cleaned and cleaned.lower() not in {existing.lower() for existing in terms}:
            terms.append(cleaned)
        if len(terms) >= 4:
            break

    if not terms and visual:
        visual_text = re.sub(r"\s+", " ", str(visual)).strip()
        visual_text = re.sub(
            r"^(show|shows|showing|shot of|image of|photo of|footage of|visual of|scene of)\s+",
            "",
            visual_text,
            flags=re.IGNORECASE,
        )
        if _search_term_is_visual(visual_text):
            terms.append(_clean_query([visual_text]))
    return terms[:4]


def _valid_query(query, alias_index):
    tokens = _tokens(query)
    if not tokens:
        return False
    if len(tokens) == 1:
        return _norm(query) in alias_index
    return True


def _query_contains_context(query, context_parts):
    query_norm = _norm(query)
    query_words = set(query_norm.split())
    for part in context_parts:
        value = _norm(part)
        if not value:
            continue
        if value in query_norm:
            return True
        words = [word for word in value.split() if len(word) >= 4]
        if not words:
            continue
        hits = sum(1 for word in words if word in query_words)
        if hits >= max(1, len(words) // 2):
            return True
    return False


def compose_queries(scenario, plan, bible, max_queries=4):
    by_id = bible.get("by_id", {})
    alias_index = bible.get("alias_index", {})
    subject = by_id.get(scenario.get("subject_id"))
    anchor = subject["canonical"] if subject else ""
    terms = _normalize_search_terms(scenario.get("search_terms") or [], scenario.get("visual") or "")
    event = scenario.get("event") or (plan["events"][0] if plan.get("events") else "")
    place = scenario.get("place") or plan.get("place") or ""
    year = _year_or_decade(scenario.get("era") or plan.get("time"))
    specificity = scenario.get("specificity") or "representative"
    candidates = []

    if specificity == "exact":
        if anchor:
            candidates.extend([
                _clean_query([anchor, event, year]),
                _clean_query([anchor, place, year]),
                _clean_query([anchor, event, place]),
            ])
            for term in terms[:2]:
                candidates.append(_clean_query([anchor, term, place, year]))
        elif place:
            for term in terms[:2]:
                candidates.append(_clean_query([term, place, year]))
        else:
            for term in terms[:2]:
                candidates.append(_clean_query([term, event, year]))
    elif specificity == "representative":
        for term in terms[:2]:
            if anchor:
                candidates.append(_clean_query([anchor, term, year]))
                if place:
                    candidates.append(_clean_query([term, place, year]))
            elif place:
                candidates.append(_clean_query([term, place, year]))
            elif event:
                candidates.append(_clean_query([term, event, year]))
            elif year:
                candidates.append(_clean_query([term, year]))
            else:
                candidates.append(_clean_query([term]))
        if anchor and subject and subject["type"] != "person":
            for term in terms[:1]:
                candidates.append(_clean_query([anchor, term, place, year]))
    else:
        for term in terms[:2]:
            if place:
                candidates.append(_clean_query([term, place, year]))
            elif event:
                candidates.append(_clean_query([term, event, year]))
            elif year:
                candidates.append(_clean_query([term, year]))
            else:
                candidates.append(_clean_query([term]))

    queries = []
    context = [anchor, event, place, year]
    for query in candidates:
        query = _clean_query([query])
        if not query or not _valid_query(query, alias_index) or not _search_term_is_visual(query):
            continue
        if any(context) and not _query_contains_context(query, context) and queries:
            continue
        if query.lower() not in {item.lower() for item in queries}:
            queries.append(query)
        if len(queries) >= max_queries:
            break
    return queries[:max_queries]


ACCEPT_THRESHOLD = {
    "exact": 0.55,
    "representative": 0.34,
    "generic": 0.22,
}


def verify_candidate(candidate, scenario, bible):
    text = _norm(
        " ".join(
            str(candidate.get(key) or "")
            for key in (
                "title",
                "snippet",
                "description",
                "url",
                "source",
            )
        )
    )

    by_id = bible.get("by_id", {})
    reasons = []
    score = 0.0

    subject = by_id.get(
        scenario.get("subject_id")
    )

    subject_hit = False
    place_hit = False
    event_hit = False

    # ---------------------------------------------------------
    # REQUIRED SUBJECT
    # ---------------------------------------------------------
    if subject:
        aliases = {
            subject["canonical"],
            *subject.get("aliases", []),
        }

        if subject["type"] == "person":
            parts = subject["canonical"].split()

            if len(parts) >= 2:
                aliases.add(parts[-1])

        subject_hit = any(
            _norm(alias) and _norm(alias) in text
            for alias in aliases
        )

        if subject_hit:
            score += 0.45
            reasons.append(
                f"mentions {subject['canonical']}"
            )
        else:
            reasons.append(
                f"missing required subject {subject['canonical']}"
            )

    # ---------------------------------------------------------
    # REQUIRED LOCATION
    # ---------------------------------------------------------
    place = str(
        scenario.get("place")
        or scenario.get("location")
        or ""
    ).strip()

    if place:
        place_norm = _norm(place)

        # Avoid counting the same entity twice when the subject
        # itself is the required location.
        if (
            subject
            and subject.get("type") == "location"
            and _norm(subject.get("canonical") or "") == place_norm
        ):
            place = ""

        place_aliases = {
            place_norm,
        }

        # Also allow individual meaningful location words for
        # multi-word places, while avoiding tiny words.
        place_parts = [
            _norm(part)
            for part in place.split()
            if len(part) > 2
        ]

        place_hit = (
            place_norm in text
            or (
                bool(place_parts)
                and all(part in text for part in place_parts)
            )
        )

        if place_hit:
            score += 0.20
            reasons.append(
                f"mentions location {place}"
            )
        else:
            reasons.append(
                f"missing required location {place}"
            )

    # ---------------------------------------------------------
    # SEARCH / VISUAL OVERLAP
    # ---------------------------------------------------------

    # ---------------------------------------------------------
    # ERA
    # ---------------------------------------------------------
    era = _year_or_decade(
        scenario.get("era")
    )

    if era and era[:4] in text:
        score += 0.10
        reasons.append("era match")

    # ---------------------------------------------------------
    # WRONG PERSON PENALTY
    # ---------------------------------------------------------
    if subject and not subject_hit:
        others = []

        for entity in bible.get("entities", []):
            if entity["id"] == subject["id"]:
                continue

            if entity["type"] != "person":
                continue

            if _norm(entity["canonical"]) in text:
                others.append(entity)

        if others:
            score -= 0.30
            reasons.append(
                "names another bible person"
            )

    # ---------------------------------------------------------
    # SHOT TYPE
    # ---------------------------------------------------------
    shot_type = str(
        scenario.get("shot_type")
        or scenario.get("kind")
        or ""
    ).lower()

    specificity = str(
        scenario.get("specificity")
        or "representative"
    ).lower()

    # Person-specific footage MUST identify the person.
    if subject and subject.get("type") == "person":
        if not subject_hit:
            return {
                "accepted": False,
                "score": round(max(0.0, score), 3),
                "reasons": reasons + [
                    "person verification failed"
                ],
            }

    # Location-specific footage MUST identify the location.
    if place and shot_type in (
        "location",
        "place",
        "person_activity",
        "event",
        "action",
        "archival",
    ):
        if not place_hit and specificity in (
            "exact",
            "representative",
        ):
            return {
                "accepted": False,
                "score": round(max(0.0, score), 3),
                "reasons": reasons + [
                    "location verification failed"
                ],
            }

    # Exact shots remain completely strict.
    if specificity == "exact":
        if subject and not subject_hit:
            return {
                "accepted": False,
                "score": round(max(0.0, score), 3),
                "reasons": reasons + [
                    "exact subject verification failed"
                ],
            }

        if place and not place_hit:
            return {
                "accepted": False,
                "score": round(max(0.0, score), 3),
                "reasons": reasons + [
                    "exact location verification failed"
                ],
            }

    # ---------------------------------------------------------
    # FINAL SCORE THRESHOLD
    # ---------------------------------------------------------
    threshold = ACCEPT_THRESHOLD.get(
        specificity,
        0.50,
    )

    accepted = score >= threshold

    return {
        "accepted": accepted,
        "score": round(max(0.0, score), 3),
        "reasons": reasons,
    }

def serialize_bible(bible):
    return {
        key: value
        for key, value in (bible or {}).items()
        if key not in {"by_id", "alias_index"}
    }


def hydrate_bible(bible):
    bible = dict(bible or {})
    entities = []
    for entity in bible.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        item = dict(entity)
        item.setdefault("aliases", [])
        item.setdefault("role", "")
        item.setdefault("description", "")
        item.setdefault("disambiguation", item.get("description") or "")
        item.setdefault("date_range", "")
        item.setdefault("location", "")
        entities.append(item)
    bible["entities"] = entities
    bible["by_id"] = {entity["id"]: entity for entity in entities if entity.get("id")}
    bible["alias_index"] = build_alias_index(entities)
    bible.setdefault("primary_subject_id", entities[0]["id"] if entities else None)
    bible.setdefault("story_era", bible.get("time_period") or "")
    bible.setdefault("time_period", bible.get("story_era") or "")
    bible.setdefault("tone", "")
    bible.setdefault("banned_terms", [])
    bible.setdefault("banned_visuals", [])
    bible.setdefault("title", "")
    bible.setdefault("story_type", "other")
    bible.setdefault("timeline", [])
    return bible


def plan_script(script_text, sentences, llm, cache_path=None):
    key = hashlib.sha256((str(script_text) + "\n" + "\n".join(sentences)).encode("utf-8")).hexdigest()[:16]

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
        print("STORY: no LLM configured -> STRUCTURAL FALLBACK MODE.")

    if cache_path and mode == "llm":
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(
            json.dumps(
                {
                    "key": key,
                    "mode": mode,
                    "bible": serialize_bible(bible),
                    "plans": plans,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return bible, plans, mode


def describe_plan(index, plan):
    lines = [
        f"SENTENCE {index + 1}: {plan['sentence']}",
        f"  MEANING : {plan['meaning'] or '(degraded)'}",
        f"  ROLE    : {plan['role']}   SUBJECT: {plan['subject_id']}   ENTITIES: {plan['entities']}",
    ]
    if plan.get("events"):
        lines.append(f"  EVENTS  : {plan['events']} TIME: {plan.get('time')} PLACE: {plan.get('place')}")
    for number, scenario in enumerate(plan["scenarios"], 1):
        lines.append(f"  SHOT {number} : [{scenario['specificity']}/{scenario['kind']}] {scenario['visual']}")
        for query in scenario["queries"]:
            lines.append(f"      query: {query}")
    return "\n".join(lines)


_EDGE_PUNCT = "\"'â€œâ€â€˜â€™()[]{}:;,.!?"


def capitalized_runs(sentence):
    sentence = str(sentence or "")
    words = sentence.split()
    runs = []
    current = []
    start = 0
    previous = ""

    def flush():
        nonlocal current
        if current:
            runs.append({
                "words": list(current),
                "initial": start == 0,
                "prev": previous,
                "sentence": sentence,
            })
            current = []

    for index, raw in enumerate(words):
        core = raw.strip(_EDGE_PUNCT)
        is_capitalized = (
            bool(core)
            and len(core) > 1
            and core[0].isupper()
            and re.match(r"^[A-Za-z][\w'â€™\-]*$", core)
        )
        if is_capitalized:
            if not current:
                start = index
                previous = words[index - 1].strip(_EDGE_PUNCT).lower() if index > 0 else ""
            current.append(core)
            if raw[-1:] in ",.;:!?)" "â€\"":
                flush()
        else:
            flush()
    flush()
    return runs


def _looks_like_name_phrase(value, run):
    words = value.split()
    if not 2 <= len(words) <= 5:
        return False
    sentence = str(run.get("sentence") or "")
    sentence_low = sentence.lower()
    previous = str(run.get("prev") or "").lower()
    if previous in {"a", "an", "the"}:
        return False
    person_patterns = [
        r"\bwas born\b", r"\bborn in\b", r"\bdied\b", r"\bwas killed\b",
        r"\bwas murdered\b", r"\bwas arrested\b", r"\bwas convicted\b",
        r"\bwas elected\b", r"\bwas appointed\b", r"\bserved as\b",
        r"\bworked as\b", r"\bmarried\b", r"\bson of\b", r"\bdaughter of\b",
        r"\bhusband of\b", r"\bwife of\b", r"\bfather of\b", r"\bmother of\b",
        r"\bbrother of\b", r"\bsister of\b", r"\bknown as\b", r"\baccording to\b",
        r"\bsaid\b", r"\bsaid that\b",
    ]
    if any(re.search(pattern, sentence_low) for pattern in person_patterns):
        return True
    phrase_low = value.lower()
    structural_patterns = [
        rf"\b{re.escape(phrase_low)}\b\s+(?:was|is|became|had|has|"
        rf"worked|served|joined|left|returned|moved|lived|created|"
        rf"founded|built|led|ran|owned|died|appeared)\b",
    ]
    return any(re.search(pattern, sentence_low) for pattern in structural_patterns)


def _looks_like_organization(value):
    low = value.lower()
    organization_patterns = [
        r"\bdepartment\b", r"\bagency\b", r"\bbureau\b", r"\bministry\b",
        r"\bgovernment\b", r"\bcompany\b", r"\bcorporation\b", r"\binc\b",
        r"\bltd\b", r"\buniversity\b", r"\bcollege\b", r"\bschool\b",
        r"\bfoundation\b", r"\bassociation\b", r"\borganization\b",
        r"\binstitute\b", r"\bcommittee\b", r"\bcommission\b",
        r"\bnewspaper\b", r"\bnetwork\b", r"\bair force\b", r"\barmy\b",
        r"\bnavy\b", r"\bpolice\b", r"\bcourt\b", r"\bchurch\b",
        r"\bmuseum\b", r"\bteam\b", r"\bclub\b",
    ]
    return any(re.search(pattern, low) for pattern in organization_patterns)


def _looks_like_place(value):
    low = value.lower()
    place_patterns = [
        r"\b(?:city|town|village|county|state|province)\b",
        r"\b(?:street|road|avenue|boulevard|lane|drive)\b",
        r"\b(?:island|river|lake|mountain|valley|desert)\b",
        r"\b(?:district|neighborhood|region)\b",
        r"\b(?:airport|station|square|bridge|park)\b",
        r"\b(?:hospital|prison|library|church|school)\b",
    ]
    return any(re.search(pattern, low) for pattern in place_patterns)


def _looks_like_object(value):
    low = value.lower()
    object_context = [
        r"\bcalled\b", r"\bnamed\b", r"\bknown as\b", r"\bused\b",
        r"\bcarried\b", r"\bheld\b", r"\bopened\b", r"\bfound\b",
        r"\bdiscovered\b", r"\bcontained\b",
    ]
    return any(re.search(pattern, low) for pattern in object_context)


def _merge_entity(entities, canonical, entity_type, role="", description=""):
    canonical = _canonical_without_possessive(canonical)
    if not canonical:
        return None
    key = _norm(canonical)
    for entity in entities:
        existing_key = _norm(entity["canonical"])
        if key == existing_key or key in {_norm(alias) for alias in entity.get("aliases", [])}:
            if role and not entity["role"]:
                entity["role"] = role
            if description and not entity["description"]:
                entity["description"] = description
                if not entity.get("disambiguation"):
                    entity["disambiguation"] = description
            return entity

    entity_id = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
    if not entity_id:
        return None
    entity = {
        "id": entity_id,
        "canonical": canonical,
        "type": entity_type,
        "role": role,
        "aliases": [],
        "pronoun": None,
        "description": description,
        "disambiguation": description,
        "date_range": "",
        "location": "",
    }
    entities.append(entity)
    return entity


def heuristic_bible(sentences):
    sentences = [str(sentence or "").strip() for sentence in (sentences or []) if str(sentence or "").strip()]
    full_text = " ".join(sentences)
    entities = []

    for sentence in sentences:
        for run in capitalized_runs(sentence):
            words = list(run["words"])
            if run["initial"] and len(words) == 1:
                continue
            if not words:
                continue
            value = _canonical_without_possessive(" ".join(words))
            if not value:
                continue
            if _looks_like_organization(value):
                _merge_entity(entities, value, "organization")
                continue
            if _looks_like_place(value):
                _merge_entity(entities, value, "place", "location")
                continue
            if _looks_like_name_phrase(value, run):
                _merge_entity(entities, value, "person")
                continue

    for entity in entities:
        if entity["type"] != "person":
            continue
        parts = entity["canonical"].split()
        if len(parts) >= 2:
            entity["_possible_aliases"] = [parts[0], parts[-1]]

    alias_owners = {}
    for entity in entities:
        for alias in entity.get("_possible_aliases", []):
            key = _norm(alias)
            if len(key) < 3:
                continue
            alias_owners.setdefault(key, set()).add(entity["id"])

    for entity in entities:
        aliases = []
        for alias in entity.get("_possible_aliases", []):
            key = _norm(alias)
            if len(alias_owners.get(key, set())) == 1:
                aliases.append(alias)
        entity["aliases"] = aliases
        entity.pop("_possible_aliases", None)

    merged = []
    for entity in entities:
        found = None
        for existing in merged:
            if _norm(existing["canonical"]) == _norm(entity["canonical"]):
                found = existing
                break
        if found:
            if entity["role"] and not found["role"]:
                found["role"] = entity["role"]
            for alias in entity.get("aliases", []):
                if alias not in found["aliases"]:
                    found["aliases"].append(alias)
        else:
            merged.append(entity)
    entities = merged

    for entity in entities:
        aliases = [entity["canonical"], *entity.get("aliases", [])]
        count = 0
        for alias in aliases:
            alias = _canonical_without_possessive(alias)
            if not alias:
                continue
            count += len(re.findall(r"(?<![\w])" + re.escape(alias) + r"(?![\w])", full_text, flags=re.IGNORECASE))
        entity["_count"] = count

    timeline = []
    seen_timeline = set()
    for sentence in sentences:
        years = re.findall(r"\b(?:1[5-9]\d{2}|20\d{2})\b", sentence)
        if not years:
            continue
        what = _clean_text(sentence)
        if len(what) > 220:
            what = what[:220].rstrip() + "..."
        for year in years:
            key = (year, what)
            if key in seen_timeline:
                continue
            seen_timeline.add(key)
            timeline.append({"when": year, "what": what})

    primary = None
    people = [entity for entity in entities if entity["type"] == "person"]
    if people:
        primary = max(people, key=lambda entity: entity.get("_count", 0))["id"]
    elif entities:
        primary = max(entities, key=lambda entity: entity.get("_count", 0))["id"]

    for entity in entities:
        entity.pop("_count", None)

    priority = {"person": 0, "organization": 1, "place": 2, "event": 3, "object": 4}
    entities.sort(key=lambda entity: (priority.get(entity["type"], 9), entity["canonical"].lower()))

    return {
        "title": "",
        "story_type": "other",
        "time_period": "",
        "story_era": "",
        "tone": "",
        "banned_terms": [],
        "banned_visuals": [],
        "primary_subject_id": primary,
        "entities": entities[:50],
        "timeline": timeline[:100],
        "events": [],
    }


def heuristic_entities_in_text(text):
    names = []
    places = []
    for run in capitalized_runs(text):
        words = list(run["words"])
        if run["initial"] and len(words) == 1:
            continue
        if not words:
            continue
        value = _canonical_without_possessive(" ".join(words))
        if not value:
            continue
        if _looks_like_place(value):
            if value not in places:
                places.append(value)
        elif _looks_like_name_phrase(value, run):
            if value not in names:
                names.append(value)
    return names, places







