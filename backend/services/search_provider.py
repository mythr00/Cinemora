import os
from typing import Dict, List

import requests
from dotenv import load_dotenv

load_dotenv()

SERPER_API_KEY = os.getenv("SERPER_API_KEY")

SERPER_HEADERS = {
    "X-API-KEY": SERPER_API_KEY or "",
    "Content-Type": "application/json",
}


def check_api_key():
    if not SERPER_API_KEY:
        raise RuntimeError(
            "SERPER_API_KEY is missing from the .env file."
        )


def search_web(query: str) -> List[Dict]:
    check_api_key()

    response = requests.post(
        "https://google.serper.dev/search",
        headers=SERPER_HEADERS,
        json={"q": query},
        timeout=30,
    )

    response.raise_for_status()
    data = response.json()

    results = []

    for item in data.get("organic", []):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", ""),
            "source": "google_serper",
        })

    return results


def search_images(query: str, limit: int = 20) -> List[Dict]:
    check_api_key()

    response = requests.post(
        "https://google.serper.dev/images",
        headers=SERPER_HEADERS,
        json={"q": query},
        timeout=30,
    )

    response.raise_for_status()
    data = response.json()

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

        if len(results) >= limit:
            break

    return results


def search_videos(query: str, limit: int = 15) -> List[Dict]:
    check_api_key()

    response = requests.post(
        "https://google.serper.dev/videos",
        headers=SERPER_HEADERS,
        json={"q": query},
        timeout=30,
    )

    response.raise_for_status()
    data = response.json()

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

        if len(results) >= limit:
            break

    return results