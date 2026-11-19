"""AUD-17 regression tests: genuine media verification.

Real media files are generated with ffmpeg so probing is verified against
actual content, not mocks. The key regression: a corrupt file larger than
10,000 bytes used to PASS validate_video (size-only check) and silently
become a placeholder clip at render; it must now be rejected. Assets must
also record their REAL probed duration — never the fabricated 8s constant.
"""

import os
import subprocess

import pytest
from PIL import Image

from services import media_pipeline
from services.media_probe import probe_image, probe_video


def _ffmpeg(args, out):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *args, str(out)],
        check=True,
        timeout=60,
    )
    return out


@pytest.fixture(scope="module")
def real_clip(tmp_path_factory):
    out = tmp_path_factory.mktemp("media") / "real.mp4"
    return _ffmpeg(
        [
            "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-shortest", "-c:v", "mpeg4", "-c:a", "aac",
        ],
        out,
    )


@pytest.fixture(scope="module")
def audio_only(tmp_path_factory):
    out = tmp_path_factory.mktemp("media") / "audio.m4a"
    return _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "aac"], out)


@pytest.fixture()
def corrupt_file(tmp_path):
    out = tmp_path / "corrupt.mp4"
    out.write_bytes(os.urandom(20_000))  # > the old 10,000-byte threshold
    return out


def test_probe_video_reports_real_facts(real_clip):
    probe = probe_video(real_clip)
    assert probe is not None
    assert 2.9 <= probe["duration_seconds"] <= 3.1
    assert probe["width"] == 320
    assert probe["height"] == 240
    assert probe["fps"] == pytest.approx(15, abs=0.5)
    assert probe["has_audio"] is True


def test_probe_video_rejects_corrupt_file(corrupt_file):
    assert probe_video(corrupt_file) is None


def test_probe_video_rejects_audio_only(audio_only):
    assert probe_video(audio_only) is None


def test_probe_video_rejects_missing_and_text(tmp_path):
    assert probe_video(tmp_path / "nope.mp4") is None
    text = tmp_path / "notavideo.mp4"
    text.write_text("this is not a video")
    assert probe_video(text) is None


def test_validate_video_uses_real_streams(real_clip, corrupt_file, audio_only):
    assert media_pipeline.validate_video(real_clip) is True
    # Regression: the corrupt file is 20,000 bytes — the old size-only
    # check returned True here.
    assert media_pipeline.validate_video(corrupt_file) is False
    assert media_pipeline.validate_video(audio_only) is False


def test_probe_image_reports_dimensions(tmp_path):
    img_path = tmp_path / "still.png"
    Image.new("RGB", (111, 77), color=(10, 20, 30)).save(img_path)
    probe = probe_image(img_path)
    assert probe == {"width": 111, "height": 77, "format": "PNG"}
    garbage = tmp_path / "garbage.png"
    garbage.write_bytes(os.urandom(5_000))
    assert probe_image(garbage) is None


def test_downloaded_asset_records_probed_duration(real_clip, tmp_path, monkeypatch):
    """_uve_download_video_candidate must stamp the file's real duration,
    not DEFAULT_CLIP_DURATION (8)."""

    def fake_download(url, output_path, start_time=0, duration=8):
        import shutil

        shutil.copy(real_clip, output_path)
        return True

    monkeypatch.setattr(media_pipeline, "download_video_clip", fake_download)
    candidate = {"url": "https://example.com/direct-video.mp4", "title": "fixture"}
    asset = media_pipeline._uve_download_video_candidate(
        candidate, tmp_path, sentence_number=1, asset_index=1
    )
    assert asset is not None
    assert 2.9 <= asset["duration_seconds"] <= 3.1
    assert asset["duration_seconds"] != media_pipeline.DEFAULT_CLIP_DURATION
    assert asset["width"] == 320 and asset["height"] == 240
    assert asset["probed"] is True


def test_downloaded_corrupt_asset_is_rejected(corrupt_file, tmp_path, monkeypatch):
    def fake_download(url, output_path, start_time=0, duration=8):
        import shutil

        shutil.copy(corrupt_file, output_path)
        return True

    monkeypatch.setattr(media_pipeline, "download_video_clip", fake_download)
    candidate = {"url": "https://example.com/direct-video.mp4", "title": "fixture"}
    asset = media_pipeline._uve_download_video_candidate(
        candidate, tmp_path, sentence_number=1, asset_index=1
    )
    assert asset is None
    # The invalid download must not linger as a timeline candidate.
    # (The corrupt_file fixture itself also lives in tmp_path, so match the
    # pipeline's own output naming.)
    assert list(tmp_path.glob("sentence_*.mp4")) == []
