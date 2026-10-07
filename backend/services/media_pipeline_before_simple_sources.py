import os
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import requests
from PIL import Image, UnidentifiedImageError

from services.media import download_image, search_images
from services.visual_registry import get_registry
from services import story_understanding as story


# ==========================================================================
# EVIDENCE CLASSIFICATION
# ==========================================================================

EVIDENCE_IDENTITY = "identity"
EVIDENCE_LOCATION = "location"
EVIDENCE_ORGANIZATION = "organization"
EVIDENCE_OBJECT = "object"
EVIDENCE_EVENT = "event"
EVIDENCE_ACTION = "action"
EVIDENCE_ARCHIVAL = "archival"
EVIDENCE_DOCUMENT = "document"
EVIDENCE_DATA = "data"
EVIDENCE_GENERAL_BROLL = "general_b_roll"

EVENT_SYNONYMS = {
    "bankruptcy": {"bankruptcy", "bankrupt", "insolvency", "insolvent"},
    "landing": {"landing", "landed", "landings"},
    "invasion": {"invasion", "invaded", "invading"},
    "war": {"war", "warfare", "battle"},
    "investigation": {"investigation", "investigated", "investigators"},
    "disappearance": {"disappearance", "disappeared", "missing"},
    "construction": {"construction", "constructed", "building"},
    "invention": {"invention", "invented", "inventor"},
    "launch": {"launch", "launched"},
    "collapse": {"collapse", "collapsed"},
    "crash": {"crash", "crashed"},
    "death": {"death", "died", "killed", "murder"},
    "filing": {"filing", "filed"},
    "attack": {"attack", "attacked"},
    "discovery": {"discovery", "discovered"},
}

WEAK_OVERLAP_TOKENS = {
    "financial", "finance", "money", "market", "stock",
    "earnings", "rally", "investment", "business", "company",
}


def classify_evidence_type(*, asset=None, shot_type="", query="", sentence=""):
    asset = asset or {}
    role = str(asset.get("asset_role", "")).lower().strip()
    scope = str(asset.get("asset_scope", "")).lower().strip()
    shot = str(shot_type or "").lower().strip()
    combined = " ".join([str(query or ""), str(sentence or ""), role, scope, shot]).lower()

    if (asset.get("identity_locked", False) or role == "real_person_photo"
            or scope == "real_person_photo" or shot == "real_person_photo"):
        return EVIDENCE_IDENTITY
    if scope in {"location", "location_photo", "location_broll"} or shot in {"location", "location_photo", "location_broll"}:
        return EVIDENCE_LOCATION
    if shot in {"organization", "company", "corporate"}:
        return EVIDENCE_ORGANIZATION
    if shot in {"document", "legal_document", "official_record"}:
        return EVIDENCE_DOCUMENT
    if shot in {"archival", "historical", "archive"}:
        return EVIDENCE_ARCHIVAL
    if shot in {"event", "event_footage", "incident"}:
        return EVIDENCE_EVENT
    if shot in {"action", "action_broll"}:
        return EVIDENCE_ACTION
    if shot in {"object", "product", "device"}:
        return EVIDENCE_OBJECT
    if shot in {"data", "chart", "graph", "statistics"}:
        return EVIDENCE_DATA
    if any(x in combined for x in ("headquarters", "hq", "factory", "building", "city", "town", "street", "lake", "river", "ocean", "sea", "port", "harbor", "harbour", "beach", "beaches", "courthouse", "campus")):
        return EVIDENCE_LOCATION
    if any(x in combined for x in ("company", "corporation", "organization", "organisation", "brand", "business", "enterprise", "firm")):
        return EVIDENCE_ORGANIZATION
    if any(x in combined for x in ("filing", "court", "lawsuit", "bankruptcy", "report", "record", "statement", "contract", "patent", "letter", "memo", "transcript")):
        return EVIDENCE_DOCUMENT
    if any(x in combined for x in ("archive", "archival", "historical", "historic", "vintage", "newsreel", "old footage", "old photograph")):
        return EVIDENCE_ARCHIVAL
    if any(x in combined for x in ("battle", "war", "collapse", "crash", "attack", "launch", "trial", "murder", "disappearance", "acquisition", "merger", "invention", "incident", "landing", "invasion")):
        return EVIDENCE_EVENT
    if any(x in combined for x in ("building", "manufacturing", "fighting", "sailing", "flying", "running", "walking", "speaking", "filming", "testing", "launching", "working")):
        return EVIDENCE_ACTION
    if any(x in combined for x in ("camera", "ship", "aircraft", "vehicle", "machine", "weapon", "computer", "phone", "prototype", "invention", "device", "product")):
        return EVIDENCE_OBJECT
    if any(x in combined for x in ("data", "chart", "graph", "statistics", "statistic", "percentage", "revenue", "price", "market", "population", "counter")):
        return EVIDENCE_DATA
    return EVIDENCE_GENERAL_BROLL


def calculate_evidence_strength(*, asset=None, evidence_type="", sentence=""):
    asset = asset or {}
    if asset.get("identity_locked", False):
        return "high"
    role = str(asset.get("asset_role", "")).lower()
    source = str(asset.get("source", "")).lower()
    if evidence_type == EVIDENCE_IDENTITY:
        return "high"
    if role in {"real_person_photo", "archival", "historical", "official_document"}:
        return "high"
    if evidence_type in {EVIDENCE_LOCATION, EVIDENCE_ORGANIZATION, EVIDENCE_OBJECT, EVIDENCE_EVENT, EVIDENCE_DOCUMENT, EVIDENCE_ARCHIVAL} and source not in {"pexels", "pixabay"}:
        return "medium"
    if evidence_type in {EVIDENCE_ACTION, EVIDENCE_DATA}:
        return "medium"
    return "low"


def _analyze_current_text(text):
    try:
        from services.script_analyzer import analyze_sentence_fast
        return analyze_sentence_fast(text) or {}
    except Exception:
        return {}


def extract_evidence_entity(*, evidence_type="", sentence="", query="", asset=None):
    asset = asset or {}
    evidence_type = str(evidence_type or "").lower().strip()
    sentence_text = str(sentence or "").strip()
    query_text = str(query or "").strip()
    analysis = _analyze_current_text(sentence_text)
    entities = list(analysis.get("entities") or [])
    locations = list(analysis.get("locations") or [])
    objects = list(analysis.get("objects") or [])
    dates = list(analysis.get("dates") or [])
    event = str(analysis.get("event") or "").strip()

    if asset.get("identity_locked", False):
        identity = asset.get("identity_entity") or asset.get("person_name") or asset.get("entity_name") or ""
        if str(identity).strip():
            return str(identity).strip()
    if evidence_type == EVIDENCE_IDENTITY:
        return str(asset.get("person_name") or asset.get("identity_entity") or "").strip() or (entities[0] if entities else "")
    if evidence_type == EVIDENCE_ORGANIZATION:
        return str(asset.get("entity_name") or asset.get("organization") or asset.get("company") or "").strip() or (entities[0] if entities else "")
    if evidence_type == EVIDENCE_LOCATION:
        return str(asset.get("location") or asset.get("entity_name") or "").strip() or (locations[0] if locations else (entities[0] if entities else ""))
    if evidence_type == EVIDENCE_OBJECT:
        return str(asset.get("entity_name") or asset.get("object_name") or "").strip() or (objects[0] if objects else "")
    if evidence_type == EVIDENCE_DOCUMENT:
        combined = " ".join([sentence_text, query_text]).lower()
        for document in ("bankruptcy filing", "court filing", "court document", "sec filing", "patent", "lawsuit", "contract", "report", "transcript", "statement", "memo", "letter", "filing"):
            if document in combined:
                return document
        return str(asset.get("document_name") or asset.get("entity_name") or "").strip()
    if evidence_type == EVIDENCE_DATA:
        match = re.search(
            r"\$\s?\d+(?:\.\d+)?\s?(?:million|billion|trillion)?"
            r"|\b\d+(?:\.\d+)?\s?(?:kg|lbs?|million|billion|trillion)\b"
            r"|\b\d+(?:\.\d+)?%",
            " ".join([sentence_text, query_text]), flags=re.I)
        return match.group(0) if match else str(asset.get("data_entity") or asset.get("entity_name") or "").strip()
    if evidence_type == EVIDENCE_EVENT:
        return str(asset.get("event_name") or asset.get("entity_name") or "").strip() or event
    if evidence_type == EVIDENCE_ARCHIVAL:
        if dates:
            return dates[0]
        year_match = re.search(r"\b(?:18|19|20)\d{2}\b", " ".join([sentence_text, query_text]))
        return year_match.group(0) if year_match else str(asset.get("entity_name") or "").strip()
    return str(asset.get("entity_name") or asset.get("evidence_entity") or "").strip()


def enrich_visual_with_evidence(visual, *, shot_type="", query="", sentence=""):
    if not isinstance(visual, dict):
        return visual
    evidence_type = classify_evidence_type(asset=visual, shot_type=shot_type, query=query, sentence=sentence)
    visual["evidence_type"] = evidence_type
    visual["evidence_strength"] = calculate_evidence_strength(asset=visual, evidence_type=evidence_type, sentence=sentence)
    visual["evidence_entity"] = extract_evidence_entity(evidence_type=evidence_type, sentence=sentence, query=query, asset=visual)
    reasons = {
        EVIDENCE_IDENTITY: "identity_locked_real_person", EVIDENCE_LOCATION: "location_context",
        EVIDENCE_ORGANIZATION: "organization_context", EVIDENCE_OBJECT: "object_or_product_context",
        EVIDENCE_EVENT: "event_context", EVIDENCE_ACTION: "action_context",
        EVIDENCE_ARCHIVAL: "historical_archival_context", EVIDENCE_DOCUMENT: "document_or_record_context",
        EVIDENCE_DATA: "data_or_statistical_context", EVIDENCE_GENERAL_BROLL: "general_b_roll",
    }
    visual["evidence_reason"] = reasons.get(evidence_type, "general_b_roll")
    return visual


