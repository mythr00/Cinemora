"""Stock discovery providers — Pexels + Pixabay (Gate 2, WS4).

Founder decision 2026-11-26 (Annex B of
~/workspace/cinemora-audit/UNIFIED_VISUAL_PRODUCTION_AUDIT_2026-11-26.md):
the 2026-11-19 permanent ban (IA-05) on Pexels and Pixabay is LIFTED.
Both are restored as discovery/sourcing providers inside the existing
Assets Manager — stock videos AND photographs — integrated through
services.pipeline_discovery exactly like Archive.org (a provider
module + pipeline_discovery wrappers + registration in
_uve_scenario_search). No separate stock system.

API keys: both APIs require a key (a keyless request returns 401).
Keys are read from the environment at CALL time — PEXELS_API_KEY /
PIXABAY_API_KEY (documented in backend/.env.example; the deployment
supplies them via keys.env). NO key is configured at the time this
module was written, and none is invented here: without a key every
search is a silent no-op returning [] (one metrics note per provider
per process at most), never raising and never slowing the pipeline.
With a key, any provider error (transport, HTTP status, malformed
payload) likewise degrades to [] with the failure recorded — a stock
provider can never break discovery.

Illustrative-stock honesty (founder rule, Annex B rule 4 — HARD):
every candidate this module returns is stamped
``illustrative_stock: True`` and ``provenance: "illustrative_stock"``
in the same provenance/evidence position the other providers use for
their origin fields (source/provider/license/rights_status), so a
stock candidate can never be presented as authentic footage of a
real event or person. Stock candidates receive NO bypass anywhere:
the shared verification chain (named-person / event / date / social
gates in services.story_understanding) applies to them exactly as
to every other provider's candidates.

Licences are recorded honestly under the providers' real licence
names — "Pexels License" / "Pixabay Content License". Neither
licence requires attribution, but the creator's name and profile
URL are recorded on every candidate anyway (Annex B rule 5).
"""

from __future__ import annotations

import logging
import os

import requests

from .http_retry import request_with_retry

try:
    from . import provider_health
except ImportError:  # pragma: no cover - flat-layout fallback
    import provider_health

logger = logging.getLogger(__name__)

_PEXELS_VIDEOS_URL = "https://api.pexels.com/videos/search"
_PEXELS_IMAGES_URL = "https://api.pexels.com/v1/search"
_PIXABAY_VIDEOS_URL = "https://pixabay.com/api/videos/"
_PIXABAY_IMAGES_URL = "https://pixabay.com/api/"
_TIMEOUT = 25.0

_PEXELS_LICENSE = "Pexels License"
_PEXELS_LICENSE_URL = "https://www.pexels.com/license/"
_PIXABAY_LICENSE = "Pixabay Content License"
_PIXABAY_LICENSE_URL = "https://pixabay.com/service/license-summary/"

# Providers whose keyless no-op has already been noted in this
# process — the note is emitted at most once per provider per run.
_KEYLESS_NOTED: set = set()


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "Cinemora/1.0 (documentary research)"})
    return session


def _pexels_api_key() -> str:
    return str(os.getenv("PEXELS_API_KEY") or "").strip()


def _pixabay_api_key() -> str:
    return str(os.getenv("PIXABAY_API_KEY") or "").strip()


def _api_key(key: str, env_name: str, provider: str) -> str:
    """The provider's API key (read literally above), or "".

    Keyless is a configured-off state, not an error: note it once
    per provider per process and let the caller return []."""
    if not key and provider not in _KEYLESS_NOTED:
        _KEYLESS_NOTED.add(provider)
        logger.info(
            "stock_providers: %s not configured (%s unset) — "
            "stock search is a no-op until a key is supplied",
            provider, env_name,
        )
    return key


def _get_json(url, *, params=None, headers=None):
    """One provider GET via the shared retry helper, or None.

    A 429 is recorded against the provider's scope in
    provider_health (the same scope recording the other providers
    use) before the status is raised to the caller's failure
    isolation. Returns the parsed JSON dict, or None when the
    payload is not a JSON object."""
    session = _session()
    if headers:
        session.headers.update(headers)
    response = request_with_retry(
        "GET", url, session=session, params=params, timeout=_TIMEOUT,
    )
    if getattr(response, "status_code", None) == 429:
        provider_health.record_rate_limit(provider_health.scope_for_url(url))
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else None


def _stock_stamp(candidate: dict) -> dict:
    """The illustrative-stock honesty stamp (Annex B rule 4)."""
    candidate["illustrative_stock"] = True
    candidate["provenance"] = "illustrative_stock"
    return candidate


