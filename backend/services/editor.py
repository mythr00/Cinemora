"""Self-service editor backend (Stage 6).

The editor edits a finished production as a STRUCTURED production —
timeline items + asset records + ledger decisions — never as a
flattened MP4. Everything here is non-destructive:

- The original production's artefacts (timeline.json, narration,
  captions, the rendered film, its ledger rows, its certificate)
  are READ by the editor and never written.
- Editing works on REVISIONS. Revision 0 imports a completed run's
  timeline into the editor document shape. Every applied batch of
  operations creates the NEXT revision node (parent_revision_id)
  instead of mutating its parent; the chain is the backend basis
  for undo/redo, and earlier revisions stay reachable/renderable.
- Asset-changing operations append AssetDecision rows tagged with
  the revision id (migration 0004). The ledger stays append-only;
  manual editing NEVER makes an asset verified: replacements earn
  a fresh probe + fresh verdict through Stage 1's evidence-safe
  swap machinery (media_pipeline._refresh_swapped_asset — the core
  apply_review_decisions uses), uploads are user_supplied with
  licence "unknown", library assets keep their own recorded
  evidence verbatim.

TIMING MODEL. Narration is the spine: every narration sentence owns
a fixed window [start, end] derived from the production's sentence
timings. Visual items live inside one sentence window, and within
each window the items' durations ALWAYS sum to the window duration
— time is conserved. Shortening an item frees time into an explicit
gap item; lengthening consumes gap time (or fails). A gap is a
first-class, flagged timeline item: it is never silently filled,
and it blocks export unless the caller explicitly renders with
gaps flagged (the gap is then covered by extending the previous
genuine visual — the renderer's own recorded holdover behaviour —
and the export record names every gap it covered).

RENDERING reuses the production machinery in a per-revision work
directory: renderer.render_timeline (frame-locked), narration
per-sentence parts (Stage 2D), music.prepare_soundtrack,
captions.build_captions / write_edited_captions (the Stage 3
single writer), caption_renderer burn, and the final video auditor.
Reuse is at the ASSET level: unchanged assets are never
re-downloaded, re-researched or re-verified, narration/captions/
soundtrack are rebuilt only when the render plan's dependency
rules say so. Segment-level rendered-clip reuse is NOT possible
with the existing renderer: render_scene assembles clips
positionally (clips/scene_N/clip_<index>.mp4) from source media on
every pass and has no content-keyed clip cache, so the frame-locked
renderer re-renders clips from the (reused) source assets. The
render plan records exactly which items are reused vs changed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from models import Project, ProjectRevision
from services import media_paths, media_probe, plan_document, run_ledger
from services.atomic_write import write_json_atomic, write_text_atomic

DOC_VERSION = 1
MIN_ITEM_DURATION = 0.2
TIME_TOLERANCE = 0.06

VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

UPLOAD_MAX_BYTES_DEFAULT = 512 * 1024 * 1024  # 512 MiB


class EditorError(Exception):
    """A domain error with an HTTP status + stable machine code
    (the API layer maps it onto the v1 error shape)."""

    def __init__(self, status_code: int, code: str, message: str,
                 details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _item_id() -> str:
    return "itm_" + uuid.uuid4().hex[:16]


def _session():
    from database import SessionLocal

    return SessionLocal()


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _atomic_write_json(path, payload) -> str:
    """Write JSON atomically (temp + fsync + os.replace, the MA-11
    pattern, via services.atomic_write since Stage 4). Returns the
    sha256 of the written payload."""
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    write_text_atomic(path, text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_filename(revision_id) -> str:
    return f"revision_{revision_id}.json"


def _project_dir(project_id) -> Path:
    return media_paths.project_dir(project_id)


# ---------------------------------------------------------------------
# Classification (the founder's Part 4 vocabulary)
# ---------------------------------------------------------------------


def classify_asset(asset, kind=None) -> str:
    """One of: verified_footage, positional_segment,
    generated_graphic, still_image, user_supplied, unverified,
    rejected. Derived ONLY from the asset's own recorded evidence —
    never from the fact that a human placed it on a timeline."""
    if not isinstance(asset, dict):
        return "unverified"
    if asset.get("user_supplied"):
        return "user_supplied"
    if kind == "graphic" or asset.get("generated"):
        return "generated_graphic"
    evidence = asset.get("evidence_class") or asset.get("segment_evidence")
    if evidence == "positional_no_contract":
        return "positional_segment"
    verification = asset.get("verification")
    if isinstance(verification, dict) and verification.get("accepted") is True:
        return "still_image" if asset.get("type") == "image" else "verified_footage"
    if isinstance(verification, dict) and verification.get("accepted") is False:
        # A manual swap that no scenario could verify carries the
        # explicit manual_swap_unverified verdict (Stage 1): it was
        # never verified — "unverified", not "rejected".
        reason = verification.get("rejection_reason") or verification.get("reason")
        if reason == "manual_swap_unverified":
            return "unverified"
        return "rejected"
    return "unverified"


# ---------------------------------------------------------------------
# Revision rows (DB = index of record)
# ---------------------------------------------------------------------


def _row_view(row) -> dict:
    return {
        "revision_id": row.id,
        "project_id": row.project_id,
        "run_id": row.run_id,
        "parent_revision_id": row.parent_revision_id,
        "revision_number": row.revision_number,
        "status": row.status,
        "document_path": row.document_path,
        "document_hash": row.document_hash,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _get_row(db, project_id, revision_id):
    from models import Project, ProjectRevision

    row = db.get(ProjectRevision, str(revision_id))
    if row is None or row.project_id != str(project_id):
        raise EditorError(
            404, "revision_not_found", f"No revision {revision_id} "
            f"for project {project_id}."
        )
    return row


def _load_doc(project_id, row) -> dict:
    path = _project_dir(project_id) / row.document_path
    doc = _read_json(path)
    if not isinstance(doc, dict):
        raise EditorError(
            500, "revision_document_missing",
            f"Revision document {row.document_path} is missing or unreadable.",
        )
    return doc


def _persist_new_revision(project_id, run_id, parent_row, doc) -> dict:
    """Write the document file, then insert its DB row. On a DB
    failure the orphaned file is removed again — a revision exists
    only when both halves exist."""
    from models import ProjectRevision

    revision_id = doc["revision_id"]
    filename = document_filename(revision_id)
    project_dir = _project_dir(project_id)
    doc_hash = _atomic_write_json(project_dir / filename, doc)
    db = _session()
    try:
        row = ProjectRevision(
            id=revision_id,
            project_id=str(project_id),
            run_id=str(run_id),
            parent_revision_id=parent_row.id if parent_row else None,
            revision_number=doc["revision_number"],
            status=doc.get("status", "draft"),
            document_path=filename,
            document_hash=doc_hash,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        return _row_view(row)
    except Exception:
        try:
            (project_dir / filename).unlink()
        except OSError:
            pass
        raise
    finally:
        db.close()


def _update_row_status(project_id, revision_id, status):
    from models import ProjectRevision

    db = _session()
    try:
        row = db.get(ProjectRevision, str(revision_id))
        if row is not None:
            row.status = status
            row.updated_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


def _load_project_row(project_id) -> dict | None:
    from models import Project

    db = _session()
    try:
        project = db.get(Project, str(project_id))
        if project is None:
            return None
        return {
            "id": project.id,
            "script": project.script,
            "title": project.title,
            "category": project.category,
            "status": project.status,
        }
    finally:
        db.close()


def _update_doc_file(project_id, doc) -> str:
    """Rewrite an existing revision document in place (same path,
    same DB row) — used for render-state bookkeeping, narration
    retiming write-back and export records. Returns the new hash."""
    path = _project_dir(project_id) / document_filename(doc["revision_id"])
    digest = _atomic_write_json(path, doc)
    db = _session()
    try:
        row = db.get(ProjectRevision, doc["revision_id"])
        if row is not None:
            row.document_hash = digest
            db.commit()
    finally:
        db.close()
    return digest


def _sync_status(db, row):
    """Reflect a finished render run onto a revision still marked
    rendering (the worker updates it too; this keeps reads honest
    when a render died with its worker — reconcile_stale_runs has
    already marked the orphaned run failed/interrupted)."""
    if row.status != "rendering":
        return row
    for run in run_ledger.list_runs_for(row.project_id):
        options = run.get("options") or {}
        if (
            options.get("kind") == "revision_render"
            and str(options.get("revision_id")) == row.id
        ):
            if run["status"] in run_ledger.TERMINAL_STATUSES:
                row.status = (
                    "draft" if run["status"] == "cancelled" else "failed"
                )
                row.updated_at = datetime.now(timezone.utc)
                db.commit()
            break
    return row


def list_revisions(project_id) -> list[dict]:
    from models import ProjectRevision

    db = _session()
    try:
        rows = (
            db.query(ProjectRevision)
            .filter(ProjectRevision.project_id == str(project_id))
            .order_by(ProjectRevision.created_at.asc())
            .all()
        )
        return [_row_view(_sync_status(db, row)) for row in rows]
    finally:
        db.close()


def get_revision(project_id, revision_id) -> tuple[dict, dict]:
    db = _session()
    try:
        row = _sync_status(db, _get_row(db, project_id, revision_id))
        view = _row_view(row)
    finally:
        db.close()
    return view, _load_doc(project_id, row)


def latest_revision(project_id) -> tuple[dict, dict] | None:
    from models import ProjectRevision

    db = _session()
    try:
        row = (
            db.query(ProjectRevision)
            .filter(ProjectRevision.project_id == str(project_id))
            .order_by(ProjectRevision.created_at.desc())
            .first()
        )
        if row is None:
            return None
        view = _row_view(_sync_status(db, row))
    finally:
        db.close()
    return view, _load_doc(project_id, row)


def revision_chain_ids(project_id, revision_id) -> list[str]:
    """Chain ids from revision 0 down to (and including) the given
    revision, following parent links."""
    from models import ProjectRevision

    db = _session()
    try:
        chain = []
        seen = set()
        current = str(revision_id)
        while current and current not in seen:
            seen.add(current)
            row = db.get(ProjectRevision, current)
            if row is None or row.project_id != str(project_id):
                break
            chain.append(row.id)
            current = row.parent_revision_id
        return list(reversed(chain))
    finally:
        db.close()


# ---------------------------------------------------------------------
# Import: production timeline -> editor document (revision 0)
# ---------------------------------------------------------------------


def _sentence_number_map(project_dir, timeline) -> dict:
    """(scene_number, position_in_scene) -> global sentence_number,
    from plan.json when present; else sequential enumeration."""
    plan = _read_json(project_dir / plan_document.PLAN_FILENAME)
    mapping = {}
    if isinstance(plan, dict):
        for scene in plan.get("scenes") or []:
            numbers = scene.get("sentence_numbers") or []
            for position, number in enumerate(numbers):
                if isinstance(number, int):
                    mapping[(scene.get("scene_number"), position)] = number
    return mapping


def import_production(project_id, run) -> dict:
    """Build the revision-0 editor document from a completed run's
    recorded artefacts. READ-ONLY with respect to the production:
    timeline.json, rights_manifest.json, captions.json,
    soundtrack.json, production_state.json and the ledger are only
    ever read."""
    project_dir = _project_dir(project_id)
    run_id = run["run_id"]
    # Stage 4 (DATA-004): the import serves THIS run — its derived
    # artefacts resolve to the run's artefact directory, falling
    # back to the legacy project-level files for runs that predate
    # run-scoping. A later re-run can no longer change what an
    # earlier run's import sees.
    timeline = _read_json(
        media_paths.resolve_artefact(project_dir, "timeline.json", run_id)
    )
    if not isinstance(timeline, dict):
        raise EditorError(
            409, "no_timeline",
            "The production has no timeline.json to import.",
        )

    # Rights by file path (rights_manifest.json).
    rights_by_path = {}
    for entry in (
        _read_json(
            media_paths.resolve_artefact(
                project_dir, "rights_manifest.json", run_id
            )
        )
        or []
    ):
        if isinstance(entry, dict) and entry.get("file_path"):
            rights_by_path[str(entry["file_path"])] = {
                "license": entry.get("license") or "unknown",
                "license_url": entry.get("license_url") or "",
                "rights_status": entry.get("rights_status")
                or "verify_before_publish",
                "creator": entry.get("creator") or "",
                "source": entry.get("source") or "",
                "retrieved_at": entry.get("retrieved_at") or "",
            }

    # Ledger decisions for evidence matching (content hash first —
    # the same identity record_decisions stamped).
    decisions, _ = run_ledger.list_decisions(run_id, 100_000, 0)
    by_hash = {}
    by_key = {}
    for row in decisions:
        if row.get("content_hash"):
            by_hash.setdefault(row["content_hash"], row)
        if row.get("asset_key"):
            by_key.setdefault(str(row["asset_key"]), row)
    hash_cache: dict = {}

    number_map = _sentence_number_map(project_dir, timeline)

    items: list[dict] = []
    sentences: list[dict] = []
    fallback_number = 0
    scenes = timeline.get("scenes") or []
    flat_scenes = scenes if scenes else [{"scene_number": 1, "items": timeline.get("items") or []}]
    for scene in flat_scenes:
        if not isinstance(scene, dict):
            continue
        scene_number = scene.get("scene_number") or 1
        scene_items = [
            i for i in (scene.get("items") or []) if isinstance(i, dict)
        ]
        scene_items.sort(key=lambda i: (i.get("sentence_index", 0), i.get("start", 0)))
        windows: dict[int, list[dict]] = {}
        for item in scene_items:
            windows.setdefault(item.get("sentence_index", 0), []).append(item)
        for position in sorted(windows):
            group = windows[position]
            number = number_map.get((scene_number, position))
            if number is None:
                fallback_number += 1
                number = fallback_number
            else:
                fallback_number = max(fallback_number, number)
            start = min(float(i.get("start") or 0) for i in group)
            end = max(float(i.get("end") or 0) for i in group)
            text = next(
                (i.get("sentence") for i in group if i.get("sentence")), ""
            )
            sentences.append(
                {
                    "sentence_number": number,
                    "scene_number": scene_number,
                    "text": text,
                    "start": round(start, 3),
                    "end": round(end, 3),
                }
            )
            for item in group:
                path = str(item.get("file_path") or item.get("path") or "")
                content_hash = run_ledger._file_sha256(path, hash_cache)
                row = by_hash.get(content_hash) if content_hash else None
                if row is None and path:
                    row = by_key.get(path)
                kind = (
                    "graphic"
                    if item.get("assignment") == "motion_graphic"
                    or item.get("generator") == "motion_engine"
                    or item.get("template")
                    else "clip"
                )
                asset = {
                    "file_path": path or None,
                    "type": item.get("type") or "video",
                    "source": item.get("source"),
                    "provider": item.get("source"),
                    "source_url": item.get("source_url")
                    or item.get("video_url")
                    or item.get("image_url"),
                    "title": item.get("title"),
                    "clip_start": float(item.get("clip_start") or 0.0),
                    "source_duration": (
                        row.get("interval_duration") if row else None
                    ),
                    "decision_id": row.get("id") if row else None,
                    "evidence_class": (
                        (row.get("evidence_class") if row else None)
                        or item.get("segment_evidence")
                    ),
                    "verification": row.get("verification") if row else None,
                    "rights": (
                        (row.get("rights") if row else None)
                        or rights_by_path.get(path)
                    ),
                    "user_supplied": False,
                    "generated": kind == "graphic",
                }
                graphic = None
                if kind == "graphic":
                    graphic = {
                        "template": item.get("template"),
                        "family": item.get("family"),
                        "style": item.get("style"),
                        "inputs": item.get("engine_inputs"),
                        "engine_metadata": item.get("engine_metadata"),
                        # Phase 2 (MG-03): the complete recorded
                        # regeneration recipe, carried from the
                        # production asset record through the
                        # timeline item. None for graphics produced
                        # before recipes were recorded — regeneration
                        # then honestly refuses (inputs not recorded).
                        "recipe": item.get("graphic_recipe"),
                    }
                items.append(
                    {
                        "item_id": _item_id(),
                        "kind": kind,
                        "sentence_number": number,
                        "scene_number": scene_number,
                        "start": round(float(item.get("start") or 0), 3),
                        "end": round(float(item.get("end") or 0), 3),
                        "duration": round(
                            float(item.get("duration") or 0), 3
                        ),
                        "assignment": item.get("assignment"),
                        "transition": item.get("transition") or "cut",
                        "asset": asset,
                        "graphic": graphic,
                        "classification": classify_asset(asset, kind),
                        "flags": [],
                    }
                )

    # Narration + voice.
    state = (
        _read_json(
            media_paths.resolve_artefact(
                project_dir, "production_state.json", run_id
            )
        )
        or {}
    )
    voice = state.get("voice") if isinstance(state.get("voice"), dict) else {}
    narration = {
        "file": "narration.wav",
        "voice_id": voice.get("voice_id_used")
        or (run.get("options") or {}).get("voice_id"),
        "sentences": sentences,
        "dirty_sentences": [],
    }

    # Music (Stage 3H honest soundtrack record; Stage H carries
    # the record's explicit state + the record itself into the
    # revision, so a revision's music provenance starts from what
    # the production actually recorded — never re-derived).
    soundtrack = _read_json(project_dir / "soundtrack.json") or {}
    music_file = None
    if soundtrack.get("source"):
        candidate = project_dir / str(soundtrack["source"])
        if candidate.is_file():
            music_file = str(candidate)
    if music_file is None and (run.get("options") or {}).get("music_file"):
        candidate = Path(str(run["options"]["music_file"]))
        if candidate.is_file():
            music_file = str(candidate)
    try:
        from services.music import MUSIC_VOLUME

        default_music_volume = MUSIC_VOLUME
    except Exception:
        default_music_volume = 0.1
    # Pre-Stage-H soundtrack records have no "state": derive only
    # what the old record supports (mixed -> applied; anything else
    # -> not_requested — disabled/requested_but_missing cannot be
    # distinguished in old records and are not invented).
    imported_state = soundtrack.get("state") or (
        "applied" if soundtrack.get("music") == "mixed" else "not_requested"
    )
    if music_file is None and imported_state == "applied":
        imported_state = "not_requested"
    music = {
        "file": music_file,
        "source": soundtrack.get("source"),
        "mixed": soundtrack.get("music"),
        "state": imported_state,
        # D7: editor music entries carry a licence field — for
        # operator/editor-supplied files it is "unknown", recorded
        # as unknown, never upgraded.
        "license": "unknown",
        "soundtrack_record": soundtrack if soundtrack else None,
    }

    # Captions (segments verbatim; words kept until a text edit
    # makes them stale).
    captions_data = _read_json(project_dir / "captions.json") or {}
    captions = {
        "file": "captions.json",
        "mode": state.get("captions_mode") or "burn",
        "timing_source": captions_data.get("timing_source"),
        "provisional": bool(captions_data.get("provisional")),
        "segments": [
            s for s in (captions_data.get("segments") or [])
            if isinstance(s, dict)
        ],
        "stale": False,
        "stale_reason": None,
    }

    duration = max((s["end"] for s in sentences), default=0.0)
    doc = {
        "version": DOC_VERSION,
        "revision_id": _new_id(),
        "project_id": str(project_id),
        "run_id": str(run_id),
        "parent_revision_id": None,
        "revision_number": 0,
        "status": "draft",
        "created_at": _now_iso(),
        "created_from": "production_import",
        "duration_seconds": round(duration, 3),
        "tracks": {
            "visual": items,
            "narration": narration,
            "music": music,
            "captions": captions,
        },
        "volumes": {"narration": 1.0, "music": default_music_volume},
        "op_log": [
            {
                "op_id": _new_id(),
                "type": "import_production",
                "actor": "system",
                "at": _now_iso(),
                "summary": (
                    f"Revision 0 imported from run {run_id}: "
                    f"{len(items)} visual items, {len(sentences)} "
                    "narration sentences."
                ),
            }
        ],
        "exports": [],
        "render_state": None,
        "last_render_error": None,
    }
    _retime(doc)
    return doc


def _existing_root_revision(project_id):
    """(view, doc) of the project's revision-0 root — the row with
    parent_revision_id NULL — or None when the project has none."""
    db = _session()
    try:
        row = (
            db.query(ProjectRevision)
            .filter(
                ProjectRevision.project_id == str(project_id),
                ProjectRevision.parent_revision_id.is_(None),
            )
            .order_by(ProjectRevision.created_at.asc())
            .first()
        )
        root_id = row.id if row is not None else None
    finally:
        db.close()
    if root_id is None:
        return None
    return get_revision(project_id, root_id)


def create_revision_zero(project_id, run_id=None) -> tuple[dict, dict]:
    """Create revision 0 from a completed run (the given one, else
    the latest completed run of the project).

    Race-safe (Stage 6 adversarial defect D1): the single-root
    partial unique index (migration 0005) lets exactly one creator
    insert the root; a concurrent creator's insert fails with an
    integrity error and the winner's root — which imports the same
    run identically — is returned instead of an error."""
    run = None
    if run_id:
        run = run_ledger.get_run(run_id)
        if run is None or run["project_id"] != str(project_id):
            raise EditorError(404, "run_not_found", f"No run {run_id}.")
        if run["status"] != "completed":
            raise EditorError(
                409, "run_not_completed",
                "Only a completed run can be imported into the editor.",
            )
    else:
        for candidate in run_ledger.list_runs_for(project_id):
            if candidate["status"] == "completed":
                run = candidate
                break
    if run is None:
        raise EditorError(
            404, "no_completed_run",
            "This project has no completed production run to edit.",
        )
    doc = import_production(project_id, run)
    try:
        view = _persist_new_revision(project_id, run["run_id"], None, doc)
    except IntegrityError:
        # D1 race loser: another creator inserted this project's
        # root first (the single-root index rejected ours, and
        # _persist_new_revision already removed our document file).
        # The existing root is the answer the caller wanted; only
        # when NO root exists was the integrity error about
        # something else — that failure propagates unchanged.
        existing = _existing_root_revision(project_id)
        if existing is None:
            raise
        return existing
    return view, doc


def create_revision(project_id, *, from_revision_id=None, from_run_id=None,
                    actor="operator") -> tuple[dict, dict]:
    """Create a draft revision: from an existing revision (a branch
    node copying its document) or from a run (a fresh revision 0
    chain for that run)."""
    if from_revision_id:
        parent_view, parent_doc = get_revision(project_id, from_revision_id)
        db = _session()
        try:
            parent_row = _get_row(db, project_id, from_revision_id)
        finally:
            db.close()
        doc = copy.deepcopy(parent_doc)
        doc["revision_id"] = _new_id()
        doc["parent_revision_id"] = parent_row.id
        doc["revision_number"] = parent_row.revision_number + 1
        doc["status"] = "draft"
        doc["created_at"] = _now_iso()
        doc["created_from"] = "revision_branch"
        doc["render_state"] = None
        doc["last_render_error"] = None
        # A branch has never rendered: it must not inherit the
        # parent's export records (they name the PARENT's output
        # file) — the same reset apply_operations performs
        # (Stage 6 adversarial defect D2).
        doc["exports"] = []
        doc["op_log"] = list(parent_doc.get("op_log") or []) + [
            {
                "op_id": _new_id(),
                "type": "branch_revision",
                "actor": actor,
                "at": _now_iso(),
                "summary": f"Branched from revision {parent_row.id}.",
            }
        ]
        view = _persist_new_revision(
            project_id, parent_row.run_id, parent_row, doc
        )
        return view, doc
    return create_revision_zero(project_id, run_id=from_run_id)


def get_editor(project_id) -> tuple[dict, dict]:
    """The latest revision for the studio's editor screen; creates
    revision 0 from the latest completed run when none exists."""
    latest = latest_revision(project_id)
    if latest is not None:
        return latest
    return create_revision_zero(project_id)


# ---------------------------------------------------------------------
# Timing: windows are conserved
# ---------------------------------------------------------------------


def _windows(doc) -> dict[int, dict]:
    narration = doc["tracks"]["narration"]
    return {
        s["sentence_number"]: s for s in narration.get("sentences") or []
    }


def _visual(doc) -> list[dict]:
    return doc["tracks"]["visual"]


def _window_items(doc, sentence_number) -> list[dict]:
    return [
        item for item in _visual(doc)
        if item.get("sentence_number") == sentence_number
    ]


def _retime(doc):
    """Recompute start/end from sentence windows + item durations.
    Items are laid out sequentially inside their window in document
    order; the doc's duration is the last window's end."""
    windows = _windows(doc)
    for item in _visual(doc):
        window = windows.get(item.get("sentence_number"))
        if window is None:
            continue
        item["_cursor"] = window["start"]
    # Sequential layout per window, in document order.
    cursors = {n: w["start"] for n, w in windows.items()}
    for item in _visual(doc):
        number = item.get("sentence_number")
        if number not in cursors:
            continue
        item["start"] = round(cursors[number], 3)
        cursors[number] += float(item.get("duration") or 0)
        item["end"] = round(cursors[number], 3)
        item.pop("_cursor", None)
    if windows:
        doc["duration_seconds"] = round(
            max(w["end"] for w in windows.values()), 3
        )


def _check_window_conservation(doc):
    """Every window's items must sum to the window duration —
    the invariant every operation preserves."""
    problems = []
    windows = _windows(doc)
    for number, window in windows.items():
        total = sum(
            float(i.get("duration") or 0)
            for i in _window_items(doc, number)
        )
        expected = float(window["end"]) - float(window["start"])
        if abs(total - expected) > TIME_TOLERANCE:
            problems.append(
                f"sentence {number}: items sum to {total:.3f}s but "
                f"the narration window is {expected:.3f}s"
            )
    return problems


def _find_item(doc, item_id) -> dict:
    for item in _visual(doc):
        if item.get("item_id") == item_id:
            return item
    raise EditorError(422, "item_not_found", f"No timeline item {item_id}.")


def _gap_item(sentence_number, scene_number, duration, reason, extra=None):
    item = {
        "item_id": _item_id(),
        "kind": "gap",
        "sentence_number": sentence_number,
        "scene_number": scene_number,
        "start": 0.0,
        "end": 0.0,
        "duration": round(float(duration), 3),
        "assignment": "gap",
        "transition": "cut",
        "asset": None,
        "graphic": None,
        "classification": "gap",
        "flags": ["gap", reason],
    }
    if extra:
        item.update(extra)
    return item


def _free_time(doc, sentence_number, amount, after_item_id=None):
    """Return `amount` seconds to the window as explicit gap time:
    extend the first gap at/after the position, else insert a new
    gap item there."""
    if amount <= 0.001:
        return
    items = _visual(doc)
    window = _window_items(doc, sentence_number)
    start_at = 0
    if after_item_id:
        for index, item in enumerate(window):
            if item.get("item_id") == after_item_id:
                start_at = index + 1
                break
    for item in window[start_at:]:
        if item.get("kind") == "gap":
            item["duration"] = round(float(item["duration"]) + amount, 3)
            return
    gap = _gap_item(
        sentence_number,
        window[0]["scene_number"] if window else 1,
        amount,
        "freed_time",
    )
    if after_item_id:
        for index, item in enumerate(items):
            if item.get("item_id") == after_item_id:
                items.insert(index + 1, gap)
                return
    items.append(gap)


def _consume_time(doc, sentence_number, amount) -> bool:
    """Take `amount` seconds from the window's gap items (earliest
    first). Returns False — mutating nothing — when the window does
    not hold enough explicit gap time."""
    if amount <= 0.001:
        return True
    gaps = [
        i for i in _window_items(doc, sentence_number)
        if i.get("kind") == "gap"
    ]
    available = sum(float(g["duration"]) for g in gaps)
    if available + TIME_TOLERANCE < amount:
        return False
    remaining = amount
    for gap in gaps:
        if remaining <= 0.001:
            break
        take = min(float(gap["duration"]), remaining)
        gap["duration"] = round(float(gap["duration"]) - take, 3)
        remaining -= take
    doc["tracks"]["visual"] = [
        i for i in _visual(doc)
        if not (i.get("kind") == "gap" and float(i.get("duration") or 0) <= 0.001)
    ]
    return True


# ---------------------------------------------------------------------
# Uploads (Part 2.4: probed, user_supplied, rights unknown)
# ---------------------------------------------------------------------

UPLOADS_DIRNAME = "uploads"
UPLOADS_INDEX = "index.json"


def upload_max_bytes() -> int:
    try:
        return max(
            1, int(os.environ.get("CINEMORA_UPLOAD_MAX_BYTES", ""))
        )
    except ValueError:
        return UPLOAD_MAX_BYTES_DEFAULT
    except TypeError:
        return UPLOAD_MAX_BYTES_DEFAULT


def _uploads_index(project_dir) -> list[dict]:
    data = _read_json(project_dir / UPLOADS_DIRNAME / UPLOADS_INDEX)
    if isinstance(data, dict) and isinstance(data.get("uploads"), list):
        return data["uploads"]
    return []


def _write_uploads_index(project_dir, uploads):
    _atomic_write_json(
        project_dir / UPLOADS_DIRNAME / UPLOADS_INDEX,
        {"version": 1, "uploads": uploads},
    )


def get_upload(project_id, upload_id) -> dict | None:
    for entry in _uploads_index(_project_dir(project_id)):
        if entry.get("upload_id") == upload_id:
            return entry
    return None


def _ffprobe_duration(path) -> float | None:
    """Format duration via ffprobe (renderer.probe_media is
    video-stream oriented and returns None for audio-only files)."""
    import subprocess

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        value = (json.loads(result.stdout).get("format") or {}).get("duration")
        return round(float(value), 3) if value else None
    except Exception:
        return None


def _probe_any(path) -> tuple[str, dict] | None:
    """(type, probe) for a media file: video / image / audio."""
    suffix = Path(path).suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        probe = media_probe.probe_video(path)
        return ("video", probe) if probe else None
    if suffix in IMAGE_SUFFIXES:
        probe = media_probe.probe_image(path)
        return ("image", probe) if probe else None
    if suffix in AUDIO_SUFFIXES:
        duration = _ffprobe_duration(path)
        if duration:
            return "audio", {"duration_seconds": duration}
        return None
    probe = media_probe.probe_video(path)
    if probe:
        return "video", probe
    probe = media_probe.probe_image(path)
    if probe:
        return "image", probe
    return None


def _latest_completed_run(project_id):
    """The project's most recent completed run dict, or None."""
    for run in run_ledger.list_runs_for(project_id):
        if run.get("status") == "completed":
            return run
    return None


