from pathlib import Path

path = Path(r".\services\media_pipeline.py")

text = path.read_text(encoding="utf-8")

start_marker = "        print(\n            f\"SENTENCE {sentence_number}: \""

start = text.find(start_marker)

if start == -1:
    raise SystemExit("DAMAGED SENTENCE PRINT BLOCK NOT FOUND")

end = text.find(
    "\n\n\n    # --------------------------------------------------------",
    start,
)

if end == -1:
    raise SystemExit("END OF SENTENCE BLOCK NOT FOUND")

replacement = """        print(
            f"SENTENCE {sentence_number}: "
            f"{len(sentence_images)} assigned image(s) "
            f"— "
            f"{sentence_images[0].get('asset_scope', 'none') if sentence_images else 'none'}"
        )"""

text = text[:start] + replacement + text[end:]

path.write_text(text, encoding="utf-8")

print("MEDIA PIPELINE PRINT BLOCK REPAIRED")