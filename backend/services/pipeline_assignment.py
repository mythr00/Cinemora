"""Assignment/coverage seam of the former media_pipeline monolith
(Phase 5.4).

Everything that decides WHICH verified candidate serves a beat and
whether the beat is covered: the eligibility gate (gate_all_failures
/ gate_shot_need and the Phase 5 subject/place matching helpers —
_fold_name, _name_tokens, _alias_match, _subject_match, _place_match,
_place_satisfied, _gate_type_tokens; thresholds and matching logic
unchanged, including the 6437244 folded/tokenised/alias-aware fix),
intent-aware pick adjustments (_intent_rank_adjustment,
_beat_wants_video, _apply_video_preference, _apply_source_quality_
tiebreak), the pick itself (_uve_pick), and the coverage helpers
(_uve_prepare_sentence_result, _uve_selection_block_reason,
_uve_beat_distinct_asset_keys, _uve_beat_distinct_asset_level_keys,
_uve_coverage_shortfall, COVERAGE_DRAW_MAX_ATTEMPTS).

Re-exported through media_pipeline; every historical
``media_pipeline.<name>`` reference keeps working unchanged.

CALL-SITE CONVENTION — read before editing: the names this module
still needs from outside itself are result_title and
get_sentence_text (small sentence/result helpers that remain in
media_pipeline) and _uve_candidate_key (candidate identity, in
pipeline_verification); all three are resolved through
media_pipeline's namespace at call time
(``_media_pipeline().<name>``), the same convention as the other
pipeline_* modules. The eligibility constants (INTENT_FIT_MAX,
MISREPRESENTATION_DEMOTION, REUSE_PENALTY_PER_USE,
VIDEO_PREFERENCE_EPSILON) are defined here and re-exported;
media_pipeline's own uses resolve to these same objects.

WHAT STAYED in media_pipeline, and why (Phase 5.4 coupling note):
the coverage ORCHESTRATION — _uve_search_and_pick (which contains
the nested _draw_reserves draw loop and the tier-escalation hand-off
to services.tier_ladder) and _uve_process_scene_media — is the
conductor every seam hangs off: it pushes the AM-021 query log,
drives discovery/verification/fetch in sequence, patches and is
patched at a dozen points, and owns the remaining RunContext-bound
segment state. It stays as the orchestrator; this module is the
pure decision layer it calls.
"""

import re
import unicodedata

from services import rejection_diagnostics
from services import story_understanding as story
from services import visual_identity

try:
    from services import timeline as timeline_service
except ImportError:  # flat-layout fallback
    import timeline as timeline_service

try:
    from services.canonical_media import canonical_source_key
except ImportError:  # flat-layout fallback
    from canonical_media import canonical_source_key  # noqa: F401


def _media_pipeline():
    """Lazy handle on media_pipeline (circular-import-safe; see the
    module docstring for why cross-seam calls resolve through it)."""
    try:
        from services import media_pipeline
    except ImportError:  # flat-layout fallback
        import media_pipeline
    return media_pipeline


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


