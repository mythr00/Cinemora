import os
import requests


SERPER_API_KEY = os.getenv("SERPER_API_KEY")


def search_images(query: str, num_results: int = 10):
    """
    Search Google Images through Serper.
    Returns image results that can later be downloaded.
    """

    if not SERPER_API_KEY:
        raise RuntimeError("SERPER_API_KEY is not configured.")

    url = "https://google.serper.dev/images"

    headers = {
        "X-API-KEY": SERPER_API_KEY,
        "Content-Type": "application/json",
    }

    payload = {
        "q": query,
        "num": num_results,
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    results = []

    for item in data.get("images", []):
        results.append({
            "title": item.get("title"),
            "image_url": item.get("imageUrl"),
            "source_url": item.get("link"),
            "source": item.get("source"),
            "thumbnail": item.get("thumbnailUrl"),
        })

    return results