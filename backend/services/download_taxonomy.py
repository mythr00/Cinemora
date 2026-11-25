"""Download-failure taxonomy (Master Directive Phase 3a — C-02/C-12).

Before this module, a failed candidate download collapsed into a
handful of counters (``downloads_failed``) that mixed at least six
unlike outcomes — network/HTTP errors, rights rejections,
verification failures, duplicates, budget exhaustion and timeouts —
so a run like Petito's 58/76 could not be diagnosed after the fact.

This module is the ONE classification every download call site
uses. ``classify_download_failure`` maps a legacy reason string
(plus optional HTTP status / exception evidence) onto a closed,
machine-readable class vocabulary:

- ``network_error``      transport failure with no HTTP answer
                         (connection reset, DNS, premature EOF…)
- ``http_error``         an HTTP answer that is an error; the
                         status rides on the record
- ``timeout``            the attempt died on a timeout (socket,
                         subprocess or read)
- ``rights_rejected``    a rights/policy rejection (licence,
                         banned source, rights status)
- ``verification_failed``the bytes arrived (or were probed) and a
                         named verification check rejected them —
                         the check rides on the record
- ``duplicate``          never re-requested: this candidate /
                         segment already failed or was already
                         used in this run
- ``budget_exhausted``   a budget gate (production / tier / beat /
                         speculative) refused the attempt before
                         any request was made
- ``source_unavailable`` the source cannot serve this candidate
                         (no URL, no evidenced segment, gone)
- ``unknown``            none of the evidence fits — recorded
                         honestly, never silently re-bucketed

Retry policy (C-12) lives here too, so every call site shares it:
deterministically-doomed candidates (4xx other than 429, rights
rejections, verification failures, duplicates, budget stops) are
NEVER retried; transient failures (network error, timeout, 429,
5xx) get at most ONE retry (``MAX_TRANSIENT_RETRIES``).
"""

from __future__ import annotations

import re

FAILURE_CLASSES = (
    "network_error",
    "http_error",
    "timeout",
    "rights_rejected",
    "verification_failed",
    "duplicate",
    "budget_exhausted",
    "source_unavailable",
    "unknown",
)

#: C-12: transient failures get at most one retry, ever.
MAX_TRANSIENT_RETRIES = 1

_BUDGET_REASONS = frozenset({
    "budget_exhausted",
    "production_download_budget",
    "production_search_budget",
    "tier_download_budget",
    "tier_query_budget",
    "speculative_download_budget",
    "beat_download_attempt_cap",
    "wall_clock",
})

_DUPLICATE_REASONS = frozenset({
    "previously_failed_this_run",
    "duplicate",
    "duplicate_candidate",
    "already_used_file",
    "already_used_clip",
    "already_used_source",
    "already_used_title",
    "segment_reused",
})

_VERIFICATION_REASONS = frozenset({
    "invalid_media",
    "probe_failed",
    "technical_below_minimum",
    "undersized_file",
    "decode_failed",
    "transcript_mismatch",
    "speculative_frame_unconfirmed",
    "frame_verification",
})

_SOURCE_UNAVAILABLE_REASONS = frozenset({
    "no_segment_evidence",
    "no_url",
    "source_unavailable",
    "no_segment",
    "missing_candidate_key",
})

_STATUS_RE = re.compile(r"\b(4\d\d|5\d\d)\b")


