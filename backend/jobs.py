import hashlib
import json
import os
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

from services import genre_packs
from services import media_paths
from services import run_context
from services import run_ledger
from services import run_metrics
from services.production_state import (
    create_production_state,
    start_stage,
    complete_stage,
    fail_stage,
    utc_now,
)


REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
QUEUE_NAME = "video_generation"

redis_connection = Redis.from_url(REDIS_URL, decode_responses=False)

video_queue = Queue(
    QUEUE_NAME,
    connection=redis_connection,
    default_timeout=3600,
)

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

    The JSON is written to a temp file in the SAME directory, flushed
    and fsynced, then os.replace()d over production_state.json — so a
    reader (or a crash mid-write) can never observe a half-written
    state file: the file on disk is always either the previous
    complete state or the new complete state.
    """
    project_dir = Path(project_dir)
    state_file = project_dir / "production_state.json"
    payload = json.dumps(
        _persistable_state(state), indent=2, ensure_ascii=False
    )
    fd, tmp_name = tempfile.mkstemp(
        dir=str(project_dir),
        prefix=".production_state.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, state_file)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return state_file


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
            run_failure={"stage": stage, "error": str(error)},
            execution=run_metrics.snapshot(),
            genre_pack=state.get("genre_pack"),
        )
        write_asset_report(project_dir, failure_report)
        print("Asset report (failure): written from partial state.")
    except Exception as report_error:
        print(f"Asset report (failure) could not be written: {report_error}")
    # Stage 2: close the ledger run (failed, or cancelled when the
    # error is a cooperative cancellation), record whatever decisions
    # the partial state supports, finish the run manifest, and restore
    # the previous run context. All fail-soft (run_ledger never
    # raises for database problems).
    run_id = state.get("_run_id") if isinstance(state, dict) else None
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
        manifest_path = None
        manifest = state.get("_manifest")
        if isinstance(manifest, dict):
            enriched = run_ledger.enrich_manifest(
                manifest,
                status="cancelled" if cancelled else "failed",
                execution=run_metrics.snapshot(),
                asset_report=failure_report,
            )
            written = run_ledger.write_manifest(project_dir, enriched)
            manifest_path = str(written) if written else None
        run_ledger.finish_run(
            run_id,
            "cancelled" if cancelled else "failed",
            error="cancelled" if cancelled else str(error),
            execution=run_metrics.snapshot(),
            manifest_path=manifest_path,
            stage=stage,
        )
    if isinstance(state, dict) and "_run_context_previous" in state:
        run_context.deactivate(state.pop("_run_context_previous"))
    print()
    print("========================================")
    print("PIPELINE FAILED")
    print("========================================")
    print(f"Stage: {stage}")
    print(f"Error: {error}")
    print()
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
        return {
            "analysis": analysis,
            "sentences": sentences,
            "scenes": scenes,
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

    return {
        "analysis": analysis,
        "sentences": sentences,
        "scenes": scenes,
        "script_hash": script_hash,
        "project_id": project_id,
        "bible": bible,
        "case_bible_path": str(bible_path) if bible_path else "",
    }


def enqueue_test_job(project_id: str):
    job = video_queue.enqueue(test_job, project_id, at_front=True)
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
    # "music_file", "voice_id", "captions") are merged into the
    # ledger/manifest options snapshot next to the entrypoint.
    # Unknown keys pass through harmlessly.
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
        print(
            f"Duplicate submission for project {project_id}: active "
            f"run {submission['run_id']} already covers this script; "
            "no new job queued."
        )
        return submission["run_id"]
    run_id = submission["run_id"] if submission else None
    try:
        job = video_queue.enqueue(
            run_full_pipeline,
            project_id,
            script,
            run_id=run_id,
            options=run_options,
            at_front=True,
        )
    except Exception as error:
        if run_id:
            run_ledger.finish_run(
                run_id, "failed", error=f"enqueue failed: {error}"
            )
        raise
    print(f"Queued full pipeline job: {job.id} (run {run_id})")
    return job.id


def enqueue_analysis_phase(project_id: str, script: str, run_id: str, options=None):
    """Queue ONLY the analysis phase of a plan-gated run (Stage 4).
    The run row already exists (created by the caller's
    run_ledger.submit_production); an enqueue failure marks it
    failed rather than orphaning it, mirroring enqueue_full_pipeline."""
    try:
        job = video_queue.enqueue(
            run_analysis_phase,
            project_id,
            script,
            run_id=run_id,
            options=options,
            at_front=True,
        )
    except Exception as error:
        if run_id:
            run_ledger.finish_run(
                run_id, "failed", error=f"enqueue failed: {error}"
            )
        raise
    print(f"Queued analysis-phase job: {job.id} (run {run_id})")
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
        job = video_queue.enqueue(
            editor.run_revision_render,
            project_id,
            revision_id,
            run_id,
            at_front=True,
        )
    except Exception as error:
        if run_id:
            run_ledger.finish_run(
                run_id, "failed", error=f"enqueue failed: {error}"
            )
        raise
    print(f"Queued revision-render job: {job.id} (run {run_id})")
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

    _set_project_status(project_id, "running")
    try:
        # Stage 5A: the analysis phase resolves the genre pack the
        # same way the full pipeline does, so a gated run's research
        # is shaped by the same pack its production will record.
        _genre_info = genre_packs.resolve_genre_pack(options, script)
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
    if run_id:
        run_ledger.set_run_status(
            run_id, "awaiting_approval", stage="script_analysis"
        )
    _set_project_status(project_id, "awaiting_approval")
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


def run_full_pipeline(project_id: str, script: str, run_id: str = None, options=None):
    print()
    print("=" * 70)
    print("DOCUMENTARY STUDIO")
    print("FULL PRODUCTION PIPELINE")
    print("=" * 70)
    print(f"Project: {project_id}")
    print("=" * 70)
    print()

    project_dir = media_paths.project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)

    run_metrics.begin_run()

    # Stage 3 options plumbing: recognised keys "music_file" (path),
    # "voice_id" (str), "captions" ("burn"|"sidecar"|"off", default
    # "burn"). Unknown keys pass through harmlessly into the
    # ledger/manifest options snapshot.
    _merged_options = {"entrypoint": "run_full_pipeline"}
    if isinstance(options, dict):
        _merged_options.update(options)
    options = _merged_options
    captions_mode = str(options.get("captions") or "burn").lower()
    if captions_mode not in ("burn", "sidecar", "off"):
        captions_mode = "burn"

    # Stage 5A: resolve the genre pack (explicit "genre" option,
    # else the conservative script detector, else "general") BEFORE
    # the ledger run begins, so the resolved pack id lands in the
    # run's options snapshot and therefore in the run manifest.
    _genre_info = genre_packs.resolve_genre_pack(options, script)
    _genre_pack = _genre_info["pack"]
    options["genre_pack"] = _genre_info["pack_id"]
    options["genre_source"] = _genre_info["source"]
    print(
        f"Genre pack: {_genre_info['pack_id']} "
        f"(source: {_genre_info['source']})"
    )

    # Stage 2: claim or begin the ledger run. All fail-soft — the
    # ledger records the run; it never gates it. When the caller
    # carries a run_id (the enqueue path), the queued run is claimed
    # atomically; a run whose state is KNOWN to be non-claimable
    # (cancelled while queued, claimed by another delivery, already
    # finished) is not executed twice. When the ledger cannot be
    # read, the run proceeds (availability first). The claim happens
    # BEFORE any status write-back so an aborted duplicate never
    # marks the project "running".
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
                print(
                    f"Run {run_id} not claimable (status: "
                    f"{existing['status']}); not executing."
                )
                return {
                    "project_id": project_id,
                    "status": existing["status"],
                    "run_id": run_id,
                    "deduplicated_execution": True,
                }
        _run_id = run_id
    else:
        _run_id = run_ledger.begin_run(
            project_id,
            script,
            options=_merged_options,
        )

    state = create_production_state()
    state["genre_pack"] = {
        "id": _genre_info["pack_id"],
        "source": _genre_info["source"],
        "requested": _genre_info["requested"],
    }
    save_production_state(project_dir, state)
    _set_project_status(project_id, "running")

    # Stage 2 (continued): per-run context (2C) + run manifest (2E).
    state["_run_id"] = _run_id
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
    run_ledger.write_manifest(project_dir, _manifest)

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
    final_video = project_dir / "documentary_final_output.mp4"

    timeline_file = None
    expected_duration = None
    soundtrack_record = None

    try:
        run_stage(state, "voice_actor", project_dir)
        print("Voice Actor")
        voice_meta = generate_narration(
            text=script,
            output_path=str(narration_file),
            voice_id=options.get("voice_id"),
        )
        if isinstance(voice_meta, dict):
            state["voice"] = voice_meta
            print(f"Voice: {voice_meta}")
        if not narration_file.exists():
            raise RuntimeError("narration.wav was not created.")
        print(f"Voiceover created: {narration_file}")
        # Stage 3I: captions.py is the single caption writer —
        # Whisper word timings when usable, proportional segments
        # (stamped provisional) otherwise; SRT/VTT sidecars always.
        build_captions(str(narration_file), script, str(captions_json))
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
        finish_stage(state, "voice_actor", project_dir)
    except Exception as error:
        fail_pipeline(state, "voice_actor", error, project_dir)

    try:
        run_stage(state, "script_analysis", project_dir)
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
        finish_stage(state, "script_analysis", project_dir)
    except Exception as error:
        fail_pipeline(state, "script_analysis", error, project_dir)

    try:
        run_stage(state, "asset_manager", project_dir)
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

        for scene in scenes:
            scene_number = scene["scene_number"]
            scene["project_id"] = project_id
            scene["script_hash"] = script_hash
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
            "registry": _get_registry(project_id).summary(),
        }
        (project_dir / "asset_manager_metrics.json").write_text(
            _json.dumps(_am_report, indent=2), encoding="utf-8"
        )
        print("=" * 60)
        print(f"ASSET MANAGER METRICS: {_am_report['asset_manager_minutes']} min")
        print(_json.dumps(_am_final))
        print("=" * 60)
        finish_stage(state, "asset_manager", project_dir)
    except Exception as error:
        fail_pipeline(state, "asset_manager", error, project_dir)

    try:
        # Motion Designer temporarily disabled
        print("Motion Designer")
        # AUD-30: this stage previously only ever called
        # finish_stage — it never recorded a start. Run it through
        # the same stage lifecycle as every other stage.
        run_stage(state, "motion_designer", project_dir)

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
                            "scenario": _sentence.get("scenario") or {},
                        }
                    )
                    break

        if graphic_targets:
            print(
                f"Graphics targets (uncovered sentences): "
                f"{len(graphic_targets)}"
            )

        motion_graphics_dir = project_dir / "motion_graphics"
        motion_graphics = build_motion_graphics_assets(
            motion_data,
            motion_graphics_dir,
            mode="balanced",
            targets=graphic_targets,
            max_stats=MOTION_GRAPHIC_MAX_STATS,
            genre_pack=_genre_pack,
        )

        # Attach each standalone motion graphic to its target scene/sentence
        for graphic in motion_graphics:
            scene_number = graphic.get("scene_number")
            sentence_index = graphic.get("sentence_index")

            if scene_number is None:
                continue

            for processed_scene in processed_scenes:
                scene = processed_scene.get("scene") or {}
                if scene.get("scene_number") != scene_number:
                    continue

                visual = dict(graphic)
                visual["type"] = "video"
                visual["source"] = "cinemora_motion_graphics"
                visual["motion_graphic"] = True
                visual["sentence_index"] = sentence_index
                visual["file_path"] = graphic.get("file_path") or graphic.get("path")
                visual["path"] = visual["file_path"]

                processed_scene.setdefault("videos", []).append(visual)
                break

        print(f"Motion graphics generated: {len(motion_graphics)}")
        print(f"Motion graphics attached to timeline candidates: {sum(1 for s in processed_scenes for v in s.get("videos", []) if v.get("motion_graphic"))}")
        finish_stage(state, "motion_designer", project_dir)
    except Exception as error:
        fail_pipeline(state, "motion_designer", error, project_dir)

    # ==================================================
    # SOUND DESIGNER
    # ==================================================

    try:
        run_stage(state, "sound_designer", project_dir)
        print("Sound Designer")

        # Stage 3H: an explicitly supplied music file (options)
        # wins; otherwise the legacy project background.mp3 if it
        # exists. No downloads, no library — user-supplied only.
        music_option = options.get("music_file")
        if music_option:
            soundtrack_music = str(music_option)
        else:
            legacy_music = project_dir / "background.mp3"
            soundtrack_music = (
                str(legacy_music) if legacy_music.exists() else None
            )

        soundtrack_record = prepare_soundtrack(
            narration_file=str(narration_file),
            output_file=str(soundtrack_file),
            music_file=soundtrack_music,
        )

        if not soundtrack_file.exists():
            raise RuntimeError(
                "Sound Designer did not create soundtrack."
            )

        if isinstance(soundtrack_record, dict):
            state["soundtrack"] = soundtrack_record
            (project_dir / "soundtrack.json").write_text(
                json.dumps(soundtrack_record, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"Soundtrack record: {soundtrack_record}")

        print(f"Soundtrack created: {soundtrack_file}")

        finish_stage(
            state,
            "sound_designer",
            project_dir,
        )

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
        print("Video Editor")

        # ----------------------------------------------
        # Build visual timeline
        # ----------------------------------------------

        timeline_result = build_project_timeline(
            project_id,
            processed_scenes,
            output_dir=str(media_paths.media_root()),
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
            uncovered = coverage.get("uncovered_sentences") or []
            detail = ", ".join(
                f"sentence {u.get('sentence_number')} "
                f"(scene {u.get('scene_number')}, "
                f"{u.get('start')}s-{u.get('end')}s)"
                for u in uncovered
            )
            raise RuntimeError(
                "TIMELINE COVERAGE INCOMPLETE: "
                f"{len(uncovered)} of {coverage.get('total_sentences')} "
                f"narration sentences have no visual coverage: {detail}. "
                "Refusing to render an incomplete documentary."
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

            create_ass_file(
                str(captions_json),
                str(captions_ass),
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

        if final_video.exists():
            try:
                final_video.unlink()
            except OSError as error:
                print(
                    "WARNING: could not remove old "
                    f"final video: {error}"
                )

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
            execution=run_metrics.snapshot(),
            soundtrack=(
                soundtrack_record
                if isinstance(soundtrack_record, dict)
                else None
            ),
            genre_pack={
                "id": _genre_info["pack_id"],
                "source": _genre_info["source"],
            },
        )
        write_asset_report(project_dir, asset_report)

        rights_manifest = build_rights_manifest(processed_scenes)
        write_rights_manifest(project_dir, rights_manifest)
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
        # Final audit
        # ----------------------------------------------

        final_audit = validate_final_video(
            str(final_video),
            expected_duration=(
                expected_duration
                if expected_duration > 0
                else None
            ),
            asset_report=asset_report,
        )

        # MA-14: honour the auditor's verdict. A failed verdict
        # fails the production through the normal fail path — the
        # rendered file may exist, but the recorded outcome (project
        # status + failure asset report) is a failure carrying the
        # auditor's reasons. Warnings alone never fail a run.
        _verdict = final_audit.get("verdict") or {}
        if _verdict.get("evaluated") and not _verdict.get("passed", True):
            raise RuntimeError(
                "Final video audit FAILED: "
                + "; ".join(_verdict.get("failures") or ["unspecified"])
            )

        print(
            "Final video audit complete: "
            f"{final_audit['duration_seconds']:.3f}s"
        )

        finish_stage(
            state,
            "video_editor",
            project_dir,
        )

    except Exception as error:
        fail_pipeline(
            state,
            "video_editor",
            error,
            project_dir,
        )


    state["status"] = "completed"
    state["current_stage"] = None
    # AUD-30: completed_at was declared in the state schema but never
    # set; a finished run now carries its finish time.
    state["completed_at"] = utc_now()
    save_production_state(project_dir, state)
    _set_project_status(project_id, "completed")

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
        "production_state": str(project_dir / "production_state.json"),
        "case_bible": str(project_dir / "case_bible.json"),
        "scene_count": len(scenes),
        "sentence_count": len(sentences),
        "visual_count": visual_count,
        "expected_duration_seconds": expected_duration,
        "script_hash": script_hash,
    }

    result_file = project_dir / "pipeline_result.json"
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

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
        manifest_path = None
        if isinstance(_manifest, dict):
            stage_outputs = [
                name
                for name in (
                    "narration.wav",
                    "captions.json",
                    "case_bible.json",
                    "timeline.json",
                    "documentary_final_output.mp4",
                    "asset_report.json",
                    "rights_manifest.json",
                    "production_state.json",
                    "pipeline_result.json",
                )
                if (project_dir / name).exists()
            ]
            enriched = run_ledger.enrich_manifest(
                _manifest,
                status="completed",
                execution=run_metrics.snapshot(),
                asset_report=asset_report,
                stage_outputs=stage_outputs,
            )
            written = run_ledger.write_manifest(project_dir, enriched)
            manifest_path = str(written) if written else None
        run_ledger.finish_run(
            _run_id,
            "completed",
            execution=run_metrics.snapshot(),
            manifest_path=manifest_path,
        )
    if "_run_context_previous" in state:
        run_context.deactivate(state.pop("_run_context_previous"))

    print()
    print("=" * 70)
    print("DOCUMENTARY PIPELINE COMPLETE")
    print("=" * 70)
    print(f"FINAL VIDEO:\n{final_video}")
    return result


def enqueue_script_analysis(project_id: str, script: str):
    job = video_queue.enqueue(
        analyze_project_script, project_id, script, at_front=True
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
        process_project_media, project_id, scenes, at_front=True
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
        process_project_media, project_id, scenes, at_front=True
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


