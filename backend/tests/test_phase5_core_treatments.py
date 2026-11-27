"""Phase 5 core treatments (Motion Graphics Programme, 2026-11-27).

Pins for the rebuilt treatments — B1 chronology, C1 route /
locator, B6 relationship network, B5/B7 recorded-data charts
with the units+source contract, A1/X5 lower thirds over
footage with caption-space coordination, and F3 document
examination surfaced through the real chain.

Every treatment is fed ONLY by recorded data (case-bible
timeline, scenario contracts, manifest verified_data, sentence
records, verified assets); every refusal is typed; prose alone
never qualifies a beat.
"""

import json

from PIL import Image

from services import composition, graphics_pipeline, visual_director
from services.genre_packs import load_pack
from services.motion_engine import templates as engine_templates
from services.motion_engine.templates_v5 import (
    chronology, locator, relationship_network, route,
)

THEME = "editorial"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _manifest(entries):
    return {"version": 1, "project_id": "p5", "theme": None,
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


def _data(bible=None, sentences=None, processed=None):
    return {
        "case_bible": bible or {"title": "Test Story", "timeline": []},
        "scenes": [{
            "scene_number": 1,
            "sentences": sentences or [
                {"text": "A beat with no usable payload.",
                 "sentence_number": 1},
            ],
        }],
        "processed_scenes": processed or [],
    }


def _build(tmp_path, data, entries):
    return graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=_manifest(entries))


def _record(tmp_path):
    return json.loads((tmp_path / "motion_graphics.json").read_text())


def _skip_reasons(tmp_path):
    record = _record(tmp_path)
    reasons = [s.get("reason") for s in
               (record.get("targets") or {}).get("skipped") or []]
    reasons += [f.get("reason") for f in
                record.get("graphic_failures") or []]
    return reasons


# ---------------------------------------------------------------------------
# B1 — chronology template contract
# ---------------------------------------------------------------------------

EVENTS = [
    {"when": "1889", "what": "Tower completed"},
    {"when": "1901", "what": "Radio signals sent"},
    {"when": "1944", "what": "Liberation of Paris"},
    {"when": "1989", "what": "Centenary exhibition"},
]


def test_chronology_renders_from_recorded_events(tmp_path):
    path, meta = chronology(
        {"events": EVENTS, "title": "A Century in Paris"},
        THEME, tmp_path / "c.mp4", duration=1.0)
    assert path is not None
    assert meta["template"] == "chronology"
    assert meta["inputs"]["events"] == 4


def test_chronology_refuses_fewer_than_three_events(tmp_path):
    path, meta = chronology(
        {"events": EVENTS[:2]}, THEME, tmp_path / "c.mp4",
        duration=1.0)
    assert path is None
    assert meta["reason"] == "insufficient_events"


def test_chronology_refuses_label_that_does_not_fit(tmp_path):
    events = EVENTS + [
        {"when": "2001", "what": "Antidisestablishmentarianism"},
        {"when": "2010", "what": "Millennium celebrations held"},
        {"when": "2019", "what": "Fire damages the cathedral"},
        {"when": "2024", "what": "Olympic games return"},
    ]
    path, meta = chronology(
        {"events": events}, THEME, tmp_path / "c.mp4", duration=1.0)
    assert path is None
    assert meta["reason"] == "label_does_not_fit"


def test_chronology_refuses_sentence_length_label(tmp_path):
    events = [dict(e) for e in EVENTS]
    events[1] = {
        "when": "1901",
        "what": " ".join(["word"] * 40),
    }
    path, meta = chronology(
        {"events": events}, THEME, tmp_path / "c.mp4", duration=1.0)
    assert path is None
    assert meta["reason"] == "label_does_not_fit"


# ---------------------------------------------------------------------------
# C1 — route / locator template contracts
# ---------------------------------------------------------------------------

def test_route_renders_between_two_recorded_places(tmp_path):
    path, meta = route(
        {"places": [{"name": "France"}, {"name": "Germany"}],
         "title": "Eastward"},
        THEME, tmp_path / "r.mp4", duration=1.0)
    assert path is not None
    assert meta["inputs"]["places"] == 2


def test_route_refuses_single_place(tmp_path):
    path, meta = route(
        {"places": [{"name": "France"}]}, THEME,
        tmp_path / "r.mp4", duration=1.0)
    assert path is None
    assert meta["reason"] == "insufficient_places"


