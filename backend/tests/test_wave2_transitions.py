"""F9 completion (Wave 2) — split-screen / parallax-depth /
film-strip transitions and the match-cut rule.

Segment kinds follow the Wave 1 contract: exact 2W frame counts
on real clips, verified pixel behaviour, and a renderer-level
frame-count equality check against the all-cut film. The
match-cut rule is planning only: it never honours a segment,
consumes zero frames, and applies only when BOTH adjacent items
record the same continuity basis + key with reason "continuity".
"""

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from services import renderer, transitions

RED = (200, 30, 30)
BLUE = (30, 30, 200)
W = 6


def _solid_png(path, colour):
    path = Path(path).with_suffix(".bmp")
    Image.new("RGB", (320, 180), colour).save(path)
    return str(path)


def _make_clip(image_path, out_path, frames=30):
    assert renderer.create_image_clip(
        image_path, frames / renderer.FPS, out_path, frame_count=frames
    )
    assert renderer.count_frames_exact(out_path) == frames
    return str(out_path)


@pytest.fixture(scope="module")
def colour_clips(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("wave2_transition_clips")
    red_png = _solid_png(workdir / "red.png", RED)
    blue_png = _solid_png(workdir / "blue.png", BLUE)
    clip_a = _make_clip(red_png, workdir / "a.mp4")
    clip_b = _make_clip(blue_png, workdir / "b.mp4")
    return clip_a, clip_b


def _frame_pixel(video_path, frame_index, xy=(960, 540)):
    out = Path(str(video_path) + f".frame{frame_index}.png")
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(video_path),
            "-vf", f"select='eq(n\\,{frame_index})'",
            "-frames:v", "1", str(out),
        ],
        check=True,
    )
    return Image.open(out).convert("RGB").getpixel(xy)


def _segment(colour_clips, kind, tmp_path, window=W):
    clip_a, clip_b = colour_clips
    result = transitions.build_transition_segment(
        clip_a, clip_b, kind, window, tmp_path / f"{kind}.mp4"
    )
    assert result is not None, f"{kind} segment failed to build"
    return result


def _close(pixel, colour, tol=45):
    return all(abs(pixel[i] - colour[i]) <= tol for i in range(3))


# ---------------------------------------------------------------------------
# plan_transitions — the new kinds + the match-cut rule
# ---------------------------------------------------------------------------

def test_plan_new_kinds_honoured_with_default_windows():
    items = [
        {},
        {"transition": "split_screen",
         "transition_reason": "scene_boundary"},
        {"transition": "parallax_depth",
         "transition_reason": "time_passage"},
        {"transition": "film_strip",
         "transition_reason": "continuity"},
    ]
    plan = transitions.plan_transitions(items)
    assert [r["transition"] for r in plan] == [
        "split_screen", "parallax_depth", "film_strip"]
    assert all(r["honoured"] for r in plan)
    assert [r["window_frames"] for r in plan] == [12, 12, 16]
    assert all(r["match_cut"] is None for r in plan)


def test_plan_new_kind_without_reason_downgraded():
    items = [{}, {"transition": "film_strip"}]
    record = transitions.plan_transitions(items)[0]
    assert record["transition"] == "cut"
    assert record["downgraded"] is True
    assert record["downgrade_cause"] == (
        "missing_or_invalid_transition_reason")


def test_match_cut_applies_on_recorded_continuity():
    items = [
        {"transition": "cut",
         "match_cut": {"basis": "subject", "key": "the white van"}},
        {"transition": "match_cut", "transition_reason": "continuity",
         "match_cut": {"basis": "subject", "key": "the white van"}},
    ]
    record = transitions.plan_transitions(items)[0]
    # A rule, not an effect: the edit stays a cut, no segment is
    # honoured, no frames are consumed — the evidence is recorded.
    assert record["transition"] == "cut"
    assert record["honoured"] is False
    assert record["downgraded"] is False
    assert record["window_frames"] == 0
    assert record["match_cut"] == {
        "basis": "subject", "key": "the white van"}


def test_match_cut_downgraded_when_continuity_unrecorded():
    items = [
        {"transition": "cut"},
        {"transition": "match_cut", "transition_reason": "continuity",
         "match_cut": {"basis": "subject", "key": "the white van"}},
    ]
    record = transitions.plan_transitions(items)[0]
    assert record["transition"] == "cut"
    assert record["downgraded"] is True
    assert record["downgrade_cause"] == "match_cut_continuity_unrecorded"
    assert record["match_cut"] is None


def test_match_cut_downgraded_on_key_mismatch():
    items = [
        {"match_cut": {"basis": "place", "key": "the atrium"}},
        {"transition": "match_cut", "transition_reason": "continuity",
         "match_cut": {"basis": "place", "key": "the lobby"}},
    ]
    record = transitions.plan_transitions(items)[0]
    assert record["downgraded"] is True
    assert record["downgrade_cause"] == "match_cut_continuity_unrecorded"


def test_match_cut_downgraded_on_wrong_reason():
    items = [
        {"match_cut": {"basis": "shape", "key": "arch"}},
        {"transition": "match_cut",
         "transition_reason": "scene_boundary",
         "match_cut": {"basis": "shape", "key": "arch"}},
    ]
    record = transitions.plan_transitions(items)[0]
    assert record["downgraded"] is True
    assert record["downgrade_cause"] == "match_cut_continuity_unrecorded"


