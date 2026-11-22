"""Stage-level resume (Transformation Phase 5.1 — PERF-001, decided
under the takeover mandate; AM-020 folded in).

The pipeline has always CHECKPOINTED at every stage boundary
(jobs.save_production_state writes production_state.json into the
run's artefact directory), but nothing consumed the checkpoints: a
failed or VM-killed run re-ran every stage from zero (PERF-001
measured 5/6 stages redone, twice, on one production).

This module is the consumer. When a stage completes, jobs records —
into that stage's record in production_state.json — the stage's
resume payload (a JSON file under ``runs/<run_id>/resume/`` holding
everything the stage's downstream consumers need from it) and the
sha256 of every artefact file the stage's adoption depends on. When
a NEW run of the same project carries the identical script (full
sha256, compared from the run manifests) and an equal options
snapshot, it may ADOPT a prior run's completed stages, in order:

- a stage is adoptable only when every earlier stage was adopted,
  the prior stage record says "completed", a resume payload was
  recorded, and every recorded artefact still exists and hashes to
  the recorded value;
- the first stage that fails validation stops the adoption chain —
  that stage and all later ones execute normally (a clean run from
  that boundary), and the fallback is recorded;
- a different script, different options, or no prior run means no
  adoption at all, recorded the same way.

Adoption is recorded in the new run's production state (each adopted
stage record carries ``adopted: true`` + ``adopted_from_run`` and
inherits the source stage's artefact manifest, so a third run can
adopt through the chain) and in the run's ``resume`` record (source
run, adopted vs executed stages, fallback reason when the chain
stopped early). The artefact hashes are the trust anchor: project-
level artefacts are shared between runs of a project, so an
intervening different production that overwrote them invalidates
adoption by hash mismatch — never silently.

AM-020 (registry persistence) is the asset stage's piece of this:
the visual registry's usage state rides in the asset_manager stage
payload (VisualRegistry.export_state / restore_state) and is
restored on adoption, so cross-scene reuse enforcement sees the
adopted run's usage exactly as if the stage had executed here.
"""

import hashlib
import json
from pathlib import Path

from services.atomic_write import write_json_atomic
from services.production_state import PRODUCTION_STAGES

RESUME_DIR_NAME = "resume"

#: Manifest option keys that describe HOW the run was submitted,
#: not WHAT it produces; they never block adoption.
_VOLATILE_OPTION_KEYS = frozenset({"entrypoint"})

#: Terminal manifest/state statuses a prior run may be adopted from.
#: A still-"running" prior run is never a source: its artefacts may
#: be mid-write by a live worker.
_SOURCE_STATUSES = frozenset({"failed", "cancelled", "completed"})


