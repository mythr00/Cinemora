from services.timeline import build_project_timeline

sentences = [
    "Apollo 11 approached the Moon during its historic mission.",
    "The astronauts prepared for the lunar landing.",
    "The Eagle eventually landed on the Moon.",
]

processed = []

for i, sentence in enumerate(sentences, start=1):
    processed.append({
        "scene_number": i,
        "scene": {
            "scene_number": i,
        },
        "sentences": [
            {
                "sentence_number": i,
                "sentence_text": sentence,
            }
        ],
        "videos": [],
        "images": [],
    })

result = build_project_timeline(
    "youtube-test",
    processed,
    output_dir=r".\media",
)

print()
print("=" * 60)
print("TIMELINE FOCUS TEST")
print("=" * 60)
print("SCRIPT SENTENCES:", result["alignment"].get("script_sentences"))
print("ALIGNED SENTENCES:", result["alignment"].get("aligned_sentences"))
print("NARRATION:", result.get("duration"))
print("TIMELINE:", result.get("timeline_file"))
