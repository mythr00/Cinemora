"""Coverage assessment for asset_manager stage adoption (C-01).

Stage adoption (services.stage_resume) historically validated only
artefact hashes: a "completed" asset_manager stage whose coverage
was defective — in the Petito case 58/76 downloads failed and
sentence 23 was never searched, because the shared escalation
download budget was exhausted before its scene was reached — was
adopted by the retry as healthy, and the run then failed at the
video_editor coverage gate with no asset-stage retry left.

This module is the coverage side of adoption. It reads a stage
payload's ``processed_scenes`` (the same shape the pipeline
produces and the asset report consumes) and classifies EVERY
sentence the stage was responsible for:

- ``covered`` — the sentence has an assigned asset (its sentence
  result carries media, or a scene asset is stamped with its
  number);
- ``graphics_fallback`` — no footage, but a graphics fallback
  decision is recorded for the sentence (a missing_visuals entry
  whose outcome routes it to generated graphics, or an explicit
  graphics decision stamp);
- ``unresolved_searched`` — no asset, but an explicit unresolved
  marker exists AND the record shows a search was actually
  attempted for it (a missing_visuals entry whose tier attempts
  executed queries, a ``no_verified_asset`` entry — that reason is
  only written after the primary search plus recovery rounds — or
  a coverage-pool-exhaustion record);
- ``starved`` — an unresolved marker exists but the search was
  never actually attempted: every recorded tier attempt ended
  ``budget_exhausted`` with ZERO executed queries, or the entry is
  an unsearched-role entry whose ladder entry never executed;
- ``unrecorded`` — the sentence appears in the stage's sentence /
  beat records with no asset, no fallback and no unresolved
  marker at all: it was silently dropped.

Adoption is coverage-complete iff no sentence is ``starved`` or
``unrecorded``. Hash validation stays in stage_resume; this is the
additional gate, not a replacement.
"""

from __future__ import annotations

COVERED = "covered"
GRAPHICS_FALLBACK = "graphics_fallback"
UNRESOLVED_SEARCHED = "unresolved_searched"
STARVED = "starved"
UNRECORDED = "unrecorded"

COMPLETE_STATUSES = frozenset({COVERED, GRAPHICS_FALLBACK, UNRESOLVED_SEARCHED})
INCOMPLETE_STATUSES = frozenset({STARVED, UNRECORDED})

_BUDGET_STOP_REASONS = frozenset({
    "production_download_budget",
    "production_search_budget",
    "tier_download_budget",
    "tier_query_budget",
    "wall_clock",
})


def _sentence_number(value):
    if isinstance(value, dict):
        for key in ("sentence_number", "number", "sentence_index"):
            raw = value.get(key)
            if raw is not None:
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    continue
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _attempt_executed_queries(attempt):
    """How many queries a tier attempt actually EXECUTED. An
    attempt whose query list is empty executed none — whatever its
    outcome claims. (The Petito sentence-23 attempt recorded
    outcome budget_exhausted with an empty query list.)"""
    if not isinstance(attempt, dict):
        return 0
    queries = attempt.get("queries")
    return len(queries) if isinstance(queries, list) else 0


def _attempt_was_starved(attempt):
    if not isinstance(attempt, dict):
        return False
    if _attempt_executed_queries(attempt) > 0:
        return False
    outcome = str(attempt.get("outcome") or "")
    stop = str(attempt.get("stop_reason") or "")
    return outcome == "budget_exhausted" or stop in _BUDGET_STOP_REASONS


def _entry_search_evidence(entry):
    """(searched, starved) for one missing_visuals entry."""
    attempts = entry.get("tier_attempts") or []
    if attempts:
        if any(_attempt_executed_queries(a) > 0 for a in attempts):
            return True, False
        if any(_attempt_was_starved(a) for a in attempts):
            return False, True
        # Attempts recorded but none executed and none explicitly
        # budget-stopped (e.g. no_concepts): the ladder was entered
        # and decided — that is an attempted search.
        return True, False
    reason = str(entry.get("reason") or "")
    if reason == "no_verified_asset":
        # Written only after the primary search and the bounded
        # coverage-recovery rounds ran for the beat.
        return True, False
    if entry.get("coverage_pool_exhausted"):
        return True, False
    if reason == "unsearched_role":
        # An unsearched-role entry with no tier attempts recorded
        # never entered the ladder — nothing was attempted.
        return False, True
    # A bare explicit unresolved marker with no contrary evidence:
    # the marker itself is the record (legacy payloads).
    return True, False