def sha256_file(path):
    """Streaming sha256 of a file, hex digest."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relpath(path, project_dir):
    """A path's identity inside the project tree (POSIX separators),
    or None when it lives outside the project directory."""
    try:
        return Path(path).resolve().relative_to(
            Path(project_dir).resolve()
        ).as_posix()
    except (ValueError, OSError):
        return None


def _media_paths_in_payload(payload):
    """File paths a stage payload references (asset stages): string
    values under path-like keys, anywhere in the structure."""
    found = []

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("path", "file_path", "local_path") and isinstance(
                    value, str
                ) and value:
                    found.append(value)
                else:
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)
    return found


def stage_dependency_paths(stage, project_dir, artefact_dir, payload):
    """The artefact files whose current bytes a stage's adoption
    depends on — the files downstream stages actually consume.
    Derived documents a later stage regenerates (plan.json,
    visual_plan.json v1) are deliberately absent: guarding them
    would make adoption fail on content the adopting run does not
    need. Files that do not exist are returned as-is; the recorder
    skips them and the validator treats recorded files only."""
    project_dir = Path(project_dir)
    artefact_dir = Path(artefact_dir)
    if stage == "voice_actor":
        paths = [
            project_dir / "narration.wav",
            project_dir / "captions.json",
            project_dir / "captions.srt",
            project_dir / "captions.vtt",
        ]
        parts_dir = project_dir / "narration_parts"
        if parts_dir.is_dir():
            paths.extend(sorted(parts_dir.iterdir()))
        return paths
    if stage == "script_analysis":
        return [project_dir / "case_bible.json"]
    if stage == "asset_manager":
        paths = []
        for raw in _media_paths_in_payload(payload):
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = project_dir / candidate
            paths.append(candidate)
        return paths
    if stage == "motion_designer":
        paths = [
            project_dir / "graphics_manifest.json",
            project_dir / "visual_plan.json",
        ]
        graphics_dir = project_dir / "motion_graphics"
        if graphics_dir.is_dir():
            paths.extend(
                sorted(p for p in graphics_dir.rglob("*") if p.is_file())
            )
        return paths
    if stage == "sound_designer":
        paths = [
            project_dir / "soundtrack.wav",
            project_dir / "soundtrack.json",
        ]
        if isinstance(payload, dict):
            reference = str(payload.get("soundtrack_reference_name") or "")
            if reference:
                paths.append(project_dir / reference)
        return paths
    if stage == "video_editor":
        return [
            artefact_dir / name
            for name in (
                "documentary_final_output.mp4",
                "documentary_delivery.mp4",
                "timeline.json",
                "asset_report.json",
                "rights_manifest.json",
                "visual_quality.json",
                "production_report.json",
                "delivery.json",
            )
        ]
    return []


def record_stage_completion(state, stage, project_dir, artefact_dir, payload):
    """Record a completed stage's resume evidence on the run state.

    Writes the payload JSON (strictly serialised — a payload that
    cannot be serialised is recorded as unavailable and the stage
    simply never adopts; the run itself is unaffected) and stamps
    the stage record with the sha256 of every dependency artefact
    that exists. Fail-soft by design: resume bookkeeping must never
    fail a production. Returns the stage record."""
    project_dir = Path(project_dir)
    artefact_dir = Path(artefact_dir)
    record = (state.get("stages") or {}).get(stage)
    if not isinstance(record, dict):
        return record
    try:
        payload_rel = None
        payload_error = None
        if payload is not None:
            try:
                json.dumps(payload)
            except (TypeError, ValueError) as error:
                payload_error = f"{type(error).__name__}: {error}"
            if payload_error is None:
                payload_path = (
                    artefact_dir / RESUME_DIR_NAME / f"{stage}.json"
                )
                write_json_atomic(payload_path, payload, ensure_ascii=False)
                payload_rel = _relpath(payload_path, project_dir)
        artefacts = {}
        for path in stage_dependency_paths(
            stage, project_dir, artefact_dir, payload
        ):
            try:
                if path.is_file():
                    rel = _relpath(path, project_dir)
                    if rel is not None and rel not in artefacts:
                        artefacts[rel] = sha256_file(path)
            except OSError:
                continue
        if payload_rel is not None:
            payload_path = project_dir / payload_rel
            if payload_path.is_file():
                artefacts[payload_rel] = sha256_file(payload_path)
        record["artefacts"] = artefacts
        record["resume_payload"] = payload_rel
        if payload_error is not None:
            record["resume_payload_error"] = payload_error
    except Exception as error:  # fail-soft: bookkeeping never fails a run
        record["resume_record_error"] = f"{type(error).__name__}: {error}"
    return record


def _options_equal(source_options, current_options):
    """Whether two manifest options snapshots describe the same
    production (volatile submission keys ignored)."""
    def _clean(options):
        return {
            key: value
            for key, value in (options or {}).items()
            if key not in _VOLATILE_OPTION_KEYS
        }

    return _clean(source_options) == _clean(current_options)


def _validate_stage(record, project_dir):
    """(ok, reason) for one source stage record: completed, payload
    recorded, and every recorded artefact present + hash-true."""
    if not isinstance(record, dict):
        return False, "no_stage_record"
    if record.get("status") != "completed":
        return False, f"stage_status_{record.get('status')}"
    if not record.get("resume_payload"):
        return False, "no_resume_payload"
    artefacts = record.get("artefacts")
    if not isinstance(artefacts, dict) or not artefacts:
        return False, "no_artefact_manifest"
    for rel, expected in artefacts.items():
        path = Path(project_dir) / rel
        if not path.is_file():
            return False, f"artefact_missing:{rel}"
        try:
            if sha256_file(path) != expected:
                return False, f"artefact_hash_mismatch:{rel}"
        except OSError as error:
            return False, f"artefact_unreadable:{rel}:{error}"
    return True, ""


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def find_resume_source(project_dir, current_run_id, script_sha256,
                       current_options):
    """The best adoption source for a new run: a prior run of the
    same project whose manifest carries the identical script hash
    and an equal options snapshot, in a terminal state, with the
    longest validatable stage prefix. Returns a plan dict or None
    (with the reason recorded on the plan's "fallback" when a
    candidate existed but could not be used)."""
    project_dir = Path(project_dir)
    runs_dir = project_dir / "runs"
    if not runs_dir.is_dir():
        return {"source_run_id": None, "stages": {},
                "fallback": "no_prior_runs"}
    best = None
    fallbacks = []
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir() or run_dir.name == str(current_run_id):
            continue
        manifest = _load_json(run_dir / "run_manifest.json")
        state = _load_json(run_dir / "production_state.json")
        if not isinstance(manifest, dict) or not isinstance(state, dict):
            continue
        if manifest.get("script_hash") != script_sha256:
            continue
        # Status authority is the ledger DB when it has the row:
        # worker-start reconciliation (run_ledger.reconcile_stale_runs)
        # marks VM-killed runs "failed" there, while a killed run's
        # manifest FILE still says "running" (it is only enriched at
        # a graceful finish). The files are the fallback.
        status = ""
        try:
            from services import run_ledger

            row = run_ledger.get_run(run_dir.name)
            if isinstance(row, dict) and row.get("status"):
                status = str(row["status"])
        except Exception:
            status = ""
        if not status:
            status = str(manifest.get("status") or state.get("status") or "")
        if status not in _SOURCE_STATUSES:
            fallbacks.append(f"{run_dir.name}:run_status_{status or 'unknown'}")
            continue
        if not _options_equal(manifest.get("options"), current_options):
            fallbacks.append(f"{run_dir.name}:options_mismatch")
            continue
        stages = {}
        stop_reason = ""
        for stage in PRODUCTION_STAGES:
            record = (state.get("stages") or {}).get(stage)
            ok, reason = _validate_stage(record, project_dir)
            if not ok:
                stop_reason = f"{stage}:{reason}"
                break
            stages[stage] = {
                "record": record,
                "payload_path": project_dir / str(record["resume_payload"]),
            }
        if stages:
            candidate = {
                "source_run_id": run_dir.name,
                "source_state": state,
                "stages": stages,
                "fallback": stop_reason or None,
            }
            if best is None or len(stages) > len(best["stages"]):
                best = candidate
        elif stop_reason:
            fallbacks.append(f"{run_dir.name}:{stop_reason}")
    if best is not None:
        return best
    return {
        "source_run_id": None,
        "stages": {},
        "fallback": "; ".join(fallbacks) if fallbacks else "no_matching_prior_run",
    }


def plan_resume(project_dir, current_run_id, script_sha256, current_options):
    """The adoption plan for a starting run + the resume record to
    stamp on its state/manifest. Never raises."""
    try:
        plan = find_resume_source(
            project_dir, current_run_id, script_sha256, current_options
        )
    except Exception as error:  # fail-soft: a planning error is a clean run
        plan = {
            "source_run_id": None,
            "stages": {},
            "fallback": f"planning_error:{type(error).__name__}:{error}",
        }
    plan["record"] = {
        "source_run_id": plan.get("source_run_id"),
        "adopted_stages": [],
        "executed_stages": [],
        "fallback": plan.get("fallback"),
    }
    return plan


def load_stage_payload(plan, stage):
    """The recorded payload dict for an adoptable stage, or None."""
    entry = (plan.get("stages") or {}).get(stage)
    if not entry:
        return None
    payload = _load_json(entry["payload_path"])
    return payload if isinstance(payload, dict) else None


def mark_stage_adopted(state, plan, stage):
    """Stamp an adopted stage on the NEW run's state: adopted flag,
    source run, and the source stage's artefact manifest + payload
    pointer (copied verbatim, so a later run can adopt through this
    one). Also extends the run's resume record."""
    record = (state.get("stages") or {}).get(stage)
    source = (plan.get("stages") or {}).get(stage) or {}
    source_record = source.get("record") or {}
    if isinstance(record, dict):
        record["adopted"] = True
        record["adopted_from_run"] = plan.get("source_run_id")
        if isinstance(source_record.get("artefacts"), dict):
            record["artefacts"] = dict(source_record["artefacts"])
        if source_record.get("resume_payload"):
            record["resume_payload"] = source_record["resume_payload"]
    resume_record = state.get("resume")
    if isinstance(resume_record, dict):
        adopted = resume_record.setdefault("adopted_stages", [])
        if stage not in adopted:
            adopted.append(stage)


def mark_stage_executed(state, stage):
    """Record an executed (not adopted) stage in the resume record."""
    resume_record = state.get("resume") if isinstance(state, dict) else None
    if isinstance(resume_record, dict):
        executed = resume_record.setdefault("executed_stages", [])
        if stage not in executed:
            executed.append(stage)
