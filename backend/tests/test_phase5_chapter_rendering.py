"""Phase 5 — chapter rendering & deterministic clip caching.

Covers the Phase 5 acceptance surface:

- a chapter whose inputs are unchanged is NOT re-rendered on a
  repeat render (cache hit at chapter, clip and segment level);
- a change to any input class — source content, timing,
  transition windows, narration master, render settings, the
  Phase 3 classification stamp — misses the cache for exactly the
  affected chapters;
- a render interrupted mid-way (simulated worker kill) resumes
  reusing completed chapters and completed clips, rendering only
  what is missing (acceptance #2);
- the assembled film is equivalent to the monolithic render for
  identical inputs: same frame count, duration, streams, and
  chapter-boundary continuity (acceptance #16's rendering half);
- an unclean scene (holdover) is never stored under its input
  key, so it can never be served stale;
- a chapter-path inconsistency falls back to the monolithic path
  and the film still renders (the chapter path can only make a
  render faster, never change its verdict);
- the cache pruner bounds the store per production and never
  touches anything outside ``render_cache/``.

Fixtures are tiny real encodes (solid-colour BMP stills, 1s
scenes) through the real renderer, in the wave-1 pattern.
"""

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from services import render_cache, renderer, transitions

RED = (200, 30, 30)
BLUE = (30, 30, 200)
GREEN = (30, 180, 30)
YELLOW = (220, 200, 30)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _solid_bmp(path, colour):
    # BMP (uncompressed) so the file clears the renderer's
    # valid_media size floor — a solid PNG compresses below it.
    path = Path(path).with_suffix(".bmp")
    Image.new("RGB", (320, 180), colour).save(path)
    return str(path)


def _item(image_path, start, end, **extra):
    item = {
        "type": "image",
        "file_path": image_path,
        "path": image_path,
        "start": float(start),
        "end": float(end),
        "duration": float(end) - float(start),
        "transition": "cut",
    }
    item.update(extra)
    return item


def _two_scenes(tmp_path):
    red = _solid_bmp(tmp_path / "red.bmp", RED)
    blue = _solid_bmp(tmp_path / "blue.bmp", BLUE)
    return [
        {"scene_number": 1, "items": [_item(red, 0.0, 1.0)]},
        {"scene_number": 2, "items": [_item(blue, 1.0, 2.0)]},
    ]


def _audit(project_dir):
    return json.loads(
        (Path(project_dir) / "renderer_timing_audit.json").read_text()
    )


def _chapter_statuses(project_dir):
    return [c["status"] for c in _audit(project_dir)["chapters"]]


def _counting_create_image_clip(monkeypatch):
    calls = []
    original = renderer.create_image_clip

    def counting(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(renderer, "create_image_clip", counting)
    return calls


def _frame_mean(path, frame_index):
    out = Path(str(path) + f".mean{frame_index}.png")
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(path),
            "-vf", f"select='eq(n\\,{frame_index})',scale=64:36",
            "-frames:v", "1", str(out),
        ],
        check=True, capture_output=True,
    )
    img = Image.open(out).convert("RGB")
    pixels = list(img.getdata())
    n = len(pixels)
    return tuple(sum(p[c] for p in pixels) / n for c in range(3))


# ---------------------------------------------------------------------------
# Chapter cache: hit on identical inputs
# ---------------------------------------------------------------------------

def test_repeat_render_reuses_every_chapter_and_encodes_nothing(
    tmp_path, monkeypatch
):
    project_dir = tmp_path / "project"
    scenes = _two_scenes(tmp_path)

    output = renderer.render_timeline(
        scenes, project_dir, expected_duration=2.0
    )
    assert renderer.count_frames_exact(output) == 60
    assert _chapter_statuses(project_dir) == ["rendered", "rendered"]
    assert (project_dir / "chapters" / "chapter_001.mp4").exists()
    assert (project_dir / "chapters" / "chapter_002.mp4").exists()
    assert (project_dir / "render_cache").is_dir()

    calls = _counting_create_image_clip(monkeypatch)
    output2 = renderer.render_timeline(
        scenes, project_dir, expected_duration=2.0
    )
    assert renderer.count_frames_exact(output2) == 60
    # Every chapter reused; not a single clip was re-encoded.
    assert _chapter_statuses(project_dir) == ["reused", "reused"]
    assert calls == []
    stats = _audit(project_dir)["render_cache"]
    assert stats["chapter_hits"] == 2
    assert stats["chapter_misses"] == 0