def _pick_pexels_video_file(video_files) -> dict | None:
    """Best practical Pexels video file: an MP4 at most 720p wide
    (matching the pipeline's 720p extraction), preferring the
    largest file at or under the cap; the smallest MP4 when every
    file exceeds it."""
    mp4s = []
    for entry in video_files or []:
        if not isinstance(entry, dict):
            continue
        link = str(entry.get("link") or "").strip()
        if not link:
            continue
        if str(entry.get("file_type") or "").lower() not in ("video/mp4", ""):
            if not link.lower().split("?", 1)[0].endswith(".mp4"):
                continue
        try:
            width = int(entry.get("width") or 0)
        except (TypeError, ValueError):
            width = 0
        mp4s.append((width, entry))
    if not mp4s:
        return None
    under = [pair for pair in mp4s if 0 < pair[0] <= 720]
    if under:
        under.sort(key=lambda pair: -pair[0])
        return under[0][1]
    mp4s.sort(key=lambda pair: pair[0] or 10 ** 9)
    return mp4s[0][1]


def _pick_pixabay_video_file(files) -> dict | None:
    """Best practical Pixabay video file: the largest variant at
    most 720p wide, else the smallest variant."""
    variants = []
    for entry in (files or {}).values():
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        try:
            width = int(entry.get("width") or 0)
        except (TypeError, ValueError):
            width = 0
        variants.append((width, entry))
    if not variants:
        return None
    under = [pair for pair in variants if 0 < pair[0] <= 720]
    if under:
        under.sort(key=lambda pair: -pair[0])
        return under[0][1]
    variants.sort(key=lambda pair: pair[0] or 10 ** 9)
    return variants[0][1]


def _duration(value):
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return None
    return seconds or None


def _dimension(value):
    try:
        return int(value or 0) or None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Pexels
# ---------------------------------------------------------------------------

def search_pexels_videos(query: str, limit: int = 5) -> list[dict]:
    """Pexels stock VIDEO candidates, normalised to the shared
    discovery candidate shape. Silent [] without a key or on any
    provider error — never raises into discovery."""
    query = str(query or "").strip()
    if not query:
        return []
    key = _api_key(_pexels_api_key(), "PEXELS_API_KEY", "pexels")
    if not key:
        return []
    try:
        payload = _get_json(
            _PEXELS_VIDEOS_URL,
            params={"query": query, "per_page": max(limit * 2, limit)},
            headers={"Authorization": key},
        )
    except Exception as error:
        logger.info("stock_providers: pexels video search failed: %s", error)
        return []
    if not payload:
        return []
    results = []
    for item in payload.get("videos") or []:
        if len(results) >= limit:
            break
        if not isinstance(item, dict):
            continue
        chosen = _pick_pexels_video_file(item.get("video_files"))
        if not chosen:
            continue
        download_url = str(chosen.get("link") or "").strip()
        page_url = str(item.get("url") or "").strip()
        if not download_url or not page_url:
            continue
        user = item.get("user") or {}
        creator = str(user.get("name") or "").strip()
        results.append(_stock_stamp({
            "title": query,
            "video_url": download_url,
            "url": page_url,
            "link": page_url,
            "source_url": page_url,
            "source": "pexels",
            "provider": "pexels",
            "kind": "video",
            "type": "video",
            "creator": creator,
            "channel": creator,
            "creator_url": str(user.get("url") or "").strip(),
            "license": _PEXELS_LICENSE,
            "license_url": _PEXELS_LICENSE_URL,
            "attribution_required": False,
            "rights_status": "recorded",
            "snippet": query,
            "duration_seconds": _duration(item.get("duration")),
            "duration": _duration(item.get("duration")),
            "width": _dimension(chosen.get("width")),
            "height": _dimension(chosen.get("height")),
            "matched_query": query,
        }))
    return results


def search_pexels_images(query: str, limit: int = 5) -> list[dict]:
    """Pexels stock PHOTOGRAPH candidates (see search_pexels_videos)."""
    query = str(query or "").strip()
    if not query:
        return []
    key = _api_key(_pexels_api_key(), "PEXELS_API_KEY", "pexels")
    if not key:
        return []
    try:
        payload = _get_json(
            _PEXELS_IMAGES_URL,
            params={"query": query, "per_page": max(limit * 2, limit)},
            headers={"Authorization": key},
        )
    except Exception as error:
        logger.info("stock_providers: pexels image search failed: %s", error)
        return []
    if not payload:
        return []
    results = []
    for item in payload.get("photos") or []:
        if len(results) >= limit:
            break
        if not isinstance(item, dict):
            continue
        src = item.get("src") or {}
        download_url = str(
            src.get("large") or src.get("original") or src.get("medium") or ""
        ).strip()
        page_url = str(item.get("url") or "").strip()
        if not download_url or not page_url:
            continue
        creator = str(item.get("photographer") or "").strip()
        alt = str(item.get("alt") or "").strip()
        results.append(_stock_stamp({
            "title": alt or query,
            "image_url": download_url,
            "url": download_url,
            "link": page_url,
            "source_url": page_url,
            "source": "pexels",
            "provider": "pexels",
            "kind": "image",
            "type": "image",
            "creator": creator,
            "creator_url": str(item.get("photographer_url") or "").strip(),
            "license": _PEXELS_LICENSE,
            "license_url": _PEXELS_LICENSE_URL,
            "attribution_required": False,
            "rights_status": "recorded",
            "snippet": alt or query,
            "width": _dimension(item.get("width")),
            "height": _dimension(item.get("height")),
            "matched_query": query,
        }))
    return results


