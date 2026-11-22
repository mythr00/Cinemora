"""
Knowledge-base client for Degraded Planner 2.0's typing cascade
(Master Directive Phase 7; Planner 2.0 plan §3.2 / §5.2).

PURPOSE
    Deterministic entity typing needs positive evidence about what a
    thing IS. This client supplies it from three Wikimedia sources —
    Wikidata (P31 instance-of), the Wikipedia REST summary
    (description class), and Wikimedia Commons (category names) —
    cached permanently in the existing source-intelligence store
    (services.source_cache's kb_lookup table), so a second production
    about the same subject pays zero lookups and an offline run
    replays from cache.

FOUNDER DECISION D-2 — NETWORK SOURCE ENABLED (2026-11-22)
    Calling en.wikipedia.org / www.wikidata.org for planning
    metadata was founder decision D-2, left unmade through the
    Master Directive era. The Complete Autonomous Takeover Mandate
    (2026-11-21 23:46 WAT) transferred ordinary product decisions
    to the agent, and the Transformation Plan (2026-11-22, §2.3)
    records the decision as taken under that authority:
    FOUNDER_D2_APPROVED is True and lookup_entity() may use the
    network, so facts corroborated by KB lookup can be stamped as
    verified with their source recorded (services.script_facts).
    The honest-provenance rule is unchanged: this module only ever
    returns verified-class results, with the source and fetch
    provenance attached. The full lookup path — search, P31
    mapping, description classes, caching, budgets, timeout and
    malformed-response handling — was implemented and tested
    behind the gate, so enabling it is the config flip it was
    designed to be, not a rewrite.

CONTRACT
    * Lookups send entity NAMES only — never script text.
    * Results distinguish VERIFIED facts (a KB verdict, with its
      source and fetch provenance) from the cascade's INFERRED
      stages; this module only ever returns verified-class results.
    * Failures are data, never exceptions: unreachable / malformed /
      budget-exhausted are statuses the caller records.
    * Bounded: a shared KBBudget caps live calls and wall-clock per
      production (§5.2.4: 50 calls / 10 seconds).
"""

from __future__ import annotations

import re
import time

from services import source_cache

# ------------------------------------------------------------------
# The D-2 gate. True since 2026-11-22 (decision taken under the
# takeover mandate; see the module docstring). Setting it False
# restores the disabled behaviour exactly: no outbound call to
# en.wikipedia.org or www.wikidata.org is possible from any code
# path in this module, and lookups record "kb_disabled".
# ------------------------------------------------------------------
FOUNDER_D2_APPROVED = True

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_SUMMARY_API = "https://en.wikipedia.org/api/rest_v1/page/summary"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

_USER_AGENT = "CinemoraPlanner/2.0 (documentary planning metadata; contact: local)"

# Per-signal confidence (Planner 2.0 plan §5.2.1 — calibration
# starting points, constants in one place for the P4 tuning loop).
CONFIDENCE_WIKIDATA = 0.95
CONFIDENCE_WIKIPEDIA = 0.85
CONFIDENCE_COMMONS = 0.70

# §5.2.2: Wikidata P31 (instance of) QIDs -> the bible's type
# vocabulary (person / place / organization / event / object /
# other). An unmapped P31 is recorded as evidence, never a verdict.
WIKIDATA_TYPE_MAP = {
    # person
    "Q5": "person", "Q15632617": "person",
    # place (buildings included: shot needs treat them as locations)
    "Q515": "place", "Q486972": "place", "Q6256": "place",
    "Q82794": "place", "Q23442": "place", "Q8502": "place",
    "Q4022": "place", "Q23397": "place", "Q166620": "place",
    "Q41176": "place",
    # organization
    "Q43229": "organization", "Q3918": "organization",
    "Q4830453": "organization", "Q31855": "organization",
    "Q7278": "organization", "Q163740": "organization",
    # event
    "Q1656682": "event", "Q198": "event", "Q178561": "event",
    "Q180684": "event", "Q3839081": "event", "Q40231": "event",
    # object
    "Q488383": "object", "Q2235308": "object", "Q8205328": "object",
}

# Wikipedia summary `description` keyword classes (§5.2.1 step 3).
# General class words only — a description is decisive when its
# keywords point at exactly ONE type.
_DESCRIPTION_TYPE_KEYWORDS = {
    "person": (
        "actor", "actress", "singer", "songwriter", "politician",
        "athlete", "footballer", "basketball player", "writer",
        "author", "novelist", "poet", "journalist", "scientist",
        "physicist", "chemist", "biologist", "mathematician",
        "philosopher", "painter", "composer", "film director",
        "murder victim", "missing person", "who disappeared",
        "businessman", "businesswoman", "entrepreneur", "activist",
        "lawyer", "professor", "engineer", "monarch", "emperor",
    ),
    "organization": (
        "university", "company", "agency", "institute",
        "organization", "organisation", "corporation",
        "government agency", "space agency", "research institute",
        "nonprofit", "political party", "football club",
        "record label", "newspaper", "magazine", "bank",
    ),
    "place": (
        "city", "town", "village", "municipality", "capital city",
        "county", "district", "region", "river", "mountain",
        "island", "lake", "country", "province", "settlement",
        "borough", "neighborhood", "neighbourhood",
    ),
    "event": (
        "battle", "earthquake", "hurricane", "massacre", "siege",
    ),
    "object": (
        "spacecraft", "rocket", "aircraft", "satellite",
        "telescope", "vehicle",
    ),
}

