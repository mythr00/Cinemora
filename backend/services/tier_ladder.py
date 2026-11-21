"""Tier ladder for documentary B-roll (Intelligent B-Roll System,
plan §3/§5; Master Directive Phase 2).

The ladder is the escalation discipline the coverage pass uses when
a beat's own verified pool cannot cover its sentences: T1 direct
(today's search) -> T2 related-specific (the story's real places /
objects) -> T3 activity B-roll (the sentence's activity, performed by
anyone plausible) -> T4 atmospheric (place-type + era + mood) -> T5
generated graphics (the existing motion-graphics route, fed by
missing_visuals). This module owns the ladder's pure machinery:

- the tier order and per-role entry tiers (D-B3: unsearched-role
  sentences enter at T3, never T1/T2);
- the D-B2 budget constants and the per-production escalation
  ledger (searches / downloads / wall-clock soft guard);
- derived-scenario construction per tier, from a shot need's
  fallback concepts + visual intentions and the case bible —
  phrases the plan itself carries, never invented entities;
- genre-pack B-roll vocabulary resolution (plan §4.3), fail-closed
  to the general pack like every other pack field.

Searching, verification and drawing live in media_pipeline, which
runs derived scenarios through the ordinary build_shot_needs ->
compose_queries -> search -> verify path. Nothing here touches a
threshold, a reuse cap or the coverage gate.
"""

from __future__ import annotations

import time

from services import genre_packs

# ---------------------------------------------------------------------------
# Tier order + entry tiers
# ---------------------------------------------------------------------------

#: The searchable ladder, in escalation order. ``graphic`` (T5) is
#: not a search tier — it is reached through missing_visuals — so it
#: is not in this sequence; it IS in the tier stamp vocabulary
#: (rejection_diagnostics.TIERS).
TIER_ORDER = ("direct", "related", "activity_broll", "atmospheric")

#: Mirror of media_pipeline.NO_SEARCH_ROLES (kept there as the
#: pipeline's own constant; duplicated here because media_pipeline
#: imports this module, not the reverse). D-B3: sentences in these
#: roles enter the ladder at T3 — the pipeline has already decided,
#: by role semantics, not to seek direct evidence for them.
NO_SEARCH_ROLES = frozenset(
    {"transition", "reflection", "hook", "conclusion", "motivation"}
)


def tiers_after(tier):
    """The ladder tiers strictly below ``tier``, in order. An
    unknown tier label escalates from the top (defensive: a need
    stamped with a foreign label is treated as a T1 need)."""
    label = str(tier or "direct").strip().lower()
    if label not in TIER_ORDER:
        label = "direct"
    return TIER_ORDER[TIER_ORDER.index(label) + 1:]


def entry_tier_for_role(role):
    """Where a sentence with this role enters the ladder (D-B3)."""
    if str(role or "").strip().lower() in NO_SEARCH_ROLES:
        return "activity_broll"
    return "direct"


# ---------------------------------------------------------------------------
# Budgets (decision D-B2, founder-approved 2026-11-21)
# ---------------------------------------------------------------------------

#: Per escalating need: how many composed queries each tier may
#: spend, and how many download attempts each tier may make.
TIER_QUERY_BUDGETS = {"related": 2, "activity_broll": 3, "atmospheric": 2}
TIER_DOWNLOAD_ATTEMPTS = 2

#: Per production: the ladder's total additional cost.
PRODUCTION_MAX_EXTRA_SEARCHES = 40
PRODUCTION_MAX_EXTRA_DOWNLOADS = 20

#: Soft wall-clock guard: once escalation has consumed more than
#: this fraction of the asset stage's elapsed time, remaining needs
#: stop escalating (recorded, never silent).
WALL_CLOCK_FRACTION = 0.25

#: The wall-clock guard is a SOFT guard against late-stage
#: escalation spirals; it cannot meaningfully trip before the
#: stage has run this long (in the opening seconds the escalation
#: share is arithmetically unstable, and the hard search/download
#: budgets above are the binding cost controls there).
WALL_CLOCK_MIN_STAGE_SECONDS = 60.0

# Stop causes recorded on tier attempts (the family name is
# rejection_diagnostics.TIER_BUDGET_EXHAUSTED).
STOP_TIER_QUERY_BUDGET = "tier_query_budget"
STOP_PRODUCTION_SEARCH_BUDGET = "production_search_budget"
STOP_PRODUCTION_DOWNLOAD_BUDGET = "production_download_budget"
STOP_TIER_DOWNLOAD_BUDGET = "tier_download_budget"
STOP_WALL_CLOCK = "wall_clock"