# ---------------------------------------------------------------------------
# Pixabay
# ---------------------------------------------------------------------------

def search_pixabay_videos(query: str, limit: int = 5) -> list[dict]:
    """Pixabay stock VIDEO candidates, normalised to the shared
    discovery candidate shape. Silent [] without a key or on any
    provider error — never raises into discovery."""
    query = str(query or "").strip()
    if not query:
        return []
    key = _api_key(_pixabay_api_key(), "PIXABAY_API_KEY", "pixabay")
    if not key:
        return []
    try:
        payload = _get_json(
            _PIXABAY_VIDEOS_URL,
            params={"key": key, "q": query, "per_page": max(limit * 2, 3)},
        )
    except Exception as error:
        logger.info("stock_providers: pixabay video search failed: %s", error)
        return []
    if not payload:
        return []
    results = []
    for item in payload.get("hits") or []:
        if len(results) >= limit:
            break
        if not isinstance(item, dict):
            continue
        chosen = _pick_pixabay_video_file(item.get("videos"))
        if not chosen:
            continue
        download_url = str(chosen.get("url") or "").strip()
        page_url = str(item.get("pageURL") or "").strip()
        if not download_url or not page_url:
            continue
        creator = str(item.get("user") or "").strip()
        user_id = item.get("user_id")
        creator_url = (
            f"https://pixabay.com/users/{creator}-{user_id}/"
            if creator and user_id else ""
        )
        tags = str(item.get("tags") or "").strip()
        results.append(_stock_stamp({
            "title": tags or query,
            "video_url": download_url,
            "url": page_url,
            "link": page_url,
            "source_url": page_url,
            "source": "pixabay",
            "provider": "pixabay",
            "kind": "video",
            "type": "video",
            "creator": creator,
            "channel": creator,
            "creator_url": creator_url,
            "license": _PIXABAY_LICENSE,
            "license_url": _PIXABAY_LICENSE_URL,
            "attribution_required": False,
            "rights_status": "recorded",
            "snippet": tags or query,
            "duration_seconds": _duration(item.get("duration")),
            "duration": _duration(item.get("duration")),
            "width": _dimension(chosen.get("width")),
            "height": _dimension(chosen.get("height")),
            "matched_query": query,
        }))
    return results


def search_pixabay_images(query: str, limit: int = 5) -> list[dict]:
    """Pixabay stock PHOTOGRAPH candidates (see search_pixabay_videos)."""
    query = str(query or "").strip()
    if not query:
        return []
    key = _api_key(_pixabay_api_key(), "PIXABAY_API_KEY", "pixabay")
    if not key:
        return []
    try:
        payload = _get_json(
            _PIXABAY_IMAGES_URL,
            params={"key": key, "q": query, "per_page": max(limit * 2, 3)},
        )
    except Exception as error:
        logger.info("stock_providers: pixabay image search failed: %s", error)
        return []
    if not payload:
        return []
    results = []
    for item in payload.get("hits") or []:
        if len(results) >= limit:
            break
        if not isinstance(item, dict):
            continue
        download_url = str(
            item.get("largeImageURL") or item.get("webformatURL") or ""
        ).strip()
        page_url = str(item.get("pageURL") or "").strip()
        if not download_url or not page_url:
            continue
        creator = str(item.get("user") or "").strip()
        user_id = item.get("user_id")
        creator_url = (
            f"https://pixabay.com/users/{creator}-{user_id}/"
            if creator and user_id else ""
        )
        tags = str(item.get("tags") or "").strip()
        results.append(_stock_stamp({
            "title": tags or query,
            "image_url": download_url,
            "url": download_url,
            "link": page_url,
            "source_url": page_url,
            "source": "pixabay",
            "provider": "pixabay",
            "kind": "image",
            "type": "image",
            "creator": creator,
            "creator_url": creator_url,
            "license": _PIXABAY_LICENSE,
            "license_url": _PIXABAY_LICENSE_URL,
            "attribution_required": False,
            "rights_status": "recorded",
            "snippet": tags or query,
            "width": _dimension(item.get("imageWidth")),
            "height": _dimension(item.get("imageHeight")),
            "matched_query": query,
        }))
    return results
