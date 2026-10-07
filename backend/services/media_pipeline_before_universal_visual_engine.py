import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import requests
from PIL import Image, UnidentifiedImageError

from services.media import download_image, search_images
from services.visual_registry import get_registry



# ================================================================
# UNIVERSAL EVIDENCE LAYER V1
# ================================================================

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


def classify_evidence_type(*, asset=None, shot_type="", query="", sentence=""):
    asset = asset or {}

    role = str(asset.get("asset_role", "")).lower().strip()
    scope = str(asset.get("asset_scope", "")).lower().strip()
    shot = str(shot_type or "").lower().strip()

    combined = " ".join([
        str(query or ""),
        str(sentence or ""),
        role,
        scope,
        shot,
    ]).lower()

    # Identity is the strongest and most explicit classification.
    if (
        asset.get("identity_locked", False)
        or role == "real_person_photo"
        or scope == "real_person_photo"
        or shot == "real_person_photo"
    ):
        return EVIDENCE_IDENTITY

    if (
        scope in {"location", "location_photo", "location_broll"}
        or shot in {"location", "location_photo", "location_broll"}
    ):
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

    # Contextual keyword fallback.
    if any(x in combined for x in (
        "headquarters", "hq", "factory", "building",
        "city", "town", "street", "lake", "river",
        "ocean", "sea", "port", "harbor", "harbour",
        "rochester", "tallahassee"
    )):
        return EVIDENCE_LOCATION

    if any(x in combined for x in (
        "company", "corporation", "organization", "organisation",
        "brand", "business", "enterprise", "kodak", "apple",
        "microsoft", "google", "amazon", "tesla", "ftx"
    )):
        return EVIDENCE_ORGANIZATION

    if any(x in combined for x in (
        "filing", "court", "lawsuit", "bankruptcy", "report",
        "record", "statement", "contract", "patent",
        "letter", "memo", "transcript"
    )):
        return EVIDENCE_DOCUMENT

    if any(x in combined for x in (
        "archive", "archival", "historical", "historic",
        "vintage", "newsreel", "old footage", "old photograph"
    )):
        return EVIDENCE_ARCHIVAL

    if any(x in combined for x in (
        "battle", "war", "collapse", "crash", "attack",
        "launch", "trial", "murder", "disappearance",
        "acquisition", "merger", "invention", "incident"
    )):
        return EVIDENCE_EVENT

    if any(x in combined for x in (
        "building", "manufacturing", "fighting", "sailing",
        "flying", "running", "walking", "speaking",
        "filming", "testing", "launching", "working"
    )):
        return EVIDENCE_ACTION

    if any(x in combined for x in (
        "camera", "ship", "aircraft", "vehicle", "machine",
        "weapon", "computer", "phone", "prototype",
        "invention", "device", "product"
    )):
        return EVIDENCE_OBJECT

    if any(x in combined for x in (
        "data", "chart", "graph", "statistics", "statistic",
        "percentage", "revenue", "price", "market",
        "population", "counter"
    )):
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

    if role in {
        "real_person_photo",
        "archival",
        "historical",
        "official_document",
    }:
        return "high"

    if evidence_type in {
        EVIDENCE_LOCATION,
        EVIDENCE_ORGANIZATION,
        EVIDENCE_OBJECT,
        EVIDENCE_EVENT,
        EVIDENCE_DOCUMENT,
        EVIDENCE_ARCHIVAL,
    } and source not in {"pexels", "pixabay"}:
        return "medium"

    if evidence_type in {EVIDENCE_ACTION, EVIDENCE_DATA}:
        return "medium"

    return "low"



def extract_evidence_entity(
    *,
    evidence_type="",
    sentence="",
    query="",
    asset=None,
):
    """
    Resolve the concrete entity represented by an evidence visual.

    This is universal across documentary niches.

    Examples:
        organization + Kodak sentence -> Kodak
        location + Rochester sentence -> Rochester
        object + digital camera sentence -> digital camera
        identity + Mike Williams sentence -> Mike Williams
        data + 3.6 kg sentence -> 3.6 kg
    """

    asset = asset or {}

    evidence_type = str(
        evidence_type or ""
    ).lower().strip()

    sentence_text = str(
        sentence or ""
    ).strip()

    query_text = str(
        query or ""
    ).strip()

    combined = " ".join(
        [
            sentence_text,
            query_text,
            str(asset.get("person_name", "")),
            str(asset.get("entity_name", "")),
            str(asset.get("query", "")),
            str(asset.get("search_query", "")),
        ]
    )

    # --------------------------------------------------------
    # Already locked identity
    # --------------------------------------------------------

    if asset.get("identity_locked", False):

        identity = (
            asset.get("identity_entity")
            or asset.get("person_name")
            or asset.get("entity_name")
            or ""
        )

        if str(identity).strip():
            return str(identity).strip()

    # --------------------------------------------------------
    # PERSON
    # --------------------------------------------------------

    if evidence_type == EVIDENCE_IDENTITY:

        known_people = (
            "steven sasson",
            "mike williams",
            "brian winchester",
            "denise williams",
        )

        lower_combined = combined.lower()

        for person in known_people:
            if person in lower_combined:
                return person.title()

        # Reuse the existing entity extractor when available.
        extractor = globals().get(
            "extract_entities"
        )

        if callable(extractor):

            try:
                entities = extractor(
                    sentence_text
                )

                if isinstance(entities, list):

                    for entity in entities:

                        if isinstance(entity, dict):

                            name = (
                                entity.get("name")
                                or entity.get("text")
                                or ""
                            )

                        else:
                            name = str(entity)

                        if str(name).strip():
                            return str(name).strip()

            except Exception:
                pass

        return (
            asset.get("person_name")
            or asset.get("identity_entity")
            or ""
        )

    # --------------------------------------------------------
    # ORGANIZATION / COMPANY
    # --------------------------------------------------------

    if evidence_type == EVIDENCE_ORGANIZATION:

        organizations = (
            "Kodak",
            "Apple",
            "Microsoft",
            "Google",
            "Amazon",
            "Tesla",
            "FTX",
            "Enron",
            "Theranos",
            "Boeing",
            "NASA",
            "SEC",
        )

        lower_combined = combined.lower()

        for organization in organizations:
            if organization.lower() in lower_combined:
                return organization

        existing = (
            asset.get("entity_name")
            or asset.get("organization")
            or asset.get("company")
            or ""
        )

        if str(existing).strip():
            return str(existing).strip()

    # --------------------------------------------------------
    # LOCATION
    # --------------------------------------------------------

    if evidence_type == EVIDENCE_LOCATION:

        locations = (
            "Rochester",
            "Tallahassee",
            "Lake Seminole",
            "New York",
            "California",
            "Florida",
            "Bahamas",
            "Pacific Ocean",
            "Atlantic Ocean",
            "Hudson River",
        )

        lower_combined = combined.lower()

        for location in locations:
            if location.lower() in lower_combined:
                return location

        existing = (
            asset.get("location")
            or asset.get("entity_name")
            or ""
        )

        if str(existing).strip():
            return str(existing).strip()

    # --------------------------------------------------------
    # OBJECT / PRODUCT / DEVICE
    # --------------------------------------------------------

    if evidence_type == EVIDENCE_OBJECT:

        objects = (
            "digital camera",
            "camera",
            "prototype",
            "computer",
            "phone",
            "aircraft",
            "ship",
            "vehicle",
            "device",
            "machine",
            "product",
            "technology",
        )

        lower_combined = combined.lower()

        # Longest / most specific first.
        for obj in sorted(
            objects,
            key=len,
            reverse=True,
        ):
            if obj in lower_combined:
                return obj

        existing = (
            asset.get("entity_name")
            or asset.get("object_name")
            or ""
        )

        if str(existing).strip():
            return str(existing).strip()

    # --------------------------------------------------------

    # DOCUMENT

    # --------------------------------------------------------


    if evidence_type == EVIDENCE_DOCUMENT:


        import re


        documents = (

            "SEC filing",

            "bankruptcy filing",

            "court filing",

            "court document",

            "patent",

            "lawsuit",

            "contract",

            "report",

            "transcript",

            "statement",

            "memo",

            "letter",

        )


        lower_combined = combined.lower()


        # Explicit document phrases.

        for document in sorted(

            documents,

            key=len,

            reverse=True,

        ):

            if document.lower() in lower_combined:

                return document


        # Semantic document patterns.

        document_patterns = (

            (

                r"\bfiled\s+(?:for\s+)?bankruptcy"

                r"(?:\s+protection)?\b",

                "bankruptcy filing",

            ),

            (

                r"\b(?:filed|submitted|made)\s+(?:an?\s+)?SEC\s+filing\b",

                "SEC filing",

            ),

            (

                r"\bfiled\s+(?:a\s+)?lawsuit\b",

                "lawsuit",

            ),

            (

                r"\bfiled\s+(?:a\s+)?patent\b",

                "patent",

            ),

            (

                r"\bsubmitted\s+(?:a\s+)?report\b",

                "report",

            ),

            (

                r"\bfiled\s+(?:a\s+)?court\s+document\b",

                "court document",

            ),

            (

                r"\bfiled\s+(?:a\s+)?court\s+filing\b",

                "court filing",

            ),

        )


        for pattern, document_name in document_patterns:


            if re.search(

                pattern,

                combined,

                flags=re.IGNORECASE,

            ):

                return document_name


        existing = (

            asset.get("document_name")

            or asset.get("entity_name")

            or ""

        )


        if str(existing).strip():

            return str(existing).strip()


    # --------------------------------------------------------
    # DATA
    # --------------------------------------------------------

    if evidence_type == EVIDENCE_DATA:

        import re

        data_patterns = (
            r"\$\s?\d+(?:\.\d+)?\s?(?:million|billion|trillion)?",
            r"\b\d+(?:\.\d+)?\s?(?:kg|lbs?|million|billion|trillion)\b",
            r"\b\d+(?:\.\d+)?%",
        )

        for pattern in data_patterns:

            match = re.search(
                pattern,
                combined,
                flags=re.IGNORECASE,
            )

            if match:
                return match.group(0)

        existing = (
            asset.get("data_entity")
            or asset.get("entity_name")
            or ""
        )

        if str(existing).strip():
            return str(existing).strip()

    # --------------------------------------------------------
    # EVENT
    # --------------------------------------------------------

    if evidence_type == EVIDENCE_EVENT:

        existing = (
            asset.get("event_name")
            or asset.get("entity_name")
            or ""
        )

        if str(existing).strip():
            return str(existing).strip()

        # Use recognizable event phrases from the narration.
        event_phrases = (
            "Battle of Midway",
            "bankruptcy",
            "acquisition",
            "merger",
            "launch",
            "invention",
            "murder",
            "disappearance",
            "trial",
            "crash",
            "attack",
            "collapse",
        )

        lower_combined = combined.lower()

        for event in sorted(
            event_phrases,
            key=len,
            reverse=True,
        ):
            if event.lower() in lower_combined:
                return event

    # --------------------------------------------------------
    # ARCHIVAL
    # --------------------------------------------------------

    if evidence_type == EVIDENCE_ARCHIVAL:

        import re

        year_match = re.search(
            r"\b(?:18|19|20)\d{2}\b",
            combined,
        )

        if year_match:
            return year_match.group(0)

        existing = (
            asset.get("entity_name")
            or asset.get("archive_entity")
            or ""
        )

        if str(existing).strip():
            return str(existing).strip()

    # --------------------------------------------------------
    # FALLBACK
    # --------------------------------------------------------

    existing = (
        asset.get("entity_name")
        or asset.get("evidence_entity")
        or ""
    )

    if str(existing).strip():
        return str(existing).strip()

    return ""


