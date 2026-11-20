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
    # Bounded retry with backoff + Retry-After: a single Wikimedia 429
    # must not kill an otherwise verified image download outright.
    response = request_with_retry(
        "GET",
        image_url,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    Path(output_path).write_bytes(response.content)

from datetime import datetime, timezone

from services.visual_registry import get_registry
from services import run_metrics
from services import story_understanding as story
from services.media_probe import probe_image, probe_video
from services.http_retry import request_with_retry, classify_error

try:
    from services.canonical_media import (
        canonical_candidate_key,
        canonical_source_key,
    )
except ImportError:  # flat-layout fallback
    from canonical_media import (  # noqa: F401
        canonical_candidate_key,
        canonical_source_key,
    )

try:
    from services import process_utils
except ImportError:  # flat-layout fallback
    import process_utils


# ==========================================================================
# EVIDENCE CLASSIFICATION
# ==========================================================================
# DM-18 DISPOSITION (Quality Recovery Phase 1, recorded — not deleted):
# the evidence-scoring subsystem in this section
# (classify_evidence_type / calculate_visual_evidence_score /
# rank_visuals_by_evidence and their helpers, media_pipeline.py:80-308
# at the audited HEAD) is NOT called by the selection path — live
# selection is verify_candidate + universal_score + the bounded
# format-intent bonus, and no score from this subsystem influences any
# acceptance, ranking or timeline decision. It is quarantined IN PLACE
# as formally classified dead code pending the founder-gated IA-18
# deletion decision; it must not be wired into selection silently
# (that would be a behaviour change requiring its own tests and
# approval), and test_degraded_* pins that the selection path does
# not call it. The fabricated-default half of DM-18 (timeline
# selection_score defaults) is a reporting concern for Phase 3, not
# touched here.

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

# Segment-retrieval provenance (founder directive: download only the
# segment required, and prove it). The real download functions record
# HOW each clip was retrieved here, keyed by output path; the asset
# builder pops the entry and stamps it onto the accepted asset.
# Methods: "yt-dlp-download-sections" (partial), "ffmpeg-range-seek"
# (partial), "full-download-trim" (NOT partial — reported honestly).
DOWNLOAD_PROVENANCE: dict = {}

# Source durations learned during segment-evidence checks, keyed by
# URL, so accepted assets can report original vs extracted duration
# without re-probing remote sources.
_SOURCE_DURATION_CACHE: dict = {}

# VA-03/VA-06: per-run segment-evidence bookkeeping, keyed by URL.
# _SEGMENT_BASIS records HOW a start time was established inside
# _youtube_start ("chapter" | "transcript" | "description" |
# "positional_no_contract") so a positional estimate is never
# relabelled as verified evidence downstream. _SEGMENT_ATTEMPTS lists
# every evidence source tried for a URL, in order, with its outcome.
# _SEGMENT_FAILURE_CAUSE records why evidence failed, distinguishing
# a broken information channel from genuinely absent evidence.
_SEGMENT_BASIS: dict = {}
_SEGMENT_ATTEMPTS: dict = {}
_SEGMENT_FAILURE_CAUSE: dict = {}

# yt-dlp TLS trust (founder investigation, 2026-11-19): yt-dlp verifies
# TLS against certifi's bundled Mozilla roots by default and ignores
# both the system trust store and SSL_CERT_FILE. Behind a TLS-
# intercepting egress proxy whose CA is installed in the system store
# (and named by SSL_CERT_FILE / REQUESTS_CA_BUNDLE), every extraction
# therefore fails with CERTIFICATE_VERIFY_FAILED even though curl,
# git and requests — which honour the environment bundle — work.
# When the environment configures a CA bundle, pass yt-dlp's own
# supported --compat-options no-certifi so it verifies against the
# system / environment trust chain instead. Verification stays ON;
# --no-check-certificate is never used anywhere in this pipeline.
def _yt_dlp_base_command() -> list:
    command = [sys.executable, "-m", "yt_dlp"]
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"):
        command += ["--compat-options", "no-certifi"]
    return command


# Per-run resilience state (cleared by reset_media_caches each job):
# URLs whose download already failed in this run are never re-requested
# by a later beat/scene — a throttled or dead URL stays dead for the run
# instead of burning discovery budget again and again.
_FAILED_DOWNLOAD_URLS: set = set()

# Discovery failures recorded by _uve_scenario_search (provider, query,
# error) so the asset report can distinguish DISCOVERY failures from
# download and verification failures instead of losing them in prints.
_DISCOVERY_FAILURES: list = []


def _record_discovery_failure(provider, query, error, *, attempt=1, retry_count=0):
    """Record one discovery (provider search) failure.

    Quality Recovery Phase 2 (workstream D; DM-09): the record carries
    the full failure shape the brief requires — provider, query,
    attempt number, error-class enum, HTTP status where available,
    transient/non-transient classification, retry count and circuit-
    breaker state — so a provider failure is never collapsed with an
    empty result set (zero failures recorded != failures unknown; the
    artefact-level distinction is completed in Phase 3 reporting).
    The legacy "error" string is kept for existing consumers.
    """
    classification = classify_error(error)
    _DISCOVERY_FAILURES.append(
        {
            "provider": str(provider),
            "query": str(query)[:160],
            "error": f"{type(error).__name__}: {error}"[:200],
            "attempt": int(attempt or 1),
            "error_class": classification["error_class"],
            "http_status": classification["http_status"],
            "transient": classification["transient"],
            "retry_count": int(retry_count or 0),
            "breaker_state": {
                "consecutive_failures": int(
                    _search_failure_state.get("consecutive_failures") or 0
                ),
                "tripped": bool(
                    (_search_failure_state.get("consecutive_failures") or 0)
                    >= MAX_CONSECUTIVE_SEARCH_FAILURES
                ),
            },
        }
    )


def drain_discovery_failures():
    """Return and clear the discovery failures recorded since last drain."""
    failures = list(_DISCOVERY_FAILURES)
    _DISCOVERY_FAILURES.clear()
    return failures


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

            # F-1 threading: carry the scenario's format intent into the
            # beat loop, where unified selection uses it as a bounded
            # ranking signal.
            "format_intent": str(
                scenario.get("format_intent") or "any"
            ).strip().lower(),

            "format_intent_confidence": scenario.get(
                "format_intent_confidence"
            ) or 0.0,

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
    for offset, sentence in enumerate(beat.get("sentences") or []):
        if _sentence_role(sentence) in NO_SEARCH_ROLES:
            continue
        for scenario in _uve_scenarios_for(sentence):
            # DM-17: remember which sentence this scenario came from,
            # so distribution can deal its assets to that sentence
            # only (see _distribute_beat_assets).
            scenario["_sentence_offset"] = offset
            scenarios.append(scenario)
    unique, seen = [], set()
    for scenario in scenarios:
        key = tuple((scenario.get("queries") or [])[:3])
        if key in seen:
            continue
        seen.add(key)
        unique.append(scenario)
    return unique[:6]


def _beat_scenario_owner_offsets(beat):
    """Map flattened beat scenario_index (1-based, in _beat_scenarios
    order) -> the offset within the beat of the sentence that OWNS
    that scenario, from the _sentence_offset stamps _beat_scenarios
    leaves on each scenario it builds."""
    owners = {}
    for index, scenario in enumerate(_beat_scenarios(beat), start=1):
        offset = scenario.get("_sentence_offset")
        if isinstance(offset, int):
            owners[index] = offset
    return owners


def _distribute_beat_assets(beat, beat_videos, beat_images):
    """Direct per-sentence assignments for a beat (canonical DM-17).

    An asset is dealt ONLY to the sentence that owns the scenario it
    was verified for (its stamped scenario_index). The old code dealt
    beat_videos[offset] / beat_images[offset] by position, so a
    sentence could receive a SIBLING scenario's asset labelled as its
    direct assignment. An asset whose scenario owner is unknown is
    not dealt as direct at all — it stays in the scene aggregates,
    where the timeline may use it only as honestly labelled rotation.
    Returns a list, per sentence offset, of (videos, images)."""
    count = len(beat.get("sentences") or [])
    owners = _beat_scenario_owner_offsets(beat)
    per_sentence = [([], []) for _ in range(count)]

    if not owners:
        # No scenario->sentence linkage exists for this beat (the
        # scenarios did not come from _beat_scenarios — e.g. a caller
        # supplied them directly). DM-17 has nothing to enforce
        # against, so keep the legacy positional dealing rather than
        # dropping verified assets on the floor.
        for offset in range(count):
            if offset < len(beat_videos or []):
                per_sentence[offset][0].append(dict(beat_videos[offset]))
            if offset < len(beat_images or []):
                per_sentence[offset][1].append(dict(beat_images[offset]))
        return per_sentence

    def owner_of(asset):
        try:
            index = int(asset.get("scenario_index"))
        except (TypeError, ValueError):
            return None
        return owners.get(index)

    for asset in beat_videos or []:
        owner = owner_of(asset)
        if owner is not None and owner < count:
            per_sentence[owner][0].append(dict(asset))
    for asset in beat_images or []:
        owner = owner_of(asset)
        if owner is not None and owner < count:
            per_sentence[owner][1].append(dict(asset))
    return per_sentence


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
    # AUD-15: the licence of an arbitrary web image is UNKNOWN. This helper
    # previously stamped "royalty-free" on any URL — a false rights claim.
    return {"image_url": url, "url": url, "link": url, "title": title,
            "source": source, "license": "unknown", "snippet": title}


def _youtube_video_hit(url, title, source):
    # AUD-15: see _web_image_hit — never fabricate a licence.
    return {"video_url": url, "url": url, "link": url, "title": title,
            "source": source, "license": "unknown", "snippet": title}


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

        provider, host = provider_for_url(url)
        output.append({
            "title": item.get("title", ""),
            "url": url,
            "link": url,
            "snippet": item.get("snippet", ""),
            "source": provider,
            "provider": provider,
            "source_domain": host,
            "license": "unknown",
            "rights_status": "verify_before_publish",
            "matched_query": query,
        })

    return output


def _web_source_is_video_candidate(candidate):
    """MS-2: can this web result plausibly yield moving footage?

    True for direct media files, known video hosts, and pages whose URL
    marks them as video pages (news /video/ and /av/ paths, /watch).
    Plain articles are NOT attempted — a failed or misleading extraction
    must never be how footage is 'found'."""
    url = str(
        candidate.get("url") or candidate.get("video_url") or candidate.get("link") or ""
    ).strip()
    if not url:
        return False
    if is_direct_media_url(url):
        return True
    provider, _host = provider_for_url(url)
    if provider in {"youtube", "vimeo", "dailymotion"}:
        return True
    from urllib.parse import urlparse

    path = (urlparse(url).path or "").lower()
    return "/video" in path or "/watch" in path or "/av/" in path


def search_image_sources(query, limit=5):
    """Real image candidates from Serper image search (AUD-13).

    Normalised to the same candidate shape as the other providers. The
    licence of a web image is NOT known, so it is recorded as "unknown" —
    never "royalty-free" (that false label existed in dead helper code and
    must not come back). Founder restriction: no Pexels/Pixabay retrieval.
    """
    from services.search_provider import search_images

    query = str(query or "").strip()
    if not query:
        return []

    try:
        results = search_images(query, limit=max(limit * 2, 10))
    except Exception as error:
        print(f"Image search failed: {error}")
        return []

    output = []

    for item in results[:limit]:
        image_url = str(item.get("image_url") or "").strip()
        if not image_url:
            continue

        page_url = str(item.get("source_url") or item.get("link") or "").strip()

        output.append({
            "title": item.get("title", ""),
            "image_url": image_url,
            "url": image_url,
            "link": page_url,
            "source_url": page_url,
            "snippet": item.get("snippet", "") or item.get("title", ""),
            "source": item.get("source", ""),
            "type": "image",
            "license": "unknown",
            "rights_status": "verify_before_publish",
            "matched_query": query,
        })

    return output


def search_wikimedia_images(query, limit=5):
    """Wikimedia Commons candidates with REAL licence evidence (AUD-15).

    Commons is the fallback image source whose licence name, licence URL
    and creator come from the provider's own metadata — the only image
    source in the pipeline that can honestly record a licence. Everything
    else stays "unknown". No API key required; no Pexels/Pixabay.
    """
    from services.licensed_media import search_wikimedia

    query = str(query or "").strip()
    if not query:
        return []

    try:
        hits = search_wikimedia(query, limit=limit)
    except Exception as error:
        print(f"Wikimedia search failed: {error}")
        return []

    output = []
    for hit in hits:
        image_url = str(hit.get("download_url") or "").strip()
        if not image_url:
            continue

        creator = re.sub("<[^>]+>", "", str(hit.get("creator") or "")).strip()
        licence = str(hit.get("license") or "").strip()

        output.append({
            "title": hit.get("title", ""),
            "image_url": image_url,
            "url": image_url,
            "link": hit.get("source_url", ""),
            "source_url": hit.get("source_url", ""),
            "snippet": hit.get("alt", "") or hit.get("title", ""),
            "source": "wikimedia",
            "type": "image",
            "creator": creator,
            "license": licence or "unknown",
            "license_url": hit.get("license_url", ""),
            "rights_status": "license_recorded" if licence else "verify_before_publish",
            "matched_query": query,
        })

    return output


_KNOWN_VIDEO_PROVIDERS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "vimeo.com": "vimeo",
    "dailymotion.com": "dailymotion",
    "archive.org": "archive.org",
    "commons.wikimedia.org": "wikimedia",
    "upload.wikimedia.org": "wikimedia",
    "wikimedia.org": "wikimedia",
}