def test_route_refuses_invented_geography(tmp_path):
    path, meta = route(
        {"places": [{"name": "France"}, {"name": "Atlantis"}]},
        THEME, tmp_path / "r.mp4", duration=1.0)
    assert path is None
    assert meta["reason"] == "unknown_place"


def test_locator_renders_one_recorded_place(tmp_path):
    path, meta = locator(
        {"places": [{"name": "France"}]}, THEME,
        tmp_path / "l.mp4", duration=1.0)
    assert path is not None
    assert meta["inputs"]["place"] == "France"


def test_locator_refuses_no_place_and_unknown_place(tmp_path):
    path, meta = locator({"places": []}, THEME, tmp_path / "l.mp4",
                         duration=1.0)
    assert path is None and meta["reason"] == "no_places"
    path, meta = locator(
        {"places": [{"name": "Atlantis"}]}, THEME,
        tmp_path / "l.mp4", duration=1.0)
    assert path is None and meta["reason"] == "unknown_place"


def test_locator_with_recorded_coordinates(tmp_path):
    path, meta = locator(
        {"places": [{"name": "Kyoto", "lon": 135.7681,
                     "lat": 35.0116, "country": "Japan"}]},
        THEME, tmp_path / "l.mp4", duration=1.0)
    assert path is not None


# ---------------------------------------------------------------------------
# B6 — relationship network template contract
# ---------------------------------------------------------------------------

NETWORK = {
    "nodes": [{"name": "Ada Lovelace"}, {"name": "Charles Babbage"},
              {"name": "Lord Byron"}],
    "edges": [
        {"from": "Ada Lovelace", "to": "Charles Babbage",
         "label": "collaborated"},
        {"from": "Ada Lovelace", "to": "Lord Byron",
         "label": "daughter"},
    ],
}


def test_network_renders_recorded_relations(tmp_path):
    path, meta = relationship_network(
        NETWORK, THEME, tmp_path / "n.mp4", duration=1.0)
    assert path is not None
    assert meta["inputs"]["edges"] == 2


def test_network_refuses_too_few_entities_and_no_edges(tmp_path):
    path, meta = relationship_network(
        {"nodes": [{"name": "Solo"}], "edges": []}, THEME,
        tmp_path / "n.mp4", duration=1.0)
    assert path is None
    assert meta["reason"] == "insufficient_entities"
    path, meta = relationship_network(
        {"nodes": NETWORK["nodes"], "edges": []}, THEME,
        tmp_path / "n.mp4", duration=1.0)
    assert path is None
    assert meta["reason"] == "no_recorded_edges"


def test_network_refuses_edge_to_unrecorded_entity(tmp_path):
    data = {
        "nodes": NETWORK["nodes"],
        "edges": [{"from": "Ada Lovelace", "to": "Ghost Person",
                   "label": "knew"}],
    }
    path, meta = relationship_network(
        data, THEME, tmp_path / "n.mp4", duration=1.0)
    assert path is None
    assert meta["reason"] == "edge_endpoint_unrecorded"


# ---------------------------------------------------------------------------
# Director candidacy — recorded data only
# ---------------------------------------------------------------------------

def _plan_v1(rows):
    return {"version": 1, "project_id": "p5", "sentences": rows}


def _row(number, intent="graphic_explanation"):
    return {
        "sentence_number": number,
        "scene_number": 1,
        "primary_visual_intent": intent,
        "graphic_fallback": {"allowed": True, "kind": "chart"},
    }


def _outcome(number, index=0, scenario=None, text="", starved=True,
             covered=False):
    return {
        "sentence_number": number, "scene_number": 1,
        "sentence_index": index, "text": text,
        "scenario": scenario or {}, "covered": covered,
        "starved": starved, "tier": None, "asset_title": None,
        "asset_file": None, "scene_photo_count": 0,
    }


def _candidates_for(number, outcome, bible=None):
    _plan, manifest = visual_director.reconcile_visual_plan(
        _plan_v1([_row(number)]), {number: outcome}, bible=bible)
    entries = manifest["entries"]
    assert len(entries) == 1
    return entries[0]["template_candidates"]


def test_director_chronology_candidacy_needs_anchored_bible_events():
    bible = {"timeline": [
        {"when": "1889", "what": "Tower completed",
         "sentence_number": 5},
        {"when": "1901", "what": "Radio signals sent"},
        {"when": "1944", "what": "Liberation of Paris"},
    ]}
    candidates = _candidates_for(5, _outcome(5), bible=bible)
    assert "chronology" in candidates
    # The same bible, a beat no event is anchored to: no candidacy.
    candidates = _candidates_for(9, _outcome(9), bible=bible)
    assert "chronology" not in candidates


