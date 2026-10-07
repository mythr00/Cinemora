import json
import sys
from pathlib import Path

from services.script_analyzer import analyze_script
from services.media_pipeline import process_scene_media
from services.timeline import build_project_timeline
from services.renderer import render_timeline
from services.narration import generate_narration
from services.audio_mixer import add_narration


BASE_DIR = Path(__file__).resolve().parent


def convert_analysis_to_scenes(analysis):
    """
    Convert sentence-level script analysis into scenes
    expected by the media pipeline.
    """

    sentences = analysis.get("sentences", [])
    scenes = []

    for index, sentence in enumerate(sentences, start=1):
        narration = sentence.get("text", "").strip()

        if not narration:
            continue

        word_count = len(narration.split())
        duration_seconds = max(
            5,
            round(word_count / 2.3, 2),
        )

        scene = {
            "scene_number": index,
            "narration": narration,
            "voiceover": narration,
            "duration_seconds": duration_seconds,
            "text": narration,
            "sentence_id": sentence.get("sentence_id", index),
            "entities": sentence.get("entities", []),
            "direct_entities": sentence.get("direct_entities", []),
            "dates": sentence.get("dates", []),
            "visual_types": sentence.get("visual_types", []),
            "actions": sentence.get("actions", []),
            "event": sentence.get("event", ""),
            "visual_intent": sentence.get("visual_intent", {}),
            "keywords": sentence.get("keywords", []),
            "search_queries": sentence.get("search_queries", []),
        }

        scenes.append(scene)

    return scenes


def generate_documentary(project_id: str, script: str):
    print("\n" + "=" * 60)
    print("DOCUMENTARY GENERATOR")
    print("=" * 60)

    project_dir = BASE_DIR / "media" / project_id

    project_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------
    # STEP 1 — ANALYZE SCRIPT
    # --------------------------------------------------

    print("\n[1/6] ANALYZING SCRIPT...")

    analysis = analyze_script(script)

    scenes = convert_analysis_to_scenes(analysis)

    if not scenes:
        raise RuntimeError(
            "Script analysis produced no scenes."
        )

    print(f"Created {len(scenes)} scenes.")
    print(
        f"Analyzed "
        f"{analysis.get('sentence_count', 0)} "
        f"sentences."
    )

    # --------------------------------------------------
    # STEP 2 — PROCESS MEDIA
    # --------------------------------------------------

    print("\n[2/6] PROCESSING MEDIA...")

    processed_scenes = []

    for scene in scenes:
        print(
            f"\nScene "
            f"{scene['scene_number']}..."
        )

        result = process_scene_media(
            scene,
            str(project_dir),
        )

        result["scene_number"] = scene["scene_number"]

        result["duration_seconds"] = scene.get(
            "duration_seconds",
            10,
        )

        result["narration"] = scene.get(
            "narration",
            scene.get("voiceover", ""),
        )

        result["voiceover"] = result["narration"]

        for key in [
            "sentence_id",
            "entities",
            "direct_entities",
            "dates",
            "visual_types",
            "actions",
            "event",
            "visual_intent",
            "keywords",
            "search_queries",
        ]:
            result[key] = scene.get(key)

        processed_scenes.append(result)

        print(
            f"Scene {scene['scene_number']}: "
            f"{result.get('image_count', 0)} images, "
            f"{result.get('video_count', 0)} videos"
        )

    # --------------------------------------------------
    # STEP 3 — GENERATE NARRATION
    # --------------------------------------------------

    print("\n[3/6] GENERATING NARRATION...")

    narration_text_parts = []

    for scene in scenes:
        text = scene.get(
            "narration",
            scene.get("voiceover", ""),
        )

        if text:
            narration_text_parts.append(text.strip())

    narration_text = "\n\n".join(narration_text_parts)

    if not narration_text:
        raise RuntimeError(
            "No narration text was found in the analyzed scenes."
        )

    narration_file = project_dir / "narration.wav"

    generate_narration(
        text=narration_text,
        output_path=str(narration_file),
    )

    # --------------------------------------------------
    # STEP 4 — BUILD TIMELINE
    # --------------------------------------------------

    print("\n[4/6] BUILDING TIMELINE...")

    timeline_result = build_project_timeline(
        project_id=project_id,
        processed_scenes=processed_scenes,
        output_dir=str(BASE_DIR / "media"),
    )

    timeline_file = timeline_result["timeline_file"]

    print(f"Timeline: {timeline_file}")

    # --------------------------------------------------
    # STEP 5 — RENDER VIDEO
    # --------------------------------------------------

    print("\n[5/6] RENDERING VIDEO...")

    silent_video = project_dir / "documentary.mp4"

    render_result = render_timeline(
        timeline_result.get("scenes") or timeline_result.get("timeline") or [],
        str(project_dir),
        expected_duration=timeline_result.get("duration"),
    )

    print(
        f"Silent video: "
        f"{render_result}"
    )

    # --------------------------------------------------
    # STEP 6 — ADD NARRATION
    # --------------------------------------------------

    print("\n[6/6] ADDING NARRATION...")

    final_video = (
        project_dir /
        "documentary_final.mp4"
    )

    add_narration(
        video_file=str(silent_video),
        narration_file=str(narration_file),
        output_file=str(final_video),
    )

    # --------------------------------------------------
    # SAVE RESULT
    # --------------------------------------------------

    result = {
        "project_id": project_id,
        "status": "completed",
        "scene_count": len(scenes),
        "sentence_count": analysis.get(
            "sentence_count",
            0,
        ),
        "timeline_file": str(timeline_file),
        "narration_file": str(narration_file),
        "video_file": str(final_video),
    }

    result_file = (
        project_dir /
        "generation_result.json"
    )

    with open(
        result_file,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            result,
            file,
            indent=2,
            ensure_ascii=False,
        )

    print("\n" + "=" * 60)
    print("DOCUMENTARY GENERATION COMPLETE")
    print("=" * 60)

    print(
        f"\nFINAL VIDEO:\n"
        f"{final_video}"
    )

    print(
        f"\nResult file:\n"
        f"{result_file}"
    )

    return result


def main():
    if len(sys.argv) < 2:
        print(
            "\nUsage:\n"
            "python generate_documentary.py script.txt [project_id]"
        )
        return

    script_file = Path(sys.argv[1])

    if not script_file.exists():
        raise FileNotFoundError(
            f"Script not found: {script_file}"
        )

    script = script_file.read_text(
        encoding="utf-8"
    )

    if not script.strip():
        raise ValueError(
            "Script file is empty."
        )

    project_id = (
        sys.argv[2]
        if len(sys.argv) >= 3
        else "documentary-test"
    )

    generate_documentary(
        project_id=project_id,
        script=script,
    )


if __name__ == "__main__":
    main()






