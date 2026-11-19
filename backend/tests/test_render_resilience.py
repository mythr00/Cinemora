"""AUD-22 / AUD-24 regression tests: render resilience + dead stubs.

- probe_media used to ESTIMATE frame counts (duration x nominal FPS)
  when nb_frames was absent, and audit_clip compared that estimate
  exactly — a wrong estimate aborted whole renders with FRAME LOCK
  FAILURE. audit_clip now recounts exactly before declaring a mismatch.
- The per-sentence media stubs silently returned empty results; they now
  raise NotImplementedError pointing at the real engine.
"""

import subprocess

import pytest

from services import media_pipeline, renderer


@pytest.fixture(scope="module")
def clip_60_frames(tmp_path_factory):
    out = tmp_path_factory.mktemp("frames") / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=1920x1080:rate=30",
            "-c:v", "mpeg4", str(out),
        ],
        check=True,
        timeout=60,
    )
    return out


def test_count_frames_exact(clip_60_frames):
    assert renderer.count_frames_exact(clip_60_frames) == 60


def test_audit_clip_recounts_when_estimate_disagrees(clip_60_frames, monkeypatch):
    real_probe = renderer.probe_media(clip_60_frames)
    assert real_probe is not None

    def fake_probe(path):
        info = dict(real_probe)
        info["frame_count"] = 55  # the (wrong) estimate
        info["frame_count_estimated"] = True
        return info

    monkeypatch.setattr(renderer, "probe_media", fake_probe)
    result = renderer.audit_clip(
        clip_60_frames, expected_frames=60, expected_duration=2.0,
        scene_number=1, item_index=1,
    )
    assert result["status"] == "ok"
    assert result["actual_frames"] == 60
    assert result["frame_count_source"] == "counted_exact"


def test_audit_clip_still_fails_on_genuine_mismatch(clip_60_frames, monkeypatch):
    real_probe = renderer.probe_media(clip_60_frames)

    def fake_probe(path):
        info = dict(real_probe)
        info["frame_count"] = 55
        info["frame_count_estimated"] = True
        return info

    monkeypatch.setattr(renderer, "probe_media", fake_probe)
    result = renderer.audit_clip(
        clip_60_frames, expected_frames=61, expected_duration=2.0,
        scene_number=1, item_index=1,
    )
    assert result["status"] == "mismatch"
    assert result["actual_frames"] == 60  # recounted truth, still != 61


def test_sentence_stubs_raise_instead_of_returning_empty():
    with pytest.raises(NotImplementedError):
        media_pipeline.process_sentence_videos({"text": "x"}, "/tmp")
    with pytest.raises(NotImplementedError):
        media_pipeline.process_sentence_images({"text": "x"}, "/tmp")
    with pytest.raises(NotImplementedError):
        media_pipeline.process_sentence_media({"text": "x"}, "/tmp", 1)