# ---------------------------------------------------------------------------
# Transformation Phase 5 (Phase 4.3 follow-up): name matching for the
# metadata gate. Phase 4.3's labelled calibration measured the gate's
# absence checks (missing_subject / missing_place / missing_person)
# rejecting candidates whose recorded metadata DOES name the required
# subject/place in a non-literal form — the canonical case being the
# place string "boise_courtroom" (an entity id that leaked into the
# need's place field) never literally matching "Boise courtroom" in a
# title. The helpers below keep the gate's historical semantics
# (full-string or all-significant-parts substring presence) and add
# exactly three conservative extensions, each recorded as a match
# basis on the candidate record:
#
#   1. Unicode folding on BOTH sides (lowercase, ß -> ss, NFKD
#      diacritic stripping): "Bernauer Strasse" now matches a title's
#      "Bernauer Straße".
#   2. Tokenisation on any non-alphanumeric run, not just whitespace:
#      "boise_courtroom" yields the parts {boise, courtroom}.
#   3. Bible aliases: the case bible's recorded aliases for the
#      subject/place entity (stamped on the need as subject_aliases /
#      place_aliases by build_shot_needs and _uve_verify_all) satisfy
#      the same checks — parity with verify_candidate, which has
#      always matched the subject's aliases. An alias that reduces to
#      a single significant token is honoured only when it is
#      discriminative: an all-caps acronym (FBI, WSU), a token of
#      length >= 6, or a head token of the canonical name — for a
#      person only the LAST token (the surname, "Kohberger"); for
#      other entities the first or last (a place's proper-name head,
#      "Boise" of "Boise courtroom"). A person's lone given name
#      ("Bryan", "Madison") never satisfies a check on its own: that
#      would admit on weaker evidence than the canonical name
#      requires, the direction Phase 4.3's accepted-stratum
#      false-admit rate (33.3%) warns against.
#
# Nothing here touches the similarity thresholds (0.55/0.34/0.22),
# the reuse caps, or the category profiles: eligibility stays the
# gate's, ranking stays the scorer's.
# ---------------------------------------------------------------------------

_NAME_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "at", "to", "and", "or",
    "with", "from", "by", "for",
})


def _fold_name(text):
    """Fold a name or metadata blob for comparison: lowercase,
    ß -> ss, NFKD-normalise and strip combining marks (diacritics)."""
    folded = str(text or "").lower().replace("ß", "ss")
    folded = unicodedata.normalize("NFKD", folded)
    return "".join(c for c in folded if not unicodedata.combining(c))


def _name_tokens(text):
    """Folded alphanumeric tokens of a name/blob (any non-alphanumeric
    run is a separator, so underscores and hyphens split compounds)."""
    return re.findall(r"[a-z0-9]+", _fold_name(text))


def _sig_name_tokens(text):
    """The significant tokens of a name: folded tokens longer than
    two characters that are not stopwords, order-preserving."""
    tokens = []
    for token in _name_tokens(text):
        if len(token) > 2 and token not in _NAME_STOPWORDS:
            tokens.append(token)
    return tokens


def _name_match(name, blob):
    """Whether ``name`` is evidenced by the metadata blob, and how.

    Returns a basis dict {"via": "full"|"tokens", "matched": name,
    "tokens": [...]} or None. Semantics extend the historical check
    (full string, else every significant part as a substring) with
    Unicode folding and non-whitespace tokenisation only."""
    folded_blob = _fold_name(blob)
    folded_name = " ".join(_fold_name(name).split())
    if folded_name and folded_name in folded_blob:
        return {"via": "full", "matched": str(name), "tokens": _sig_name_tokens(name)}
    parts = _sig_name_tokens(name)
    if parts and all(part in folded_blob for part in parts):
        return {"via": "tokens", "matched": str(name), "tokens": parts}
    return None


def _alias_match(aliases, canonical, blob, kind=""):
    """Whether any bible alias for an entity is evidenced by the blob.

    Multi-token aliases go through the ordinary name match. A
    single-significant-token alias is honoured only when it is
    discriminative (see the Phase 5 note above): an all-caps acronym
    in its recorded form, a token of length >= 6, or one of the
    canonical name's head tokens — for a PERSON only the LAST token
    (the surname: "Kohberger"); for any other entity the FIRST or
    LAST token (a place's proper-name head or head noun: "Boise" of
    "Boise courtroom", "wall" of "Berlin Wall"). A person's lone
    given name never qualifies. The basis records the alias that
    satisfied the check."""
    canonical_tokens = _sig_name_tokens(canonical)
    if kind == "person":
        head_tokens = set(canonical_tokens[-1:]) if canonical_tokens else set()
    else:
        head_tokens = set()
        if canonical_tokens:
            head_tokens.add(canonical_tokens[0])
            head_tokens.add(canonical_tokens[-1])
    for alias in aliases or []:
        alias_text = str(alias or "").strip()
        if not alias_text:
            continue
        if _fold_name(alias_text) == _fold_name(canonical):
            continue
        tokens = _sig_name_tokens(alias_text)
        if len(tokens) == 1:
            token = tokens[0]
            recorded_tokens = [
                t for t in re.findall(r"[A-Za-z0-9]+", alias_text)
            ]
            is_acronym = any(
                len(t) >= 2 and t.isupper() for t in recorded_tokens
            )
            if not (is_acronym or token in head_tokens or len(token) >= 6):
                continue
            if token in _fold_name(blob):
                return {
                    "via": "alias",
                    "matched": alias_text,
                    "tokens": [token],
                }
            continue
        basis = _name_match(alias_text, blob)
        if basis:
            basis = dict(basis)
            basis["via"] = "alias"
            basis["matched"] = alias_text
            return basis
    return None


