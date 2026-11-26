import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from redis import Redis
from rq import Queue

from services.script_analyzer import analyze_script, write_case_bible
from services.media_pipeline import process_scene_media, reset_media_caches
from services.timeline import build_project_timeline
from services.renderer import render_timeline
from services.narration import generate_narration
from services.audio_mixer import add_narration
from services.music import prepare_soundtrack
from services.captions import build_captions, validate_captions
from services.caption_renderer import burn_captions
from services.graphics_pipeline import build_motion_graphics_assets
from services.final_video_auditor import validate_final_video
from services.asset_report import (
    build_asset_report,
    build_rights_manifest,
    write_asset_report,
    write_rights_manifest,
)
from services.visual_registry import reset_registry
from services import production_report as production_report_service
from services import visual_quality as visual_quality_service
from services import stage_resume

import logging

from services import genre_packs
from services import logging_setup
from services import media_paths
from services import scene_structure
from services import media_probe
from services import narration as narration_service
from services import timing as timing_service
from services.atomic_write import write_json_atomic, write_text_atomic
from services import music as music_service
from services import run_context
from services import run_ledger
from services import run_metrics
from services import release_status
from services.production_state import (
    create_production_state,
    start_stage,
    complete_stage,
    fail_stage,
    utc_now,
)


# B-13 (Phase 5c-1): run lifecycle events (enqueue / claim /
# start / finish) ride the structured cinemora logger tree.
logger = logging_setup.get_logger("jobs")

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
QUEUE_NAME = "video_generation"

redis_connection = Redis.from_url(REDIS_URL, decode_responses=False)

video_queue = Queue(
    QUEUE_NAME,
    connection=redis_connection,
    default_timeout=3600,
)

# Phase 8.4 two-lane design (03 §3.3): interactive work (plan
# analysis, revision renders, priority-tier productions) runs on
# the priority lane; workers always drain it first. Per-kind job
# timeouts replace the single 1-hour default (03 §3.3 table).
PRIORITY_QUEUE_NAME = "video_generation_priority"

video_queue_priority = Queue(
    PRIORITY_QUEUE_NAME,
    connection=redis_connection,
    default_timeout=3600,
)

FULL_PIPELINE_TIMEOUT_SECONDS = 4 * 3600
ANALYSIS_TIMEOUT_SECONDS = 1800
REVISION_RENDER_TIMEOUT_SECONDS = 3600


def _dispatch_quietly():
    """Fail-soft dispatcher pass (Phase 8.4): submissions and
    completions are the two moments capacity changes."""
    try:
        from services import dispatcher

        dispatcher.dispatch_once()
    except Exception:
        pass

# PIPE-101: queue ordering policy is FIFO. Every enqueue in this
# module appends to the BACK of the queue (RQ's default); no call
# site passes at_front. The blanket at_front=True that used to sit
# on every enqueue made the queue LIFO — newer submissions jumped
# ahead of older ones and could starve them — and no call site had
# a genuine priority need. If a future call site truly needs
# priority, it must introduce an explicit, documented priority
# mechanism rather than reviving a blanket at_front.

SENTENCES_PER_SCENE = 40

# Stage A: cap on UNTARGETED stat motion graphics per project.
# Targeted graphics (for sentences with no verified visual) are
# never blocked by this cap.
MOTION_GRAPHIC_MAX_STATS = 3


def _persistable_state(state: dict) -> dict:
    """The subset of the run state that may be written to disk.

    MA-09: keys starting with "_" are in-memory run internals — above
    all "_partial" (VA-07's failure-report references: live scene
    lists, the analysis dict, the timeline result). They stay on the
    in-memory dict for fail_pipeline, but must never be serialised
    into production_state.json, where they bloat the file and leak
    pipeline internals to any future /status consumer.
    """
    return {
        key: value
        for key, value in state.items()
        if not str(key).startswith("_")
    }


def save_production_state(project_dir: Path, state: dict):
    """Persist the production state atomically (MA-11).

    The write goes through services.atomic_write (the single MA-11
    pattern: temp file in the SAME directory, flushed and fsynced,
    then os.replace()d) — so a reader (or a crash mid-write) can
    never observe a half-written state file: the file on disk is
    always either the previous complete state or the new complete
    state.

    Stage 4 (DATA-004): when the state belongs to a ledger run (the
    in-memory state carries ``_run_id``), the state file is written
    into that run's artefact directory (``runs/<run_id>/``), so a
    later run of the same project can never overwrite this run's
    progress record. States without a run (legacy entry points)
    keep the project-level path. Readers resolve through
    media_paths.resolve_artefact.
    """
    project_dir = Path(project_dir)
    run_id = state.get("_run_id") if isinstance(state, dict) else None
    target_dir = (
        media_paths.run_subdir(project_dir, run_id) if run_id else project_dir
    )
    state_file = target_dir / "production_state.json"
    payload = json.dumps(
        _persistable_state(state), indent=2, ensure_ascii=False
    )
    write_text_atomic(state_file, payload)
    return state_file


def _evidence_ledger_entries(project_dir):
    """Phase 3: the production's durable evidence-ledger rows for
    the asset report (services.discovery_memory's evidence table,
    opened on the project directory). Fail-soft: an unusable store
    yields an empty list — the report then states "not_recorded",
    never an error."""
    try:
        from services.discovery_memory import DiscoveryMemory

        store = DiscoveryMemory.open(str(project_dir))
        if store is None:
            return []
        return store.evidence_entries()
    except Exception:
        return []


def _set_project_status(project_id, status: str):
    """AUD-03: write the run's status back to the Project row.

    Best-effort by design: the database is the observability surface
    for the run, never a pipeline dependency — any failure (DB down,
    unknown project, bad config) is logged and swallowed so it can
    never fail a production. The import is lazy for the same reason.
    """
    try:
        from database import SessionLocal
        from models import Project

        db = SessionLocal()
        try:
            project = db.get(Project, str(project_id))
            if project is None:
                return
            project.status = status
            db.commit()
        finally:
            db.close()
    except Exception as error:
        print(
            f"WARNING: could not write project status "
            f"'{status}' for {project_id}: {error}"
        )


# FS-2 / FS-D: the genre keys a user could once feed into pack
# resolution. None of them may influence which pack a production
# resolves: the documentary treatment is backend-derived from the
# script (genre_packs.detect_genre, via resolve_genre_pack's
# detected/default branches) for every project, old and new.
_USER_GENRE_OPTION_KEYS = ("genre", "genre_pack", "project_category")


def _options_for_genre_resolution(options):
    """A COPY of the run options with every user-fed genre key
    removed, for genre-pack resolution.

    FS-2 / FS-D cuts the two user-fed inputs the resolver used to
    honour: (1) an explicit ``genre``/``genre_pack`` option — the
    create/configure flows no longer offer a genre selection and
    the create API strips the keys at storage, but a project or
    run created before FS-D may still carry one in its stored
    options; (2) the project's stored ``category`` label, which
    the run-9 wiring used to inject here as ``project_category``
    (via the now-removed ``_options_with_project_category``) — a
    stored category can never again act as a user-chosen pack
    override. Resolution therefore always lands on the resolver's
    detector/default branches. The caller's dict is not mutated:
    jobs stamps the RESOLVED pack back onto the real options
    (``genre_pack``/``genre_source``) after resolution, and
    downstream consumers read those stamps.
    """
    cleaned = dict(options) if isinstance(options, dict) else {}
    for key in _USER_GENRE_OPTION_KEYS:
        cleaned.pop(key, None)
    return cleaned


class _StageAdopted(Exception):
    """Internal control flow (Transformation Phase 5.1): the stage
    was adopted from a prior run's validated artefacts, so its body
    is skipped. Raised only at the top of a stage's try block, after
    the adoption restore + finish have completed; caught by the
    stage's own ``except _StageAdopted`` clause. Never a failure."""


class _OwnershipLostAbort(Exception):
    """Internal control flow (Phase 1 durability): this execution
    discovered at a stage boundary that it no longer owns its run
    (same-run resume handed the run to another worker) and stopped
    WITHOUT touching the run's ledger state or artefacts.
    fail_pipeline converts RunOwnershipLost into this abort; it
    propagates out of run_full_pipeline so the (already superseded)
    RQ job ends, while the run itself continues under its owner."""


def _finish_executed_stage(state, stage, project_dir, artefact_dir, payload=None):
    """finish_stage + Phase 5.1 resume bookkeeping for a stage that
    EXECUTED in this run: record its artefact hashes + resume
    payload (services.stage_resume) before the state is saved, and
    mark it executed in the run's resume record."""
    stage_resume.record_stage_completion(
        state, stage, project_dir, artefact_dir, payload
    )
    stage_resume.mark_stage_executed(state, stage)
    finish_stage(state, stage, project_dir)


def _adopt_stage(state, stage, project_dir, plan):
    """Complete a stage by ADOPTION (Phase 5.1): the stage finishes
    normally in the ledger/state, then the adoption is stamped
    (adopted flag, source run, inherited artefact manifest) and the
    state saved again so the record on disk carries both."""
    finish_stage(state, stage, project_dir)
    stage_resume.mark_stage_adopted(state, plan, stage)
    save_production_state(project_dir, state)
    print(
        f"RESUME: stage '{stage}' adopted from run "
        f"{plan.get('source_run_id')} — validated artefacts reused, "
        "stage body skipped."
    )


def run_stage(state: dict, stage: str, project_dir: Path):
    # Stage 2B: cooperative cancellation + ledger stage tracking.
    # Both are fail-soft (a ledger problem never wedges a run); only
    # an actual cancellation request interrupts the pipeline, via
    # RunCancelled, which the stage's except block routes into
    # fail_pipeline like any other stage error.
    run_id = state.get("_run_id") if isinstance(state, dict) else None
    if run_id:
        if run_ledger.is_cancel_requested(run_id):
            raise run_ledger.RunCancelled(
                f"Run {run_id} was cancelled before stage '{stage}'."
            )
        run_ledger.set_run_stage(run_id, stage)
        # Stage 3 (R2): a stage boundary is also a heartbeat — even
        # if the run's heartbeat thread died, crossing into a new
        # stage refreshes the run's liveness evidence.
        run_ledger.touch_run(run_id)
        # Phase 1: a stage boundary is also the zombie checkpoint.
        # If this execution's heartbeat went stale, the run was
        # requeued and another worker claimed it (same-run resume)
        # — this execution must stop here rather than run in
        # parallel with the run's owner. Positive evidence only
        # (ownership_lost is fail-soft toward not aborting).
        if run_ledger.ownership_lost(run_id):
            raise run_ledger.RunOwnershipLost(
                f"Run {run_id} is owned by another worker "
                "(resumed after heartbeat loss); this superseded "
                "execution stops at the stage boundary."
            )
    start_stage(state, stage)
    run_metrics.stage_started(stage)
    save_production_state(project_dir, state)
    print()
    print("========================================")
    print(f"STAGE: {stage.upper()}")
    print("========================================")
    print()


def finish_stage(state: dict, stage: str, project_dir: Path):
    complete_stage(state, stage)
    run_metrics.stage_finished(stage)
    save_production_state(project_dir, state)


def fail_pipeline(state: dict, stage: str, error: Exception, project_dir: Path):
    # Phase 1: an ownership-lost abort is NOT a failure of the
    # run — the run is healthy under its new owner. This execution
    # must not finish the run, settle its billing, or write any
    # run artefact (state, reports, manifest): all of those belong
    # to the owner. Log, release the run context, and convert to
    # the internal abort that ends this superseded job quietly.
    if isinstance(error, run_ledger.RunOwnershipLost):
        run_id = state.get("_run_id") if isinstance(state, dict) else None
        logging_setup.log_event(
            logger,
            logging.WARNING,
            "run_ownership_lost_abort",
            project_id=Path(project_dir).name,
            run_id=run_id,
            stage=stage,
        )
        print(
            f"OWNERSHIP LOST: superseded execution of run {run_id} "
            f"stopping at stage '{stage}' without touching the run."
        )
        if isinstance(state, dict) and "_run_context_previous" in state:
            run_context.deactivate(state.pop("_run_context_previous"))
        raise _OwnershipLostAbort(str(error))
    fail_stage(state, stage, str(error))
    # AUD-30: a failed run is still a finished run — stamp the
    # completion time the state machine previously never set.
    state["completed_at"] = utc_now()
    run_metrics.stage_failed(stage)
    save_production_state(project_dir, state)
    # AUD-03: the failure is a status transition like any other.
    # project_dir is media/<project_id>, so its name is the id.
    _set_project_status(Path(project_dir).name, "failed")
    # VA-07: a failed run must still leave its asset report, built
    # from whatever partial state the pipeline had accumulated, so a
    # failure is diagnosable from the report alone. A problem writing
    # the report never masks the original error.
    # Stage 4 (DATA-004): a run's failure artefacts belong to the
    # run — they are written into its artefact directory so a later
    # re-run cannot overwrite the record of this failure. Without a
    # run (legacy callers) the project-level path is kept.
    run_id = state.get("_run_id") if isinstance(state, dict) else None
    artefact_dir = (
        media_paths.run_subdir(project_dir, run_id) if run_id else project_dir
    )
    failure_report = None
    try:
        partial = state.get("_partial") or {}
        partial_analysis = partial.get("analysis")
        failure_report = build_asset_report(
            partial.get("processed_scenes") or [],
            timeline_result=partial.get("timeline_result"),
            analysis_mode=(
                partial_analysis.get("mode")
                if isinstance(partial_analysis, dict)
                else None
            ),
            planner=(
                partial_analysis.get("planner")
                if isinstance(partial_analysis, dict)
                else None
            ),
            # C-07: a failed run's report carries the research
            # outcome too (from the run state when recorded, else
            # derived from the partial analysis's bible).
            research=(
                state.get("research")
                if isinstance(state, dict)
                and isinstance(state.get("research"), dict)
                else _research_stage_record(
                    {"analysis": partial_analysis}
                )
            ),
            run_failure={"stage": stage, "error": str(error)},
            execution=run_metrics.snapshot(),
            genre_pack=state.get("genre_pack"),
            evidence_ledger=_evidence_ledger_entries(project_dir),
            # Stage H: a run that reached the Sound Designer stage
            # carries its soundtrack record in state — including the
            # "failed" record for a corrupt music file — and the
            # failure report must show it.
            soundtrack=state.get("soundtrack"),
        )
        write_asset_report(artefact_dir, failure_report)
        print("Asset report (failure): written from partial state.")
    except Exception as report_error:
        print(f"Asset report (failure) could not be written: {report_error}")
    # Stage 2: close the ledger run (failed, or cancelled when the
    # error is a cooperative cancellation), record whatever decisions
    # the partial state supports, finish the run manifest, and restore
    # the previous run context. All fail-soft (run_ledger never
    # raises for database problems).
    if run_id:
        cancelled = isinstance(error, run_ledger.RunCancelled)
        if failure_report is not None:
            partial = state.get("_partial") or {}
            run_ledger.record_decisions(
                run_id,
                Path(project_dir).name,
                failure_report,
                partial.get("timeline_result"),
                processed_scenes=partial.get("processed_scenes"),
            )
            # Stage H (M7): the run's music outcome is a ledger
            # decision too — recorded AFTER the visual decisions
            # (record_decisions skips runs that already have rows).
            if isinstance(state.get("soundtrack"), dict):
                run_ledger.record_music_decision(
                    run_id,
                    Path(project_dir).name,
                    state["soundtrack"],
                )
        manifest_path = None
        manifest = state.get("_manifest")
        if isinstance(manifest, dict):
            enriched = run_ledger.enrich_manifest(
                manifest,
                status="cancelled" if cancelled else "failed",
                execution=run_metrics.snapshot(),
                asset_report=failure_report,
            )
            written = run_ledger.write_manifest(artefact_dir, enriched)
            manifest_path = str(written) if written else None
        run_ledger.finish_run(
            run_id,
            "cancelled" if cancelled else "failed",
            error="cancelled" if cancelled else str(error),
            execution=run_metrics.snapshot(),
            manifest_path=manifest_path,
            stage=stage,
            # Phase 4: whatever statuses the run had stamped when
            # it failed (e.g. technical_status "failed" from the
            # final-audit gate) ride the finish; unstamped stays
            # None — NULL in the ledger means "not evaluated".
            technical_status=(
                state.get("technical_status")
                if isinstance(state, dict) else None
            ),
            release_status=(
                state.get("release_status")
                if isinstance(state, dict) else None
            ),
            release_findings=(
                state.get("release_findings")
                if isinstance(state, dict) else None
            ),
        )
    if isinstance(state, dict) and "_run_context_previous" in state:
        run_context.deactivate(state.pop("_run_context_previous"))
    # Phase 8.4: a terminal run frees capacity — let the dispatcher
    # promote the next queued production.
    _dispatch_quietly()
    logging_setup.log_event(
        logger,
        logging.ERROR,
        "run_failed",
        project_id=Path(project_dir).name,
        run_id=run_id,
        stage=stage,
        error=str(error),
    )
    raise error


