"""Versioning (founder priority #9; gap-audit Phase G).

The gap audit's findings (doc 04 §3): timeline versioning already
exists in a strong form (the editor revision chain, §3.1 Complete);
what was Partial was everything around it —

- §3.2 script version history: every run records the full script's
  sha256 (``production_runs.script_hash``) but the project's current
  script is one overwritten field, and earlier script *texts* were
  never snapshotted as browsable objects. The audit's prescribed
  fix: "snapshot script text per run submission into a versions
  view (the data path already exists); restore = set project
  script to a recorded version."
- §3.3 project snapshots: no named bundle of script + plan +
  revision pointer existed, and nothing restored one.

This module is that completion, built ONLY over the recorded
histories — the run ledger, the revision chain, plan.json /
plan_decisions.json, and the QA assembly — plus one project
artefact of its own, ``project_versions.json`` (the
plan_decisions.json / research_studio.json pattern: user
annotations and snapshots are project artefacts, never a parallel
version store that could disagree with the records):

- SCRIPT SNAPSHOTS. The API records the exact script text into
  the store when a project is created and when a run is submitted
  (``record_script_snapshot``, called fail-soft from api_v1), and
  when a snapshot is taken or a script restored. A script version
  is a distinct script_hash across the project's runs; its text is
  available ONLY from a recorded snapshot or from the project's
  current script field when the hash matches it. Historical
  versions whose text was never snapshotted report
  ``text_state: "not_recorded"`` — the hash survives in the run
  records, the text does not, and no text is ever reconstructed
  (e.g. by joining analysed sentences, which would be a different
  document wearing the script's name).
- THE UNIFIED VIEW is an assembly: production runs (with their
  recorded options, execution times and QA verdicts), editor
  revisions (with the operations that created each one, read from
  the revision documents' own op logs), script versions, plan
  decisions, the current plan document and snapshots, in one
  chronological list. A run's "changes" compare its recorded
  script hash and options against the previous run's; a
  revision's changes are the op-log entries its parent does not
  carry. Nothing is summarised by a model and nothing is
  estimated.
- COMPARE diffs only what is genuinely recorded for BOTH sides:
  script texts (a real unified diff) when both texts survive;
  revision op chains; run fields/options/verdicts; snapshot
  components (including a sentence-level plan diff when both plan
  documents are recorded). Any other pairing is presented
  side-by-side with the diff marked not_recorded and the reason
  stated — never a reconstructed diff.
- SNAPSHOTS capture, at creation time: the current script (text
  snapshotted into the store), the plan document (plan.json
  verbatim — the exact file text plus its sha256 — or
  not_recorded when no plan document exists), and the revision
  pointer (the chain's latest revision id/number/document hash,
  or not_recorded). RESTORE applies the recorded components
  through the existing machinery only: the project script field
  is set to the recorded text (the §3.2 restore), plan.json is
  written back byte-for-byte through the atomic writer, and the
  timeline is restored by BRANCHING the pointed revision through
  editor.create_revision — the original chain is never mutated,
  and the branch can be rendered through the existing render
  endpoint (the Phase G acceptance: restore a project to a
  snapshot and re-render; original chain untouched). What a
  snapshot does NOT contain is stated on every restore: the
  story-analysis cache, plan decisions recorded after the
  snapshot (append-only history), and production runs/artefacts.
- LABELS (a user's name + note on any timeline entry) live in the
  same store, keyed by entry id, owner-scoped by the API's
  project choke point like everything else here.

Every failure is data or a VersioningError with the v1 shape.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from models import Project
from services import editor, media_paths, plan_document, qa_report, run_ledger
from services.atomic_write import write_json_atomic, write_text_atomic

SCHEMA = "cinemora.project_versions/1"
STORE_FILENAME = "project_versions.json"

RECORDED = "recorded"
NOT_RECORDED = "not_recorded"
UNAVAILABLE = "unavailable"

# A unified diff longer than this is truncated and SAYS so.
MAX_DIFF_LINES = 500


class VersioningError(Exception):
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


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _project_dir(project_id) -> Path:
    return media_paths.project_dir(project_id)


def _session():
    from database import SessionLocal

    return SessionLocal()


# ---------------------------------------------------------------------
# Store (the project_versions.json project artefact)
# ---------------------------------------------------------------------


def _empty_store() -> dict:
    return {
        "version": 1,
        "script_snapshots": [],
        "labels": {},
        "snapshots": [],
        "restores": [],
    }


def load_store(project_id) -> dict:
    """The versioning store for a project; an empty store when the
    project predates the feature (absence is not an error — the
    view then derives everything from the run/revision records
    and the current script field alone)."""
    path = _project_dir(project_id) / STORE_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    store = _empty_store()
    for key in ("script_snapshots", "snapshots", "restores"):
        if isinstance(data.get(key), list):
            store[key] = data[key]
    if isinstance(data.get("labels"), dict):
        store["labels"] = data["labels"]
    store["version"] = data.get("version") or 1
    return store


def save_store(project_id, store: dict) -> dict:
    project_dir = _project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(project_dir / STORE_FILENAME, store)
    return store


# ---------------------------------------------------------------------
# Script snapshots — the §3.2 data path (text recorded at submission)
# ---------------------------------------------------------------------


def _snapshot_record(script_text: str, source: str, actor) -> dict:
    return {
        "script_hash": run_ledger.script_sha256(script_text),
        "text": script_text,
        "source": source,
        "recorded_at": _now_iso(),
        "recorded_by": actor,
    }


def record_script_snapshot(project_id, script_text, source: str,
                           actor=None, store: dict | None = None) -> dict:
    """Record the exact text of a script version into the store
    (idempotent per hash: the first recording stands, because it is
    the earliest evidence of the text). Returns the record."""
    if script_text is None:
        return None
    own_store = store is None
    if own_store:
        store = load_store(project_id)
    digest = run_ledger.script_sha256(script_text)
    for existing in store["script_snapshots"]:
        if existing.get("script_hash") == digest:
            return existing
    record = _snapshot_record(script_text, source, actor)
    store["script_snapshots"].append(record)
    if own_store:
        save_store(project_id, store)
    return record


def try_record_script_snapshot(project_id, script_text, source: str,
                               actor=None) -> None:
    """Fail-soft wrapper for the API hooks (project creation, run
    submission): snapshotting must never break the operation that
    triggered it. A failure here only means the version's text
    state stays not_recorded until a later recording succeeds."""
    try:
        record_script_snapshot(project_id, script_text, source, actor=actor)
    except Exception:
        pass


def _recorded_script_texts(project_id, store: dict, current_script) -> dict:
    """script_hash -> {"text", "source", "recorded_at"} for every
    script text that genuinely survives: the store's snapshots,
    plus the project's CURRENT script field under its own hash
    (the live field is itself a recorded text). Snapshot sources
    win over the current-field label for the same hash — the text
    is identical either way (same hash), only the provenance
    label differs, and the snapshot's is the earlier record."""
    texts: dict[str, dict] = {}
    for snap in store["script_snapshots"]:
        digest = snap.get("script_hash")
        if digest and isinstance(snap.get("text"), str):
            texts.setdefault(
                digest,
                {
                    "text": snap["text"],
                    "source": snap.get("source"),
                    "recorded_at": snap.get("recorded_at"),
                },
            )
    if isinstance(current_script, str):
        digest = run_ledger.script_sha256(current_script)
        if digest not in texts:
            texts[digest] = {
                "text": current_script,
                "source": "current_project_script",
                "recorded_at": None,
            }
    return texts


