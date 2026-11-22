"""F9 transitions basics (Wave 1) — frame-exact edit-point transitions.

Covers plan_transitions downgrade rules, per-kind segment renders
on real clips (exact 2W frame counts, genuine blends / black
boundary / horizontal slide / burn flare), and render_timeline
integration: a faded film has exactly the all-cut film's frame
count, and an all-cut film uses the plain concat path unchanged.
"""

import copy
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from services import renderer, transitions

RED = (200, 30, 30)
BLUE = (30, 30, 200)
W = 6


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _solid_png(path, colour):
    # BMP (uncompressed) so the file clears the renderer's
    # valid_media size floor — a solid-colour PNG compresses to
    # well under 1000 bytes and would be treated as invalid media.
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
    workdir = tmp_path_factory.mktemp("transition_clips")
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


# ---------------------------------------------------------------------------
# plan_transitions
# ---------------------------------------------------------------------------

def test_plan_no_reason_downgraded_to_cut_with_cause():
    items = [{"transition": "cut"}, {"transition": "fade"}]
    plan = transitions.plan_transitions(items)
    assert len(plan) == 1
    record = plan[0]
    assert record["transition"] == "cut"
    assert record["honoured"] is False
    assert record["downgraded"] is True
    assert record["downgrade_cause"] == "missing_or_invalid_transition_reason"
    assert record["window_frames"] == 0


def test_plan_invalid_reason_downgraded():
    items = [{}, {"transition": "dissolve", "transition_reason": "vibes"}]
    record = transitions.plan_transitions(items)[0]
    assert record["transition"] == "cut"
    assert record["downgrade_cause"] == "missing_or_invalid_transition_reason"


def test_plan_unknown_type_downgraded():
    items = [{}, {"transition": "spin_wipe", "transition_reason": "scene_boundary"}]
    record = transitions.plan_transitions(items)[0]
    assert record["transition"] == "cut"
    assert record["downgraded"] is True
    assert record["downgrade_cause"] == "unknown_transition"


def test_plan_valid_honoured_with_default_windows():
    items = [
        {},
        {"transition": "fade", "transition_reason": "scene_boundary"},
        {"transition": "whip_pan", "transition_reason": "time_passage"},
        {"transition": "film_burn", "transition_reason": "continuity"},
    ]
    plan = transitions.plan_transitions(items)
    assert [r["transition"] for r in plan] == ["fade", "whip_pan", "film_burn"]
    assert all(r["honoured"] for r in plan)
    assert plan[0]["window_frames"] == 12
    assert plan[1]["window_frames"] == 8
    assert plan[2]["window_frames"] == 12


def test_plan_item_zero_never_has_incoming_transition():
    items = [{"transition": "fade", "transition_reason": "scene_boundary"}]
    assert transitions.plan_transitions(items) == []
    items = [
        {"transition": "dissolve", "transition_reason": "scene_boundary"},
        {"transition": "cut"},
    ]
    plan = transitions.plan_transitions(items)
    assert len(plan) == 1 and plan[0]["index"] == 1


def test_plan_plain_cut_is_not_a_downgrade():
    record = transitions.plan_transitions([{}, {"transition": "cut"}])[0]
    assert record["transition"] == "cut"
    assert record["downgraded"] is False
    assert record["downgrade_cause"] is None


# ---------------------------------------------------------------------------
# Segments on real clips
# ---------------------------------------------------------------------------

def test_dissolve_segment_exact_frames_and_genuine_blend(colour_clips, tmp_path):
    result = _segment(colour_clips, "dissolve", tmp_path)
    assert result["frames"] == 2 * W
    assert renderer.count_frames_exact(result["path"]) == 2 * W
    first = _frame_pixel(result["path"], 0)
    assert first[0] > 150 and first[2] < 80  # pure A (red)
    last = _frame_pixel(result["path"], 2 * W - 1)
    assert last[2] > 150 and last[0] < 80  # pure B (blue)
    mid = _frame_pixel(result["path"], W)
    # A genuine blend: both channels strictly between the two colours.
    assert 60 < mid[0] < 170
    assert 60 < mid[2] < 170