def script_hash_for(script: str) -> str:
    return hashlib.sha256(str(script or "").encode("utf-8")).hexdigest()[:16]


def _script_sentence_count(script: str):
    """Sentence count for the run manifest, using narration's own
    sentence split so the number means the same thing everywhere.
    None when it cannot be determined (the manifest records it
    honestly as null rather than guessing)."""
    try:
        from services.narration import _split_sentences

        return len(_split_sentences(script))
    except Exception:
        return None


def _planned_scenes_if_sane(planned, sentences, project_id, script_hash, bible, bible_path):
    """Return production scenes built from analyzer-planned scenes,
    or None when the plan is missing/insane (caller falls back to
    flat chunking). Sanity = the planned scenes' sentence lists,
    concatenated, are exactly ``sentences`` (same objects, same
    order, each exactly once) and no scene is empty."""
    if not isinstance(planned, list) or not planned:
        return None
    flat = []
    for scene in planned:
        if not isinstance(scene, dict):
            return None
        scene_sentences = scene.get("sentences")
        if not isinstance(scene_sentences, list) or not scene_sentences:
            return None
        flat.extend(scene_sentences)
    if len(flat) != len(sentences) or any(
        a is not b for a, b in zip(flat, sentences)
    ):
        return None

    # Normalise into fresh scene dicts in the production shape.
    groups = [list(scene["sentences"]) for scene in planned]
    # Merge guard: a trailing scene of <2 sentences joins the
    # previous scene (unless it is the only scene).
    if len(groups) > 1 and len(groups[-1]) < 2:
        groups[-2].extend(groups.pop())

    scenes = []
    for group in groups:
        scenes.append(
            {
                "scene_number": len(scenes) + 1,
                "sentences": group,
                "text": " ".join(
                    str(s.get("text") or "")
                    for s in group
                    if isinstance(s, dict) and s.get("text")
                ),
                "sentence_count": len(group),
                "project_id": project_id,
                "script_hash": script_hash,
                "bible": bible,
                "case_bible_path": str(bible_path) if bible_path else "",
            }
        )
    return scenes


def _apply_scene_governor(scenes, project_dir, sentence_count):
    """FP-2 scene governor, post-narration split (Master Directive
    Phase 2): when the production's narration parts manifest holds
    a MEASURED duration for every sentence, scenes longer than
    scene_structure.SCENE_SPLIT_THRESHOLD_SECONDS are split at beat
    boundaries. The threshold is a founder editorial parameter —
    it is recorded in the returned governor record (which lands in
    the production plan) whether or not any scene split. Fail-soft:
    no manifest, a part count that does not match the script, or
    an unmeasured part leaves the scenes untouched and the record
    says why."""
    governor = {
        "split_threshold_seconds": (
            scene_structure.SCENE_SPLIT_THRESHOLD_SECONDS
        ),
        "narration_split_applied": False,
        "scenes_split": 0,
        "reason": None,
    }
    if not project_dir:
        governor["reason"] = "no_project_dir"
        return scenes, governor
    try:
        data = narration_service.load_parts_manifest_data(
            Path(project_dir) / "narration_parts" / "parts_manifest.json"
        )
    except Exception:
        data = None
    parts = data.get("parts") if isinstance(data, dict) else None
    if not isinstance(parts, list) or len(parts) != sentence_count:
        governor["reason"] = "narration_manifest_unavailable"
        return scenes, governor
    durations = []
    for part in parts:
        value = (
            part.get("measured_duration_seconds")
            if isinstance(part, dict)
            else None
        )
        try:
            durations.append(float(value))
        except (TypeError, ValueError):
            governor["reason"] = "narration_not_fully_measured"
            return scenes, governor
    split = scene_structure.split_scenes_by_narration(scenes, durations)
    parents = {
        scene["narration_split"]["parent_scene_number"]
        for scene in split
        if scene.get("narration_split")
    }
    governor["scenes_split"] = len(parents)
    governor["narration_split_applied"] = bool(parents)
    governor["scenes_after"] = len(split)
    return split, governor


def build_production_scenes(script: str, project_id: str = "", project_dir=None, genre_pack=None):
    project_dir = Path(project_dir) if project_dir else (
        media_paths.project_dir(project_id) if project_id else None
    )
    cache_path = None
    if project_dir:
        project_dir.mkdir(parents=True, exist_ok=True)
        cache_path = str(project_dir / "story_understanding_cache.json")

    analysis = analyze_script(
        script,
        project_id=project_id,
        cache_path=cache_path,
        genre_pack=genre_pack,
    )

    if not isinstance(analysis, dict):
        raise RuntimeError(
            f"Script analyzer returned unexpected type: {type(analysis).__name__}"
        )

    sentences = analysis.get("sentences")
    if not isinstance(sentences, list):
        raise RuntimeError("Script analyzer did not return a 'sentences' list.")
    if not sentences:
        raise RuntimeError("Script analyzer returned zero sentences.")

    bible = analysis.get("bible") or {}

    # Research Studio (Phase D): user entity corrections recorded
    # in the studio store are applied to the analysed bible BEFORE
    # the Case Bible and plan documents are written, so a correction
    # demonstrably changes this plan and every downstream consumer
    # of the bible. Fail-soft with the outcome printed — a studio
    # artefact problem must never lose an analysis.
    if project_id and isinstance(bible, dict) and bible.get("entities"):
        try:
            from services import research_studio as _research_studio

            _applied = _research_studio.apply_entity_corrections(
                project_id, bible
            )
            if _applied:
                print(
                    "Research Studio: entity corrections applied: "
                    + ", ".join(
                        f"{a.get('entity_id')}.{a.get('field')}"
                        f"={'ok' if a.get('applied') else 'skipped'}"
                        for a in _applied
                    )
                )
        except Exception as _correction_error:  # recorded, not silent
            print(
                "Research Studio: entity corrections NOT applied: "
                f"{type(_correction_error).__name__}: {_correction_error}"
            )

    bible_path = None
    if project_dir:
        bible_path = write_case_bible(analysis, project_dir)
        print(f"Case Bible saved: {bible_path}")

    script_hash = script_hash_for(script)

    # Stage 3C: prefer the story-structured scenes the analyzer
    # derived (analysis["scenes"]) when they are sane — they cover
    # every sentence exactly once, in order, with no empty scene.
    # A trailing scene of <2 sentences merges into the previous one
    # (unless it is the only scene). Anything else falls back to the
    # historical flat SENTENCES_PER_SCENE chunking, unchanged.
    planned_scenes = analysis.get("scenes")
    scenes = _planned_scenes_if_sane(
        planned_scenes, sentences, project_id, script_hash, bible, bible_path
    )
    if scenes is not None:
        print(
            f"Production scenes: using {len(scenes)} story-structured "
            "scene(s) from script analysis."
        )
        scenes, scene_governor = _apply_scene_governor(
            scenes, project_dir, len(sentences)
        )
        return {
            "analysis": analysis,
            "sentences": sentences,
            "scenes": scenes,
            "scene_governor": scene_governor,
            "script_hash": script_hash,
            "project_id": project_id,
            "bible": bible,
            "case_bible_path": str(bible_path) if bible_path else "",
        }

    scenes = []
    for start_index in range(0, len(sentences), SENTENCES_PER_SCENE):
        scene_sentences = sentences[start_index:start_index + SENTENCES_PER_SCENE]
        if not scene_sentences:
            continue

        scene_number = len(scenes) + 1
        scene_text_parts = []
        for sentence in scene_sentences:
            if not isinstance(sentence, dict):
                raise RuntimeError("Invalid sentence object from analyzer.")
            sentence_text = sentence.get("text", "")
            if sentence_text:
                scene_text_parts.append(sentence_text)

        scenes.append({
            "scene_number": scene_number,
            "sentences": scene_sentences,
            "text": " ".join(scene_text_parts),
            "sentence_count": len(scene_sentences),
            "project_id": project_id,
            "script_hash": script_hash,
            "bible": bible,
            "case_bible_path": str(bible_path) if bible_path else "",
        })

    if not scenes:
        raise RuntimeError("Unable to create production scenes.")

    scenes, scene_governor = _apply_scene_governor(
        scenes, project_dir, len(sentences)
    )
    return {
        "analysis": analysis,
        "sentences": sentences,
        "scenes": scenes,
        "scene_governor": scene_governor,
        "script_hash": script_hash,
        "project_id": project_id,
        "bible": bible,
        "case_bible_path": str(bible_path) if bible_path else "",
    }


def enqueue_promoted_run(run, project, is_priority):
    """Enqueue one dispatcher-promoted run (Phase 8.4). Called by
    services.dispatcher with ORM rows. Returns the RQ job id, or
    None when the enqueue failed — in which case the run STAYS
    QUEUED in the ledger backlog and a later dispatch pass retries
    the promotion (design 03 §3.1: the ledger, not RQ, is the
    queue of record; a Redis outage delays productions, it does
    not fail them)."""
    options = dict(run.options or {})
    queue = video_queue_priority if is_priority else video_queue
    try:
        job = queue.enqueue(
            run_full_pipeline,
            project.id,
            project.script,
            job_timeout=FULL_PIPELINE_TIMEOUT_SECONDS,
            run_id=run.id,
            options=options,
        )
    except Exception as exc:
        print(
            f"Dispatcher: promotion of run {run.id} failed "
            f"({exc}); the run stays in the ledger backlog."
        )
        return None
    return job.id


def enqueue_test_job(project_id: str):
    job = video_queue.enqueue(test_job, project_id)
    print(f"Queued test job: {job.id}")
    return job.id


def test_job(project_id: str):
    print(f"Starting job for project: {project_id}")
    for step in range(1, 6):
        print(f"Processing step {step}/5...")
    print(f"Job completed for project: {project_id}")
    return {"project_id": project_id, "status": "completed"}


def enqueue_full_pipeline(project_id: str, script: str, options=None, run_id=None):
    # Stage 3 options plumbing: caller options (recognised keys:
    # "music_file", "music_enabled", "music_volume",
    # "narration_volume", "voice_id", "captions") are merged into
    # the ledger/manifest options snapshot next to the entrypoint.
    # Unknown keys pass through harmlessly.
    # Phase 8.4: remember whether the caller released an EXISTING
    # run (plan approval) versus submitting a new one — only new
    # submissions route through the dispatcher backlog.
    _release = run_id is not None
    run_options = {"entrypoint": "enqueue_full_pipeline"}
    if isinstance(options, dict):
        run_options.update(options)
    # Stage 2B (AUD-07/10): the submission is recorded in the run
    # ledger FIRST and deduplicated — a second submission of the same
    # script for the same project while a run is active returns that
    # run instead of queueing a duplicate production. An enqueue
    # failure marks the run failed rather than orphaning it silently.
    # When the ledger is unavailable the enqueue proceeds without a
    # run_id (the pipeline then begins an ad-hoc run, also fail-soft).
    # Stage 4: a caller may pass an EXISTING run_id to release it
    # directly (plan approval releasing the run its analysis phase
    # parked in 'awaiting_approval'); the submission step is then
    # skipped because the run already exists by design.
    if run_id is not None:
        submission = {"run_id": run_id, "deduplicated": False}
    else:
        submission = run_ledger.submit_production(
            project_id,
            script,
            options=run_options,
        )
    if submission and submission.get("deduplicated"):
        logging_setup.log_event(
            logger,
            logging.INFO,
            "submission_deduplicated",
            project_id=project_id,
            run_id=submission["run_id"],
        )
        return submission["run_id"]
    run_id = submission["run_id"] if submission else None
    # Phase 8.4: a NEW ledger submission enters the dispatcher's
    # backlog instead of enqueueing directly — the fair dispatcher
    # (services.dispatcher) promotes it when per-user and platform
    # capacity allow. Returns the promoted job id when promotion
    # was immediate, else None (the run is queued in the ledger;
    # its status tells the truth). The direct-enqueue path below is
    # unchanged for the two cases that were never new submissions:
    # an explicit run_id (the plan-approval release — the run
    # already held an active slot while parked in
    # awaiting_approval) and a ledger-unavailable submission (the
    # fail-soft ad-hoc enqueue it always had).
    if submission is not None and not _release:
        from services import dispatcher

        for promoted_run_id, job_id in dispatcher.dispatch_once():
            if promoted_run_id == run_id:
                logging_setup.log_event(
                    logger,
                    logging.INFO,
                    "run_enqueued",
                    project_id=project_id,
                    run_id=run_id,
                    job_id=job_id,
                )
                return job_id
        logging_setup.log_event(
            logger,
            logging.INFO,
            "run_queued_backlog",
            project_id=project_id,
            run_id=run_id,
        )
        return None
    try:
        job = video_queue.enqueue(
            run_full_pipeline,
            project_id,
            script,
            job_timeout=FULL_PIPELINE_TIMEOUT_SECONDS,
            run_id=run_id,
            options=run_options,
        )
    except Exception as error:
        if run_id:
            run_ledger.finish_run(
                run_id, "failed", error=f"enqueue failed: {error}"
            )
        raise
    logging_setup.log_event(
        logger,
        logging.INFO,
        "run_enqueued",
        project_id=project_id,
        run_id=run_id,
        job_id=job.id,
    )
    return job.id


def enqueue_analysis_phase(project_id: str, script: str, run_id: str, options=None):
    """Queue ONLY the analysis phase of a plan-gated run (Stage 4).
    The run row already exists (created by the caller's
    run_ledger.submit_production); an enqueue failure marks it
    failed rather than orphaning it, mirroring enqueue_full_pipeline."""
    try:
        # Interactive lane (Phase 8.4): a user is waiting on the
        # plan, so analysis rides the priority queue.
        job = video_queue_priority.enqueue(
            run_analysis_phase,
            project_id,
            script,
            job_timeout=ANALYSIS_TIMEOUT_SECONDS,
            run_id=run_id,
            options=options,
        )
    except Exception as error:
        if run_id:
            run_ledger.finish_run(
                run_id, "failed", error=f"enqueue failed: {error}"
            )
        raise
    logging_setup.log_event(
        logger,
        logging.INFO,
        "run_enqueued",
        project_id=project_id,
        run_id=run_id,
        job_id=job.id,
        kind="analysis_phase",
    )
    return job.id


