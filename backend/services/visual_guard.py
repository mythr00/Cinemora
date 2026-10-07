from __future__ import annotations

import os
import re
from pathlib import Path

import requests


FACE_WORDS = re.compile(
    r"\b(portrait|headshot|face|selfie|woman|man|girl|boy|model|actor|person|people)\b",
    re.I,
)

BEAT_QUERIES = {
    "police": [
        "police car night emergency lights",
        "police station exterior",
        "crime scene tape",
        "detective desk case files",
    ],
    "court": [
        "empty courtroom benches",
        "judge gavel close up",
        "jury box empty courtroom",
        "legal documents table",
    ],
    "press": [
        "news camera tripod",
        "journalist notebook pen",
        "press microphones podium",
        "newspaper printing press",
    ],
    "water": [
        "misty lake dawn",
        "fishing boat on lake",
        "river shoreline water",
        "empty wooden boat dock",
    ],
    "insurance": [
        "insurance documents desk",
        "office paperwork calculator",
        "stack of folders archive",
    ],
    "aftermath": [
        "empty suburban street dusk",
        "rain on window glass",
        "cemetery trees distance",
    ],
    "investigation": [
        "case files folders table",
        "evidence boxes archive",
        "flashlight dark woods",
        "old telephone answering machine",
    ],
}

BEAT_HINTS = [
    (re.compile(r"\b(police|detective|sheriff|arrest|interrogat)", re.I), "police"),
    (re.compile(r"\b(court|trial|jury|judge|sentence|convict|appeal)", re.I), "court"),
    (re.compile(r"\b(news|reporter|journalist|headline|press)", re.I), "press"),
    (re.compile(r"\b(lake|boat|fish|water|dock|river|ocean|sea|beach)", re.I), "water"),
    (re.compile(r"\b(insur|policy|payout|money)", re.I), "insurance"),
    (re.compile(r"\b(remain|grave|funeral|aftermath)", re.I), "aftermath"),
]


def detect_beat(text: str) -> str:
    for pat, beat in BEAT_HINTS:
        if pat.search(text or ""):
            return beat
    return "investigation"


def mentions_named_person(text: str) -> bool:
    try:
        from services.script_analyzer import detect_entities
        return bool(detect_entities(text or ""))
    except Exception:
        return False


def safe_queries_for_text(text: str) -> list[str]:
    beat = detect_beat(text)
    queries = list(BEAT_QUERIES.get(beat, BEAT_QUERIES["investigation"]))
    try:
        from services.script_analyzer import analyze_sentence
        analysis = analyze_sentence(text or "") or {}
        queries = list(analysis.get("search_queries") or []) + queries
    except Exception:
        pass
    seen = set()
    out = []
    for query in queries:
        key = str(query or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(str(query).strip())
    return out[:8]


def looks_like_face(title: str) -> bool:
    return bool(FACE_WORDS.search(title or ""))


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "DocumentaryStudio/0.3"})
    return s


def search_licensed_images(query: str, limit: int = 8) -> list[dict]:
    hits = _pexels_photos(query, limit) + _pixabay_photos(query, limit)
    return [h for h in hits if not looks_like_face(h.get("title", ""))][:limit]


def search_licensed_videos(query: str, limit: int = 5) -> list[dict]:
    hits = _pexels_videos(query, limit) + _pixabay_videos(query, limit)
    return [h for h in hits if not looks_like_face(h.get("title", ""))][:limit]


def download_direct(url: str, dest: str) -> bool:
    try:
        Path(os.path.dirname(dest)).mkdir(parents=True, exist_ok=True)
        r = _session().get(url, timeout=60, stream=True)
        r.raise_for_status()
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(64 * 1024):
                f.write(chunk)
        os.replace(tmp, dest)
        return os.path.getsize(dest) > 2048
    except Exception as exc:
        print(f"licensed download failed: {exc}")
        return False


def _pexels_photos(q: str, limit: int) -> list[dict]:
    key = os.getenv("PEXELS_API_KEY", "")
    if not key:
        return []
    r = _session().get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": key},
        params={"query": q, "per_page": limit, "orientation": "landscape", "size": "large"},
        timeout=30,
    )
    if r.status_code != 200:
        print("Pexels photo error", r.status_code, r.text[:200])
        return []
    out = []
    for p in r.json().get("photos", []):
        src = p.get("src") or {}
        url = src.get("large2x") or src.get("large") or src.get("original")
        if not url:
            continue
        out.append({
            "image_url": url,
            "url": url,
            "title": p.get("alt") or q,
            "source": "pexels",
            "license": "Pexels License",
        })
    return out


def _pexels_videos(q: str, limit: int) -> list[dict]:
    key = os.getenv("PEXELS_API_KEY", "")
    if not key:
        return []
    r = _session().get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": key},
        params={"query": q, "per_page": limit, "orientation": "landscape", "size": "medium"},
        timeout=30,
    )
    if r.status_code != 200:
        print("Pexels video error", r.status_code, r.text[:200])
        return []
    out = []
    for v in r.json().get("videos", []):
        files = [f for f in (v.get("video_files") or []) if "mp4" in (f.get("file_type") or "")]
        files.sort(key=lambda f: abs((f.get("width") or 0) - 1920))
        if not files:
            continue
        url = files[0]["link"]
        out.append({
            "video_url": url,
            "url": url,
            "title": q,
            "source": "pexels",
            "license": "Pexels License",
        })
    return out


def _pixabay_photos(q: str, limit: int) -> list[dict]:
    key = os.getenv("PIXABAY_API_KEY", "")
    if not key:
        return []
    r = _session().get(
        "https://pixabay.com/api/",
        params={
            "key": key,
            "q": q,
            "image_type": "photo",
            "orientation": "horizontal",
            "safesearch": "true",
            "per_page": max(3, min(limit, 20)),
        },
        timeout=30,
    )
    if r.status_code != 200:
        print("Pixabay photo error", r.status_code, r.text[:200])
        return []
    out = []
    for p in r.json().get("hits", []):
        url = p.get("largeImageURL") or p.get("webformatURL")
        if not url:
            continue
        out.append({
            "image_url": url,
            "url": url,
            "title": p.get("tags") or q,
            "source": "pixabay",
            "license": "Pixabay Content License",
        })
    return out


def _pixabay_videos(q: str, limit: int) -> list[dict]:
    key = os.getenv("PIXABAY_API_KEY", "")
    if not key:
        return []
    r = _session().get(
        "https://pixabay.com/api/videos/",
        params={
            "key": key,
            "q": q,
            "safesearch": "true",
            "per_page": max(3, min(limit, 20)),
        },
        timeout=30,
    )
    if r.status_code != 200:
        print("Pixabay video error", r.status_code, r.text[:200])
        return []
    out = []
    for v in r.json().get("hits", []):
        vids = v.get("videos") or {}
        pick = vids.get("medium") or vids.get("large") or vids.get("small") or {}
        url = pick.get("url")
        if not url:
            continue
        out.append({
            "video_url": url,
            "url": url,
            "title": v.get("tags") or q,
            "source": "pixabay",
            "license": "Pixabay Content License",
        })
    return out