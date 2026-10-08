import hashlib
import json
import os
from pathlib import Path

from redis import Redis
from rq import Queue

from services.script_analyzer import analyze_script, write_case_bible
from services.media_pipeline import process_scene_media
from services.timeline import build_project_timeline
from services.renderer import render_timeline
from services.narration import generate_narration
from services.audio_mixer import add_narration
from services.music import prepare_soundtrack
from services.captions import transcribe_audio
from services.caption_renderer import burn_captions
from services.graphics_pipeline import build_motion_graphics_assets
from services.final_video_auditor import validate_final_video
from services.visual_registry import reset_registry

from services.production_state import (
    create_production_state,
    start_stage,
    complete_stage,
    fail_stage,
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


def save_production_state(project_dir: Path, state: dict):
    state_file = project_dir / "production_state.json"
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    return state_file


def run_stage(state: dict, stage: str, project_dir: Path):
    start_stage(state, stage)
    save_production_state(project_dir, state)
    print()
    print("========================================")
    print(f"STAGE: {stage.upper()}")
    print("========================================")
    print()


def finish_stage(state: dict, stage: str, project_dir: Path):
    complete_stage(state, stage)
    save_production_state(project_dir, state)


def fail_pipeline(state: dict, stage: str, error: Exception, project_dir: Path):
    fail_stage(state, stage, str(error))
    save_production_state(project_dir, state)
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


def build_production_scenes(script: str, project_id: str = "", project_dir=None):
    project_dir = Path(project_dir) if project_dir else (
        Path("media") / project_id if project_id else None
    )
    cache_path = None
    if project_dir:
        project_dir.mkdir(parents=True, exist_ok=True)
        cache_path = str(project_dir / "story_understanding_cache.json")

    analysis = analyze_script(
        script,
        project_id=project_id,
        cache_path=cache_path,
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


def enqueue_full_pipeline(project_id: str, script: str):
    job = video_queue.enqueue(
        run_full_pipeline,
        project_id,
        script,
        at_front=True,
    )
    print(f"Queued full pipeline job: {job.id}")
    return job.id


def run_full_pipeline(project_id: str, script: str):
    print()
    print("=" * 70)
    print("DOCUMENTARY STUDIO")
    print("FULL PRODUCTION PIPELINE")
    print("=" * 70)
    print(f"Project: {project_id}")
    print("=" * 70)
    print()

    project_dir = Path("media") / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    state = create_production_state()
    save_production_state(project_dir, state)

    processed_scenes = []
    scenes = []
    sentences = []
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

    try:
        run_stage(state, "voice_actor", project_dir)
        print("Voice Actor")
        generate_narration(text=script, output_path=str(narration_file))
        if not narration_file.exists():
            raise RuntimeError("narration.wav was not created.")
        print(f"Voiceover created: {narration_file}")
        transcribe_audio(str(narration_file), str(captions_json))
        if not captions_json.exists():
            raise RuntimeError("Caption transcription failed.")
        print(f"Captions generated: {captions_json}")
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
        )
        analysis = production_data["analysis"]
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

        motion_data = {
            "case_bible": analysis.get("bible") or {},
            "scenes": scenes,
            "processed_scenes": processed_scenes,
        }

        motion_graphics_dir = project_dir / "motion_graphics"
        motion_graphics = build_motion_graphics_assets(
            motion_data,
            motion_graphics_dir,
            mode="balanced",
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

        music_file = project_dir / "background.mp3"

        prepare_soundtrack(
            narration_file=str(narration_file),
            output_file=str(soundtrack_file),
            music_file=(
                str(music_file)
                if music_file.exists()
                else None
            ),
        )

        if not soundtrack_file.exists():
            raise RuntimeError(
                "Sound Designer did not create soundtrack."
            )

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
            output_dir="media",
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
        # Captions
        # ----------------------------------------------

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
            str(captioned_video),
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
        # Final audit
        # ----------------------------------------------

        final_audit = validate_final_video(
            str(final_video),
            expected_duration=(
                expected_duration
                if expected_duration > 0
                else None
            ),
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
    save_production_state(project_dir, state)

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
    project_dir = Path("media") / project_id
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
    project_dir = Path("media") / project_id
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
        project_id, processed_scenes, output_dir="media"
    )
    return {
        "project_id": project_id,
        "status": "media_processed",
        "scenes": processed_scenes,
        "timeline": timeline_result,
    }


