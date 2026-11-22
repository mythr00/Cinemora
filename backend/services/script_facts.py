"""Structured script facts (Transformation Phase 2.3, 2026-11-22).

Phase 6/8 built the verified-fact graphics path (AM-009) end to
end, but no provider emitted structured facts, so the measured
firing rate of fact-based graphics on real productions was zero
and chronologies shipped nearly empty. This module is the narrow,
deterministic fact source the transformation plan (§2.3) orders:

* EXTRACTION (planning stage): dates, quantities and dated events
  are pulled from the script's own sentences by regex — no LLM, no
  network, no inference. Every fact is stamped `script_claim` with
  its sentence reference: the basis says exactly what the fact is
  (the script asserts it), never more.
* TIMELINE DERIVATION: each dated-event fact becomes a case-bible
  timeline entry in the bible's own shape ({when, what}) plus the
  provenance fields (support "script_asserted", fact_basis,
  fact_id, sentence_number), so the chronology graphic can render
  populated from the script alone and every rendered event stays
  traceable to its sentence.
* CORROBORATION: a script fact may be upgraded to
  `research_supported` ONLY by a recorded knowledge-base verdict
  (services.kb_client, enabled under the takeover mandate — see
  that module) whose returned description actually contains the
  fact's year. The KB source is recorded on the fact. A lookup
  that finds nothing, or whose description does not support the
  date, leaves the basis at `script_claim` and records the check —
  a fact is never upgraded silently or on a name match alone.

The fact table rides on the case bible (`bible["script_facts"]`)
and is consumed by services.evidence_producer (starved-beat
graphics evidence) and by the graphics pipeline's chronology
fact-basis stamp.
"""

from __future__ import annotations

import re

from services import kb_client

BASIS_SCRIPT = "script_claim"
BASIS_RESEARCH = "research_supported"

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}
_MONTH_ALT = "|".join(_MONTHS)
_SEASON_ALT = "spring|summer|autumn|fall|winter"

# Event-grade date patterns, most specific first. Each entry:
# (regex, kind) where kind names the precision. A bare year is a
# date fact but never a dated event (too coarse to label a
# chronology node honestly).
_EVENT_DATE_PATTERNS = (
    (re.compile(
        rf"\b({_MONTH_ALT})\s+(\d{{1,2}})(?:st|nd|rd|th)?,\s*(\d{{4}})\b",
        re.IGNORECASE), "month_day_year"),
    (re.compile(
        rf"\b({_MONTH_ALT})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b",
        re.IGNORECASE), "month_day"),
    (re.compile(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_ALT}),?\s+(\d{{4}})\b",
        re.IGNORECASE), "day_month_year"),
    (re.compile(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_ALT})\b",
        re.IGNORECASE), "day_month"),
    (re.compile(
        rf"\b({_MONTH_ALT})\s+(\d{{4}})\b",
        re.IGNORECASE), "month_year"),
    (re.compile(
        rf"\b({_SEASON_ALT})\s+(?:of\s+)?(\d{{4}})\b",
        re.IGNORECASE), "season_year"),
    (re.compile(
        rf"\b({_MONTH_ALT})\b",
        re.IGNORECASE), "month"),
)
_BARE_YEAR_RE = re.compile(r"\b(1[5-9]\d\d|20[0-2]\d)\b")

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUM_ALT = r"\d[\d,]*(?:\.\d+)?|" + "|".join(_WORD_NUMBERS)
_UNITS = (
    "people|persons|feet|foot|ft|miles?|kilometres?|kilometers?|"
    "metres?|meters?|yards?|days?|weeks?|months?|years?|hours?|"
    "minutes?|seconds?|dollars?|pounds?|tons?|tonnes?|kilograms?|"
    "storeys?|floors?|passengers?|victims?|residents?|workers?|"
    "homes?|buildings?|vehicles?|containers?|ships?|students?|"
    "officers?|children|men|women|rooms?|steps?|pages?|"
    "percent|per cent"
)
_QUANTITY_RE = re.compile(
    rf"\b({_NUM_ALT})[\s-]+({_UNITS})\b", re.IGNORECASE
)
_AGE_RE = re.compile(r"\b(\d{1,3})-year-old\b", re.IGNORECASE)
_PERCENT_RE = re.compile(r"\b(\d[\d,]*(?:\.\d+)?)\s*(?:%|percent\b)", re.IGNORECASE)
_MONEY_RE = re.compile(r"[$£€]\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|billion|thousand))?", re.IGNORECASE)


def _sentence_items(sentences):
    """Normalise the accepted sentence shapes to (number, text).

    Accepts dicts with sentence_number/text (the plan shape) or
    plain strings (numbered from 1 in order)."""
    items = []
    for position, sentence in enumerate(sentences or [], start=1):
        if isinstance(sentence, dict):
            number = sentence.get("sentence_number") or position
            text = str(sentence.get("text") or "").strip()
        else:
            number = position
            text = str(sentence or "").strip()
        if text:
            items.append((int(number), text))
    return items