def enrich_visual_with_evidence(
    visual,
    *,
    shot_type="",
    query="",
    sentence="",
):
    if not isinstance(visual, dict):
        return visual

    evidence_type = classify_evidence_type(
        asset=visual,
        shot_type=shot_type,
        query=query,
        sentence=sentence,
    )

    strength = calculate_evidence_strength(
        asset=visual,
        evidence_type=evidence_type,
        sentence=sentence,
    )

    visual["evidence_type"] = evidence_type
    visual["evidence_strength"] = strength
    visual["evidence_entity"] = extract_evidence_entity(
        evidence_type=evidence_type,
        sentence=sentence,
        query=query,
        asset=visual,
    )

    reasons = {
        EVIDENCE_IDENTITY: "identity_locked_real_person",
        EVIDENCE_LOCATION: "location_context",
        EVIDENCE_ORGANIZATION: "organization_context",
        EVIDENCE_OBJECT: "object_or_product_context",
        EVIDENCE_EVENT: "event_context",
        EVIDENCE_ACTION: "action_context",
        EVIDENCE_ARCHIVAL: "historical_archival_context",
        EVIDENCE_DOCUMENT: "document_or_record_context",
        EVIDENCE_DATA: "data_or_statistical_context",
        EVIDENCE_GENERAL_BROLL: "general_b_roll",
    }

    visual["evidence_reason"] = reasons.get(
        evidence_type,
        "general_b_roll",
    )

    return visual



# SENTENCE EVIDENCE INTELLIGENCE V1

def infer_preferred_evidence_types(sentence=""):
    """
    Infer which evidence categories should be preferred for a sentence.

    This is sentence-driven rather than candidate-driven.
    The narration determines what kind of evidence is useful.
    """

    sentence_text = str(sentence or "").lower().strip()

    if not sentence_text:
        return [
            EVIDENCE_GENERAL_BROLL,
        ]

    preferred = []

    def add(*types):
        for evidence_type in types:
            if evidence_type not in preferred:
                preferred.append(evidence_type)

    # ------------------------------------------------
    # PERSON / IDENTITY
    # ------------------------------------------------

    if any(
        phrase in sentence_text
        for phrase in (
            "steven sasson",
            "mike williams",
            "brian winchester",
            "denise williams",
            "person",
            "founder",
            "inventor",
            "ceo",
            "executive",
            "president",
            "director",
        )
    ):
        add(EVIDENCE_IDENTITY)

    # ------------------------------------------------
    # ORGANIZATION / COMPANY
    # ------------------------------------------------

    if any(
        phrase in sentence_text
        for phrase in (
            "kodak",
            "apple",
            "microsoft",
            "google",
            "amazon",
            "tesla",
            "ftx",
            "company",
            "corporation",
            "organization",
            "organisation",
            "brand",
            "business",
            "enterprise",
            "firm",
            "manufacturer",
            "employer",
            "headquarters",
        )
    ):
        add(EVIDENCE_ORGANIZATION)

    # ------------------------------------------------
    # LOCATION
    # ------------------------------------------------

    if any(
        phrase in sentence_text
        for phrase in (
            "rochester",
            "tallahassee",
            "lake seminole",
            "new york",
            "california",
            "florida",
            "city",
            "town",
            "village",
            "country",
            "state",
            "located",
            "location",
            "headquarters",
            "factory",
            "plant",
            "campus",
            "street",
            "lake",
            "river",
            "ocean",
            "sea",
            "port",
            "harbor",
            "harbour",
        )
    ):
        add(EVIDENCE_LOCATION)

    # ------------------------------------------------
    # OBJECT / PRODUCT / DEVICE
    # ------------------------------------------------

    if any(
        phrase in sentence_text
        for phrase in (
            "camera",
            "digital camera",
            "prototype",
            "device",
            "machine",
            "computer",
            "phone",
            "ship",
            "aircraft",
            "vehicle",
            "product",
            "invention",
            "technology",
            "equipment",
        )
    ):
        add(EVIDENCE_OBJECT)

    # ------------------------------------------------
    # DOCUMENT / RECORD
    # ------------------------------------------------

    if any(
        phrase in sentence_text
        for phrase in (
            "document",
            "documents",
            "filing",
            "filed",
            "court",
            "lawsuit",
            "legal",
            "contract",
            "patent",
            "report",
            "record",
            "records",
            "statement",
            "letter",
            "memo",
            "transcript",
            "bankruptcy filing",
        )
    ):
        add(EVIDENCE_DOCUMENT)

    # ------------------------------------------------
    # EVENTS
    # ------------------------------------------------

    if any(
        phrase in sentence_text
        for phrase in (
            "invention",
            "invented",
            "launched",
            "launch",
            "acquired",
            "acquisition",
            "merger",
            "merged",
            "bankruptcy",
            "collapsed",
            "collapse",
            "crash",
            "attack",
            "battle",
            "war",
            "trial",
            "murder",
            "disappearance",
            "disappeared",
            "incident",
            "accident",
            "death",
            "died",
            "founded",
            "founded in",
        )
    ):
        add(EVIDENCE_EVENT)

    # ------------------------------------------------
    # ACTION
    # ------------------------------------------------

    if any(
        phrase in sentence_text
        for phrase in (
            "built",
            "created",
            "developed",
            "designed",
            "manufactured",
            "produced",
            "tested",
            "testing",
            "working",
            "worked",
            "searched",
            "searching",
            "investigated",
            "investigating",
            "filmed",
            "speaking",
            "walking",
            "running",
            "flying",
            "sailing",
            "fighting",
        )
    ):
        add(EVIDENCE_ACTION)

    # ------------------------------------------------
    # ARCHIVAL / HISTORICAL
    # ------------------------------------------------

    import re

    if (
        re.search(r"\b(18|19|20)\d{2}\b", sentence_text)
        or any(
            phrase in sentence_text
            for phrase in (
                "historical",
                "history",
                "historic",
                "archive",
                "archival",
                "vintage",
                "old footage",
                "old photograph",
                "years earlier",
                "decades earlier",
                "at the time",
                "originally",
                "in 1975",
                "in 1980",
                "in 1990",
                "in 2000",
                "in 2010",
                "in 2012",
            )
        )
    ):
        add(EVIDENCE_ARCHIVAL)

    # ------------------------------------------------
    # DATA / NUMBERS / FINANCIAL CLAIMS
    # ------------------------------------------------

    if (
        re.search(r"\b\d+(?:\.\d+)?%\b", sentence_text)
        or re.search(r"\$\s?\d", sentence_text)
        or re.search(r"\b\d+(?:\.\d+)?\s?(?:kg|lbs?|million|billion|trillion)\b", sentence_text)
        or any(
            phrase in sentence_text
            for phrase in (
                "revenue",
                "profit",
                "loss",
                "sales",
                "stock",
                "share price",
                "market",
                "percentage",
                "percent",
                "statistics",
                "data",
                "million",
                "billion",
                "trillion",
                "declined",
                "increased",
                "grew",
                "fell",
                "rose",
            )
        )
    ):
        add(EVIDENCE_DATA)

    # ------------------------------------------------
    # FALLBACK
    # ------------------------------------------------

    if not preferred:
        add(EVIDENCE_GENERAL_BROLL)

    return preferred