def test_fade_segment_exact_frames_and_black_boundary(colour_clips, tmp_path):
    result = _segment(colour_clips, "fade", tmp_path)
    assert result["frames"] == 2 * W
    assert renderer.count_frames_exact(result["path"]) == 2 * W
    first = _frame_pixel(result["path"], 0)
    assert first[0] > 150  # full brightness at the start
    # Frames straddling the boundary (alpha ~0.5) are near-black.
    for index in (W - 1, W):
        pixel = _frame_pixel(result["path"], index)
        assert max(pixel) < 45, f"frame {index} not near-black: {pixel}"
    last = _frame_pixel(result["path"], 2 * W - 1)
    assert last[2] > 150  # full brightness B at the end


def test_whip_pan_segment_slides_horizontally(colour_clips, tmp_path):
    result = _segment(colour_clips, "whip_pan", tmp_path)
    assert result["frames"] == 2 * W
    assert renderer.count_frames_exact(result["path"]) == 2 * W
    mid = W
    left = _frame_pixel(result["path"], mid, xy=(100, 540))
    right = _frame_pixel(result["path"], mid, xy=(1820, 540))
    assert left[0] > 120 and left[2] < 90  # A still on the left
    assert right[2] > 120 and right[0] < 90  # B entered from the right
    first_left = _frame_pixel(result["path"], 0, xy=(100, 540))
    last_left = _frame_pixel(result["path"], 2 * W - 1, xy=(100, 540))
    assert first_left[0] > 120  # starts as A
    assert last_left[2] > 120  # ends as B — the frame slid left


def test_film_burn_segment_exact_frames_with_flare(colour_clips, tmp_path):
    result = _segment(colour_clips, "film_burn", tmp_path)
    assert result["frames"] == 2 * W
    assert renderer.count_frames_exact(result["path"]) == 2 * W
    assert Path(result["path"]).stat().st_size > 1000
    # Near the boundary the flare is bright and warm.
    out = Path(result["path"] + ".burnframe.png")
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", result["path"],
            "-vf", f"select='eq(n\\,{W})'", "-frames:v", "1", str(out),
        ],
        check=True,
    )
    image = Image.open(out).convert("RGB")
    centre = image.getpixel((960, 594))
    assert centre[0] > 200, f"no bright flare at centre: {centre}"
    assert centre[0] >= centre[2]  # warm (red-dominant), not blue B


def test_segment_window_clamped_to_shorter_clip(colour_clips, tmp_path):
    clip_a, _ = colour_clips
    short_png = _solid_png(tmp_path / "short.png", BLUE)
    short_clip = _make_clip(short_png, tmp_path / "short.mp4", frames=4)
    result = transitions.build_transition_segment(
        clip_a, short_clip, "dissolve", 12, tmp_path / "clamped.mp4"
    )
    assert result is not None
    assert result["window_frames"] == 4
    assert result["window_clamped"] is True
    assert renderer.count_frames_exact(result["path"]) == 8


def test_segment_rejects_unknown_kind_and_tiny_clips(colour_clips, tmp_path):
    clip_a, clip_b = colour_clips
    assert transitions.build_transition_segment(
        clip_a, clip_b, "spin_wipe", W, tmp_path / "x.mp4"
    ) is None
    tiny_png = _solid_png(tmp_path / "tiny.png", RED)
    tiny_clip = _make_clip(tiny_png, tmp_path / "tiny.mp4", frames=1)
    assert transitions.build_transition_segment(
        tiny_clip, clip_b, "dissolve", W, tmp_path / "y.mp4"
    ) is None


def test_split_for_transition_exact_parts(colour_clips, tmp_path):
    clip_a, _ = colour_clips
    parts = transitions.split_for_transition(clip_a, W, tmp_path, stem="a")
    assert parts["total_frames"] == 30
    assert parts["head_frames"] == 24 and parts["tail_frames"] == W
    assert renderer.count_frames_exact(parts["head_path"]) == 24
    assert renderer.count_frames_exact(parts["tail_path"]) == W


# ---------------------------------------------------------------------------
# render_timeline integration
# ---------------------------------------------------------------------------

