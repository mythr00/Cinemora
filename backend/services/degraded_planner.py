"""
Degraded Planner 2.0 — the deterministic, no-LLM planning engine
(Master Directive Phase 7; DEGRADED_PLANNER_2_PLAN_2026-11-21).

Degraded mode is a FIRST-CLASS planner, not a shameful fallback:
when no LLM is configured, when the provider fails, or when a batch
of LLM output is unusable, this module produces the plan instead —
extraction, typing, subjects, roles, places, eras and scenarios —
deterministically, with every load-bearing judgement carrying a
confidence and the name of the signal that produced it.

It replaces the old capitalisation-counter heuristic
(`heuristic_bible` + `normalize_plan`'s synthesised scenario):

  * Extraction 2.0 (§5.1): capitalised runs scored by recurrence and
    position; a CLOSED-CLASS SCREEN makes phantom entities from
    pronouns / question words / sentence-initial function words
    (`what`, `their`, …) impossible — there is no code path that
    admits them; alias clustering incl. initialisms.
  * Typing (§5.2): a cascade — KB cache -> KB stage -> lexicon
    name-structure -> context frames. The old place-preposition
    VOTE IS DELETED: type comes only from positive evidence about
    what the thing is, or the entity is honestly `other`.
  * Subjects (§5.3): a mention graph with salience scoring and
    pronoun carry with confidence decay; a sentence may honestly
    have NO subject instead of anchoring on a wrong entity.
  * Roles (§5.4): a deterministic cue classifier over the existing
    12-role vocabulary (the old degraded path assigned `background`
    to every sentence by construction).
  * Places & eras (§5.5): a gazetteer of place-TYPED entities (plus
    lowercase-recovered mentions) feeds per-sentence place/time —
    the fields the old degraded plans left empty on 39/39 sentences.
  * Scenario factory (§5.6): 1–4 scenarios per sentence with REAL
    search terms, proposed UPSTREAM of the unchanged
    `normalize_plan` validator and judged by it like everyone
    else's — never constructed downstream of it.

FOUNDER-GATE BOUNDARIES (Phase 7 scope):
  * D-1 (corroboration-based specificity) is UNMADE. The factory
    stamps specificity with EXACTLY today's degraded rule — a
    scenario anchored on a resolved bible entity is `exact`, an
    unanchored one is `generic` (the rule in normalize_plan's
    synthesis branch). The corroboration computation exists here
    (`specificity_basis`, recorded per scenario and aggregated in
    the plan's quality block) but NEVER drives the stamp, and the
    low-confidence scenario widening is not applied.
  * D-2 (Wikipedia/Wikidata lookups) is UNMADE. The KB stage runs
    through services.kb_client with its network source OFF: it
    consults the cache, records `kb_disabled` in the entity's
    signals, and returns no verdict. Zero outbound calls.
  * DM-2(b) (degraded-plan policy) is not implemented — only the
    recording half (mode / degraded_reason / counts) lands, in
    story_understanding's planner block.
  * This module NEVER calls an LLM and never touches the network.

Rollout (P5): wired at the two existing fallback points
(build_case_bible's bible fallback, analyze_sentences' missing
batch items) behind the CINEMORA_DEGRADED_PLANNER hatch — default
ON; `=1` (or legacy/heuristic/off/0) selects the old heuristic,
which stays importable as the escape hatch.
"""

from __future__ import annotations

import os
import re
from collections import Counter

from services import kb_client
from services import story_understanding as story

PLANNER_VERSION = "degraded-2.0"

#: CINEMORA_DEGRADED_PLANNER values that select the OLD heuristic.
_HATCH_OFF_VALUES = {"0", "1", "legacy", "heuristic", "off", "false"}

#: §5.2.5: an entity's type is TRUSTED (may anchor the primary
#: subject and subject scenarios without a low-confidence flag) only
#: at or above this confidence.
TYPE_CONFIDENCE_FLOOR = 0.7

#: Per-signal typing confidences (§5.2.1 — calibration constants in
#: one place; the KB steps' values live in kb_client).
TYPING_SIGNAL_CONFIDENCE = {
    "lexicon": 0.75,
    "acronym": 0.70,
    "acronym_unexpanded": 0.55,
    "frames": 0.55,
    "none": 0.40,
}

#: §5.3 salience weights (calibration constants in one place).
SALIENCE_WEIGHTS = {
    "mention": 3.0,
    "mention_decay": 0.85,
    "first_in_sentence": 2.0,
    "early_mention": 1.5,
    "recurrence": 1.0,
    "type_bonus": {"person": 1.0, "event": 0.5},
}

_SUBJECT_MARGIN = 1.5          # §5.3: salience margin for a clear win
_PRONOUN_CARRY_BASE = 0.9      # §5.3: carry confidence at distance 0
_PRONOUN_CARRY_DECAY = 0.8     # §5.3: per-sentence decay
_PRONOUN_CARRY_FLOOR = 0.4     # §5.3: below this the carry is refused


def planner_enabled():
    """The P5 hatch: Planner 2.0 is the default degraded planner;
    CINEMORA_DEGRADED_PLANNER=1 (legacy/heuristic/off/0) selects
    the old heuristic escape hatch."""
    value = str(os.environ.get("CINEMORA_DEGRADED_PLANNER") or "").strip().lower()
    return value not in _HATCH_OFF_VALUES


# ------------------------------------------------------------------
# §5.1 Extraction 2.0 — closed-class screen + candidate scoring
# ------------------------------------------------------------------

#: Closed grammatical classes that can never be entities in English:
#: pronouns + possessives (story.PRONOUNS), question words, and
#: sentence-initial function words. This is a grammatical-class
#: screen, not a per-script stoplist.
_QUESTION_WORDS = {"who", "whom", "whose", "what", "which", "when", "where", "why", "how"}
_FUNCTION_OPENERS = {
    "the", "a", "an", "and", "but", "or", "so", "yet", "then", "now",
    "here", "there", "after", "before", "during", "while", "as", "if",
    "on", "in", "at", "by", "for", "from", "with", "into", "over",
    "under", "of", "to", "once", "when", "while",
    # Sentence-initial prepositions/conjunctions/quantifiers that
    # capitalised-run extraction sees capitalised ("Within days the
    # barbed wire gave way..."): as openers they are grammar, not
    # names, so they strip like months do.
    "within", "among", "between", "through", "against", "behind",
    "upon", "across", "along", "until", "since", "although",
    "though", "whenever", "wherever", "every", "each", "all",
    "both", "either", "neither", "any", "another", "more", "most",
    "much", "many", "few",
}

