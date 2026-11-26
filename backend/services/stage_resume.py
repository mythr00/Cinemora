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
- the asset_manager stage must ALSO pass the C-01 coverage gate
  (Phase 3a, services.asset_coverage): every sentence the stage
  owed is covered, graphics-fallback-decided, or explicitly
  unresolved after an attempted search. A starved or unrecorded
  sentence refuses adoption and stops the chain — the retry
  re-runs asset_manager in full (the registry/segment state the
  stage owns is production-scoped, so a partial per-scene adoption
  would corrupt reuse accounting; see Phase 3a report);
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
import time
from pathlib import Path

from services.atomic_write import write_json_atomic
from services.production_state import PRODUCTION_STAGES

RESUME_DIR_NAME = "resume"

#: Manifest option keys that describe HOW the run was submitted,
#: not WHAT it produces; they never block adoption.
#: Option keys that never change what a production IS: submission
#: plumbing ("entrypoint") and ledger retry bookkeeping
#: ("retry_of"/"retry_attempt"/"retry_not_before" — the full set
#: the dispatcher's retry sweep stamps). A retry child MUST
#: compare options-equal to its parent or stage adoption is
#: refused (options_mismatch) and the retry re-runs every stage
#: from scratch — the exact failure the Phase 8.6 crash battery
#: caught live (2026-11-22; the first fix missed retry_not_before
#: and the battery's recovery line caught that too).
_VOLATILE_OPTION_KEYS = frozenset(
    {
        "entrypoint",
        "retry_of",
        "retry_attempt",
        "retry_not_before",
        # Phase 1 same-run resume bookkeeping: how often the source
        # run was requeued says nothing about WHAT it produces.
        "resume_count",
        "last_resume_at",
    }
)

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


def _validate_asset_coverage(record, project_dir):
    """C-01 coverage gate (Phase 3a): an asset_manager stage is
    adoptable only when its coverage is COMPLETE — every sentence
    it owed has an assigned asset, a recorded graphics fallback,
    or an explicit unresolved marker backed by an actually-attempted
    search (services.asset_coverage). Hash validation (integrity)
    stays in _validate_stage; this is the additional gate that
    stops a defective-but-intact stage — Petito attempt 7's, whose
    sentence 23 was starved by the shared download budget before
    its search ever ran — from being adopted as healthy."""
    payload = _load_json(Path(project_dir) / str(record.get("resume_payload") or ""))
    if not isinstance(payload, dict):
        return False, "coverage_payload_unreadable"
    processed = payload.get("processed_scenes")
    if not isinstance(processed, list):
        return False, "coverage_no_processed_scenes"
    try:
        from services import asset_coverage

        assessment = asset_coverage.assess_asset_coverage(processed)
    except Exception as error:  # fail closed: unassessable != complete
        return False, f"coverage_assessment_error:{type(error).__name__}"
    if not assessment["complete"]:
        numbers = ",".join(
            str(entry["sentence_number"])
            for entry in assessment["incomplete"]
        )
        return False, f"coverage_incomplete:{numbers}"
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
        # The ledger read gets a short retry: under concurrent
        # production the SQLite ledger can be momentarily locked,
        # and falling back to a killed source's file status
        # ("running" forever) would refuse a legitimate adoption
        # (Phase 8.6 battery find, 2026-11-23 — the same lock burst
        # that starved the child's claim).
        for _attempt in range(4):
            try:
                from services import run_ledger

                row = run_ledger.get_run(run_dir.name)
                if isinstance(row, dict) and row.get("status"):
                    status = str(row["status"])
                break
            except Exception:
                status = ""
                time.sleep(0.25)
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
            if ok and stage == "asset_manager":
                # C-01: integrity is not coverage — a hash-true
                # asset stage with starved/unrecorded sentences is
                # refused here, which also stops the adoption
                # chain, so no later stage (motion/video_editor)
                # can adopt over the gap.
                ok, reason = _validate_asset_coverage(record, project_dir)
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


def plan_self_resume(project_dir, current_run_id, prior_state):
    """Phase 1 durability: the resume plan a run builds from its
    OWN previous execution's stage records.

    When a heartbeat-stale run is requeued and claimed again (same
    run row — see run_ledger._requeue_stale_run), run_full_pipeline
    re-enters with the same run id. Its prior production_state —
    captured by the caller BEFORE the fresh state overwrites it —
    carries the stage records of the execution that died: each
    completed stage's resume payload pointer + artefact hashes.
    Those records are validated with exactly the cross-run rules
    (_validate_stage + the C-01 asset coverage gate): identity
    substitutes for the script/options equality and terminal-status
    checks (the claimant IS this run's owner, so the "source may
    be mid-write" concern does not apply), but artefact integrity
    is NOT waived — a stage whose files changed or vanished since
    the crash is re-executed, never trusted.

    Returns a plan dict shaped like find_resume_source's, with
    source_run_id == current_run_id and "self_resume": True, or a
    plan with empty stages when nothing validates. Never raises.
    """
    plan = {
        "source_run_id": None,
        "stages": {},
        "fallback": None,
        "self_resume": True,
    }
    try:
        if not isinstance(prior_state, dict) or not current_run_id:
            plan["fallback"] = "no_prior_state"
            return plan
        project_dir = Path(project_dir)
        prior_stages = prior_state.get("stages") or {}
        stages = {}
        stop_reason = ""
        for stage in PRODUCTION_STAGES:
            record = prior_stages.get(stage)
            ok, reason = _validate_stage(record, project_dir)
            if ok and stage == "asset_manager":
                ok, reason = _validate_asset_coverage(record, project_dir)
            if not ok:
                stop_reason = f"{stage}:{reason}"
                break
            stages[stage] = {
                "record": record,
                "payload_path": project_dir / str(record["resume_payload"]),
            }
        if stages:
            plan["source_run_id"] = str(current_run_id)
            plan["stages"] = stages
            plan["fallback"] = stop_reason or None
        else:
            plan["fallback"] = stop_reason or "no_validatable_stages"
        return plan
    except Exception as error:  # fail-soft: a planning error is a clean run
        plan["stages"] = {}
        plan["source_run_id"] = None
        plan["fallback"] = f"planning_error:{type(error).__name__}:{error}"
        return plan