# ---------------------------------------------------------------------
# Script versions (derived from the run ledger + recorded texts)
# ---------------------------------------------------------------------


def _script_versions(store: dict, project: dict, runs: list[dict]) -> list[dict]:
    texts = _recorded_script_texts(project["id"], store, project.get("script"))
    current_hash = (
        run_ledger.script_sha256(project["script"])
        if isinstance(project.get("script"), str)
        else None
    )
    by_hash: dict[str, dict] = {}
    for run in runs:
        digest = run.get("script_hash")
        if not digest:
            continue
        version = by_hash.setdefault(
            digest,
            {
                "script_hash": digest,
                "run_ids": [],
                "first_submitted_at": None,
                "last_submitted_at": None,
            },
        )
        version["run_ids"].append(run.get("run_id"))
        created = _iso(run.get("created_at"))
        if created:
            if version["first_submitted_at"] is None or created < version["first_submitted_at"]:
                version["first_submitted_at"] = created
            if version["last_submitted_at"] is None or created > version["last_submitted_at"]:
                version["last_submitted_at"] = created
    # Texts recorded without any run (a created-but-never-run
    # script, a restored script) are versions too.
    for digest, info in texts.items():
        if digest not in by_hash:
            by_hash[digest] = {
                "script_hash": digest,
                "run_ids": [],
                "first_submitted_at": None,
                "last_submitted_at": None,
            }
    versions = []
    for digest, version in by_hash.items():
        info = texts.get(digest)
        version["run_count"] = len(version["run_ids"])
        version["is_current"] = digest == current_hash
        version["text_state"] = RECORDED if info else NOT_RECORDED
        version["text_source"] = info["source"] if info else None
        version["char_count"] = len(info["text"]) if info else None
        version["first_seen_at"] = (
            version["first_submitted_at"]
            or (info["recorded_at"] if info else None)
        )
        versions.append(version)
    versions.sort(key=lambda v: (v["first_seen_at"] is None, v["first_seen_at"] or ""))
    return versions


def _text_diff(a_text: str, b_text: str) -> dict:
    """A real unified diff of two recorded texts (capped, and the
    cap is reported rather than silently applied)."""
    a_lines = a_text.splitlines()
    b_lines = b_text.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            a_lines, b_lines, fromfile="a", tofile="b", lineterm=""
        )
    )
    added = sum(
        1 for line in diff_lines
        if line.startswith("+") and not line.startswith("+++")
    )
    removed = sum(
        1 for line in diff_lines
        if line.startswith("-") and not line.startswith("---")
    )
    truncated = len(diff_lines) > MAX_DIFF_LINES
    return {
        "state": RECORDED,
        "added_lines": added,
        "removed_lines": removed,
        "diff": "\n".join(diff_lines[:MAX_DIFF_LINES]),
        "diff_truncated": truncated,
        "diff_line_count": len(diff_lines),
    }


def _text_diff_state(a_info, b_info) -> dict:
    """The diff section for two script texts that may not both
    survive: recorded only when BOTH texts are recorded."""
    if a_info and b_info:
        return _text_diff(a_info["text"], b_info["text"])
    missing = []
    if not a_info:
        missing.append("a")
    if not b_info:
        missing.append("b")
    return {
        "state": NOT_RECORDED,
        "reason": (
            "The script text was not recorded for side(s) "
            + ", ".join(missing)
            + " — only the hash survives in the run records, and a "
            "diff is never reconstructed from derived artefacts."
        ),
        "added_lines": None,
        "removed_lines": None,
        "diff": None,
        "diff_truncated": False,
        "diff_line_count": None,
    }


