from pathlib import Path

path = Path("services/media_pipeline.py")
text = path.read_text(encoding="utf-8")

old = '''    output_path = Path(output_path)

    try:
        if output_path.exists():
'''

new = '''    output_path = Path(output_path).resolve()

    try:
        if output_path.exists():
'''

if old not in text:
    raise SystemExit("ERROR: output_path block not found.")

text = text.replace(old, new, 1)

old2 = '''    output_template = str(
        output_path.with_name(
            output_path.stem + "_source.%(ext)s"
        )
    )
'''

new2 = '''    output_template = str(
        output_path.with_name(
            output_path.stem + "_source.%(ext)s"
        )
    ).replace("\\\\", "/")
'''

if old2 not in text:
    raise SystemExit("ERROR: output_template block not found.")

text = text.replace(old2, new2, 1)

path.write_text(text, encoding="utf-8")

print("ABSOLUTE PATH PATCH COMPLETE")