# EVIDENCE SCORING ENGINE V1

EVIDENCE_STRENGTH_SCORES = {
    "high": 100,
    "medium": 65,
    "low": 25,
}


def calculate_visual_evidence_score(
    visual,
    *,
    sentence="",
    preferred_evidence_types=None,
):
    """
    Calculate how strongly a visual supports the narrated sentence.

    This is intentionally additive metadata only.
    Existing asset-selection behavior is not changed by this function.
    """

    if not isinstance(visual, dict):
        return 0

    preferred = set(
        str(value).lower().strip()
        for value in (preferred_evidence_types or [])
        if str(value).strip()
    )

    evidence_type = str(
        visual.get("evidence_type", "")
    ).lower().strip()

    strength = str(
        visual.get("evidence_strength", "")
    ).lower().strip()

    score = EVIDENCE_STRENGTH_SCORES.get(
        strength,
        0,
    )

    # Locked identity evidence is always extremely strong.
    if visual.get("identity_locked", False):
        score += 100

    # Exact evidence-type match.
    if evidence_type in preferred:
        score += 60

    sentence_text = str(
        sentence or ""
    ).lower().strip()

    entity = str(
        visual.get("evidence_entity", "")
    ).lower().strip()

    # Entity appearing in the narration is a strong relevance signal.
    if entity and entity in sentence_text:
        score += 50

    # Location/entity metadata can provide useful contextual support.
    source = str(
        visual.get("source", "")
    ).lower().strip()

    if source == "youtube":
        score += 10

    # Generic filler should never outrank actual evidence simply because
    # it exists.
    if evidence_type == EVIDENCE_GENERAL_BROLL:
        score -= 20

    return max(0, int(score))


def rank_visuals_by_evidence(
    visuals,
    *,
    sentence="",
    preferred_evidence_types=None,
):
    """
    Return visuals sorted by evidence support.

    Existing identity_locked visuals remain at the top.
    """

    if not isinstance(visuals, list):
        return []

    ranked = []

    for index, visual in enumerate(visuals):
        if not isinstance(visual, dict):
            continue

        score = calculate_visual_evidence_score(
            visual,
            sentence=sentence,
            preferred_evidence_types=preferred_evidence_types,
        )

        visual["evidence_score"] = score

        ranked.append(
            (
                bool(visual.get("identity_locked", False)),
                score,
                index,
                visual,
            )
        )

    ranked.sort(
        key=lambda item: (
            item[0],
            item[1],
            -item[2],
        ),
        reverse=True,
    )

    return [
        item[3]
        for item in ranked
    ]


def enrich_visual_collection_with_evidence(
    visuals,
    *,
    shot_type="",
    query="",
    sentence="",
):
    if not isinstance(visuals, list):
        return visuals

    for visual in visuals:
        enrich_visual_with_evidence(
            visual,
            shot_type=shot_type,
            query=query,
            sentence=sentence,
        )

    return visuals



DEFAULT_IMAGE_COUNT = 3
DEFAULT_VIDEO_COUNT = 2
DEFAULT_CLIP_DURATION = 8
MAX_CONSECUTIVE_SEARCH_FAILURES = 8
YT_DLP_TIMEOUT = 120
FFMPEG_TIMEOUT = 90
TEMP_FILE_SUFFIXES = (".part", ".ytdl", ".temp")
TEMP_FILE_PATTERNS = ("*.part", "*.ytdl", "*.temp")

_search_failure_state = {"consecutive_failures": 0, "last_error": None}

_STOP = {
    "a", "an", "the", "of", "in", "on", "at", "to", "and", "or", "for",
    "with", "from", "missing", "news", "photo", "image",
}


class SearchCircuitBreakerTripped(RuntimeError):
    pass


def _record_search_success():
    _search_failure_state["consecutive_failures"] = 0


def _record_search_failure(error):
    _search_failure_state["consecutive_failures"] += 1
    _search_failure_state["last_error"] = error
    if _search_failure_state["consecutive_failures"] >= MAX_CONSECUTIVE_SEARCH_FAILURES:
        raise SearchCircuitBreakerTripped(
            "Search API has failed "
            f"{_search_failure_state['consecutive_failures']} times. Last error: {error!r}"
        )


NAMED_PEOPLE = (
    "mike williams",
    "brian winchester",
    "denise williams",
)

FACE_WORDS = re.compile(
    r"\b(portrait|headshot|face|selfie|woman|man|girl|boy|model|actor|person|people|couple)\b",
    re.I,
)

ANIMAL_WORDS = re.compile(
    r"\b(chicken|chickens|rooster|hen|cow|cows|pig|pigs|goat|sheep|horse|"
    r"barn|farm|farmer|livestock|duck|goose|puppy|kitten|cat|dog)\b",
    re.I,
)

PLACE_TERMS = {
    "lake seminole": "Lake Seminole",
    "tallahassee": "Tallahassee",
    "jacksonville": "Jacksonville",
}

OBJECT_TERMS = {
    "boat": "boat",
    "truck": "truck",
    "insurance": "insurance documents",
    "court": "court documents",
    "police": "police",
    "jury": "jury",
}

BEAT_HINTS = [
    (re.compile(r"\b(police|detective|sheriff|arrest|divers?|search(ed|ing)?)\b", re.I), "police"),
    (re.compile(r"\b(court|trial|jury|judge|sentence|convict|appeal|courthouse)\b", re.I), "court"),
    (re.compile(r"\b(news|reporter|journalist|headline|camera|press)\b", re.I), "press"),
    (re.compile(r"\b(lake|seminole|boat|fish|water|dock|drown)\b", re.I), "lake"),
    (re.compile(r"\b(insur|policy|payout|papers?|files?|records?)\b", re.I), "insurance"),
    (re.compile(r"\b(remain|grave|funeral|aftermath|years)\b", re.I), "aftermath"),
]

SHOTS_BY_BEAT = {
    "lake": [
        ("location", ["Lake Seminole Georgia", "Lake Seminole fog morning", "Seminole county lake shoreline"]),
        ("location", ["fishing boat Lake Seminole", "empty boat dock Georgia lake"]),
        ("location", ["Tallahassee Florida pickup truck", "rural Georgia boat ramp"]),
    ],
    "police": [
        ("news", ["Lake Seminole search divers", "Georgia police lake search"]),
        ("broll", ["police car night emergency lights", "crime scene tape woods"]),
        ("document", ["missing person case file folders", "evidence boxes archive"]),
    ],
    "court": [
        ("news", ["Brian Winchester trial", "Denise Williams trial Tallahassee"]),
        ("location", ["Leon County courthouse Tallahassee", "Tallahassee courthouse columns"]),
        ("document", ["court documents on table", "criminal case file folders"]),
    ],
    "press": [
        ("news", ["Mike Williams missing news 2000", "Lake Seminole disappearance headline"]),
        ("broll", ["news camera on tripod", "newspaper printing press"]),
    ],
    "insurance": [
        ("document", ["life insurance policy document", "insurance papers on desk"]),
        ("broll", ["stack of archive folders", "hands reviewing files"]),
    ],
    "aftermath": [
        ("location", ["Tallahassee Florida suburban street", "quiet Tallahassee neighborhood"]),
        ("broll", ["rain on window glass", "empty park bench overcast"]),
    ],
    "investigation": [
        ("document", ["Florida case files 2000", "detective desk case folders"]),
        ("news", ["Mike Williams Tallahassee 2000 missing", "Brian Winchester interview"]),
        ("location", ["Tallahassee Florida downtown", "Leon County Florida"]),
    ],
}