def _subject_match(subject, aliases, blob, kind=""):
    """The subject check's verdict + basis: the canonical name's own
    match, else a discriminative bible alias's match, else None.
    ``kind`` is "person" when the subject entity is a person (the
    need's required_type, or its stamped subject_entity_type)."""
    basis = _name_match(subject, blob)
    if basis:
        return basis
    return _alias_match(aliases, subject, blob, kind=kind)


def _place_match(place, blob, aliases=None):
    """The place check's verdict + basis (see _place_satisfied for
    the component semantics), extended with the place entity's bible
    aliases when no component matches."""
    components = [c.strip() for c in str(place or "").split(",") if c.strip()]
    for component in components or [str(place or "")]:
        basis = _name_match(component, blob)
        if basis:
            basis = dict(basis)
            basis["component"] = component
            return basis
    if aliases:
        return _alias_match(aliases, place, blob)
    return None


def _place_satisfied(place, blob, aliases=None):
    """Whether a need's place requirement is met by the metadata blob.

    Stage 1 compound-place fix: a compound place string ("King Road
    house, Moscow, Idaho") names a set of place components; the old
    check required EVERY token of the whole compound to appear,
    which almost no real title+snippet satisfies (881 missing_place
    rejections in the stored Kohberger run). Each comma-separated
    component is now an alternative: the component's full string, or
    all of its significant parts, satisfies the requirement. A
    single-component place behaves exactly as before.

    Transformation Phase 5: matching goes through _place_match —
    Unicode folding, non-whitespace tokenisation, and the place
    entity's bible aliases (when the need carries them)."""
    return _place_match(place, blob, aliases) is not None


_TYPE_TOKEN_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "at", "to", "and", "or",
    "with", "inside", "near", "over", "under", "during", "into",
    "from", "by", "for", "as", "is", "are", "was", "were", "its",
    "it", "this", "that", "shot", "scene", "view", "image", "photo",
    "photograph", "footage", "video", "showing", "shows", "depicting",
    "depicts", "close", "wide", "front", "back",
})


def _gate_type_tokens(need):
    """The activity/place-TYPE tokens of a shot need (Stage 7 / R7,
    review C.B.2): the content words of the shot's own description
    (visual_requirement / description / search_terms) MINUS every
    token belonging to the required subject or the named place —
    the type set is "bedroom, student house, night", never the
    proper noun. Order-preserving, de-duplicated."""
    named = set()
    for value in (need.get("required_subject"), need.get("place")):
        named.update(re.findall(r"[a-z0-9]+", str(value or "").lower()))
    tokens = []
    seen = set()

    def _collect(text):
        for token in re.findall(r"[a-z0-9]+", str(text or "").lower()):
            if (
                len(token) <= 2
                or token in named
                or token in _TYPE_TOKEN_STOPWORDS
                or token in seen
            ):
                continue
            seen.add(token)
            tokens.append(token)

    _collect(need.get("visual_requirement"))
    _collect(need.get("description"))
    for term in need.get("search_terms") or []:
        _collect(term)
    return tokens