def test_segment_cache_serves_boundary_transition_on_repeat(
    tmp_path, monkeypatch
):
    project_dir = tmp_path / "project"
    scenes = _two_scenes(tmp_path)
    scenes[1]["items"][0].update(
        {"transition": "fade", "transition_reason": "scene_boundary"}
    )
    renderer.render_timeline(scenes, project_dir, expected_duration=2.0)

    builds = []
    original = transitions.build_transition_segment

    def counting(*args, **kwargs):
        builds.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        transitions, "build_transition_segment", counting
    )
    output = renderer.render_timeline(
        scenes, project_dir, expected_duration=2.0
    )
    assert renderer.count_frames_exact(output) == 60
    assert _chapter_statuses(project_dir) == ["reused", "reused"]
    # The boundary segment came from the segment cache — it was
    # not rebuilt even though the chapters were only reused.
    assert builds == []
    audit = _audit(project_dir)
    assert audit["transitions"]["status"] == "applied"
    assert len(audit["transitions"]["applied"]) == 1
    assert audit["render_cache"]["segment_hits"] >= 1


# ---------------------------------------------------------------------------
# Cache misses: one test per input class
# ---------------------------------------------------------------------------

def _render_then_change(tmp_path, monkeypatch, change):
    project_dir = tmp_path / "project"
    scenes = _two_scenes(tmp_path)
    renderer.render_timeline(scenes, project_dir, expected_duration=2.0)
    assert _chapter_statuses(project_dir) == ["rendered", "rendered"]
    change(scenes, project_dir, tmp_path)
    renderer.render_timeline(scenes, project_dir, expected_duration=2.0)
    return _chapter_statuses(project_dir)


def test_miss_on_source_content_change(tmp_path, monkeypatch):
    def change(scenes, project_dir, root):
        # Same path, different bytes — the content hash moves.
        _solid_bmp(root / "blue.bmp", GREEN)

    assert _render_then_change(tmp_path, monkeypatch, change) == [
        "reused", "rendered",
    ]


def test_miss_on_timing_change(tmp_path, monkeypatch):
    def change(scenes, project_dir, root):
        scenes[1]["items"][0]["duration"] = 1.5
        scenes[1]["items"][0]["end"] = 2.5

    project_dir = tmp_path / "project"
    scenes = _two_scenes(tmp_path)
    renderer.render_timeline(scenes, project_dir, expected_duration=2.0)
    change(scenes, project_dir, tmp_path)
    renderer.render_timeline(scenes, project_dir, expected_duration=2.5)
    assert _chapter_statuses(project_dir) == ["reused", "rendered"]


def test_miss_on_transition_change_reaches_both_edge_chapters(
    tmp_path, monkeypatch
):
    def change(scenes, project_dir, root):
        scenes[1]["items"][0].update(
            {"transition": "fade", "transition_reason": "scene_boundary"}
        )

    # The boundary window trims BOTH adjacent chapters' edge
    # clips, so both chapter keys move.
    assert _render_then_change(tmp_path, monkeypatch, change) == [
        "rendered", "rendered",
    ]


