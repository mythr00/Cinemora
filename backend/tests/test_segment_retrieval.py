"""Segment-only retrieval (founder directive): download only the
evidenced interval, never the whole long video — and prove it.

- Direct media URLs: ffmpeg input seeking issues HTTP Range
  requests; the bytes served are a fraction of the full file.
- Page URLs (yt-dlp): the command carries --download-sections with
  the exact evidenced interval.
- Provenance (original vs extracted duration, method, partial flag)
  is stamped onto accepted assets and surfaced in the asset report.
  A full download is reported as NOT partial — never dressed up.
- YouTube is an equal supplementary provider: enabled by default,
  returned with the same shape/honesty as every other provider.
"""

import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from services import media_pipeline, search_provider
from services.asset_report import build_asset_report


def _make_video(path, seconds=30):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            # Noise makes the encode near-incompressible, so a 30s
            # source is ~16MB — larger than ffmpeg's ~5MB probe read,
            # which is what makes the byte savings of range retrieval
            # measurable (on small files probing alone eats the file).
            "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=10:duration={seconds},noise=alls=70:allf=t",
            "-f", "lavfi", "-i", f"anoisesrc=duration={seconds}",
            "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "28", "-pix_fmt", "yuv420p",
            # Frequent keyframes like real-world encodes (archive /
            # broadcaster MP4s): without them a seek to 20s must decode
            # from the only earlier keyframe and byte savings vanish.
            "-g", "20", "-keyint_min", "20", "-sc_threshold", "0",
            "-movflags", "+faststart", "-shortest", str(path),
        ],
        check=True,
    )
    return Path(path)


