import json
import os
from pathlib import Path

from redis import Redis
from rq import Queue

from services.script_analyzer import analyze_script
from services.media_pipeline import process_scene_media
from services.timeline import build_project_timeline
from services.renderer import render_timeline
from services.narration import generate_narration
from services.audio_mixer import add_narration
from services.music import add_background_music
from services.captions import transcribe_audio
from services.caption_renderer import burn_captions
from services.graphics_pipeline import build_graphics

from services.production_state import (
    create_production_state,
    start_stage,
    complete_stage,
    fail_stage,
)


# ============================================================
# CONFIG
# ============================================================

REDIS_URL = os.getenv(
    "REDIS_URL",
    "redis://127.0.0.1:6379/0",
)

QUEUE_NAME = "video_generation"

redis_connection = Redis.from_url(
    REDIS_URL,
    decode_responses=False,
)

video_queue = Queue(
    QUEUE_NAME,
    connection=redis_connection,
    default_timeout=3600,
)


# ============================================================
# SCENE CONFIG
# ============================================================

# The script analyzer currently returns sentence-level analysis.
#
# The media pipeline already supports:
#
# scene = {
#     "scene_number": ...,
#     "sentences": [...]
# }
#
# We therefore group analyzed sentences into manageable
# production scenes.
#
# 40 sentences per scene gives us approximately 13-14 scenes
# for a 500+ sentence documentary.
#
# This is deliberately kept here rather than inside the
# analyzer so the analyzer remains responsible only for
# sentence understanding.

SENTENCES_PER_SCENE = 40


# ============================================================
# HELPERS
# ============================================================

def save_production_state(
    project_dir: Path,
    state: dict,
):
    """
    Save the current production state.
    """

    state_file = (
        project_dir /
        "production_state.json"
    )

    with open(
        state_file,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            state,
            f,
            indent=2,
            ensure_ascii=False,
        )

    return state_file


def run_stage(
    state: dict,
    stage: str,
    project_dir: Path,
):
    """
    Start a production stage and persist its state.
    """

    start_stage(
        state,
        stage,
    )

    save_production_state(
        project_dir,
        state,
    )

    print()
    print("========================================")
    print(f"STAGE: {stage.upper()}")
    print("========================================")
    print()


def finish_stage(
    state: dict,
    stage: str,
    project_dir: Path,
):
    """
    Complete a production stage and persist its state.
    """

    complete_stage(
        state,
        stage,
    )

    save_production_state(
        project_dir,
        state,
    )


def fail_pipeline(
    state: dict,
    stage: str,
    error: Exception,
    project_dir: Path,
):
    """
    Mark the current stage as failed and persist state.
    """

    fail_stage(
        state,
        stage,
        str(error),
    )

    save_production_state(
        project_dir,
        state,
    )

    print()
    print("========================================")
    print("PIPELINE FAILED")
    print("========================================")
    print(f"Stage: {stage}")
    print(f"Error: {error}")
    print()

    raise error


# ============================================================
# SCRIPT ANALYSIS → PRODUCTION SCENES
# ============================================================

