"""Phase 6 — composition engine (AM-011), six themes and the
motion registry (AM-024).

Composition rules pinned: layers are validated explicitly (an
out-of-bounds layer is rejected with named failures, never
rendered, never silently dropped), a lower-third-over-footage
composition renders through real ffmpeg with the layer recorded,
and the Director's decisions stamp onto timeline items by
(scene_number, sentence_index) using the item's own duration.
"""

import subprocess
from pathlib import Path

from services import composition
from services.motion_engine import motion_registry, themes


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_validation_rejects_layer_ending_after_item():
    layer = composition.lower_third_layer(
        "Ocean Parkway", start=0.0, end=9.5
    )
    failures = composition.validate_layer(layer, item_duration=4.0)
    assert "layer_ends_after_item" in failures


def test_validation_rejects_out_of_frame_layer():
    layer = composition.lower_third_layer("Label", start=0.0, end=2.0)
    layer["position"] = {"x": 1800, "y": 900, "w": 400, "h": 300}
    failures = composition.validate_layer(layer, item_duration=4.0)
    assert "layer_outside_frame" in failures


def test_validation_rejects_unknown_animation():
    layer = composition.lower_third_layer("Label", start=0.0, end=2.0)
    layer["animation"] = {"entrance": "cartwheel", "exit": "fade_out"}
    failures = composition.validate_layer(layer, item_duration=4.0)
    assert any(
        f.startswith("unknown_entrance_animation") for f in failures
    )


def test_validation_accepts_well_formed_lower_third():
    layer = composition.lower_third_layer(
        "Ocean Parkway", "Long Island", start=0.0, duration=4.0
    )
    assert composition.validate_layer(layer, item_duration=4.0) == []


def test_footage_window_uses_recorded_window():
    layer = composition.footage_window_layer(
        "/media/clip.mp4", clip_start=12.5, clip_end=18.0, duration=5.5,
    )
    assert layer["source"]["clip_start"] == 12.5
    assert layer["source"]["clip_end"] == 18.0
    assert composition.validate_layer(layer, item_duration=5.5) == []
    empty = composition.footage_window_layer(
        "/media/clip.mp4", clip_start=18.0, clip_end=18.0, duration=5.5,
    )
    assert "media_layer_window_empty" in composition.validate_layer(
        empty, item_duration=5.5
    )


def test_split_screen_layer_is_not_renderable():
    # PHASE 8 SUPERSESSION (2026-11-21): Phase 6 shipped
    # split_screen as constructor + validation only, marked
    # renderable: False, and this test pinned valid_layers == [].
    # Phase 8 wires the render path (AM-011 completion), so a
    # valid split_screen layer is now renderable like any other;
    # the render itself is pinned in test_phase8_composition.py.
    layer = composition.split_screen_layer(
        "/media/clip.mp4", clip_start=0.0, clip_end=4.0, duration=4.0,
    )
    plan = {"pattern": "split_screen", "layers": [layer]}
    composition.validate_composition(plan, 4.0)
    assert plan["layers"][0]["validation"]["passed"] is True
    assert composition.valid_layers(plan) == [layer]


# ---------------------------------------------------------------------------
# Real render: lower third over footage
# ---------------------------------------------------------------------------

def _make_base_clip(path, seconds=2):
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"color=c=navy:s=1920x1080:r=30:d={seconds}",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(path),
        ],
        check=True, capture_output=True,
    )
    return path


def test_lower_third_composition_renders(tmp_path):
    from services.media_probe import probe_video

    base = _make_base_clip(tmp_path / "base.mp4", seconds=2)
    layer = composition.lower_third_layer(
        "Ocean Parkway", "Long Island, New York",
        start=0.0, duration=2.0, theme="investigative",
    )
    plan = {"pattern": "lower_third_card", "layers": [layer]}
    composition.validate_composition(plan, 2.0)
    out = tmp_path / "composed.mp4"
    ok, record = composition.apply_composition(
        base, plan, out, tmp_path / "work",
    )
    assert ok is True
    assert record["applied"][0]["kind"] == "lower_third_card"
    assert record["failed"] == []
    probe = probe_video(out)
    assert probe is not None
    assert abs(probe["duration_seconds"] - 2.0) < 0.35
    # The base clip is untouched by a successful composition (the
    # composed result is a NEW file).
    assert base.exists()


