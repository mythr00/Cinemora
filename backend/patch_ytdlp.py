from pathlib import Path

path = Path("services/media_pipeline.py")
text = path.read_text(encoding="utf-8")

if "import sys" not in text:
    text = text.replace(
        "import subprocess\n",
        "import subprocess\nimport sys\n",
        1,
    )

text = text.replace(
    '"yt-dlp",',
    'sys.executable,\n            "-m",\n            "yt_dlp",',
)

path.write_text(text, encoding="utf-8")

print("PATCH COMPLETE")