def _awaiting_approval_run(project_id):
    """The project's run parked at the Stage 4 plan-approval gate
    (an analysis phase ran; a plan exists), or None."""
    for run in run_ledger.list_runs_for(project_id):
        if run.get("status") == "awaiting_approval":
            return run
    return None


def store_upload(project_id, filename, fileobj) -> dict:
    """Store one uploaded file under the project's uploads/ dir.

    The file is streamed to disk with the size cap enforced
    (CINEMORA_UPLOAD_MAX_BYTES, default 512 MiB), probed with
    ffprobe (unprobeable -> 422 and the file is removed), classified
    user_supplied with licence "unknown" — never invented — and
    recorded in the project's upload index (uploads/index.json),
    the same index editor operations resolve {upload_id} against.

    Two storage contexts:

    - Editor context (a revision exists, or a completed run can
      seed revision 0): the upload is also recorded as a ledger
      decision tagged with the project's current editor revision
      (revision 0 is created first when none exists).
    - Pre-run plan-review context (Brief Stage 5b): the project's
      run is parked in "awaiting_approval" — a plan exists but no
      production has completed, so there is no timeline to import
      and revision 0 must NOT be created. The upload is stored and
      indexed exactly as above (marked "pre_run": true) so the
      plan-review replace flow can reference its file_path and,
      once the production completes and revision 0 exists, editor
      operations can consume it by upload_id from the same index.
      No ledger decision is recorded: record_editor_decision tags
      a revision, and an untagged row on the awaiting run would
      surface in the original run's certificate — the D3 isolation
      forbids exactly that. The plan decision that consumes the
      upload is itself recorded in plan_decisions.json.

    A project with neither context has nothing to attach an upload
    to and is refused with 404 no_production_context.
    """
    project_dir = _project_dir(project_id)
    baseline_view = None
    baseline = latest_revision(project_id)
    if baseline is not None:
        baseline_view = baseline[0]
    elif _latest_completed_run(project_id) is not None:
        baseline_view = create_revision_zero(project_id)[0]
    elif _awaiting_approval_run(project_id) is None:
        raise EditorError(
            404, "no_production_context",
            "This project has no plan awaiting approval and no "
            "completed production run, so there is nothing to "
            "attach an upload to.",
        )
    # Remaining case: pre-run plan-review context (baseline_view
    # stays None) — store without creating revision 0 and without
    # a revision-tagged ledger decision.
    pre_run = baseline_view is None

    safe_name = Path(str(filename or "upload")).name.replace(" ", "_")
    upload_id = _new_id()
    uploads_dir = project_dir / UPLOADS_DIRNAME
    uploads_dir.mkdir(parents=True, exist_ok=True)
    dest = uploads_dir / f"{upload_id}_{safe_name}"

    cap = upload_max_bytes()
    size = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = fileobj.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > cap:
                    raise EditorError(
                        413, "upload_too_large",
                        f"Upload exceeds the {cap}-byte cap "
                        "(CINEMORA_UPLOAD_MAX_BYTES).",
                    )
                out.write(chunk)
    except BaseException:
        try:
            dest.unlink()
        except OSError:
            pass
        raise

    probed = _probe_any(dest)
    if probed is None:
        try:
            dest.unlink()
        except OSError:
            pass
        raise EditorError(
            422, "upload_unprobeable",
            "The uploaded file could not be probed as video, image "
            "or audio; it was not stored.",
        )
    media_type, probe = probed
    asset = {
        "upload_id": upload_id,
        "file_path": str(dest),
        "file_name": dest.name,
        "type": media_type,
        "source": "user_supplied",
        "provider": "user_upload",
        "source_url": None,
        "title": Path(safe_name).stem.replace("_", " "),
        "clip_start": 0.0,
        "source_duration": probe.get("duration_seconds"),
        "decision_id": None,
        "evidence_class": None,
        "verification": None,
        "rights": {
            "license": "unknown",
            "license_url": "",
            "rights_status": "verify_before_publish",
            "creator": "",
            "source": "user_supplied",
            "retrieved_at": _now_iso(),
        },
        "user_supplied": True,
        "generated": False,
        "size_bytes": size,
    }
    if pre_run:
        asset["pre_run"] = True
    uploads = _uploads_index(project_dir)
    uploads.append(asset)
    _write_uploads_index(project_dir, uploads)
    if not pre_run:
        asset["decision_id"] = run_ledger.record_editor_decision(
            baseline_view["run_id"],
            project_id,
            revision_id=baseline_view["revision_id"],
            decision_type="user_supplied",
            asset=asset,
        )
    return asset