def _is_graphics_fallback(entry):
    if not isinstance(entry, dict):
        return False
    if entry.get("graphics_fallback") or entry.get("graphic_target"):
        return True
    if str(entry.get("coverage_outcome") or "") == "covered_by_graphic":
        return True
    if str(entry.get("decision") or "") in ("graphic", "graphics", "motion_graphic"):
        return True
    return False


def assess_asset_coverage(processed_scenes):
    """Classify every sentence in an asset stage's output.

    Returns a dict: ``complete`` (bool), ``per_sentence`` (number ->
    status), ``incomplete`` (list of {sentence_number, scene,
    status, reason}), and counts by status. A payload with no
    sentence evidence at all (legacy/stub shapes) has no expected
    sentences and is complete — the gate refuses only on POSITIVE
    evidence of a starved or unrecorded sentence.
    """
    per_sentence = {}
    incomplete = []
    scene_of = {}

    for scene in processed_scenes or []:
        if not isinstance(scene, dict):
            continue
        scene_number = scene.get("scene_number")
        stats = scene.get("asset_stats") or {}

        expected = set()
        covered = set()

        # Sentence results: the stage's own per-sentence record.
        for result in scene.get("sentences") or []:
            if not isinstance(result, dict):
                continue
            number = _sentence_number(result)
            if number is None:
                continue
            expected.add(number)
            scene_of[number] = scene_number
            if result.get("videos") or result.get("images") or result.get("visuals"):
                covered.add(number)

        # Beat sentence lists name every sentence the stage owed.
        for beat in scene.get("beats") or []:
            if not isinstance(beat, dict):
                continue
            for raw in beat.get("sentence_numbers") or []:
                number = _sentence_number(raw)
                if number is not None:
                    expected.add(number)
                    scene_of.setdefault(number, scene_number)

        # Scene-level assets stamped with their sentence number.
        for asset in list(scene.get("videos") or []) + list(scene.get("images") or []):
            if not isinstance(asset, dict):
                continue
            number = _sentence_number(asset)
            if number is not None:
                covered.add(number)

        missing = {}
        for entry in stats.get("missing_visuals") or []:
            if not isinstance(entry, dict):
                continue
            number = _sentence_number(entry)
            if number is not None:
                missing[number] = entry
                expected.add(number)
                scene_of.setdefault(number, scene_number)

        unsearched_only = set()
        for entry in stats.get("uncovered_unsearched") or []:
            if not isinstance(entry, dict):
                continue
            number = _sentence_number(entry)
            if number is not None:
                expected.add(number)
                scene_of.setdefault(number, scene_number)
                if number not in missing:
                    unsearched_only.add(number)

        for number in sorted(expected):
            if number in covered:
                status = COVERED
            elif number in missing:
                entry = missing[number]
                if _is_graphics_fallback(entry):
                    status = GRAPHICS_FALLBACK
                else:
                    searched, starved = _entry_search_evidence(entry)
                    status = STARVED if starved else UNRESOLVED_SEARCHED
            elif number in unsearched_only:
                # Named as unsearched, never ladder-entered, never
                # recorded missing: nothing was attempted for it.
                status = STARVED
            else:
                status = UNRECORDED
            per_sentence[number] = status
            if status in INCOMPLETE_STATUSES:
                incomplete.append({
                    "sentence_number": number,
                    "scene": scene_of.get(number),
                    "status": status,
                    "reason": (
                        "searched_but_starved_by_budget"
                        if status == STARVED
                        else "no_asset_no_fallback_no_unresolved_marker"
                    ),
                })

    counts = {}
    for status in per_sentence.values():
        counts[status] = counts.get(status, 0) + 1
    return {
        "complete": not incomplete,
        "expected_sentences": sorted(per_sentence),
        "per_sentence": per_sentence,
        "counts": counts,
        "incomplete": incomplete,
    }


def coverage_incomplete_sentences(processed_scenes):
    """The sentence numbers whose coverage blocks adoption."""
    return [
        entry["sentence_number"]
        for entry in assess_asset_coverage(processed_scenes)["incomplete"]
    ]
