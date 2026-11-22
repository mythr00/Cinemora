"""URL hygiene at discovery (run-11 coverage fix 1, 2026-11-22).

Run 11 measured the failure this module exists to prevent: Serper
video results arrive with ``google.com/goto?url=<opaque token>``
redirect wrappers as their link. The pipeline treated the wrapper
as the candidate's URL, so the segment-evidence stage handed the
wrapper itself to yt-dlp ("unknown duration" / "no timestamp
evidence") and 69 of the run's 108 download failures were
``no_segment_evidence`` deaths on URLs that were never real media
addresses at all. A redirector wrapper is not a destination: it
must be resolved to the real destination before the candidate is
scored, verified or fetched, and when resolution fails the
candidate is rejected early with a typed reason instead of burning
segment-evidence and download attempts on a dead wrapper.

Two resolution routes, in order:

- **Decoded** — some wrappers carry the destination in their own
  query parameters (Google ``/url?q=``, Facebook ``l.php?u=``,
  DuckDuckGo ``/l/?uddg=``, YouTube ``/redirect?q=``, Bing
  ``/ck/a?u=a1<base64>``). The destination is recovered locally,
  no network involved.
- **Resolved** — opaque wrappers (Google ``/goto``) carry a signed
  token whose payload contains no readable destination (verified
  against run 11's records: the token base64-decodes to binary
  with no URL inside). The only honest resolution is to follow
  the redirect over HTTP (HEAD first, streamed GET as fallback)
  and take the final URL. Resolutions — successes and failures —
  are cached per process, so repeated passes over the same
  wrapper cost one HTTP follow per production.

This module never guesses a destination: when neither route
produces one, the status is ``failed`` and the caller rejects the
candidate (rejection_diagnostics.UNRESOLVABLE_REDIRECTOR_REASON).
"""

import base64
import threading
from urllib.parse import parse_qs, unquote, urlparse

import requests

STATUS_NOT_REDIRECTOR = "not_redirector"
STATUS_DECODED = "decoded"
STATUS_RESOLVED = "resolved"
STATUS_FAILED = "failed"

HTTP_TIMEOUT_SECONDS = 10.0

_GOOGLE_HOSTS = {"google.com", "www.google.com"}
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}

# wrapper URL -> (final URL or None, status). Failures are cached
# too: a dead wrapper stays dead for the rest of the production,
# and re-resolving it on every pass would re-burn the very time
# this fix exists to save.
_RESOLUTION_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _host(url):
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def _path(url):
    try:
        return urlparse(str(url or "")).path or ""
    except ValueError:
        return ""


def _query_params(url):
    try:
        return parse_qs(urlparse(str(url or "")).query)
    except ValueError:
        return {}


def _first_param(params, *names):
    for name in names:
        values = params.get(name)
        if values and str(values[0]).strip():
            return str(values[0]).strip()
    return ""


def is_redirector_url(url):
    """Whether ``url`` is a known redirector wrapper rather than a
    real destination. Opaque wrappers (Google /goto) count: they
    are redirectors even though their destination is not locally
    recoverable."""
    text = str(url or "").strip()
    if not text:
        return False
    host = _host(text)
    path = _path(text)
    if host in _GOOGLE_HOSTS and path in ("/goto", "/url"):
        return True
    if host == "l.facebook.com" and path == "/l.php":
        return True
    if host in ("duckduckgo.com", "www.duckduckgo.com") and path.startswith("/l/"):
        return True
    if host in _YOUTUBE_HOSTS and path == "/redirect":
        return True
    if host in ("bing.com", "www.bing.com") and path == "/ck/a":
        return True
    return False


def _local_destination(url):
    """The destination embedded in the wrapper's own parameters,
    or None when the wrapper is opaque (or its embedded value is
    not itself a plain http(s) URL)."""
    host = _host(url)
    path = _path(url)
    params = _query_params(url)
    candidate = ""
    if host in _GOOGLE_HOSTS and path == "/url":
        candidate = _first_param(params, "q", "url")
    elif host == "l.facebook.com" and path == "/l.php":
        candidate = _first_param(params, "u")
    elif host in ("duckduckgo.com", "www.duckduckgo.com") and path.startswith("/l/"):
        candidate = _first_param(params, "uddg")
    elif host in _YOUTUBE_HOSTS and path == "/redirect":
        candidate = _first_param(params, "q")
    elif host in ("bing.com", "www.bing.com") and path == "/ck/a":
        raw = _first_param(params, "u")
        if raw.startswith("a1") and len(raw) > 2:
            token = raw[2:]
            try:
                decoded = base64.urlsafe_b64decode(
                    token + "=" * (-len(token) % 4)
                ).decode("utf-8", "replace")
            except Exception:
                decoded = ""
            if decoded.startswith(("http://", "https://")):
                candidate = decoded
    if candidate:
        candidate = unquote(candidate).strip()
        if candidate.startswith(("http://", "https://")):
            return candidate
    return None


def _http_follow(url):
    """Follow a redirector wrapper over HTTP and return the final
    destination URL, or None when the chain does not end at a
    non-redirector destination. HEAD first (no body transfer);
    a streamed GET that is closed immediately is the fallback for
    endpoints that refuse HEAD. Module-level so tests can stub
    the network at this one seam."""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.head(
            url, allow_redirects=True,
            timeout=HTTP_TIMEOUT_SECONDS, headers=headers,
        )
        final = str(response.url or "").strip()
        if final and final != url and not is_redirector_url(final):
            return final
    except requests.RequestException:
        pass
    try:
        with requests.get(
            url, allow_redirects=True, stream=True,
            timeout=HTTP_TIMEOUT_SECONDS, headers=headers,
        ) as response:
            final = str(response.url or "").strip()
            if final and final != url and not is_redirector_url(final):
                return final
    except requests.RequestException:
        pass
    return None


def resolve_candidate_url(url):
    """Resolve one candidate URL.

    Returns ``(final_url, status)``: for a non-redirector the URL
    itself with ``not_redirector``; for a wrapper, the recovered
    destination with ``decoded`` / ``resolved``; for a wrapper
    whose destination cannot be recovered, ``(None, "failed")``.
    """
    text = str(url or "").strip()
    if not text or not is_redirector_url(text):
        return text, STATUS_NOT_REDIRECTOR
    with _CACHE_LOCK:
        cached = _RESOLUTION_CACHE.get(text)
    if cached is not None:
        return cached
    local = _local_destination(text)
    if local and not is_redirector_url(local):
        result = (local, STATUS_DECODED)
    else:
        final = _http_follow(text)
        result = (
            (final, STATUS_RESOLVED) if final else (None, STATUS_FAILED)
        )
    with _CACHE_LOCK:
        _RESOLUTION_CACHE[text] = result
    return result


def clear_resolution_cache():
    """Drop every cached resolution (production boundaries and
    tests)."""
    with _CACHE_LOCK:
        _RESOLUTION_CACHE.clear()
