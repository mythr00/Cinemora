import os
import time

from services.media_pipeline import (
    get_sentence_queries,
    collect_video_candidates,
    download_video_clip,
)

sentence = {
    "sentence_number": 1,
    "text": "The mountain began to collapse after heavy rainfall triggered a massive landslide.",
    "queries": [
        "mountain landslide collapse heavy rainfall",
        "massive landslide mountain collapse",
        "landslide after heavy rain",
    ],
    "entities": ["mountain", "landslide"],
    "actions": ["collapse"],
    "event": "landslide",
}

print("=" * 70)
print("VIDEO TIMING TEST")
print("=" * 70)

# ------------------------------------------------------------
# SEARCH
# ------------------------------------------------------------

start = time.time()

candidates = collect_video_candidates(
    sentence,
    sentence["queries"],
)

search_time = time.time() - start

print()
print(f"SEARCH TIME: {search_time:.2f} seconds")
print(f"CANDIDATES: {len(candidates)}")

if not candidates:
    print("No candidates.")
    raise SystemExit

best = candidates[0]

url = (
    best.get("video_url")
    or best.get("url")
    or best.get("link")
    or ""
).strip()

title = best.get("title", "")

print(f"BEST TITLE: {title}")
print(f"BEST URL: {url}")

# ------------------------------------------------------------
# DOWNLOAD
# ------------------------------------------------------------

output_dir = os.path.join(
    "media",
    "_speed_test",
)

os.makedirs(output_dir, exist_ok=True)

output_path = os.path.join(
    output_dir,
    "timing_test.mp4",
)

start = time.time()

success = download_video_clip(
    url,
    output_path,
    0,
    8,
)

download_time = time.time() - start

print()
print(f"DOWNLOAD TIME: {download_time:.2f} seconds")
print(f"DOWNLOAD SUCCESS: {success}")

# ------------------------------------------------------------
# TOTAL
# ------------------------------------------------------------

print()
print("=" * 70)
print("TIMING RESULT")
print("=" * 70)

print(f"Search:   {search_time:.2f}s")
print(f"Download: {download_time:.2f}s")
print(f"Total:    {search_time + download_time:.2f}s")