def test_match_cut_never_a_segment_kind():
    assert "match_cut" not in transitions.NON_CUT_TRANSITIONS
    assert transitions.build_transition_segment(
        "a.mp4", "b.mp4", "match_cut", W, "x.mp4") is None


# ---------------------------------------------------------------------------
# Segment builds — exact frames + pixel behaviour
# ---------------------------------------------------------------------------

def test_split_screen_segment(colour_clips, tmp_path):
    result = _segment(colour_clips, "split_screen", tmp_path)
    assert result["frames"] == 2 * W
    path = result["path"]
    # First frame pure A, last frame pure B.
    assert _close(_frame_pixel(path, 0), RED)
    assert _close(_frame_pixel(path, 2 * W - 1), BLUE)
    # Mid-window: left still A, right already B (the split).
    mid = W - 1
    assert _close(_frame_pixel(path, mid, (300, 540)), RED)
    assert _close(_frame_pixel(path, mid, (1600, 540)), BLUE)


def test_parallax_depth_segment(colour_clips, tmp_path):
    result = _segment(colour_clips, "parallax_depth", tmp_path)
    assert result["frames"] == 2 * W
    path = result["path"]
    assert _close(_frame_pixel(path, 0), RED)
    assert _close(_frame_pixel(path, 2 * W - 1), BLUE)
    # Mid-window: B has arrived at the right edge; A remains at
    # the left, DIMMED below its source brightness (the depth
    # treatment), still red-dominant.
    mid = W - 1
    assert _close(_frame_pixel(path, mid, (1700, 540)), BLUE)
    left = _frame_pixel(path, mid, (200, 540))
    assert left[0] > 100 and left[0] < RED[0] - 8, left
    assert left[0] > left[2] + 60, left


def test_film_strip_segment(colour_clips, tmp_path):
    result = _segment(colour_clips, "film_strip", tmp_path)
    assert result["frames"] == 2 * W
    path = result["path"]
    assert _close(_frame_pixel(path, 0), RED)
    assert _close(_frame_pixel(path, 2 * W - 1), BLUE)
    # At the seam frame, the strip band is drawn over the seam:
    # its centre darkens the incoming colour by half, and its
    # sprocket edge zone is near-black away from the holes.
    mid = W - 1
    sx = int(1920 * mid / (2 * W - 1))
    seam = _frame_pixel(path, mid, (sx, 540))
    assert seam[2] < BLUE[2] - 60 and seam[0] < 60, seam
    edge = _frame_pixel(path, mid, (sx - 70, 540))
    assert max(edge) < 40, edge
    # Left of the seam the incoming (blue) clip already shows;
    # right of it the outgoing (red) still shows.
    assert _close(_frame_pixel(path, mid, (sx - 400, 540)), BLUE)
    assert _close(_frame_pixel(path, mid, (sx + 400, 540)), RED)


# ---------------------------------------------------------------------------
# Renderer integration — a film using a Wave 2 transition has
# exactly the all-cut film's frame count, and a match cut leaves
# the film (and its audit) as a plain cut with the rule recorded
# ---------------------------------------------------------------------------

def _two_scene_timeline(tmp_path, first_extra, second_extra):
    red_png = _solid_png(tmp_path / "red.png", RED)
    blue_png = _solid_png(tmp_path / "blue.png", BLUE)
    item_a = {
        "type": "image", "file_path": red_png, "path": red_png,
        "start": 0.0, "end": 1.0, "duration": 1.0, "transition": "cut",
    }
    item_a.update(first_extra)
    item_b = {
        "type": "image", "file_path": blue_png, "path": blue_png,
        "start": 1.0, "end": 2.0, "duration": 1.0, "transition": "cut",
    }
    item_b.update(second_extra)
    return [
        {"scene_number": 1, "items": [item_a]},
        {"scene_number": 2, "items": [item_b]},
    ]


def test_render_timeline_split_screen_preserves_total_frames(tmp_path):
    project_dir = tmp_path / "split_project"
    scenes = _two_scene_timeline(
        tmp_path, {},
        {"transition": "split_screen",
         "transition_reason": "scene_boundary"})
    output = renderer.render_timeline(
        scenes, project_dir, expected_duration=2.0)
    assert renderer.count_frames_exact(output) == 60
    audit = json.loads(
        (project_dir / "renderer_timing_audit.json").read_text())
    assert audit["transitions"]["status"] == "applied"
    applied = audit["transitions"]["applied"]
    assert len(applied) == 1
    assert applied[0]["transition"] == "split_screen"
    assert applied[0]["segment_frames"] == 24


def test_render_timeline_match_cut_is_a_plain_cut_with_record(tmp_path):
    project_dir = tmp_path / "match_project"
    scenes = _two_scene_timeline(
        tmp_path,
        {"match_cut": {"basis": "subject", "key": "the white van"}},
        {"transition": "match_cut", "transition_reason": "continuity",
         "match_cut": {"basis": "subject", "key": "the white van"}})
    output = renderer.render_timeline(
        scenes, project_dir, expected_duration=2.0)
    assert renderer.count_frames_exact(output) == 60
    audit = json.loads(
        (project_dir / "renderer_timing_audit.json").read_text())
    # No segment was built (the film is a plain concat), but the
    # plan record carries the match-cut evidence.
    assert audit["transitions"]["status"] == "all_cut"
    assert audit["transitions"]["applied"] == []
    records = audit["transitions"]["records"]
    assert records[0]["match_cut"] == {
        "basis": "subject", "key": "the white van"}
    assert not (project_dir / "transitions").exists()
