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


def event_id_for(scene, shot_index, candidate_id):
    """Stable event id: hash of scene + shot + canonical candidate id."""
    raw = f"{scene}|{shot_index}|{candidate_id}"
    return "evt_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


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
    specificity="",
    category="",
    matched_query="",
    shot_index=None,
    beat_id=None,
    sentence_numbers=None,
):
    """One evaluation-event record (the candidate_records schema).

    ``reason`` is the PRIMARY rejection reason for rejected events
    ("" otherwise). ``verdict_reasons`` / ``contributions`` are the
    contributing evidence — non-additive diagnostics only."""
    if score_basis is None:
        if stage == "metadata_gate":
            score_basis = "gate_pre_scoring"
        elif score is not None:
            score_basis = "verifier_score"
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
        "specificity": specificity,
        "category": category,
        "matched_query": matched_query,
        "shot_index": shot_index,
        "beat_id": beat_id,
        "sentence_numbers": list(sentence_numbers or []),
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
    - ``contributing_reasons``: NON-ADDITIVE counts of the verifier's
      recorded reasons across rejected scoring events. Never sum
      these into the primary funnel.
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
        for verdict_reason in record.get("verdict_reasons") or []:
            _bump(contributing, verdict_reason)

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
