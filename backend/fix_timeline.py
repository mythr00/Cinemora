import json
from pathlib import Path

PROJECT = Path("media") / "15e0f37d-c3a3-4001-a801-e9f7007cb627"

TIMELINE_FILE = PROJECT / "timeline.json"

ACTUAL_VIDEO_DURATION = 52.662857


with open(TIMELINE_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)


old_duration = float(data["duration_seconds"])

scale = ACTUAL_VIDEO_DURATION / old_duration

print("========================================")
print("FIXING TIMELINE")
print("========================================")
print(f"Old duration: {old_duration:.3f}s")
print(f"Actual video: {ACTUAL_VIDEO_DURATION:.3f}s")
print(f"Scale: {scale:.6f}")
print()


project_time = 0.0


for scene in data["scenes"]:

    old_scene_duration = float(
        scene.get("duration_seconds", 10)
    )

    new_scene_duration = (
        old_scene_duration * scale
    )

    scene["duration_seconds"] = round(
        new_scene_duration,
        3
    )

    print(
        f"Scene {scene['scene_number']}: "
        f"{project_time:.3f}s → "
        f"{project_time + new_scene_duration:.3f}s"
    )

    for item in scene.get("timeline", []):

        old_start = float(item["start"])
        old_end = float(item["end"])

        new_start = old_start * scale
        new_end = old_end * scale

        item["start"] = round(
            new_start,
            3
        )

        item["end"] = round(
            new_end,
            3
        )

        item["duration"] = round(
            new_end - new_start,
            3
        )

        item["project_start"] = round(
            project_time + new_start,
            3
        )

        item["project_end"] = round(
            project_time + new_end,
            3
        )

    project_time += new_scene_duration


data["duration_seconds"] = round(
    project_time,
    3
)


with open(
    TIMELINE_FILE,
    "w",
    encoding="utf-8"
) as f:
    json.dump(
        data,
        f,
        indent=2,
        ensure_ascii=False
    )


print()
print("========================================")
print("TIMELINE FIXED")
print("========================================")
print(
    f"New duration: "
    f"{data['duration_seconds']:.3f}s"
)
print(
    f"Saved: {TIMELINE_FILE}"
)