"""Reconciling production report (Phase 3, workstream P).

One report per production that cross-checks the artefacts against
each other — timeline ↔ asset report ↔ rights manifest ↔ renderer
audit ↔ run ledger ↔ revision records — and answers the brief's 25
observability questions, with unknown/unavailable where a source is
absent instead of zero or silence.

The report carries three SEPARATE verdicts (integrity, quality,
reliability) from services.quality_verdict. This module only
REPORTS: since Phase 3b (C-06), jobs.py's completion gate
(_honour_quality_verdict) consumes the quality verdict this report
computes — a quality FAIL there fails the run. The verdicts
themselves are unchanged by that gating.
"""

from __future__ import annotations

import json
from pathlib import Path

from services.final_video_auditor import _audit_verdict
from services.quality_verdict import (
    composite_quality_verdict,
    integrity_verdict,
    reliability_verdict,
)
from services import media_paths, visual_quality


def _load_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check(name, status, detail, values):
    return {"name": name, "status": status, "detail": detail, "values": values}


def _compare_check(name, left_label, left, right_label, right):
    values = {left_label: left, right_label: right}
    if left is None or right is None:
        return _check(
            name, "unavailable",
            f"cannot compare: {left_label}={left!r}, {right_label}={right!r}",
            values,
        )
    if left == right:
        return _check(name, "match", f"{left_label} == {right_label} == {left!r}", values)
    return _check(
        name, "mismatch",
        f"{left_label}={left!r} != {right_label}={right!r}", values,
    )


