"""
Script analyzer: SCRIPT -> CASE BIBLE -> SENTENCE MEANING/ROLE -> VISUAL SCENARIOS.

Every sentence record keeps the legacy fields the rest of the pipeline reads
(text, entities, locations, dates, actions, event, visual_types,
search_queries, keywords, ...) and adds the new understanding fields
(meaning, role, subject_id, scenarios, ...).
"""

import json
import re
from pathlib import Path

from services.llm_client import get_llm
from services import story_understanding as story


def clean_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def split_sentences(script):
    text = clean_text(script)
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    sentences = [clean_text(part) for part in parts if clean_text(part)]
    return sentences or [text]


_DATE_PATTERNS = [
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+(?:19|20)\d{2}\b",
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+(?:of\s+)?(?:19|20)\d{2}\b",
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}\b",
    r"\b(?:18|19|20)\d{2}\b",
]


def extract_dates(text):
    dates = []
    for pattern in _DATE_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = match.group(0)
            if value not in dates and not any(value in d for d in dates):
                dates.append(value)
    return dates


def extract_money(text):
    matches = re.findall(
        r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:million|billion|trillion))?"
        r"|\b\d+(?:\.\d+)?\s*(?:million|billion|trillion)\s+dollars?\b",
        text,
        flags=re.IGNORECASE,
    )
    return [clean_text(item) for item in matches]


def analyze_sentence_fast(text):
    text = clean_text(text)

    names, places = story.heuristic_entities_in_text(text)

    # Generic fallback for obvious named subjects.
    # This is topic-agnostic and only runs when the
    # primary entity recognizer finds nothing.
    if not names:
        fallback_names = []

        words = re.findall(
            r"[A-Za-z0-9][A-Za-z0-9'-]*",
            text,
        )

        index = 0

        while index < len(words):
            word = words[index]

            # Examples:
            # Apollo 11
            # Flight 93
            # World War 2
            if (
                word[:1].isupper()
                and index + 1 < len(words)
                and words[index + 1].isdigit()
            ):
                fallback_names.append(
                    f"{word} {words[index + 1]}"
                )
                index += 2
                continue

            # Examples:
            # Mike Williams
            # Lake Seminole
            # North Florida Christian
            if (
                word[:1].isupper()
                and word.lower()
                not in {
                    "The",
                    "A",
                    "An",
                    "This",
                    "That",
                    "These",
                    "Those",
                    "During",
                    "While",
                    "After",
                    "Before",
                    "When",
                    "Where",
                    "How",
                    "Why",
                }
            ):
                phrase = [word]
                next_index = index + 1

                while (
                    next_index < len(words)
                    and words[next_index][:1].isupper()
                ):
                    phrase.append(
                        words[next_index]
                    )
                    next_index += 1

                if len(phrase) >= 2:
                    fallback_names.append(
                        " ".join(phrase)
                    )
                    index = next_index
                    continue

            index += 1

        names = list(
            dict.fromkeys(
                x.strip()
                for x in fallback_names
                if x.strip()
            )
        )

    dates = extract_dates(text)
    queries = []

    if names:
        queries.append(
            " ".join(
                [
                    names[0],
                    *places[:1],
                    *dates[:1],
                ]
            ).strip()
        )

        queries.append(names[0])

    elif places:
        queries.append(
            " ".join(
                places[:1] + dates[:1]
            ).strip()
        )

    # Sentence fallback search query.
    if not queries:
        words = re.findall(
            r"[A-Za-z0-9']+",
            text,
        )

        stop_words = {
            "the", "a", "an", "and", "or", "but",
            "if", "then", "this", "that", "these",
            "those", "during", "while", "with",
            "from", "into", "onto", "over", "under",
            "for", "to", "of", "in", "on", "at",
            "by", "as", "is", "was", "were", "are",
            "be", "been", "being", "its", "it",
            "their", "his", "her", "they", "them",
            "he", "she", "we", "you", "our", "your",
        }

        useful = []
        index = 0

        while index < len(words):
            word = words[index]

            if (
                index + 1 < len(words)
                and word[:1].isupper()
                and words[index + 1].isdigit()
            ):
                useful.append(
                    f"{word} {words[index + 1]}"
                )
                index += 2
                continue

            if (
                word.lower() not in stop_words
                and len(word) > 2
            ):
                useful.append(word)

            index += 1

        if useful:
            queries.append(
                " ".join(useful[:10])
            )

    return {
        "text": text,
        "entities": names,
        "locations": places,
        "dates": dates,
        "money": extract_money(text),
        "actions": [],
        "event": "",
        "objects": [],
        "visual_types": [],
        "keywords": [],
        "search_queries": [
            q
            for i, q in enumerate(queries)
            if q and q not in queries[:i]
        ],
    }

