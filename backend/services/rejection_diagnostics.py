"""Stage 1 / R5: structured rejection & acceptance diagnostics.

Implements the Master Architecture Review Deliverable 6 (C.A)
design on the existing artefacts — no schema migration:

- The source of truth is the per-candidate record list in
  ``asset_report.json -> candidate_records.records``. Records are
  EVALUATION EVENTS, not distinct candidates: the same candidate is
  evaluated once per shot need it is retrieved for, so event counts
  and distinct-candidate counts are always reported separately and
  never mixed into one denominator.
- Every rejected event carries exactly ONE primary rejection reason
  (the terminating stage's reason, in pipeline execution order:
  gate -> scoring -> selection -> segment -> download -> transcript
  -> technical). Primary-reason counts are ADDITIVE: they sum
  exactly to the number of rejected events.
- Contributing evidence (the verifier's ``reasons`` list and score
  ``contributions``) is recorded on the event for inspection and is
  aggregated NON-additively in a separate block — it is never summed
  into the primary funnel.
- Gate rejections carry ``score_basis="gate_pre_scoring"`` with
  score 0.0: the candidate was rejected before scoring existed, and
  the record must not pretend otherwise.
- The distinct-candidate view deduplicates events by candidate_id
  and keeps each candidate's BEST outcome under the precedence
  accepted > verified > download_failed > rejected.

The ledger (run_ledger.record_decisions) consumes the same records:
rejected rows carry the event's primary reason, stage and score in
the existing ``verification`` JSON column — no new column, no new
table.

Stage 6 / R5 completion adds:

- ``contributing_reasons`` on every event built from live pipeline
  data: the OTHER checks that independently failed for the same
  candidate, in execution order (gate collect-all failures for gate
  events; the verifier's ``failed_checks`` for scoring events).
  Decisions are unchanged — the primary (first, in execution order)
  reason still decides; contributing reasons are diagnostics only
  and are aggregated in their own non-additive block.
- ``dimension_results``: per-dimension pass/fail derived from the
  verifier's contributions for subject/topical (and era when the
  scenario carries an era); ``not_evaluated`` where a dimension was
  never scored — never inferred.
- ``segment_evidence`` / ``segment_failure_cause`` /
  ``transcript_check`` where the pipeline produced them.
- Selection-stage events for EVERY verified-but-not-picked
  candidate (ASSET-016): the pick loop stamps why — a registry/cap
  block, or ``pick_limit_reached`` for the pool remainder the loop
  never reached because the shot's pick slots were already filled
  by higher-ranked candidates.
- ``REJECTION_REASON_REGISTRY``: the closed reason vocabulary
  (Architecture Review C.A.4, consolidated — every string a live
  stamp site can emit, with the stages it can terminate). The
  retired score-below alias is deliberately absent:
  the verifier's reason is ``below_threshold``.
- ``recompute_stored_funnel``: the C.A.6 historical recompute for
  stored productions whose reports predate this schema.
"""

import hashlib

# Pipeline execution order of the stages that can terminate a
# candidate's evaluation. Used only to document which reason is
# "primary" (the one emitted by the terminating stage); events are
# stamped with their stage at the point of rejection.
STAGE_ORDER = (
    "dedupe",
    "metadata_gate",
    "scoring",
    "selection",
    "segment_evidence",
    "download",
    "transcript_verification",
    "technical_validation",
    "verification",
    "accepted",
)

OUTCOME_PRECEDENCE = {
    "rejected": 0,
    "download_failed": 1,
    "verified": 2,
    "accepted": 3,
}

# Closed vocabulary extension for drops that previously left no
# record at all (see _uve_verify_all's dedupe).
DEDUPE_REASON = "duplicate_candidate"
MISSING_KEY_REASON = "missing_candidate_key"

# Stage 6 / ASSET-016: the reason recorded for a verified candidate
# the pick loop never reached — the shot's pick slots (limit in
# _uve_pick) were filled by higher-ranked candidates first. The
# candidate was eligible; it was not examined for cap/registry
# blocks, and the record must not pretend otherwise.
PICK_LIMIT_REASON = "pick_limit_reached"

