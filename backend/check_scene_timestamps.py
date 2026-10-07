import os
from pathlib import Path
from datetime import datetime

pid = "8a367fbe-9477-463d-a1b3-7ebf58116ff4"
base = Path("media") / pid

for n in [4, 5, 6]:
    scene_dir = base / f"scene_{n}"
    files = sorted(scene_dir.rglob("*"), key=lambda p: p.stat().st_mtime if p.is_file() else 0)
    files = [p for p in files if p.is_file()]
    print(f"--- scene_{n}: {len(files)} files ---")
    if files:
        print("  earliest:", datetime.fromtimestamp(files[0].stat().st_mtime), files[0].name)
        print("  latest:  ", datetime.fromtimestamp(files[-1].stat().st_mtime), files[-1].name)
    print()