def gate_all_failures(candidate, shot_need):
    """Every gate check that fails for this candidate, in the
    gate's execution order (Stage 6 / R5 collect-all diagnostics).

    ``gate_shot_need`` is implemented on top of this list — its
    primary reason is the first entry — so the recorded primary can
    never disagree with the decision, and the remaining entries are
    the contributing failures the first-fail gate used to destroy
    (Architecture Review C.A.3.2). Diagnostics only: nothing here
    changes which candidates pass.
    """
    failures = []

    def _fail(reason):
        if reason not in failures:
            failures.append(reason)

    need = shot_need or {}

    blob = _title_blob(candidate)

    # Workstream J: banned providers are rejected at the gate too —
    # the canonical banned-provider outcome, whatever seam produced
    # the candidate (verify_candidate enforces the same rejection for
    # direct callers).
    if story.is_banned_provider(candidate):
        _fail(story.BANNED_PROVIDER_REASON)

    # Plan §3.3 item 1 (tests 17-18): wrong-project provenance is
    # rejected at the gate too — a candidate record stamped with
    # another project's identity never enters this run's pool
    # (verify_candidate enforces the same rejection for direct
    # callers). A missing stamp is not a mismatch: unstamped
    # discovery candidates pass through to the checks below.
    if story.is_foreign_provenance(candidate):
        _fail(story.FOREIGN_PROVENANCE_REASON)

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
        _fail(story.CONTRADICTORY_DATE)

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

    # Stage 1 / R7: category-aware profile. The absence checks below
    # are REQUIRED-field checks, and which fields are required is a
    # property of the visual category the need belongs to (see
    # story_understanding.CATEGORY_PROFILES): evidence needs keep the
    # full named-subject/named-place contract; contextual and
    # environmental needs (Stage 7 completion of R7, review C.B.2)
    # drop the named-entity absence checks for the type-presence
    # floor further down; atmosphere (generic) needs have no
    # subject/place contract to enforce at the gate —
    # verify_candidate's topical-evidence rules and threshold remain
    # their acceptance bar. Contradictions, forbidden terms, banned
    # providers and foreign provenance above/below are NOT profiled:
    # they reject in every category.
    _profile = story.category_profile(story.resolve_visual_category(need))

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
            _fail("forbidden_term")
            break

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
                _fail(story.CONTRADICTORY_PERSON)
                break
    _need_place = str(need.get("place") or "").strip().lower()
    if _need_place and not _named(_need_place):
        for other in need.get("other_places") or []:
            if _named(other):
                _fail(story.CONTRADICTORY_PLACE)
                break

    # Stage 7 / R7 (C.B.2): the contextual/environmental
    # TYPE-presence floor. Profiles with type_presence_required do
    # not require the named subject/place — the absence checks below
    # stay off for them — but the candidate must show SOME presence
    # of the shot's activity/place-type tokens. The failure is the
    # registered no_topical_evidence reason, recorded against the
    # TYPE requirement, never as a missing named entity. When the
    # need carries no type tokens the floor is not evaluable and
    # does not fire: absence of a contract is not a failure, and the
    # scorer still judges the candidate on its own rules.
    if _profile.get("type_presence_required"):
        _type_tokens = _gate_type_tokens(need)
        if _type_tokens and not any(
            token in blob for token in _type_tokens
        ):
            _fail("no_topical_evidence")

    # Transformation Phase 5: subject/place satisfaction goes
    # through the folded, tokenised, alias-aware matchers
    # (_subject_match / _place_match); every satisfied check's basis
    # (canonical full/tokens, or the bible alias that matched) is
    # recorded on the candidate record below as _gate_match_basis.
    match_basis = {}

    def _subject_ok():
        basis = _subject_match(
            subject,
            need.get("subject_aliases") or [],
            blob,
            kind=(
                "person"
                if kind == "person"
                or str(need.get("subject_entity_type") or "").strip().lower()
                == "person"
                else ""
            ),
        )
        if basis:
            match_basis["subject"] = basis
        return basis is not None

    def _place_ok():
        basis = _place_match(
            place, blob, need.get("place_aliases") or []
        )
        if basis:
            match_basis["place"] = basis
        return basis is not None

    # Named-subject contracts require the requested subject to
    # appear in the searchable metadata — in the categories whose
    # profile requires a subject (evidence).
    if (
        _profile["subject_required"]
        and kind == "person"
        and subject
    ):
        if not _subject_ok():
            _fail("missing_person")

    # Entity/location contracts require the subject (same profile
    # condition as the person contract above).
    if (
        _profile["subject_required"]
        and kind in {
            "location",
            "organization",
            "event",
            "object",
        }
        and subject
    ):
        if not _subject_ok():
            _fail("missing_subject")

    # If the category profile requires a place, it must appear in
    # the candidate metadata (compound places: any one component).
    if _profile["place_required"] and place:
        if not _place_ok():
            _fail("missing_place")

    # Exact contracts require all explicit identity
    # information to survive the metadata gate.
    if specificity == "exact":
        if subject:
            if not _subject_ok():
                _fail("missing_subject")

        if place:
            if not _place_ok():
                _fail("missing_place")

    if isinstance(candidate, dict) and match_basis:
        candidate["_gate_match_basis"] = match_basis

    return failures