GENERIC_LIBRARY = {
    "lake": ["Lake Seminole Georgia shoreline"],
    "police": ["Georgia police car"],
    "court": ["Tallahassee courthouse"],
    "press": ["newspaper printing press"],
    "documents": ["case files folders on table"],
    "night": ["Florida road at night"],
}


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
    session.headers.update({"User-Agent": "DocumentaryStudio/0.3"})
    return session


def mentions_named_person(text):
    low = str(text or "").lower()
    return any(name in low for name in NAMED_PEOPLE)


def looks_like_face(title):
    return bool(FACE_WORDS.search(str(title or "")))


def looks_like_animal(title):
    return bool(ANIMAL_WORDS.search(str(title or "")))


def blocked_visual(title):
    title = str(title or "")
    if looks_like_animal(title):
        return True
    if mentions_named_person(title):
        return False
    if looks_like_face(title):
        return True
    return False


def query_relevance(query, title):
    q = str(query or "").lower()
    t = str(title or "").lower()
    if not t:
        return 0.0
    if "mike williams" in q and "williams" not in t:
        return 0.0
    if "winchester" in q and "winchester" not in t:
        return 0.0
    if "denise" in q and "denise" not in t:
        return 0.0
    tokens = [w for w in re.findall(r"[a-z0-9]+", q) if w not in _STOP and len(w) > 3]
    if not tokens:
        return 1.0
    hits = sum(1 for w in tokens if w in t)
    return hits / len(tokens)


def uniq(items):
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def extract_entities(text):
    low = (text or "").lower()
    people = [name.title() for name in NAMED_PEOPLE if name in low]
    places = [label for key, label in PLACE_TERMS.items() if key in low]
    objects = [label for key, label in OBJECT_TERMS.items() if key in low]
    dates = re.findall(
        r"\b(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{1,2},\s+\d{4}\b|\b(?:19|20)\d{2}\b",
        text or "",
    )
    return EntityBank(
        people=uniq(people),
        places=uniq(places),
        dates=uniq(dates),
        objects=uniq(objects),
    )


def detect_label(text):
    for pattern, label in BEAT_HINTS:
        if pattern.search(text or ""):
            return label
    return "investigation"


def segment_beats(sentences, min_len=3, max_len=8):
    if not sentences:
        return []
    labels = [detect_label(item) for item in sentences]
    groups = []
    start = 0
    current = labels[0]
    for index, label in enumerate(labels):
        long_enough = (index - start) >= min_len
        too_long = (index - start + 1) > max_len
        if (label != current and long_enough) or too_long:
            groups.append((current, start, index - 1))
            start = index
            current = label
    groups.append((current, start, len(sentences) - 1))

    beats = []
    for beat_no, (label, start, end) in enumerate(groups, start=1):
        indexes = list(range(start, end + 1))
        text = " ".join(sentences[i] for i in indexes)
        beat_id = f"beat_{beat_no:02d}_{label}"
        template = SHOTS_BY_BEAT.get(label, SHOTS_BY_BEAT["investigation"])
        shots = [
            ShotNeed(beat_id, shot_index, shot_type, f"{label} shot {shot_index}", queries)
            for shot_index, (shot_type, queries) in enumerate(template, start=1)
        ]
        beats.append(Beat(beat_id, label, start, end, text, indexes, shots))
    return beats


def entity_anchor_shots(entities):
    shots = []
    index = 1
    person_queries = {
        "Mike Williams": [
            "Mike Williams Tallahassee 2000 missing",
            "Mike Williams Lake Seminole",
            "Mike Williams Florida hunting",
        ],
        "Brian Winchester": [
            "Brian Winchester Tallahassee",
            "Brian Winchester trial",
        ],
        "Denise Williams": [
            "Denise Williams trial Tallahassee",
            "Denise Williams Florida court",
        ],
    }
    for person in entities.people:
        queries = person_queries.get(person)
        if queries:
            shots.append(ShotNeed("entity_anchors", index, "real_person_photo", person, queries))
            index += 1
    for place in entities.places:
        if "lake" in place.lower() or "seminole" in place.lower():
            queries = ["Lake Seminole Georgia", "Lake Seminole aerial", "Seminole lake shoreline fog"]
        elif "tallahassee" in place.lower():
            queries = ["Tallahassee Florida skyline", "Tallahassee downtown", "Leon County courthouse"]
        else:
            queries = [f"{place} Florida", f"{place} aerial"]
        shots.append(ShotNeed("entity_anchors", index, "location_photo", place, queries))
        index += 1
    mapping = {
        "insurance documents": ["life insurance policy document", "insurance papers on desk"],
        "court documents": ["Florida court documents", "criminal case file folders"],
        "boat": ["fishing boat Lake Seminole", "empty wooden boat dock Georgia"],
        "truck": ["pickup truck boat ramp Florida"],
        "police": ["Georgia police lake search"],
        "jury": ["Leon County courtroom Tallahassee"],
    }
    for obj in entities.objects:
        queries = mapping.get(obj)
        if not queries:
            continue
        shots.append(ShotNeed("entity_anchors", index, "anchor", obj, queries))
        index += 1
    return shots


def generic_filler_shots():
    return [
        ShotNeed("generic_library", index, "filler", label, queries)
        for index, (label, queries) in enumerate(GENERIC_LIBRARY.items(), start=1)
    ]


def build_visual_plan(sentences):
    full_text = " ".join(sentences)
    entities = extract_entities(full_text)
    beats = segment_beats(sentences)
    shots = entity_anchor_shots(entities)
    for beat in beats:
        shots.extend(beat.shots)
    shots.extend(generic_filler_shots())

    print("=" * 70)
    print("VISUAL PLAN — SEARCH PER SHOT, NOT PER SENTENCE")
    print("=" * 70)
    print(f"People: {entities.people or ['none — no face search']}")
    print(f"Places: {entities.places}")
    print(f"Dates: {entities.dates}")
    print(f"Objects: {entities.objects}")
    print(f"Beats: {len(beats)}")
    for beat in beats:
        print(f"  {beat.beat_id} sentences {beat.start + 1}-{beat.end + 1} shots={len(beat.shots)}")
    print(f"Total shot searches: {len(shots)}")
    return {"entities": entities, "beats": beats, "shots": shots}


def _licensed_image_hit(url, title, source):
    return {
        "image_url": url,
        "url": url,
        "link": url,
        "title": title,
        "source": source,
        "license": "royalty-free",
        "snippet": title,
    }


def _licensed_video_hit(url, title, source):
    return {
        "video_url": url,
        "url": url,
        "link": url,
        "title": title,
        "source": source,
        "license": "royalty-free",
        "snippet": title,
    }


def _rank_hits(query, hits, limit):
    ranked = []
    for hit in hits:
        title = hit.get("title", "")
        if blocked_visual(title):
            continue
        score = query_relevance(query, title)
        if score < 0.34:
            continue
        ranked.append((score, hit))
    ranked.sort(key=lambda row: -row[0])
    return [hit for score, hit in ranked[:limit]]


def search_licensed_images(query, limit=8):
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
                hits.append(_licensed_image_hit(url, title, item.get("source") or "web"))
    return _rank_hits(query, hits, limit)