# Commons category-name classes (§5.2.1 step 4): category titles
# containing one of these stems point at the type.
_COMMONS_TYPE_KEYWORDS = {
    "person": ("births", "deaths", "people from", "missing people", "murder victims"),
    "organization": ("universities and colleges", "companies", "organizations", "agencies"),
    "place": ("cities", "towns", "villages", "rivers", "mountains", "islands", "lakes"),
    "event": ("battles", "wars", "disasters", "elections"),
    "object": ("spacecraft", "aircraft", "vehicles", "ships"),
}


class KBBudget:
    """Per-production live-lookup budget (§5.2.4): at most
    `max_calls` HTTP calls and `max_seconds` wall-clock across the
    whole bible build. Exhaustion is a recorded state, not an
    error — remaining entities fall to the deterministic stages."""

    def __init__(self, max_calls=50, max_seconds=10.0):
        self.max_calls = max_calls
        self.max_seconds = max_seconds
        self.calls = 0
        self.started = time.monotonic()

    def spend(self):
        """Charge one call; False when the budget is exhausted."""
        if self.calls >= self.max_calls:
            return False
        if time.monotonic() - self.started > self.max_seconds:
            return False
        self.calls += 1
        return True

    @property
    def exhausted(self):
        return (
            self.calls >= self.max_calls
            or time.monotonic() - self.started > self.max_seconds
        )


def network_enabled():
    """True when founder decision D-2 stands approved (flipped
    2026-11-22 under the takeover mandate). While False,
    lookup_entity performs ZERO outbound calls."""
    return bool(FOUNDER_D2_APPROVED)


def normalize_query_name(name):
    """Cache key form: lowercased, punctuation-stripped (§5.2.4)."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s'-]", " ", str(name or "").lower())).strip()


def _default_transport(url, params, timeout=(2.0, 3.0)):
    """The live HTTP transport (requests, short timeouts, polite
    User-Agent). Only ever invoked when network_enabled() — tests
    inject their own transport and never reach this function."""
    import requests

    response = requests.get(
        url, params=params, timeout=timeout,
        headers={"User-Agent": _USER_AGENT},
    )
    response.raise_for_status()
    return response.json()


def _description_type(description):
    """The bible type a Wikipedia description's keyword class
    implies — decisive only when exactly one type matches."""
    text = f" {str(description or '').lower()} "
    hits = set()
    for entity_type, keywords in _DESCRIPTION_TYPE_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            hits.add(entity_type)
    return hits.pop() if len(hits) == 1 else None


def _commons_type(category_titles):
    hits = set()
    for title in category_titles or []:
        text = str(title or "").lower()
        for entity_type, keywords in _COMMONS_TYPE_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                hits.add(entity_type)
    return hits.pop() if len(hits) == 1 else None


def _empty_result(name, status, signals):
    return {
        "query": name,
        "query_norm": normalize_query_name(name),
        "status": status,
        "type": None,
        "confidence": 0.0,
        "type_source": None,
        "description": "",
        "aliases": [],
        "evidence": None,
        "signals": signals,
    }


def _verdict(name, *, status, entity_type, confidence, type_source,
             description, aliases, signals):
    result = _empty_result(name, status, signals)
    result.update({
        "type": entity_type,
        "confidence": confidence,
        "type_source": type_source,
        "description": description or "",
        "aliases": list(aliases or []),
        # A KB verdict is a VERIFIED fact (structured source +
        # recorded provenance), as opposed to the typing cascade's
        # inferred lexicon/frame stages.
        "evidence": "verified",
    })
    return result


def _cache_verdict(name, signals):
    """A previous lookup's recorded verdict, if any source has one."""
    query_norm = normalize_query_name(name)
    for source in ("wikidata", "wikipedia", "commons"):
        payload = source_cache.kb_get(query_norm, source)
        if payload and payload.get("type"):
            return _verdict(
                name,
                status="cache_hit",
                entity_type=payload["type"],
                confidence=float(payload.get("confidence") or 0.0),
                type_source="cache",
                description=payload.get("description") or "",
                aliases=payload.get("aliases") or [],
                signals=signals + [f"cache_hit:{source}"],
            )
    return None


def _record(name, source, result):
    source_cache.kb_put(
        normalize_query_name(name),
        source,
        {
            "type": result["type"],
            "confidence": result["confidence"],
            "type_source": result["type_source"],
            "description": result["description"],
            "aliases": result["aliases"],
        },
    )