# ---------------------------------------------------------------------------
# Closed rejection-reason vocabulary (Architecture Review C.A.4).
#
# Every reason string a live stamp site can place on a candidate
# evaluation event, mapped to the stage(s) it can terminate. The
# vocabulary is consolidated from the code as found — gate
# (media_pipeline.gate_shot_need), scoring
# (story_understanding.verify_candidate), selection (_uve_pick +
# visual_registry.can_use), segment/download/technical (the
# downloaders' _download_error stamps and _uve_fetch_first —
# including the image path's rate_limited / http_error /
# undersized_file / decode_failed codes, which the review's C.A.4
# list missed), transcript verification, and this module's dedupe
# reasons — plus PICK_LIMIT_REASON, added by Stage 6 for ASSET-016.
# The retired
# parallel-counter score-below alias is NOT in the registry:
# the verifier's reason is "below_threshold", and the
# alias's drift from it was one of the defects C.A.3.4 names.
# ---------------------------------------------------------------------------
REJECTION_REASON_REGISTRY = {
    # dedupe (this module, via _uve_verify_all)
    "duplicate_candidate": ("dedupe",),
    "missing_candidate_key": ("dedupe", "selection"),
    # metadata gate (gate_shot_need)
    "banned_provider": ("metadata_gate", "scoring"),
    "foreign_provenance": ("metadata_gate", "scoring"),
    "contradictory_date": ("metadata_gate", "scoring"),
    "contradictory_person": ("metadata_gate", "scoring"),
    "contradictory_place": ("metadata_gate", "scoring"),
    "contradictory_event": ("scoring",),
    "forbidden_term": ("metadata_gate",),
    "missing_person": ("metadata_gate",),
    "missing_subject": ("metadata_gate",),
    "missing_place": ("metadata_gate",),
    # scoring (verify_candidate)
    "missing_required_subject": ("scoring",),
    "no_topical_evidence": ("scoring",),
    "below_threshold": ("scoring",),
    # selection (_uve_pick / VisualRegistry.can_use)
    "source_usage_limit": ("selection",),
    "already_used_file": ("selection",),
    "already_used_clip": ("selection",),
    "segment_overlap": ("selection",),
    "already_used_source": ("selection",),
    "recent_source": ("selection",),
    "already_used_title": ("selection",),
    "registry_block": ("selection",),
    "pick_limit_reached": ("selection",),
    # segment evidence / download (downloaders, _uve_fetch_first)
    "no_url": ("segment_evidence", "download"),
    "no_segment_evidence": ("segment_evidence", "download"),
    "download_failed": ("download",),
    "previously_failed_this_run": ("download",),
    "invalid_media": ("download",),
    "probe_failed": ("download",),
    "rate_limited": ("download",),
    "http_error": ("download",),
    # technical validation
    "technical_below_minimum": ("technical_validation",),
    "undersized_file": ("technical_validation",),
    "decode_failed": ("technical_validation",),
    # transcript verification (Stage 3B demotion)
    "transcript_mismatch": ("transcript_verification",),
}

# Reason families stamped with a dynamic suffix (the downloaders
# stamp f"download_exception: {exc}"). Membership is by prefix.
REJECTION_REASON_PREFIXES = ("download_exception:",)


def is_registered_reason(reason):
    """Whether a reason string belongs to the closed vocabulary."""
    text = str(reason or "")
    if not text:
        return False
    if text in REJECTION_REASON_REGISTRY:
        return True
    return any(text.startswith(p) for p in REJECTION_REASON_PREFIXES)