def _date_mentions(text):
    """All date mentions in one sentence, as dicts with display,
    precision and (when present) year. Overlapping matches are
    resolved greedily: a span consumed by a more specific pattern
    is not re-matched by a coarser one."""
    mentions = []
    consumed = []

    def _free(span):
        return all(span[1] <= s or span[0] >= e for s, e in consumed)

    for pattern, precision in _EVENT_DATE_PATTERNS:
        for match in pattern.finditer(text):
            if not _free(match.span()):
                continue
            consumed.append(match.span())
            year = None
            for group in match.groups():
                if group and re.fullmatch(r"\d{4}", group):
                    year = int(group)
            mentions.append({
                "display": match.group(0),
                "precision": precision,
                "year": year,
                "span": match.span(),
            })
    for match in _BARE_YEAR_RE.finditer(text):
        if not _free(match.span()):
            continue
        consumed.append(match.span())
        mentions.append({
            "display": match.group(0),
            "precision": "year",
            "year": int(match.group(0)),
            "span": match.span(),
        })
    mentions.sort(key=lambda m: m["span"][0])
    return mentions


def _quantity_mentions(text):
    """Quantity mentions (number + unit, ages, percents, money),
    deduplicated by span."""
    mentions = []
    consumed = []

    def _free(span):
        return all(span[1] <= s or span[0] >= e for s, e in consumed)

    for pattern in (_AGE_RE, _PERCENT_RE, _MONEY_RE, _QUANTITY_RE):
        for match in pattern.finditer(text):
            if not _free(match.span()):
                continue
            consumed.append(match.span())
            mentions.append({
                "display": match.group(0),
                "span": match.span(),
            })
    mentions.sort(key=lambda m: m["span"][0])
    return mentions


def extract_script_facts(sentences, bible=None):
    """The fact list for a script's sentences.

    Fact shapes:
    * dated_event: {kind, when, what, year, sentence_number, ...}
      — one per sentence carrying an event-grade date; the
      chronology's currency.
    * date: a bare-year mention that is not part of a dated event.
    * quantity: {kind, display, sentence_number, ...}.

    Every fact carries basis "script_claim" and source
    {"type": "script", "sentence_number": n}. When a bible is
    supplied, dated events also record the bible entities the
    sentence mentions ("subjects": canonical names) — the
    corroboration stage's lookup candidates.
    """
    entity_names = []
    if isinstance(bible, dict):
        for entity in bible.get("entities") or []:
            if isinstance(entity, dict) and entity.get("canonical"):
                entity_names.append(str(entity["canonical"]))
                for alias in entity.get("aliases") or []:
                    if alias:
                        entity_names.append(str(alias))

    facts = []
    counter = 0

    def _add(fact):
        nonlocal counter
        counter += 1
        fact["fact_id"] = f"sf_{counter:04d}"
        facts.append(fact)

    for number, text in _sentence_items(sentences):
        dates = _date_mentions(text)
        event_dates = [d for d in dates if d["precision"] != "year"]
        if event_dates:
            first = event_dates[0]
            subjects = [
                name for name in entity_names
                if name.lower() in text.lower()
            ]
            _add({
                "kind": "dated_event",
                "when": first["display"],
                "what": text,
                "year": first["year"],
                "precision": first["precision"],
                "sentence_number": number,
                "basis": BASIS_SCRIPT,
                "source": {"type": "script", "sentence_number": number},
                "subjects": subjects,
            })
        else:
            for mention in dates:
                _add({
                    "kind": "date",
                    "display": mention["display"],
                    "year": mention["year"],
                    "sentence_number": number,
                    "basis": BASIS_SCRIPT,
                    "source": {"type": "script", "sentence_number": number},
                })
        for mention in _quantity_mentions(text):
            _add({
                "kind": "quantity",
                "display": mention["display"],
                "sentence_number": number,
                "basis": BASIS_SCRIPT,
                "source": {"type": "script", "sentence_number": number},
            })
    return facts


def build_fact_table(sentences, bible=None):
    """The bible-ready fact table: the facts plus a production
    record counting what was extracted (a measured table, never a
    silent empty)."""
    facts = extract_script_facts(sentences, bible=bible)
    return {
        "facts": facts,
        "record": {
            "sentences": len(_sentence_items(sentences)),
            "facts": len(facts),
            "dated_events": sum(
                1 for f in facts if f["kind"] == "dated_event"
            ),
            "dates": sum(1 for f in facts if f["kind"] == "date"),
            "quantities": sum(
                1 for f in facts if f["kind"] == "quantity"
            ),
        },
    }