def infer_preferred_evidence_types(sentence=""):
    sentence_text = str(sentence or "").lower().strip()
    if not sentence_text:
        return [EVIDENCE_GENERAL_BROLL]
    preferred = []

    def add(*types):
        for evidence_type in types:
            if evidence_type not in preferred:
                preferred.append(evidence_type)

    if any(p in sentence_text for p in ("person", "founder", "inventor", "ceo", "executive", "president", "director")):
        add(EVIDENCE_IDENTITY)
    if any(p in sentence_text for p in ("company", "corporation", "organization", "organisation", "brand", "business", "enterprise", "firm", "manufacturer", "employer", "headquarters")):
        add(EVIDENCE_ORGANIZATION)
    if any(p in sentence_text for p in ("city", "town", "village", "country", "state", "located", "location", "headquarters", "factory", "plant", "campus", "street", "lake", "river", "ocean", "sea", "port", "harbor", "harbour", "beach", "beaches", "courthouse", "battlefield")):
        add(EVIDENCE_LOCATION)
    if any(p in sentence_text for p in ("camera", "prototype", "device", "machine", "computer", "phone", "ship", "aircraft", "vehicle", "product", "invention", "technology", "equipment")):
        add(EVIDENCE_OBJECT)
    if any(p in sentence_text for p in ("document", "documents", "filing", "filed", "court", "lawsuit", "legal", "contract", "patent", "report", "record", "records", "statement", "letter", "memo", "transcript")):
        add(EVIDENCE_DOCUMENT)
    if any(p in sentence_text for p in ("invention", "invented", "launched", "launch", "acquired", "acquisition", "merger", "merged", "bankruptcy", "collapsed", "collapse", "crash", "attack", "battle", "war", "trial", "murder", "disappearance", "disappeared", "incident", "accident", "death", "died", "founded", "landed", "landing", "invaded", "invasion")):
        add(EVIDENCE_EVENT)
    if any(p in sentence_text for p in ("built", "created", "developed", "designed", "manufactured", "produced", "tested", "testing", "working", "worked", "searched", "searching", "investigated", "investigating", "filmed", "speaking", "walking", "running", "flying", "sailing", "fighting")):
        add(EVIDENCE_ACTION)
    if re.search(r"\b(18|19|20)\d{2}\b", sentence_text) or any(p in sentence_text for p in ("historical", "history", "historic", "archive", "archival", "vintage", "old footage", "old photograph", "years earlier", "decades earlier", "at the time", "originally")):
        add(EVIDENCE_ARCHIVAL)
    if (re.search(r"\b\d+(?:\.\d+)?%\b", sentence_text) or re.search(r"\$\s?\d", sentence_text)
            or re.search(r"\b\d+(?:\.\d+)?\s?(?:kg|lbs?|million|billion|trillion)\b", sentence_text)
            or any(p in sentence_text for p in ("revenue", "profit", "loss", "sales", "stock", "share price", "market", "percentage", "percent", "statistics", "data", "million", "billion", "trillion", "declined", "increased", "grew", "fell", "rose"))):
        add(EVIDENCE_DATA)
    if not preferred:
        add(EVIDENCE_GENERAL_BROLL)
    return preferred


EVIDENCE_STRENGTH_SCORES = {"high": 100, "medium": 65, "low": 25}


def calculate_visual_evidence_score(visual, *, sentence="", preferred_evidence_types=None):
    if not isinstance(visual, dict):
        return 0
    preferred = {str(v).lower().strip() for v in (preferred_evidence_types or []) if str(v).strip()}
    evidence_type = str(visual.get("evidence_type", "")).lower().strip()
    strength = str(visual.get("evidence_strength", "")).lower().strip()
    score = EVIDENCE_STRENGTH_SCORES.get(strength, 0)
    if visual.get("identity_locked", False):
        score += 100
    if evidence_type in preferred:
        score += 60
    sentence_text = str(sentence or "").lower().strip()
    entity = str(visual.get("evidence_entity", "")).lower().strip()
    if entity and entity in sentence_text:
        score += 50
    if str(visual.get("source", "")).lower().strip() == "youtube":
        score += 10
    if evidence_type == EVIDENCE_GENERAL_BROLL:
        score -= 20
    return max(0, int(score))


def rank_visuals_by_evidence(visuals, *, sentence="", preferred_evidence_types=None):
    if not isinstance(visuals, list):
        return []
    ranked = []
    for index, visual in enumerate(visuals):
        if not isinstance(visual, dict):
            continue
        score = calculate_visual_evidence_score(visual, sentence=sentence, preferred_evidence_types=preferred_evidence_types)
        visual["evidence_score"] = score
        ranked.append((bool(visual.get("identity_locked", False)), score, index, visual))
    ranked.sort(key=lambda item: (item[0], item[1], -item[2]), reverse=True)
    return [item[3] for item in ranked]


def enrich_visual_collection_with_evidence(visuals, *, shot_type="", query="", sentence=""):
    if not isinstance(visuals, list):
        return visuals
    for visual in visuals:
        enrich_visual_with_evidence(visual, shot_type=shot_type, query=query, sentence=sentence)
    return visuals


# ==========================================================================
# CONFIG / HELPERS
# ==========================================================================

DEFAULT_IMAGE_COUNT = 3
DEFAULT_VIDEO_COUNT = 2
DEFAULT_CLIP_DURATION = 8
MAX_CONSECUTIVE_SEARCH_FAILURES = 8
YT_DLP_TIMEOUT = 120
FFMPEG_TIMEOUT = 90
TEMP_FILE_SUFFIXES = (".part", ".ytdl", ".temp")
TEMP_FILE_PATTERNS = ("*.part", "*.ytdl", "*.temp")
MAX_IDENTITY_PEOPLE_PER_SCENE = 8
NO_SEARCH_ROLES = {"transition", "reflection", "hook", "conclusion", "motivation"}
MAX_BEAT_SENTENCES = 5

_search_failure_state = {"consecutive_failures": 0, "last_error": None}
_STOP = {"a", "an", "the", "of", "in", "on", "at", "to", "and", "or", "for", "with", "from", "missing", "news", "photo", "image"}


class SearchCircuitBreakerTripped(RuntimeError):
    pass


def _record_search_success():
    _search_failure_state["consecutive_failures"] = 0


def _record_search_failure(error):
    _search_failure_state["consecutive_failures"] += 1
    _search_failure_state["last_error"] = error
    if _search_failure_state["consecutive_failures"] >= MAX_CONSECUTIVE_SEARCH_FAILURES:
        raise SearchCircuitBreakerTripped(
            f"Search API has failed {_search_failure_state['consecutive_failures']} times. Last error: {error!r}")


FACE_WORDS = re.compile(r"\b(portrait|headshot|face|selfie|woman|man|girl|boy|model|actor|person|people|couple)\b", re.I)
ANIMAL_WORDS = re.compile(
    r"\b(chicken|chickens|rooster|hen|cow|cows|pig|pigs|goat|sheep|horse|barn|farm|farmer|livestock|duck|goose|puppy|kitten|cat|dog)\b", re.I)

BEAT_HINTS = [
    (re.compile(r"\b(police|detective|sheriff|arrest|search(ed|ing)?)\b", re.I), "police"),
    (re.compile(r"\b(court|trial|jury|judge|sentence|convict|appeal|courthouse)\b", re.I), "court"),
    (re.compile(r"\b(news|reporter|journalist|headline|press)\b", re.I), "press"),
    (re.compile(r"\b(lake|boat|fish|water|dock|river|ocean|sea|beach)\b", re.I), "water"),
    (re.compile(r"\b(document|letter|file|record|disk|metadata)\b", re.I), "documents"),
    (re.compile(r"\b(church|neighbor|family|community)\b", re.I), "community"),
]


@dataclass
class EntityBank:
    people: list = field(default_factory=list)
    places: list = field(default_factory=list)
    dates: list = field(default_factory=list)
    objects: list = field(default_factory=list)


@dataclass
class ShotNeed:
    beat_id: str
    shot_index: int
    shot_type: str
    description: str
    queries: list


@dataclass
class Beat:
    beat_id: str
    label: str
    start: int
    end: int
    text: str
    sentence_indexes: list
    shots: list


def _http():
    session = requests.Session()
    session.headers.update({"User-Agent": "DocumentaryStudio/0.4"})
    return session


def mentions_named_person(text):
    return bool(_analyze_current_text(text).get("entities"))


def looks_like_face(title):
    return bool(FACE_WORDS.search(str(title or "")))


def looks_like_animal(title):
    return bool(ANIMAL_WORDS.search(str(title or "")))


def blocked_visual(title, allow_people=False):
    title = str(title or "")
    if looks_like_animal(title):
        return True
    if allow_people or mentions_named_person(title):
        return False
    return looks_like_face(title)


def query_relevance(query, title):
    q = str(query or "").lower()
    t = str(title or "").lower()
    if not t:
        return 0.0
    tokens = [w for w in re.findall(r"[a-z0-9]+", q) if w not in _STOP and len(w) > 3]
    if not tokens:
        return 1.0
    return sum(1 for w in tokens if story._stem(w) in t or w in t) / len(tokens)


def uniq(items):
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def extract_entities(text):
    analysis = _analyze_current_text(text)
    return EntityBank(
        people=uniq(list(analysis.get("entities") or [])),
        places=uniq(list(analysis.get("locations") or [])),
        dates=uniq(list(analysis.get("dates") or [])),
        objects=uniq(list(analysis.get("objects") or [])),
    )


