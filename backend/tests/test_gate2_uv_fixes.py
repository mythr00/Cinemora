"""Gate 2 follow-up — Unified Visual Audit fixes (UV-01/03/12).

UV-01: the bible's script_facts is a {facts, record} DICT in
production; the graphics consumers that iterated it as a list
silently saw no facts. Both consumers (_fact_quantities and the
HUD assembler) now share one normalised intake, and quantity
facts in the production display-only shape (no parsed
value/unit) derive value/unit from their own display string.

UV-12: a quantity fact the bible's own research labels
conflicted (bible["conflicts"] registry match, or an explicit
label on the fact) is excluded from every graphic, with the
exclusion recorded in the graphics manifest.

UV-03: the lower-third label prefers the beat's recorded bible
identifier (scenario place / bible timeline date for the
sentence) over the covering asset's raw search title.
"""

import json
from pathlib import Path

from PIL import Image

from services import graphics_pipeline, visual_director
from services.genre_packs import load_pack


def _stub_validator(monkeypatch):
    monkeypatch.setattr(
        graphics_pipeline, "validate_graphic_output",
        lambda path, expected_duration=None, **kwargs: {
            "passed": True, "failure_kind": None, "detail": "",
            "checks": {}, "probe": None,
        },
    )


def _manifest(entries):
    return {"version": 1, "project_id": "uv", "theme": None,
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


def _quantity(value, unit, number, date=""):
    return {
        "kind": "quantity", "value": value, "unit": unit,
        "display": f"{value} {unit}", "sentence_number": number,
        "date": {"display": date},
    }


_SHIP_FACTS = [
    _quantity(12, "ships", 1, "2019"),
    _quantity(30, "ships", 2, "2020"),
    _quantity(55, "ships", 3, "2021"),
]

_TEXTS = [
    "Throughput climbed year after year.",
    "The yard kept growing through the season.",
    "The harbour handled 55 ships that year.",
]


def _chart_data(script_facts, bible_extra=None):
    bible = {"title": "The Port", "timeline": [],
             "script_facts": script_facts}
    if bible_extra:
        bible.update(bible_extra)
    return {
        "case_bible": bible,
        "scenes": [{
            "scene_number": 1,
            "sentences": [
                {"text": text, "sentence_number": index + 1}
                for index, text in enumerate(_TEXTS)
            ],
        }],
    }


# ---------------------------------------------------------------------
# UV-01 — production dict shape reaches the chart/HUD assemblers
# ---------------------------------------------------------------------


def test_uv01_line_chart_from_production_dict_shape(tmp_path, monkeypatch):
    """The wave-1 chart test passed a bare list; production
    passes {"facts": [...], "record": {...}}. The dict shape
    must yield the same chart from the recorded facts."""
    _stub_validator(monkeypatch)
    table = {
        "facts": _SHIP_FACTS,
        "record": {"sentences": 3, "facts": 3, "quantities": 3},
    }
    entry = _entry("uv_0001", 1, 0, 1, _TEXTS[0],
                   templates=["line_chart"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        _chart_data(table), tmp_path, mode="balanced",
        manifest=_manifest([entry]),
    )
    charts = [a for a in assets if a.get("template") == "line_chart"]
    assert len(charts) == 1
    points = charts[0]["engine_inputs"]["points"]
    assert [p["value"] for p in points] == [12.0, 30.0, 55.0]


def test_uv01_line_chart_from_display_only_production_facts(
    tmp_path, monkeypatch
):
    """The REAL production fact shape (services.script_facts):
    quantity facts carry display + sentence_number + basis —
    no parsed value/unit. Value/unit derive from the display
    string, so these facts reach the chart too."""
    _stub_validator(monkeypatch)
    facts = [
        {"kind": "quantity", "display": "12 ships",
         "sentence_number": 1, "basis": "script_claim",
         "fact_id": "sf_0001"},
        {"kind": "quantity", "display": "30 ships",
         "sentence_number": 2, "basis": "script_claim",
         "fact_id": "sf_0002"},
        {"kind": "quantity", "display": "55 ships",
         "sentence_number": 3, "basis": "script_claim",
         "fact_id": "sf_0003"},
    ]
    table = {"facts": facts, "record": {"facts": 3, "quantities": 3}}
    entry = _entry("uv_0002", 1, 0, 1, _TEXTS[0],
                   templates=["line_chart"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        _chart_data(table), tmp_path, mode="balanced",
        manifest=_manifest([entry]),
    )
    charts = [a for a in assets if a.get("template") == "line_chart"]
    assert len(charts) == 1
    points = charts[0]["engine_inputs"]["points"]
    assert [p["value"] for p in points] == [12.0, 30.0, 55.0]


def test_uv01_hud_readouts_from_production_dict_shape(
    tmp_path, monkeypatch
):
    _stub_validator(monkeypatch)
    photo = tmp_path / "yard.jpg"
    Image.new("RGB", (800, 600), (70, 90, 130)).save(photo)
    table = {
        "facts": [
            _quantity(42, "units", 1),
            _quantity(7, "cranes", 2),
        ],
        "record": {"facts": 2, "quantities": 2},
    }
    data = _chart_data(table)
    data["processed_scenes"] = [{
        "scene": {"scene_number": 1, "sentences": []},
        "images": [{
            "verification": {"accepted": True},
            "path": str(photo), "file_path": str(photo),
            "provider": "wikimedia",
            "source_url": "https://example.org/yard",
            "title": "The container yard", "type": "image",
        }],
        "videos": [],
    }]
    entry = _entry("uv_0003", 1, 1, 2, _TEXTS[1],
                   templates=["hud_overlay"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    huds = [a for a in assets if a.get("template") == "hud_overlay"]
    assert len(huds) == 1
    readouts = huds[0]["engine_inputs"]["readouts"]
    # Only sentence 2's own fact (7 cranes) — sentence 1's fact
    # does not leak into this beat's overlay.
    assert readouts == [{"label": "cranes", "value": "7 cranes"}]


# ---------------------------------------------------------------------
# UV-12 — conflicted facts never reach a graphic
# ---------------------------------------------------------------------


def test_uv12_conflicted_quantity_fact_excluded_from_chart(
    tmp_path, monkeypatch
):
    """The bible's research registry records a conflict over the
    55-ships value for sentence 3's fact (sources disagreed:
    55 vs 61). That fact must not reach the chart; the
    unlabelled same-unit facts are too few to chart, so the
    chart is built from the supported crane series instead,
    and the exclusion is recorded in the manifest."""
    _stub_validator(monkeypatch)
    crane_facts = [
        _quantity(3, "cranes", 1, "2019"),
        _quantity(5, "cranes", 2, "2020"),
        _quantity(9, "cranes", 3, "2021"),
    ]
    table = {"facts": _SHIP_FACTS + crane_facts,
             "record": {"facts": 6, "quantities": 6}}
    bible_extra = {
        "conflicts": [{
            "subject": "the_port",
            "subject_name": "The Port",
            "fact_hint": (
                "2021 The harbour handled 55 ships that year."
            ),
            "values": [
                {"value": "55", "source_url": "https://a.example"},
                {"value": "61", "source_url": "https://b.example"},
            ],
        }],
    }
    entry = _entry("uv_0004", 1, 0, 1, _TEXTS[0],
                   templates=["line_chart"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        _chart_data(table, bible_extra), tmp_path, mode="balanced",
        manifest=_manifest([entry]),
    )
    charts = [a for a in assets if a.get("template") == "line_chart"]
    assert len(charts) == 1
    points = charts[0]["engine_inputs"]["points"]
    assert [p["value"] for p in points] == [3.0, 5.0, 9.0]
    manifest = json.loads(
        (tmp_path / "motion_graphics.json").read_text()
    )
    exclusions = manifest["fact_exclusions"]
    assert len(exclusions) == 1
    assert exclusions[0]["sentence_number"] == 3
    assert exclusions[0]["display"] == "55 ships"
    assert exclusions[0]["reason"] == "research_conflict"


def test_uv12_explicitly_labelled_fact_excluded(tmp_path, monkeypatch):
    """A fact carrying the conflict label on itself is excluded
    even with no registry entry; with it gone the series is too
    short and no chart is produced from those facts."""
    _stub_validator(monkeypatch)
    facts = [
        _quantity(12, "ships", 1, "2019"),
        dict(_quantity(30, "ships", 2, "2020"), conflicted=True),
        _quantity(55, "ships", 3, "2021"),
    ]
    table = {"facts": facts, "record": {"facts": 3, "quantities": 3}}
    entry = _entry("uv_0005", 1, 0, 1, _TEXTS[0],
                   templates=["line_chart"])
    assets = graphics_pipeline.build_motion_graphics_assets(
        _chart_data(table), tmp_path, mode="balanced",
        manifest=_manifest([entry]),
    )
    assert not [a for a in assets if a.get("template") == "line_chart"]
    manifest = json.loads(
        (tmp_path / "motion_graphics.json").read_text()
    )
    assert len(manifest["fact_exclusions"]) == 1
    assert manifest["fact_exclusions"][0]["display"] == "30 ships"


# ---------------------------------------------------------------------
# UV-03 — lower-third prefers the bible identifier
# ---------------------------------------------------------------------


def _director_plan(rows):
    return {
        "version": 1, "project_id": "uv03",
        "analysis_mode": "llm", "scene_governor": None,
        "sentences": rows,
    }


def _director_row(number, scene=1):
    return {
        "sentence_number": number, "scene_number": scene,
        "role": "statement", "primary_visual_intent": "exact",
        "secondary_visual_intents": [], "intent_queries": {},
        "graphic_fallback": {"allowed": True, "kind": "stat"},
        "preferred_media_type": "video",
        "specificity_requirement": "exact",
        "visual_misrepresentation_risk": "low",
        "intents_source": "llm_plan", "fallback_concepts": None,
        "concepts_source": None, "entry_tier": "direct",
    }


def _director_outcome(number, title=None, scenario=None):
    return {
        "sentence_number": number, "scene_number": 1,
        "sentence_index": 0, "text": "A beat.", "scenario": scenario or {},
        "covered": True, "starved": False, "tier": "direct",
        "asset_title": title, "asset_file": "/media/asset.mp4",
        "scene_photo_count": 0, "secondary_video": None,
    }


def test_uv03_lower_third_prefers_bible_place_and_date():
    """The beat's bible carries a place (scenario) and a date
    (timeline entry recorded for this sentence): the label is
    the place, the sublabel the date — never the covering
    asset's raw search title."""
    pack = load_pack("general")
    plan = _director_plan([_director_row(1)])
    outcomes = {
        1: _director_outcome(
            1,
            title="File:Hyatt lobby wide shot.jpg",
            scenario={"place": "Hyatt Regency Kansas City"},
        ),
    }
    bible = {
        "timeline": [{
            "when": "17 July 1981",
            "what": "The walkways collapsed.",
            "sentence_number": 1,
        }],
    }
    plan_v2, _manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=pack, bible=bible
    )
    composition = plan_v2["sentences"][0]["decision"]["composition"]
    assert composition["pattern"] == "lower_third_card"
    assert composition["label"] == "Hyatt Regency Kansas City"
    assert composition["sublabel"] == "17 July 1981"
    assert composition["label_source"] == "bible_identifier"


def test_uv03_lower_third_bible_date_only():
    """No scenario place: the bible date itself is the label,
    in preference to the asset's search title."""
    pack = load_pack("general")
    plan = _director_plan([_director_row(1)])
    outcomes = {
        1: _director_outcome(1, title="crowds crossing checkpoint"),
    }
    bible = {
        "timeline": [{
            "when": "9 November 1989",
            "what": "The Wall fell.",
            "sentence_number": 1,
        }],
    }
    plan_v2, _manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=pack, bible=bible
    )
    composition = plan_v2["sentences"][0]["decision"]["composition"]
    assert composition["label"] == "9 November 1989"
    assert composition["label_source"] == "bible_identifier"


def test_uv03_lower_third_falls_back_to_asset_title_without_bible():
    """No bible identifier for the beat: the historical
    behaviour is unchanged — the asset's own recorded title is
    the label."""
    pack = load_pack("general")
    plan = _director_plan([_director_row(1)])
    outcomes = {1: _director_outcome(1, title="Harbour at dawn")}
    plan_v2, _manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=pack
    )
    composition = plan_v2["sentences"][0]["decision"]["composition"]
    assert composition["label"] == "Harbour at dawn"
    assert composition["label_source"] == "asset_title"
