"""Animation Studio Expansion §11 — the multi-engine
composition proof: one timeline assembled from graphics
rendered by FIVE different engines (PIL, Manim, matplotlib,
Blender, web/Chromium), every clip passed through the real
path's own technical validation (validate_graphic_output) and
visual inspection (frame_inspection.inspect_video), and the
composition assembled with the renderer's own
concatenate_clips. No production job is submitted (the film
stop is honoured); this exercises the same functions the
pipeline itself uses.

Each engine segment is skip-gated on that engine's
availability; the test requires at least three engines to be
present so it can never pass vacuously.
"""

import subprocess

import pytest
from PIL import Image, ImageDraw

from services import graphics_pipeline, renderer
from services.media_probe import probe_video
from services.motion_engine import (
    blender_templates, frame_inspection, manim_templates,
    mpl_templates, templates, web_templates,
)

DURATION = 1.2


def _document_image(path):
    img = Image.new("RGB", (1200, 1600), (250, 248, 240))
    d = ImageDraw.Draw(img)
    for i in range(18):
        y = 120 + i * 62
        d.rectangle([90, y, 1110, y + 16], fill=(70, 68, 62))
    for i in range(8):
        y = 1080 + i * 46
        d.rectangle([640, y, 1110, y + 20], fill=(30, 28, 26))
    img.save(path)
    return str(path)


def _render_pil(tmp_path):
    image = _document_image(tmp_path / "doc.png")
    out = tmp_path / "seg_pil.mp4"
    rendered, meta = templates.TEMPLATES["document_examination"](
        {"image": image,
         "examine": {"x": 0.5, "y": 0.65, "w": 0.42, "h": 0.28}},
        None, out, duration=DURATION)
    assert rendered is not None, meta
    return out


def _render_manim(tmp_path):
    if not manim_templates.manim_available():
        pytest.skip("manim unavailable")
    out = tmp_path / "seg_manim.mp4"
    rendered, meta = templates.TEMPLATES["manim_bar_chart"](
        {"recorded": True, "title": "Traffic by route",
         "items": [
             {"label": "Harbour", "value": 120},
             {"label": "Rail", "value": 260},
             {"label": "Road", "value": 190}],
         "units": "ships per year", "source": "Port records"},
        None, out, duration=DURATION)
    assert rendered is not None, meta
    return out


def _render_mpl(tmp_path):
    out = tmp_path / "seg_mpl.mp4"
    rendered, meta = mpl_templates.mpl_bar_chart(
        {"recorded": True, "title": "Exports",
         "items": [
             {"label": "1901", "value": 40, "display": "40"},
             {"label": "1911", "value": 75, "display": "75"},
             {"label": "1921", "value": 130, "display": "130"}],
         "units": "thousand tonnes", "source": "Customs returns"},
        None, out, duration=DURATION)
    assert rendered is not None, meta
    return out


def _render_blender(tmp_path, monkeypatch):
    if not blender_templates.blender_available():
        pytest.skip("blender unavailable")
    monkeypatch.setenv("CINEMORA_BLENDER_WIDTH", "480")
    monkeypatch.setenv("CINEMORA_BLENDER_HEIGHT", "270")
    monkeypatch.setenv("CINEMORA_BLENDER_SAMPLES", "6")
    out = tmp_path / "seg_blender.mp4"
    rendered, meta = templates.TEMPLATES["blender_title_3d"](
        {"title": "The Harbour", "date_label": "1897"},
        None, out, duration=DURATION)
    assert rendered is not None, meta
    assert meta["visualisation"] == "illustrative"
    return out


def _render_web(tmp_path):
    if not web_templates.web_engine_available():
        pytest.skip("web engine unavailable")
    out = tmp_path / "seg_web.mp4"
    rendered, meta = templates.TEMPLATES["three_globe"](
        {"places": [
            {"name": "London", "lon": -0.12, "lat": 51.5},
            {"name": "New York", "lon": -74.0, "lat": 40.7}]},
        None, out, duration=DURATION)
    assert rendered is not None, meta
    return out


def test_multi_engine_composition(tmp_path, monkeypatch):
    segments = []
    builders = [
        ("pil", _render_pil),
        ("manim", _render_manim),
        ("mpl", _render_mpl),
        ("blender", _render_blender),
        ("web", _render_web),
    ]
    for name, build in builders:
        try:
            if name == "blender":
                clip = build(tmp_path, monkeypatch)
            else:
                clip = build(tmp_path)
        except pytest.skip.Exception:
            continue
        segments.append((name, clip))
    assert len(segments) >= 3, \
        f"only {len(segments)} engines available: {segments}"

    # The real path's own gates, applied to every engine's
    # output exactly as register_asset applies them.
    for name, clip in segments:
        verdict = graphics_pipeline.validate_graphic_output(
            clip, expected_duration=DURATION)
        assert verdict["passed"], (name, verdict["detail"])
        inspection = frame_inspection.inspect_video(clip)
        assert inspection["passed"], (name, inspection["defects"])

    # Assembly through the renderer's own concatenation.
    out = tmp_path / "composition.mp4"
    ok = renderer.concatenate_clips(
        [str(clip) for _name, clip in segments], out)
    assert ok
    probe = probe_video(out)
    assert probe["width"] == 1920 and probe["height"] == 1080
    expected = DURATION * len(segments)
    assert abs(float(probe["duration_seconds"]) - expected) < 0.6
    # The composition decodes end to end.
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(out), "-f", "null", "-"],
        capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0
