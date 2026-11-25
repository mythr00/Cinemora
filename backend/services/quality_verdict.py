"""Composite quality / integrity / reliability verdicts (Phase 3).

Pure functions, no I/O. This module turns a visual-quality
measurement (services.visual_quality) plus the Asset Manager report
into three SEPARATE verdicts:

- quality    — PASS / QUALITY_WARNING / FAIL from mutually exclusive
               metric bands (see THRESHOLDS). Degraded mode stays
               RETIRED: these bands only REPORT; nothing here relaxes
               a threshold, substitutes media, or changes how a run
               completes. (Phase 3b / C-06: jobs.py's completion gate
               consumes this module's quality FAIL — a run whose
               composite quality verdict is FAIL is recorded failed.
               The bands and verdict semantics here are unchanged;
               the gating lives in jobs._honour_quality_verdict.)
- integrity  — a restatement of the final auditor's hard verdict
               (services.final_video_auditor._audit_verdict): FAIL
               when it recorded failures, PASS when it evaluated
               clean, UNKNOWN when it did not evaluate. A quality
               warning NEVER bypasses an integrity failure, and a
               quality FAIL never rewrites the integrity verdict.
- reliability — PASS / QUALITY_WARNING / UNKNOWN from provider /
               download failure facts, keeping "recorded, none
               occurred" strictly separate from "not collected"
               (unknown).

Every metric carries a basis label — ESTABLISHED (already encoded in
the codebase), DERIVED, or PROVISIONAL (a reasoned starting gate from
the two stored reference productions; never quoted as validated) —
and the label is emitted with every evaluation.
"""

from __future__ import annotations

PASS = "PASS"
QUALITY_WARNING = "QUALITY_WARNING"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

# ---------------------------------------------------------------------------
# Threshold registry. Bands are mutually exclusive by construction:
# evaluate_metric applies the ordered rules for a metric and returns
# exactly one band (or None when the value is unknown).
# ---------------------------------------------------------------------------

THRESHOLDS = {
    "direct_share": {
        "basis": "PROVISIONAL",
        "thresholds": "PASS >= 0.50; QUALITY_WARNING >= 0.30 and < 0.50; FAIL < 0.30",
        "source_metric": "R1 direct relevance share",
    },
    "genuine_coverage_acceptable": {
        "basis": "PROVISIONAL",
        "thresholds": "PASS >= 0.55; QUALITY_WARNING >= 0.35 and < 0.55; FAIL < 0.35",
        "source_metric": "C2 acceptable = (direct + approved_graphic) / N",
    },
    "repeated_item_rate": {
        "basis": "PROVISIONAL",
        "thresholds": "PASS <= 0.40; QUALITY_WARNING > 0.40 and <= 0.65; FAIL > 0.65",
        "source_metric": "D2 repeated-item rate",
    },
    "unique_assets_per_minute": {
        "basis": "PROVISIONAL",
        "thresholds": "PASS >= 3.0; QUALITY_WARNING >= 1.5 and < 3.0; FAIL < 1.5",
        "source_metric": "D1 unique canonical assets per minute",
    },
    "variety_ratio": {
        "basis": "PROVISIONAL",
        "thresholds": "PASS >= 0.50; QUALITY_WARNING >= 0.25 and < 0.50; FAIL < 0.25",
        "source_metric": "D5 variety ratio (unique assets / timeline items)",
    },
    "rotation_share": {
        "basis": "ESTABLISHED",
        "thresholds": "QUALITY_WARNING > 0.50; otherwise PASS (no standalone FAIL band)",
        "source_metric": "R2 contextual (rotation) share — existing auditor limit",
    },
    "positional_share": {
        "basis": "ESTABLISHED",
        "thresholds": "QUALITY_WARNING > 0.25; otherwise PASS (no standalone FAIL band)",
        "source_metric": "E4 positional-segment share — existing auditor limit",
    },
    "single_provider_footage_share": {
        "basis": "ESTABLISHED",
        "thresholds": "QUALITY_WARNING > 0.80; otherwise PASS (no standalone FAIL band)",
        "source_metric": "D4 single-provider footage share — existing reliance warning",
    },
    "rotation_dominance": {
        "basis": "PROVISIONAL",
        "thresholds": (
            "FAIL when rotation_share > 0.80 AND direct_share < 0.30; otherwise PASS"
        ),
        "source_metric": "Combined R2+R1 rule (the Kohberger signature)",
    },
}


def _band_direct_share(value):
    if value >= 0.50:
        return PASS
    if value >= 0.30:
        return QUALITY_WARNING
    return FAIL


def _band_genuine_coverage(value):
    if value >= 0.55:
        return PASS
    if value >= 0.35:
        return QUALITY_WARNING
    return FAIL


