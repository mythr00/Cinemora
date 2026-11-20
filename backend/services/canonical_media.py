"""Canonical media identity (Quality Recovery Phase 2, workstream C;
canonical DM-16 / DM-11).

One source, one identity — regardless of how its URL is spelled.
Before this module, candidate identity was the raw URL string
(``sha256(url)[:12]``), so the same video as ``youtu.be/X`` and as
``youtube.com/watch?v=X&utm_source=...`` was retrieved, verified,
downloaded and counted TWICE, and the failed-download guard was
defeated by respelling the URL.

Canonicalisation rules:

* YouTube — ``youtu.be/<id>``, ``youtube.com/watch?v=<id>``,
  ``/embed/<id>``, ``/shorts/<id>``, ``/live/<id>`` and ``/v/<id>``
  are the same source: ``youtube:<id>``. Every other query parameter
  is dropped for YouTube watch URLs.
* archive.org — ``/details/<item>`` and ``/download/<item>/<file>``
  are the same ITEM: ``archive.org:<item>`` at source level. Segment
  identity stays distinct from source identity (the registry's
  segment-overlap blocking works on top of the source key), and two
  different items never merge.
* Wikimedia — an upload URL's thumbnail spelling
  (``.../thumb/<path>/<file>/<width>px-<file>``) and the original
  file URL are the same source.
* Everything else — scheme/host normalised (lowercase), tracking
  parameters removed (``utm_*``, click ids, share tokens), remaining
  query parameters sorted, fragment and trailing slash dropped.

Genuinely different assets never merge: different video ids,
different archive items and different paths stay different keys.
This module is pure stdlib and has no pipeline imports, so any layer
(registry, caches, pipeline) can use it without import cycles.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Query parameters that never change which asset a URL identifies.
TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
    "si", "feature", "ref", "ref_src", "ref_url", "spm", "igshid",
    "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "utm_id", "utm_reader", "utm_name",
    "utm_social", "utm_social-type", "_ga", "yclid", "twclid",
})

_YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")
_YOUTUBE_PATH_ID = re.compile(r"^/(?:embed|shorts|live|v)/([^/?#]+)")


def _host(parsed) -> str:
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def youtube_video_id(url) -> str:
    """The YouTube video id a URL identifies, or "" when it is not a
    YouTube video URL."""
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return ""
    host = _host(parsed)
    if host not in _YOUTUBE_HOSTS:
        return ""
    if host == "youtu.be":
        candidate = (parsed.path or "").strip("/").split("/")[0]
        return candidate if _YOUTUBE_ID_RE.match(candidate) else ""
    if parsed.path == "/watch":
        for key, value in parse_qsl(parsed.query):
            if key == "v" and _YOUTUBE_ID_RE.match(value or ""):
                return value
        return ""
    match = _YOUTUBE_PATH_ID.match(parsed.path or "")
    if match and _YOUTUBE_ID_RE.match(match.group(1)):
        return match.group(1)
    return ""


def archive_org_item(url) -> str:
    """The archive.org item identifier a URL identifies (details,
    download, embed or metadata spellings), or ""."""
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return ""
    if _host(parsed) != "archive.org":
        return ""
    parts = [p for p in (parsed.path or "").split("/") if p]
    if len(parts) >= 2 and parts[0] in {"details", "download", "embed", "metadata"}:
        return parts[1]
    return ""


def _wikimedia_file_key(parsed) -> str:
    """Canonical key for a Wikimedia upload URL, unifying an original
    file with its generated thumbnails."""
    path = parsed.path or ""
    # /wikipedia/commons/thumb/a/ab/File.jpg/800px-File.jpg
    #   -> /wikipedia/commons/a/ab/File.jpg
    if "/thumb/" in path:
        head, _, tail = path.partition("/thumb/")
        segments = [s for s in tail.split("/") if s]
        if len(segments) >= 2:
            # Last segment is the sized thumbnail name; the rest is
            # the original file's path below thumb/.
            path = head + "/" + "/".join(segments[:-1])
    return f"wikimedia:{path}"


def canonical_url(url) -> str:
    """A normalised URL string: same asset -> same string, while the
    result is still a URL (used for cache keys that must stay URLs).

    YouTube watch URLs collapse to ``https://youtube.com/watch?v=<id>``
    whatever their original spelling; archive.org item/download URLs
    keep their path (item identity is handled at source-key level).
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    video_id = youtube_video_id(raw)
    if video_id:
        # The www watch spelling is the canonical form: it is the
        # spelling YouTube itself emits and the one existing stored
        # cache profiles already use.
        return f"https://www.youtube.com/watch?v={video_id}"
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw.lower()
    host = (parsed.netloc or "").lower()
    if not host:
        return raw.lower()
    scheme = (parsed.scheme or "https").lower()
    path = (parsed.path or "").rstrip("/") or "/"
    params = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    params.sort()
    return urlunparse((scheme, host, path, "", urlencode(params), ""))


def canonical_source_key(url) -> str:
    """The canonical SOURCE identity of a URL.

    YouTube videos and archive.org items collapse to their id-based
    keys; Wikimedia uploads unify thumbnails with originals; every
    other URL is its canonical_url(). Two URLs with the same key are
    the same source; different assets always produce different keys.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    video_id = youtube_video_id(raw)
    if video_id:
        return f"youtube:{video_id}"
    item = archive_org_item(raw)
    if item:
        return f"archive.org:{item}"
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw.lower()
    host = _host(parsed)
    if host == "upload.wikimedia.org" or host.endswith(".wikimedia.org"):
        return _wikimedia_file_key(parsed)
    return canonical_url(raw)


def candidate_url(candidate) -> str:
    """The best identifying URL on a candidate/asset dict, in the
    pipeline's established field precedence."""
    if not isinstance(candidate, dict):
        return ""
    return str(
        candidate.get("url")
        or candidate.get("video_url")
        or candidate.get("image_url")
        or candidate.get("download_url")
        or candidate.get("link")
        or ""
    )


def canonical_candidate_key(candidate) -> str:
    """Canonical source key for a candidate/asset dict."""
    return canonical_source_key(candidate_url(candidate))
