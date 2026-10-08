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



def download_image(image_url, output_path):
    response = requests.get(
        image_url,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    Path(output_path).write_bytes(response.content)

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
    if evidence_type in {EVIDENCE_LOCATION, EVIDENCE_ORGANIZATION, EVIDENCE_OBJECT, EVIDENCE_EVENT, EVIDENCE_DOCUMENT, EVIDENCE_ARCHIVAL}:
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

    banned = [
        str(x)
        for x in (
            bible.get("banned_terms")
            or bible.get("forbidden_terms")
            or []
        )
        if str(x).strip()
    ]

    era = str(
        bible.get("story_era")
        or bible.get("time_period")
        or ""
    ).strip()

    needs = []

    for index, scenario in enumerate(
        _beat_scenarios(beat),
        start=1,
    ):
        subject_id = (
            scenario.get("subject_id")
            or beat.get("subject_id")
            or ""
        )

        entity = _entity(
            bible,
            subject_id,
        )

        place = str(
            scenario.get("place")
            or beat.get("place")
            or entity.get("location")
            or ""
        ).strip()

        shot_era = str(
            scenario.get("era")
            or era
        ).strip()

        kind = str(
            scenario.get("kind")
            or "archival"
        )

        specificity = str(
            scenario.get("specificity")
            or "representative"
        )

        required_subject = str(
            scenario.get("subject")
            or entity.get("canonical")
            or ""
        ).strip()

        required_type = str(
            scenario.get("required_type")
            or entity.get("type")
            or kind
            or ""
        ).strip()

        event = str(
            scenario.get("event")
            or ""
        ).strip()

        visual_requirement = str(
            scenario.get("visual_requirement")
            or scenario.get("visual")
            or ""
        ).strip()

        description = str(
            scenario.get("visual")
            or ""
        ).strip()

        queries = list(
            scenario.get("queries")
            or []
        )

        needs.append({
            "beat_id": beat.get("beat_id"),
            "shot_index": index,
            "shot_type": kind,
            "specificity": specificity,
            "description": description,
            "required_subject_id": subject_id or None,
            "required_subject": required_subject,
            "required_type": required_type,
            "era": shot_era,
            "place": place,
            "event": event,
            "visual_requirement": visual_requirement,
            "forbidden": banned,
            "priority": (
                1
                if specificity == "exact"
                else (
                    2
                    if specificity == "representative"
                    else 3
                )
            ),
            "fallback": (
                "related_entity"
                if specificity == "exact"
                else "same_era_place"
            ),
            "queries": queries,
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


def _uve_scenarios_for(sentence):
    """
    Build the visual scenario contract from the already-analyzed
    sentence data.

    The LLM/story-understanding layer is the source of truth.
    This function must NOT re-interpret the raw sentence with
    heuristic entity/event extraction.
    """

    if not sentence:
        return []

    # ---------------------------------------------------------
    # Read the existing LLM scenario data.
    # ---------------------------------------------------------

    scenarios = (
        sentence.get("scenarios")
        or sentence.get("visual_scenarios")
        or []
    )

    if isinstance(scenarios, dict):
        scenarios = [scenarios]

    output = []

    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue

        subject = str(
            scenario.get("subject")
            or ""
        ).strip()

        subject_id = str(
            scenario.get("subject_id")
            or ""
        ).strip()

        event = str(
            scenario.get("event")
            or ""
        ).strip()

        place = str(
            scenario.get("place")
            or ""
        ).strip()

        era = str(
            scenario.get("era")
            or ""
        ).strip()

        specificity = str(
            scenario.get("specificity")
            or "representative"
        ).strip()

        kind = str(
            scenario.get("kind")
            or "archival"
        ).strip()

        visual_requirement = str(
            scenario.get("visual_requirement")
            or scenario.get("visual")
            or ""
        ).strip()

        queries = [
            str(q).strip()
            for q in (
                scenario.get("queries")
                or scenario.get("search_terms")
                or []
            )
            if str(q).strip()
        ]

        # -----------------------------------------------------
        # Preserve the LLM's entity type when available.
        # Never infer a subject from the first words of the
        # sentence.
        # -----------------------------------------------------

        required_type = str(
            scenario.get("required_type")
            or ""
        ).strip()

        if not required_type:
            entity_type = str(
                scenario.get("entity_type")
                or ""
            ).strip()

            if entity_type:
                required_type = entity_type
            elif event:
                required_type = "event"
            elif subject:
                required_type = "entity"
            else:
                required_type = kind

        # -----------------------------------------------------
        # De-duplicate queries without modifying their meaning.
        # -----------------------------------------------------

        clean_queries = []
        seen_queries = set()

        for query in queries:
            key = query.lower()

            if key in seen_queries:
                continue

            seen_queries.add(key)
            clean_queries.append(query)

        # -----------------------------------------------------
        # If the LLM supplied no queries, construct only from
        # already-known semantic fields.
        # -----------------------------------------------------

        if not clean_queries:
            candidates = []

            if subject and event:
                candidates.append(
                    f"{subject} {event}"
                )

            if subject and place:
                candidates.append(
                    f"{subject} {place}"
                )

            if subject and era:
                candidates.append(
                    f"{subject} {era}"
                )

            if subject:
                candidates.append(subject)

            if event and place:
                candidates.append(
                    f"{event} {place}"
                )

            if event:
                candidates.append(event)

            if place and era:
                candidates.append(
                    f"{place} {era}"
                )

            for query in candidates:
                query = " ".join(
                    str(query).split()
                ).strip()

                if not query:
                    continue

                key = query.lower()

                if key in seen_queries:
                    continue

                seen_queries.add(key)
                clean_queries.append(query)

        if not subject and not event and not place and not clean_queries:
            continue

        output.append({
            "visual": str(
                scenario.get("visual")
                or visual_requirement
                or ""
            ).strip(),

            "kind": kind,

            "subject_id": (
                subject_id
                or None
            ),

            "specificity": specificity,

            "search_terms": [
                str(x).strip()
                for x in (
                    scenario.get("search_terms")
                    or []
                )
                if str(x).strip()
            ],

            "event": event,

            "place": place,

            "era": era,

            "subject": subject,

            "required_type": required_type,

            "visual_requirement": visual_requirement,

            "entities": (
                scenario.get("entities")
                or []
            ),

            "locations": (
                scenario.get("locations")
                or ([place] if place else [])
            ),

            "dates": (
                scenario.get("dates")
                or ([era] if era else [])
            ),

            "objects": (
                scenario.get("objects")
                or []
            ),

            "actions": (
                scenario.get("actions")
                or []
            ),

            "queries": clean_queries[:6],
        })

    return output
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

def _web_image_hit(url, title, source):
    return {"image_url": url, "url": url, "link": url, "title": title,
            "source": source, "license": "royalty-free", "snippet": title}


def _youtube_video_hit(url, title, source):
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


def search_web_sources(query, limit=8):
    from services.search_provider import search_web

    query = str(query or "").strip()
    if not query:
        return []

    try:
        results = search_web(query)
    except Exception as error:
        print(f"Web source search failed: {error}")
        return []

    output = []

    for item in results[:limit]:
        url = str(item.get("url") or "").strip()
        if not url:
            continue

        output.append({
            "title": item.get("title", ""),
            "url": url,
            "link": url,
            "snippet": item.get("snippet", ""),
            "source": "web",
            "rights_status": "verify_before_publish",
            "matched_query": query,
        })

    return output


def search_youtube_videos(query, limit=5):
    query = str(query or "").strip()
    if not query:
        return []

    from services.search_provider import search_videos

    results = []
    seen = set()

    try:
        hits = search_videos(query, limit=max(limit * 2, 10))
    except Exception as error:
        print(f"YouTube video search failed: {error}")
        return []

    for hit in hits:
        url = str(
            hit.get("video_url")
            or hit.get("url")
            or hit.get("link")
            or ""
        ).strip()

        if "youtube.com/watch" not in url and "youtu.be/" not in url:
            continue

        if "&" in url:
            url = url.split("&", 1)[0]

        if url in seen:
            continue

        seen.add(url)

        results.append({
            "video_url": url,
            "url": url,
            "link": url,
            "title": hit.get("title", ""),
            "source": "youtube",
            "license": "unknown",
            "rights_status": "verify_before_publish",
            "snippet": hit.get("snippet", ""),
            "date": hit.get("date", ""),
            "duration": hit.get("duration", ""),
            "matched_query": query,
        })

        if len(results) >= limit:
            break

    return results


def search_source_videos(query, limit=5):
    return search_youtube_videos(query, limit=limit)


def _slug_title(page_url, fallback):
    match = re.search(r"/video/(.+?)-\d+/?$", str(page_url or ""))
    return match.group(1).replace("-", " ") if match else fallback





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
        code, _, stderr = run_command_safe(command, FFMPEG_TIMEOUT, "FFmpeg source")
        try:
            raw_path.unlink()
        except OSError:
            pass
        if code != 0 or not validate_video(output_path):
            print((stderr or "")[-1000:])
            return False
        return True
    except Exception as error:
        print(f"Source video download failed: {error}")
        try:
            if raw_path.exists():
                raw_path.unlink()
        except OSError:
            pass
        return False


def download_video_clip(
    video_url,
    output_path,
    start_time=0,
    duration=DEFAULT_CLIP_DURATION,
):
    """
    Download one YouTube segment.

    Segment selection happens upstream.
    This function handles reliable extraction and
    final 1080p normalization.
    """

    output_path = Path(output_path)
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if is_direct_media_url(video_url):
        return download_direct_video(
            video_url,
            output_path,
            duration,
        )

    try:
        if output_path.exists():
            output_path.unlink()
    except OSError:
        pass

    cleanup_temp_files(output_path.parent)

    section_end = float(start_time) + float(duration)

    output_template = str(
        output_path.with_name(
            output_path.stem + "_source.%(ext)s"
        )
    ).replace("\\", "/")

    command = [
        sys.executable,
        "-m",
        "yt_dlp",

        "--no-playlist",
        "--no-warnings",
        "--restrict-filenames",

        "--retries", "5",
        "--fragment-retries", "5",
        "--retry-sleep", "1",
        "--socket-timeout", "30",

        # Use yt-dlp's normal YouTube client handling.
        # Avoid forcing a single client such as VISIONOS.

        # Prefer downloadable MP4-compatible formats.
        "-f",
        "bv*[height<=720]+ba/"
        "bv*[height<=720]/"
        "b[height<=720]/"
        "best",

        "--download-sections",
        f"*{start_time}-{section_end}",

        "--force-keyframes-at-cuts",

        "--no-part",

        "-o",
        output_template,

        video_url,
    ]

    print(
        f"YOUTUBE CLIP EXTRACTION: "
        f"{video_url} "
        f"start={start_time}s duration={duration}s"
    )

    returncode, stdout, stderr = run_command_safe(
        command,
        180,
        "yt-dlp",
    )

    if returncode != 0:
        print("yt-dlp clip extraction failed:")
        print((stderr or "")[-3000:])

        cleanup_temp_files(output_path.parent)

        return False

    candidates = [
        item
        for item in output_path.parent.glob(
            f"{output_path.stem}_source.*"
        )
        if (
            item.is_file()
            and item.suffix.lower()
            not in TEMP_FILE_SUFFIXES
        )
    ]

    if not candidates:
        print(
            "yt-dlp completed but no "
            "source clip was found."
        )

        cleanup_temp_files(output_path.parent)

        return False

    candidates.sort(
        key=lambda item: -item.stat().st_size
    )

    source_clip = candidates[0]

    ffmpeg_command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_clip),
        "-t",
        str(duration),
        "-an",
        "-vf",
        "scale=1920:1080:"
        "force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)",
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    returncode, stdout, stderr = run_command_safe(
        ffmpeg_command,
        FFMPEG_TIMEOUT,
        "FFmpeg YouTube",
    )

    try:
        source_clip.unlink()
    except OSError:
        pass

    cleanup_temp_files(output_path.parent)

    if (
        returncode != 0
        or not validate_video(output_path)
    ):
        print(
            "YouTube clip normalization failed."
        )

        if stderr:
            print((stderr or "")[-2000:])

        try:
            if output_path.exists():
                output_path.unlink()
        except OSError:
            pass

        return False

    print(
        f"VALID YOUTUBE VIDEO CLIP: "
        f"{output_path}"
    )

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

# ==========================================================================

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


def _uve_clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _uve_safe_filename(value, max_length=70):
    value = re.sub(r"[^A-Za-z0-9]+", "_", _uve_clean_text(value)).strip("_")
    return (value or "visual")[:max_length]


_yt_segment_uses = {}

_youtube_metadata_cache = {}
_youtube_transcript_cache = {}

# =====================================================================
# YouTube source intelligence: ONE yt-dlp call per URL, persistent cache.
# (added by patch_youtube_info.py)
# =====================================================================
import threading as _threading
import time as _time
from urllib.error import HTTPError as _HTTPError

try:
    from services import source_cache
except ImportError:  # flat-layout fallback
    import source_cache

YT_INFO_TIMEOUT = 40
_yt_info_failed = set()
_yt_transcript_failed = set()
_yt_info_locks = {}
_yt_info_guard = _threading.Lock()


def _youtube_info(key):
    """
    Full yt-dlp info for a URL. One subprocess per URL per process, and
    the useful parts (duration, title, description, chapters) are written
    to the persistent source profile so later runs need no yt-dlp call.
    """
    info = _youtube_metadata_cache.get(key)
    if isinstance(info, dict) and info.get("_full"):
        return info

    if key in _yt_info_failed:
        return None

    with _yt_info_guard:
        lock = _yt_info_locks.setdefault(key, _threading.Lock())

    with lock:
        info = _youtube_metadata_cache.get(key)
        if isinstance(info, dict) and info.get("_full"):
            return info

        if source_cache.yt_breaker_open():
            print(
                f"YOUTUBE INFO SKIPPED (429 cooldown "
                f"{source_cache.yt_breaker_remaining():.0f}s): {key}"
            )
            return None

        source_cache.bump("ytdlp_calls")
        started = _time.time()

        try:
            code, stdout, stderr = run_command_safe(
                [
                    sys.executable,
                    "-m",
                    "yt_dlp",
                    "--no-warnings",
                    "--dump-single-json",
                    "--skip-download",
                    key,
                ],
                YT_INFO_TIMEOUT,
                "yt-dlp info",
            )
        except Exception as exc:
            print(f"YOUTUBE INFO FAILED: {key}: {exc}")
            _yt_info_failed.add(key)
            return None

        elapsed = _time.time() - started

        if code != 0 or not stdout:
            if "429" in str(stderr or ""):
                source_cache.yt_breaker_trip(90)
                print(f"YOUTUBE INFO 429: {key} ({elapsed:.1f}s) - pausing YouTube calls")
            else:
                _yt_info_failed.add(key)
                print(f"YOUTUBE INFO FAILED: {key} ({elapsed:.1f}s)")
            return None

        try:
            info = json.loads(stdout)
        except Exception as exc:
            print(f"YOUTUBE INFO PARSE FAILED: {key}: {exc}")
            _yt_info_failed.add(key)
            return None

        info["_full"] = True
        _youtube_metadata_cache[key] = info

        source_cache.profile_upsert(
            key,
            status="ok",
            duration=info.get("duration"),
            title=str(info.get("title") or ""),
            channel=str(info.get("channel") or info.get("uploader") or ""),
            description=str(info.get("description") or "")[:4000],
            chapters=info.get("chapters") or [],
        )

        print(f"YOUTUBE INFO: {key} ({elapsed:.1f}s)")
        return info


def _youtube_slim(key):
    """duration / chapters / title / description without a yt-dlp call
    when the URL was analysed before (this run or an earlier one)."""
    info = _youtube_metadata_cache.get(key)

    if not (isinstance(info, dict) and info.get("_full")):
        persisted = source_cache.profile_get(key)

        if persisted and persisted.get("duration"):
            return {
                "duration": persisted["duration"],
                "chapters": persisted.get("chapters") or [],
                "title": persisted.get("title") or "",
                "description": persisted.get("description") or "",
            }

        info = _youtube_info(key)

    if not info:
        return None

    return {
        "duration": info.get("duration"),
        "chapters": info.get("chapters") or [],
        "title": info.get("title") or "",
        "description": info.get("description") or "",
    }


def _youtube_transcript_segment(
    url,
    subject="",
    place="",
    event="",
    era="",
):
    """
    Find a relevant timestamp inside a YouTube automatic transcript.

    Evidence is evaluated across a short transcript window rather
    than one caption line. This is important because documentary
    narration often introduces the subject, location, and event
    across several consecutive sentences.

    This function is intentionally niche-agnostic.
    """

    key = str(url or "").split("&", 1)[0].strip()

    if not key:
        return None

    def normalize(value):
        value = " ".join(
            str(value or "").split()
        ).strip().lower()

        value = re.sub(
            r"^(the|a|an)\s+",
            "",
            value,
            flags=re.IGNORECASE,
        )

        return value

    subject = normalize(subject)
    place = normalize(place)
    event = normalize(event)
    era = normalize(era)

    terms = [
        value
        for value in (
            subject,
            place,
            event,
            era,
        )
        if value
    ]

    if not terms:
        return None

    # ---------------------------------------------------------
    # Transcript: memory cache -> persistent cache -> one download.
    # Failures (no captions, HTTP 429) are cached, never retried
    # per shot.
    # ---------------------------------------------------------

    transcript = _youtube_transcript_cache.get(key)

    if transcript is None:
        if key in _yt_transcript_failed:
            return None

        persisted = source_cache.profile_get(key)

        if persisted and persisted.get("transcript"):
            transcript = persisted["transcript"]
            _youtube_transcript_cache[key] = transcript
        elif persisted and persisted.get("transcript_status") in (
            "unavailable",
            "rate_limited",
        ):
            print(
                f"YOUTUBE TRANSCRIPT SKIPPED "
                f"(cached {persisted['transcript_status']}): {key}"
            )
            return None

    if transcript is None:
        info = _youtube_info(key)

        if not info:
            return None

        manual = info.get("subtitles") or {}
        auto = info.get("automatic_captions") or {}

        tracks = (
            manual.get("en")
            or auto.get("en")
            or auto.get("en-orig")
            or []
        )

        json_track = next(
            (
                track
                for track in tracks
                if str(track.get("ext") or "").lower() == "json3"
            ),
            None,
        )

        caption_url = str(
            (json_track or {}).get("url") or ""
        ).strip()

        if not caption_url:
            print(
                f"YOUTUBE TRANSCRIPT UNAVAILABLE: {key}"
            )
            _yt_transcript_failed.add(key)
            source_cache.profile_upsert(
                key,
                transcript_status="unavailable",
                fail_reason="no english json3 caption track",
            )
            return None

        try:
            from urllib.request import Request, urlopen

            request = Request(
                caption_url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                },
            )

            source_cache.bump("transcript_downloads")

            with urlopen(
                request,
                timeout=30,
            ) as response:
                raw = response.read()

            transcript = json.loads(
                raw.decode(
                    "utf-8",
                    errors="replace",
                )
            )

            _youtube_transcript_cache[key] = transcript

            source_cache.profile_upsert(
                key,
                transcript_status="ok",
                transcript=transcript,
            )

        except _HTTPError as exc:
            if exc.code == 429:
                source_cache.yt_breaker_trip(90)
                source_cache.profile_upsert(
                    key,
                    transcript_status="rate_limited",
                    fail_reason="HTTP 429",
                    retry_after=_time.time() + 600,
                )

            print(
                f"YOUTUBE TRANSCRIPT DOWNLOAD FAILED: "
                f"{exc}"
            )
            _yt_transcript_failed.add(key)
            return None

        except Exception as exc:
            print(
                f"YOUTUBE TRANSCRIPT DOWNLOAD FAILED: "
                f"{exc}"
            )
            _yt_transcript_failed.add(key)
            return None

    info = _youtube_metadata_cache.get(key) or {}

    events = transcript.get("events") or []

    passages = []

    for event_item in events:
        segs = event_item.get("segs") or []

        if not segs:
            continue

        text_parts = [
            str(seg.get("utf8") or "")
            for seg in segs
        ]

        passage_text = " ".join(
            part.strip()
            for part in text_parts
            if part.strip()
        ).strip()

        if not passage_text:
            continue

        try:
            start_time = (
                float(
                    event_item.get("tStartMs")
                    or 0
                )
                / 1000.0
            )
        except (TypeError, ValueError):
            continue

        passages.append(
            {
                "start": start_time,
                "text": passage_text,
            }
        )

    if not passages:
        return None

    # ---------------------------------------------------------
    # Build context windows.
    #
    # Each candidate passage gets surrounding transcript
    # context. This allows evidence to appear across several
    # consecutive captions instead of requiring everything
    # to occur in one subtitle line.
    # ---------------------------------------------------------

    window_seconds = 30

    candidates = []

    for index, passage in enumerate(passages):
        start_time = passage["start"]
        window_end = (
            start_time
            + window_seconds
        )

        window_items = []

        for later in passages[index:]:
            if later["start"] > window_end:
                break

            window_items.append(later)

        if not window_items:
            continue

        context_text = " ".join(
            item["text"]
            for item in window_items
        )

        context_normalized = normalize(
            context_text
        )

        if not context_normalized:
            continue

        score = 0
        matched = []

        for term in terms:
            if term in context_normalized:
                score += 10
                matched.append(term)
                continue

            words = [
                word
                for word in term.split()
                if len(word) >= 3
            ]

            if not words:
                continue

            matches = sum(
                1
                for word in words
                if word in context_normalized
            )

            if matches:
                score += matches * 2
                matched.append(term)

        # -----------------------------------------------------
        # Generic event-language support.
        #
        # Documentary narration often expresses an event
        # indirectly:
        #
        # disappearance -> missing / disappeared / vanished
        # death         -> died / killed / dead
        # launch        -> launched / launch
        # landing       -> landed / landing
        #
        # This is intentionally generic and not tied to any
        # particular documentary subject.
        # -----------------------------------------------------

        event_aliases = {
            "disappearance": (
                "disappeared",
                "disappear",
                "missing",
                "vanished",
                "last seen",
            ),
            "death": (
                "died",
                "dead",
                "killed",
                "murdered",
            ),
            "launch": (
                "launched",
                "launching",
                "launch",
            ),
            "landing": (
                "landed",
                "landing",
            ),
            "flood": (
                "flooded",
                "flooding",
                "flood",
            ),
            "arrest": (
                "arrested",
                "arrest",
                "detained",
            ),
        }

        normalized_event = event

        aliases = event_aliases.get(
            normalized_event,
            (),
        )

        alias_matches = [
            alias
            for alias in aliases
            if alias in context_normalized
        ]

        if alias_matches:
            score += 6
            matched.extend(
                alias_matches[:3]
            )

        # -----------------------------------------------------
        # Strong exact-contract rule.
        #
        # An exact subject/location contract requires the
        # subject OR its meaningful words plus the location.
        # Event aliases can satisfy the event component.
        # -----------------------------------------------------

        # -----------------------------------------------------
        # Primary evidence is the subject.
        #
        # Supporting fields such as place or era do not always
        # appear in the same narration sentence. A documentary
        # may introduce a person first and explain the location
        # several sentences later.
        # -----------------------------------------------------

        if subject:
            subject_present = (
                subject in context_normalized
                or all(
                    word in context_normalized
                    for word in subject.split()
                    if len(word) >= 3
                )
            )

            if not subject_present:
                continue

        # -----------------------------------------------------
        # For an event contract, require either the exact event
        # or a recognized event-language equivalent.
        # -----------------------------------------------------

        if event:
            event_present = (
                event in context_normalized
                or bool(alias_matches)
                or all(
                    word in context_normalized
                    for word in event.split()
                    if len(word) >= 3
                )
            )

            if not event_present:
                continue

        # -----------------------------------------------------
        # Location is supporting evidence.
        #
        # If it is present, reward it. If it is absent from this
        # particular 30-second narration window, do not throw
        # away otherwise strong subject/event evidence.
        # -----------------------------------------------------

        if place:
            place_present = (
                place in context_normalized
                or all(
                    word in context_normalized
                    for word in place.split()
                    if len(word) >= 3
                )
            )

            if place_present:
                score += 5
                matched.append(
                    f"place:{place}"
                )

        # -----------------------------------------------------
        # Evidence-quality bonuses.
        #
        # Prefer windows where the primary subject and event
        # appear close together. This is stronger evidence than
        # a window that merely mentions the subject while
        # discussing an unrelated part of the story.
        # -----------------------------------------------------

        subject_positions = []
        event_positions = []

        if subject:
            position = context_normalized.find(subject)

            if position >= 0:
                subject_positions.append(position)

        if event:
            position = context_normalized.find(event)

            if position >= 0:
                event_positions.append(position)

        for alias in aliases:
            position = context_normalized.find(alias)

            if position >= 0:
                event_positions.append(position)

        if subject_positions and event_positions:
            distance = min(
                abs(
                    subject_position
                    - event_position
                )
                for subject_position in subject_positions
                for event_position in event_positions
            )

            if distance <= 160:
                score += 12
            elif distance <= 400:
                score += 6

        # Exact subject phrase is stronger than scattered
        # individual-name matches.
        if subject and subject in context_normalized:
            score += 5

        # Prefer direct event wording over a weak alias.
        if event and event in context_normalized:
            score += 5

        if score <= 0:
            continue

        candidates.append(
            {
                "score": score,
                "start": start_time,
                "text": context_text,
                "matched": matched,
            }
        )

    if not candidates:
        print(
            f"YOUTUBE TRANSCRIPT NO MATCH: "
            f"{key} terms={terms}"
        )
        return None

    candidates.sort(
        key=lambda item: (
            -item["score"],
            item["start"],
        )
    )

    # Keep a broad pool of strong transcript regions. The caller
    # may request multiple clips from the same YouTube source,
    # so we must not collapse the entire video down to one
    # timestamp.
    candidates = candidates[:40]

    used = _yt_segment_uses.setdefault(
        key,
        [],
    )

    duration = int(
        DEFAULT_CLIP_DURATION
    )

    # Keep clips from the same long source visually separated.
    # This prevents three consecutive clips from becoming one
    # repeated scene broken into 8-second pieces.
    minimum_gap = max(
        duration * 3,
        24,
    )

    for candidate in candidates:
        start_time = int(
            max(0, candidate["start"])
        )

        end_time = (
            start_time
            + duration
        )

        overlaps = any(
            start_time < used_end
            and end_time > used_start
            for used_start, used_end in used
        )

        too_close = any(
            abs(
                start_time
                - used_start
            ) < minimum_gap
            for used_start, used_end in used
        )

        if overlaps or too_close:
            continue

        used.append(
            (
                start_time,
                end_time,
            )
        )

        print(
            f"YOUTUBE TRANSCRIPT SEGMENT: "
            f"{key} "
            f"start={start_time}s "
            f"score={candidate['score']} "
            f"matched={candidate['matched']} "
            f"text='{candidate['text'][:240]}'"
        )

        return start_time

    print(
        f"YOUTUBE TRANSCRIPT EXHAUSTED: "
        f"{key}"
    )

    return None


# =====================================================================
# Evidence-based timestamp fallback helpers.
# (added by patch_youtube_fallback.py)
# =====================================================================
# Set False to reject sources instead of ever using a low-confidence window.
YT_LOW_CONFIDENCE_FALLBACK = True
# Fractions of the video (0..1) tried, in order, for low-confidence windows.
# The intro (first 20%) and outro (last 20%) are skipped on purpose.
YT_LOW_CONFIDENCE_WINDOWS = (0.2, 0.4, 0.6, 0.8)

_YT_TS_LINE = re.compile(
    r"^\s*[-*\u2022]?\s*\(?((?:\d{1,2}:)?\d{1,2}:\d{2})\)?\s*[-:.)\u2013\u2014]*\s*(.+?)\s*$"
)


def _yt_norm_text(value):
    return " ".join(str(value or "").split()).strip().lower()


def _yt_term_score(text, terms):
    """Same scoring idea as the chapter scorer: exact term +10,
    individual words +2 each."""
    text = _yt_norm_text(text)
    score = 0
    matched = []

    for term in terms:
        if not term:
            continue

        if term in text:
            score += 10
            matched.append(term)
            continue

        words = [w for w in term.split() if len(w) >= 3]
        hits = sum(1 for w in words if w in text)

        if words and hits:
            score += hits * 2
            matched.append(term)

    return score, matched


def _yt_phrase_present(text, phrase):
    text = _yt_norm_text(text)
    phrase = _yt_norm_text(phrase)

    if not phrase:
        return False

    if phrase in text:
        return True

    words = [w for w in phrase.split() if len(w) >= 3]

    return bool(words) and all(w in text for w in words)


def _youtube_description_moments(description):
    """Parse '12:34 Something happens' lines -> [(seconds, text), ...]."""
    moments = []

    for line in str(description or "").splitlines():
        match = _YT_TS_LINE.match(line)

        if not match:
            continue

        seconds = 0

        for part in match.group(1).split(":"):
            seconds = seconds * 60 + int(part)

        moments.append((seconds, match.group(2)))

    return moments


def _youtube_start(
    url,
    subject="",
    place="",
    event="",
    era="",
):
    """
    Choose a safe, relevant, non-overlapping segment from a YouTube source.

    This function is intentionally niche-agnostic.
    It does not know about specific people, places, events, industries,
    historical periods, or documentary topics.

    Relevance is determined only from the supplied visual contract:
        subject
        place
        event
        era

    Returns:
        int  -> valid clip start time
        None -> no suitable unused segment was found
    """

    key = str(url or "").split("&", 1)[0].strip()

    if not key:
        return None

    used = _yt_segment_uses.setdefault(key, [])
    duration = int(DEFAULT_CLIP_DURATION)

    # ---------------------------------------------------------
    # Normalize contract terms.
    # ---------------------------------------------------------

    def normalize(value):
        value = " ".join(
            str(value or "").split()
        ).strip().lower()

        value = re.sub(
            r"^(the|a|an)\s+",
            "",
            value,
            flags=re.IGNORECASE,
        )

        return value.strip()

    subject = normalize(subject)
    place = normalize(place)
    event = normalize(event)
    era = normalize(era)

    contract_terms = []

    for value in (
        subject,
        place,
        event,
        era,
    ):
        if value and value not in contract_terms:
            contract_terms.append(value)

    # ---------------------------------------------------------
    # Get the real YouTube duration.
    # ---------------------------------------------------------

    slim = _youtube_slim(key)

    video_duration = None

    if slim and slim.get("duration"):
        try:
            video_duration = int(float(slim["duration"]))
        except (TypeError, ValueError):
            video_duration = None

    # Chapters come from the same single lookup as duration.
    chapters = list((slim or {}).get("chapters") or [])

    if not video_duration or video_duration <= 0:
        print(
            f"YOUTUBE SEGMENT REJECTED: "
            f"unknown duration: {key}"
        )
        return None

    max_start = max(
        0,
        video_duration - duration,
    )

    print(
        f"YOUTUBE SOURCE DURATION: "
        f"{key} duration={video_duration}s "
        f"max_start={max_start}s"
    )

    # ---------------------------------------------------------
    # Segment overlap protection.
    # ---------------------------------------------------------

    def overlaps(start_time):
        end_time = start_time + duration

        for used_start, used_end in used:
            if (
                start_time < used_end
                and end_time > used_start
            ):
                return True

        return False

    def valid_start(start_time):
        try:
            start_time = int(start_time)
        except (TypeError, ValueError):
            return False

        if start_time < 0:
            return False

        if start_time > max_start:
            return False

        if overlaps(start_time):
            return False

        return True

    def register(start_time):
        used.append(
            (
                int(start_time),
                int(start_time) + duration,
            )
        )

    # ---------------------------------------------------------
    # Read YouTube chapters.
    # ---------------------------------------------------------

    # Chapters were loaded above together with the duration
    # (one yt-dlp call per URL, persisted).

    scored_chapters = []

    for chapter in chapters:
        title = str(
            chapter.get("title") or ""
        ).strip()

        if not title:
            continue

        try:
            start_time = int(
                float(
                    chapter.get("start_time") or 0
                )
            )
        except (TypeError, ValueError):
            continue

        if not valid_start(start_time):
            continue

        title_normalized = normalize(title)

        # -------------------------------------------------
        # Generic contract scoring.
        #
        # Exact multi-word terms are strongest.
        # Individual words provide secondary matching.
        # -------------------------------------------------

        score = 0
        matched_terms = []

        for term in contract_terms:
            if not term:
                continue

            if term in title_normalized:
                score += 10
                matched_terms.append(term)
                continue

            words = [
                word
                for word in term.split()
                if len(word) >= 3
            ]

            word_matches = sum(
                1
                for word in words
                if word in title_normalized
            )

            if words and word_matches:
                score += word_matches * 2
                matched_terms.append(term)

        scored_chapters.append({
            "score": score,
            "start": start_time,
            "title": title,
            "matched_terms": matched_terms,
        })

    chapters = scored_chapters

    chapters.sort(
        key=lambda item: (
            -item["score"],
            item["start"],
        )
    )

    # ---------------------------------------------------------
    # Strict contract:
    #
    # If we know what the shot is supposed to show,
    # do NOT grab an arbitrary chapter just because the
    # source video itself looked relevant.
    # ---------------------------------------------------------

    strict_contract = bool(
        subject
        or place
        or event
        or era
    )

    for chapter in chapters:
        if chapter["score"] <= 0:
            continue

        start_time = chapter["start"]

        if not valid_start(start_time):
            continue

        register(start_time)

        print(
            f"YOUTUBE SEGMENT: {key} "
            f"start={start_time}s "
            f"end={start_time + duration}s "
            f"chapter='{chapter['title']}' "
            f"score={chapter['score']} "
            f"matched={chapter['matched_terms']}"
        )

        return start_time

    # ---------------------------------------------------------
    # No matching chapter.
    #
    # Try the timed YouTube transcript before rejecting the
    # source. This allows long videos without chapters to yield
    # the exact section where the requested subject/event/place
    # is discussed.
    # ---------------------------------------------------------

    transcript_start = _youtube_transcript_segment(
        key,
        subject=subject,
        place=place,
        event=event,
        era=era,
    )

    if transcript_start is not None:
        # _youtube_transcript_segment() already registered
        # the range in _yt_segment_uses.
        print(
            f"YOUTUBE SEGMENT: {key} "
            f"start={transcript_start}s "
            f"end={transcript_start + duration}s "
            f"source=transcript"
        )

        return transcript_start

    # ---------------------------------------------------------
    # No exact chapter or transcript match.
    #
    # IMPORTANT:
    # The YouTube source has already passed the upstream
    # search + verification gates. Therefore, when this is
    # the same long source being reused for another shot,
    # do not reject it simply because the next segment does
    # not contain the exact contract words in its transcript.
    #
    # First try another unused chapter. Chapters are preferred
    # because they represent meaningful sections of the source.
    # ---------------------------------------------------------

    if strict_contract:
        # -----------------------------------------------------
        # Evidence-based fallback.
        #
        # Replaces "any unused chapter, then the first free slot
        # from 0". A segment is only chosen when there is a reason:
        #
        #   1. a timestamp listed in the video description matches
        #      the contract
        #   2. low-confidence spaced window, only when the source
        #      itself is about the most specific contract term
        #
        # Otherwise the source is rejected for this shot and the
        # caller can try a better source.
        # -----------------------------------------------------

        source_title = str((slim or {}).get("title") or "")
        source_description = str((slim or {}).get("description") or "")

        description_hits = []

        for moment_start, moment_text in _youtube_description_moments(
            source_description
        ):
            moment_score, moment_matched = _yt_term_score(
                moment_text,
                contract_terms,
            )

            if moment_score > 0:
                description_hits.append(
                    (moment_score, moment_start, moment_text, moment_matched)
                )

        description_hits.sort(
            key=lambda item: (-item[0], item[1])
        )

        for moment_score, moment_start, moment_text, moment_matched in description_hits:
            if not valid_start(moment_start):
                continue

            register(moment_start)

            print(
                f"YOUTUBE SEGMENT: {key} "
                f"start={moment_start}s "
                f"end={moment_start + duration}s "
                f"source=description "
                f"line='{moment_text[:80]}' "
                f"score={moment_score} "
                f"matched={moment_matched}"
            )

            return moment_start

        most_specific = event or place or subject

        source_has_evidence = _yt_phrase_present(
            f"{source_title} {source_description[:1500]}",
            most_specific,
        )

        if YT_LOW_CONFIDENCE_FALLBACK and source_has_evidence:
            minimum_gap = max(duration * 3, 24)

            for fraction in YT_LOW_CONFIDENCE_WINDOWS:
                candidate = int(max_start * fraction)

                if not valid_start(candidate):
                    continue

                if any(
                    abs(candidate - used_start) < minimum_gap
                    for used_start, _used_end in used
                ):
                    continue

                register(candidate)

                print(
                    f"YOUTUBE SEGMENT: {key} "
                    f"start={candidate}s "
                    f"end={candidate + duration}s "
                    f"fallback=low_confidence "
                    f"window={fraction} "
                    f"evidence='{most_specific}' "
                    f"subject='{subject}' "
                    f"place='{place}' "
                    f"event='{event}'"
                )

                return candidate

        print(
            f"YOUTUBE SEGMENT REJECTED: no timestamp evidence: {key} "
            f"subject='{subject}' place='{place}' event='{event}' "
            f"source_evidence={source_has_evidence} "
            f"used_segments={len(used)}"
        )

        return None

    # ---------------------------------------------------------
    # No specific contract.
    #
    # This is allowed for genuinely representative footage.
    # Use evenly spaced positions rather than old hard-coded
    # topic-specific timestamps.
    # ---------------------------------------------------------

    if not chapters:
        candidate = 0

        while candidate <= max_start:
            if valid_start(candidate):
                register(candidate)

                print(
                    f"YOUTUBE SEGMENT: {key} "
                    f"start={candidate}s "
                    f"end={candidate + duration}s "
                    f"representative=True"
                )

                return candidate

            candidate += duration

    # ---------------------------------------------------------
    # Chapters exist but none matched and no strict contract.
    # Use an unused chapter.
    # ---------------------------------------------------------

    for chapter in chapters:
        start_time = chapter["start"]

        if not valid_start(start_time):
            continue

        register(start_time)

        print(
            f"YOUTUBE SEGMENT: {key} "
            f"start={start_time}s "
            f"end={start_time + duration}s "
            f"chapter='{chapter['title']}' "
            f"representative=True"
        )

        return start_time

    # ---------------------------------------------------------
    # Source exhausted.
    # ---------------------------------------------------------

    print(
        f"YOUTUBE SOURCE EXHAUSTED: {key} "
        f"duration={video_duration}s "
        f"used_segments={len(used)}"
    )

    return None


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

def _uve_candidate_key(candidate):
    if not isinstance(candidate, dict):
        return ""

    url = str(
        candidate.get("url")
        or candidate.get("video_url")
        or candidate.get("image_url")
        or candidate.get("link")
        or ""
    ).strip().lower()

    if not url:
        return ""

    # Search candidates do not have a timestamp yet.
    # The timestamp is assigned later by _youtube_start().
    # Do not use _yt_segment_uses here because it contains
    # a list of timestamps, not one timestamp.
    start = candidate.get("clip_start")

    if start is not None:
        try:
            start = int(float(start))
        except (TypeError, ValueError):
            start = str(start)

        return f"{url}|{start}"

    # Allow the same YouTube source to be selected again.
    # _youtube_start() will choose a different segment.
    if "youtube.com" in url or "youtu.be" in url:
        return f"{url}|pending"

    return url


def _uve_download_video_candidate(
    candidate,
    scene_dir,
    sentence_number,
    asset_index,
):
    if not isinstance(candidate, dict):
        return None

    url = str(
        candidate.get("url")
        or candidate.get("video_url")
        or candidate.get("link")
        or ""
    ).strip()

    if not url:
        return None

    need = candidate.get("shot_need") or {}

    subject = str(
        need.get("required_subject")
        or candidate.get("subject")
        or ""
    ).strip()

    place = str(
        need.get("place")
        or candidate.get("place")
        or ""
    ).strip()

    event = str(
        need.get("event")
        or candidate.get("event")
        or ""
    ).strip()

    era = str(
        need.get("era")
        or candidate.get("era")
        or ""
    ).strip()

    start = 0

    if (
        "youtube.com" in url.lower()
        or "youtu.be" in url.lower()
    ):
        start = _youtube_start(
            url,
            subject=subject,
            place=place,
            event=event,
            era=era,
        )

        # -----------------------------------------------------
        # No suitable segment was found in this source.
        #
        # Reject the source cleanly so the caller can try the
        # next YouTube candidate.
        # -----------------------------------------------------

        if start is None:
            print(
                f"YOUTUBE SOURCE REJECTED: "
                f"no suitable segment "
                f"subject='{subject}' "
                f"place='{place}' "
                f"event='{event}' "
                f"era='{era}' "
                f"url={url}"
            )
            return None

    safe_query = str(
        candidate.get("matched_query")
        or candidate.get("query")
        or "youtube_video"
    ).strip()

    safe_query = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        safe_query,
    ).strip("_")[:80] or "youtube_video"

    output_path = Path(scene_dir) / (
        f"sentence_{int(sentence_number):04d}"
        f"_video_{int(asset_index):02d}"
        f"_{int(start):05d}s"
        f"_{safe_query}.mp4"
    )

    try:
        ok = download_video_clip(
            url,
            output_path,
            start_time=start,
            duration=DEFAULT_CLIP_DURATION,
        )

    except Exception as exc:
        print(
            f"VIDEO DOWNLOAD FAILED: "
            f"{url} start={start}s error={exc}"
        )
        return None

    if not ok:
        return None

    if not validate_video(output_path):
        try:
            output_path.unlink()
        except OSError:
            pass

        return None

    asset = dict(candidate)

    asset.update(
        {
            "file_path": str(output_path),
            "path": str(output_path),
            "video_url": url,
            "url": url,
            "type": "video",
            "visual_type": "video",
            "duration_seconds": DEFAULT_CLIP_DURATION,
            "sentence_number": sentence_number,
            "clip_start": start,
            "clip_duration": DEFAULT_CLIP_DURATION,
        }
    )

    print(
        f"YOUTUBE ASSET READY: "
        f"start={start}s "
        f"duration={DEFAULT_CLIP_DURATION}s "
        f"subject='{subject}' "
        f"place='{place}' "
        f"event='{event}' "
        f"era='{era}' "
        f"path={output_path}"
    )

    return asset


