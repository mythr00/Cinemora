"""Editor Studio read models + preview machinery.

The Studio (founder priority #5, Resolve Edit-page paradigm) is a
projection layer over the existing revision editor — it builds
nothing parallel:

- ``studio_view`` projects a revision document into the multitrack
  model the Studio UI renders: video / motion-graphics / captions /
  narration / music tracks, every entry carrying the media key the
  existing media endpoint serves it by (when the file is recorded
  in the project's media index).
- ``media_pool`` lists the project's real asset pool — the source
  run's ledger decisions (verified assets AND the alternates the
  pipeline recorded), the revision chain's editor decisions, and
  the project's uploads — with file presence stated honestly.
- ``preview_frame`` extracts the actual frame the CURRENT
  (unrendered) revision document shows at a timeline instant: the
  covering item is resolved through the renderer's own scene
  builder (so gap holdovers preview exactly as a render would
  cover them), and the frame is cut from the item's real source
  media at its mapped source time. Nothing is fabricated: a
  missing file is an error, never a placeholder image.
- ``waveform`` computes real per-bucket peaks from the actual
  narration / music audio via ffmpeg decode, cached on disk.

All edits still flow through services.editor.apply_operations and
all renders through editor.request_render; this module never
mutates a revision.
"""

from __future__ import annotations

import hashlib
import mimetypes
import subprocess
from array import array
from pathlib import Path

from services import editor, media_paths, plan_document, run_ledger
from services.atomic_write import write_json_atomic

EditorError = editor.EditorError

_PREVIEW_DIRNAME = "editor_preview"
_FFMPEG_TIMEOUT_SECONDS = 60
_WAVEFORM_SAMPLE_RATE = 8000


def _project_dir(project_id) -> Path:
    return media_paths.project_dir(project_id)


def _media_key(project_dir, index, path):
    """The servable media key for a file, or None when the media
    index does not record it (the file then cannot be streamed by
    the media endpoint and the UI must say so, not guess)."""
    if not path:
        return None
    try:
        key = plan_document.media_key_for(project_dir, path)
    except (ValueError, OSError):
        return None
    return key if key in index else None


def _type_for_path(path) -> str:
    suffix = Path(str(path)).suffix.lower()
    if suffix in editor.VIDEO_SUFFIXES:
        return "video"
    if suffix in editor.IMAGE_SUFFIXES:
        return "image"
    if suffix in editor.AUDIO_SUFFIXES:
        return "audio"
    return "unknown"


# ---------------------------------------------------------------------
# Studio view (multitrack projection of the revision document)
# ---------------------------------------------------------------------


def _item_view(project_dir, index, item) -> dict:
    asset = item.get("asset") or {}
    path = asset.get("file_path")
    return {
        "item_id": item["item_id"],
        "kind": item["kind"],
        "sentence_number": item.get("sentence_number"),
        "scene_number": item.get("scene_number"),
        "start": item.get("start"),
        "end": item.get("end"),
        "duration": item.get("duration"),
        "transition": item.get("transition") or "cut",
        "transition_reason": item.get("transition_reason"),
        "transition_window_frames": item.get("transition_window_frames"),
        "assignment": item.get("assignment"),
        "classification": item.get("classification"),
        "flags": item.get("flags") or [],
        "graphic": (
            {
                "template": (item.get("graphic") or {}).get("template"),
                "family": (item.get("graphic") or {}).get("family"),
                "style": (item.get("graphic") or {}).get("style"),
                "inputs": (item.get("graphic") or {}).get("inputs"),
                "has_recipe": isinstance(
                    (item.get("graphic") or {}).get("recipe"), dict
                ),
            }
            if item["kind"] == "graphic"
            else None
        ),
        "asset": {
            "file_path": path,
            "file_name": Path(str(path)).name if path else None,
            "type": asset.get("type"),
            "title": asset.get("title"),
            "source": asset.get("source"),
            "source_url": asset.get("source_url"),
            "clip_start": asset.get("clip_start") or 0,
            "source_duration": asset.get("source_duration"),
            "decision_id": asset.get("decision_id"),
            "media_key": _media_key(project_dir, index, path),
        },
    }