# ---------------------------------------------------------------------
# Timeline entries — one assembly over the recorded histories
# ---------------------------------------------------------------------


def _run_verdicts(project_id, run: dict) -> dict:
    """The run's QA verdicts via the Phase F assembly over the
    run's own recorded artefacts (never recomputed here)."""
    try:
        report = qa_report.build_qa_report(project_id, run)
    except Exception:
        return {
            "state": UNAVAILABLE,
            "integrity": None,
            "quality": None,
            "reliability": None,
            "delivery_state": UNAVAILABLE,
            "delivery_status": None,
        }
    verdicts = report["verdicts"].get("verdicts") or {}
    return {
        "state": report["verdicts"]["state"],
        "integrity": (verdicts.get("integrity") or {}).get("verdict"),
        "quality": (verdicts.get("quality") or {}).get("verdict"),
        "reliability": (verdicts.get("reliability") or {}).get("verdict"),
        "delivery_state": report["delivery"]["state"],
        "delivery_status": report["delivery"].get("status"),
    }


def _options_changed(a_options, b_options):
    """Keys whose recorded values differ between two runs' option
    snapshots; None when neither side recorded any options (there
    is then nothing recorded to compare)."""
    a = a_options or {}
    b = b_options or {}
    if not a and not b:
        return None
    return sorted(
        key for key in set(a) | set(b) if a.get(key) != b.get(key)
    )


def _run_entries(project_id, runs: list[dict]) -> list[dict]:
    """Run entries in chronological order, each carrying its
    changes vs the previous run FROM RECORDED DATA: the script
    hash and the options snapshot are the run's own records."""
    entries = []
    previous = None
    for run in sorted(runs, key=lambda r: _iso(r.get("created_at")) or ""):
        verdicts = _run_verdicts(project_id, run)
        execution = run.get("execution") or {}
        changes = None
        if previous is not None:
            script_changed = None
            if run.get("script_hash") and previous.get("script_hash"):
                script_changed = run["script_hash"] != previous["script_hash"]
            changes = {
                "vs_entry_id": f"run:{previous['run_id']}",
                "script_changed": script_changed,
                "options_changed": _options_changed(
                    previous.get("options"), run.get("options")
                ),
            }
        entries.append(
            {
                "entry_id": f"run:{run['run_id']}",
                "kind": "run",
                "at": _iso(run.get("created_at")),
                "title": (
                    f"Production run {str(run['run_id'])[:8]}… — "
                    f"{run.get('status')}"
                ),
                "detail": {
                    "run_id": run.get("run_id"),
                    "status": run.get("status"),
                    "stage": run.get("stage"),
                    "script_hash": run.get("script_hash"),
                    "error": run.get("error"),
                    "started_at": _iso(run.get("started_at")),
                    "finished_at": _iso(run.get("finished_at")),
                    "options": run.get("options"),
                    "execution": {
                        "wall_time_seconds": execution.get("wall_time_seconds"),
                        "stage_durations_seconds": execution.get(
                            "stage_durations_seconds"
                        ),
                    },
                    "verdicts": verdicts,
                },
                "changes": changes,
                "_payload": {"run": run, "verdicts": verdicts},
            }
        )
        previous = run
    return entries


def _op_summaries(ops: list) -> list[dict]:
    return [
        {
            "op_id": op.get("op_id"),
            "type": op.get("type"),
            "actor": op.get("actor"),
            "at": op.get("at"),
            "summary": op.get("summary"),
        }
        for op in ops
        if isinstance(op, dict)
    ]


def _revision_entries(project_id) -> list[dict]:
    """Revision entries: the chain's own rows, with the operations
    that created each node read from the revision documents' op
    logs (a node's creating ops are exactly the op-log entries its
    parent's log does not carry — the log is cumulative by
    construction in services/editor.py)."""
    try:
        views = editor.list_revisions(project_id)
    except Exception:
        return []
    docs: dict[str, dict] = {}
    for view in views:
        try:
            _view, doc = editor.get_revision(project_id, view["revision_id"])
            docs[view["revision_id"]] = doc
        except Exception:
            docs[view["revision_id"]] = {}
    entries = []
    for view in views:
        doc = docs.get(view["revision_id"]) or {}
        op_log = doc.get("op_log") or []
        parent_id = view.get("parent_revision_id")
        if parent_id and parent_id in docs:
            parent_op_ids = {
                op.get("op_id")
                for op in (docs[parent_id].get("op_log") or [])
                if isinstance(op, dict)
            }
            creating_ops = [
                op for op in op_log
                if isinstance(op, dict) and op.get("op_id") not in parent_op_ids
            ]
        else:
            creating_ops = [op for op in op_log if isinstance(op, dict)]
        entries.append(
            {
                "entry_id": f"revision:{view['revision_id']}",
                "kind": "revision",
                "at": _iso(view.get("created_at")),
                "title": (
                    f"Editor revision {view.get('revision_number')} — "
                    f"{view.get('status')}"
                ),
                "detail": {
                    "revision_id": view.get("revision_id"),
                    "revision_number": view.get("revision_number"),
                    "parent_revision_id": parent_id,
                    "run_id": view.get("run_id"),
                    "status": view.get("status"),
                    "document_hash": view.get("document_hash"),
                    "created_from": doc.get("created_from"),
                    "duration_seconds": doc.get("duration_seconds"),
                    "visual_items": len(
                        (doc.get("tracks") or {}).get("visual") or []
                    ),
                    "export_count": len(doc.get("exports") or []),
                },
                "changes": {
                    "vs_entry_id": (
                        f"revision:{parent_id}" if parent_id else None
                    ),
                    "operations_applied": _op_summaries(creating_ops),
                },
                "_payload": {"view": view, "doc": doc},
            }
        )
    return entries