def _two_scene_timeline(tmp_path, second_item_extra):
    red_png = _solid_png(tmp_path / "red.png", RED)
    blue_png = _solid_png(tmp_path / "blue.png", BLUE)
    item_a = {
        "type": "image", "file_path": red_png, "path": red_png,
        "start": 0.0, "end": 1.0, "duration": 1.0, "transition": "cut",
    }
    item_b = {
        "type": "image", "file_path": blue_png, "path": blue_png,
        "start": 1.0, "end": 2.0, "duration": 1.0, "transition": "cut",
    }
    item_b.update(second_item_extra)
    return [
        {"scene_number": 1, "items": [item_a]},
        {"scene_number": 2, "items": [item_b]},
    ]


def test_render_timeline_fade_preserves_total_frames(tmp_path, monkeypatch):
    captured = {}
    original_concat = renderer.concatenate_clips

    def capturing_concat(clips, output_path, expected_frames=None):
        captured["clips"] = list(clips)
        captured["expected_frames"] = expected_frames
        return original_concat(clips, output_path, expected_frames=expected_frames)

    monkeypatch.setattr(renderer, "concatenate_clips", capturing_concat)

    fade_dir = tmp_path / "fade_project"
    scenes = _two_scene_timeline(
        tmp_path, {"transition": "fade", "transition_reason": "scene_boundary"}
    )
    output = renderer.render_timeline(scenes, fade_dir, expected_duration=2.0)
    fade_frames = renderer.count_frames_exact(output)
    audit = json.loads((fade_dir / "renderer_timing_audit.json").read_text())
    assert audit["transitions"]["status"] == "applied"
    applied = audit["transitions"]["applied"]
    assert len(applied) == 1
    assert applied[0]["transition"] == "fade"
    assert applied[0]["window_frames"] == 12
    assert applied[0]["segment_frames"] == 24
    # The join sequence is NOT the plain clip list — a segment is in it.
    assert any("transition_" in Path(c).name for c in captured["clips"])

    cut_dir = tmp_path / "cut_project"
    cut_scenes = _two_scene_timeline(tmp_path, {})
    cut_output = renderer.render_timeline(cut_scenes, cut_dir, expected_duration=2.0)
    cut_frames = renderer.count_frames_exact(cut_output)

    assert fade_frames == cut_frames == 60


def test_render_timeline_all_cut_uses_plain_concat_unchanged(tmp_path, monkeypatch):
    captured = {}
    original_concat = renderer.concatenate_clips

    def capturing_concat(clips, output_path, expected_frames=None):
        captured["clips"] = list(clips)
        return original_concat(clips, output_path, expected_frames=expected_frames)

    monkeypatch.setattr(renderer, "concatenate_clips", capturing_concat)

    project_dir = tmp_path / "plain_project"
    scenes = _two_scene_timeline(tmp_path, {})
    output = renderer.render_timeline(
        copy.deepcopy(scenes), project_dir, expected_duration=2.0
    )
    assert renderer.count_frames_exact(output) == 60
    # Plain concat: exactly the two scene clips, in order, nothing else.
    assert len(captured["clips"]) == 2
    assert all("transitions" not in str(c) for c in captured["clips"])
    assert [Path(c).name for c in captured["clips"]] == ["clip_0001.mp4", "clip_0001.mp4"]
    audit = json.loads((project_dir / "renderer_timing_audit.json").read_text())
    assert audit["transitions"]["status"] == "all_cut"
    assert audit["transitions"]["applied"] == []
    assert not (project_dir / "transitions").exists()


def test_render_timeline_reasonless_transition_downgraded_in_audit(tmp_path):
    project_dir = tmp_path / "downgrade_project"
    scenes = _two_scene_timeline(tmp_path, {"transition": "dissolve"})
    output = renderer.render_timeline(scenes, project_dir, expected_duration=2.0)
    assert renderer.count_frames_exact(output) == 60
    audit = json.loads((project_dir / "renderer_timing_audit.json").read_text())
    assert audit["transitions"]["status"] == "all_cut"
    downgraded = audit["transitions"]["downgraded"]
    assert len(downgraded) == 1
    assert downgraded[0]["downgrade_cause"] == "missing_or_invalid_transition_reason"