def gate_shot_need(candidate, shot_need):
    """
    Strict metadata gate for a visual contract.

    This gate is intentionally generic. It rejects candidates
    that clearly contradict the requested subject, location,
    event, or other forbidden terms before download.

    The decision is the first failure in gate_all_failures'
    execution order — identical to the historical first-fail
    behaviour; the collect-all list exists for diagnostics.
    """
    failures = gate_all_failures(candidate, shot_need)
    if isinstance(candidate, dict):
        # Stage 6 / R5 diagnostic side channel: the full failure
        # list rides on the candidate so _uve_verify_all can record
        # contributing reasons WITHOUT evaluating the gate a second
        # time (VA-11: the gate executes exactly once per candidate,
        # here, and never again in the pick).
        candidate["_gate_failures"] = failures
    if failures:
        return False, failures[0]
    return True, ""

# Metadata-absence bypass (run-6345 fix, 2026-11-23): the score
# penalty a SPECULATIVE candidate carries. A candidate whose gate
# failures are absence-only (rejection_diagnostics.ABSENCE_REASONS)
# and whom the scorer accepts on positive evidence is admitted
# conditionally — its verdict score is reduced by this penalty, so
# it ranks below every metadata-verified candidate of comparable
# evidence and can only win a pick when the verified pool cannot
# serve the beat. 0.10 sits on the existing bounded-adjustment
# scale (format-intent bonus max 0.10; reuse penalty 0.05/use;
# intent fit 0.05): large enough to demote a speculative candidate
# below a same-evidence verified one, small enough not to bury it
# below unrelated verified material. The admission is never silent:
# the marker (metadata_absence_bypass), this penalty and the
# absence reasons are recorded on the verdict, the candidate
# record and the decision ledger, downloads are budget-capped
# (services.tier_ladder speculative account), and the asset must
# still be CONFIRMED by frame-level verification after download —
# see _uve_verify_all / _uve_fetch_first in pipeline_verification.
METADATA_ABSENCE_PENALTY = 0.10

# Quality Recovery Phase 2 (workstream C): selection-time REUSE
# PENALTY per prior use of a candidate's canonical source. Bounded by
# construction: at the hard cap of 3 uses the maximum differential is
# 2 x 0.05 = 0.10, so a candidate that is MATERIALLY more relevant
# (raw-score gap > 0.10) can never be overtaken by a fresher one, and
# the pool here contains only verifier-ACCEPTED candidates, so the
# penalty can never promote an unverified candidate. The hard source
# cap stays absolute and is enforced separately below.
REUSE_PENALTY_PER_USE = 0.05

