"""Stage 5B tests: Motion Engine v2 templates.

Each template renders a real, ffprobe-verified 1920x1080 MP4 from
caller-supplied data with the standard metadata shape
(generator "motion_engine", generated True), and refuses with a
recorded reason when its inputs are insufficient.
"""

import pytest
from PIL import Image

from services.motion_engine import templates
from services.motion_engine.core import probe_video

DURATION = 1.2


@pytest.fixture()
def photos(tmp_path):
    paths = []
    for i, colour in enumerate([(70, 90, 120), (120, 80, 60), (60, 110, 80)]):
        p = tmp_path / f"photo{i}.jpg"
        Image.new("RGB", (800, 600), colour).save(p)
        paths.append(str(p))
    return paths


def _assert_render(name, data, style, tmp_path, **meta_checks):
    path, meta = templates.TEMPLATES[name](
        data, style, tmp_path / f"{name}.mp4", duration=DURATION,
        sentence_index=3, scene_number=2,
    )
    assert path is not None, meta
    info = probe_video(path)
    assert info["width"] == 1920 and info["height"] == 1080
    assert info["duration"] > 0
    assert meta["generator"] == "motion_engine"
    assert meta["template"] == name
    assert meta["generated"] is True
    assert meta["sentence_index"] == 3 and meta["scene_number"] == 2
    for key, expected in meta_checks.items():
        assert meta["inputs"][key] == expected, (name, key, meta["inputs"])
    return meta


def _assert_refusal(name, data, reason, tmp_path):
    path, meta = templates.TEMPLATES[name](
        data, "editorial", tmp_path / "refused.mp4", duration=DURATION,
    )
    assert path is None
    assert meta["reason"] == reason


# ---------------------------------------------------------------------------
# split_panel
# ---------------------------------------------------------------------------

def test_split_panel_renders(photos, tmp_path):
    _assert_render(
        "split_panel",
        {"photo": photos[0], "title": "Combined conditions",
         "bullets": ["Prolonged water immersion", "Construction disturbances",
                     "Highway and bridge load"]},
        "editorial", tmp_path, bullets=3,
    )


def test_split_panel_refusals(photos, tmp_path):
    _assert_refusal("split_panel", {"title": "T", "bullets": ["b"]}, "no_photo", tmp_path)
    _assert_refusal("split_panel", {"photo": photos[0], "bullets": ["b"]}, "no_title", tmp_path)
    _assert_refusal("split_panel", {"photo": photos[0], "title": "T"}, "no_bullets", tmp_path)
    _assert_refusal("split_panel", {"photo": "/nonexistent.jpg", "title": "T",
                                    "bullets": ["b"]}, "no_photo", tmp_path)


# ---------------------------------------------------------------------------
# scan_grid
# ---------------------------------------------------------------------------

def test_scan_grid_renders_with_texture(photos, tmp_path):
    _assert_render(
        "scan_grid",
        {"title": "Complex geological conditions",
         "caption": "Deep zone of heavily unloaded rock identified",
         "report_label": "Official report | Jul 1949"},
        "editorial", tmp_path, image=False,
    )


def test_scan_grid_renders_with_image(photos, tmp_path):
    _assert_render("scan_grid", {"image": photos[0]}, "history", tmp_path, image=True)


def test_scan_grid_refusal(tmp_path):
    _assert_refusal("scan_grid", {}, "no_scan_content", tmp_path)


# ---------------------------------------------------------------------------
# photo_node_diagram
# ---------------------------------------------------------------------------

def test_photo_node_diagram_renders(photos, tmp_path):
    _assert_render(
        "photo_node_diagram",
        {"nodes": [{"path": photos[0], "label": "Fractured rock"},
                   {"path": photos[1], "label": "Deep valleys"},
                   {"path": photos[2], "label": "Steep slopes"}],
         "title": "The terrain problem"},
        "editorial", tmp_path, nodes=3,
    )


