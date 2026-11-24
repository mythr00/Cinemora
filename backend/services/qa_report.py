"""QA Report (founder priority #8; gap-audit Phase F).

The gap audit's finding (doc 05 §3.3): the measurement machinery
runs on every production — the quality verdicts, the visual-quality
measurement, frame verification, the final auditor, cross-artefact
reconciliation — but the records live as separate artefact files
and the only user surface was the certificate's summary section.
This module is the unified Documentary Quality Report OVER those
records, never a second measurement:

- ASSEMBLY ONLY. Every value in the report is read from an
  artefact the production itself recorded (production_report.json,
  visual_quality.json, asset_report.json, delivery.json, resolved
  per run through services.media_paths). Nothing is recomputed,
  re-banded, averaged or estimated; verdicts pass through exactly
  as recorded (a quality FAIL stays FAIL, a reliability UNKNOWN
  stays UNKNOWN), and every quality metric carries the recorded
  value, band, basis and threshold text it was judged against.
- HONEST ABSENCE. Each section carries a state — "recorded",
  "not_recorded" (the artefact was never written for this run) or
  "unavailable" (the artefact exists but cannot be parsed) — and a
  section whose source artefact is absent reports its fields as
  None, never as a reconstructed number. A check that did not run
  is shown as not run; the absence of a report is not a pass.
- DRILL-DOWN is a join of recorded listings only: the per-sentence
  outcome accounting (visual_quality.sentence_outcomes), the tier
  stamps per sentence and the missing-visual records (both from
  the asset report) are merged by sentence number; a field no
  record carries for that sentence stays None.
- Scores: NONE are invented here. The nine vision categories of
  doc 05 §3.2 map onto recorded sections (visual → verdicts +
  coverage + diversity; source → candidates funnel; rights →
  rights; audio/captions → audio_captions; technical → delivery +
  integrity); story quality and factual *accuracy* have no
  defensible recorded score and are presented as recorded facts
  under honest labels, exactly as the audit rules.
"""

from __future__ import annotations

import json
from pathlib import Path

from services import media_paths

SCHEMA = "cinemora.qa_report/1"

RECORDED = "recorded"
NOT_RECORDED = "not_recorded"
UNAVAILABLE = "unavailable"

# The artefacts the report assembles, in read order.
_ARTEFACTS = (
    "production_report.json",
    "visual_quality.json",
    "asset_report.json",
    "delivery.json",
)


def _load(project_dir: Path, filename: str, run_id):
    """One artefact as (state, data). Mirrors the certificate's
    quality-section contract: absent -> not_recorded, unparseable
    -> unavailable, parsed dict -> recorded. A parsed non-dict is
    not a shape any writer produces, so it reads as unavailable."""
    path = media_paths.resolve_artefact(project_dir, filename, run_id)
    if not path.is_file():
        return NOT_RECORDED, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return UNAVAILABLE, None
    if not isinstance(data, dict):
        return UNAVAILABLE, None
    return RECORDED, data


def _section(state, **fields):
    return {"state": state, **fields}


def _verdicts_section(state, report):
    if state != RECORDED:
        return _section(state, schema=None, verdicts=None)
    return _section(
        RECORDED,
        schema=report.get("schema"),
        # Verbatim: integrity / quality / reliability, each with
        # its recorded failures, per-metric value+band+basis+
        # thresholds, fail/warning reasons and unknown metrics.
        verdicts=report.get("verdicts"),
    )


def _reconciliation_section(state, report):
    if state != RECORDED:
        return _section(state, all_reconciled=None, failures=None, checks=None)
    reconciliation = report.get("reconciliation") or {}
    return _section(
        RECORDED,
        all_reconciled=reconciliation.get("all_reconciled"),
        failures=reconciliation.get("failures") or [],
        # Full recorded detail (name, status, detail, values) —
        # the certificate shows only name+status; the QA report is
        # the drill-down surface.
        checks=reconciliation.get("checks") or [],
    )