class EscalationLedger:
    """Per-production escalation accounts (D-B2).

    One ledger per production: it hangs off the production's
    VisualRegistry instance (see ledger_for), whose lifetime is the
    production's asset stage — jobs resets the registry per
    production, so the accounts reset with it. The clock is
    injectable for tests.

    Wall-clock guard (soft): the time actually SPENT inside
    escalation (accumulated between begin_escalation /
    end_escalation, including the currently open interval) may not
    exceed WALL_CLOCK_FRACTION of the asset stage's elapsed time.
    The stage's start is the registry's creation time — the
    registry is reset when the asset stage begins — passed in as
    ``stage_started_at``; a ledger without one anchors to its own
    creation and the guard then measures from there."""

    def __init__(self, clock=None, stage_started_at=None):
        self._clock = clock or time.monotonic
        self.asset_stage_started_at = (
            stage_started_at
            if stage_started_at is not None
            else self._clock()
        )
        self.escalation_seconds = 0.0
        self._active_started = None
        self.searches = 0
        self.downloads = 0

    # -- wall clock -------------------------------------------------
    def begin_escalation(self):
        if self._active_started is None:
            self._active_started = self._clock()

    def end_escalation(self):
        if self._active_started is not None:
            self.escalation_seconds += max(
                self._clock() - self._active_started, 0.0
            )
            self._active_started = None

    def _escalation_time(self):
        total = self.escalation_seconds
        if self._active_started is not None:
            total += max(self._clock() - self._active_started, 0.0)
        return total

    def wall_clock_exceeded(self):
        active = self._escalation_time()
        if active <= 0:
            return False
        stage_elapsed = max(
            self._clock() - self.asset_stage_started_at, 0.0
        )
        if stage_elapsed < WALL_CLOCK_MIN_STAGE_SECONDS:
            return False
        return active > WALL_CLOCK_FRACTION * stage_elapsed

    # -- searches ---------------------------------------------------
    def can_spend_searches(self, count=1):
        return (
            self.searches + int(count) <= PRODUCTION_MAX_EXTRA_SEARCHES
            and not self.wall_clock_exceeded()
        )

    def spend_searches(self, count=1):
        self.searches += int(count)

    # -- downloads --------------------------------------------------
    def can_spend_download(self):
        return (
            self.downloads < PRODUCTION_MAX_EXTRA_DOWNLOADS
            and not self.wall_clock_exceeded()
        )

    def spend_download(self):
        self.downloads += 1

    def summary(self):
        return {
            "extra_searches": self.searches,
            "extra_downloads": self.downloads,
            "search_budget": PRODUCTION_MAX_EXTRA_SEARCHES,
            "download_budget": PRODUCTION_MAX_EXTRA_DOWNLOADS,
        }


def ledger_for(registry):
    """The production's escalation ledger, created lazily on the
    registry instance, its wall-clock anchored to the registry's
    creation (the asset stage's start). A missing registry yields
    a throwaway ledger (budgets still enforced within the call)."""
    if registry is None:
        return EscalationLedger()
    ledger = getattr(registry, "_tier_escalation_ledger", None)
    if ledger is None:
        ledger = EscalationLedger(
            stage_started_at=getattr(registry, "created_at", None)
        )
        try:
            registry._tier_escalation_ledger = ledger
        except Exception:
            pass
    return ledger


# ---------------------------------------------------------------------------
# Genre-pack B-roll vocabulary (plan §4.3)
# ---------------------------------------------------------------------------

def broll_vocabulary_for(bible):
    """(pack_id, vocabulary) for a production's case bible.

    The bible's story_type names the pack when it is one of the
    pack ids; everything else resolves to the general pack — the
    genre_packs fail-closed rule. A pack without a vocabulary (or
    any load failure) yields an empty dict: the ladder then runs on
    sentence-derived concepts alone, recorded via the derived
    need's vocabulary_pack field."""
    pack_id = "general"
    story_type = str((bible or {}).get("story_type") or "").strip().lower()
    if story_type in genre_packs.PACK_IDS:
        pack_id = story_type
    try:
        pack = genre_packs.load_pack(pack_id)
        resolved = genre_packs.resolve_visual_profile(pack) if pack else {}
        profile = (
            resolved.get("profile")
            if isinstance(resolved, dict)
            else None
        )
        vocabulary = (
            profile.get("broll_vocabulary")
            if isinstance(profile, dict)
            else None
        )
    except Exception:
        vocabulary = None
    return pack_id, vocabulary if isinstance(vocabulary, dict) else {}


