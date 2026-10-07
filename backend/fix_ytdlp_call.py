from pathlib import Path

path = Path("services/media_pipeline.py")
text = path.read_text(encoding="utf-8")

old = '''    returncode, stdout, stderr = run_command_safe(
        command,
        YT_DLP_TIMEOUT,
        sys.executable,
            "-m",
            "yt_dlp",
    )
'''

new = '''    returncode, stdout, stderr = run_command_safe(
        command,
        YT_DLP_TIMEOUT,
        "yt-dlp",
    )
'''

if old not in text:
    raise SystemExit("ERROR: Expected broken block was not found.")

text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")

print("FIX COMPLETE")
