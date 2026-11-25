"""Documentary standard tests (Phase 7, C-14): the MUTE TEST and
the RADIO TEST from PERFECT_DOCUMENTARY_STANDARD.md (§12: "If you
mute it, it should still tell the story. If you close your eyes, it
should still make sense"), formalised as an executable QA artefact.

Honesty rules, mirroring the rest of the QA machinery:

- A check is COMPUTED only where real production data honestly
  supports a proxy judgement (coverage accounting, substitution
  counts, diversity, timing consistency, caption alignment, final
  audio levels). A computed check is "pass", "fail", or "unknown"
  (the data was not recorded — never silently passed).
- Judgements no proxy can make (does the silent film actually
  tell the story? does the narration alone actually make sense?)
  are MANUAL checks: each carries the exact reviewer question and
  is recorded "requires_review" until a reviewer records a verdict
  through `manual_reviews`. A manual item NEVER silently passes.
- Verdict interaction (see services.production_report): a FAILED
  computed check escalates the composite quality verdict to FAIL,
  exactly like any other QA failure. Manual items do NOT flip a
  verdict — but `inspection_status` keeps the distinction: a
  production whose manual items are unreviewed is
  "pending_manual_review", never "passed_inspection", however
  green its verdicts are.
"""

from __future__ import annotations

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"
REQUIRES_REVIEW = "requires_review"

# Visual-change density floor: the same FAIL band the composite
# quality verdict already applies to D1 (unique assets/minute).
MIN_UNIQUE_ASSETS_PER_MINUTE = 1.5
# Loudness tolerance around the configured target (LUFS).
LOUDNESS_TOLERANCE_LU = 3.0


def _check(check_id, test, kind, status, detail, value=None, question=None,
           flag=None):
    entry = {
        "id": check_id,
        "test": test,
        "kind": kind,
        "status": status,
        "detail": detail,
        "value": value,
    }
    if question is not None:
        entry["question"] = question
    if flag is not None:
        entry["flag"] = flag
    return entry


def _manual(check_id, test, question, manual_reviews):
    review = (manual_reviews or {}).get(check_id)
    if review in (PASS, FAIL):
        return _check(check_id, test, "manual", review,
                      f"Reviewer recorded: {review}.", question=question)
    return _check(check_id, test, "manual", REQUIRES_REVIEW,
                  "Not yet reviewed — a reviewer must answer the "
                  "question; this item is never auto-passed.",
                  question=question)


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ---------------------------------------------------------------------
# Mute test (visual-only proxies)
# ---------------------------------------------------------------------

