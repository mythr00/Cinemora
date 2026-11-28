"""Matplotlib chart templates (GitHub Skills Programme, Lane 1).

Pins: registration, the refusal discipline (insufficient data,
engine unavailable — refused before any render), the Director's
engine ordering (Manim -> matplotlib -> PIL), real house-format
renders probed off disk, and recipe regeneration through the
pipeline entry point.
"""

import pytest

from services import media_probe, visual_director
from services.motion_engine import mpl_templates, templates

SERIES = {
    "points": [
        {"label": "2019", "value": 12, "display": "12"},
        {"label": "2020", "value": 19, "display": "19"},
        {"label": "2021", "value": 31, "display": "31"},
        {"label": "2022", "value": 47, "display": "47"},
    ],
    "title": "Adoption", "units": "thousands",
}
COMPARISON = {
    "items": [
        {"label": "Alpha", "value": 40, "display": "40"},
        {"label": "Beta", "value": 65, "display": "65"},
        {"label": "Gamma", "value": 52, "display": "52"},
    ],
    "title": "Output by plant",
}


def test_templates_registered():
    assert templates.TEMPLATES["mpl_line_chart"] is mpl_templates.mpl_line_chart
    assert templates.TEMPLATES["mpl_bar_chart"] is mpl_templates.mpl_bar_chart


def test_refusals_are_typed_and_render_nothing(tmp_path):
    out = tmp_path / "x.mp4"
    rendered, meta = mpl_templates.mpl_line_chart(
        {"points": [{"label": "a", "value": 1}]}, None, out, duration=1.0
    )
    assert rendered is None and meta["reason"] == "insufficient_points"
    rendered, meta = mpl_templates.mpl_bar_chart(
        {"items": []}, None, out, duration=1.0
    )
    assert rendered is None and meta["reason"] == "insufficient_items"
    assert not out.exists()


def test_unavailable_engine_refuses_before_render(monkeypatch, tmp_path):
    monkeypatch.setattr(mpl_templates, "mpl_available", lambda: False)
    rendered, meta = mpl_templates.mpl_line_chart(
        SERIES, None, tmp_path / "x.mp4", duration=1.0
    )
    assert rendered is None and meta["reason"] == "matplotlib_unavailable"


def test_two_point_series_renders_here_though_manim_refuses(tmp_path):
    # The division of labour: Manim refuses under 3 points; the
    # matplotlib engine takes the 2-point series.
    data = {"points": SERIES["points"][:2], "title": "Two points"}
    rendered, meta = mpl_templates.mpl_line_chart(
        data, None, tmp_path / "two.mp4", duration=1.0
    )
    assert rendered is not None and meta["engine"] == "matplotlib"


def _candidates(text):
    outcome = {"text": text, "scenario": {}, "scene_photo_count": 0}
    candidates, _affinities = visual_director._template_candidates(
        {}, outcome, None)
    return candidates


def test_director_orders_mpl_between_manim_and_pil():
    candidates = _candidates(
        "Exports rose from 120 tonnes in 1901 to 340 tonnes, "
        "then 560 tonnes and finally 910 tonnes by 1930.")
    assert candidates.index("manim_line_chart") < candidates.index("mpl_line_chart")
    assert candidates.index("mpl_line_chart") < candidates.index("line_chart")
    candidates = _candidates(
        "Output compared across the three plants this year, "
        "with Alpha at 40, Beta at 65 and Gamma at 52 units.")
    assert candidates.index("manim_bar_chart") < candidates.index("mpl_bar_chart")
    assert candidates.index("mpl_bar_chart") < candidates.index("bar_chart")


@pytest.mark.parametrize("template,data", [
    ("mpl_line_chart", SERIES),
    ("mpl_bar_chart", COMPARISON),
])
def test_real_renders_match_house_format(tmp_path, template, data):
    out = tmp_path / f"{template}.mp4"
    rendered, meta = templates.TEMPLATES[template](
        data, None, out, duration=2.0, sentence_index=3, scene_number=1
    )
    assert rendered is not None, meta
    info = media_probe.probe_video(out)
    assert info["width"] == 1920 and info["height"] == 1080
    assert abs(info["fps"] - 30.0) < 0.6
    assert abs(info["duration_seconds"] - 2.0) < 0.25


def test_recipe_regeneration_renders(tmp_path):
    from services.graphics_pipeline import render_graphic_from_recipe

    recipe = {
        "family": "motion_engine", "template": "mpl_bar_chart",
        "inputs": COMPARISON, "style": None, "duration": 2.0,
    }
    out = tmp_path / "regen.mp4"
    rendered, meta = render_graphic_from_recipe(recipe, out)
    assert rendered is not None, meta
    info = media_probe.probe_video(out)
    assert info["width"] == 1920 and info["height"] == 1080


# ---------------------------------------------------------------------------
# Animation Studio Expansion (mission §9): chart source-line
# consistency — the recorded-data contract (units + source) now
# binds this engine exactly as it binds the PIL family.
# ---------------------------------------------------------------------------

def test_recorded_series_without_source_refuses_typed(tmp_path):
    data = dict(SERIES, recorded=True)
    rendered, meta = mpl_templates.mpl_line_chart(
        data, None, tmp_path / "x.mp4", duration=1.0
    )
    assert rendered is None and meta["reason"] == "chart_source_missing"


def test_recorded_series_without_units_refuses_typed(tmp_path):
    data = dict(SERIES, recorded=True, source="Example Bureau")
    del data["units"]
    rendered, meta = mpl_templates.mpl_line_chart(
        data, None, tmp_path / "x.mp4", duration=1.0
    )
    assert rendered is None and meta["reason"] == "chart_units_missing"


def test_recorded_comparison_without_source_refuses_typed(tmp_path):
    data = dict(COMPARISON, recorded=True, units="units")
    rendered, meta = mpl_templates.mpl_bar_chart(
        data, None, tmp_path / "x.mp4", duration=1.0
    )
    assert rendered is None and meta["reason"] == "chart_source_missing"


def test_recorded_series_with_source_renders_house_format(tmp_path):
    data = dict(SERIES, recorded=True, source="Example Bureau")
    out = tmp_path / "recorded.mp4"
    rendered, meta = mpl_templates.mpl_line_chart(
        data, None, out, duration=1.0
    )
    assert rendered is not None
    info = media_probe.probe_video(out)
    assert info["width"] == 1920 and info["height"] == 1080


def test_source_line_text_wording_and_estimated_label():
    assert mpl_templates._source_line({}) == ""
    assert mpl_templates._source_line({"source": "Example Bureau"}) == \
        "Source — Example Bureau"
    assert mpl_templates._source_line(
        {"source": "Example Bureau", "estimated": True}
    ) == "Source — Example Bureau · estimated"
