"""Gate 2 / Motion Graphics audit fixes (04-MOTION-GRAPHICS-AUDIT):

MG-07 — every photo fed into a graphic assembler passes the
        person-conflict check (the Container film's Eleanor
        Roosevelt photo under a Malcom McLean sentence is the
        pinned artefact).
MG-06-residual — photo-bearing templates/assemblers never render
        a raw caller title, file name or source label; only a
        designed identifier (place/date/name) or nothing.
MG-11 — engine graphics render AT the sentence slot's duration
        (no fixed 3.0s render left to a tpad freeze).
MG-04 — red_string_board declines when its nodes carry no
        labels (strings between unlabelled photos fabricate
        relationships).
MG-08 — series point labels are the quantity's own display,
        never the prose tail before the number.
MG-18 — not implemented: no shared canvas/compositor constant
        exists for the caption band (core.safe_rect/place_labels
        have no template consumers; every template hardcodes its
        geometry), so it is reported, not forced.
"""

import json
import subprocess

from PIL import Image, ImageChops

from services import graphics_pipeline
from services.motion_engine import templates as engine_templates
from services.motion_engine import templates_v3


ROOSEVELT_TITLE = (
    "File:Eleanor Roosevelt at Reverend Hayes Housing Project "
    "in Newark, New Jersey - NARA - 196043.jpg"
)
MCLEAN_TEXT = "Malcom McLean watched the ships loading at Newark."


def _photo(path, base=(70, 90, 130), block=(200, 170, 60)):
    img = Image.new("RGB", (800, 600), base)
    px = img.load()
    for y in range(600):
        for x in range(0, 800, 4):
            shade = int(30 * (y / 600))
            px[x, y] = tuple(min(255, c + shade) for c in base)
    for y in range(200, 400):
        for x in range(300, 560):
            px[x, y] = block
    img.save(path)
    return str(path)


def _manifest(entries):
    return {"version": 1, "project_id": "mg", "theme": None,
            "entries": entries}


def _entry(manifest_id, scene, index, number, text, templates,
           scenario=None):
    return {
        "manifest_id": manifest_id,
        "target": {
            "scene_number": scene, "sentence_index": index,
            "sentence_number": number,
        },
        "text": text,
        "scenario": scenario or {},
        "purpose": "coverage_fallback",
        "template_candidates": templates,
        "family_candidates": [],
        "theme": None,
        "fact_basis": "script_claim",
        "evidence_ref": None,
        "verified_data": None,
    }


def _container_data(tmp_path, images):
    return {
        "case_bible": {
            "title": "The Container",
            "timeline": [],
            "entities": [
                {"canonical": "Malcom McLean", "type": "person"},
                {"canonical": "Newark, New Jersey", "type": "place"},
            ],
        },
        "scenes": [{
            "scene_number": 1,
            "sentences": [
                {"text": MCLEAN_TEXT, "sentence_number": 1,
                 "locations": ["Newark"], "dates": ["1956"],
                 "entities": ["Malcom McLean"]},
                {"text": "The cranes kept working through the night.",
                 "sentence_number": 2},
            ],
        }],
        "processed_scenes": [{
            "scene": {"scene_number": 1, "sentences": []},
            "images": images,
            "videos": [],
        }],
    }


def _verified_image(path, title, **extra):
    record = {
        "verification": {"accepted": True},
        "path": path, "file_path": path,
        "provider": "wikimedia",
        "source_url": "https://example.org/x",
        "title": title, "type": "image",
    }
    record.update(extra)
    return record


def _probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def _frame(video_path, out_png, at):
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-ss", str(at),
         "-i", str(video_path), "-vframes", "1", str(out_png)],
        check=True,
    )
    return Image.open(out_png).convert("RGB")


def _mean_diff(a, b):
    diff = ImageChops.difference(
        a.resize((320, 180)), b.resize((320, 180)))
    px = list(diff.getdata())
    return sum(sum(c) for c in px) / (len(px) * 3)


# ---------------------------------------------------------------------------
# MG-07 — person-conflict check on every photo path
# ---------------------------------------------------------------------------

def test_mg07_conflict_check_names_the_artefact_person():
    bible = {"entities": [
        {"canonical": "Malcom McLean", "type": "person"}]}
    conflict = graphics_pipeline.photo_person_conflict(
        {"title": ROOSEVELT_TITLE}, MCLEAN_TEXT, bible)
    assert conflict == "Eleanor Roosevelt"


