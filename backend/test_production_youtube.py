import os
from services.media_pipeline import process_scene_media

project_dir = r".\youtube_smoke_test"
os.makedirs(project_dir, exist_ok=True)

scene = {
    "project_id": "youtube_smoke_test",
    "scene_number": 1,
    "text": (
        "Apollo 11 approached the Moon during its historic mission. "
        "The astronauts prepared for the lunar landing. "
        "The Eagle eventually landed on the Moon."
    ),
    "narration": (
        "Apollo 11 approached the Moon during its historic mission. "
        "The astronauts prepared for the lunar landing. "
        "The Eagle eventually landed on the Moon."
    ),
}

result = process_scene_media(
    scene,
    project_dir,
    image_count=0,
    video_count=3,
)

print()
print("=" * 60)
print("SMOKE TEST RESULT")
print("=" * 60)
print("VIDEOS:", len(result.get("videos") or []))
print("IMAGES:", len(result.get("images") or []))
print("SENTENCES:", len(result.get("sentences") or []))

for i, video in enumerate(result.get("videos") or [], 1):
    print(
        f"VIDEO {i}: "
        f"title={video.get('title')} | "
        f"start={video.get('clip_start')} | "
        f"path={video.get('file_path')}"
    )
