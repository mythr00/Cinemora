import json
from pathlib import Path

project_dir = Path("media/1e8db933-5815-45e7-9d02-a743b6e3ecec")
timeline_file = project_dir / "timeline.json"

with open(timeline_file, "r", encoding="utf-8") as f:
    data = json.load(f)

narration_duration = float(data["duration_seconds"])

items = []

for scene in data.get("scenes", []):
    for item in scene.get("items", []):
        items.append(item)

if not items:
    raise RuntimeError("No timeline items found")

items.sort(
    key=lambda x: (
        float(x.get("start", 0)),
        int(x.get("sentence_index", 0))
    )
)

# Every visual now owns the complete interval until
# the next sentence begins. The first visual owns the
# initial silence as well.
for i, item in enumerate(items):

    current_start = float(item.get("start", 0))

    if i == 0:
        render_start = 0.0
    else:
        render_start = current_start

    if i + 1 < len(items):
        next_start = float(
            items[i + 1].get("start", current_start)
        )
        render_end = next_start
    else:
        render_end = narration_duration

    duration = max(
        0.001,
        render_end - render_start
    )

    item["render_start"] = round(render_start, 3)
    item["render_end"] = round(render_end, 3)
    item["duration"] = round(duration, 3)

# Rebuild scene-level item timing metadata.
for scene in data.get("scenes", []):
    scene_items = scene.get("items", [])

    if scene_items:
        scene["render_start"] = min(
            float(x["render_start"])
            for x in scene_items
        )

        scene["render_end"] = max(
            float(x["render_end"])
            for x in scene_items
        )

        scene["render_duration_seconds"] = (
            scene["render_end"]
            - scene["render_start"]
        )

# Verify the actual rendered duration.
duration_sum = sum(
    float(item["duration"])
    for item in items
)

print("TIMELINE GLOBAL DURATION FIX")
print("=" * 60)
print(f"Narration duration : {narration_duration:.3f}")
print(f"Visual items       : {len(items)}")
print(f"Render duration    : {duration_sum:.3f}")
print(f"Difference         : {abs(duration_sum - narration_duration):.6f}")
print(
    f"First render       : "
    f"{items[0]['render_start']:.3f} -> "
    f"{items[0]['render_end']:.3f}"
)
print(
    f"Last render        : "
    f"{items[-1]['render_start']:.3f} -> "
    f"{items[-1]['render_end']:.3f}"
)

if abs(duration_sum - narration_duration) > 0.01:
    raise RuntimeError(
        "Duration verification failed"
    )

with open(timeline_file, "w", encoding="utf-8") as f:
    json.dump(
        data,
        f,
        indent=2,
        ensure_ascii=False
    )

print()
print("TIMELINE.JSON UPDATED SUCCESSFULLY")
