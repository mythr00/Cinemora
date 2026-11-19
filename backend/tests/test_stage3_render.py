"""Stage 3F: still motion + clip offsets (MA-15/MA-16).

- timeline.build_scene_timeline stamps "clip_start" on VIDEO items
  (sentence-verified, rotation and legacy paths) from the chosen
  asset; image items keep their shape.
- renderer.create_image_clip produces REAL motion (zoompan) with an
  exact frame count.
- renderer.create_video_clip honours start_offset via input seeking.
"""

import hashlib
import subprocess
from pathlib import Path

import pytest

from services import renderer
from services.timeline import build_scene_timeline


# ---------------------------------------------------------------
# timeline clip_start
# ---------------------------------------------------------------

def _visual(tmp_path, name, vtype="video", clip_start=None):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 2048)
    visual = {
        "file_path": str(path),
        "path": str(path),
        "type": vtype,
        "title": name,
        "verification": {"accepted": True},
    }
    if clip_start is not None:
        visual["clip_start"] = clip_start
    return visual


def _timings(n):
    return [
        {
            "sentence_index": i,
            "text": f"Sentence {i}.",
            "start": float(i * 5),
            "end": float(i * 5 + 4),
        }
        for i in range(n)
    ]


def test_sentence_verified_video_item_carries_clip_start(tmp_path):
    v = _visual(tmp_path, "seg.mp4", clip_start=7.5)
    scene_media = {
        "scene_number": 1,
        "duration_seconds": 5,
        "videos": [v],
        "images": [],
        "sentences": [{"sentence_number": 1, "videos": [v], "images": []}],
    }
    items = build_scene_timeline(scene_media, _timings(1))["items"]
    assert items[0]["assignment"] == "sentence"
    assert items[0]["clip_start"] == 7.5


def test_rotation_video_item_carries_clip_start(tmp_path):
    v = _visual(tmp_path, "rot.mp4", clip_start=3.25)
    scene_media = {
        "scene_number": 1,
        "duration_seconds": 5,
        "videos": [v],
        "images": [],
        "sentences": [{"sentence_number": 1, "videos": [], "images": []}],
    }
    items = build_scene_timeline(scene_media, _timings(1))["items"]
    assert items[0]["assignment"] == "rotation"
    assert items[0]["clip_start"] == 3.25


def test_video_item_without_asset_offset_defaults_to_zero(tmp_path):
    v = _visual(tmp_path, "plain.mp4")
    scene_media = {
        "scene_number": 1,
        "duration_seconds": 5,
        "videos": [v],
        "images": [],
        "sentences": [{"sentence_number": 1, "videos": [v], "images": []}],
    }
    items = build_scene_timeline(scene_media, _timings(1))["items"]
    assert items[0]["clip_start"] == 0.0


def test_image_item_shape_unchanged_no_clip_start(tmp_path):
    img = _visual(tmp_path, "still.jpg", vtype="image", clip_start=9.0)
    scene_media = {
        "scene_number": 1,
        "duration_seconds": 5,
        "videos": [],
        "images": [img],
        "sentences": [{"sentence_number": 1, "videos": [], "images": [img]}],
    }
    items = build_scene_timeline(scene_media, _timings(1))["items"]
    assert items[0]["type"] == "image"
    assert "clip_start" not in items[0]


def test_legacy_fallback_video_item_carries_clip_start(tmp_path):
    v = _visual(tmp_path, "legacy.mp4", clip_start=11.0)
    scene_media = {
        "scene_number": 1,
        "duration_seconds": 10,
        "videos": [v],
        "images": [],
        "sentences": [],
    }
    items = build_scene_timeline(scene_media, None)["items"]
    assert items[0]["type"] == "video"
    assert items[0]["clip_start"] == 11.0


# ---------------------------------------------------------------
# renderer fixtures
# ---------------------------------------------------------------

