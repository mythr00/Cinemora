"""Global visual assignment with opportunity cost (Master
Production Recovery, Phase 3).

Phase 0 root cause 3: assignment was greedy per sentence in scene
order — each sentence's pick consumed sources and segments before
later sentences were even searched, there was no global pass and no
opportunity cost, so a sentence with abundant alternatives could
take the only asset another sentence needed.

This module is the global pass. It is PURE: it consumes per-unit
verified pools (already searched and verified by the existing
discovery path) and returns a plan; it searches nothing, verifies
nothing and downloads nothing. The caller applies the plan to the
real pick (``pipeline_assignment._uve_pick`` still enforces every
cap and registry rule, so a plan the live state contradicts
degrades gracefully instead of forcing a bad assignment).

Algorithm (deterministic; same pools -> same plan):

- A *unit* is one shot-need pass of one beat: ``{key, candidates}``
  where each candidate is ``{identity, source_key, score}`` in pool
  (relevance) order. ``identity`` is the candidate's durable
  identity (candidate_id, falling back to its canonical key);
  ``source_key`` its canonical source.
- ``demand(c)`` = number of units whose pool contains ``c`` — how
  contested the asset is.
- ``flexibility(u)`` = number of distinct identities in ``u``'s
  pool — how many alternatives the unit has.
- ``regret(u)`` = best score in ``u`` minus the best score among
  candidates from a DIFFERENT source than the best — what the unit
  loses if its best source is taken.
- Units are processed most-constrained first (flexibility asc,
  regret desc, original order): the unit with no alternatives
  claims its best before a flexible unit can take it.
- Within a unit, candidates are chosen by *adjusted score* =
  ``score - OPPORTUNITY_EPSILON * (demand - 1)``: a candidate three
  units need is discounted against an equally relevant candidate
  only one unit needs. The epsilon (0.02, the pipeline's existing
  tie-break scale) can reorder near-ties toward abundance; it can
  never make a clearly worse asset beat a clearly better one.
- A candidate identity is assigned to at most ONE unit; a unit that
  loses a candidate this way has it recorded in its ``reserved``
  map (identity -> winning unit key) so the pick can stamp the
  truthful reason and the sentence stays distinguishable from a
  genuine discovery gap (acceptance #7, assignment half).
- Source capacity is respected at plan level (``source_cap`` minus
  ``source_usage`` already committed): a unit is never planned a
  fourth use of a source the registry would refuse anyway. The
  live registry re-checks at pick time regardless.
"""

from __future__ import annotations


#: Opportunity-cost discount per additional unit demanding a
#: candidate. Deliberately the pipeline's tie-break scale (0.02):
#: large enough to break near-ties toward abundant assets, far too
#: small to override a real relevance gap.
OPPORTUNITY_EPSILON = 0.02

#: Default per-unit pick limit — mirrors _uve_pick's limit.
DEFAULT_LIMIT = 3


def _unit_stats(candidates):
    best = None
    for cand in candidates:
        if best is None or cand["score"] > best["score"]:
            best = cand
    flexibility = len({c["identity"] for c in candidates})
    if best is None:
        return flexibility, 0.0
    other_scores = [
        c["score"] for c in candidates
        if c["source_key"] != best["source_key"]
    ]
    regret = best["score"] - (max(other_scores) if other_scores else 0.0)
    return flexibility, regret


def plan_assignments(units, *, limit=DEFAULT_LIMIT, source_cap=None,
                     source_usage=None):
    """Compute the global assignment plan for one scene's units.

    ``units``: list of ``{"key": hashable, "candidates": [...]}``
    in scene order. Returns ``{key: {"picks": [identity, ...],
    "reserved": {identity: winning_key}, "contended": int}}`` —
    ``picks`` in the order the unit should take them, ``reserved``
    the identities this unit loses to another unit.
    """
    source_usage = dict(source_usage or {})
    planned_source_uses = {}

    demand = {}
    for unit in units:
        for identity in {c["identity"] for c in unit["candidates"]}:
            demand[identity] = demand.get(identity, 0) + 1

    def _source_available(source_key):
        if source_cap is None:
            return True
        used = source_usage.get(source_key, 0)
        planned = planned_source_uses.get(source_key, 0)
        return used + planned < source_cap

    ordered = []
    for index, unit in enumerate(units):
        flexibility, regret = _unit_stats(unit["candidates"])
        ordered.append((flexibility, -regret, index, unit))
    ordered.sort(key=lambda entry: (entry[0], entry[1], entry[2]))

    assigned = {}  # identity -> unit key
    plan = {}
    for _flex, _neg_regret, _index, unit in ordered:
        key = unit["key"]
        ranked = sorted(
            enumerate(unit["candidates"]),
            key=lambda pair: (
                -(
                    pair[1]["score"]
                    - OPPORTUNITY_EPSILON
                    * (demand.get(pair[1]["identity"], 1) - 1)
                ),
                -pair[1]["score"],
                pair[0],
            ),
        )
        picks = []
        reserved = {}
        for _pool_index, cand in ranked:
            identity = cand["identity"]
            if len(picks) >= limit:
                break
            winner = assigned.get(identity)
            if winner is not None and winner != key:
                reserved[identity] = winner
                continue
            if not _source_available(cand["source_key"]):
                continue
            picks.append(identity)
            assigned[identity] = key
            planned_source_uses[cand["source_key"]] = (
                planned_source_uses.get(cand["source_key"], 0) + 1
            )
        plan[key] = {
            "picks": picks,
            "reserved": reserved,
            "contended": len(reserved),
        }

    # Units with no candidates (or absent from the ordering for any
    # reason) still get an explicit empty plan entry: "planned,
    # nothing available" is a recorded outcome, not an absence.
    for unit in units:
        plan.setdefault(
            unit["key"], {"picks": [], "reserved": {}, "contended": 0}
        )
    return plan