def _plan_entries(project_id, project_dir: Path) -> list[dict]:
    entries = []
    for decision in plan_document.load_plan_decisions(project_dir):
        if not isinstance(decision, dict):
            continue
        entries.append(
            {
                "entry_id": (
                    f"plan_decision:{decision.get('recorded_at')}:"
                    f"{decision.get('sentence_number')}:{decision.get('action')}"
                ),
                "kind": "plan_decision",
                "at": decision.get("recorded_at"),
                "title": (
                    f"Plan decision — {decision.get('action')} on "
                    f"sentence {decision.get('sentence_number')}"
                ),
                "detail": {"decision": decision},
                "changes": None,
                "_payload": {"decision": decision},
            }
        )
    try:
        plan = plan_document.build_plan(project_id, project_dir)
    except Exception:
        plan = None
    if isinstance(plan, dict):
        bible = plan.get("bible") or {}
        entries.append(
            {
                "entry_id": "plan:current",
                "kind": "plan",
                "at": plan.get("generated_at"),
                "title": "Production plan (current)",
                "detail": {
                    "plan_source": plan.get("plan_source"),
                    "analysis_mode": plan.get("analysis_mode"),
                    "generated_at": plan.get("generated_at"),
                    "scene_count": len(plan.get("scenes") or []),
                    "sentence_count": len(plan.get("sentences") or []),
                    "bible_title": bible.get("title"),
                    "bible_story_type": bible.get("story_type"),
                },
                "changes": None,
                "_payload": {"document": plan},
            }
        )
    return entries


def _snapshot_summary(snapshot: dict) -> dict:
    plan = snapshot.get("plan") or {}
    revision = snapshot.get("revision") or {}
    return {
        "id": snapshot.get("id"),
        "name": snapshot.get("name"),
        "note": snapshot.get("note"),
        "created_at": snapshot.get("created_at"),
        "created_by": snapshot.get("created_by"),
        "script": {"script_hash": (snapshot.get("script") or {}).get("script_hash")},
        "plan": {
            "state": plan.get("state"),
            "sha256": plan.get("sha256"),
            "generated_at": plan.get("generated_at"),
            "decisions_recorded": plan.get("decisions_recorded"),
        },
        "revision": {
            "state": revision.get("state"),
            "revision_id": revision.get("revision_id"),
            "revision_number": revision.get("revision_number"),
            "document_hash": revision.get("document_hash"),
            "run_id": revision.get("run_id"),
            "status": revision.get("status"),
        },
    }


def _snapshot_entries(store: dict) -> list[dict]:
    return [
        {
            "entry_id": f"snapshot:{snapshot['id']}",
            "kind": "snapshot",
            "at": snapshot.get("created_at"),
            "title": f"Snapshot — {snapshot.get('name')}",
            "detail": _snapshot_summary(snapshot),
            "changes": None,
            "_payload": {"snapshot": snapshot},
        }
        for snapshot in store["snapshots"]
    ]


_KIND_ORDER = {
    "script": 0,
    "plan": 1,
    "plan_decision": 2,
    "run": 3,
    "revision": 4,
    "snapshot": 5,
}


def _build_entries(project_id, project: dict, store: dict):
    """All timeline entries with their private payloads (the
    payload is stripped before the view is served; compare and
    label validation resolve entries from the same assembly, so
    an id that can be labelled is exactly an id that exists)."""
    runs = run_ledger.list_runs_for(project_id)
    versions = _script_versions(store, project, runs)
    # The script-entry change diffs resolve texts from the store
    # snapshots AND the current field (the full resolver).
    entries: list[dict] = []
    entries.extend(_run_entries(project_id, runs))
    entries.extend(_revision_entries(project_id))
    entries.extend(_script_entries_with_texts(store, project, versions))
    entries.extend(_plan_entries(project_id, _project_dir(project_id)))
    entries.extend(_snapshot_entries(store))
    labels = store["labels"]
    for entry in entries:
        entry["label"] = labels.get(entry["entry_id"])
    entries.sort(
        key=lambda e: (
            e["at"] is None,
            e["at"] or "",
            _KIND_ORDER.get(e["kind"], 99),
            e["entry_id"],
        )
    )
    return entries, versions


def _script_entries_with_texts(store: dict, project: dict,
                               versions: list[dict]) -> list[dict]:
    """Script entries whose change diffs use the full text
    resolver (store snapshots + the current script field)."""
    texts = _recorded_script_texts(project["id"], store, project.get("script"))
    entries = []
    previous = None
    for version in versions:
        changes = None
        if previous is not None:
            changes = {
                "vs_entry_id": f"script:{previous['script_hash']}",
                "text_diff": _text_diff_state(
                    texts.get(previous["script_hash"]),
                    texts.get(version["script_hash"]),
                ),
            }
        entries.append(
            {
                "entry_id": f"script:{version['script_hash']}",
                "kind": "script",
                "at": version["first_seen_at"],
                "title": (
                    f"Script version {version['script_hash'][:12]}…"
                    + (" (current)" if version["is_current"] else "")
                ),
                "detail": {
                    "script_hash": version["script_hash"],
                    "is_current": version["is_current"],
                    "run_ids": version["run_ids"],
                    "run_count": len(version["run_ids"]),
                    "first_submitted_at": version["first_submitted_at"],
                    "last_submitted_at": version["last_submitted_at"],
                    "text_state": version["text_state"],
                    "text_source": version["text_source"],
                    "char_count": version["char_count"],
                },
                "changes": changes,
                "_payload": {"version": version},
            }
        )
        previous = version
    return entries