def enqueue_revision_render(project_id: str, revision_id: str, run_id: str):
    """Queue an editor revision render (Stage 6). The run row was
    already submitted through run_ledger.submit_revision_render by
    services.editor.request_render; this only enqueues the worker
    job. The render claims the run itself, writes to
    revision_<id>_final.mp4 and never touches the production's
    documentary_final_output.mp4. An enqueue failure marks the run
    failed rather than orphaning it."""
    from services import editor

    try:
        # Interactive lane (Phase 8.4): revision renders ride the
        # priority queue — the user is waiting in the editor.
        job = video_queue_priority.enqueue(
            editor.run_revision_render,
            project_id,
            revision_id,
            run_id,
            job_timeout=REVISION_RENDER_TIMEOUT_SECONDS,
        )
    except Exception as error:
        if run_id:
            run_ledger.finish_run(
                run_id, "failed", error=f"enqueue failed: {error}"
            )
        raise
    logging_setup.log_event(
        logger,
        logging.INFO,
        "run_enqueued",
        project_id=project_id,
        run_id=run_id,
        job_id=job.id,
        kind="revision_render",
    )
    return job.id


def run_analysis_phase(project_id: str, script: str, run_id: str = None, options=None):
    """Stage 4 plan gate, phase 1: story analysis ONLY.

    Runs the same analysis the full pipeline's script_analysis stage
    runs (build_production_scenes → analyze_script, which caches by
    script content), persists the plan document (plan.json — the
    studio's review surface), then parks the ledger run in
    "awaiting_approval". No asset retrieval, no narration, no render.

    Release path: POST /api/v1/projects/{id}/plan/approve enqueues
    run_full_pipeline with the same run_id and options
    {"plan_approved": True}; the pipeline claims the run from
    "awaiting_approval" and its analysis stage reuses the persisted
    story_understanding_cache.json instead of re-analysing.
    """
    print()
    print("=" * 70)
    print("DOCUMENTARY STUDIO — ANALYSIS PHASE (plan gate)")
    print("=" * 70)
    print(f"Project: {project_id}")
    print("=" * 70)
    print()

    project_dir = media_paths.project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)

    if run_id is not None:
        claimed = run_ledger.claim_run(run_id)
        if claimed is None:
            existing = run_ledger.get_run(run_id)
            if existing is not None:
                print(
                    f"Run {run_id} not claimable (status: "
                    f"{existing['status']}); analysis phase not executed."
                )
                return {
                    "project_id": project_id,
                    "status": existing["status"],
                    "run_id": run_id,
                    "deduplicated_execution": True,
                }
            # B-02 (Phase 5a): same closure as run_full_pipeline —
            # never execute a caller-carried run unclaimed when
            # its ledger state cannot even be read.
            print(
                f"Run {run_id} could not be claimed and its ledger "
                "state could not be read; REFUSING to execute the "
                "analysis phase unclaimed (B-02)."
            )
            return {
                "project_id": project_id,
                "status": "unclaimed_execution_refused",
                "run_id": run_id,
                "deduplicated_execution": True,
            }
        # Stage 3 (R2): heartbeat for the analysis run, as in the
        # full pipeline — it stops itself when the run parks in
        # awaiting_approval or finishes.
        run_ledger.start_heartbeat(run_id)
        # Phase 8.4: this run's provider counters are its own.
        run_metrics.begin_run(run_id)

    _set_project_status(project_id, "running")
    try:
        # Stage 5A: the analysis phase resolves the genre pack the
        # same way the full pipeline does, so a gated run's research
        # is shaped by the same pack its production will record.
        # FS-2: the pack is backend-derived from the script —
        # user-fed genre keys are stripped before the resolver
        # sees the options (see _options_for_genre_resolution).
        _genre_info = genre_packs.resolve_genre_pack(
            _options_for_genre_resolution(options), script
        )
        production_data = build_production_scenes(
            script,
            project_id=project_id,
            project_dir=project_dir,
            genre_pack=_genre_info["pack"],
        )
    except Exception as error:
        if run_id:
            run_ledger.finish_run(
                run_id,
                "failed",
                error=f"analysis phase: {error}",
                stage="script_analysis",
            )
        _set_project_status(project_id, "failed")
        raise

    from services import plan_document

    plan_document.write_plan_document(
        project_dir, production_data["analysis"], project_id
    )
    # Master Directive Phase 2 (§4.2): the pre-retrieval visual
    # plan rides beside plan.json (additive, fail-soft like it).
    plan_document.write_visual_plan_document(
        project_dir,
        production_data["analysis"],
        project_id,
        scene_governor=production_data.get("scene_governor"),
    )
    if run_id:
        run_ledger.set_run_status(
            run_id, "awaiting_approval", stage="script_analysis"
        )
    _set_project_status(project_id, "awaiting_approval")
    # Phase 8.4: the analysis run leaving "running" frees capacity.
    _dispatch_quietly()
    print(
        f"Analysis phase complete: run {run_id} is awaiting plan approval."
    )
    return {
        "project_id": project_id,
        "status": "awaiting_approval",
        "run_id": run_id,
        "sentence_count": len(production_data["sentences"]),
        "scene_count": len(production_data["scenes"]),
    }


def _build_graphic_targets(processed_scenes):
    """Translate the Asset Manager's missing-visual records into
    graphics targets (Stage A targeting, used by run_full_pipeline's
    Motion Designer stage).

    IA-03: a sentence's scenario comes from its "scenarios" LIST —
    sentence records carry no singular "scenario" key (the builder
    previously read exactly that key, so every production target
    arrived with scenario={}). The first scenario is taken with the
    same normalisation services.plan_document._proposed_visual
    applies: ``scenarios = sentence.get("scenarios") or []``, then
    the first entry only when it is a dict. A sentence with
    missing/empty scenarios honestly gets {} — nothing is invented.
    """
    graphic_targets = []
    for processed_scene in processed_scenes:
        _scene = processed_scene.get("scene") or {}
        _scene_number = _scene.get("scene_number")
        _sentences = _scene.get("sentences") or []
        _missing = (
            (processed_scene.get("asset_stats") or {})
            .get("missing_visuals")
            or []
        )
        for _record in _missing:
            _sentence_number = _record.get("sentence_number")
            if _sentence_number is None:
                continue
            for _index, _sentence in enumerate(_sentences):
                if not isinstance(_sentence, dict):
                    continue
                if _sentence.get("sentence_number") != _sentence_number:
                    continue
                _scenarios = _sentence.get("scenarios") or []
                _scenario = (
                    _scenarios[0]
                    if _scenarios and isinstance(_scenarios[0], dict)
                    else {}
                )
                graphic_targets.append(
                    {
                        "scene_number": _scene_number,
                        "sentence_index": _index,
                        "sentence_number": _sentence_number,
                        "text": (
                            _sentence.get("text")
                            or _sentence.get("sentence_text")
                            or ""
                        ),
                        "scenario": _scenario,
                    }
                )
                break
    return graphic_targets


def _annotate_missing_visual_graphics_outcomes(
    processed_scenes, motion_graphics_file
):
    """Run-10 fix: stamp each missing_visuals record with the
    graphics stage's outcome for its sentence.

    The Asset Manager's missing_visuals entries are the canonical
    record of a beat that got no verified visual; the graphics
    stage's per-target outcomes (targets.outcomes in the written
    motion_graphics.json) say what the T5 route then did with the
    beat — generated (and by which family, e.g. the
    coverage-guarantee closing card) or skipped (with the typed
    MG-08 reason). Without this stamp, a beat the graphics path
    genuinely could not serve reads in the report as merely
    "uncovered", with the reason stranded in the graphics
    manifest. Data propagation only: the entries are the same
    dicts the asset report aggregates, so both the success and
    failure reports carry the stamp. Fail-soft — an unreadable
    graphics record annotates nothing. Returns the number of
    entries annotated."""
    try:
        record = json.loads(
            Path(motion_graphics_file).read_text(encoding="utf-8")
        )
    except Exception:
        return 0
    outcomes = ((record.get("targets") or {}).get("outcomes")) or []
    by_key = {
        (outcome.get("scene_number"), outcome.get("sentence_number")):
            outcome
        for outcome in outcomes
        if isinstance(outcome, dict)
    }
    if not by_key:
        return 0
    annotated = 0
    for processed_scene in processed_scenes or []:
        if not isinstance(processed_scene, dict):
            continue
        scene = processed_scene.get("scene") or {}
        scene_number = scene.get("scene_number")
        stats = processed_scene.get("asset_stats") or {}
        for entry in stats.get("missing_visuals") or []:
            if not isinstance(entry, dict):
                continue
            outcome = by_key.get(
                (scene_number, entry.get("sentence_number"))
            )
            if outcome is None:
                continue
            entry["graphics_outcome"] = {
                "outcome": outcome.get("outcome"),
                "family": outcome.get("family"),
                "reason": outcome.get("reason"),
            }
            annotated += 1
    return annotated


def _annotate_missing_visuals_motion_graphics_disabled(processed_scenes):
    """Phase 9 (§13.1): with motion graphics disabled by the
    production configuration, every missing-visual record is
    stamped with the graphics outcome it actually had — not
    attempted, because disabled — so the asset report shows the
    beat as honestly uncovered-by-configuration rather than
    merely "uncovered" with the reason stranded. Returns the
    number of entries stamped."""
    stamped = 0
    for processed_scene in processed_scenes or []:
        if not isinstance(processed_scene, dict):
            continue
        stats = processed_scene.get("asset_stats") or {}
        for entry in stats.get("missing_visuals") or []:
            if not isinstance(entry, dict):
                continue
            entry["graphics_outcome"] = {
                "outcome": "not_attempted",
                "family": None,
                "reason": "motion_graphics_disabled",
            }
            stamped += 1
    return stamped


# MG-07: closed vocabulary for graphics the attach step refuses.
ATTACH_SKIP_REASONS = frozenset({
    "not_delivered",            # builder did not mark it delivered
    "graphic_validation_failed",  # no passing validation verdict recorded
    "missing_graphic_file",     # recorded file is not on disk
    "missing_scene_association",  # no scene_number, or scene not found
})


def _attach_motion_graphics(motion_graphics, processed_scenes):
    """Attach standalone motion graphics to their target scenes.

    MG-07: only graphics the builder explicitly marked `delivered`
    AND that carry a passing Phase 1 validation verdict may be
    attached — the previous loop attached anything with a
    scene_number, so undelivered graphics stayed off screen only
    incidentally. Every refused graphic is returned in the skipped
    list with a typed reason (ATTACH_SKIP_REASONS); nothing is
    attached silently or refused silently.

    Returns (attached_count, skipped) where skipped entries are
    {"graphic": <id/type>, "reason": <typed reason>}.
    """
    attached = 0
    skipped = []
    for graphic in motion_graphics or []:
        label = (
            graphic.get("motion_graphic_id")
            or graphic.get("graphic_type")
            or "unknown"
        )
        if graphic.get("delivered") is not True:
            skipped.append({"graphic": label, "reason": "not_delivered"})
            continue
        validation = graphic.get("validation") or {}
        if validation.get("passed") is not True:
            skipped.append(
                {"graphic": label, "reason": "graphic_validation_failed"}
            )
            continue
        file_path = graphic.get("file_path") or graphic.get("path")
        if not file_path or not Path(file_path).is_file():
            skipped.append(
                {"graphic": label, "reason": "missing_graphic_file"}
            )
            continue
        scene_number = graphic.get("scene_number")
        target_scene = None
        if scene_number is not None:
            for processed_scene in processed_scenes:
                scene = processed_scene.get("scene") or {}
                if scene.get("scene_number") == scene_number:
                    target_scene = processed_scene
                    break
        if target_scene is None:
            skipped.append(
                {"graphic": label, "reason": "missing_scene_association"}
            )
            continue

        visual = dict(graphic)
        visual["type"] = "video"
        visual["source"] = "cinemora_motion_graphics"
        visual["motion_graphic"] = True
        visual["sentence_index"] = graphic.get("sentence_index")
        visual["file_path"] = file_path
        visual["path"] = file_path

        target_scene.setdefault("videos", []).append(visual)
        attached += 1
    return attached, skipped


def _coverage_incomplete_error(uncovered, total_sentences):
    """The video_editor coverage gate's typed error (Gate 2: one
    construction shared by the gate itself and the pre-render
    coverage repair pass, so a sentence that survives repair fails
    with exactly the error the gate would have raised — same
    prefix, same refusal, no second dialect)."""
    detail = ", ".join(
        f"sentence {u.get('sentence_number')} "
        f"(scene {u.get('scene_number')}"
        + (
            f", {u.get('start')}s-{u.get('end')}s)"
            if u.get("start") is not None and u.get("end") is not None
            else ")"
        )
        for u in uncovered
    )
    return RuntimeError(
        "TIMELINE COVERAGE INCOMPLETE: "
        f"{len(uncovered)} of {total_sentences} "
        f"narration sentences have no visual coverage: {detail}. "
        "Refusing to render an incomplete documentary."
    )


def _sentences_still_missing(processed_scenes):
    """Gate 2 (Blocker 3): the missing_visuals entries that are
    STILL uncovered after the Motion Designer stage — an entry is
    resolved when the graphics stage generated a graphic for the
    sentence (its stamped graphics_outcome) or a delivered motion
    graphic for the sentence is attached to the scene. Returns a
    list of {"scene_number", "sentence_number", "entry"}.

    Fix-up (Gate 2 verification): an entry is a repair candidate
    only when the graphics stage actually adjudicated it — a
    graphics_outcome stamp is recorded (generated / skipped /
    not_attempted). An entry with NO stamp was never put to the
    graphics chain (e.g. the stage was stubbed, adopted or its
    manifest was unreadable), so it is not evidence of a beat
    still uncovered AFTER graphics: treating it as one made the
    repair pass re-run the asset worker for beats the pipeline
    had already settled and pre-empted the timeline coverage
    gate below, breaking the Phase 3a stage-resume contract
    (a searched-but-unresolved beat must reach the gate, and a
    starved beat's re-search belongs to the asset stage's own
    retry, not a second asset pass inside video_editor). The
    timeline gate itself is unchanged and still decides every
    sentence the repair pass does not touch."""
    still = []
    for processed_scene in processed_scenes or []:
        if not isinstance(processed_scene, dict):
            continue
        scene = processed_scene.get("scene") or {}
        scene_number = scene.get("scene_number")
        sentences = scene.get("sentences") or []
        index_by_number = {
            sentence.get("sentence_number"): index
            for index, sentence in enumerate(sentences)
            if isinstance(sentence, dict)
        }
        attached = {
            visual.get("sentence_number")
            for visual in processed_scene.get("videos") or []
            if isinstance(visual, dict) and visual.get("motion_graphic")
        }
        attached_indexes = {
            visual.get("sentence_index")
            for visual in processed_scene.get("videos") or []
            if isinstance(visual, dict) and visual.get("motion_graphic")
        }
        stats = processed_scene.get("asset_stats") or {}
        for entry in stats.get("missing_visuals") or []:
            if not isinstance(entry, dict):
                continue
            number = entry.get("sentence_number")
            if number is None:
                continue
            outcome = entry.get("graphics_outcome") or {}
            if not outcome.get("outcome"):
                # Never adjudicated by the graphics stage — not a
                # post-graphics repair candidate (see docstring).
                continue
            if outcome.get("outcome") == "generated":
                continue
            if number in attached:
                continue
            if index_by_number.get(number) in attached_indexes:
                continue
            still.append(
                {
                    "scene_number": scene_number,
                    "sentence_number": number,
                    "entry": entry,
                }
            )
    return still


