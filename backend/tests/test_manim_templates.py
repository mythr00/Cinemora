"""GitHub Skills Programme (2026-11-25): the Manim render engine
behind the Motion Engine template contract.

Pins the integration, not just the package:
- both templates are registered in the ONE registry the graphics
  chain and the recipe regenerator look up;
- the recorded-evidence refusals fire BEFORE any render is spent
  (small payloads stay on the PIL engine) and the unavailable
  engine refuses with its typed reason, so the pipeline's
  fall-through to the PIL candidates is exercised by construction;
- the Director proposes the Manim treatment first for recorded
  data beats (series / comparison) — the "which animation for
  which scene" decision — with the PIL chart retained behind it;
- when Manim IS available, a real render lands in the house
  format (1920x1080, 30 fps, exact duration) and a production
  recipe regenerates through graphics_pipeline's own entry point.
"""

import pytest

from services import visual_director
from services.media_probe import probe_video
from services.motion_engine import manim_templates
from services.motion_engine import templates as engine_templates

SERIES = {
    "points": [
        {"label": "1900", "value": 12, "display": "12"},
        {"label": "1910", "value": 28, "display": "28"},
        {"label": "1920", "value": 47, "display": "47"},
        {"label": "1930", "value": 92, "display": "92"},
    ],
    "title": "Atlantic crossings",
    "unit": "ships",
}

COMPARISON = {
    "items": [
        {"label": "Design load", "value": 100, "display": "100"},
        {"label": "Actual load", "value": 200, "display": "200"},
        {"label": "Failure point", "value": 260, "display": "260"},
    ],
    "title": "Walkway loads",
}

requires_manim = pytest.mark.skipif(
    not manim_templates.manim_available(),
    reason="manim not available on this host",
)


# ---------------------------------------------------------------------------
# Registry + refusal discipline
# ---------------------------------------------------------------------------

def test_manim_templates_registered():
    for name in ("manim_line_chart", "manim_bar_chart"):
        assert engine_templates.TEMPLATES[name] is (
            manim_templates.MANIM_TEMPLATES[name])


def test_line_chart_refuses_insufficient_points(tmp_path):
    data = {"points": SERIES["points"][:2], "title": "Too small"}
    rendered, meta = manim_templates.manim_line_chart(
        data, "editorial", tmp_path / "x.mp4", duration=3.0)
    assert rendered is None
    assert meta["reason"] == "insufficient_points"
    assert not (tmp_path / "x.mp4").exists()


def test_bar_chart_refuses_insufficient_items(tmp_path):
    data = {"items": COMPARISON["items"][:2], "title": "Too small"}
    rendered, meta = manim_templates.manim_bar_chart(
        data, "editorial", tmp_path / "x.mp4", duration=3.0)
    assert rendered is None
    assert meta["reason"] == "insufficient_items"
    assert not (tmp_path / "x.mp4").exists()


def test_refuses_with_typed_reason_when_engine_unavailable(
        tmp_path, monkeypatch):
    monkeypatch.setattr(manim_templates, "manim_available", lambda: False)
    rendered, meta = manim_templates.manim_line_chart(
        SERIES, "editorial", tmp_path / "x.mp4", duration=3.0)
    assert rendered is None
    assert meta["reason"] == "manim_unavailable"
    rendered, meta = manim_templates.manim_bar_chart(
        COMPARISON, "editorial", tmp_path / "y.mp4", duration=3.0)
    assert rendered is None
    assert meta["reason"] == "manim_unavailable"


def test_templates_refuse_empty_data_with_typed_reasons(tmp_path):
    rendered, meta = manim_templates.manim_line_chart(
        {}, "editorial", tmp_path / "x.mp4", duration=3.0)
    assert rendered is None and meta["reason"] == "insufficient_points"
    rendered, meta = manim_templates.manim_bar_chart(
        {}, "editorial", tmp_path / "y.mp4", duration=3.0)
    assert rendered is None and meta["reason"] == "insufficient_items"


# ---------------------------------------------------------------------------
# Director routing — which animation for which scene
# ---------------------------------------------------------------------------

def _candidates(text, scenario=None):
    outcome = {"text": text, "scenario": scenario or {},
               "scene_photo_count": 0}
    candidates, _affinities = visual_director._template_candidates(
        {}, outcome, None)
    return candidates


def test_director_proposes_manim_line_first_for_series():
    candidates = _candidates(
        "Exports rose from 120 tonnes in 1901 to 340 tonnes, "
        "then 560 tonnes and finally 910 tonnes by 1930.")
    assert "manim_line_chart" in candidates
    assert "line_chart" in candidates
    assert candidates.index("manim_line_chart") < candidates.index(
        "line_chart")


def test_director_proposes_manim_bar_first_for_comparison():
    candidates = _candidates(
        "The bridge carried 200 vehicles a day compared with "
        "450 on the old ferry route and 90 on the rail link.")
    assert "manim_bar_chart" in candidates
    assert "bar_chart" in candidates
    assert candidates.index("manim_bar_chart") < candidates.index(
        "bar_chart")


def test_director_does_not_propose_manim_without_data_cues():
    candidates = _candidates(
        "The harbour slept under a grey sky while gulls circled "
        "the empty pier.")
    assert "manim_line_chart" not in candidates
    assert "manim_bar_chart" not in candidates


# ---------------------------------------------------------------------------
# Real renders (engine available) — house format + recipe regeneration
# ---------------------------------------------------------------------------

def _assert_house_format(path, duration):
    probe = probe_video(path)
    assert probe is not None
    assert probe["width"] == 1920
    assert probe["height"] == 1080
    assert abs(float(probe["fps"]) - 30.0) < 0.6
    assert abs(float(probe["duration_seconds"]) - duration) < 0.25


@requires_manim
def test_line_chart_renders_house_format(tmp_path):
    out = tmp_path / "line.mp4"
    rendered, meta = manim_templates.manim_line_chart(
        SERIES, "editorial", out, duration=3.0,
        sentence_index=0, scene_number=1)
    assert rendered == str(out)
    assert meta["generator"] == "motion_engine"
    assert meta["template"] == "manim_line_chart"
    assert meta["engine"] == "manim"
    assert meta["generated"] is True
    _assert_house_format(out, 3.0)


@requires_manim
def test_bar_chart_renders_house_format(tmp_path):
    out = tmp_path / "bar.mp4"
    rendered, meta = manim_templates.manim_bar_chart(
        COMPARISON, "history", out, duration=3.0,
        sentence_index=1, scene_number=2)
    assert rendered == str(out)
    assert meta["template"] == "manim_bar_chart"
    assert meta["engine"] == "manim"
    _assert_house_format(out, 3.0)


@requires_manim
def test_recipe_regeneration_renders_through_pipeline(tmp_path):
    from services import graphics_pipeline

    recipe = {
        "family": "motion_engine",
        "template": "manim_line_chart",
        "inputs": SERIES,
        "style": "editorial",
        "duration": 3.0,
        "sentence_index": 0,
        "scene_number": 1,
    }
    out = tmp_path / "regen.mp4"
    rendered, meta = graphics_pipeline.render_graphic_from_recipe(
        recipe, out)
    assert rendered == str(out)
    assert meta["engine"] == "manim"
    _assert_house_format(out, 3.0)