# Master Directive Phase 3 — intent-aware ranking (Part 6 Stage B)
# and the AM-004 video preference. ARCHITECTURE RULE (Phase 1): these
# signals order the ALREADY-VERIFIED pool only. Eligibility (gate,
# verifier thresholds 0.55/0.34/0.22, contradiction vetoes) is decided
# before ranking and is never touched here: no signal below can admit,
# rescue or re-judge a candidate. Every signal is bounded, named, and
# recorded on the candidate it moved (selection reasons), following
# the format-intent bonus pattern (capped at 0.10 x confidence).
#
#: Medium-fit bonus when the beat's primary visual intent expects a
#: medium the scenario's format intent did NOT already reward (the
#: format bonus is checked first; a signal is never counted twice).
INTENT_FIT_MAX = 0.05
#: Demotion for an exact, high-misrepresentation-risk beat when the
#: verifier's own dimension record shows NO subject evidence for the
#: candidate — on such a beat a subject-less visual is the one most
#: likely to misrepresent. Bounded, so it can only reorder near-equal
#: verified candidates, never bury a materially stronger one.
MISREPRESENTATION_DEMOTION = 0.05
#: AM-004 (revised): within the accepted pool, a verified video is
#: preferred over a verified still only when their effective scores
#: are COMPARABLE — inside this epsilon. Small by design (register:
#: the epsilon must stay small); recorded on every candidate it moves.
VIDEO_PREFERENCE_EPSILON = 0.05

#: Primary intents whose expected medium is moving footage / stills.
#: Every other intent (establishing, archival, graphic_explanation,
#: ...) carries no medium expectation beyond the format intent.
_INTENT_VIDEO_INTENTS = frozenset({
    "actual_event", "activity_broll", "human_activity",
})
_INTENT_IMAGE_INTENTS = frozenset({"object_detail"})


def _intent_rank_adjustment(item, kind, shot_need, scenario, format_bonus):
    """The bounded Phase 3 ranking adjustment for one pool candidate.

    Returns (adjustment, reasons). Inputs are the need's Phase 2
    visual_intents and the verifier's RECORDED dimension
    contributions — no signal re-derives what the verifier already
    scored: the medium fit fires only when this kind's format-intent
    bonus was zero, and the misrepresentation demotion reads the
    verdict's subject contribution rather than re-judging relevance.
    """
    intents = (shot_need or {}).get("visual_intents")
    if not isinstance(intents, dict):
        return 0.0, []
    adjustment = 0.0
    reasons = []
    primary = str(intents.get("primary_visual_intent") or "").strip().lower()
    if format_bonus == 0:
        if primary in _INTENT_VIDEO_INTENTS and kind == "video":
            adjustment += INTENT_FIT_MAX
            reasons.append(f"intent_fit:video:{primary}")
        elif primary in _INTENT_IMAGE_INTENTS and kind == "image":
            adjustment += INTENT_FIT_MAX
            reasons.append(f"intent_fit:image:{primary}")
    specificity = str(
        intents.get("specificity_requirement") or ""
    ).strip().lower()
    risk = str(
        intents.get("visual_misrepresentation_risk") or ""
    ).strip().lower()
    if specificity == "exact" and risk == "high":
        verdict = item.get("verification") or {}
        contributions = verdict.get("contributions") or {}
        if verdict.get("accepted") and not contributions.get("subject"):
            adjustment -= MISREPRESENTATION_DEMOTION
            reasons.append("misrepresentation_demotion:no_subject_evidence")
    return round(adjustment, 3), reasons


def _beat_wants_video(scenario, shot_need):
    """AM-004 trigger: the beat asks for moving footage — via the
    scenario's format intent, the intents' preferred media type, or
    a footage-shaped primary intent."""
    if str((scenario or {}).get("format_intent") or "").strip().lower() == "footage":
        return True
    intents = (shot_need or {}).get("visual_intents")
    if not isinstance(intents, dict):
        return False
    if str(intents.get("preferred_media_type") or "").strip().lower() == "video":
        return True
    return str(
        intents.get("primary_visual_intent") or ""
    ).strip().lower() in _INTENT_VIDEO_INTENTS