def _mute_checks(measurement, asset_report, manual_reviews):
    outcomes = measurement.get("sentence_outcomes") or {}
    buckets = outcomes.get("buckets") or {}
    diversity = measurement.get("diversity") or {}
    subs = asset_report.get("substitutions") or {}
    checks = []

    sentence_count = _num(outcomes.get("sentence_count"))
    uncovered = _num(buckets.get("uncovered"))
    if sentence_count and uncovered is not None:
        checks.append(_check(
            "mute.sentence_visual_coverage", "mute_test", "computed",
            PASS if uncovered == 0 else FAIL,
            "Every narration sentence has a timeline visual "
            f"({sentence_count - int(uncovered)}/{int(sentence_count)} covered).",
            value={"sentence_count": int(sentence_count),
                   "uncovered": int(uncovered)},
        ))
    else:
        checks.append(_check(
            "mute.sentence_visual_coverage", "mute_test", "computed",
            UNKNOWN,
            "Sentence outcome accounting was not recorded; coverage "
            "cannot be judged from data.", value=None))

    text_cards = _num(subs.get("text_cards"))
    if text_cards is not None:
        checks.append(_check(
            "mute.no_full_sentence_text_cards", "mute_test", "computed",
            PASS if text_cards == 0 else FAIL,
            "No full-sentence text cards stand in for visuals "
            f"(founder defect ruling; recorded text_cards={int(text_cards)}).",
            value={"text_cards": int(text_cards)},
        ))
    else:
        checks.append(_check(
            "mute.no_full_sentence_text_cards", "mute_test", "computed",
            UNKNOWN, "Substitution counts were not recorded.", value=None))

    density = _num(diversity.get("d1_unique_assets_per_minute"))
    if density is not None:
        checks.append(_check(
            "mute.visual_change_density", "mute_test", "computed",
            PASS if density >= MIN_UNIQUE_ASSETS_PER_MINUTE else FAIL,
            f"Unique visuals per minute = {density} "
            f"(floor {MIN_UNIQUE_ASSETS_PER_MINUTE}, the D1 FAIL band).",
            value={"unique_assets_per_minute": density},
        ))
    else:
        checks.append(_check(
            "mute.visual_change_density", "mute_test", "computed",
            UNKNOWN, "Diversity measurement was not recorded.", value=None))

    checks.append(_manual(
        "mute.story_comprehensible_silent", "mute_test",
        "Watch the film muted, start to finish: can you follow the "
        "story — its people, places, dates and turns — from the "
        "visuals alone, without the narration?",
        manual_reviews))
    checks.append(_manual(
        "mute.visual_specificity", "mute_test",
        "Mute test, second pass: is every visual the most specific "
        "answer to its sentence (the actual person/place/document/"
        "mechanism, or an honest labelled stand-in) — is any shot "
        "mere wallpaper that could be swapped without loss?",
        manual_reviews))
    return checks


# ---------------------------------------------------------------------
# Radio test (audio-only proxies)
# ---------------------------------------------------------------------

def _radio_checks(measurement, asset_report, manual_reviews):
    checks = []
    consistency = asset_report.get("timing_consistency")
    alignment = asset_report.get("captions_alignment")
    final_audio = asset_report.get("final_audio")

    if isinstance(consistency, dict) and consistency.get("within_tolerance") is not None:
        checks.append(_check(
            "radio.narration_timing_consistency", "radio_test", "computed",
            PASS if consistency.get("within_tolerance") else FAIL,
            "Narration is present and its measured parts sum to the "
            "concatenated narration file within tolerance "
            f"(divergence {consistency.get('divergence_seconds')}s, "
            f"source {consistency.get('timing_source')}).",
            value={"within_tolerance": bool(consistency.get("within_tolerance")),
                   "divergence_seconds": consistency.get("divergence_seconds")},
        ))
    else:
        checks.append(_check(
            "radio.narration_timing_consistency", "radio_test", "computed",
            UNKNOWN,
            "Timing consistency was not recorded; narration presence "
            "across the film cannot be verified from data.", value=None))

    if isinstance(alignment, dict):
        aligned = _num(alignment.get("aligned")) or 0
        failed = _num(alignment.get("alignment_failed")) or 0
        timing_source = (consistency or {}).get("timing_source") if isinstance(consistency, dict) else None
        provisional = timing_source not in ("measured_part", "whisper", None)
        status = FAIL if (failed > 0 and aligned == 0) else PASS
        checks.append(_check(
            "radio.captions_exist_and_timed", "radio_test", "computed",
            status,
            f"Captions exist with alignment recorded "
            f"(aligned={int(aligned)}, alignment_failed={int(failed)}, "
            f"timing_source={timing_source}).",
            value={"aligned": int(aligned), "alignment_failed": int(failed),
                   "timing_source": timing_source,
                   "provisional_timing": bool(provisional)},
            flag="provisional_timing" if provisional else None,
        ))
    else:
        checks.append(_check(
            "radio.captions_exist_and_timed", "radio_test", "computed",
            UNKNOWN,
            "No caption alignment record; caption existence/timing "
            "cannot be verified from data.", value=None))

    if isinstance(final_audio, dict) and _num(final_audio.get("integrated_lufs")) is not None:
        lufs = float(final_audio["integrated_lufs"])
        target = _num(final_audio.get("target_lufs"))
        peak = _num(final_audio.get("true_peak_dbtp"))
        peak_max = _num(final_audio.get("max_true_peak_dbtp"))
        loud_ok = target is None or abs(lufs - float(target)) <= LOUDNESS_TOLERANCE_LU
        peak_ok = peak is None or peak_max is None or float(peak) <= float(peak_max) + 0.05
        checks.append(_check(
            "radio.audio_levels", "radio_test", "computed",
            PASS if (loud_ok and peak_ok) else FAIL,
            f"Final audio measured at {lufs} LUFS "
            f"(target {target}, tolerance ±{LOUDNESS_TOLERANCE_LU}) "
            f"with true peak {peak} dBTP (ceiling {peak_max}).",
            value={"integrated_lufs": lufs, "target_lufs": target,
                   "true_peak_dbtp": peak, "max_true_peak_dbtp": peak_max},
        ))
    else:
        checks.append(_check(
            "radio.audio_levels", "radio_test", "computed",
            UNKNOWN,
            "Final audio levels were not measured/recorded.", value=None))

    checks.append(_manual(
        "radio.story_comprehensible_blind", "radio_test",
        "Listen with eyes closed, start to finish: does the "
        "narration alone make sense — every section present, no "
        "argument step carried only by an on-screen graphic?",
        manual_reviews))
    checks.append(_manual(
        "radio.narration_clarity_and_pacing", "radio_test",
        "Audio-only pass: is the narration clear and evenly paced "
        "throughout, with music/effects never masking speech?",
        manual_reviews))
    return checks


