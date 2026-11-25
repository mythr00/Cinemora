"""Full-text extraction for recorded research sources
(GitHub Skills Research & Enhancement Programme, 2026-11-25).

WHY THIS EXISTS. The bible research pass (story_understanding.
enrich_bible_with_research) attaches sources to entities as
{url, title, snippet, retrieved_at} — and the support/conflict
checks then read the SNIPPET: two or three sentences a search
provider chose. That is thin evidence. This module is the missing
full-text layer: for an attached source it fetches the page (or
PDF) and extracts its main text, so corroboration reads what the
source actually says.

WHAT IT IS NOT. It is not a fact-checker and it verifies nothing
by itself: extraction widens the recorded evidence, and the
existing support/conflict machinery (which never resolves a
conflict and never upgrades a claim beyond "externally_supported"
on recorded agreement) decides what the evidence means. A failed
extraction is recorded as failed; the snippet stays the evidence.

TOOLS (Lane 3 evaluation, all permissive licences):
* trafilatura (Apache-2.0) — HTML main-text + metadata extraction.
* pypdf (BSD-3) — PDF text; pdfplumber (MIT) as the bounded
  fallback when pypdf yields almost nothing (layout-heavy PDFs),
  page-capped because it is ~10x slower.
* PyMuPDF was REJECTED for this role: AGPL-3.0/commercial dual,
  and the vendor's own SaaS interpretation would require a paid
  licence for Cinemora's closed-source deployment.

SAFETY. Fetching arbitrary URLs from a worker has an SSRF
surface, so:
* the pass is OPT-IN per deployment
  (CINEMORA_SOURCE_EXTRACTION_ENABLED=1; the production Docker
  image sets it deliberately — the same closed-by-default posture
  as the operator-token window);
* only http/https, no URL credentials, and the host must resolve
  exclusively to public addresses (loopback / private /
  link-local / reserved / multicast are refused), re-checked on
  every redirect hop (max 3);
* responses are size-capped (5 MB) and time-capped.
Residual, recorded honestly: DNS is resolved for the guard and
again by the HTTP stack (a rebinding race is theoretically
possible); the fetched set is search-result URLs for researched
subjects, which bounds the practical exposure.

FAIL-SOFT. extract_from_url never raises: every outcome is a
record dict {status, extractor, text, title, date, char_count,
reason} with status extracted | failed | refused | disabled.
"""

import ipaddress
import os
import socket
from io import BytesIO
from urllib.parse import urlparse

import requests

MAX_BYTES = 5_000_000
MAX_TEXT_CHARS = 20_000
MAX_REDIRECTS = 3
MAX_PDF_PAGES_PYPDF = 50
MAX_PDF_PAGES_PLUMBER = 20
_PDF_MIN_CHARS = 200
_TIMEOUT = (3.05, 15)
_USER_AGENT = "CinemoraResearch/1.0 (documentary source extraction)"


def extraction_enabled():
    """Deployment opt-in for outbound source fetching (see the
    module docstring for why this is closed by default)."""
    return os.environ.get(
        "CINEMORA_SOURCE_EXTRACTION_ENABLED", "0").strip() == "1"


def _record(status, extractor=None, text="", title=None, date=None,
            char_count=0, reason="", date_iso=None):
    return {
        "status": status,
        "extractor": extractor,
        "text": text,
        "title": title,
        "date": date,
        "date_iso": date_iso,
        "char_count": char_count,
        "reason": reason,
    }


def _normalise_date(raw):
    """ISO (YYYY-MM-DD) for a source's STATED date string, via
    dateparser — or None when it does not parse.

    This is dateparser's one bounded role (Lane 3 evaluation):
    source metadata dates arrive in wild formats, and a recorded
    ISO form makes them comparable. It is never used on the
    script chronology path (script_facts normalises
    deterministically from its own matched components). The raw
    string is always kept alongside; an unparseable date stays
    unparsed — no date is invented. Input is length-capped
    (metadata strings only, never documents)."""
    text = str(raw or "").strip()
    if not text or len(text) > 64:
        return None
    try:
        import dateparser

        parsed = dateparser.parse(text)
    except Exception:
        return None
    if parsed is None:
        return None
    return parsed.date().isoformat()


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------

