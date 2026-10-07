import os
import time
from services.media_pipeline import process_sentence_videos

sentence = {
    "sentence_number": 1,
    "text": "The mountain began to collapse after heavy rainfall triggered a massive landslide.",
    "queries": [
        "mountain landslide collapse heavy rainfall",
        "massive landslide mountain collapse",
        "landslide after heavy rain"
    ],
    "entities": ["mountain", "landslide"],
    "actions": ["collapse"],
    "event": "landslide"
}

test_dir = os.path.join("media", "_speed_test")

os.makedirs(test_dir, exist_ok=True)

start = time.time()

result = process_sentence_videos(
    sentence,
    test_dir,
    video_count=1,
)

elapsed = time.time() - start

print()
print("=" * 70)
print("SPEED TEST COMPLETE")
print("=" * 70)
print(f"Elapsed: {elapsed:.2f} seconds")
print(f"Videos selected: {len(result)}")

for item in result:
    print(f"Title: {item.get('title', '')}")
    print(f"Path: {item.get('path', '')}")
    print(f"Transcript score: {item.get('transcript_score', 'N/A')}")
