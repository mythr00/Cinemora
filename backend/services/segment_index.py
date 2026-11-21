"""Segment identity and the transcript-anchored segment index
(Stage 7 / R8 — Master Architecture Review section C.D).

C.D.2/C.D.3: a segment of a video source is identified by

    segment_id = sha256(canonical_source_key | start_ms | end_ms)

and a still (or a whole-asset video use) by

    segment_id = sha256(canonical_source_key | whole)

The identity is a function of the CANONICAL source (services
.canonical_media.canonical_source_key — the same identity the rest
of the pipeline already uses), never of a raw URL spelling, a file
path or a sentence number: two beats that use the same window of the
same source share one segment_id however the URL was spelled, and
two different windows of one source never share one. The id is
minted at segment-selection time and stamped on the candidate, the
asset, the timeline item and the ledger row, so provenance follows
the segment rather than the file that happened to carry it.

The index itself (built once per source, stored in the
source-intelligence cache via services.source_cache) normalises a
source's chapters, transcript windows and description moments into
candidate segments keyed by canonical source key. An indexed segment
carries its basis (chapter title / transcript text hash / description
line) and NO verdict: the index is evidence organisation, not
pre-verification. Segment EVIDENCE is reused across beats and runs;
segment VERDICTS remain contract-relative and are re-evaluated per
beat/category without re-calling yt-dlp or re-downloading a
transcript.
"""

import hashlib

try:
    from services import source_cache
    from services.canonical_media import canonical_source_key
except ImportError:  # flat execution (scripts run inside services/)
    import source_cache
    from canonical_media import canonical_source_key

#: Cap on indexed candidate segments per source (C.D.3 cost control:
#: index builds stay bounded however chapter/transcript-rich a
#: source is; chapters are kept first, then transcript passages,
#: then description moments).
SEGMENT_INDEX_MAX_PER_SOURCE = 240


def segment_id_for(source_key, start_seconds, end_seconds):
    """The windowed segment identity (C.D.3)."""
    start_ms = int(round(float(start_seconds) * 1000))
    end_ms = int(round(float(end_seconds) * 1000))
    digest = hashlib.sha256(
        f"{source_key}|{start_ms}|{end_ms}".encode("utf-8")
    ).hexdigest()
    return f"seg_{digest}"


def whole_segment_id_for(source_key):
    """The whole-asset segment identity (stills; whole-source uses)."""
    digest = hashlib.sha256(
        f"{source_key}|whole".encode("utf-8")
    ).hexdigest()
    return f"seg_{digest}"


def source_key_for(mapping):
    """Public wrapper: the canonical source key of a
    candidate/asset/item dict (see _source_key_for)."""
    return _source_key_for(mapping)


def _source_key_for(mapping):
    """The canonical source key for a candidate/asset/item dict, from
    whichever URL field identifies the MEDIA source; None when it
    carries no URL. Field priority differs by kind: a video's
    identity is its video_url/url (a candidate's source_url can be
    the search page that LED to the video, not the video), while a
    still's identity is its image_url, then its source page."""
    if not isinstance(mapping, dict):
        return None
    if _is_video(mapping):
        fields = ("video_url", "url", "source_url", "image_url")
    else:
        fields = ("image_url", "source_url", "video_url", "url")
    for field in fields:
        value = mapping.get(field)
        if isinstance(value, str) and value.strip():
            return canonical_source_key(value)
    return None


def _is_video(mapping):
    kind = str(
        mapping.get("type") or mapping.get("visual_type") or ""
    ).strip().lower()
    if kind in ("video", "moving"):
        return True
    if kind in ("image", "still", "photo"):
        return False
    # Untyped dicts: a recorded clip window means video.
    return mapping.get("clip_start") is not None


def segment_id_for_asset(asset):
    """The segment identity of a downloaded asset dict, or None when
    the asset carries no source identity (generated graphics, local
    files without provenance). Videos use their probed window
    (clip_start + probed duration); stills use the whole-asset id."""
    if not isinstance(asset, dict):
        return None
    if asset.get("segment_id"):
        return str(asset["segment_id"])
    source_key = _source_key_for(asset)
    if not source_key:
        return None
    if _is_video(asset):
        start = asset.get("clip_start")
        duration = asset.get("clip_duration")
        if duration is None:
            duration = asset.get("duration_seconds")
        if start is not None and duration is not None:
            try:
                start_f = float(start)
                return segment_id_for(
                    source_key, start_f, start_f + float(duration)
                )
            except (TypeError, ValueError):
                return None
        return None
    return whole_segment_id_for(source_key)


def segment_id_for_item(item):
    """The segment identity of a timeline item. A stamped segment_id
    wins; otherwise the identity is RECONSTRUCTED from the item's
    recorded source window (source_url + clip_start + duration) —
    the reconstruction C.F.3 specifies for stored timelines, which
    predate the stamp. None when the item records no source window
    (graphics, holdovers without provenance)."""
    if not isinstance(item, dict):
        return None
    if item.get("segment_id"):
        return str(item["segment_id"])
    source_key = _source_key_for(item)
    if not source_key:
        return None
    if _is_video(item):
        start = item.get("clip_start")
        duration = item.get("duration_seconds")
        if duration is None:
            duration = item.get("duration")
        if start is not None and duration is not None:
            try:
                start_f = float(start)
                return segment_id_for(
                    source_key, start_f, start_f + float(duration)
                )
            except (TypeError, ValueError):
                return None
        return None
    return whole_segment_id_for(source_key)


# ---------------------------------------------------------------------------
# The index (storage lives in services.source_cache)
# ---------------------------------------------------------------------------

def indexed_segments(source_key):
    """The indexed candidate segments for a canonical source key."""
    if not source_key:
        return []
    return source_cache.segment_index_for(source_key)


def store_segment_index(source_key, segments):
    """Persist a built index for one source, capped per C.D.3."""
    capped = list(segments or [])[:SEGMENT_INDEX_MAX_PER_SOURCE]
    source_cache.segment_index_replace(source_key, capped)
    source_cache.bump("segment_index_builds")
    return capped


def ensure_segment_index(source_key, builder):
    """Return (segments, built). Builds the index via ``builder()``
    only when the source has none yet — the index is built ONCE per
    source and reused across beats and runs (C.D.3)."""
    existing = indexed_segments(source_key)
    if existing:
        return existing, False
    built = store_segment_index(source_key, builder())
    return built, True
