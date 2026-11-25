"""Lane 2 content-evidence integrations (GitHub Skills Programme).

Covers the three adopted pieces, all record-only:
* ffmpeg freezedetect/blackdetect intervals (content_analysis),
* PySceneDetect shot boundaries (shot_detection) + the timeline's
  shot-aware window snap,
* in-house perceptual hashing (perceptual_hash),
and their wiring into frame_verification.verify_asset_frames.

The fixture is a synthetic 12s clip with KNOWN structure, built by
ffmpeg at test time: testsrc 0-3s (moving), black 3-5s, smptebars
5-8s (static), testsrc 8-12s with a frozen frame inserted at 8-11s.
"""

import subprocess

import pytest
from PIL import Image, ImageDraw

from services import content_analysis, perceptual_hash, shot_detection
from services.frame_verification import verify_asset_frames
from services.timeline import _recorded_scenes, build_scene_timeline


@pytest.fixture(scope="module")
def fixture_clip(tmp_path_factory):
    out = tmp_path_factory.mktemp("lane2") / "fixture.mp4"
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
        "-f", "lavfi", "-i", "color=black:duration=2:size=320x240:rate=10",
        "-f", "lavfi", "-i", "smptebars=duration=3:size=320x240:rate=10",
        "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=10",
        "-filter_complex",
        "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0,"
        "loop=loop=30:size=1:start=80,fps=10",
        "-t", "12", str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


# --- content_analysis -------------------------------------------------

def test_content_analysis_finds_black_and_freeze(fixture_clip):
    record = content_analysis.analyse_video_content(fixture_clip)
    assert record["status"] == "evaluated"
    blacks = record["black_intervals"]
    assert any(abs(b["start"] - 3.0) < 0.3 and abs(b["end"] - 5.0) < 0.3
               for b in blacks), blacks
    freezes = record["freeze_intervals"]
    # The inserted frozen frame (8-11s) must appear inside some
    # freeze interval (static bars merge neighbouring intervals —
    # the assertion is containment, not exact equality).
    assert any(f["start"] <= 8.3 and f["end"] >= 10.7 for f in freezes), freezes


def test_content_analysis_missing_file_is_typed():
    record = content_analysis.analyse_video_content("/nonexistent/x.mp4")
    assert record["status"] == "not_evaluated"
    assert record["reason"] == "file_missing"


# --- shot_detection ---------------------------------------------------

def test_detect_shots_finds_true_cuts(fixture_clip):
    record = shot_detection.detect_shots(fixture_clip, duration_seconds=12)
    assert record["status"] == "evaluated"
    starts = [s["start"] for s in record["scenes"]]
    assert starts[0] == 0.0
    assert any(abs(s - 3.0) < 0.35 for s in starts), starts
    assert any(abs(s - 5.0) < 0.35 for s in starts), starts


def test_detect_shots_refuses_overlong_clips_without_running(fixture_clip):
    record = shot_detection.detect_shots(
        fixture_clip, duration_seconds=shot_detection.MAX_DURATION_SECONDS + 1
    )
    assert record["status"] == "not_evaluated"
    assert record["reason"] == "clip_too_long_for_shot_detection"


def test_snap_window_inside_shot():
    scenes = [
        {"start": 0.0, "end": 3.0, "duration": 3.0},
        {"start": 3.0, "end": 9.0, "duration": 6.0},
        {"start": 9.0, "end": 10.0, "duration": 1.0},
    ]
    snap = shot_detection.snap_window_inside_shot
    # Already inside one shot: untouched.
    assert snap(scenes, 3.5, 4.0) == (3.5, 4.0, False)
    # Straddling into a shot that can hold the window: shift.
    assert snap(scenes, 2.0, 4.0) == (3.0, 4.0, True)
    # Straddling into a shot too short to hold it: clamp.
    start, duration, adjusted = snap(scenes, 8.0, 4.0)
    assert adjusted and start == 8.0 and duration == 1.0
    # No scenes: the pre-shot behaviour, exactly.
    assert snap([], 2.0, 4.0) == (2.0, 4.0, False)


