"""Phase 2.5 Wave 1 — Motion Graphics Engine build: verification.

Per the plan's §C verification rule, every new template renders
through build_motion_graphics_assets on targeted beats drawn from
the three finish-line film scripts: the Petito true-crime script
(verbatim, as pinned in test_phase2_script_facts), Hyatt Regency
walkway beats for the engineering film, and container-port beats
for the industrial film (the engineering/industrial scripts do not
exist as full scripts yet — these beats are constructed from the
packs' subject matter, which the report states plainly).

MG-05 validation runs on every production render (the pipeline's
register_asset path); the frame checks below extract real frames
and assert they are non-blank, theme-coloured, and — where the
template is text-led — that OCR reads the expected words.
"""

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from services import genre_packs, graphics_pipeline, visual_director
from services.motion_engine import templates as engine_templates
from services.motion_engine import themes as engine_themes

WAVE1_TEMPLATES = [
    "line_chart", "pie_chart",
    "border_map", "route_map", "trade_route_map", "military_map",
    "exploded_view", "mechanical_cutaway", "blueprint_section",
    "red_string_board", "newspaper_montage", "profile_reveal",
    "photo_parallax", "text_overlay", "text_highlight",
    "archival_title",
]

PETITO_OPENING = (
    "In the summer of 2021, a white van crossed America, carrying "
    "a dream a generation shared."
)
PETITO_QUOTE = (
    'The last text, on August 30, read: "No service in Yosemite" '
    "— a park hundreds of miles from where they were."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _manifest(entries):
    return {"version": 1, "project_id": "w1", "theme": None,
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
    """A textured synthetic photo (gradient + block + stripe) so
    parallax/evidence renders have real content to move."""
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


def _ocr_text(png_path):
    from services.frame_verification import _rapidocr_text

    return _rapidocr_text([str(png_path)]) or ""


def _stub_validator(monkeypatch):
    monkeypatch.setattr(
        graphics_pipeline, "validate_graphic_output",
        lambda path, expected_duration=None, **kwargs: {
            "passed": True, "failure_kind": None, "detail": "",
            "checks": {}, "probe": None,
        },
    )


# ---------------------------------------------------------------------------
# Registry + typed refusals
# ---------------------------------------------------------------------------

def test_registry_covers_all_wave1_templates():
    for name in WAVE1_TEMPLATES + ["document_presentation"]:
        assert name in engine_templates.TEMPLATES, name
    # 15 pre-Wave-1 + closing_card + 17 Wave 1 registrations.
    assert len(engine_templates.TEMPLATES) >= 31


def test_wave1_templates_refuse_with_typed_reasons(tmp_path):
    theme = engine_themes.THEMES["investigative"]
    cases = {
        "line_chart": ({"points": [{"label": "a", "value": 1}]},
                       "insufficient_points"),
        "pie_chart": ({"items": [{"label": "a", "value": 0}]},
                      "no_numeric_items"),
        "military_map": ({"regions": [{"country": "France"}]},
                         "no_movement_data"),
        "newspaper_montage": ({"articles": []}, "no_articles"),
        "profile_reveal": ({"title": "X"}, "no_photo"),
        "archival_title": ({}, "no_title"),
        "text_highlight": ({}, "no_text"),
        "photo_parallax": ({}, "no_photo"),
        "document_presentation": (
            {"image": str(tmp_path / "missing.jpg")},
            "document_image_missing"),
    }
    for name, (data, reason) in cases.items():
        path, meta = engine_templates.TEMPLATES[name](
            data, theme, str(tmp_path / "x.mp4"), duration=1.0)
        assert path is None, name
        assert meta["reason"] == reason, (name, meta)


# ---------------------------------------------------------------------------
# F1 — charts through the production chain (industrial beats)
# ---------------------------------------------------------------------------

def test_line_chart_from_fact_table_through_pipeline(tmp_path):
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
    data = {
        "case_bible": {"title": "The Port", "timeline": [],
                       "script_facts": facts},
        "scenes": [{
            "scene_number": 1,
            "sentences": [{
                "text": "Throughput climbed year after year.",
                "sentence_number": 1,
            }],
        }],
    }
    entry = _entry("w1_0001", 1, 0, 1,
                   "Throughput climbed year after year.",
                   templates=["line_chart"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    charts = [a for a in assets if a.get("template") == "line_chart"]
    assert len(charts) == 1
    points = charts[0]["engine_inputs"]["points"]
    assert [p["value"] for p in points] == [12.0, 30.0, 55.0]
    assert charts[0]["validation"]["passed"]  # MG-05
    frame = _frame(charts[0]["file_path"], tmp_path / "lc.png")
    _assert_frame_alive(frame)
    text = _ocr_text(tmp_path / "lc.png")
    if text:
        assert "55" in text or "THROUGHPUT" in text.upper()


def test_pie_chart_share_sentence_through_pipeline(tmp_path):
    text = ("Road freight accounted for 60 units, rail for 25 units "
            "and air for 15 units of the port's total.")
    data = {
        "case_bible": {"title": "The Port", "timeline": []},
        "scenes": [{
            "scene_number": 1,
            "sentences": [{"text": text, "sentence_number": 1}],
        }],
    }
    entry = _entry("w1_0002", 1, 0, 1, text, templates=["pie_chart"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    pies = [a for a in assets if a.get("template") == "pie_chart"]
    assert len(pies) == 1
    assert pies[0]["validation"]["passed"]
    frame = _frame(pies[0]["file_path"], tmp_path / "pie.png")
    _assert_frame_alive(frame)


# ---------------------------------------------------------------------------
# F5 — schematic variants (Hyatt engineering beats)
# ---------------------------------------------------------------------------

def test_exploded_view_from_scenario_layers(tmp_path):
    text = ("The design change doubled the load carried by the "
            "hanger rods.")
    layers = [
        {"label": "Walkway deck", "depth": 1},
        {"label": "Hanger rods", "depth": 2},
        {"label": "Box beam", "depth": 1},
    ]
    data = {
        "case_bible": {"title": "Hyatt Regency", "timeline": []},
        "scenes": [{
            "scene_number": 1,
            "sentences": [{"text": text, "sentence_number": 1}],
        }],
    }
    entry = _entry(
        "w1_0003", 1, 0, 1, text, templates=["exploded_view"],
        scenario={"event": "Hyatt Regency walkway collapse",
                  "layers": layers})
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    views = [a for a in assets if a.get("template") == "exploded_view"]
    assert len(views) == 1
    assert views[0]["engine_inputs"]["variant"] == "exploded"
    assert views[0]["validation"]["passed"]
    frame = _frame(views[0]["file_path"], tmp_path / "ex.png")
    _assert_frame_alive(frame)


def test_schematic_variant_without_layers_refuses(tmp_path):
    # No recorded layers — no schematic: the assembler yields
    # nothing and the directed attempt is not served by the
    # variant (the beat falls to the coverage floor instead).
    text = "The rods were never checked against the doubled load."
    data = {
        "case_bible": {"title": "Hyatt Regency", "timeline": []},
        "scenes": [{
            "scene_number": 1,
            "sentences": [{"text": text, "sentence_number": 1}],
        }],
    }
    entry = _entry("w1_0004", 1, 0, 1, text,
                   templates=["exploded_view"], scenario={})
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    assert not [a for a in assets
                if a.get("template") == "exploded_view"]


# ---------------------------------------------------------------------------
# F4 — document unlock + investigation variants (Petito beats)
# ---------------------------------------------------------------------------

def _petito_scene_assets(tmp_path):
    doc = _photo(tmp_path / "report.jpg", base=(225, 220, 205),
                 block=(60, 60, 60))
    face = _photo(tmp_path / "gabby.jpg", base=(120, 100, 80),
                  block=(230, 220, 200))
    van = _photo(tmp_path / "van.jpg")
    processed = [{
        "scene": {"scene_number": 1, "sentences": []},
        "images": [
            {"verification": {"accepted": True},
             "evidence_type": "document", "path": doc,
             "file_path": doc, "provider": "wikimedia",
             "source_url": "https://example.org/report",
             "title": "Moab police report", "type": "image"},
            {"verification": {"accepted": True},
             "evidence_type": "identity", "path": face,
             "file_path": face, "provider": "wikimedia",
             "source_url": "https://example.org/gabby",
             "title": "Gabby Petito", "type": "image"},
            {"verification": {"accepted": True},
             "path": van, "file_path": van, "provider": "wikimedia",
             "source_url": "https://example.org/van",
             "title": "The white van", "type": "image"},
        ],
        "videos": [],
    }]
    return processed


def _petito_data(tmp_path, with_assets=True):
    return {
        "case_bible": {
            "title": "The Disappearance of Gabby Petito",
            "timeline": [],
        },
        "scenes": [{
            "scene_number": 1,
            "sentences": [
                {"text": PETITO_OPENING, "sentence_number": 1,
                 "locations": ["New York"], "dates": ["2021"]},
                {"text": PETITO_QUOTE, "sentence_number": 2},
                {"text": "The bodycam footage showed Gabby in tears.",
                 "sentence_number": 3},
                {"text": "Officers separated them for the night.",
                 "sentence_number": 4},
            ],
        }],
        "processed_scenes": (
            _petito_scene_assets(tmp_path) if with_assets else []),
    }


def test_document_presentation_unlocks_with_genuine_asset(tmp_path):
    data = _petito_data(tmp_path)
    entry = _entry("w1_0010", 1, 2, 3,
                   "The bodycam footage showed Gabby in tears.",
                   templates=["document_presentation"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    docs = [a for a in assets
            if a.get("template") == "document_presentation"]
    assert len(docs) == 1
    source = docs[0]["engine_inputs"]["document_source"]
    assert source["source"] == "wikimedia"
    assert source["url"] == "https://example.org/report"
    assert docs[0]["validation"]["passed"]
    frame = _frame(docs[0]["file_path"], tmp_path / "doc.png")
    _assert_frame_alive(frame)


def test_document_presentation_stays_refused_without_asset(tmp_path):
    data = _petito_data(tmp_path, with_assets=False)
    entry = _entry("w1_0011", 1, 2, 3,
                   "The bodycam footage showed Gabby in tears.",
                   templates=["document_presentation"])
    graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    manifest = json.loads(
        (tmp_path / "motion_graphics.json").read_text())
    refusals = [
        f for f in manifest["graphic_failures"]
        if f["reason"] == "document_presentation_disabled"
    ]
    assert len(refusals) == 1
    assert not [
        a for a in manifest["assets"]
        if a.get("template") == "document_presentation"
    ]


def test_investigation_variants_through_pipeline(tmp_path):
    data = _petito_data(tmp_path)
    entries = [
        _entry("w1_0020", 1, 0, 1, PETITO_OPENING,
               templates=["newspaper_montage"]),
        _entry("w1_0021", 1, 1, 2, PETITO_QUOTE,
               templates=["profile_reveal"]),
        _entry("w1_0022", 1, 2, 3,
               "The bodycam footage showed Gabby in tears.",
               templates=["photo_parallax"]),
        _entry("w1_0023", 1, 3, 4,
               "Officers separated them for the night.",
               templates=["red_string_board"]),
    ]
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced",
        manifest=_manifest(entries))
    by_template = {}
    for asset in assets:
        by_template.setdefault(asset.get("template"), []).append(asset)
    for name in ("newspaper_montage", "profile_reveal",
                 "photo_parallax", "red_string_board"):
        assert len(by_template.get(name) or []) == 1, name
        assert by_template[name][0]["validation"]["passed"]
    # Full-bleed photo treatment: no theme background is visible
    # by design (the photo fills the frame; theme tokens ride the
    # title band), so the check is size + non-blank only.
    frame = _frame(by_template["photo_parallax"][0]["file_path"],
                   tmp_path / "par.png", at=1.5)
    assert frame.size == (1920, 1080)
    px = list(frame.resize((64, 36)).getdata())
    means = [sum(c) / 3 for c in px]
    mean = sum(means) / len(means)
    variance = sum((m - mean) ** 2 for m in means) / len(means)
    assert variance > 25, "parallax frame looks blank"


# ---------------------------------------------------------------------------
# F6/F7 — text treatments (Petito beats)
# ---------------------------------------------------------------------------

def test_archival_title_opening_beat(tmp_path):
    data = _petito_data(tmp_path, with_assets=False)
    entry = _entry("w1_0030", 1, 0, 1, PETITO_OPENING,
                   templates=["archival_title"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    titles = [a for a in assets if a.get("template") == "archival_title"]
    assert len(titles) == 1
    assert titles[0]["validation"]["passed"]
    frame = _frame(titles[0]["file_path"], tmp_path / "arch.png",
                   at=1.5)
    _assert_frame_alive(frame)
    text = _ocr_text(tmp_path / "arch.png")
    if text:
        assert "PETITO" in text.upper() or "GABBY" in text.upper()


def test_text_highlight_petito_quote_beat(tmp_path):
    data = _petito_data(tmp_path, with_assets=False)
    entry = _entry("w1_0031", 1, 1, 2, PETITO_QUOTE,
                   templates=["text_highlight"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    hits = [a for a in assets if a.get("template") == "text_highlight"]
    assert len(hits) == 1
    assert hits[0]["engine_inputs"]["highlight"] == (
        "No service in Yosemite")
    assert hits[0]["validation"]["passed"]
    frame = _frame(hits[0]["file_path"], tmp_path / "hl.png", at=2.0)
    assert frame.size == (1920, 1080)


# ---------------------------------------------------------------------------
# Director reachability (§D.2 acceptance: every template proposed
# under its recorded-evidence condition)
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


def test_director_registers_every_wave1_template():
    assert "line_chart" in _candidates(
        "Levels rose 12 units, then 30 units, then 55 units.")
    assert "pie_chart" in _candidates(
        "Road moved 60 units while rail moved 25 units.")
    place = {"place": "France"}
    assert "border_map" in _candidates(
        "The border was closed at dawn.", scenario=place)
    assert "route_map" in _candidates(
        "They drove from France to Germany.", scenario=place)
    assert "military_map" in _candidates(
        "The army advanced on the capital.", scenario=place)
    assert "trade_route_map" in _candidates(
        "Trade through the port doubled.", scenario=place)
    layers = {"layers": [{"label": "Deck", "depth": 1}]}
    for name in ("exploded_view", "mechanical_cutaway",
                 "blueprint_section"):
        assert name in _candidates(
            "The section shows the load path.", scenario=layers), name
    assert "document_presentation" in _candidates(
        "The report was filed.",
        document_asset={"path": "/x.jpg", "source": "wikimedia",
                        "url": "https://example.org"})
    assert "document_presentation" not in _candidates(
        "The report was filed.")
    assert "newspaper_montage" in _candidates(
        "The newspaper headline announced the verdict.")
    assert "profile_reveal" in _candidates(
        "She was last seen in July.",
        scene_has_identity_photo=True)
    assert "red_string_board" in _candidates(
        "Two photographs pinned the timeline.",
        scene_photo_count=2)
    assert "photo_parallax" in _candidates(
        "The van crossed the desert.", scene_photo_count=1)
    assert "text_overlay" in _candidates(
        "The van crossed the desert.", scene_photo_count=1)
    assert "archival_title" in _candidates(
        "It began in the summer.", sentence_index=0)
    assert "archival_title" not in _candidates(
        "It began in the summer.", sentence_index=3)
    assert "text_highlight" in _candidates(
        'The note read: "No service in Yosemite".')


def test_lower_third_reachable_on_all_five_packs():
    """F6 wiring check: the Director's lower-third rule needs the
    pack's lower_third_card weight >= 1 and >= every other
    non-full-frame pattern. Engineering and industrial failed
    this before Wave 1 (weight 1 < side_panel 2; key absent)."""
    for pack_id in ("general", "history", "true_crime",
                    "engineering", "industrial"):
        pack = genre_packs.load_pack(pack_id)
        comp = pack["visual_profile"]["composition_affinities"]
        weight = comp.get("lower_third_card") or 0
        others = [
            v for k, v in comp.items()
            if k not in ("lower_third_card", "full_frame")
        ]
        assert weight >= 1, pack_id
        assert all(weight >= v for v in others), pack_id


# ---------------------------------------------------------------------------
# §D.4 — T5 guarantee widened by exactly one step
# ---------------------------------------------------------------------------

def _guarantee_data(events):
    return {
        "case_bible": {"title": "Hyatt Regency", "timeline": events},
        "scenes": [{
            "scene_number": 1,
            "sentences": [{
                "text": "In August 1981, the skywalk fell.",
                "sentence_number": 1,
            }],
        }],
    }


def test_t5_guarantee_serves_timeline_recap(tmp_path, monkeypatch):
    _stub_validator(monkeypatch)
    monkeypatch.setattr(
        graphics_pipeline, "_render_timeline_graphic",
        lambda path, *a, **k: (
            Path(path).parent.mkdir(parents=True, exist_ok=True),
            Path(path).write_bytes(b"\x00" * 2048),
            "proportional",
        )[-1],
    )

    def fake_comparison(data, style, out_path, duration=5.0,
                        sentence_index=None, scene_number=None):
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 4096)
        return str(path), {
            "generator": "motion_engine",
            "template": "stat_comparison", "generated": True,
        }

    monkeypatch.setitem(
        engine_templates.TEMPLATES, "stat_comparison", fake_comparison)
    events = [
        {"when": "1979", "what": "Design changed"},
        {"when": "July 1981", "what": "Walkways collapse"},
        {"when": "1983", "what": "Inquiry reports"},
    ]
    data = {
        "case_bible": {"title": "Hyatt Regency", "timeline": events},
        "scenes": [{
            "scene_number": 1,
            "sentences": [
                {"text": "In 1981 the load was 60 tons, up from "
                         "25 tons in 1979.", "sentence_number": 1},
                {"text": "In August 1981, the skywalk fell.",
                 "sentence_number": 2},
            ],
        }],
    }
    targets = [
        {"scene_number": 1, "sentence_index": 0, "sentence_number": 1,
         "text": "In 1981 the load was 60 tons, up from 25 tons "
                 "in 1979.",
         "family_candidates": ["stat"]},
        {"scene_number": 1, "sentence_index": 1, "sentence_number": 2,
         "text": "In August 1981, the skywalk fell.",
         "family_candidates": []},
    ]
    assets = graphics_pipeline.build_motion_graphics_assets(
        _guarantee_data(events) | data, tmp_path, mode="balanced",
        targets=targets,
    )
    recaps = [
        a for a in assets
        if a.get("family") == "timeline"
        and a.get("coverage_guarantee") is True
    ]
    assert len(recaps) == 1
    assert recaps[0]["sentence_number"] == 2
    manifest = json.loads(
        (tmp_path / "motion_graphics.json").read_text())
    by_sentence = {
        o["sentence_number"]: o
        for o in manifest["targets"]["outcomes"]
    }
    assert by_sentence[2]["reason"] == (
        "coverage_guarantee:timeline_recap")


def test_t5_guarantee_serves_outcome_path_before_card(tmp_path):
    events = [
        {"when": "1979", "what": "Design changed"},
        {"when": "July 1981", "what": "Walkways collapse"},
    ]
    text = "First the rods bent, then the walkway fell."
    data = {
        "case_bible": {"title": "Hyatt Regency", "timeline": events},
        "scenes": [{
            "scene_number": 1,
            "sentences": [{"text": text, "sentence_number": 1}],
        }],
    }
    entry = _entry("w1_0040", 1, 0, 1, text, templates=[])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]))
    paths = [a for a in assets if a.get("template") == "outcome_path"]
    assert len(paths) == 1
    assert paths[0]["coverage_guarantee"] is True
    assert not [a for a in assets
                if a.get("template") == "closing_card"]
    manifest = json.loads(
        (tmp_path / "motion_graphics.json").read_text())
    outcome = manifest["targets"]["outcomes"][0]
    assert outcome["reason"] == "coverage_guarantee:outcome_path"


# ---------------------------------------------------------------------------
# §D.3 — template-level variety within the engine family
# ---------------------------------------------------------------------------

def test_template_level_variety_rotates_repeat(tmp_path, monkeypatch):
    _stub_validator(monkeypatch)
    monkeypatch.setattr(
        graphics_pipeline, "_render_stat_graphic",
        lambda path, *a, **k: Path(path).write_bytes(b"\x00" * 2048),
    )

    def fake_template(name):
        def render(data, style, out_path, duration=5.0,
                   sentence_index=None, scene_number=None):
            path = Path(out_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x00" * 4096)
            return str(path), {
                "generator": "motion_engine", "template": name,
                "generated": True,
            }
        return render

    for name in ("stat_comparison", "bar_chart", "ring_badges"):
        monkeypatch.setitem(
            engine_templates.TEMPLATES, name, fake_template(name))

    data = {
        "case_bible": {"title": "The Port", "timeline": []},
        "scenes": [{
            "scene_number": 1,
            "sentences": [
                {"text": "Road moved 60 units while rail moved "
                         "25 units.", "sentence_number": 1},
                {"text": "Road moved 70 units while rail moved "
                         "20 units.", "sentence_number": 2},
            ],
        }],
    }
    targets = [
        {"scene_number": 1, "sentence_index": 0, "sentence_number": 1,
         "text": "Road moved 60 units while rail moved 25 units."},
        {"scene_number": 1, "sentence_index": 1, "sentence_number": 2,
         "text": "Road moved 70 units while rail moved 20 units."},
    ]
    pack = genre_packs.load_pack("general")
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", targets=targets,
        genre_pack=pack)
    templates = [a.get("template") for a in assets]
    assert len(templates) == 2, templates
    assert templates[0] != templates[1], templates
    assert set(templates) <= {
        "stat_comparison", "bar_chart", "ring_badges"}