@pytest.fixture(scope="module")
def pattern_image(tmp_path_factory):
    out = tmp_path_factory.mktemp("img") / "pattern.png"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=size=1280x720:duration=1:rate=1",
            "-frames:v", "1", str(out),
        ],
        check=True, timeout=60,
    )
    return out


@pytest.fixture(scope="module")
def colour_source(tmp_path_factory):
    """6s source: red 0-2s, green 2-4s, blue 4-6s."""
    out = tmp_path_factory.mktemp("vid") / "colours.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=red:size=320x180:duration=2:rate=30",
            "-f", "lavfi", "-i", "color=c=green:size=320x180:duration=2:rate=30",
            "-f", "lavfi", "-i", "color=c=blue:size=320x180:duration=2:rate=30",
            "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
        ],
        check=True, timeout=60,
    )
    return out


def _frame_png(video, t, out):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-ss", str(t),
            "-i", str(video), "-frames:v", "1", str(out),
        ],
        check=True, timeout=60,
    )
    return Path(out)


def _mean_rgb(png):
    from PIL import Image

    im = Image.open(png).convert("RGB").resize((16, 16))
    px = list(im.getdata())
    return tuple(sum(c[i] for c in px) / len(px) for i in range(3))


# ---------------------------------------------------------------
# renderer: image motion
# ---------------------------------------------------------------

def test_image_clip_has_real_motion_and_exact_frames(pattern_image, tmp_path):
    out = tmp_path / "img_motion.mp4"
    assert renderer.create_image_clip(
        str(pattern_image), 2.0, str(out), frame_count=60, motion="zoom_in"
    )
    assert renderer.count_frames_exact(out) == 60

    first = _frame_png(out, 0.0, tmp_path / "first.png")
    last = _frame_png(out, 1.9, tmp_path / "last.png")
    assert hashlib.md5(first.read_bytes()).hexdigest() != hashlib.md5(
        last.read_bytes()
    ).hexdigest()


def test_image_clip_pan_right_also_moves(pattern_image, tmp_path):
    out = tmp_path / "img_pan.mp4"
    assert renderer.create_image_clip(
        str(pattern_image), 2.0, str(out), frame_count=60, motion="pan_right"
    )
    assert renderer.count_frames_exact(out) == 60
    first = _frame_png(out, 0.0, tmp_path / "p_first.png")
    last = _frame_png(out, 1.9, tmp_path / "p_last.png")
    assert first.read_bytes() != last.read_bytes()


# ---------------------------------------------------------------
# renderer: video start offset
# ---------------------------------------------------------------

def test_video_clip_start_offset_seeks_into_source(colour_source, tmp_path):
    out = tmp_path / "offset.mp4"
    assert renderer.create_video_clip(
        str(colour_source), 1.0, str(out), frame_count=30, start_offset=2.0
    )
    assert renderer.count_frames_exact(out) == 30

    clip_rgb = _mean_rgb(_frame_png(out, 0.1, tmp_path / "c.png"))
    src_at_2 = _mean_rgb(_frame_png(colour_source, 2.1, tmp_path / "s2.png"))
    src_at_0 = _mean_rgb(_frame_png(colour_source, 0.1, tmp_path / "s0.png"))

    # Green segment (2-4s): green channel dominates in clip + source@2…
    assert clip_rgb[1] > clip_rgb[0] and clip_rgb[1] > clip_rgb[2]
    assert src_at_2[1] > src_at_2[0]
    # …while the source at 0s is red — the clip did NOT start at 0.
    assert src_at_0[0] > src_at_0[1]


def test_video_clip_default_starts_at_zero(colour_source, tmp_path):
    out = tmp_path / "plain.mp4"
    assert renderer.create_video_clip(
        str(colour_source), 1.0, str(out), frame_count=30
    )
    rgb = _mean_rgb(_frame_png(out, 0.1, tmp_path / "p.png"))
    assert rgb[0] > rgb[1]  # red segment
