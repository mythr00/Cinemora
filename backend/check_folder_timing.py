from pathlib import Path
from datetime import datetime

pid = "8a367fbe-9477-463d-a1b3-7ebf58116ff4"
base = Path("media") / pid

for n in [4, 5]:
    scene_dir = base / f"scene_{n}"
    subdirs = sorted(
        [p for p in scene_dir.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_ctime
    )
    print(f"--- scene_{n}: {len(subdirs)} sentence folders ---")
    for p in subdirs[:5]:
        print(" ", datetime.fromtimestamp(p.stat().st_ctime), p.name)
    print("  ...")
    for p in subdirs[-5:]:
        print(" ", datetime.fromtimestamp(p.stat().st_ctime), p.name)
    print()