def _queries_from_text(text):
    analysis = _analyze_current_text(text)
    queries = list(analysis.get("search_queries") or [])
    entities = list(analysis.get("entities") or [])
    locations = list(analysis.get("locations") or [])
    dates = list(analysis.get("dates") or [])
    if entities:
        queries.append(entities[0])
        queries.append(f"{entities[0]} archival")
    if entities and locations:
        queries.append(f"{entities[0]} {locations[0]}")
    if locations:
        queries.append(locations[0])
    if dates and entities:
        queries.append(f"{entities[0]} {dates[0]}")
    return uniq([q for q in queries if str(q).strip()])[:8]


def detect_label(text):
    for pattern, label in BEAT_HINTS:
        if pattern.search(text or ""):
            return label
    return "general"


def segment_beats(sentences, min_len=3, max_len=8):
    if not sentences:
        return []
    labels = [detect_label(item) for item in sentences]
    groups, start, current = [], 0, labels[0]
    for index, label in enumerate(labels):
        long_enough = (index - start) >= min_len
        too_long = (index - start + 1) > max_len
        if (label != current and long_enough) or too_long:
            groups.append((current, start, index - 1))
            start, current = index, label
    groups.append((current, start, len(sentences) - 1))
    beats = []
    for beat_no, (label, s0, e0) in enumerate(groups, start=1):
        indexes = list(range(s0, e0 + 1))
        text = " ".join(sentences[i] for i in indexes)
        queries = _queries_from_text(text) or [text[:80]]
        shots = [ShotNeed(f"beat_{beat_no:02d}_{label}", n, "search", f"{label} shot {n}",
                          queries[n - 1:n] or queries[:1])
                 for n in range(1, min(4, max(2, len(queries) + 1)))]
        beats.append(Beat(f"beat_{beat_no:02d}_{label}", label, s0, e0, text, indexes, shots))
    return beats


def entity_anchor_shots(entities):
    shots, index = [], 1
    for person in entities.people:
        shots.append(ShotNeed("entity_anchors", index, "real_person_photo", person, [person, f"{person} photo", f"{person} archive"]))
        index += 1
    for place in entities.places:
        shots.append(ShotNeed("entity_anchors", index, "location_photo", place, [place, f"{place} historical", f"{place} archival"]))
        index += 1
    for obj in entities.objects:
        shots.append(ShotNeed("entity_anchors", index, "anchor", obj, [str(obj), f"{obj} archival"]))
        index += 1
    return shots


def generic_filler_shots():
    return []


def build_visual_plan(sentences):
    entities = extract_entities(" ".join(sentences))
    beats = segment_beats(sentences)
    shots = entity_anchor_shots(entities)
    for beat in beats:
        shots.extend(beat.shots)
    return {"entities": entities, "beats": beats, "shots": shots}


# ==========================================================================
# CASE BIBLE
# ==========================================================================

def _load_case_bible(scene, project_dir):
    raw_bible = None
    if isinstance(scene, dict):
        raw_bible = scene.get("bible") or scene.get("story_bible")
        bible_path = scene.get("case_bible_path") or ""
    else:
        bible_path = ""
    if not raw_bible:
        if not bible_path:
            bible_path = os.path.join(project_dir, "case_bible.json")
        if bible_path and os.path.exists(bible_path):
            try:
                with open(bible_path, "r", encoding="utf-8") as handle:
                    raw_bible = json.load(handle)
            except Exception as exc:
                print(f"CASE BIBLE FILE READ FAILED: {exc}")
                raw_bible = None
    try:
        bible = story.hydrate_bible(raw_bible)
    except Exception as exc:
        print(f"CASE BIBLE LOAD SKIPPED: {exc}")
        bible = story.hydrate_bible({})
    print(
        "CASE BIBLE LOADED: "
        f"{bible.get('title') or '(untitled)'} "
        f"entities={len(bible.get('entities') or [])} "
        f"era={bible.get('story_era') or bible.get('time_period')}"
    )
    return bible


# ==========================================================================
# SENTENCE ACCESS
# ==========================================================================

def get_sentence_list(scene):
    sentences = scene.get("sentences") or scene.get("sentence_analysis") or scene.get("sentence_data") or []
    return sentences if isinstance(sentences, list) else []


def get_sentence_text(sentence):
    if isinstance(sentence, str):
        return sentence.strip()
    if not isinstance(sentence, dict):
        return ""
    return (sentence.get("text") or sentence.get("sentence") or sentence.get("content") or "").strip()


def get_sentence_number(sentence, fallback):
    if isinstance(sentence, dict):
        value = sentence.get("sentence_id") or sentence.get("sentence_number") or sentence.get("number") or sentence.get("index")
        if isinstance(value, int):
            return value
    return fallback


def result_title(result):
    return str((result or {}).get("title", "") or "").strip()


def result_source(result):
    return str((result or {}).get("source", "") or "").strip().lower()


def result_url(result):
    if not isinstance(result, dict):
        return ""
    return str(result.get("url") or result.get("link") or result.get("video_url") or result.get("image_url") or "").strip()


# ==========================================================================
# STORY BEATS (Stage 2)
# ==========================================================================

def _sentence_place(sentence, bible):
    if not isinstance(sentence, dict):
        return ""
    place = str(sentence.get("place") or "").strip()
    if place:
        return place
    locations = sentence.get("locations") or []
    if locations:
        return str(locations[0]).strip()
    by_id = (bible or {}).get("by_id") or {}
    for entity_id in sentence.get("entity_ids") or []:
        entity = by_id.get(entity_id) or {}
        if entity.get("type") == "place":
            return str(entity.get("canonical") or "").strip()
    return ""


def _sentence_subject(sentence):
    if not isinstance(sentence, dict):
        return ""
    return str(sentence.get("subject_id") or "").strip()


def _sentence_role(sentence):
    if not isinstance(sentence, dict):
        return "background"
    return str(sentence.get("role") or "background").strip().lower()


def _beat_group_key(sentence, bible):
    role = _sentence_role(sentence)
    if role in NO_SEARCH_ROLES:
        return ("__transition__", "", role)
    return (_sentence_subject(sentence), _sentence_place(sentence, bible), "")


def _entity(bible, entity_id):
    if not entity_id or not isinstance(bible, dict):
        return {}
    return (bible.get("by_id") or {}).get(entity_id) or {}


def build_shot_needs(beat, bible=None):
    bible = bible or {}
    if not beat.get("searchable", True):
        return []
    banned = [str(x) for x in (bible.get("banned_terms") or bible.get("forbidden_terms") or []) if str(x).strip()]
    era = str(bible.get("story_era") or bible.get("time_period") or "").strip()
    needs = []
    for index, scenario in enumerate(_beat_scenarios(beat), start=1):
        subject_id = scenario.get("subject_id") or beat.get("subject_id") or ""
        entity = _entity(bible, subject_id)
        place = str(scenario.get("place") or beat.get("place") or entity.get("location") or "").strip()
        shot_era = str(scenario.get("era") or era).strip()
        kind = str(scenario.get("kind") or "archival")
        specificity = str(scenario.get("specificity") or "representative")
        needs.append({
            "beat_id": beat.get("beat_id"),
            "shot_index": index,
            "shot_type": kind,
            "specificity": specificity,
            "description": scenario.get("visual") or "",
            "required_subject_id": subject_id or None,
            "required_subject": entity.get("canonical") or "",
            "required_type": entity.get("type") or "",
            "era": shot_era,
            "place": place,
            "event": scenario.get("event") or "",
            "forbidden": banned,
            "priority": 1 if specificity == "exact" else (2 if specificity == "representative" else 3),
            "fallback": "related_entity" if specificity == "exact" else "same_era_place",
            "queries": list(scenario.get("queries") or []),
        })
    return needs


def build_story_beats(sentences, bible=None):
    bible = bible or {}
    prepared = []
    for index, sentence in enumerate(sentences or []):
        text = get_sentence_text(sentence)
        if not text:
            continue
        prepared.append({
            "index": index,
            "sentence": sentence,
            "text": text,
            "sentence_number": get_sentence_number(sentence, index + 1),
            "subject_id": _sentence_subject(sentence),
            "place": _sentence_place(sentence, bible),
            "role": _sentence_role(sentence),
            "key": _beat_group_key(sentence, bible),
        })

    beats = []
    current = None
    for item in prepared:
        start_new = (
            current is None
            or item["key"] != current["key"]
            or item["role"] in NO_SEARCH_ROLES
            or current["role"] in NO_SEARCH_ROLES
            or len(current["sentence_indexes"]) >= MAX_BEAT_SENTENCES
        )
        if start_new:
            if current:
                beats.append(current)
            beat_no = len(beats) + 1
            subject = item["subject_id"] or "none"
            place = item["place"] or "none"
            current = {
                "beat_id": f"beat_{beat_no:02d}_{item['role']}_{subject}_{place}",
                "beat_index": beat_no,
                "label": subject if subject != "none" else (item["place"] or item["role"]),
                "subject_id": item["subject_id"],
                "place": item["place"],
                "role": item["role"],
                "searchable": item["role"] not in NO_SEARCH_ROLES,
                "sentence_indexes": [item["index"]],
                "sentence_numbers": [item["sentence_number"]],
                "sentences": [item["sentence"]],
                "text": item["text"],
                "key": item["key"],
            }
        else:
            current["sentence_indexes"].append(item["index"])
            current["sentence_numbers"].append(item["sentence_number"])
            current["sentences"].append(item["sentence"])
            current["text"] = (current["text"] + " " + item["text"]).strip()
    if current:
        beats.append(current)
    for beat in beats:
        beat["shot_needs"] = build_shot_needs(beat, bible)
    return beats