# ---------------------------------------------------------------------
# Phase 1: asset-stage scene checkpoints
# ---------------------------------------------------------------------
# The stage payload above is written only when the asset stage
# COMPLETES. A worker that dies mid-stage (the Hyatt pattern: scene
# 9 of 10 after ~100 minutes) used to lose every scene it had
# processed, because jobs persisted nothing until the stage's end.
# The progress checkpoint below is the incremental counterpart: after
# each scene commits, jobs records the scenes committed so far plus
# the visual registry's export_state() — the same state the completed
# stage payload carries — so a same-run resume re-enters the stage
# with the registry restored and the committed scenes skipped.

ASSET_PROGRESS_NAME = "asset_manager.progress.json"


def asset_progress_path(artefact_dir):
    return Path(artefact_dir) / RESUME_DIR_NAME / ASSET_PROGRESS_NAME


def _selected_asset_paths(scene_entry):
    """The local media files a committed scene result actually
    selected (its top-level images/videos entries) — the files
    downstream stages consume. Candidate records nested deeper in
    the result also carry path-like keys (including candidates
    that were rejected or never downloaded), so they are NOT
    integrity evidence and are deliberately not collected."""
    found = []
    if not isinstance(scene_entry, dict):
        return found
    for kind in ("images", "videos"):
        for item in scene_entry.get(kind) or []:
            if not isinstance(item, dict):
                continue
            for key in ("path", "file_path", "local_path"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    found.append(value)
                    break
    return found


def record_asset_progress(artefact_dir, project_dir, *, script_hash,
                          processed_scenes, registry_state):
    """Persist the asset stage's incremental checkpoint (atomic
    write). Fail-soft by the module's contract: returns True when
    written, False on any problem — checkpointing must never fail
    a production.

    Integrity follows the module's trust pattern: every selected
    asset file that exists at record time is sha256-stamped into
    the checkpoint, and load_asset_progress re-verifies the
    stamps. Files absent at record time are not stamped (nothing
    can vouch for them) and do not block the checkpoint."""
    try:
        project_dir = Path(project_dir)
        scenes = []
        committed = []
        asset_hashes = {}
        for entry in processed_scenes or []:
            if not isinstance(entry, dict):
                continue
            scene = entry.get("scene") or {}
            number = scene.get("scene_number")
            if number is None:
                number = entry.get("scene_number")
            if number is None:
                continue
            committed.append(int(number))
            scenes.append(entry)
            for raw in _selected_asset_paths(entry):
                candidate = Path(raw)
                if not candidate.is_absolute():
                    candidate = project_dir / candidate
                try:
                    if candidate.is_file():
                        asset_hashes[str(candidate)] = sha256_file(candidate)
                except OSError:
                    continue
        payload = {
            "version": 1,
            "script_hash": script_hash,
            "committed_scenes": committed,
            "processed_scenes": scenes,
            "registry": registry_state or {},
            "asset_hashes": asset_hashes,
        }
        # Strict serialisability is part of the contract: a scene
        # result that cannot serialise is not checkpointed (the
        # completed-stage recorder applies the same rule).
        json.dumps(payload)
        write_json_atomic(
            asset_progress_path(artefact_dir), payload, ensure_ascii=False
        )
        return True
    except Exception:
        return False


def load_asset_progress(artefact_dir, project_dir, *, script_hash):
    """Load and validate this run's asset-stage checkpoint, or
    None when there is nothing trustworthy to resume from.

    Validation: the checkpoint belongs to this run's artefact
    directory (the caller passes its own), the script hash matches
    the run being executed, every committed scene result is
    present, and every asset file stamped at record time still
    exists and hashes to its recorded value — a scene whose
    selected assets were deleted or replaced after the crash is
    NOT skipped; the whole checkpoint is refused (fail-closed to
    a full stage re-run rather than a partial skip over changed
    files)."""
    try:
        payload = _load_json(asset_progress_path(artefact_dir))
        if not isinstance(payload, dict):
            return None
        if script_hash and payload.get("script_hash") != script_hash:
            return None
        scenes = payload.get("processed_scenes")
        committed = payload.get("committed_scenes")
        if not isinstance(scenes, list) or not scenes:
            return None
        if not isinstance(committed, list) or len(committed) != len(scenes):
            return None
        for raw, expected in (payload.get("asset_hashes") or {}).items():
            candidate = Path(raw)
            if not candidate.is_file():
                return None
            try:
                if sha256_file(candidate) != expected:
                    return None
            except OSError:
                return None
        return payload
    except Exception:
        return None


def clear_asset_progress(artefact_dir):
    """Drop the incremental checkpoint once the completed stage
    payload supersedes it. Fail-soft."""
    try:
        asset_progress_path(artefact_dir).unlink(missing_ok=True)
    except Exception:
        pass


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