def analyze_sentence(sentence, sentence_number=1, previous_entities=None):
    """Context-free analysis of one sentence (no LLM). Use analyze_script for the real thing."""
    if isinstance(sentence, dict):
        text = clean_text(sentence.get("text") or sentence.get("sentence") or sentence.get("narration") or "")
        sentence_number = sentence.get("sentence_number") or sentence.get("sentence_id") or sentence_number
    else:
        text = clean_text(sentence)
    result = analyze_sentence_fast(text)
    result["sentence_number"] = sentence_number
    result["entity_records"] = (
        [{"name": n, "type": "entity"} for n in result["entities"]]
        + [{"name": n, "type": "location"} for n in result["locations"]]
    )
    result["visual_requirements"] = {
        k: result.get(k, [])
        for k in ("entities", "locations", "dates", "actions", "event", "visual_types", "search_queries")
    }
    return result


def _dedupe(values):
    out = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def _sentence_record(index, text, plan, bible):
    by_id = bible.get("by_id", {})
    named = [by_id[e] for e in plan.get("entity_ids", []) if e in by_id]
    people_and_orgs = [e["canonical"] for e in named if e["type"] != "place"]
    places = [e["canonical"] for e in named if e["type"] == "place"]
    if plan.get("place") and plan["place"] not in places:
        places.append(plan["place"])

    keywords, queries, kinds = [], [], []
    for scenario in plan.get("scenarios", []):
        keywords.extend(scenario.get("search_terms", []))
        queries.extend(scenario.get("queries", []))
        kinds.append(scenario.get("kind"))

    events = plan.get("events", [])
    dates = extract_dates(text)
    if plan.get("time") and plan["time"] not in dates:
        dates.append(plan["time"])

    return {
        "sentence_number": index + 1,
        "text": text,
        "meaning": plan.get("meaning", ""),
        "role": plan.get("role", "background"),
        "subject_id": plan.get("subject_id"),
        "entity_ids": plan.get("entity_ids", []),
        "story_events": events,
        "scenarios": plan.get("scenarios", []),
        "degraded": bool(plan.get("degraded")),
        "entities": people_and_orgs,
        "locations": places,
        "dates": dates,
        "money": extract_money(text),
        "actions": events,
        "event": events[0] if events else "",
        "visual_types": _dedupe(kinds),
        "search_queries": _dedupe(queries)[:8],
        "keywords": _dedupe(keywords)[:8],
        "objects": [],
        "entity_records": (
            [{"name": e["canonical"], "type": e["type"]} for e in named]
        ),
        "visual_requirements": {
            "entities": people_and_orgs,
            "locations": places,
            "dates": dates,
            "actions": events,
            "event": events[0] if events else "",
            "visual_types": _dedupe(kinds),
            "search_queries": _dedupe(queries)[:8],
        },
    }