def test_director_chronology_candidacy_from_scenario_timeline():
    scenario = {"timeline": EVENTS[:3]}
    candidates = _candidates_for(3, _outcome(3, scenario=scenario))
    assert "chronology" in candidates


def test_director_map_and_network_candidacy_from_recorded_data():
    scenario = {"places": [{"name": "France"}, {"name": "Germany"}]}
    assert "route" in _candidates_for(1, _outcome(1, scenario=scenario))
    scenario = {"places": [{"name": "France"}]}
    candidates = _candidates_for(1, _outcome(1, scenario=scenario))
    assert "locator" in candidates and "route" not in candidates
    scenario = {"relationships": [
        {"from": "Ada Lovelace", "to": "Charles Babbage",
         "label": "collaborated"},
    ]}
    assert "relationship_network" in _candidates_for(
        1, _outcome(1, scenario=scenario))


def test_director_no_candidacy_from_prose_alone():
    # Years, place names and relationships in the narration TEXT
    # are not recorded data — none of the rebuilt treatments may
    # be proposed on prose cues (the removed families' heuristic).
    text = ("In 1889 Ada travelled from France to Germany and met "
            "Charles in 1901 before returning in 1944.")
    candidates = _candidates_for(1, _outcome(1, text=text))
    for name in ("chronology", "route", "locator",
                 "relationship_network"):
        assert name not in candidates


# ---------------------------------------------------------------------------
# Pipeline chain — the treatments render through the real chain
# ---------------------------------------------------------------------------

def test_chronology_through_pipeline_from_bible(tmp_path):
    bible = {"title": "Paris", "timeline": EVENTS}
    entry = _entry("p5_0001", 1, 0, 1, "The years passed.",
                   templates=["chronology"])
    assets = _build(tmp_path, _data(bible=bible), [entry])
    charts = [a for a in assets if a.get("template") == "chronology"]
    assert len(charts) == 1
    assert charts[0]["engine_inputs"]["events"] == EVENTS
    assert charts[0]["validation"]["passed"]


def test_chronology_pipeline_refuses_two_events_and_long_payloads(
        tmp_path):
    bible = {"title": "Paris", "timeline": EVENTS[:2]}
    entry = _entry("p5_0002", 1, 0, 1, "Two dates only.",
                   templates=["chronology"])
    assets = _build(tmp_path, _data(bible=bible), [entry])
    assert not [a for a in assets if a.get("template") == "chronology"]
    # A bible whose events are sentence-length: every event is
    # excluded per-event by the short-payload gate, so fewer
    # than 3 usable events remain and the treatment declines.
    long_events = [
        {"when": "1889", "what": " ".join(["word"] * 20)},
        {"when": "1901", "what": " ".join(["text"] * 20)},
        {"when": "1944", "what": " ".join(["more"] * 20)},
    ]
    bible = {"title": "Paris", "timeline": long_events}
    entry = _entry("p5_0003", 1, 0, 1, "Long payloads.",
                   templates=["chronology"])
    assets = _build(tmp_path, _data(bible=bible), [entry])
    assert not [a for a in assets if a.get("template") == "chronology"]


def test_route_and_locator_through_pipeline(tmp_path):
    scenario = {"places": [{"name": "France"}, {"name": "Germany"}]}
    entry = _entry("p5_0004", 1, 0, 1, "The journey east.",
                   templates=["route"], scenario=scenario)
    assets = _build(tmp_path, _data(), [entry])
    routes = [a for a in assets if a.get("template") == "route"]
    assert len(routes) == 1
    assert routes[0]["validation"]["passed"]

    scenario = {"places": [{"name": "France"}]}
    entry = _entry("p5_0005", 1, 0, 1, "It happened in France.",
                   templates=["locator"], scenario=scenario)
    assets = _build(tmp_path, _data(), [entry])
    locators = [a for a in assets if a.get("template") == "locator"]
    assert len(locators) == 1


def test_network_through_pipeline(tmp_path):
    scenario = {"relationships": NETWORK["edges"]}
    entry = _entry("p5_0006", 1, 0, 1, "Their lives intertwined.",
                   templates=["relationship_network"],
                   scenario=scenario)
    assets = _build(tmp_path, _data(), [entry])
    networks = [a for a in assets
                if a.get("template") == "relationship_network"]
    assert len(networks) == 1
    assert networks[0]["engine_inputs"]["edges"] == NETWORK["edges"]