def search_youtube_videos(query, limit=5):
    """
    Discover YouTube videos through the existing Serper search layer.

    yt-dlp remains responsible for downloading/extracting the actual
    YouTube video later. This avoids making yt-dlp perform the search,
    which can hang on YouTube.
    """

    query = str(query or "").strip()

    if not query:
        return []

    search_queries = [
        f"site:youtube.com/watch {query}",
        f"site:youtube.com/watch \"{query}\"",
    ]

    results = []
    seen = set()

    for search_query in search_queries:

        if len(results) >= limit:
            break

        try:
            print(
                f"YOUTUBE DISCOVERY QUERY: "
                f"{search_query}"
            )

            hits = search_images(
                search_query,
                limit=max(5, min(limit * 2, 10)),
            ) or []

        except Exception as error:
            print(
                f"YouTube Serper discovery failed: "
                f"{error}"
            )
            continue

        for hit in hits:

            if not isinstance(hit, dict):
                continue

            possible_urls = [
                hit.get("link"),
                hit.get("url"),
                hit.get("source_url"),
                hit.get("video_url"),
            ]

            youtube_url = ""

            for possible_url in possible_urls:

                candidate = str(
                    possible_url or ""
                ).strip()

                if (
                    "youtube.com/watch" in candidate
                    or "youtu.be/" in candidate
                ):
                    youtube_url = candidate
                    break

            if not youtube_url:
                continue

            if "&" in youtube_url:
                youtube_url = youtube_url.split("&")[0]

            if youtube_url in seen:
                continue

            title = str(
                hit.get("title")
                or hit.get("snippet")
                or query
            ).strip()

            if not title:
                title = query

            score = query_relevance(
                query,
                title,
            )

            if score < 0.20:
                continue

            seen.add(youtube_url)

            results.append({
                "video_url": youtube_url,
                "url": youtube_url,
                "link": youtube_url,
                "title": title,
                "source": "youtube",
                "license": "unknown",
                "rights_status": "verify_before_publish",
                "snippet": str(
                    hit.get("snippet")
                    or title
                ),
            })

            if len(results) >= limit:
                break

    return results[:limit]


def search_licensed_videos(query, limit=5):
    """
    Combined video candidate search.

    Provider order:
        1. Pexels
        2. Pixabay
        3. YouTube

    All providers contribute candidates to the same ranking pool.
    A failed provider does NOT stop the others.
    """

    query = str(query or "").strip()

    if not query:
        return []

    candidates = []

    # ---------------------------------------------------------
    # PEXELS
    # ---------------------------------------------------------

    try:
        pexels_hits = _pexels_videos(
            query,
            limit,
        ) or []

        candidates.extend(pexels_hits)

        if pexels_hits:
            print(
                f"VIDEO PROVIDER PEXELS: "
                f"{len(pexels_hits)} candidates"
            )

    except Exception as error:
        print(
            f"Pexels video search failed: {error}"
        )

    # ---------------------------------------------------------
    # PIXABAY
    # ---------------------------------------------------------

    try:
        pixabay_hits = _pixabay_videos(
            query,
            limit,
        ) or []

        candidates.extend(pixabay_hits)

        if pixabay_hits:
            print(
                f"VIDEO PROVIDER PIXABAY: "
                f"{len(pixabay_hits)} candidates"
            )

    except Exception as error:
        print(
            f"Pixabay video search failed: {error}"
        )

    # ---------------------------------------------------------
    # YOUTUBE
    # ---------------------------------------------------------

    try:
        youtube_hits = search_youtube_videos(
            query,
            limit,
        ) or []

        candidates.extend(youtube_hits)

        if youtube_hits:
            print(
                f"VIDEO PROVIDER YOUTUBE: "
                f"{len(youtube_hits)} candidates"
            )

    except Exception as error:
        print(
            f"YouTube video search failed: {error}"
        )

    # ---------------------------------------------------------
    # Deduplicate by URL.
    # ---------------------------------------------------------

    unique = []
    seen = set()

    for hit in candidates:

        url = str(
            hit.get("url")
            or hit.get("video_url")
            or ""
        ).strip()

        if not url:
            continue

        key = url.lower()

        if key in seen:
            continue

        seen.add(key)
        unique.append(hit)

    # ---------------------------------------------------------
    # Rank the combined provider pool.
    # ---------------------------------------------------------

    ranked = _rank_hits(
        query,
        unique,
        max(limit * 3, limit),
    )

    # ---------------------------------------------------------
    # Prefer provider diversity when available.
    #
    # This prevents the entire pool from becoming one provider
    # when several providers have relevant results.
    # ---------------------------------------------------------

    selected = []
    selected_urls = set()

    provider_targets = (
        "pexels",
        "pixabay",
        "youtube",
    )

    # First pass: give each provider a chance.
    for provider in provider_targets:

        for hit in ranked:

            if str(
                hit.get("source", "")
            ).lower() != provider:
                continue

            url = str(
                hit.get("url")
                or hit.get("video_url")
                or ""
            ).strip()

            if not url or url in selected_urls:
                continue

            selected.append(hit)
            selected_urls.add(url)
            break

    # Second pass: fill remaining slots by relevance.
    for hit in ranked:

        if len(selected) >= limit:
            break

        url = str(
            hit.get("url")
            or hit.get("video_url")
            or ""
        ).strip()

        if not url or url in selected_urls:
            continue

        selected.append(hit)
        selected_urls.add(url)

    return selected[:limit]


def _pexels_photos(query, limit):
    key = os.getenv("PEXELS_API_KEY", "").strip()
    if not key:
        return []
    try:
        response = _http().get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": key},
            params={"query": query, "per_page": limit, "orientation": "landscape", "size": "large"},
            timeout=30,
        )
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


def _pexels_videos(query, limit):
    key = os.getenv("PEXELS_API_KEY", "").strip()
    if not key:
        return []
    try:
        response = _http().get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": key},
            params={"query": query, "per_page": limit, "orientation": "landscape", "size": "medium"},
            timeout=30,
        )
        if response.status_code != 200:
            print(f"Pexels video error {response.status_code}")
            return []
        out = []
        for video in response.json().get("videos", []):
            files = [
                item
                for item in (video.get("video_files") or [])
                if "mp4" in str(item.get("file_type") or "")
            ]
            files.sort(key=lambda item: abs((item.get("width") or 0) - 1920))
            if files:
                out.append(_licensed_video_hit(files[0]["link"], query, "pexels"))
        return out
    except Exception as error:
        print(f"Pexels video failed: {error}")
        return []


def _pixabay_photos(query, limit):
    key = os.getenv("PIXABAY_API_KEY", "").strip()
    if not key:
        return []
    try:
        response = _http().get(
            "https://pixabay.com/api/",
            params={
                "key": key,
                "q": query,
                "image_type": "photo",
                "orientation": "horizontal",
                "safesearch": "true",
                "per_page": max(3, min(limit, 20)),
            },
            timeout=30,
        )
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
        response = _http().get(
            "https://pixabay.com/api/videos/",
            params={"key": key, "q": query, "safesearch": "true", "per_page": max(3, min(limit, 20))},
            timeout=30,
        )
        if response.status_code != 200:
            print(f"Pixabay video error {response.status_code}")
            return []
        out = []
        for video in response.json().get("hits", []):
            files = video.get("videos") or {}
            pick = files.get("medium") or files.get("large") or files.get("small") or {}
            url = pick.get("url")
            if url:
                out.append(_licensed_video_hit(url, video.get("tags") or query, "pixabay"))
        return out
    except Exception as error:
        print(f"Pixabay video failed: {error}")
        return []


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
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
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
        command = [
            "ffmpeg", "-y", "-i", str(raw_path), "-t", str(duration),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-an", "-movflags", "+faststart", str(output_path),
        ]
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
    """
    Download only the requested section of a video.

    Pexels/Pixabay/direct media:
        download the direct file, then normalize with FFmpeg.

    YouTube:
        use yt-dlp with a video-only MP4 stream.
        We intentionally do NOT download audio because Documentary
        Studio replaces source audio with narration/music later.
    """

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------
    # DIRECT / LICENSED MEDIA
    # ---------------------------------------------------------

    if (
        is_direct_media_url(video_url)
        or "pexels.com" in str(video_url).lower()
        or "pixabay.com" in str(video_url).lower()
    ):
        return download_direct_video(
            video_url,
            output_path,
            duration,
        )

    # ---------------------------------------------------------
    # CLEAN OLD OUTPUT
    # ---------------------------------------------------------

    try:
        if output_path.exists():
            output_path.unlink()
    except OSError:
        pass

    cleanup_temp_files(output_path.parent)

    output_template = str(
        output_path.with_name(
            output_path.stem + "_source.%(ext)s"
        )
    ).replace("\\", "/")

    # ---------------------------------------------------------
    # YOUTUBE
    #
    # IMPORTANT:
    # Do NOT request:
    #
    # bv+ba
    #
    # That forces yt-dlp to fetch separate video + audio
    # streams and merge them.
    #
    # Documentary Studio does not need YouTube audio.
    #
    # Use the MP4 video-only stream instead.
    # ---------------------------------------------------------

    command = [
        sys.executable,
        "-m",
        "yt_dlp",

        "--no-playlist",
        "--no-warnings",
        "--restrict-filenames",

        "--retries",
        "1",

        "--fragment-retries",
        "1",

        "--socket-timeout",
        "10",

        # Prefer MP4 video-only up to 720p.
        "-f",
        "bestvideo[ext=mp4][height<=720]/bestvideo[height<=720]",

        # Download ONLY the requested section.
        "--download-sections",
        f"*{start_time}-{start_time + duration}",

        # No audio download.
        "--no-part",

        "-o",
        output_template,

        video_url,
    ]

    print()
    print("=" * 70)
    print("YOUTUBE CLIP EXTRACTION")
    print("=" * 70)
    print(f"URL: {video_url}")
    print(f"Start: {start_time}s")
    print(f"Duration: {duration}s")
    print("Format: bestvideo MP4 <=720p")
    print("Audio: disabled")
    print("=" * 70)

    returncode, stdout, stderr = run_command_safe(
        command,
        90,
        "yt-dlp",
    )

    if returncode != 0:
        print("yt-dlp clip extraction failed:")
        print((stderr or "")[-3000:])
        cleanup_temp_files(output_path.parent)
        return False

    # ---------------------------------------------------------
    # FIND DOWNLOADED SOURCE CLIP
    # ---------------------------------------------------------

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
            "yt-dlp completed but no source clip was found."
        )
        cleanup_temp_files(output_path.parent)
        return False

    candidates.sort(
        key=lambda item: -item.stat().st_size
    )

    source_clip = candidates[0]

    # ---------------------------------------------------------
    # NORMALIZE TO DOCUMENTARY OUTPUT FORMAT
    #
    # YouTube source may be 480p/720p and ~29.97fps.
    # Normalize it to the project's 1080p/30fps video pipeline.
    #
    # No source audio is retained.
    # ---------------------------------------------------------

    ffmpeg_command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_clip),

        "-t",
        str(duration),

        "-an",

        "-vf",
        (
            "scale=1920:1080:"
            "force_original_aspect_ratio=decrease,"
            "pad=1920:1080:"
            "(ow-iw)/2:"
            "(oh-ih)/2"
        ),

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

    print("NORMALIZING YOUTUBE CLIP WITH FFMPEG...")

    returncode, stdout, stderr = run_command_safe(
        ffmpeg_command,
        FFMPEG_TIMEOUT,
        "FFmpeg YouTube",
    )

    # ---------------------------------------------------------
    # CLEAN SOURCE
    # ---------------------------------------------------------

    try:
        source_clip.unlink()
    except OSError:
        pass

    cleanup_temp_files(output_path.parent)

    # ---------------------------------------------------------
    # FINAL VALIDATION
    # ---------------------------------------------------------

    if (
        returncode != 0
        or not validate_video(output_path)
    ):
        print("YouTube clip normalization failed.")

        if stderr:
            print((stderr or "")[-2000:])

        try:
            if output_path.exists():
                output_path.unlink()
        except OSError:
            pass

        return False

    print()
    print("=" * 70)
    print("VALID YOUTUBE VIDEO CLIP")
    print("=" * 70)
    print(f"Output: {output_path}")

    try:
        print(
            f"Size: "
            f"{output_path.stat().st_size:,} bytes"
        )
    except OSError:
        pass

    print("=" * 70)

    return True

