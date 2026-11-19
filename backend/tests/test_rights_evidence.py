"""AUD-15 regression tests: rights & reuse evidence.

- Wikimedia Commons candidates carry the licence, licence URL and creator
  reported by the provider itself.
- YouTube assets record creator (channel) + retrieval timestamp, and the
  licence stays "unknown" — never upgraded.
- The dead helpers that stamped "royalty-free" on arbitrary URLs no
  longer do.
- The per-project rights manifest preserves all of it, per asset, with
  the sentences each asset illustrates.
"""

import shutil
import subprocess

import pytest

from services import licensed_media, media_pipeline
from services.asset_report import build_rights_manifest


def test_wikimedia_candidates_carry_real_licence(monkeypatch):
    def fake_search_wikimedia(query, limit=6):
        return [
            {
                "kind": "image",
                "source": "wikimedia",
                "source_url": "https://commons.wikimedia.org/wiki/File:Wall.jpg",
                "creator": "<a href='/wiki/User:Photo'>J. Photographer</a>",
                "license": "CC BY-SA 4.0",
                "license_url": "https://creativecommons.org/licenses/by-sa/4.0",
                "title": "File:Wall.jpg",
                "alt": "Crowds at the wall",
                "download_url": "https://upload.wikimedia.org/wall.jpg",
            }
        ]

    monkeypatch.setattr(licensed_media, "search_wikimedia", fake_search_wikimedia)
    results = media_pipeline.search_wikimedia_images("berlin wall", limit=5)
    assert len(results) == 1
    item = results[0]
    assert item["source"] == "wikimedia"
    assert item["license"] == "CC BY-SA 4.0"
    assert item["license_url"].endswith("/by-sa/4.0")
    assert item["creator"] == "J. Photographer"  # HTML stripped
    assert item["rights_status"] == "license_recorded"
    assert item["image_url"] == "https://upload.wikimedia.org/wall.jpg"


def test_dead_helpers_no_longer_fabricate_royalty_free():
    assert media_pipeline._web_image_hit("https://x.org/a.jpg", "t", "s")["license"] == "unknown"
    assert media_pipeline._youtube_video_hit("https://youtube.com/watch?v=1", "t", "s")["license"] == "unknown"


@pytest.fixture(scope="module")
def real_clip(tmp_path_factory):
    out = tmp_path_factory.mktemp("rights") / "real.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=15",
            "-c:v", "mpeg4", str(out),
        ],
        check=True,
        timeout=60,
    )
    return out


def test_video_asset_records_creator_and_retrieval_time(real_clip, tmp_path, monkeypatch):
    def fake_download(url, output_path, start_time=0, duration=8):
        shutil.copy(real_clip, output_path)
        return True

    monkeypatch.setattr(media_pipeline, "download_video_clip", fake_download)
    candidate = {
        "url": "https://example.com/direct.mp4",
        "title": "Archival footage",
        "creator": "National Archive Channel",
        "license": "unknown",
    }
    asset = media_pipeline._uve_download_video_candidate(
        candidate, tmp_path, sentence_number=1, asset_index=1
    )
    assert asset is not None
    assert asset["creator"] == "National Archive Channel"
    assert asset["retrieved_at"]
    assert asset["license"] == "unknown"  # never upgraded


def test_rights_manifest_preserves_evidence_per_asset():
    scenes = [
        {
            "videos": [
                {
                    "file_path": "/p/v1.mp4", "type": "video", "title": "Footage",
                    "source": "youtube", "video_url": "https://youtube.com/watch?v=1",
                    "creator": "Some Channel", "license": "unknown",
                    "rights_status": "verify_before_publish",
                    "retrieved_at": "2026-11-19T08:00:00+00:00",
                }
            ],
            "images": [
                {
                    "file_path": "/p/i1.jpg", "type": "image", "title": "File:Wall.jpg",
                    "source": "wikimedia",
                    "source_url": "https://commons.wikimedia.org/wiki/File:Wall.jpg",
                    "image_url": "https://upload.wikimedia.org/wall.jpg",
                    "creator": "J. Photographer", "license": "CC BY-SA 4.0",
                    "license_url": "https://creativecommons.org/licenses/by-sa/4.0",
                    "rights_status": "license_recorded",
                    "retrieved_at": "2026-11-19T08:01:00+00:00",
                }
            ],
            "sentences": [
                {
                    "sentence_number": 7,
                    "videos": [{"file_path": "/p/v1.mp4"}],
                    "images": [{"file_path": "/p/i1.jpg"}],
                }
            ],
        }
    ]
    manifest = build_rights_manifest(scenes)
    assert len(manifest) == 2
    by_path = {entry["file_path"]: entry for entry in manifest}

    video = by_path["/p/v1.mp4"]
    assert video["license"] == "unknown"
    assert video["creator"] == "Some Channel"
    assert video["content_url"] == "https://youtube.com/watch?v=1"
    assert video["sentence_numbers"] == [7]

    image = by_path["/p/i1.jpg"]
    assert image["license"] == "CC BY-SA 4.0"
    assert image["license_url"].endswith("/by-sa/4.0")
    assert image["creator"] == "J. Photographer"
    assert image["source_url"].startswith("https://commons.wikimedia.org")
    assert image["sentence_numbers"] == [7]