def event_id_for(scene, shot_index, candidate_id):
    """Stable event id: hash of scene + shot + canonical candidate id."""
    raw = f"{scene}|{shot_index}|{candidate_id}"
    return "evt_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _dimension_results(contributions, stage, era_applicable):
    """Per-dimension pass/fail from the verifier's contributions.

    Only subject/topical/era are derivable (C.A.2); era is
    ``not_evaluated`` when the scenario carries no era to judge
    against. A gate event was never scored, so every dimension is
    ``not_evaluated`` — never inferred from the gate's own checks.
    Returns None when nothing truthful can be said (unscored,
    non-gate events)."""
    if isinstance(contributions, dict):
        return {
            "subject": (
                "pass" if (contributions.get("subject") or 0) > 0 else "fail"
            ),
            "topical": (
                "pass" if (contributions.get("topical") or 0) > 0 else "fail"
            ),
            "era": (
                ("pass" if (contributions.get("era") or 0) > 0 else "fail")
                if era_applicable
                else "not_evaluated"
            ),
        }
    if stage == "metadata_gate":
        return {
            "subject": "not_evaluated",
            "topical": "not_evaluated",
            "era": "not_evaluated",
        }
    return None


# ---------------------------------------------------------------------------
# B-roll tier vocabulary (Intelligent B-Roll System, B0 — plan §3.1/§6.1)
# ---------------------------------------------------------------------------
#: The closed tier vocabulary. A tier names HOW a visual relates to
#: the sentence it illustrates: ``direct`` (the actual subject /
#: place / event), ``related`` (the story's real places/objects in a
#: related context), ``activity_broll`` (the sentence's activity,
#: performed by anyone plausible), ``atmospheric`` (place-type +
#: era + mood) and ``graphic`` (a generated motion graphic, never
#: footage). B0 stamps tiers on today's pipeline only — every search
#: the pipeline runs today is a direct (T1) search, so today's
#: events, assets and items are stamped ``direct`` (graphics
#: ``graphic``); the escalation tiers arrive with B2/B3.
TIERS = ("direct", "related", "activity_broll", "atmospheric", "graphic")

#: The tiers that count as B-roll for the §6.2 share (T3 + T4).
BROLL_TIERS = ("activity_broll", "atmospheric")


def normalise_tier(value, default="direct"):
    """A recorded tier, or ``default`` when the value is absent or
    outside the closed vocabulary. Never invents a tier: legacy
    records without a stamp resolve to the caller's default."""
    return value if value in TIERS else default


def build_event(
    *,
    candidate_id,
    url="",
    provider="unknown",
    kind="",
    outcome,
    stage,
    reason="",
    score=None,
    score_basis=None,
    contributions=None,
    verdict_reasons=None,
    contributing_reasons=None,
    dimension_results=None,
    era_applicable=False,
    segment_evidence=None,
    segment_failure_cause=None,
    transcript_check=None,
    specificity="",
    category="",
    matched_query="",
    shot_index=None,
    beat_id=None,
    sentence_numbers=None,
    segment_id=None,
    tier="direct",
):
    """One evaluation-event record (the candidate_records schema).

    ``reason`` is the PRIMARY rejection reason for rejected events
    ("" otherwise). ``contributing_reasons`` is the machine-readable
    list of OTHER checks that independently failed for this event
    (Stage 6 collect-all diagnostics; None when the producing path
    could not evaluate them, e.g. legacy callers).
    ``verdict_reasons`` / ``contributions`` are the contributing
    evidence — non-additive diagnostics only."""
    if score_basis is None:
        if stage == "metadata_gate":
            score_basis = "gate_pre_scoring"
        elif score is not None:
            score_basis = "verifier_score"
    if dimension_results is None:
        dimension_results = _dimension_results(
            contributions, stage, era_applicable
        )
    return {
        "candidate_id": candidate_id,
        "url": url,
        "provider": provider,
        "kind": kind,
        "outcome": outcome,
        "stage": stage,
        "reason": reason,
        "primary_rejection_reason": reason if outcome == "rejected" else "",
        "score": score,
        "score_basis": score_basis,
        "contributions": contributions,
        "verdict_reasons": list(verdict_reasons or []),
        "contributing_reasons": (
            list(contributing_reasons)
            if contributing_reasons is not None
            else None
        ),
        "dimension_results": dimension_results,
        "segment_evidence": segment_evidence,
        # Stage 7 / R8 (C.D.3): evaluation events reference the
        # segment they concern, so segment-level provenance and
        # reuse counts can be derived across runs. None when the
        # event predates segment selection (gate/scoring events are
        # truthfully segment-less — identity is minted at download).
        "segment_id": segment_id,
        "segment_failure_cause": segment_failure_cause,
        "transcript_check": transcript_check,
        "specificity": specificity,
        "category": category,
        "matched_query": matched_query,
        "shot_index": shot_index,
        "beat_id": beat_id,
        "sentence_numbers": list(sentence_numbers or []),
        # B-roll B0 (§6.1): the tier of the need this candidate was
        # evaluated for, so the funnel can be read per tier. Every
        # search the pipeline runs today is a direct search; the
        # escalation tiers (B2/B3) stamp their own.
        "tier": normalise_tier(tier),
    }


