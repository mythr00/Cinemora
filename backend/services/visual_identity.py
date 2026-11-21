#!/usr/bin/env python3
"""The ONE visual-identity definition (Master Directive Phase 5).

AM-003 / AM-017 + the repetition cheap wins: every place that
counts, deals or measures DISTINCT visuals — the timeline's
rotation/cap accounting, the pipeline's coverage/variety
arithmetic, visual_quality's headline count, the asset report —
resolves identity through this module, so the platform can never
again publish two different "distinct asset" numbers for one film
(the audit found film 1 reported as "5 distinct files" for 4
actual visuals: one photo had been downloaded twice).

Identity ladder (first match wins), per record:

1. a generated graphic counts by its FILE — it is not a segment
   of any source (``file:{path}``);
2. a video counts by SEGMENT — the Stage 6/R8 segment identity
   (canonical source + recorded window), stamped at download or
   reconstructed from the record's own fields
   (``segment:{segment_id}``);
3. a still counts by CANONICAL ASSET — the whole-segment identity
   of its canonical source (``asset:{segment_id}``), so the same
   source image reached via a thumbnail URL, the original URL or
   a respelled URL is one visual;
4. anything without source identity falls back to its file path
   (``file:{path}``), then to a caller-supplied fallback.

The ladder has two levels, both derived from the same canonical
source identity, and each consumer uses the level its question
needs:

- SEGMENT level (``base_identity_key`` / ``resolve_identity_groups``)
  — the dealing identity: the timeline's rotation and cap
  accounting and the pipeline's coverage arithmetic, where two
  windows of one source are two cards with separate allowances
  (Stage 7 semantics, unchanged).
- ASSET level (``asset_identity_key`` / ``resolve_asset_groups``)
  — the REPORTING identity behind AM-017's headline: a video
  counts by its canonical SOURCE (the clip is one visual however
  many of its windows are shown — the frame audit's "7 distinct
  visuals" for Apollo counts assets, and a segment-level headline
  would let continuation inflate reported variety), a still by
  its whole source as above, a graphic by its file.

On top of the ladder, CONTENT-HASH unification for STILLS: two
still records whose canonical identities DIFFER but whose
downloaded bytes are identical are the same visual (film 1's
duplicated building-steps photo). Videos do NOT hash-unify: a
video's identity is its source WINDOW (the ladder's segment
identity — a re-downloaded segment already unifies through it,
respelled URLs included), while a container file's bytes identify
the file, not a window inside it. Generated graphics never
hash-unify either: a graphic's identity is its file by
definition above.

The hash itself comes from the record's ``content_hash`` stamp
(the pipeline stamps it at download) or from a caller-supplied
``hash_of`` callback (measurement code passes one that reads the
files a stored project left on disk). Identity resolution never
reads files itself, so it stays usable in pure contexts.
"""

from __future__ import annotations

import hashlib
import os

from services import segment_index

_VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi")


def content_hash_for_file(path) -> str | None:
    """SHA-256 hex digest of a file's bytes; None when unreadable."""
    if not path:
        return None
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def record_path(record) -> str:
    if not isinstance(record, dict):
        return ""
    return str(record.get("file_path") or record.get("path") or "")


def record_visual_type(record) -> str:
    """``video`` / ``image`` for a record — the same rule the
    timeline has always used (explicit type first, file extension
    as the fallback)."""
    if not isinstance(record, dict):
        return "image"
    visual_type = str(record.get("type") or "").lower()
    if visual_type in ("video", "image"):
        return visual_type
    if record_path(record).lower().endswith(_VIDEO_EXTENSIONS):
        return "video"
    return "image"


def is_graphic(record) -> bool:
    return bool(
        isinstance(record, dict)
        and (record.get("motion_graphic") or record.get("generated"))
    )


def base_identity_key(record, fallback: str = "") -> str:
    """One record's ladder identity (no content-hash unification)."""
    if not isinstance(record, dict):
        return fallback
    path = record_path(record)
    if is_graphic(record):
        return f"file:{path}" if path else fallback
    # Assets resolve through segment_id_for_asset (probed window);
    # timeline items through segment_id_for_item (their recorded
    # shown window). A stamped segment_id wins inside both.
    segment_id = segment_index.segment_id_for_asset(
        record
    ) or segment_index.segment_id_for_item(record)
    if segment_id:
        if record_visual_type(record) == "video":
            return f"segment:{segment_id}"
        return f"asset:{segment_id}"
    if path:
        return f"file:{path}"
    return fallback


