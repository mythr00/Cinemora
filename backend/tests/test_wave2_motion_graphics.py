"""Phase 2.5 Wave 2 — Motion Graphics Engine breadth build.

Per the plan's verification rule, the Wave 2 families render
through build_motion_graphics_assets on targeted beats (the
Hyatt engineering and container-port industrial beats from the
Wave 1 pattern, plus scenario-recorded data contracts), render
directly with synthetic recorded data, and refuse with typed
reasons when their recorded material is absent. MG-05 validation
runs on every production render; frame checks extract real
frames and assert they are non-blank and theme-coloured.

The plan's Wave 2 cross-genre demo production is NOT run here —
the founder's no-video-until-complete directive defers it to the
finish-line films.
"""

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from services import genre_packs, graphics_pipeline, visual_director
from services.motion_engine import templates as engine_templates
from services.motion_engine import themes as engine_themes

# P0 removal (2026-11-27): milestone_waveform was removed
# completely from the Wave 2 registrations; it is pinned ABSENT
# in tests/test_p0_removal_pins.py.
WAVE2_TEMPLATES = [
    "bubble_chart", "population_pyramid", "animated_dashboard",
    "checklist_graphic",
    "territory_expansion", "satellite_zoom", "heatmap",
    "classified_file", "newspaper_wall", "crime_scene_diagram",
    "construction_sequence", "molecular_diagram",
    "hud_overlay", "holographic_panel", "before_after",
    "layered_reveal", "object_labels",
]


# ---------------------------------------------------------------------------
# Helpers (the Wave 1 test pattern)
# ---------------------------------------------------------------------------

def _manifest(entries):
    return {"version": 1, "project_id": "w2", "theme": None,
            "entries": entries}


def _entry(manifest_id, scene, index, number, text, templates,
           scenario=None, **extra):
    entry = {
        "manifest_id": manifest_id,
        "target": {
            "scene_number": scene, "sentence_index": index,
            "sentence_number": number,
        },
        "text": text,
        "scenario": scenario or {},
        "purpose": "coverage_fallback",
        "template_candidates": templates,
        "family_candidates": ["event_schematic"],
        "theme": None,
        "fact_basis": "script_claim",
        "evidence_ref": None,
        "verified_data": None,
    }
    entry.update(extra)
    return entry


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


def _frame(video_path, out_png, at=1.0):
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-ss", str(at),
         "-i", str(video_path), "-vframes", "1", str(out_png)],
        check=True,
    )
    return Image.open(out_png).convert("RGB")


def _assert_frame_alive(img, theme_name="investigative"):
    assert img.size == (1920, 1080)
    px = list(img.resize((64, 36)).getdata())
    means = [sum(c) / 3 for c in px]
    mean = sum(means) / len(means)
    variance = sum((m - mean) ** 2 for m in means) / len(means)
    assert variance > 25, "frame looks blank (near-zero variance)"
    bg = engine_themes.THEMES[theme_name]["bg"]
    close = sum(
        1 for c in px
        if all(abs(c[i] - bg[i]) <= 24 for i in range(3))
    )
    assert close >= 3, "theme background colour absent from frame"


def _data(scenes, case_bible=None, processed=None):
    return {
        "case_bible": case_bible or {"title": "Wave 2", "timeline": []},
        "scenes": scenes,
        "processed_scenes": processed or [],
    }


def _scene(text, number=1, **extra):
    sentence = {"text": text, "sentence_number": number}
    sentence.update(extra)
    return {"scene_number": 1, "sentences": [sentence]}


def _multi_sentence_scene(texts):
    """One scene holding several sentences — the pipeline serves
    at most one directed graphic per sentence, so multi-template
    pipeline tests give each entry its own sentence."""
    return {"scene_number": 1, "sentences": [
        {"text": text, "sentence_number": index + 1}
        for index, text in enumerate(texts)
    ]}


# ---------------------------------------------------------------------------
# Registry + typed refusals
# ---------------------------------------------------------------------------

