"""Animation Studio Expansion — the web render engine
(mission §5): Three.js globe, GSAP kinetic type, Lottie route
through headless Chromium.

Pins: registration, recorded-data refusals (unresolvable
places are never invented; the kinetic gate is the short
quoted passage only), the typed unavailable path, the Lottie
document generator's shape, Director candidacy, and real
renders (skip-gated on the engine being installed).
"""

import pytest

from services import visual_director
from services.media_probe import probe_video
from services.motion_engine import templates, web_templates

requires_web = pytest.mark.skipif(
    not web_templates.web_engine_available(),
    reason="web engine (node + node_modules) not available")


@pytest.fixture(autouse=True)
def _fresh_probe():
    web_templates._probe.cache_clear()
    yield
    web_templates._probe.cache_clear()


PLACES = [
    {"name": "London", "lon": -0.12, "lat": 51.5},
    {"name": "New York", "lon": -74.0, "lat": 40.7},
]


def test_web_templates_registered():
    assert templates.TEMPLATES["three_globe"] is \
        web_templates.three_globe
    assert templates.TEMPLATES["gsap_kinetic_type"] is \
        web_templates.gsap_kinetic_type
    assert templates.TEMPLATES["lottie_route"] is \
        web_templates.lottie_route


def test_globe_refuses_unresolvable_places(tmp_path):
    rendered, meta = web_templates.three_globe(
        {"places": [{"name": "Atlantis"}]}, None,
        tmp_path / "x.mp4", duration=1.0)
    assert rendered is None and meta["reason"] == "globe_places_missing"
    rendered, meta = web_templates.three_globe(
        {}, None, tmp_path / "x.mp4", duration=1.0)
    assert rendered is None and meta["reason"] == "globe_places_missing"


def test_lottie_refuses_below_two_places(tmp_path):
    rendered, meta = web_templates.lottie_route(
        {"places": [PLACES[0]]}, None, tmp_path / "x.mp4",
        duration=1.0)
    assert rendered is None
    assert meta["reason"] == "lottie_route_places_missing"


def test_kinetic_refuses_long_or_missing_text(tmp_path):
    rendered, meta = web_templates.gsap_kinetic_type(
        {}, None, tmp_path / "x.mp4", duration=1.0)
    assert rendered is None and meta["reason"] == "kinetic_text_missing"
    rendered, meta = web_templates.gsap_kinetic_type(
        {"text": "word " * 40}, None, tmp_path / "x.mp4",
        duration=1.0)
    assert rendered is None and meta["reason"] == "kinetic_text_missing"


def test_unavailable_engine_refuses_typed(tmp_path, monkeypatch):
    monkeypatch.setenv("CINEMORA_WEB_ENABLED", "0")
    web_templates._probe.cache_clear()
    rendered, meta = web_templates.three_globe(
        {"places": PLACES}, None, tmp_path / "x.mp4", duration=1.0)
    assert rendered is None
    assert meta["reason"] == "web_engine_unavailable"


def test_lottie_document_shape():
    colors = {"bg": "#14120f", "surface": "#e8e2d4",
              "text": "#f2ede1", "muted": "#8a8378",
              "accent": "#b3412e", "accent2": "#c8a24a"}
    doc = web_templates._lottie_route_document(
        [(100.0, 500.0), (600.0, 300.0), (1100.0, 420.0)],
        colors, 3.0)
    assert doc["fr"] == 30 and doc["op"] == 90
    assert doc["w"] == web_templates._W
    names = [layer["nm"] for layer in doc["layers"]]
    assert names == ["route-draw", "route-underlay",
                     "marker-start", "marker-end", "pulse"]
    inds = [layer["ind"] for layer in doc["layers"]]
    assert len(set(inds)) == len(inds)


def _candidates(text, scenario=None, outcome_extra=None):
    outcome = {"text": text, "scenario": scenario or {},
               "scene_photo_count": 0}
    outcome.update(outcome_extra or {})
    candidates, _affinities = visual_director._template_candidates(
        {}, outcome, None)
    return candidates


def test_director_proposes_web_treatments_on_records():
    two = _candidates(
        "The ship sailed from London to New York.",
        scenario={"places": ["London", "New York"]})
    assert "three_globe" in two and "lottie_route" in two
    one = _candidates(
        "The ship reached London.",
        scenario={"places": ["London"]})
    assert "three_globe" in one and "lottie_route" not in one
    quoted = _candidates('He wrote "we shall return" in the log.')
    assert "gsap_kinetic_type" in quoted
    plain = _candidates("The harbour slept quietly.")
    assert "three_globe" not in plain
    assert "gsap_kinetic_type" not in plain


def _assert_house_format(path, duration):
    probe = probe_video(path)
    assert probe is not None
    assert probe["width"] == 1920
    assert probe["height"] == 1080
    assert abs(float(probe["fps"]) - 30.0) < 0.6
    assert abs(float(probe["duration_seconds"]) - duration) < 0.25


@requires_web
def test_real_globe_render(tmp_path):
    out = tmp_path / "globe.mp4"
    rendered, meta = web_templates.three_globe(
        {"places": PLACES}, None, out, duration=2.0)
    assert rendered is not None, meta
    _assert_house_format(out, 2.0)
    assert meta["engine"] == "web"
    assert meta["render_stats"]["frames_rendered"] == 60


@requires_web
def test_real_kinetic_render(tmp_path):
    out = tmp_path / "kinetic.mp4"
    rendered, meta = web_templates.gsap_kinetic_type(
        {"text": "We shall return", "attribution": "The Log"},
        None, out, duration=2.0)
    assert rendered is not None, meta
    _assert_house_format(out, 2.0)


@requires_web
def test_real_lottie_render(tmp_path):
    out = tmp_path / "lottie.mp4"
    rendered, meta = web_templates.lottie_route(
        {"places": PLACES}, None, out, duration=2.0)
    assert rendered is not None, meta
    _assert_house_format(out, 2.0)
    assert meta["inputs"]["lottie_layers"] == 5