def timeline_events_from_facts(facts):
    """Bible-shaped timeline entries for the dated-event facts.

    Each entry keeps the bible's own {when, what} shape and adds
    the provenance the graphics manifest surfaces: support
    "script_asserted" (the bible's existing vocabulary for an
    unresearched fact), the fact basis, the fact id and the
    sentence reference."""
    events = []
    seen = set()
    for fact in facts or []:
        if not isinstance(fact, dict) or fact.get("kind") != "dated_event":
            continue
        key = (fact.get("when"), fact.get("sentence_number"))
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "when": fact.get("when"),
            "what": fact.get("what"),
            "support": "script_asserted",
            "fact_basis": fact.get("basis") or BASIS_SCRIPT,
            "fact_id": fact.get("fact_id"),
            "sentence_number": fact.get("sentence_number"),
        })
    return events


_WORD_RE = re.compile(r"[a-z0-9]+")


def _token_overlap(a, b):
    """Containment overlap of two texts' word sets:
    |intersection| / min(|a|, |b|)."""
    words_a = set(_WORD_RE.findall(str(a or "").lower()))
    words_b = set(_WORD_RE.findall(str(b or "").lower()))
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / min(len(words_a), len(words_b))


def _when_matches(extracted_when, existing_when):
    a = re.sub(r"\s+", " ", str(extracted_when or "").lower()).strip(" ,")
    b = re.sub(r"\s+", " ", str(existing_when or "").lower()).strip(" ,")
    return bool(a and b and (a in b or b in a))


def merge_timeline_events(existing, facts):
    """The bible timeline with extracted dated events appended —
    only those no existing entry already covers. An extracted
    event is covered when an existing entry shares its date
    (containment) with a similar text, or its text is near-verbatim
    in an existing entry. Existing entries are never modified."""
    merged = list(existing or [])
    for event in timeline_events_from_facts(facts):
        covered = False
        for entry in merged:
            if not isinstance(entry, dict):
                continue
            overlap = _token_overlap(event["what"], entry.get("what"))
            if overlap >= 0.6 or (
                _when_matches(event["when"], entry.get("when"))
                and overlap >= 0.25
            ):
                covered = True
                break
        if not covered:
            merged.append(event)
    return merged


def corroborate_facts(facts, *, lookup=None, budget=None):
    """KB-corroborate dated-event facts, in place. Returns the
    corroboration record.

    For each dated event, the fact's recorded subjects are looked
    up (each distinct name once, under one shared KBBudget). A
    fact is upgraded to research_supported ONLY when a lookup
    returns a VERIFIED verdict (kb_client's evidence == "verified")
    whose description contains the fact's year — the KB record
    then genuinely supports the date, and the source is recorded
    on the fact (source {type: "kb", source, query, status}).
    Every attempted check is recorded on the fact as `kb_check`,
    corroborated or not; a fact with no year or no subjects is
    not checked and says so in the record counts.
    """
    lookup = lookup if lookup is not None else kb_client.lookup_entity
    budget = budget if budget is not None else kb_client.KBBudget()
    record = {
        "facts_considered": 0,
        "checked": 0,
        "corroborated": 0,
        "not_corroborated": 0,
        "skipped_no_subject_or_year": 0,
        "lookups": 0,
    }
    verdicts = {}
    for fact in facts or []:
        if not isinstance(fact, dict) or fact.get("kind") != "dated_event":
            continue
        record["facts_considered"] += 1
        year = fact.get("year")
        subjects = fact.get("subjects") or []
        if not year or not subjects:
            record["skipped_no_subject_or_year"] += 1
            continue
        record["checked"] += 1
        fact_check = None
        upgraded = False
        for name in subjects:
            if name not in verdicts:
                verdicts[name] = lookup(name, budget=budget)
                record["lookups"] += 1
            verdict = verdicts[name]
            fact_check = {
                "query": name,
                "status": verdict.get("status"),
                "source": verdict.get("type_source"),
                "confidence": verdict.get("confidence"),
            }
            description = str(verdict.get("description") or "")
            if (
                verdict.get("evidence") == "verified"
                and str(year) in description
            ):
                fact["basis"] = BASIS_RESEARCH
                fact["source"] = {
                    "type": "kb",
                    "source": verdict.get("type_source"),
                    "query": name,
                    "status": verdict.get("status"),
                }
                fact["kb_check"] = fact_check
                upgraded = True
                break
        if fact_check is not None:
            fact["kb_check"] = fact_check
        if upgraded:
            record["corroborated"] += 1
        else:
            record["not_corroborated"] += 1
    return record


def apply_fact_bases_to_timeline(bible, facts):
    """After corroboration, refresh the fact_basis of the bible
    timeline entries that came from the fact table (matched by
    fact_id). Entries without a fact_id (planner-supplied) are
    never touched."""
    by_id = {
        f.get("fact_id"): f for f in facts or []
        if isinstance(f, dict) and f.get("fact_id")
    }
    if not by_id or not isinstance(bible, dict):
        return 0
    updated = 0
    for entry in bible.get("timeline") or []:
        if not isinstance(entry, dict):
            continue
        fact = by_id.get(entry.get("fact_id"))
        if fact is not None and entry.get("fact_basis") != fact.get("basis"):
            entry["fact_basis"] = fact.get("basis")
            updated += 1
    return updated
