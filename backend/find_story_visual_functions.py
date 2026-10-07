from pathlib import Path
import re

files = [
    Path("services/story.py"),
    Path("services/script_analyzer.py"),
    Path("services/media_pipeline.py"),
    Path("jobs.py"),
]

targets = [
    "plan_script",
    "_uve_scenarios_for",
    "_uve_scenario_search",
    "_uve_search_candidates",
    "_uve_verify_all",
    "_uve_pick",
    "compose_queries",
    "verify_candidate",
    "collect_video_candidates",
    "collect_image_candidates",
]

for path in files:
    print()
    print("=" * 110)
    print(f"FILE: {path}")
    print("=" * 110)

    if not path.exists():
        print("FILE NOT FOUND")
        continue

    source = path.read_text(encoding="utf-8-sig")
    lines = source.splitlines()

    for target in targets:
        matches = []

        for i, line in enumerate(lines, start=1):
            if target in line:
                matches.append((i, line))

        if not matches:
            continue

        print()
        print(f"### TARGET: {target}")
        print("-" * 110)

        for line_number, line in matches:
            print(f"{line_number:5}: {line}")

            start = max(1, line_number - 8)
            end = min(len(lines), line_number + 20)

            print("      CONTEXT:")
            for j in range(start, end + 1):
                print(f"{j:5}: {lines[j - 1]}")

            print()

print()
print("=" * 110)
print("SEARCH COMPLETE")
print("=" * 110)