def write_review_log(project_dir, scene_number, beat, records):
    path = Path(project_dir) / f"scene_{scene_number}_review.json"
    existing = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    existing = [row for row in existing if row.get("beat_id") != beat.get("beat_id")]
    existing.append({
        "beat_id": beat.get("beat_id"),
        "role": beat.get("role"),
        "searchable": beat.get("searchable", True),
        "shot_needs": beat.get("shot_needs") or [],
        "records": records,
    })
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"REVIEW SAVED: {path}")
    return path
def write_beats_debug(beats, project_dir, scene_number=1):
    path = Path(project_dir) / f"scene_{scene_number}_beats.json"
    payload = []
    for beat in beats:
        payload.append({
            "beat_id": beat["beat_id"],
            "subject_id": beat.get("subject_id"),
            "place": beat.get("place"),
            "role": beat.get("role"),
            "searchable": beat.get("searchable", True),
            "sentence_numbers": beat.get("sentence_numbers"),
            "text": beat.get("text"),
            "shot_needs": beat.get("shot_needs") or [],
        })
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"BEATS SAVED: {path} count={len(payload)}")
    return path


def _beat_scenarios(beat):
    scenarios = []
    if not beat.get("searchable", True):
        return scenarios
    for sentence in beat.get("sentences") or []:
        if _sentence_role(sentence) in NO_SEARCH_ROLES:
            continue
        for scenario in _uve_scenarios_for(sentence):
            scenarios.append(scenario)
    unique, seen = [], set()
    for scenario in scenarios:
        key = tuple((scenario.get("queries") or [])[:3])
        if key in seen:
            continue
        seen.add(key)
        unique.append(scenario)
    return unique[:6]


def dry_run_beats(script, project_dir, project_id="beats_dry_run", llm="auto"):
    from services.script_analyzer import analyze_script, write_case_bible
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    analysis = analyze_script(
        script,
        project_id=project_id,
        cache_path=str(project_dir / "story_understanding_cache.json"),
        llm=llm,
    )
    write_case_bible(analysis, project_dir)
    bible = story.hydrate_bible(analysis.get("bible"))
    beats = build_story_beats(analysis.get("sentences") or [], bible)
    path = write_beats_debug(beats, project_dir, scene_number=1)
    return {
        "beats_path": str(path),
        "beat_count": len(beats),
        "sentence_count": analysis.get("sentence_count"),
    }


# ==========================================================================
# PROVIDER SEARCH
# ==========================================================================

def _licensed_image_hit(url, title, source):
    return {"image_url": url, "url": url, "link": url, "title": title,
            "source": source, "license": "royalty-free", "snippet": title}


def _licensed_video_hit(url, title, source):
    return {"video_url": url, "url": url, "link": url, "title": title,
            "source": source, "license": "royalty-free", "snippet": title}


def _rank_hits(query, hits, limit, allow_people=False):
    ranked = []
    for hit in hits:
        title = hit.get("title", "")
        if blocked_visual(title, allow_people=allow_people):
            continue
        score = query_relevance(query, title)
        if score < 0.20:
            continue
        ranked.append((score, hit))
    ranked.sort(key=lambda row: -row[0])
    return [hit for score, hit in ranked[:limit]]


def search_licensed_images(query, limit=8, allow_people=False):
    hits = _pexels_photos(query, limit) + _pixabay_photos(query, limit)
    if query_relevance(query, " ".join(h.get("title", "") for h in hits[:3])) < 0.34:
        try:
            extra = search_images(query, limit=limit) or []
        except TypeError:
            try:
                extra = search_images(query) or []
            except Exception as error:
                print(f"Web image search failed: {error}")
                extra = []
        except Exception as error:
            print(f"Web image search failed: {error}")
            extra = []
        for item in extra:
            if not isinstance(item, dict):
                continue
            url = item.get("image_url") or item.get("url") or item.get("link") or ""
            title = item.get("title") or item.get("snippet") or query
            if url:
                hit = _licensed_image_hit(url, title, item.get("source") or "web")
                hit["source_url"] = item.get("source_url") or item.get("link") or ""
                hits.append(hit)
    return _rank_hits(query, hits, limit, allow_people=allow_people)


def search_youtube_videos(query, limit=5):
    query = str(query or "").strip()
    if not query:
        return []
    results, seen = [], set()
    for search_query in (f"site:youtube.com/watch {query}", f'site:youtube.com/watch "{query}"'):
        if len(results) >= limit:
            break
        try:
            print(f"YOUTUBE DISCOVERY QUERY: {search_query}")
            hits = search_images(search_query, limit=max(5, min(limit * 2, 10))) or []
        except Exception as error:
            print(f"YouTube Serper discovery failed: {error}")
            continue
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            youtube_url = ""
            for possible in (hit.get("link"), hit.get("url"), hit.get("source_url"), hit.get("video_url")):
                candidate = str(possible or "").strip()
                if "youtube.com/watch" in candidate or "youtu.be/" in candidate:
                    youtube_url = candidate
                    break
            if not youtube_url:
                continue
            if "&" in youtube_url:
                youtube_url = youtube_url.split("&", 1)[0]
            if youtube_url in seen:
                continue
            title = str(hit.get("title") or hit.get("snippet") or query).strip() or query
            if query_relevance(query, title) < 0.20:
                continue
            seen.add(youtube_url)
            results.append({"video_url": youtube_url, "url": youtube_url, "link": youtube_url,
                            "title": title, "source": "youtube", "license": "unknown",
                            "rights_status": "verify_before_publish",
                            "snippet": str(hit.get("snippet") or title)})
            if len(results) >= limit:
                break
    return results[:limit]


def search_licensed_videos(query, limit=5, allow_people=False):
    query = str(query or "").strip()
    if not query:
        return []
    candidates = []
    for name, fn in (("PEXELS", lambda: _pexels_videos(query, limit)),
                     ("PIXABAY", lambda: _pixabay_videos(query, limit)),
                     ("YOUTUBE", lambda: search_youtube_videos(query, limit))):
        try:
            hits = fn() or []
            candidates.extend(hits)
            if hits:
                print(f"VIDEO PROVIDER {name}: {len(hits)} candidates")
        except Exception as error:
            print(f"{name} video search failed: {error}")

    unique, seen = [], set()
    for hit in candidates:
        url = str(hit.get("url") or hit.get("video_url") or "").strip()
        if not url or url.lower() in seen:
            continue
        seen.add(url.lower())
        unique.append(hit)

    ranked = _rank_hits(query, unique, max(limit * 3, limit), allow_people=allow_people)
    selected, selected_urls = [], set()
    for provider in ("pexels", "pixabay", "youtube"):
        for hit in ranked:
            if str(hit.get("source", "")).lower() != provider:
                continue
            url = str(hit.get("url") or hit.get("video_url") or "").strip()
            if url and url not in selected_urls:
                selected.append(hit)
                selected_urls.add(url)
                break
    for hit in ranked:
        if len(selected) >= limit:
            break
        url = str(hit.get("url") or hit.get("video_url") or "").strip()
        if url and url not in selected_urls:
            selected.append(hit)
            selected_urls.add(url)
    return selected[:limit]


def _pexels_photos(query, limit):
    key = os.getenv("PEXELS_API_KEY", "").strip()
    if not key:
        return []
    try:
        response = _http().get("https://api.pexels.com/v1/search", headers={"Authorization": key},
                               params={"query": query, "per_page": limit, "orientation": "landscape", "size": "large"}, timeout=30)
        if response.status_code != 200:
            print(f"Pexels photo error {response.status_code}")
            return []
        out = []
        for photo in response.json().get("photos", []):
            src = photo.get("src") or {}
            url = src.get("large2x") or src.get("large") or src.get("original")
            if url:
                out.append(_licensed_image_hit(url, photo.get("alt") or query, "pexels"))
        return out
    except Exception as error:
        print(f"Pexels photo failed: {error}")
        return []


def _slug_title(page_url, fallback):
    match = re.search(r"/video/(.+?)-\d+/?$", str(page_url or ""))
    return match.group(1).replace("-", " ") if match else fallback


def _pexels_videos(query, limit):
    key = os.getenv("PEXELS_API_KEY", "").strip()
    if not key:
        return []
    try:
        response = _http().get("https://api.pexels.com/videos/search", headers={"Authorization": key},
                               params={"query": query, "per_page": limit, "orientation": "landscape", "size": "medium"}, timeout=30)
        if response.status_code != 200:
            print(f"Pexels video error {response.status_code}")
            return []
        out = []
        for video in response.json().get("videos", []):
            files = [f for f in (video.get("video_files") or []) if "mp4" in str(f.get("file_type") or "")]
            files.sort(key=lambda f: abs((f.get("width") or 0) - 1920))
            if files:
                out.append(_licensed_video_hit(files[0]["link"], _slug_title(video.get("url"), query), "pexels"))
        return out
    except Exception as error:
        print(f"Pexels video failed: {error}")
        return []


def _pixabay_photos(query, limit):
    key = os.getenv("PIXABAY_API_KEY", "").strip()
    if not key:
        return []
    try:
        response = _http().get("https://pixabay.com/api/", params={
            "key": key, "q": query, "image_type": "photo", "orientation": "horizontal",
            "safesearch": "true", "per_page": max(3, min(limit, 20))}, timeout=30)
        if response.status_code != 200:
            print(f"Pixabay photo error {response.status_code}")
            return []
        out = []
        for photo in response.json().get("hits", []):
            url = photo.get("largeImageURL") or photo.get("webformatURL")
            if url:
                out.append(_licensed_image_hit(url, photo.get("tags") or query, "pixabay"))
        return out
    except Exception as error:
        print(f"Pixabay photo failed: {error}")
        return []