def _apply_video_preference(pool, scenario, shot_need):
    """AM-004 (revised): stable epsilon-group reorder of the accepted
    pool — within a run of candidates whose effective scores are
    comparable (within VIDEO_PREFERENCE_EPSILON of the group's best),
    verified videos come before verified stills. Candidates outside
    the window never move; the reorder is stable, so relative order
    inside each kind is preserved. Every candidate whose position
    changed is stamped video_preference_applied."""
    if len(pool) < 2 or not _beat_wants_video(scenario, shot_need):
        return pool

    def _score(item):
        for key in ("_effective_score", "_selection_score"):
            value = item.get(key)
            if value is not None:
                return value
        return item.get("universal_score") or 0

    def _is_video(item):
        if item.get("_pool_kind"):
            return item.get("_pool_kind") == "video"
        return (item.get("type") or item.get("visual_type")) == "video"

    ordered = []
    group = []

    def _flush():
        if not group:
            return
        videos = [i for i in group if _is_video(i)]
        stills = [i for i in group if not _is_video(i)]
        if videos and stills:
            reordered = videos + stills
            before = {id(i): n for n, i in enumerate(group)}
            for n, item in enumerate(reordered):
                if before[id(item)] != n:
                    item["video_preference_applied"] = True
            ordered.extend(reordered)
        else:
            ordered.extend(group)
        group.clear()

    for item in pool:
        if group and abs(_score(group[0]) - _score(item)) > VIDEO_PREFERENCE_EPSILON:
            _flush()
        group.append(item)
    _flush()
    return ordered


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

    # Phase 3 / AM-004: within comparable effective scores, a beat
    # that wants video prefers a verified video over a verified
    # still. Ranking only — the pool is unchanged in membership.
    pool = _apply_video_preference(pool, scenario, shot_need)

    picked = []
    source_counts = {}
    picked_pool_ids = set()

    for _rank, candidate in enumerate(pool):
        # Stage 6 / ASSET-016: every pool candidate leaves this loop
        # with a recorded selection outcome — picked, blocked with
        # its reason, or (below, after the loop) marked as never
        # reached. The rank is the candidate's position in the
        # effective-score order it was considered in.
        candidate["selection_rank"] = _rank
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

        key = _media_pipeline()._uve_candidate_key(candidate)

        if not key:
            # Was a silent skip: a verified candidate vanished with
            # no recorded reason (ASSET-016).
            candidate["reject_reason"] = (
                rejection_diagnostics.MISSING_KEY_REASON
            )
            candidate["rejection_stage"] = "selection"
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
                title=_media_pipeline().result_title(candidate),
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
        picked_pool_ids.add(id(candidate))

        source_counts[source_key] = source_counts.get(source_key, 0) + 1

        if len(picked) >= limit:
            break

    # Stage 6 / ASSET-016: the pick-loop remainder. Candidates the
    # loop never reached (the pick limit filled first) were verified
    # and eligible but carry no outcome; stamp the truthful reason
    # so the asset report can explain why they were not picked.
    # Candidates examined and blocked already carry their reason.
    for _rank, candidate in enumerate(pool):
        if id(candidate) in picked_pool_ids:
            continue
        if candidate.get("reject_reason"):
            continue
        candidate.setdefault("selection_rank", _rank)
        candidate["reject_reason"] = rejection_diagnostics.PICK_LIMIT_REASON
        candidate["rejection_stage"] = "selection"

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

def _uve_prepare_sentence_result(sentence, sentence_number, images, videos, story_people):
    sentence_text = _media_pipeline().get_sentence_text(sentence)
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

def _uve_selection_block_reason(candidate, registry):
    """The selection-eligibility checks _uve_pick applies to a
    candidate it reaches (per-source usage cap, candidate key,
    registry can_use), as a standalone predicate for the coverage
    pass. Returns the registered rejection reason when the candidate
    is blocked, else None. Mirrors _uve_pick exactly — the coverage
    pass may only ever draw a candidate the primary pick itself
    could have taken."""
    candidate_url = str(
        candidate.get("url")
        or candidate.get("video_url")
        or candidate.get("link")
        or ""
    ).strip()
    try:
        existing_uses = registry.source_usage_count(candidate_url)
    except Exception:
        existing_uses = 0
    if existing_uses >= registry.max_source_uses:
        return "source_usage_limit"
    key = _media_pipeline()._uve_candidate_key(candidate)
    if not key:
        return rejection_diagnostics.MISSING_KEY_REASON
    try:
        allowed, why = registry.can_use(
            url=key,
            title=_media_pipeline().result_title(candidate),
            allow_source_reuse=True,
        )
    except Exception:
        allowed, why = True, ""
    if not allowed:
        return why or "registry_block"
    return None