def build_standard_tests(measurement=None, asset_report=None,
                         manual_reviews=None) -> dict:
    """The C-14 checklist artefact for one production.

    `manual_reviews` optionally maps a manual check id to a
    reviewer-recorded "pass"/"fail"; anything else leaves the item
    at "requires_review"."""
    measurement = measurement if isinstance(measurement, dict) else {}
    asset_report = asset_report if isinstance(asset_report, dict) else {}
    mute = _mute_checks(measurement, asset_report, manual_reviews)
    radio = _radio_checks(measurement, asset_report, manual_reviews)
    all_checks = mute + radio
    failed = [c["id"] for c in all_checks
              if c["kind"] == "computed" and c["status"] == FAIL]
    failed += [c["id"] for c in all_checks
               if c["kind"] == "manual" and c["status"] == FAIL]
    pending = [c["id"] for c in all_checks if c["status"] == REQUIRES_REVIEW]
    unknown = [c["id"] for c in all_checks
               if c["kind"] == "computed" and c["status"] == UNKNOWN]
    return {
        "mute_test": {"checks": mute},
        "radio_test": {"checks": radio},
        "automatable_failed": [c["id"] for c in all_checks
                               if c["kind"] == "computed" and c["status"] == FAIL],
        "failed": failed,
        "manual_pending": pending,
        "unknown": unknown,
    }


def inspection_status(verdicts, standard_tests) -> str:
    """The inspection outcome, kept distinct from the verdicts:
    - "failed": a verdict is FAIL or a standard-test check failed;
    - "pending_manual_review": nothing failed, but manual items
      (or unmeasured computed items) remain — the production may
      NOT be reported as passed inspection;
    - "passed_inspection": no failures and every manual item has
      a recorded reviewer pass;
    - "unknown": no verdicts/standard tests to judge from."""
    if not isinstance(standard_tests, dict) or not standard_tests:
        return "unknown"
    verdicts = verdicts if isinstance(verdicts, dict) else {}
    verdict_values = [
        (v or {}).get("verdict") for v in verdicts.values()
        if isinstance(v, dict)
    ]
    if any(v == "FAIL" for v in verdict_values) or standard_tests.get("failed"):
        return "failed"
    if standard_tests.get("manual_pending") or standard_tests.get("unknown"):
        return "pending_manual_review"
    if not verdict_values:
        return "unknown"
    return "passed_inspection"