def _pixabay_videos(query, limit):
    key = os.getenv("PIXABAY_API_KEY", "").strip()
    if not key:
        return []
    try:
        response = _http().get("https://pixabay.com/api/videos/", params={
            "key": key, "q": query, "safesearch": "true", "per_page": max(3, min(limit, 20))}, timeout=30)
        if response.status_code != 200:
            print(f"Pixabay video error {response.status_code}")
            return []
        out = []
        for video in response.json().get("hits", []):
            files = video.get("videos") or {}
            pick = files.get("medium") or files.get("large") or files.get("small") or {}
            if pick.get("url"):
                out.append(_licensed_video_hit(pick["url"], video.get("tags") or query, "pixabay"))
        return out
    except Exception as error:
        print(f"Pixabay video failed: {error}")
        return []


# ==========================================================================
# DOWNLOAD / VALIDATION
# ==========================================================================

def is_direct_media_url(url):
    low = str(url or "").lower().split("?", 1)[0]
    return low.endswith((".mp4", ".mov", ".webm", ".m4v", ".jpg", ".jpeg", ".png", ".webp"))


def kill_process_tree(process):
    if process is None:
        return
    pid = getattr(process, "pid", None)
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def run_command_safe(command, timeout, label):
    process = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"{label} TIMED OUT ({timeout}s)")
            kill_process_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except Exception:
                stdout, stderr = "", ""
            return -999, stdout or "", stderr or f"{label} timed out"
        return process.returncode, stdout or "", stderr or ""
    except Exception as error:
        print(f"{label} failed to start: {error}")
        if process is not None:
            kill_process_tree(process)
        return -998, "", str(error)


def cleanup_temp_files(directory):
    directory = Path(directory)
    if not directory.exists():
        return
    for pattern in TEMP_FILE_PATTERNS:
        for file in directory.glob(pattern):
            try:
                if file.is_file():
                    file.unlink()
            except OSError:
                pass


def validate_image(path):
    try:
        path = Path(path)
        if not path.exists() or path.stat().st_size < 1000:
            return False
        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img:
            img.load()
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def validate_video(path):
    try:
        path = Path(path)
        return path.exists() and path.stat().st_size >= 10_000
    except OSError:
        return False


def download_valid_image(image_url, output_path):
    output_path = Path(output_path).resolve()
    try:
        if output_path.exists():
            output_path.unlink()
        download_image(image_url, str(output_path))
        if not validate_image(output_path):
            try:
                output_path.unlink()
            except OSError:
                pass
            return False
        return True
    except Exception as error:
        print(f"IMAGE DOWNLOAD FAILED: {error}")
        try:
            if output_path.exists():
                output_path.unlink()
        except OSError:
            pass
        return False


def download_direct_video(url, output_path, duration=DEFAULT_CLIP_DURATION):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = output_path.with_name(output_path.stem + "_raw.mp4")
    try:
        response = _http().get(url, timeout=60, stream=True)
        response.raise_for_status()
        with open(raw_path, "wb") as handle:
            for chunk in response.iter_content(64 * 1024):
                handle.write(chunk)
        if not raw_path.exists() or raw_path.stat().st_size < 10_000:
            return False
        command = ["ffmpeg", "-y", "-i", str(raw_path), "-t", str(duration), "-c:v", "libx264",
                   "-preset", "ultrafast", "-crf", "28", "-an", "-movflags", "+faststart", str(output_path)]
        code, _, stderr = run_command_safe(command, FFMPEG_TIMEOUT, "FFmpeg licensed")
        try:
            raw_path.unlink()
        except OSError:
            pass
        if code != 0 or not validate_video(output_path):
            print((stderr or "")[-1000:])
            return False
        return True
    except Exception as error:
        print(f"Licensed video download failed: {error}")
        try:
            if raw_path.exists():
                raw_path.unlink()
        except OSError:
            pass
        return False


def download_video_clip(video_url, output_path, start_time=0, duration=DEFAULT_CLIP_DURATION):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if is_direct_media_url(video_url) or "pexels.com" in str(video_url).lower() or "pixabay.com" in str(video_url).lower():
        return download_direct_video(video_url, output_path, duration)

    try:
        if output_path.exists():
            output_path.unlink()
    except OSError:
        pass
    cleanup_temp_files(output_path.parent)
    output_template = str(output_path.with_name(output_path.stem + "_source.%(ext)s")).replace("\\", "/")
    command = [
        sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--restrict-filenames",
        "--retries", "1", "--fragment-retries", "1", "--socket-timeout", "10",
        "-f", "bestvideo[ext=mp4][height<=720]/bestvideo[height<=720]",
        "--download-sections", f"*{start_time}-{start_time + duration}",
        "--no-part", "-o", output_template, video_url,
    ]
    print(f"YOUTUBE CLIP EXTRACTION: {video_url} start={start_time}s duration={duration}s")
    returncode, stdout, stderr = run_command_safe(command, 90, "yt-dlp")
    if returncode != 0:
        print("yt-dlp clip extraction failed:")
        print((stderr or "")[-3000:])
        cleanup_temp_files(output_path.parent)
        return False

    candidates = [i for i in output_path.parent.glob(f"{output_path.stem}_source.*")
                  if i.is_file() and i.suffix.lower() not in TEMP_FILE_SUFFIXES]
    if not candidates:
        print("yt-dlp completed but no source clip was found.")
        cleanup_temp_files(output_path.parent)
        return False
    candidates.sort(key=lambda i: -i.stat().st_size)
    source_clip = candidates[0]
    ffmpeg_command = [
        "ffmpeg", "-y", "-i", str(source_clip), "-t", str(duration), "-an",
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
        "-r", "30", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output_path),
    ]
    returncode, stdout, stderr = run_command_safe(ffmpeg_command, FFMPEG_TIMEOUT, "FFmpeg YouTube")
    try:
        source_clip.unlink()
    except OSError:
        pass
    cleanup_temp_files(output_path.parent)
    if returncode != 0 or not validate_video(output_path):
        print("YouTube clip normalization failed.")
        if stderr:
            print((stderr or "")[-2000:])
        try:
            if output_path.exists():
                output_path.unlink()
        except OSError:
            pass
        return False
    print(f"VALID YOUTUBE VIDEO CLIP: {output_path}")
    return True


def download_video_with_ytdlp(video_url, output_path, start_time=0, duration=DEFAULT_CLIP_DURATION):
    return download_video_clip(video_url, output_path, start_time, duration)


def process_sentence_videos(sentence, sentence_dir, video_count=DEFAULT_VIDEO_COUNT):
    return []


def process_sentence_images(sentence, sentence_dir, image_count=DEFAULT_IMAGE_COUNT):
    return []


def process_sentence_media(sentence, project_dir, scene_number, image_count=DEFAULT_IMAGE_COUNT, video_count=DEFAULT_VIDEO_COUNT):
    return {"sentence_number": get_sentence_number(sentence, 1), "sentence_text": get_sentence_text(sentence),
            "images": [], "videos": [], "visuals": [], "sentence": sentence}


# ==========================================================================
# REAL PEOPLE
# ==========================================================================

def search_person_images(person, limit=6):
    person = str(person or "").strip()
    if not _real_subject(person):
        print(f"SKIP PERSON SEARCH: {person}")
        return []
    if not person:
        return []
    candidates, seen = [], set()
    for query in (f'"{person}"', f"{person} photo", f"{person} portrait", f"{person} archive"):
        try:
            results = search_images(query, limit=8) or []
        except Exception as exc:
            print(f"PERSON SEARCH FAILED: {query}: {exc}")
            continue
        for item in results:
            image_url = item.get("image_url") or item.get("url") or item.get("thumbnail_url")
            if not image_url or image_url in seen:
                continue
            seen.add(image_url)
            searchable = " ".join([str(item.get("title") or ""), str(item.get("snippet") or ""),
                                   str(item.get("source") or ""), str(item.get("source_url") or item.get("link") or "")]).lower()
            person_lower = person.lower()
            score = 0
            if person_lower in searchable:
                score += 100
            for part in [p for p in re.findall(r"[a-z0-9]+", person_lower) if len(p) >= 3]:
                if re.search(rf"\b{re.escape(part)}\b", searchable):
                    score += 15
            for term in ("trial", "court", "case", "interview", "portrait", "photo", "photos", "archive", "biography"):
                if term in searchable:
                    score += 3
            for source_name in ("associated press", "ap news", "reuters", "wikipedia", "bbc", "npr", "library of congress"):
                if source_name in searchable:
                    score += 25
                    break
            for generic in ("stock photo", "stock image", "businessman", "businesswoman", "generic", "model",
                            "portrait of a man", "portrait of a woman", "young man", "young woman"):
                if generic in searchable:
                    score -= 80
            if score < 80:
                continue
            candidate = dict(item)
            candidate.update({
                "person_name": person, "entity_type": "person", "asset_role": "real_person_photo",
                "identity_score": score, "identity_confidence": round(min(1.0, score / 150.0), 3),
                "evidence_level": "identity_candidate",
                "source_url": item.get("source_url") or item.get("link") or "",
            })
            candidates.append(candidate)
    candidates.sort(key=lambda item: item.get("identity_score", 0), reverse=True)
    return candidates[:limit]


def download_person_identity_asset(person, scene_dir, index=1):
    candidates = search_person_images(person, limit=10)
    if not candidates:
        print(f"NO PERSON IMAGE FOUND: {person}")
        return None
    scene_dir = Path(scene_dir)
    scene_dir.mkdir(parents=True, exist_ok=True)
    safe_person = re.sub(r"[^a-zA-Z0-9]+", "_", person.lower()).strip("_")
    output_path = scene_dir / f"person_{safe_person}_{index}.jpg"
    for candidate in candidates:
        urls = []
        for key in ("image_url", "url", "thumbnail_url"):
            value = candidate.get(key)
            if value and value not in urls:
                urls.append(value)
        for image_url in urls:
            try:
                if output_path.exists():
                    output_path.unlink()
            except OSError:
                pass
            try:
                download_image(image_url, str(output_path))
            except Exception as exc:
                print(f"Download failed: {exc}")
                continue
            if not output_path.exists():
                continue
            try:
                file_size = output_path.stat().st_size
            except OSError:
                file_size = 0
            if file_size < 10_000 or not validate_image(output_path):
                try:
                    output_path.unlink()
                except OSError:
                    pass
                continue
            asset = dict(candidate)
            asset.update({
                "file_path": str(output_path), "path": str(output_path), "image_url": image_url, "url": image_url,
                "asset_role": "real_person_photo", "entity_type": "person", "person_name": person,
                "identity_locked": True, "rights_status": "verify_before_publish", "license": "unknown",
            })
            print(f"PERSON IMAGE DOWNLOADED: {person} -> {output_path}")
            return asset
    print(f"ALL PERSON IMAGE DOWNLOADS FAILED: {person}")
    return None


