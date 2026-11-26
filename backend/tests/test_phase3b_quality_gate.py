"""Phase 3b / C-06, superseded by Phase 4 — a quality verdict of
FAIL is a release finding, not a run failure.

C-06 originally made a quality FAIL fail the run. Phase 4's
technical/release status separation superseded that gate: the
film is technically deliverable, and the FAIL is recorded on the
completed run through release_status.derive_run_statuses
(release_status "not_release_grade", the verdict carried in the
release findings). The verdict itself is unchanged — it is still
computed exactly as before, still preserved and visible.

These tests pin jobs._honour_quality_verdict — the seam the
video_editor stage calls after _honour_final_audit:

- FAIL (verdicts-dict shape, as stored on asset_report) does NOT
  raise: it returns the quality verdict and stamps it onto the
  run state so the completed run keeps it visible;
- FAIL (bare quality-verdict shape) does not raise either;
- a REAL composite verdict computed from a Hyatt-shaped measurement
  is FAIL and passes through the seam without raising; a healthy
  measurement is PASS;
- PASS / QUALITY_WARNING / absent / malformed verdicts behave
  exactly as before (no raise).
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


def test_fail_verdict_does_not_raise_and_is_stamped_on_state():
    state = {}
    result = jobs._honour_quality_verdict(_verdicts(_FAIL_QUALITY), state=state)
    # Phase 4: the FAIL is a release finding on a deliverable film —
    # the seam returns the verdict instead of raising.
    assert result is _FAIL_QUALITY
    assert result["fail_reasons"] == _FAIL_QUALITY["fail_reasons"]
    # The verdict is preserved on the run state (production_state.json
    # is saved from this state when the run completes).
    assert state["quality_verdict"]["verdict"] == "FAIL"
    assert state["quality_verdict"]["fail_reasons"] == _FAIL_QUALITY["fail_reasons"]


def test_fail_verdict_bare_shape_does_not_raise():
    quality = dict(_FAIL_QUALITY)
    assert jobs._honour_quality_verdict(quality) is quality


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


def test_real_hyatt_shaped_composite_is_a_release_finding_not_a_failure():
    quality = composite_quality_verdict(_measurement(0.16), {})
    assert quality["verdict"] == "FAIL"
    state = {}
    assert jobs._honour_quality_verdict(_verdicts(quality), state=state) is quality
    assert state["quality_verdict"]["verdict"] == "FAIL"


def test_real_healthy_composite_passes_the_gate():
    quality = composite_quality_verdict(_measurement(0.90), {})
    assert quality["verdict"] == "PASS"
    assert jobs._honour_quality_verdict(_verdicts(quality)) is quality