def write_case_bible(analysis, project_dir):
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    bible = analysis.get("bible") if isinstance(analysis, dict) else {}
    path = project_dir / "case_bible.json"
    path.write_text(json.dumps(bible or {}, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def dry_run_case_bible(script, project_dir, project_id="dry_run", llm="auto"):
    """Build and save Case Bible only. No search, no render."""
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    cache_path = str(project_dir / "story_understanding_cache.json")
    analysis = analyze_script(
        script,
        project_id=project_id,
        cache_path=cache_path,
        llm=llm,
    )
    bible_path = write_case_bible(analysis, project_dir)
    bible = analysis.get("bible") or {}
    summary = {
        "project_id": project_id,
        "mode": analysis.get("mode"),
        "bible_path": str(bible_path),
        "title": bible.get("title"),
        "story_type": bible.get("story_type"),
        "story_era": bible.get("story_era") or bible.get("time_period"),
        "tone": bible.get("tone"),
        "banned_terms": bible.get("banned_terms") or [],
        "banned_visuals": bible.get("banned_visuals") or [],
        "entity_count": len(bible.get("entities") or []),
        "entities": bible.get("entities") or [],
        "sentence_count": analysis.get("sentence_count"),
        "roles": [
            {
                "sentence_number": item.get("sentence_number"),
                "role": item.get("role"),
                "subject_id": item.get("subject_id"),
                "text": item.get("text"),
                "search_queries": item.get("search_queries") or [],
            }
            for item in (analysis.get("sentences") or [])
        ],
    }
    summary_path = project_dir / "case_bible_dry_run.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "bible_path": str(bible_path),
        "summary_path": str(summary_path),
        "analysis": analysis,
    }


def analyze_script(script, project_id="", cache_path=None, llm="auto"):
    """
    llm="auto" -> use configured provider (services.llm_client.get_llm()).
    llm=None   -> force degraded (no-LLM) mode.
    llm=callable(system, user) -> use that.
    """
    sentence_texts = split_sentences(script)
    if llm == "auto":
        llm = get_llm()

    bible, plans, mode = story.plan_script(
        clean_text(script), sentence_texts, llm, cache_path=cache_path
    )

    analyzed = [
        _sentence_record(i, text, plans[i], bible)
        for i, text in enumerate(sentence_texts)
    ]

    all_entities, all_locations, all_dates = [], [], []
    for item in analyzed:
        for key, bucket in (("entities", all_entities), ("locations", all_locations), ("dates", all_dates)):
            for value in item.get(key, []):
                if value not in bucket:
                    bucket.append(value)

    print()
    print("=" * 70)
    print(f"STORY UNDERSTANDING ({mode.upper()} mode)")
    print("=" * 70)
    print(f"Story: {bible.get('title') or '(untitled)'}  [{bible.get('story_type')}]")
    print(f"Main subject: {bible.get('primary_subject_id')}")
    print(f"Era: {bible.get('story_era') or bible.get('time_period') or '(none)'}")
    print(f"Tone: {bible.get('tone') or '(none)'}")
    print(f"Case entities ({len(bible.get('entities', []))}):")
    for entity in bible.get("entities", [])[:25]:
        print(
            f"  - {entity['canonical']} [{entity['type']}] "
            f"aliases={entity.get('aliases')} "
            f"disambiguation={entity.get('disambiguation')}"
        )
    degraded_count = sum(1 for item in analyzed if item["degraded"])
    print(f"Sentences: {len(analyzed)}  (degraded: {degraded_count})")

    return {
        "text": clean_text(script),
        "entities": all_entities,
        "locations": all_locations,
        "dates": all_dates,
        "sentences": analyzed,
        "sentence_count": len(analyzed),
        "bible": story.serialize_bible(bible),
        "mode": mode,
        "project_id": project_id,
    }


def get_sentence_queries(sentence):
    if isinstance(sentence, dict) and "search_queries" in sentence:
        return sentence.get("search_queries", [])
    return analyze_sentence(sentence).get("search_queries", [])


def get_sentence_entities(sentence):
    if isinstance(sentence, dict) and "entities" in sentence:
        return sentence.get("entities", [])
    return analyze_sentence(sentence).get("entities", [])


def get_sentence_actions(sentence):
    if isinstance(sentence, dict) and "actions" in sentence:
        return sentence.get("actions", [])
    return analyze_sentence(sentence).get("actions", [])


def get_sentence_visual_types(sentence):
    if isinstance(sentence, dict) and "visual_types" in sentence:
        return sentence.get("visual_types", [])
    return analyze_sentence(sentence).get("visual_types", [])