def _band_repeated_item_rate(value):
    if value <= 0.40:
        return PASS
    if value <= 0.65:
        return QUALITY_WARNING
    return FAIL


def _band_unique_per_minute(value):
    if value >= 3.0:
        return PASS
    if value >= 1.5:
        return QUALITY_WARNING
    return FAIL


def _band_variety_ratio(value):
    if value >= 0.50:
        return PASS
    if value >= 0.25:
        return QUALITY_WARNING
    return FAIL


def _band_warning_over(limit):
    def band(value):
        return QUALITY_WARNING if value > limit else PASS

    return band


_BAND_RULES = {
    "direct_share": _band_direct_share,
    "genuine_coverage_acceptable": _band_genuine_coverage,
    "repeated_item_rate": _band_repeated_item_rate,
    "unique_assets_per_minute": _band_unique_per_minute,
    "variety_ratio": _band_variety_ratio,
    "rotation_share": _band_warning_over(0.50),
    "positional_share": _band_warning_over(0.25),
    "single_provider_footage_share": _band_warning_over(0.80),
}


def evaluate_metric(name, value):
    """Evaluate one metric against its registered bands.

    Returns {"value", "band", "basis", "thresholds"}; band is None
    when the value is None (unknown — never invented). Exactly one
    band is returned for any known value.
    """
    registry = THRESHOLDS.get(name)
    if registry is None:
        return {
            "value": value,
            "band": None,
            "basis": None,
            "thresholds": None,
        }
    if value is None:
        band = None
    elif name == "rotation_dominance":
        # The combined rule's value is the boolean trigger (or a
        # {"rotation_share", "direct_share"} dict, handled by the
        # composite); a bare boolean is accepted here.
        band = FAIL if value is True else PASS
    else:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            band = None
        else:
            band = _BAND_RULES[name](numeric)
    return {
        "value": value,
        "band": band,
        "basis": registry["basis"],
        "thresholds": registry["thresholds"],
    }


def _get(d, *keys):
    current = d
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _metric_values(measurement, asset_report):
    """Pull the raw metric values out of a measurement (+ report)."""
    measurement = measurement if isinstance(measurement, dict) else {}
    report = asset_report if isinstance(asset_report, dict) else {}

    diversity = measurement.get("diversity") or {}
    outcomes = measurement.get("sentence_outcomes") or {}
    buckets = outcomes.get("buckets") or {}
    shares = outcomes.get("shares") or {}
    relevance = measurement.get("relevance") or {}

    direct_share = relevance.get("direct_assignment_share")
    if direct_share is None:
        direct_share = shares.get("direct")

    acceptable = None
    if buckets and outcomes.get("sentence_count"):
        n = outcomes["sentence_count"]
        acceptable = (
            (buckets.get("direct", 0) + buckets.get("approved_graphic", 0)) / n
            if n
            else None
        )
    elif shares:
        parts = [shares.get("direct"), shares.get("approved_graphic")]
        if all(p is not None for p in parts):
            acceptable = parts[0] + parts[1]

    rotation_share = shares.get("contextual")
    if rotation_share is None:
        breakdown = measurement.get("assignment_breakdown") or {}
        rotation = breakdown.get("rotation") or {}
        if isinstance(rotation, dict):
            rotation_share = rotation.get("item_share")

    positional_share = _get(
        report, "verification_dimensions", "segment_evidence",
        "positional_assignment_share",
    )
    if positional_share is None:
        positional_share = measurement.get("positional_assignment_share")

    provider_share = _get(
        report, "providers", "single_provider_reliance", "footage_share"
    )
    if provider_share is None:
        provider_share = measurement.get("single_provider_footage_share")

    return {
        "direct_share": direct_share,
        "genuine_coverage_acceptable": acceptable,
        "repeated_item_rate": diversity.get("d2_repeated_item_rate"),
        "unique_assets_per_minute": diversity.get("d1_unique_assets_per_minute"),
        "variety_ratio": diversity.get("variety_ratio"),
        "rotation_share": rotation_share,
        "positional_share": positional_share,
        "single_provider_footage_share": provider_share,
    }