def test_registry_covers_all_wave2_templates():
    for name in WAVE2_TEMPLATES:
        assert name in engine_templates.TEMPLATES, name
    # 31 at Wave 1 + 18 Wave 2 registrations.
    # +2 (GitHub Skills Programme, 2026-11-25): the Manim engine's
    # manim_line_chart / manim_bar_chart registered alongside.
    # +2 (GitHub Skills Programme, 2026-11-25): the matplotlib
    # chart engine's mpl_line_chart / mpl_bar_chart.
    # P0 removal (2026-11-27): eleven templates removed
    # completely — 53 down to 42.
    assert len(engine_templates.TEMPLATES) == 42


def test_wave2_templates_refuse_with_typed_reasons(tmp_path):
    theme = engine_themes.THEMES["investigative"]
    photo = _photo(tmp_path / "p.jpg")
    cases = {
        "bubble_chart": ({"items": [{"label": "a", "value": 1}]},
                         "insufficient_items"),
        "population_pyramid": (
            {"groups": [{"label": "a", "left": 1}]}, "no_paired_items"),
        "animated_dashboard": (
            {"stats": [{"label": "a", "value": 1}]},
            "insufficient_stats"),
        "checklist_graphic": ({"items": []}, "no_items"),
        "territory_expansion": (
            {"states": [{"label": "1900",
                         "regions": [{"country": "France"}]}]},
            "insufficient_states"),
        "satellite_zoom": ({}, "no_image"),
        "heatmap": ({"points": [{"at": [0.1, 0.1]}]},
                    "insufficient_points"),
        "newspaper_wall": (
            {"articles": [{"headline": "A"}, {"headline": "B"}]},
            "insufficient_articles"),
        "crime_scene_diagram": ({"markers": []}, "no_markers"),
        "construction_sequence": (
            {"stages": [{"label": "A"}]}, "insufficient_stages"),
        "molecular_diagram": (
            {"atoms": [{"element": "O", "at": [0.5, 0.5]}]},
            "insufficient_atoms"),
        "hud_overlay": ({}, "no_photo"),
        "holographic_panel": (
            {"items": [{"label": "a", "value": "1"}]},
            "insufficient_items"),
        "before_after": ({"before": photo}, "missing_pair"),
        "layered_reveal": (
            {"photos": [{"path": photo}]}, "insufficient_photos"),
        "object_labels": (
            {"photo": photo, "labels": []}, "no_labels"),
        "classified_file": (
            {"image": str(tmp_path / "missing.jpg")},
            "document_image_missing"),
    }
    for name, (data, reason) in cases.items():
        path, meta = engine_templates.TEMPLATES[name](
            data, theme, str(tmp_path / "x.mp4"), duration=0.6)
        assert path is None, name
        assert meta["reason"] == reason, (name, meta)


def test_document_presentation_base_behaviour_unchanged(tmp_path):
    # The classified variant must not leak into the base
    # template: no variant key recorded, base name in metadata.
    theme = engine_themes.THEMES["investigative"]
    path, meta = engine_templates.TEMPLATES["document_presentation"](
        {"body": "A short recorded memo body.", "title": "Memo"},
        theme, str(tmp_path / "doc.mp4"), duration=0.6)
    assert path is not None
    assert meta["template"] == "document_presentation"
    assert "variant" not in meta["inputs"]
    path, meta = engine_templates.TEMPLATES["classified_file"](
        {"body": "A short recorded memo body.", "title": "Memo"},
        theme, str(tmp_path / "cls.mp4"), duration=0.6)
    assert path is not None
    assert meta["template"] == "classified_file"
    assert meta["inputs"]["variant"] == "classified"


# ---------------------------------------------------------------------------
# Direct renders with synthetic recorded data (frame-checked)
# ---------------------------------------------------------------------------