# ---------------------------------------------------------------------------
# Derived-scenario construction (plan §5.2)
# ---------------------------------------------------------------------------

def _bible_entities(bible):
    entities = []
    by_id = (bible or {}).get("by_id") or {}
    if isinstance(by_id, dict) and by_id:
        entities.extend(
            e for e in by_id.values() if isinstance(e, dict)
        )
    for entity in (bible or {}).get("entities") or []:
        if isinstance(entity, dict) and entity not in entities:
            entities.append(entity)
    return entities


def _match_entity(bible, phrase, types=None):
    """The bible entity whose canonical (or an alias) equals the
    phrase, case-insensitively — else None. Exact matching only:
    a concept phrase earns an exact T2 anchor by BEING a bible
    entity, never by resembling one."""
    target = str(phrase or "").strip().lower()
    if not target:
        return None
    for entity in _bible_entities(bible):
        if types and str(entity.get("type") or "").lower() not in types:
            continue
        names = [entity.get("canonical")] + list(entity.get("aliases") or [])
        if any(str(n or "").strip().lower() == target for n in names):
            return entity
    return None


def _scenario(**fields):
    base = {
        "visual": "",
        "kind": "archival",
        "subject_id": None,
        "subject": "",
        "required_type": "",
        "specificity": "representative",
        "search_terms": [],
        "event": None,
        "place": "",
        "era": "",
        "format_intent": "any",
        "format_intent_confidence": 0.0,
        "entities": [],
        "locations": [],
        "objects": [],
        "actions": [],
    }
    base.update(fields)
    return base


def _concepts_of(need):
    concepts = (need or {}).get("fallback_concepts")
    return concepts if isinstance(concepts, dict) else {}


def _derived_related(need, bible):
    """T2 — related-specific: the story's real places and objects.

    Priority (plan §5.2): concept objects that ARE bible entities
    (exact anchors), then the need's named place (exact when it is
    a bible place), then remaining concept objects as
    representative detail shots. At most two scenarios — the tier's
    query budget is two, so a third could never be searched."""
    concepts = _concepts_of(need)
    objects = [
        str(o).strip() for o in concepts.get("objects") or [] if str(o).strip()
    ]
    place = str((need or {}).get("place") or "").strip()
    era = str((need or {}).get("era") or "").strip()
    scenarios = []
    used_phrases = set()

    def _push(scenario, phrase):
        scenarios.append(scenario)
        used_phrases.add(str(phrase or "").strip().lower())

    for phrase in objects:
        if len(scenarios) >= 2:
            break
        entity = _match_entity(bible, phrase, types={"object", "place"})
        if entity is None:
            continue
        canonical = str(entity.get("canonical") or phrase)
        kind = "place" if str(entity.get("type")) == "place" else "object"
        _push(
            _scenario(
                visual=f"{canonical}",
                kind=kind,
                subject_id=entity.get("id"),
                subject=canonical,
                required_type=(
                    "location" if kind == "place" else "object"
                ),
                specificity="exact",
                search_terms=[canonical],
                place=canonical if kind == "place" else place,
                era=era,
                locations=[canonical] if kind == "place" else [],
                objects=[canonical] if kind == "object" else [],
            ),
            phrase,
        )
    if place and len(scenarios) < 2 and place.lower() not in used_phrases:
        entity = _match_entity(bible, place, types={"place"})
        _push(
            _scenario(
                visual=place,
                kind="place",
                subject_id=entity.get("id") if entity else None,
                subject=place,
                required_type="location",
                specificity="exact" if entity else "representative",
                search_terms=[place],
                place=place,
                era=era,
                locations=[place],
            ),
            place,
        )
    for phrase in objects:
        if len(scenarios) >= 2:
            break
        if phrase.lower() in used_phrases:
            continue
        _push(
            _scenario(
                visual=phrase,
                kind="object",
                subject=phrase,
                required_type="object",
                specificity="representative",
                search_terms=[phrase],
                place=place,
                era=era,
                objects=[phrase],
            ),
            phrase,
        )
    return scenarios