def studio_view(project_id, revision_id) -> dict:
    """The multitrack Studio model for one revision."""
    from services import transitions as transitions_service
    from services.motion_engine import templates as engine

    view, doc = editor.get_revision(project_id, revision_id)
    project_dir = _project_dir(project_id)
    index = plan_document.build_media_index(project_id, project_dir)
    tracks = doc.get("tracks") or {}
    visual = tracks.get("visual") or []

    video_items = [
        _item_view(project_dir, index, item)
        for item in visual
        if item["kind"] in ("clip", "gap")
    ]
    graphic_items = [
        _item_view(project_dir, index, item)
        for item in visual
        if item["kind"] == "graphic"
    ]
    narration_track = tracks.get("narration") or {}
    narration_path = media_paths.resolve_artefact(
        project_dir, "narration.wav", view["run_id"]
    )
    music_track = tracks.get("music") or {}
    captions_track = tracks.get("captions") or {}

    return {
        **view,
        "duration_seconds": doc.get("duration_seconds"),
        "volumes": doc.get("volumes") or {},
        "render_state": doc.get("render_state"),
        "last_render_error": doc.get("last_render_error"),
        "exports": doc.get("exports") or [],
        "tracks": [
            {
                "id": "video",
                "kind": "video",
                "name": "Video",
                "items": video_items,
            },
            {
                "id": "graphics",
                "kind": "graphics",
                "name": "Motion graphics",
                "items": graphic_items,
            },
            {
                "id": "captions",
                "kind": "captions",
                "name": "Captions",
                "segments": [
                    {
                        "segment_index": position,
                        "start": segment.get("start"),
                        "end": segment.get("end"),
                        "text": segment.get("text") or "",
                        "edited": bool(segment.get("edited")),
                    }
                    for position, segment in enumerate(
                        captions_track.get("segments") or []
                    )
                ],
                "stale": bool(captions_track.get("stale")),
                "stale_reason": captions_track.get("stale_reason"),
            },
            {
                "id": "narration",
                "kind": "audio",
                "name": "Narration",
                "volume": (doc.get("volumes") or {}).get("narration", 1.0),
                "voice_id": narration_track.get("voice_id"),
                "media_key": _media_key(
                    project_dir, index,
                    str(narration_path) if narration_path.is_file() else None,
                ),
                "sentences": [
                    {
                        "sentence_number": s.get("sentence_number"),
                        "scene_number": s.get("scene_number"),
                        "start": s.get("start"),
                        "end": s.get("end"),
                        "text": s.get("text") or "",
                        "dirty": s.get("sentence_number")
                        in (narration_track.get("dirty_sentences") or []),
                    }
                    for s in (narration_track.get("sentences") or [])
                ],
            },
            {
                "id": "music",
                "kind": "audio",
                "name": "Music",
                "volume": (doc.get("volumes") or {}).get("music", 0.0),
                "state": music_track.get("state"),
                "file_name": (
                    Path(str(music_track["file"])).name
                    if music_track.get("file")
                    else None
                ),
                "media_key": _media_key(
                    project_dir, index, music_track.get("file")
                ),
            },
        ],
        "catalogues": {
            # The transitions the renderer actually honours, minus
            # match_cut (a rule over production evidence, refused
            # by the set_transition op with its reason).
            "transitions": sorted(
                transitions_service.VALID_TRANSITIONS - {"match_cut"}
            ),
            "transition_reasons": sorted(transitions_service.VALID_REASONS),
            "graphic_templates": sorted(engine.TEMPLATES.keys()),
        },
    }


# ---------------------------------------------------------------------
# Media pool (the project's real recorded assets)
# ---------------------------------------------------------------------