def _bump(counter, key, amount=1):
    key = str(key or "unknown")
    counter[key] = counter.get(key, 0) + amount


def aggregate_funnel(records):
    """Aggregate evaluation-event records into the rejection funnel.

    Returns a dict with:
    - ``events``: total event count and counts by outcome.
    - ``primary_rejections``: ADDITIVE counts by stage, reason,
      provider, kind, specificity and category. The by-reason total
      sums exactly to the number of rejected events.
    - ``contributing_reasons``: NON-ADDITIVE counts of the other
      independently-failed checks across rejected events (the
      record's machine-readable ``contributing_reasons`` list;
      records built before Stage 6 fall back to their
      ``verdict_reasons`` evidence list). Never sum these into the
      primary funnel.
    - ``distinct_candidates``: one row per candidate_id with its best
      outcome (precedence above) and the counts by best outcome.
    """
    records = list(records or [])
    by_outcome = {}
    primary = {
        "total": 0,
        "by_stage": {},
        "by_reason": {},
        "by_stage_reason": {},
        "by_provider": {},
        "by_kind": {},
        "by_specificity": {},
        "by_category": {},
    }
    contributing = {}
    best = {}

    for record in records:
        outcome = str(record.get("outcome") or "unknown")
        _bump(by_outcome, outcome)
        cid = record.get("candidate_id")
        if cid:
            current = best.get(cid)
            if current is None or (
                OUTCOME_PRECEDENCE.get(outcome, -1)
                > OUTCOME_PRECEDENCE.get(current, -1)
            ):
                best[cid] = outcome
        if outcome != "rejected":
            continue
        primary["total"] += 1
        stage = str(record.get("stage") or "unknown")
        reason = str(
            record.get("primary_rejection_reason")
            or record.get("reason")
            or "unknown"
        )
        _bump(primary["by_stage"], stage)
        _bump(primary["by_reason"], reason)
        _bump(primary["by_stage_reason"], f"{stage}:{reason}")
        _bump(primary["by_provider"], record.get("provider"))
        _bump(primary["by_kind"], record.get("kind"))
        _bump(primary["by_specificity"], record.get("specificity"))
        _bump(primary["by_category"], record.get("category"))
        contributing_list = record.get("contributing_reasons")
        if not isinstance(contributing_list, list):
            # Pre-Stage-6 records carry only the verifier's prose
            # evidence list; it is the contributing evidence they
            # have, and it is counted as-is rather than dropped.
            contributing_list = record.get("verdict_reasons") or []
        for contributing_reason in contributing_list:
            _bump(contributing, contributing_reason)

    distinct_by_outcome = {}
    for outcome in best.values():
        _bump(distinct_by_outcome, outcome)

    return {
        "events": {"total": len(records), "by_outcome": by_outcome},
        "primary_rejections": primary,
        "contributing_reasons": contributing,
        "distinct_candidates": {
            "total": len(best),
            "by_best_outcome": distinct_by_outcome,
        },
    }


def primary_reason_counts(records):
    """Additive primary-reason counts over rejected events.

    The single derivation every consumer (asset report headline
    counter, per-scene stats view) must use, so no parallel counting
    path can drift from the records again (C.A.3.4)."""
    counts = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        if record.get("outcome") != "rejected":
            continue
        reason = (
            record.get("primary_rejection_reason")
            or record.get("reason")
            or "unknown"
        )
        _bump(counts, reason)
    return counts