def download_video_with_ytdlp(video_url, output_path, start_time=0, duration=DEFAULT_CLIP_DURATION):
    return download_video_clip(video_url, output_path, start_time, duration)


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
        value = (
            sentence.get("sentence_id")
            or sentence.get("sentence_number")
            or sentence.get("number")
            or sentence.get("index")
        )
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
    return str(
        result.get("url")
        or result.get("link")
        or result.get("video_url")
        or result.get("image_url")
        or ""
    ).strip()


def process_sentence_videos(sentence, sentence_dir, video_count=DEFAULT_VIDEO_COUNT):
    return []


def process_sentence_images(sentence, sentence_dir, image_count=DEFAULT_IMAGE_COUNT):
    return []


def process_sentence_media(
    sentence,
    project_dir,
    scene_number,
    image_count=DEFAULT_IMAGE_COUNT,
    video_count=DEFAULT_VIDEO_COUNT,
):
    return {
        "sentence_number": get_sentence_number(sentence, 1),
        "sentence_text": get_sentence_text(sentence),
        "images": [],
        "videos": [],
        "visuals": [],
        "sentence": sentence,
    }



def process_scene_media(scene, project_dir, image_count=3, video_count=3):
    scene_number = scene.get("scene_number", 1)
    scene_dir = os.path.join(project_dir, f"scene_{scene_number}")
    os.makedirs(scene_dir, exist_ok=True)

    print()
    print("=" * 70)
    print(f"BEAT ASSET ENGINE — SCENE {scene_number}")
    print("=" * 70)

    raw_sentences = get_sentence_list(scene)
    sentence_texts = [get_sentence_text(item) for item in raw_sentences]
    if not any(sentence_texts):
        fallback = str(scene.get("text", scene.get("narration", "")) or "").strip()
        sentence_texts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", fallback) if part.strip()]
        raw_sentences = sentence_texts

    plan = build_visual_plan(sentence_texts)
    shots = plan["shots"]
    registry = get_registry()
    search_cache = {}

    def cached_hits(kind, queries):
        key = kind + "|" + "||".join(queries)
        if key not in search_cache:
            hits = []
            for query in queries:
                print(f"{kind} SHOT QUERY: {query}")
                if kind == "VIDEO":
                    hits.extend(search_licensed_videos(query, limit=4))
                else:
                    hits.extend(search_licensed_images(query, limit=6))
            search_cache[key] = hits
            if hits:
                _record_search_success()
        return list(search_cache[key])

    shot_videos = []
    shot_images = []

    for shot in shots:
        picked_video = False
        for hit in cached_hits("VIDEO", shot.queries):
            url = result_url(hit)
            title = result_title(hit)
            if not url or blocked_visual(title):
                continue
            ok, _reason = registry.can_use(url=url, title=title)
            if not ok:
                continue
            path = os.path.join(scene_dir, f"{shot.beat_id}_{shot.shot_index}_video.mp4")
            good = validate_video(path) or (
                download_video_clip(url, path, 0, 8) and validate_video(path)
            )
            if good:
                shot_videos.append({
                    "file_path": path,
                    "path": path,
                    "video_url": url,
                    "url": url,
                    "title": title,
                    "source": result_source(hit),
                    "asset_scope": (
                        "b_roll"
                        if (
                            shot.shot_type == "real_person_photo"
                            and result_source(hit) in {"pexels", "pixabay"}
                        )
                        else shot.shot_type
                    ),
                    "beat_id": shot.beat_id,
                    "shot_index": shot.shot_index,
                    "scene_number": scene_number,
                    "type": "video",
                    "duration_seconds": 8,
                })
                registry.register(path=path, url=url, title=title)
                picked_video = True
                break
        if picked_video:
            continue
        for hit in cached_hits("IMAGE", shot.queries):
            url = result_url(hit)
            title = result_title(hit)
            if not url or blocked_visual(title):
                continue
            ok, _reason = registry.can_use(url=url, title=title)
            if not ok:
                continue
            path = os.path.join(scene_dir, f"{shot.beat_id}_{shot.shot_index}_image.jpg")
            if validate_image(path) or download_valid_image(url, path):
                if validate_image(path):
                    shot_images.append({
                        "file_path": path,
                        "path": path,
                        "image_url": url,
                        "url": url,
                        "title": title,
                        "source": hit.get("source", ""),
                        "asset_scope": (
                            "b_roll"
                            if (
                                shot.shot_type == "real_person_photo"
                                and str(hit.get("source", "")).lower()
                                in {"pexels", "pixabay"}
                            )
                            else shot.shot_type
                        ),
                        "beat_id": shot.beat_id,
                        "shot_index": shot.shot_index,
                        "scene_number": scene_number,
                        "type": "image",
                    })
                    registry.register(path=path, url=url, title=title)
                    break

    print(f"SHOT VIDEOS: {len(shot_videos)}")
    print(f"SHOT IMAGES: {len(shot_images)}")
    print(f"UNIQUE SEARCHES: {len(search_cache)}")

    sentence_results = []
    used_video = 0
    used_image = 0
    for sentence_index, sentence in enumerate(raw_sentences):
        sentence_number = get_sentence_number(sentence, sentence_index + 1)
        if isinstance(sentence, str):
            sentence_text = sentence
        else:
            sentence_text = get_sentence_text(sentence)
        sentence_videos = []
        sentence_images = []
        if shot_videos:
            sentence_videos = [dict(shot_videos[used_video % len(shot_videos)])]
            used_video += 1
        if shot_images:
            sentence_images = [dict(shot_images[used_image % len(shot_images)])]
            used_image += 1
        for item in sentence_videos + sentence_images:
            item["sentence_number"] = sentence_number
            item["matched_sentence"] = sentence_text
        visuals = []
        for item in sentence_videos:
            copy = dict(item)
            copy["visual_type"] = "video"
            visuals.append(copy)
        for item in sentence_images:
            copy = dict(item)
            copy["visual_type"] = "image"
            visuals.append(copy)
        sentence_results.append({
            "sentence_number": sentence_number,
            "sentence_text": sentence_text,
            "images": sentence_images,
            "videos": sentence_videos,
            "visuals": visuals,
            "sentence": sentence,
            "asset_scope": "beat_reuse",
        })
        print(
            f"SENTENCE {sentence_number}: timing only "
            f"videos={len(sentence_videos)} images={len(sentence_images)}"
        )

    print(f"Registry summary: {registry.summary()}")
    return {
        "scene_number": scene_number,
        "images": shot_images,
        "videos": shot_videos,
        "visuals": shot_videos + shot_images,
        "sentences": sentence_results,
        "scene": scene,
        "asset_engine": "BEAT_PLAN",
        "asset_scope": "beat_reuse",
        "sentence_count": len(sentence_texts),
    }