def _reconciliation(measurement, asset_report, timeline, rights_manifest, renderer_audit):
    checks = []

    # sentence_count
    timeline_cov = timeline.get("coverage") if isinstance(timeline, dict) else None
    report_cov = asset_report.get("coverage")
    outcomes_n = (measurement.get("sentence_outcomes") or {}).get("sentence_count")
    sc_values = {
        "timeline_coverage_total": (
            timeline_cov.get("total_sentences") if isinstance(timeline_cov, dict) else None
        ),
        "timeline_sentence_count_field": (
            timeline.get("sentence_count") if isinstance(timeline, dict) else None
        ),
        "asset_report_coverage_total": (
            report_cov.get("total_sentences") if isinstance(report_cov, dict) else None
        ),
        "sentence_outcomes_count": outcomes_n,
    }
    present = [v for v in sc_values.values() if v is not None]
    if len(present) < 2:
        checks.append(_check("sentence_count", "unavailable",
                               "fewer than two sentence-count sources present", sc_values))
    elif len(set(present)) == 1:
        checks.append(_check("sentence_count", "match",
                               f"all sources agree on {present[0]} sentences", sc_values))
    else:
        checks.append(_check("sentence_count", "mismatch",
                               f"sentence-count sources disagree: {sc_values}", sc_values))

    # assignment_counts
    m_breakdown = measurement.get("assignment_breakdown") or {}
    m_counts = {
        k: (v.get("count") if isinstance(v, dict) else None)
        for k, v in m_breakdown.items()
    }
    r_counts = asset_report.get("assignment_breakdown")
    if not m_counts or not isinstance(r_counts, dict) or not r_counts:
        checks.append(_check("assignment_counts", "unavailable",
                               "assignment breakdown missing from a source",
                               {"measurement": m_counts, "asset_report": r_counts}))
    elif m_counts == dict(r_counts):
        checks.append(_check("assignment_counts", "match",
                               "assignment counts agree", {"counts": m_counts}))
    else:
        checks.append(_check("assignment_counts", "mismatch",
                               "assignment counts disagree",
                               {"measurement": m_counts, "asset_report": dict(r_counts)}))

    # missing_visuals_count
    checks.append(_compare_check(
        "missing_visuals_count",
        "measurement_uncovered", (measurement.get("uncovered_beats") or {}).get("count"),
        "asset_report_missing_visual_count", asset_report.get("missing_visual_count"),
    ))

    # accepted_vs_rights_manifest
    total_accepted = asset_report.get("total_accepted_assets")
    manifest = rights_manifest if isinstance(rights_manifest, list) else []
    music_entries = sum(1 for e in manifest if isinstance(e, dict) and e.get("type") == "music")
    non_music = len(manifest) - music_entries
    checks.append(_compare_check(
        "accepted_vs_rights_manifest",
        "asset_report_total_accepted", total_accepted,
        "rights_manifest_non_music_entries", non_music,
    ))
    # annotate music separately in the detail/values of that check
    checks[-1]["values"]["rights_manifest_music_entries"] = music_entries
    checks[-1]["values"]["rights_manifest_total_entries"] = len(manifest)
    if checks[-1]["status"] == "match":
        checks[-1]["detail"] += f"; {music_entries} music entr(ies) counted separately"

    # substitutions_consistency (shared keys only: the report's
    # substitutions block also carries generated_graphics, which the
    # authenticity block intentionally does not)
    subs = asset_report.get("substitutions")
    auth_subs = (asset_report.get("authenticity") or {}).get("substitutions")
    shared_keys = ("text_cards", "placeholder_clips", "unverified_assets")
    if not isinstance(subs, dict) or not isinstance(auth_subs, dict):
        checks.append(_check("substitutions_consistency", "unavailable",
                               "substitutions block missing from a source",
                               {"substitutions": subs, "authenticity_substitutions": auth_subs}))
    else:
        left = {k: subs.get(k) for k in shared_keys}
        right = {k: auth_subs.get(k) for k in shared_keys}
        if left == right:
            checks.append(_check("substitutions_consistency", "match",
                                   "substitution counts agree on shared keys",
                                   {"shared": left}))
        else:
            checks.append(_check("substitutions_consistency", "mismatch",
                                   "substitution counts disagree on shared keys",
                                   {"substitutions": left, "authenticity": right}))

    # renderer placeholder / holdover
    for name, report_key, audit_key in (
        ("renderer_placeholder_count", "placeholder_clips", "placeholder_count"),
        ("renderer_holdover_count", "holdover_clips", "holdover_count"),
    ):
        audit_value = renderer_audit.get(audit_key) if isinstance(renderer_audit, dict) else None
        report_value = asset_report.get(report_key)
        if not isinstance(renderer_audit, dict) or audit_value is None or report_value is None:
            checks.append(_check(name, "unavailable",
                                   "renderer audit unavailable or value unknown",
                                   {"asset_report": report_value, "renderer_audit": audit_value}))
        elif int(audit_value) == int(report_value):
            checks.append(_check(name, "match",
                                   f"both record {audit_value}",
                                   {"asset_report": report_value, "renderer_audit": audit_value}))
        else:
            checks.append(_check(name, "mismatch",
                                   "renderer audit and asset report disagree",
                                   {"asset_report": report_value, "renderer_audit": audit_value}))

    # rotation_assignments
    rotation_measurement = None
    if m_counts.get("rotation") is not None:
        rotation_measurement = m_counts.get("rotation")
    elif _is_number((measurement.get("report_fields") or {}).get("rotation_assignments")):
        rotation_measurement = measurement["report_fields"]["rotation_assignments"]
    checks.append(_compare_check(
        "rotation_assignments",
        "measurement_rotation", rotation_measurement,
        "asset_report_rotation_assignments", asset_report.get("rotation_assignments"),
    ))

    # repeated_footage
    d2_items = (measurement.get("diversity") or {}).get("d2_repeated_items")
    report_repeated = asset_report.get("repeated_footage")
    values = {"measurement_d2_repeated_items": d2_items,
              "asset_report_repeated_footage": report_repeated}
    if d2_items is None or report_repeated is None:
        checks.append(_check("repeated_footage", "unavailable",
                               "repeated-footage value missing from a source", values))
    elif int(d2_items) == int(report_repeated):
        checks.append(_check("repeated_footage", "match",
                               "repeated counts agree", values))
    elif int(report_repeated) > int(d2_items):
        checks.append(_check(
            "repeated_footage", "explained_difference",
            "asset report counts looped items in repeated_footage; the "
            "measurement's D2 counts only items whose canonical identity "
            "appeared earlier in timeline order — the report's larger "
            "number is the loop-inclusive count, not a disagreement.",
            values))
    else:
        checks.append(_check("repeated_footage", "mismatch",
                               "measurement counts more repeats than the asset report",
                               values))

    failures = [c["name"] for c in checks if c["status"] == "mismatch"]
    return {
        "checks": checks,
        "failures": failures,
        "all_reconciled": not failures,
    }