def _people_for_sentence(sentence, bible):
    by_id = bible.get("by_id", {}) if isinstance(bible, dict) else {}
    if not isinstance(sentence, dict) or not by_id:
        return []
    mention_ids = story.find_mentions(get_sentence_text(sentence), bible.get("alias_index", {}))
    ids = [e for e in mention_ids if e in by_id and by_id[e].get("type") == "person"]
    for scenario in sentence.get("scenarios", []) or []:
        sid = scenario.get("subject_id")
        if (
            sid in by_id
            and by_id[sid].get("type") == "person"
            and scenario.get("specificity") == "exact"
            and sid not in ids
        ):
            ids.append(sid)
    return [by_id[i]["canonical"] for i in ids if i in by_id]


def _add_identity_assets_to_scene(result, scene, project_dir):
    if not isinstance(result, dict):
        return result
    sentences = result.get("sentences", []) or []
    if not sentences:
        return result
    scene_number = result.get("scene_number", scene.get("scene_number", 1) if isinstance(scene, dict) else 1)
    scene_dir = Path(project_dir) / f"scene_{scene_number}"

    counts = Counter()
    for sentence_result in sentences:
        for person in sentence_result.get("story_people", []) or []:
            counts[person] += 1
    scene_people = [p for p, _ in counts.most_common(MAX_IDENTITY_PEOPLE_PER_SCENE)]
    if not scene_people:
        return result

    person_assets = {}
    for index, person in enumerate(scene_people, start=1):
        asset = download_person_identity_asset(person, scene_dir, index=index)
        if asset:
            person_assets[person] = asset
    if not person_assets:
        return result

    scene_images = list(result.get("images", []) or [])
    scene_visuals = list(result.get("visuals", []) or [])
    existing_paths = {str(i.get("file_path")) for i in scene_images if i.get("file_path")}
    for asset in person_assets.values():
        path = str(asset.get("file_path"))
        if path not in existing_paths:
            scene_images.insert(0, dict(asset))
            scene_visuals.insert(0, dict(asset))
            existing_paths.add(path)
    result["images"] = scene_images
    result["visuals"] = scene_visuals

    for sentence_result in sentences:
        selected_person = next((p for p in sentence_result.get("story_people", []) or [] if p in person_assets), None)
        if not selected_person:
            continue
        selected_asset = person_assets[selected_person]
        identity_path = str(selected_asset.get("file_path"))
        sentence_images = list(sentence_result.get("images", []) or [])
        sentence_visuals = list(sentence_result.get("visuals", []) or [])
        if not any(str(i.get("file_path")) == identity_path for i in sentence_images):
            sentence_images.insert(0, dict(selected_asset))
        if not any(str(i.get("file_path")) == identity_path for i in sentence_visuals):
            sentence_visuals.insert(0, dict(selected_asset))
        sentence_result["images"] = sentence_images
        sentence_result["visuals"] = sentence_visuals
        sentence_result["identity_entity"] = selected_person
        sentence_result["identity_asset"] = dict(selected_asset)
        sentence_result["identity_locked"] = True

    result["person_assets"] = list(person_assets.values())
    result["identity_assets"] = list(person_assets.values())
    result["identity_engine"] = "STORY_BIBLE_ENTITIES"
    return result


# ==========================================================================
# UNIVERSAL SCENARIO VISUAL ENGINE
# ==========================================================================

def _uve_clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _uve_safe_filename(value, max_length=70):
    value = re.sub(r"[^A-Za-z0-9]+", "_", _uve_clean_text(value)).strip("_")
    return (value or "visual")[:max_length]


def _uve_candidate_key(candidate):
    if not isinstance(candidate, dict):
        return ""
    url = str(candidate.get("url") or candidate.get("video_url") or candidate.get("image_url") or candidate.get("link") or "").strip().lower()
    start = candidate.get("clip_start")
    if start is None and ("youtube.com" in url or "youtu.be" in url):
        start = _yt_segment_uses.get(url.split("&", 1)[0], 0)
    return f"{url}|{start}"


def _uve_sentence_number(sentence, fallback):
    return get_sentence_number(sentence, fallback)


def _uve_scenarios_for(sentence):
    if isinstance(sentence, dict) and sentence.get("scenarios"):
        return sentence["scenarios"]
    text = get_sentence_text(sentence)
    analysis = _analyze_current_text(text)
    queries = list(analysis.get("search_queries") or [])
    if not queries:
        return []
    return [{
        "visual": text[:80], "kind": "archival", "subject_id": None, "specificity": "representative",
        "search_terms": [], "event": None, "place": None, "era": None, "queries": queries[:3],
    }]
_DATE_WORDS = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december", "in"}
_SPORTS = ("nfl", "nba", "highlights", "touchdown", "goal", "soccer", "football", "basketball", "baseball", "espn")


def _real_subject(name):
    parts = [p for p in str(name or "").lower().split() if p not in _DATE_WORDS]
    return len(parts) >= 2 and not str(name or "").lower().startswith("in ")


def queries_from_shot_need(need):
    subject = str(need.get("required_subject") or "").strip()
    if not _real_subject(subject):
        subject = ""
    kind = str(need.get("required_type") or "").lower()
    place = str(need.get("place") or "").strip()
    description = str(need.get("description") or need.get("event") or "").strip()
    built = []
    if kind == "person" and subject:
        if place:
            built.append(f"{subject} {place}")
        built.append(f"{subject} interview")
        return built[:4]
    if description and subject:
        built.append(f"{description} {subject}"[:80])
    if description:
        built.append(description[:80])
    if subject and place:
        built.append(f"{subject} {place}")
    elif subject:
        built.append(subject)
    elif place:
        built.append(place)
    return [q for q in built if q][:4]

def providers_for_shot_need(need):
    kind = str(need.get("shot_type") or "")
    subject = str(need.get("required_subject") or "")
    if _real_subject(subject) or kind in {"person_activity", "event", "document", "archival"}:
        return ("youtube", "web")
    return ("pexels", "pixabay")
def _uve_scenario_search(scenario, search_cache, project_id, shot_need=None):
    need = shot_need or {}
    allow_people = scenario.get("kind") == "person_activity" or bool(scenario.get("subject_id") or need.get("required_subject_id"))
    queries = queries_from_shot_need(need) if need else list(scenario.get("queries") or [])[:3]
    providers = providers_for_shot_need(need) if need else ("pexels", "pixabay", "youtube")
    videos, images = [], []
    for query in queries[:3]:
        query = str(query).strip()
        if not query:
            continue
        cache_key = f"{project_id}|{query.lower()}|{int(allow_people)}|{'|'.join(providers)}"
        if cache_key not in search_cache:
            print(f"UNIVERSAL SEARCH: {query} providers={','.join(providers)}")
            video_hits, image_hits = [], []
            if "pexels" in providers or "pixabay" in providers:
                try:
                    video_hits = search_licensed_videos(query, limit=8, allow_people=allow_people) or []
                    video_hits = [h for h in video_hits if str(h.get("source") or "").lower() in providers or str(h.get("source") or "").lower() == "youtube"]
                except Exception as exc:
                    print(f"VIDEO SEARCH ERROR: {query}: {exc}")
            elif "youtube" in providers:
                try:
                    video_hits = search_youtube_videos(query, limit=5) or []
                except Exception as exc:
                    print(f"VIDEO SEARCH ERROR: {query}: {exc}")
            if "web" in providers or "pexels" in providers or "pixabay" in providers:
                try:
                    image_hits = search_licensed_images(query, limit=10, allow_people=allow_people) or []
                    if "web" in providers and "pexels" not in providers:
                        image_hits = [h for h in image_hits if str(h.get("source") or "").lower() not in {"pexels", "pixabay"}]
                except Exception as exc:
                    print(f"IMAGE SEARCH ERROR: {query}: {exc}")
            search_cache[cache_key] = (video_hits, image_hits)
        cached_videos, cached_images = search_cache[cache_key]
        for target, source in ((videos, cached_videos), (images, cached_images)):
            for item in source:
                if isinstance(item, dict):
                    candidate = dict(item)
                    candidate["_uve_query"] = query
                    candidate["matched_query"] = query
                    candidate["provider"] = str(candidate.get("source") or "")
                    candidate["shot_need"] = {
                        "required_subject": need.get("required_subject"),
                        "place": need.get("place"),
                        "era": need.get("era"),
                        "specificity": need.get("specificity"),
                    }
                    target.append(candidate)
    return videos, images