def _questions_section(state, report):
    if state != RECORDED:
        return _section(state, questions=None)
    raw = report.get("questions") or {}
    questions = []
    if isinstance(raw, dict):
        for qid in sorted(raw):
            entry = raw[qid]
            if isinstance(entry, dict):
                questions.append(
                    {
                        "id": qid,
                        "question": entry.get("question"),
                        "answer": entry.get("answer"),
                        "state": entry.get("state"),
                    }
                )
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                questions.append(
                    {
                        "id": entry.get("id"),
                        "question": entry.get("question"),
                        "answer": entry.get("answer"),
                        "state": entry.get("state"),
                    }
                )
    return _section(RECORDED, questions=questions)


def _coverage_section(vq_state, measurement, ar_state, asset_report):
    """Coverage from two recorded sources: the measurement's
    per-sentence outcome accounting and the asset report's own
    coverage block. Each sub-block keeps its own state so a run
    that recorded one but not the other shows exactly that."""
    outcomes = None
    uncovered = None
    if vq_state == RECORDED:
        raw = measurement.get("sentence_outcomes") or {}
        if raw:
            outcomes = {
                "sentence_count": raw.get("sentence_count"),
                "buckets": raw.get("buckets"),
                "shares": raw.get("shares"),
                "reconciles": raw.get("reconciles"),
                "sentences_without_verified_visual": raw.get(
                    "sentences_without_verified_visual"
                ),
            }
        uncovered = measurement.get("uncovered_beats")
    asset_coverage = None
    if ar_state == RECORDED:
        asset_coverage = asset_report.get("coverage")
    state = RECORDED if (outcomes is not None or asset_coverage is not None) else (
        vq_state if vq_state != RECORDED else ar_state
    )
    return _section(
        state,
        sentence_outcomes=outcomes,
        asset_report_coverage=asset_coverage,
        uncovered_beats=uncovered,
    )


def _frame_verification_section(ar_state, asset_report, vq_state, measurement):
    if ar_state != RECORDED and vq_state != RECORDED:
        return _section(
            ar_state if ar_state != RECORDED else vq_state,
            summary=None,
            integrity_categories=None,
            rejections=None,
        )
    summary = None
    rejections = None
    if ar_state == RECORDED:
        dimensions = asset_report.get("verification_dimensions") or {}
        summary = dimensions.get("visual_frame")
        records = (asset_report.get("candidate_records") or {}).get("records") or []
        # Frame-verification rejection events, projected to the
        # recorded fields that explain the rejection. Runs whose
        # frame stage rejected nothing record an empty list here —
        # an empty list is a recorded fact, not a missing section.
        rejections = [
            {
                "candidate_id": r.get("candidate_id"),
                "scene": r.get("scene"),
                "sentence_number": r.get("sentence_number"),
                "provider": r.get("provider"),
                "kind": r.get("kind"),
                "url": r.get("url"),
                "outcome": r.get("outcome"),
                "reason": r.get("reason"),
                "primary_rejection_reason": r.get("primary_rejection_reason"),
                "verdict_reasons": r.get("verdict_reasons"),
            }
            for r in records
            if isinstance(r, dict) and r.get("stage") == "frame_verification"
        ]
    integrity_categories = None
    if vq_state == RECORDED:
        integrity_categories = measurement.get("integrity_categories")
    return _section(
        RECORDED,
        summary=summary,
        integrity_categories=integrity_categories,
        rejections=rejections,
    )


