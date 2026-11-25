"""Asset Library (founder priority #7; gap-audit Phase E).

The gap audit's central finding for this surface (doc 04 §2): the
Asset Manager is a production *evidence* system; the vision's Asset
*Library* is a user media product, and the gap is the library layer
— which must be built ON the ledger, never beside it. This module
is that layer:

- POPULATION is derived, never stored: every ``asset_decisions``
  row of the user's projects whose media file can be resolved,
  plus every entry of the projects' upload indexes, grouped by
  CONTENT HASH (the sha256 the ledger itself records). One library
  item = one distinct piece of content; its usages are the ledger
  rows (and upload sightings) that reference it, each carrying its
  verification verdict and rights dict VERBATIM. Nothing is
  summarised into invented metadata: a title is the recorded title
  or the file name (labelled as such), a licence is the recorded
  licence or "unknown", a verification state is derived from the
  recorded verdict by the published rule in ``_verification_state``
  and the verbatim verdict is always available in the detail view.
- Rows whose file cannot be resolved and which carry no content
  hash cannot be keyed into the library at all; they are COUNTED
  (``unkeyed_records``) in every listing summary rather than
  silently dropped. Rows with a hash but no surviving file remain
  listed with ``available: False`` — the record exists; the file
  does not, and the library says so.
- ORGANISATION (folders / tags — audit §2.4) is the one genuinely
  new data here. It lives in the Phase 8 preferences document's
  ``library`` section (the platform's single preferences store,
  the pattern the Assistant's section established) — keyed by
  content hash, so organisation follows the content across
  projects. No second media database.
- CROSS-PROJECT REUSE (audit §2.5) rides the editor's upload
  machinery, generalised: importing an asset copies its file into
  the target project's uploads, writes an upload-index entry whose
  rights and verification are the SOURCE decision's records carried
  verbatim, and appends a ``library_reuse`` ledger decision in the
  target project with ``previous_decision_id`` pointing at the
  source decision — provenance is carried across, never copied
  bare, so both projects' certificates tell the story. The import
  obeys exactly the editor upload contexts (an editor revision, or
  a plan parked at approval; otherwise the same 404
  ``no_production_context`` uploads get). The production reuse caps
  (3/3/3) and lexical (token-overlap) similarity thresholds are pipeline quality controls
  and are NOT touched by anything here (audit §2.5).
- DUPLICATES (audit §2.6) fall out of the hash grouping: an item
  whose content lives at more than one distinct file path is a
  duplicate, with the reclaimable bytes computed from the real
  file sizes.
- STORAGE numbers come from services.admin_storage's media-tree
  walk (the one accounting the platform has), filtered to the
  user's own projects — the library never invents a second set of
  numbers for the same bytes. The per-type breakdown is computed
  over library items' own recorded files and labelled as such.
- OWNERSHIP is Phase 8's: the visible project set is the
  principal's own projects (legacy NULL-owner rows only for the
  operator-token principal, exactly as the API's choke point
  resolves them). Another user's asset is indistinguishable from a
  nonexistent one — 404 everywhere.

Narration is deliberately absent: the ledger records no narration
decisions (the composed soundtrack's MUSIC decisions are recorded
and do appear, as audio). Claiming a narration library would
require inventing records the pipeline never made.
"""

from __future__ import annotations

import mimetypes
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from models import AssetDecision, Project, UserPreference
from services import admin_storage, editor, media_paths, plan_document
from services import run_ledger