def _uve_verify_all(candidates, scenario, bible):
    scored, seen = [], set()
    for candidate in candidates:
        key = _uve_candidate_key(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        verdict = story.verify_candidate(candidate, scenario, bible)
        item = dict(candidate)
        item["verification"] = verdict
        item["universal_score"] = verdict["score"]
        item["relevance_score"] = verdict["score"]
        scored.append(item)
    scored.sort(key=lambda c: c["universal_score"], reverse=True)
    return scored


RELAXED_MIN_SCORE = 0.15

def _title_blob(candidate):
    return " ".join([
        str(candidate.get("title") or ""),
        str(candidate.get("snippet") or ""),
        str(candidate.get("matched_query") or ""),
    ]).lower()


def gate_shot_need(candidate, shot_need):
    need = shot_need or {}
    blob = _title_blob(candidate)
    subject = str(need.get("required_subject") or "").strip().lower()
    place = str(need.get("place") or "").strip().lower()
    kind = str(need.get("required_type") or "").lower()
    if any(word in blob for word in _SPORTS) and "sport" not in subject:
        return False, "unrelated_sports"
    if kind != "person" and any(
        word in blob for word in ("president", "prime minister", "flag ceremony", "election", "summit")
    ):
        return False, "unrelated_official"
    if not need or str(need.get("specificity") or "") != "exact":
        return True, ""
    for banned in need.get("forbidden") or []:
        if str(banned).strip() and str(banned).lower() in blob:
            return False, "forbidden_term"
    if subject and not _real_subject(subject):
        return False, "bad_subject"
    if subject and subject not in blob:
        parts = [p for p in subject.split() if len(p) > 2]
        if parts and not all(p in blob for p in parts):
            return False, "missing_subject"
    if place and place not in blob:
        return False, "missing_place"
    return True, ""
    
def _uve_pick(scored, scenario, registry, used_keys, limit=3, shot_need=None):
    pool = []
    for candidate in scored:
        ok, reason = gate_shot_need(candidate, shot_need)
        if not ok:
            candidate["reject_reason"] = reason
            print(f"      REJECT {reason}: {result_title(candidate)[:80]}")
            continue
        if (candidate.get("verification") or {}).get("accepted"):
            candidate["verification_status"] = "verified"
            pool.append(candidate)
    if not pool and str((shot_need or {}).get("specificity") or "") != "exact":
        for candidate in scored:
            if candidate.get("reject_reason"):
                continue
            if float(candidate.get("universal_score") or 0) >= 0.05:
                candidate["verification_status"] = "relaxed"
                pool.append(candidate)
    picked = []
    for candidate in pool:
        key = _uve_candidate_key(candidate)
        if not key or key in used_keys:
            continue
        try:
            allowed, why = registry.can_use(url=key, title=result_title(candidate))
        except Exception:
            allowed, why = True, ""
        if not allowed:
            candidate["reject_reason"] = why or "registry_block"
            continue
        item = dict(candidate)
        item["verification_score"] = item.get("universal_score")
        picked.append(item)
        if len(picked) >= limit:
            break
    return picked


def _uve_annotate(asset, scenario, scenario_index, sentence_text):
    asset.update({
        "scenario_index": scenario_index,
        "scenario_visual": scenario.get("visual", ""),
        "scenario_kind": scenario.get("kind", ""),
        "scenario_specificity": scenario.get("specificity", ""),
        "scenario_subject_id": scenario.get("subject_id"),
        "asset_scope": scenario.get("kind") or "context_broll",
        "visual_role": scenario.get("kind") or "context_broll",
        "matched_sentence": sentence_text,
        "rights_status": asset.get("rights_status", "verify_before_publish"),
    })
    return asset


_yt_segment_uses = {}


def _youtube_start(url):
    key = str(url or "").split("&", 1)[0]
    index = _yt_segment_uses.get(key, 0)
    _yt_segment_uses[key] = index + 1
    starts = (0, 20, 45, 75, 110)
    return starts[min(index, len(starts) - 1)]

_yt_segment_uses = {}


def _youtube_start(url, subject="", place=""):
    key = str(url or "").split("&", 1)[0]
    used = _yt_segment_uses.setdefault(key, [])
    words = [w.lower() for w in f"{subject} {place}".split() if len(w) > 2]
    chapters = []
    try:
        code, stdout, _ = run_command_safe(
            [sys.executable, "-m", "yt_dlp", "--no-warnings", "--dump-json", "--skip-download", key],
            40,
            "yt-dlp chapters",
        )
        if code == 0 and stdout:
            info = json.loads(stdout)
            for chapter in info.get("chapters") or []:
                title = str(chapter.get("title") or "").lower()
                start = int(float(chapter.get("start_time") or 0))
                if start in used:
                    continue
                score = sum(1 for word in words if word in title)
                chapters.append((score, start, title))
    except Exception as exc:
        print(f"CHAPTER LOOKUP FAILED: {exc}")
    chapters.sort(key=lambda row: (-row[0], row[1]))
    start = chapters[0][1] if chapters and chapters[0][0] > 0 else 0
    if start in used:
        start = 0
    used.append(start)
    print(f"YOUTUBE SEGMENT: {key} start={start}s matched={bool(chapters and chapters[0][0])}")
    return start

def _uve_download_video_candidate(candidate, scene_dir, sentence_number, asset_index):
    url = str(candidate.get("url") or candidate.get("video_url") or candidate.get("link") or "").strip()
    if not url:
        return None
    need = candidate.get("shot_need") or {}
    start = 0
    if "youtube.com" in url or "youtu.be" in url:
        start = _youtube_start(
            url,
            need.get("required_subject") or "",
            need.get("place") or "",
        )
    output_path = os.path.join(
        scene_dir,
        f"sentence_{sentence_number:04d}_video_{asset_index:02d}_{start}_{_uve_safe_filename(candidate.get('matched_query', 'video'))}.mp4",
    )
    try:
        if not validate_video(output_path) and not download_video_clip(url, output_path, start, 8):
            return None
    except Exception as exc:
        print(f"UNIVERSAL VIDEO DOWNLOAD FAILED: {exc}")
        return None
    if not validate_video(output_path):
        return None
    asset = dict(candidate)
    asset.update({
        "file_path": output_path, "path": output_path, "video_url": url, "url": url,
        "type": "video", "visual_type": "video", "duration_seconds": 8,
        "sentence_number": sentence_number, "clip_start": start,
    })
    return asset

def _uve_download_image_candidate(candidate, scene_dir, sentence_number, asset_index):
    url = str(candidate.get("image_url") or candidate.get("url") or candidate.get("link") or "").strip()
    if not url:
        return None
    if any(bad in url.lower() for bad in ("tiktok.com", "gannett-cdn.com")):
        return None

def _uve_download_image_candidate(candidate, scene_dir, sentence_number, asset_index):
    url = str(candidate.get("image_url") or candidate.get("url") or candidate.get("link") or "").strip()
    if not url:
        return None
    if any(bad in url.lower() for bad in ("tiktok.com", "gannett-cdn.com")):
        return None
    output_path = os.path.join(scene_dir, f"sentence_{sentence_number:04d}_image_{asset_index:02d}_{_uve_safe_filename(candidate.get('matched_query', 'image'))}.jpg")
    try:
        if not validate_image(output_path) and not download_valid_image(url, output_path):
            return None
    except Exception as exc:
        print(f"UNIVERSAL IMAGE DOWNLOAD FAILED: {exc}")
        return None
    if not validate_image(output_path):
        return None
    asset = dict(candidate)
    asset.update({"file_path": output_path, "path": output_path, "image_url": url, "url": url, "type": "image",
                  "visual_type": "image", "sentence_number": sentence_number})
    return asset


def _uve_fetch_first(candidates, downloader, scene_dir, sentence_number, asset_index, registry, used_keys):
    for candidate in candidates:
        key = _uve_candidate_key(candidate)
        if key in used_keys:
            continue
        asset = downloader(candidate, scene_dir, sentence_number, asset_index)
        if not asset:
            continue
        used_keys.add(key)
        try:
            registry.register(path=asset.get("file_path"), url=key, title=asset.get("title", ""))
        except Exception:
            pass
        return asset
    return None


def _uve_prepare_sentence_result(sentence, sentence_number, images, videos, story_people):
    sentence_text = get_sentence_text(sentence)
    visuals = []
    for asset in videos:
        item = dict(asset)
        item["visual_type"] = "video"
        visuals.append(item)
    for asset in images:
        item = dict(asset)
        item["visual_type"] = "image"
        visuals.append(item)
    plan = sentence if isinstance(sentence, dict) else {}
    return {
        "sentence_number": sentence_number,
        "sentence_text": sentence_text,
        "images": images, "videos": videos, "visuals": visuals,
        "sentence": sentence,
        "asset_scope": "beat_verified",
        "story_people": story_people,
        "meaning": plan.get("meaning", ""),
        "role": plan.get("role", ""),
        "visual_requirements": {
            "entities": plan.get("entities", []), "locations": plan.get("locations", []),
            "dates": plan.get("dates", []), "actions": plan.get("actions", []),
            "event": plan.get("event", ""), "visual_types": plan.get("visual_types", []),
            "keywords": plan.get("keywords", []), "queries": plan.get("search_queries", []),
        },
    }

def _write_text_card(scene_dir, beat, shot_need=None):
    need = shot_need or {}
    title = str(need.get("required_subject") or beat.get("label") or "Documentary").strip()
    place = str(need.get("place") or "").strip()
    era = str(need.get("era") or "").strip()
    subtitle = " ".join(part for part in (place, era) if part) or str(beat.get("text") or "")[:90]
    safe = _uve_safe_filename(title or "card")
    path = Path(scene_dir) / f"card_{safe}.jpg"
    image = Image.new("RGB", (1920, 1080), (18, 22, 28))
    try:
        from PIL import ImageDraw
        draw = ImageDraw.Draw(image)
        draw.text((120, 420), title[:80], fill=(245, 245, 245))
        draw.text((120, 520), subtitle[:110], fill=(180, 180, 180))
    except Exception:
        pass
    image.save(path, "JPEG", quality=90)
    return {
        "file_path": str(path),
        "path": str(path),
        "title": title,
        "type": "image",
        "visual_type": "image",
        "source": "text_card",
        "asset_scope": "fallback_card",
        "verification_status": "graphic_fallback",
        "reject_reason": "no_verified_clip",
    }

def _uve_process_scene_media(scene, project_dir, image_count=3, video_count=3):
    if not isinstance(scene, dict):
        scene = {}
    scene_number = scene.get("scene_number", 1)
    scene_dir = os.path.join(project_dir, f"scene_{scene_number}")
    os.makedirs(scene_dir, exist_ok=True)

    print()
    print("=" * 78)
    print(f"STORY-DRIVEN VISUAL ENGINE - SCENE {scene_number}")
    print("=" * 78)

    raw_sentences = get_sentence_list(scene)
    if not any(get_sentence_text(s) for s in raw_sentences):
        fallback = _uve_clean_text(scene.get("text") or scene.get("narration") or "")
        raw_sentences = [p.strip() for p in re.split(r"(?<=[.!?])\s+", fallback) if p.strip()]

    project_id = str(scene.get("project_id") or Path(project_dir).name)
    registry = get_registry(project_id)
    bible = _load_case_bible(scene, project_dir)
    beats = build_story_beats(raw_sentences, bible)
    write_beats_debug(beats, project_dir, scene_number=scene_number)
    print(f"BEATS: {len(beats)}")

    search_cache, used_keys = {}, set()
    all_images, all_videos, sentence_results = [], [], []
    results_by_number = {}

    for beat in beats:
        print()
        print("*" * 78)
        print(f"BEAT {beat['beat_id']}")
        print(f"  subject={beat.get('subject_id')} place={beat.get('place')} searchable={beat.get('searchable')}")
        print(f"  sentences={beat.get('sentence_numbers')}")
        for need in beat.get("shot_needs") or []:
            print(
                f"  NEED {need['shot_index']}: {need['shot_type']}/{need['specificity']} "
                f"subject={need.get('required_subject') or '-'} "
                f"place={need.get('place') or '-'} era={need.get('era') or '-'}"
            )
        print("*" * 78)

        scenarios = _beat_scenarios(beat)
        videos_left = max(1, int(video_count or 1))
        images_left = max(1, int(image_count or 1))
        beat_videos, beat_images = [], []
        review_records = []

        if not beat.get("searchable", True):
            print(f"  NO SEARCH (role={beat.get('role')})")
        else:
            for scenario_index, scenario in enumerate(scenarios, start=1):
                print(f"  SHOT {scenario_index}: [{scenario.get('specificity')}/{scenario.get('kind')}] {scenario.get('visual')}")
                queries = scenario.get("queries") or []
                for query in queries:
                    print(f"      query: {query}")
                if not queries:
                    print("      (no queries - skipped)")
                    continue

                shot_need = {}
                for need in beat.get("shot_needs") or []:
                    if need.get("shot_index") == scenario_index:
                        shot_need = need
                        break
                if shot_need.get("queries"):
                    scenario = dict(scenario)
                    scenario["queries"] = shot_need["queries"]
                print(f"      routed={','.join(providers_for_shot_need(shot_need)) if shot_need else 'stock'}")
                raw_videos, raw_images = _uve_scenario_search(scenario, search_cache, project_id, shot_need)
                scored_videos = _uve_verify_all(raw_videos, scenario, bible)
                scored_images = _uve_verify_all(raw_images, scenario, bible)
                print(f"      candidates: videos={len(scored_videos)} images={len(scored_images)}")

                if videos_left > 0:
                    picks = _uve_pick(scored_videos, scenario, registry, used_keys, shot_need=shot_need)
                    asset = _uve_fetch_first(
                        picks, _uve_download_video_candidate, scene_dir,
                        beat["sentence_numbers"][0], len(beat_videos) + 1, registry, used_keys,
                    )
                    if asset:
                        beat_videos.append(_uve_annotate(asset, scenario, scenario_index, beat.get("text", "")))
                        videos_left -= 1
                        print(f"      + video ({asset.get('verification_status')}, score {asset.get('universal_score')})")
                if images_left > 0:
                    picks = _uve_pick(scored_images, scenario, registry, used_keys, shot_need=shot_need)
                    asset = _uve_fetch_first(
                        picks, _uve_download_image_candidate, scene_dir,
                        beat["sentence_numbers"][0], len(beat_images) + 1, registry, used_keys,
                    )
                    if asset:
                        beat_images.append(_uve_annotate(asset, scenario, scenario_index, beat.get("text", "")))
                        images_left -= 1
                        print(f"      + image ({asset.get('verification_status')}, score {asset.get('universal_score')})")
                review_records.append({
                    "shot_index": scenario_index,
                    "queries": queries_from_shot_need(shot_need) if shot_need else queries,
                    "providers": list(providers_for_shot_need(shot_need)) if shot_need else [],
                    "rejected": [
                        {"title": result_title(item), "reason": item.get("reject_reason")}
                        for item in (scored_videos + scored_images)
                        if item.get("reject_reason")
                    ][:12],
                    "picked": [result_title(item) for item in beat_videos + beat_images][-2:],
                })

        if beat.get("searchable", True) and not beat_videos and not beat_images:
            need = (beat.get("shot_needs") or [{}])[0]
            if str(need.get("specificity") or "") == "exact" or not need:
                card = _write_text_card(scene_dir, beat, need)
                beat_images.append(card)
                print(f"  FALLBACK CARD: {card['title']}")

        write_review_log(project_dir, scene_number, beat, review_records)

        for offset, sentence in enumerate(beat.get("sentences") or []):
            sentence_number = beat["sentence_numbers"][offset]
            people = _people_for_sentence(sentence, bible)
            result = _uve_prepare_sentence_result(
                sentence, sentence_number,
                [dict(item) for item in beat_images],
                [dict(item) for item in beat_videos],
                people,
            )
            result["beat_id"] = beat["beat_id"]
            result["beat_searchable"] = beat.get("searchable", True)
            results_by_number[sentence_number] = result

    for sentence_index, sentence in enumerate(raw_sentences, start=1):
        sentence_number = _uve_sentence_number(sentence, sentence_index)
        if not get_sentence_text(sentence):
            continue
        result = results_by_number.get(sentence_number)
        if not result:
            result = _uve_prepare_sentence_result(
                sentence, sentence_number, [], [], _people_for_sentence(sentence, bible)
            )
        sentence_results.append(result)
        all_images.extend(result.get("images") or [])
        all_videos.extend(result.get("videos") or [])

    all_visuals = []
    seen_paths = set()
    for asset in all_videos + all_images:
        path = str(asset.get("file_path") or "")
        if path and path in seen_paths:
            continue
        if path:
            seen_paths.add(path)
        item = dict(asset)
        item["visual_type"] = asset.get("visual_type") or asset.get("type") or "image"
        all_visuals.append(item)

    print()
    print("=" * 78)
    print(f"ENGINE COMPLETE - SCENE {scene_number}: beats={len(beats)} videos={len(all_videos)} images={len(all_images)} sentences={len(sentence_results)}")
    print("=" * 78)
    try:
        registry_summary = registry.summary()
    except Exception:
        registry_summary = {}

    result = {
        "scene_number": scene_number,
        "images": all_images, "videos": all_videos, "visuals": all_visuals,
        "sentences": sentence_results, "scene": scene,
        "beats": [
            {
                "beat_id": beat["beat_id"],
                "subject_id": beat.get("subject_id"),
                "place": beat.get("place"),
                "searchable": beat.get("searchable", True),
                "sentence_numbers": beat.get("sentence_numbers"),
                "shot_needs": beat.get("shot_needs") or [],
            }
            for beat in beats
        ],
        "asset_engine": "STORY_DRIVEN_VISUAL_V2",
        "asset_scope": "beat_verified",
        "sentence_count": len(sentence_results),
        "visual_engine_version": "STORY_DRIVEN_VISUAL_V2_BEATS",
        "registry_summary": registry_summary,
        "case_bible": {
            "title": bible.get("title"),
            "story_type": bible.get("story_type"),
            "story_era": bible.get("story_era") or bible.get("time_period"),
            "tone": bible.get("tone"),
            "entity_ids": [e.get("id") for e in (bible.get("entities") or [])],
        },
    }
    result = _add_identity_assets_to_scene(result, scene, project_dir)
    return apply_review_decisions(result, project_dir)
def write_decision_template(project_dir, scene_number, beats):
    path = Path(project_dir) / f"scene_{scene_number}_decisions.json"
    if path.exists():
        return path
    rows = []
    for beat in beats or []:
        rows.append({
            "beat_id": beat.get("beat_id"),
            "approve": True,
            "swap_path": "",
        })
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"DECISIONS TEMPLATE: {path}")
    return path


def apply_review_decisions(result, project_dir):
    if not isinstance(result, dict):
        return result
    scene_number = result.get("scene_number", 1)
    path = Path(project_dir) / f"scene_{scene_number}_decisions.json"
    write_decision_template(project_dir, scene_number, result.get("beats") or [])
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return result
    by_id = {row.get("beat_id"): row for row in rows if isinstance(row, dict)}
    for sentence in result.get("sentences") or []:
        decision = by_id.get(sentence.get("beat_id")) or {}
        if decision.get("approve", True) and not decision.get("swap_path"):
            continue
        swap = str(decision.get("swap_path") or "").strip()
        if swap and os.path.exists(swap):
            for bucket in ("images", "videos", "visuals"):
                for asset in sentence.get(bucket) or []:
                    asset["file_path"] = swap
                    asset["path"] = swap
                    asset["verification_status"] = "swapped"
            continue
        sentence["images"] = []
        sentence["videos"] = []
        sentence["visuals"] = []
        sentence["rejected_by_review"] = True
    return result
def process_scene_media(scene, project_dir, image_count=3, video_count=3):
    return _uve_process_scene_media(scene, project_dir, image_count=image_count, video_count=video_count)