def recompute_stored_funnel(asset_report, scene_reviews):
    """C.A.6 historical recompute for one stored production.

    ``asset_report`` is the production's stored asset_report dict;
    ``scene_reviews`` maps scene number -> the parsed
    scene_<n>_review.json list (per-beat entries carrying their own
    shot_needs and per-shot records). Returns the C.A.5-shaped
    funnel report for the production with every field that the
    stored artefacts cannot support flagged in ``not_recoverable``
    and the stored counters published next to the records-derived
    ones in ``counter_discrepancies`` — discrepancies are reported,
    never resolved by fiat.
    """
    report = asset_report if isinstance(asset_report, dict) else {}
    records = (
        (report.get("candidate_records") or {}).get("records") or []
    )
    funnel = aggregate_funnel(records)
    derived_reasons = primary_reason_counts(records)

    # Per-shot slice: review records joined to the shot needs the
    # same review entries carry (specificity / shot_type /
    # required_subject / place / era survive there).
    by_shot_type_reason = {}
    by_specificity_reason = {}
    by_shot_type_specificity_reason = {}
    joined_rejected = 0
    for scene_number, entries in (scene_reviews or {}).items():
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            needs = {
                need.get("shot_index"): need
                for need in entry.get("shot_needs") or []
                if isinstance(need, dict)
            }
            for shot in entry.get("records") or []:
                if not isinstance(shot, dict):
                    continue
                need = needs.get(shot.get("shot_index")) or {}
                shot_type = str(need.get("shot_type") or "unknown")
                specificity = str(need.get("specificity") or "unknown")
                for rejected in shot.get("rejected") or []:
                    if not isinstance(rejected, dict):
                        continue
                    reason = str(rejected.get("reason") or "unknown")
                    joined_rejected += 1
                    _bump(by_shot_type_reason, f"{shot_type}:{reason}")
                    _bump(
                        by_specificity_reason, f"{specificity}:{reason}"
                    )
                    _bump(
                        by_shot_type_specificity_reason,
                        f"{shot_type}|{specificity}|{reason}",
                    )

    stored_reasons = report.get("rejection_reasons") or {}
    provider_funnel = (
        (report.get("providers") or {}).get("funnel") or {}
    )
    provider_discovered = sum(
        int(counts.get("discovered") or 0)
        for counts in provider_funnel.values()
        if isinstance(counts, dict)
    )
    return {
        "shape": "c.a.5_recompute_v1",
        "events_total": len(records),
        "rejection_funnel": funnel,
        "per_shot": {
            "joined_rejected_candidates": joined_rejected,
            "by_shot_type_reason": by_shot_type_reason,
            "by_specificity_reason": by_specificity_reason,
            "by_shot_type_specificity_reason": (
                by_shot_type_specificity_reason
            ),
            "note": (
                "Shot attribution comes from the review files' own "
                "shot_needs join; candidate kind per record was not "
                "stored and is NOT inferred here (the funnel's "
                "by_kind therefore reads 'unknown' for these runs)."
            ),
        },
        "counter_discrepancies": {
            "stored_rejection_reasons": dict(stored_reasons),
            "stored_rejection_reasons_total": sum(
                int(v) for v in stored_reasons.values()
                if isinstance(v, (int, float))
            ),
            "records_derived_rejection_reasons": derived_reasons,
            "records_derived_total": sum(derived_reasons.values()),
            "stored_total_retrieved_assets": report.get(
                "total_retrieved_assets"
            ),
            "candidate_records_total": len(records),
            "provider_funnel_discovered_total": provider_discovered,
            "note": (
                "The three headline counters (provider-funnel "
                "discovered / candidate records / stored "
                "total_retrieved_assets) were produced by different "
                "code paths with different scopes; their scopes were "
                "never defined in one place. The recompute publishes "
                "all three, unresolved (C.A.6)."
            ),
        },
        "not_recoverable": [
            "scores and contributions (never persisted for these runs)",
            "contributing reasons (first-fail recording destroyed them at source)",
            "segment attempts/failure causes and transcript-check outcomes for rejected candidates",
            "which query surfaced a candidate (review files list queries per shot, not per candidate)",
            "candidate kind per record (not stored; not inferred)",
        ],
    }