#: Globally-known institutional initialisms — a local lexicon
#: resource of the same kind as the suffix tables: these initials
#: denote institutions in general English usage, independent of
#: any particular script.
_KNOWN_INSTITUTION_ACRONYMS = {
    "NASA", "FBI", "CIA", "NSA", "NATO", "BBC", "CNN", "UN", "EU",
    "WHO", "IMF", "OPEC", "IRS", "SEC", "FDA", "EPA", "CDC", "ATF",
    "DEA", "DOJ", "KGB", "MI5", "MI6", "USSR", "MIT",
}

#: Calendar words pattern like months (story.MONTHS): a weekday may
#: sit inside a genuine multiword name at an edge, but a calendar
#: word alone is never an entity ("Monday demonstrations" is about
#: a place and a movement, not an entity called Monday).
_CALENDAR_TITLES = set(story.MONTHS) | {
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
    "Saturday", "Sunday",
}
CLOSED_CLASS_TOKENS = set(story.PRONOUNS) | _QUESTION_WORDS | _FUNCTION_OPENERS


def _candidate_stats(sentences):
    """Capitalised-run candidates with occurrence evidence.

    Mirrors heuristic_bible's run processing (month stripping, mid-
    sentence evidence) and adds the closed-class screen: leading
    closed-class tokens are stripped from sentence-initial runs
    exactly the way months are, and a single-token candidate whose
    lowercase form is a closed-class word is rejected outright —
    in ANY position. `what` (post-colon capital) and `their`
    (possessive) therefore have no admission path.
    """
    stats = {}
    for si, sentence in enumerate(sentences):
        for run in story.capitalized_runs(sentence):
            words = list(run["words"])
            prev = run["prev"]
            if run["initial"]:
                while words and (
                    words[0] in _CALENDAR_TITLES
                    or words[0].lower() in CLOSED_CLASS_TOKENS
                ):
                    prev = words.pop(0).lower()
            else:
                while words and words[0] in _CALENDAR_TITLES:
                    prev = words.pop(0).lower()
            while words and words[-1] in _CALENDAR_TITLES:
                words.pop()
            if not words:
                continue
            # A trailing possessive is a form of the bare name, not
            # a different entity ("Hungary's" -> "Hungary").
            if words and (words[-1].endswith("'s") or words[-1].endswith("’s")):
                words[-1] = words[-1][:-2]
                if not words[-1]:
                    words.pop()
            if not words:
                continue
            if len(words) == 1 and (
                words[0].lower() in CLOSED_CLASS_TOKENS
                or words[0] in _CALENDAR_TITLES
            ):
                continue
            text = " ".join(words)
            entry = stats.setdefault(text, {
                "words": words, "count": 0, "mid": 0,
                "sentences": [], "prevs": [],
            })
            entry["count"] += 1
            if not run["initial"] or len(words) < len(run["words"]) or len(words) > 1:
                entry["mid"] += 1
            if si not in entry["sentences"]:
                entry["sentences"].append(si)
            entry["prevs"].append(prev)
    return stats


def _initials(words):
    return "".join(w[0].upper() for w in words if w)


def _cluster_candidates(stats):
    """Alias clustering (§5.1): token-subset absorption (the old
    rule, kept) plus initialism matching (an all-caps short form
    whose letters spell a multiword candidate's initials). The
    canonical is the most frequent full form; every other surface
    form becomes an alias."""
    admitted = {
        text: entry for text, entry in stats.items()
        if entry["mid"] >= 1 or entry["count"] >= 2
    }
    multi = {t: e for t, e in admitted.items() if len(e["words"]) > 1}
    ordered = sorted(
        admitted.items(),
        key=lambda kv: (-len(kv[1]["words"]), -kv[1]["count"], kv[0]),
    )
    clusters = []
    absorbed = set()
    for text, entry in ordered:
        if text in absorbed:
            continue
        aliases = []
        if len(entry["words"]) > 1:
            for other, other_entry in admitted.items():
                if other == text or other in absorbed:
                    continue
                if len(other_entry["words"]) != 1:
                    continue
                token = other_entry["words"][0]
                owners = [m for m in multi if token in multi[m]["words"]]
                is_initialism = (
                    re.fullmatch(r"[A-Z]{2,6}", token) is not None
                    and _initials(entry["words"]) == token
                )
                if (token in entry["words"] and len(owners) == 1) or is_initialism:
                    aliases.append(other)
                    absorbed.add(other)
        sentences = list(entry["sentences"])
        count = entry["count"]
        for alias in aliases:
            alias_entry = admitted[alias]
            count += alias_entry["count"]
            for si in alias_entry["sentences"]:
                if si not in sentences:
                    sentences.append(si)
        clusters.append({
            "canonical": text,
            "words": entry["words"],
            "aliases": aliases,
            "count": count,
            "sentences": sorted(sentences),
        })
    return clusters


# ------------------------------------------------------------------
# §5.2 Typing cascade (KB stage disabled — see module docstring)
# ------------------------------------------------------------------

#: Lexicon name-structure rules (§5.2.3). Suffix rules apply to the
#: FINAL token of a multi-word candidate only — a bare two-word name
#: is never typed `person` by shape (that was the old rule's error
#: in reverse), and a single token is never typed by suffix.
_ORG_SUFFIXES = {
    "University", "Institute", "College", "Company", "Inc", "Corp",
    "Corporation", "Agency", "Bureau", "Department", "Army", "Navy",
    "Court", "Hospital", "Museum", "Foundation", "Association",
    "Commission", "Administration", "School", "Bank", "Airlines",
    "Studios", "Laboratories",
}
_PLACE_SUFFIXES = {
    "City", "Town", "River", "Mount", "Lake", "Island", "County",
    "State", "Street", "Bridge", "Park", "Square", "Bay", "Beach",
    "Harbor", "Harbour", "Valley", "Desert", "Ocean", "Sea",
    "Falls", "Springs",
}

