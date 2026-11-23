"""Stage 3E tests: Cinematic Motion Graphics Engine v1.

Each template renders a REAL MP4 at short duration (1.0-1.5s) and
the output is ffprobe-verified as 1920x1080 with positive duration.
Refusals are tested too: territory_map without polygons and
document_presentation without text must return (None, reason) —
the engine never invents geography or documents.
"""

import pytest
from PIL import Image

from services.motion_engine import core, templates
from services.motion_engine.core import probe_video

SHORT = 1.2


def _check(path):
    info = probe_video(path)
    assert info["width"] == 1920
    assert info["height"] == 1080
    assert info["duration"] > 0


def _photo(tmp_path, name="photo.jpg", colour=(90, 110, 140)):
    p = tmp_path / name
    Image.new("RGB", (800, 600), colour).save(p, quality=88)
    return str(p)


def test_core_easing_and_keyframes():
    assert core.ease_out(0.0) == 0.0 and core.ease_out(1.0) == 1.0
    assert core.ease_in_out(0.5) == pytest.approx(0.5)
    track = core.KeyframeTrack([(0.0, 0.0), (2.0, 100.0)])
    assert track.value_at(0.0) == 0.0
    assert track.value_at(2.0) == 100.0
    assert 0 < track.value_at(1.0) < 100


def test_territory_map_refuses_without_polygons(tmp_path):
    path, meta = templates.territory_map(
        {"regions": [], "title": "Nowhere"}, "editorial",
        tmp_path / "map.mp4", duration=SHORT,
    )
    assert path is None
    assert meta["reason"] == "no_polygon_data"


def test_territory_map_renders(tmp_path):
    data = {
        "regions": [{"points": [[0.2, 0.25], [0.7, 0.25], [0.75, 0.7], [0.25, 0.75]],
                     "label": "Test Region"}],
        "routes": [{"points": [[0.25, 0.6], [0.5, 0.45], [0.7, 0.35]], "label": "Route"}],
        "title": "Test Map",
    }
    path, meta = templates.territory_map(data, "editorial", tmp_path / "map.mp4",
                                          duration=SHORT, sentence_index=2, scene_number=1)
    _check(path)
    assert meta["generator"] == "motion_engine"
    assert meta["template"] == "territory_map"
    assert meta["generated"] is True
    assert meta["sentence_index"] == 2 and meta["scene_number"] == 1


def test_evidence_board_renders_with_generated_photo(tmp_path):
    photo = _photo(tmp_path)
    data = {"photos": [{"path": photo, "caption": "Stand-in still"},
                       {"path": str(tmp_path / "missing.jpg"), "caption": "gone"}],
            "title": "The Board", "labels": ["Exhibit A"]}
    path, meta = templates.evidence_board(data, "true_crime", tmp_path / "board.mp4",
                                           duration=SHORT)
    _check(path)
    assert meta["inputs"]["photos"] == 1  # missing file skipped


def test_evidence_board_refuses_with_no_valid_photos(tmp_path):
    path, meta = templates.evidence_board(
        {"photos": [{"path": str(tmp_path / "nope.jpg"), "caption": "x"}]},
        "history", tmp_path / "board.mp4", duration=SHORT,
    )
    assert path is None and meta["reason"] == "no_valid_photos"


def test_editorial_panel_renders(tmp_path):
    photo = _photo(tmp_path)
    data = {"photo": photo, "title": "A Decision",
            "options": ["Option one", "Option two"], "footer": "Source note"}
    path, meta = templates.editorial_panel(data, "editorial", tmp_path / "panel.mp4",
                                            duration=SHORT)
    _check(path)
    assert meta["template"] == "editorial_panel"


def test_data_gauge_renders(tmp_path):
    path, meta = templates.data_gauge(
        {"percent": 62, "label": "Crew below deck", "sublabel": "At the time"},
        "editorial", tmp_path / "gauge.mp4", duration=SHORT,
    )
    _check(path)
    assert meta["inputs"]["percent"] == 62


def test_data_gauge_refuses_without_percent(tmp_path):
    path, meta = templates.data_gauge(
        {"label": "x"}, "editorial", tmp_path / "gauge.mp4", duration=SHORT)
    assert path is None and meta["reason"] == "no_percent_value"


def test_outcome_path_renders(tmp_path):
    data = {"outcomes": [{"label": "Start", "outcome": "Began"},
                         {"label": "Middle", "outcome": "Turned"},
                         {"label": "End", "outcome": "Resolved"}],
            "title_left": "Before", "title_right": "After"}
    path, meta = templates.outcome_path(data, "true_crime", tmp_path / "path.mp4",
                                         duration=SHORT)
    _check(path)
    assert meta["inputs"]["outcomes"] == 3


def test_stat_comparison_renders(tmp_path):
    data = {"items": [{"label": "First", "value": 200, "display": "200"},
                      {"label": "Second", "value": 120, "display": "120"}],
            "title": "Comparison"}
    path, meta = templates.stat_comparison(data, "history", tmp_path / "cmp.mp4",
                                            duration=SHORT)
    _check(path)
    assert meta["inputs"]["items"] == 2


def test_document_presentation_renders_without_source_label(tmp_path):
    # PIN CHANGED (founder defect ruling 2026-11-23): this test
    # previously pinned the source label being rendered into the
    # document header. Source/provider labels must never be
    # burned into a graphic; the template now ignores a supplied
    # source_label entirely — it is neither drawn nor recorded
    # as a rendered input.
    data = {"title": "Signal", "source_label": "BENCHMARK RECONSTRUCTION",
            "date_label": "31 July 1949",
            "body": "Have rejoined the fleet south of Woosung. God save the King.",
            "highlight": "rejoined the fleet"}
    path, meta = templates.document_presentation(
        data, "history", tmp_path / "doc.mp4", duration=SHORT)
    _check(path)
    assert not meta["inputs"].get("source_label")
    assert meta["inputs"]["body_chars"] == len(data["body"])


def test_document_presentation_refuses_without_text(tmp_path):
    path, meta = templates.document_presentation(
        {"title": "Empty", "body": ""}, "history", tmp_path / "doc.mp4",
        duration=SHORT)
    assert path is None and meta["reason"] == "no_document_text"