class _RangeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    payload = b""
    bytes_served = 0
    saw_range = False
    saw_deep_range = False

    def _write_counted(self, body):
        # Write in small chunks so a client that closes early (the
        # whole point of range retrieval) is measured by what it
        # actually consumed, not by what a fast loopback kernel
        # buffer could absorb.
        view = memoryview(body)
        for offset in range(0, len(view), 8192):
            chunk = view[offset:offset + 8192]
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            type(self).bytes_served += len(chunk)

    def do_GET(self):
        data = type(self).payload
        range_header = self.headers.get("Range")
        if range_header:
            type(self).saw_range = True
            match = re.match(r"bytes=(\d+)-(\d*)", range_header)
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else len(data) - 1
            end = min(end, len(data) - 1)
            if start > len(data) * 0.4:
                type(self).saw_deep_range = True
            body = data[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self._write_counted(body)
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self._write_counted(data)

    def log_message(self, *args):
        pass


@pytest.fixture()
def range_server(tmp_path):
    video = _make_video(tmp_path / "long_source.mp4", seconds=30)
    _RangeHandler.payload = video.read_bytes()
    _RangeHandler.bytes_served = 0
    _RangeHandler.saw_range = False
    _RangeHandler.saw_deep_range = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/long_source.mp4", video.stat().st_size
    server.shutdown()


def test_direct_download_retrieves_only_the_segment(range_server, tmp_path):
    url, full_size = range_server
    out = tmp_path / "clip.mp4"
    ok = media_pipeline.download_direct_video(url, out, duration=4, start_time=20)
    assert ok is True
    # The client sought INTO the file (a range request starting deep
    # in the byte stream, near the 20s segment) and consumed only a
    # fraction of the file — not a download-then-trim.
    assert _RangeHandler.saw_range
    assert _RangeHandler.saw_deep_range
    assert _RangeHandler.bytes_served < full_size * 0.6
    # The extracted file is real and ~4 seconds (probed, not claimed).
    probe = media_pipeline.probe_video(out)
    assert probe is not None
    assert 3.0 <= probe["duration_seconds"] <= 5.5
    # Provenance records a genuine partial retrieval.
    provenance = media_pipeline.DOWNLOAD_PROVENANCE[str(out)]
    assert provenance == {"method": "ffmpeg-range-seek", "partial": True}


def test_ytdlp_download_uses_download_sections(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, timeout, label):
        captured["command"] = command
        return 1, "", "simulated failure"

    monkeypatch.setattr(media_pipeline, "run_command_safe", fake_run)
    out = tmp_path / "clip.mp4"
    ok = media_pipeline.download_video_clip(
        "https://www.youtube.com/watch?v=abc123", out, start_time=20, duration=4
    )
    assert ok is False  # simulated failure — but the command was right
    command = captured["command"]
    assert "--download-sections" in command
    idx = command.index("--download-sections")
    assert command[idx + 1] == "*20-24.0"


def test_provenance_stamped_on_accepted_asset(monkeypatch, tmp_path):
    clip = _make_video(tmp_path / "real.mp4", seconds=4)
    url = "https://commons.wikimedia.org/wiki/File:Berlin_Wall.webm"
    media_pipeline._SOURCE_DURATION_CACHE[url] = 1800.0

    def fake_download(candidate_url, output_path, start_time=0, duration=8):
        Path(output_path).write_bytes(clip.read_bytes())
        media_pipeline.DOWNLOAD_PROVENANCE[str(output_path)] = {
            "method": "yt-dlp-download-sections",
            "partial": True,
        }
        return True

    monkeypatch.setattr(
        media_pipeline, "_segment_start_detailed",
        lambda u, **kw: (600, "verified_segment"),
    )
    monkeypatch.setattr(media_pipeline, "download_video_clip", fake_download)

    candidate = {
        "url": url,
        "title": "Berlin Wall segment",
        "license": "unknown",
        "matched_query": "berlin wall",
        "shot_need": {"required_subject": "Berlin Wall", "place": "Berlin"},
    }
    asset = media_pipeline._uve_download_video_candidate(
        candidate, str(tmp_path), sentence_number=3, asset_index=1
    )
    assert asset is not None
    assert asset["source_duration_seconds"] == 1800.0
    assert asset["clip_start"] == 600
    assert asset["segment_evidence"] == "verified_segment"
    assert asset["download_method"] == "yt-dlp-download-sections"
    assert asset["partial_download"] is True
    assert 3.0 <= asset["duration_seconds"] <= 5.5  # extracted, probed


def test_report_surfaces_segment_retrieval():
    scene = {
        "scene_number": 1,
        "videos": [
            {
                "type": "video",
                "file_path": "/x/clip.mp4",
                "provider": "youtube",
                "url": "https://www.youtube.com/watch?v=abc123",
                "duration_seconds": 8.0,
                "source_duration_seconds": 1800.0,
                "clip_start": 600,
                "segment_evidence": "verified_segment",
                "download_method": "yt-dlp-download-sections",
                "partial_download": True,
                "verification_status": "verified",
                "license": "unknown",
            }
        ],
        "images": [],
        "sentences": [],
        "asset_stats": {},
    }
    report = build_asset_report([scene])
    assert len(report["segment_retrieval"]) == 1
    entry = report["segment_retrieval"][0]
    assert entry["source_duration_seconds"] == 1800.0
    assert entry["extracted_duration_seconds"] == 8.0
    assert entry["segment_start_seconds"] == 600
    assert entry["partial_download"] is True


def test_youtube_enabled_by_default_and_equal(monkeypatch):
    monkeypatch.delenv("CINEMORA_DISABLED_PROVIDERS", raising=False)
    assert "youtube" not in media_pipeline._disabled_providers()

    def mixed(query, limit=15):
        return [
            {"title": "YT hit", "video_url": "https://www.youtube.com/watch?v=abc123",
             "url": "https://www.youtube.com/watch?v=abc123", "snippet": "Berlin Wall",
             "source": "Channel", "date": "", "duration": ""},
            {"title": "Vimeo hit", "video_url": "https://vimeo.com/12345",
             "url": "https://vimeo.com/12345", "snippet": "Berlin Wall",
             "source": "DocFilms", "date": "", "duration": ""},
        ]

    monkeypatch.setattr(search_provider, "search_videos", mixed)
    results = media_pipeline.search_videos_multi("berlin wall", limit=8)
    providers = {item["provider"] for item in results}
    assert providers == {"youtube", "vimeo"}
    # Same honesty for YouTube as for anyone: licence unknown.
    assert all(item["license"] == "unknown" for item in results)
