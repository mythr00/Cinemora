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

import copy
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

#: Metadata-absence bypass (run-6345 fix, 2026-11-23): speculative
#: downloads of gate-absence candidates are capped per beat at the
#: D-B2 per-need download-attempt number, and per production they
#: share the PRODUCTION_MAX_EXTRA_DOWNLOADS account above with
#: tier-escalation downloads — the production never spends more
#: than 20 extra downloads in total, whichever route spends them.
SPECULATIVE_PER_BEAT_DOWNLOAD_CAP = TIER_DOWNLOAD_ATTEMPTS

#: Breadth-first allocation (founder live test, Petito run
#: 1b0a4508): the caps above used to be spent strictly in scene
#: order, so early scenes' repeated full ladder walks drained the
#: account and the final scene's sentences recorded
#: budget_exhausted attempts with zero executed queries — the
#: ladder never ran for them at all. The ledger now allocates the
#: SAME caps breadth-first: every escalating need's FIRST executed
#: query is a protected "breadth" spend (it may use the whole
#: remaining account), and "deepening" spends (every later query
#: or download for that need) are gated so a fairness reserve
#: survives — one slot per registered need that has not had its
#: first attempt yet, plus this lookahead for needs in scenes the
#: stage has not reached. The caps themselves are unchanged.
FAIRNESS_LOOKAHEAD_SEARCHES = 8
FAIRNESS_LOOKAHEAD_DOWNLOADS = 4

#: Floor / beyond-floor split (Gate 2 starvation fix, 2026-11-26).
#: The reserve-plus-lookahead gate above was not enough: the
#: lookahead is a fixed guess (8 searches / 4 downloads), while a
#: production's later scenes can hold far more unserved needs than
#: that, and registration is per scene — so an early scene's
#: deepening could still lawfully spend past the floors of needs
#: in scenes the stage had not reached (Petito: 21/96 tier
#: attempts budget_exhausted, sentence 23 executed zero queries;
#: Container: 52/88). The accounts are therefore SPLIT, inside the
#: same D-B2 caps: half of each account is a FLOOR reserve that
#: only first attempts may draw on, and beyond-floor consumers —
#: search deepening, download deepening, and speculative
#: (metadata-absence bypass) downloads, which share ONE
#: beyond-floor download sub-account — may spend only from the
#: other half. First attempts (a need's first executed query
#: round, its first escalation download) may always use the whole
#: remaining account. The provable invariant: a later need's first
#: attempt can be refused only when the account is genuinely
#: exhausted, and at that point at least half of everything spent
#: went to first attempts — never to earlier scenes' deepening or
#: to speculative downloads. The caps themselves (40 / 20) are
#: unchanged.
FLOOR_SEARCH_RESERVE = PRODUCTION_MAX_EXTRA_SEARCHES // 2
DEEPENING_SEARCH_BUDGET = (
    PRODUCTION_MAX_EXTRA_SEARCHES - FLOOR_SEARCH_RESERVE
)
FLOOR_DOWNLOAD_RESERVE = PRODUCTION_MAX_EXTRA_DOWNLOADS // 2
BEYOND_FLOOR_DOWNLOAD_BUDGET = (
    PRODUCTION_MAX_EXTRA_DOWNLOADS - FLOOR_DOWNLOAD_RESERVE
)

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

# ---------------------------------------------------------------------------
# Graphic-preferred beats (UV-05, Unified Visual Audit, 2026-11-26)
#
# T4 atmospheric is the ladder's last SEARCH tier; T5 graphics are
# reached only through missing_visuals. For a beat whose content is
# inherently explanatory — a statistic, a process, a comparison —
# generic atmospheric footage is the WEAKEST support, weaker than
# the designed graphic the graphics path would produce (founder
# Scenario D; Annex A: strongest relevant support per beat, never
# fill with irrelevant material). When such a beat reaches the
# atmospheric tier — which the walk only does with zero verified
# candidates from every earlier tier — the atmospheric spend is
# skipped and the beat routes to graphics instead: the attempt is
# recorded with outcome GRAPHICS_PREFERRED (no queries, no
# downloads consumed) and the beat's missing_visuals entry carries
# reason "graphic_preferred" with this signal record.
#
# The signal is read ONLY from data the plan already carries —
# nothing is invented here:
# - the need's visual_intents primary is "graphic_explanation"; or
# - the plan's graphic_fallback explicitly allows a "statistic" or
#   "diagram" graphic (kinds the deterministic derivation never
#   produces — it yields only map/timeline — so their presence
#   means the plan itself authored an explanatory graphic); or
# - the scenario's format intent is "chart" or "graphic" at
#   confidence >= GRAPHIC_FORMAT_MIN_CONFIDENCE (the deterministic
#   format inference stamps chart at 0.9 for number-bearing text
#   and graphic at 0.4 for process text).
# Map/timeline fallbacks and geographic format intent are
# deliberately NOT signals: placed/timed beats are common and are
# well served by establishing footage — they are not inherently
# explanatory.
# ---------------------------------------------------------------------------