def test_failed_layer_keeps_base_and_records(tmp_path):
    base = _make_base_clip(tmp_path / "base.mp4", seconds=2)
    layer = composition.lower_third_layer(
        "Label", start=0.0, duration=2.0,
    )
    layer["source"]["label"] = ""  # validation must refuse it
    plan = {"pattern": "lower_third_card", "layers": [layer]}
    composition.validate_composition(plan, 2.0)
    out = tmp_path / "composed.mp4"
    ok, record = composition.apply_composition(
        base, plan, out, tmp_path / "work",
    )
    assert ok is False
    assert record["reason"] == "no_valid_layers"
    assert not out.exists()
    assert base.exists()


# ---------------------------------------------------------------------------
# Director decisions -> timeline items
# ---------------------------------------------------------------------------

def test_apply_decisions_stamps_matching_items_only():
    plan_v2 = {
        "version": 2,
        "theme": "investigative",
        "sentences": [
            {
                "sentence_number": 1, "scene_number": 1,
                "sentence_index": 0,
                "decision": {
                    "treatment": "footage",
                    "composition": {
                        "pattern": "lower_third_card",
                        "label": "Ocean Parkway",
                        "sublabel": "Long Island",
                        "reason": "scene_entry_label",
                    },
                },
            },
            {
                "sentence_number": 2, "scene_number": 1,
                "sentence_index": 1,
                "decision": {"treatment": "footage"},
            },
        ],
    }
    timeline = {
        "scenes": [
            {
                "scene_number": 1,
                "items": [
                    {"sentence_index": 0, "duration": 4.0,
                     "file_path": "/media/a.mp4"},
                    {"sentence_index": 1, "duration": 3.0,
                     "file_path": "/media/b.mp4"},
                ],
            },
        ],
    }
    stamped = composition.apply_decisions_to_timeline(timeline, plan_v2)
    assert stamped == 1
    first, second = timeline["scenes"][0]["items"]
    layer = first["composition"]["layers"][0]
    assert layer["kind"] == "lower_third_card"
    assert layer["source"]["label"] == "Ocean Parkway"
    assert layer["source"]["theme"] == "investigative"
    assert layer["end"] == 4.0  # the item's own recorded duration
    assert layer["validation"]["passed"] is True
    assert "composition" not in second


# ---------------------------------------------------------------------------
# Themes + registry
# ---------------------------------------------------------------------------

def test_six_themes_exist_and_legacy_pack_values_map():
    assert set(themes.THEMES) == {
        "editorial_dark", "documentary_clean", "archival",
        "cinematic_data", "technical_blueprint", "investigative",
    }
    assert themes.resolve_theme("editorial")["name"] == "editorial_dark"
    assert themes.resolve_theme("history")["name"] == "archival"
    assert themes.resolve_theme("true_crime")["name"] == "investigative"
    assert themes.resolve_theme("cinematic_data")["name"] == "cinematic_data"
    assert themes.resolve_theme("nonsense") is None
    pack_theme = themes.theme_for_pack({"visual_theme": "history"})
    assert pack_theme["name"] == "archival"
    assert themes.theme_for_pack(None) is None
    assert themes.theme_for_pack({}) is None


def test_theme_tokens_are_engine_ready():
    from services.motion_engine.styles import resolve_style

    for name, theme in themes.THEMES.items():
        style_name, tokens = resolve_style(theme)
        assert style_name == name
        for key in ("bg", "surface", "text", "muted", "accent",
                    "accent2", "font", "font_bold"):
            assert key in tokens


def test_motion_registry_vocabulary():
    names = motion_registry.animation_names()
    assert "rise_in" in names["entrances"]
    assert "fade_out" in names["exits"]
    assert motion_registry.easing_function("ease_out") is not None
    assert motion_registry.easing_function("bogus") is None
    assert motion_registry.duration_seconds("short") == 0.4
    # Every entrance/exit references registered easings + durations.
    for entry in (
        list(motion_registry.ENTRANCES.values())
        + list(motion_registry.EXITS.values())
    ):
        assert entry["easing"] in motion_registry.EASINGS
        assert entry["duration"] in motion_registry.DURATIONS
