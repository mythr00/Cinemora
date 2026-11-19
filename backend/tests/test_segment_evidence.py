"""MS-3 regression tests: provider-neutral segment evidence.

Rules under test (founder directive):
- A long video with no transcript/chapter/description evidence is
  REJECTED — timestamp zero is never silently assumed.
- A short source (<= 2x the clip length) is a whole asset: start 0 is
  evidence-backed, and the asset records segment_evidence.
- download_direct_video honours start_time (it previously ignored it
  and always trimmed from second 0).

All media is generated locally and served over a loopback HTTP server —
real downloads, real probes, no external network.
"""

import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from services import media_pipeline


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture()
def http_root(tmp_path):
    handler = partial(_QuietHandler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield tmp_path, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _make_video(path, duration, color="green"):
    # Temporal noise keeps flat-colour video from compressing below the
    # downloader's 10 KB sanity floor while staying colour-dominant.
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c={color}:size=320x240:duration={duration}:rate=10",
            "-vf", "noise=alls=30:allf=t",
            "-c:v", "mpeg4", "-q:v", "5", str(path),
        ],
        check=True,
        timeout=60,
    )


def test_long_direct_video_without_evidence_is_rejected(http_root, tmp_path):
    root, base = http_root
    _make_video(root / "long.mp4", 30)
    candidate = {
        "title": "Long briefing",
        "video_url": f"{base}/long.mp4",
        "url": f"{base}/long.mp4",
        "source": "example.gov",
        "provider": "example.gov",
    }
    result = media_pipeline._uve_download_video_candidate(
        candidate, str(tmp_path / "scene"), 1, 1
    )
    assert result is None
    assert candidate["_download_error"] == "no_segment_evidence"


def test_short_direct_video_is_whole_asset(http_root, tmp_path):
    root, base = http_root
    _make_video(root / "short.mp4", 6)
    candidate = {
        "title": "Short clip",
        "video_url": f"{base}/short.mp4",
        "url": f"{base}/short.mp4",
        "source": "example.gov",
        "provider": "example.gov",
    }
    result = media_pipeline._uve_download_video_candidate(
        candidate, str(tmp_path / "scene"), 1, 1
    )
    assert result is not None
    assert result["segment_evidence"] == "whole_asset"
    assert result["clip_start"] == 0
    assert result["duration_seconds"] == pytest.approx(6, abs=0.6)


def test_direct_download_honours_start_time(http_root, tmp_path):
    root, base = http_root
    _make_video(root / "red.mp4", 2, color="red")
    _make_video(root / "blue.mp4", 2, color="blue")
    concat = root / "list.txt"
    concat.write_text("file 'red.mp4'\nfile 'blue.mp4'\n")
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
            "-i", str(concat), "-c", "copy", str(root / "twotone.mp4"),
        ],
        check=True,
        timeout=60,
    )

    out = tmp_path / "clip.mp4"
    ok = media_pipeline.download_direct_video(
        f"{base}/twotone.mp4", out, duration=1, start_time=3
    )
    assert ok

    frame = tmp_path / "frame.png"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", "0.5", "-i", str(out),
         "-frames:v", "1", str(frame)],
        check=True,
        timeout=60,
    )
    image = Image.open(frame).convert("RGB")
    pixels = list(image.getdata())
    mean = tuple(sum(px[i] for px in pixels) / len(pixels) for i in range(3))
    # Second half of the source is blue: the clip taken at t=3 must be
    # blue-dominant (before the fix it was always the red first half).
    assert mean[2] > mean[0] + 40
