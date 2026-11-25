"""Search provider (Master Project Directive Phase 4c / D-08).

This module is the ONE client for Serper — Cinemora's sole search
provider (web / images / videos). Every live caller reaches Serper
through here: the request construction, the env-configured key
(``SERPER_API_KEY``, resolved at call time), the bounded
concurrency / retry policy, the result-cache and the call metrics
all live in this single place. (The former duplicate client
``services/asset_finder.py`` had zero importers anywhere in the
repo and was removed in Phase 4c — see the D-08 census in
PHASE4-CAPABILITY-DECISIONS.md's companion report / commit.)

Failures are TYPED, following the llm_provider taxonomy style
(Phase 4b): :class:`SearchProviderError` carries a ``kind`` —
``not_configured`` / ``quota`` (HTTP 402) / ``auth`` (401/403) /
``rate_limit`` (429, after the bounded retries) / ``upstream``
(5xx after retries, other HTTP, transport errors) — so consumers
branch on the kind instead of parsing message text. Pipeline
behaviour (queries, per-production budgets, result shapes) is
unchanged by the seam.
"""

import os
import random
import threading
import time
from typing import Callable, Dict, List

import requests
from dotenv import load_dotenv

try:
    from services import source_cache
except ImportError:  # pragma: no cover - flat-layout fallback
    import source_cache

try:
    from services import run_metrics
except ImportError:  # pragma: no cover - flat-layout fallback
    import run_metrics

load_dotenv()

SERPER_API_KEY = os.getenv("SERPER_API_KEY")

SERPER_HEADERS = {
    "X-API-KEY": SERPER_API_KEY or "",
    "Content-Type": "application/json",
}

SERPER_BASE = "https://google.serper.dev"
SERPER_MAX_CONCURRENCY = int(os.getenv("SERPER_MAX_CONCURRENCY", "4"))
SERPER_MAX_ATTEMPTS = 4

_serper_sem = threading.BoundedSemaphore(SERPER_MAX_CONCURRENCY)


class SearchProviderError(RuntimeError):
    """Serper could not serve the call. ``kind`` is one of
    ``not_configured``, ``quota``, ``auth``, ``rate_limit``,
    ``upstream``; ``status_code`` is the HTTP status when one was
    received (else None). A RuntimeError subclass, so pre-Phase-4c
    callers that caught the old RuntimeError keep working."""

    KINDS = ("not_configured", "quota", "auth", "rate_limit", "upstream")

    def __init__(self, kind, message, status_code=None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code

    @classmethod
    def for_status(cls, status_code, body_text=""):
        if status_code == 402:
            kind = "quota"
        elif status_code in (401, 403):
            kind = "auth"
        elif status_code == 429:
            kind = "rate_limit"
        else:
            kind = "upstream"
        return cls(
            kind,
            f"Serper request failed (HTTP {status_code}): "
            f"{str(body_text)[:500]}",
            status_code=status_code,
        )


def _api_key():
    """The Serper key, resolved at CALL time (env wins; the
    import-time module constant is the fallback), so deployment
    config and test monkeypatching are honoured without a restart
    of the import graph."""
    return os.getenv("SERPER_API_KEY") or SERPER_API_KEY


def _headers():
    return {
        "X-API-KEY": _api_key() or "",
        "Content-Type": "application/json",
    }


def check_api_key():
    if not _api_key():
        raise SearchProviderError(
            "not_configured",
            "SERPER_API_KEY is missing from the .env file.",
        )


def _post_with_retry(path: str, query: str) -> dict:
    """
    One real Serper request, with bounded concurrency and exponential
    backoff on HTTP 429 / 5xx. Every real request is counted.
    """
    last_exc = None

    for attempt in range(SERPER_MAX_ATTEMPTS):
        with _serper_sem:
            # Phase 8.4: the platform token bucket sits in front of
            # the per-process semaphore — the Serper key's rate
            # limit is a platform resource shared by every worker
            # (design 03 §5). Fail-open if Redis is down.
            try:
                from services import rate_bucket

                rate_bucket.acquire("serper", timeout=30.0)
            except Exception:
                pass
            source_cache.bump("serper_calls")
            # Stage 0 metrics: this function is the single choke
            # point every real Serper request passes through (cache
            # hits never reach it), split by endpoint = search type.
            run_metrics.serper_call(path)
            try:
                response = requests.post(
                    f"{SERPER_BASE}/{path}",
                    headers=_headers(),
                    json={"q": query},
                    timeout=30,
                )
            except requests.RequestException as exc:
                # Transport failure: retryable, classified upstream
                # if the bounded attempts are exhausted.
                last_exc = SearchProviderError(
                    "upstream", f"Serper request failed (transport): {exc}"
                )
                last_exc.__cause__ = exc
                response = None

        if response is not None:
            if response.status_code == 429:
                source_cache.bump("serper_429")
            if response.status_code != 429 and response.status_code < 500:
                if response.status_code >= 400:
                    # Non-retryable HTTP failure (auth / quota /
                    # client error): typed, raised immediately —
                    # the same point the old raise_for_status() hit.
                    raise SearchProviderError.for_status(
                        response.status_code,
                        getattr(response, "text", "") or "",
                    )
                return response.json()
            last_exc = SearchProviderError.for_status(
                response.status_code,
                getattr(response, "text", "") or "",
            )
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else None
        else:
            delay = None

        if attempt < SERPER_MAX_ATTEMPTS - 1:
            if delay is None:
                delay = (2 ** attempt) + random.uniform(0, 0.5)
            time.sleep(min(delay, 20))

    raise last_exc if last_exc else SearchProviderError(
        "upstream", "Serper request failed"
    )


def _cached_search(
    kind: str,
    path: str,
    query: str,
    parse: Callable[[dict], List[Dict]],
) -> List[Dict]:
    cached = source_cache.search_get(kind, query)
    if cached is not None:
        return cached

    check_api_key()
    data = _post_with_retry(path, query)
    results = parse(data)
    source_cache.search_put(kind, query, results)
    return results


def _parse_web(data: dict) -> List[Dict]:
    results = []
    for item in data.get("organic", []):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", ""),
            "source": "google_serper",
        })
    return results


def _parse_images(data: dict) -> List[Dict]:
    results = []
    for item in data.get("images", []):
        image_url = item.get("imageUrl", "")
        source_url = item.get("link", "")

        if not image_url:
            continue

        results.append({
            "title": item.get("title", ""),
            "image_url": image_url,
            "source_url": source_url,
            "url": image_url,
            "snippet": item.get("snippet", ""),
            "source": item.get("source", "google_images"),
        })
    return results


def _parse_videos(data: dict) -> List[Dict]:
    results = []
    for item in data.get("videos", []):
        video_url = item.get("link", "")

        if not video_url:
            continue

        results.append({
            "title": item.get("title", ""),
            "video_url": video_url,
            "url": video_url,
            "snippet": item.get("snippet", ""),
            "source": item.get("source", ""),
            "date": item.get("date", ""),
            "duration": item.get("duration", ""),
        })
    return results


def search_web(query: str) -> List[Dict]:
    return _cached_search("web", "search", query, _parse_web)


def search_images(query: str, limit: int = 20) -> List[Dict]:
    # The full parsed list is cached; the limit is applied on the way out,
    # so a later call with a different limit still hits the cache.
    return _cached_search("images", "images", query, _parse_images)[:limit]


def search_videos(query: str, limit: int = 15) -> List[Dict]:
    return _cached_search("videos", "videos", query, _parse_videos)[:limit]