def _uve_scenario_search(
    scenario,
    search_cache,
    project_id,
    shot_need=None,
):
    """
    Simple documentary search.

    Sources:
      - YouTube
      - general web sources

    Search queries come directly from the visual
    scenario / shot contract. No legacy provider
    routing or universal-search layer is used.
    """

    need = shot_need or {}

    queries = list(
        need.get("queries")
        or scenario.get("queries")
        or []
    )

    # If a scenario somehow has no generated query,
    # use its visual text as the final generic fallback.
    if not queries:
        visual = str(
            scenario.get("visual")
            or ""
        ).strip()

        if visual:
            queries = [visual]

    videos = []
    web_sources = []

    seen_video_urls = set()
    seen_web_urls = set()

    # Remove duplicate / near-duplicate search queries before
    # spending Serper calls. Keep the first useful wording.
    unique_queries = []
    query_signatures = []

    for raw_query in queries:
        query = " ".join(
            str(raw_query or "").split()
        ).strip()

        if not query:
            continue

        words = sorted(
            {
                word.lower()
                for word in query.split()
                if len(word) >= 3
            }
        )

        signature = " ".join(words)

        if not signature:
            continue

        duplicate = False

        for existing in query_signatures:
            existing_words = set(existing.split())
            current_words = set(signature.split())

            if not existing_words or not current_words:
                continue

            overlap = (
                len(existing_words & current_words)
                / max(
                    len(existing_words | current_words),
                    1,
                )
            )

            if overlap >= 0.75:
                duplicate = True
                break

        if duplicate:
            continue

        query_signatures.append(signature)
        unique_queries.append(query)

        if len(unique_queries) >= 4:
            break

    for query in unique_queries:
        query = " ".join(
            str(query or "").split()
        ).strip()

        if not query:
            continue

        cache_key = (
            f"simple_sources::{query.lower()}"
        )

        if cache_key in search_cache:
            cached = search_cache[
                cache_key
            ]

            videos.extend(
                cached.get("videos") or []
            )

            web_sources.extend(
                cached.get("web_sources") or []
            )

            continue

        try:
            found_videos = search_youtube_videos(
                query,
                limit=5,
            )
        except Exception as error:
            print(
                f"YouTube search failed "
                f"for '{query}': {error}"
            )
            found_videos = []

        try:
            found_web = search_web_sources(
                query,
                limit=5,
            )
        except Exception as error:
            print(
                f"Web search failed "
                f"for '{query}': {error}"
            )
            found_web = []

        search_cache[cache_key] = {
            "videos": found_videos,
            "web_sources": found_web,
        }

        for video in found_videos:
            url = str(
                video.get("video_url")
                or video.get("url")
                or video.get("link")
                or ""
            ).strip()

            if not url:
                continue

            if url in seen_video_urls:
                continue

            seen_video_urls.add(url)
            videos.append(video)

        for source in found_web:
            url = str(
                source.get("url")
                or source.get("link")
                or ""
            ).strip()

            if not url:
                continue

            if url in seen_web_urls:
                continue

            seen_web_urls.add(url)
            web_sources.append(source)

    return videos, web_sources