# ---------------------------------------------------------------------
# Asset resolution for operations
# ---------------------------------------------------------------------


def _download_url(url, dest_dir) -> Path:
    """Download a caller-supplied URL into dest_dir using the
    existing machinery: a direct fetch first (requests), falling
    back to yt-dlp through media_pipeline's TLS-safe base command
    for page URLs (YouTube etc.). The caller probes the result —
    an unprobeable download is rejected there, never trusted."""
    import subprocess

    import requests

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(url.split("?")[0]).suffix.lower()
    if suffix not in VIDEO_SUFFIXES | IMAGE_SUFFIXES | AUDIO_SUFFIXES:
        suffix = ".mp4"
    dest = dest_dir / f"download_{uuid.uuid4().hex[:12]}{suffix}"
    try:
        with requests.get(url, stream=True, timeout=120) as response:
            response.raise_for_status()
            with open(dest, "wb") as out:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        out.write(chunk)
    except Exception:
        try:
            dest.unlink()
        except OSError:
            pass
    if dest.is_file() and _probe_any(dest) is not None:
        return dest
    # Fall back to yt-dlp (page URLs, e.g. a YouTube watch URL).
    try:
        dest.unlink()
    except OSError:
        pass
    try:
        from services import media_pipeline

        base = list(media_pipeline._yt_dlp_base_command())
    except Exception:
        base = ["yt-dlp"]
    target = dest_dir / f"download_{uuid.uuid4().hex[:12]}.%(ext)s"
    try:
        subprocess.run(
            base + ["-o", str(target), "--no-playlist", url],
            capture_output=True, timeout=600, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    for candidate in sorted(dest_dir.glob("download_*")):
        if _probe_any(candidate) is not None:
            return candidate
    raise EditorError(
        422, "download_failed",
        f"Could not download a probeable media file from {url}.",
    )


def _asset_from_file(project_id, path, *, source, source_url=None,
                     title=None) -> dict:
    probed = _probe_any(path)
    if probed is None:
        raise EditorError(
            422, "asset_unprobeable",
            f"{path} could not be probed as media.",
        )
    media_type, probe = probed
    return {
        "file_path": str(path),
        "type": media_type,
        "source": source,
        "provider": source,
        "source_url": source_url,
        "title": title or Path(str(path)).stem.replace("_", " "),
        "clip_start": 0.0,
        "source_duration": probe.get("duration_seconds"),
        "decision_id": None,
        "evidence_class": None,
        "verification": None,
        "rights": {
            "license": "unknown",
            "license_url": "",
            "rights_status": "verify_before_publish",
            "creator": "",
            "source": source,
            "retrieved_at": _now_iso(),
        },
        "user_supplied": False,
        "generated": False,
    }


def _asset_from_library(project_id, decision_id) -> dict:
    """An asset from the project's own ledger (its recorded
    evidence travels verbatim — the asset keeps ITS verdict; the
    editor never re-verifies it for a new sentence)."""
    from models import AssetDecision

    db = _session()
    try:
        row = db.get(AssetDecision, str(decision_id))
        if row is None or row.project_id != str(project_id):
            raise EditorError(
                422, "library_asset_not_found",
                f"No ledger decision {decision_id} on this project.",
            )
        snapshot = {
            "content_hash": row.content_hash,
            "asset_key": row.asset_key,
            "source_url": row.source_url,
            "provider": row.provider,
            "evidence_class": row.evidence_class,
            "verification": row.verification,
            "rights": row.rights,
            "sentence_number": row.sentence_number,
            "interval_duration": row.interval_duration,
        }
    finally:
        db.close()
    path = None
    if snapshot["asset_key"] and Path(str(snapshot["asset_key"])).is_file():
        path = str(snapshot["asset_key"])
    if path is None and snapshot["content_hash"]:
        project_dir = _project_dir(project_id)
        candidates = set()
        for item in plan_document._timeline_items(project_dir):
            candidate = item.get("file_path") or item.get("path")
            if candidate:
                candidates.add(str(candidate))
        index = plan_document.build_media_index(project_id, project_dir)
        for entry in index.values():
            candidates.add(str(entry["path"]))
        cache: dict = {}
        for candidate in candidates:
            if run_ledger._file_sha256(candidate, cache) == snapshot["content_hash"]:
                path = candidate
                break
    if path is None:
        raise EditorError(
            422, "library_asset_file_missing",
            "The ledger decision's media file is not present in the "
            "project directory, so it cannot be added.",
        )
    probed = _probe_any(path)
    if probed is None:
        raise EditorError(
            422, "asset_unprobeable",
            "The library asset's file could not be probed.",
        )
    media_type, probe = probed
    return {
        "file_path": path,
        "type": media_type,
        "source": snapshot["provider"],
        "provider": snapshot["provider"],
        "source_url": snapshot["source_url"],
        "title": Path(path).stem.replace("_", " "),
        "clip_start": 0.0,
        "source_duration": probe.get("duration_seconds")
        or snapshot["interval_duration"],
        "decision_id": str(decision_id),
        "evidence_class": snapshot["evidence_class"],
        "verification": snapshot["verification"],
        "rights": snapshot["rights"],
        "user_supplied": False,
        "generated": False,
        "_library_sentence_number": snapshot["sentence_number"],
    }


def _resolve_asset_spec(project_id, spec, media_dir) -> dict:
    """Resolve an operation's asset spec ({file_path} | {upload_id}
    | {library_decision_id} | {source_url}) to an editor asset."""
    if not isinstance(spec, dict):
        raise EditorError(422, "asset_required", "An asset spec is required.")
    if spec.get("upload_id"):
        upload = get_upload(project_id, str(spec["upload_id"]))
        if upload is None:
            raise EditorError(
                422, "upload_not_found",
                f"No upload {spec['upload_id']} on this project.",
            )
        return copy.deepcopy(upload)
    if spec.get("library_decision_id"):
        return _asset_from_library(project_id, str(spec["library_decision_id"]))
    if spec.get("file_path"):
        path = Path(str(spec["file_path"]))
        if not path.is_file():
            raise EditorError(
                422, "asset_file_missing",
                f"Replacement file {path} does not exist on the server.",
            )
        return _asset_from_file(
            project_id, path,
            source=spec.get("source") or "local_file",
            source_url=spec.get("source_url"),
            title=spec.get("title"),
        )
    if spec.get("source_url"):
        downloaded = _download_url(str(spec["source_url"]), media_dir)
        return _asset_from_file(
            project_id, downloaded,
            source=spec.get("source") or "url_download",
            source_url=str(spec["source_url"]),
            title=spec.get("title"),
        )
    raise EditorError(
        422, "asset_required",
        "An asset spec needs file_path, upload_id, "
        "library_decision_id or source_url.",
    )


# ---------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------


class _OpContext:
    def __init__(self, project_id, parent_doc, new_revision_id):
        self.project_id = str(project_id)
        self.source_run_id = parent_doc["run_id"]
        self.new_revision_id = new_revision_id
        self.media_dir = (
            _project_dir(project_id) / "editor_media" / new_revision_id
        )
        self.ledger_actions: list = []


def _embedded_sentence(project_dir, sentence_number):
    """The analyzed sentence + bible from the production's story
    cache (the scenario source Stage 1's swap verification needs),
    or (None, None) when unreachable."""
    cache = _read_json(project_dir / "story_understanding_cache.json")
    if not isinstance(cache, dict):
        return None, None
    plans = cache.get("plans") or []
    sentence = None
    if sentence_number and 0 < sentence_number <= len(plans):
        sentence = plans[sentence_number - 1]
    return sentence, cache.get("bible")


def _op_remove_clip(doc, op, ctx):
    item = _find_item(doc, op.get("item_id"))
    if item["kind"] == "gap":
        raise EditorError(422, "cannot_remove_gap", "A gap item cannot be removed.")
    policy = op.get("gap_policy") or "leave_gap"
    if policy not in ("leave_gap", "close_gap"):
        raise EditorError(
            422, "invalid_gap_policy",
            "gap_policy must be 'leave_gap' or 'close_gap'.",
        )
    number = item["sentence_number"]
    freed = float(item["duration"])
    visual = _visual(doc)
    index = visual.index(item)
    predecessor_id = visual[index - 1]["item_id"] if index > 0 else None
    if policy == "leave_gap":
        visual[index] = _gap_item(
            number, item["scene_number"], freed, "removed_leave_gap",
            {"removed_item_id": item["item_id"]},
        )
        return f"Removed {item['item_id']}; explicit gap left ({freed:.2f}s)."
    # close_gap: the freed time is absorbed inside the same window —
    # an existing gap first, then a still (stills extend freely),
    # then a video sibling up to its probed source headroom; any
    # remainder becomes an explicit gap (never invented coverage).
    visual.pop(index)
    remaining = freed
    siblings = _window_items(doc, number)
    for sibling in siblings:
        if sibling["kind"] == "gap":
            sibling["duration"] = round(float(sibling["duration"]) + remaining, 3)
            remaining = 0.0
            break
    if remaining > TIME_TOLERANCE:
        for sibling in siblings:
            if sibling["kind"] != "gap" and (sibling.get("asset") or {}).get("type") == "image":
                sibling["duration"] = round(float(sibling["duration"]) + remaining, 3)
                remaining = 0.0
                break
    if remaining > TIME_TOLERANCE:
        for sibling in siblings:
            asset = sibling.get("asset") or {}
            if sibling["kind"] == "gap" or asset.get("type") != "video":
                continue
            if asset.get("source_duration") is None and asset.get("file_path"):
                probed = _probe_any(asset["file_path"])
                if probed:
                    asset["source_duration"] = probed[1].get("duration_seconds")
            if asset.get("source_duration") is None:
                continue
            headroom = (
                float(asset["source_duration"])
                - float(asset.get("clip_start") or 0)
                - float(sibling["duration"])
            )
            take = min(max(headroom, 0.0), remaining)
            if take > 0:
                sibling["duration"] = round(float(sibling["duration"]) + take, 3)
                remaining -= take
            if remaining <= TIME_TOLERANCE:
                break
    if remaining > TIME_TOLERANCE:
        _free_time(doc, number, remaining, after_item_id=predecessor_id)
        return (
            f"Removed {item['item_id']} (close_gap): {freed - remaining:.2f}s "
            f"absorbed by siblings, {remaining:.2f}s left as an explicit gap "
            "(no sibling could cover it)."
        )
    return f"Removed {item['item_id']} (close_gap); window absorbed {freed:.2f}s."


def _op_replace_clip(doc, op, ctx):
    from services import media_pipeline

    item = _find_item(doc, op.get("item_id"))
    if item["kind"] == "gap":
        raise EditorError(422, "cannot_replace_gap", "A gap item cannot be replaced.")
    spec = op.get("replacement") or op.get("asset")
    asset = _resolve_asset_spec(ctx.project_id, spec, ctx.media_dir)
    swap_path = asset["file_path"]
    original = item.get("asset") or {}
    pipeline_original = {
        "file_path": original.get("file_path"),
        "path": original.get("file_path"),
        "type": original.get("type"),
        "visual_type": original.get("type"),
        "candidate_id": "",
        "source": original.get("source"),
        "source_url": original.get("source_url"),
        "title": original.get("title"),
    }
    bucket = "videos" if asset["type"] == "video" else "images"
    decision = {
        "source": asset.get("source"),
        "source_url": asset.get("source_url") or "",
        "title": asset.get("title"),
        "creator": (asset.get("rights") or {}).get("creator") or "",
    }
    sentence_obj, bible = _embedded_sentence(
        _project_dir(ctx.project_id), item.get("sentence_number")
    )
    sentence_result = {"sentence_number": item.get("sentence_number"),
                       "sentence": sentence_obj}
    refreshed, _accepted = media_pipeline._refresh_swapped_asset(
        pipeline_original, bucket, swap_path, decision, sentence_result, bible
    )
    if refreshed is None:
        raise EditorError(
            422, "swap_failed_probe",
            "The replacement could not be probed; the original clip "
            "is untouched and no new revision was created.",
        )
    new_asset = {
        "file_path": refreshed.get("file_path"),
        "type": asset["type"],
        "source": refreshed.get("source"),
        "provider": refreshed.get("source"),
        "source_url": refreshed.get("source_url"),
        "title": asset.get("title"),
        "clip_start": 0.0,
        "source_duration": refreshed.get("duration_seconds"),
        "decision_id": None,
        "evidence_class": refreshed.get("segment_evidence"),
        "verification": refreshed.get("verification"),
        "rights": run_ledger._asset_rights(refreshed),
        "user_supplied": bool(asset.get("user_supplied")),
        "generated": False,
        "swapped_from": {
            "file_path": original.get("file_path"),
            "decision_id": original.get("decision_id"),
        },
    }
    item["asset"] = new_asset
    if item["kind"] == "graphic":
        item["kind"] = "clip"
        item["graphic"] = None
    item["classification"] = classify_asset(new_asset, item["kind"])
    item.setdefault("flags", []).append("replaced")

    def _ledger(item=item, original=pipeline_original, refreshed=refreshed):
        item["asset"]["decision_id"] = run_ledger.record_swap_decisions(
            ctx.source_run_id,
            ctx.project_id,
            scene_number=item.get("scene_number"),
            sentence_number=item.get("sentence_number"),
            original=original,
            refreshed=refreshed,
            revision_id=ctx.new_revision_id,
        )

    ctx.ledger_actions.append(_ledger)
    verdict = (refreshed.get("verification") or {}).get("accepted")
    return (
        f"Replaced {item['item_id']} with {Path(swap_path).name}; fresh "
        f"probe + verdict recorded (accepted={verdict}); rights re-stamped "
        "unknown/verify_before_publish; nothing inherited."
    )


def _op_add_clip(doc, op, ctx):
    spec = op.get("asset") or op.get("replacement")
    asset = _resolve_asset_spec(ctx.project_id, spec, ctx.media_dir)
    if asset["type"] == "audio":
        raise EditorError(
            422, "audio_not_visual",
            "Audio assets belong to the music track, not the visual track.",
        )
    number = op.get("sentence_number")
    after_item_id = op.get("after_item_id")
    if number is None and after_item_id:
        number = _find_item(doc, after_item_id)["sentence_number"]
    windows = _windows(doc)
    if number not in windows:
        raise EditorError(
            422, "sentence_not_found",
            f"No narration sentence {number} to add the clip to.",
        )
    gaps = [
        i for i in _window_items(doc, number) if i.get("kind") == "gap"
    ]
    available = sum(float(g["duration"]) for g in gaps)
    duration = op.get("duration")
    if duration is None:
        duration = min(float(asset.get("source_duration") or 0), available)
    duration = float(duration)
    if duration < MIN_ITEM_DURATION:
        raise EditorError(
            422, "duration_too_small",
            f"Clip duration must be at least {MIN_ITEM_DURATION}s.",
        )
    if duration > available + TIME_TOLERANCE:
        raise EditorError(
            422, "insufficient_gap_time",
            f"Sentence {number} holds {available:.2f}s of explicit gap "
            f"time; adding a {duration:.2f}s clip needs gap time to be "
            "freed first (remove or trim a clip in that sentence). "
            "The editor never compresses or invents coverage.",
        )
    if not _consume_time(doc, number, duration):
        raise EditorError(422, "insufficient_gap_time", "Gap consumption failed.")
    library_sentence = asset.pop("_library_sentence_number", None)
    item = {
        "item_id": _item_id(),
        "kind": "graphic" if asset.get("generated") else "clip",
        "sentence_number": number,
        "scene_number": windows[number]["scene_number"],
        "start": 0.0,
        "end": 0.0,
        "duration": round(duration, 3),
        "assignment": "editor_added",
        "transition": "cut",
        "asset": asset,
        "graphic": None,
        "classification": classify_asset(asset, "clip"),
        "flags": ["editor_added"],
    }
    if library_sentence is not None and library_sentence != number:
        item["flags"].append("not_verified_for_this_sentence")
    visual = _visual(doc)
    if after_item_id:
        for index, existing in enumerate(visual):
            if existing.get("item_id") == after_item_id:
                visual.insert(index + 1, item)
                break
        else:
            raise EditorError(422, "item_not_found", f"No timeline item {after_item_id}.")
    else:
        window = _window_items(doc, number)
        if window:
            last = window[-1]
            visual.insert(visual.index(last) + 1, item)
        else:
            visual.append(item)

    def _ledger(item=item, asset=asset):
        item["asset"]["decision_id"] = run_ledger.record_editor_decision(
            ctx.source_run_id,
            ctx.project_id,
            revision_id=ctx.new_revision_id,
            decision_type=(
                "user_supplied" if asset.get("user_supplied") else "selected"
            ),
            asset=asset,
            scene_number=item.get("scene_number"),
            sentence_number=item.get("sentence_number"),
            previous_decision_id=asset.get("decision_id"),
        ) or item["asset"].get("decision_id")

    ctx.ledger_actions.append(_ledger)
    return f"Added clip {item['item_id']} to sentence {number} ({duration:.2f}s)."


def _duration_delta(doc, item, new_duration):
    """Apply a duration change, conserving the window: freed time
    becomes an explicit gap after the item; extra time is consumed
    from the window's gaps (422 when there is not enough)."""
    old = float(item["duration"])
    delta = float(new_duration) - old
    if delta > TIME_TOLERANCE:
        if not _consume_time(doc, item["sentence_number"], delta):
            raise EditorError(
                422, "insufficient_gap_time",
                f"Extending by {delta:.2f}s needs explicit gap time in "
                f"sentence {item['sentence_number']}; free some first "
                "(remove or trim a clip there).",
            )
    item["duration"] = round(float(new_duration), 3)
    if delta < -TIME_TOLERANCE:
        _free_time(doc, item["sentence_number"], -delta,
                   after_item_id=item["item_id"])


def _op_trim_clip(doc, op, ctx):
    item = _find_item(doc, op.get("item_id"))
    if item["kind"] == "gap":
        raise EditorError(422, "cannot_trim_gap", "A gap item cannot be trimmed.")
    asset = item.get("asset") or {}
    new_start = op.get("clip_start", asset.get("clip_start") or 0)
    new_duration = float(op.get("duration", item["duration"]))
    if new_duration < MIN_ITEM_DURATION:
        raise EditorError(
            422, "duration_too_small",
            f"Duration must be at least {MIN_ITEM_DURATION}s.",
        )
    if asset.get("type") == "image":
        if new_start not in (0, 0.0, None) and float(new_start) != float(asset.get("clip_start") or 0):
            raise EditorError(
                422, "image_has_no_in_point",
                "Still images have no source in-point to trim.",
            )
        new_start = asset.get("clip_start") or 0
    else:
        if asset.get("source_duration") is None and asset.get("file_path"):
            probed = _probe_any(asset["file_path"])
            if probed:
                asset["source_duration"] = probed[1].get("duration_seconds")
        if asset.get("source_duration") is not None:
            if float(new_start) < 0 or (
                float(new_start) + new_duration
                > float(asset["source_duration"]) + TIME_TOLERANCE
            ):
                raise EditorError(
                    422, "trim_out_of_bounds",
                    f"Trim [{new_start}, {float(new_start) + new_duration}] "
                    f"exceeds the asset's available source interval "
                    f"[0, {asset['source_duration']}].",
                )
    _duration_delta(doc, item, new_duration)
    asset["clip_start"] = float(new_start)
    return (
        f"Trimmed {item['item_id']}: in-point {float(new_start):.2f}s, "
        f"duration {new_duration:.2f}s."
    )


def _op_move_clip(doc, op, ctx):
    item = _find_item(doc, op.get("item_id"))
    if item["kind"] == "gap":
        raise EditorError(422, "cannot_move_gap", "Gap items stay where their time is.")
    visual = _visual(doc)
    target_number = op.get("to_sentence_number") or item["sentence_number"]
    after_item_id = op.get("after_item_id")
    if after_item_id:
        anchor = _find_item(doc, after_item_id)
        if anchor["sentence_number"] != target_number:
            raise EditorError(
                422, "anchor_in_other_sentence",
                "after_item_id belongs to a different sentence.",
            )
    source_number = item["sentence_number"]
    index = visual.index(item)
    predecessor_id = visual[index - 1]["item_id"] if index > 0 else None
    if target_number == source_number:
        visual.pop(index)
        if after_item_id:
            for position, existing in enumerate(visual):
                if existing.get("item_id") == after_item_id:
                    visual.insert(position + 1, item)
                    break
            else:
                visual.append(item)
        else:
            window = _window_items(doc, source_number)
            visual.insert(visual.index(window[-1]) + 1, item) if window else visual.append(item)
        return f"Reordered {item['item_id']} within sentence {source_number}."
    duration = float(item["duration"])
    if not _consume_time(doc, target_number, duration):
        raise EditorError(
            422, "insufficient_gap_time",
            f"Sentence {target_number} holds less than {duration:.2f}s of "
            "explicit gap time; moving a clip across sentences consumes "
            "gap time at the target and frees it at the source.",
        )
    visual.pop(visual.index(item))
    _free_time(doc, source_number, duration, after_item_id=predecessor_id)
    item["sentence_number"] = target_number
    item["scene_number"] = _windows(doc)[target_number]["scene_number"]
    item.setdefault("flags", []).append("moved")
    if after_item_id:
        for position, existing in enumerate(visual):
            if existing.get("item_id") == after_item_id:
                visual.insert(position + 1, item)
                break
        else:
            visual.append(item)
    else:
        window = _window_items(doc, target_number)
        if window:
            visual.insert(visual.index(window[-1]) + 1, item)
        else:
            visual.append(item)
    return f"Moved {item['item_id']} to sentence {target_number}."


def _op_split_clip(doc, op, ctx):
    item = _find_item(doc, op.get("item_id"))
    if item["kind"] == "gap":
        raise EditorError(422, "cannot_split_gap", "A gap item cannot be split.")
    offset = float(op.get("at_offset") or 0)
    duration = float(item["duration"])
    if offset < MIN_ITEM_DURATION or duration - offset < MIN_ITEM_DURATION:
        raise EditorError(
            422, "split_out_of_bounds",
            f"Split offset must leave at least {MIN_ITEM_DURATION}s on "
            f"both sides of a {duration:.2f}s clip.",
        )
    second = copy.deepcopy(item)
    second["item_id"] = _item_id()
    second["duration"] = round(duration - offset, 3)
    asset = second.get("asset") or {}
    if asset.get("type") == "video":
        asset["clip_start"] = round(
            float(asset.get("clip_start") or 0) + offset, 3
        )
    item["duration"] = round(offset, 3)
    group = item["item_id"]
    item.setdefault("flags", []).append("split_part_1")
    second.setdefault("flags", []).append("split_part_2")
    item["split_group"] = group
    second["split_group"] = group
    visual = _visual(doc)
    visual.insert(visual.index(item) + 1, second)
    return f"Split {group} at {offset:.2f}s into two items (same asset, same evidence)."


def _op_set_graphic_duration(doc, op, ctx):
    item = _find_item(doc, op.get("item_id"))
    if item["kind"] != "graphic":
        raise EditorError(
            422, "not_a_graphic", "set_graphic_duration applies to graphics only."
        )
    new_duration = float(op.get("duration") or 0)
    if new_duration < MIN_ITEM_DURATION:
        raise EditorError(422, "duration_too_small", "Duration too small.")
    _duration_delta(doc, item, new_duration)
    return f"Graphic {item['item_id']} duration set to {new_duration:.2f}s."


def _discard_regen_tmp(tmp_path):
    try:
        Path(tmp_path).unlink(missing_ok=True)
    except OSError:
        pass


def _op_regenerate_graphic(doc, op, ctx):
    item = _find_item(doc, op.get("item_id"))
    if item["kind"] != "graphic":
        raise EditorError(422, "not_a_graphic", "Only graphics can be regenerated.")
    graphic = item.get("graphic") or {}
    recipe = graphic.get("recipe")
    template = graphic.get("template")
    inputs = graphic.get("inputs")

    # Phase 2 (MG-03): production graphics carry a complete recorded
    # recipe (template + full inputs for engine graphics; the exact
    # render inputs for the native families). A graphic explicitly
    # marked non-regenerable refuses with its recorded reason —
    # never with a false "inputs not recorded" claim.
    if isinstance(recipe, dict) and recipe.get("regenerable") is False:
        raise EditorError(
            422, "graphic_not_regenerable",
            "This graphic was recorded as non-regenerable: "
            + str(recipe.get("non_regenerable_reason") or "no reason recorded")
            + ". Replace it or leave it as rendered.",
        )
    if not isinstance(recipe, dict) and (not template or inputs is None):
        raise EditorError(
            422, "graphic_inputs_not_recorded",
            "This graphic's template inputs were not recorded by the "
            "production, so it cannot be regenerated without inventing "
            "data. Replace it or leave it as rendered.",
        )

    from services import graphics_pipeline

    ctx.media_dir.mkdir(parents=True, exist_ok=True)
    out_path = ctx.media_dir / f"graphic_{item['item_id']}.mp4"
    tmp_path = ctx.media_dir / f"graphic_{item['item_id']}.regenerating.mp4"
    duration = float(item["duration"])
    label = (
        (recipe or {}).get("template")
        or template
        or (recipe or {}).get("family")
        or "graphic"
    )

    # Transactional regeneration: render to a temporary file,
    # validate it under the Phase 1 contract, and promote it over
    # the previous asset ONLY on success. Any failure — refusal,
    # exception (MG-11), or invalid output — leaves the item and
    # its previous valid asset completely untouched.
    try:
        if isinstance(recipe, dict):
            rendered, metadata = (
                graphics_pipeline.render_graphic_from_recipe(
                    recipe, tmp_path, duration=duration
                )
            )
        else:
            from services.motion_engine import templates as engine

            template_fn = engine.TEMPLATES.get(template)
            if template_fn is None:
                raise EditorError(
                    422, "unknown_template", f"No template {template}."
                )
            rendered, metadata = template_fn(
                copy.deepcopy(inputs),
                graphic.get("style") or "editorial",
                tmp_path,
                duration=duration,
                sentence_index=None,
                scene_number=item.get("scene_number"),
            )
        if not rendered:
            reason = (metadata or {}).get("reason") or "template_refused"
            raise EditorError(
                422, "graphic_render_failed",
                f"The {label} template refused the recorded inputs: {reason}.",
            )
        verdict = graphics_pipeline.validate_graphic_output(
            tmp_path, expected_duration=duration
        )
        if not verdict["passed"]:
            # Server-side diagnostics keep the precise failure; the
            # client receives the structured refusal.
            print(
                f"REGENERATE GRAPHIC VALIDATION FAILED "
                f"({item['item_id']}): {verdict['detail']}"
            )
            raise EditorError(
                422, "graphic_render_failed",
                "The regenerated graphic failed validation and was "
                "not promoted; the previous graphic is unchanged.",
            )
    except EditorError:
        _discard_regen_tmp(tmp_path)
        raise
    except Exception as error:
        # MG-11: a template/render exception maps to the structured
        # 422 contract — it must never escape as an unhandled
        # server error. The original error stays in server-side
        # diagnostics; no internals are exposed to the client.
        print(f"REGENERATE GRAPHIC FAILED ({item['item_id']}): {error!r}")
        _discard_regen_tmp(tmp_path)
        raise EditorError(
            422, "graphic_render_failed",
            "The graphic could not be regenerated from its recorded "
            "inputs; the previous graphic is unchanged.",
        )
    os.replace(tmp_path, out_path)

    # The recorded source names the family that actually rendered
    # the regeneration (engine vs native pipeline), not a blanket
    # engine label.
    _regen_source = (
        "cinemora_motion_engine"
        if (recipe or {}).get("family") == "motion_engine"
        or (not isinstance(recipe, dict) and template)
        else "cinemora_motion_graphics"
    )
    new_asset = {
        "file_path": str(out_path),
        "type": "video",
        "source": _regen_source,
        "provider": _regen_source,
        "source_url": None,
        "title": (item.get("asset") or {}).get("title"),
        "clip_start": 0.0,
        "source_duration": duration,
        "decision_id": None,
        "evidence_class": None,
        "verification": None,
        "rights": None,
        "user_supplied": False,
        "generated": True,
    }
    item["asset"] = new_asset
    item["graphic"] = {**graphic, "engine_metadata": metadata}
    item["classification"] = "generated_graphic"
    item.setdefault("flags", []).append("regenerated")

    def _ledger(item=item, asset=new_asset):
        item["asset"]["decision_id"] = run_ledger.record_editor_decision(
            ctx.source_run_id,
            ctx.project_id,
            revision_id=ctx.new_revision_id,
            decision_type="generated",
            asset=asset,
            scene_number=item.get("scene_number"),
            sentence_number=item.get("sentence_number"),
        )

    ctx.ledger_actions.append(_ledger)
    return f"Regenerated graphic {item['item_id']} from its recorded inputs ({label})."


def _op_edit_caption(doc, op, ctx):
    captions = doc["tracks"]["captions"]
    segments = captions.get("segments") or []
    index = op.get("segment_index")
    if not isinstance(index, int) or not (0 <= index < len(segments)):
        raise EditorError(
            422, "segment_not_found", f"No caption segment {index}."
        )
    segment = segments[index]
    text_changed = "text" in op and op["text"] is not None
    if text_changed:
        if not str(op["text"]).strip():
            raise EditorError(422, "caption_text_empty", "Caption text cannot be empty.")
        segment["text"] = str(op["text"])
    new_start = float(op.get("start", segment.get("start") or 0))
    new_end = float(op.get("end", segment.get("end") or 0))
    if new_end <= new_start:
        raise EditorError(422, "caption_timing_invalid", "Caption end must follow start.")
    timing_changed = (
        abs(new_start - float(segment.get("start") or 0)) > 0.001
        or abs(new_end - float(segment.get("end") or 0)) > 0.001
    )
    if timing_changed:
        old_start = float(segment.get("start") or 0)
        old_end = float(segment.get("end") or 0)
        shift = new_start - old_start
        same_duration = abs((new_end - new_start) - (old_end - old_start)) <= 0.01
        segment["start"] = round(new_start, 3)
        segment["end"] = round(new_end, 3)
        words = segment.get("words")
        if words and same_duration:
            for word in words:
                word["start"] = round(float(word.get("start") or 0) + shift, 3)
                word["end"] = round(float(word.get("end") or 0) + shift, 3)
        elif words:
            # Word timings no longer describe the segment; the ASS
            # writer falls back to segment-level events (Stage 3I).
            segment.pop("words", None)
    if text_changed:
        # Edited words invalidate the recorded word timings.
        segment.pop("words", None)
        segment["edited"] = True
    return f"Caption segment {index} edited."


def _op_regenerate_narration(doc, op, ctx):
    number = op.get("sentence_number")
    windows = _windows(doc)
    if number not in windows:
        raise EditorError(422, "sentence_not_found", f"No sentence {number}.")
    sentence = windows[number]
    if op.get("text"):
        sentence["text"] = str(op["text"])
    narration = doc["tracks"]["narration"]
    dirty = narration.setdefault("dirty_sentences", [])
    if number not in dirty:
        dirty.append(number)
    if op.get("voice_id"):
        narration["voice_id"] = str(op["voice_id"])
    captions = doc["tracks"]["captions"]
    captions["stale"] = True
    captions["stale_reason"] = (
        f"narration sentence {number} marked for regeneration; "
        "captions rebuild from the new narration at render time"
    )
    return (
        f"Narration sentence {number} marked for regeneration; dependent "
        "captions + timings flagged dirty."
    )


def _op_set_music(doc, op, ctx):
    music = doc["tracks"]["music"]
    if "music_file" in op and op["music_file"] is None and not op.get("upload_id"):
        music["file"] = None
        music["source"] = None
        # Stage H (M1): removal is an explicit DISABLE — renders
        # honour it even when music files exist in the project.
        music["state"] = "disabled"
        music["license"] = "unknown"
        return "Music removed."
    if op.get("upload_id"):
        upload = get_upload(ctx.project_id, str(op["upload_id"]))
        if upload is None:
            raise EditorError(422, "upload_not_found", f"No upload {op['upload_id']}.")
        if upload["type"] != "audio":
            raise EditorError(422, "upload_not_audio", "The upload is not an audio file.")
        music["file"] = upload["file_path"]
        music["source"] = "user_supplied"
        music["state"] = "applied"
        music["license"] = "unknown"
        return f"Music set to upload {op['upload_id']}."
    path = Path(str(op.get("music_file") or ""))
    if not path.is_absolute():
        path = _project_dir(ctx.project_id) / path
    if not path.is_file():
        raise EditorError(422, "music_file_missing", f"Music file {path} does not exist.")
    if _probe_any(path) is None or _probe_any(path)[0] != "audio":
        raise EditorError(422, "music_unprobeable", "The music file could not be probed as audio.")
    music["file"] = str(path)
    music["source"] = "project_file"
    music["state"] = "applied"
    music["license"] = "unknown"
    return f"Music set to {path.name}."


def _op_set_volumes(doc, op, ctx):
    for key in ("narration", "music"):
        if key in op and op[key] is not None:
            value = float(op[key])
            if not (0.0 <= value <= 2.0):
                raise EditorError(
                    422, "volume_out_of_range",
                    f"{key} volume must be between 0.0 and 2.0.",
                )
            doc["volumes"][key] = value
    return f"Volumes: narration={doc['volumes']['narration']}, music={doc['volumes']['music']}."


_OP_HANDLERS = {
    "remove_clip": _op_remove_clip,
    "replace_clip": _op_replace_clip,
    "add_clip": _op_add_clip,
    "trim_clip": _op_trim_clip,
    "move_clip": _op_move_clip,
    "split_clip": _op_split_clip,
    "set_graphic_duration": _op_set_graphic_duration,
    "regenerate_graphic": _op_regenerate_graphic,
    "edit_caption": _op_edit_caption,
    "regenerate_narration_sentence": _op_regenerate_narration,
    "set_music": _op_set_music,
    "set_volumes": _op_set_volumes,
}


def apply_operations(project_id, revision_id, operations, actor="operator"):
    """Validate + apply a batch of operations ATOMICALLY: the batch
    runs against a deep copy; any invalid operation rejects the
    whole batch (EditorError 422) and nothing is persisted — no
    document, no DB row, no ledger rows (ledger appends are deferred
    until every operation has validated). On success the batch
    becomes the next revision node in the chain."""
    if not isinstance(operations, list) or not operations:
        raise EditorError(
            422, "operations_required",
            "operations must be a non-empty list.",
        )
    for operation in operations:
        if not isinstance(operation, dict) or not operation.get("type"):
            raise EditorError(
                422, "invalid_operation",
                "Every operation needs a type.",
            )
        if operation["type"] not in _OP_HANDLERS:
            raise EditorError(
                422, "unknown_operation",
                f"Unknown operation type {operation['type']}.",
            )
    parent_view, parent_doc = get_revision(project_id, revision_id)
    db = _session()
    try:
        parent_row = _get_row(db, project_id, revision_id)
        run_id = parent_row.run_id
        parent_number = parent_row.revision_number
        parent_id = parent_row.id
    finally:
        db.close()

    new_doc = copy.deepcopy(parent_doc)
    new_revision_id = _new_id()
    ctx = _OpContext(project_id, parent_doc, new_revision_id)
    entries = []
    try:
        for operation in operations:
            handler = _OP_HANDLERS[operation["type"]]
            summary = handler(new_doc, operation, ctx)
            entries.append(
                {
                    "op_id": _new_id(),
                    "type": operation["type"],
                    "actor": actor,
                    "at": _now_iso(),
                    "params": {
                        key: value
                        for key, value in operation.items()
                        if key != "type"
                    },
                    "summary": summary,
                }
            )
    except EditorError:
        raise
    except Exception as error:
        raise EditorError(
            422, "operation_failed", f"Operation batch rejected: {error}"
        ) from error

    _retime(new_doc)
    problems = _check_window_conservation(new_doc)
    if problems:
        raise EditorError(
            422, "timeline_inconsistent",
            "The batch would break narration-window conservation: "
            + "; ".join(problems),
        )

    new_doc["revision_id"] = new_revision_id
    new_doc["parent_revision_id"] = parent_id
    new_doc["revision_number"] = parent_number + 1
    new_doc["status"] = "draft"
    new_doc["created_at"] = _now_iso()
    new_doc["created_from"] = "operations"
    new_doc["render_state"] = None
    new_doc["last_render_error"] = None
    new_doc["exports"] = []
    new_doc["op_log"] = list(parent_doc.get("op_log") or []) + entries

    # Every operation validated: only now do the deferred ledger
    # appends run (they may stamp decision ids onto the new items).
    for action in ctx.ledger_actions:
        action()
    view = _persist_new_revision(project_id, run_id, parent_row, new_doc)
    return view, new_doc


# ---------------------------------------------------------------------
# Render plan (Part 3: explicit dependency rules)
# ---------------------------------------------------------------------

DEPENDENCY_RULES = [
    "visual: items whose id/file/in-point/duration differ from the parent revision are re-rendered from their (already local) assets; unchanged items are reused at ASSET level — never re-downloaded, re-researched or re-verified. Segment-level rendered-clip reuse is NOT possible with the existing renderer (render_scene assembles clips positionally from source media on every pass; there is no content-keyed clip cache), so final assembly always runs.",
    "narration: rebuilt only when the revision marks narration sentences dirty (regenerate_narration_sentence) or the voice changed; per-sentence parts are reused by content hash (Stage 2D) so unchanged sentences are never re-synthesised.",
    "captions: rebuilt only when caption segments differ from the parent or narration is rebuilt; a narration rebuild replaces the segments (they are flagged stale on the document until then) — captions are never silently left out of sync.",
    "soundtrack: remixed only when the music file, the music volume or the narration volume changed, or narration was rebuilt.",
    "assembly: the final concat + narration mix + caption burn always runs (the renderer has no partial-assembly mode).",
    "retiming: when narration is rebuilt, sentence windows are re-derived from the new part durations and visual items are retimed proportionally within each window; the retimed timings are written back into the revision document with an op-log entry.",
]


def compute_render_plan(parent_doc, doc) -> dict:
    """The diff between a revision and its parent (or revision 0's
    production), expressed as exactly what must be redone."""
    parent_items = {}
    if parent_doc is not None:
        parent_items = {
            item["item_id"]: item for item in (parent_doc.get("tracks", {}).get("visual") or [])
        }
    current_items = {
        item["item_id"]: item for item in _visual(doc)
    }
    changed, reused, new_items = [], [], []
    for item_id, item in current_items.items():
        parent = parent_items.get(item_id)
        if parent is None:
            new_items.append(item_id)
            continue
        signature = (
            (item.get("asset") or {}).get("file_path"),
            (item.get("asset") or {}).get("clip_start"),
            item.get("duration"),
            item.get("kind"),
        )
        parent_signature = (
            (parent.get("asset") or {}).get("file_path"),
            (parent.get("asset") or {}).get("clip_start"),
            parent.get("duration"),
            parent.get("kind"),
        )
        (changed if signature != parent_signature else reused).append(item_id)
    removed = [item_id for item_id in parent_items if item_id not in current_items]
    gaps = [
        item["item_id"] for item in _visual(doc) if item.get("kind") == "gap"
    ]

    narration = doc["tracks"]["narration"]
    dirty = list(narration.get("dirty_sentences") or [])
    voice_changed = bool(
        parent_doc is not None
        and (parent_doc["tracks"]["narration"].get("voice_id")
             != narration.get("voice_id"))
    )
    narration_rebuild = bool(dirty) or voice_changed or parent_doc is None

    parent_captions = (
        (parent_doc or {}).get("tracks", {}).get("captions", {})
    )
    captions_differ = (
        parent_doc is not None
        and parent_captions.get("segments") != doc["tracks"]["captions"].get("segments")
    )
    captions_rebuild = bool(captions_differ or narration_rebuild)

    music = doc["tracks"]["music"]
    parent_music = (parent_doc or {}).get("tracks", {}).get("music", {})
    volumes_changed = bool(
        parent_doc is not None
        and (parent_doc.get("volumes") or {}) != (doc.get("volumes") or {})
    )
    soundtrack_rebuild = bool(
        narration_rebuild
        or volumes_changed
        or (parent_doc is not None and parent_music.get("file") != music.get("file"))
    )
    return {
        "visual": {
            "changed_item_ids": changed,
            "reused_item_ids": reused,
            "new_item_ids": new_items,
            "removed_item_ids": removed,
            "gap_item_ids": gaps,
            "reuse_level": "asset",
        },
        "narration_rebuild": narration_rebuild,
        "narration_dirty_sentences": dirty,
        "voice_changed": voice_changed,
        "captions_rebuild": captions_rebuild,
        "soundtrack_rebuild": soundtrack_rebuild,
        "assembly": True,
        "retiming": (
            "proportional_sentence_windows" if narration_rebuild else "none"
        ),
        "rules": DEPENDENCY_RULES,
    }


# ---------------------------------------------------------------------
# Rendering through the Stage 2 job lifecycle
# ---------------------------------------------------------------------


class _RenderCancelled(Exception):
    pass


def gap_items(doc) -> list[dict]:
    return [item for item in _visual(doc) if item.get("kind") == "gap"]


def request_render(project_id, revision_id, allow_gaps=False) -> dict:
    """Validate + submit a revision render. Unresolved gaps BLOCK
    the render by default; with allow_gaps=True the render proceeds
    and the export record names every gap it covered (the renderer
    extends the previous genuine visual across the gap — its own
    recorded holdover behaviour)."""
    view, doc = get_revision(project_id, revision_id)
    if view["status"] == "rendering":
        raise EditorError(
            409, "render_in_progress",
            "A render is already running for this revision.",
        )
    gaps = gap_items(doc)
    if gaps and not allow_gaps:
        raise EditorError(
            422, "unresolved_gaps",
            f"The revision has {len(gaps)} unresolved gap item(s); "
            "exports are blocked by default. Fill the gaps (add a "
            "clip) or re-render with allow_gaps=true to export with "
            "the gaps explicitly flagged.",
            details={
                "gap_item_ids": [gap["item_id"] for gap in gaps],
                "gap_sentences": sorted(
                    {gap["sentence_number"] for gap in gaps}
                ),
            },
        )
    parent_doc = None
    if doc.get("parent_revision_id"):
        try:
            _pv, parent_doc = get_revision(
                project_id, doc["parent_revision_id"]
            )
        except EditorError:
            parent_doc = None
    plan = compute_render_plan(parent_doc, doc)
    project = _load_project_row(project_id)
    script_text = (project or {}).get("script") or ""
    submitted = run_ledger.submit_revision_render(
        project_id, script_text, revision_id, view["run_id"]
    )
    if submitted is None:
        raise EditorError(
            503, "ledger_unavailable",
            "The run ledger is unavailable; the render was not submitted.",
        )
    run_id = submitted["run_id"]
    if submitted.get("deduplicated"):
        # An active render run for this revision already exists;
        # its job will do the work — do not queue a second one.
        return {
            "revision_id": revision_id,
            "run_id": run_id,
            "status": "rendering",
            "allow_gaps": bool(allow_gaps),
            "render_plan": plan,
            "deduplicated": True,
        }
    doc["render_state"] = {
        "run_id": run_id,
        "allow_gaps": bool(allow_gaps),
        "plan": plan,
        "requested_at": _now_iso(),
    }
    doc["last_render_error"] = None
    _update_doc_file(project_id, doc)
    _update_row_status(project_id, revision_id, "rendering")
    import jobs

    jobs.enqueue_revision_render(project_id, revision_id, run_id)
    return {
        "revision_id": revision_id,
        "run_id": run_id,
        "status": "rendering",
        "allow_gaps": bool(allow_gaps),
        "render_plan": plan,
    }


def _revision_asset_report(doc, work_dir) -> dict:
    """An asset-report-shaped dict for the final video auditor,
    computed from the revision document (the same keys the
    production auditor consumes — see final_video_auditor)."""
    visual = _visual(doc)
    covered, uncovered = [], []
    for number in sorted(_windows(doc)):
        items = [
            item for item in visual
            if item["sentence_number"] == number and item["kind"] != "gap"
        ]
        (covered if items else uncovered).append(number)
    rights_recorded = 0
    rights_unknown = 0
    for item in visual:
        if item["kind"] in ("gap", "graphic"):
            # Generated graphics carry no licence accounting (the
            # production report counts them under generated_graphics,
            # not accepted assets).
            continue
        rights = (item.get("asset") or {}).get("rights")
        if isinstance(rights, dict) and rights.get("license"):
            rights_recorded += 1
            if rights["license"] == "unknown":
                rights_unknown += 1
    holdover = 0
    timing_audit = _read_json(Path(work_dir) / "renderer_timing_audit.json")
    if isinstance(timing_audit, dict):
        holdover = int(timing_audit.get("missing_media_holdover_count") or 0)
    return {
        "coverage": {
            "total_sentences": len(covered) + len(uncovered),
            "covered_sentences": covered,
            "uncovered_sentences": uncovered,
            "complete": not uncovered,
        },
        "authenticity": {
            "substitutions": {"text_cards": 0, "placeholder_clips": 0},
        },
        "segment_retrieval": {},
        # The auditor subtracts generated_graphics itself, so the
        # total mirrors the production report: every non-gap item.
        "total_accepted_assets": sum(
            1 for item in visual if item["kind"] != "gap"
        ),
        "rights": {
            "recorded_licences": rights_recorded,
            "unknown_licences": rights_unknown,
        },
        "generated_graphics": sum(
            1 for item in visual if item["kind"] == "graphic"
        ),
        "timeline_items": len(visual),
        "rotation_assignments": 0,
        "missing_visuals": [
            {"sentence_number": number} for number in uncovered
        ],
        "verification_dimensions": {},
        "_editor_holdover_count": holdover,
    }


def _build_render_scenes(doc):
    """Scene lists for the renderer, plus the holdover record.

    Gap items are covered by HOLDOVER of the nearest genuine visual
    — the production renderer's own missing-media behaviour — never
    by invented footage, and every holdover is recorded so the
    export names exactly which visual was held across which gap.
    Without this the rendered film would run shorter than the
    narration windows and the renderer's frame lock would (rightly)
    refuse it. A revision with no genuine visuals at all cannot
    render."""
    scenes: dict[int, list] = {}
    holdovers: list[dict] = []
    last = None
    pending: list[dict] = []

    def _cover(gap, held_item):
        cover = dict(held_item)
        cover["start"] = gap["start"]
        cover["end"] = gap["end"]
        cover["duration"] = gap["duration"]
        cover["assignment"] = "holdover"
        cover["editor_gap_holdover"] = gap["item_id"]
        scene_number = int(gap.get("scene_number") or 1)
        scenes.setdefault(scene_number, []).append(cover)
        holdovers.append(
            {
                "gap_item_id": gap["item_id"],
                "sentence_number": gap["sentence_number"],
                "duration": gap["duration"],
                "held_file": (held_item.get("file_path")),
            }
        )

    for item in _visual(doc):
        if item["kind"] == "gap":
            if last is not None:
                _cover(item, last)
            else:
                pending.append(item)
            continue
        rendered = _renderer_item(item)
        for gap in pending:
            _cover(gap, rendered)
        pending = []
        scene_number = int(item.get("scene_number") or 1)
        scenes.setdefault(scene_number, []).append(rendered)
        last = rendered
    if pending and last is None:
        raise RuntimeError(
            "The revision has no genuine visuals to render "
            "(every window is an unresolved gap)."
        )
    scene_list = [
        {"scene_number": number, "items": scenes[number]}
        for number in sorted(scenes)
    ]
    return scene_list, holdovers


def _renderer_item(item) -> dict:
    asset = item.get("asset") or {}
    path = asset.get("file_path")
    return {
        "type": asset.get("type") or "video",
        "file_path": path,
        "path": path,
        "source": asset.get("source"),
        "source_url": asset.get("source_url"),
        "title": asset.get("title"),
        "sentence_index": (item["sentence_number"] or 1) - 1,
        "scene_number": item.get("scene_number"),
        "start": item["start"],
        "end": item["end"],
        "duration": item["duration"],
        "assignment": item.get("assignment") or "editor",
        "relevance_status": (
            "editor_revision" if item["kind"] != "graphic" else "generated"
        ),
        "clip_start": asset.get("clip_start") or 0,
        "motion": "zoom_in",
        "transition": item.get("transition") or "cut",
        "video_url": path if asset.get("type") == "video" else None,
        "image_url": path if asset.get("type") == "image" else None,
    }


def _music_source_type(doc, music_track):
    """How the revision's music file arrived (Stage H source
    classification). Editor-set tracks carry their classification
    in the track's "source"; an imported production track falls
    back to what the production recorded (legacy file vs operator
    option), else operator_placed — the production's music could
    only have arrived by an operator's hand."""
    source = music_track.get("source")
    if source in ("user_supplied", "project_file"):
        return source
    record = music_track.get("soundtrack_record") or {}
    if record.get("decided_by") == "legacy_background_mp3":
        return "legacy_project_file"
    if record.get("source_type"):
        return record["source_type"]
    return "operator_placed"


def _production_soundtrack_matches(doc, music_track, production_record,
                                   source_run_id, narration_volume,
                                   music_volume, music_service,
                                   mix_config=None):
    """Whether copying the PRODUCTION soundtrack.wav is honest for
    this revision (Stage H): the dependency plan compares the
    revision against its PARENT, but the copy source is the
    production's soundtrack — so the copy is allowed only when the
    production's own record matches this revision's music
    selection AND volumes. Stage J adds the mix shape: when the
    production record carries a "mix" section and the revision's
    resolved mix_config is supplied, their fingerprints must match
    — a production mixed with different fades/ducking/
    normalisation is a different soundtrack. A production record
    WITHOUT a mix section predates Stage J; its audio was shaped
    by the legacy chain, so the revision rebuilds with the current
    chain rather than silently shipping legacy-shaped audio. Any
    doubt rebuilds (a rebuild is always honest; a wrong copy is
    not)."""
    if not isinstance(production_record, dict):
        return False
    if mix_config is not None:
        production_mix = production_record.get("mix")
        if not isinstance(production_mix, dict):
            return False
        if (
            music_service.mix_fingerprint(production_mix)
            != music_service.mix_fingerprint(mix_config)
        ):
            return False
    run = run_ledger.get_run(source_run_id) or {}
    run_options = run.get("options") or {}
    try:
        production_music_volume = float(
            run_options.get("music_volume")
            if run_options.get("music_volume") is not None
            else music_service.MUSIC_VOLUME
        )
        production_narration_volume = float(
            run_options.get("narration_volume")
            if run_options.get("narration_volume") is not None
            else 1.0
        )
    except (TypeError, ValueError):
        return False
    if abs(production_music_volume - music_volume) > 0.001:
        return False
    if abs(production_narration_volume - narration_volume) > 0.001:
        return False
    state = production_record.get("state") or (
        "applied" if production_record.get("music") == "mixed"
        else "not_requested"
    )
    selected = music_track.get("file")
    if not selected:
        return state != "applied"
    if state != "applied":
        return False
    identity = production_record.get("identity") or {}
    recorded_hash = identity.get("content_sha256")
    if recorded_hash:
        return recorded_hash == music_service.file_sha256(selected)
    # Pre-Stage-H records carry only the basename.
    return production_record.get("source") == Path(str(selected)).name


def _copied_soundtrack_record(production_record, music_track,
                              source_type, music_service):
    """The revision's soundtrack record when the production
    soundtrack is reused verbatim: the production's record IS the
    description of that audio. When no production record exists
    (very old productions), describe the selection truthfully from
    the file itself."""
    if isinstance(production_record, dict) and production_record:
        return production_record
    selected = music_track.get("file")
    if selected:
        return {
            "music": "mixed",
            "source": Path(str(selected)).name,
            "loudness_lufs": None,
            "state": "applied",
            "requested": False,
            "requested_file": str(selected),
            "decided_by": "editor_revision",
            "source_type": source_type,
            "music_volume": None,
            "identity": music_service.describe_music_file(
                selected, source_type
            ),
            "mix": None,
            "error": None,
            "status": "soundtrack_created",
            "output_file": None,
        }
    return {
        "music": "absent",
        "source": None,
        "loudness_lufs": None,
        "state": music_track.get("state") or "not_requested",
        "requested": False,
        "requested_file": None,
        "decided_by": "editor_revision",
        "source_type": None,
        "music_volume": None,
        "identity": None,
        "mix": None,
        "error": None,
        "status": "soundtrack_created",
        "output_file": None,
    }


def music_certificate_data(doc) -> dict:
    """The revision certificate's music section (Stage H, M7/M8),
    derived from the revision document: its own soundtrack record
    when a render (or the production import) produced one, else
    the track's selection stated plainly. Licence is "unknown"
    unless the record carries evidence — it never does for
    operator/editor-supplied files."""
    music = (doc.get("tracks") or {}).get("music") or {}
    record = music.get("soundtrack_record")
    if isinstance(record, dict) and record:
        identity = record.get("identity") or {}
        mix = record.get("mix") or {}
        normalisation = mix.get("normalisation") or {}
        return {
            "state": record.get("state") or "not_requested",
            "file_name": identity.get("file_name")
            or record.get("source"),
            "content_sha256": identity.get("content_sha256"),
            "source_type": record.get("source_type")
            or identity.get("source_type"),
            "license": identity.get("license") or "unknown",
            "rights_status": "verify_before_publish",
            "decided_by": record.get("decided_by"),
            "music_volume": record.get("music_volume"),
            # Stage J (P5): the revision's mix provenance — the
            # preset that shaped it and whether the soundtrack was
            # normalised, with the measured result.
            "mix_preset": (mix.get("preset") or {}).get("name"),
            "normalised": bool(normalisation.get("applied")),
            "normalisation_target_lufs": normalisation.get("target_lufs"),
            "normalisation_post_lufs": normalisation.get(
                "post_integrated_lufs"
            ),
        }
    selected = music.get("file")
    return {
        "state": music.get("state")
        or ("applied" if selected else "not_recorded"),
        "file_name": Path(str(selected)).name if selected else None,
        "content_sha256": None,
        "source_type": music.get("source"),
        "license": music.get("license") or "unknown",
        "rights_status": "verify_before_publish",
        "decided_by": None,
        "music_volume": None,
    }


def run_revision_render(project_id, revision_id, run_id) -> dict:
    """Execute one revision render (called by the jobs worker).
    Runs under the Stage 2 lifecycle: claim -> phases with
    cooperative cancellation checks -> auditor -> export record.
    Never touches the production's artefacts: all work happens in
    editor_renders/<revision_id>/ and the output is
    revision_<id>_final.mp4."""
    from services import captions as captions_service
    from services import music as music_service
    from services import narration as narration_service
    from services import renderer
    from services import timing as timing_service

    project_id = str(project_id)
    claimed = run_ledger.claim_run(run_id)
    if claimed is None:
        status = None
        db = _session()
        try:
            from models import ProductionRun

            row = db.get(ProductionRun, str(run_id))
            status = row.status if row else None
        finally:
            db.close()
        if status == "cancelled":
            _update_row_status(project_id, revision_id, "draft")
        return {"status": status or "not_claimed", "run_id": run_id}

    # Stage 3 (R2): heartbeat for the render run while it executes,
    # exactly as for production runs in jobs — self-terminating when
    # the run leaves "running", fail-soft throughout.
    run_ledger.start_heartbeat(run_id)

    def _cancel_check():
        if run_ledger.is_cancel_requested(run_id):
            raise _RenderCancelled()

    project_dir = _project_dir(project_id)
    work_dir = project_dir / "editor_renders" / revision_id
    work_dir.mkdir(parents=True, exist_ok=True)
    view, doc = get_revision(project_id, revision_id)
    render_state = doc.get("render_state") or {}
    allow_gaps = bool(render_state.get("allow_gaps"))
    plan = render_state.get("plan") or compute_render_plan(None, doc)
    source_run_id = view["run_id"]

    try:
        # ---- narration -------------------------------------------
        narration_track = doc["tracks"]["narration"]
        sentences = narration_track.get("sentences") or []
        narration_file = work_dir / "narration.wav"
        if plan["narration_rebuild"] and sentences:
            parts_dir = work_dir / "narration_parts"
            parts_dir.mkdir(parents=True, exist_ok=True)
            production_parts = project_dir / "narration_parts"
            if production_parts.is_dir():
                for part in production_parts.iterdir():
                    target = parts_dir / part.name
                    if not target.exists():
                        shutil.copy2(part, target)
            text = " ".join(
                (sentence.get("text") or "").strip() for sentence in sentences
            )
            narration_service.generate_narration(
                text,
                narration_file,
                voice_id=narration_track.get("voice_id"),
            )
            # Re-derive windows from the measured part durations
            # and retime proportionally inside each window. Stage 5
            # (R4): the windows come from services.timing — the
            # same authority production uses, so revision and
            # production timing can never diverge in method —
            # replacing this block's private per-part duration
            # loop. A part without a measurement fails the render
            # loudly (timing_unavailable); the old loop silently
            # kept the previous window for any unmeasured part.
            manifest = narration_service.load_parts_manifest_data(
                parts_dir / "parts_manifest.json"
            )
            if manifest is None:
                raise timing_service.TimingUnavailableError(
                    "timing_unavailable: the revision's narration "
                    "parts manifest is missing — cannot retime."
                )
            windows = timing_service.sentence_windows(manifest)
            if len(windows) != len(sentences):
                raise timing_service.TimingUnavailableError(
                    "timing_unavailable: the revision's parts "
                    f"manifest describes {len(windows)} sentences "
                    f"but the revision has {len(sentences)}."
                )
            if windows:
                cursor = 0.0
                retimed = {}
                visual = _visual(doc)
                for index, sentence in enumerate(sentences):
                    number = sentence["sentence_number"]
                    old_start = float(sentence["start"])
                    old_end = float(sentence["end"])
                    new_duration = windows[index]["duration"]
                    new_start, new_end = (
                        windows[index]["start"],
                        windows[index]["end"],
                    )
                    scale = (
                        (new_end - new_start) / (old_end - old_start)
                        if old_end > old_start else 1.0
                    )
                    for item in visual:
                        if item["sentence_number"] == number:
                            item["start"] = round(
                                new_start
                                + (float(item["start"]) - old_start) * scale,
                                3,
                            )
                            item["end"] = round(
                                new_start
                                + (float(item["end"]) - old_start) * scale,
                                3,
                            )
                            item["duration"] = round(
                                float(item["end"]) - float(item["start"]), 3
                            )
                    sentence["start"] = round(new_start, 3)
                    sentence["end"] = round(new_end, 3)
                    retimed[number] = round(new_duration, 3)
                    cursor = new_end
                doc["duration_seconds"] = round(cursor, 3)
                doc["op_log"] = list(doc.get("op_log") or []) + [
                    {
                        "op_id": _new_id(),
                        "type": "retime_after_narration",
                        "actor": "render",
                        "at": _now_iso(),
                        "params": {"windows": retimed},
                        "summary": "Sentence windows re-derived from "
                        "regenerated narration; items retimed "
                        "proportionally.",
                    }
                ]
            narration_track["dirty_sentences"] = []
            narration_track["file"] = str(narration_file)
        else:
            production_narration = project_dir / "narration.wav"
            if production_narration.is_file():
                shutil.copy2(production_narration, narration_file)
            elif narration_track.get("file") and Path(
                narration_track["file"]
            ).is_file():
                shutil.copy2(narration_track["file"], narration_file)
            else:
                raise RuntimeError(
                    "No narration audio available for this revision."
                )
        _cancel_check()

        # ---- soundtrack ------------------------------------------
        music_track = doc["tracks"]["music"]
        soundtrack_file = work_dir / "soundtrack.wav"
        narration_volume = float((doc.get("volumes") or {}).get("narration", 1.0))
        music_volume = float(
            (doc.get("volumes") or {}).get("music", music_service.MUSIC_VOLUME)
        )
        # Stage H (M8): the render keeps the soundtrack RECORD —
        # prepare_soundtrack's return is no longer discarded. The
        # revision gets its own soundtrack.json in its work dir and
        # the record is stored on the document's music track; the
        # production's soundtrack.json is never rewritten.
        music_source_type = _music_source_type(doc, music_track)
        production_record = _read_json(project_dir / "soundtrack.json")
        # Stage J (P4): the revision's mix configuration — the
        # document's music volume is explicit; fades/ducking/
        # normalisation resolve from env, the production run's
        # genre preset, or defaults, exactly as jobs resolves them.
        source_run = run_ledger.get_run(source_run_id) or {}
        source_options = source_run.get("options") or {}
        mix_config = music_service.resolve_mix_config(
            genre_pack=source_options.get("genre_pack"),
            music_volume=music_volume,
        )
        copy_ok = (
            not plan["soundtrack_rebuild"]
            and (project_dir / "soundtrack.wav").is_file()
            and _production_soundtrack_matches(
                doc, music_track, production_record, source_run_id,
                narration_volume, music_volume, music_service,
                mix_config=mix_config,
            )
        )
        mix_reference = narration_file
        if not copy_ok:
            mix_input = narration_file
            if abs(narration_volume - 1.0) > 0.001:
                adjusted = work_dir / "narration_adjusted.wav"
                # renderer.run_ffmpeg is the renderer's only command
                # helper (S7-D1: this call site named a nonexistent
                # run_command, so ANY narration volume != 1.0 crashed
                # the render). It reports failure by returning False
                # rather than raising — surface that as a render
                # failure so the run records failed honestly.
                if not renderer.run_ffmpeg(
                    [
                        "ffmpeg", "-y", "-i", str(narration_file),
                        "-af", f"volume={narration_volume}",
                        str(adjusted),
                    ]
                ):
                    raise RuntimeError(
                        "Narration volume adjustment failed "
                        f"(volume={narration_volume})."
                    )
                mix_input = adjusted
            mix_reference = mix_input
            try:
                soundtrack_record = music_service.prepare_soundtrack(
                    mix_input,
                    soundtrack_file,
                    music_file=music_track.get("file"),
                    music_volume=music_volume,
                    source_type=music_source_type,
                    decided_by="editor_revision",
                    requested=bool(music_track.get("file")),
                    disabled=music_track.get("state") == "disabled",
                    mix_config=mix_config,
                )
            except Exception as prep_error:
                failed_record = music_service.failed_music_record(
                    requested_file=music_track.get("file"),
                    source_type=music_source_type,
                    decided_by="editor_revision",
                    requested=bool(music_track.get("file")),
                    error=prep_error,
                )
                write_json_atomic(work_dir / "soundtrack.json", failed_record)
                raise
        else:
            shutil.copy2(project_dir / "soundtrack.wav", soundtrack_file)
            soundtrack_record = _copied_soundtrack_record(
                production_record, music_track, music_source_type,
                music_service,
            )
        write_json_atomic(work_dir / "soundtrack.json", soundtrack_record)
        music_track["soundtrack_record"] = soundtrack_record
        if soundtrack_record.get("state"):
            music_track["state"] = soundtrack_record["state"]
        _cancel_check()

        # ---- visual render ---------------------------------------
        scene_list, holdovers = _build_render_scenes(doc)
        raw_video = work_dir / "documentary_raw.mp4"
        renderer.render_timeline(
            scene_list,
            work_dir,
            expected_duration=float(doc["duration_seconds"]),
        )
        rendered_raw = work_dir / "documentary.mp4"
        if not rendered_raw.is_file():
            raise RuntimeError("The renderer produced no documentary.mp4.")
        _cancel_check()

        from services.audio_mixer import add_narration

        narrated = work_dir / "documentary_narrated.mp4"
        add_narration(rendered_raw, soundtrack_file, narrated)
        final = narrated

        # ---- captions --------------------------------------------
        captions_track = doc["tracks"]["captions"]
        captions_json = work_dir / "captions.json"
        mode = captions_track.get("mode") or "burn"
        if plan["captions_rebuild"] and plan["narration_rebuild"]:
            script_text = " ".join(
                (sentence.get("text") or "").strip() for sentence in sentences
            )
            built = captions_service.build_captions(
                narration_file, script_text, captions_json
            )
            captions_track["segments"] = built.get("segments") or []
            captions_track["timing_source"] = built.get("timing_source")
            captions_track["provisional"] = bool(built.get("provisional"))
            captions_track["stale"] = False
            captions_track["stale_reason"] = None
        elif plan["captions_rebuild"]:
            captions_service.write_edited_captions(
                {
                    "segments": captions_track.get("segments") or [],
                    "timing_source": captions_track.get("timing_source"),
                    "provisional": bool(captions_track.get("provisional")),
                },
                captions_json,
            )
        else:
            for name in ("captions.json", "captions.srt", "captions.vtt"):
                source = project_dir / name
                if source.is_file():
                    shutil.copy2(source, work_dir / name)
        if mode == "burn" and (work_dir / "captions.json").is_file():
            from services import caption_renderer

            ass_path = work_dir / "captions.ass"
            caption_renderer.create_ass_file(
                work_dir / "captions.json", ass_path
            )
            burned = work_dir / "documentary_captioned.mp4"
            caption_renderer.burn_captions(narrated, ass_path, burned)
            final = burned
        captions_track["file"] = str(captions_json)
        _cancel_check()

        # ---- publish + audit -------------------------------------
        revision_final = project_dir / f"revision_{revision_id}_final.mp4"
        shutil.copy2(final, revision_final)
        # Stage H (M9/M10): measure the exported file's audio and
        # cross-check the revision's soundtrack record against it,
        # exactly as productions do — the auditor verdicts on a
        # provenance mismatch, never on a loudness value.
        export_audio = music_service.final_audio_record(
            revision_final, soundtrack_record=soundtrack_record
        )
        audio_provenance = music_service.assess_audio_provenance(
            soundtrack_record.get("state"), revision_final, mix_reference
        )
        report = _revision_asset_report(doc, work_dir)
        report["soundtrack"] = soundtrack_record
        report["final_audio"] = export_audio
        report["audio_provenance"] = audio_provenance
        from services.final_video_auditor import validate_final_video

        audit = validate_final_video(
            revision_final,
            expected_duration=float(doc["duration_seconds"]),
            asset_report=report,
        )
        verdict = audit.get("verdict") or {}
        failures = list(verdict.get("failures") or [])
        warnings = list(verdict.get("warnings") or [])
        evaluated = bool(verdict.get("evaluated"))
        # INT-102: an unevaluated verdict is NOT a pass. The export
        # decision previously treated "not evaluated" as passed,
        # the same gate-bypass shape as the production completion
        # gate; an audit that never ran must stop the export, not
        # wave it through.
        passed = evaluated and bool(verdict.get("passed", False))
        gaps = gap_items(doc)
        exported_with_gaps: list[int] = []
        if not passed and allow_gaps and gaps:
            # The ONLY failures a flagged gap may excuse are the
            # coverage failures for the gap sentences themselves.
            gap_sentences = {gap["sentence_number"] for gap in gaps}
            uncovered = set(
                (report.get("coverage") or {}).get("uncovered_sentences")
                or []
            )
            non_gap_failures = [
                failure for failure in failures
                if not failure.startswith("coverage_incomplete")
            ]
            if not non_gap_failures and uncovered and uncovered <= gap_sentences:
                passed = True
                exported_with_gaps = sorted(uncovered)
                warnings.append(
                    "coverage gaps exported under allow_gaps for "
                    f"sentence(s) {exported_with_gaps}"
                )
        if not passed:
            raise RuntimeError(
                "Final video audit failed for the revision render: "
                + "; ".join(failures or ["unknown failure"])
            )
        verdict = {
            "evaluated": evaluated,
            "passed": passed,
            "failures": failures,
            "warnings": warnings,
            "duration_seconds": audit.get("duration_seconds"),
            "exported_with_gaps": exported_with_gaps,
        }
        export = {
            "export_id": _new_id(),
            "file": str(revision_final),
            "file_name": revision_final.name,
            "sha256": run_ledger._file_sha256(str(revision_final), {}),
            "at": _now_iso(),
            "run_id": run_id,
            "allow_gaps": allow_gaps,
            "gaps_exported": [
                {
                    "item_id": gap["item_id"],
                    "sentence_number": gap["sentence_number"],
                    "duration": gap["duration"],
                }
                for gap in gaps
            ],
            "holdovers": holdovers,
            "audit": verdict,
            "render_plan": plan,
            # Stage H (M9): the export record carries the measured
            # audio of the exported file itself.
            "audio_levels": export_audio,
            "audio_provenance": audio_provenance,
        }
        doc.setdefault("exports", []).append(export)
        doc["render_state"] = {**(doc.get("render_state") or {}),
                               "completed_at": _now_iso()}
        _update_doc_file(project_id, doc)
        _update_row_status(project_id, revision_id, "exported")
        # Stage H (M7): the revision's music decision joins the
        # ledger, tagged with the revision id — the original run's
        # certificate (revision decisions excluded, D3) is
        # untouched by it.
        run_ledger.record_music_decision(
            source_run_id, project_id, soundtrack_record,
            revision_id=revision_id,
        )
        run_ledger.finish_run(
            run_id, "completed",
            execution={"revision_id": revision_id, "entrypoint": "editor_render"},
        )
        return {
            "status": "exported",
            "run_id": run_id,
            "revision_id": revision_id,
            "export": export,
        }
    except _RenderCancelled:
        run_ledger.finish_run(run_id, "cancelled")
        _update_row_status(project_id, revision_id, "draft")
        return {"status": "cancelled", "run_id": run_id}
    except Exception as error:
        run_ledger.finish_run(run_id, "failed", error=str(error))
        try:
            _view, failed_doc = get_revision(project_id, revision_id)
            failed_doc["last_render_error"] = str(error)
            _update_doc_file(project_id, failed_doc)
        except Exception:
            pass
        _update_row_status(project_id, revision_id, "failed")
        return {
            "status": "failed",
            "run_id": run_id,
            "error": str(error),
        }


# ---------------------------------------------------------------------
# Certificate support (Part 5)
# ---------------------------------------------------------------------


def final_assets_for_certificate(doc) -> list[dict]:
    assets = []
    for item in _visual(doc):
        asset = item.get("asset") or {}
        assets.append(
            {
                "item_id": item["item_id"],
                "kind": item["kind"],
                "classification": item.get("classification"),
                "sentence_number": item.get("sentence_number"),
                "scene_number": item.get("scene_number"),
                "file_name": (
                    Path(str(asset.get("file_path"))).name
                    if asset.get("file_path") else None
                ),
                "provider": asset.get("provider") or asset.get("source"),
                "source_url": asset.get("source_url"),
                "evidence_class": asset.get("evidence_class"),
                "verification": plan_document._verification_outcome(
                    asset.get("verification")
                ),
                "rights": asset.get("rights"),
                "timeline": {
                    "start": item.get("start"),
                    "duration": item.get("duration"),
                },
            }
        )
    return assets
