"""Animation Studio Expansion — the Blender 3D department
(mission §4): registration, recorded-contract refusals, the
typed unavailable path, the subprocess+conform chain against a
fake Blender binary, Director candidacy on recorded contracts
only, and real Cycles renders (skip-gated on availability).

The fake binary honours the real one's contract: `--version`
prints a version line; a render run reads the payload env vars
and writes the PNG sequence. It proves the engine plumbing
without paying Cycles render time in the unit suite; the real
renders below prove the scenes themselves.
"""

import json
import os
import stat
import sys
import textwrap

import pytest

from services import visual_director
from services.media_probe import probe_video
from services.motion_engine import blender_templates, templates

requires_blender = pytest.mark.skipif(
    not blender_templates.blender_available(),
    reason="Blender binary not available on this host")


@pytest.fixture(autouse=True)
def _fresh_probe():
    blender_templates._probe.cache_clear()
    yield
    blender_templates._probe.cache_clear()


PARTS = [
    {"name": "casing", "shape": "cylinder", "size": [1.6, 2.6],
     "position": [0, 0, 1.6], "material": "body"},
    {"name": "rotor", "shape": "cylinder", "size": [1.0, 1.2],
     "position": [0, 0, 1.6], "explode": [0, 0, 2.2],
     "material": "accent"},
]
GEARS = {"gears": [
    {"name": "driver", "radius": 1.8, "teeth": 18, "thickness": 0.5,
     "position": [-2.0, 0.0, 0.0], "speed": 12, "direction": 1},
    {"name": "driven", "radius": 1.2, "teeth": 12, "thickness": 0.5,
     "position": [1.05, 0.0, 0.0], "speed": 18, "direction": -1},
]}
GRID = [[round(0.5 + 0.4 * ((r - 3.5) ** 2 + (c - 3.5) ** 2) / 25.0, 3)
         for c in range(8)] for r in range(8)]


# ---------------------------------------------------------------------------
# Registration + contract refusals
# ---------------------------------------------------------------------------

def test_blender_templates_registered():
    for name in ("blender_exploded_view", "blender_mechanical",
                 "blender_building", "blender_terrain",
                 "blender_title_3d"):
        assert templates.TEMPLATES[name] is getattr(
            blender_templates, name)


def test_exploded_refuses_without_recorded_geometry(tmp_path):
    for bad in ({}, {"parts": []}, {"parts": [PARTS[0]]},
                {"parts": [{"name": "x"}, {"name": "y"}]}):
        rendered, meta = blender_templates.blender_exploded_view(
            bad, None, tmp_path / "x.mp4", duration=1.0)
        assert rendered is None
        assert meta["reason"] == "blender_geometry_missing"


def test_mechanical_refuses_without_gears(tmp_path):
    rendered, meta = blender_templates.blender_mechanical(
        {"gears": [{"name": "bad"}]}, None, tmp_path / "x.mp4",
        duration=1.0)
    assert rendered is None and meta["reason"] == "blender_gears_missing"


def test_building_refuses_without_structure(tmp_path):
    for bad in ({}, {"structure": {}}, {"structure": {"floors": 0}},
                {"structure": {"floors": 99}}):
        rendered, meta = blender_templates.blender_building(
            bad, None, tmp_path / "x.mp4", duration=1.0)
        assert rendered is None
        assert meta["reason"] == "blender_structure_missing"


def test_terrain_refuses_without_grid(tmp_path):
    ragged = {"terrain": {"grid": [[0.1] * 4, [0.2] * 3]}}
    for bad in ({}, {"terrain": {}}, {"terrain": {"grid": [[0.1]]}},
                ragged):
        rendered, meta = blender_templates.blender_terrain(
            bad, None, tmp_path / "x.mp4", duration=1.0)
        assert rendered is None
        assert meta["reason"] == "blender_terrain_missing"


def test_title_refuses_without_title(tmp_path):
    rendered, meta = blender_templates.blender_title_3d(
        {}, None, tmp_path / "x.mp4", duration=1.0)
    assert rendered is None and meta["reason"] == "no_title"


# ---------------------------------------------------------------------------
# Unavailable engine — typed refusal, never a crash
# ---------------------------------------------------------------------------

def test_unavailable_engine_refuses_typed(tmp_path, monkeypatch):
    monkeypatch.setenv("CINEMORA_BLENDER_BIN", "/nonexistent/blender")
    blender_templates._probe.cache_clear()
    assert not blender_templates.blender_available()
    rendered, meta = blender_templates.blender_exploded_view(
        {"parts": PARTS}, None, tmp_path / "x.mp4", duration=1.0)
    assert rendered is None and meta["reason"] == "blender_unavailable"


def test_disabled_engine_refuses_typed(tmp_path, monkeypatch):
    monkeypatch.setenv("CINEMORA_BLENDER_ENABLED", "0")
    blender_templates._probe.cache_clear()
    rendered, meta = blender_templates.blender_terrain(
        {"terrain": {"grid": GRID}}, None, tmp_path / "x.mp4",
        duration=1.0)
    assert rendered is None and meta["reason"] == "blender_unavailable"