def test_photo_node_diagram_refusals(photos, tmp_path):
    _assert_refusal("photo_node_diagram",
                    {"nodes": [{"path": photos[0], "label": "only one"}]},
                    "insufficient_nodes", tmp_path)
    _assert_refusal("photo_node_diagram", {"nodes": []}, "insufficient_nodes", tmp_path)
    _assert_refusal("photo_node_diagram",
                    {"nodes": [{"path": "/nope.jpg"}, {"path": "/nope2.jpg"}]},
                    "insufficient_nodes", tmp_path)


# ---------------------------------------------------------------------------
# ring_badges
# ---------------------------------------------------------------------------

def test_ring_badges_renders(tmp_path):
    _assert_render(
        "ring_badges",
        {"badges": [{"value": "NEW ROAD", "label": ""},
                    {"value": "2", "label": "DECKS"}]},
        "editorial", tmp_path, badges=2,
    )


def test_ring_badges_single(tmp_path):
    _assert_render(
        "ring_badges",
        {"badges": [{"value": "101", "label": "DAYS TRAPPED"}]},
        "history", tmp_path, badges=1,
    )


def test_ring_badges_refusal(tmp_path):
    _assert_refusal("ring_badges", {"badges": []}, "no_badges", tmp_path)
    _assert_refusal("ring_badges", {"badges": [{"label": "no value"}]}, "no_badges", tmp_path)


# ---------------------------------------------------------------------------
# cross_section
# ---------------------------------------------------------------------------

_LAYERS = [
    {"label": "River water", "kind": "water", "depth": 2.0, "color": [74, 116, 158]},
    {"label": "Riverbed", "depth": 1.2, "color": [122, 96, 62]},
    {"label": "Rock", "depth": 1.6, "color": [84, 66, 44]},
]


def test_cross_section_renders(tmp_path):
    meta = _assert_render(
        "cross_section",
        {"layers": _LAYERS,
         "markers": [{"kind": "ship", "x": 0.5, "layer": 0, "label": "HMS Amethyst"}],
         "headline": {"value": 101, "suffix": "", "label": "Days trapped"}},
        "history", tmp_path, layers=3, schematic=True,
    )
    assert meta["inputs"]["headline"] is True


def test_cross_section_refusal(tmp_path):
    _assert_refusal("cross_section", {"layers": []}, "no_layer_data", tmp_path)
    _assert_refusal("cross_section", {"layers": [{"depth": 0}]}, "no_layer_data", tmp_path)
    _assert_refusal("cross_section", {}, "no_layer_data", tmp_path)


# ---------------------------------------------------------------------------
# bar_chart
# ---------------------------------------------------------------------------

def test_bar_chart_renders(tmp_path):
    _assert_render(
        "bar_chart",
        {"items": [{"label": "USA", "value": 215, "display": "215M"},
                   {"label": "China", "value": 38, "display": "38M"},
                   {"label": "Japan", "value": 29, "display": "29M"}],
         "title": "Top canal users", "units": "Million tons"},
        "editorial", tmp_path, items=3, units=True,
    )


def test_bar_chart_refusal(tmp_path):
    _assert_refusal("bar_chart", {"items": []}, "no_numeric_items", tmp_path)
    _assert_refusal("bar_chart", {"items": [{"label": "x"}]}, "no_numeric_items", tmp_path)


# ---------------------------------------------------------------------------
# satellite_annotation
# ---------------------------------------------------------------------------

def test_satellite_annotation_renders(photos, tmp_path):
    _assert_render(
        "satellite_annotation",
        {"image": photos[0],
         "route": [[0.15, 0.6], [0.4, 0.45], [0.7, 0.5]],
         "label": {"text": "Puerto de Balboa", "at": [0.06, 0.1], "point": [0.7, 0.5]}},
        "editorial", tmp_path, route_points=3, label=True,
    )


def test_satellite_annotation_refusals(photos, tmp_path):
    _assert_refusal("satellite_annotation",
                    {"route": [[0.1, 0.1], [0.2, 0.2]]}, "no_image", tmp_path)
    _assert_refusal("satellite_annotation",
                    {"image": photos[0]}, "no_annotation", tmp_path)