def _direct_cases(tmp_path):
    photo = _photo(tmp_path / "a.jpg")
    photo2 = _photo(tmp_path / "b.jpg", base=(150, 80, 60),
                    block=(60, 120, 200))
    return {
        "bubble_chart": {
            "items": [
                {"label": "Road", "value": 60, "size": 900},
                {"label": "Rail", "value": 25, "size": 300},
                {"label": "Air", "value": 15, "size": 120},
            ], "title": "Freight by mode"},
        "population_pyramid": {
            "groups": [
                {"label": "0-20", "left": 30, "right": 29},
                {"label": "21-40", "left": 26, "right": 27},
                {"label": "41-60", "left": 18, "right": 20},
            ], "left_label": "Men", "right_label": "Women",
            "title": "Population by age"},
        "animated_dashboard": {
            "stats": [
                {"label": "Ships", "value": 55, "display": "55"},
                {"label": "Cranes", "value": 12, "display": "12"},
                {"label": "TEU", "value": 9000, "display": "9,000"},
            ], "title": "Port today"},
        "checklist_graphic": {
            "items": [
                {"label": "Inspect the hanger rods", "state": "done"},
                {"label": "Check the box beam welds", "state": "active"},
                {"label": "Sign off the drawings", "state": "pending"},
            ], "title": "Inspection"},
        "territory_expansion": {
            "states": [
                {"label": "1900", "regions": [{"country": "France"}]},
                {"label": "1950", "regions": [
                    {"country": "France"}, {"country": "Germany"}]},
            ], "title": "Expansion"},
        "satellite_zoom": {
            "image": photo, "label": "The port", "focus": [0.5, 0.5]},
        "heatmap": {
            "points": [
                {"at": [0.3, 0.4], "weight": 5},
                {"at": [0.5, 0.5], "weight": 9},
                {"at": [0.6, 0.3], "weight": 3},
            ], "regions": [{"country": "France"}], "title": "Activity"},
        "newspaper_wall": {
            "articles": [
                {"headline": "Walkway design changed on drawings",
                 "source": "Kansas City Star", "date": "1981"},
                {"headline": "Engineers face licence hearing",
                 "source": "The Times", "date": "1982"},
                {"headline": "Memorial dedicated to the victims",
                 "source": "City Post", "date": "2015"},
            ], "title": "The coverage"},
        "crime_scene_diagram": {
            "markers": [
                {"label": "Case file", "at": [0.3, 0.4]},
                {"label": "Photo log", "at": [0.6, 0.6]},
            ],
            "rooms": [{"x": 0.05, "y": 0.08, "w": 0.62, "h": 0.84,
                       "label": "Hall"}],
            "title": "Scene plan"},
        "construction_sequence": {
            "stages": [
                {"label": "Foundations"}, {"label": "Steel frame"},
                {"label": "Walkway decks"},
            ], "title": "Building the hotel"},
        "molecular_diagram": {
            "atoms": [
                {"element": "O", "at": [0.5, 0.4]},
                {"element": "H", "at": [0.35, 0.62]},
                {"element": "H", "at": [0.65, 0.62]},
            ], "bonds": [[0, 1], [0, 2]], "title": "Water"},
        "hud_overlay": {
            "photo": photo,
            "readouts": [{"label": "Flow", "value": "42 units"}],
            "title": "Terminal systems"},
        "holographic_panel": {
            "items": [
                {"label": "Depth", "value": "12 m"},
                {"label": "Flow", "value": "42"},
            ], "title": "Harbour panel"},
        "before_after": {
            "before": photo, "after": photo2, "title": "The yard"},
        "layered_reveal": {
            "photos": [
                {"path": photo, "caption": "Arrival"},
                {"path": photo2, "caption": "Departure"},
            ], "title": "The journey"},
        "object_labels": {
            "photo": photo,
            "labels": [
                {"text": "Gantry crane", "at": [0.4, 0.4]},
                {"text": "Gate", "at": [0.7, 0.7]},
            ]},
        "classified_file": {
            "image": photo, "title": "Inspection memo",
            "source_label": "City archive"},
    }


@pytest.mark.parametrize("name", WAVE2_TEMPLATES)
def test_wave2_template_renders_recorded_data(tmp_path, name):
    theme = engine_themes.THEMES["investigative"]
    data = _direct_cases(tmp_path)[name]
    path, meta = engine_templates.TEMPLATES[name](
        data, theme, str(tmp_path / f"{name}.mp4"), duration=1.2)
    assert path is not None, (name, meta)
    assert meta["template"] == name
    frame = _frame(path, tmp_path / f"{name}.png", at=0.9)
    if name in ("satellite_zoom", "before_after", "layered_reveal",
                "hud_overlay", "object_labels", "newspaper_wall"):
        # Photo-full frames: no theme background by design (the
        # photo fills the frame — or, for the newspaper wall, the
        # papers tile it) — size + non-blank only, the Wave 1
        # parallax precedent.
        assert frame.size == (1920, 1080)
        px = list(frame.resize((64, 36)).getdata())
        means = [sum(c) / 3 for c in px]
        mean = sum(means) / len(means)
        variance = sum((m - mean) ** 2 for m in means) / len(means)
        assert variance > 25, f"{name} frame looks blank"
    else:
        _assert_frame_alive(frame)


