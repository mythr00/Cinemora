"""Server-persisted Editor Studio track locks (Phase 6, A-17).

Track locks used to be client-only useState in the Studio page:
cleared on refresh, unknown to the server, and ignored by the
Inspector. They are now editor PROJECT state, stored server-side
in `editor_track_locks.json` under the project's media directory
(the established pattern for editor documents: files for
documents, DB for records — services/editor.py's revision
documents live the same way).

- `locked_tracks(project_id)` reads the persisted set (unknown /
  missing file = no locks; unknown track ids in the file are
  dropped, never trusted).
- `set_track_lock(project_id, track_id, locked)` validates the
  track id against the Studio's five tracks and writes atomically.
- `enforce_operations(project_id, doc, operations)` is the
  enforcement half: services/editor.apply_operations calls it
  before applying a batch, and any operation that would modify a
  locked track's clips/content is rejected with EditorError
  409 track_locked, so the whole batch fails atomically — the
  same rejection discipline as every other invalid batch.

Operation -> track mapping (the Studio track ids):
- item operations (remove/replace/trim/move/split/set_transition/
  assign_graphic/regenerate_graphic/set_graphic_duration): the
  track of the item they name — "graphics" for graphic items,
  "video" otherwise; add_clip creates a visual item -> "video".
- edit_caption -> "captions".
- regenerate_narration_sentence -> "narration".
- set_music -> "music".
- set_volumes -> "narration" and/or "music", per the keys present.
"""

from __future__ import annotations

import json
from pathlib import Path

from services import media_paths
from services.atomic_write import write_text_atomic

TRACK_IDS = ("video", "graphics", "captions", "narration", "music")

_LOCKS_FILENAME = "editor_track_locks.json"

_ITEM_OPS = frozenset(
    {
        "remove_clip",
        "replace_clip",
        "trim_clip",
        "move_clip",
        "split_clip",
        "set_transition",
        "assign_graphic",
        "regenerate_graphic",
        "set_graphic_duration",
    }
)


def _locks_path(project_id) -> Path:
    return media_paths.project_dir(project_id) / _LOCKS_FILENAME


def locked_tracks(project_id) -> list[str]:
    """The persisted locked track ids for a project (sorted)."""
    try:
        raw = json.loads(_locks_path(project_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    tracks = raw.get("locked_tracks") if isinstance(raw, dict) else None
    if not isinstance(tracks, list):
        return []
    return sorted({t for t in tracks if t in TRACK_IDS})


def set_track_lock(project_id, track_id: str, locked: bool) -> list[str]:
    """Persist one track's lock state; returns the full locked set.

    Raises ValueError for an unknown track id (the API layer maps
    it to 422)."""
    if track_id not in TRACK_IDS:
        raise ValueError(
            f"Unknown track {track_id!r}; expected one of: {', '.join(TRACK_IDS)}."
        )
    current = set(locked_tracks(project_id))
    if locked:
        current.add(track_id)
    else:
        current.discard(track_id)
    result = sorted(current)
    path = _locks_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        path, json.dumps({"locked_tracks": result}, indent=2, ensure_ascii=False)
    )
    return result


def _item_track(doc: dict, item_id) -> str | None:
    for item in (doc.get("tracks") or {}).get("visual") or []:
        if item.get("item_id") == item_id:
            return "graphics" if item.get("kind") == "graphic" else "video"
    return None


def operation_tracks(doc: dict, operation: dict) -> set[str]:
    """The Studio tracks one editor operation would modify."""
    op_type = operation.get("type")
    if op_type in _ITEM_OPS:
        track = _item_track(doc, operation.get("item_id"))
        return {track} if track else set()
    if op_type == "add_clip":
        return {"video"}
    if op_type == "edit_caption":
        return {"captions"}
    if op_type == "regenerate_narration_sentence":
        return {"narration"}
    if op_type == "set_music":
        return {"music"}
    if op_type == "set_volumes":
        return {k for k in ("narration", "music") if k in operation}
    return set()


def enforce_operations(project_id, doc: dict, operations: list) -> None:
    """Reject (EditorError 409 track_locked) any operation batch
    that touches a locked track. Called by editor.apply_operations
    before any operation is applied, so a locked batch persists
    nothing — no revision, no ledger rows."""
    from services.editor import EditorError

    locked = set(locked_tracks(project_id))
    if not locked:
        return
    for operation in operations:
        hit = operation_tracks(doc, operation) & locked
        if hit:
            track = sorted(hit)[0]
            raise EditorError(
                409,
                "track_locked",
                f"The {track} track is locked. Unlock it in the Editor "
                "Studio before editing its clips.",
            )
