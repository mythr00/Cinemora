import os
from pathlib import Path
from datetime import datetime

pid = "8a367fbe-9477-463d-a1b3-7ebf58116ff4"
base = Path("media") / pid

print("=== Do scene_5 / scene_6 source folders exist? ===")
for n in [1, 2, 3, 4, 5, 6]:
    scene_dir = base / f"scene_{n}"
    if scene_dir.exists():
        files = list(scene_dir.glob("*"))
        print(f"scene_{n}: EXISTS, {len(files)} files")
    else:
        print(f"scene_{n}: DOES NOT EXIST")

print()
print("=== Actual downloaded SOURCE assets only (scene_N folders, not clips/) ===")
downloads = []
for n in [1, 2, 3, 4, 5, 6]:
    scene_dir = base / f"scene_{n}"
    if scene_dir.exists():
        for p in scene_dir.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".mp4", ".webp"):
                downloads.append((p.stat().st_mtime, str(p)))

downloads.sort()
if downloads:
    print("First source download:", datetime.fromtimestamp(downloads[0][0]), downloads[0][1])
    print("Last source download: ", datetime.fromtimestamp(downloads[-1][0]), downloads[-1][1])
    print()
    print("Last 15 SOURCE downloads by time:")
    for mtime, path in downloads[-15:]:
        print(" ", datetime.fromtimestamp(mtime), path)
else:
    print("No source downloads found at all under scene_N folders.")