def _uve_verify_all(candidates, scenario, bible, shot_need=None):
    scored, seen = [], set()

    for candidate in candidates:
        key = _uve_candidate_key(candidate)

        if not key or key in seen:
            continue

        seen.add(key)

        # Cheap metadata gate BEFORE expensive LLM verification.
        ok, reason = gate_shot_need(
            candidate,
            shot_need,
        )

        if not ok:
            item = dict(candidate)
            source_cache.bump("gate_rejects")
            item["reject_reason"] = reason
            item["verification"] = {
                "accepted": False,
                "score": 0,
            }
            item["universal_score"] = 0
            item["relevance_score"] = 0
            scored.append(item)

            print(
                f"      REJECT {reason}: "
                f"{result_title(candidate)[:80]}"
            )
            continue

        source_cache.bump("llm_verify_calls")
        verdict = story.verify_candidate(
            candidate,
            scenario,
            bible,
        )

        item = dict(candidate)
        item["verification"] = verdict
        item["universal_score"] = verdict["score"]
        item["relevance_score"] = verdict["score"]
        scored.append(item)

    scored.sort(
        key=lambda c: c["universal_score"],
        reverse=True,
    )

    return scored


RELAXED_MIN_SCORE = 0.15

def _title_blob(candidate):
    return " ".join([
        str(candidate.get("title") or ""),
        str(candidate.get("snippet") or ""),
        str(candidate.get("matched_query") or ""),
    ]).lower()