# --- perceptual_hash --------------------------------------------------

def _image(draw_fn, size=(64, 64)):
    img = Image.new("RGB", size, "black")
    draw_fn(ImageDraw.Draw(img))
    return img


def test_hashes_distance_zero_for_identical_and_large_for_different():
    a = _image(lambda d: d.rectangle([8, 8, 40, 40], fill="white"))
    b = _image(lambda d: d.rectangle([8, 8, 40, 40], fill="white"))
    c = _image(lambda d: d.ellipse([20, 20, 60, 60], fill="white"))
    assert perceptual_hash.hamming(
        perceptual_hash.dhash(a), perceptual_hash.dhash(b)) == 0
    assert perceptual_hash.hamming(
        perceptual_hash.phash(a), perceptual_hash.phash(b)) == 0
    assert perceptual_hash.hamming(
        perceptual_hash.dhash(a), perceptual_hash.dhash(c)) > 8


def test_frame_hashes_summary():
    frames = [
        _image(lambda d: d.rectangle([8, 8, 40, 40], fill="white")),
        _image(lambda d: d.rectangle([8, 8, 40, 40], fill="white")),
        _image(lambda d: d.ellipse([20, 20, 60, 60], fill="white")),
    ]
    summary = perceptual_hash.frame_hashes(frames)
    assert summary["count"] == 3
    assert summary["min_pairwise_dhash"] == 0
    assert summary["max_pairwise_dhash"] > 8


# --- wiring: verify_asset_frames --------------------------------------

def test_verify_asset_frames_records_content_evidence(fixture_clip):
    asset = {
        "file_path": str(fixture_clip), "path": str(fixture_clip),
        "type": "video", "duration_seconds": 12,
    }
    record = verify_asset_frames(asset)
    assert record["content_analysis"]["status"] == "evaluated"
    assert record["content_analysis"]["black_intervals"]
    assert record["shots"]["status"] == "evaluated"
    assert record["shots"]["scene_count"] >= 3
    assert record["frame_hashes"]["count"] >= 1
    # Record-only: the content evidence never invents a verdict —
    # the outcome is whatever the frame detectors decided.
    assert record["outcome"] in ("pass", "reject", "not_evaluated")


# --- wiring: timeline shot snap ---------------------------------------

def test_recorded_scenes_extraction():
    visual = {
        "frame_check": {
            "shots": {"status": "evaluated",
                      "scenes": [{"start": 0.0, "end": 3.0, "duration": 3.0}]},
        },
    }
    assert _recorded_scenes(visual)[0]["end"] == 3.0
    assert _recorded_scenes({"frame_check": {"shots": {"status": "not_evaluated"}}}) == []
    assert _recorded_scenes({}) == []


def test_timeline_window_snaps_inside_shot(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00" * 2048)
    visual = {
        "file_path": str(path), "path": str(path), "type": "video",
        "title": "clip.mp4", "verification": {"accepted": True},
        "clip_start": 2.0, "clip_duration": 12, "duration_seconds": 12,
        "frame_check": {
            "shots": {
                "status": "evaluated",
                "scenes": [
                    {"start": 0.0, "end": 3.0, "duration": 3.0},
                    {"start": 3.0, "end": 9.0, "duration": 6.0},
                    {"start": 9.0, "end": 12.0, "duration": 3.0},
                ],
            },
        },
    }
    scene_media = {
        "scene_number": 1, "duration_seconds": 5,
        "videos": [visual], "images": [],
        "sentences": [{"sentence_number": 1, "videos": [visual], "images": []}],
    }
    timings = [{
        "sentence_index": 0, "text": "Sentence 0.",
        "start": 0.0, "end": 5.0,
    }]
    result = build_scene_timeline(scene_media, timings)
    item = result["items"][0]
    # The raw window 2.0-7.0 straddles the 3s cut; the snap shifts
    # it into the following shot, preserving the 5s window length.
    assert item["clip_start"] == 3.0
    assert item["clip_end"] == 8.0
    assert item.get("shot_snapped") is True