def provider_for_url(url):
    """(provider, host) for a URL. Known video hosts get canonical
    provider names; anything else is identified by its domain, so a news
    site is reported as itself (e.g. "bbc.com"), never as another
    provider. Multi-source correction MS-1."""
    from urllib.parse import urlparse

    host = (urlparse(str(url or "")).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for suffix, name in _KNOWN_VIDEO_PROVIDERS.items():
        if host == suffix or host.endswith("." + suffix):
            return name, host
    return (host or "web"), host


def _disabled_providers():
    """Providers disabled via CINEMORA_DISABLED_PROVIDERS (comma list).
    Provider toggles are part of the modular retrieval design: any
    provider — including YouTube — can be switched off without touching
    the others."""
    raw = os.getenv("CINEMORA_DISABLED_PROVIDERS", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def search_videos_multi(query, limit=5):
    """Multi-source video discovery (MS-1).

    Serper's video index covers many hosts — YouTube, Vimeo, news sites,
    archives and more. The previous implementation DISCARDED every result
    that was not a YouTube watch URL and stamped the survivors
    source="youtube". This function preserves every result, tags it with
    its real provider/domain, and lets the shared gate/verifier/pick
    machinery judge all of them by relevance alone. YouTube has no
    special status here.
    """
    query = str(query or "").strip()
    if not query:
        return []

    from services.search_provider import search_videos

    disabled = _disabled_providers()
    results = []
    seen = set()

    try:
        hits = search_videos(query, limit=max(limit * 3, 15))
    except Exception as error:
        print(f"Video search failed: {error}")
        return []

    for hit in hits:
        url = str(
            hit.get("video_url")
            or hit.get("url")
            or hit.get("link")
            or ""
        ).strip()

        if not url:
            continue

        provider, host = provider_for_url(url)
        if provider in disabled:
            continue

        if provider == "youtube" and "&" in url:
            url = url.split("&", 1)[0]

        dedupe_key = url.split("#", 1)[0]
        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)

        results.append({
            "video_url": url,
            "url": url,
            "link": url,
            "title": hit.get("title", ""),
            "source": provider,
            "provider": provider,
            "source_domain": host,
            # Serper's per-result "source" is the channel/site name —
            # keep it as creator evidence (AUD-15).
            "channel": hit.get("source", ""),
            "creator": hit.get("source", ""),
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


def search_wikimedia_videos(query, limit=5):
    """Wikimedia Commons VIDEO candidates (MS-4) with provider licence
    evidence. Supplementary provider: Commons is one source among many,
    with no priority over or under any other."""
    from services.licensed_media import search_wikimedia_videos as _search

    query = str(query or "").strip()
    if not query:
        return []

    output = []
    for hit in _search(query, limit=limit):
        video_url = str(hit.get("download_url") or "").strip()
        if not video_url:
            continue
        creator = re.sub("<[^>]+>", "", str(hit.get("creator") or "")).strip()
        licence = str(hit.get("license") or "").strip()
        output.append({
            "title": hit.get("title", ""),
            "video_url": video_url,
            "url": hit.get("source_url") or video_url,
            "source_url": hit.get("source_url") or "",
            "source": "wikimedia",
            "provider": "wikimedia",
            "creator": creator,
            "channel": creator,
            "license": licence if licence else "unknown",
            "license_url": hit.get("license_url") or "",
            "rights_status": "recorded" if licence else "verify_before_publish",
            "snippet": hit.get("alt") or "",
            "matched_query": query,
        })
    return output


def search_archive_org_videos(query, limit=3):
    """Archive.org moving-image candidates (MS-4) — an independent
    provider with its own search + metadata, licence evidence included
    when the item records it."""
    from services.archive_org import search_archive_videos

    query = str(query or "").strip()
    if not query:
        return []
    return search_archive_videos(query, limit=limit)


def search_youtube_videos(query, limit=5):
    """Legacy name kept for existing callers. Since the multi-source
    correction (MS-1) this returns results from ALL video providers via
    search_videos_multi — it is no longer YouTube-only despite the name."""
    return search_videos_multi(query, limit=limit)


def search_source_videos(query, limit=5):
    return search_youtube_videos(query, limit=limit)


def _slug_title(page_url, fallback):
    match = re.search(r"/video/(.+?)-\d+/?$", str(page_url or ""))
    return match.group(1).replace("-", " ") if match else fallback





def is_direct_media_url(url):
    low = str(url or "").lower().split("?", 1)[0]
    return low.endswith((".mp4", ".mov", ".webm", ".m4v", ".jpg", ".jpeg", ".png", ".webp"))


def kill_process_tree(process):
    # Stage 2G-a: delegate to the portable implementation (Windows
    # keeps taskkill; POSIX kills the child's process group when it
    # is in a different group than us). Kept as a module-level name
    # because run_command_safe calls it.
    process_utils.kill_process_tree(process)


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
    """A video counts as valid only if ffprobe confirms a real video stream
    with a usable duration (AUD-17). The previous check — file exists and is
    >= 10,000 bytes — let corrupt downloads pass and become silent
    placeholder clips at render time."""
    return probe_video(path) is not None


def _technical_minimums():
    """Configurable technical minimums (VA-12).

    Deliberately conservative: they reject only media that cannot
    plausibly serve a 1080p timeline — sub-second clips and
    thumbnail-grade dimensions. There is NO frame-rate minimum
    (archival footage legitimately runs at unusual rates) and NO
    aspect-ratio rejection (the renderer normalizes). Every value is
    overridable via the environment for unusual productions.
    """
    def _env(name, default):
        try:
            return float(os.environ.get(name) or default)
        except (TypeError, ValueError):
            return float(default)

    return {
        "min_video_width": _env("CINEMORA_MIN_VIDEO_WIDTH", 320),
        "min_video_height": _env("CINEMORA_MIN_VIDEO_HEIGHT", 240),
        "min_video_duration": _env("CINEMORA_MIN_VIDEO_DURATION", 1.0),
        "min_image_width": _env("CINEMORA_MIN_IMAGE_WIDTH", 320),
        "min_image_height": _env("CINEMORA_MIN_IMAGE_HEIGHT", 240),
    }


def _video_technical_failures(probe, minimums=None):
    """Reasons a probed video is technically unsuitable (VA-12).

    Returns a list of failed check names; empty means technically
    acceptable. This is a TECHNICAL judgement only — it says nothing
    about relevance, which the gate/verifier own.
    """
    minimums = minimums or _technical_minimums()
    failures = []
    if probe.get("duration_seconds") is not None and (
        probe["duration_seconds"] < minimums["min_video_duration"]
    ):
        failures.append("duration_below_minimum")
    width, height = probe.get("width"), probe.get("height")
    if width is not None and width < minimums["min_video_width"]:
        failures.append("width_below_minimum")
    if height is not None and height < minimums["min_video_height"]:
        failures.append("height_below_minimum")
    return failures


def _image_technical_failures(probe, minimums=None):
    minimums = minimums or _technical_minimums()
    failures = []
    width, height = probe.get("width"), probe.get("height")
    if width is not None and width < minimums["min_image_width"]:
        failures.append("width_below_minimum")
    if height is not None and height < minimums["min_image_height"]:
        failures.append("height_below_minimum")
    return failures


def _download_image_detailed(image_url, output_path):
    """Download + validate an image with an explicit reason (VA-13).

    Returns (ok, reason): reason is "" on success, else one of
    no_url / rate_limited / http_error / download_failed /
    undersized_file / decode_failed / probe_failed.
    """
    output_path = Path(output_path)
    if not image_url:
        return False, "no_url"
    try:
        if output_path.exists():
            output_path.unlink()
        download_image(image_url, str(output_path))
    except Exception as error:
        print(f"IMAGE DOWNLOAD FAILED: {error}")
        try:
            if output_path.exists():
                output_path.unlink()
        except OSError:
            pass
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status == 429 or "429" in str(error):
            return False, "rate_limited"
        if status is not None or "HTTP" in type(error).__name__:
            return False, "http_error"
        if re.search(r"\b[45]\d\d\b", str(error)):
            return False, "http_error"
        return False, "download_failed"
    if not output_path.exists() or output_path.stat().st_size < 1000:
        try:
            output_path.unlink()
        except OSError:
            pass
        return False, "undersized_file"
    try:
        with Image.open(output_path) as img:
            img.verify()
        with Image.open(output_path) as img:
            img.load()
    except Exception:
        try:
            output_path.unlink()
        except OSError:
            pass
        return False, "decode_failed"
    return True, ""


def download_valid_image(image_url, output_path):
    ok, _reason = _download_image_detailed(image_url, Path(output_path).resolve())
    return ok


def download_direct_video(url, output_path, duration=DEFAULT_CLIP_DURATION, start_time=0):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Segment-only retrieval FIRST (founder directive): ffmpeg input
    # seeking (-ss BEFORE -i) makes the HTTP client request only the
    # byte ranges around the evidenced segment instead of pulling the
    # whole file. Servers that cannot serve ranges, or sources that
    # fail this path, fall through to the full-download trim below —
    # which is then reported honestly as NOT partial.
    seek_command = [
        "ffmpeg", "-y",
        "-ss", str(max(0, int(start_time or 0))),
        "-i", str(url),
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-an", "-movflags", "+faststart", str(output_path),
    ]
    seek_code, _, _seek_err = run_command_safe(
        seek_command, FFMPEG_TIMEOUT, "FFmpeg range download"
    )
    if seek_code == 0 and validate_video(output_path):
        DOWNLOAD_PROVENANCE[str(output_path)] = {
            "method": "ffmpeg-range-seek",
            "partial": True,
        }
        return True
    try:
        if output_path.exists():
            output_path.unlink()
    except OSError:
        pass

    raw_path = output_path.with_name(output_path.stem + "_raw.mp4")
    try:
        response = _http().get(url, timeout=60, stream=True)
        response.raise_for_status()
        with open(raw_path, "wb") as handle:
            for chunk in response.iter_content(64 * 1024):
                handle.write(chunk)
        if not raw_path.exists() or raw_path.stat().st_size < 10_000:
            return False
        # MS-3: honour the evidence-backed start_time (was silently
        # ignored — direct downloads always trimmed from second 0).
        command = ["ffmpeg", "-y", "-ss", str(max(0, int(start_time or 0))), "-i", str(raw_path), "-t", str(duration), "-c:v", "libx264",
                   "-preset", "ultrafast", "-crf", "28", "-an", "-movflags", "+faststart", str(output_path)]
        code, _, stderr = run_command_safe(command, FFMPEG_TIMEOUT, "FFmpeg source")
        try:
            raw_path.unlink()
        except OSError:
            pass
        if code != 0 or not validate_video(output_path):
            print((stderr or "")[-1000:])
            return False
        # This path downloaded the whole file before trimming — the
        # provenance says so; it is never dressed up as partial.
        DOWNLOAD_PROVENANCE[str(output_path)] = {
            "method": "full-download-trim",
            "partial": False,
        }
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
            start_time=start_time,
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
        *_yt_dlp_base_command(),

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

    # The yt-dlp path retrieves only the evidenced section
    # (--download-sections), never the whole source video.
    DOWNLOAD_PROVENANCE[str(output_path)] = {
        "method": "yt-dlp-download-sections",
        "partial": True,
    }

    return True

def download_video_with_ytdlp(video_url, output_path, start_time=0, duration=DEFAULT_CLIP_DURATION):
    return download_video_clip(video_url, output_path, start_time, duration)


def process_sentence_videos(sentence, sentence_dir, video_count=DEFAULT_VIDEO_COUNT):
    # AUD-24: this was a stub that silently returned []. A legacy test
    # script exercised it and "passed" while testing nothing. Fail loudly.
    raise NotImplementedError(
        "process_sentence_videos is a retired stub. Use process_scene_media "
        "(story-driven visual engine) — it assigns verified visuals per sentence."
    )


def process_sentence_images(sentence, sentence_dir, image_count=DEFAULT_IMAGE_COUNT):
    # AUD-24: see process_sentence_videos.
    raise NotImplementedError(
        "process_sentence_images is a retired stub. Use process_scene_media "
        "(story-driven visual engine) — it assigns verified visuals per sentence."
    )


def process_sentence_media(sentence, project_dir, scene_number, image_count=DEFAULT_IMAGE_COUNT, video_count=DEFAULT_VIDEO_COUNT):
    # AUD-24: see process_sentence_videos. This stub returned a plausible-
    # looking but empty result dict, which silently produced visual-less
    # sentences wherever it was called.
    raise NotImplementedError(
        "process_sentence_media is a retired stub. Use process_scene_media "
        "(story-driven visual engine) — it assigns verified visuals per sentence."
    )


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


def reset_media_caches():
    """Clear every process-global YouTube cache (AUD-18).

    These caches are keyed by raw URL and were NEVER reset: in the
    long-lived worker, project B inherited project A's used segments and
    permanent failure blacklists, so identical URLs were spuriously
    rejected in unrelated later projects. jobs.run_full_pipeline calls
    this at the start of every production run, alongside the registry
    reset. The persistent source_cache (SQLite) is intentionally NOT
    cleared — it is the cross-project memory; these are per-job only.
    """
    _yt_segment_uses.clear()
    _youtube_metadata_cache.clear()
    _youtube_transcript_cache.clear()
    _yt_info_failed.clear()
    _yt_transcript_failed.clear()
    _yt_info_locks.clear()
    _FAILED_DOWNLOAD_URLS.clear()
    _DISCOVERY_FAILURES.clear()
    _SEGMENT_BASIS.clear()
    _SEGMENT_ATTEMPTS.clear()
    _SEGMENT_FAILURE_CAUSE.clear()


# =====================================================================
# Stage 2C — RunContext binding for the per-run globals above.
#
# The containers below are rebound (NOT copied) to the active
# RunContext's containers on activation, so every existing read/write
# site in this module lands in the active context with no logic
# changes. The DEFAULT context wraps the ORIGINAL container objects
# created at import time, so behaviour before any activation is
# bit-identical to the old module-global behaviour.
#
# NOT migrated, deliberately:
# - _yt_info_locks / _yt_info_guard: synchronization primitives —
#   swapping lock identity mid-flight would break mutual exclusion.
# - services.source_cache: SQLite profiles are cross-project BY
#   DESIGN (the persistent cross-run memory).
# - services.run_metrics: already run-scoped.
# - visual_registry: project-keyed.
# =====================================================================
try:
    from services import run_context
except ImportError:  # flat-layout fallback
    import run_context

_RUN_CONTEXT_BINDINGS = {
    "_FAILED_DOWNLOAD_URLS": "failed_download_urls",
    "_DISCOVERY_FAILURES": "discovery_failures",
    "_SEGMENT_BASIS": "segment_basis",
    "_SEGMENT_ATTEMPTS": "segment_attempts",
    "_SEGMENT_FAILURE_CAUSE": "segment_failure_cause",
    "_yt_segment_uses": "yt_segment_uses",
    "_youtube_metadata_cache": "youtube_metadata_cache",
    "_youtube_transcript_cache": "youtube_transcript_cache",
    "_yt_info_failed": "yt_info_failed",
    "_yt_transcript_failed": "yt_transcript_failed",
    "_search_failure_state": "search_failure_state",
}


def _bind_run_context(ctx):
    """Rebind this module's per-run globals to *ctx*'s containers."""
    if ctx is None:
        return
    module_globals = globals()
    for global_name, context_attr in _RUN_CONTEXT_BINDINGS.items():
        module_globals[global_name] = getattr(ctx, context_attr)


_DEFAULT_RUN_CONTEXT = run_context.RunContext(
    failed_download_urls=_FAILED_DOWNLOAD_URLS,
    discovery_failures=_DISCOVERY_FAILURES,
    segment_basis=_SEGMENT_BASIS,
    segment_attempts=_SEGMENT_ATTEMPTS,
    segment_failure_cause=_SEGMENT_FAILURE_CAUSE,
    yt_segment_uses=_yt_segment_uses,
    youtube_metadata_cache=_youtube_metadata_cache,
    youtube_transcript_cache=_youtube_transcript_cache,
    yt_info_failed=_yt_info_failed,
    yt_transcript_failed=_yt_transcript_failed,
    search_failure_state=_search_failure_state,
)
run_context.set_default(_DEFAULT_RUN_CONTEXT)
run_context.register_binder(_bind_run_context)


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
                    *_yt_dlp_base_command(),
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

        # VA-14: a stale profile (past its TTL) is re-analysed rather
        # than trusted — the video may have become unavailable since.
        if (
            persisted and persisted.get("duration")
            and persisted.get("status") != "stale"
        ):
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


def _get_youtube_transcript(url):
    """
    Load the raw yt-dlp json3 transcript for a source URL, or None.

    This is the SINGLE transcript loading path: memory cache ->
    persistent source profile -> one caption download (failures are
    cached in _yt_transcript_failed and never retried per shot).
    _youtube_transcript_segment uses it to find segments, and the
    Stage 3B transcript-interval verification uses it to read the
    text of an already-chosen segment. Fail-soft: any failure
    returns None.
    """
    key = str(url or "").split("&", 1)[0].strip()

    if not key:
        return None

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

    return transcript


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

    transcript = _get_youtube_transcript(key)

    if transcript is None:
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
# Stage 3B — transcript-interval verification (review §2.4)
#
# verify_candidate judges a candidate by its METADATA (title /
# snippet / description). This layer judges the actual spoken words
# inside the segment that was chosen: the transcript text overlapping
# [clip_start, clip_start + duration] must contain at least one of
# the scenario's subject/topical tokens. It can ONLY demote — a
# mismatch rejects an otherwise-accepted candidate; a match or an
# unavailable transcript never changes a verdict, and a candidate the
# verifier already rejected is never upgraded.
# =====================================================================

TRANSCRIPT_MIN_WINDOW_TOKENS = 5


def _transcript_window_text(url, start, duration=DEFAULT_CLIP_DURATION):
    """Transcript text overlapping [start, start+duration] for a
    source video, or None when no transcript is available.

    Reuses the single transcript loading path
    (_get_youtube_transcript: memory cache -> persistent profile ->
    one download) — no second fetcher. Transcript entries are the
    json3 events the loader returns; an entry overlaps the window
    when its [tStartMs, tStartMs + dDurationMs] interval intersects
    it (an entry with no duration counts when its start falls inside
    the window). Returns "" when a transcript exists but no entry
    overlaps the window.
    """
    try:
        start = float(start)
    except (TypeError, ValueError):
        return None
    try:
        duration = float(duration or DEFAULT_CLIP_DURATION)
    except (TypeError, ValueError):
        duration = float(DEFAULT_CLIP_DURATION)
    if duration <= 0:
        duration = float(DEFAULT_CLIP_DURATION)

    transcript = _get_youtube_transcript(url)
    if not isinstance(transcript, dict):
        return None

    window_end = start + duration
    parts = []
    for event_item in transcript.get("events") or []:
        segs = event_item.get("segs") or []
        if not segs:
            continue
        text = " ".join(
            part.strip()
            for part in (str(seg.get("utf8") or "") for seg in segs)
            if part.strip()
        ).strip()
        if not text:
            continue
        try:
            ev_start = float(event_item.get("tStartMs") or 0) / 1000.0
        except (TypeError, ValueError):
            continue
        raw_duration = event_item.get("dDurationMs")
        try:
            ev_end = ev_start + float(raw_duration) / 1000.0 if raw_duration else ev_start
        except (TypeError, ValueError):
            ev_end = ev_start
        if ev_end > ev_start:
            overlaps = ev_start < window_end and ev_end > start
        else:
            overlaps = start <= ev_start < window_end
        if overlaps:
            parts.append(text)
    return " ".join(parts)


def _transcript_check_terms(scenario, bible):
    """Subject/topical content tokens for the interval check.

    Mirrors verify_candidate's term extraction in
    story_understanding: the scenario subject's canonical name and
    aliases, the scenario's subject label, its search_terms and its
    visual description — all through the same _content_tokens
    machinery, so the interval check and the verifier speak the
    same token language.
    """
    scenario = scenario or {}
    terms = set()
    by_id = (bible or {}).get("by_id") or {}
    subject = by_id.get(scenario.get("subject_id"))
    if subject:
        terms |= story._content_tokens(subject.get("canonical"))
        for alias in subject.get("aliases") or []:
            terms |= story._content_tokens(alias)
    if scenario.get("subject"):
        terms |= story._content_tokens(scenario.get("subject"))
    for term in scenario.get("search_terms") or []:
        terms |= story._content_tokens(term)
    terms |= story._content_tokens(scenario.get("visual"))
    return terms


def check_transcript_interval(window_text, scenario, bible=None):
    """Judge one transcript window against a scenario.

    - "no_transcript" (NEUTRAL): no window text, or the window
      yields fewer than TRANSCRIPT_MIN_WINDOW_TOKENS content tokens,
      or the scenario defines no subject/topical terms to check.
    - "interval_transcript_match": the window has enough tokens and
      at least one subject/topical token appears in it.
    - "transcript_mismatch": the window has enough tokens and ZERO
      subject/topical tokens appear — the segment is about something
      else, whatever its metadata claimed.
    """
    if window_text is None:
        return "no_transcript"
    window_tokens = story._content_tokens(window_text)
    if len(window_tokens) < TRANSCRIPT_MIN_WINDOW_TOKENS:
        return "no_transcript"
    terms = _transcript_check_terms(scenario, bible)
    if not terms:
        return "no_transcript"
    if terms & window_tokens:
        return "interval_transcript_match"
    return "transcript_mismatch"


def _apply_transcript_check(asset, scenario, bible):
    """Run the Stage 3B check on one downloaded asset.

    Applies only to candidates that (a) passed verify_candidate,
    (b) carry real segment evidence (segment_evidence is not
    "none"/empty) and (c) have a source-video transcript. Stamps
    asset["verification"]["transcript_check"] whenever the check
    runs. On "transcript_mismatch" the verdict is demoted in place
    (accepted=False, rejection_reason="transcript_mismatch") and the
    label is returned; every other outcome leaves the verdict
    untouched. Returns None when the check does not apply or cannot
    run (fail-soft). This layer ONLY ever demotes.
    """
    if not isinstance(asset, dict):
        return None
    verification = asset.get("verification") or {}
    if not verification.get("accepted"):
        return None
    segment_evidence = str(asset.get("segment_evidence") or "").strip()
    if not segment_evidence or segment_evidence == "none":
        return None
    url = str(asset.get("video_url") or asset.get("url") or "").strip()
    start = asset.get("clip_start")
    if not url or start is None:
        return None
    duration = (
        asset.get("clip_duration")
        or asset.get("duration_seconds")
        or DEFAULT_CLIP_DURATION
    )
    try:
        window_text = _transcript_window_text(url, start, duration)
        result = check_transcript_interval(window_text, scenario, bible)
    except Exception as error:  # a transcript failure never crashes a pick
        print(f"TRANSCRIPT CHECK SKIPPED (error): {url}: {error}")
        return None
    stamped = dict(verification)
    stamped["transcript_check"] = result
    if result == "transcript_mismatch":
        stamped["accepted"] = False
        stamped["rejection_reason"] = "transcript_mismatch"
        asset["reject_reason"] = "transcript_mismatch"
        asset["rejection_stage"] = "transcript_verification"
    asset["verification"] = stamped
    return result


# =====================================================================
# Evidence-based timestamp fallback helpers.
# (added by patch_youtube_fallback.py)
# =====================================================================
# Set False to reject sources instead of ever using a low-confidence window.
# AUD-16 (founder directive 2026-11-19): set to False permanently. An
# arbitrary 20/40/60/80% window of a video whose title merely mentions the
# subject is NOT verified footage; sources without timestamp evidence are
# rejected so the caller can try the next candidate.
YT_LOW_CONFIDENCE_FALLBACK = False
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
        _SEGMENT_BASIS[key] = "chapter"
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
        _SEGMENT_BASIS[key] = "transcript"
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
            _SEGMENT_BASIS[key] = "description"
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

                _SEGMENT_BASIS[key] = "positional_no_contract"
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

                _SEGMENT_BASIS[key] = "positional_no_contract"
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
        _SEGMENT_BASIS[key] = "chapter"
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


_VISUAL_FORMAT_TERMS = {
    "map": ("map", "karte", "atlas", "cartograph"),
    "document": (
        "document", "manuscript", "decree", "treaty", "newspaper",
        "front page", "court record", "proclamation",
    ),
    "diagram": ("diagram", "schematic", "infographic", "chart", "graph"),
    "photograph": ("photograph", "photo of"),
}


def classify_visual_format(asset):
    """Evidence-based visual-format classification (F-3).

    Correction rule: an asset is NEVER classified solely from the search
    query or the intended format. Classification uses the asset's own
    metadata and source context (provider title/snippet terms); when no
    evidence supports a specific kind, the format stays "uncertain" —
    the asset can still be a genuine verified image; kind and
    authenticity are separate properties. Returns (format, evidence).
    """
    asset_type = (asset.get("type") or asset.get("visual_type") or "").lower()
    if asset_type == "video":
        return "footage", "media_probe"
    if asset.get("generated"):
        return (asset.get("visual_format") or "graphic"), "generated_in_house"

    text = (
        f"{asset.get('title') or ''} {asset.get('snippet') or ''} "
        f"{asset.get('source_url') or ''}"
    ).lower()
    for fmt, terms in _VISUAL_FORMAT_TERMS.items():
        if any(term in text for term in terms):
            return fmt, "source_context"
    return "uncertain", "unknown"


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
    # F-3: stamp the evidence-based visual format. The scenario's intent
    # is recorded only as a hint when the evidence is inconclusive —
    # intent alone never classifies an asset.
    if not asset.get("visual_format"):
        fmt, evidence = classify_visual_format(asset)
        asset["visual_format"] = fmt
        asset["visual_format_evidence"] = evidence
        if evidence == "unknown":
            asset["format_hint"] = scenario.get("format_intent") or "any"
    return asset

def _uve_candidate_key(candidate):
    """Dedup/selection key for a candidate.

    Quality Recovery Phase 2 (workstream C; canonical DM-16): the base
    is the CANONICAL source key (services.canonical_media), not the
    raw URL string — youtu.be/X and watch?v=X are one candidate, and
    tracking parameters never mint a "new" candidate. Segment identity
    stays distinct from source identity: a stamped clip_start makes
    the key segment-specific, and an unstamped YouTube candidate keeps
    the "|pending" form so the same source may be selected again for
    a different segment (the registry's overlap blocking then applies
    on the canonical source)."""
    if not isinstance(candidate, dict):
        return ""

    base = canonical_candidate_key(candidate)

    if not base:
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

        return f"{base}|{start}"

    # Allow the same YouTube source to be selected again.
    # _youtube_start() will choose a different segment.
    if base.startswith("youtube:"):
        return f"{base}|pending"

    return base


def _remote_duration_seconds(url):
    """Duration of a remote media file via ffprobe, or None.

    probe_video() is deliberately local-only (it verifies downloaded
    files); segment evidence for a direct URL needs the SOURCE duration
    before any download decision, so it is probed here, bounded.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", str(url),
            ],
            capture_output=True, text=True, timeout=25,
        )
        if result.returncode != 0:
            return None
        info = json.loads(result.stdout or "{}")
        value = (info.get("format") or {}).get("duration")
        return float(value) if value else None
    except Exception:
        return None


def _provider_duration_seconds(candidate):
    """Duration claimed by the candidate's own provider metadata (VA-03).

    Returns (seconds_or_None, source_label). Accepts plain seconds and
    HH:MM:SS / MM:SS strings; anything else is ignored, never guessed.
    This is a FALLBACK for establishing a source's duration when the
    primary channels (remote probe / yt-dlp info) cannot — it is used
    only for the whole-asset determination, never to invent a segment
    inside a long video.
    """
    raw = candidate.get("duration_seconds")
    if raw is None:
        raw = candidate.get("duration")
    value = None
    if isinstance(raw, (int, float)) and raw > 0:
        value = float(raw)
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            value = float(text)
        except ValueError:
            try:
                nums = [float(part) for part in text.split(":")]
                if len(nums) == 2:
                    value = nums[0] * 60 + nums[1]
                elif len(nums) == 3:
                    value = nums[0] * 3600 + nums[1] * 60 + nums[2]
            except ValueError:
                value = None
    if value and value > 0:
        return value, str(
            candidate.get("provider") or candidate.get("source") or "provider"
        )
    return None, ""


def _segment_start_detailed(url, subject="", place="", event="", era="",
                            duration=DEFAULT_CLIP_DURATION,
                            provider_duration=None,
                            provider_duration_source=""):
    """Provider-neutral segment evidence (MS-3, extended VA-03/VA-06).

    Returns (start_seconds, evidence) or (None, "none"). Evidence
    classes:
    - "whole_asset": the source is short enough (<= 2x the clip length)
      that starting at 0 is the asset itself, not an arbitrary window
      of a long video. The duration may come from a remote probe,
      yt-dlp info, or — as a recorded fallback — the provider's own
      metadata (archive.org file lengths, Wikimedia videoinfo).
    - "verified_segment": the transcript/chapter/description evidence
      machinery (_youtube_start, yt-dlp based and NOT YouTube-only)
      selected a segment matched to the visual contract.
    - "positional_no_contract": the contract was empty and
      _youtube_start chose an evenly spaced position. That is an
      estimate, NOT verified evidence, and is labelled accordingly
      (VA-06).
    - "none": no evidence — the caller must reject the segment rather
      than invent a timestamp. Timestamp zero is never assumed for a
      long source.

    Every source tried is recorded in _SEGMENT_ATTEMPTS[url], and a
    failed lookup records its cause in _SEGMENT_FAILURE_CAUSE[url]
    ("info_unavailable" when the information channel itself failed,
    vs "no_evidence" when the duration was known but no timed
    evidence exists). A failed lookup NEVER becomes a segment.
    """
    if not url:
        return None, "none"
    clip = int(duration or DEFAULT_CLIP_DURATION)
    attempts = []
    _SEGMENT_ATTEMPTS[url] = attempts

    if is_direct_media_url(url):
        source_duration = _remote_duration_seconds(url)
        if source_duration:
            _SOURCE_DURATION_CACHE[url] = float(source_duration)
            attempts.append({
                "source": "remote_probe", "outcome": "duration_known",
                "duration_seconds": float(source_duration),
            })
        else:
            attempts.append({"source": "remote_probe", "outcome": "unavailable"})
            if provider_duration:
                _SOURCE_DURATION_CACHE[url] = float(provider_duration)
                attempts.append({
                    "source": "provider_metadata", "outcome": "duration_known",
                    "duration_seconds": float(provider_duration),
                    "provider": provider_duration_source,
                })
        source_duration = _SOURCE_DURATION_CACHE.get(url)
        if source_duration and float(source_duration) <= clip * 2:
            return 0, "whole_asset"
        if source_duration:
            # A long direct file has no timed-evidence channel; the
            # honest outcome is rejection, never an invented window.
            attempts.append({
                "source": "timed_evidence",
                "outcome": "no_path_for_direct_media",
            })
            _SEGMENT_FAILURE_CAUSE[url] = "no_evidence_path_for_long_direct_media"
        else:
            _SEGMENT_FAILURE_CAUSE[url] = "duration_unknown"
        return None, "none"

    # Page URLs on any yt-dlp-supported site: a short source qualifies
    # as a whole asset; longer sources need timed evidence.
    slim = _youtube_slim(str(url).split("&", 1)[0].strip())
    source_duration = None
    if slim and slim.get("duration"):
        try:
            source_duration = float(slim["duration"])
        except (TypeError, ValueError):
            source_duration = None
    if source_duration:
        _SOURCE_DURATION_CACHE[url] = float(source_duration)
        attempts.append({
            "source": "yt_dlp_info", "outcome": "duration_known",
            "duration_seconds": float(source_duration),
        })
    else:
        attempts.append({"source": "yt_dlp_info", "outcome": "unavailable"})
        if provider_duration:
            source_duration = float(provider_duration)
            _SOURCE_DURATION_CACHE[url] = source_duration
            attempts.append({
                "source": "provider_metadata", "outcome": "duration_known",
                "duration_seconds": source_duration,
                "provider": provider_duration_source,
            })
    if source_duration and source_duration <= clip * 2:
        return 0, "whole_asset"

    start = _youtube_start(
        url, subject=subject, place=place, event=event, era=era
    )
    basis = _SEGMENT_BASIS.get(str(url).split("&", 1)[0].strip())
    if start is None:
        attempts.append({"source": "timed_evidence", "outcome": "none_found"})
        _SEGMENT_FAILURE_CAUSE[url] = (
            "no_evidence" if source_duration else "info_unavailable"
        )
        return None, "none"
    attempts.append({
        "source": "timed_evidence", "outcome": basis or "verified_segment",
    })
    if basis == "positional_no_contract":
        return int(start), "positional_no_contract"
    return int(start), "verified_segment"


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
        candidate["_download_error"] = "no_url"
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

    # MS-3: segment evidence is provider-neutral. ANY source — YouTube,
    # Vimeo, a news page, an archive file — must present evidence for
    # its segment: a whole-asset case (short source) or a verified
    # timed segment. No evidence -> reject; timestamp zero is never
    # silently assumed for a long video.
    provider_duration, provider_duration_source = (
        _provider_duration_seconds(candidate)
    )
    start, segment_evidence = _segment_start_detailed(
        url,
        subject=subject,
        place=place,
        event=event,
        era=era,
        duration=DEFAULT_CLIP_DURATION,
        provider_duration=provider_duration,
        provider_duration_source=provider_duration_source,
    )

    if start is None:
        print(
            f"SOURCE REJECTED (no segment evidence): "
            f"subject='{subject}' "
            f"place='{place}' "
            f"event='{event}' "
            f"era='{era}' "
            f"url={url}"
        )
        candidate["_download_error"] = "no_segment_evidence"
        # VA-03: preserve WHY evidence failed (broken information
        # channel vs genuinely absent evidence) and every source tried.
        candidate["_segment_attempts"] = list(_SEGMENT_ATTEMPTS.get(url) or [])
        candidate["_segment_failure_cause"] = (
            _SEGMENT_FAILURE_CAUSE.get(url) or ""
        )
        candidate["rejection_stage"] = "segment_evidence"
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
        candidate["_download_error"] = f"download_exception: {exc}"
        return None

    if not ok:
        candidate["_download_error"] = "download_failed"
        return None

    if not validate_video(output_path):
        candidate["_download_error"] = "invalid_media"
        try:
            output_path.unlink()
        except OSError:
            pass

        return None

    # AUD-17: record the file's REAL probed facts. The asset previously
    # claimed duration_seconds = DEFAULT_CLIP_DURATION (8s) regardless of
    # what was actually downloaded.
    probe = probe_video(output_path)
    if probe is None:
        candidate["_download_error"] = "probe_failed"
        try:
            output_path.unlink()
        except OSError:
            pass

        return None

    # VA-12: defensible technical minimums, recorded distinctly from
    # relevance rejections — the facts and the failed checks travel
    # with the candidate into the failure records and the report.
    technical_failures = _video_technical_failures(probe)
    if technical_failures:
        candidate["_download_error"] = "technical_below_minimum"
        candidate["_technical_rejection"] = {
            "facts": probe,
            "failed": technical_failures,
            "minimums": _technical_minimums(),
        }
        candidate["rejection_stage"] = "technical_validation"
        try:
            output_path.unlink()
        except OSError:
            pass
        return None

    asset = dict(candidate)
    asset.pop("_download_error", None)
    _download_provenance = DOWNLOAD_PROVENANCE.pop(str(output_path), {}) or {}

    asset.update(
        {
            "file_path": str(output_path),
            "path": str(output_path),
            "video_url": url,
            "url": url,
            "type": "video",
            "visual_type": "video",
            "duration_seconds": probe["duration_seconds"],
            "technical_checks": {
                "passed": True,
                "facts": {
                    "width": probe["width"],
                    "height": probe["height"],
                    "fps": probe["fps"],
                    "codec": probe["codec"],
                    "has_audio": probe["has_audio"],
                },
                "minimums": _technical_minimums(),
            },
            "sentence_number": sentence_number,
            "clip_start": start,
            # MS-3: how the start time was evidenced — whole_asset or
            # verified_segment; recorded on every accepted video.
            "segment_evidence": segment_evidence,
            "segment_evidence_basis": (
                _SEGMENT_BASIS.get(str(url).split("&", 1)[0].strip())
                or segment_evidence
            ),
            # Segment-retrieval provenance: the ORIGINAL source
            # duration (when the evidence stage learned it), how the
            # clip was retrieved, and whether retrieval was genuinely
            # partial. Unknown stays None — never guessed.
            "source_duration_seconds": _SOURCE_DURATION_CACHE.get(url),
            "download_method": _download_provenance.get("method"),
            "partial_download": _download_provenance.get("partial"),
            "clip_duration": probe["duration_seconds"],
            "width": probe["width"],
            "height": probe["height"],
            "fps": probe["fps"],
            "codec": probe["codec"],
            "has_audio": probe["has_audio"],
            "probed": True,
            # AUD-15 rights evidence: who made it (when the provider says),
            # and when WE retrieved it. Licence stays whatever the
            # candidate honestly carried — "unknown" for YouTube.
            "creator": candidate.get("creator")
            or candidate.get("channel")
            or "",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    print(
        f"YOUTUBE ASSET READY: "
        f"start={start}s "
        f"duration={probe['duration_seconds']}s "
        f"subject='{subject}' "
        f"place='{place}' "
        f"event='{event}' "
        f"era='{era}' "
        f"path={output_path}"
    )

    return asset


def _uve_download_image_candidate(
    candidate,
    scene_dir,
    sentence_number,
    asset_index,
):
    """Download one verified image candidate into a real image asset (AUD-13).

    Mirrors _uve_download_video_candidate: the file is downloaded with real
    content validation (download_valid_image decodes it via Pillow), then
    probed so the asset records true dimensions/format. Licence stays
    "unknown" unless the candidate itself carries evidence — it is never
    upgraded to a clearance it does not have.
    """
    if not isinstance(candidate, dict):
        return None

    image_url = str(
        candidate.get("image_url")
        or candidate.get("url")
        or ""
    ).strip()

    if not image_url:
        return None

    safe_query = str(
        candidate.get("matched_query")
        or candidate.get("query")
        or "image"
    ).strip()
    safe_query = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        safe_query,
    ).strip("_")[:80] or "image"

    url_path = image_url.split("?", 1)[0].lower()
    extension = ".jpg"
    for suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if url_path.endswith(suffix):
            extension = suffix
            break

    output_path = Path(scene_dir) / (
        f"sentence_{int(sentence_number):04d}"
        f"_image_{int(asset_index):02d}"
        f"_{safe_query}{extension}"
    )

    ok, reason = _download_image_detailed(image_url, output_path)
    if not ok:
        # VA-13: image failures carry explicit reason codes, exactly
        # like the video path — never a silent None.
        candidate["_download_error"] = reason
        candidate["rejection_stage"] = (
            "technical_validation"
            if reason in ("undersized_file", "decode_failed", "probe_failed")
            else "download"
        )
        return None

    probe = probe_image(output_path)
    if probe is None:
        candidate["_download_error"] = "probe_failed"
        candidate["rejection_stage"] = "technical_validation"
        try:
            output_path.unlink()
        except OSError:
            pass
        return None

    # VA-12: image technical minimums (thumbnail-grade stills cannot
    # serve a 1080p timeline).
    image_failures = _image_technical_failures(probe)
    if image_failures:
        candidate["_download_error"] = "technical_below_minimum"
        candidate["_technical_rejection"] = {
            "facts": probe,
            "failed": image_failures,
            "minimums": _technical_minimums(),
        }
        candidate["rejection_stage"] = "technical_validation"
        try:
            output_path.unlink()
        except OSError:
            pass
        return None

    page_url = str(
        candidate.get("source_url")
        or candidate.get("link")
        or ""
    ).strip()

    asset = dict(candidate)
    asset.update(
        {
            "file_path": str(output_path),
            "path": str(output_path),
            "image_url": image_url,
            "url": page_url or image_url,
            "source_url": page_url,
            "type": "image",
            "visual_type": "image",
            "sentence_number": sentence_number,
            "width": probe["width"],
            "height": probe["height"],
            "format": probe["format"],
            "probed": True,
            "technical_checks": {
                "passed": True,
                "facts": {"width": probe["width"], "height": probe["height"]},
                "minimums": _technical_minimums(),
            },
            "license": candidate.get("license") or "unknown",
            "rights_status": candidate.get("rights_status")
            or "verify_before_publish",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    print(
        f"IMAGE ASSET READY: "
        f"{probe['width']}x{probe['height']} "
        f"source={candidate.get('source') or ''} "
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
    images = []

    seen_video_urls = set()
    seen_web_urls = set()
    seen_image_urls = set()

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

            images.extend(
                cached.get("images") or []
            )

            continue

        try:
            found_videos = search_videos_multi(
                query,
                limit=8,
            )
        except Exception as error:
            _record_discovery_failure("serper-videos", query, error)
            print(
                f"Video search failed "
                f"for '{query}': {error}"
            )
            found_videos = []

        try:
            found_web = search_web_sources(
                query,
                limit=5,
            )
        except Exception as error:
            _record_discovery_failure("serper-web", query, error)
            print(
                f"Web search failed "
                f"for '{query}': {error}"
            )
            found_web = []

        try:
            found_images = search_image_sources(
                query,
                limit=5,
            )
        except Exception as error:
            _record_discovery_failure("serper-images", query, error)
            print(
                f"Image search failed "
                f"for '{query}': {error}"
            )
            found_images = []

        # Wikimedia Commons fallback (AUD-15): appended after Serper
        # results; carries genuine licence evidence when available.
        try:
            found_images = found_images + search_wikimedia_images(
                query,
                limit=3,
            )
        except Exception as error:
            _record_discovery_failure("wikimedia", query, error)
            print(
                f"Wikimedia search failed "
                f"for '{query}': {error}"
            )

        search_cache[cache_key] = {
            "videos": found_videos,
            "web_sources": found_web,
            "images": found_images,
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

        for image in found_images:
            image_url = str(
                image.get("image_url")
                or ""
            ).strip()

            if not image_url:
                continue

            if image_url in seen_image_urls:
                continue

            seen_image_urls.add(image_url)
            images.append(image)

    # F-3: format-aware discovery. When the scenario's format intent is
    # a map or a document with real confidence, run ONE additional
    # intent-shaped image search so those formats are sought
    # deliberately instead of found by luck. The shaped query is a
    # discovery aid only — classification of anything it returns still
    # requires metadata/source evidence (classify_visual_format).
    _intent = str(scenario.get("format_intent") or "any").lower()
    try:
        _intent_conf = float(scenario.get("format_intent_confidence") or 0)
    except (TypeError, ValueError):
        _intent_conf = 0.0
    if _intent in {"map", "document"} and _intent_conf >= 0.5 and unique_queries:
        shaped_query = f"{unique_queries[0]} {_intent}"
        shaped_images = []
        try:
            shaped_images = search_image_sources(shaped_query, limit=5)
        except Exception as error:
            _record_discovery_failure("serper-images", shaped_query, error)
            print(f"Shaped image search failed for '{shaped_query}': {error}")
        try:
            shaped_images = shaped_images + search_wikimedia_images(
                shaped_query, limit=3
            )
        except Exception as error:
            _record_discovery_failure("wikimedia", shaped_query, error)
            print(f"Shaped Wikimedia search failed for '{shaped_query}': {error}")
        for image in shaped_images:
            image_url = str(image.get("image_url") or "").strip()
            if not image_url or image_url in seen_image_urls:
                continue
            seen_image_urls.add(image_url)
            images.append(image)

    # MS-4: supplementary independent providers, queried once per
    # scenario on its primary query. Each is failure-isolated: one
    # provider being down or empty never blocks the others, and none
    # of them is the centre of discovery — they join the same pool.
    if unique_queries:
        primary_query = unique_queries[0]

        def _merge_videos(found):
            for video in found or []:
                url = str(
                    video.get("video_url") or video.get("url") or ""
                ).strip()
                if not url or url in seen_video_urls:
                    continue
                seen_video_urls.add(url)
                videos.append(video)

        try:
            _merge_videos(search_archive_org_videos(primary_query, limit=3))
        except Exception as error:
            _record_discovery_failure("archive.org", primary_query, error)
            print(f"Archive.org search failed for '{primary_query}': {error}")

        try:
            _merge_videos(search_wikimedia_videos(primary_query, limit=5))
        except Exception as error:
            _record_discovery_failure("wikimedia", primary_query, error)
            print(f"Wikimedia video search failed for '{primary_query}': {error}")

        # Targeted institutional search: only when general discovery
        # found no moving footage at all for this scenario.
        if not videos:
            try:
                targeted_query = (
                    f"{primary_query} site:archive.org OR site:loc.gov "
                    f"OR site:si.edu OR site:nps.gov"
                )
                _merge_videos(search_videos_multi(targeted_query, limit=5))
            except Exception as error:
                _record_discovery_failure("serper-videos-targeted", primary_query, error)
                print(f"Targeted search failed for '{primary_query}': {error}")

    return videos, web_sources, images

def _candidate_id_for(candidate):
    """Stable per-run candidate identifier (VA-08).

    Derived from the candidate's CANONICAL source identity (Phase 2,
    canonical DM-16), so the same source carries the same id through
    verification, selection, download and reporting within a run —
    however its URL was spelled by whichever search seam found it.
    """
    import hashlib

    key = canonical_candidate_key(candidate)
    return "cand_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _uve_verify_all(candidates, scenario, bible, shot_need=None):
    scored, seen = [], set()

    # Phase 1: enrich the gate's shot need (a COPY — the caller's dict
    # is never mutated) with the bible's other people/places so the
    # gate can judge positive contradictions where it has the data;
    # verify_candidate applies the same rules from the bible directly.
    gate_need = shot_need
    if shot_need is not None and bible:
        gate_need = dict(shot_need)
        _entities = bible.get("entities") or []
        _req_id = shot_need.get("required_subject_id")
        gate_need.setdefault("other_people", [
            e.get("canonical") for e in _entities
            if e.get("type") == "person" and e.get("id") != _req_id
            and e.get("canonical")
        ])
        _place_norm = str(shot_need.get("place") or "").strip().lower()
        gate_need.setdefault("other_places", [
            e.get("canonical") for e in _entities
            if e.get("type") in {"place", "location"}
            and e.get("canonical")
            and str(e.get("canonical")).strip().lower() != _place_norm
        ])

    for candidate in candidates:
        candidate["candidate_id"] = _candidate_id_for(candidate)
        key = _uve_candidate_key(candidate)

        if not key or key in seen:
            continue

        seen.add(key)

        # Cheap metadata gate BEFORE expensive LLM verification.
        ok, reason = gate_shot_need(
            candidate,
            gate_need,
        )

        if not ok:
            item = dict(candidate)
            source_cache.bump("gate_rejects")
            item["reject_reason"] = reason
            item["rejection_stage"] = "metadata_gate"
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
        if not verdict.get("accepted"):
            # VA-04/VA-08: scoring rejections used to leave NO record —
            # the verdict (score, contributions, reason) was computed
            # and then dropped. Stamp the reason and stage so the
            # review log and asset report can explain the decision.
            item["reject_reason"] = (
                verdict.get("rejection_reason") or "score_below_threshold"
            )
            item["rejection_stage"] = "scoring"
        scored.append(item)

    scored.sort(
        key=lambda c: c["universal_score"],
        reverse=True,
    )

    return scored


# RELAXED_MIN_SCORE was removed with the relaxed pick pool (AUD-16):
# verifier-rejected candidates can no longer enter the accepted pool.

def _title_blob(candidate):
    """Candidate-supplied metadata ONLY (VA-05).

    The matched search query used to be included here, which let the
    query's own words satisfy the gate's subject/place checks — an
    irrelevant candidate could pass because the QUERY mentioned the
    subject. The query remains on the candidate for diagnostics and
    reporting; it is no longer evidence about the candidate.
    """
    return " ".join([
        str(candidate.get("title") or ""),
        str(candidate.get("snippet") or ""),
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

    # Workstream J: banned providers are rejected at the gate too —
    # the canonical banned-provider outcome, whatever seam produced
    # the candidate (verify_candidate enforces the same rejection for
    # direct callers).
    if story.is_banned_provider(candidate):
        return False, story.BANNED_PROVIDER_REASON

    # Quality Recovery Phase 1 (workstream A): POSITIVE contradictions
    # the shot need carries enough data to judge. An explicit date
    # incompatible with the need's era is a contradiction; a missing
    # date is not. When the caller supplies the bible's OTHER people /
    # places (see _uve_verify_all), naming one of them while not
    # naming the required subject/place is a contradiction — absence
    # alone stays on the missing_* paths below.
    if story.era_compatibility(
        need.get("era"), story.extract_years(blob)
    ) is False:
        return False, story.CONTRADICTORY_DATE

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

    # Positive person/place contradictions (only when the caller
    # supplied the alternatives — _uve_verify_all enriches the need
    # from the case bible; direct callers without them skip to the
    # absence checks, and verify_candidate still applies the full
    # bible-based contradiction rules).
    def _named(name):
        text = str(name or "").strip().lower()
        if not text:
            return False
        if text in blob:
            return True
        parts = [p for p in text.split() if len(p) > 2]
        return bool(parts) and all(p in blob for p in parts)

    if subject and not _named(subject):
        for other in need.get("other_people") or []:
            if _named(other):
                return False, story.CONTRADICTORY_PERSON
    _need_place = str(need.get("place") or "").strip().lower()
    if _need_place and not _named(_need_place):
        for other in need.get("other_places") or []:
            if _named(other):
                return False, story.CONTRADICTORY_PLACE

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

# Quality Recovery Phase 2 (workstream C): selection-time REUSE
# PENALTY per prior use of a candidate's canonical source. Bounded by
# construction: at the hard cap of 3 uses the maximum differential is
# 2 x 0.05 = 0.10, so a candidate that is MATERIALLY more relevant
# (raw-score gap > 0.10) can never be overtaken by a fresher one, and
# the pool here contains only verifier-ACCEPTED candidates, so the
# penalty can never promote an unverified candidate. The hard source
# cap stays absolute and is enforced separately below.
REUSE_PENALTY_PER_USE = 0.05


def _uve_pick(
    scored,
    scenario,
    registry,
    used_keys,
    limit=3,
    shot_need=None,
    reuse_penalty_per_use=None,
):
    pool = []

    # VA-11: the metadata gate runs ONCE, in _uve_verify_all, which
    # stamps reject_reason on every candidate it rejects. This function
    # previously re-ran gate_shot_need on every candidate — duplicating
    # the work, double-printing every REJECT line (which inflated
    # rejection counts in logs ~2x), and re-stamping the same reasons.
    # The pick now trusts the recorded gate outcome: a candidate is
    # eligible only when the verifier accepted it. (The unused
    # strict_specific computation that also lived here was dead code
    # and has been removed.)
    for candidate in scored:
        verification = (
            candidate.get("verification")
            or {}
        )

        if verification.get("accepted"):
            candidate["verification_status"] = "verified"
            pool.append(candidate)

    # AUD-16 / acceptance criterion 4: there is NO relaxed pool. The old
    # code admitted verifier-REJECTED candidates at score >= 0.15 — below
    # the verifier's own lowest acceptance threshold — and labelled them
    # "relaxed". Only candidates the verifier accepted may be picked; if
    # none were accepted, the beat falls back honestly (real image, then a
    # counted card) instead of using rejected footage.

    # Phase 2 reuse penalty: re-rank the ACCEPTED pool by effective
    # score (raw selection score minus penalty x canonical source
    # uses), so a fresh near-equal source beats a reused one. Stable
    # sort: equal effective scores keep the relevance order.
    _per_use = (
        REUSE_PENALTY_PER_USE
        if reuse_penalty_per_use is None
        else float(reuse_penalty_per_use)
    )
    if _per_use:
        def _effective(candidate):
            raw = candidate.get("_selection_score")
            if raw is None:
                raw = candidate.get("universal_score") or 0
            try:
                uses = registry.source_usage_count(
                    str(
                        candidate.get("url")
                        or candidate.get("video_url")
                        or candidate.get("link")
                        or ""
                    )
                )
            except Exception:
                uses = 0
            penalty = _per_use * uses
            candidate["reuse_penalty"] = round(penalty, 3)
            candidate["_effective_score"] = round((raw or 0) - penalty, 3)
            return (raw or 0) - penalty

        pool = sorted(pool, key=_effective, reverse=True)

    picked = []
    source_counts = {}

    for candidate in pool:
        # MS-1: the per-source usage cap applies to EVERY provider
        # equally. Previously it applied only to YouTube URLs.
        # Phase 2: counted on the CANONICAL source key, so respelled
        # URLs of one source share the cap.
        candidate_url = str(
            candidate.get("url")
            or candidate.get("video_url")
            or candidate.get("link")
            or ""
        ).strip()
        source_key = canonical_source_key(candidate_url)

        batch_uses = source_counts.get(source_key, 0)
        try:
            existing_uses = registry.source_usage_count(candidate_url)
        except Exception:
            existing_uses = 0

        if existing_uses + batch_uses >= registry.max_source_uses:
            candidate["reject_reason"] = "source_usage_limit"
            candidate["rejection_stage"] = "selection"
            continue

        key = _uve_candidate_key(candidate)

        if not key:
            continue

        # MS-1: source reuse is provider-neutral. Previously only YouTube
        # sources could be re-picked (the segment machinery would choose
        # a different timestamp); other providers were silently skipped
        # once used. Segment differentiation now exists for every
        # provider (MS-3 evidence + registry segment ranges), so the
        # carve-out is removed.
        try:
            allowed, why = registry.can_use(
                url=key,
                title=result_title(candidate),
                allow_source_reuse=True,
            )
        except Exception:
            allowed, why = True, ""

        if not allowed:
            candidate["rejection_stage"] = "selection"
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

        source_counts[source_key] = source_counts.get(source_key, 0) + 1

        if len(picked) >= limit:
            break

    return _apply_source_quality_tiebreak(picked)


_TIEBREAK_EPSILON = 0.02


def _apply_source_quality_tiebreak(picked):
    """MS-5: reconnect visual_selector as a TIE-BREAK ONLY.

    Picks arrive relevance-ordered. Candidates whose universal_scores
    sit within a small epsilon (0.02) of their group's leader are
    re-ordered by source-quality (domain) score; a relevance gap
    larger than the epsilon can NEVER be overridden by provider
    prestige. Every pick is stamped with source_quality for reporting.

    Phase 2: grouping uses the Phase 2 EFFECTIVE score (raw minus
    reuse penalty) when _uve_pick stamped one — otherwise the
    penalty's fresh-source preference at a near-tie would be undone
    here by the very domain prestige it must not be overridden by.
    """
    if len(picked) < 2:
        return picked
    try:
        from services.visual_selector import get_domain_score
    except Exception:
        return picked

    for item in picked:
        url = (
            item.get("url") or item.get("video_url")
            or item.get("image_url") or ""
        )
        try:
            item["source_quality"] = get_domain_score(url)
        except Exception:
            item["source_quality"] = 0

    ordered, group = [], []

    def flush():
        if group:
            ordered.extend(
                sorted(
                    group,
                    key=lambda i: i.get("source_quality") or 0,
                    reverse=True,
                )
            )
            group.clear()

    def _rank_score(item):
        effective = item.get("_effective_score")
        return effective if effective is not None else (item.get("universal_score") or 0)

    for item in picked:
        score = _rank_score(item)
        if group and abs(_rank_score(group[0]) - score) > _TIEBREAK_EPSILON:
            flush()
        group.append(item)
    flush()
    return ordered


def _uve_download_any_candidate(candidate, scene_dir, sentence_number, asset_index):
    """F-2 dispatcher: route a unified-pool pick to its format's
    downloader. The pool kind was stamped when the pool was built."""
    if candidate.get("_pool_kind") == "image":
        return _uve_download_image_candidate(
            candidate, scene_dir, sentence_number, asset_index
        )
    return _uve_download_video_candidate(
        candidate, scene_dir, sentence_number, asset_index
    )


def _uve_fetch_first(candidates, downloader, scene_dir, sentence_number, asset_index, registry, used_keys, failures=None, transcript_scenario=None, transcript_bible=None):
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        candidate_url = str(
            candidate.get("url")
            or candidate.get("video_url")
            or candidate.get("image_url")
            or ""
        )
        # Phase 2: the failed-download guard keys on the CANONICAL
        # source identity — a failed candidate is never re-downloaded
        # within a run, however its URL is respelled.
        candidate_key = canonical_source_key(candidate_url)
        if candidate_url and candidate_key in _FAILED_DOWNLOAD_URLS:
            # Already failed once this run (throttled, dead, invalid):
            # never re-request it; record the skip as a failure too.
            if failures is not None:
                failures.append(
                    {
                        "provider": candidate.get("provider")
                        or candidate.get("source")
                        or "unknown",
                        "url": candidate_url,
                        "reason": "previously_failed_this_run",
                    }
                )
            continue

        # Stage 0 metrics: every real download attempt passes
        # through this loop (previously-failed URLs skipped above are
        # not attempts — nothing is requested for them).
        run_metrics.download_attempted()
        asset = downloader(
            candidate,
            scene_dir,
            sentence_number,
            asset_index,
        )

        if not asset:
            if candidate_url:
                _FAILED_DOWNLOAD_URLS.add(candidate_key)
            # MS-2: a failed download is recorded with its actual reason
            # (stashed on the candidate by the downloader) — never silent,
            # never substituted with unrelated footage.
            if failures is not None:
                failure = {
                    "provider": candidate.get("provider")
                    or candidate.get("source")
                    or "unknown",
                    "url": str(
                        candidate.get("url")
                        or candidate.get("video_url")
                        or candidate.get("image_url")
                        or ""
                    ),
                    "reason": candidate.get("_download_error")
                    or "download_failed",
                }
                failure["candidate_id"] = candidate.get("candidate_id")
                failure.setdefault(
                    "stage", candidate.get("rejection_stage") or "download"
                )
                if candidate.get("_technical_rejection"):
                    failure["technical_rejection"] = (
                        candidate["_technical_rejection"]
                    )
                if candidate.get("_segment_failure_cause"):
                    failure["segment_failure_cause"] = (
                        candidate["_segment_failure_cause"]
                    )
                if candidate.get("_segment_attempts"):
                    failure["segment_attempts"] = candidate["_segment_attempts"]
                if candidate.get("rejection_stage"):
                    failure["stage"] = candidate["rejection_stage"]
                failures.append(failure)
            continue

        # Stage 0 metrics: the download produced a file — count it
        # and its size as downloaded bytes (measured now, while the
        # file the downloader wrote is on disk).
        _dl_path = asset.get("file_path") or asset.get("path")
        _dl_bytes = 0
        try:
            if _dl_path and os.path.exists(_dl_path):
                _dl_bytes = os.path.getsize(_dl_path)
        except OSError:
            _dl_bytes = 0
        run_metrics.download_succeeded(_dl_bytes)

        key = _uve_candidate_key(asset)

        if key and key in used_keys:
            try:
                path = asset.get("file_path")
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
            continue

        # Stage 3B: transcript-interval verification. The asset was
        # accepted by verify_candidate and carries real segment
        # evidence; if the spoken words inside the chosen interval
        # contain none of the scenario's subject/topical tokens, the
        # candidate is DEMOTED (never upgraded) before it is
        # registered or returned: the downloaded file is removed, the
        # rejection is recorded, and the loop moves to the next pick.
        if transcript_scenario is not None:
            check = _apply_transcript_check(
                asset, transcript_scenario, transcript_bible
            )
            if check == "transcript_mismatch":
                print(
                    f"      TRANSCRIPT MISMATCH (demoted): "
                    f"{candidate_url} start={asset.get('clip_start')}"
                )
                if failures is not None:
                    failures.append({
                        "provider": candidate.get("provider")
                        or candidate.get("source")
                        or "unknown",
                        "url": candidate_url,
                        "reason": "transcript_mismatch",
                        "candidate_id": candidate.get("candidate_id"),
                        "stage": "transcript_verification",
                    })
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
            # Phase 2: registry identity is canonical (the registry
            # canonicalises internally too); the clip key is built on
            # the canonical source key so segment bookkeeping is
            # spelling-independent.
            registry_url = str(
                asset.get("video_url")
                or asset.get("url")
                or key
                or ""
            )

            clip_start = asset.get("clip_start")

            if clip_start is not None:
                try:
                    clip_start = int(float(clip_start))
                except (TypeError, ValueError):
                    clip_start = str(clip_start)

                clip_key = (
                    f"{canonical_source_key(registry_url)}|{clip_start}"
                )
            else:
                clip_key = key

            # AUD-20: pass the segment range so the registry's overlap
            # protection actually engages — register() supported start/end
            # all along, but this call never supplied them, leaving
            # used_segments permanently empty.
            segment_start = None
            segment_end = None
            if isinstance(clip_start, (int, float)):
                segment_start = float(clip_start)
                raw_duration = (
                    asset.get("duration_seconds")
                    or asset.get("clip_duration")
                    or DEFAULT_CLIP_DURATION
                )
                try:
                    segment_end = segment_start + float(raw_duration)
                except (TypeError, ValueError):
                    segment_end = None

            registry.register(
                path=asset.get("file_path"),
                url=registry_url,
                title=asset.get("title", ""),
                clip_key=clip_key,
                start=segment_start,
                end=segment_end,
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

def _uve_search_and_pick(
    scenario, shot_need, *,
    search_cache, project_id, bible, registry, used_keys,
    scene_dir, scene_stats, beat, beat_videos, beat_images,
    scenario_index, queries, review_records,
):
    """One full discovery pass for a scenario.

    Search every enabled provider, verify every candidate, rank the
    survivors in the unified format-aware pool, pick under the
    verifier's acceptance rules and download the winner. All accounting
    (provider funnel, candidate totals, rejection reasons, download
    failures, discovery failures, review record) is recorded on
    scene_stats / review_records. An accepted asset is appended to
    beat_videos / beat_images and returned; otherwise None is returned
    and the caller decides about coverage recovery. Nothing here ever
    fabricates or substitutes an asset.
    """
    raw_videos, raw_web_sources, raw_images = _uve_scenario_search(scenario, search_cache, project_id, shot_need)
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
    scored_images = _uve_verify_all(
        raw_images,
        scenario,
        bible,
        shot_need=shot_need,
    )
    print(f"      candidates: videos={len(scored_videos)} web={len(scored_web_sources)} images={len(scored_images)}")

    # F-4/MS-6: provider + kind funnels for the asset
    # report. Discovered = raw pool size; verified/rejected
    # from the scored lists (the verifier's own verdicts).
    _provider_funnel = scene_stats.setdefault("providers", {})
    _kind_funnel = scene_stats.setdefault("kinds", {})

    def _funnel_add(pool, scored, kind):
        kind_counts = _kind_funnel.setdefault(
            kind, {"discovered": 0, "verified": 0, "rejected": 0}
        )
        kind_counts["discovered"] += len(pool)
        for cand in pool:
            provider = str(
                cand.get("provider") or cand.get("source") or "unknown"
            )
            _provider_funnel.setdefault(
                provider,
                {"discovered": 0, "verified": 0, "rejected": 0, "downloaded": 0, "download_failed": 0},
            )["discovered"] += 1
        for cand in scored:
            provider = str(
                cand.get("provider") or cand.get("source") or "unknown"
            )
            entry = _provider_funnel.setdefault(
                provider,
                {"discovered": 0, "verified": 0, "rejected": 0, "downloaded": 0, "download_failed": 0},
            )
            if (cand.get("verification") or {}).get("accepted"):
                entry["verified"] += 1
                kind_counts["verified"] += 1
            else:
                entry["rejected"] += 1
                kind_counts["rejected"] += 1

    _funnel_add(raw_videos, scored_videos, "video")
    _funnel_add(raw_web_sources, scored_web_sources, "web")
    _funnel_add(raw_images, scored_images, "image")

    # Stage 0 metrics: candidates discovered in this pass — the same
    # raw pools the funnels above are built from.
    run_metrics.candidates_discovered(
        len(raw_videos) + len(raw_web_sources) + len(raw_images)
    )

    scene_stats["candidates_considered"] += len(scored_videos) + len(scored_images)
    scene_stats["candidates_rejected"] += sum(
        1
        for item in (scored_videos + scored_web_sources + scored_images)
        if item.get("reject_reason")
    )

    # Failure classification: every rejected candidate contributes its
    # reason (gate reason, or score_below_threshold when the verifier
    # scored it under the acceptance bar) to the scene's totals, which
    # the asset report aggregates. Nothing is rejected silently.
    _reasons = scene_stats.setdefault("rejection_reasons", {})
    for _item in (scored_videos + scored_web_sources + scored_images):
        if (_item.get("verification") or {}).get("accepted"):
            continue
        _reason = _item.get("reject_reason") or "score_below_threshold"
        _reasons[_reason] = _reasons.get(_reason, 0) + 1

    # F-2: UNIFIED SELECTION. Verified video candidates,
    # footage-capable web candidates (MS-2) and verified image
    # candidates compete in ONE pool for this scenario. Ranking
    # is relevance (universal_score) adjusted by format-intent
    # fit — a bounded bonus (max 0.10) that can tip a close
    # contest toward the format that best communicates the
    # sentence, but can never rescue a weak candidate over a
    # clearly more relevant one of another format, and never
    # admits a verifier-rejected candidate (_uve_pick still
    # enforces acceptance). No format is tried "first" any
    # more: the old video->image cascade is gone.
    intent = str(scenario.get("format_intent") or "any").lower()
    intent_conf = scenario.get("format_intent_confidence") or 0.0
    try:
        intent_conf = float(intent_conf)
    except (TypeError, ValueError):
        intent_conf = 0.0

    def _intent_bonus(kind):
        if intent == "any" or intent_conf <= 0:
            return 0.0
        if kind == "video":
            return 0.10 * intent_conf if intent == "footage" else 0.0
        # image-side candidates serve photograph/document/
        # map/chart intents fully, graphic intents partially.
        if intent in {"photograph", "document", "map", "chart"}:
            return 0.10 * intent_conf
        if intent == "graphic":
            return 0.05 * intent_conf
        return 0.0

    combined_pool = []
    pool_keys = set()
    for item in scored_videos:
        item = dict(item)
        item["_pool_kind"] = "video"
        item["_selection_score"] = (item.get("universal_score") or 0) + _intent_bonus("video")
        combined_pool.append(item)
        pool_keys.add(_uve_candidate_key(item))
    for web_item in scored_web_sources:
        if not _web_source_is_video_candidate(web_item):
            continue
        web_key = _uve_candidate_key(web_item)
        if not web_key or web_key in pool_keys:
            continue
        pool_keys.add(web_key)
        web_item = dict(web_item)
        web_item["_pool_kind"] = "video"
        web_item["_selection_score"] = (web_item.get("universal_score") or 0) + _intent_bonus("video")
        combined_pool.append(web_item)
    for item in scored_images:
        item = dict(item)
        item["_pool_kind"] = "image"
        item["_selection_score"] = (item.get("universal_score") or 0) + _intent_bonus("image")
        combined_pool.append(item)

    combined_pool.sort(key=lambda c: c.get("_selection_score") or 0, reverse=True)

    picks = _uve_pick(combined_pool, scenario, registry, used_keys, shot_need=shot_need)
    fetch_failures = []
    asset = _uve_fetch_first(
        picks, _uve_download_any_candidate, scene_dir,
        beat["sentence_numbers"][0], len(beat_videos) + len(beat_images) + 1,
        registry, used_keys, failures=fetch_failures,
        transcript_scenario=scenario, transcript_bible=bible,
    )
    if fetch_failures:
        scene_stats.setdefault("download_failures", []).extend(fetch_failures)
        for failure in fetch_failures:
            entry = scene_stats.setdefault("providers", {}).setdefault(
                str(failure.get("provider") or "unknown"),
                {"discovered": 0, "verified": 0, "rejected": 0, "downloaded": 0, "download_failed": 0},
            )
            entry["download_failed"] += 1
    accepted_asset = None
    if asset:
        _dl_provider = str(asset.get("provider") or asset.get("source") or "unknown")
        scene_stats.setdefault("providers", {}).setdefault(
            _dl_provider,
            {"discovered": 0, "verified": 0, "rejected": 0, "downloaded": 0, "download_failed": 0},
        )["downloaded"] += 1
        annotated = _uve_annotate(asset, scenario, scenario_index, beat.get("text", ""))
        accepted_asset = annotated
        if asset.get("type") == "video" or asset.get("visual_type") == "video":
            beat_videos.append(annotated)
            print(f"      + video ({asset.get('verification_status')}, score {asset.get('universal_score')})")
        else:
            beat_images.append(annotated)
            print(f"      + image ({asset.get('verification_status')}, score {asset.get('universal_score')})")
    elif picks:
        scene_stats["downloads_failed"] += 1
    # VA-08: one traceable record per candidate outcome in this pass,
    # so the asset report's aggregates are built from the same records
    # an operator can inspect individually.
    _records = scene_stats.setdefault("candidate_records", [])
    for _item in (scored_videos + scored_web_sources + scored_images):
        _base = {
            "candidate_id": _item.get("candidate_id"),
            "url": (
                _item.get("url") or _item.get("video_url")
                or _item.get("image_url") or ""
            ),
            "provider": str(
                _item.get("provider") or _item.get("source") or "unknown"
            ),
        }
        if _item.get("reject_reason"):
            _records.append({
                **_base,
                "outcome": "rejected",
                "stage": _item.get("rejection_stage") or "scoring",
                "reason": _item.get("reject_reason"),
            })
        elif (_item.get("verification") or {}).get("accepted"):
            _records.append({
                **_base, "outcome": "verified",
                "stage": "verification", "reason": "",
            })
    for _item in combined_pool:
        # Selection-stage rejections are stamped by _uve_pick on the
        # pool copies; verification-stage ones are already recorded
        # from the scored lists above.
        if _item.get("rejection_stage") == "selection":
            _records.append({
                "candidate_id": _item.get("candidate_id"),
                "url": (
                    _item.get("url") or _item.get("video_url")
                    or _item.get("image_url") or ""
                ),
                "provider": str(
                    _item.get("provider") or _item.get("source") or "unknown"
                ),
                "outcome": "rejected",
                "stage": "selection",
                "reason": _item.get("reject_reason") or "",
            })
    for _failure in fetch_failures:
        _records.append({
            "candidate_id": _failure.get("candidate_id"),
            "url": _failure.get("url", ""),
            "provider": str(_failure.get("provider") or "unknown"),
            "outcome": "download_failed",
            "stage": _failure.get("stage") or "download",
            "reason": _failure.get("reason", ""),
        })
    if accepted_asset is not None:
        _records.append({
            "candidate_id": accepted_asset.get("candidate_id"),
            "url": (
                accepted_asset.get("url") or accepted_asset.get("video_url")
                or ""
            ),
            "provider": str(
                accepted_asset.get("provider")
                or accepted_asset.get("source") or "unknown"
            ),
            "outcome": "accepted", "stage": "accepted", "reason": "",
        })

    review_records.append({
        "shot_index": scenario_index,
        "queries": list(queries or []),
        # F-4: the providers ACTUALLY present in this
        # scenario's pools (was hardcoded ["youtube", "web"]).
        "providers": sorted({
            str(c.get("provider") or c.get("source") or "unknown")
            for c in (raw_videos + raw_web_sources + raw_images)
        }) if shot_need else [],
        # VA-08: the rejected list is NOT capped — diagnostics must
        # cover every rejected candidate, each with its stable id and
        # the stage that rejected it.
        "rejected": [
            {
                "candidate_id": item.get("candidate_id"),
                "title": result_title(item),
                "reason": item.get("reject_reason"),
                "stage": item.get("rejection_stage") or "scoring",
            }
            for item in (scored_videos + scored_web_sources + scored_images)
            if item.get("reject_reason")
        ],
        "picked": [result_title(item) for item in beat_videos + beat_images][-2:],
    })

    _discovery_failures = drain_discovery_failures()
    if _discovery_failures:
        scene_stats.setdefault("discovery_failures", []).extend(_discovery_failures)
    return accepted_asset



MAX_COVERAGE_RECOVERY_ROUNDS = 2

_RECOVERY_STOPWORDS = {
    "about", "after", "before", "their", "there", "these", "those",
    "under", "until", "where", "which", "while", "would", "could",
    "should", "began", "every", "first", "other", "being", "between",
}


def _query_signature(query):
    """Word-set signature used to recognise already-tried queries."""
    return " ".join(
        sorted({w.lower() for w in str(query or "").split() if len(w) >= 3})
    )


# Quality Recovery Phase 2 (workstream D; DM-06): FACET signatures.
# A query's facet set is the set of meaningful search facets it uses —
# person / organisation / place / date / event / evidence-medium —
# derived from the beat's own shot contracts. Reformulation must
# change the facet set, not just reword: a pure rewording (same facet
# set, near-identical words) is recognised as already tried and never
# re-executed, and every executed recovery query is recorded with its
# facet signature alongside its string.
_FACET_MEDIUM_TERMS = {
    "photograph", "photo", "archive", "archival", "footage",
    "documentary", "newsreel", "image", "video",
}


def _facet_words(value):
    return {
        w.strip(".,;:!?\"'()").lower()
        for w in str(value or "").split()
        if len(w.strip(".,;:!?\"'()")) >= 3
    }


def _query_facets(query, beat=None):
    """The facet set a query uses, judged against the beat's contracts."""
    words = _facet_words(query)
    facets = set()
    if not words:
        return frozenset()
    if words & _FACET_MEDIUM_TERMS:
        facets.add("evidence")
    if any(re.fullmatch(r"(1[5-9]\d\d|20\d\d)s?", w) for w in words):
        facets.add("date")
    beat = beat or {}
    needs = beat.get("shot_needs") or []
    subject_hit = False
    for need in needs:
        subject_words = _facet_words(need.get("required_subject"))
        if subject_words and subject_words & words:
            subject_hit = True
            kind = str(need.get("required_type") or "").lower()
            facets.add(
                "organisation" if kind == "organization"
                else "person" if kind == "person"
                else "subject"
            )
        if _facet_words(need.get("place")) & words:
            facets.add("place")
        if _facet_words(need.get("event")) & words:
            facets.add("event")
        era_words = _facet_words(need.get("era"))
        if era_words & words:
            facets.add("date")
    if not subject_hit and _facet_words(
        str(beat.get("subject_id") or "").replace("_", " ")
    ) & words:
        facets.add("person")
    if _facet_words(beat.get("place")) & words:
        facets.add("place")
    return frozenset(facets)


def _facet_signature(query, beat=None):
    """Stable facet signature string for one query (sorted facets)."""
    return "|".join(sorted(_query_facets(query, beat)))


def _word_set(query):
    return {w for w in _query_signature(query).split() if w}


def _jaccard_words(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _recovery_queries(beat, tried_queries, broad=False, limit=4):
    """Alternative targeted queries for a beat that found no asset.

    Built deterministically from the beat's own shot contracts — the
    required subject anchored with place / era / event — never from
    generic filler. Round 1 keeps the era anchor; the broader round 2
    drops it and tries archive/photograph phrasings. Queries whose
    word-set signature was already tried are excluded, so recovery can
    never silently repeat the exact search that just failed.
    """
    needs = beat.get("shot_needs") or []
    beat_subject = str(beat.get("subject_id") or "").replace("_", " ").strip()
    beat_place = str(beat.get("place") or "").strip()
    candidates = []
    for need in needs:
        subject = str(need.get("required_subject") or "").strip()
        place = str(need.get("place") or beat_place).strip()
        era = str(need.get("era") or "").strip()
        event = str(need.get("event") or "").strip()
        if broad:
            templates = [
                (subject, place),
                (subject, "photograph"),
                (subject, place, "archive"),
            ]
        else:
            templates = [
                # Phase 2: the full-facet combination first — its
                # facet set {person, event, place, date} differs from
                # every initial-query facet set, so at least one
                # recovery query reformulates by FACETS, not wording
                # (required test 14(b)).
                (subject, event, place, era),
                (subject, place, era),
                (subject, event),
                (subject, place),
                (subject, era, "documentary"),
            ]
        for parts in templates:
            query = " ".join(p for p in parts if p).strip()
            if query:
                candidates.append(query)
    if beat_subject:
        if broad:
            candidates.append(f"{beat_subject} {beat_place} archive".strip())
        else:
            candidates.append(f"{beat_subject} {beat_place}".strip())
            candidates.append(f"{beat_subject} archive footage")
    if broad:
        text = str(beat.get("text") or "")
        words = [w.strip(".,;:!?\"'()") for w in text.split()]
        keywords = [
            w for w in words
            if len(w) >= 5 and w.lower() not in _RECOVERY_STOPWORDS
        ][:6]
        if keywords:
            candidates.append(" ".join(keywords))

    # Phase 2: a candidate is already-tried when its WORD signature
    # was tried, or when it is a pure rewording of a tried query —
    # same facet set with near-identical words (Jaccard >= 0.75, the
    # pipeline's existing near-duplicate threshold). A query whose
    # facet set is new is a genuine reformulation and executes.
    tried = [
        (_query_signature(q), _query_facets(q, beat), _word_set(q))
        for q in tried_queries
    ]
    tried_signatures = {sig for sig, _, _ in tried}
    out = []
    for query in candidates:
        signature = _query_signature(query)
        if not signature or signature in tried_signatures:
            continue
        facets = _query_facets(query, beat)
        words = _word_set(query)
        if any(
            facets == tried_facets
            and _jaccard_words(words, tried_words) >= 0.75
            for _, tried_facets, tried_words in tried
        ):
            continue
        tried_signatures.add(signature)
        tried.append((signature, facets, words))
        out.append(query)
        if len(out) >= limit:
            break
    return out


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
    # AUD-16/E: per-scene Asset Manager statistics. Every fallback and
    # failure is counted here and surfaces in the project asset report —
    # substitutions must never be silent.
    scene_stats = {
        "candidates_considered": 0,
        "candidates_rejected": 0,
        "downloads_failed": 0,
        "text_cards": 0,
    }
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
                print("      routed=youtube,web,images")
                _uve_search_and_pick(
                    scenario, shot_need,
                    search_cache=search_cache, project_id=project_id,
                    bible=bible, registry=registry, used_keys=used_keys,
                    scene_dir=scene_dir, scene_stats=scene_stats, beat=beat,
                    beat_videos=beat_videos, beat_images=beat_images,
                    scenario_index=scenario_index, queries=queries,
                    review_records=review_records,
                )

        recovery_rounds_done = 0
        if beat.get("searchable", True) and not beat_videos and not beat_images and scenarios:
            # COVERAGE RECOVERY (founder directive): before a beat may
            # be declared uncovered, re-search it with alternative
            # targeted queries built from its own shot contracts.
            # Bounded rounds, never the same query twice, verification
            # and acceptance rules unchanged — recovery improves
            # DISCOVERY only. A beat is recorded missing only after
            # recovery is exhausted, and the attempts are reported.
            tried_queries = []
            for _sc in scenarios:
                tried_queries.extend(_sc.get("queries") or [])
            for _need in beat.get("shot_needs") or []:
                tried_queries.extend(_need.get("queries") or [])
            base_scenario = dict(scenarios[0])
            base_need = (beat.get("shot_needs") or [{}])[0]
            for round_no in range(1, MAX_COVERAGE_RECOVERY_ROUNDS + 1):
                recovery_queries = _recovery_queries(
                    beat, tried_queries, broad=(round_no > 1),
                )
                if not recovery_queries:
                    break
                recovery_rounds_done = round_no
                # Stage 0 metrics: a recovery round is actually
                # being executed for this beat.
                run_metrics.coverage_recovery_round()
                tried_queries.extend(recovery_queries)
                print(
                    f"  COVERAGE RECOVERY round {round_no} for "
                    f"{beat['beat_id']}: {recovery_queries}"
                )
                recovery_scenario = dict(base_scenario)
                recovery_scenario["queries"] = recovery_queries
                recovered_asset = _uve_search_and_pick(
                    recovery_scenario, base_need,
                    search_cache=search_cache, project_id=project_id,
                    bible=bible, registry=registry, used_keys=used_keys,
                    scene_dir=scene_dir, scene_stats=scene_stats, beat=beat,
                    beat_videos=beat_videos, beat_images=beat_images,
                    scenario_index=int(base_need.get("shot_index") or 1),
                    queries=recovery_queries,
                    review_records=review_records,
                )
                scene_stats.setdefault("recovery_attempts", []).append(
                    {
                        "beat_id": beat.get("beat_id"),
                        "round": round_no,
                        "queries": recovery_queries,
                        # Phase 2: every executed query is recorded
                        # with its facet signature as well as its
                        # string, so reformulation is auditable and a
                        # tried signature is never re-executed.
                        "facet_signatures": [
                            _facet_signature(q, beat)
                            for q in recovery_queries
                        ],
                        "outcome": "covered" if recovered_asset else "no_asset",
                    }
                )
                if recovered_asset:
                    print(
                        f"  COVERAGE RECOVERED for {beat['beat_id']} "
                        f"in round {round_no}"
                    )
                    break

        if beat.get("searchable", True) and not beat_videos and not beat_images:
            # NO placeholder is created here. The generated text-card
            # fallback was removed permanently (founder directive):
            # a sentence with no verified visual is recorded
            # explicitly in scene_stats["missing_visuals"] and flows
            # into the asset report; the timeline covers the slot
            # with a genuine scene visual (rotation) or a renderer
            # holdover of real media — never a fabricated card.
            need = (beat.get("shot_needs") or [{}])[0]
            numbers = beat.get("sentence_numbers") or []
            for offset in range(len(beat.get("sentences") or [])):
                scene_stats.setdefault("missing_visuals", []).append(
                    {
                        "sentence_number": (
                            numbers[offset] if offset < len(numbers) else None
                        ),
                        "reason": "no_verified_asset",
                        "specificity": str(need.get("specificity") or ""),
                        "recovery_rounds": recovery_rounds_done,
                    }
                )
                run_context.note_missing_visual()
            print(
                "  MISSING VISUAL: no verified asset for this beat "
                "— recorded in the asset report, no placeholder created"
            )

        write_review_log(project_dir, scene_number, beat, review_records)

        # DM-17: deal each asset to the sentence that owns its
        # scenario (stamped scenario_index), never by raw position.
        _distribution = _distribute_beat_assets(
            beat, beat_videos, beat_images
        )

        for offset, sentence in enumerate(beat.get("sentences") or []):
            sentence_number = beat["sentence_numbers"][offset]
            people = _people_for_sentence(sentence, bible)

            # Assign each downloaded clip to the sentence/scenario that
            # produced it instead of copying the entire beat asset list
            # onto every sentence.
            sentence_videos, sentence_images = _distribution[offset]

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
        "asset_stats": scene_stats,
        "case_bible": {
            "title": bible.get("title"),
            "story_type": bible.get("story_type"),
            "story_era": bible.get("story_era") or bible.get("time_period"),
            "tone": bible.get("tone"),
            "entity_ids": [e.get("id") for e in (bible.get("entities") or [])],
        },
    }
    return apply_review_decisions(result, project_dir, bible=bible)
def write_decision_template(project_dir, scene_number, beats):
    path = Path(project_dir) / f"scene_{scene_number}_decisions.json"
    if path.exists():
        return path
    rows = []
    for beat in beats or []:
        # MA-34: one row per SENTENCE — decisions are applied at
        # sentence granularity, so the template must let a reviewer
        # target one sentence without touching its beat siblings. A
        # beat with no sentence list keeps its single beat-level row.
        sentence_numbers = beat.get("sentence_numbers") or []
        if sentence_numbers:
            for number in sentence_numbers:
                rows.append({
                    "sentence_number": number,
                    "beat_id": beat.get("beat_id"),
                    "approve": True,
                    "swap_path": "",
                })
        else:
            rows.append({
                "beat_id": beat.get("beat_id"),
                "approve": True,
                "swap_path": "",
            })
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"DECISIONS TEMPLATE: {path}")
    return path


def _refresh_swapped_asset(asset, bucket, swap_path, decision, sentence_result, bible):
    """MA-33: build the replacement record for a manual review swap.

    The replacement inherits NOTHING evidentiary from the original:

    - technical facts come from a FRESH probe of the new file
      (services.media_probe) — inherited duration/resolution/codec
      fields are cleared first, so nothing stale survives;
    - segment evidence is cleared (segment_evidence="none", no
      clip_start, no download provenance) — the replacement was
      never segment-verified;
    - rights are re-stamped honestly: licence "unknown", creator /
      source from the decision record when it supplies them, else
      "user_supplied", rights_status "verify_before_publish";
    - the verification verdict is REPLACED by a fresh
      story.verify_candidate run against the sentence's scenario
      (reached via the analyzed sentence embedded in the result).
      When no scenario is reachable, the verdict is an explicit
      rejection ("manual_swap_unverified") so every downstream
      filter treats the swap as unverified instead of inheriting
      the original's acceptance.

    Returns (refreshed_asset, verification_accepted), or
    (None, False) when the probe fails — the caller then leaves the
    original asset untouched and records the failed swap.
    """
    kind = bucket
    if bucket == "visuals":
        kind = (
            "videos"
            if (asset.get("visual_type") or asset.get("type")) == "video"
            else "images"
        )
    probe = probe_video(swap_path) if kind == "videos" else probe_image(swap_path)
    if not probe:
        return None, False

    refreshed = dict(asset)
    refreshed["file_path"] = swap_path
    refreshed["path"] = swap_path
    refreshed["swapped_from"] = {
        "file_path": asset.get("file_path") or asset.get("path") or "",
        "candidate_id": asset.get("candidate_id") or "",
    }
    refreshed["manual_swap"] = True

    for key in (
        "duration_seconds", "width", "height", "fps", "codec",
        "format", "has_audio",
    ):
        refreshed.pop(key, None)
    refreshed.update(probe)

    refreshed["segment_evidence"] = "none"
    for key in (
        "clip_start", "segment_evidence_basis",
        "source_duration_seconds", "download_method",
        "partial_download",
    ):
        refreshed.pop(key, None)

    refreshed["license"] = "unknown"
    refreshed["license_url"] = ""
    refreshed["creator"] = (
        decision.get("creator") or decision.get("source") or "user_supplied"
    )
    refreshed["source"] = decision.get("source") or "user_supplied"
    refreshed["source_url"] = decision.get("source_url") or ""
    refreshed["rights_status"] = "verify_before_publish"
    refreshed["retrieved_at"] = datetime.now(timezone.utc).isoformat()

    scenario = None
    embedded = sentence_result.get("sentence")
    if isinstance(embedded, dict):
        scenarios = _uve_scenarios_for(embedded)
        if scenarios:
            scenario = scenarios[0]
    candidate_meta = {
        "title": decision.get("title")
        or Path(swap_path).stem.replace("_", " ").replace("-", " "),
        "snippet": decision.get("snippet") or decision.get("description") or "",
        "description": decision.get("description") or decision.get("snippet") or "",
        "url": decision.get("source_url") or "",
        "source": refreshed["source"],
    }
    if scenario is not None:
        verification = story.verify_candidate(
            candidate_meta, scenario, bible or {}
        )
    else:
        verification = {
            "accepted": False,
            "reason": "manual_swap_unverified",
            "rejection_reason": "manual_swap_unverified",
        }
    refreshed["verification"] = verification
    refreshed["verification_status"] = (
        "verified" if verification.get("accepted") else "swapped"
    )
    return refreshed, bool(verification.get("accepted"))


def apply_review_decisions(result, project_dir, bible=None):
    if not isinstance(result, dict):
        return result
    scene_number = result.get("scene_number", 1)
    path = Path(project_dir) / f"scene_{scene_number}_decisions.json"
    write_decision_template(project_dir, scene_number, result.get("beats") or [])
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return result

    # MA-34: decisions are keyed per SENTENCE first. The old code
    # indexed rows by beat_id alone, so one decision silently
    # overwrote EVERY sentence of a beat, and sentences whose
    # results carry no beat_id were never decided at all. A row
    # naming a sentence_number targets exactly that sentence; a
    # beat-only row remains the fallback for its sentences.
    by_sentence = {}
    by_beat = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("sentence_number") is not None:
            by_sentence[row.get("sentence_number")] = row
        elif row.get("beat_id") is not None:
            by_beat[row.get("beat_id")] = row

    outcomes = []
    aggregate_swaps = {}

    for sentence in result.get("sentences") or []:
        sentence_number = sentence.get("sentence_number")
        decision = None
        if sentence_number is not None and sentence_number in by_sentence:
            decision = by_sentence[sentence_number]
        elif sentence.get("beat_id") is not None:
            decision = by_beat.get(sentence.get("beat_id"))
        if not decision:
            continue
        if decision.get("approve", True) and not decision.get("swap_path"):
            continue
        swap = str(decision.get("swap_path") or "").strip()
        if swap and os.path.exists(swap):
            # MA-33: evidence-safe swap — see _refresh_swapped_asset.
            # A failed probe leaves the original asset untouched and
            # is recorded as a failed swap, never applied silently.
            for bucket in ("images", "videos", "visuals"):
                refreshed_bucket = []
                for asset in sentence.get(bucket) or []:
                    refreshed, accepted = _refresh_swapped_asset(
                        asset, bucket, swap, decision, sentence, bible
                    )
                    previous_path = (
                        asset.get("file_path") or asset.get("path") or ""
                    )
                    if refreshed is None:
                        outcomes.append({
                            "sentence_number": sentence_number,
                            "beat_id": sentence.get("beat_id"),
                            "swap_path": swap,
                            "previous_file_path": previous_path,
                            "previous_candidate_id": asset.get("candidate_id") or "",
                            "outcome": "swap_failed_probe",
                            "verification_accepted": False,
                        })
                        refreshed_bucket.append(asset)
                        continue
                    outcomes.append({
                        "sentence_number": sentence_number,
                        "beat_id": sentence.get("beat_id"),
                        "swap_path": swap,
                        "previous_file_path": previous_path,
                        "previous_candidate_id": asset.get("candidate_id") or "",
                        "outcome": "swapped",
                        "verification_accepted": accepted,
                    })
                    aggregate_swaps[previous_path] = refreshed
                    refreshed_bucket.append(refreshed)
                sentence[bucket] = refreshed_bucket
            continue
        sentence["images"] = []
        sentence["videos"] = []
        sentence["visuals"] = []
        sentence["rejected_by_review"] = True

    # MA-34: scene aggregates must reference the swapped files too —
    # otherwise the timeline's rotation pool resurrects the pre-swap
    # original straight from result["videos"]/["images"]/["visuals"].
    if aggregate_swaps:
        for bucket in ("videos", "images", "visuals"):
            entries = result.get(bucket)
            if not entries:
                continue
            synced = []
            for entry in entries:
                old_path = entry.get("file_path") or entry.get("path") or ""
                refreshed = aggregate_swaps.get(old_path)
                if refreshed is None:
                    synced.append(entry)
                    continue
                merged = dict(refreshed)
                if entry.get("visual_type"):
                    merged["visual_type"] = entry["visual_type"]
                synced.append(merged)
            result[bucket] = synced

    if outcomes:
        result["review_decision_outcomes"] = outcomes
    return result
def process_scene_media(scene, project_dir, image_count=3, video_count=3):
    """Canonical scene entry point (story-driven visual engine).

    Note (AUD-24): image_count/video_count are legacy parameters kept for
    call-site compatibility. They do NOT cap retrieval — the number of
    visuals is driven by the scene's story scenarios. They are documented
    here so nobody mistakes them for working controls.
    """
    return _uve_process_scene_media(scene, project_dir, image_count=image_count, video_count=video_count)












































