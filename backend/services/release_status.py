"""Technical / release status separation (Phase 4).

Pure functions, no I/O: this module classifies final-audit
failures and derives a finished run's two independent statuses.

- ``technical_status`` answers "did the production machinery
  produce a technically valid film?" — an audit that was never
  evaluated, or that failed on a technical-class code, means the
  film is not even technically sound.
- ``release_status`` answers "may this film be presented as a
  finished, release-grade documentary?" — coverage gaps,
  prohibited substitutions, unclassified visuals, illustrative
  material presented as evidence, or a composite quality FAIL
  all hold a technically valid film at not_release_grade. The
  billing consequence of that hold is founder-gated and lives
  at the single switch point in
  services/billing/generation_billing.py; nothing here decides
  money.

Classification is FAIL-CLOSED: a failure code this module does
not recognise is technical, so a future auditor failure type can
never silently become deliverable.
"""

from __future__ import annotations

import json

TECHNICAL = "technical"
RELEASE = "release"

TECHNICAL_PASSED = "passed"
TECHNICAL_FAILED = "failed"

RELEASE_GRADE = "release_grade"
NOT_RELEASE_GRADE = "not_release_grade"

# Audit failure codes that hold a film from release grade: the
# film may be technically valid, but it must not be presented as
# a finished documentary while any of these stand.
RELEASE_AUDIT_CODES = frozenset({
    "coverage_incomplete",
    "prohibited_substitution",
    "unclassified_visuals",
    "illustrative_presented_as_evidence",
})

# Audit failure codes that are technical failures. (Any code in
# neither set is ALSO technical — see classify_audit_failure.)
TECHNICAL_AUDIT_CODES = frozenset({
    "audit_not_evaluated",
    "missing_mandatory_evidence",
    "provenance_mismatch",
})


def audit_failure_code(failure) -> str:
    """The code prefix of an auditor failure string: the text
    before the first ":", stripped (the whole string, stripped,
    when there is no colon)."""
    text = failure if isinstance(failure, str) else str(failure or "")
    return text.split(":", 1)[0].strip()


def classify_audit_failure(failure) -> str:
    """RELEASE when the failure's code is a known release-class
    code, otherwise TECHNICAL — fail-closed: an unrecognised code
    is technical, so a future failure type can never silently
    become deliverable."""
    if audit_failure_code(failure) in RELEASE_AUDIT_CODES:
        return RELEASE
    return TECHNICAL


def _failure_list(value) -> list:
    """Normalise a failures-ish value to a list, defensively."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def split_audit_failures(failures) -> dict:
    """Split auditor failure strings into {"technical": [...],
    "release": [...]}, preserving order within each class."""
    split = {"technical": [], "release": []}
    for failure in _failure_list(failures):
        split[classify_audit_failure(failure)].append(failure)
    return split


def _json_safe(value):
    """Return a JSON-safe copy of value (verbatim for values that
    are already JSON-safe)."""
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        try:
            return json.loads(json.dumps(value, default=str))
        except (TypeError, ValueError):
            return None


def derive_run_statuses(audit_verdict, quality_verdict) -> dict:
    """Derive a finished run's technical_status / release_status
    plus its release findings.

    audit_verdict is final_video_auditor._audit_verdict's dict
    (keys: evaluated, passed, failures, warnings, checks, and
    optionally release_failures — release-class findings the
    auditor reports separately from failures). quality_verdict
    is quality_verdict.composite_quality_verdict's dict or None.

    Malformed inputs are handled defensively: a non-dict audit
    verdict is treated as unevaluated — technical failed and
    not release grade, with empty findings lists.
    """
    quality = quality_verdict if isinstance(quality_verdict, dict) else None

    if not isinstance(audit_verdict, dict):
        return {
            "technical_status": TECHNICAL_FAILED,
            "release_status": NOT_RELEASE_GRADE,
            "release_findings": _json_safe({
                "audit_release_failures": [],
                "audit_warnings": [],
                "quality_verdict": quality,
                "classification_audit": None,
            }),
        }

    failures = _failure_list(audit_verdict.get("failures"))
    split = split_audit_failures(failures)
    extra_release = _failure_list(audit_verdict.get("release_failures"))

    technical_status = (
        TECHNICAL_FAILED
        if (not audit_verdict.get("evaluated")) or split["technical"]
        else TECHNICAL_PASSED
    )
    release_status = (
        NOT_RELEASE_GRADE
        if split["release"]
        or extra_release
        or (quality is not None and quality.get("verdict") == "FAIL")
        else RELEASE_GRADE
    )

    checks = audit_verdict.get("checks")
    classification_audit = (
        checks.get("classification") if isinstance(checks, dict) else None
    )

    findings = {
        "audit_release_failures": split["release"] + extra_release,
        "audit_warnings": _failure_list(audit_verdict.get("warnings")),
        "quality_verdict": quality,
        "classification_audit": classification_audit,
    }
    return {
        "technical_status": technical_status,
        "release_status": release_status,
        "release_findings": _json_safe(findings),
    }
