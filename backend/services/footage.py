import os
import json
import requests
from urllib.parse import urlparse


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA_DIR = os.path.join(BASE_DIR, "media")


def ensure_scene_directory(scene_number: int):
    """
    Create the media directory for a scene.
    """

    scene_dir = os.path.join(
        MEDIA_DIR,
        f"scene_{scene_number}"
    )

    os.makedirs(scene_dir, exist_ok=True)

    return scene_dir


def get_video_extension(url: str):
    """
    Try to determine the video file extension from a URL.
    """

    try:
        path = urlparse(url).path.lower()

        for extension in [
            ".mp4",
            ".mov",
            ".webm",
            ".mkv",
            ".avi",
        ]:
            if path.endswith(extension):
                return extension

    except Exception:
        pass

    return ".mp4"


def download_direct_video(
    video_url: str,
    output_path: str,
    timeout: int = 60,
):
    """
    Download a direct video file URL.

    This should only be used for footage that is actually
    available for direct downloading and that you have
    permission/license to use.
    """

    response = requests.get(
        video_url,
        stream=True,
        timeout=timeout,
        headers={
            "User-Agent": "Mozilla/5.0"
        },
    )

    response.raise_for_status()

    content_type = response.headers.get(
        "content-type",
        ""
    ).lower()

    if not (
        content_type.startswith("video/")
        or "application/octet-stream" in content_type
    ):
        raise RuntimeError(
            f"URL does not appear to be a direct video file. "
            f"Content-Type: {content_type}"
        )

    output_dir = os.path.dirname(output_path)

    if output_dir:
        os.makedirs(
            output_dir,
            exist_ok=True
        )

    with open(output_path, "wb") as file:

        for chunk in response.iter_content(
            chunk_size=1024 * 1024
        ):

            if chunk:
                file.write(chunk)

    return output_path


def save_video_sources(
    scene_number: int,
    videos: list,
):
    """
    Save discovered video sources for a scene.

    This does not download copyrighted streaming videos.
    It stores the source information so the renderer/UI
    can show the available footage sources.
    """

    scene_dir = ensure_scene_directory(
        scene_number
    )

    output_file = os.path.join(
        scene_dir,
        "video_sources.json"
    )

    cleaned_videos = []

    for video in videos:

        video_url = video.get(
            "video_url",
            ""
        )

        if not video_url:
            continue

        cleaned_videos.append({
            "title": video.get(
                "title",
                ""
            ),

            "video_url": video_url,

            "source": video.get(
                "source",
                ""
            ),

            "snippet": video.get(
                "snippet",
                ""
            ),

            "date": video.get(
                "date",
                ""
            ),

            "duration": video.get(
                "duration",
                ""
            ),

            "thumbnail_url": video.get(
                "thumbnail_url",
                ""
            ),

            "status": "source_only",
        })

    with open(
        output_file,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            cleaned_videos,
            file,
            indent=2,
            ensure_ascii=False,
        )

    return output_file


def create_footage_manifest(
    scene_number: int,
    images: list,
    videos: list,
):
    """
    Create one manifest containing all media
    discovered for a scene.
    """

    scene_dir = ensure_scene_directory(
        scene_number
    )

    manifest_path = os.path.join(
        scene_dir,
        "media_manifest.json"
    )

    manifest = {
        "scene_number": scene_number,

        "images": images,

        "videos": videos,

        "image_count": len(images),

        "video_count": len(videos),

        "status": "ready_for_editing",
    }

    with open(
        manifest_path,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            manifest,
            file,
            indent=2,
            ensure_ascii=False,
        )

    return manifest_path


def prepare_scene_footage(scene_result):
    """
    Prepare media discovered by the media pipeline.

    The function:
    - creates the scene directory
    - saves video sources
    - creates a media manifest
    """

    scene_number = scene_result[
        "scene_number"
    ]

    images = scene_result.get(
        "images",
        []
    )

    videos = scene_result.get(
        "videos",
        []
    )

    ensure_scene_directory(
        scene_number
    )

    video_sources_file = save_video_sources(
        scene_number,
        videos,
    )

    manifest_file = create_footage_manifest(
        scene_number,
        images,
        videos,
    )

    return {
        "scene_number": scene_number,

        "video_sources_file": video_sources_file,

        "manifest_file": manifest_file,

        "image_count": len(images),

        "video_count": len(videos),

        "status": "prepared",
    }