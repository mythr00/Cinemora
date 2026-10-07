import os
from pathlib import Path
from datetime import datetime

pid = "8a367fbe-9477-463d-a1b3-7ebf58116ff4"
base = Path("media") / pid

files = []
for p in base.rglob("*"):
    if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".mp4", ".webp"):
        files.append((p.stat().st_mtime, str(p)))

files.sort()
print("First download:", datetime.fromtimestamp(files[0][0]), files[0][1])
print("Last download: ", datetime.fromtimestamp(files[-1][0]), files[-1][1])
print()
print("Last 10 downloads by time:")
for mtime, path in files[-10:]:
    print(" ", datetime.fromtimestamp(mtime), path)