def _sentences_section(vq_state, measurement, ar_state, asset_report):
    """Per-sentence drill-down: outcome (measurement), tier stamp
    and missing-visual record (asset report), merged by sentence
    number. Only sentences at least one record names appear; a
    field no record carries stays None."""
    by_number: dict[int, dict] = {}

    def entry(number):
        return by_number.setdefault(
            number,
            {
                "sentence_number": number,
                "scene_number": None,
                "outcome": None,
                "tier": None,
                "missing_visual": None,
            },
        )

    if vq_state == RECORDED:
        outcomes = measurement.get("sentence_outcomes") or {}
        for item in outcomes.get("per_sentence") or []:
            if isinstance(item, dict) and item.get("sentence_number") is not None:
                entry(item["sentence_number"])["outcome"] = item.get("outcome")
    if ar_state == RECORDED:
        tier_block = asset_report.get("tier_breakdown") or {}
        for item in tier_block.get("per_sentence") or []:
            if not isinstance(item, dict):
                continue
            index = item.get("sentence_index")
            number = item.get("sentence_number")
            if number is None and index is not None:
                number = index + 1
            if number is None:
                continue
            row = entry(number)
            row["tier"] = item.get("tier")
            if item.get("scene_number") is not None:
                row["scene_number"] = item.get("scene_number")
        for item in asset_report.get("missing_visuals") or []:
            if isinstance(item, dict) and item.get("sentence_number") is not None:
                row = entry(item["sentence_number"])
                if item.get("scene") is not None:
                    row["scene_number"] = item.get("scene")
                row["missing_visual"] = {
                    "reason": item.get("reason"),
                    "role": item.get("role"),
                    "graphics_outcome": item.get("graphics_outcome"),
                }
    state = RECORDED if by_number else (
        vq_state if vq_state != RECORDED else ar_state
    )
    return _section(
        state,
        sentences=[by_number[n] for n in sorted(by_number)],
    )


def _candidates_section(ar_state, asset_report, vq_state, measurement):
    if ar_state != RECORDED:
        return _section(ar_state, totals=None, funnel=None, rejection_reasons=None)
    records = asset_report.get("candidate_records") or {}
    totals = {
        "total_retrieved_assets": asset_report.get("total_retrieved_assets"),
        "candidates_evaluated": asset_report.get("candidates_evaluated"),
        "total_accepted_assets": asset_report.get("total_accepted_assets"),
        "total_rejected_assets": asset_report.get("total_rejected_assets"),
        "verified_moving_footage": asset_report.get("verified_moving_footage"),
        "verified_images": asset_report.get("verified_images"),
        "unverified_assets": asset_report.get("unverified_assets"),
        "failed_downloads": asset_report.get("failed_downloads"),
        "download_attempt_failures": asset_report.get("download_attempt_failures"),
        "low_confidence_selections": asset_report.get("low_confidence_selections"),
    }
    rejection_reasons = None
    if vq_state == RECORDED:
        rejection_reasons = measurement.get("rejection_reasons")
    return _section(
        RECORDED,
        totals=totals,
        funnel={
            "by_outcome": records.get("by_outcome"),
            "by_stage": records.get("by_stage"),
            "rejection_funnel": asset_report.get("rejection_funnel"),
        },
        rejection_reasons=rejection_reasons,
    )


def _defects_section(ar_state, asset_report):
    if ar_state != RECORDED:
        return _section(
            ar_state,
            substitutions=None,
            authenticity=None,
            visual_quality_error=None,
            run_failure=None,
        )
    return _section(
        RECORDED,
        # The recorded substitution counts (text cards, placeholder
        # clips, unverified assets, generated graphics) — the
        # defect flags the final auditor judges.
        substitutions=asset_report.get("substitutions"),
        authenticity=asset_report.get("authenticity"),
        # FP-13: a failed quality-report build is recorded in the
        # asset report as visual_quality_error; the film completed
        # without verdicts. Surfaced verbatim when present.
        visual_quality_error=asset_report.get("visual_quality_error"),
        run_failure=asset_report.get("run_failure"),
    )


def _audio_captions_section(ar_state, asset_report):
    if ar_state != RECORDED:
        return _section(
            ar_state, final_audio=None, timing_consistency=None,
            captions_alignment=None,
        )
    return _section(
        RECORDED,
        final_audio=asset_report.get("final_audio"),
        timing_consistency=asset_report.get("timing_consistency"),
        captions_alignment=asset_report.get("captions_alignment"),
    )


