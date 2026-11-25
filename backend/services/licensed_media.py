from __future__ import annotations

import logging
import re
import hashlib
import time
from pathlib import Path
from urllib.parse import quote_plus

import requests

from .visual_policy import VisualRequest, reject_candidate
from .http_retry import request_with_retry

try:
    from . import run_metrics
except ImportError:  # pragma: no cover - flat-layout fallback
    import run_metrics

try:
    from . import provider_health
except ImportError:  # pragma: no cover - flat-layout fallback
    import provider_health

logger = logging.getLogger(__name__)

UA = "DocumentaryStudio/0.3 (local research tool)"

COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


def _commons_request(url, *, params=None, timeout=30, stream=False):
    """One Wikimedia Commons request with Gate 2 retry-before-
    backoff (footage-discovery directive, 2026-11-26).

    Commons throttling used to silence the only licence-evidenced
    source for a whole production: a 429 burst tripped the
    provider_health scope backoff and every later candidate was
    refused pre-request. Here the request first makes a small
    number of BOUNDED DELAYED retries — honouring Retry-After,
    capped via provider_health.WIKIMEDIA_MAX_RETRY_DELAY_SECONDS
    so the total delay stays inside the caller's existing timeout
    budget — and each delayed retry is recorded in
    provider_health's metrics. Only a request that is STILL 429
    after those retries is recorded as a rate limit, so a
    persistent 429 stream still backs the scope off exactly as
    before; a transient burst that a retry rides out never reaches
    the backoff accounting at all.
    """
    scope = provider_health.scope_for_url(url)

    def _recorded_sleep(delay):
        provider_health.record_delayed_retry(scope, delay)
        time.sleep(delay)

    kwargs = {"timeout": timeout}
    if params is not None:
        kwargs["params"] = params
    if stream:
        kwargs["stream"] = True
    response = request_with_retry(
        "GET",
        url,
        session=_session(),
        max_attempts=1 + provider_health.WIKIMEDIA_RETRY_ATTEMPTS,
        base_delay=0.5,
        max_delay=provider_health.WIKIMEDIA_MAX_RETRY_DELAY_SECONDS,
        sleep=_recorded_sleep,
        **kwargs,
    )
    if getattr(response, "status_code", None) == 429:
        # The bounded retries are spent and Commons is still
        # throttling: this is backoff evidence, recorded once per
        # exhausted request (never per raw 429).
        provider_health.record_rate_limit(scope)
    return response


def search_licensed(req: VisualRequest, limit: int = 6) -> list[dict]:
    """Licensed stills search — Wikimedia Commons ONLY.

    IA-05 (founder's PERMANENT ban, 2026-11-19): the Pexels/Pixabay
    provider implementations that used to be wired in here were removed.
    They must not be reintroduced. Commons video is available separately
    via search_wikimedia_videos; this entry point returns no video hits.
    """
    hits: list[dict] = []
    if req.kind == "graphic":
        return hits
    if req.kind == "video":
        return hits
    hits += _wikimedia(req.query, limit)

    clean = []
    for h in hits:
        reason = reject_candidate(h, req)
        if reason:
            h["rejected"] = reason
            continue
        clean.append(h)
    return clean


