"""Per-production provider download health (run-11 coverage fix 2).

Run 11's measured funnel: 105 downloads attempted, 16 succeeded.
The failures were not evenly spread — they concentrated by
provider scope: google.com/goto wrappers 5 downloaded / 84 failed,
YouTube 0 / 12 (with repeated HTTP 429s in the worker log), while
Wikimedia went 1 / 0 and the long tail of direct hosts mostly
delivered or failed once. The pipeline had no memory of any of
this: it kept handing candidates to scopes that were actively
rate-limiting it, retrying into the ground, while verified
candidates from healthy scopes waited undrawn behind them.

This module is that memory, scoped to ONE production (reset by
``reset_media_caches`` at every production start):

- every real download attempt's outcome is recorded per provider
  scope (the fetch URL's host, ``www.`` stripped), so per-provider
  success is a measured fact of the run, available via
  :func:`snapshot`;
- HTTP 429 observations are recorded where they are actually seen
  (yt-dlp info, transcript downloads, clip extraction, image
  downloads). ``RATE_LIMIT_TRIP_COUNT`` consecutive 429s from one
  scope backs that scope off for the rest of the production:
  :func:`is_backed_off` is consulted before every fetch, and a
  backed-off scope's candidates are refused with the typed
  ``provider_backed_off`` reason — no request is made, the
  candidate is NOT marked as a failed download (the scope is at
  fault, not the candidate), and the draw moves on to candidates
  from scopes that actually deliver;
- a successful download from a scope clears its consecutive-429
  count (the limit was transient); a non-429 failure does not
  advance it (a dead URL is not a rate limit).

Nothing here changes a verdict, a score or a budget: it only
decides whether a fetch is attempted at all, and records why not.
"""

import threading
from urllib.parse import urlparse

#: Consecutive HTTP 429s from one provider scope after which the
#: scope is backed off for the rest of the production.
RATE_LIMIT_TRIP_COUNT = 3

_LOCK = threading.Lock()
_SCOPES = {}


def scope_for_url(url):
    """The provider scope for a fetch URL: its host, lowercased,
    with a leading ``www.`` stripped. Empty string for no URL."""
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _entry(scope):
    return _SCOPES.setdefault(scope, {
        "attempts": 0,
        "succeeded": 0,
        "failed": 0,
        "rate_limited": 0,
        "consecutive_rate_limits": 0,
        "backed_off": False,
        "backed_off_reason": None,
    })


def record_download_outcome(scope, ok):
    """Record one real download attempt's outcome for a scope."""
    if not scope:
        return
    with _LOCK:
        entry = _entry(scope)
        entry["attempts"] += 1
        if ok:
            entry["succeeded"] += 1
            entry["consecutive_rate_limits"] = 0
        else:
            entry["failed"] += 1


def record_rate_limit(scope):
    """Record one observed HTTP 429 from a scope. Trips the
    production backoff at RATE_LIMIT_TRIP_COUNT consecutive 429s;
    returns True when this observation tripped (or the scope was
    already backed off)."""
    if not scope:
        return False
    with _LOCK:
        entry = _entry(scope)
        entry["rate_limited"] += 1
        entry["consecutive_rate_limits"] += 1
        if (
            entry["consecutive_rate_limits"] >= RATE_LIMIT_TRIP_COUNT
            and not entry["backed_off"]
        ):
            entry["backed_off"] = True
            entry["backed_off_reason"] = (
                f"{entry['consecutive_rate_limits']} consecutive "
                "HTTP 429 responses"
            )
        return entry["backed_off"]


def is_backed_off(scope):
    """Whether the scope is backed off for the rest of this
    production."""
    if not scope:
        return False
    with _LOCK:
        entry = _SCOPES.get(scope)
        return bool(entry and entry["backed_off"])


def snapshot():
    """A deep-enough copy of the per-scope health table, for the
    production record: attempts / succeeded / failed /
    rate_limited / backed_off per provider scope."""
    with _LOCK:
        return {scope: dict(entry) for scope, entry in _SCOPES.items()}


def reset():
    """Clear all health state (called at every production start,
    alongside the other per-run caches)."""
    with _LOCK:
        _SCOPES.clear()