def asset_identity_key(record, fallback: str = "") -> str:
    """One record's ASSET-level ladder identity (the AM-017
    headline level): canonical source where a source URL exists —
    a video by its whole source, never by window — else its file
    path, else the caller's fallback."""
    if not isinstance(record, dict):
        return fallback
    path = record_path(record)
    if is_graphic(record):
        return f"file:{path}" if path else fallback
    source_key = segment_index.source_key_for(record)
    if source_key:
        return f"asset:{segment_index.whole_segment_id_for(source_key)}"
    if path:
        return f"file:{path}"
    return fallback


def stamped_content_hash(record) -> str | None:
    if isinstance(record, dict):
        value = record.get("content_hash")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def video_window(record):
    """The source window a video record/item shows, as
    ``(start, end)`` in seconds. ``end`` is the record's stamped
    ``clip_end`` when present, else start + its recorded duration
    (asset records: ``clip_duration`` / ``duration_seconds``;
    timeline items: their shown ``duration``), else None (window
    length unknown)."""
    try:
        start = float(record.get("clip_start") or 0.0)
    except (TypeError, ValueError):
        start = 0.0
    end = record.get("clip_end")
    if isinstance(end, (int, float)) and not isinstance(end, bool):
        return start, float(end)
    for field in ("clip_duration", "duration_seconds", "duration"):
        value = record.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return start, start + float(value)
    return start, None


def resolve_identity_groups(records, hash_of=None, fallback_of=None,
                            key_of=None) -> list:
    """Group key per record (parallel list), unifying records that
    are the same visual under the ladder + content-hash rules.

    ``hash_of(record)`` may supply a content hash for records that
    carry no stamp (measurement passes a file-reading callback);
    the record's own ``content_hash`` stamp wins when both exist.
    ``fallback_of(index, record)`` supplies the identity for
    records with no ladder identity at all; the default gives each
    such record its own group (they never silently merge).
    ``key_of`` selects the ladder level (segment by default; the
    headline passes ``asset_identity_key``).
    The group key is the ladder identity of the group's earliest
    member, so keys stay stable and human-readable.
    """
    key_fn = key_of or base_identity_key
    records = list(records or [])
    count = len(records)
    base_keys = []
    for index, record in enumerate(records):
        fallback = (
            fallback_of(index, record)
            if fallback_of is not None
            else f"<no_identity:{index}>"
        )
        base_keys.append(key_fn(record, fallback=fallback))

    parent = list(range(count))

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_base: dict[str, int] = {}
    for index, key in enumerate(base_keys):
        if key in by_base:
            union(index, by_base[key])
        else:
            by_base[key] = index

    by_hash: dict[tuple, int] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or is_graphic(record):
            continue
        if record_visual_type(record) == "video":
            continue
        digest = stamped_content_hash(record)
        if digest is None and hash_of is not None:
            try:
                digest = hash_of(record)
            except Exception:
                digest = None
        if not digest:
            continue
        signature = ("still", digest)
        if signature in by_hash:
            union(index, by_hash[signature])
        else:
            by_hash[signature] = index

    return [base_keys[find(index)] for index in range(count)]


def distinct_identity_keys(records, hash_of=None, fallback_of=None) -> set:
    """The set of distinct visual identities among ``records``."""
    return set(resolve_identity_groups(records, hash_of, fallback_of))


def resolve_asset_groups(records, hash_of=None, fallback_of=None) -> list:
    """Group keys at the ASSET level (the AM-017 headline level) —
    ``resolve_identity_groups`` with ``asset_identity_key``."""
    return resolve_identity_groups(
        records, hash_of=hash_of, fallback_of=fallback_of,
        key_of=asset_identity_key,
    )


def distinct_asset_keys(records, hash_of=None, fallback_of=None) -> set:
    """The set of distinct ASSET-level identities among records."""
    return set(resolve_asset_groups(records, hash_of, fallback_of))
