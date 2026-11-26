"""Phase 4 (Workstream B) — technical/release status separation at
the auditor and the completion gates.

Pins three things:

- the final auditor's per-visual CLASSIFICATION AUDIT
  (services.final_video_auditor._audit_verdict over the report's
  authenticity.timeline_visuals): counts by class, unclassified
  items and illustrative-stock-presented-as-evidence are
  RELEASE-class findings on verdict["release_failures"] ONLY —
  "passed" / "failures" (integrity) are untouched;
- the completion gates (jobs._honour_final_audit): RELEASE-class
  audit failures no longer raise; TECHNICAL-class failures still
  fail the run, carrying the technical reasons only;
- the deliverable chain end to end: a release-failing audit
  verdict + a FAIL quality verdict complete the run in the
  ledger with technical_status "passed" and release_status
  "not_release_grade", findings persisted — while a technical
  audit failure still raises before any completion.
"""

import uuid

import pytest

import jobs
import models
from database import Base, SessionLocal, engine
from services import release_status, run_ledger
from services.asset_report import build_asset_report
from services.final_video_auditor import _audit_verdict

Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------
# Classification audit — fabricated timelines through build_asset_report
# ---------------------------------------------------------------------

def _report_with_items(items):
    timeline_result = {"items": items, "scenes": []}
    return build_asset_report([], timeline_result=timeline_result)


def _item(**overrides):
    item = {
        "index": 0,
        "scene_number": 1,
        "classification": "authentic_footage",
        "assignment": "sentence",
        "verification_category": "evidence",
    }
    item.update(overrides)
    return item


def test_classification_audit_all_valid_counts_and_no_release_failures():
    report = _report_with_items([
        _item(index=0),
        _item(index=1, classification="generated_graphic",
              assignment="motion_graphic", verification_category=None),
        _item(index=2, classification="authentic_photo"),
    ])
    # The report carries the RAW per-item list the audit consumes.
    visuals = report["authenticity"]["timeline_visuals"]
    assert [v["classification"] for v in visuals] == [
        "authentic_footage", "generated_graphic", "authentic_photo",
    ]
    assert visuals[0]["assignment"] == "sentence"
    assert visuals[0]["verification_category"] == "evidence"

    verdict = _audit_verdict(report)
    classification = verdict["checks"]["classification"]
    assert classification["total"] == 3
    assert classification["counts"] == {
        "authentic_footage": 1,
        "authentic_photo": 1,
        "generated_graphic": 1,
    }
    assert classification["unclassified"] == 0
    assert classification["illustrative_presented_as_evidence"] == 0
    assert verdict["release_failures"] == []


def test_classification_audit_unclassified_items_are_release_findings_only():
    missing = _item(index=0)
    del missing["classification"]
    report = _report_with_items([
        missing,
        _item(index=1, classification="not-a-real-class"),
        _item(index=2),
    ])
    verdict = _audit_verdict(report)
    classification = verdict["checks"]["classification"]
    assert classification["total"] == 3
    assert classification["unclassified"] == 2
    # Counting normalises the missing/invalid stamps to "unknown";
    # the raw evidence itself is what made them unclassified.
    assert classification["counts"] == {
        "authentic_footage": 1,
        "unknown": 2,
    }
    assert verdict["release_failures"] == [
        "unclassified_visuals: 2 timeline visual(s) carry no valid "
        "authenticity classification"
    ]
    # Integrity is untouched: the release finding never leaks into
    # "failures" and never flips "passed".
    assert verdict["failures"] == []
    assert verdict["passed"] is True


def test_classification_audit_illustrative_stock_presented_as_evidence():
    report = _report_with_items([
        _item(index=0, classification="illustrative_stock"),
    ])
    verdict = _audit_verdict(report)
    classification = verdict["checks"]["classification"]
    assert classification["illustrative_presented_as_evidence"] == 1
    assert verdict["release_failures"] == [
        "illustrative_presented_as_evidence: 1 illustrative-stock "
        "visual(s) presented as sentence-verified evidence for "
        "direct-evidence sentences"
    ]
    assert verdict["failures"] == []
    assert verdict["passed"] is True


def test_classification_audit_illustrative_stock_contextual_is_legitimate():
    # The same illustrative-stock item serving a contextual (non-
    # evidence) sentence is legitimate B-roll material — no finding.
    report = _report_with_items([
        _item(index=0, classification="illustrative_stock",
              verification_category="contextual"),
    ])
    verdict = _audit_verdict(report)
    classification = verdict["checks"]["classification"]
    assert classification["counts"] == {"illustrative_stock": 1}
    assert classification["illustrative_presented_as_evidence"] == 0
    assert verdict["release_failures"] == []


def test_classification_audit_legacy_report_without_timeline_visuals():
    report = _report_with_items([_item(index=0)])
    del report["authenticity"]["timeline_visuals"]
    verdict = _audit_verdict(report)
    # Not recorded is not failed: null totals, no release failures.
    assert verdict["checks"]["classification"] == {
        "total": None,
        "counts": {},
        "unclassified": None,
        "illustrative_presented_as_evidence": None,
    }
    assert verdict["release_failures"] == []


def test_release_failures_key_always_present():
    assert _audit_verdict(None)["release_failures"] == []
    assert _audit_verdict({})["release_failures"] == []


# ---------------------------------------------------------------------
# Completion gates — release-class vs technical-class audit failures
# ---------------------------------------------------------------------

