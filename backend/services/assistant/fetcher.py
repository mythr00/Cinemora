"""The guarded page fetcher behind `retrieve_resource_details`
(design assistant/08 §6.4).

Rules, enforced here in code: http/https only; standard ports
only; the hostname must resolve exclusively to public addresses
(private, loopback, link-local, reserved and multicast ranges are
refused — which also makes the backend's own API unreachable);
redirects are followed manually, re-validating every hop; size
and time caps; HTML/text content types only. What comes back is
metadata + a text excerpt, returned to the model strictly as data.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from services import url_hygiene

MAX_BYTES = 512_000
TIMEOUT_SECONDS = 10
MAX_REDIRECTS = 3
_ALLOWED_PORTS = (None, 80, 443)
_ALLOWED_CONTENT_TYPES = ("text/html", "text/plain", "application/xhtml+xml")


class FetchRefused(Exception):
    """The fetch was refused by the guardrails (not a site error)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _host_is_public(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError):
        return False
    addresses = {info[4][0] for info in infos if info and info[4]}
    if not addresses:
        return False
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return False
    return True


def validate_url(url: str) -> str:
    """Return the URL to fetch, or raise FetchRefused."""
    text = str(url or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise FetchRefused("Only http/https URLs can be fetched.")
    if not parsed.hostname:
        raise FetchRefused("The URL has no host.")
    if parsed.port not in _ALLOWED_PORTS:
        raise FetchRefused("Only standard ports (80/443) can be fetched.")
    if parsed.username or parsed.password:
        raise FetchRefused("URLs with embedded credentials are refused.")
    if not _host_is_public(parsed.hostname):
        raise FetchRefused(
            "The host does not resolve to a public address; "
            "private and loopback destinations are refused."
        )
    return text


def _http_get(url: str):
    """The transport seam (tests stub this). No redirects at this
    layer — the caller validates every hop."""
    return requests.get(
        url,
        timeout=TIMEOUT_SECONDS,
        allow_redirects=False,
        headers={"User-Agent": "CinemoraAssistant/1.0 (metadata fetch)"},
        stream=True,
    )


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title_parts: list[str] = []
        self.description: str | None = None
        self.text_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
        if tag == "meta":
            attributes = dict(attrs)
            if (
                attributes.get("name", "").lower() == "description"
                or attributes.get("property", "").lower() == "og:description"
            ) and attributes.get("content"):
                self.description = attributes["content"]

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in ("script", "style", "noscript") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        elif not self._skip_depth:
            self.text_parts.append(data)


def fetch_resource(url: str) -> dict:
    """Fetch metadata + excerpt for one URL under the guardrails.
    Refusals raise FetchRefused with the reason; transport failures
    are reported in the result, not raised — the caller states
    what happened, never invents content."""
    resolved, status = url_hygiene.resolve_candidate_url(url)
    if resolved is None:
        raise FetchRefused(
            "The URL is a redirect wrapper whose destination could "
            "not be recovered."
        )
    current = validate_url(resolved)
    response = None
    for _hop in range(MAX_REDIRECTS + 1):
        response = _http_get(current)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            if not location:
                break
            current = validate_url(urljoin(current, location))
            continue
        break
    if response is None:
        return {"url": current, "fetch_status": "no_response"}
    content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    result = {
        "url": current,
        "resolved_from": url if url != current else None,
        "fetch_status": f"http_{response.status_code}",
        "content_type": content_type or None,
    }
    if response.status_code != 200:
        return result
    if content_type and not content_type.startswith(_ALLOWED_CONTENT_TYPES):
        result["note"] = (
            "Content type is not HTML/text, so no content was extracted."
        )
        return result
    raw = b""
    try:
        for chunk in response.iter_content(chunk_size=64_000):
            raw += chunk
            if len(raw) >= MAX_BYTES:
                raw = raw[:MAX_BYTES]
                result["truncated"] = True
                break
    except Exception as error:
        result["fetch_status"] = f"read_error:{type(error).__name__}"
        return result
    text = raw.decode("utf-8", errors="replace")
    if content_type.startswith("text/html") or "<html" in text[:500].lower():
        parser = _MetaParser()
        try:
            parser.feed(text)
        except Exception:
            pass
        result["title"] = " ".join(
            part.strip() for part in parser.title_parts if part.strip()
        ) or None
        result["description"] = parser.description
        excerpt = " ".join(
            part.strip() for part in parser.text_parts if part.strip()
        )
        result["excerpt"] = re.sub(r"\s+", " ", excerpt)[:2000] or None
    else:
        result["title"] = None
        result["description"] = None
        result["excerpt"] = re.sub(r"\s+", " ", text)[:2000] or None
    return result
