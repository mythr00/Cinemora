import json, sys
from pathlib import Path

pid = "8a367fbe-9477-463d-a1b3-7ebf58116ff4"
target_scene_number = int(sys.argv[1])

d = json.load(open(Path("media")/pid/"timeline.json", encoding="utf-8"))

for scene in d["scenes"]:
    if scene["scene_number"] == target_scene_number:
        clean = {k: v for k, v in scene.items() if k not in ("items", "visuals")}
        print(json.dumps(clean, indent=2, ensure_ascii=False))
        break
