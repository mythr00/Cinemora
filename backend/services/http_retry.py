"""Bounded HTTP retry with exponential backoff and Retry-After support.

Provider resilience (founder directive, 2026-11-19): a single rate-limited
provider must not terminate visual discovery. Serper already retries inside
``search_provider._post_with_retry``; the Wikimedia Commons, archive.org and
direct-download paths used bare ``requests`` calls, so one HTTP 429 killed
that request outright. This module gives those paths the same discipline:

* retry on HTTP 429 and 5xx, and on transport errors (connection / timeout);
* exponential backoff with a ceiling;
* honour the ``Retry-After`` header (seconds or HTTP-date) when present,
  capped so a hostile or broken header cannot stall the pipeline forever;
* strict attempt limit — after the budget is spent, the last response is
  returned (or the last transport exception re-raised) so callers keep
  their existing status handling and the failure is recorded, never hidden.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 1.0
DEFAULT_MAX_DELAY = 60.0
RETRY_STATUSES = (429, 500, 502, 503, 504)


def is_transient_status(status) -> bool:
    """Transient HTTP failure (Quality Recovery Phase 2, workstream D):
    429 and 5xx are worth retrying; every other 4xx is a definitive
    answer and is NOT retried. Matches the retry behaviour this module
    and search_provider already implement — this names it so failure
    records can carry the classification."""
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    return code == 429 or 500 <= code <= 599


def classify_error(error) -> dict:
    """Classify a provider/transport error for failure records:
    {error_class, http_status, transient}. Transport failures
    (timeout / connection) are transient; an HTTP error's class comes
    from its status; anything else is recorded by exception type with
    transient=None (unknown — never silently zero/false)."""
    status = None
    response = getattr(error, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
    if status is None and hasattr(error, "status_code"):
        status = getattr(error, "status_code")
    if status is not None:
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = None
    if status is not None:
        return {
            "error_class": f"http_{status}",
            "http_status": status,
            "transient": is_transient_status(status),
        }
    name = type(error).__name__ if error is not None else "UnknownError"
    lowered = name.lower()
    if "timeout" in lowered:
        return {"error_class": "timeout", "http_status": None, "transient": True}
    if "connection" in lowered or "transport" in lowered:
        return {"error_class": "transport", "http_status": None, "transient": True}
    return {"error_class": lowered, "http_status": None, "transient": None}


def parse_retry_after(value, now=None):
    """Return the Retry-After delay in seconds, or None if unparsable."""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (when - current).total_seconds())


def backoff_delay(attempt, base_delay=DEFAULT_BASE_DELAY, max_delay=DEFAULT_MAX_DELAY, retry_after=None):
    """Delay before the next attempt (attempt is 1-based for the failure)."""
    if retry_after is not None:
        return min(float(retry_after), max_delay)
    return min(base_delay * (2 ** (attempt - 1)), max_delay)


def request_with_retry(
    method,
    url,
    *,
    session=None,
    max_attempts=DEFAULT_MAX_ATTEMPTS,
    base_delay=DEFAULT_BASE_DELAY,
    max_delay=DEFAULT_MAX_DELAY,
    retry_statuses=RETRY_STATUSES,
    sleep=time.sleep,
    **kwargs,
):
    """Perform an HTTP request with bounded retries.

    Returns the final ``requests.Response`` — including a final 429/5xx
    response once the attempt budget is spent, so the caller's existing
    ``raise_for_status`` / status handling records the failure honestly.
    Transport exceptions are retried too; the last one is re-raised when
    the budget is spent.
    """
    attempts = max(1, int(max_attempts))
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            if session is not None:
                verb = getattr(session, method.lower(), None)
                if callable(verb):
                    response = verb(url, **kwargs)
                else:
                    response = session.request(method, url, **kwargs)
            else:
                response = requests.request(method, url, **kwargs)
        except requests.RequestException as error:
            last_exc = error
            if attempt >= attempts:
                raise
            sleep(backoff_delay(attempt, base_delay, max_delay))
            continue
        status = getattr(response, "status_code", 200)
        if status not in retry_statuses or attempt >= attempts:
            return response
        retry_after = parse_retry_after(
            (getattr(response, "headers", None) or {}).get("Retry-After")
        )
        sleep(backoff_delay(attempt, base_delay, max_delay, retry_after))
    if last_exc is not None:  # pragma: no cover - loop always returns/raises
        raise last_exc
    raise RuntimeError("request_with_retry: no attempt was made")