def test_mg07_parallax_refuses_the_roosevelt_photo(tmp_path):
    photo = _photo(tmp_path / "roosevelt.jpg")
    data = _container_data(
        tmp_path, [_verified_image(photo, ROOSEVELT_TITLE)])
    entry = _entry("mg_0701", 1, 0, 1, MCLEAN_TEXT,
                   ["photo_parallax"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    parallax = [a for a in assets
                if a.get("template") == "photo_parallax"]
    assert not parallax
    for asset in assets:
        inputs = json.dumps(asset.get("engine_inputs") or {})
        assert "Eleanor Roosevelt" not in inputs
        assert "196043" not in inputs


def test_mg07_document_presentation_refuses_conflicted_document(
        tmp_path):
    doc = _photo(tmp_path / "doc.jpg")
    data = _container_data(tmp_path, [_verified_image(
        doc, ROOSEVELT_TITLE, evidence_type="document")])
    entry = _entry("mg_0702", 1, 0, 1, MCLEAN_TEXT,
                   ["document_presentation"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    assert not [a for a in assets
                if a.get("template") == "document_presentation"]


# ---------------------------------------------------------------------------
# MG-06-residual — no raw caller title / file name / source label
# ---------------------------------------------------------------------------

def test_mg06_designed_text_drops_metadata_and_source_labels():
    clean = templates_v3._designed_text
    assert clean(ROOSEVELT_TITLE) == ""
    assert clean("SOURCE • WIKIMEDIA") == ""
    assert clean("container_ship_1956.jpg") == ""
    assert clean(
        "A very long descriptive archive title that runs well "
        "past any headline length a designer would set") == ""
    assert clean("Newark") == "Newark"
    assert clean("Malcom McLean") == "Malcom McLean"
    assert clean("") == ""


def test_mg06_newspaper_headlines_are_short_payloads_only(tmp_path):
    long_title = (
        "Eleanor Roosevelt at Reverend Hayes Housing Project "
        "in Newark New Jersey during the housing tour")
    images = [
        _verified_image(_photo(tmp_path / "a.jpg"), ROOSEVELT_TITLE),
        _verified_image(_photo(tmp_path / "b.jpg"), long_title),
        _verified_image(_photo(tmp_path / "c.jpg"),
                        "Dock workers strike"),
        _verified_image(_photo(tmp_path / "d.jpg"),
                        "Port traffic grows"),
    ]
    data = _container_data(tmp_path, images)
    entry = _entry("mg_0601", 1, 1, 2,
                   "The cranes kept working through the night.",
                   ["newspaper_montage"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    montage = [a for a in assets
               if a.get("template") == "newspaper_montage"]
    assert len(montage) == 1
    headlines = [
        article["headline"]
        for article in montage[0]["engine_inputs"]["articles"]
    ]
    assert headlines == ["Dock workers strike", "Port traffic grows"]
    blob = json.dumps(montage[0]["engine_inputs"])
    assert "File:" not in blob
    assert "Eleanor" not in blob
    assert "SOURCE" not in blob


# ---------------------------------------------------------------------------
# MG-04 — red_string_board requires labels for connected nodes
# ---------------------------------------------------------------------------

# P0 removal (2026-11-27): the two MG-04 template tests are
# deleted — red_string_board is removed completely, so there is
# no template left to refuse or render. The pipeline-level pin
# below survives: a directed board entry produces nothing.


def test_mg04_pipeline_declines_unlabelled_board(tmp_path):
    images = [
        _verified_image(_photo(tmp_path / "p1.jpg"), "First photo"),
        _verified_image(_photo(tmp_path / "p2.jpg"), "Second photo"),
    ]
    data = _container_data(tmp_path, images)
    entry = _entry("mg_0401", 1, 1, 2,
                   "The cranes kept working through the night.",
                   ["red_string_board"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    assert not [a for a in assets
                if a.get("template") == "red_string_board"]


# ---------------------------------------------------------------------------
# MG-08 — series labels are the quantity's own display
# ---------------------------------------------------------------------------

def test_mg08_series_labels_are_displays_not_prose_tails():
    points = graphics_pipeline._series_from_text(
        "The gantry crane, standing more than idle, loaded "
        "20 tonnes before dawn, 35 tonnes by noon and "
        "50 tonnes before dusk.")
    assert points is not None and len(points) == 3
    assert [p["display"] for p in points] == [
        "20 tonnes", "35 tonnes", "50 tonnes"]
    for point in points:
        assert point["label"] == point["display"]
        assert "standing" not in point["label"]
        assert "more" not in point["label"]
        assert point["unit"] == "tonnes"


# ---------------------------------------------------------------------------
# MG-11 — engine graphics render at slot length
# ---------------------------------------------------------------------------

def test_mg11_template_renders_at_requested_duration(tmp_path):
    out = tmp_path / "chart8.mp4"
    rendered, _meta = engine_templates.TEMPLATES["line_chart"](
        {"points": [
            {"label": "1956", "value": 12, "display": "12"},
            {"label": "1960", "value": 30, "display": "30"},
            {"label": "1965", "value": 55, "display": "55"},
        ], "title": "Port Traffic", "units": "ships"},
        "investigative", out, duration=8.0)
    assert rendered is not None
    duration = _probe_duration(out)
    assert 7.8 <= duration <= 8.2
    early = _frame(out, tmp_path / "f02.png", at=0.2)
    settled_a = _frame(out, tmp_path / "f70.png", at=7.0)
    settled_b = _frame(out, tmp_path / "f79.png", at=7.9)
    # The end state is the designed settled state: the last
    # second is (near-)static, and it differs from the opening —
    # the animation ran its full course at slot length rather
    # than a 3s render being frozen by tpad.
    assert _mean_diff(settled_a, settled_b) < 2.0
    # Threshold calibrated at Gate 2 verification: the measured
    # early-vs-settled whole-frame mean diff for this chart is
    # ~1.67 (the line/area are thin elements on a dark frame, so
    # the whole-frame mean is small even though the chart itself
    # is fully redrawn). 1.0 keeps the discrimination — a frame
    # that never animated differs by ~0 — with real margin.
    assert _mean_diff(early, settled_b) > 1.0


def test_mg11_builder_renders_at_slot_duration(tmp_path):
    data = _container_data(tmp_path, [])
    entry = _entry("mg_1101", 1, 0, 1, MCLEAN_TEXT, ["archival_title"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]),
        slot_durations={1: 9.0})
    assert len(assets) == 1
    assert 8.5 <= assets[0]["duration"] <= 9.5
    probed = _probe_duration(assets[0]["file_path"])
    assert 8.8 <= probed <= 9.2


def test_mg11_builder_default_duration_without_slot(tmp_path):
    data = _container_data(tmp_path, [])
    entry = _entry("mg_1102", 1, 0, 1, MCLEAN_TEXT, ["archival_title"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    assert len(assets) == 1
    assert assets[0]["duration"] == 3.0