def gate_shot_need(candidate, shot_need):
    """
    Strict metadata gate for a visual contract.

    This gate is intentionally generic. It rejects candidates
    that clearly contradict the requested subject, location,
    event, or other forbidden terms before download.
    """

    need = shot_need or {}

    blob = _title_blob(candidate)

    subject = str(
        need.get("required_subject")
        or ""
    ).strip().lower()

    place = str(
        need.get("place")
        or ""
    ).strip().lower()

    kind = str(
        need.get("required_type")
        or ""
    ).lower()

    specificity = str(
        need.get("specificity")
        or ""
    ).lower()

    # Explicit forbidden terms always win.
    for banned in (
        need.get("forbidden")
        or []
    ):
        banned_text = str(
            banned or ""
        ).strip().lower()

        if (
            banned_text
            and banned_text in blob
        ):
            return False, "forbidden_term"

    # Exact person/entity contracts require the requested
    # subject to appear in the searchable metadata.
    if (
        kind == "person"
        and subject
    ):
        subject_parts = [
            part
            for part in subject.split()
            if len(part) > 2
        ]

        if subject not in blob:
            if (
                not subject_parts
                or not all(
                    part in blob
                    for part in subject_parts
                )
            ):
                return False, "missing_person"

    # Exact entity/location contracts require the subject.
    if (
        kind in {
            "location",
            "organization",
            "event",
            "object",
        }
        and subject
    ):
        subject_parts = [
            part
            for part in subject.split()
            if len(part) > 2
        ]

        if subject not in blob:
            if (
                not subject_parts
                or not all(
                    part in blob
                    for part in subject_parts
                )
            ):
                return False, "missing_subject"

    # If a specific place is required, it must appear in
    # the candidate metadata.
    if place:
        place_parts = [
            part
            for part in place.split()
            if len(part) > 2
        ]

        if place not in blob:
            if (
                not place_parts
                or not all(
                    part in blob
                    for part in place_parts
                )
            ):
                return False, "missing_place"

    # Exact contracts require all explicit identity
    # information to survive the metadata gate.
    if specificity == "exact":
        if subject:
            subject_parts = [
                part
                for part in subject.split()
                if len(part) > 2
            ]

            if subject not in blob:
                if (
                    not subject_parts
                    or not all(
                        part in blob
                        for part in subject_parts
                    )
                ):
                    return False, "missing_subject"

        if place:
            place_parts = [
                part
                for part in place.split()
                if len(part) > 2
            ]

            if place not in blob:
                if (
                    not place_parts
                    or not all(
                        part in blob
                        for part in place_parts
                    )
                ):
                    return False, "missing_place"

    return True, ""