def composite_quality_verdict(measurement, asset_report=None):
    """The composite quality verdict for one production.

    FAIL if any evaluated metric FAILs; else QUALITY_WARNING if any
    warns; else PASS when at least one metric evaluated; else the
    verdict is None (nothing evaluable — reported, never guessed).
    """
    values = _metric_values(measurement, asset_report)
    metrics = {}
    fail_reasons = []
    warning_reasons = []
    unknown_metrics = []

    for name, value in values.items():
        evaluation = evaluate_metric(name, value)
        metrics[name] = evaluation
        if value is None or evaluation["band"] is None:
            unknown_metrics.append(name)
        elif evaluation["band"] == FAIL:
            fail_reasons.append(
                f"{name}={value} -> FAIL ({evaluation['thresholds']})"
            )
        elif evaluation["band"] == QUALITY_WARNING:
            warning_reasons.append(
                f"{name}={value} -> QUALITY_WARNING ({evaluation['thresholds']})"
            )

    # Combined rule (PROVISIONAL): rotation dominance.
    rotation = values.get("rotation_share")
    direct = values.get("direct_share")
    if rotation is not None and direct is not None:
        triggered = bool(rotation > 0.80 and direct < 0.30)
        evaluation = {
            "value": {"rotation_share": rotation, "direct_share": direct},
            "band": FAIL if triggered else PASS,
            "basis": THRESHOLDS["rotation_dominance"]["basis"],
            "thresholds": THRESHOLDS["rotation_dominance"]["thresholds"],
        }
        metrics["rotation_dominance"] = evaluation
        if triggered:
            fail_reasons.append(
                "rotation_dominance: rotation_share "
                f"{rotation} > 0.80 with direct_share {direct} < 0.30 -> FAIL"
            )
    else:
        metrics["rotation_dominance"] = {
            "value": None,
            "band": None,
            "basis": THRESHOLDS["rotation_dominance"]["basis"],
            "thresholds": THRESHOLDS["rotation_dominance"]["thresholds"],
        }
        unknown_metrics.append("rotation_dominance")

    evaluated = [m for m in metrics.values() if m["band"] is not None]
    if fail_reasons:
        verdict = FAIL
    elif warning_reasons:
        verdict = QUALITY_WARNING
    elif evaluated:
        verdict = PASS
    else:
        verdict = None

    return {
        "verdict": verdict,
        "metrics": metrics,
        "fail_reasons": fail_reasons,
        "warning_reasons": warning_reasons,
        "unknown_metrics": unknown_metrics,
    }


def integrity_verdict(audit_verdict):
    """Restate the final auditor's verdict as the integrity verdict.

    Input is final_video_auditor._audit_verdict's dict. The existing
    "passed" semantics are untouched by this restatement.
    """
    if not isinstance(audit_verdict, dict):
        return {"verdict": UNKNOWN, "failures": [], "warnings": []}
    failures = list(audit_verdict.get("failures") or [])
    warnings = list(audit_verdict.get("warnings") or [])
    if failures:
        verdict = FAIL
    elif audit_verdict.get("evaluated"):
        verdict = PASS
    else:
        verdict = UNKNOWN
    return {"verdict": verdict, "failures": failures, "warnings": warnings}


def _provider_failure_facts(measurement, asset_report):
    measurement = measurement if isinstance(measurement, dict) else {}
    report = asset_report if isinstance(asset_report, dict) else {}

    provider = measurement.get("provider_failures")
    if isinstance(provider, dict) and provider.get("state") is not None:
        count = provider.get("count")
        state = provider.get("state")
    elif "discovery_failures" in report:
        failures_list = report.get("discovery_failures")
        count = len(failures_list) if isinstance(failures_list, list) else 0
        state = (
            "recorded"
            if report.get("discovery_failures_state") == "recorded"
            else "unknown"
        )
    else:
        count = None
        state = "unknown"

    failed_downloads = report.get("failed_downloads")
    if failed_downloads is None:
        failed_downloads = _get(measurement, "report_fields", "failed_downloads")
    total_accepted = report.get("total_accepted_assets")
    if total_accepted is None:
        total_accepted = _get(measurement, "report_fields", "total_accepted_assets")

    return {
        "discovery_failures_count": count,
        "discovery_failures_state": state,
        "failed_downloads": failed_downloads,
        "total_accepted_assets": total_accepted,
    }


def reliability_verdict(measurement, asset_report=None):
    """Reliability verdict from provider / download failure facts.

    UNKNOWN when the provider-failure collection state is unknown
    (a legacy empty list cannot be distinguished from not-collected);
    QUALITY_WARNING when recorded discovery failures occurred, or
    failed downloads exceed accepted assets (F3); else PASS.
    """
    facts = _provider_failure_facts(measurement, asset_report)
    basis = {
        "discovery_failures": "PROVISIONAL",
        "failed_downloads_vs_accepted": "PROVISIONAL",
        "rule_source": "F1/F3 of the degraded-mode metrics framework",
    }
    if facts["discovery_failures_state"] != "recorded":
        verdict = UNKNOWN
    elif (facts["discovery_failures_count"] or 0) > 0:
        verdict = QUALITY_WARNING
    elif (
        isinstance(facts["failed_downloads"], (int, float))
        and isinstance(facts["total_accepted_assets"], (int, float))
        and facts["failed_downloads"] > facts["total_accepted_assets"]
    ):
        verdict = QUALITY_WARNING
    else:
        verdict = PASS
    return {"verdict": verdict, "basis": basis, "facts": facts}