# =============================================================
# REAL PERSON IDENTITY ENGINE
# REAL_PERSON_IDENTITY_ENGINE_V1
# =============================================================

def search_person_images(person, limit=6):
    """
    Search specifically for real images of a named person.

    Pexels/Pixabay are intentionally NOT used here because
    generic stock media can falsely match a person's name.
    """

    from services.media import search_images

    person = str(person or "").strip()

    if not person:
        return []

    query_variants = [
        f'"{person}"',
        f"{person} photo",
        f"{person} portrait",
        f"{person} trial",
        f"{person} Tallahassee",
    ]

    candidates = []
    seen = set()

    for query in query_variants:

        try:
            results = search_images(
                query,
                limit=8,
            ) or []
        except Exception as exc:
            print(
                f"PERSON SEARCH FAILED: "
                f"{query}: {exc}"
            )
            continue

        for item in results:

            image_url = (
                item.get("image_url")
                or item.get("url")
                or item.get("thumbnail_url")
            )

            if not image_url:
                continue

            if image_url in seen:
                continue

            seen.add(image_url)

            title = str(
                item.get("title") or ""
            )

            snippet = str(
                item.get("snippet") or ""
            )

            source = str(
                item.get("source") or ""
            )

            source_url = str(
                item.get("source_url")
                or item.get("link")
                or ""
            )

            searchable = " ".join(
                [
                    title,
                    snippet,
                    source,
                    source_url,
                ]
            ).lower()

            person_lower = person.lower()

            score = 0

            # -------------------------------------------------
            # Exact full-name match = strongest signal.
            # -------------------------------------------------

            if person_lower in searchable:
                score += 100

            # -------------------------------------------------
            # Individual name components.
            # -------------------------------------------------

            name_parts = [
                part
                for part in re.findall(
                    r"[a-z0-9]+",
                    person_lower,
                )
                if len(part) >= 3
            ]

            for part in name_parts:

                if re.search(
                    rf"\b{re.escape(part)}\b",
                    searchable,
                ):
                    score += 15

            # -------------------------------------------------
            # Documentary/news context.
            # -------------------------------------------------

            context_terms = (
                "trial",
                "murder",
                "missing",
                "disappearance",
                "sentenced",
                "sentence",
                "court",
                "case",
                "arrest",
                "police",
                "tallahassee",
                "florida",
                "victim",
                "defendant",
                "husband",
                "wife",
                "photo",
                "photos",
            )

            for term in context_terms:

                if term in searchable:
                    score += 3

            # -------------------------------------------------
            # Preferred journalism/public-record sources.
            # -------------------------------------------------

            preferred_sources = (
                "tallahassee democrat",
                "cbs news",
                "wfsu",
                "wtxl",
                "associated press",
                "ap news",
                "reuters",
                "washington post",
                "new york post",
                "npr",
                "wikipedia",
            )

            for source_name in preferred_sources:

                if source_name in searchable:
                    score += 25
                    break

            # -------------------------------------------------
            # Reject obvious generic stock results.
            # -------------------------------------------------

            generic_terms = (
                "stock photo",
                "stock image",
                "businessman",
                "businesswoman",
                "generic",
                "model",
                "portrait of a man",
                "portrait of a woman",
                "young man",
                "young woman",
            )

            for generic in generic_terms:

                if generic in searchable:
                    score -= 80

            # Require meaningful identity evidence.
            if score < 80:
                continue

            candidate = dict(item)

            candidate["person_name"] = person
            candidate["entity_type"] = "person"
            candidate["asset_role"] = "real_person_photo"
            candidate["identity_score"] = score
            candidate["identity_confidence"] = round(
                min(1.0, score / 150.0),
                3,
            )
            candidate["evidence_level"] = (
                "identity_candidate"
            )

            # Preserve the actual article/source page.
            candidate["source_url"] = (
                item.get("source_url")
                or item.get("link")
                or ""
            )

            candidates.append(candidate)

    candidates.sort(
        key=lambda item: item.get(
            "identity_score",
            0,
        ),
        reverse=True,
    )

    return candidates[:limit]


def download_person_identity_asset(
    person,
    scene_dir,
    index=1,
):
    """
    Search for and download one strong real-person image.

    Important:
    Publisher CDNs may block direct downloads.
    Therefore we try multiple identity candidates
    and multiple available image URLs.
    """

    from services.media import download_image

    candidates = search_person_images(
        person,
        limit=10,
    )

    if not candidates:
        print(
            f"NO PERSON IMAGE FOUND: {person}"
        )
        return None

    print()
    print("-" * 70)
    print(
        f"PERSON IDENTITY SEARCH: {person}"
    )
    print("-" * 70)

    for rank, candidate in enumerate(
        candidates,
        start=1,
    ):
        print(
            f"{rank}. "
            f"score={candidate.get('identity_score')} "
            f"| "
            f"{candidate.get('source', '')} "
            f"| "
            f"{candidate.get('title', '')}"
        )

    scene_dir = Path(scene_dir)

    scene_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    safe_person = re.sub(
        r"[^a-zA-Z0-9]+",
        "_",
        person.lower(),
    ).strip("_")

    output_path = (
        scene_dir
        / f"person_{safe_person}_{index}.jpg"
    )

    # --------------------------------------------------------
    # Try every strong identity candidate.
    # --------------------------------------------------------

    for candidate_number, candidate in enumerate(
        candidates,
        start=1,
    ):

        urls = []

        for key in (
            "image_url",
            "url",
            "thumbnail_url",
        ):

            value = candidate.get(key)

            if value and value not in urls:
                urls.append(value)

        if not urls:
            continue

        print()
        print(
            f"IDENTITY DOWNLOAD CANDIDATE "
            f"{candidate_number}/{len(candidates)}"
        )

        print(
            f"Person: {person}"
        )

        print(
            f"Title: {candidate.get('title', '')}"
        )

        print(
            f"Source: {candidate.get('source', '')}"
        )

        for url_number, image_url in enumerate(
            urls,
            start=1,
        ):

            print(
                f"Trying image URL "
                f"{url_number}/{len(urls)}..."
            )

            try:

                if output_path.exists():
                    output_path.unlink()

            except OSError:
                pass

            try:

                download_image(
                    image_url,
                    str(output_path),
                )

            except Exception as exc:

                print(
                    f"Download failed: {exc}"
                )

                continue

            # ------------------------------------------------
            # Make sure something real was actually created.
            # ------------------------------------------------

            if not output_path.exists():

                print(
                    "No file was created."
                )

                continue

            try:

                file_size = output_path.stat().st_size

            except OSError:

                file_size = 0

            if file_size < 10_000:

                print(
                    f"Downloaded file too small: "
                    f"{file_size} bytes"
                )

                try:
                    output_path.unlink()
                except OSError:
                    pass

                continue

            # ------------------------------------------------
            # Validate that it is actually an image.
            # ------------------------------------------------

            try:

                if not validate_image(output_path):

                    print(
                        "Downloaded file failed "
                        "image validation."
                    )

                    output_path.unlink()

                    continue

            except Exception as exc:

                print(
                    f"Image validation failed: {exc}"
                )

                try:
                    output_path.unlink()
                except OSError:
                    pass

                continue

            # ------------------------------------------------
            # SUCCESS
            # ------------------------------------------------

            asset = dict(candidate)

            asset["file_path"] = str(
                output_path
            )

            asset["path"] = str(
                output_path
            )

            asset["image_url"] = (
                image_url
            )

            asset["url"] = (
                image_url
            )

            asset["asset_role"] = (
                "real_person_photo"
            )

            asset["entity_type"] = (
                "person"
            )

            asset["person_name"] = (
                person
            )

            asset["identity_locked"] = (
                True
            )

            # Do NOT falsely claim a news image is
            # royalty-free just because it was found
            # through search.
            asset["rights_status"] = (
                "verify_before_publish"
            )

            asset["license"] = (
                "unknown"
            )

            print()
            print("=" * 70)
            print(
                f"PERSON IMAGE DOWNLOADED: {person}"
            )
            print(
                f"File: {output_path}"
            )
            print(
                f"Size: {file_size:,} bytes"
            )
            print(
                f"Source: "
                f"{candidate.get('source', '')}"
            )
            print("=" * 70)

            return asset

    print()
    print(
        f"ALL PERSON IMAGE DOWNLOADS FAILED: "
        f"{person}"
    )

    return None