def _public_entry(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if key != "_payload"}


def build_versions_view(project_id, project: dict) -> dict:
    """The unified Versions view for one project: the timeline
    entries, the script versions, the snapshots and the recorded
    restore events — all assembled from the recorded histories."""
    store = load_store(project_id)
    entries, versions = _build_entries(project_id, project, store)
    return {
        "schema": SCHEMA,
        "project_id": str(project_id),
        "current_script_hash": (
            run_ledger.script_sha256(project["script"])
            if isinstance(project.get("script"), str)
            else None
        ),
        "entries": [_public_entry(entry) for entry in entries],
        "script_versions": versions,
        "snapshots": [_snapshot_summary(s) for s in store["snapshots"]],
        "restores": store["restores"],
    }


def get_script_version(project_id, project: dict, script_hash: str) -> dict:
    """One script version in full, including its text when the
    text is recorded. An unknown hash is a 404: a version exists
    only where a run, a snapshot or the current field records it."""
    store = load_store(project_id)
    runs = run_ledger.list_runs_for(project_id)
    versions = _script_versions(store, project, runs)
    for version in versions:
        if version["script_hash"] == script_hash:
            texts = _recorded_script_texts(
                project_id, store, project.get("script")
            )
            info = texts.get(script_hash)
            return {
                **version,
                "project_id": str(project_id),
                "text": info["text"] if info else None,
            }
    raise VersioningError(
        404,
        "script_version_not_found",
        f"No script version {script_hash} is recorded for this project.",
    )


# ---------------------------------------------------------------------
# Compare — diffs only where BOTH sides genuinely record the basis
# ---------------------------------------------------------------------


def _resolve_entry(project_id, project: dict, store: dict, entry_id: str):
    entries, _versions = _build_entries(project_id, project, store)
    for entry in entries:
        if entry["entry_id"] == entry_id:
            return entry
    raise VersioningError(
        404,
        "version_entry_not_found",
        f"No version entry {entry_id} exists for this project.",
    )


def _side_summary(project_id, project: dict, store: dict, entry: dict) -> dict:
    """The compare side: the timeline entry plus the recorded
    payload facts a diff is computed from (never the raw store)."""
    kind = entry["kind"]
    payload = entry.get("_payload") or {}
    side = {"entry": _public_entry(entry)}
    if kind == "script":
        texts = _recorded_script_texts(project_id, store, project.get("script"))
        info = texts.get(entry["detail"]["script_hash"])
        side["script_text"] = info["text"] if info else None
    elif kind == "revision":
        side["doc"] = payload.get("doc") or {}
    elif kind == "run":
        side["run"] = payload.get("run") or {}
    elif kind == "snapshot":
        snapshot = payload.get("snapshot") or {}
        texts = _recorded_script_texts(project_id, store, project.get("script"))
        info = texts.get((snapshot.get("script") or {}).get("script_hash"))
        side["script_text"] = info["text"] if info else None
        plan = snapshot.get("plan") or {}
        side["plan_document"] = (
            _parse_plan_text(plan.get("text"))
            if plan.get("state") == RECORDED
            else None
        )
    elif kind == "plan":
        side["plan_document"] = payload.get("document")
    return side


def _parse_plan_text(text):
    if not isinstance(text, str):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _plan_doc_diff(a_doc, b_doc) -> dict:
    """Sentence-level diff of two recorded plan documents: counts,
    bible fields, and per-sentence text changes keyed by the
    recorded sentence numbers. A document whose sentences carry
    no text yields a not_recorded text section — counts still
    compare, because counts are recorded either way."""
    if not isinstance(a_doc, dict) or not isinstance(b_doc, dict):
        missing = "a" if not isinstance(a_doc, dict) else "b"
        return {
            "state": NOT_RECORDED,
            "reason": (
                f"No plan document is recorded for side {missing}."
            ),
            "scene_count": None,
            "sentence_count": None,
            "sentence_text_changes": None,
        }

    def sentences_by_number(doc):
        result = {}
        for row in doc.get("sentences") or []:
            if isinstance(row, dict) and row.get("sentence_number") is not None:
                result[row["sentence_number"]] = row.get("text")
        return result

    a_sentences = sentences_by_number(a_doc)
    b_sentences = sentences_by_number(b_doc)
    text_changes = None
    only_a = None
    only_b = None
    if any(text is not None for text in a_sentences.values()) and any(
        text is not None for text in b_sentences.values()
    ):
        text_changes = [
            {
                "sentence_number": number,
                "a_text": a_sentences[number],
                "b_text": b_sentences[number],
            }
            for number in sorted(set(a_sentences) & set(b_sentences))
            if a_sentences[number] != b_sentences[number]
        ]
        only_a = sorted(set(a_sentences) - set(b_sentences))
        only_b = sorted(set(b_sentences) - set(a_sentences))
    a_bible = a_doc.get("bible") or {}
    b_bible = b_doc.get("bible") or {}
    return {
        "state": RECORDED,
        "scene_count": {
            "a": len(a_doc.get("scenes") or []),
            "b": len(b_doc.get("scenes") or []),
        },
        "sentence_count": {
            "a": len(a_doc.get("sentences") or []),
            "b": len(b_doc.get("sentences") or []),
        },
        "bible": {
            "title": {"a": a_bible.get("title"), "b": b_bible.get("title")},
            "story_type": {
                "a": a_bible.get("story_type"),
                "b": b_bible.get("story_type"),
            },
        },
        "sentence_text_changes": text_changes,
        "sentences_only_in_a": only_a,
        "sentences_only_in_b": only_b,
    }