def _rights_section(ar_state, asset_report):
    if ar_state != RECORDED:
        return _section(ar_state, recorded=None, unknown_licensing=None, note=None)
    dimensions = asset_report.get("verification_dimensions") or {}
    return _section(
        RECORDED,
        recorded=dimensions.get("rights"),
        unknown_licensing=asset_report.get("unknown_licensing"),
        note=asset_report.get("rights_note"),
    )


def _delivery_section(state, delivery):
    if state != RECORDED:
        return _section(
            state, status=None, decode_validated=None, duration_seconds=None,
            profile=None, master_bitrate_kbps=None, delivery_bitrate_kbps=None,
            size_ratio=None,
        )
    return _section(
        RECORDED,
        status=delivery.get("status"),
        decode_validated=delivery.get("decode_validated"),
        duration_seconds=delivery.get("duration_seconds"),
        profile=delivery.get("profile"),
        master_bitrate_kbps=delivery.get("master_bitrate_kbps"),
        delivery_bitrate_kbps=delivery.get("delivery_bitrate_kbps"),
        size_ratio=delivery.get("size_ratio"),
    )


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def build_qa_report(project_id: str, run: dict) -> dict:
    """Assemble the QA report for one production run from its
    recorded artefacts. ``run`` is the run_ledger run dict; the
    caller has already established project ownership and that the
    run belongs to the project."""
    run_id = run.get("run_id")
    project_dir = media_paths.project_dir(project_id)
    states = {}
    data = {}
    for filename in _ARTEFACTS:
        states[filename], data[filename] = _load(project_dir, filename, run_id)

    pr_state, report = states["production_report.json"], data["production_report.json"]
    vq_state, measurement = states["visual_quality.json"], data["visual_quality.json"]
    ar_state, asset_report = states["asset_report.json"], data["asset_report.json"]
    dl_state, delivery = states["delivery.json"], data["delivery.json"]
    report = report or {}
    measurement = measurement or {}
    asset_report = asset_report or {}

    return {
        "schema": SCHEMA,
        "project_id": project_id,
        "run": {
            "run_id": run_id,
            "status": run.get("status"),
            "stage": run.get("stage"),
            "error": run.get("error"),
            "created_at": _iso(run.get("created_at")),
            "started_at": _iso(run.get("started_at")),
            "finished_at": _iso(run.get("finished_at")),
        },
        "artefacts": states,
        "verdicts": _verdicts_section(pr_state, report),
        "reconciliation": _reconciliation_section(pr_state, report),
        "questions": _questions_section(pr_state, report),
        "coverage": _coverage_section(vq_state, measurement, ar_state, asset_report),
        "tiers": _section(
            vq_state,
            tier_accounting=measurement.get("tier_accounting") if vq_state == RECORDED else None,
        ),
        "diversity": _section(
            vq_state,
            diversity=measurement.get("diversity") if vq_state == RECORDED else None,
        ),
        "relevance": _section(
            vq_state,
            relevance=measurement.get("relevance") if vq_state == RECORDED else None,
        ),
        "genre_expectations": _section(
            vq_state,
            genre_expectations=(
                measurement.get("genre_expectations") if vq_state == RECORDED else None
            ),
        ),
        "runtime_share": _section(
            vq_state,
            runtime_share=measurement.get("runtime_share") if vq_state == RECORDED else None,
            runtime_share_by_format=(
                measurement.get("runtime_share_by_format") if vq_state == RECORDED else None
            ),
        ),
        "frame_verification": _frame_verification_section(
            ar_state, asset_report, vq_state, measurement
        ),
        "sentences": _sentences_section(vq_state, measurement, ar_state, asset_report),
        "candidates": _candidates_section(ar_state, asset_report, vq_state, measurement),
        "defects": _defects_section(ar_state, asset_report),
        "audio_captions": _audio_captions_section(ar_state, asset_report),
        "rights": _rights_section(ar_state, asset_report),
        "delivery": _delivery_section(dl_state, delivery),
    }