def _status_from_text(text):
    match = _STATUS_RE.search(str(text or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def is_transient_http_status(status):
    """429 and 5xx are transient; every other error status is a
    definitive answer from the source."""
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    return code == 429 or 500 <= code <= 599


def classify_download_failure(reason=None, *, http_status=None,
                              detail=None, error=None, stage=None):
    """Classify one failed download attempt.

    ``reason`` is the legacy reason string a call site already
    stamps (``candidate["_download_error"]``, a failure record's
    ``reason``…). ``http_status`` / ``error`` / ``detail`` are
    optional extra evidence. Returns a record dict:
    ``{failure_class, reason, http_status, check, retryable}`` —
    ``check`` names the verification check for
    ``verification_failed`` records, else None.
    """
    raw_reason = str(reason or "").strip()
    base = raw_reason.split(":", 1)[0].strip().lower()
    detail_text = str(detail or "")
    error_text = f"{type(error).__name__}: {error}" if error is not None else ""
    status = http_status
    if status is None and error is not None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if status is None:
        status = _status_from_text(detail_text) or _status_from_text(error_text)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None

    combined = f"{base} {detail_text} {error_text}".lower()
    failure_class = "unknown"
    check = None

    if base in _BUDGET_REASONS or "budget" in base:
        failure_class = "budget_exhausted"
    elif base in _DUPLICATE_REASONS:
        failure_class = "duplicate"
    elif (
        "rights" in base
        or "licence" in base
        or "license" in base
        or base == "banned_provider"
    ):
        failure_class = "rights_rejected"
    elif base in _VERIFICATION_REASONS or stage in (
        "technical_validation", "frame_verification",
        "transcript_verification",
    ) and base not in ("download_failed", "download_exception", ""):
        failure_class = "verification_failed"
        check = base or str(stage or "")
    elif base in _SOURCE_UNAVAILABLE_REASONS:
        failure_class = "source_unavailable"
        check = base
    elif base == "rate_limited" or base == "provider_backed_off":
        failure_class = "http_error"
        if status is None:
            status = 429
    elif (
        "timeout" in base
        or "timeout" in error_text.lower()
        or "timed out" in combined
    ):
        failure_class = "timeout"
    elif base == "http_error" or status is not None and base in (
        "download_failed", "download_exception", "http_error",
    ):
        failure_class = "http_error"
    elif base in ("network_error", "connection_error", "transport",
                  "download_failed", "download_exception"):
        failure_class = "network_error"
    elif base:
        failure_class = "unknown"

    retryable = failure_class in ("network_error", "timeout") or (
        failure_class == "http_error" and is_transient_http_status(status)
    )
    return {
        "failure_class": failure_class,
        "reason": raw_reason,
        "http_status": status,
        "check": check,
        "retryable": bool(retryable),
    }


def should_retry(failure_record_or_class, *, retries_done=0):
    """Whether one more attempt is permitted under the C-12 policy:
    only transient classes, and at most MAX_TRANSIENT_RETRIES."""
    if int(retries_done or 0) >= MAX_TRANSIENT_RETRIES:
        return False
    if isinstance(failure_record_or_class, dict):
        return bool(failure_record_or_class.get("retryable"))
    return str(failure_record_or_class or "") in ("network_error", "timeout")


def annotate_failure(failure, *, sentence_number=None):
    """Return a copy of a failure record dict stamped with its
    taxonomy fields (``failure_class`` / ``http_status`` /
    ``check``), preserving the record's existing context
    (provider, url, candidate_id, stage…). Idempotent: a record
    that already carries a failure_class is returned unchanged."""
    if not isinstance(failure, dict):
        return failure
    if failure.get("failure_class"):
        return failure
    classified = classify_download_failure(
        failure.get("reason"),
        http_status=failure.get("http_status"),
        detail=failure.get("detail") or failure.get("stop_reason"),
        stage=failure.get("stage"),
    )
    annotated = dict(failure)
    annotated["failure_class"] = classified["failure_class"]
    annotated["http_status"] = classified["http_status"]
    annotated["check"] = classified["check"]
    annotated["retryable"] = classified["retryable"]
    if sentence_number is not None and annotated.get("sentence_number") is None:
        annotated["sentence_number"] = sentence_number
    return annotated


def aggregate_failure_classes(failures):
    """Aggregate failure records BY CLASS (C-02): counts per class,
    per class×provider, and per class×HTTP-status. Records without
    a stamped class are classified on the fly — the aggregate is
    always complete, never a partial sum."""
    by_class = {}
    by_class_provider = {}
    by_class_status = {}
    total = 0
    for failure in failures or []:
        if not isinstance(failure, dict):
            continue
        annotated = annotate_failure(failure)
        failure_class = annotated.get("failure_class") or "unknown"
        provider = str(annotated.get("provider") or "unknown")
        by_class[failure_class] = by_class.get(failure_class, 0) + 1
        key = f"{failure_class}:{provider}"
        by_class_provider[key] = by_class_provider.get(key, 0) + 1
        if annotated.get("http_status") is not None:
            skey = f"{failure_class}:{annotated['http_status']}"
            by_class_status[skey] = by_class_status.get(skey, 0) + 1
        total += 1
    return {
        "total": total,
        "by_class": by_class,
        "by_class_provider": by_class_provider,
        "by_class_status": by_class_status,
    }