def _identity_people_in_sentence(sentence):
    """
    Detect the named people already known by the
    documentary entity system.
    """

    text = str(sentence or "")
    low = text.lower()

    people = []

    # First use the existing entity extractor.
    try:

        entities = extract_entities(text)

        people = list(
            getattr(
                entities,
                "people",
                [],
            )
            or []
        )

    except Exception:
        people = []

    # Safety fallback to the current named-person bank.
    if not people:

        for name in NAMED_PEOPLE:

            if name.lower() in low:

                people.append(
                    name.title()
                )

    # Remove duplicates while preserving order.
    output = []

    for person in people:

        if person not in output:
            output.append(person)

    return output


def _add_identity_assets_to_scene(
    result,
    scene,
    project_dir,
):
    """
    Adds real-person identity assets to the existing
    scene result without replacing generic B-roll.
    """

    if not isinstance(result, dict):
        return result

    sentences = result.get(
        "sentences",
        [],
    ) or []

    if not sentences:
        return result

    scene_number = result.get(
        "scene_number",
        scene.get(
            "scene_number",
            1,
        ),
    )

    scene_dir = (
        Path(project_dir)
        / f"scene_{scene_number}"
    )

    # ---------------------------------------------------------
    # Determine which named people actually occur in this scene.
    # ---------------------------------------------------------

    scene_people = []

    for sentence_result in sentences:

        sentence_text = (
            sentence_result.get(
                "sentence_text"
            )
            or sentence_result.get(
                "sentence"
            )
            or ""
        )

        for person in _identity_people_in_sentence(
            sentence_text
        ):

            if person not in scene_people:
                scene_people.append(person)

    if not scene_people:
        return result

    print()
    print("=" * 70)
    print(
        f"IDENTITY ENGINE — SCENE {scene_number}"
    )
    print(
        "People:",
        ", ".join(scene_people),
    )
    print("=" * 70)

    person_assets = {}

    # ---------------------------------------------------------
    # One strong identity anchor per person per scene.
    # ---------------------------------------------------------

    for index, person in enumerate(
        scene_people,
        start=1,
    ):

        asset = download_person_identity_asset(
            person,
            scene_dir,
            index=index,
        )

        if asset:

            person_assets[person] = asset

    if not person_assets:
        print(
            "IDENTITY ENGINE: no usable person assets"
        )
        return result

    # ---------------------------------------------------------
    # Add to scene-level image/visual pools.
    # ---------------------------------------------------------

    scene_images = list(
        result.get(
            "images",
            [],
        )
        or []
    )

    scene_visuals = list(
        result.get(
            "visuals",
            [],
        )
        or []
    )

    existing_paths = {
        str(item.get("file_path"))
        for item in scene_images
        if item.get("file_path")
    }

    for person, asset in person_assets.items():

        path = str(
            asset.get("file_path")
        )

        if path not in existing_paths:

            scene_images.insert(
                0,
                dict(asset),
            )

            scene_visuals.insert(
                0,
                dict(asset),
            )

            existing_paths.add(path)

    result["images"] = scene_images
    result["visuals"] = scene_visuals

    # ---------------------------------------------------------
    # Identity-lock sentences.
    #
    # This is the critical part:
    #
    # sentence mentioning Mike Williams
    #        ↓
    # Mike Williams asset
    #
    # NOT:
    #
    # sentence → random round-robin asset
    # ---------------------------------------------------------

    for sentence_result in sentences:

        sentence_text = (
            sentence_result.get(
                "sentence_text"
            )
            or sentence_result.get(
                "sentence"
            )
            or ""
        )

        people = _identity_people_in_sentence(
            sentence_text
        )

        if not people:
            continue

        selected_person = None
        selected_asset = None

        for person in people:

            if person in person_assets:

                selected_person = person
                selected_asset = (
                    person_assets[person]
                )

                break

        if not selected_asset:
            continue

        sentence_images = list(
            sentence_result.get(
                "images",
                [],
            )
            or []
        )

        sentence_visuals = list(
            sentence_result.get(
                "visuals",
                [],
            )
            or []
        )

        identity_path = str(
            selected_asset.get(
                "file_path"
            )
        )

        already_image = any(
            str(item.get("file_path"))
            == identity_path
            for item in sentence_images
        )

        if not already_image:

            sentence_images.insert(
                0,
                dict(selected_asset),
            )

        already_visual = any(
            str(item.get("file_path"))
            == identity_path
            for item in sentence_visuals
        )

        if not already_visual:

            sentence_visuals.insert(
                0,
                dict(selected_asset),
            )

        sentence_result["images"] = (
            sentence_images
        )

        sentence_result["visuals"] = (
            sentence_visuals
        )

        sentence_result["identity_entity"] = (
            selected_person
        )

        sentence_result["identity_asset"] = (
            dict(selected_asset)
        )

        sentence_result["identity_locked"] = True

    result["person_assets"] = list(
        person_assets.values()
    )

    result["identity_assets"] = list(
        person_assets.values()
    )

    result["identity_engine"] = (
        "REAL_PERSON_SERPER"
    )

    return result


# Keep the original scene engine intact.
_original_process_scene_media = process_scene_media


# =============================================================
# REAL PERSON IDENTITY WRAPPER
# REAL_PERSON_IDENTITY_WRAPPER_V1
# =============================================================

_original_process_scene_media = process_scene_media


def process_scene_media(
    scene,
    project_dir,
    image_count=3,
    video_count=3,
):
    """
    Run the original BEAT ASSET ENGINE first,
    then inject verified named-person identity assets.
    """

    result = _original_process_scene_media(
        scene,
        project_dir,
        image_count=image_count,
        video_count=video_count,
    )

    result = _add_identity_assets_to_scene(
        result,
        scene,
        project_dir,
    )

    # EVIDENCE SENTENCE CONTEXT V2

    def _get_sentence_text(sentence_data):
        if not isinstance(sentence_data, dict):
            return ""

        for key in (
            "text",
            "sentence",
            "script_text",
            "content",
            "narration",
            "line",
        ):
            value = sentence_data.get(key)

            if isinstance(value, str) and value.strip():
                return value.strip()

        return ""

    def _enrich_list(items, sentence_text=""):
        if not isinstance(items, list):
            return items

        for item in items:
            if not isinstance(item, dict):
                continue

            enrich_visual_with_evidence(
                item,
                shot_type=(
                    item.get("asset_scope")
                    or item.get("asset_role")
                    or ""
                ),
                query=(
                    item.get("query")
                    or item.get("search_query")
                    or ""
                ),
                sentence=sentence_text,
            )

        return items

    # Scene-level collections
    _enrich_list(
        result.get("images", []),
        " ".join(
            str(s.get("text", ""))
            for s in result.get("sentences", [])
            if isinstance(s, dict)
        ),
    )

    _enrich_list(
        result.get("videos", []),
        " ".join(
            str(s.get("text", ""))
            for s in result.get("sentences", [])
            if isinstance(s, dict)
        ),
    )

    _enrich_list(
        result.get("visuals", []),
        " ".join(
            str(s.get("text", ""))
            for s in result.get("sentences", [])
            if isinstance(s, dict)
        ),
    )

    # Sentence-level collections
    sentence_contexts = []

    for sentence_index, sentence_result in enumerate(
        result.get("sentences", []),
        start=1,
    ):
        if not isinstance(sentence_result, dict):
            continue

        sentence_text = _get_sentence_text(sentence_result)

        if sentence_text:
            sentence_result["evidence_sentence"] = sentence_text

        sentence_contexts.append(sentence_text)

        _enrich_list(
            sentence_result.get("images", []),
            sentence_text,
        )

        _enrich_list(
            sentence_result.get("videos", []),
            sentence_text,
        )

        _enrich_list(
            sentence_result.get("visuals", []),
            sentence_text,
        )

    result["evidence_sentence_context"] = sentence_contexts
    result["evidence_layer"] = "UNIVERSAL_EVIDENCE_V1"

    return result

