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


def check_api_key():
    if not SERPER_API_KEY:
        raise RuntimeError(
            "SERPER_API_KEY is missing from the .env file."
        )


def _post_with_retry(path: str, query: str) -> dict:
    """
    One real Serper request, with bounded concurrency and exponential
    backoff on HTTP 429 / 5xx. Every real request is counted.
    """
    last_exc = None

    for attempt in range(SERPER_MAX_ATTEMPTS):
        with _serper_sem:
            source_cache.bump("serper_calls")
            try:
                response = requests.post(
                    f"{SERPER_BASE}/{path}",
                    headers=SERPER_HEADERS,
                    json={"q": query},
                    timeout=30,
                )
            except requests.RequestException as exc:
                last_exc = exc
                response = None

        if response is not None:
            if response.status_code == 429:
                source_cache.bump("serper_429")
            if response.status_code != 429 and response.status_code < 500:
                response.raise_for_status()
                return response.json()
            last_exc = requests.HTTPError(
                f"Serper HTTP {response.status_code}", response=response
            )
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else None
        else:
            delay = None

        if attempt < SERPER_MAX_ATTEMPTS - 1:
            if delay is None:
                delay = (2 ** attempt) + random.uniform(0, 0.5)
            time.sleep(min(delay, 20))

    raise last_exc if last_exc else RuntimeError("Serper request failed")


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