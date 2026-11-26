"""Search provider (Master Project Directive Phase 4c / D-08;
Master Implementation Directive 2026-11-26 — SearXNG primary;
founder decision 2026-11-26 — the former paid provider removed
completely).

This module is the ONE search choke point for the whole platform
(web / images / videos). Every live caller reaches search through
here: the request construction, the bounded concurrency / retry
policies, the result-cache and the call metrics all live in this
single place. (The former duplicate client
``services/asset_finder.py`` had zero importers anywhere in the
repo and was removed in Phase 4c — see the D-08 census in
PHASE4-CAPABILITY-DECISIONS.md's companion report / commit.)

SOLE PROVIDER (2026-11-26). SearXNG (self-hosted, no API key) is
the platform's only search provider. The former paid provider
was removed completely by founder decision: there is no second
provider, no fallback chain and no automatic provider switching
anywhere in the system. Results are normalised into the result shapes the
pipeline has always consumed, deduplicated by URL, and carry the
supplying engine in ``source`` plus ``domain`` / ``category`` /
``retrieved_at`` metadata.

When SearXNG is unconfigured (``SEARXNG_URL`` unset) or
unavailable (transport/HTTP failure after the bounded retries, a
malformed body, or the circuit breaker open), a search fails
with the typed :class:`SearchProviderError` — it never silently
switches providers and never fabricates results. A failed
instance trips a circuit breaker
(``SEARXNG_FAILURE_COOLDOWN_SECONDS``, default 60) so a down
instance is not hammered query after query. An empty result set
from a healthy instance is a legitimate answer and is returned
as an empty list, not an error.

Attribution is recorded per call at this choke point —
run_metrics counters, the provider-usage ledger (one row per
real HTTP attempt, priced by the Phase 8 cost engine) and the
``search_call`` log event — while each result's ``source`` keeps
the supplying engine/site name the pipeline has always preserved
as provenance. Search results are evidence, never verified
facts; downstream fact verification is untouched by search.

Failures are TYPED, following the llm_provider taxonomy style
(Phase 4b): :class:`SearchProviderError` carries a ``kind`` —
``not_configured`` / ``quota`` (HTTP 402) / ``auth`` (401/403) /
``rate_limit`` (429, after the bounded retries) / ``upstream``
(5xx after retries, other HTTP, transport errors) — so consumers
branch on the kind instead of parsing message text.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

try:
    from services import logging_setup
except ImportError:  # pragma: no cover - flat-layout fallback
    import logging_setup

# B-13 (Phase 5c-1): real search requests emit one structured
# event at the choke point below.
logger = logging_setup.get_logger("search_provider")

try:
    from services import source_cache
except ImportError:  # pragma: no cover - flat-layout fallback
    import source_cache

try:
    from services import run_metrics
except ImportError:  # pragma: no cover - flat-layout fallback
    import run_metrics

load_dotenv()


class SearchProviderError(RuntimeError):
    """SearXNG could not serve the call. ``kind`` is one of
    ``not_configured``, ``quota``, ``auth``, ``rate_limit``,
    ``upstream``; ``status_code`` is the HTTP status when one was
    received (else None). A RuntimeError subclass, so pre-Phase-4c
    callers that caught the old RuntimeError keep working."""

    KINDS = ("not_configured", "quota", "auth", "rate_limit", "upstream")

    def __init__(self, kind, message, status_code=None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code


def _record_search_usage(provider: str, kind: str) -> None:
    """Append one provider-usage ledger row for one REAL search
    HTTP attempt (Master Implementation 2026-11-26), so the
    Phase 8 cost engine can price search like it prices LLM
    usage. Fail-soft by construction (record_usage never raises);
    the extra guard here keeps even an import failure out of the
    search path. Rows carry the bound run id when a production
    run is active, so per-run search cost is attributable."""
    try:
        from services import provider_usage

        provider_usage.record_usage(
            provider=provider,
            model=None,
            usage=None,
            context={
                "run_id": run_metrics.bound_run_id(),
                "context": f"search:{kind}",
            },
        )
    except Exception:
        pass


# ---------------------------------------------------------------------
# SearXNG (Master Implementation 2026-11-26) — self-hosted, the
# platform's sole search provider. Configuration is env-driven
# and resolved at call time, so deployments and tests can
# reconfigure without an import restart:
#   SEARXNG_URL                          instance base URL (no key)
#   SEARXNG_TIMEOUT_SECONDS              per-attempt timeout (20)
#   SEARXNG_MAX_ATTEMPTS                 bounded attempts (2)
#   SEARXNG_MAX_CONCURRENCY              per-process semaphore (4)
#   SEARXNG_FAILURE_COOLDOWN_SECONDS     circuit-breaker open time (60)
#   SEARXNG_ENGINES_WEB / _IMAGES / _VIDEOS   optional engine lists
# ---------------------------------------------------------------------

_SEARXNG_KIND_CATEGORIES = {
    "web": "general",
    "images": "images",
    "videos": "videos",
}

# Kind -> the endpoint-path vocabulary run_metrics counts in
# (the web endpoint is counted as "search").
_KIND_TO_PATH = {
    "web": "search",
    "images": "images",
    "videos": "videos",
}

_searxng_sem = threading.BoundedSemaphore(
    int(os.getenv("SEARXNG_MAX_CONCURRENCY", "4"))
)

_breaker_lock = threading.Lock()
_breaker_open_until = 0.0

_health_lock = threading.Lock()
_health_cache = {"checked_at": 0.0, "result": None}


def _searxng_url() -> str:
    return (os.getenv("SEARXNG_URL") or "").strip().rstrip("/")


def searxng_configured() -> bool:
    """Whether a SearXNG instance URL is configured. The one
    configuration check consumers (assistant tools, reports) use
    instead of knowing provider details."""
    return bool(_searxng_url())


def _searxng_timeout() -> float:
    try:
        return max(1.0, float(os.getenv("SEARXNG_TIMEOUT_SECONDS", "20")))
    except ValueError:
        return 20.0


def _searxng_max_attempts() -> int:
    try:
        return max(1, int(os.getenv("SEARXNG_MAX_ATTEMPTS", "2")))
    except ValueError:
        return 2


def _breaker_cooldown() -> float:
    try:
        return max(0.0, float(os.getenv("SEARXNG_FAILURE_COOLDOWN_SECONDS", "60")))
    except ValueError:
        return 60.0


def _breaker_is_open() -> bool:
    with _breaker_lock:
        return time.monotonic() < _breaker_open_until


def _breaker_trip() -> None:
    global _breaker_open_until
    cooldown = _breaker_cooldown()
    if cooldown <= 0:
        return
    with _breaker_lock:
        _breaker_open_until = max(
            _breaker_open_until, time.monotonic() + cooldown
        )


def _breaker_reset() -> None:
    global _breaker_open_until
    with _breaker_lock:
        _breaker_open_until = 0.0


def _searxng_engines(kind: str) -> str:
    return (os.getenv(f"SEARXNG_ENGINES_{kind.upper()}") or "").strip()


def _dedupe_by_url(results: List[Dict], key: str = "url") -> List[Dict]:
    """Drop duplicate URLs (fragment/trailing-slash variants),
    preserving first-seen order. Applied to SearXNG batches,
    whose multi-engine aggregation routinely repeats results."""
    seen = set()
    out = []
    for item in results:
        raw = str(item.get(key) or "").strip()
        norm = raw.split("#", 1)[0].rstrip("/")
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(item)
    return out


def _result_meta(kind: str, url: str) -> dict:
    """The retained source metadata for search results: source
    domain, search category and the retrieval timestamp
    (title/URL/snippet ride in the core fields)."""
    return {
        "domain": urlparse(url).netloc.lower() if url else "",
        "category": kind,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def _parse_searxng_web(data: dict) -> List[Dict]:
    results = []
    for item in data.get("results", []):
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        results.append({
            "title": item.get("title", ""),
            "url": url,
            "snippet": item.get("content", "") or "",
            "source": item.get("engine", "") or "searxng",
            **_result_meta("web", url),
        })
    return _dedupe_by_url(results)


def _parse_searxng_images(data: dict) -> List[Dict]:
    results = []
    for item in data.get("results", []):
        image_url = str(item.get("img_src") or "").strip()
        if not image_url:
            continue
        source_url = str(item.get("url") or "").strip()
        results.append({
            "title": item.get("title", ""),
            "image_url": image_url,
            "source_url": source_url,
            "url": image_url,
            "snippet": "",
            "source": (
                item.get("source", "")
                or item.get("engine", "")
                or "searxng"
            ),
            **_result_meta("images", image_url),
        })
    return _dedupe_by_url(results, key="image_url")


def _parse_searxng_videos(data: dict) -> List[Dict]:
    results = []
    for item in data.get("results", []):
        video_url = str(item.get("url") or "").strip()
        if not video_url:
            continue
        published = item.get("publishedDate")
        results.append({
            "title": item.get("title", ""),
            "video_url": video_url,
            "url": video_url,
            "snippet": item.get("content", "") or "",
            "source": item.get("engine", "") or "searxng",
            "date": str(published) if published else "",
            "duration": str(item.get("length") or ""),
            **_result_meta("videos", video_url),
        })
    return _dedupe_by_url(results, key="video_url")


_SEARXNG_PARSERS = {
    "web": _parse_searxng_web,
    "images": _parse_searxng_images,
    "videos": _parse_searxng_videos,
}


def _searxng_search(kind: str, query: str) -> List[Dict]:
    """One logical SearXNG search: bounded concurrency, the
    platform rate bucket, bounded retries, typed failures.
    Raises SearchProviderError when the instance cannot serve a
    usable response — there is no fallback provider."""
    url = _searxng_url()
    if not url:
        raise SearchProviderError(
            "not_configured", "SEARXNG_URL is not configured."
        )
    if _breaker_is_open():
        raise SearchProviderError(
            "upstream",
            "SearXNG circuit breaker is open after recent "
            "failures; cooling down before retrying the instance.",
        )
    params = {
        "q": query,
        "format": "json",
        "categories": _SEARXNG_KIND_CATEGORIES[kind],
        "safesearch": "0",
        "language": "en",
    }
    engines = _searxng_engines(kind)
    if engines:
        params["engines"] = engines

    attempts = _searxng_max_attempts()
    last_exc = None
    for attempt in range(attempts):
        with _searxng_sem:
            try:
                from services import rate_bucket

                rate_bucket.acquire("searxng", timeout=10.0)
            except Exception:
                pass
            source_cache.bump("searxng_calls")
            run_metrics.search_call("searxng", _KIND_TO_PATH[kind])
            _record_search_usage("searxng", kind)
            logging_setup.log_event(
                logger,
                logging.INFO,
                "search_call",
                provider="searxng",
                endpoint=kind,
                attempt=attempt + 1,
            )
            try:
                response = requests.get(
                    f"{url}/search",
                    params=params,
                    headers={"Accept": "application/json"},
                    timeout=_searxng_timeout(),
                )
            except requests.RequestException as exc:
                last_exc = SearchProviderError(
                    "upstream",
                    f"SearXNG request failed (transport): {exc}",
                )
                last_exc.__cause__ = exc
                response = None

        if response is not None:
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    data = None
                if isinstance(data, dict) and isinstance(
                    data.get("results"), list
                ):
                    _breaker_reset()
                    return _SEARXNG_PARSERS[kind](data)
                last_exc = SearchProviderError(
                    "upstream",
                    "SearXNG returned a malformed response body.",
                )
            elif response.status_code == 429:
                last_exc = SearchProviderError(
                    "rate_limit",
                    "SearXNG request failed (HTTP 429): rate limited",
                    status_code=429,
                )
            else:
                last_exc = SearchProviderError(
                    "upstream",
                    f"SearXNG request failed "
                    f"(HTTP {response.status_code}): "
                    f"{(getattr(response, 'text', '') or '')[:500]}",
                    status_code=response.status_code,
                )

        if attempt < attempts - 1:
            time.sleep(min(0.5 * (2 ** attempt), 5))

    _breaker_trip()
    raise last_exc if last_exc else SearchProviderError(
        "upstream", "SearXNG request failed"
    )


def searxng_health() -> dict:
    """Liveness of the configured SearXNG instance (GET
    /healthz), cached for 30 seconds so health consumers never
    hammer the instance. Never raises."""
    url = _searxng_url()
    if not url:
        return {"configured": False, "ok": False, "error": "not_configured"}
    now = time.monotonic()
    with _health_lock:
        cached = _health_cache["result"]
        if cached is not None and now - _health_cache["checked_at"] < 30:
            return dict(cached)
    result = {"configured": True, "ok": False, "url": url}
    started = time.monotonic()
    try:
        response = requests.get(f"{url}/healthz", timeout=5)
        result["latency_ms"] = round((time.monotonic() - started) * 1000)
        result["status_code"] = response.status_code
        result["ok"] = response.status_code == 200
        if not result["ok"]:
            result["error"] = f"HTTP {response.status_code}"
    except requests.RequestException as exc:
        result["latency_ms"] = round((time.monotonic() - started) * 1000)
        result["error"] = f"transport: {exc}"
    with _health_lock:
        _health_cache["checked_at"] = time.monotonic()
        _health_cache["result"] = result
    return dict(result)


def search_health() -> dict:
    """Search health for admin/ops surfaces: the sole provider's
    readiness and circuit-breaker state. Never raises."""
    return {
        "primary": "searxng",
        "searxng": {
            **searxng_health(),
            "breaker_open": _breaker_is_open(),
        },
    }


def _cached_search(kind: str, query: str) -> List[Dict]:
    cached = source_cache.search_get(kind, query)
    if cached is not None:
        return cached

    try:
        results = _searxng_search(kind, query)
    except SearchProviderError as exc:
        if exc.kind != "not_configured":
            run_metrics.search_provider_failure("searxng")
        raise
    source_cache.search_put(kind, query, results)
    return results


def search_web(query: str) -> List[Dict]:
    return _cached_search("web", query)


def search_images(query: str, limit: int = 20) -> List[Dict]:
    # The full parsed list is cached; the limit is applied on the way out,
    # so a later call with a different limit still hits the cache.
    return _cached_search("images", query)[:limit]


def search_videos(query: str, limit: int = 15) -> List[Dict]:
    return _cached_search("videos", query)[:limit]