def _revision_diff(project_id, a_entry, a_side, b_entry, b_side) -> dict:
    a_doc = a_side.get("doc") or {}
    b_doc = b_side.get("doc") or {}
    a_ops = {
        op.get("op_id"): op
        for op in (a_doc.get("op_log") or [])
        if isinstance(op, dict)
    }
    b_ops = {
        op.get("op_id"): op
        for op in (b_doc.get("op_log") or [])
        if isinstance(op, dict)
    }
    chains = {}
    for label, entry in (("a", a_entry), ("b", b_entry)):
        try:
            chains[label] = editor.revision_chain_ids(
                project_id, entry["detail"]["revision_id"]
            )
        except Exception:
            chains[label] = []
    common = [rid for rid in chains["a"] if rid in set(chains["b"])]
    return {
        "kind": "revision_ops",
        "state": RECORDED,
        "common_ancestor_revision_id": common[-1] if common else None,
        "document_hash": {
            "a": a_entry["detail"].get("document_hash"),
            "b": b_entry["detail"].get("document_hash"),
            "equal": (
                a_entry["detail"].get("document_hash")
                == b_entry["detail"].get("document_hash")
            ),
        },
        "ops_only_in_a": _op_summaries(
            [op for op_id, op in a_ops.items() if op_id not in b_ops]
        ),
        "ops_only_in_b": _op_summaries(
            [op for op_id, op in b_ops.items() if op_id not in a_ops]
        ),
    }


def _run_diff(a_entry, b_entry) -> dict:
    a = a_entry["detail"]
    b = b_entry["detail"]
    fields = {}
    for key in ("status", "stage", "script_hash", "error"):
        fields[key] = {"a": a.get(key), "b": b.get(key)}
    return {
        "kind": "run_fields",
        "state": RECORDED,
        "fields": fields,
        "options_changed": _options_changed(a.get("options"), b.get("options")),
        "options": {"a": a.get("options"), "b": b.get("options")},
        "execution": {"a": a.get("execution"), "b": b.get("execution")},
        "verdicts": {"a": a.get("verdicts"), "b": b.get("verdicts")},
    }


def _snapshot_diff(a_entry, a_side, b_entry, b_side) -> dict:
    a_snap = (a_entry.get("_payload") or {}).get("snapshot") or {}
    b_snap = (b_entry.get("_payload") or {}).get("snapshot") or {}
    a_hash = (a_snap.get("script") or {}).get("script_hash")
    b_hash = (b_snap.get("script") or {}).get("script_hash")
    a_texts = (
        {"text": a_side["script_text"]} if a_side.get("script_text") is not None else None
    )
    b_texts = (
        {"text": b_side["script_text"]} if b_side.get("script_text") is not None else None
    )
    return {
        "kind": "snapshot_components",
        "state": RECORDED,
        "script": {
            "a_hash": a_hash,
            "b_hash": b_hash,
            "hash_equal": a_hash == b_hash and a_hash is not None,
            "text_diff": _text_diff_state(a_texts, b_texts),
        },
        "plan": _plan_doc_diff(
            a_side.get("plan_document"), b_side.get("plan_document")
        ),
        "revision": {
            "a": a_entry["detail"].get("revision"),
            "b": b_entry["detail"].get("revision"),
        },
    }


def compare_versions(project_id, project: dict, a_id: str, b_id: str) -> dict:
    """Compare two version entries. The diff is computed only for
    pairings that share a recorded basis (script/script,
    revision/revision, run/run, snapshot/snapshot, plan/snapshot);
    every other pairing returns both entries side-by-side with
    the diff marked not_recorded and the reason stated."""
    store = load_store(project_id)
    a_entry = _resolve_entry(project_id, project, store, a_id)
    b_entry = _resolve_entry(project_id, project, store, b_id)
    a_side = _side_summary(project_id, project, store, a_entry)
    b_side = _side_summary(project_id, project, store, b_entry)
    kinds = (a_entry["kind"], b_entry["kind"])

    if kinds == ("script", "script"):
        a_info = (
            {"text": a_side["script_text"]}
            if a_side.get("script_text") is not None
            else None
        )
        b_info = (
            {"text": b_side["script_text"]}
            if b_side.get("script_text") is not None
            else None
        )
        diff = {"kind": "script_text", **_text_diff_state(a_info, b_info)}
    elif kinds == ("revision", "revision"):
        diff = _revision_diff(project_id, a_entry, a_side, b_entry, b_side)
    elif kinds == ("run", "run"):
        diff = _run_diff(a_entry, b_entry)
    elif kinds == ("snapshot", "snapshot"):
        diff = _snapshot_diff(a_entry, a_side, b_entry, b_side)
    elif set(kinds) == {"plan", "snapshot"}:
        diff = {
            "kind": "plan_document",
            **_plan_doc_diff(
                a_side.get("plan_document"), b_side.get("plan_document")
            ),
        }
    else:
        diff = {
            "kind": "side_by_side",
            "state": NOT_RECORDED,
            "reason": (
                "These version kinds share no recorded basis for a "
                "diff; both entries are shown with their recorded "
                "fields only."
            ),
        }
        # A script-hash comparison IS recorded for any pair of
        # entries that both carry one — include it rather than
        # pretend the sides share nothing at all.
        a_hash = a_entry["detail"].get("script_hash") or (
            (a_entry["detail"].get("script") or {}).get("script_hash")
        )
        b_hash = b_entry["detail"].get("script_hash") or (
            (b_entry["detail"].get("script") or {}).get("script_hash")
        )
        if a_hash and b_hash:
            diff["script_hash"] = {
                "a": a_hash,
                "b": b_hash,
                "equal": a_hash == b_hash,
            }

    def public_side(side):
        return {
            key: value
            for key, value in side.items()
            if key not in ("script_text", "doc", "run", "plan_document")
        }

    return {
        "schema": SCHEMA,
        "project_id": str(project_id),
        "a": public_side(a_side),
        "b": public_side(b_side),
        "diff": diff,
    }