def test_new_treatments_are_director_only_in_undirected_chain(tmp_path):
    # No manifest: caller targets walk the undirected chain, which
    # must never volunteer the bible-global treatments.
    bible = {"title": "Paris", "timeline": EVENTS}
    data = _data(bible=bible)
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced",
        targets=[{
            "scene_number": 1, "sentence_index": 0,
            "sentence_number": 1, "text": "The years passed.",
            "scenario": {"places": [{"name": "France"},
                                     {"name": "Germany"}]},
        }])
    assert not [a for a in assets if a.get("template") in (
        "chronology", "route", "locator", "relationship_network")]


# ---------------------------------------------------------------------------
# B5/B7 — recorded-data charts: units + source required
# ---------------------------------------------------------------------------

SERIES = [
    {"label": "2019", "value": 12, "display": "12 ships"},
    {"label": "2020", "value": 30, "display": "30 ships"},
    {"label": "2021", "value": 55, "display": "55 ships"},
]


def _chart_entry(verified_data):
    return _entry(
        "p5_0010", 1, 0, 1, "Throughput climbed year after year.",
        templates=["line_chart"], verified_data=verified_data)


def test_recorded_series_with_units_and_source_renders(tmp_path):
    verified = {"series": SERIES, "units": "ships",
                "source": "Port authority returns",
                "display": "55 ships"}
    assets = _build(tmp_path, _data(), [_chart_entry(verified)])
    charts = [a for a in assets if a.get("template") == "line_chart"]
    assert len(charts) == 1
    inputs = charts[0]["engine_inputs"]
    assert inputs["units"] == "ships"
    assert inputs["source"] == "Port authority returns"
    assert inputs["recorded"] is True
    assert charts[0]["validation"]["passed"]


def test_recorded_series_missing_source_refuses(tmp_path):
    verified = {"series": SERIES, "units": "ships",
                "display": "55 ships"}
    assets = _build(tmp_path, _data(), [_chart_entry(verified)])
    assert not [a for a in assets if a.get("template") == "line_chart"]
    assert "chart_source_missing" in _skip_reasons(tmp_path)


def test_recorded_series_missing_units_refuses(tmp_path):
    verified = {"series": SERIES, "source": "Port authority returns",
                "display": "55 ships"}
    assets = _build(tmp_path, _data(), [_chart_entry(verified)])
    assert not [a for a in assets if a.get("template") == "line_chart"]
    assert "chart_units_missing" in _skip_reasons(tmp_path)


def test_year_only_series_refuses(tmp_path):
    verified = {
        "series": [
            {"label": "a", "value": 1998}, {"label": "b", "value": 2001},
            {"label": "c", "value": 2005},
        ],
        "units": "years", "source": "Some record",
        "display": "2005",
    }
    assets = _build(tmp_path, _data(), [_chart_entry(verified)])
    assert not [a for a in assets if a.get("template") == "line_chart"]


def test_fact_table_series_renders_with_recorded_unit_and_source(
        tmp_path):
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
    bible = {"title": "The Port", "timeline": [],
             "script_facts": facts}
    entry = _entry("p5_0011", 1, 0, 1,
                   "Throughput climbed year after year.",
                   templates=["line_chart"])
    assets = _build(tmp_path, _data(bible=bible), [entry])
    charts = [a for a in assets if a.get("template") == "line_chart"]
    assert len(charts) == 1
    inputs = charts[0]["engine_inputs"]
    assert inputs["units"] == "ships"
    assert inputs["source"] == "Case bible fact table"


def test_chart_template_refuses_recorded_without_units_or_source(
        tmp_path):
    from services.motion_engine.templates_v3 import line_chart

    data = {"points": SERIES, "recorded": True, "source": "X"}
    path, meta = line_chart(data, THEME, tmp_path / "x.mp4",
                            duration=1.0)
    assert path is None and meta["reason"] == "chart_units_missing"
    data = {"points": SERIES, "recorded": True, "units": "ships"}
    path, meta = line_chart(data, THEME, tmp_path / "x.mp4",
                            duration=1.0)
    assert path is None and meta["reason"] == "chart_source_missing"


# ---------------------------------------------------------------------------
# A1 + X5 — lower thirds over footage, caption coordination
# ---------------------------------------------------------------------------