def _uve_pick(
    scored,
    scenario,
    registry,
    used_keys,
    limit=3,
    shot_need=None,
):
    pool = []

    need = shot_need or {}

    specificity = str(
        need.get("specificity") or ""
    ).lower()

    required_type = str(
        need.get("required_type") or ""
    ).lower()

    required_subject = str(
        need.get("required_subject") or ""
    ).strip()

    place = str(
        need.get("place") or ""
    ).strip()

    strict_specific = (
        required_type in {
            "person",
            "location",
            "organization",
            "event",
            "object",
        }
        or bool(required_subject)
        or bool(place)
        or specificity == "exact"
    )

    for candidate in scored:
        ok, reason = gate_shot_need(
            candidate,
            shot_need,
        )

        if not ok:
            candidate["reject_reason"] = reason
            print(
                f"      REJECT {reason}: "
                f"{result_title(candidate)[:80]}"
            )
            continue

        verification = (
            candidate.get("verification")
            or {}
        )

        if verification.get("accepted"):
            candidate["verification_status"] = "verified"
            pool.append(candidate)

    if (
        not pool
        and not strict_specific
        and specificity != "exact"
    ):
        for candidate in scored:
            if candidate.get("reject_reason"):
                continue

            score = float(
                candidate.get("universal_score") or 0
            )

            if score >= RELAXED_MIN_SCORE:
                candidate["verification_status"] = "relaxed"
                pool.append(candidate)

    picked = []
    youtube_source_counts = {}


    for candidate in pool:
        # Enforce a maximum of 3 clips from the same YouTube source.
        candidate_url = str(
            candidate.get("url")
            or candidate.get("video_url")
            or candidate.get("link")
            or ""
        ).strip().split("&", 1)[0]

        is_youtube_candidate = (
            "youtube.com" in candidate_url.lower()
            or "youtu.be" in candidate_url.lower()
        )

        if is_youtube_candidate:
            batch_uses = youtube_source_counts.get(candidate_url.lower(), 0)
            try:
                existing_uses = registry.source_usage_count(candidate_url)
            except Exception:
                existing_uses = 0

            if existing_uses + batch_uses >= registry.max_source_uses:
                candidate["reject_reason"] = "source_usage_limit"
                continue

        key = _uve_candidate_key(candidate)

        if not key:
            continue

        is_youtube = (
            "youtube.com" in key
            or "youtu.be" in key
        )

        # Search candidates do not have clip_start yet.
        # For YouTube, allow the source to be selected again.
        # _youtube_start() will choose a new timestamp later.
        if key in used_keys and not is_youtube:
            continue

        try:
            allowed, why = registry.can_use(
                url=key,
                title=result_title(candidate),
                allow_source_reuse=is_youtube,
            )
        except Exception:
            allowed, why = True, ""

        if not allowed:
            candidate["reject_reason"] = (
                why or "registry_block"
            )
            continue

        item = dict(candidate)

        if shot_need:
            item["shot_need"] = dict(
                shot_need
            )

        item["verification_score"] = (
            item.get("universal_score")
        )

        picked.append(item)

        if is_youtube_candidate:
            source_key = candidate_url.lower()
            youtube_source_counts[source_key] = youtube_source_counts.get(source_key, 0) + 1

        if len(picked) >= limit:
            break

    return picked


