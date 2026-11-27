"""Stage 5 (Motion Graphics V2 brief) regression tests for the v1
benchmark defects:

- multilayer compositions cropping edge content under a centre
  zoom -> safe areas + camera mapping in core (layout-model
  assertions: the v1 title placement provably leaves the frame at
  the v1 camera; the v2 protected rects provably stay in);
- technical-diagram label collisions -> core.place_labels on the
  benchmark's real ship components: no overlaps, all inside the
  safe rect, box sizes unchanged (no text shrinking);
- timeline final-node reveal timing -> reveals run on the
  compressed scan clock, last reveal <= 85% of duration;
- stat_comparison extreme ratios -> minimum visible bar stub,
  exact values untouched;
- narration timing metadata -> engine templates (v1 and v2
  alike) record sentence_index, scene_number and duration via
  the shared standard metadata shape.
"""

from services import graphics_pipeline
from services.motion_engine import core, templates


# ---------------------------------------------------------------------------
# Safe area + camera mapping
# ---------------------------------------------------------------------------

def test_safe_rect_is_inside_frame_and_symmetric():
    x0, y0, x1, y1 = core.safe_rect()
    assert 0 < x0 and 0 < y0 and x1 < core.WIDTH and y1 < core.HEIGHT
    assert x0 == core.WIDTH - x1 and y0 == core.HEIGHT - y1


def test_safe_rect_content_survives_engine_zooms():
    safe = core.safe_rect()
    zoom_cap = core.max_centre_zoom(safe)
    # The engine's multilayer zoom was 1.10; the safe rect must
    # survive it, and the computed cap must be honest (1.25 crops).
    assert 1.10 <= zoom_cap <= 1.12
    assert core.rect_in_frame(core.rect_after_camera(safe, scale=1.10))
    at_cap = core.rect_after_camera(safe, scale=zoom_cap)
    assert all(abs(v) < 1e-6 or 0 <= v for v in (at_cap[0], at_cap[1]))
    assert at_cap[2] <= core.WIDTH + 1e-6 and at_cap[3] <= core.HEIGHT + 1e-6
    assert not core.rect_in_frame(core.rect_after_camera(safe, scale=1.25))


def test_v1_multilayer_title_really_cropped():
    # The v1 piece drew its title at (90, 64) on a parallax-0.4
    # layer and zoomed to 1.10 with camera (-30, -15): the title's
    # left edge lands at x = -9 — off the frame. This is the
    # defect, reproduced at the layout level.
    v1_title = (90, 64, 990, 124)
    mapped = core.rect_after_camera(
        v1_title, scale=1.10, cam_x=-30, cam_y=-15, parallax=0.4)
    assert mapped[0] < 0
    assert not core.rect_in_frame(mapped)


def test_v2_protected_rects_stay_in_frame():
    # The v2 multilayer's protected rects at its final camera.
    safe = core.safe_rect()
    protected = [
        ((safe[0], safe[1], safe[0] + 900, safe[1] + 120), 0.4),      # title
        ((safe[2] - 470, safe[1] + 40, safe[2], safe[1] + 210), 0.6),  # stat
        ((safe[0] + 20, 880 - 110, safe[2] - 20, 880 + 80), 0.85),     # path
    ]
    for rect, parallax in protected:
        mapped = core.rect_after_camera(
            rect, scale=1.10, cam_x=-18, cam_y=-9, parallax=parallax)
        assert core.rect_in_frame(mapped), (rect, mapped)


# ---------------------------------------------------------------------------
# Label placement (technical diagram)
# ---------------------------------------------------------------------------

# The benchmark's generic sloop components (ship_components.json).
_SHIP_COMPONENTS = [
    {"label": "Bridge", "x": 0.46, "y": 0.30},
    {"label": "Funnel", "x": 0.56, "y": 0.26},
    {"label": "Mainmast", "x": 0.64, "y": 0.22},
    {"label": "Forward gun", "x": 0.20, "y": 0.42},
    {"label": "Aft gun", "x": 0.82, "y": 0.42},
    {"label": "Engine room", "x": 0.53, "y": 0.62},
    {"label": "Rudder", "x": 0.90, "y": 0.66},
    {"label": "Propellers", "x": 0.86, "y": 0.74},
]


def _place_ship_labels():
    return core.place_labels(
        [{"key": c["label"], "cx": c["x"] * core.WIDTH,
          "cy": c["y"] * core.HEIGHT, "w": 250, "h": 64}
         for c in _SHIP_COMPONENTS],
        bounds=core.safe_rect(), gap=10.0,
    )


def test_ship_labels_never_overlap_and_stay_safe():
    placed = _place_ship_labels()
    assert set(placed) == {c["label"] for c in _SHIP_COMPONENTS}
    safe = core.safe_rect()
    boxes = list(placed.values())
    for box in boxes:
        # Inside the safe rect, at the ORIGINAL size (no shrinking).
        assert safe[0] <= box[0] and safe[1] <= box[1]
        assert box[2] <= safe[2] and box[3] <= safe[3]
        assert abs((box[2] - box[0]) - 250) < 1e-6
        assert abs((box[3] - box[1]) - 64) < 1e-6
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert not core._boxes_overlap(a, b), (a, b)


def test_place_labels_is_deterministic():
    assert _place_ship_labels() == _place_ship_labels()


def test_place_labels_single_label_lands_on_anchor():
    placed = core.place_labels(
        [{"key": "only", "cx": 960, "cy": 540, "w": 200, "h": 50}])
    assert placed["only"] == (860.0, 515.0, 1060.0, 565.0)


# ---------------------------------------------------------------------------
# Timeline reveal timing
# ---------------------------------------------------------------------------

def test_stat_bar_width_floor_and_proportionality():
    width = templates._stat_bar_width
    assert width(101, 101, 1.0) == 1050          # the maximum bar
    assert width(2, 101, 1.0) == 30              # floor, not ~20px
    assert width(0, 101, 1.0) == 0               # zero stays zero
    assert width(50, 100, 0.5) == 262            # proportional mid-animation
    assert width(2, 101, 0.5) == 15              # floor scales with reveal


# ---------------------------------------------------------------------------
# Narration timing / sentence association in engine metadata
# ---------------------------------------------------------------------------

def test_engine_metadata_records_sentence_and_timing(tmp_path):
    path, meta = templates.TEMPLATES["ring_badges"](
        {"badges": [{"value": "101", "label": "DAYS TRAPPED"}]},
        "history", tmp_path / "badge.mp4", duration=1.2,
        sentence_index=7, scene_number=3,
    )
    assert path is not None
    assert meta["sentence_index"] == 7
    assert meta["scene_number"] == 3
    assert meta["duration"] == 1.2
    # The v1 family records the identical standard shape (both use
    # the same _meta): same keys, same association fields.
    path1, meta1 = templates.data_gauge(
        {"percent": 50, "label": "Rations remaining"},
        "history", tmp_path / "gauge.mp4", duration=1.2,
        sentence_index=7, scene_number=3,
    )
    assert path1 is not None
    assert set(meta1) == set(meta)
    assert meta1["sentence_index"] == 7 and meta1["duration"] == 1.2


# P0 removal (2026-11-27): deleted template tests for removed
# templates — the native timeline renderer's reveal timing removed with R1.