def test_crime_scene_stamps_schematic(tmp_path):
    theme = engine_themes.THEMES["investigative"]
    _path, meta = engine_templates.TEMPLATES["crime_scene_diagram"](
        {"markers": [{"label": "Case file", "at": [0.3, 0.4]}]},
        theme, str(tmp_path / "cs.mp4"), duration=0.6)
    assert meta["inputs"]["schematic"] is True


# ---------------------------------------------------------------------------
# Through the production chain (build_motion_graphics_assets)
# ---------------------------------------------------------------------------

def test_dashboard_from_recorded_fact_table(tmp_path):
    # The dashboard's recorded-data route: one card per distinct
    # recorded unit (first recorded value), plus a series for any
    # unit with >= 3 recorded values.
    facts = [
        {"kind": "quantity", "value": 114, "unit": "people",
         "display": "114 people", "sentence_number": 1,
         "date": {"display": ""}},
        {"kind": "quantity", "value": 216, "unit": "people",
         "display": "216 people", "sentence_number": 1,
         "date": {"display": ""}},
        {"kind": "quantity", "value": 1600, "unit": "people",
         "display": "1,600 people", "sentence_number": 1,
         "date": {"display": ""}},
        {"kind": "quantity", "value": 65, "unit": "feet",
         "display": "65 feet", "sentence_number": 1,
         "date": {"display": ""}},
        {"kind": "quantity", "value": 140, "unit": "million dollars",
         "display": "$140 million", "sentence_number": 1,
         "date": {"display": ""}},
    ]
    text = "The toll mounted across the city that night."
    data = _data(
        [_scene(text)],
        case_bible={"title": "The Walkway", "timeline": [],
                    "script_facts": facts})
    entries = [
        _entry("w2_0001", 1, 0, 1, text,
               templates=["animated_dashboard"]),
    ]
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest(entries))
    dash = [a for a in assets if a.get("template") == "animated_dashboard"]
    assert len(dash) == 1
    assert len(dash[0]["engine_inputs"]["stats"]) == 3
    assert len(dash[0]["engine_inputs"]["series"]) == 3
    assert dash[0]["validation"]["passed"]
    frame = _frame(dash[0]["file_path"], tmp_path / "dash.png")
    _assert_frame_alive(frame)


