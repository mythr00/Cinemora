import os

from services.timeline import build_project_timeline


PROJECT_ID = "15e0f37d-c3a3-4001-a801-e9f7007cb627"

PROJECT_DIR = os.path.join(
    "media",
    PROJECT_ID,
)


SCENE_DURATIONS = {
    1: 16,
    2: 13,
    3: 18,
    4: 11,
}


processed_scenes = []


for scene_number, duration in SCENE_DURATIONS.items():

    scene_dir = os.path.join(
        PROJECT_DIR,
        f"scene_{scene_number}",
    )

    print(
        f"\nScanning scene {scene_number}..."
    )

    images = []

    for filename in sorted(
        os.listdir(scene_dir)
    ):

        if not filename.lower().endswith(
            (
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
            )
        ):
            continue

        file_path = os.path.join(
            scene_dir,
            filename,
        )

        images.append({
            "path": file_path,
            "title": filename,
            "source": "downloaded_media",
            "url": None,
            "score": 0,
        })

    print(
        f"Found {len(images)} images"
    )

    processed_scenes.append({
        "scene_number": scene_number,
        "duration_seconds": duration,
        "images": images,
        "videos": [],
    })


print("\nBuilding timeline...")

result = build_project_timeline(
    PROJECT_ID,
    processed_scenes,
    output_dir="media",
)


print("\n========================================")
print("TIMELINE REBUILT")
print("========================================")

print(
    "File:",
    result["timeline_file"],
)

print(
    "Duration:",
    result["duration_seconds"],
    "seconds",
)

print(
    "Scenes:",
    result["scene_count"],
)