def _derived_activity(need, bible, vocabulary):
    """T3 — activity B-roll: the sentence's activity, performed by
    anyone plausible. The lead scenario carries the activity
    phrases as search terms, the parent need's place/era inherited
    (the contradiction vetoes and era scoring still apply), no
    required subject, representative specificity — judged by the
    contextual type-presence floor, never as evidence of the
    actual subject.

    When the sentence's own activities are thin (<2), the genre
    pack's activity vocabulary adds a second scenario (plan §4.3)
    — pack phrases never anchor anything; they only widen the
    activity search."""
    concepts = _concepts_of(need)
    activities = [
        str(a).strip()
        for a in concepts.get("activities") or []
        if str(a).strip()
    ]
    if not activities:
        return []
    settings = [
        str(s).strip()
        for s in concepts.get("settings") or []
        if str(s).strip()
    ]
    terms = list(activities[:2])
    if activities and settings:
        combined = f"{activities[0]} {settings[0]}"
        if combined not in terms:
            terms.append(combined)
    place = str((need or {}).get("place") or "").strip()
    era = str((need or {}).get("era") or "").strip()
    scenarios = [
        _scenario(
            visual=activities[0],
            kind="person_activity",
            subject="",
            required_type="person",
            specificity="representative",
            search_terms=terms[:4],
            place=place,
            era=era,
            actions=list(activities),
        )
    ]
    if len(activities) < 2 and vocabulary:
        # Pack top-up phrases form their OWN scenario: the
        # verifier's topical overlap divides by a scenario's whole
        # term set, so folding pack phrases into the sentence's
        # scenario would dilute the score of every candidate the
        # tier returns. A separate scenario keeps each term set
        # tight and each verdict explainable.
        extra = []
        for phrase in vocabulary.get("activities") or []:
            phrase = str(phrase).strip()
            if phrase and phrase not in terms:
                extra.append(phrase)
            if len(extra) >= 2:
                break
        if extra:
            topup = _scenario(
                visual=extra[0],
                kind="person_activity",
                subject="",
                required_type="person",
                specificity="representative",
                search_terms=extra,
                place=place,
                era=era,
                actions=list(extra),
            )
            topup["vocabulary_topup"] = True
            scenarios.append(topup)
    return scenarios


def _derived_atmospheric(need, bible, vocabulary):
    """T4 — atmospheric / setting: place-type + era + mood, no
    specific claim. Generic specificity, upgraded to representative
    only when the parent need names a real place (a region anchors
    it). The pack vocabulary may supply place-types when the
    sentence's own concepts carry none."""
    concepts = _concepts_of(need)
    place_types = [
        str(p).strip()
        for p in concepts.get("place_types") or []
        if str(p).strip()
    ]
    settings = [
        str(s).strip()
        for s in concepts.get("settings") or []
        if str(s).strip()
    ]
    era_terms = [
        str(t).strip()
        for t in concepts.get("era_terms") or []
        if str(t).strip()
    ]
    mood = [
        str(m).strip() for m in concepts.get("mood") or [] if str(m).strip()
    ]
    topup = False
    if not place_types and vocabulary:
        for phrase in vocabulary.get("place_types") or []:
            phrase = str(phrase).strip()
            if phrase and phrase not in place_types:
                place_types.append(phrase)
                topup = True
            if len(place_types) >= 2:
                break
    terms = []
    for phrase in place_types[:2] + settings[:2] + era_terms[:1] + mood[:1]:
        if phrase not in terms:
            terms.append(phrase)
    if not terms:
        return []
    place = str((need or {}).get("place") or "").strip()
    scenario = _scenario(
        visual=" ".join(terms),
        kind="place" if (place_types or settings) else "abstract_broll",
        subject="",
        required_type="location" if (place_types or settings) else "",
        specificity="representative" if place else "generic",
        search_terms=terms[:4],
        place=place,
        era=str((need or {}).get("era") or "").strip()
        or (era_terms[0] if era_terms else ""),
        locations=[place] if place else [],
    )
    if topup:
        scenario["vocabulary_topup"] = True
    return [scenario]


def derived_scenarios_for_tier(need, bible, tier, *, vocabulary=None):
    """Raw (plan-shaped) derived scenarios for one ladder tier,
    built from the need's fallback concepts and the bible. Empty
    when the tier has nothing honest to search — the caller records
    that outcome instead of inventing a query."""
    label = str(tier or "").strip().lower()
    if label == "related":
        return _derived_related(need, bible)
    if label == "activity_broll":
        return _derived_activity(need, bible, vocabulary or {})
    if label == "atmospheric":
        return _derived_atmospheric(need, bible, vocabulary or {})
    return []
