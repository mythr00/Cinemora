#!/usr/bin/env python3
"""Stage 10 supplement — moving-footage proof over the real network.

Why this exists: the main E2E's YouTube search returned ZERO results from
this machine — YouTube soft-blocks search from this datacenter egress IP
(verified directly with yt-dlp: playlist_count 0), and upload.wikimedia.org
rate-limits the same IP (HTTP 429, Wikimedia SRE ref daab851). So neither
the YouTube branch nor a Commons video could be exercised live here.

This supplement proves the moving-footage path that CAN run: a real
archive video (Internet Archive — the founder's priority list includes
government & historical archives) flowing through the production
`_uve_download_video_candidate`: real download, real ffprobe probing,
real licence preservation, Stage 7 evidence stamping. The item's licence
(CC BY-NC-ND 3.0) is recorded exactly as the archive reports it — the
proof is about evidence fidelity, not clearance.

Exit code 0 = passed.
"""

import os
import sys
from pathlib import Path
from urllib.parse import quote

BACKEND = Path(__file__).resolve().parent.parent
OUT_DIR = Path.home() / "workspace" / "cinemora-audit" / "e2e-media" / "archive-video"
IDENTIFIER = "Aldeadle-60SECMichaelTheurerOnTheFallOfTheBerlinWall951-2"

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/cinemora-e2e.db")
os.environ.setdefault("BREWKEG_API_KEY", "e2e-no-key")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ["SOURCE_INTEL_DB"] = str(OUT_DIR / "source_intel.sqlite3")

sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)


def main():
    import requests

    from services.media_pipeline import _uve_download_video_candidate
    from services.media_probe import probe_video

    # 1. Real archive metadata: title, creator, licence (provider-reported).
    resp = requests.get(
        f"https://archive.org/metadata/{IDENTIFIER}",
        timeout=30,
        headers={"User-Agent": "CinemoraE2E/1.0"},
    )
    data = resp.json()
    metadata = data.get("metadata") or {}
    title = metadata.get("title", "")
    creator = metadata.get("creator") or ""
    licence_url = metadata.get("licenseurl") or ""
    licence = "CC BY-NC-ND 3.0" if "by-nc-nd/3.0" in licence_url else (licence_url or "unknown")

    video_file = None
    for entry in data.get("files") or []:
        name = entry.get("name", "")
        if name.endswith("_512kb.mp4"):
            video_file = name
            break
    if not video_file:
        print("FAIL: no suitable derivative file in archive item")
        return 1
    download_url = f"https://archive.org/download/{IDENTIFIER}/{quote(video_file)}"
    print(f"Archive video: {title} | licence: {licence} | file: {video_file}")

    # 2. Production-shaped candidate, as an archive search would emit it.
    candidate = {
        "url": download_url,
        "video_url": download_url,
        "title": title,
        "source": "archive.org",
        "source_url": f"https://archive.org/details/{IDENTIFIER}",
        "creator": creator,
        "license": licence,
        "license_url": licence_url,
        "rights_status": "license_recorded" if licence_url else "verify_before_publish",
        "snippet": "On the fall of the Berlin Wall",
    }

    # 3. The real production download+probe path.
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    asset = _uve_download_video_candidate(
        candidate, OUT_DIR, sentence_number=1, asset_index=1
    )

    failures = []

    def check(name, condition, detail=""):
        print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")
        if not condition:
            failures.append(name)

    check("asset downloaded", asset is not None)
    if asset is None:
        return 1
    check("asset probed (not fabricated)", asset.get("probed") is True)
    check(
        # The engine cuts a fixed-length clip from the source; the recorded
        # duration must be that clip's PROBED duration (the ffprobe-
        # agreement check below is what proves it is measured, not the
        # old fabricated constant — pre-fix code stamped 8s without ever
        # probing, and would equally have "agreed" with nothing).
        "real duration recorded",
        isinstance(asset.get("duration_seconds"), (int, float)) and asset["duration_seconds"] > 1,
        f"({asset.get('duration_seconds')}s)",
    )
    check(
        "real resolution recorded",
        bool(asset.get("width")) and bool(asset.get("height")),
        f"({asset.get('width')}x{asset.get('height')})",
    )
    check("licence preserved from provider", asset.get("license") == licence, f"({licence})")
    check("retrieval timestamp stamped", bool(asset.get("retrieved_at")))
    probe = probe_video(asset["file_path"])
    check(
        "independent ffprobe agrees",
        probe is not None and abs(probe["duration_seconds"] - asset["duration_seconds"]) < 0.6,
    )

    print(f"ARCHIVE VIDEO PROOF: {'PASSED' if not failures else 'FAILED ' + str(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