def test_miss_on_narration_change(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    (project_dir / "narration.wav").write_bytes(b"\x11" * 4096)
    scenes = _two_scenes(tmp_path)
    renderer.render_timeline(scenes, project_dir, expected_duration=2.0)
    assert _chapter_statuses(project_dir) == ["rendered", "rendered"]

    (project_dir / "narration.wav").write_bytes(b"\x22" * 4096)
    renderer.render_timeline(scenes, project_dir, expected_duration=2.0)
    assert _chapter_statuses(project_dir) == ["rendered", "rendered"]


def test_miss_on_render_settings_change(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    scenes = _two_scenes(tmp_path)
    renderer.render_timeline(scenes, project_dir, expected_duration=2.0)
    monkeypatch.setattr(renderer, "CRF", "24")
    renderer.render_timeline(scenes, project_dir, expected_duration=2.0)
    assert _chapter_statuses(project_dir) == ["rendered", "rendered"]


def test_miss_on_classification_change(tmp_path, monkeypatch):
    def change(scenes, project_dir, root):
        # The Phase 3 authenticity stamp is part of the slice.
        scenes[0]["items"][0]["classification"] = "illustrative_stock"

    assert _render_then_change(tmp_path, monkeypatch, change) == [
        "rendered", "reused",
    ]


# ---------------------------------------------------------------------------
# Interruption / resume (acceptance #2)
# ---------------------------------------------------------------------------

def test_kill_mid_render_resumes_reusing_completed_chapters(
    tmp_path, monkeypatch
):
    project_dir = tmp_path / "project"
    red = _solid_bmp(tmp_path / "red.bmp", RED)
    blue = _solid_bmp(tmp_path / "blue.bmp", BLUE)
    green = _solid_bmp(tmp_path / "green.bmp", GREEN)
    scenes = [
        {"scene_number": 1, "items": [_item(red, 0.0, 1.0)]},
        {"scene_number": 2, "items": [_item(blue, 1.0, 2.0)]},
        {"scene_number": 3, "items": [_item(green, 2.0, 3.0)]},
    ]

    original_render_scene = renderer.render_scene
    state = {"killed": False}

    def killing_render_scene(scene, project_dir_arg, *args, **kwargs):
        if scene.get("scene_number") == 2 and not state["killed"]:
            state["killed"] = True
            raise RuntimeError("simulated worker kill")
        return original_render_scene(
            scene, project_dir_arg, *args, **kwargs
        )

    monkeypatch.setattr(renderer, "render_scene", killing_render_scene)
    with pytest.raises(RuntimeError, match="simulated worker kill"):
        renderer.render_timeline(scenes, project_dir, expected_duration=3.0)
    # Chapter 1 completed and was cached before the kill.
    assert (project_dir / "chapters" / "chapter_001.mp4").exists()
    assert not (project_dir / "documentary.mp4").exists()

    monkeypatch.setattr(renderer, "render_scene", original_render_scene)
    calls = _counting_create_image_clip(monkeypatch)
    output = renderer.render_timeline(
        scenes, project_dir, expected_duration=3.0
    )
    assert renderer.count_frames_exact(output) == 90
    # Only the missing chapters rendered; chapter 1 was reused and
    # its clip was not re-encoded (2 encodes: scenes 2 and 3).
    assert _chapter_statuses(project_dir) == [
        "reused", "rendered", "rendered",
    ]
    assert len(calls) == 2


def test_kill_mid_scene_reuses_completed_clips(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    red = _solid_bmp(tmp_path / "red.bmp", RED)
    blue = _solid_bmp(tmp_path / "blue.bmp", BLUE)
    green = _solid_bmp(tmp_path / "green.bmp", GREEN)
    scenes = [{
        "scene_number": 1,
        "items": [
            _item(red, 0.0, 1.0),
            _item(blue, 1.0, 2.0),
            _item(green, 2.0, 3.0),
        ],
    }]

    original = renderer.create_image_clip
    state = {"count": 0}

    def killing(*args, **kwargs):
        state["count"] += 1
        if state["count"] == 2:
            raise RuntimeError("simulated worker kill")
        return original(*args, **kwargs)

    monkeypatch.setattr(renderer, "create_image_clip", killing)
    with pytest.raises(RuntimeError, match="simulated worker kill"):
        renderer.render_timeline(scenes, project_dir, expected_duration=3.0)

    calls = _counting_create_image_clip(monkeypatch)
    output = renderer.render_timeline(
        scenes, project_dir, expected_duration=3.0
    )
    assert renderer.count_frames_exact(output) == 90
    # Clip 1 came from the clip cache; only clips 2 and 3 encoded.
    assert len(calls) == 2
    stats = _audit(project_dir)["render_cache"]
    assert stats["clip_hits"] >= 1


# ---------------------------------------------------------------------------
# Equivalence with the monolithic path (acceptance #16, rendering half)
# ---------------------------------------------------------------------------

def _equivalence_scenes(tmp_path):
    red = _solid_bmp(tmp_path / "red.bmp", RED)
    blue = _solid_bmp(tmp_path / "blue.bmp", BLUE)
    green = _solid_bmp(tmp_path / "green.bmp", GREEN)
    yellow = _solid_bmp(tmp_path / "yellow.bmp", YELLOW)
    return [
        {"scene_number": 1, "items": [_item(red, 0.0, 1.0)]},
        {
            "scene_number": 2,
            "items": [
                _item(blue, 1.0, 1.5),
                _item(
                    green, 1.5, 2.0,
                    transition="dissolve",
                    transition_reason="time_passage",
                ),
            ],
        },
        {
            "scene_number": 3,
            "items": [
                _item(
                    yellow, 2.0, 3.0,
                    transition="fade",
                    transition_reason="scene_boundary",
                )
            ],
        },
    ]


def test_chaptered_output_equivalent_to_monolithic(tmp_path, monkeypatch):
    scenes = _equivalence_scenes(tmp_path)

    mono_dir = tmp_path / "mono"
    monkeypatch.setenv("CINEMORA_CHAPTER_RENDERING", "0")
    mono_output = renderer.render_timeline(
        scenes, mono_dir, expected_duration=3.0
    )

    chapter_dir = tmp_path / "chaptered"
    monkeypatch.setenv("CINEMORA_CHAPTER_RENDERING", "1")
    chapter_output = renderer.render_timeline(
        scenes, chapter_dir, expected_duration=3.0
    )

    # Same duration / frame count / streams.
    assert renderer.count_frames_exact(mono_output) == 90
    assert renderer.count_frames_exact(chapter_output) == 90
    mono_info = renderer.probe_media(mono_output)
    chapter_info = renderer.probe_media(chapter_output)
    assert abs(mono_info["duration"] - chapter_info["duration"]) < 0.05
    assert (mono_info["width"], mono_info["height"]) == (1920, 1080)
    assert (chapter_info["width"], chapter_info["height"]) == (1920, 1080)
    assert abs(mono_info["avg_fps"] - chapter_info["avg_fps"]) < 0.01

    # Chapter-boundary continuity: sampled frames across the whole
    # film — including inside both transition windows (frames
    # ~39-50 internal dissolve, ~54-65 boundary fade) — match the
    # monolithic film's frames closely (identical decoded inputs
    # to the same final encode; small encoder variance allowed).
    for frame_index in (0, 20, 44, 45, 59, 60, 75, 89):
        mono_mean = _frame_mean(mono_output, frame_index)
        chapter_mean = _frame_mean(chapter_output, frame_index)
        for channel in range(3):
            assert abs(mono_mean[channel] - chapter_mean[channel]) <= 4.0, (
                frame_index, mono_mean, chapter_mean
            )

    # The chaptered audit records the same transition story.
    audit = _audit(chapter_dir)
    assert audit["transitions"]["status"] == "applied"
    assert len(audit["transitions"]["applied"]) == 2
    assert _chapter_statuses(chapter_dir) == [
        "rendered", "rendered", "rendered",
    ]


# ---------------------------------------------------------------------------
# Honesty / safety properties
# ---------------------------------------------------------------------------

def test_holdover_scene_is_never_served_from_the_chapter_cache(
    tmp_path, monkeypatch
):
    project_dir = tmp_path / "project"
    red = _solid_bmp(tmp_path / "red.bmp", RED)
    missing = str(tmp_path / "missing.bmp")
    scenes = [{
        "scene_number": 1,
        "items": [
            _item(red, 0.0, 1.0),
            # Missing media: covered by a holdover of the genuine
            # scene visual — an unclean render by construction.
            _item(missing, 1.0, 2.0),
        ],
    }]
    output = renderer.render_timeline(
        scenes, project_dir, expected_duration=2.0
    )
    assert renderer.count_frames_exact(output) == 60
    assert _audit(project_dir)["holdover_count"] == 1

    renderer.render_timeline(scenes, project_dir, expected_duration=2.0)
    # Not stored under the input key → rendered again, never
    # served as if it were the deterministic chapter.
    assert _chapter_statuses(project_dir) == ["rendered"]


def test_chapter_path_inconsistency_falls_back_to_monolithic(
    tmp_path, monkeypatch
):
    project_dir = tmp_path / "project"
    scenes = _two_scenes(tmp_path)
    monkeypatch.setattr(
        renderer, "_join_chapter_body", lambda *a, **k: False
    )
    output = renderer.render_timeline(
        scenes, project_dir, expected_duration=2.0
    )
    assert renderer.count_frames_exact(output) == 60
    # The monolithic path produced the film and its audit (no
    # chapter records — the legacy audit shape).
    assert "chapters" not in _audit(project_dir)


def test_cache_disabled_renders_without_reuse(tmp_path, monkeypatch):
    monkeypatch.setenv("CINEMORA_RENDER_CACHE", "0")
    project_dir = tmp_path / "project"
    scenes = _two_scenes(tmp_path)
    renderer.render_timeline(scenes, project_dir, expected_duration=2.0)
    calls = _counting_create_image_clip(monkeypatch)
    renderer.render_timeline(scenes, project_dir, expected_duration=2.0)
    assert _chapter_statuses(project_dir) == ["rendered", "rendered"]
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Cache store units
# ---------------------------------------------------------------------------

def test_canonicalise_drops_composition_render_record_and_hashes_files(
    tmp_path
):
    media = _solid_bmp(tmp_path / "still.bmp", RED)
    item = {
        "file_path": media,
        "composition": {
            "layers": [{"kind": "lower_third_card", "label": "X"}],
            "render": {"applied": [], "volatile": "output-only"},
        },
    }
    first = render_cache.item_fingerprint(item)
    item["composition"]["render"] = {"applied": ["different"]}
    second = render_cache.item_fingerprint(item)
    # The render OUTPUT stamped onto the plan never moves the key.
    assert first == second
    assert first["file_path"]["__file_sha256__"] == render_cache.hash_file(
        media
    )


def test_prune_bounds_cache_and_never_touches_production_files(tmp_path):
    project_dir = tmp_path / "project"
    store = render_cache.CacheStore(project_dir)
    master = project_dir / "documentary.mp4"
    project_dir.mkdir(parents=True)
    master.write_bytes(b"MASTER" * 500)

    source = tmp_path / "entry.mp4"
    source.write_bytes(b"\x07" * 6000)
    keys = []
    for i in range(4):
        key = render_cache.hash_json({"entry": i})
        keys.append(key)
        assert store.put("clip", key, source, metadata={"i": i})
    orphan = store.dir / "clip-deadbeef.partial-1-abcdef12.mp4"
    orphan.write_bytes(b"partial")

    evicted = store.prune(max_bytes=13000)
    assert evicted >= 2
    remaining = list(store.dir.glob("clip-*.mp4"))
    assert 0 < len(remaining) <= 2
    # The newest entries survive; the orphan partial was swept.
    assert store.lookup("clip", keys[-1]) is not None
    assert not orphan.exists()
    # Nothing outside render_cache/ was touched.
    assert master.read_bytes() == b"MASTER" * 500