def media_pool(project_id, revision_id, limit=100, offset=0) -> dict:
    """Ledger decisions for the revision's source run (production
    + editor-appended) plus the project's uploads. An entry whose
    file is not on disk is listed with available=False — the pool
    describes the project's records honestly; replace/add resolve
    through the editor's own machinery, which refuses missing
    files with its own error."""
    view, doc = editor.get_revision(project_id, revision_id)
    project_dir = _project_dir(project_id)
    index = plan_document.build_media_index(project_id, project_dir)

    used_paths = set()
    used_decisions = set()
    for item in (doc.get("tracks") or {}).get("visual") or []:
        asset = item.get("asset") or {}
        if asset.get("file_path"):
            used_paths.add(str(asset["file_path"]))
        if asset.get("decision_id"):
            used_decisions.add(str(asset["decision_id"]))

    entries: list[dict] = []
    seen_paths: set[str] = set()
    decisions, _total = run_ledger.list_decisions(view["run_id"], 100_000, 0)
    for row in decisions:
        path = row.get("asset_key")
        if not path or str(path) in seen_paths:
            continue
        seen_paths.add(str(path))
        exists = Path(str(path)).is_file()
        asset_like = {
            "user_supplied": False,
            "evidence_class": row.get("evidence_class"),
            "verification": row.get("verification"),
            "type": _type_for_path(path),
            # A "generated" ledger decision IS a generated graphic —
            # the classification derives from that recorded type,
            # exactly as classify_asset expects.
            "generated": row.get("decision_type") == "generated",
        }
        entries.append(
            {
                "pool_id": f"ledger:{row['id']}",
                "origin": "ledger",
                "decision_id": row["id"],
                "upload_id": None,
                "file_path": str(path),
                "file_name": Path(str(path)).name,
                "type": _type_for_path(path),
                "classification": editor.classify_asset(asset_like),
                "provider": row.get("provider"),
                "source_url": row.get("source_url"),
                "scene_number": row.get("scene_number"),
                "sentence_number": row.get("sentence_number"),
                "revision_id": row.get("revision_id"),
                "decision_type": row.get("decision_type"),
                "source_duration": row.get("interval_duration"),
                "media_key": _media_key(project_dir, index, str(path))
                if exists
                else None,
                "available": exists,
                "in_timeline": str(row["id"]) in used_decisions
                or str(path) in used_paths,
            }
        )
    for upload in editor._uploads_index(project_dir):
        path = upload.get("file_path")
        if not path or str(path) in seen_paths:
            continue
        seen_paths.add(str(path))
        exists = Path(str(path)).is_file()
        entries.append(
            {
                "pool_id": f"upload:{upload.get('upload_id')}",
                "origin": "upload",
                "decision_id": None,
                "upload_id": upload.get("upload_id"),
                "file_path": str(path),
                "file_name": Path(str(path)).name,
                "type": upload.get("type") or _type_for_path(path),
                "classification": "user_supplied",
                "provider": "user_supplied",
                "source_url": None,
                "scene_number": None,
                "sentence_number": None,
                "revision_id": None,
                "decision_type": "upload",
                "source_duration": upload.get("source_duration"),
                "media_key": _media_key(project_dir, index, str(path))
                if exists
                else None,
                "available": exists,
                "in_timeline": str(path) in used_paths,
            }
        )

    total = len(entries)
    limit = min(max(1, int(limit)), 500)
    offset = max(0, int(offset))
    return {
        "revision_id": view["revision_id"],
        "run_id": view["run_id"],
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": entries[offset : offset + limit],
    }


# ---------------------------------------------------------------------
# Frame-accurate preview of the unrendered document
# ---------------------------------------------------------------------


def _covering_item(doc, t: float) -> dict:
    """The renderer-shaped item covering timeline instant t —
    resolved through editor._build_render_scenes so gap holdovers
    preview exactly as a render covers them (flagged on the item
    as editor_gap_holdover)."""
    try:
        scenes, _holdovers = editor._build_render_scenes(doc)
    except RuntimeError as error:
        raise EditorError(422, "no_visuals", str(error)) from error
    flat = [item for scene in scenes for item in scene["items"]]
    flat.sort(key=lambda i: (float(i.get("start") or 0), float(i.get("end") or 0)))
    if not flat:
        raise EditorError(422, "no_visuals", "The revision has no visual items.")
    for item in flat:
        if float(item.get("start") or 0) <= t < float(item.get("end") or 0):
            return item
    if t < float(flat[0].get("start") or 0):
        return flat[0]
    return flat[-1]