# ---------------------------------------------------------------------------
# Subprocess chain against a fake Blender binary
# ---------------------------------------------------------------------------

_FAKE_BLENDER = textwrap.dedent("""
    import json, os, sys
    if "--version" in sys.argv:
        print("Blender 9.9.9")
        raise SystemExit(0)
    from PIL import Image
    with open(os.environ["CINEMORA_BLENDER_DATA"]) as fh:
        payload = json.load(fh)
    frames = max(1, round(payload["duration"] * payload["fps"]))
    out = os.environ["CINEMORA_BLENDER_OUT"]
    for i in range(1, frames + 1):
        Image.new("RGB", (payload["width"], payload["height"]),
                  (24, 22, 20)).save(
                      os.path.join(out, "frame_%04d.png" % i))
""")


def _fake_blender(tmp_path, monkeypatch):
    script = tmp_path / "fake-blender"
    script.write_text("#!" + sys.executable + "\n" + _FAKE_BLENDER)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CINEMORA_BLENDER_BIN", str(script))
    monkeypatch.delenv("CINEMORA_BLENDER_ENABLED", raising=False)
    blender_templates._probe.cache_clear()
    assert blender_templates.blender_available()
    assert blender_templates._blender_version() == "9.9.9"


def test_fake_binary_full_chain(tmp_path, monkeypatch):
    _fake_blender(tmp_path, monkeypatch)
    out = tmp_path / "exploded.mp4"
    rendered, meta = blender_templates.blender_exploded_view(
        {"parts": PARTS}, None, out, duration=2.0)
    assert rendered is not None, meta
    probe = probe_video(out)
    assert probe["width"] == 1920 and probe["height"] == 1080
    assert abs(float(probe["duration_seconds"]) - 2.0) < 0.25
    assert meta["engine"] == "blender"
    assert meta["visualisation"] == "illustrative"
    assert meta["render_stats"]["frames_rendered"] == 30


# ---------------------------------------------------------------------------
# Director candidacy — recorded contracts only
# ---------------------------------------------------------------------------

def _candidates(text, scenario=None, outcome_extra=None):
    outcome = {"text": text, "scenario": scenario or {},
               "scene_photo_count": 0}
    outcome.update(outcome_extra or {})
    candidates, _affinities = visual_director._template_candidates(
        {}, outcome, None)
    return candidates


def test_director_proposes_blender_only_on_contracts():
    plain = _candidates("The engine was opened for inspection.")
    for name in ("blender_exploded_view", "blender_mechanical",
                 "blender_building", "blender_terrain"):
        assert name not in plain
    assert "blender_exploded_view" in _candidates(
        "The engine was opened.", scenario={"geometry": PARTS})
    assert "blender_mechanical" in _candidates(
        "The gears turned.", scenario=GEARS)
    assert "blender_building" in _candidates(
        "The tower rose.", scenario={"structure": {"floors": 9}})
    assert "blender_terrain" in _candidates(
        "The valley flooded.", scenario={"terrain": {"grid": GRID}})


def test_director_proposes_blender_title_on_opening_beat():
    candidates = _candidates(
        "The harbour opened in 1897.", outcome_extra={"sentence_index": 0})
    assert "blender_title_3d" in candidates
    assert candidates.index("archival_title") < candidates.index(
        "blender_title_3d")


# ---------------------------------------------------------------------------
# Real renders (engine available) — house format + honesty stamps
# ---------------------------------------------------------------------------

def _assert_house_format(path, duration):
    probe = probe_video(path)
    assert probe is not None
    assert probe["width"] == 1920
    assert probe["height"] == 1080
    assert abs(float(probe["fps"]) - 30.0) < 0.6
    assert abs(float(probe["duration_seconds"]) - duration) < 0.25


@pytest.fixture()
def _small_render(monkeypatch):
    monkeypatch.setenv("CINEMORA_BLENDER_WIDTH", "640")
    monkeypatch.setenv("CINEMORA_BLENDER_HEIGHT", "360")
    monkeypatch.setenv("CINEMORA_BLENDER_SAMPLES", "8")


@requires_blender
def test_real_exploded_view_render(tmp_path, _small_render):
    out = tmp_path / "exploded.mp4"
    rendered, meta = blender_templates.blender_exploded_view(
        {"parts": PARTS}, None, out, duration=1.5)
    assert rendered is not None, meta
    _assert_house_format(out, 1.5)
    assert meta["blender_version"]
    assert meta["render_stats"]["frames_rendered"] >= 20


@requires_blender
def test_real_title_render(tmp_path, _small_render):
    out = tmp_path / "title.mp4"
    rendered, meta = blender_templates.blender_title_3d(
        {"title": "The Harbour", "kicker": "19th century",
         "date_label": "1897"}, None, out, duration=1.5)
    assert rendered is not None, meta
    _assert_house_format(out, 1.5)