def _ip_is_public(ip_text):
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def guard_url(url):
    """None when the URL may be fetched, else a refusal reason.
    The host must resolve ONLY to public addresses."""
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in ("http", "https"):
        return "scheme_not_allowed"
    if parsed.username or parsed.password:
        return "url_credentials_refused"
    host = parsed.hostname
    if not host:
        return "host_missing"
    try:
        ipaddress.ip_address(host)
        return None if _ip_is_public(host) else "host_not_public"
    except ValueError:
        pass  # a hostname: resolve and check every answer
    try:
        answers = socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80),
            proto=socket.IPPROTO_TCP,
        )
    except Exception:
        return "dns_failed"
    ips = {answer[4][0] for answer in answers}
    if not ips:
        return "dns_failed"
    if not all(_ip_is_public(ip) for ip in ips):
        return "host_not_public"
    return None


# ---------------------------------------------------------------------------
# Fetch (guarded, redirect-revalidating, size/time capped)
# ---------------------------------------------------------------------------

def _fetch(url):
    """Returns (status_code, content_type, body_bytes) or raises
    _Refused(reason) / requests exceptions for the caller to record."""
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        reason = guard_url(current)
        if reason:
            raise _Refused(reason)
        response = requests.get(
            current,
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT,
            stream=True,
            allow_redirects=False,
        )
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    return response.status_code, "", b""
                from urllib.parse import urljoin

                current = urljoin(current, location)
                continue
            body = b""
            for chunk in response.iter_content(chunk_size=64 * 1024):
                body += chunk
                if len(body) > MAX_BYTES:
                    raise _Refused("response_too_large")
            return (
                response.status_code,
                response.headers.get("Content-Type", ""),
                body,
            )
        finally:
            response.close()
    raise _Refused("too_many_redirects")


class _Refused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Extractors (pure over bytes — directly testable)
# ---------------------------------------------------------------------------

def extract_html(html_text, url=""):
    """trafilatura main-text + metadata over an HTML document."""
    import trafilatura

    text = trafilatura.extract(
        html_text,
        url=url or None,
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    title = None
    date = None
    try:
        metadata = trafilatura.extract_metadata(html_text)
        if metadata is not None:
            title = getattr(metadata, "title", None)
            date = getattr(metadata, "date", None)
    except Exception:
        pass
    if not text or not text.strip():
        return _record("failed", extractor="trafilatura",
                       reason="no_main_text_extracted")
    text = text.strip()
    return _record(
        "extracted", extractor="trafilatura",
        text=text[:MAX_TEXT_CHARS], title=title, date=date,
        date_iso=_normalise_date(date),
        char_count=len(text),
    )


def extract_pdf(data):
    """pypdf first; pdfplumber (page-capped) when pypdf yields
    almost nothing — layout-heavy PDFs are its strength."""
    text = ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        parts = []
        for page in reader.pages[:MAX_PDF_PAGES_PYPDF]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(parts).strip()
    except Exception:
        text = ""
    if len(text) >= _PDF_MIN_CHARS:
        return _record(
            "extracted", extractor="pypdf",
            text=text[:MAX_TEXT_CHARS], char_count=len(text),
        )
    try:
        import pdfplumber

        parts = []
        with pdfplumber.open(BytesIO(data)) as pdf:
            for page in pdf.pages[:MAX_PDF_PAGES_PLUMBER]:
                try:
                    parts.append(page.extract_text() or "")
                except Exception:
                    continue
        plumber_text = "\n".join(parts).strip()
    except Exception:
        plumber_text = ""
    if len(plumber_text) > len(text):
        text = plumber_text
        extractor = "pdfplumber"
    else:
        extractor = "pypdf"
    if not text:
        return _record("failed", extractor=extractor,
                       reason="no_pdf_text_extracted")
    return _record(
        "extracted", extractor=extractor,
        text=text[:MAX_TEXT_CHARS], char_count=len(text),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def extract_from_url(url, *, _fetch_fn=None):
    """Fetch + extract one recorded source URL. Never raises."""
    url = str(url or "").strip()
    if not url:
        return _record("refused", reason="url_missing")
    fetch = _fetch_fn or _fetch
    try:
        status_code, content_type, body = fetch(url)
    except _Refused as refused:
        return _record("refused", reason=refused.reason)
    except Exception as error:
        return _record(
            "failed", reason=f"fetch_error:{type(error).__name__}")
    if status_code != 200:
        return _record("failed", reason=f"http_{status_code}")
    if not body:
        return _record("failed", reason="empty_response")
    if body[:5] == b"%PDF-" or "application/pdf" in (content_type or ""):
        return extract_pdf(body)
    try:
        html_text = body.decode("utf-8", errors="replace")
    except Exception:
        return _record("failed", reason="undecodable_response")
    return extract_html(html_text, url=url)