# ---------------------------------------------------------------------
# Snapshots — capture and restore (gap-audit §3.3, Phase G)
# ---------------------------------------------------------------------


def create_snapshot(project_id, project: dict, name: str, note,
                    actor=None) -> dict:
    """Capture a named bundle of the project's current recorded
    state: script (text snapshotted), plan document (verbatim),
    revision pointer (the chain's latest revision). Components
    that do not exist are recorded as not_recorded — a snapshot
    of a young project is honest about what it could not hold."""
    store = load_store(project_id)
    project_dir = _project_dir(project_id)

    script_text = project.get("script")
    script_record = {"script_hash": None}
    if isinstance(script_text, str):
        record = record_script_snapshot(
            project_id, script_text, "snapshot", actor=actor, store=store
        )
        script_record = {"script_hash": record["script_hash"]}

    decisions_count = len(plan_document.load_plan_decisions(project_dir))
    plan_path = project_dir / plan_document.PLAN_FILENAME
    if plan_path.is_file():
        try:
            plan_text = plan_path.read_text(encoding="utf-8")
            plan_doc = _parse_plan_text(plan_text)
            if plan_doc is None:
                raise ValueError("plan document is not a JSON object")
            plan_record = {
                "state": RECORDED,
                "text": plan_text,
                "sha256": hashlib.sha256(plan_text.encode("utf-8")).hexdigest(),
                "generated_at": plan_doc.get("generated_at"),
                "decisions_recorded": decisions_count,
            }
        except (OSError, ValueError):
            plan_record = {
                "state": UNAVAILABLE,
                "text": None,
                "sha256": None,
                "generated_at": None,
                "decisions_recorded": decisions_count,
            }
    else:
        plan_record = {
            "state": NOT_RECORDED,
            "text": None,
            "sha256": None,
            "generated_at": None,
            "decisions_recorded": decisions_count,
        }

    revision_record = {"state": NOT_RECORDED}
    try:
        latest = editor.latest_revision(project_id)
    except Exception:
        latest = None
    if latest is not None:
        view, _doc = latest
        revision_record = {
            "state": RECORDED,
            "revision_id": view.get("revision_id"),
            "revision_number": view.get("revision_number"),
            "document_hash": view.get("document_hash"),
            "run_id": view.get("run_id"),
            "status": view.get("status"),
        }

    snapshot = {
        "id": _new_id("snap"),
        "name": name,
        "note": note,
        "created_at": _now_iso(),
        "created_by": actor,
        "script": script_record,
        "plan": plan_record,
        "revision": revision_record,
    }
    store["snapshots"].append(snapshot)
    save_store(project_id, store)
    return _snapshot_summary(snapshot)


def _find_snapshot(store: dict, snapshot_id: str) -> dict:
    for snapshot in store["snapshots"]:
        if snapshot.get("id") == snapshot_id:
            return snapshot
    raise VersioningError(
        404, "snapshot_not_found", f"No snapshot {snapshot_id} for this project."
    )


def get_snapshot(project_id, snapshot_id: str) -> dict:
    """One snapshot in full: the summary plus its recorded plan
    document (parsed) when it holds one."""
    store = load_store(project_id)
    snapshot = _find_snapshot(store, snapshot_id)
    plan = snapshot.get("plan") or {}
    return {
        **_snapshot_summary(snapshot),
        "project_id": str(project_id),
        "plan_document": (
            _parse_plan_text(plan.get("text"))
            if plan.get("state") == RECORDED
            else None
        ),
    }


def delete_snapshot(project_id, snapshot_id: str) -> dict:
    """Remove a snapshot record. Only the snapshot record itself
    is deleted: the script snapshots it referenced stay (they are
    the script versions' recorded texts), and no run, revision or
    plan artefact is touched."""
    store = load_store(project_id)
    snapshot = _find_snapshot(store, snapshot_id)
    store["snapshots"] = [
        s for s in store["snapshots"] if s.get("id") != snapshot_id
    ]
    save_store(project_id, store)
    return {"deleted": snapshot_id, "name": snapshot.get("name")}


def _set_project_script(project_id, script_text: str) -> None:
    db = _session()
    try:
        row = db.get(Project, str(project_id))
        if row is None:
            raise VersioningError(
                404, "project_not_found", f"No project {project_id}."
            )
        row.script = script_text
        db.commit()
    finally:
        db.close()