def _tier_answer(tier_breakdown):
    """The q26 answer payload from an asset report's tier_breakdown:
    runtime share per tier plus the B-roll share. None when the
    breakdown is absent or not recorded (pre-B0 productions) — the
    question then reports unknown, never a fabricated zero."""
    if not isinstance(tier_breakdown, dict):
        return None
    if tier_breakdown.get("state") != "recorded":
        return None
    by_tier = tier_breakdown.get("by_tier") or {}
    answer = {
        tier: (by_tier.get(tier) or {}).get("runtime_share")
        for tier in (
            "direct", "related", "activity_broll", "atmospheric", "graphic",
        )
    }
    answer["broll_share"] = tier_breakdown.get("broll_share")
    # Phase 3 / D-B1: the founder-band comparison rides on the same
    # answer (report only — within_band / over_band / unknown).
    answer["band"] = tier_breakdown.get("band")
    return answer


def _planner_answer(planner):
    """The q30 answer payload from an asset report's planner block:
    mode, planner version, degraded_reason, degraded sentence
    counts and the quality summary. None when no block was
    recorded — the question then reports unknown."""
    if not isinstance(planner, dict):
        return None
    return {
        "mode": planner.get("mode"),
        "planner_version": planner.get("planner_version"),
        "degraded_reason": planner.get("degraded_reason"),
        "failure_kind": planner.get("failure_kind"),
        "degraded_sentences": planner.get("degraded_sentences"),
        "sentences": planner.get("sentences"),
        "quality": planner.get("quality"),
    }