def preview_frame(project_id, revision_id, t: float):
    """(path, content_type) of the real frame at timeline time t.

    Videos: the frame is cut from the item's source media at
    clip_start + (t - item.start), cached under the project dir
    keyed by file identity + source frame number, so repeat
    requests for the same frame are free. Images: the image file
    itself. Missing media is an error — never a placeholder."""
    _view, doc = editor.get_revision(project_id, revision_id)
    project_dir = _project_dir(project_id)
    try:
        t = float(t)
    except (TypeError, ValueError):
        raise EditorError(422, "time_invalid", "t must be a number.") from None
    if t < 0:
        raise EditorError(422, "time_invalid", "t must not be negative.")
    item = _covering_item(doc, t)
    path = item.get("file_path")
    if not path or not Path(str(path)).is_file():
        raise EditorError(
            404, "frame_unavailable",
            "The visual covering this instant has no media file on disk.",
        )
    path = Path(str(path))
    media_type = item.get("type") or _type_for_path(path)
    if media_type == "image":
        return path, (
            mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
    if media_type != "video":
        raise EditorError(
            422, "frame_unsupported",
            f"The covering visual is {media_type}, not video or image.",
        )
    start = float(item.get("start") or 0)
    end = float(item.get("end") or 0)
    offset = max(0.0, t - start)
    if t >= end:
        # Past the item's end (the playhead can sit on the final
        # frame): preview the item's last frame, not past its media.
        offset = max(0.0, (end - start) - 0.04)
    source_t = float(item.get("clip_start") or 0) + offset
    stat = path.stat()
    frame_number = int(round(source_t * 30))
    cache_key = hashlib.sha256(
        f"{path}|{stat.st_size}|{stat.st_mtime_ns}|{frame_number}".encode()
    ).hexdigest()[:24]
    cache_dir = project_dir / _PREVIEW_DIRNAME
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"frame_{cache_key}.jpg"
    if target.is_file() and target.stat().st_size > 0:
        return target, "image/jpeg"
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-ss", f"{source_t:.3f}",
                "-i", str(path),
                "-frames:v", "1",
                "-vf", "scale=960:-2",
                "-q:v", "3",
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise EditorError(
            500, "frame_extract_failed",
            "ffmpeg could not extract the preview frame.",
        ) from error
    if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        raise EditorError(
            500, "frame_extract_failed",
            "ffmpeg could not extract the preview frame from the "
            "covering item's media.",
        )
    return target, "image/jpeg"


# ---------------------------------------------------------------------
# Waveforms (real decoded peaks, cached)
# ---------------------------------------------------------------------


def _audio_source(project_id, doc, view, track: str) -> Path:
    project_dir = _project_dir(project_id)
    if track == "narration":
        path = media_paths.resolve_artefact(
            project_dir, "narration.wav", view["run_id"]
        )
        if not path.is_file():
            raise EditorError(
                422, "waveform_unavailable",
                "The production recorded no narration file.",
            )
        return path
    if track == "music":
        music = (doc.get("tracks") or {}).get("music") or {}
        if not music.get("file"):
            raise EditorError(
                422, "waveform_unavailable",
                "This revision has no music file.",
            )
        path = Path(str(music["file"]))
        if not path.is_file():
            raise EditorError(
                422, "waveform_unavailable",
                "The revision's music file is not on disk.",
            )
        return path
    raise EditorError(
        422, "unknown_track", "track must be 'narration' or 'music'."
    )


def waveform(project_id, revision_id, track: str) -> dict:
    """Per-bucket peak amplitudes of the real audio file, decoded
    with ffmpeg to mono f32le and bucketed at ~20 buckets/second
    (bounded). Cached by file identity next to the preview frames;
    a changed file recomputes."""
    view, doc = editor.get_revision(project_id, revision_id)
    project_dir = _project_dir(project_id)
    path = _audio_source(project_id, doc, view, track)
    stat = path.stat()
    cache_key = hashlib.sha256(
        f"wave|{path}|{stat.st_size}|{stat.st_mtime_ns}".encode()
    ).hexdigest()[:24]
    cache_dir = project_dir / _PREVIEW_DIRNAME
    cache_file = cache_dir / f"waveform_{cache_key}.json"
    if cache_file.is_file():
        cached = editor._read_json(cache_file)
        if isinstance(cached, dict) and "peaks" in cached:
            return cached

    probed = editor._probe_any(path)
    duration = float((probed[1].get("duration_seconds") if probed else 0) or 0)
    if duration <= 0:
        raise EditorError(
            422, "waveform_unavailable",
            "The audio file could not be probed for a duration.",
        )
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-i", str(path),
                "-ac", "1",
                "-ar", str(_WAVEFORM_SAMPLE_RATE),
                "-f", "f32le",
                "-",
            ],
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise EditorError(
            500, "waveform_failed", "ffmpeg could not decode the audio.",
        ) from error
    if result.returncode != 0 or not result.stdout:
        raise EditorError(
            500, "waveform_failed", "ffmpeg could not decode the audio.",
        )
    samples = array("f")
    samples.frombytes(result.stdout[: len(result.stdout) // 4 * 4])
    bucket_count = min(4000, max(200, int(duration * 20)))
    per_bucket = max(1, len(samples) // bucket_count)
    raw_peaks = []
    for bucket in range(bucket_count):
        chunk = samples[bucket * per_bucket : (bucket + 1) * per_bucket]
        if not chunk:
            raw_peaks.append(0.0)
            continue
        raw_peaks.append(max(abs(v) for v in chunk))
    # Peaks are the waveform SHAPE, normalised to the file's own
    # maximum (resampling can overshoot full scale slightly, so raw
    # buckets are not bounded by 1.0). The true maximum is carried
    # separately as `peak` — nothing about the level is hidden.
    peak = max(raw_peaks) if raw_peaks else 0.0
    if peak > 0:
        peaks = [round(value / peak, 4) for value in raw_peaks]
    else:
        peaks = [0.0] * len(raw_peaks)
    payload = {
        "track": track,
        "duration_seconds": round(duration, 3),
        "bucket_count": bucket_count,
        "bucket_seconds": round(duration / bucket_count, 4),
        "peak": round(peak, 4),
        "peaks": peaks,
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(cache_file, payload)
    return payload