def test_lower_third_band_never_intersects_caption_band():
    layer = composition.lower_third_layer(
        "Gabby Petito", "Victim", start=0.0, duration=4.0)
    pos = layer["position"]
    band = (pos["x"], pos["y"], pos["x"] + pos["w"],
            pos["y"] + pos["h"])
    assert not composition._bands_intersect(
        band, composition.caption_band())
    plan = {"pattern": "lower_third_card", "layers": [layer]}
    composition.validate_composition(plan, 4.0)
    assert layer["validation"]["passed"], layer["validation"]


def test_lower_third_inside_caption_band_fails_validation():
    layer = composition.lower_third_layer(
        "Gabby Petito", None, start=0.0, duration=4.0)
    # The pre-X5 position: squarely inside the caption band.
    layer["position"] = {"x": 96, "y": 816, "w": 1184, "h": 132}
    failures = composition.validate_layer(layer, 4.0)
    assert "lower_third_overlaps_caption_band" in failures


def test_lower_third_overlong_label_fails_validation():
    layer = composition.lower_third_layer(
        " ".join(["Supercalifragilisticexpialidocious"] * 6),
        None, start=0.0, duration=4.0)
    failures = composition.validate_layer(layer, 4.0)
    assert "lower_third_label_does_not_fit" in failures


def _covered_outcome(number, index, scenario, title="Footage"):
    return {
        "sentence_number": number, "scene_number": 1,
        "sentence_index": index, "text": "", "scenario": scenario,
        "covered": True, "starved": False, "tier": "direct",
        "asset_title": title, "asset_file": "/media/a.mp4",
        "scene_photo_count": 0,
    }


BIBLE_PERSON = {
    "entities": [{
        "id": "gabby_petito", "canonical": "Gabby Petito",
        "type": "person", "role": "victim",
    }],
    "timeline": [],
}


def test_director_name_super_for_recorded_person_introduction():
    pack = load_pack("general")
    scenario = {"subject": "gabby_petito",
                "subject_name": "Gabby Petito"}
    plan = _plan_v1([_row(1), _row(2)])
    outcomes = {
        1: _covered_outcome(1, 0, scenario),
        2: _covered_outcome(2, 1, scenario),
    }
    plan_v2, _manifest = visual_director.reconcile_visual_plan(
        plan, outcomes, genre_pack=pack, bible=BIBLE_PERSON)
    first = plan_v2["sentences"][0]["decision"]
    second = plan_v2["sentences"][1]["decision"]
    assert first["composition"]["reason"] == "name_super"
    assert first["composition"]["label"] == "Gabby Petito"
    assert first["composition"]["sublabel"] == "victim"
    # One super per person: the second beat gets none.
    assert "composition" not in second


def test_director_name_super_requires_bible_person_record():
    pack = load_pack("general")
    # subject_name recorded but the bible says the entity is a
    # PLACE — no name super may fire.
    bible = {"entities": [{
        "id": "gabby_petito", "canonical": "Gabby Petito",
        "type": "place", "role": "location",
    }], "timeline": []}
    scenario = {"subject": "gabby_petito",
                "subject_name": "Gabby Petito"}
    plan_v2, _m = visual_director.reconcile_visual_plan(
        _plan_v1([_row(1)]),
        {1: _covered_outcome(1, 0, scenario)},
        genre_pack=pack, bible=bible)
    decision = plan_v2["sentences"][0]["decision"]
    assert decision.get("composition", {}).get("reason") != "name_super"


def test_compositions_off_means_no_lower_thirds():
    # The Director-level OFF gate: compositions=False issues NO
    # composition decisions of any kind (the jobs-level gate that
    # keeps a stale plan from stamping an OFF run is pinned in
    # test_p0_removal_pins).
    pack = load_pack("general")
    scenario = {"subject": "gabby_petito",
                "subject_name": "Gabby Petito"}
    plan_v2, _m = visual_director.reconcile_visual_plan(
        _plan_v1([_row(1)]),
        {1: _covered_outcome(1, 0, scenario)},
        genre_pack=pack, bible=BIBLE_PERSON, compositions=False)
    assert "composition" not in plan_v2["sentences"][0]["decision"]
    assert plan_v2["summary"]["compositions"] == 0