def _questions(measurement, asset_report, verdicts):
    outcomes = measurement.get("sentence_outcomes") or {}
    buckets = outcomes.get("buckets") or {}
    diversity = measurement.get("diversity") or {}
    timeline_block = measurement.get("timeline") or {}
    quality_metrics = (verdicts.get("quality") or {}).get("metrics") or {}
    provider_failures = measurement.get("provider_failures") or {}
    targeted = measurement.get("targeted_graphics") or {}
    rejection = measurement.get("rejection_reasons") or {}
    reliance = (asset_report.get("providers") or {}).get("single_provider_reliance") or {}
    rights = asset_report.get("rights") or {}
    subs = asset_report.get("substitutions") or {}
    seg = (asset_report.get("verification_dimensions") or {}).get("segment_evidence") or {}
    human = (measurement.get("relevance") or {}).get("human_adjudicated_relevance") or {}

    def q(question, answer, state=None):
        if state is None:
            state = "answered" if answer is not None else "unknown"
        return {"question": question, "answer": answer, "state": state}

    def band_answer(metric_name, value):
        evaluation = quality_metrics.get(metric_name) or {}
        if value is None and evaluation.get("band") is None:
            return None
        return {"value": value, "band": evaluation.get("band"),
                "basis": evaluation.get("basis")}

    distribution = rejection.get("distribution")
    top_reason = None
    if isinstance(distribution, dict) and distribution:
        top_name = max(distribution, key=lambda k: distribution[k])
        top_reason = {"reason": top_name, "count": distribution[top_name]}

    recovery = asset_report.get("recovery_attempts")
    return {
        "q01": q("How many sentences does the production have?",
                 outcomes.get("sentence_count")),
        "q02": q("How many sentences got a direct (verified-for-sentence) visual?",
                 buckets.get("direct")),
        "q03": q("How many sentences got an approved generated graphic?",
                 buckets.get("approved_graphic")),
        "q04": q("How many sentences were filled contextually (rotation)?",
                 buckets.get("contextual")),
        "q05": q("How many sentences were covered by an editorial holdover?",
                 buckets.get("editorial_holdover")),
        "q06": q("How many sentences were uncovered (no timeline item)?",
                 buckets.get("uncovered")),
        "q07": q("How many sentences had no verified visual of their own "
                 "(missing visuals, before any fill)?",
                 outcomes.get("sentences_without_verified_visual")),
        "q08": q("How many unique canonical assets appear on the timeline?",
                 diversity.get("d1_unique_assets",
                               timeline_block.get("unique_canonical_assets_on_timeline"))),
        "q09": q("What is the D1 unique-assets-per-minute rate and its band?",
                 band_answer("unique_assets_per_minute",
                             diversity.get("d1_unique_assets_per_minute"))),
        "q10": q("What is the D2 repeated-item rate and its band?",
                 band_answer("repeated_item_rate",
                             diversity.get("d2_repeated_item_rate"))),
        "q11": q("What is the maximum reuse count of a single asset?",
                 timeline_block.get("max_reuse_count")),
        "q12": q("What are the runtime shares by kind (moving/still/graphics)?",
                 measurement.get("runtime_share")),
        "q13": q("How many targeted graphics were requested?",
                 targeted.get("targets_requested"),
                 "unavailable" if targeted.get("targets_requested") is None else "answered"),
        "q14": q("How many targeted graphics appear on the timeline?",
                 targeted.get("targeted_graphics_on_timeline"),
                 "unavailable" if targeted.get("targeted_graphics_on_timeline") is None else "answered"),
        "q15": q("How many candidate rejections were recorded in total?",
                 rejection.get("total")),
        "q16": q("What is the top rejection reason?", top_reason),
        "q17": q("How many provider (discovery) failures were recorded, and "
                 "was the collection state recorded?",
                 {"count": provider_failures.get("count"),
                  "state": provider_failures.get("state")}
                 if provider_failures else None,
                 "answered" if provider_failures.get("state") == "recorded" else "unknown"),
        "q18": q("How many downloads failed?",
                 asset_report.get("failed_downloads")),
        "q19": q("How many recovery attempts were recorded?",
                 len(recovery) if isinstance(recovery, list) else None,
                 "answered" if isinstance(recovery, list) else "unavailable"),
        "q20": q("What is the single-provider footage reliance share, and "
                 "does it warn?",
                 {"footage_share": reliance.get("footage_share"),
                  "warning": reliance.get("warning"),
                  "provider": reliance.get("provider"),
                  # Phase 3 / AM-022b: the reliance figures state
                  # their bases, and stills reliance rides along.
                  "footage_base": reliance.get("footage_base"),
                  "stills_share": reliance.get("stills_share"),
                  "stills_base": reliance.get("stills_base"),
                  "stills_provider": reliance.get("stills_provider")}
                 if reliance else None),
        "q21": q("How many assets have a recorded licence vs unknown licensing?",
                 {"recorded_licences": rights.get("recorded_licences"),
                  "unknown_licences": rights.get("unknown_licences")}
                 if rights else None),
        "q22": q("How many text cards, placeholder clips and unverified "
                 "assets were substituted?",
                 {"text_cards": subs.get("text_cards"),
                  "placeholder_clips": subs.get("placeholder_clips"),
                  "unverified_assets": subs.get("unverified_assets")}
                 if subs else None,
                 "unknown" if subs.get("placeholder_clips") is None and subs else "answered"),
        "q23": q("What share of assignments rest on positional segments?",
                 seg.get("positional_assignment_share")),
        "q24": q("What is the human-adjudicated relevance state?",
                 {"value": human.get("value"), "state": human.get("state"),
                  "basis": human.get("basis")} if human else None,
                 "unknown"),
        "q25": q("What are the overall integrity, quality and reliability verdicts?",
                 {"integrity": (verdicts.get("integrity") or {}).get("verdict"),
                  "quality": (verdicts.get("quality") or {}).get("verdict"),
                  "reliability": (verdicts.get("reliability") or {}).get("verdict")}),
        # B-roll B0 (§6.1): the tier question — answered from the
        # asset report's tier_breakdown (runtime shares per tier +
        # the B-roll share), unknown when the production's timeline
        # carried no tier stamps.
        "q26": q("How much of this film is direct footage of its actual "
                 "subject, and how much is related or activity B-roll?",
                 _tier_answer(asset_report.get("tier_breakdown")),
                 "answered"
                 if isinstance(asset_report.get("tier_breakdown"), dict)
                 and asset_report["tier_breakdown"].get("state") == "recorded"
                 else "unknown"),
        # Master Directive Phase 2 / B2: the ladder question —
        # answered from the asset report's tier_escalation totals
        # (attempts, spend, outcomes, budget exhaustion), unknown
        # when the report carries no ladder record (pre-B2
        # productions). An answered zero is honest: escalation ran
        # nowhere because no beat recorded a shortfall.
        "q27": q("When direct footage fell short, what did the tier "
                 "ladder try, what did it cost, and what did it cover?",
                 asset_report.get("tier_escalation")
                 if isinstance(asset_report.get("tier_escalation"), dict)
                 else None),
        # Master Directive Phase 3 / AM-022: the discovery question —
        # answered from the asset report's discovery block. The
        # degraded state (zero Serper calls BECAUSE no search key
        # was configured) is a first-class run fact, distinct from
        # zero calls for other reasons and from no record at all
        # (pre-Phase-3 productions report unknown).
        "q28": q("Was discovery degraded because no search key was "
                 "configured?",
                 asset_report.get("discovery")
                 if isinstance(asset_report.get("discovery"), dict)
                 else None),
        # Master Directive Phase 6 (motion continuity): answered
        # from the measurement's continuity block (visual_quality)
        # — restart repeats of already-shown video windows,
        # consecutive identical items, kind switches and composed
        # items, measured on the finished timeline. Unknown when
        # the measurement carries no continuity block (pre-Phase-6
        # measurements).
        "q29": q("Does the finished film repeat itself — restarted "
                 "video windows, back-to-back identical visuals — "
                 "and where do composed overlays appear?",
                 measurement.get("continuity")
                 if isinstance(measurement.get("continuity"), dict)
                 else None),
        # Master Directive Phase 7 / P0 (DM-1 + DM-2(a)): the
        # planner question — answered from the asset report's
        # planner block: which planner produced the visual plan,
        # why it degraded when it did, how many sentences it
        # degraded, and its measured plan quality. Unknown when
        # the production recorded no planner block (pre-Phase-7
        # productions) — never a fabricated "llm".
        "q30": q("Which planner produced the visual plan, why did "
                 "it degrade if it did, and how good was the plan?",
                 _planner_answer(asset_report.get("planner"))),
    }


