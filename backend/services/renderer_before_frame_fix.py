import os
import subprocess
from pathlib import Path


FFMPEG_TIMEOUT = 60

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".webm",
    ".mkv",
    ".avi",
}


# ============================================================
# FFMPEG
# ============================================================

def run_ffmpeg(
    command,
    timeout=FFMPEG_TIMEOUT,
):

    print()
    print("FFmpeg:")
    print(" ".join(str(x) for x in command))

    try:

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )

        if result.returncode != 0:

            print()
            print(
                "FFMPEG FAILED:"
            )

            print(
                result.stderr[-4000:]
            )

            return False

        return True

    except subprocess.TimeoutExpired:

        print(
            "FFMPEG TIMEOUT"
        )

        return False

    except Exception as error:

        print(
            f"FFMPEG ERROR: {error}"
        )

        return False


# ============================================================
# VALIDATION
# ============================================================

def valid_media(path):

    if not path:
        return False

    path = Path(path)

    if not path.exists():
        return False

    try:
        if path.stat().st_size < 1000:
            return False
    except OSError:
        return False

    return True


# ============================================================
# IMAGE CLIP
# ============================================================

def create_image_clip(
    image_path,
    duration,
    output_path,
):

    if not valid_media(image_path):

        print(
            f"Skipping invalid image: "
            f"{image_path}"
        )

        return False

    duration = max(
        float(duration),
        0.1,
    )

    output_path = Path(
        output_path
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    command = [
        "ffmpeg",
        "-y",

        "-loop",
        "1",

        "-i",
        str(image_path),

        "-t",
        str(duration),

        "-vf",
        (
            "scale=1920:1080:"
            "force_original_aspect_ratio=increase,"
            "crop=1920:1080,"
            "setsar=1,"
            "format=yuv420p"
        ),

        "-r",
        "30",

        "-c:v",
        "libx264",

        "-preset",
        "ultrafast",

        "-crf",
        "23",

        "-pix_fmt",
        "yuv420p",

        "-an",

        "-movflags",
        "+faststart",

        str(output_path),
    ]

    success = run_ffmpeg(
        command
    )

    if not success:
        return False

    if not valid_media(output_path):

        print(
            "Image clip was not created."
        )

        return False

    print(
        f"IMAGE CLIP COMPLETE: "
        f"{output_path}"
    )

    return True


# ============================================================
# VIDEO CLIP
# ============================================================

def create_video_clip(
    video_path,
    duration,
    output_path,
):

    if not valid_media(video_path):

        print(
            f"Skipping invalid video: "
            f"{video_path}"
        )

        return False

    duration = max(
        float(duration),
        0.1,
    )

    output_path = Path(
        output_path
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    extension = Path(
        video_path
    ).suffix.lower()

    # --------------------------------------------------------
    # Normalize unusual containers through FFmpeg.
    # --------------------------------------------------------

    command = [
        "ffmpeg",
        "-y",

        "-i",
        str(video_path),

        "-t",
        str(duration),

        "-vf",
        (
            "scale=1920:1080:"
            "force_original_aspect_ratio=increase,"
            "crop=1920:1080,"
            "setsar=1,"
            "format=yuv420p"
        ),

        "-r",
        "30",

        "-an",

        "-c:v",
        "libx264",

        "-preset",
        "ultrafast",

        "-crf",
        "23",

        "-pix_fmt",
        "yuv420p",

        "-movflags",
        "+faststart",

        str(output_path),
    ]

    success = run_ffmpeg(
        command
    )

    if not success:
        return False

    if not valid_media(output_path):

        print(
            "Video clip was not created."
        )

        return False

    print(
        f"VIDEO CLIP COMPLETE: "
        f"{output_path}"
    )

    return True


# ============================================================
# SCENE RENDERING
# ============================================================

def render_scene(
    scene,
    project_dir,
):

    scene_number = scene.get(
        "scene_number",
        1,
    )

    items = (
        scene.get("items")
        or scene.get("visuals")
        or []
    )

    if not items:

        print(
            f"Scene {scene_number}: "
            "no timeline items."
        )

        return []

    clips_dir = Path(
        project_dir
    ) / "clips" / (
        f"scene_{scene_number}"
    )

    clips_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    rendered_clips = []

    print()
    print("=" * 70)
    print(
        f"RENDERING SCENE "
        f"{scene_number}"
    )
    print("=" * 70)

    for index, item in enumerate(items):

        media_type = (
            item.get("type")
            or ""
        ).lower()

        media_path = (
            item.get("file_path")
            or item.get("path")
            or ""
        )

        duration = float(
            item.get(
                "duration",
                5,
            )
        )

        if not media_path:

            print(
                f"Item {index + 1}: "
                "missing media path"
            )

            continue

        output_path = (
            clips_dir
            / f"clip_{index + 1:04d}.mp4"
        )

        print()
        print(
            f"Visual {index + 1}/"
            f"{len(items)}"
        )

        print(
            f"Type: {media_type}"
        )

        print(
            f"Duration: "
            f"{duration:.2f}s"
        )

        print(
            f"Source: "
            f"{media_path}"
        )

        # ----------------------------------------------------
        # VIDEO
        # ----------------------------------------------------

        if media_type == "video":

            success = create_video_clip(
                media_path,
                duration,
                output_path,
            )

        # ----------------------------------------------------
        # IMAGE
        # ----------------------------------------------------

        else:

            success = create_image_clip(
                media_path,
                duration,
                output_path,
            )

        if success:

            rendered_clips.append(
                str(output_path)
            )

        else:

            print(
                f"FAILED visual "
                f"{index + 1}"
            )

    print()
    print(
        f"Scene {scene_number}: "
        f"{len(rendered_clips)} valid clips"
    )

    return rendered_clips


# ============================================================
# CONCAT
# ============================================================

def concatenate_clips(
    clips,
    output_path,
):

    if not clips:

        print(
            "No clips available "
            "for concatenation."
        )

        return False

    output_path = Path(
        output_path
    )

    concat_file = (
        output_path.parent
        / "concat.txt"
    )

    with open(
        concat_file,
        "w",
        encoding="utf-8",
    ) as file:

        for clip in clips:

            clip_path = (
                Path(clip)
                .resolve()
            )

            # FFmpeg concat format.
            safe_path = (
                str(clip_path)
                .replace(
                    "\\",
                    "/",
                )
                .replace(
                    "'",
                    "'\\''",
                )
            )

            file.write(
                f"file '{safe_path}'\n"
            )

    command = [
        "ffmpeg",
        "-y",

        "-f",
        "concat",

        "-safe",
        "0",

        "-i",
        str(concat_file),

        "-c",
        "copy",

        "-movflags",
        "+faststart",

        str(output_path),
    ]

    success = run_ffmpeg(
        command
    )

    if not success:
        return False

    if not valid_media(output_path):

        print(
            "Final concatenated "
            "video missing."
        )

        return False

    print()
    print(
        f"CONCAT COMPLETE: "
        f"{output_path}"
    )

    return True


# ============================================================
# COMPLETE TIMELINE RENDER
# ============================================================

def render_timeline(
    scenes,
    project_dir,
):

    project_dir = Path(
        project_dir
    )

    project_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    all_clips = []

    print()
    print("=" * 70)
    print("DOCUMENTARY RENDERER")
    print("=" * 70)

    print(
        f"Scenes: {len(scenes)}"
    )

    # --------------------------------------------------------
    # Render every scene
    # --------------------------------------------------------

    for scene in scenes:

        scene_clips = render_scene(
            scene,
            str(project_dir),
        )

        all_clips.extend(
            scene_clips
        )

    # --------------------------------------------------------
    # Final documentary
    # --------------------------------------------------------

    raw_video = (
        project_dir
        / "documentary.mp4"
    )

    if raw_video.exists():

        try:
            raw_video.unlink()
        except OSError:

            print(
                "Could not remove "
                "existing documentary.mp4"
            )

    success = concatenate_clips(
        all_clips,
        raw_video,
    )

    if not success:

        raise RuntimeError(
            "Could not create "
            "documentary.mp4"
        )

    print()
    print("=" * 70)
    print("RENDER COMPLETE")
    print("=" * 70)

    print(
        f"Clips: {len(all_clips)}"
    )

    print(
        f"Video: {raw_video}"
    )

    print("=" * 70)

    return str(raw_video)