#: Context-frame verb classes (§5.2.3). Agent/patient frames vote
#: PERSON — `happened to X`, `search for X` are person evidence,
#: the exact constructions that poisoned the old preposition vote.
_PERSON_SUBJECT_VERBS = (
    "said", "told", "asked", "replied", "died", "disappeared",
    "vanished", "was born", "was killed", "was found", "was last seen",
    "was never seen", "was seen", "worked", "lived", "testified",
    "pleaded", "grew up",
)
_STRONG_PATIENT_RES = (
    r"(?:search|searched|searching|looking|hunt|hunting) for ",
    r"happened to ",
    r"name was ",
    r"named ",
)
_LOCATIVE_PREPS = (
    "in", "at", "near", "from", "to", "into", "across", "around",
    "throughout", "outside", "inside", "toward", "towards", "on", "onto",
)
_GEO_OF_NOUNS = ("streets", "city", "town", "outskirts", "coast", "banks", "heart")
_PLACE_NOUNS = ("city", "town", "village", "river", "mountain", "island", "lake", "county", "capital")


def _frame_votes(cluster, sentences):
    """(person_votes, place_votes, strong_person_votes) counted over
    the cluster's own mention sentences, from the sentence text."""
    person = place = strong = 0
    forms = [cluster["canonical"], *cluster["aliases"]]
    for si in cluster["sentences"]:
        text = sentences[si]
        for form in forms:
            esc = re.escape(form)
            for pattern in _STRONG_PATIENT_RES:
                if re.search(pattern + esc + r"(?![\w])", text):
                    person += 1
                    strong += 1
            verb_alt = "|".join(re.escape(v) for v in _PERSON_SUBJECT_VERBS)
            if re.search(esc + r"(?![\w])[^.!?;]{0,60}?\b(?:" + verb_alt + r")\b", text):
                person += 1
            prep_alt = "|".join(_LOCATIVE_PREPS)
            if re.search(r"\b(?:" + prep_alt + r")\s+(?:the\s+)?" + esc + r"(?![\w])", text):
                place += 1
            geo_alt = "|".join(_GEO_OF_NOUNS)
            if re.search(r"\b(?:" + geo_alt + r")\s+of\s+(?:the\s+)?" + esc + r"(?![\w])", text):
                place += 1
            noun_alt = "|".join(_PLACE_NOUNS)
            if re.search(esc + r"(?![\w])\s+(?:is|was)\s+a\s+(?:" + noun_alt + r")\b", text):
                place += 1
    return person, place, strong


def _type_cluster(cluster, sentences, kb_budget, kb_stats):
    """One cluster through the cascade. Returns (type, confidence,
    type_source, signals, description, kb_aliases).

    Order (§5.2.1, KB stage disabled): cache -> KB stage (records
    kb_disabled, no verdict while D-2 is unmade) -> lexicon ->
    frames -> honest `other`. NOTHING here infers type from a
    preposition ratio: place requires positive place evidence.
    """
    signals = []
    kb = kb_client.lookup_entity(cluster["canonical"], budget=kb_budget)
    kb_stats["lookups"] += 1
    if kb["status"] == "cache_hit":
        kb_stats["cache_hits"] += 1
    elif kb["status"] == "live":
        kb_stats["live"] += 1
    elif kb["status"] in ("unreachable", "malformed"):
        kb_stats["unreachable"] = True
    if kb["status"] == "budget_exhausted":
        kb_stats["budget_exhausted"] = True
    signals.extend(kb["signals"])
    if kb["type"]:
        return (
            kb["type"], kb["confidence"], kb["type_source"],
            signals, kb["description"], kb["aliases"],
        )

    words = cluster["words"]
    if len(words) >= 2:
        last = words[-1]
        if last in _ORG_SUFFIXES:
            return ("organization", TYPING_SIGNAL_CONFIDENCE["lexicon"],
                    "lexicon", signals + ["lexicon:org_suffix"], "", [])
        if last in _PLACE_SUFFIXES:
            return ("place", TYPING_SIGNAL_CONFIDENCE["lexicon"],
                    "lexicon", signals + ["lexicon:place_suffix"], "", [])
        if words[0] == "City" and words[1] == "of":
            return ("place", TYPING_SIGNAL_CONFIDENCE["lexicon"],
                    "lexicon", signals + ["lexicon:city_of"], "", [])
    if len(words) == 1 and re.fullmatch(r"[A-Z]{2,6}", words[0]):
        # An all-caps acronym in a documentary script is, by name
        # structure, an organisation (space agency, bureau, alliance)
        # — a shape rule about English, not a story fact. It is
        # TRUSTED only when the initialism is anchored: either the
        # script itself expands it (the multiword form is among the
        # cluster's aliases) or it is a globally-known institution
        # in the local lexicon below. An unanchored all-caps token
        # may equally be a substance, a measure or a concept, so it
        # types as an organisation at frame-level confidence,
        # untrusted.
        expanded = words[0] in _KNOWN_INSTITUTION_ACRONYMS or any(
            len(str(alias).split()) >= 2 for alias in cluster["aliases"]
        )
        if expanded:
            return ("organization", TYPING_SIGNAL_CONFIDENCE["acronym"],
                    "lexicon", signals + ["lexicon:acronym"], "", [])
        return ("organization", TYPING_SIGNAL_CONFIDENCE["acronym_unexpanded"],
                "lexicon", signals + ["lexicon:acronym_no_expansion"], "", [])

    person_votes, place_votes, strong_votes = _frame_votes(cluster, sentences)
    total = person_votes + place_votes
    if total:
        if person_votes and person_votes / total >= 2 / 3 and (
            person_votes >= 2 or strong_votes >= 1
        ):
            return ("person", TYPING_SIGNAL_CONFIDENCE["frames"],
                    "frames", signals + ["frames:person"], "", [])
        if place_votes and place_votes == total:
            return ("place", TYPING_SIGNAL_CONFIDENCE["frames"],
                    "frames", signals + ["frames:place"], "", [])

    # No decisive signal. When the KB stage was consulted and is
    # disabled, that is honestly WHY the type is unknown — the
    # label says so instead of dressing a guess up as knowledge.
    source = "kb_disabled" if "kb_disabled" in signals else "none"
    return ("other", TYPING_SIGNAL_CONFIDENCE["none"], source, signals, "", [])


def _entity_id(canonical):
    return re.sub(r"[^a-z0-9]+", "_", canonical.lower()).strip("_") or "entity"