def test_bubble_chart_from_fact_table(tmp_path):
    facts = [
        {"kind": "quantity", "value": 12, "unit": "ships",
         "display": "12 ships", "sentence_number": 1,
         "date": {"display": "2019"}},
        {"kind": "quantity", "value": 30, "unit": "ships",
         "display": "30 ships", "sentence_number": 2,
         "date": {"display": "2020"}},
        {"kind": "quantity", "value": 55, "unit": "ships",
         "display": "55 ships", "sentence_number": 3,
         "date": {"display": "2021"}},
    ]
    text = "Throughput climbed year after year."
    data = _data(
        [_scene(text)],
        case_bible={"title": "The Port", "timeline": [],
                    "script_facts": facts})
    entry = _entry("w2_0002", 1, 0, 1, text, templates=["bubble_chart"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    charts = [a for a in assets if a.get("template") == "bubble_chart"]
    assert len(charts) == 1
    assert [i["value"] for i in charts[0]["engine_inputs"]["items"]] == [
        12.0, 30.0, 55.0]
    # No second recorded dimension: area encodes the value, and
    # the engine record says so.
    assert charts[0]["engine_metadata"]["inputs"]["size_source"] == "value"
    assert charts[0]["validation"]["passed"]


def test_milestone_waveform_entry_produces_nothing(tmp_path):
    """P0 removal pin (replaces the case-bible waveform render
    test): the bible timeline no longer buys a milestone_waveform
    graphic — an entry naming the removed template produces
    nothing at all."""
    timeline = [
        {"when": "1956", "what": "First container sailing"},
        {"when": "1970", "what": "ISO standard fixed"},
        {"when": "2020", "what": "Mega ships arrive"},
    ]
    text = "By 2020 the industry had been transformed."
    data = _data(
        [_scene(text)],
        case_bible={"title": "The Box", "timeline": timeline})
    entry = _entry("w2_0003", 1, 0, 1, text,
                   templates=["milestone_waveform"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    assert assets == []


def test_scenario_contract_templates_through_pipeline(tmp_path):
    texts = [
        "The sequence was recorded stage by stage.",
        "Every recorded check was worked through in order.",
        "The compound was recorded atom by atom.",
        "The scene was recorded marker by marker.",
    ]
    scenario = {
        "stages": [{"label": "Foundations"}, {"label": "Frame"}],
        "checklist": [
            {"label": "Inspect rods", "state": "done"},
            {"label": "Check welds", "state": "pending"},
        ],
        "molecule": {
            "name": "Water",
            "atoms": [
                {"element": "O", "at": [0.5, 0.4]},
                {"element": "H", "at": [0.35, 0.62]},
                {"element": "H", "at": [0.65, 0.62]},
            ],
            "bonds": [[0, 1], [0, 2]],
        },
        "scene_markers": [{"label": "Case file", "at": [0.3, 0.4]}],
    }
    data = _data([_multi_sentence_scene(texts)])
    entries = [
        _entry("w2_0010", 1, 0, 1, texts[0],
               templates=["construction_sequence"], scenario=scenario),
        _entry("w2_0011", 1, 1, 2, texts[1],
               templates=["checklist_graphic"], scenario=scenario),
        _entry("w2_0012", 1, 2, 3, texts[2],
               templates=["molecular_diagram"], scenario=scenario),
        _entry("w2_0013", 1, 3, 4, texts[3],
               templates=["crime_scene_diagram"], scenario=scenario),
    ]
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest(entries))
    by_template = {a.get("template") for a in assets}
    for name in ("construction_sequence", "checklist_graphic",
                 "molecular_diagram", "crime_scene_diagram"):
        assert name in by_template, name
    for asset in assets:
        if asset.get("template") in by_template:
            assert asset["validation"]["passed"]


def test_scenario_contract_absent_no_render(tmp_path):
    # No recorded scenario data — the directed attempt is not
    # served (the beat falls through the chain, as Wave 1's
    # layer-less schematic does).
    text = "The structure rose over the summer."
    data = _data([_scene(text)])
    entry = _entry("w2_0014", 1, 0, 1, text,
                   templates=["construction_sequence"], scenario={})
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    assert not [a for a in assets
                if a.get("template") == "construction_sequence"]


def _scene_assets(tmp_path):
    doc = _photo(tmp_path / "memo.jpg", base=(225, 220, 205),
                 block=(60, 60, 60))
    yard = _photo(tmp_path / "yard.jpg")
    crane = _photo(tmp_path / "crane.jpg", base=(150, 80, 60),
                   block=(60, 120, 200))
    ship = _photo(tmp_path / "ship.jpg", base=(40, 60, 110),
                  block=(220, 200, 90))
    processed = [{
        "scene": {"scene_number": 1, "sentences": []},
        "images": [
            {"verification": {"accepted": True},
             "evidence_type": "document", "path": doc,
             "file_path": doc, "provider": "wikimedia",
             "source_url": "https://example.org/memo",
             "title": "Inspection memo", "type": "image"},
            {"verification": {"accepted": True},
             "path": yard, "file_path": yard, "provider": "wikimedia",
             "source_url": "https://example.org/yard",
             "title": "The container yard", "type": "image"},
            {"verification": {"accepted": True},
             "path": crane, "file_path": crane, "provider": "wikimedia",
             "source_url": "https://example.org/crane",
             "title": "Gantry crane", "type": "image"},
            {"verification": {"accepted": True},
             "path": ship, "file_path": ship, "provider": "wikimedia",
             "source_url": "https://example.org/ship",
             "title": "Container ship", "type": "image"},
        ],
        "videos": [],
    }]
    return processed


def test_classified_file_unlocks_with_genuine_document(tmp_path):
    data = _data([_scene("The memo recorded the change.")],
                 processed=_scene_assets(tmp_path))
    entry = _entry("w2_0020", 1, 0, 1,
                   "The memo recorded the change.",
                   templates=["classified_file"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    files = [a for a in assets if a.get("template") == "classified_file"]
    assert len(files) == 1
    assert files[0]["engine_inputs"]["variant"] == "classified"
    assert files[0]["engine_inputs"]["document_source"]["source"] == (
        "wikimedia")
    assert files[0]["validation"]["passed"]
    frame = _frame(files[0]["file_path"], tmp_path / "cls.png", at=1.2)
    _assert_frame_alive(frame)


def test_classified_file_stays_refused_without_document(tmp_path):
    data = _data([_scene("The memo recorded the change.")],
                 processed=[])
    entry = _entry("w2_0021", 1, 0, 1,
                   "The memo recorded the change.",
                   templates=["classified_file"])
    graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    manifest = json.loads(
        (tmp_path / "motion_graphics.json").read_text())
    refusals = [
        f for f in manifest["graphic_failures"]
        if f["reason"] == "document_presentation_disabled"
        and "classified_file" in (f.get("detail") or "")
    ]
    assert len(refusals) == 1
    assert not [a for a in manifest["assets"]
                if a.get("template") == "classified_file"]


def test_photo_backed_wave2_through_pipeline(tmp_path):
    facts = [
        {"kind": "quantity", "value": 42, "unit": "units",
         "display": "42 units", "sentence_number": 1,
         "date": {"display": ""}},
        # The HUD assembler reads the readouts recorded for ITS
        # sentence (sentence 2) — a fact on another sentence does
        # not leak into the overlay.
        {"kind": "quantity", "value": 7, "unit": "cranes",
         "display": "7 cranes", "sentence_number": 2,
         "date": {"display": ""}},
    ]
    texts = [
        "The terminal moved 42 units before dawn.",
        "The readouts climbed as the shift wore on.",
        "Each machine in the yard had its place.",
        "By morning the papers carried the story.",
    ]
    data = _data(
        [_multi_sentence_scene(texts)],
        case_bible={"title": "The Port", "timeline": [],
                    "script_facts": facts},
        processed=_scene_assets(tmp_path))
    scenario = {
        "place": "France",
        "object_labels": [{"text": "Gantry crane", "at": [0.4, 0.4]}],
    }
    entries = [
        _entry("w2_0030", 1, 0, 1, texts[0],
               templates=["layered_reveal"]),
        _entry("w2_0031", 1, 1, 2, texts[1], templates=["hud_overlay"]),
        _entry("w2_0032", 1, 2, 3, texts[2],
               templates=["object_labels"], scenario=scenario),
        _entry("w2_0033", 1, 3, 4, texts[3],
               templates=["newspaper_wall"]),
    ]
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest(entries))
    by_template = {a.get("template"): a for a in assets}
    for name in ("layered_reveal", "hud_overlay", "object_labels",
                 "newspaper_wall"):
        assert name in by_template, name
        assert by_template[name]["validation"]["passed"], name
    assert len(by_template["layered_reveal"]
               ["engine_inputs"]["photos"]) >= 2
    assert by_template["hud_overlay"]["engine_inputs"]["readouts"]


# ---------------------------------------------------------------------------
# Director reachability (every template under its recorded
# evidence condition — and not without it)
# ---------------------------------------------------------------------------

def _candidates(text, scenario=None, **outcome_extra):
    outcome = {
        "text": text, "scenario": scenario or {},
        "scene_photo_count": 0, "document_asset": None,
        "scene_has_identity_photo": False, "sentence_index": 5,
    }
    outcome.update(outcome_extra)
    candidates, _aff = visual_director._template_candidates(
        {}, outcome, None)
    return candidates


def test_director_registers_every_wave2_template():
    series = "Levels rose 12 units, then 30 units, then 55 units."
    for name in ("bubble_chart", "animated_dashboard",
                 "holographic_panel"):
        assert name in _candidates(series), name
    # P0 removal: a dated beat must NEVER propose the removed
    # milestone_waveform.
    assert "milestone_waveform" not in _candidates(
        "In 1956 the first ship sailed.")
    assert "population_pyramid" in _candidates(
        "The town's ages shifted.", scenario={
            "pyramid": [{"label": "0-20", "left": 3, "right": 3}]})
    assert "population_pyramid" not in _candidates(
        "The town's ages shifted.")
    assert "checklist_graphic" in _candidates(
        "The steps were followed.", scenario={
            "checklist": [{"label": "Inspect"}]})
    assert "construction_sequence" in _candidates(
        "The frame rose.", scenario={
            "stages": [{"label": "A"}, {"label": "B"}]})
    assert "construction_sequence" not in _candidates(
        "The frame rose.", scenario={"stages": [{"label": "A"}]})
    assert "molecular_diagram" in _candidates(
        "The compound broke down.", scenario={
            "molecule": {"atoms": [{"element": "O"}]}})
    assert "heatmap" in _candidates(
        "Reports clustered in the north.", scenario={
            "density_points": [{}, {}, {}]})
    assert "heatmap" not in _candidates(
        "Reports clustered in the north.", scenario={
            "density_points": [{}]})
    assert "territory_expansion" in _candidates(
        "The realm grew.", scenario={
            "expansion": {"states": [{}, {}]}})
    assert "crime_scene_diagram" in _candidates(
        "Markers were placed.", scenario={
            "scene_markers": [{"label": "x", "at": [0.1, 0.1]}]})
    assert "before_after" in _candidates(
        "The yard before and after.", scenario={
            "before_image": "a.jpg", "after_image": "b.jpg"})
    assert "before_after" not in _candidates(
        "The yard before and after.")
    assert "classified_file" in _candidates(
        "The memo surfaced.",
        document_asset={"path": "/x.jpg", "source": "wikimedia",
                        "url": "https://example.org"})
    assert "classified_file" not in _candidates("The memo surfaced.")
    assert "newspaper_wall" in _candidates(
        "The newspaper headline announced the verdict.")
    assert "hud_overlay" in _candidates(
        "Levels reached 42 units.", scene_photo_count=1)
    assert "hud_overlay" not in _candidates(
        "Levels reached 42 units.", scene_photo_count=0)
    assert "object_labels" in _candidates(
        "The crane stood over the yard.",
        scenario={"object_labels": [{"text": "Crane",
                                      "at": [0.4, 0.4]}]},
        scene_photo_count=1)
    assert "satellite_zoom" in _candidates(
        "Seen from above, the harbour filled the frame.",
        scenario={"place": "France"}, scene_photo_count=1)
    assert "satellite_zoom" not in _candidates(
        "The harbour filled the frame.",
        scenario={"place": "France"}, scene_photo_count=1)
    assert "layered_reveal" in _candidates(
        "Photographs emerged.", scene_photo_count=3)
    assert "layered_reveal" not in _candidates(
        "Photographs emerged.", scene_photo_count=2)


# ---------------------------------------------------------------------------
# Pack reachability: affinities claimed + profiles validate
# ---------------------------------------------------------------------------

def test_wave2_pack_affinities_and_validation():
    packs = genre_packs.available_packs()
    # The five wave-2 packs must all be present. The catalogue is
    # no longer exactly five: commit 575fe8e (Phase M) deliberately
    # added the biography / science / investigative packs (see
    # PHASE_M_PACKS in test_phase2_genre_packs), so this pins the
    # wave-2 five as a subset; the invariant loop below still runs
    # over EVERY pack in the catalogue.
    assert {
        "general", "history", "true_crime", "engineering", "industrial",
    } <= {p["id"] for p in packs}
    by_id = {p["id"]: p for p in packs}
    for pack in packs:
        failures = genre_packs.validate_visual_profile(
            pack.get("visual_profile"), pack["id"])
        assert failures == [], (pack["id"], failures)
        for name in WAVE2_TEMPLATES:
            assert name in genre_packs.ASSEMBLED_TEMPLATES, name
            assert name in pack["graphics_motifs"], (pack["id"], name)
    # The family-weighted affinities the plan assigns.
    assert by_id["true_crime"]["visual_profile"][
        "template_affinities"]["crime_scene_diagram"] == 3
    assert by_id["engineering"]["visual_profile"][
        "template_affinities"]["construction_sequence"] == 3
    assert by_id["industrial"]["visual_profile"][
        "template_affinities"]["animated_dashboard"] == 3
    assert by_id["history"]["visual_profile"][
        "template_affinities"]["territory_expansion"] == 3
