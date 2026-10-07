from __future__ import annotations

import os
import re
import hashlib
from pathlib import Path
from urllib.parse import quote_plus

import requests

from .visual_policy import VisualRequest, reject_candidate

UA = "DocumentaryStudio/0.3 (local research tool)"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


def search_licensed(req: VisualRequest, limit: int = 6) -> list[dict]:
    hits: list[dict] = []
    if req.kind == "graphic":
        return hits
    if req.kind == "video":
        hits += _pexels_videos(req.query, limit)
        hits += _pixabay_videos(req.query, limit)
    else:
        hits += _pexels_photos(req.query, limit)
        hits += _pixabay_photos(req.query, limit)
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
    r = _session().get(url, timeout=60, stream=True)
    r.raise_for_status()
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(64 * 1024):
            f.write(chunk)
    tmp.replace(path)
    return path


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
    r.raise_for_status()
    out = []
    for p in r.json().get("photos", []):
        src = p.get("src") or {}
        out.append(
            {
                "kind": "image",
                "source": "pexels",
                "source_url": p.get("url"),
                "creator": (p.get("photographer") or ""),
                "license": "Pexels License",
                "license_url": "https://www.pexels.com/license/",
                "title": p.get("alt") or q,
                "alt": p.get("alt") or "",
                "tags": "",
                "download_url": src.get("large2x") or src.get("large") or src.get("original"),
            }
        )
    return [x for x in out if x.get("download_url")]


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
    r.raise_for_status()
    out = []
    for v in r.json().get("videos", []):
        files = v.get("video_files") or []
        files = [f for f in files if (f.get("file_type") or "").startswith("video/mp4")]
        files.sort(key=lambda f: abs((f.get("width") or 0) - 1920))
        if not files:
            continue
        out.append(
            {
                "kind": "video",
                "source": "pexels",
                "source_url": v.get("url"),
                "creator": (v.get("user") or {}).get("name", ""),
                "license": "Pexels License",
                "license_url": "https://www.pexels.com/license/",
                "title": q,
                "alt": "",
                "tags": " ".join(v.get("tags") or []) if isinstance(v.get("tags"), list) else "",
                "download_url": files[0]["link"],
            }
        )
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
    r.raise_for_status()
    out = []
    for p in r.json().get("hits", []):
        out.append(
            {
                "kind": "image",
                "source": "pixabay",
                "source_url": p.get("pageURL"),
                "creator": p.get("user") or "",
                "license": "Pixabay Content License",
                "license_url": "https://pixabay.com/service/license-summary/",
                "title": p.get("tags") or q,
                "alt": p.get("tags") or "",
                "tags": p.get("tags") or "",
                "download_url": p.get("largeImageURL") or p.get("webformatURL"),
            }
        )
    return [x for x in out if x.get("download_url")]


def _pixabay_videos(q: str, limit: int) -> list[dict]:
    key = os.getenv("PIXABAY_API_KEY", "")
    if not key:
        return []
    r = _session().get(
        "https://pixabay.com/api/videos/",
        params={
            "key": key,
            "q": q,
            "video_type": "film",
            "safesearch": "true",
            "per_page": max(3, min(limit, 20)),
        },
        timeout=30,
    )
    r.raise_for_status()
    out = []
    for v in r.json().get("hits", []):
        vids = v.get("videos") or {}
        pick = vids.get("medium") or vids.get("large") or vids.get("small") or {}
        url = pick.get("url")
        if not url:
            continue
        out.append(
            {
                "kind": "video",
                "source": "pixabay",
                "source_url": v.get("pageURL"),
                "creator": v.get("user") or "",
                "license": "Pixabay Content License",
                "license_url": "https://pixabay.com/service/license-summary/",
                "title": v.get("tags") or q,
                "alt": v.get("tags") or "",
                "tags": v.get("tags") or "",
                "download_url": url,
            }
        )
    return out


def _wikimedia(q: str, limit: int) -> list[dict]:
    """PD / CC stills only. Useful later for verified archival images."""
    r = _session().get(
        "https://commons.wikimedia.org/w/api.php",
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