def build_production_report(*, measurement, asset_report, timeline, rights_manifest,
                            renderer_audit=None, run_ledger_summary=None,
                            revision_summary=None):
    measurement = measurement if isinstance(measurement, dict) else {}
    asset_report = asset_report if isinstance(asset_report, dict) else {}
    timeline = timeline if isinstance(timeline, dict) else {}
    rights_manifest = rights_manifest if isinstance(rights_manifest, list) else []

    verdicts = {
        "integrity": integrity_verdict(_audit_verdict(asset_report)),
        "quality": composite_quality_verdict(measurement, asset_report),
        "reliability": reliability_verdict(measurement, asset_report),
    }
    return {
        "schema": "cinemora.production_report/1",
        "verdicts": verdicts,
        "reconciliation": _reconciliation(
            measurement, asset_report, timeline, rights_manifest, renderer_audit
        ),
        "questions": _questions(measurement, asset_report, verdicts),
        "sources": {
            "run_ledger": run_ledger_summary or {"state": "unavailable"},
            "revision_records": revision_summary or {"state": "unavailable"},
        },
    }


def build_production_report_for_project(project_dir):
    """Build the report from a project's stored artefacts on disk."""
    project_dir = Path(project_dir)
    measurement = visual_quality.measure_project(project_dir)
    # Stage 4 (DATA-004): derived artefacts resolve run-scoped
    # first (freshest run copy), legacy project-level as fallback.
    timeline = _load_json(
        media_paths.resolve_artefact(project_dir, "timeline.json"), {}
    )
    asset_report = _load_json(
        media_paths.resolve_artefact(project_dir, "asset_report.json"), {}
    )
    rights_manifest = _load_json(
        media_paths.resolve_artefact(project_dir, "rights_manifest.json"), []
    )
    renderer_audit = _load_json(project_dir / "renderer_timing_audit.json", None)

    run_manifest_path = media_paths.resolve_artefact(
        project_dir, "run_manifest.json"
    )
    if run_manifest_path.exists():
        run_ledger_summary = {"state": "recorded", "present": True,
                              "file": "run_manifest.json"}
    else:
        run_ledger_summary = {"state": "unavailable", "present": False}

    revision_files = sorted(project_dir.glob("revision_*.json"))
    if revision_files:
        revision_summary = {
            "state": "recorded",
            "count": len(revision_files),
            "files": [p.name for p in revision_files],
        }
    else:
        revision_summary = {"state": "unavailable", "count": 0}

    return build_production_report(
        measurement=measurement,
        asset_report=asset_report if isinstance(asset_report, dict) else {},
        timeline=timeline if isinstance(timeline, dict) else {},
        rights_manifest=rights_manifest if isinstance(rights_manifest, list) else [],
        renderer_audit=renderer_audit if isinstance(renderer_audit, dict) else None,
        run_ledger_summary=run_ledger_summary,
        revision_summary=revision_summary,
    )