def _uve_fetch_first(candidates, downloader, scene_dir, sentence_number, asset_index, registry, used_keys):
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        asset = downloader(
            candidate,
            scene_dir,
            sentence_number,
            asset_index,
        )

        if not asset:
            continue

        key = _uve_candidate_key(asset)

        if key and key in used_keys:
            try:
                path = asset.get("file_path")
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
            continue

        if key:
            used_keys.add(key)

        try:
            registry_url = str(
                asset.get("video_url")
                or asset.get("url")
                or key
                or ""
            ).split("&", 1)[0]

            clip_start = asset.get("clip_start")

            if clip_start is not None:
                try:
                    clip_start = int(float(clip_start))
                except (TypeError, ValueError):
                    clip_start = str(clip_start)

                clip_key = f"{registry_url}|{clip_start}"
            else:
                clip_key = key

            registry.register(
                path=asset.get("file_path"),
                url=registry_url,
                title=asset.get("title", ""),
                clip_key=clip_key,
            )
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
        videos_left = len(scenarios)
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
                print("      routed=youtube,web")
                raw_videos, raw_web_sources = _uve_scenario_search(scenario, search_cache, project_id, shot_need)
                scored_videos = _uve_verify_all(
                    raw_videos,
                    scenario,
                    bible,
                    shot_need=shot_need,
                )
                scored_web_sources = _uve_verify_all(
                    raw_web_sources,
                    scenario,
                    bible,
                    shot_need=shot_need,
                )
                print(f"      candidates: videos={len(scored_videos)} web={len(scored_web_sources)}")

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
                review_records.append({
                    "shot_index": scenario_index,
                    "queries": list(queries or []),
                    "providers": ["youtube", "web"] if shot_need else [],
                    "rejected": [
                        {"title": result_title(item), "reason": item.get("reject_reason")}
                        for item in (scored_videos + scored_web_sources)
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

            # Assign each downloaded clip to the sentence/scenario that
            # produced it instead of copying the entire beat asset list
            # onto every sentence.
            sentence_videos = []
            if offset < len(beat_videos):
                sentence_videos.append(dict(beat_videos[offset]))

            sentence_images = []
            if offset < len(beat_images):
                sentence_images.append(dict(beat_images[offset]))

            result = _uve_prepare_sentence_result(
                sentence,
                sentence_number,
                sentence_images,
                sentence_videos,
                people,
            )

            result["beat_id"] = beat["beat_id"]
            result["beat_searchable"] = beat.get("searchable", True)
            results_by_number[sentence_number] = result

    for sentence_index, sentence in enumerate(raw_sentences, start=1):
        sentence_number = get_sentence_number(sentence, sentence_index)
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












