def test_name_super_reaches_timeline_item_as_layer():
    pack = load_pack("general")
    scenario = {"subject": "gabby_petito",
                "subject_name": "Gabby Petito"}
    plan_v2, _m = visual_director.reconcile_visual_plan(
        _plan_v1([_row(1)]),
        {1: _covered_outcome(1, 0, scenario)},
        genre_pack=pack, bible=BIBLE_PERSON)
    timeline_data = {"scenes": [{
        "scene_number": 1,
        "items": [{"sentence_index": 0, "duration": 4.0}],
    }]}
    stamped = composition.apply_decisions_to_timeline(
        timeline_data, plan_v2)
    assert stamped == 1
    item = timeline_data["scenes"][0]["items"][0]
    plan = item["composition"]
    assert plan["pattern"] == "lower_third_card"
    layer = plan["layers"][0]
    assert layer["kind"] == "lower_third_card"
    assert layer["source"]["label"] == "Gabby Petito"
    assert layer["source"]["sublabel"] == "victim"
    assert layer["validation"]["passed"], layer["validation"]
    pos = layer["position"]
    band = (pos["x"], pos["y"], pos["x"] + pos["w"],
            pos["y"] + pos["h"])
    assert not composition._bands_intersect(
        band, composition.caption_band())


def test_lower_third_layer_renders_over_footage(tmp_path):
    # The composition render path itself: a stamped plan applied
    # to a real base clip produces a composed clip.
    import subprocess

    base = tmp_path / "base.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=c=0x34506e:s=1920x1080:r=30:d=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(base)],
        check=True)
    layer = composition.lower_third_layer(
        "Gabby Petito", "victim", start=0.0, duration=2.0)
    plan = {"pattern": "lower_third_card", "layers": [layer]}
    composition.validate_composition(plan, 2.0)
    out = tmp_path / "composed.mp4"
    ok, record = composition.apply_composition(
        base, plan, out, tmp_path / "work")
    assert ok, record
    assert record["applied"][0]["kind"] == "lower_third_card"
    assert out.exists()


# ---------------------------------------------------------------------------
# F3 — document examination surfaced through the real chain
# ---------------------------------------------------------------------------

def _doc_processed(tmp_path, evidence_type="document"):
    doc = tmp_path / "report.jpg"
    Image.new("RGB", (800, 1000), (225, 220, 205)).save(doc)
    asset = {
        "verification": {"accepted": True},
        "path": str(doc), "file_path": str(doc),
        "provider": "wikimedia",
        "source_url": "https://example.org/report",
        "title": "Moab police report", "type": "image",
    }
    if evidence_type is not None:
        asset["evidence_type"] = evidence_type
    return [{
        "scene": {"scene_number": 1, "sentences": []},
        "images": [asset], "videos": [],
    }]


def test_document_presentation_renders_with_genuine_document_asset(
        tmp_path):
    data = _data(processed=_doc_processed(tmp_path))
    entry = _entry("p5_0020", 1, 0, 1,
                   "The report documented the findings.",
                   templates=["document_presentation"])
    assets = _build(tmp_path, data, [entry])
    docs = [a for a in assets
            if a.get("template") == "document_presentation"]
    assert len(docs) == 1
    assert docs[0]["engine_inputs"]["document_source"]["source"] \
        == "wikimedia"
    assert docs[0]["validation"]["passed"]


def test_classified_file_renders_with_genuine_document_asset(tmp_path):
    data = _data(processed=_doc_processed(tmp_path))
    entry = _entry("p5_0021", 1, 0, 1,
                   "The report documented the findings.",
                   templates=["classified_file"])
    assets = _build(tmp_path, data, [entry])
    docs = [a for a in assets if a.get("template") == "classified_file"]
    assert len(docs) == 1
    assert docs[0]["validation"]["passed"]


def test_document_gate_refuses_ordinary_photo_asset(tmp_path):
    # The same scene, but the asset is an ordinary photo (no
    # document classification): the gate refuses, typed.
    data = _data(processed=_doc_processed(tmp_path,
                                          evidence_type=None))
    entry = _entry("p5_0022", 1, 0, 1,
                   "The report documented the findings.",
                   templates=["document_presentation"],
                   family_candidates=[])
    assets = _build(tmp_path, data, [entry])
    assert not [a for a in assets
                if a.get("template") == "document_presentation"]
    assert "document_presentation_disabled" in _skip_reasons(tmp_path)


# ---------------------------------------------------------------------------
# Registry — the rebuilt names exist, the removed names stay gone
# ---------------------------------------------------------------------------

def test_rebuilt_templates_registered_under_new_names():
    for name in ("chronology", "route", "locator",
                 "relationship_network"):
        assert name in engine_templates.TEMPLATES
    for removed in ("timeline", "route_map", "territory_map",
                    "border_map", "trade_route_map", "military_map"):
        assert removed not in engine_templates.TEMPLATES