class LibraryError(Exception):
    """A domain error with an HTTP status + stable machine code
    (the API layer maps it onto the v1 error shape)."""

    def __init__(self, status_code: int, code: str, message: str,
                 details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


# Decision types that mean the asset actually served in a film or
# was deliberately brought in by a person — the preferred import
# sources and the "used" population. Everything else with a file
# (verified_not_picked, rejected, download_failed survivors) is
# still library content, labelled by its own recorded type.
USED_DECISION_TYPES = frozenset(
    {
        "selected",
        "still",
        "swapped_in",
        "rotated",
        "generated",
        "user_supplied",
        "music",
        "library_reuse",
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------
# Ownership (Phase 8 semantics, mirroring api_v1's choke point)
# ---------------------------------------------------------------------


def _visible_projects(db, principal) -> list:
    """The projects this principal may see: their own, plus legacy
    NULL-owner rows for the operator-token principal only."""
    projects = db.query(Project).order_by(Project.created_at.asc()).all()
    visible = []
    for project in projects:
        if project.owner_id is None:
            if principal.auth_method == "operator_token":
                visible.append(project)
        elif project.owner_id == principal.user_id:
            visible.append(project)
    return visible


def _visible_project(db, principal, project_id):
    project = db.get(Project, str(project_id))
    if project is None:
        return None
    if project.owner_id is None:
        return project if principal.auth_method == "operator_token" else None
    return project if project.owner_id == principal.user_id else None


# ---------------------------------------------------------------------
# File resolution + hashing
# ---------------------------------------------------------------------

_hash_cache: dict = {}


def _hash_file(path) -> str | None:
    """sha256 of a file, cached on (path, size, mtime) so repeated
    library builds over the same tree stay cheap. None when the
    file cannot be read."""
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key in _hash_cache:
        return _hash_cache[key]
    digest = run_ledger._file_sha256(str(path), {})
    if digest:
        _hash_cache[key] = digest
    return digest


def _contained_file(project_dir, path) -> Path | None:
    """The resolved Path when `path` names an existing file under
    the project directory, else None. The containment check is the
    media index's own rule: records pointing outside the project
    (operator paths, foreign projects) are not this project's
    library files."""
    if not path:
        return None
    try:
        resolved = Path(str(path)).resolve()
        resolved.relative_to(Path(project_dir).resolve())
    except (ValueError, OSError):
        return None
    return resolved if resolved.is_file() else None


def _type_for_path(path) -> str:
    suffix = Path(str(path)).suffix.lower()
    if suffix in editor.VIDEO_SUFFIXES:
        return "video"
    if suffix in editor.IMAGE_SUFFIXES:
        return "image"
    if suffix in editor.AUDIO_SUFFIXES:
        return "audio"
    return "unknown"


def _verification_state(verification) -> str:
    """The derived filter label for one recorded verdict. The rule
    is published here and nowhere else: the pipeline's verdicts
    carry ``accepted`` (media_pipeline) or ``outcome``
    (frame_verification); editor-stamped verdicts may carry
    ``status``. A dict with none of these is "recorded" — a verdict
    exists but does not summarise into an outcome, and the library
    refuses to guess one. The verbatim dict always rides in the
    detail view beside this label."""
    if not isinstance(verification, dict) or not verification:
        return "not_recorded"
    if "accepted" in verification:
        return "accepted" if verification.get("accepted") else "rejected"
    outcome = verification.get("outcome")
    if isinstance(outcome, str) and outcome:
        return {"pass": "passed", "fail": "failed"}.get(outcome, outcome)
    status = verification.get("status")
    if isinstance(status, str) and status:
        return status
    return "recorded"


# ---------------------------------------------------------------------
# The index: ledger rows + upload sightings, grouped by content hash
# ---------------------------------------------------------------------


def _new_item(content_hash: str) -> dict:
    return {
        "content_hash": content_hash,
        "usages": [],
        "sightings": [],
        "paths": {},  # resolved file path -> project_id
    }


def _build_index(db, principal) -> dict:
    """Derive the principal's whole library from the records.

    Returns {"items": {hash: item}, "projects": {id: Project},
    "unkeyed_records": {"decisions": n, "uploads": n}}.
    """
    projects = _visible_projects(db, principal)
    project_by_id = {p.id: p for p in projects}
    items: dict[str, dict] = {}
    unkeyed = {"decisions": 0, "uploads": 0}

    for project in projects:
        project_dir = media_paths.project_dir(project.id)
        rows = (
            db.query(AssetDecision)
            .filter(AssetDecision.project_id == project.id)
            .order_by(AssetDecision.created_at.asc())
            .all()
        )

        # This project's hash -> path map, from the cheapest
        # recorded sources first: the upload index and the rows'
        # own asset_key paths. The plan document's media index is
        # consulted only when some row's content hash is still
        # unresolved (it is the most expensive source to build).
        hash_map: dict[str, str] = {}
        upload_records = []
        if project_dir.is_dir():
            for entry in editor._uploads_index(project_dir):
                resolved = _contained_file(project_dir, entry.get("file_path"))
                digest = _hash_file(resolved) if resolved else None
                upload_records.append((entry, resolved, digest))
                if digest and resolved:
                    hash_map.setdefault(digest, str(resolved))
        for row in rows:
            resolved = _contained_file(project_dir, row.asset_key)
            if resolved:
                digest = row.content_hash or _hash_file(resolved)
                if digest:
                    hash_map.setdefault(digest, str(resolved))
        if any(
            row.content_hash and row.content_hash not in hash_map
            for row in rows
        ):
            index = plan_document.build_media_index(project.id, project_dir)
            for entry in index.values():
                digest = _hash_file(entry["path"])
                if digest:
                    hash_map.setdefault(digest, str(entry["path"]))

        for entry, resolved, digest in upload_records:
            if digest is None:
                unkeyed["uploads"] += 1
                continue
            item = items.setdefault(digest, _new_item(digest))
            item["sightings"].append(
                {
                    "project_id": project.id,
                    "upload_id": entry.get("upload_id"),
                    "title": entry.get("title"),
                    "file_name": entry.get("file_name")
                    or (Path(str(resolved)).name if resolved else None),
                    "file_path": str(resolved) if resolved else None,
                    "type": entry.get("type"),
                    "provider": entry.get("provider"),
                    "size_bytes": entry.get("size_bytes"),
                    "decision_id": entry.get("decision_id"),
                    "rights": entry.get("rights"),
                    "library_provenance": entry.get("library_provenance"),
                }
            )
            if resolved:
                item["paths"][str(resolved)] = project.id

        for row in rows:
            resolved = _contained_file(project_dir, row.asset_key)
            digest = row.content_hash or (
                _hash_file(resolved) if resolved else None
            )
            if digest is None:
                unkeyed["decisions"] += 1
                continue
            if resolved is None and digest in hash_map:
                resolved = Path(hash_map[digest])
            item = items.setdefault(digest, _new_item(digest))
            item["usages"].append(
                {
                    "decision_id": row.id,
                    "project_id": project.id,
                    "run_id": row.run_id,
                    "revision_id": row.revision_id,
                    "scene_number": row.scene_number,
                    "sentence_number": row.sentence_number,
                    "decision_type": row.decision_type,
                    "evidence_class": row.evidence_class,
                    "verification": row.verification,
                    "verification_state": _verification_state(row.verification),
                    "rights": row.rights,
                    "provider": row.provider,
                    "source_url": row.source_url,
                    "asset_key": row.asset_key,
                    "previous_decision_id": row.previous_decision_id,
                    "created_at": row.created_at.isoformat()
                    if row.created_at
                    else None,
                    "file_path": str(resolved) if resolved else None,
                    "file_present": resolved is not None,
                }
            )
            if resolved:
                item["paths"][str(resolved)] = project.id

    for item in items.values():
        _finish_item(item)
    return {
        "items": items,
        "projects": project_by_id,
        "unkeyed_records": unkeyed,
    }


def _finish_item(item: dict) -> None:
    """Canonical file + seen range, derived once per build."""
    usages = item["usages"]
    sightings = item["sightings"]
    canonical = None
    for usage in sorted(
        usages,
        key=lambda u: (u["created_at"] or "", u["decision_id"]),
        reverse=True,
    ):
        if usage["file_present"] and usage["file_path"]:
            canonical = (usage["file_path"], usage["project_id"])
            break
    if canonical is None:
        for sighting in sightings:
            if sighting["file_path"]:
                canonical = (sighting["file_path"], sighting["project_id"])
                break
    item["canonical_path"] = canonical[0] if canonical else None
    item["canonical_project_id"] = canonical[1] if canonical else None
    size = None
    if canonical:
        try:
            size = Path(canonical[0]).stat().st_size
        except OSError:
            size = None
    if size is None:
        for sighting in sightings:
            if sighting["size_bytes"]:
                size = sighting["size_bytes"]
                break
    item["size_bytes"] = size
    stamps = [u["created_at"] for u in usages if u["created_at"]]
    item["first_seen"] = min(stamps) if stamps else None
    item["last_seen"] = max(stamps) if stamps else None


# ---------------------------------------------------------------------
# Item views
# ---------------------------------------------------------------------


def _item_rights(item: dict):
    """The item's rights record: the most recent usage that
    recorded one, verbatim; else an upload sighting's recorded
    rights; else None (never recorded anywhere)."""
    for usage in sorted(
        item["usages"],
        key=lambda u: (u["created_at"] or "", u["decision_id"]),
        reverse=True,
    ):
        if isinstance(usage["rights"], dict) and usage["rights"]:
            return usage["rights"]
    for sighting in item["sightings"]:
        if isinstance(sighting.get("rights"), dict) and sighting["rights"]:
            return sighting["rights"]
    return None


def _licence_state(rights) -> str:
    licence = (rights or {}).get("license")
    return "recorded" if licence and licence != "unknown" else "unknown"


def _item_title(item: dict):
    """(title, recorded?) — the recorded title (AM-014 carries it
    in rights, candidate rows in verification) when any usage
    recorded one, else the upload's title, else the file name's
    stem, labelled unrecorded so the UI never dresses a file name
    up as a catalogue title."""
    for usage in sorted(
        item["usages"],
        key=lambda u: (u["created_at"] or "", u["decision_id"]),
        reverse=True,
    ):
        for carrier in (usage["rights"], usage["verification"]):
            if isinstance(carrier, dict) and carrier.get("title"):
                return str(carrier["title"]), True
    for sighting in item["sightings"]:
        if sighting.get("title"):
            return str(sighting["title"]), True
    if item["canonical_path"]:
        return Path(item["canonical_path"]).stem.replace("_", " "), False
    return item["content_hash"][:12], False


def _item_type(item: dict) -> str:
    if item["canonical_path"]:
        derived = _type_for_path(item["canonical_path"])
        if derived != "unknown":
            return derived
    if any(u["decision_type"] == "music" for u in item["usages"]):
        return "audio"
    for sighting in item["sightings"]:
        if sighting.get("type"):
            return str(sighting["type"])
    return "unknown"


def _project_ref(projects: dict, project_id):
    project = projects.get(project_id)
    return {
        "id": project_id,
        "title": project.title if project else None,
    }


def _summary_view(item: dict, projects: dict, org_items: dict) -> dict:
    rights = _item_rights(item)
    title, title_recorded = _item_title(item)
    organisation = org_items.get(item["content_hash"]) or {}
    project_ids = sorted(
        {u["project_id"] for u in item["usages"]}
        | {s["project_id"] for s in item["sightings"]}
    )
    available = item["canonical_path"] is not None
    return {
        "id": item["content_hash"],
        "content_hash": item["content_hash"],
        "type": _item_type(item),
        "title": title,
        "title_recorded": title_recorded,
        "file_name": Path(item["canonical_path"]).name
        if item["canonical_path"]
        else None,
        "size_bytes": item["size_bytes"],
        "available": available,
        "media_url": (
            f"/api/v1/library/assets/{item['content_hash']}/media"
            if available
            else None
        ),
        "providers": sorted(
            {u["provider"] for u in item["usages"] if u["provider"]}
            | {s["provider"] for s in item["sightings"] if s.get("provider")}
            | ({"user_upload"} if item["sightings"] else set())
        ),
        "rights": rights,
        "licence_state": _licence_state(rights),
        "verification_states": sorted(
            {u["verification_state"] for u in item["usages"]}
        ),
        "decision_types": sorted(
            {u["decision_type"] for u in item["usages"]}
            | ({"upload"} if item["sightings"] else set())
        ),
        "usage_count": len(item["usages"]),
        "projects": [_project_ref(projects, pid) for pid in project_ids],
        "copies": len(item["paths"]),
        "duplicate": len(item["paths"]) > 1,
        "tags": list(organisation.get("tags") or []),
        "folder_id": organisation.get("folder_id"),
        "first_seen": item["first_seen"],
        "last_seen": item["last_seen"],
    }


def _detail_view(item: dict, projects: dict, org_items: dict) -> dict:
    view = _summary_view(item, projects, org_items)
    usages = []
    for usage in sorted(
        item["usages"], key=lambda u: (u["created_at"] or "", u["decision_id"])
    ):
        usages.append(
            {
                **usage,
                "project": _project_ref(projects, usage["project_id"]),
            }
        )
    view["usages"] = usages
    view["upload_sightings"] = [
        {**s, "project": _project_ref(projects, s["project_id"])}
        for s in item["sightings"]
    ]
    view["copies_detail"] = [
        {
            "file_path": path,
            "project": _project_ref(projects, project_id),
            "size_bytes": item["size_bytes"],
        }
        for path, project_id in sorted(item["paths"].items())
    ]
    view["duplicate_reclaimable_bytes"] = (
        item["size_bytes"] * (len(item["paths"]) - 1)
        if len(item["paths"]) > 1 and item["size_bytes"]
        else 0
    )
    view["home_project"] = (
        _project_ref(projects, item["canonical_project_id"])
        if item["canonical_project_id"]
        else None
    )
    view["home_media_key"] = None
    if item["canonical_path"] and item["canonical_project_id"]:
        project_dir = media_paths.project_dir(item["canonical_project_id"])
        index = plan_document.build_media_index(
            item["canonical_project_id"], project_dir
        )
        try:
            key = plan_document.media_key_for(
                project_dir, item["canonical_path"]
            )
        except (ValueError, OSError):
            key = None
        view["home_media_key"] = key if key in index else None
    return view


# ---------------------------------------------------------------------
# Organisation: folders + tags in the preferences document
# ---------------------------------------------------------------------


def _load_organisation(db, user_id) -> dict:
    if not user_id:
        return {"folders": [], "items": {}}
    row = db.get(UserPreference, user_id)
    document = dict((row.document if row else None) or {})
    section = document.get("library")
    if not isinstance(section, dict):
        section = {}
    folders = [
        f
        for f in (section.get("folders") or [])
        if isinstance(f, dict) and f.get("id") and f.get("name")
    ]
    items = section.get("items")
    if not isinstance(items, dict):
        items = {}
    return {"folders": folders, "items": items}


def _save_organisation(db, user_id, organisation: dict) -> None:
    if not user_id:
        raise LibraryError(
            403,
            "no_user",
            "Library organisation needs a user account.",
        )
    row = db.get(UserPreference, user_id)
    document = dict((row.document if row else None) or {})
    document["library"] = organisation
    if row is None:
        db.add(UserPreference(user_id=user_id, document=document))
    else:
        row.document = document
    db.commit()


def list_folders(db, principal) -> dict:
    organisation = _load_organisation(db, principal.user_id)
    counts: dict[str, int] = {}
    for entry in organisation["items"].values():
        folder_id = (entry or {}).get("folder_id")
        if folder_id:
            counts[folder_id] = counts.get(folder_id, 0) + 1
    return {
        "folders": [
            {**folder, "item_count": counts.get(folder["id"], 0)}
            for folder in organisation["folders"]
        ]
    }


def create_folder(db, principal, name: str) -> dict:
    name = str(name or "").strip()
    if not name:
        raise LibraryError(422, "folder_name_required", "A folder needs a name.")
    organisation = _load_organisation(db, principal.user_id)
    if any(
        f["name"].lower() == name.lower() for f in organisation["folders"]
    ):
        raise LibraryError(
            409, "folder_exists", f"A folder named “{name}” already exists."
        )
    folder = {"id": _new_id("folder"), "name": name, "created_at": _now_iso()}
    organisation["folders"].append(folder)
    _save_organisation(db, principal.user_id, organisation)
    return {**folder, "item_count": 0}


def delete_folder(db, principal, folder_id: str) -> dict:
    organisation = _load_organisation(db, principal.user_id)
    kept = [f for f in organisation["folders"] if f["id"] != folder_id]
    if len(kept) == len(organisation["folders"]):
        raise LibraryError(404, "folder_not_found", f"No folder {folder_id}.")
    organisation["folders"] = kept
    unfiled = 0
    for entry in organisation["items"].values():
        if isinstance(entry, dict) and entry.get("folder_id") == folder_id:
            entry["folder_id"] = None
            unfiled += 1
    _save_organisation(db, principal.user_id, organisation)
    return {"deleted": folder_id, "items_unfiled": unfiled}


def set_item_organisation(
    db, principal, content_hash: str, *, tags=None, folder_id=...,
) -> dict:
    """Set an item's tags and/or folder. Only the fields supplied
    change (folder_id=... means untouched; folder_id=None unfiles).
    The item must exist in the principal's library — organising
    another user's asset is a 404, like everything else here."""
    index = _build_index(db, principal)
    item = index["items"].get(str(content_hash))
    if item is None:
        raise LibraryError(
            404, "asset_not_found", f"No library asset {content_hash}."
        )
    organisation = _load_organisation(db, principal.user_id)
    entry = dict(organisation["items"].get(item["content_hash"]) or {})
    if tags is not None:
        cleaned = []
        for tag in tags:
            tag = str(tag).strip()
            if tag and tag not in cleaned:
                cleaned.append(tag)
        entry["tags"] = cleaned
    if folder_id is not ...:
        if folder_id is not None and not any(
            f["id"] == folder_id for f in organisation["folders"]
        ):
            raise LibraryError(
                404, "folder_not_found", f"No folder {folder_id}."
            )
        entry["folder_id"] = folder_id
    organisation["items"][item["content_hash"]] = entry
    _save_organisation(db, principal.user_id, organisation)
    return _summary_view(item, index["projects"], organisation["items"])


# ---------------------------------------------------------------------
# Listing / search / detail
# ---------------------------------------------------------------------


def list_assets(
    db,
    principal,
    *,
    q=None,
    project_id=None,
    type=None,
    provider=None,
    licence=None,
    verification=None,
    decision_type=None,
    folder=None,
    tag=None,
    duplicates_only=False,
    limit=100,
    offset=0,
) -> dict:
    """Browse + search the library. Filters match the RECORDED
    data: q is a substring over title / file name / source URL /
    provider / tags; the rest are exact matches against the
    derived item fields (licence and verification use the derived
    states documented on the item view)."""
    index = _build_index(db, principal)
    organisation = _load_organisation(db, principal.user_id)
    org_items = organisation["items"]
    views = [
        _summary_view(item, index["projects"], org_items)
        for item in index["items"].values()
    ]

    def matches(view) -> bool:
        if q:
            needle = str(q).lower()
            haystacks = [
                view["title"] or "",
                view["file_name"] or "",
                *(view["providers"]),
                *(view["tags"]),
            ]
            item = index["items"][view["content_hash"]]
            haystacks += [
                u["source_url"] or "" for u in item["usages"]
            ]
            if not any(needle in h.lower() for h in haystacks):
                return False
        if project_id and not any(
            p["id"] == project_id for p in view["projects"]
        ):
            return False
        if type and view["type"] != type:
            return False
        if provider and provider not in view["providers"]:
            return False
        if licence and view["licence_state"] != licence:
            return False
        if verification and verification not in view["verification_states"]:
            return False
        if decision_type and decision_type not in view["decision_types"]:
            return False
        if folder:
            if folder == "unfiled":
                if view["folder_id"]:
                    return False
            elif view["folder_id"] != folder:
                return False
        if tag and tag not in view["tags"]:
            return False
        if duplicates_only and not view["duplicate"]:
            return False
        return True

    filtered = [v for v in views if matches(v)]
    filtered.sort(
        key=lambda v: (v["last_seen"] or "", v["first_seen"] or "", v["id"]),
        reverse=True,
    )
    total = len(filtered)
    limit = min(max(1, int(limit)), 500)
    offset = max(0, int(offset))
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": filtered[offset : offset + limit],
        "summary": {
            "library_items": len(views),
            "library_usages": sum(
                len(i["usages"]) for i in index["items"].values()
            ),
            "projects_covered": len(index["projects"]),
            "unkeyed_records": index["unkeyed_records"],
        },
        "folders": [
            {"id": f["id"], "name": f["name"]}
            for f in organisation["folders"]
        ],
    }


def get_asset(db, principal, content_hash: str) -> dict:
    index = _build_index(db, principal)
    item = index["items"].get(str(content_hash))
    if item is None:
        raise LibraryError(
            404, "asset_not_found", f"No library asset {content_hash}."
        )
    organisation = _load_organisation(db, principal.user_id)
    return _detail_view(item, index["projects"], organisation["items"])


def list_duplicates(db, principal) -> dict:
    """Audit §2.6: the same content stored at more than one path.
    Reclaimable bytes assume the copies are byte-identical — they
    are, by construction: they share one sha256."""
    index = _build_index(db, principal)
    organisation = _load_organisation(db, principal.user_id)
    duplicates = []
    for item in index["items"].values():
        if len(item["paths"]) <= 1:
            continue
        view = _detail_view(item, index["projects"], organisation["items"])
        duplicates.append(
            {
                "content_hash": item["content_hash"],
                "title": view["title"],
                "type": view["type"],
                "size_bytes": item["size_bytes"],
                "copies": view["copies_detail"],
                "extra_copies": len(item["paths"]) - 1,
                "reclaimable_bytes": view["duplicate_reclaimable_bytes"],
            }
        )
    duplicates.sort(key=lambda d: d["reclaimable_bytes"], reverse=True)
    return {
        "duplicates": duplicates,
        "total_items": len(duplicates),
        "total_reclaimable_bytes": sum(
            d["reclaimable_bytes"] for d in duplicates
        ),
    }


# ---------------------------------------------------------------------
# Storage (admin_storage's accounting, owner-filtered)
# ---------------------------------------------------------------------


def storage_view(db, principal) -> dict:
    """Per-project usage is admin_storage.walk()'s numbers — the
    platform's one accounting — restricted to the principal's
    projects. The per-type breakdown covers LIBRARY items only
    (their canonical files, counted once each) and is labelled so;
    it is a view of the library, not a second disk accounting."""
    data = admin_storage.walk()
    projects = _visible_projects(db, principal)
    owned = []
    total_bytes = 0
    total_files = 0
    for project in projects:
        info = data["projects"].get(project.id)
        size = info["bytes"] if info else 0
        files = info["files"] if info else 0
        total_bytes += size
        total_files += files
        owned.append(
            {
                "project_id": project.id,
                "title": project.title,
                "status": project.status,
                "bytes": size,
                "files": files,
                "in_media_tree": info is not None,
            }
        )
    owned.sort(key=lambda p: p["bytes"], reverse=True)

    index = _build_index(db, principal)
    by_type: dict[str, dict] = {}
    duplicate_items = 0
    reclaimable = 0
    for item in index["items"].values():
        if item["canonical_path"] is None:
            continue
        bucket = by_type.setdefault(
            _item_type(item), {"items": 0, "bytes": 0}
        )
        bucket["items"] += 1
        bucket["bytes"] += item["size_bytes"] or 0
        if len(item["paths"]) > 1:
            duplicate_items += 1
            reclaimable += (item["size_bytes"] or 0) * (len(item["paths"]) - 1)
    return {
        "media_root": data["root"],
        "total_bytes": total_bytes,
        "total_files": total_files,
        "projects": owned,
        "library_by_type": by_type,
        "duplicates": {
            "items": duplicate_items,
            "reclaimable_bytes": reclaimable,
        },
    }


# ---------------------------------------------------------------------
# Media serving
# ---------------------------------------------------------------------


def resolve_media_path(db, principal, content_hash: str) -> Path:
    """The file to serve for a library item: any recorded copy
    under a project the principal owns, verified to hash to the
    requested content. 404 asset_not_found when the principal's
    library has no such content at all; 404 asset_file_missing
    when records exist but no file survives — the same honesty
    split as the item views."""
    projects = _visible_projects(db, principal)
    if not projects:
        raise LibraryError(
            404, "asset_not_found", f"No library asset {content_hash}."
        )
    project_ids = [p.id for p in projects]
    rows = (
        db.query(AssetDecision)
        .filter(
            AssetDecision.project_id.in_(project_ids),
            AssetDecision.content_hash == str(content_hash),
        )
        .all()
    )
    seen_record = bool(rows)
    for project in projects:
        project_dir = media_paths.project_dir(project.id)
        for row in rows:
            if row.project_id != project.id:
                continue
            resolved = _contained_file(project_dir, row.asset_key)
            if resolved and _hash_file(resolved) == str(content_hash):
                return resolved
        if not project_dir.is_dir():
            continue
        for entry in editor._uploads_index(project_dir):
            resolved = _contained_file(project_dir, entry.get("file_path"))
            if resolved and _hash_file(resolved) == str(content_hash):
                seen_record = True
                return resolved
        index = plan_document.build_media_index(project.id, project_dir)
        for entry in index.values():
            if _hash_file(entry["path"]) == str(content_hash):
                seen_record = True
                return Path(entry["path"])
    if seen_record:
        raise LibraryError(
            404,
            "asset_file_missing",
            "The asset is recorded in the library but its media "
            "file is not present on disk.",
        )
    raise LibraryError(
        404, "asset_not_found", f"No library asset {content_hash}."
    )


def media_content_type(path) -> str:
    return (
        mimetypes.guess_type(Path(path).name)[0]
        or "application/octet-stream"
    )


# ---------------------------------------------------------------------
# Cross-project reuse (audit §2.5): import into a project's uploads
# ---------------------------------------------------------------------


def _choose_source(item: dict):
    """The usage or sighting an import copies from: a file-backed
    usage that actually served (or was brought in) first, then any
    file-backed usage, then a file-backed upload sighting. Returns
    ("usage", usage) | ("sighting", sighting) | None."""
    file_usages = [u for u in item["usages"] if u["file_present"]]
    used = [u for u in file_usages if u["decision_type"] in USED_DECISION_TYPES]
    for pool in (used, file_usages):
        if pool:
            return "usage", sorted(
                pool,
                key=lambda u: (u["created_at"] or "", u["decision_id"]),
                reverse=True,
            )[0]
    for sighting in item["sightings"]:
        if sighting["file_path"]:
            return "sighting", sighting
    return None


def import_asset(db, principal, content_hash: str, target_project_id: str) -> dict:
    """Copy a library asset into another project's media pool.

    The copy lands in the target's uploads (the editor's own
    attach machinery), with an index entry carrying the source
    decision's rights + verification verbatim and a
    ``library_provenance`` block; when the target has an editor
    context, a ``library_reuse`` ledger decision is appended to
    the target's current revision with ``previous_decision_id``
    pointing at the source decision, so the target's per-revision
    certificate records the reuse and its provenance. In the
    pre-run plan-review context no ledger row is written — exactly
    as editor.store_upload behaves there, for the same D3 reason.
    """
    target = _visible_project(db, principal, target_project_id)
    if target is None:
        raise LibraryError(
            404, "project_not_found", f"No project {target_project_id}."
        )
    index = _build_index(db, principal)
    item = index["items"].get(str(content_hash))
    if item is None:
        raise LibraryError(
            404, "asset_not_found", f"No library asset {content_hash}."
        )
    chosen = _choose_source(item)
    if chosen is None:
        raise LibraryError(
            422,
            "asset_file_missing",
            "The asset is recorded in the library but no copy of "
            "its media file survives on disk, so it cannot be "
            "imported.",
        )
    kind, source = chosen
    source_path = Path(source["file_path"])
    source_project_id = source["project_id"]
    if kind == "usage":
        source_decision_id = source["decision_id"]
        source_run_id = source["run_id"]
        source_upload_id = None
        provider = source["provider"] or "unknown"
        rights = source["rights"] if isinstance(source["rights"], dict) else None
        verification = source["verification"]
        evidence_class = source["evidence_class"]
        source_url = source["source_url"]
        user_supplied = source["decision_type"] == "user_supplied"
        generated = source["decision_type"] == "generated"
        scene_number = source["scene_number"]
        sentence_number = source["sentence_number"]
    else:
        source_decision_id = source.get("decision_id")
        source_run_id = None
        source_upload_id = source.get("upload_id")
        provider = "user_upload"
        rights = source.get("rights") if isinstance(source.get("rights"), dict) else None
        verification = None
        evidence_class = None
        source_url = None
        user_supplied = True
        generated = False
        scene_number = None
        sentence_number = None

    # The editor context, resolved exactly as editor.store_upload
    # resolves it (an existing revision, else revision 0 seeded
    # from the latest completed run, else the plan-review context,
    # else nothing to attach to).
    baseline_view = None
    baseline = editor.latest_revision(target_project_id)
    if baseline is not None:
        baseline_view = baseline[0]
    elif editor._latest_completed_run(target_project_id) is not None:
        baseline_view = editor.create_revision_zero(target_project_id)[0]
    elif editor._awaiting_approval_run(target_project_id) is None:
        raise LibraryError(
            404,
            "no_production_context",
            "This project has no plan awaiting approval and no "
            "completed production run, so there is nothing to "
            "attach an imported asset to.",
        )
    pre_run = baseline_view is None

    project_dir = media_paths.project_dir(target_project_id)
    uploads_dir = project_dir / editor.UPLOADS_DIRNAME
    uploads_dir.mkdir(parents=True, exist_ok=True)
    import_id = _new_id("import")
    safe_name = source_path.name.replace(" ", "_")
    dest = uploads_dir / f"{import_id}_{safe_name}"
    shutil.copyfile(source_path, dest)

    probed = editor._probe_any(dest)
    if probed is None:
        try:
            dest.unlink()
        except OSError:
            pass
        raise LibraryError(
            422,
            "asset_unprobeable",
            "The imported copy could not be probed as video, image "
            "or audio; it was not stored.",
        )
    media_type, probe = probed
    title, _recorded = _item_title(item)
    entry = {
        "upload_id": import_id,
        "file_path": str(dest),
        "file_name": dest.name,
        "type": media_type,
        "source": provider,
        "provider": provider,
        "source_url": source_url,
        "title": title,
        "clip_start": 0.0,
        "source_duration": probe.get("duration_seconds"),
        "decision_id": None,
        "evidence_class": evidence_class,
        "verification": verification,
        "rights": rights
        if rights is not None
        else {
            "license": "unknown",
            "license_url": "",
            "rights_status": "verify_before_publish",
            "creator": "",
            "source": provider,
            "retrieved_at": _now_iso(),
        },
        "user_supplied": user_supplied,
        "generated": generated,
        "size_bytes": dest.stat().st_size,
        "library_provenance": {
            "content_hash": item["content_hash"],
            "source_project_id": source_project_id,
            "source_run_id": source_run_id,
            "source_decision_id": source_decision_id,
            "source_upload_id": source_upload_id,
            "imported_at": _now_iso(),
        },
    }
    if pre_run:
        entry["pre_run"] = True
    else:
        entry["decision_id"] = run_ledger.record_editor_decision(
            baseline_view["run_id"],
            target_project_id,
            revision_id=baseline_view["revision_id"],
            decision_type="library_reuse",
            asset=entry,
            scene_number=scene_number,
            sentence_number=sentence_number,
            previous_decision_id=source_decision_id,
        )
    uploads = editor._uploads_index(project_dir)
    uploads.append(entry)
    editor._write_uploads_index(project_dir, uploads)
    return {
        "import": entry,
        "content_hash": item["content_hash"],
        "target_project_id": str(target_project_id),
        "revision_id": baseline_view["revision_id"] if baseline_view else None,
        "pre_run": pre_run,
    }


def library_upload(db, principal, project_id: str, filename, fileobj) -> dict:
    """Upload once into the library: the bytes are stored by the
    editor's own upload machinery in the chosen home project
    (probed, capped, indexed, ledger-recorded as user_supplied
    there), which makes the file a library item immediately —
    importable into every other project from there. The response
    adds the item's content hash so the caller can address it as a
    library asset."""
    project = _visible_project(db, principal, project_id)
    if project is None:
        raise LibraryError(
            404, "project_not_found", f"No project {project_id}."
        )
    stored = editor.store_upload(project_id, filename, fileobj)
    digest = _hash_file(stored.get("file_path"))
    return {"upload": stored, "content_hash": digest}