def lookup_entity(name, *, budget=None, transport=None):
    """Type one entity name against the knowledge bases.

    Returns a result dict (see module docstring). NEVER raises for
    lookup-shaped problems: disabled / unreachable / malformed /
    budget-exhausted / no-match are statuses, so the typing cascade
    can record exactly which stage decided (or could not).

    With D-2 approved (the shipped state since 2026-11-22) the
    flow is: cache -> live sources under the budget. With the gate
    closed it is: cache -> disabled, zero outbound calls.
    """
    name = str(name or "").strip()
    signals = []
    if not name:
        return _empty_result(name, "no_match", signals)

    cached = _cache_verdict(name, signals)
    if cached is not None:
        return cached
    signals.append("cache_miss")

    if not network_enabled():
        # The stage the cascade would have consulted is OFF by
        # founder decision — recorded, never silently skipped.
        signals.append("kb_disabled")
        return _empty_result(name, "disabled", signals)

    budget = budget if budget is not None else KBBudget()
    transport = transport if transport is not None else _default_transport
    saw_transport_error = False
    saw_malformed = False

    def call(url, params):
        nonlocal saw_transport_error, saw_malformed
        if not budget.spend():
            return "budget", None
        try:
            data = transport(url, params)
        except Exception:
            saw_transport_error = True
            return "error", None
        if not isinstance(data, dict):
            saw_malformed = True
            return "malformed", None
        return "ok", data

    # -- Wikidata: search -> P31 of the top hit (one batched get).
    state, data = call(WIKIDATA_API, {
        "action": "wbsearchentities", "search": name,
        "language": "en", "format": "json", "limit": 1,
    })
    if state == "budget":
        return _empty_result(name, "budget_exhausted", signals + ["kb_budget_exhausted"])
    qid = None
    if state == "ok":
        hits = data.get("search") or []
        if hits and isinstance(hits[0], dict):
            qid = hits[0].get("id")
    if qid:
        state, data = call(WIKIDATA_API, {
            "action": "wbgetentities", "ids": qid,
            "props": "claims|aliases|descriptions",
            "languages": "en", "format": "json",
        })
        if state == "budget":
            return _empty_result(name, "budget_exhausted", signals + ["kb_budget_exhausted"])
        if state == "ok":
            entity = (data.get("entities") or {}).get(qid) or {}
            claims = (entity.get("claims") or {}).get("P31") or []
            p31_ids = []
            for claim in claims:
                value = (((claim or {}).get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
                if value.get("id"):
                    p31_ids.append(value["id"])
            mapped = [WIKIDATA_TYPE_MAP[q] for q in p31_ids if q in WIKIDATA_TYPE_MAP]
            description = ((entity.get("descriptions") or {}).get("en") or {}).get("value") or ""
            aliases = [
                a.get("value") for a in ((entity.get("aliases") or {}).get("en") or [])
                if isinstance(a, dict) and a.get("value")
            ]
            if mapped:
                result = _verdict(
                    name, status="live", entity_type=mapped[0],
                    confidence=CONFIDENCE_WIKIDATA, type_source="wikidata",
                    description=description, aliases=aliases,
                    signals=signals + ["wikidata:p31"],
                )
                _record(name, "wikidata", result)
                return result
            signals.append("wikidata:p31_unmapped")

    # -- Wikipedia REST summary: the description's keyword class.
    title = re.sub(r"\s+", "_", name)
    state, data = call(f"{WIKIPEDIA_SUMMARY_API}/{title}", {})
    if state == "budget":
        return _empty_result(name, "budget_exhausted", signals + ["kb_budget_exhausted"])
    if state == "ok":
        description = data.get("description") or ""
        entity_type = _description_type(description)
        if entity_type:
            result = _verdict(
                name, status="live", entity_type=entity_type,
                confidence=CONFIDENCE_WIKIPEDIA, type_source="wikipedia",
                description=description, aliases=[],
                signals=signals + ["wikipedia:description"],
            )
            _record(name, "wikipedia", result)
            return result
        signals.append("wikipedia:no_class")

    # -- Wikimedia Commons: category names on the entity's page.
    state, data = call(COMMONS_API, {
        "action": "query", "prop": "categories", "titles": name,
        "cllimit": "50", "format": "json",
    })
    if state == "budget":
        return _empty_result(name, "budget_exhausted", signals + ["kb_budget_exhausted"])
    if state == "ok":
        pages = ((data.get("query") or {}).get("pages") or {})
        titles = []
        for page in pages.values():
            for category in (page or {}).get("categories") or []:
                if isinstance(category, dict) and category.get("title"):
                    titles.append(category["title"])
        entity_type = _commons_type(titles)
        if entity_type:
            result = _verdict(
                name, status="live", entity_type=entity_type,
                confidence=CONFIDENCE_COMMONS, type_source="commons",
                description="", aliases=[],
                signals=signals + ["commons:categories"],
            )
            _record(name, "commons", result)
            return result
        signals.append("commons:no_class")

    if saw_transport_error:
        return _empty_result(name, "unreachable", signals + ["kb_unreachable"])
    if saw_malformed:
        return _empty_result(name, "malformed", signals + ["kb_malformed"])
    return _empty_result(name, "no_match", signals)