#: Attempt outcome recorded when the atmospheric tier is skipped
#: in favour of the graphics path.
GRAPHICS_PREFERRED = "graphics_preferred"

#: Scenario format intents that mark inherently explanatory
#: content, and the confidence floor for trusting the stamp.
GRAPHIC_FORMAT_INTENTS = ("chart", "graphic")
GRAPHIC_FORMAT_MIN_CONFIDENCE = 0.4

#: graphic_fallback kinds that are inherently explanatory (see
#: the section comment: the deterministic derivation never emits
#: these, so they are always plan-authored).
GRAPHIC_FALLBACK_EXPLANATORY_KINDS = ("statistic", "diagram")

#: Graphic kind reported to the graphics path per format intent.
_FORMAT_INTENT_GRAPHIC_KIND = {"chart": "statistic", "graphic": "diagram"}


def graphic_preferred_signal(need, scenario=None):
    """The graphics-eligible explanatory signal a need carries, or
    None. Returns {"signal": <which marker fired>, "kind": <the
    graphic kind the plan points at, or None>} — the record the
    caller stamps on the tier attempt and the missing_visuals
    entry. Pure: reads only the need's visual_intents and the
    scenario's format intent, both plan-carried data."""
    intents = (need or {}).get("visual_intents")
    if not isinstance(intents, dict):
        intents = {}
    fallback = intents.get("graphic_fallback")
    if not isinstance(fallback, dict):
        fallback = {}
    fallback_kind = (
        str(fallback.get("kind") or "").strip().lower() or None
    )
    if (
        str(intents.get("primary_visual_intent") or "").strip().lower()
        == "graphic_explanation"
    ):
        return {
            "signal": "primary_visual_intent",
            "kind": fallback_kind if fallback.get("allowed") else None,
        }
    if (
        fallback.get("allowed")
        and fallback_kind in GRAPHIC_FALLBACK_EXPLANATORY_KINDS
    ):
        return {"signal": "graphic_fallback", "kind": fallback_kind}
    scenario = scenario if isinstance(scenario, dict) else {}
    format_intent = str(
        scenario.get("format_intent") or ""
    ).strip().lower()
    try:
        confidence = float(scenario.get("format_intent_confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if (
        format_intent in GRAPHIC_FORMAT_INTENTS
        and confidence >= GRAPHIC_FORMAT_MIN_CONFIDENCE
    ):
        return {
            "signal": "format_intent",
            "kind": _FORMAT_INTENT_GRAPHIC_KIND[format_intent],
        }
    return None

# ---------------------------------------------------------------------------
# Attempt idempotency (run-10 coverage fix, 2026-11-22)
#
# An escalation SEARCH BATCH is identified by its NEED SCOPE plus
# its tier plus the normalised set of queries it executed. The
# need scope is the beat (beat_id + first sentence number, from
# the pipeline's need key — the scenario/shot index is excluded),
# so sibling shots of one beat and the coverage recovery rounds
# that re-walk the same beat share one scope, while distinct
# beats never do. A batch's search is a pure function of that
# identity — same need, same tier, same queries, same provider
# results — so within one production a completed batch is
# TERMINAL for its need: re-executing it can only re-spend the
# shared D-B2 account on results that were already verified and
# considered. Run 10 (a9a05467) drained the entire 40-search
# account this way: the closing legal beat alone was walked FOUR
# times (shot 1, shot 2, recovery round 1, recovery round 2),
# each walk re-executing the identical related batch
# "verdict forms 2021"; the phone/van beats' shots repeated the
# same pattern — 47 of 61 tier attempts ended budget_exhausted
# and the closing beats' deeper tiers never executed a query.
#
# The scope is deliberately the beat, not the whole production:
# the breadth-first fairness contract (Phase 2, pinned) gives
# every DISTINCT need its own protected first attempts, so an
# identical batch derived by a different beat still executes
# once for that beat — bounded, attributed deepening — while
# the same beat can never pay for the same batch twice.
#
# The ledger therefore keeps a production-scoped registry of
# completed batches. A later identical batch for the same need
# NEVER re-spends search budget and NEVER re-searches; when the
# recorded outcome was "verified", the recorded verified pool is
# replayed into the later walk's pool (the draw may still fetch
# a candidate that was never fetched — governed by the draw's
# own budgets, and a candidate whose source already failed a
# fetch stays dead by the Phase-2 failed-download guard, so no
# download is ever re-spent either).
# ---------------------------------------------------------------------------

#: Terminal batch outcomes the registry records. Anything else
#: (budget_exhausted with zero executed queries, wall_clock, a walk
#: interrupted mid-tier) is NOT terminal: nothing was decided.
ATTEMPT_TERMINAL_OUTCOMES = ("verified", "pool_exhausted")


def normalise_query(query):
    """Word-set signature of one query — the same normalisation
    the pipeline's tried-query recognition uses (sorted unique
    lowercase words of length >= 3), so word order and case never
    disguise a repeat."""
    return " ".join(
        sorted(
            {
                word.lower()
                for word in str(query or "").split()
                if len(word) >= 3
            }
        )
    )


def attempt_scope(need_key):
    """The need scope of a pipeline need key: the beat it belongs
    to, without the shot/scenario index. The pipeline's need keys
    are (beat_id, scenario_index, first_sentence_number) — sibling
    shots of one beat differ only in the middle element, and a
    beat's recovery rounds reuse the same key, so (beat_id,
    first_sentence) is exactly the set of walks that must share
    attempt records. Non-tuple keys pass through unchanged; None
    stays None (unattributed legacy spends)."""
    if need_key is None:
        return None
    if isinstance(need_key, (tuple, list)):
        if len(need_key) >= 3:
            return (need_key[0], need_key[2])
        return tuple(need_key)
    return need_key


def attempt_key(need_key, tier, queries):
    """The identity of one escalation search batch: its need
    scope plus its tier plus the frozenset of its normalised
    queries. ``None`` when the batch carries no usable query
    (nothing executed, nothing to record or repeat)."""
    normalised = frozenset(
        signature
        for signature in (normalise_query(q) for q in queries or [])
        if signature
    )
    if not normalised:
        return None
    return (
        attempt_scope(need_key),
        str(tier or "").strip().lower(),
        normalised,
    )


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
    creation and the guard then measures from there.

    Allocation (breadth-first): the search/download accounts are
    spent through allocate_searches / can_spend_download with a
    need key, so the caps are shared fairly across every uncovered
    need instead of draining in scene order — see
    FAIRNESS_LOOKAHEAD_SEARCHES and allocate_searches."""

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
        # Metadata-absence bypass (run-6345 fix, 2026-11-23):
        # speculative downloads — downloads of candidates the
        # metadata gate failed on absence grounds only, admitted
        # conditionally pending frame confirmation — spend from
        # the SAME production account as escalation downloads:
        # downloads + speculative_downloads may never exceed
        # PRODUCTION_MAX_EXTRA_DOWNLOADS (D-B2's ≤20 extra
        # downloads per production). Per-beat speculative spends
        # are tracked by beat key for the per-beat cap
        # (SPECULATIVE_PER_BEAT_DOWNLOAD_CAP).
        self.speculative_downloads = 0
        self._speculative_beat_downloads = {}
        # Gate 2 floor split: attributed escalation downloads
        # beyond a need's first (its deepening downloads). Together
        # with speculative_downloads this is the beyond-floor
        # download spend, capped at BEYOND_FLOOR_DOWNLOAD_BUDGET so
        # neither route can drain the floor half of the account
        # that later needs' first downloads draw on.
        self.deepening_downloads = 0
        # Breadth-first allocation state: per-need executed
        # escalation searches / downloads, keyed by the need key
        # the pipeline registers (see register_need). A need's
        # first executed search is its breadth spend; every later
        # one is deepening. Needs may be registered before they
        # escalate (the scene driver registers a whole scene's
        # needs up front) — a registered need with no executed
        # search holds a fairness-reserve slot until it spends or
        # is released.
        self._need_searches = {}
        self._need_downloads = {}
        self.breadth_searches = 0
        self.deepening_searches = 0
        # Attempt idempotency (run-10 fix): completed escalation
        # search batches, keyed by attempt_key — see the section
        # comment above ATTEMPT_TERMINAL_OUTCOMES. Records are
        # production-scoped runtime state, like the accounts.
        self._attempts = {}
        self.attempt_repeats = 0

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

    # -- need registration (breadth-first allocation) ---------------
    def register_need(self, key):
        """Register an escalating need before it spends. Idempotent;
        a ``None`` key is ignored (unattributed legacy spends keep
        the pre-fairness account semantics). Registration alone
        spends nothing — it reserves the need a fairness slot."""
        if key is None:
            return
        if key not in self._need_searches:
            self._need_searches[key] = 0
        if key not in self._need_downloads:
            self._need_downloads[key] = 0

    def release_need(self, key):
        """Drop a registered need that never spent (its beat
        completed without escalating) from the reserve accounting.
        A need that HAS spent keeps its record — its breadth spend
        is historical fact, not a reservable slot."""
        if key is None:
            return
        if self._need_searches.get(key) == 0:
            self._need_searches.pop(key, None)
        if self._need_downloads.get(key) == 0:
            self._need_downloads.pop(key, None)

    def needs_registered(self):
        return len(self._need_searches)

    def needs_with_first_attempt(self):
        return sum(
            1 for spent in self._need_searches.values() if spent > 0
        )

    def fairness_reserve(self):
        """Searches that must survive deepening spends: one per
        registered need still waiting for its first attempt, plus
        the lookahead for needs in scenes not yet reached."""
        unserved = sum(
            1 for spent in self._need_searches.values() if spent <= 0
        )
        return unserved + FAIRNESS_LOOKAHEAD_SEARCHES

    # -- searches ---------------------------------------------------
    def allocate_searches(self, count=1, need_key=None):
        """How many of ``count`` requested escalation searches may
        execute NOW for this need, under breadth-first allocation.
        Does not spend — pair with spend_searches.

        - The wall-clock guard and the production cap bind first.
        - A need that has never executed a search gets its first
          one protected (breadth): it may use the whole remaining
          account, so a late scene's first attempt is never refused
          while any budget remains.
        - Every further search is deepening: it may execute only
          while the fairness reserve survives the spend AND the
          beyond-floor sub-account (DEEPENING_SEARCH_BUDGET) has
          room — deepening can never touch the floor half of the
          account, however large the registered reserve looks.
        - Without a need key the historical semantics hold (the
          whole remaining account, nothing reserved)."""
        count = int(count)
        if count <= 0 or self.wall_clock_exceeded():
            return 0
        remaining = PRODUCTION_MAX_EXTRA_SEARCHES - self.searches
        if remaining <= 0:
            return 0
        allowed = min(count, remaining)
        if need_key is None:
            return allowed
        self.register_need(need_key)
        # Room left for deepening under the fairness reserve (the
        # pre-Gate-2 gate), and under the floor split. Both bind.
        reserve_room = max(
            PRODUCTION_MAX_EXTRA_SEARCHES
            - self.fairness_reserve()
            - self.searches,
            0,
        )
        split_room = max(
            DEEPENING_SEARCH_BUDGET - self.deepening_searches, 0
        )
        if self._need_searches.get(need_key, 0) <= 0:
            # Breadth: one protected search (it spends from the
            # whole account, so the reserve room for the deepening
            # remainder is priced after it), the rest deepening.
            return 1 + min(
                allowed - 1,
                max(reserve_room - 1, 0),
                split_room,
            )
        return min(allowed, reserve_room, split_room)

    def can_spend_searches(self, count=1, need_key=None):
        return self.allocate_searches(count, need_key=need_key) >= int(count)

    def spend_searches(self, count=1, need_key=None):
        count = int(count)
        self.searches += count
        if need_key is None or count <= 0:
            return
        self.register_need(need_key)
        prior = self._need_searches.get(need_key, 0)
        breadth = 1 if prior <= 0 else 0
        self.breadth_searches += breadth
        self.deepening_searches += count - breadth
        self._need_searches[need_key] = prior + count

    def fairness_reserve_downloads(self):
        """Downloads that must survive deepening spends (Phase 3a /
        C-01): one per registered need still waiting for its first
        download, plus the lookahead for needs in scenes the stage
        has not reached. Before Phase 3a the deepening gate priced
        only the fixed lookahead, so within a scene an early
        need's deepening downloads could consume the slots later
        registered needs were counting on — the download-side twin
        of the search starvation fairness_reserve() already
        prevented."""
        unserved = sum(
            1 for spent in self._need_downloads.values() if spent <= 0
        )
        return unserved + FAIRNESS_LOOKAHEAD_DOWNLOADS

    # -- downloads --------------------------------------------------
    def _download_gate(self, need_key, wall_clock_binds):
        if wall_clock_binds and self.wall_clock_exceeded():
            return False
        # The production account is shared with speculative
        # (metadata-absence bypass) downloads — see __init__.
        if (
            self.downloads + self.speculative_downloads
            >= PRODUCTION_MAX_EXTRA_DOWNLOADS
        ):
            return False
        if need_key is None:
            return True
        self.register_need(need_key)
        if self._need_downloads.get(need_key, 0) <= 0:
            # A need's first escalation download is breadth too:
            # a floor spend, drawable from the whole account.
            return True
        # Deepening: a beyond-floor spend. It shares the
        # beyond-floor download sub-account with speculative
        # downloads (Gate 2 floor split) and must leave the
        # fairness reserve standing, exactly as before.
        if (
            self.speculative_downloads + self.deepening_downloads + 1
            > BEYOND_FLOOR_DOWNLOAD_BUDGET
        ):
            return False
        return (
            self.downloads + self.speculative_downloads + 1
            + self.fairness_reserve_downloads()
            <= PRODUCTION_MAX_EXTRA_DOWNLOADS
        )

    # -- speculative downloads (metadata-absence bypass) ------------
    def can_spend_speculative_download(self, beat_key=None):
        """Whether a speculative (metadata-absence bypass) download
        may start: the shared production account (escalation +
        speculative ≤ PRODUCTION_MAX_EXTRA_DOWNLOADS, D-B2) and the
        per-beat speculative cap both bind. The wall-clock guard
        does NOT bind here — it gates new escalation work, and a
        speculative candidate was verified (scored) before the
        guard could trip, the can_draw_verified_download
        precedent.

        Gate 2 floor split: a speculative download is a
        beyond-floor consumer, so two further gates bind. It may
        not spend once the beyond-floor download sub-account
        (shared with escalation deepening downloads) is spent, and
        it must leave one slot standing for every registered need
        still waiting for its first escalation download — a
        speculative route can therefore never drain the reserve
        that later sentences' first tier attempts draw on, however
        early in the production it fires."""
        if (
            self.downloads + self.speculative_downloads
            >= PRODUCTION_MAX_EXTRA_DOWNLOADS
        ):
            return False
        if (
            self.speculative_downloads + self.deepening_downloads
            >= BEYOND_FLOOR_DOWNLOAD_BUDGET
        ):
            return False
        unserved = sum(
            1 for spent in self._need_downloads.values() if spent <= 0
        )
        if (
            self.downloads + self.speculative_downloads + 1 + unserved
            > PRODUCTION_MAX_EXTRA_DOWNLOADS
        ):
            return False
        if beat_key is not None and (
            self._speculative_beat_downloads.get(beat_key, 0)
            >= SPECULATIVE_PER_BEAT_DOWNLOAD_CAP
        ):
            return False
        return True

    def spend_speculative_download(self, beat_key=None):
        self.speculative_downloads += 1
        if beat_key is not None:
            self._speculative_beat_downloads[beat_key] = (
                self._speculative_beat_downloads.get(beat_key, 0) + 1
            )

    def can_spend_download(self, need_key=None):
        """Whether a NEW escalation download spend may start. The
        wall-clock guard binds here: it exists to stop new
        escalation work late in the stage."""
        return self._download_gate(need_key, wall_clock_binds=True)

    def can_draw_verified_download(self, need_key=None):
        """Whether a fetch may be drawn from an ALREADY-VERIFIED
        pool (run-11 fix 4).

        The wall-clock guard gates NEW searches and new escalation
        spends; it must not strand a pool that was verified before
        the guard tripped. Run 11's sentence 12 verified 5
        candidates, had the batch's outcome overwritten to
        budget_exhausted/wall_clock, and then drew ZERO of them —
        every draw was refused by the wall-clock check inside
        can_spend_download — and a later batch verified 12 more
        with the same result. Drawing from a verified pool is not
        new escalation work: the searches are already spent. The
        production download cap (D-B2) and the breadth-first
        fairness reserve bind exactly as in can_spend_download;
        ONLY the wall-clock check does not apply."""
        return self._download_gate(need_key, wall_clock_binds=False)

    def spend_download(self, need_key=None):
        self.downloads += 1
        if need_key is None:
            return
        self.register_need(need_key)
        prior = self._need_downloads.get(need_key, 0)
        if prior > 0:
            # Beyond this need's first download: a deepening spend
            # against the shared beyond-floor sub-account.
            self.deepening_downloads += 1
        self._need_downloads[need_key] = prior + 1

    # -- attempt idempotency (run-10 fix) ---------------------------
    def completed_attempt(self, tier, queries, need_key=None):
        """The recorded terminal record for this exact batch
        (need scope + tier + normalised query set), or None when
        the batch has not completed for this need in this
        production. A hit is counted as a repeat on the record and
        on the ledger. The returned record's ``pool_items`` are
        the first execution's verified pool (deep copies — the
        caller may mutate what it draws from them); an outcome of
        "pool_exhausted" always carries an empty pool."""
        key = attempt_key(need_key, tier, queries)
        if key is None:
            return None
        record = self._attempts.get(key)
        if record is None:
            return None
        record["repeats"] = record.get("repeats", 0) + 1
        self.attempt_repeats += 1
        return {
            **record,
            "pool_items": copy.deepcopy(record.get("pool_items") or []),
        }

    def record_attempt(self, tier, queries, *, outcome, verified=0,
                       pool_items=None, need_key=None):
        """Record a completed escalation search batch. Only
        terminal outcomes (ATTEMPT_TERMINAL_OUTCOMES) are recorded;
        anything else is ignored — a batch that never executed
        decided nothing. The first completion wins: a record is
        never overwritten. ``pool_items`` (the batch's verified
        pool, for replay) is stored as a deep copy and is kept
        only for a "verified" outcome."""
        if outcome not in ATTEMPT_TERMINAL_OUTCOMES:
            return None
        key = attempt_key(need_key, tier, queries)
        if key is None or key in self._attempts:
            return self._attempts.get(key) if key is not None else None
        record = {
            "tier": key[1],
            "need_scope": key[0],
            "queries": sorted({str(q) for q in queries or []}),
            "outcome": outcome,
            "verified": int(verified or 0),
            "pool_items": (
                copy.deepcopy(list(pool_items))
                if outcome == "verified" and pool_items
                else []
            ),
            "need_key": need_key,
            "repeats": 0,
        }
        self._attempts[key] = record
        return record

    def attempts_recorded(self):
        return len(self._attempts)

    def summary(self):
        return {
            "extra_searches": self.searches,
            "extra_downloads": self.downloads,
            # Metadata-absence bypass (run-6345 fix): speculative
            # downloads share the download_budget account above;
            # reported separately so the split stays auditable.
            "speculative_downloads": self.speculative_downloads,
            "search_budget": PRODUCTION_MAX_EXTRA_SEARCHES,
            "download_budget": PRODUCTION_MAX_EXTRA_DOWNLOADS,
            # Breadth-first allocation accounting (additive): how
            # the search account split between protected first
            # attempts and deepening, and the reserve still held.
            "breadth_searches": self.breadth_searches,
            "deepening_searches": self.deepening_searches,
            "needs_registered": self.needs_registered(),
            "needs_with_first_attempt": self.needs_with_first_attempt(),
            "fairness_reserve_searches": self.fairness_reserve(),
            "fairness_reserve_downloads": self.fairness_reserve_downloads(),
            # Floor / beyond-floor split accounting (additive,
            # Gate 2): the floor reserves deepening may never
            # touch, and the beyond-floor download spend (escalation
            # deepening + speculative) against its sub-account.
            "floor_search_reserve": FLOOR_SEARCH_RESERVE,
            "deepening_search_budget": DEEPENING_SEARCH_BUDGET,
            "floor_download_reserve": FLOOR_DOWNLOAD_RESERVE,
            "beyond_floor_download_budget": BEYOND_FLOOR_DOWNLOAD_BUDGET,
            "deepening_downloads": self.deepening_downloads,
            "beyond_floor_download_spends": (
                self.speculative_downloads + self.deepening_downloads
            ),
            # Attempt idempotency accounting (additive, run-10 fix):
            # distinct completed batches on record, and how many
            # later identical batches were recognised as repeats
            # (and therefore spent nothing).
            "attempts_recorded": self.attempts_recorded(),
            "attempt_repeats": self.attempt_repeats,
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