def _modal_period(sentences):
    """Bible time_period from date density (§5.5): the modal decade
    across all extracted years; empty when the script names none."""
    decades = Counter()
    for sentence in sentences:
        for year in story.extract_years(sentence):
            try:
                decades[int(year) // 10 * 10] += 1
            except (TypeError, ValueError):
                continue
    if not decades:
        return ""
    decade = sorted(decades.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return f"{decade}s"


def build_bible(sentences, *, kb_budget=None):
    """Planner 2.0's case bible, in heuristic_bible's return shape
    (plus additive typing/provenance fields per entity)."""
    sentences = [str(s) for s in sentences]
    stats = _candidate_stats(sentences)
    clusters = _cluster_candidates(stats)
    budget = kb_budget if kb_budget is not None else kb_client.KBBudget()
    kb_stats = {
        "lookups": 0, "cache_hits": 0, "live": 0,
        "unreachable": False, "budget_exhausted": False,
        # The D-2 gate state at build time, recorded on the bible.
        "network_enabled": kb_client.network_enabled(),
    }

    entities = []
    seen_ids = set()
    for cluster in clusters[:40]:
        etype, confidence, source, signals, description, kb_aliases = (
            _type_cluster(cluster, sentences, budget, kb_stats)
        )
        aliases = list(cluster["aliases"])
        for alias in kb_aliases:
            if alias and alias not in aliases and alias != cluster["canonical"]:
                aliases.append(alias)
        eid = _entity_id(cluster["canonical"])
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        entities.append({
            "id": eid,
            "canonical": cluster["canonical"],
            "type": etype,
            "role": "",
            "aliases": aliases,
            "pronoun": None,
            "description": description,
            "type_confidence": confidence,
            "type_source": source,
            "type_signals": signals,
            "type_low_confidence": confidence < TYPE_CONFIDENCE_FLOOR,
            "recurrence": round(
                len(cluster["sentences"]) / max(1, len(sentences)), 4
            ),
            "mention_count": cluster["count"],
        })

    bible = {
        "title": "",
        "story_type": "other",
        "time_period": _modal_period(sentences),
        "primary_subject_id": None,
        "entities": entities,
        "timeline": [],
        "planner_version": PLANNER_VERSION,
        "kb": kb_stats,
    }
    if entities:
        hydrated = story.hydrate_bible(bible)
        planner = SentencePlanner(sentences, hydrated)
        primary, primary_low = planner.primary_subject()
        bible["primary_subject_id"] = primary
        bible["primary_low_confidence"] = primary_low
        order = {eid: planner.salience_final(eid) for eid in planner.by_id}
        entities.sort(key=lambda e: -order.get(e["id"], 0.0))
    return bible


# ------------------------------------------------------------------
# §5.3 Mention graph + salience
# ------------------------------------------------------------------

def _mention_spans(sentence, alias_index):
    """Longest-match, non-overlapping mentions with surface forms:
    [(start, entity_id, capitalised)] in order of appearance. The
    alias index matches case-insensitively (find_mentions
    semantics), so lowercase-recovered mentions are found too and
    marked by their surface case (§5.1 signal 4)."""
    lowered = str(sentence or "").lower()
    taken = [False] * len(lowered)
    found = []
    for alias in sorted(alias_index, key=len, reverse=True):
        for match in re.finditer(r"(?<![\w])" + re.escape(alias) + r"(?![\w])", lowered):
            a, b = match.span()
            if any(taken[a:b]):
                continue
            for i in range(a, b):
                taken[i] = True
            surface = str(sentence)[a:b]
            found.append((a, alias_index[alias], bool(surface[:1].isupper())))
    found.sort()
    return found


_PRONOUN_CLASS = {
    "he": "person", "him": "person", "his": "person",
    "she": "person", "her": "person", "hers": "person",
    "it": "nonperson", "its": "nonperson",
    "they": "any", "them": "any", "their": "any",
}


# ------------------------------------------------------------------
# §5.4 Discourse & role engine — deterministic cue classifier
# ------------------------------------------------------------------

_ACTION_VERBS = (
    "ran", "fled", "drove", "attacked", "chased", "escaped",
    "searched", "searching", "launched", "fired", "crashed",
    "questioned", "investigated", "captured", "worked", "watched",
    "walked", "walking", "marched", "stormed", "gathered", "built",
    "fought", "rescued", "survived", "returned", "headed", "heading",
    # General documentary-narration verbs (history, news, science
    # scripts alike): people and institutions doing things on
    # camera-worthy scale.
    "opened", "fell", "protested", "celebrated", "signed",
    "announced", "declared", "ordered", "stood", "waited",
    "demanded", "crossed", "entered", "arrived", "departed",
    "chanted", "waved", "tore", "climbed", "guarded", "patrolled",
    "blocked", "sealed",
)
_EVENT_NOUN_RE = re.compile(
    r"\b(launch|explod|attack|collaps|verdict|discover|crash|erupt|battle|explosion"
    r"|fall|opening|signing|speech|protest|march|ceremon|reunif|upris|declar"
    r"|announcement|arrest|escape|trial|fire|flood|storm)\w*",
    re.IGNORECASE,
)
_EVENT_VERBS = (
    "began", "erupted", "struck", "exploded", "launched", "collapsed",
    "fell", "opened", "ended", "started", "signed", "declared",
    "announced", "divided", "rose", "marched", "reunited",
    "reopened", "sealed",
)
_EVIDENCE_CUES = (
    "records show", "footage shows", "according to", "investigators found",
    "surveillance", "fingerprint", "cctv", "autopsy", "documents reveal",
    "dna",
)
_LEGAL_CUES = (
    "court", "trial", "charged", "arrested", "sentenced", "verdict",
    "jury", "pleaded", "convicted", "lawsuit",
)
_MOTIVATION_CUES = (
    "because", "in order to", "wanted to", "hoped to", "feared",
    "believed", "planned to", "so that",
)
_REFLECTION_CUES = (
    "remains", "still", "to this day", "unanswered", "no one knows",
    "decades later", "never been found", "mystery",
)
_CONCLUSION_CUES = (
    "to this day", "in the end", "never found", "remains", "legacy",
    "without leaving", "never seen again",
)
_HOOK_DRAMA = (
    "vanished", "disappeared", "mystery", "terrifying", "deadliest",
    "largest", "never seen again", "without a trace",
)
_SETUP_CUES = (
    "was a", "is a", "was an", "is an", "was born", "grew up",
    "founded in", "built in", "name was",
)
_TRANSITION_INITIALS = (
    "then", "later", "meanwhile", "however", "but", "years later",
)
_AFTERMATH_INITIALS = ("after", "following", "in the wake of")
_AFTERMATH_CUES = ("led to", "resulted in", "in response")
_BACKGROUND_BASE = 1.0
_ROLE_MARGIN = 2.0  # a role must reach this score to beat `background`
# Deterministic tie-break order (documentary salience of the signal).
_ROLE_PRIORITY = [
    "evidence", "legal", "event", "action", "hook", "setup",
    "motivation", "aftermath", "reflection", "transition", "conclusion",
]


def _cue_count(text, cues):
    return sum(1 for cue in cues if cue in text)


def classify_role(sentence, *, index, total, prev_time, event_within_3,
                  has_mention, primary_first_mention, sentence_time):
    """(role, confidence, scores) for one sentence (§5.4).

    Cue scores sum; the top role wins only if it clears the margin
    over background's base score — otherwise the sentence is
    honestly `background`, the residue role. Confidence =
    top / (top + runner-up + background base)."""
    text = str(sentence or "")
    lowered = text.lower()
    tokens = story._tokens(text)
    scores = {}

    def add(role, points):
        if points:
            scores[role] = scores.get(role, 0.0) + points

    # hook
    hook = 0.0
    interrogative = text.rstrip().endswith("?")
    if interrogative:
        hook += 2.5
    if tokens and tokens[0].lower() in _QUESTION_WORDS:
        hook += 1.5
    hook += min(2.0, 1.5 * _cue_count(lowered, _HOOK_DRAMA))
    if len(tokens) < 8 and has_mention:
        hook += 1.0
    if index == 0:
        hook *= 1.5
    add("hook", hook)
    # setup
    setup = 2.0 * (1 if any(cue in lowered for cue in _SETUP_CUES) else 0)
    if primary_first_mention:
        setup += 1.5
    if index < max(1, total // 4):
        setup += 0.5 if setup else 0.0
    add("setup", setup)
    # action
    add("action", float(min(3, _cue_count(lowered, _ACTION_VERBS))))
    # event
    event = 0.0
    if _EVENT_NOUN_RE.search(text):
        event += 2.0
    if any(verb in lowered for verb in _EVENT_VERBS):
        event += 1.5
    if sentence_time and prev_time and sentence_time != prev_time:
        event += 1.5
    add("event", event)
    # evidence
    add("evidence", 2.0 * min(2, _cue_count(lowered, _EVIDENCE_CUES)))
    # motivation
    add("motivation", 2.0 * (1 if any(cue in lowered for cue in _MOTIVATION_CUES) else 0))
    # reflection
    reflection = 1.5 * min(2, _cue_count(lowered, _REFLECTION_CUES))
    if reflection and index >= total // 2:
        reflection += 0.5
    add("reflection", reflection)
    # legal
    add("legal", 2.0 * min(2, _cue_count(lowered, _LEGAL_CUES)))
    # aftermath
    aftermath = 0.0
    first = lowered.lstrip()
    first_token = tokens[0].lower() if tokens else ""
    if first_token in _AFTERMATH_INITIALS or first.startswith("in the wake of"):
        aftermath += 1.5
    if any(cue in lowered for cue in _AFTERMATH_CUES):
        aftermath += 1.5
    if aftermath and event_within_3:
        aftermath += 1.5
    add("aftermath", aftermath)
    # transition (sentence-initial markers only)
    transition = 0.0
    if first_token in _TRANSITION_INITIALS or first.startswith("years later"):
        transition += 2.0
    if sentence_time and prev_time:
        try:
            if abs(int(sentence_time[:4]) - int(prev_time[:4])) >= 5:
                transition += 1.5
        except (TypeError, ValueError):
            pass
    add("transition", transition)
    # conclusion (a question is not a conclusion: interrogative
    # sentences earn no closing-position boost)
    conclusion = 1.5 * min(2, _cue_count(lowered, _CONCLUSION_CUES))
    if conclusion and index >= total - 2 and not interrogative:
        conclusion += 1.0
    add("conclusion", conclusion)

    ranked = sorted(
        scores.items(),
        key=lambda kv: (-kv[1], _ROLE_PRIORITY.index(kv[0])
                        if kv[0] in _ROLE_PRIORITY else 99),
    )
    if not ranked or ranked[0][1] < _ROLE_MARGIN:
        confidence = _BACKGROUND_BASE / (
            _BACKGROUND_BASE + (ranked[0][1] if ranked else 0.0) + _BACKGROUND_BASE
        )
        return "background", round(confidence, 4), scores
    top_role, top_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    confidence = top_score / (top_score + max(runner_up, _BACKGROUND_BASE) + _BACKGROUND_BASE)
    return top_role, round(confidence, 4), scores


# ------------------------------------------------------------------
# §5.5 Place & era helpers
# ------------------------------------------------------------------

_DECADE_RE = re.compile(r"\b(1[5-9]\d\d|20\d\d)s\b")


def _sentence_time(sentence):
    """The sentence's date anchor: first explicit year, else a
    decade mention, else None."""
    years = story.extract_years(sentence)
    if years:
        return years[0]
    match = _DECADE_RE.search(str(sentence or ""))
    return match.group(0) if match else None


# ------------------------------------------------------------------
# The sentence planner (mention graph, subjects, factory)
# ------------------------------------------------------------------

class SentencePlanner:
    """Planner 2.0's per-sentence raw-plan factory, over ONE bible.

    Works against any bible in the shared contract shape — its own
    Planner 2.0 bible (full degraded mode) or an LLM bible whose
    sentence batches failed (partial_llm): entities without a
    recorded type_confidence (LLM bibles) are treated as trusted.
    Raw plans are emitted in LLM shape and validated by the
    unchanged normalize_plan, exactly like the LLM's own output.
    """

    def __init__(self, sentences, bible):
        self.sentences = [str(s) for s in sentences]
        self.bible = bible
        entities = bible.get("entities") or []
        self.by_id = bible.get("by_id") or {e["id"]: e for e in entities}
        self.alias_index = (
            bible.get("alias_index") or story.build_alias_index(list(self.by_id.values()))
        )
        self.spans = [_mention_spans(s, self.alias_index) for s in self.sentences]
        self.mention_ids = []
        for spans in self.spans:
            ordered = []
            for _start, eid, _cap in spans:
                if eid not in ordered:
                    ordered.append(eid)
            self.mention_ids.append(ordered)
        self.times = [_sentence_time(s) for s in self.sentences]
        self.places = [self._dominant_place(i) for i in range(len(self.sentences))]
        # Narrative continuity: a documentary narrates in sequence,
        # so a sentence that restates no date or place is understood
        # at the story's CURRENT time and place (the LLM plans do
        # exactly this — their time/place fields are story-relative
        # and almost never empty). The plan-level fields therefore
        # carry forward; scenario construction keeps using the
        # sentence's own mentions.
        self.effective_times = []
        last_time = None
        for value in self.times:
            if value:
                last_time = value
            self.effective_times.append(last_time)
        self.effective_places = []
        last_place = None
        for value in self.places:
            if value:
                last_place = value
            self.effective_places.append(last_place)
        # The bible's modal period is the era of last resort for
        # scenarios (§5.5: sentence date, else the bible period).
        self.period_era = story._year_or_decade(bible.get("time_period")) or None
        self._roles = [None] * len(self.sentences)
        self._last_explicit = {}  # pronoun class -> (entity_id, sentence_index)
        self._subject_by_index = {}

    # -- trust / salience ------------------------------------------

    def _trusted(self, eid):
        confidence = (self.by_id.get(eid) or {}).get("type_confidence")
        return confidence is None or confidence >= TYPE_CONFIDENCE_FLOOR

    def _recurrence(self, eid):
        hits = sum(1 for ids in self.mention_ids if eid in ids)
        return hits / max(1, len(self.sentences))

    def salience_at(self, eid, upto):
        weights = SALIENCE_WEIGHTS
        score = 0.0
        for j in range(upto + 1):
            spans = [s for s in self.spans[j] if s[1] == eid]
            if not spans:
                continue
            score += weights["mention"] * len(spans) * (
                weights["mention_decay"] ** (upto - j)
            )
            if self.spans[j] and self.spans[j][0][1] == eid:
                score += weights["first_in_sentence"]
        if any(eid in self.mention_ids[j] for j in (0, 1) if j < len(self.sentences)):
            score += weights["early_mention"]
        score += weights["recurrence"] * self._recurrence(eid) * 10
        etype = (self.by_id.get(eid) or {}).get("type")
        score += weights["type_bonus"].get(etype, 0.0)
        return score

    def salience_final(self, eid):
        if not self.sentences:
            return 0.0
        return self.salience_at(eid, len(self.sentences) - 1)

    #: §5.3 calibration: a trusted type earns this bounded bonus in
    #: the primary-subject score — trust breaks near-ties, it does
    #: not override the story's actual centre of gravity.
    _PRIMARY_TRUST_BONUS = 3.0
    #: §5.3 calibration: a person whose score reaches this share of
    #: the top score is the story's lead (documentaries are about
    #: people when a person is anywhere near the centre); a person
    #: far below it is a minor character and does not re-lead a
    #: story centred elsewhere.
    _PRIMARY_PERSON_COMPETITIVE = 0.6

    def primary_subject(self):
        """(entity_id, low_confidence) — the primary subject is the
        highest-scoring entity (salience + a bounded trust bonus;
        salience itself already carries the §5.3 person bonus),
        except that a competitively-scored person leads, per the
        constant above. DEVIATION from the plan's §5.3 cascade
        wording, recorded in the Phase 7 proposal: the cascade
        (trusted person → any person → trusted entity → any
        entity) cannot satisfy the plan's own regression cases at
        once — a story whose lead is an untrusted high-salience
        person must beat a trusted low-salience organisation
        mentioned once, a story centred on a high-salience object
        must not be re-led by a minor person, and a trusted
        mid-salience organisation must beat an untrusted place of
        similar salience. The scored rule satisfies all three
        shapes with one mechanism; the winner is flagged
        low-confidence whenever its type is untrusted, exactly as
        the cascade flagged its fallbacks."""
        if not self.by_id:
            return None, False

        def score(eid):
            bonus = self._PRIMARY_TRUST_BONUS if self._trusted(eid) else 0.0
            return self.salience_final(eid) + bonus

        scores = {eid: score(eid) for eid in self.by_id}
        top_score = max(scores.values())
        competitive_persons = [
            eid for eid in sorted(self.by_id)
            if (self.by_id[eid].get("type") == "person"
                and scores[eid] >= self._PRIMARY_PERSON_COMPETITIVE * top_score)
        ]
        if competitive_persons:
            winner = max(competitive_persons, key=lambda eid: scores[eid])
        else:
            winner = max(sorted(self.by_id), key=lambda eid: scores[eid])
        return winner, not self._trusted(winner)

    # -- places ------------------------------------------------------

    def _dominant_place(self, index):
        for eid in self.mention_ids[index]:
            entity = self.by_id.get(eid) or {}
            if entity.get("type") == "place":
                return entity.get("canonical")
        return None

    # -- roles -------------------------------------------------------

    def role_at(self, index):
        """(role, confidence) — computed deterministically from the
        script start, so call order cannot change the answer."""
        if self._roles[index] is not None:
            return self._roles[index]
        primary = self.bible.get("primary_subject_id")
        for i in range(index + 1):
            if self._roles[i] is not None:
                continue
            prev_time = self.times[i - 1] if i > 0 else None
            event_within_3 = any(
                self._roles[j] and self._roles[j][0] == "event"
                for j in range(max(0, i - 3), i)
            )
            self._roles[i] = classify_role(
                self.sentences[i],
                index=i,
                total=len(self.sentences),
                prev_time=prev_time,
                event_within_3=event_within_3,
                has_mention=bool(self.mention_ids[i]),
                primary_first_mention=(
                    primary is not None
                    and primary in self.mention_ids[i]
                    and all(
                        primary not in self.mention_ids[j] for j in range(i)
                    )
                ),
                sentence_time=self.times[i],
            )[:2]
        return self._roles[index]

    # -- subjects ----------------------------------------------------

    def _choose_subject(self, index, carry_subject):
        mentions = self.mention_ids[index]
        if mentions:
            salience = {eid: self.salience_at(eid, index) for eid in mentions}
            ranked = sorted(mentions, key=lambda eid: -salience[eid])
            top = ranked[0]
            if len(ranked) == 1:
                confidence = 0.85 if self._trusted(top) else 0.55
                return top, confidence, True
            margin = salience[top] - salience[ranked[1]]
            first = mentions[0]
            if first != top and salience[first] >= salience[top] - _SUBJECT_MARGIN:
                # A sentence that introduces a new actor is about
                # that actor, not about the story's lead (§5.3.2).
                return first, 0.6, True
            if margin >= _SUBJECT_MARGIN:
                return top, round(min(0.95, 0.6 + margin / 10.0), 4), True
            return top, 0.55, True

        # Pronoun carry with decay (§5.3.3).
        tokens = [t.lower() for t in story._tokens(self.sentences[index])]
        pronoun = next((t for t in tokens if t in _PRONOUN_CLASS), None)
        if pronoun:
            klass = _PRONOUN_CLASS[pronoun]
            candidate = self._last_explicit.get(klass)
            if candidate is None and klass != "any":
                candidate = self._last_explicit.get("any")
            if candidate is not None:
                eid, seen_at = candidate
                confidence = _PRONOUN_CARRY_BASE * (
                    _PRONOUN_CARRY_DECAY ** (index - seen_at)
                )
                if confidence >= _PRONOUN_CARRY_FLOOR:
                    return eid, round(confidence, 4), False
        return None, 0.0, False

    def _register_explicit(self, index, eid):
        etype = (self.by_id.get(eid) or {}).get("type")
        classes = ["any", "person" if etype == "person" else "nonperson"]
        for klass in classes:
            self._last_explicit[klass] = (eid, index)

    # -- scenario factory (§5.6, specificity per today's rule) -------

    def _specificity_basis(self, subject_id, place, era, document_cue):
        """The D-1 corroboration computation, RECORDED ONLY — it
        never drives the specificity stamp while D-1 is unmade."""
        subject = self.by_id.get(subject_id) if subject_id else None
        if subject is None:
            return "widened_low_confidence"
        confidence = subject.get("type_confidence")
        if confidence is not None and confidence < TYPE_CONFIDENCE_FLOOR:
            return "widened_low_confidence"
        if place:
            return "corroborated_place"
        if era:
            return "corroborated_era"
        if document_cue:
            return "document_cue"
        return "uncorroborated"

    def _scenarios(self, index, subject_id, role):
        sentence = self.sentences[index]
        subject = self.by_id.get(subject_id) if subject_id else None
        place = self.places[index]
        place_id = next(
            (eid for eid in self.mention_ids[index]
             if (self.by_id.get(eid) or {}).get("type") == "place"),
            None,
        )
        era = story._year_or_decade(self.times[index]) or self.period_era
        scenarios = []
        basis = []
        # The sentence's event noun, when it names one, rides the
        # subject scenario's event field — compose_queries builds
        # its strongest queries from anchor + event + era.
        event_match = _EVENT_NOUN_RE.search(sentence)
        sentence_event = event_match.group(0) if event_match else None

        def terms_for(*parts):
            terms = []
            for part in parts:
                for term in (part if isinstance(part, (list, tuple)) else [part]):
                    if term and str(term).strip() and str(term) not in terms:
                        terms.append(str(term))
            return terms[:4]

        # S1 — the subject scenario.
        if subject is not None:
            canonical = subject["canonical"]
            kind = {
                "person": "person_activity",
                "place": "place",
                "organization": "person_activity",
                "event": "event",
                "object": "object",
            }.get(subject.get("type"), "archival")
            if subject.get("type") == "person":
                visual = f"archival photographs of {canonical}"
                if place and place != canonical:
                    visual += f" in {place}"
            elif subject.get("type") == "place":
                visual = canonical + (f" in the {era}" if era else "")
            elif subject.get("type") == "organization":
                visual = f"{canonical} personnel and facilities"
            elif subject.get("type") == "event":
                visual = f"archival footage: {canonical}"
            elif subject.get("type") == "object":
                visual = f"close view of {canonical}"
            else:
                visual = f"archival material about {canonical}"
            scenarios.append({
                "visual": visual,
                "kind": kind,
                "subject_id": subject_id,
                # TODAY'S degraded rule (D-1 unmade): anchored on a
                # resolved bible entity -> exact.
                "specificity": "exact",
                # Era is deliberately NOT an S1 term: the era
                # rides the scenario's era field (compose_queries
                # consumes it) and earns its own S3 scenario below.
                "search_terms": terms_for(
                    canonical, subject.get("aliases") or [],
                    place if place != canonical else None,
                ),
                "event": sentence_event,
                "place": place,
                "era": era,
            })
            basis.append(self._specificity_basis(subject_id, place, era, False))

        # S2 — place-establishing (new dominant place, or sentence 0).
        if (
            place and place_id and place_id != subject_id
            and (index == 0 or self.places[index - 1] != place)
        ):
            scenarios.append({
                "visual": f"{place} — establishing view" + (f", {era}" if era else ""),
                "kind": "place",
                "subject_id": place_id,
                "specificity": "exact",
                "search_terms": terms_for(place, era),
                "event": None,
                "place": place,
                "era": era,
            })
            basis.append(self._specificity_basis(place_id, place, era, False))

        # S3 — era/context (when no earlier scenario carries the era).
        if era and not any(
            era in (sc.get("search_terms") or []) for sc in scenarios
        ):
            if subject is not None:
                scenarios.append({
                    "visual": f"archival material from the {era}",
                    "kind": "archival",
                    "subject_id": subject_id,
                    "specificity": "exact",
                    "search_terms": terms_for(subject["canonical"], era),
                    "event": None,
                    "place": place,
                    "era": era,
                })
                basis.append(self._specificity_basis(subject_id, place, era, False))
            elif place_id:
                scenarios.append({
                    "visual": f"archival material from the {era}",
                    "kind": "archival",
                    "subject_id": place_id,
                    "specificity": "exact",
                    "search_terms": terms_for(place, era),
                    "event": None,
                    "place": place,
                    "era": era,
                })
                basis.append(self._specificity_basis(place_id, place, era, False))

        # S4 — detail/object cues: statistic, document lexis, or a
        # named object entity that is not the subject.
        document_cue = bool(story._DOCUMENT_RE.search(sentence))
        stat = re.search(
            r"\b\d[\d,.]*\s?(?:percent|%|million|billion|people|dollars|"
            r"miles|kilometres|kilometers|feet|metres|meters)\b",
            sentence, re.IGNORECASE,
        )
        object_id = next(
            (eid for eid in self.mention_ids[index]
             if eid != subject_id
             and (self.by_id.get(eid) or {}).get("type") == "object"),
            None,
        )
        if len(scenarios) < 4 and (document_cue or stat or object_id):
            if object_id:
                anchor = self.by_id[object_id]
                scenarios.append({
                    "visual": f"close view of {anchor['canonical']}",
                    "kind": "object",
                    "subject_id": object_id,
                    "specificity": "exact",
                    "search_terms": terms_for(
                        anchor["canonical"],
                        subject["canonical"] if subject else None,
                    ),
                    "event": None,
                    "place": place,
                    "era": era,
                })
                basis.append(self._specificity_basis(object_id, place, era, document_cue))
            else:
                anchor_name = subject["canonical"] if subject else place
                scenarios.append({
                    "visual": (
                        f"documents and records: {anchor_name}"
                        if anchor_name else "documents and records"
                    ),
                    "kind": "document",
                    "subject_id": subject_id,
                    "specificity": "exact" if subject_id else "generic",
                    "search_terms": terms_for(
                        anchor_name, stat.group(0) if stat else None, era,
                    ),
                    "event": None,
                    "place": place,
                    "era": era,
                })
                basis.append(self._specificity_basis(subject_id, place, era, True))

        # S5 — secondary entity: another bible entity the sentence
        # names (not the subject, not the S2 place) is its own
        # visual need, anchored on that entity.
        if len(scenarios) < 4:
            used_ids = {sc.get("subject_id") for sc in scenarios}
            secondary_id = next(
                (eid for eid in self.mention_ids[index]
                 if eid not in used_ids and eid != subject_id),
                None,
            )
            if secondary_id:
                anchor = self.by_id[secondary_id]
                kind = {
                    "person": "person_activity",
                    "place": "place",
                    "organization": "person_activity",
                    "event": "event",
                    "object": "object",
                }.get(anchor.get("type"), "archival")
                scenarios.append({
                    "visual": f"archival material about {anchor['canonical']}",
                    "kind": kind,
                    "subject_id": secondary_id,
                    "specificity": "exact",
                    "search_terms": terms_for(
                        anchor["canonical"], anchor.get("aliases") or [], era,
                    ),
                    "event": None,
                    "place": place,
                    "era": era,
                })
                basis.append(
                    self._specificity_basis(secondary_id, place, era, False)
                )

        # No subject, no place, no cue: one honest generic scenario
        # WITH terms (the old synthesis carried none). An era alone
        # still earns an archival scenario anchored on the period.
        if not scenarios:
            concepts = story.derive_fallback_concepts(
                sentence, place=place, era=era,
                entities=[self.by_id[e] for e in self.mention_ids[index]
                          if e in self.by_id],
            )
            terms = terms_for(
                concepts.get("activities") or [],
                concepts.get("settings") or [],
                place,
            ) or ["documentary b-roll"]
            if era:
                scenarios.append({
                    "visual": f"archival material from the {era}",
                    "kind": "archival",
                    "subject_id": None,
                    "specificity": "generic",
                    "search_terms": terms_for(era, terms),
                    "event": None,
                    "place": place,
                    "era": era,
                })
            else:
                scenarios.append({
                    "visual": "neutral documentary b-roll",
                    "kind": "abstract_broll",
                    "subject_id": None,
                    "specificity": "generic",
                    "search_terms": terms,
                    "event": None,
                    "place": place,
                    "era": era,
                })
            basis.append("widened_low_confidence")
        # Terms guarantee (§5.6 acceptance): EVERY factory scenario
        # carries at least one real search term — a scenario with a
        # visual but no terms is a retrieval dead end.
        for scenario in scenarios:
            if not scenario.get("search_terms"):
                anchor = (
                    self.by_id.get(scenario.get("subject_id") or "") or {}
                ).get("canonical")
                scenario["search_terms"] = terms_for(
                    anchor, place, era, "archival records"
                ) or ["archival records"]
        return scenarios[:4], basis[:4]

    # -- raw plan assembly -------------------------------------------

    def raw_plan_for(self, index, carry_subject=None):
        """One sentence's raw plan in LLM shape, for the unchanged
        normalize_plan. Private `_planner2` keys carry the §5.8
        per-sentence stamps; analyze_sentences reads them onto the
        normalized plan and they never reach the validator."""
        subject_id, subject_confidence, explicit = self._choose_subject(
            index, carry_subject
        )
        if explicit and subject_id:
            self._register_explicit(index, subject_id)
        self._subject_by_index[index] = subject_id

        role, role_confidence = self.role_at(index)
        try:
            from services.media_pipeline import NO_SEARCH_ROLES
        except Exception:
            NO_SEARCH_ROLES = {
                "transition", "reflection", "hook", "conclusion", "motivation",
            }
        if role in NO_SEARCH_ROLES and role_confidence < 0.5:
            # §10 guard: a weakly-cued no-search role must not cost
            # the sentence its retrieval.
            role = "background"

        scenarios, basis = self._scenarios(index, subject_id, role)
        place = self.effective_places[index]
        era = story._year_or_decade(self.times[index]) or self.period_era
        concepts = story.derive_fallback_concepts(
            self.sentences[index], place=place, era=era,
            entities=[self.by_id[e] for e in self.mention_ids[index]
                      if e in self.by_id],
        )
        events = []
        for match in _EVENT_NOUN_RE.finditer(self.sentences[index]):
            if match.group(0) not in events:
                events.append(match.group(0))
        return {
            "subject_id": subject_id,
            "role": role,
            "meaning": "",
            "events": events[:3],
            "place": place,
            "time": self.effective_times[index],
            "scenarios": scenarios,
            "fallback_concepts": concepts,
            "_planner2": True,
            "_subject_confidence": subject_confidence,
            "_role_confidence": role_confidence,
            "_specificity_basis": basis,
        }