def restore_snapshot(project_id, project: dict, snapshot_id: str,
                     actor=None) -> dict:
    """Restore a project to a snapshot, through the existing
    machinery only, in fail-first order (the branch — the step
    that can refuse — happens before anything is written):

    1. timeline: branch the snapshot's pointed revision through
       editor.create_revision (a NEW chain node; the original
       chain is untouched and the branch renders through the
       existing render endpoint);
    2. plan: write the snapshot's recorded plan.json text back
       byte-for-byte through the atomic writer;
    3. script: set the project script field to the recorded text.

    The outcome reports each component's state separately and
    names what a snapshot never holds (analysis cache, later
    plan decisions, runs) under not_restored."""
    store = load_store(project_id)
    snapshot = _find_snapshot(store, snapshot_id)
    outcome = {
        "snapshot_id": snapshot_id,
        "snapshot_name": snapshot.get("name"),
        "restored_at": _now_iso(),
        "restored_by": actor,
        "script": {"state": NOT_RECORDED},
        "plan": {"state": (snapshot.get("plan") or {}).get("state")},
        "revision": {"state": (snapshot.get("revision") or {}).get("state")},
        "not_restored": [
            "The story-analysis cache is not part of a snapshot; "
            "the next plan generation or production re-analyses "
            "the restored script.",
            "Plan decisions are append-only history and were not "
            "removed, including any recorded after this snapshot.",
            "Production runs and their artefacts are history and "
            "were not changed.",
        ],
    }

    revision_record = snapshot.get("revision") or {}
    new_revision_id = None
    if revision_record.get("state") == RECORDED:
        try:
            view, _doc = editor.create_revision(
                project_id,
                from_revision_id=revision_record["revision_id"],
                actor=actor or "operator",
            )
        except editor.EditorError as error:
            raise VersioningError(
                error.status_code, error.code, error.message
            ) from error
        new_revision_id = view["revision_id"]
        outcome["revision"] = {
            "state": "branched",
            "from_revision_id": revision_record["revision_id"],
            "new_revision_id": new_revision_id,
            "new_revision_number": view.get("revision_number"),
        }

    plan_record = snapshot.get("plan") or {}
    if plan_record.get("state") == RECORDED:
        plan_path = _project_dir(project_id) / plan_document.PLAN_FILENAME
        write_text_atomic(plan_path, plan_record["text"])
        outcome["plan"] = {
            "state": "restored",
            "sha256": plan_record.get("sha256"),
        }

    script_hash = (snapshot.get("script") or {}).get("script_hash")
    texts = _recorded_script_texts(project_id, store, project.get("script"))
    info = texts.get(script_hash) if script_hash else None
    if info is not None:
        _set_project_script(project_id, info["text"])
        outcome["script"] = {"state": "restored", "script_hash": script_hash}
    elif script_hash:
        outcome["script"] = {
            "state": NOT_RECORDED,
            "script_hash": script_hash,
            "reason": (
                "The snapshot's script text is not in the recorded "
                "snapshots, so the script was left unchanged."
            ),
        }

    store["restores"].append(
        {
            "kind": "snapshot",
            "snapshot_id": snapshot_id,
            "at": outcome["restored_at"],
            "by": actor,
            "new_revision_id": new_revision_id,
            "script_hash": script_hash,
        }
    )
    save_store(project_id, store)
    return outcome


def restore_script_version(project_id, project: dict, script_hash: str,
                           actor=None) -> dict:
    """The §3.2 restore: set the project script to a recorded
    script version's text. Refuses honestly when only the hash
    survives — there is then nothing truthful to restore."""
    store = load_store(project_id)
    runs = run_ledger.list_runs_for(project_id)
    versions = _script_versions(store, project, runs)
    known = {v["script_hash"] for v in versions}
    if script_hash not in known:
        raise VersioningError(
            404,
            "script_version_not_found",
            f"No script version {script_hash} is recorded for this project.",
        )
    texts = _recorded_script_texts(project_id, store, project.get("script"))
    info = texts.get(script_hash)
    if info is None:
        raise VersioningError(
            422,
            "script_text_not_recorded",
            "Only this version's hash survives in the run records; "
            "its text was never snapshotted (snapshotting began "
            "with the versioning feature), so there is no recorded "
            "text to restore.",
        )
    _set_project_script(project_id, info["text"])
    store["restores"].append(
        {
            "kind": "script_version",
            "snapshot_id": None,
            "at": _now_iso(),
            "by": actor,
            "new_revision_id": None,
            "script_hash": script_hash,
        }
    )
    save_store(project_id, store)
    return {
        "script_hash": script_hash,
        "restored_at": _now_iso(),
        "char_count": len(info["text"]),
        "text_source": info["source"],
    }


# ---------------------------------------------------------------------
# Labels — user annotations on timeline entries
# ---------------------------------------------------------------------


def set_label(project_id, project: dict, entry_id: str, name, note,
              actor=None) -> dict:
    """Set (or, with both fields null, clear) the label on one
    timeline entry. The entry must exist in the assembled view —
    a label can never attach to an id nothing records."""
    store = load_store(project_id)
    entries, _versions = _build_entries(project_id, project, store)
    if entry_id not in {entry["entry_id"] for entry in entries}:
        raise VersioningError(
            404,
            "version_entry_not_found",
            f"No version entry {entry_id} exists for this project.",
        )
    if name is None and note is None:
        store["labels"].pop(entry_id, None)
        save_store(project_id, store)
        return {"entry_id": entry_id, "label": None}
    label = {
        "name": name,
        "note": note,
        "updated_at": _now_iso(),
        "updated_by": actor,
    }
    store["labels"][entry_id] = label
    save_store(project_id, store)
    return {"entry_id": entry_id, "label": label}
