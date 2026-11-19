"""Reports must state explicitly whether LLM or degraded analysis
produced the scenarios (founder requirement: heuristic fallback is
only ever an explicitly reported degraded mode, never a silent one).
"""

from services.asset_report import build_asset_report


def test_report_records_llm_mode():
    report = build_asset_report([], analysis_mode="llm")
    assert report["analysis_mode"] == "llm"


def test_report_records_degraded_mode():
    report = build_asset_report([], analysis_mode="degraded")
    assert report["analysis_mode"] == "degraded"


def test_report_mode_defaults_to_none_not_a_guess():
    report = build_asset_report([])
    assert report["analysis_mode"] is None
