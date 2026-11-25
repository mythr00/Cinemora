"""Phase 3b / C-06 — a quality verdict of FAIL must fail the run.

Before this gate, jobs.run_full_pipeline attached the composite
quality verdict (services.quality_verdict) to the production report
and completed anyway: the Hyatt-shaped film (direct_share 0.16)
was a "completed" deliverable whose own QA verdict said FAIL. Only
the final auditor's integrity verdict gated completion.

These tests pin jobs._honour_quality_verdict — the gate the
video_editor stage calls after _honour_final_audit:

- FAIL (verdicts-dict shape, as stored on asset_report) raises,
  carries the fail reasons, and stamps the verdict onto the run
  state so the failed run keeps it visible;
- FAIL (bare quality-verdict shape) raises too;
- a REAL composite verdict computed from a Hyatt-shaped measurement
  is FAIL and raises; a healthy measurement is PASS and does not;
- PASS / QUALITY_WARNING / absent / malformed verdicts behave
  exactly as before the gate existed (no raise).
"""

import pytest

import jobs
from services.quality_verdict import composite_quality_verdict


def _verdicts(quality):
    return {
        "quality": quality,
        "integrity": {"verdict": "PASS", "failures": [], "warnings": []},
        "reliability": {"verdict": "PASS", "basis": {}, "facts": {}},
    }


_FAIL_QUALITY = {
    "verdict": "FAIL",
    "metrics": {},
    "fail_reasons": ["direct_share=0.16 -> FAIL (PASS >= 0.50; ...)"],
    "warning_reasons": [],
    "unknown_metrics": [],
}


def test_fail_verdict_raises_and_is_stamped_on_state():
    state = {}
    with pytest.raises(RuntimeError) as excinfo:
        jobs._honour_quality_verdict(_verdicts(_FAIL_QUALITY), state=state)
    assert "Quality verdict FAILED" in str(excinfo.value)
    assert "direct_share=0.16" in str(excinfo.value)
    # The verdict is preserved on the run state (production_state.json
    # is saved by fail_pipeline from this state).
    assert state["quality_verdict"]["verdict"] == "FAIL"
    assert state["quality_verdict"]["fail_reasons"] == _FAIL_QUALITY["fail_reasons"]


def test_fail_verdict_bare_shape_raises():
    with pytest.raises(RuntimeError):
        jobs._honour_quality_verdict(dict(_FAIL_QUALITY))


def test_pass_verdict_does_not_raise():
    quality = {"verdict": "PASS", "fail_reasons": []}
    assert jobs._honour_quality_verdict(_verdicts(quality)) is quality


def test_quality_warning_does_not_raise():
    # Warnings have never failed a run; C-06 changes FAIL only.
    quality = {"verdict": "QUALITY_WARNING", "fail_reasons": [],
               "warning_reasons": ["rotation_share=0.6 -> QUALITY_WARNING"]}
    assert jobs._honour_quality_verdict(_verdicts(quality)) is quality


def test_absent_verdict_behaves_as_before():
    assert jobs._honour_quality_verdict(None) is None
    assert jobs._honour_quality_verdict({}) is None
    assert jobs._honour_quality_verdict({"quality": None}) is None
    # The measurement-failure shape: no quality_verdicts key at all,
    # only visual_quality_error on the report.
    assert jobs._honour_quality_verdict(
        {"visual_quality_error": "ValueError: boom"}.get("quality_verdicts")
    ) is None


def test_unmeasured_verdict_none_does_not_raise():
    # composite_quality_verdict returns verdict=None when nothing was
    # evaluable — reported, never guessed, and never a failure.
    quality = {"verdict": None, "fail_reasons": []}
    assert jobs._honour_quality_verdict(_verdicts(quality)) is quality


def _measurement(direct_share):
    return {
        "relevance": {"direct_assignment_share": direct_share},
        "sentence_outcomes": {
            "sentence_count": 10,
            "buckets": {"direct": int(direct_share * 10),
                        "approved_graphic": 0},
            "shares": {"direct": direct_share, "contextual": 0.1},
        },
        "diversity": {},
    }


def test_real_hyatt_shaped_composite_fails_the_gate():
    quality = composite_quality_verdict(_measurement(0.16), {})
    assert quality["verdict"] == "FAIL"
    with pytest.raises(RuntimeError):
        jobs._honour_quality_verdict(_verdicts(quality))


def test_real_healthy_composite_passes_the_gate():
    quality = composite_quality_verdict(_measurement(0.90), {})
    assert quality["verdict"] == "PASS"
    assert jobs._honour_quality_verdict(_verdicts(quality)) is quality