def _run_coverage_repair(processed_scenes, project_dir, repair_fn=None):
    """Gate 2 (Blocker 3): the bounded pre-render coverage repair
    pass. Runs after motion_designer, before the video_editor
    coverage gate: for each scene that still has a missing-visual
    sentence after graphics, make ONE repair attempt through the
    pipeline's existing asset machinery (process_scene_media —
    the same entry point asset_manager uses; no discovery is
    reimplemented here, and the attempt spends the existing
    budgets, including the tier ladder's per-sentence reserve).

    Merging is conservative: visuals are unioned by path (nothing
    the earlier stages produced is dropped, attached graphics
    included), and a missing_visuals entry is removed only when
    the repair's own missing_visuals record no longer lists the
    sentence. Entries the repair still lists are replaced by the
    repair's fresher record, keeping the graphics outcome stamp.
    A scene whose repair raises keeps its entries and records the
    error. Returns the repair report (also the metrics record)."""
    if repair_fn is None:
        repair_fn = process_scene_media
    still = _sentences_still_missing(processed_scenes)
    report = {
        "attempted": bool(still),
        "missing_before": [
            {
                "scene_number": item["scene_number"],
                "sentence_number": item["sentence_number"],
            }
            for item in still
        ],
        "scenes_attempted": [],
        "repaired": [],
        "still_missing": [
            {
                "scene_number": item["scene_number"],
                "sentence_number": item["sentence_number"],
            }
            for item in still
        ],
        "errors": [],
    }
    if not still:
        return report

    missing_by_scene = {}
    for item in still:
        missing_by_scene.setdefault(item["scene_number"], set()).add(
            item["sentence_number"]
        )

    for processed_scene in processed_scenes or []:
        if not isinstance(processed_scene, dict):
            continue
        scene = processed_scene.get("scene") or {}
        scene_number = scene.get("scene_number")
        wanted = missing_by_scene.get(scene_number)
        if not wanted:
            continue
        report["scenes_attempted"].append(scene_number)
        try:
            result = repair_fn(scene, str(project_dir))
        except Exception as error:
            report["errors"].append(
                {
                    "scene_number": scene_number,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            continue
        if not isinstance(result, dict):
            report["errors"].append(
                {
                    "scene_number": scene_number,
                    "error": "repair returned no result",
                }
            )
            continue

        for kind in ("videos", "images"):
            existing = processed_scene.setdefault(kind, [])
            seen = {
                visual.get("file_path") or visual.get("path")
                for visual in existing
                if isinstance(visual, dict)
            }
            for visual in result.get(kind) or []:
                if not isinstance(visual, dict):
                    continue
                key = visual.get("file_path") or visual.get("path")
                if key and key in seen:
                    continue
                existing.append(visual)
                seen.add(key)

        repair_stats = result.get("asset_stats") or {}
        repair_missing = {
            entry.get("sentence_number"): entry
            for entry in repair_stats.get("missing_visuals") or []
            if isinstance(entry, dict)
        }
        stats = processed_scene.setdefault("asset_stats", {})
        kept = []
        for entry in stats.get("missing_visuals") or []:
            if not isinstance(entry, dict) or (
                entry.get("sentence_number") not in wanted
            ):
                kept.append(entry)
                continue
            number = entry.get("sentence_number")
            if number not in repair_missing:
                report["repaired"].append(
                    {
                        "scene_number": scene_number,
                        "sentence_number": number,
                    }
                )
                continue
            fresh = dict(repair_missing[number])
            if entry.get("graphics_outcome") is not None:
                fresh["graphics_outcome"] = entry["graphics_outcome"]
            fresh["coverage_repair"] = "attempted_still_missing"
            kept.append(fresh)
        stats["missing_visuals"] = kept

    report["still_missing"] = [
        {
            "scene_number": item["scene_number"],
            "sentence_number": item["sentence_number"],
        }
        for item in _sentences_still_missing(processed_scenes)
    ]
    return report


def _honour_final_audit(final_audit, state=None):
    """INT-102 / MA-14 + Phase 4: the completion gate over the
    auditor's verdict.

    The gate is TOTAL over verdict shapes. A production may be
    recorded as completed only when the auditor actually evaluated
    the film:

    - unevaluated / missing / malformed verdict -> RuntimeError.
      An audit that never ran is not a successful audit: the
      previous gate consulted the verdict only when "evaluated"
      was truthy, so the auditor's unevaluated shape (no asset
      report) silently bypassed the gate and the run completed.
    - Phase 4 status separation: the evaluated verdict's failures
      are split by services.release_status. TECHNICAL-class
      failures (including any unrecognised code — fail-closed)
      still fail the run: RuntimeError carrying the technical
      reasons only. RELEASE-class failures, and the verdict's own
      "release_failures" (the classification audit's findings),
      do NOT raise: the film is technically deliverable, and the
      findings ride the completed run as release findings (the
      caller derives them via release_status.derive_run_statuses;
      when state is supplied here they are also stamped onto it
      as state["audit_release_failures"]).
    - An evaluated verdict that is not passed but carries no
      failures and no release findings at all still fails closed
      ("unspecified") — a condemned film with no stated reason is
      never waved through.

    Returns the split dict {"technical": [...], "release": [...]}.
    """
    verdict = (final_audit or {}).get("verdict") or {}
    if not verdict.get("evaluated"):
        raise RuntimeError(
            "Final video audit NOT EVALUATED: the auditor returned "
            "no evaluated verdict, so this production cannot be "
            "recorded as completed."
        )
    split = release_status.split_audit_failures(verdict.get("failures") or [])
    verdict_release_failures = list(verdict.get("release_failures") or [])
    if isinstance(state, dict):
        state["audit_release_failures"] = (
            split["release"] + verdict_release_failures
        )
    if split["technical"]:
        raise RuntimeError(
            "Final video audit FAILED: "
            + "; ".join(split["technical"])
        )
    if (
        not verdict.get("passed", False)
        and not (verdict.get("failures") or [])
        and not verdict_release_failures
    ):
        raise RuntimeError(
            "Final video audit FAILED: unspecified"
        )
    return split


def _honour_quality_verdict(quality_verdicts, state=None):
    """Phase 4: the quality-verdict seam after the final audit.

    C-06 (Master Project Directive Phase 3b) made a composite
    QUALITY verdict of FAIL (services.quality_verdict) fail the
    run outright. Phase 4's technical/release status separation
    supersedes that gate: a quality FAIL is a RELEASE finding,
    not a technical failure — the film is technically deliverable,
    and the FAIL is recorded on the completed run through
    release_status.derive_run_statuses (release_status
    "not_release_grade", with the verdict carried in the release
    findings). The billing consequence of a delivered-but-not-
    release-grade film is founder-gated and preserved separately;
    this seam decides nothing about money.

    Behaviour:

    - quality verdict FAIL -> NO raise. The full quality verdict
      is stamped onto the run state (state["quality_verdict"]) so
      it survives in production_state.json, and it already rides
      the asset report / production report written before this
      seam runs — the verdict stays preserved and visible on the
      completed run. The quality verdict dict is returned.
    - PASS / QUALITY_WARNING -> return; the run proceeds exactly
      as before (warnings have never failed a run).
    - verdict absent / None / unmeasured (measurement failed and
      was recorded as visual_quality_error, or a legacy report
      shape) -> return None; absence behaves exactly as it did
      before C-06.

    No verdict types are added and no band or threshold changes:
    this consumes quality_verdict.FAIL exactly as
    composite_quality_verdict computes it.

    Accepts either the production report's verdicts dict
    ({"quality": {...}, "integrity": ..., "reliability": ...}) or a
    bare quality-verdict dict ({"verdict": ...}). Returns the
    quality verdict dict it inspected, or None.
    """
    quality = None
    if isinstance(quality_verdicts, dict):
        nested = quality_verdicts.get("quality")
        if isinstance(nested, dict):
            quality = nested
        elif "verdict" in quality_verdicts:
            quality = quality_verdicts
    if quality is None:
        return None
    if quality.get("verdict") == "FAIL" and isinstance(state, dict):
        state["quality_verdict"] = quality
    return quality


def _research_stage_record(source):
    """C-07 (Phase 3b): the research pass's own stage outcome,
    derived from the bible's research_meta (services.story_
    understanding.enrich_bible_with_research).

    Research is not a dispatcher stage of its own — it executes
    inside script analysis (plan_script, after the bible is built).
    What C-07 changes is the RECORDING: the outcome is extracted
    here and stored as its own record on the run (production state,
    under the script_analysis stage record and at state level, and
    in the asset report), so a research pass that gave up after the
    failure limit is an explicit degraded marker on the run instead
    of being silently folded into script_analysis's success.

    `source` may be a production_data dict ({"bible": ...} and/or
    {"analysis": {"bible": ...}}), an analysis dict, or a bible.
    Returns a bounded record; a bible with no research_meta (legacy
    / pre-3A artefact) yields outcome "not_recorded" — never a
    fabricated success.
    """
    bible = {}
    if isinstance(source, dict):
        if isinstance(source.get("bible"), dict):
            bible = source["bible"]
        analysis = source.get("analysis")
        if isinstance(analysis, dict) and isinstance(
            analysis.get("bible"), dict
        ):
            bible = analysis["bible"]
        elif "research_meta" in source:
            bible = source
    meta = bible.get("research_meta") if isinstance(bible, dict) else None
    if not isinstance(meta, dict):
        return {
            "stage": "research",
            "recorded": False,
            "outcome": "not_recorded",
            "degraded": False,
        }
    errors = meta.get("errors")
    return {
        "stage": "research",
        "recorded": True,
        "outcome": meta.get("outcome") or "recorded",
        "degraded": bool(meta.get("degraded")),
        "researched_at": meta.get("researched_at"),
        "subjects_researched": meta.get("subjects_researched"),
        "sources_attached": meta.get("sources_attached"),
        "conflicts_found": meta.get("conflicts_found"),
        "queries_attempted": meta.get("queries_attempted"),
        "queries_failed": meta.get("queries_failed"),
        "failure_limit": meta.get("failure_limit"),
        "failure_limit_reached": bool(meta.get("failure_limit_reached")),
        "errors": list(errors)[:10] if isinstance(errors, list) else [],
        "error": meta.get("error"),
    }


def _record_research_outcome(state, source):
    """Stamp the research stage record onto the run state (C-07):
    at state level (state["research"]) and inside the
    script_analysis stage record, so the degraded marker is visible
    in production_state.json alongside the stage outcomes. Prints a
    loud line when degraded. Returns the record."""
    record = _research_stage_record(source)
    if isinstance(state, dict):
        state["research"] = record
        stages = state.setdefault("stages", {})
        stage_record = stages.setdefault("script_analysis", {})
        if isinstance(stage_record, dict):
            stage_record["research"] = record
    if record.get("degraded"):
        print(
            "RESEARCH DEGRADED: the research pass outcome was "
            f"{record.get('outcome')} (attempted="
            f"{record.get('queries_attempted')}, failed="
            f"{record.get('queries_failed')}, failure_limit_reached="
            f"{record.get('failure_limit_reached')}); this run is "
            "proceeding on a partially research-grounded bible and "
            "the run record says so."
        )
    return record


def run_full_pipeline(project_id: str, script: str, run_id: str = None, options=None):
    logging_setup.log_event(
        logger,
        logging.INFO,
        "run_started",
        project_id=project_id,
        run_id=run_id,
        entrypoint="run_full_pipeline",
    )

    project_dir = media_paths.project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)

    run_metrics.begin_run()

    # Stage 3 options plumbing: recognised keys "music_file" (path),
    # "music_enabled" (bool), "music_volume" / "narration_volume"
    # (0.0-2.0), "voice_id" (str), "captions"
    # ("burn"|"sidecar"|"off", default "burn"). Unknown keys pass
    # through harmlessly into the ledger/manifest options snapshot.
    _merged_options = {"entrypoint": "run_full_pipeline"}
    if isinstance(options, dict):
        _merged_options.update(options)
    options = _merged_options
    captions_mode = str(options.get("captions") or "burn").lower()
    if captions_mode not in ("burn", "sidecar", "off"):
        captions_mode = "burn"
    # Wave 1 captions (plan E.2): karaoke sync level for the ASS
    # renderer -- "word" (default) / "syllable" (derived timing,
    # stamped as such) / "off" (plain).
    caption_sync = str(options.get("caption_sync") or "word").lower()
    if caption_sync not in ("word", "syllable", "off"):
        caption_sync = "word"
    if caption_sync == "syllable":
        # C-13 (Phase 7): syllable sync is derived timing and is
        # no longer a user-facing option. A legacy/stored request
        # is accepted and demoted to the auto word sync (never a
        # hard failure); the renderer's internal derived-syllable
        # mechanism and its honesty stamp remain for diagnostics.
        caption_sync = "word"

    # Stage 5A: resolve the genre pack BEFORE the ledger run
    # begins, so the resolved pack id lands in the run's options
    # snapshot and therefore in the run manifest. FS-2: the pack
    # is backend-derived from the script (the conservative script
    # detector, else "general") — user-fed genre keys are stripped
    # before the resolver sees the options (see
    # _options_for_genre_resolution), so no stored category label
    # or legacy options.genre can act as a pack choice.
    _genre_info = genre_packs.resolve_genre_pack(
        _options_for_genre_resolution(options), script
    )
    _genre_pack = _genre_info["pack"]
    options["genre_pack"] = _genre_info["pack_id"]
    options["genre_source"] = _genre_info["source"]
    # Stage 7 / R7: the resolved visual profile is data, recorded in
    # the same options snapshot — which profile supplied the values,
    # any validation failures that forced the fail-closed fallback
    # to the general profile, and the pack's visual theme.
    options["visual_profile_source"] = _genre_info.get(
        "visual_profile_source"
    )
    options["visual_profile_failures"] = list(
        _genre_info.get("visual_profile_failures") or []
    )
    options["visual_theme"] = _genre_info.get("visual_theme")
    print(
        f"Genre pack: {_genre_info['pack_id']} "
        f"(source: {_genre_info['source']})"
    )

    # Phase 9 (§13.3/§14.2): a configured visual theme overrides
    # the pack's theme for every theme consumer in this run
    # (Visual Director, graphics build, caption renderer). The
    # override travels as a shallow pack copy — visual_theme
    # only — so the pack's composition affinities, template
    # data and mix preset ride untouched. With no configured
    # theme, _theme_pack IS the pack and behaviour is unchanged.
    from services import production_config as _production_config

    _config = _production_config.stored_config(options)
    _theme_pack, _theme_resolution = (
        _production_config.theme_pack_override(_config, _genre_pack)
    )
    if _theme_resolution["founder_id"] is not None:
        options["visual_theme"] = _theme_resolution["founder_id"]
        options["visual_theme_engine"] = _theme_resolution["engine_name"]
        options["visual_theme_source"] = "production_config"
        print(
            "Visual theme: "
            f"{_theme_resolution['founder_id']} "
            f"(engine: {_theme_resolution['engine_name']}, "
            "source: production_config)"
        )

    # Stage 2: claim or begin the ledger run. When the caller
    # carries a run_id (the enqueue path), the queued run is claimed
    # atomically; a run whose state is KNOWN to be non-claimable
    # (cancelled while queued, claimed by another delivery, already
    # finished) is not executed twice. The claim happens BEFORE any
    # status write-back so an aborted duplicate never marks the
    # project "running".
    #
    # B-02 (Phase 5a): the previous fall-through executed the run
    # UNCLAIMED when the claim was lost AND the follow-up read
    # could not see the run (a lock burst made get_run fail-soft
    # to None) — the production ran with no owner, no heartbeat
    # and no claim record, its DB status still reading "queued"
    # while it executed, free to be claimed and executed AGAIN by
    # the next delivery. That is the claim-status loss the audit
    # pinned, and it is now closed in code: a caller-carried run
    # that could not be claimed and whose state cannot be proven
    # is REFUSED, loudly, with an explicit record. The RQ job is
    # not retried automatically; the run stays queued in the
    # ledger, visible to the dispatcher/backlog, instead of
    # executing invisibly. (Caller-less runs still begin_run
    # fail-soft below: with no pre-existing run row there is no
    # claim to lose and no duplicate-execution hazard.)
    if run_id is not None:
        # Stage 4: a plan-approved run is claimed from
        # "awaiting_approval" (its analysis phase already ran and the
        # approval released it); any other caller-carried run claims
        # from "queued" as before.
        _claim_from = (
            ("awaiting_approval",)
            if options.get("plan_approved")
            else ("queued",)
        )
        claimed = run_ledger.claim_run(run_id, from_statuses=_claim_from)
        if claimed is None:
            existing = run_ledger.get_run(run_id)
            if existing is not None:
                logging_setup.log_event(
                    logger,
                    logging.WARNING,
                    "run_not_claimable",
                    project_id=project_id,
                    run_id=run_id,
                    status=existing["status"],
                )
                return {
                    "project_id": project_id,
                    "status": existing["status"],
                    "run_id": run_id,
                    "deduplicated_execution": True,
                }
            logging_setup.log_event(
                logger,
                logging.WARNING,
                "run_claim_refused_unreadable",
                project_id=project_id,
                run_id=run_id,
            )
            return {
                "project_id": project_id,
                "status": "unclaimed_execution_refused",
                "run_id": run_id,
                "deduplicated_execution": True,
            }
        _run_id = run_id
        logging_setup.log_event(
            logger,
            logging.INFO,
            "run_claimed",
            project_id=project_id,
            run_id=_run_id,
        )
    else:
        _run_id = run_ledger.begin_run(
            project_id,
            script,
            options=_merged_options,
        )

    # Phase 8.4: bind this run's metrics state to its ledger id —
    # counters recorded from here on belong to THIS run even when
    # another production shares the process.
    if _run_id:
        run_metrics.rebind_run(_run_id)

    # Stage 3 (R2): the run is ours (claimed atomically or begun
    # here) — beat for it until it leaves "running", so liveness
    # tooling and heartbeat-aware reconciliation can tell a live
    # production from an orphan. The thread is self-terminating
    # and fail-soft; it never gates the pipeline.
    run_ledger.start_heartbeat(_run_id)

    # Phase 1 durability: when this process is RE-ENTERING a run
    # (a heartbeat-stale run the reconciler requeued and this
    # worker claimed again — same run id), the run's own previous
    # production_state.json still holds the dead execution's stage
    # records. Capture it BEFORE the fresh state overwrites the
    # file below; stage_resume.plan_self_resume validates those
    # records (artefact hashes + coverage gate) into a self-resume
    # plan. A first entry finds no file (or an unreadable one) and
    # this stays None — behaviour is then exactly the cross-run
    # adoption path.
    _prior_state = None
    if _run_id:
        try:
            _prior_state_path = (
                media_paths.run_subdir(project_dir, _run_id)
                / "production_state.json"
            )
            if _prior_state_path.is_file():
                _prior_state = json.loads(
                    _prior_state_path.read_text(encoding="utf-8")
                )
                if not isinstance(_prior_state, dict):
                    _prior_state = None
        except Exception:
            _prior_state = None

    state = create_production_state()
    state["genre_pack"] = {
        "id": _genre_info["pack_id"],
        "source": _genre_info["source"],
        "requested": _genre_info["requested"],
    }
    # Stage 4 (DATA-004): the run's derived artefacts live in its
    # own directory (media/<project>/runs/<run_id>/) from the very
    # first write, so no later run of this project can overwrite
    # them. The state carries _run_id from the start so every
    # save_production_state in this run resolves there too. When
    # there is no ledger run (fail-soft begin), the project-level
    # directory remains the artefact home, exactly as before.
    state["_run_id"] = _run_id
    artefact_dir = (
        media_paths.run_subdir(project_dir, _run_id)
        if _run_id
        else project_dir
    )
    artefact_dir.mkdir(parents=True, exist_ok=True)
    save_production_state(project_dir, state)
    _set_project_status(project_id, "running")

    # Stage 2 (continued): per-run context (2C) + run manifest (2E).
    _run_context = run_context.RunContext(
        run_id=_run_id, project_id=str(project_id)
    )
    state["_run_context_previous"] = run_context.activate(_run_context)
    _manifest = run_ledger.build_initial_manifest(
        _run_id,
        project_id,
        script,
        options=_merged_options,
        sentence_count=_script_sentence_count(script),
    )
    state["_manifest"] = _manifest
    run_ledger.write_manifest(artefact_dir, _manifest)

    # Transformation Phase 5.1 (PERF-001): stage-level resume. A
    # prior run of this project with the identical script + options
    # may donate its completed stages; the plan validates each
    # stage's recorded artefacts (existence + sha256) in order and
    # stops at the first failure. The plan's record rides on the
    # state (production_state.json) and the manifest — adopted vs
    # executed stages stay visible in the run's ledger artefacts.
    _resume_plan = stage_resume.plan_resume(
        project_dir,
        _run_id,
        _manifest.get("script_hash"),
        _manifest.get("options"),
    )
    # Phase 1 durability: a re-entered run's OWN validated stages
    # take precedence over any cross-run donation — they are this
    # production's own work, recorded by the execution that died,
    # and its later stages depend on its own earlier artefacts.
    # The self plan is used only when it validates at least one
    # stage; otherwise the cross-run plan stands.
    if _prior_state is not None and _run_id:
        _self_plan = stage_resume.plan_self_resume(
            project_dir, _run_id, _prior_state
        )
        if _self_plan.get("stages"):
            _self_plan["record"] = {
                "source_run_id": _self_plan.get("source_run_id"),
                "adopted_stages": [],
                "executed_stages": [],
                "fallback": _self_plan.get("fallback"),
                "self_resume": True,
            }
            _resume_plan = _self_plan
            print(
                "RESUME: this run's own prior execution can donate "
                f"stages: {sorted(_resume_plan['stages'])}"
                + (
                    f" (chain stops: {_resume_plan['fallback']})"
                    if _resume_plan.get("fallback")
                    else ""
                )
            )
    state["resume"] = _resume_plan["record"]
    _manifest["resume"] = _resume_plan["record"]
    if _resume_plan.get("source_run_id"):
        print(
            "RESUME: prior run "
            f"{_resume_plan['source_run_id']} can donate stages: "
            f"{sorted(_resume_plan['stages'])}"
            + (
                f" (chain stops: {_resume_plan['fallback']})"
                if _resume_plan.get("fallback")
                else ""
            )
        )
    elif _resume_plan.get("fallback"):
        print(f"RESUME: no adoption ({_resume_plan['fallback']}).")
    save_production_state(project_dir, state)

    processed_scenes = []
    scenes = []
    sentences = []
    asset_report = None
    script_hash = script_hash_for(script)

    narration_file = project_dir / "narration.wav"
    raw_video = project_dir / "documentary.mp4"
    narrated_video = project_dir / "documentary_with_narration.mp4"
    music_video = project_dir / "documentary_with_music.mp4"
    soundtrack_file = project_dir / "soundtrack.wav"
    captions_json = project_dir / "captions.json"
    captions_ass = project_dir / "captions.ass"
    captioned_video = project_dir / "documentary_captioned.mp4"
    # Stage 4 (DATA-004, D3.9 Phase 2): the production final is a
    # run-scoped artefact. The project-level film path survives only
    # as the media index's latest-run view (resolved at read time),
    # never as a physical file a later run overwrites.
    final_video = artefact_dir / "documentary_final_output.mp4"

    timeline_file = None
    expected_duration = None
    # Stage 5 (R4): the measured parts manifest data for this run,
    # loaded by the voice_actor stage and handed to the timeline
    # build — the run's single timing authority.
    parts_manifest_data = None
    soundtrack_record = None
    # The exact narration file the soundtrack was mixed from —
    # the M10 provenance reference (volume-adjusted when the
    # narration_volume option was applied).
    soundtrack_reference = narration_file

    try:
        run_stage(state, "voice_actor", project_dir)
        _voice_payload = stage_resume.load_stage_payload(
            _resume_plan, "voice_actor"
        )
        if _voice_payload is not None:
            parts_manifest_data = (
                narration_service.load_parts_manifest_data(
                    project_dir / "narration_parts" / "parts_manifest.json"
                )
            )
            if parts_manifest_data is not None:
                for _key, _value in (
                    _voice_payload.get("state") or {}
                ).items():
                    state[_key] = _value
                _adopt_stage(state, "voice_actor", project_dir, _resume_plan)
                raise _StageAdopted("voice_actor")
            print(
                "RESUME: voice_actor payload present but the parts "
                "manifest would not load — executing the stage."
            )
        print("Voice Actor")
        # Narration is synthesised at the voice provider's native
        # rate — there is no speed adjustment anywhere in the call
        # path (founder ruling, 2026-11-22, retiring Phase 2.4's
        # per-pack pace). A historical run option carrying a
        # "narration_speed" key is ignored here, harmlessly. The
        # second voice is assigned to quoted/interview beats only
        # (narration.assign_sentence_voices returns None when no
        # beat qualifies, keeping the single-voice path exact).
        _sentence_voices = narration_service.assign_sentence_voices(
            script, primary_voice_id=options.get("voice_id")
        )
        voice_meta = generate_narration(
            text=script,
            output_path=str(narration_file),
            voice_id=options.get("voice_id"),
            sentence_voices=_sentence_voices,
        )
        if isinstance(voice_meta, dict):
            state["voice"] = voice_meta
            print(f"Voice: {voice_meta}")
        if not narration_file.exists():
            raise RuntimeError("narration.wav was not created.")
        print(f"Voiceover created: {narration_file}")
        # Stage 5 (R4): load the measured parts manifest — the
        # run's timing authority (v1 entries upgraded by
        # measurement on load). It is handed to build_captions
        # (event placement inside measured windows) and to the
        # timeline build (the windows themselves).
        parts_manifest_data = narration_service.load_parts_manifest_data(
            project_dir / "narration_parts" / "parts_manifest.json"
        )
        if parts_manifest_data is None:
            raise RuntimeError(
                "timing_unavailable: the narration parts manifest "
                "was not written by generate_narration."
            )
        # Stage 3I: captions.py is the single caption writer —
        # Whisper word timings place caption events inside the
        # measured windows when usable; otherwise single events
        # on the measured windows, stamped provisional (Stage 5);
        # SRT/VTT sidecars always.
        captions_data = build_captions(
            str(narration_file),
            script,
            str(captions_json),
            parts_manifest=parts_manifest_data,
        )
        if isinstance(captions_data, dict) and isinstance(
            captions_data.get("captions_alignment"), dict
        ):
            # Annex D2.2.6: the alignment aggregate is a run-level
            # record — a film with approximate captions says so.
            state["captions_alignment"] = captions_data[
                "captions_alignment"
            ]
        state["caption_sync"] = caption_sync
        if caption_sync == "syllable":
            # Plan E.3 honesty stamp: syllable timing is derived
            # from measured word spans, never measured.
            state["syllable_timing"] = "derived"
        # Annex D2.2.3: Σ parts vs the concatenated narration file.
        # Divergence beyond tolerance is recorded (state -> asset
        # report) and printed — never silently absorbed.
        _concat_facts = media_probe.probe_audio(narration_file)
        if _concat_facts is not None:
            _consistency = timing_service.timing_consistency(
                timing_service.total_duration(parts_manifest_data),
                _concat_facts["duration_seconds"],
            )
            state["timing_consistency"] = _consistency
            if not _consistency["within_tolerance"]:
                print(
                    "WARNING: narration timing consistency — parts "
                    f"total {_consistency['parts_total_seconds']}s "
                    "vs concatenated file "
                    f"{_consistency['concatenated_duration_seconds']}s "
                    f"(divergence {_consistency['divergence_seconds']}s "
                    f"> tolerance {_consistency['tolerance_seconds']}s); "
                    "recorded in the asset report."
                )
        if not captions_json.exists():
            raise RuntimeError("Caption transcription failed.")
        print(f"Captions generated: {captions_json}")
        try:
            from services.narration import _split_sentences as _split_for_validation

            caption_validation = validate_captions(
                str(captions_json), _split_for_validation(script)
            )
        except Exception as validation_error:
            caption_validation = {"ok": False, "error": str(validation_error)}
        state["captions_validation"] = caption_validation
        if not caption_validation.get("ok"):
            print(
                "WARNING: caption validation found uncovered segments: "
                f"{caption_validation}"
            )
        _finish_executed_stage(
            state,
            "voice_actor",
            project_dir,
            artefact_dir,
            payload={
                "state": {
                    _key: state[_key]
                    for _key in (
                        "voice",
                        "captions_alignment",
                        "caption_sync",
                        "syllable_timing",
                        "timing_consistency",
                        "captions_validation",
                    )
                    if _key in state
                }
            },
        )
    except _StageAdopted:
        pass
    except Exception as error:
        fail_pipeline(state, "voice_actor", error, project_dir)

    try:
        run_stage(state, "script_analysis", project_dir)
        _analysis_payload = stage_resume.load_stage_payload(
            _resume_plan, "script_analysis"
        )
        if _analysis_payload is not None and isinstance(
            _analysis_payload.get("production_data"), dict
        ):
            production_data = _analysis_payload["production_data"]
            analysis = production_data["analysis"]
            state["_partial"] = {
                "processed_scenes": processed_scenes,
                "analysis": analysis,
                "timeline_result": None,
            }
            sentences = production_data["sentences"]
            scenes = production_data["scenes"]
            script_hash = production_data.get("script_hash") or script_hash
            # C-07: an adopted analysis still carries its research
            # outcome — record it on this run too.
            _record_research_outcome(state, production_data)
            _adopt_stage(
                state, "script_analysis", project_dir, _resume_plan
            )
            raise _StageAdopted("script_analysis")
        print("Analyzing documentary script...")
        production_data = build_production_scenes(
            script,
            project_id=project_id,
            project_dir=project_dir,
            genre_pack=_genre_pack,
        )
        analysis = production_data["analysis"]
        # VA-07: keep references to the accumulating run state so a
        # later failure can still produce an asset report (the lists
        # are mutated in place as the pipeline progresses).
        state["_partial"] = {
            "processed_scenes": processed_scenes,
            "analysis": analysis,
            "timeline_result": None,
        }
        sentences = production_data["sentences"]
        scenes = production_data["scenes"]
        script_hash = production_data.get("script_hash") or script_hash
        # C-07 (Phase 3b): record the research pass's outcome as its
        # own stage record on the run BEFORE script_analysis is
        # finished — a degraded research pass must not be folded
        # silently into this stage's success.
        _record_research_outcome(state, production_data)
        print(f"Analyzed sentences: {len(sentences)}")
        print(f"Created production scenes: {len(scenes)}")
        print(f"Script hash: {script_hash}")
        if production_data.get("case_bible_path"):
            print(f"Case Bible: {production_data['case_bible_path']}")
        for scene in scenes:
            print(
                f"Scene {scene['scene_number']}: "
                f"{scene['sentence_count']} sentences"
            )
        # Stage 4: persist the plan document (fail-soft) so the
        # studio can serve this run's production plan.
        from services import plan_document as _plan_document

        _plan_document.write_plan_document(project_dir, analysis, project_id)
        # Master Directive Phase 2 (§4.2): see the analysis-phase
        # write — visual_plan.json v1, additive and fail-soft.
        _plan_document.write_visual_plan_document(
            project_dir,
            analysis,
            project_id,
            scene_governor=production_data.get("scene_governor"),
        )
        _finish_executed_stage(
            state,
            "script_analysis",
            project_dir,
            artefact_dir,
            payload={"production_data": production_data},
        )
    except _StageAdopted:
        pass
    except Exception as error:
        fail_pipeline(state, "script_analysis", error, project_dir)

    try:
        run_stage(state, "asset_manager", project_dir)
        _assets_payload = stage_resume.load_stage_payload(
            _resume_plan, "asset_manager"
        )
        if _assets_payload is not None and isinstance(
            _assets_payload.get("processed_scenes"), list
        ):
            from services.visual_registry import get_registry as _adopt_registry

            processed_scenes.extend(_assets_payload["processed_scenes"])
            # AM-020: restore the visual registry's usage state so
            # reuse enforcement sees the adopted stage's usage.
            _adopt_registry(project_id).restore_state(
                _assets_payload.get("registry") or {}
            )
            _adopt_stage(state, "asset_manager", project_dir, _resume_plan)
            raise _StageAdopted("asset_manager")
        print("Asset Manager")
        import json as _json
        import time as _time
        from services import source_cache as _source_cache
        from services.visual_registry import get_registry as _get_registry

        _source_cache.reset_stats()
        _am_started = _time.time()
        _am_scenes = []
        _am_prev = _source_cache.stats_summary()
        reset_registry(project_id)
        reset_media_caches()
        print(f"Visual uniqueness registry reset for project {project_id}.")
        print()

        # Phase 1 durability: scene-level checkpoint resume. When
        # THIS run's asset stage previously committed scenes before
        # its worker died, the checkpoint (this run's artefact dir,
        # script-hash matched, selected-asset hashes re-verified by
        # stage_resume.load_asset_progress) restores the registry
        # to its committed state and seeds processed_scenes with
        # the committed results; the loop below then skips exactly
        # those scenes. A scene that was mid-flight at the death
        # is not in the checkpoint, so it re-executes against the
        # restored registry — no usage is double-registered and no
        # committed scene's discovery is repeated.
        _am_committed = set()
        _progress = stage_resume.load_asset_progress(
            artefact_dir, project_dir, script_hash=script_hash
        )
        if _progress is not None:
            _get_registry(project_id).restore_state(
                _progress.get("registry") or {}
            )
            processed_scenes.extend(_progress["processed_scenes"])
            _am_committed = set(_progress["committed_scenes"])
            print(
                "ASSET RESUME: checkpoint restored — "
                f"{len(_am_committed)} committed scene(s) "
                f"{sorted(_am_committed)} will be skipped, registry "
                "state restored from the checkpoint."
            )

        for scene in scenes:
            scene_number = scene["scene_number"]
            scene["project_id"] = project_id
            scene["script_hash"] = script_hash
            if scene_number in _am_committed:
                print(
                    f"Scene {scene_number}: skipped — committed "
                    "before the interruption (checkpoint)."
                )
                continue
            print()
            print("=" * 60)
            print(f"PROCESSING SCENE {scene_number}/{len(scenes)}")
            print("=" * 60)

            _scene_started = _time.time()
            result = process_scene_media(scene, str(project_dir))
            _scene_seconds = round(_time.time() - _scene_started, 1)
            _now = _source_cache.stats_summary()
            _delta = {k: _now.get(k, 0) - _am_prev.get(k, 0) for k in _now}
            _am_prev = _now
            _am_scenes.append({"scene": scene_number, "seconds": _scene_seconds, "stats": _delta})
            print(f"ASSET METRICS scene {scene_number}: {_scene_seconds}s {_delta}")
            result["duration_seconds"] = scene.get("duration_seconds", 10)
            result["scene"] = scene
            result["project_id"] = project_id
            result["script_hash"] = script_hash
            processed_scenes.append(result)

            # Phase 1: commit this scene durably before starting
            # the next — processed scenes + registry export, the
            # same state the completed stage payload will carry.
            # Fail-soft (record_asset_progress never raises): a
            # checkpoint failure costs a re-run of this scene after
            # a crash, never the production.
            stage_resume.record_asset_progress(
                artefact_dir,
                project_dir,
                script_hash=script_hash,
                processed_scenes=processed_scenes,
                registry_state=_get_registry(project_id).export_state(),
            )

            print(
                f"Scene {scene_number}: "
                f"{len(result.get('images', []))} images, "
                f"{len(result.get('videos', []))} videos"
            )

        print("Asset Manager completed.")
        _am_total = _time.time() - _am_started
        _am_final = _source_cache.stats_summary()
        _am_report = {
            "project_id": project_id,
            "asset_manager_seconds": round(_am_total, 1),
            "asset_manager_minutes": round(_am_total / 60, 1),
            "stats": _am_final,
            "scenes": _am_scenes,
            "resumed_scenes": sorted(_am_committed),
            "registry": _get_registry(project_id).summary(),
        }
        write_json_atomic(
            project_dir / "asset_manager_metrics.json",
            _am_report,
            ensure_ascii=True,
        )
        print("=" * 60)
        print(f"ASSET MANAGER METRICS: {_am_report['asset_manager_minutes']} min")
        print(_json.dumps(_am_final))
        print("=" * 60)
        _finish_executed_stage(
            state,
            "asset_manager",
            project_dir,
            artefact_dir,
            payload={
                "processed_scenes": processed_scenes,
                "registry": _get_registry(project_id).export_state(),
            },
        )
        # The completed stage payload supersedes the incremental
        # checkpoint (Phase 1): drop it so a later re-entry never
        # mistakes mid-stage progress for a finished stage.
        stage_resume.clear_asset_progress(artefact_dir)
    except _StageAdopted:
        pass
    except Exception as error:
        fail_pipeline(state, "asset_manager", error, project_dir)

    try:
        # Motion Designer temporarily disabled
        print("Motion Designer")
        # AUD-30: this stage previously only ever called
        # finish_stage — it never recorded a start. Run it through
        # the same stage lifecycle as every other stage.
        run_stage(state, "motion_designer", project_dir)
        _motion_payload = stage_resume.load_stage_payload(
            _resume_plan, "motion_designer"
        )
        if _motion_payload is not None and isinstance(
            _motion_payload.get("processed_scenes"), list
        ):
            # The motion stage's payload SUPERSEDES the asset
            # stage's: it is the same scenes with graphics attached.
            processed_scenes.clear()
            processed_scenes.extend(_motion_payload["processed_scenes"])
            _adopt_stage(state, "motion_designer", project_dir, _resume_plan)
            raise _StageAdopted("motion_designer")

        motion_data = {
            "case_bible": analysis.get("bible") or {},
            "scenes": scenes,
            "processed_scenes": processed_scenes,
        }

        # Stage A (targeting): the Asset Manager records every
        # sentence that finished retrieval + coverage recovery with
        # no verified visual in each processed scene's
        # asset_stats["missing_visuals"] (sentence_number based).
        # Translate those records into graphics targets —
        # scene_number + scene-local sentence_index + the sentence's
        # own text — so the graphics stage can answer exactly the
        # sentences that have no verified visual. Data propagation
        # only: no retrieval or verification behaviour changes here.
        graphic_targets = _build_graphic_targets(processed_scenes)

        if graphic_targets:
            print(
                f"Graphics targets (uncovered sentences): "
                f"{len(graphic_targets)}"
            )

        # Phase 9 (§13.1/§14.2) + FS-3: the run's motion-graphics
        # configuration. The on/off toggle comes from the run's
        # production config snapshot; the intensity is backend-
        # auto — derived by production_config from this
        # production's own graphics demand (how many narration
        # sentences graphics must answer, out of how many, and
        # how chronology-dense the case bible is). Any intensity
        # value a user or a stored config once carried is inert.
        # Recorded on the production state so the report shows
        # which derived intensity actually ran.
        _graphics_context = {
            "genre_pack_id": _genre_info["pack_id"],
            "scene_count": len(processed_scenes),
            "sentence_count": sum(
                len(((ps.get("scene") or {}).get("sentences")) or [])
                for ps in processed_scenes
            ),
            "graphic_target_count": len(graphic_targets),
            "timeline_event_count": len(
                (analysis.get("bible") or {}).get("timeline") or []
            ),
        }
        _graphics_params = _production_config.graphics_build_params(
            _config, _graphics_context
        )
        state["motion_graphics"] = {
            "enabled": _graphics_params["enabled"],
            "intensity": _graphics_params["intensity"],
            "intensity_source": _graphics_params["intensity_source"],
            "mode": _graphics_params["mode"],
            "compositions": _graphics_params["compositions"],
        }
        save_production_state(project_dir, state)
        _graphics_manifest = None
        if not _graphics_params["enabled"]:
            # §13.1: disabled means NO graphics — the Director
            # directs none and the build below runs in its off
            # mode, so no graphic is inserted even as the T5
            # coverage fallback. The empty manifest is written
            # with the reason recorded, so the absence reads as
            # a configuration decision, not a failure.
            from services import plan_document as _plan_doc_mod
            from services.visual_director import (
                GRAPHICS_MANIFEST_VERSION as _MANIFEST_VERSION,
            )

            _graphics_manifest = {
                "version": _MANIFEST_VERSION,
                "project_id": project_id,
                "theme": _theme_resolution["engine_name"],
                "entries": [],
                "motion_graphics_disabled": True,
                "disabled_reason": "production_config",
            }
            _plan_doc_mod.write_graphics_manifest_document(
                project_dir, _graphics_manifest
            )
            print(
                "Motion graphics DISABLED by the production "
                "configuration — no graphics will be generated."
            )
        else:
            # Phase 6 (AM-002 Pass-2 + AM-001/D1): the Visual Director
            # reconciles the pre-retrieval visual plan (v1) against the
            # Asset Manager's actual per-sentence outcomes and issues
            # the authoritative graphics manifest; graphics generation
            # consumes the manifest — no manifest entry, no graphic.
            # visual_plan.json is upgraded in place to v2 and
            # graphics_manifest.json written beside it.
            # Phase 3 (Master Production Recovery, §11 — one
            # authoritative planning pathway) SUPERSEDES the D2
            # record-and-continue rule for this block: reconciliation
            # is NON-BYPASSABLE. A failure here — plan building,
            # evidence, the reconcile itself, or a failed document
            # write — fails the motion-designer stage loudly (and
            # Phase 1 same-run resume retries it) instead of silently
            # continuing on the legacy targets path, which let two
            # planning pathways produce two different films from one
            # production (Phase 0 root cause 3).
            _graphics_manifest = None
            try:
                # Phase 3: _plan_document is bound HERE, not only in
                # the script_analysis stage — on a resumed run that
                # ADOPTS that stage the earlier import never runs,
                # and the old fail-soft bypass silently hid the
                # resulting UnboundLocalError (Pass-2 "failed" on
                # every such resume and nobody could see why).
                from services import plan_document as _plan_document
                from services import visual_director as _visual_director

                _plan_v1 = _plan_document.visual_plan_from_analysis(
                    analysis,
                    project_id,
                    scene_governor=production_data.get("scene_governor"),
                )
                _outcomes = (
                    _visual_director.outcomes_from_processed_scenes(
                        processed_scenes
                    )
                )
                # Phase 8 (AM-009 live producer): assemble the
                # verified asset-data evidence for starved beats from
                # this production's own verified assets (recorded
                # structured facts only — see evidence_producer).
                # Fail-soft by construction; the production record
                # rides on plan v2's summary so the firing rate is a
                # reported fact, never a silent zero.
                from services import evidence_producer as _evidence

                # Transformation Phase 2.3 (§2.3): KB-corroborate the
                # planning stage's script-fact table (bible
                # ["script_facts"], services.script_facts) so facts the
                # knowledge base genuinely supports reach the Director
                # stamped research_supported with their source recorded
                # — and refresh the bible timeline's per-event bases so
                # the chronology's manifest stamp matches. Bounded by
                # the KB budget, fail-soft, and recorded on the
                # evidence record either way.
                _fact_table = (
                    (analysis.get("bible") or {}).get("script_facts")
                )
                _kb_corroboration = None
                if isinstance(_fact_table, dict) and _fact_table.get("facts"):
                    try:
                        from services import script_facts as _script_facts

                        _kb_corroboration = (
                            _script_facts.corroborate_facts(
                                _fact_table["facts"]
                            )
                        )
                        _script_facts.apply_fact_bases_to_timeline(
                            analysis.get("bible") or {}, _fact_table["facts"]
                        )
                    except Exception as _kb_error:  # recorded, not silent
                        _kb_corroboration = {
                            "error": f"{type(_kb_error).__name__}: {_kb_error}"
                        }
                _evidence_map, _evidence_record = _evidence.build_evidence(
                    processed_scenes, _outcomes, fact_table=_fact_table
                )
                if _kb_corroboration is not None:
                    _evidence_record["kb_corroboration"] = _kb_corroboration
                _plan_v2, _graphics_manifest = (
                    _visual_director.reconcile_visual_plan(
                        _plan_v1,
                        _outcomes,
                        genre_pack=_theme_pack,
                        evidence=_evidence_map,
                        compositions=_graphics_params["compositions"],
                        # UV-03: the bible rides along so the
                        # lower-third label can prefer the beat's
                        # recorded place/date identifier over the
                        # covering asset's raw search title.
                        bible=analysis.get("bible") or {},
                        production_context={
                            "timeline_event_count": len(
                                (analysis.get("bible") or {}).get(
                                    "timeline"
                                )
                                or []
                            ),
                        },
                    )
                )
                (_plan_v2.setdefault("summary", {}))[
                    "evidence_production"
                ] = _evidence_record
                _plan_path = _plan_document.write_visual_plan_v2_document(
                    project_dir, _plan_v2
                )
                _manifest_path = (
                    _plan_document.write_graphics_manifest_document(
                        project_dir, _graphics_manifest
                    )
                )
                if _plan_path is None or _manifest_path is None:
                    # The reconciled plan IS the production's plan;
                    # if it did not persist, downstream stages would
                    # read a stale v1 (or nothing) and silently plan
                    # a different film. Fail loudly (Phase 3).
                    raise RuntimeError(
                        "Visual Director Pass-2 documents did not "
                        "persist (plan "
                        f"{'written' if _plan_path else 'FAILED'}, "
                        "manifest "
                        f"{'written' if _manifest_path else 'FAILED'})"
                    )
                print(
                    "Visual Director Pass-2: "
                    f"{len(_graphics_manifest.get('entries') or [])} "
                    "targeted graphic(s) manifested, "
                    f"{(_plan_v2.get('summary') or {}).get('compositions', 0)} "
                    "composition decision(s)."
                )
            except Exception as _director_error:
                # Phase 3: recorded AND raised — the stage fails and
                # Phase 1 resume retries it; the production never
                # silently falls back to the legacy targets pathway.
                print(
                    "Visual Director Pass-2 FAILED (failing the "
                    f"motion-designer stage): {_director_error}"
                )
                raise

        # MG-11: sentence slot durations from the narration
        # parts' measured lengths (parts are sequential; part
        # index + 1 is the sentence number) so engine graphics
        # render at slot length instead of a fixed 3.0s the
        # renderer then tpad-freezes.
        _slot_durations = {}
        if isinstance(parts_manifest_data, dict):
            for _part in parts_manifest_data.get("parts") or []:
                if not isinstance(_part, dict):
                    continue
                _measured = _part.get("measured_duration_seconds")
                _index = _part.get("index")
                if _measured and _index is not None:
                    try:
                        _slot_durations[int(_index) + 1] = float(
                            _measured)
                    except (TypeError, ValueError):
                        continue

        motion_graphics_dir = project_dir / "motion_graphics"
        motion_graphics = build_motion_graphics_assets(
            motion_data,
            motion_graphics_dir,
            mode=_graphics_params["mode"],
            targets=graphic_targets,
            max_stats=(
                _graphics_params["max_stats"]
                if _graphics_params["max_stats"] is not None
                else MOTION_GRAPHIC_MAX_STATS
            ),
            genre_pack=_theme_pack,
            manifest=_graphics_manifest,
            slot_durations=_slot_durations,
        )

        # Attach each standalone motion graphic to its target
        # scene/sentence — delivered + validated graphics only
        # (MG-07); refusals are printed with their typed reasons.
        _attached, _attach_skipped = _attach_motion_graphics(
            motion_graphics, processed_scenes
        )
        for _skip in _attach_skipped:
            print(
                f"Motion graphic not attached [{_skip['graphic']}]: "
                f"{_skip['reason']}"
            )

        print(f"Motion graphics generated: {len(motion_graphics)}")
        print(f"Motion graphics attached to timeline candidates: {sum(1 for s in processed_scenes for v in s.get("videos", []) if v.get("motion_graphic"))}")

        # Run-10 fix: the graphics stage's per-target outcomes ride
        # onto the missing_visuals records the asset report will
        # aggregate — a beat the graphics path could not serve
        # carries its typed reason there, not only in the graphics
        # manifest. Fail-soft, like the targeting above.
        try:
            _annotated = _annotate_missing_visual_graphics_outcomes(
                processed_scenes,
                motion_graphics_dir / "motion_graphics.json",
            )
            if _annotated:
                print(
                    "Graphics outcomes annotated onto "
                    f"{_annotated} missing-visual record(s)."
                )
        except Exception as _annotation_error:
            print(
                "Graphics outcome annotation failed (continuing): "
                f"{_annotation_error}"
            )
        if not _graphics_params["enabled"]:
            # Phase 9 (§13.1): no graphics record exists to
            # annotate from — the disabled stamp is the outcome.
            _stamped = _annotate_missing_visuals_motion_graphics_disabled(
                processed_scenes
            )
            if _stamped:
                print(
                    "Motion-graphics-disabled outcome stamped onto "
                    f"{_stamped} missing-visual record(s)."
                )
        _finish_executed_stage(
            state,
            "motion_designer",
            project_dir,
            artefact_dir,
            payload={"processed_scenes": processed_scenes},
        )
    except _StageAdopted:
        pass
    except Exception as error:
        fail_pipeline(state, "motion_designer", error, project_dir)

    # ==================================================
    # SOUND DESIGNER
    # ==================================================

    try:
        run_stage(state, "sound_designer", project_dir)
        _sound_payload = stage_resume.load_stage_payload(
            _resume_plan, "sound_designer"
        )
        if _sound_payload is not None and isinstance(
            _sound_payload.get("soundtrack_record"), dict
        ):
            soundtrack_record = _sound_payload["soundtrack_record"]
            state["soundtrack"] = soundtrack_record
            _reference_name = str(
                _sound_payload.get("soundtrack_reference_name") or ""
            )
            soundtrack_reference = (
                project_dir / _reference_name
                if _reference_name
                else narration_file
            )
            _adopt_stage(state, "sound_designer", project_dir, _resume_plan)
            raise _StageAdopted("sound_designer")
        print("Sound Designer")

        # Stage H (M1/M3): ONE explicit music contract, resolved by
        # services.music.resolve_music_request — an explicit
        # music_enabled=false disables music entirely (a stray
        # background.mp3 can no longer override it, D2); an explicit
        # music_file (operator-placed) wins over the legacy project
        # background.mp3, which is picked up only when nothing was
        # decided explicitly. No downloads, no library (P3 is
        # founder-gated) — operator-supplied files only.
        music_decision = music_service.resolve_music_request(
            options, project_dir
        )

        # Stage J (P4): resolve the mix configuration ONCE — the
        # genre pack's mix_preset supplies defaults, env overrides
        # beat the preset, explicit options beat everything. The
        # decision's music_volume (None when not explicitly set)
        # is what lets a preset volume apply at all.
        mix_config = music_service.resolve_mix_config(
            options=options,
            genre_pack=_genre_pack,
            music_volume=music_decision["music_volume"],
        )

        # narration_volume (editor parity): adjust the narration
        # BEFORE the mix, exactly as revision renders do, so the
        # soundtrack — and the M10 provenance reference — is built
        # from the narration the audience will hear.
        mix_narration = narration_file
        narration_volume = music_decision.get("narration_volume")
        if narration_volume is not None and abs(narration_volume - 1.0) > 0.001:
            adjusted = project_dir / "narration_adjusted.wav"
            music_service.run_ffmpeg(
                [
                    "ffmpeg", "-y", "-i", str(narration_file),
                    "-af", f"volume={narration_volume}",
                    str(adjusted),
                ]
            )
            mix_narration = adjusted
        soundtrack_reference = mix_narration

        try:
            soundtrack_record = prepare_soundtrack(
                narration_file=str(mix_narration),
                output_file=str(soundtrack_file),
                music_file=music_decision["file"],
                music_volume=mix_config["music_volume"],
                source_type=music_decision["source_type"],
                decided_by=music_decision["decided_by"],
                requested=music_decision["requested"],
                disabled=music_decision["disabled"],
                mix_config=mix_config,
            )
        except Exception as prep_error:
            # D10 asymmetry, recorded: a supplied-but-unmixable
            # (corrupt/unreadable) track fails the run — but the
            # failure is first RECORDED as a state="failed"
            # soundtrack record so the failure report, manifest and
            # ledger show the music outcome instead of silence.
            failed_record = music_service.failed_music_record(
                requested_file=music_decision["file"],
                source_type=music_decision["source_type"],
                decided_by=music_decision["decided_by"],
                requested=music_decision["requested"],
                error=prep_error,
            )
            state["soundtrack"] = failed_record
            write_json_atomic(
                project_dir / "soundtrack.json", failed_record
            )
            raise

        if not soundtrack_file.exists():
            raise RuntimeError(
                "Sound Designer did not create soundtrack."
            )

        if isinstance(soundtrack_record, dict):
            state["soundtrack"] = soundtrack_record
            write_json_atomic(
                project_dir / "soundtrack.json", soundtrack_record
            )
            print(f"Soundtrack record: {soundtrack_record}")

        print(f"Soundtrack created: {soundtrack_file}")

        _finish_executed_stage(
            state,
            "sound_designer",
            project_dir,
            artefact_dir,
            payload={
                "soundtrack_record": soundtrack_record,
                "soundtrack_reference_name": Path(
                    soundtrack_reference
                ).name,
            },
        )

    except _StageAdopted:
        pass
    except Exception as error:
        fail_pipeline(
            state,
            "sound_designer",
            error,
            project_dir,
        )


    # ==================================================
    # VIDEO EDITOR
    # ==================================================

    try:
        run_stage(state, "video_editor", project_dir)
        _editor_payload = stage_resume.load_stage_payload(
            _resume_plan, "video_editor"
        )
        if _editor_payload is not None and isinstance(
            _editor_payload.get("timeline_result"), dict
        ):
            # Copy the source run's run-scoped outputs into THIS
            # run's artefact directory (their hashes were validated
            # by the plan), then restore the locals the completion
            # path consumes. The film itself is byte-identical —
            # same script, same options, validated artefacts.
            _source_dir = media_paths.run_subdir(
                project_dir, _resume_plan["source_run_id"]
            )
            for _name in (
                "documentary_final_output.mp4",
                "documentary_delivery.mp4",
                "timeline.json",
                "asset_report.json",
                "rights_manifest.json",
                "visual_quality.json",
                "production_report.json",
                "delivery.json",
            ):
                _source_file = _source_dir / _name
                if _source_file.is_file():
                    shutil.copyfile(_source_file, artefact_dir / _name)
            timeline_result = _editor_payload["timeline_result"]
            timeline_file = artefact_dir / "timeline.json"
            expected_duration = float(
                _editor_payload.get("expected_duration") or 0
            )
            _report_path = artefact_dir / "asset_report.json"
            if _report_path.is_file():
                try:
                    asset_report = json.loads(
                        _report_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    asset_report = None
            state["captions_mode"] = _editor_payload.get("captions_mode")
            if isinstance(_editor_payload.get("delivery"), dict):
                state["delivery"] = _editor_payload["delivery"]
            if isinstance(state.get("_partial"), dict):
                state["_partial"]["timeline_result"] = timeline_result
            _adopt_stage(state, "video_editor", project_dir, _resume_plan)
            raise _StageAdopted("video_editor")
        print("Video Editor")

        # ----------------------------------------------
        # Gate 2 (Blocker 3): pre-render coverage repair pass.
        # Between motion_designer and the coverage gate below:
        # sentences still in missing_visuals after graphics get
        # ONE bounded repair attempt through the existing asset
        # machinery. A sentence that survives repair + graphics
        # fails HERE with the gate's own typed coverage error —
        # early and honestly, instead of walking into a
        # guaranteed gate failure after a full timeline build.
        # The gate itself is unchanged and still runs below.
        # ----------------------------------------------

        _repair_report = _run_coverage_repair(
            processed_scenes, project_dir
        )
        state["coverage_repair"] = _repair_report
        save_production_state(project_dir, state)
        try:
            write_json_atomic(
                artefact_dir / "coverage_repair_metrics.json",
                _repair_report,
                ensure_ascii=True,
            )
        except Exception as _repair_metrics_error:
            print(
                "Coverage repair metrics write failed (continuing): "
                f"{_repair_metrics_error}"
            )
        if _repair_report["attempted"]:
            print(
                "Coverage repair: "
                f"{len(_repair_report['missing_before'])} sentence(s) "
                f"still missing after graphics, "
                f"{len(_repair_report['repaired'])} repaired, "
                f"{len(_repair_report['still_missing'])} still missing."
            )
        if _repair_report["still_missing"]:
            raise _coverage_incomplete_error(
                _repair_report["still_missing"],
                sum(
                    len((scene.get("sentences") or []))
                    for scene in scenes
                ),
            )

        # ----------------------------------------------
        # Build visual timeline
        # ----------------------------------------------

        timeline_result = build_project_timeline(
            project_id,
            processed_scenes,
            output_dir=str(media_paths.media_root()),
            artefact_dir=artefact_dir,
            parts_manifest=parts_manifest_data,
        )

        if not isinstance(timeline_result, dict):
            raise RuntimeError(
                "Timeline builder returned invalid result."
            )

        if not timeline_result.get("timeline_file"):
            raise RuntimeError(
                "Timeline builder did not return timeline_file."
            )

        timeline_file = Path(
            timeline_result["timeline_file"]
        )

        if not timeline_file.exists():
            raise RuntimeError(
                f"Timeline file missing: {timeline_file}"
            )

        expected_duration = float(
            timeline_result.get("duration_seconds")
            or timeline_result.get(
                "narration_duration_seconds"
            )
            or 0
        )

        print(f"Timeline created: {timeline_file}")
        print(
            f"Expected duration: "
            f"{expected_duration:.2f}s"
        )

        # ----------------------------------------------
        # Coverage gate: every narration sentence must have
        # visual coverage BEFORE rendering starts. An incomplete
        # timeline is rejected here with the exact uncovered
        # sentences — never padded, stretched or placeholdered.
        # ----------------------------------------------

        if isinstance(state.get("_partial"), dict):
            state["_partial"]["timeline_result"] = timeline_result
        coverage = timeline_result.get("coverage") or {}
        if coverage and not coverage.get("complete", True):
            raise _coverage_incomplete_error(
                coverage.get("uncovered_sentences") or [],
                coverage.get("total_sentences"),
            )

        # ----------------------------------------------
        # Render visual edit
        # ----------------------------------------------

        with open(
            timeline_file,
            "r",
            encoding="utf-8",
        ) as f:
            timeline_data = json.load(f)

        render_scenes = timeline_data.get(
            "scenes",
            [],
        )

        if not render_scenes:
            raise RuntimeError(
                "Timeline contains no scenes to render."
            )

        if expected_duration <= 0:
            expected_duration = float(
                timeline_data.get(
                    "duration_seconds"
                )
                or timeline_data.get(
                    "narration_duration_seconds"
                )
                or 0
            )

        print(
            f"Preparing "
            f"{len(render_scenes)} scenes..."
        )

        # Phase 6 (AM-011): stamp the Visual Director's composition
        # decisions (visual_plan.json v2) onto the timeline items
        # before rendering, and persist the stamped timeline so the
        # artefact of record — the one the editor imports — carries
        # the same composition plans the renderer will apply.
        # Fail-soft: a composition failure never blocks the render;
        # the items simply render uncomposed.
        try:
            _plan_v2_path = project_dir / "visual_plan.json"
            if _plan_v2_path.exists():
                _plan_v2_doc = json.loads(
                    _plan_v2_path.read_text(encoding="utf-8")
                )
                if (
                    isinstance(_plan_v2_doc, dict)
                    and _plan_v2_doc.get("version") == 2
                ):
                    from services import composition as _composition

                    _stamped = (
                        _composition.apply_decisions_to_timeline(
                            timeline_data, _plan_v2_doc
                        )
                    )
                    if _stamped:
                        write_json_atomic(timeline_file, timeline_data)
                        print(
                            f"Composition: {_stamped} timeline "
                            "item(s) carry a composition plan."
                        )
        except Exception as _composition_error:
            print(
                "Composition stamping failed (rendering "
                f"uncomposed): {_composition_error}"
            )

        render_timeline(
            render_scenes,
            str(project_dir),
            expected_duration=(
                expected_duration
                if expected_duration > 0
                else None
            ),
        )

        if not raw_video.exists():
            raise RuntimeError(
                "documentary.mp4 was not created."
            )

        print(
            f"Visual edit created: {raw_video}"
        )

        # ----------------------------------------------
        # Add finished soundtrack
        # ----------------------------------------------

        if narrated_video.exists():
            try:
                narrated_video.unlink()
            except OSError:
                pass

        add_narration(
            str(raw_video),
            str(soundtrack_file),
            str(narrated_video),
        )

        if not narrated_video.exists():
            raise RuntimeError(
                "Soundtrack could not be added to video."
            )

        print(
            f"Soundtrack added: {narrated_video}"
        )

        # ----------------------------------------------
        # Captions (Stage 3I: captions option — "burn" is the
        # historical behaviour; "sidecar" keeps captions.json/.srt/
        # .vtt alongside an unburned video; "off" skips burning and
        # records the mode in state)
        # ----------------------------------------------

        state["captions_mode"] = captions_mode
        save_production_state(project_dir, state)

        if captions_mode == "burn":
            from services.caption_renderer import create_ass_file
            from services.motion_engine.themes import theme_for_pack

            # Production theme for caption colours; None when the
            # pack selects no theme -- the renderer then falls back
            # to its editorial_dark tokens. Phase 9: the theme
            # pack carries any configured theme override, so
            # captions wear the same theme as the graphics.
            _caption_theme = theme_for_pack(_theme_pack)

            create_ass_file(
                str(captions_json),
                str(captions_ass),
                sync=caption_sync,
                theme=_caption_theme,
            )

            if not captions_ass.exists():
                raise RuntimeError(
                    "ASS caption file was not created."
                )

            if captioned_video.exists():
                try:
                    captioned_video.unlink()
                except OSError as error:
                    print(
                        "WARNING: could not remove old "
                        f"captioned video: {error}"
                    )

            burn_captions(
                str(narrated_video),
                str(captions_ass),
                str(captioned_video),
            )

            if not captioned_video.exists():
                raise RuntimeError(
                    "Caption rendering failed."
                )

            print(
                f"Captions burned: {captioned_video}"
            )
            pre_final_video = captioned_video
        else:
            print(
                f"Captions mode '{captions_mode}': skipping ASS/burn; "
                "sidecar captions (json/srt/vtt) are kept."
            )
            pre_final_video = narrated_video

        # ----------------------------------------------
        # Final Output
        # ----------------------------------------------

        # Phase 1 durability: publish by os.replace ALONE. The
        # previous delete-then-replace opened a window in which NO
        # final file existed at all (a kill there destroyed a
        # previously valid master and left the run's delivery
        # pointing at nothing); os.replace overwrites atomically,
        # so the old final survives until the validated new one
        # lands in the same rename.
        os.replace(
            str(pre_final_video),
            str(final_video),
        )

        if not final_video.exists():
            raise RuntimeError(
                "Final video was not created."
            )

        print(
            f"FINAL OUTPUT: {final_video}"
        )

        # Phase 8 (AM-023 machinery): the delivery encode beside
        # the master — a web-playable copy at the profile's
        # engineering-default settings (AM-023's target value is
        # the founder's, still OPEN). Fail-soft: a failed delivery
        # encode is recorded and the master stands alone; it never
        # fails the production. The record rides on the run state
        # and as a delivery.json artefact beside the master.
        try:
            from services import delivery_profile as _delivery

            _delivery_record = _delivery.produce_delivery_copy(
                final_video,
                artefact_dir / "documentary_delivery.mp4",
            )
            state["delivery"] = _delivery_record
            write_json_atomic(
                artefact_dir / "delivery.json", _delivery_record
            )
            print(
                "Delivery copy: "
                f"{_delivery_record.get('status')} "
                f"({_delivery_record.get('delivery_bytes')} bytes "
                f"vs master {_delivery_record.get('master_bytes')})"
            )
        except Exception as _delivery_error:
            print(
                "Delivery copy FAILED (master unaffected): "
                f"{_delivery_error}"
            )
            state["delivery"] = {
                "status": "failed",
                "reason": "delivery_stage_exception",
                "detail": str(_delivery_error),
            }

        # ----------------------------------------------
        # Stage H (M9/M10): measure the EXPORTED file's audio
        # (integrated loudness + true peak — reported, never
        # normalised in this stage) and cross-check the soundtrack
        # record against the audio itself: the final file is
        # compared with the exact narration the soundtrack was
        # mixed from. A mismatch is a provenance failure the final
        # auditor will verdict on below.
        # ----------------------------------------------

        final_audio_measurement = music_service.final_audio_record(
            final_video,
            soundtrack_record=(
                soundtrack_record
                if isinstance(soundtrack_record, dict)
                else None
            ),
        )
        audio_provenance = music_service.assess_audio_provenance(
            (
                soundtrack_record.get("state")
                if isinstance(soundtrack_record, dict)
                else None
            ),
            final_video,
            soundtrack_reference,
        )
        print(f"Final audio measurement: {final_audio_measurement}")
        print(f"Audio provenance: {audio_provenance}")

        # ----------------------------------------------
        # Asset Manager report (directive section E, AUD-23)
        # ----------------------------------------------

        renderer_audit = None
        renderer_audit_file = project_dir / "renderer_timing_audit.json"
        if renderer_audit_file.exists():
            try:
                renderer_audit = json.loads(
                    renderer_audit_file.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                renderer_audit = None

        asset_report = build_asset_report(
            processed_scenes,
            timeline_result=timeline_result,
            renderer_audit=renderer_audit,
            analysis_mode=analysis.get("mode") if isinstance(analysis, dict) else None,
            planner=analysis.get("planner") if isinstance(analysis, dict) else None,
            # C-07: the research stage outcome recorded at
            # script_analysis time rides the report.
            research=(
                state.get("research")
                if isinstance(state.get("research"), dict)
                else _research_stage_record({"analysis": analysis})
            ),
            execution=run_metrics.snapshot(),
            evidence_ledger=_evidence_ledger_entries(project_dir),
            soundtrack=(
                soundtrack_record
                if isinstance(soundtrack_record, dict)
                else None
            ),
            genre_pack={
                "id": _genre_info["pack_id"],
                "source": _genre_info["source"],
            },
            final_audio=final_audio_measurement,
            audio_provenance=audio_provenance,
        )

        # Stage 5 (R4, annex D2.2.3/D2.2.6): the run's timing
        # records ride the asset report — the captions alignment
        # aggregate (a film with approximate captions says so at
        # run level) and the Σ-parts consistency check (a
        # divergent concatenation is a recorded warning, never
        # silently absorbed).
        if isinstance(state.get("captions_alignment"), dict):
            asset_report["captions_alignment"] = state[
                "captions_alignment"
            ]
        if isinstance(state.get("timing_consistency"), dict):
            asset_report["timing_consistency"] = state[
                "timing_consistency"
            ]

        rights_manifest = build_rights_manifest(
            processed_scenes,
            soundtrack=(
                soundtrack_record
                if isinstance(soundtrack_record, dict)
                else None
            ),
        )
        write_rights_manifest(artefact_dir, rights_manifest)
        licensed_count = sum(
            1
            for entry in rights_manifest
            if entry["license"] and entry["license"] != "unknown"
        )
        print(
            f"Rights manifest: {len(rights_manifest)} assets, "
            f"{licensed_count} with a recorded licence, "
            f"{len(rights_manifest) - licensed_count} unknown."
        )

        print(
            "Asset report: "
            f"accepted={asset_report['total_accepted_assets']} "
            f"verified_footage={asset_report['verified_moving_footage']} "
            f"verified_images={asset_report['verified_images']} "
            f"substitutions={asset_report['substitutions']} "
            f"authentic={asset_report['authentic']}"
        )

        # ----------------------------------------------
        # Phase 3: visual-quality measurement + reconciling
        # production report. Fail-soft by design — measurement must
        # never break a render — but a failure to measure is VISIBLE
        # in the report (visual_quality_error), never silent. The
        # single write_asset_report for this run happens after the
        # embedding so asset_report.json carries the new keys. The
        # verdicts are REPORTED here; after the final audit below,
        # _honour_quality_verdict preserves a quality FAIL on the
        # run state (Phase 4: a release finding on a deliverable
        # film, not a completion gate — C-06's fail-the-run gate
        # was superseded by the technical/release status split).
        # ----------------------------------------------
        try:
            _mg_record = None
            _mg_file = project_dir / "motion_graphics" / "motion_graphics.json"
            if _mg_file.exists():
                try:
                    _mg_record = json.loads(
                        _mg_file.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    _mg_record = None
            _measurement = visual_quality_service.measure_visual_quality(
                timeline=timeline_result,
                asset_report=asset_report,
                rights_manifest=rights_manifest,
                motion_graphics=_mg_record,
                run_manifest_present=(artefact_dir / "run_manifest.json").exists(),
                project_id=project_id,
                project_dir=str(project_dir),
            )
            _production_report = production_report_service.build_production_report(
                measurement=_measurement,
                asset_report=asset_report,
                timeline=timeline_result,
                rights_manifest=rights_manifest,
                renderer_audit=renderer_audit,
                run_ledger_summary={
                    "state": "unavailable",
                    "note": "run ledger is written after the final audit in this flow",
                },
                revision_summary={
                    "state": "unavailable",
                    "note": "no revision records exist at production time",
                },
            )
            asset_report["visual_quality"] = _measurement
            asset_report["quality_verdicts"] = _production_report["verdicts"]
            write_json_atomic(
                artefact_dir / "visual_quality.json", _measurement
            )
            write_json_atomic(
                artefact_dir / "production_report.json", _production_report
            )
            print(
                "Production report: quality verdict "
                f"{(_production_report['verdicts'].get('quality') or {}).get('verdict')}"
            )
        except Exception as _measure_error:
            asset_report["visual_quality_error"] = (
                f"{type(_measure_error).__name__}: {_measure_error}"
            )
            print(
                "WARNING: visual-quality measurement failed and was "
                f"recorded in the asset report: {_measure_error}"
            )
        write_asset_report(artefact_dir, asset_report)

        # ----------------------------------------------
        # Final audit
        # ----------------------------------------------

        # Phase 4: a probe/duration RuntimeError from the auditor
        # is a TECHNICAL failure — stamp it before the re-raise so
        # fail_pipeline records the run's technical_status.
        try:
            final_audit = validate_final_video(
                str(final_video),
                expected_duration=(
                    expected_duration
                    if expected_duration > 0
                    else None
                ),
                asset_report=asset_report,
            )
        except RuntimeError:
            state["technical_status"] = "failed"
            raise

        # Phase 4: derive the run's two statuses from the audit
        # verdict and the composite quality verdict, and stamp
        # them (plus the release findings) onto the run state —
        # they ride the stage payload, the pipeline result and
        # the ledger finish below.
        statuses = release_status.derive_run_statuses(
            final_audit.get("verdict"),
            (asset_report.get("quality_verdicts") or {}).get("quality"),
        )
        state["technical_status"] = statuses["technical_status"]
        state["release_status"] = statuses["release_status"]
        state["release_findings"] = statuses["release_findings"]

        # MA-14 + INT-102, as split by Phase 4: honour the
        # auditor's verdict. TECHNICAL-class failures still fail
        # the production through the normal fail path — the
        # rendered file may exist, but the recorded outcome
        # (project status + failure asset report) is a failure
        # carrying the auditor's technical reasons. An UNEVALUATED
        # verdict also fails the gate (see _honour_final_audit):
        # it is never interpreted as a successful audit.
        # RELEASE-class failures do NOT fail the run: they ride
        # the completed run in its release findings. Warnings
        # alone never fail a run.
        _honour_final_audit(final_audit)

        # C-06, superseded by Phase 4: the composite QUALITY
        # verdict is no longer a completion gate — a quality FAIL
        # is a release finding on a deliverable film (already
        # reflected in the statuses derived above), preserved on
        # the run state and in the reports written above. PASS /
        # QUALITY_WARNING / absent verdicts proceed unchanged.
        _honour_quality_verdict(
            asset_report.get("quality_verdicts"), state=state
        )

        print(
            "Final video audit complete: "
            f"{final_audit['duration_seconds']:.3f}s"
        )

        _finish_executed_stage(
            state,
            "video_editor",
            project_dir,
            artefact_dir,
            payload={
                "expected_duration": expected_duration,
                "timeline_result": timeline_result,
                "captions_mode": captions_mode,
                "delivery": state.get("delivery"),
                "technical_status": state.get("technical_status"),
                "release_status": state.get("release_status"),
                "release_findings": state.get("release_findings"),
            },
        )

    except _StageAdopted:
        pass
    except Exception as error:
        fail_pipeline(
            state,
            "video_editor",
            error,
            project_dir,
        )


    # INT-101: the project/state must NOT be marked "completed"
    # yet. The ledger run is finished FIRST (the block below), and
    # only then is completion recorded — see the marking block
    # after the ledger work. The old order marked the project
    # completed before finish_run; a crash in that window left a
    # completed project whose run was still "running", which the
    # next worker-start reconciliation then marked FAILED — a
    # successful production recorded as failed.
    visual_count = sum(
        len(s.get("images", [])) + len(s.get("videos", []))
        for s in processed_scenes
    )

    result = {
        "project_id": project_id,
        "status": "completed",
        "video": str(final_video),
        "timeline": str(timeline_file) if timeline_file else None,
        "narration": str(narration_file),
        "captions": str(captions_json),
        "production_state": str(artefact_dir / "production_state.json"),
        "case_bible": str(project_dir / "case_bible.json"),
        "scene_count": len(scenes),
        "sentence_count": len(sentences),
        "visual_count": visual_count,
        "expected_duration_seconds": expected_duration,
        "script_hash": script_hash,
        "technical_status": state.get("technical_status"),
        "release_status": state.get("release_status"),
        "release_findings": state.get("release_findings"),
    }

    result_file = artefact_dir / "pipeline_result.json"
    write_json_atomic(result_file, result)

    # Stage 2: record the run's decisions in the ledger, finish the
    # run manifest, close the ledger run as completed, and restore
    # the previous run context. All fail-soft.
    if _run_id:
        if asset_report is not None:
            run_ledger.record_decisions(
                _run_id,
                project_id,
                asset_report,
                timeline_result,
                processed_scenes=processed_scenes,
            )
            # Stage H (M7): the run's music decision row — after
            # the visual decisions, as in the failure path.
            if isinstance(soundtrack_record, dict):
                run_ledger.record_music_decision(
                    _run_id, project_id, soundtrack_record
                )
        manifest_path = None
        if isinstance(_manifest, dict):
            # Stage 4 (DATA-004): run-scoped outputs are checked in
            # the run's artefact directory; shared/project-level
            # outputs in the project directory. The manifest records
            # bare filenames, as before — the manifest itself now
            # lives in the run directory, so run-scoped names resolve
            # against it and shared names against the project dir.
            stage_outputs = [
                name
                for name in (
                    "narration.wav",
                    "soundtrack.wav",
                    "soundtrack.json",
                    "captions.json",
                    "case_bible.json",
                    "timeline.json",
                    "documentary_final_output.mp4",
                    "asset_report.json",
                    "rights_manifest.json",
                    "visual_quality.json",
                    "production_report.json",
                    "production_state.json",
                    "pipeline_result.json",
                )
                if (
                    artefact_dir / name
                    if name in media_paths.RUN_SCOPED_ARTEFACTS
                    else project_dir / name
                ).exists()
            ]
            enriched = run_ledger.enrich_manifest(
                _manifest,
                status="completed",
                execution=run_metrics.snapshot(),
                asset_report=asset_report,
                stage_outputs=stage_outputs,
            )
            written = run_ledger.write_manifest(artefact_dir, enriched)
            manifest_path = str(written) if written else None
        run_ledger.finish_run(
            _run_id,
            "completed",
            execution=run_metrics.snapshot(),
            manifest_path=manifest_path,
            technical_status=state.get("technical_status"),
            release_status=state.get("release_status"),
            release_findings=state.get("release_findings"),
        )
        # Phase 8 (§21): register the generated videos for the
        # 50-hour retention deadline, anchored at the run's
        # finish time. Fail-soft — registration never fails a
        # production.
        try:
            from services.billing import retention

            retention.register_run_outputs(_run_id, artefact_dir)
        except Exception:
            pass
        # Phase 8.4: completion frees capacity — promote the next
        # queued production.
        _dispatch_quietly()
    if "_run_context_previous" in state:
        run_context.deactivate(state.pop("_run_context_previous"))

    # INT-101: completion is recorded ONLY NOW — after the ledger
    # run has been finished as "completed" above. Project row,
    # production state and run record therefore transition in an
    # order where no interleaving (crash, kill, DB outage in the
    # ledger block) can leave a "completed" project behind a
    # not-yet-finished run for reconciliation to mark failed.
    state["status"] = "completed"
    state["current_stage"] = None
    # AUD-30: completed_at was declared in the state schema but never
    # set; a finished run now carries its finish time.
    state["completed_at"] = utc_now()
    save_production_state(project_dir, state)
    _set_project_status(project_id, "completed")

    logging_setup.log_event(
        logger,
        logging.INFO,
        "run_completed",
        project_id=project_id,
        run_id=state.get("_run_id"),
        final_video=str(final_video),
    )
    return result


def enqueue_script_analysis(project_id: str, script: str):
    job = video_queue.enqueue(
        analyze_project_script, project_id, script
    )
    print(f"Queued analysis job: {job.id}")
    return job.id


def analyze_project_script(project_id: str, script: str):
    project_dir = media_paths.project_dir(project_id)
    production_data = build_production_scenes(
        script, project_id=project_id, project_dir=project_dir
    )
    scenes = production_data["scenes"]
    media_job = video_queue.enqueue(
        process_project_media, project_id, scenes
    )
    return {
        "project_id": project_id,
        "status": "analyzed",
        "scenes": scenes,
        "case_bible_path": production_data.get("case_bible_path"),
        "media_job_id": media_job.id,
    }


def enqueue_media_processing(project_id: str, scenes: list):
    job = video_queue.enqueue(
        process_project_media, project_id, scenes
    )
    print(f"Queued media job: {job.id}")
    return job.id


def process_project_media(project_id: str, scenes: list):
    project_dir = media_paths.project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    reset_registry(project_id)
    processed_scenes = []
    for scene in scenes:
        if isinstance(scene, dict):
            scene["project_id"] = project_id
        result = process_scene_media(scene, str(project_dir))
        result["duration_seconds"] = scene.get("duration_seconds", 10)
        result["scene"] = scene
        result["project_id"] = project_id
        processed_scenes.append(result)
    timeline_result = build_project_timeline(
        project_id, processed_scenes, output_dir=str(media_paths.media_root())
    )
    return {
        "project_id": project_id,
        "status": "media_processed",
        "scenes": processed_scenes,
        "timeline": timeline_result,
    }