def build_production_scenes(
    script: str,
):
    """
    Run the sentence analyzer and convert its result into
    production scenes.

    analyze_script() returns:

        {
            "script": str,
            "sentence_count": int,
            "sentences": [
                {
                    "text": ...,
                    "entities": ...,
                    "actions": ...,
                    "event": ...,
                    "visual_types": ...,
                    "visual_intent": ...,
                    "keywords": ...,
                    "search_queries": ...,
                    "sentence_id": ...
                }
            ]
        }

    The media pipeline expects scene dictionaries containing
    a "sentences" list.

    This function performs that conversion.
    """

    analysis = analyze_script(
        script
    )

    if not isinstance(
        analysis,
        dict,
    ):
        raise RuntimeError(
            "Script analyzer returned an unexpected type: "
            f"{type(analysis).__name__}"
        )

    sentences = analysis.get(
        "sentences"
    )

    if not isinstance(
        sentences,
        list,
    ):
        raise RuntimeError(
            "Script analyzer did not return a "
            "'sentences' list."
        )

    if not sentences:
        raise RuntimeError(
            "Script analyzer returned zero sentences."
        )

    scenes = []

    for start_index in range(
        0,
        len(sentences),
        SENTENCES_PER_SCENE,
    ):

        scene_sentences = sentences[
            start_index:
            start_index + SENTENCES_PER_SCENE
        ]

        if not scene_sentences:
            continue

        scene_number = (
            len(scenes) + 1
        )

        scene_text_parts = []

        for sentence in scene_sentences:

            if not isinstance(
                sentence,
                dict,
            ):
                raise RuntimeError(
                    "Script analyzer returned an invalid "
                    "sentence object."
                )

            sentence_text = sentence.get(
                "text",
                "",
            )

            if sentence_text:
                scene_text_parts.append(
                    sentence_text
                )

        scene = {
            "scene_number": scene_number,
            "sentences": scene_sentences,
            "text": " ".join(
                scene_text_parts
            ),
            "sentence_count": len(
                scene_sentences
            ),
        }

        scenes.append(
            scene
        )

    if not scenes:
        raise RuntimeError(
            "Unable to create production scenes."
        )

    return {
        "analysis": analysis,
        "sentences": sentences,
        "scenes": scenes,
    }


# ============================================================
# TEST JOB
# ============================================================

def enqueue_test_job(
    project_id: str,
):

    job = video_queue.enqueue(
        test_job,
        project_id,
        at_front=True,
    )

    print(
        f"Queued test job: {job.id}"
    )

    return job.id


def test_job(
    project_id: str,
):

    print(
        f"Starting job for project: "
        f"{project_id}"
    )

    for step in range(
        1,
        6,
    ):

        print(
            f"Processing step {step}/5..."
        )

    print(
        f"Job completed for project: "
        f"{project_id}"
    )

    return {
        "project_id": project_id,
        "status": "completed",
    }


# ============================================================
# FULL PIPELINE QUEUE
# ============================================================

def enqueue_full_pipeline(
    project_id: str,
    script: str,
):

    job = video_queue.enqueue(
        run_full_pipeline,
        project_id,
        script,
        at_front=True,
    )

    print(
        f"Queued full pipeline job: {job.id}"
    )

    return job.id


# ============================================================
# FULL DOCUMENTARY PIPELINE
# ============================================================

