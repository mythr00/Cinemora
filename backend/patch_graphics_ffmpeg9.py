from pathlib import Path

p = Path("services/graphics_pipeline.py")
text = p.read_text(encoding="utf-8")

old = '"-filter_complex_script",'
new = '"-/filter_complex",'

if old not in text:
    raise SystemExit("TARGET NOT FOUND - no changes made.")

text = text.replace(old, new, 1)

p.write_text(text, encoding="utf-8")

print("GRAPHICS FFMPEG 9 PATCHED")
