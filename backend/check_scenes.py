import json
from pathlib import Path

pid = "8a367fbe-9477-463d-a1b3-7ebf58116ff4"
d = json.load(open(Path("media")/pid/"timeline.json", encoding="utf-8"))

for scene in d["scenes"]:
    items = scene.get("items", scene.get("visuals", []))
    item_duration = sum(float(i.get("duration", 0)) for i in items)
    scene_span = float(scene.get("end", 0)) - float(scene.get("start", 0))
    print(f"Scene {scene['scene_number']}: {len(items)} items, item-duration={item_duration:.2f}s, scene-span={scene_span:.2f}s, gap={scene_span - item_duration:.2f}s")