def _uve_beat_distinct_asset_keys(beat_videos, beat_images):
    """Distinct on-screen identities among a beat's fetched assets,
    at the ladder's SEGMENT level — the coverage arithmetic's unit,
    matching the timeline cap it feeds (Stage 7: two windows of
    one source are two cards with separate allowances).

    Phase 5 / AM-017: resolved through services.visual_identity —
    the ONE distinct-visual definition (canonical/segment ladder
    plus content-hash unification for stills). The ladder's
    fallback tier keeps the historical order (candidate key, then
    file path, then candidate id) for records with no source
    identity."""
    assets = [
        asset
        for asset in list(beat_videos or []) + list(beat_images or [])
        if isinstance(asset, dict)
    ]
    if not assets:
        return set()
    groups = visual_identity.resolve_identity_groups(
        assets,
        fallback_of=lambda _index, _asset: str(
            _media_pipeline()._uve_candidate_key(_asset)
            or _asset.get("file_path")
            or _asset.get("candidate_id")
            or f"fetched:{_index}"
        ),
    )
    return set(groups)


def _uve_beat_distinct_asset_level_keys(beat_videos, beat_images):
    """Distinct fetched ASSETS at the ladder's ASSET level — the
    variety pass's unit (AM-003 draws distinct ASSETS toward the
    FP-4 target, and AM-017's headline counts assets: one clip is
    one visual however many of its windows a beat fetched)."""
    assets = [
        asset
        for asset in list(beat_videos or []) + list(beat_images or [])
        if isinstance(asset, dict)
    ]
    if not assets:
        return set()
    groups = visual_identity.resolve_asset_groups(
        assets,
        fallback_of=lambda _index, _asset: str(
            _media_pipeline()._uve_candidate_key(_asset)
            or _asset.get("file_path")
            or _asset.get("candidate_id")
            or f"fetched:{_index}"
        ),
    )
    return set(groups)


# Run-11 fix 3: the per-beat download-attempt cap for ONE
# coverage/variety draw session (a single _uve_search_and_pick
# pass over one beat: its primary-reserve draw plus its
# tier-escalation draw). A beat with a verified pool keeps drawing
# — a failed fetch never ends the draw the way a successful pick
# ends the primary fetch — until coverage is met, the pool is
# genuinely exhausted, a D-B2 budget binds, or this cap binds.
# The cap exists so a pathological pool of dead URLs cannot loop
# a beat's draw forever; when it binds, the stop is recorded on
# the coverage record (STOP_BEAT_DOWNLOAD_ATTEMPT_CAP), never
# silent. 16 is far above any legitimate beat need (run 11's
# hungriest beat drew 9 across a whole pass) and far below the
# run's 105 total attempts.
COVERAGE_DRAW_MAX_ATTEMPTS = 16
STOP_BEAT_DOWNLOAD_ATTEMPT_CAP = "beat_download_attempt_cap"


def _uve_coverage_shortfall(sentence_count, distinct_asset_count):
    """Additional DISTINCT assets a beat must fetch so the timeline
    can cover every one of its sentences under the segment-use cap
    (services.timeline TIMELINE_MAX_SEGMENT_USES per distinct
    asset), without relying on own-beat spanning. Zero when the
    fetched assets already suffice. This is the coverage pass's
    only trigger arithmetic — conservative by design: scene-level
    sharing with sibling beats can only add coverage the beat-local
    estimate does not count."""
    cap = timeline_service.TIMELINE_MAX_SEGMENT_USES
    covered = cap * max(int(distinct_asset_count or 0), 0)
    shortfall_sentences = int(sentence_count or 0) - covered
    if shortfall_sentences <= 0:
        return 0
    return -(-shortfall_sentences // cap)
