"""Archive.org moving-image discovery (MS-4).

An independent provider in Cinemora's multi-source retrieval: it does
not depend on any search provider, YouTube or any search index. Two public,
key-less endpoints:

- advancedsearch.php — finds items with mediatype:movies.
- metadata/<identifier> — the item's own file list and rights fields.

Licence handling is evidence-only: the item's licenseurl is mapped to
a short label ONLY when it is a recognizable Creative Commons /
public-domain URL; the URL itself is always preserved, and anything
unrecognized stays "unknown". Creator comes from the item metadata.
Nothing here assumes a result is downloadable or reusable — download,
probing, segment evidence and verification all happen downstream in
the shared pipeline, exactly as for every other provider.
"""

from __future__ import annotations

import re

import requests

from .http_retry import request_with_retry

try:
    from . import run_metrics
except ImportError:  # pragma: no cover - flat-layout fallback
    import run_metrics

_SEARCH_URL = "https://archive.org/advancedsearch.php"
_METADATA_URL = "https://archive.org/metadata/{identifier}"
_DETAILS_URL = "https://archive.org/details/{identifier}"
_DOWNLOAD_URL = "https://archive.org/download/{identifier}/{filename}"
_TIMEOUT = 25.0

_LICENCE_LABELS = (
    ("creativecommons.org/publicdomain/zero/1.0", "CC0 1.0"),
    ("creativecommons.org/publicdomain/mark/1.0", "Public Domain"),
    ("publicdomain", "Public Domain"),
    ("by-nc-nd/4.0", "CC BY-NC-ND 4.0"),
    ("by-nc-nd/3.0", "CC BY-NC-ND 3.0"),
    ("by-nc-sa/4.0", "CC BY-NC-SA 4.0"),
    ("by-nc/4.0", "CC BY-NC 4.0"),
    ("by-sa/4.0", "CC BY-SA 4.0"),
    ("by/4.0", "CC BY 4.0"),
    ("by/3.0", "CC BY 3.0"),
    ("by/2.0", "CC BY 2.0"),
)


def licence_label(license_url: str) -> str:
    url = str(license_url or "").lower()
    for needle, label in _LICENCE_LABELS:
        if needle in url:
            return label
    return "unknown"


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", str(text or "")).strip()


def _pick_mp4_file(files: list) -> dict | None:
    """Smallest practical MP4 derivative (bounded download size)."""
    mp4s = []
    for entry in files or []:
        name = str(entry.get("name") or "")
        fmt = str(entry.get("format") or "")
        if not name.lower().endswith(".mp4"):
            continue
        if "thumb" in fmt.lower():
            continue
        try:
            size = int(entry.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        mp4s.append((size, entry))
    if not mp4s:
        return None
    usable = [pair for pair in mp4s if pair[0] >= 200_000]
    pool = usable or mp4s
    pool.sort(key=lambda pair: pair[0])
    return pool[0][1]


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "Cinemora/1.0 (documentary research)"})
    return session


def search_archive_videos(query: str, limit: int = 3) -> list[dict]:
    """Search archive.org for moving-image items matching `query`."""
    query = str(query or "").strip()
    if not query:
        return []

    # Stage 0 metrics: one advancedsearch invocation per call.
    run_metrics.archive_org_search()

    session = _session()
    response = request_with_retry(
        "GET",
        _SEARCH_URL,
        session=session,
        params={
            "q": f"{query} AND mediatype:movies",
            "fl[]": ["identifier", "title", "creator", "licenseurl", "description"],
            "rows": max(limit * 2, 6),
            "output": "json",
        },
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    docs = ((response.json().get("response") or {}).get("docs")) or []

    results = []
    for doc in docs:
        if len(results) >= limit:
            break
        identifier = str(doc.get("identifier") or "").strip()
        if not identifier:
            continue
        try:
            meta_response = request_with_retry(
                "GET",
                _METADATA_URL.format(identifier=identifier),
                session=session,
                timeout=_TIMEOUT,
            )
            meta_response.raise_for_status()
            metadata = meta_response.json()
        except Exception:
            continue  # an item without metadata cannot be evidenced

        chosen = _pick_mp4_file(metadata.get("files") or [])
        if not chosen:
            continue

        meta = metadata.get("metadata") or {}
        license_url = str(
            meta.get("licenseurl") or doc.get("licenseurl") or ""
        ).strip()
        creator = meta.get("creator") or doc.get("creator") or ""
        if isinstance(creator, list):
            creator = ", ".join(str(c) for c in creator)
        description = _strip_html(
            meta.get("description") or doc.get("description") or ""
        )
        label = licence_label(license_url)

        # VA-03: archive.org metadata carries each file's length in
        # seconds — legitimate provider timing metadata, used as a
        # duration fallback when yt-dlp cannot read the item.
        try:
            duration_seconds = float(chosen.get("length") or 0) or None
        except (TypeError, ValueError):
            duration_seconds = None

        results.append(
            {
                "title": str(meta.get("title") or doc.get("title") or identifier),
                "video_url": _DOWNLOAD_URL.format(
                    identifier=identifier, filename=chosen["name"]
                ),
                "url": _DETAILS_URL.format(identifier=identifier),
                "source_url": _DETAILS_URL.format(identifier=identifier),
                "source": "archive.org",
                "provider": "archive.org",
                "creator": str(creator),
                "license": label,
                "license_url": license_url,
                "rights_status": (
                    "recorded" if label != "unknown" else "verify_before_publish"
                ),
                "snippet": description[:300],
                "duration_seconds": duration_seconds,
                "matched_query": query,
            }
        )
    return results