def _audit(failures, release_failures=None, passed=None):
    if passed is None:
        passed = not failures and not release_failures
    return {
        "verdict": {
            "evaluated": True,
            "passed": passed,
            "failures": failures,
            "release_failures": release_failures or [],
            "warnings": [],
            "checks": {},
        }
    }


def test_gate_release_only_failures_do_not_raise_and_stamp_state():
    state = {}
    audit = _audit([
        "coverage_incomplete: 2 required sentence(s) uncovered (sentences 3, 5)",
        "prohibited_substitution: text_cards=1",
    ])
    split = jobs._honour_final_audit(audit, state=state)
    assert split["technical"] == []
    assert split["release"] == audit["verdict"]["failures"]
    assert state["audit_release_failures"] == audit["verdict"]["failures"]


def test_gate_verdict_release_failures_do_not_raise_and_stamp_state():
    state = {}
    audit = _audit(
        [],
        release_failures=[
            "unclassified_visuals: 1 timeline visual(s) carry no "
            "valid authenticity classification"
        ],
        passed=True,
    )
    split = jobs._honour_final_audit(audit, state=state)
    assert split == {"technical": [], "release": []}
    assert state["audit_release_failures"] == [
        "unclassified_visuals: 1 timeline visual(s) carry no "
        "valid authenticity classification"
    ]


def test_gate_technical_failure_raises_with_technical_reasons_only():
    audit = _audit([
        "coverage_incomplete: 1 required sentence(s) uncovered",
        "missing_mandatory_evidence: 1 accepted footage asset(s) "
        "have no source URL",
    ])
    with pytest.raises(RuntimeError) as excinfo:
        jobs._honour_final_audit(audit, state={})
    message = str(excinfo.value)
    assert "Final video audit FAILED" in message
    assert "missing_mandatory_evidence" in message
    assert "coverage_incomplete" not in message


def test_gate_mixed_failures_raise_on_the_technical_class():
    audit = _audit([
        "prohibited_substitution: placeholder_clips=2",
        "provenance_mismatch: soundtrack records claim state='applied'",
    ])
    with pytest.raises(RuntimeError, match="provenance_mismatch"):
        jobs._honour_final_audit(audit)


# ---------------------------------------------------------------------
# The deliverable chain — ledger completion with separated statuses
# ---------------------------------------------------------------------

def _make_running_run(db):
    user = models.User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@example.invalid",
        password_hash="x",
    )
    db.add(user)
    db.flush()
    project = models.Project(
        id=str(uuid.uuid4()), script="a script", owner_id=user.id
    )
    db.add(project)
    db.flush()
    run = models.ProductionRun(
        id=str(uuid.uuid4()), project_id=project.id, status="running"
    )
    db.add(run)
    db.commit()
    return run.id


def test_release_failing_film_completes_with_separated_statuses():
    db = SessionLocal()
    try:
        run_id = _make_running_run(db)
    finally:
        db.close()

    audit_verdict = {
        "evaluated": True,
        "passed": False,
        "failures": [
            "coverage_incomplete: 1 required sentence(s) uncovered "
            "(sentences 4)"
        ],
        "release_failures": [
            "unclassified_visuals: 1 timeline visual(s) carry no "
            "valid authenticity classification"
        ],
        "warnings": [],
        "checks": {
            "classification": {
                "total": 3,
                "counts": {"authentic_footage": 2, "unknown": 1},
                "unclassified": 1,
                "illustrative_presented_as_evidence": 0,
            }
        },
    }
    quality = {
        "verdict": "FAIL",
        "metrics": {},
        "fail_reasons": ["direct_share=0.16 -> FAIL"],
        "warning_reasons": [],
        "unknown_metrics": [],
    }

    # The gate sequence the video_editor stage runs: neither gate
    # raises for release-class findings.
    state = {}
    jobs._honour_final_audit({"verdict": audit_verdict}, state=state)
    jobs._honour_quality_verdict({"quality": quality}, state=state)
    assert state["quality_verdict"]["verdict"] == "FAIL"

    statuses = release_status.derive_run_statuses(audit_verdict, quality)
    assert statuses["technical_status"] == "passed"
    assert statuses["release_status"] == "not_release_grade"

    assert run_ledger.finish_run(
        run_id,
        "completed",
        technical_status=statuses["technical_status"],
        release_status=statuses["release_status"],
        release_findings=statuses["release_findings"],
    ) is True

    db = SessionLocal()
    try:
        row = db.query(models.ProductionRun).filter_by(id=run_id).one()
        assert row.status == "completed"
        assert row.technical_status == "passed"
        assert row.release_status == "not_release_grade"
        findings = row.release_findings
        assert findings["audit_release_failures"] == [
            "coverage_incomplete: 1 required sentence(s) uncovered "
            "(sentences 4)",
            "unclassified_visuals: 1 timeline visual(s) carry no "
            "valid authenticity classification",
        ]
        assert findings["quality_verdict"]["verdict"] == "FAIL"
        assert findings["classification_audit"]["unclassified"] == 1
    finally:
        db.close()


def test_technical_failure_still_blocks_before_any_completion():
    audit = _audit([
        "missing_mandatory_evidence: 1 accepted footage asset(s) "
        "have no source URL"
    ])
    with pytest.raises(RuntimeError, match="missing_mandatory_evidence"):
        jobs._honour_final_audit(audit, state={})
    # And the derivation agrees: a technical-class failure is a
    # technical failure, never a deliverable film.
    statuses = release_status.derive_run_statuses(
        audit["verdict"], {"verdict": "PASS"}
    )
    assert statuses["technical_status"] == "failed"