def download_asset(hit: dict, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = hit["download_url"]
    ext = Path(url.split("?")[0]).suffix.lower() or (".mp4" if hit["kind"] == "video" else ".jpg")
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm"}:
        ext = ".mp4" if hit["kind"] == "video" else ".jpg"
    name = hashlib.sha256(url.encode()).hexdigest()[:16] + ext
    path = dest_dir / name
    if path.exists() and path.stat().st_size > 1024:
        return path
    if provider_health.is_wikimedia_scope(
        provider_health.scope_for_url(url)
    ):
        # Commons downloads get the same retry-before-backoff
        # discipline as Commons searches (Gate 2).
        r = _commons_request(url, timeout=60, stream=True)
    else:
        r = request_with_retry("GET", url, session=_session(), timeout=60, stream=True)
    r.raise_for_status()
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(64 * 1024):
            f.write(chunk)
    tmp.replace(path)
    return path


def search_wikimedia(query: str, limit: int = 6) -> list[dict]:
    """Public Wikimedia Commons search (PD / CC stills with full licence
    evidence). Added for the corrected Asset Manager (AUD-15): Wikimedia is
    the one retrieval source whose licence, licence URL and creator are
    captured from the provider itself. Pexels/Pixabay stay unwired per the
    founder's permanent restriction (2026-11-19)."""
    # Stage 0 metrics: one Commons search invocation.
    run_metrics.wikimedia_search()
    return _wikimedia(query, limit)


def _wikimedia(q: str, limit: int) -> list[dict]:
    """PD / CC stills only. Useful later for verified archival images."""
    r = _commons_request(
        COMMONS_API_URL,
        params={
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": q,
            "gsrnamespace": 6,
            "gsrlimit": limit,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|mime|size",
            "iiurlwidth": 1920,
        },
        timeout=30,
    )
    r.raise_for_status()
    pages = ((r.json().get("query") or {}).get("pages") or {})
    allowed = ("public domain", "cc0", "cc-zero", "cc by", "cc-by")
    blocked = ("noncommercial", "no derivatives", "fair use")
    out = []
    for page in pages.values():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        meta = info.get("extmetadata") or {}
        license_short = (meta.get("LicenseShortName") or {}).get("value", "")
        license_low = license_short.lower()
        if any(b in license_low for b in blocked):
            continue
        if not any(a in license_low for a in allowed):
            continue
        mime = (info.get("mime") or "")
        if not mime.startswith("image/"):
            continue
        out.append(
            {
                "kind": "image",
                "source": "wikimedia",
                "source_url": info.get("descriptionurl"),
                "creator": (meta.get("Artist") or {}).get("value", ""),
                "license": license_short,
                "license_url": (meta.get("LicenseUrl") or {}).get("value", ""),
                "title": page.get("title", q),
                "alt": re.sub("<[^>]+>", "", (meta.get("ImageDescription") or {}).get("value", "")),
                "tags": "",
                "download_url": info.get("thumburl") or info.get("url"),
            }
        )
    return out

def search_wikimedia_videos(query: str, limit: int = 5) -> list[dict]:
    """Wikimedia Commons VIDEO discovery (MS-4).

    Mirrors _wikimedia's metadata handling but for moving images, and
    WITHOUT the stills licence pre-filter: every result keeps the
    licence exactly as Commons records it (NC/ND included, honestly
    labelled) — reuse decisions belong to the rights report, not to
    silent discovery filtering. Prefers the smallest MP4 derivative so
    downloads stay practical; falls back to the original file.
    """
    # Stage 0 metrics: one Commons search invocation.
    run_metrics.wikimedia_search()
    r = _commons_request(
        COMMONS_API_URL,
        params={
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": f"filetype:video {query}",
            "gsrnamespace": 6,
            "gsrlimit": limit,
            "prop": "videoinfo",
            "viprop": "url|extmetadata|derivatives|size|mime",
        },
        timeout=30,
    )
    r.raise_for_status()
    pages = ((r.json().get("query") or {}).get("pages") or {})
    out = []
    for page in pages.values():
        infos = page.get("videoinfo") or []
        if not infos:
            continue
        info = infos[0]
        meta = info.get("extmetadata") or {}
        derivatives = [
            d for d in (info.get("derivatives") or [])
            if str(d.get("type") or "").startswith("video/")
            or str(d.get("src") or "").lower().endswith(".mp4")
        ]
        download_url = ""
        if derivatives:
            derivatives.sort(key=lambda d: int(d.get("width") or 10**9))
            download_url = str(derivatives[0].get("src") or "")
        if not download_url:
            download_url = str(info.get("url") or "")
        if not download_url:
            continue
        # VA-03: Commons videoinfo carries the file's duration when
        # the handler provides it — provider timing metadata used as
        # a duration fallback downstream. Absent stays absent.
        try:
            video_duration = float(info.get("duration") or 0) or None
        except (TypeError, ValueError):
            video_duration = None
        out.append(
            {
                "kind": "video",
                "duration_seconds": video_duration,
                "source": "wikimedia",
                "source_url": info.get("descriptionurl"),
                "creator": (meta.get("Artist") or {}).get("value", ""),
                "license": (meta.get("LicenseShortName") or {}).get("value", ""),
                "license_url": (meta.get("LicenseUrl") or {}).get("value", ""),
                "title": page.get("title", query),
                "alt": re.sub("<[^>]+>", "", (meta.get("ImageDescription") or {}).get("value", "")),
                "download_url": download_url,
            }
        )
    return out