def run_full_pipeline(
    project_id: str,
    script: str,
):

    print()
    print("=" * 70)
    print("DOCUMENTARY STUDIO")
    print("FULL PRODUCTION PIPELINE")
    print("=" * 70)
    print(f"Project: {project_id}")
    print("=" * 70)
    print()

    # --------------------------------------------------------
    # PROJECT DIRECTORY
    # --------------------------------------------------------

    project_dir = (
        Path("media") /
        project_id
    )

    project_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # PRODUCTION STATE
    # --------------------------------------------------------

    state = create_production_state()

    save_production_state(
        project_dir,
        state,
    )

    processed_scenes = []
    scenes = []

    narration_file = (
        project_dir /
        "narration.wav"
    )

    raw_video = (
        project_dir /
        "documentary.mp4"
    )

    narrated_video = (
        project_dir /
        "documentary_with_narration.mp4"
    )

    music_video = (
        project_dir /
        "documentary_with_music.mp4"
    )

    captions_json = (
        project_dir /
        "captions.json"
    )

    captions_ass = (
        project_dir /
        "captions.ass"
    )

    captioned_video = (
        project_dir /
        "documentary_captioned.mp4"
    )

    final_video = (
        project_dir /
        "documentary_final_output.mp4"
    )

    timeline_file = None

    # ========================================================
    # STAGE 1 — VOICE ACTOR
    # ========================================================

    try:

        run_stage(
            state,
            "voice_actor",
            project_dir,
        )

        print(
            "🎙 Voice Actor"
        )

        print(
            "Recording your voiceover..."
        )

        generate_narration(
            text=script,
            output_path=str(
                narration_file
            ),
        )

        if not narration_file.exists():
            raise RuntimeError(
                "Voice Actor completed but narration.wav "
                "was not created."
            )

        print()
        print(
            f"✓ Voiceover created: "
            f"{narration_file}"
        )

        print(
            f"Audio size: "
            f"{narration_file.stat().st_size:,} bytes"
        )

        finish_stage(
            state,
            "voice_actor",
            project_dir,
        )

    except Exception as error:

        fail_pipeline(
            state,
            "voice_actor",
            error,
            project_dir,
        )

    # ========================================================
    # SCRIPT ANALYSIS
    # ========================================================

    try:

        print()
        print(
            "Analyzing documentary script..."
        )

        production_data = (
            build_production_scenes(
                script
            )
        )

        analysis = production_data[
            "analysis"
        ]

        sentences = production_data[
            "sentences"
        ]

        scenes = production_data[
            "scenes"
        ]

        print()
        print(
            f"Analyzed sentences: "
            f"{len(sentences)}"
        )

        print(
            f"Created production scenes: "
            f"{len(scenes)}"
        )

        for scene in scenes:

            print(
                f"Scene "
                f"{scene['scene_number']}: "
                f"{scene['sentence_count']} sentences"
            )

        print()
        print(
            "✓ Script analysis completed."
        )

    except Exception as error:

        fail_pipeline(
            state,
            "script_analysis",
            error,
            project_dir,
        )

    # ========================================================
    # STAGE 2 — ASSET MANAGER
    # ========================================================

    try:

        run_stage(
            state,
            "asset_manager",
            project_dir,
        )

        print(
            "🎞 Asset Manager"
        )

        print(
            "Searching real footage for every sentence..."
        )

        print()

        for scene in scenes:

            scene_number = (
                scene["scene_number"]
            )

            print()
            print("=" * 60)
            print(
                f"PROCESSING SCENE "
                f"{scene_number}/{len(scenes)}"
            )
            print("=" * 60)

            print(
                f"Sentences: "
                f"{len(scene.get('sentences', []))}"
            )

            result = process_scene_media(
                scene,
                str(project_dir),
            )

            result[
                "duration_seconds"
            ] = scene.get(
                "duration_seconds",
                10,
            )

            result[
                "scene"
            ] = scene

            processed_scenes.append(
                result
            )

            print()
            print(
                f"Scene {scene_number}: "
                f"{len(result.get('images', []))} images, "
                f"{len(result.get('videos', []))} videos"
            )

        print()
        print(
            "✓ Asset Manager completed."
        )

        finish_stage(
            state,
            "asset_manager",
            project_dir,
        )

    except Exception as error:

        fail_pipeline(
            state,
            "asset_manager",
            error,
            project_dir,
        )

    # ========================================================
    # STAGE 3 — MOTION DESIGNER
    # ========================================================

    try:

        run_stage(
            state,
            "motion_designer",
            project_dir,
        )

        print(
            "🎨 Motion Designer"
        )

        print(
            "Building motion graphics & animations..."
        )

        print()

        timeline_result = (
            build_project_timeline(
                project_id,
                processed_scenes,
                output_dir="media",
            )
        )

        if not isinstance(
            timeline_result,
            dict,
        ):
            raise RuntimeError(
                "Timeline builder returned an invalid result."
            )

        if not timeline_result.get(
            "timeline_file"
        ):
            raise RuntimeError(
                "Timeline builder did not return a "
                "timeline_file."
            )

        timeline_file = Path(
            timeline_result[
                "timeline_file"
            ]
        )

        if not timeline_file.exists():
            raise RuntimeError(
                f"Timeline file was not created: "
                f"{timeline_file}"
            )

        print(
            f"Timeline created: "
            f"{timeline_file}"
        )

        # ----------------------------------------------------
        # GRAPHICS PREVIEW
        # ----------------------------------------------------

        graphics_preview = (
            project_dir /
            "graphics_preview.mp4"
        )

        try:

            build_graphics(
                video_file="",
                timeline_file=str(
                    timeline_file
                ),
                output_file=str(
                    graphics_preview
                ),
            )

        except Exception as graphics_error:

            print()
            print(
                "WARNING: Motion graphics preview "
                f"could not be created: {graphics_error}"
            )

        print()
        print(
            "✓ Motion Designer completed."
        )

        finish_stage(
            state,
            "motion_designer",
            project_dir,
        )

    except Exception as error:

        fail_pipeline(
            state,
            "motion_designer",
            error,
            project_dir,
        )

    # ========================================================
    # STAGE 4 — SOUND DESIGNER
    # ========================================================

    try:

        run_stage(
            state,
            "sound_designer",
            project_dir,
        )

        print(
            "🔊 Sound Designer"
        )

        print(
            "Mixing music & optimizing sound..."
        )

        print()

        if timeline_file is None:
            raise RuntimeError(
                "Timeline file missing before sound design."
            )

        with open(
            timeline_file,
            "r",
            encoding="utf-8",
        ) as f:

            timeline_data = json.load(
                f
            )

        render_scenes = (
            timeline_data.get(
                "scenes",
                [],
            )
        )

        if not render_scenes:
            raise RuntimeError(
                "Timeline contains no scenes to render."
            )

        print(
            f"Preparing "
            f"{len(render_scenes)} scenes..."
        )

        render_timeline(
            render_scenes,
            str(project_dir),
        )

        if not raw_video.exists():
            raise RuntimeError(
                "Renderer completed but documentary.mp4 "
                "was not created."
            )

        print(
            f"Base video created: "
            f"{raw_video}"
        )

        # ----------------------------------------------------
        # NARRATION MIX
        # ----------------------------------------------------

        add_narration(
            str(raw_video),
            str(narration_file),
            str(narrated_video),
        )

        if not narrated_video.exists():
            raise RuntimeError(
                "Narration mixing failed."
            )

        print(
            "✓ Narration mixed."
        )

        # ----------------------------------------------------
        # BACKGROUND MUSIC
        # ----------------------------------------------------

        music_file = Path(
            "media/test-project-2/background.mp3"
        )

        if music_file.exists():

            add_background_music(
                str(narrated_video),
                str(music_file),
                str(music_video),
            )

            if not music_video.exists():
                raise RuntimeError(
                    "Music mixer completed but output "
                    "video was not created."
                )

            print(
                "✓ Background music mixed."
            )

        else:

            print(
                "WARNING: Background music not found."
            )

            print(
                f"Expected: {music_file}"
            )

            music_video = narrated_video

        print()
        print(
            "✓ Sound Designer completed."
        )

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

    # ========================================================
    # STAGE 5 — VIDEO EDITOR
    # ========================================================

    try:

        run_stage(
            state,
            "video_editor",
            project_dir,
        )

        print(
            "✂ Video Editor"
        )

        print(
            "Cutting scenes & assembling the final video..."
        )

        print()

        # ----------------------------------------------------
        # WORD CAPTIONS
        # ----------------------------------------------------

        transcribe_audio(
            str(narration_file),
            str(captions_json),
        )

        if not captions_json.exists():
            raise RuntimeError(
                "Caption transcription failed."
            )

        print(
            f"✓ Captions generated: "
            f"{captions_json}"
        )

        # ----------------------------------------------------
        # ASS CAPTIONS
        # ----------------------------------------------------

        from services.caption_renderer import (
            create_ass_file,
        )

        create_ass_file(
            str(captions_json),
            str(captions_ass),
        )

        if not captions_ass.exists():
            raise RuntimeError(
                "ASS caption file was not created."
            )

        print(
            f"✓ ASS captions created: "
            f"{captions_ass}"
        )

        # ----------------------------------------------------
        # BURN CAPTIONS
        # ----------------------------------------------------

        burn_captions(
            str(music_video),
            str(captions_ass),
            str(captioned_video),
        )

        if not captioned_video.exists():
            raise RuntimeError(
                "Caption rendering failed."
            )

        print(
            f"✓ Captions burned: "
            f"{captioned_video}"
        )

        # ----------------------------------------------------
        # FINAL MOTION GRAPHICS
        # ----------------------------------------------------

        build_graphics(
            video_file=str(
                captioned_video
            ),
            timeline_file=str(
                timeline_file
            ),
            output_file=str(
                final_video
            ),
        )

        if not final_video.exists():
            raise RuntimeError(
                "Final video was not created."
            )

        print(
            f"✓ Final video created: "
            f"{final_video}"
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

    # ========================================================
    # FINAL RESULT
    # ========================================================

    state[
        "status"
    ] = "completed"

    state[
        "current_stage"
    ] = None

    save_production_state(
        project_dir,
        state,
    )

    visual_count = sum(
        len(
            scene_result.get(
                "images",
                [],
            )
        )
        +
        len(
            scene_result.get(
                "videos",
                [],
            )
        )
        for scene_result in processed_scenes
    )

    result = {
        "project_id": project_id,
        "status": "completed",
        "video": str(final_video),
        "timeline": (
            str(timeline_file)
            if timeline_file
            else None
        ),
        "narration": str(
            narration_file
        ),
        "captions": str(
            captions_json
        ),
        "production_state": str(
            project_dir /
            "production_state.json"
        ),
        "scene_count": len(
            scenes
        ),
        "sentence_count": len(
            sentences
        ),
        "visual_count": visual_count,
    }

    result_file = (
        project_dir /
        "pipeline_result.json"
    )

    with open(
        result_file,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            result,
            f,
            indent=2,
            ensure_ascii=False,
        )

    print()
    print("=" * 70)
    print("DOCUMENTARY PIPELINE COMPLETE")
    print("=" * 70)
    print()
    print(
        f"Scenes: "
        f"{result['scene_count']}"
    )
    print(
        f"Sentences: "
        f"{result['sentence_count']}"
    )
    print(
        f"Visuals: "
        f"{result['visual_count']}"
    )
    print()
    print(
        f"FINAL VIDEO:\n"
        f"{final_video}"
    )
    print()
    print(
        f"PRODUCTION STATE:\n"
        f"{project_dir / 'production_state.json'}"
    )
    print()
    print(
        f"RESULT:\n"
        f"{result_file}"
    )
    print()

    return result


# ============================================================
# LEGACY SCRIPT ANALYSIS
# ============================================================

def enqueue_script_analysis(
    project_id: str,
    script: str,
):

    job = video_queue.enqueue(
        analyze_project_script,
        project_id,
        script,
        at_front=True,
    )

    print(
        f"Queued analysis job: {job.id}"
    )

    return job.id


def analyze_project_script(
    project_id: str,
    script: str,
):

    print(
        f"Starting script analysis for project: "
        f"{project_id}"
    )

    production_data = (
        build_production_scenes(
            script
        )
    )

    scenes = production_data[
        "scenes"
    ]

    print(
        f"Created {len(scenes)} scenes."
    )

    media_job = video_queue.enqueue(
        process_project_media,
        project_id,
        scenes,
        at_front=True,
    )

    print(
        f"Media job queued: "
        f"{media_job.id}"
    )

    return {
        "project_id": project_id,
        "status": "analyzed",
        "scenes": scenes,
        "media_job_id": media_job.id,
    }


# ============================================================
# LEGACY MEDIA PROCESSING
# ============================================================

def enqueue_media_processing(
    project_id: str,
    scenes: list,
):

    job = video_queue.enqueue(
        process_project_media,
        project_id,
        scenes,
        at_front=True,
    )

    print(
        f"Queued media job: "
        f"{job.id}"
    )

    return job.id


def process_project_media(
    project_id: str,
    scenes: list,
):

    print(
        f"Starting media processing for project: "
        f"{project_id}"
    )

    project_dir = (
        Path("media") /
        project_id
    )

    project_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    processed_scenes = []

    for scene in scenes:

        result = process_scene_media(
            scene,
            str(project_dir),
        )

        result[
            "duration_seconds"
        ] = scene.get(
            "duration_seconds",
            10,
        )

        result[
            "scene"
        ] = scene

        processed_scenes.append(
            result
        )

    timeline_result = (
        build_project_timeline(
            project_id,
            processed_scenes,
            output_dir="media",
        )
    )

    return {
        "project_id": project_id,
        "status": "media_processed",
        "scenes": processed_scenes,
        "timeline": timeline_result,
    }