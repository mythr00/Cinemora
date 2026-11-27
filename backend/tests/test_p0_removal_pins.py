"""P0 removal pins — Motion Graphics Programme (2026-11-27).

The founder's P0 ruling: REMOVE ALL low-quality motion graphics
completely (templates + selection paths). These tests pin the
removal so none of it can silently come back:

1. None of the removed type names can be produced via the
   template registry, the pipeline assemblers (manifest entries
   naming them produce nothing), the Visual Director's
   candidates, the genre packs, or the recipe/regeneration
   dispatch; the native stat/process/timeline renderers and
   their pipeline entry points do not exist.
2. The T5 coverage guarantee's terminal outcomes are only
   footage-fallback (the beat is left unserved by graphics and
   jobs' annotation records the skip) or the typed
   terminal_graphics_decline — the guarantee renders nothing
   and is never re-pointed at a surviving template.
3. Years/dates are excluded from statistic candidacy for the
   surviving chart family.

The ON/OFF gate is pinned untouched: mode="off" produces no
graphics at all, exactly as before the removal.
"""

import inspect
import json

import pytest

import jobs
from services import genre_packs, graphics_pipeline, story_understanding
from services import visual_director
from services.motion_engine import templates as engine_templates
from services.motion_engine import templates_v2, templates_v3, templates_v4

REMOVED_TEMPLATES = [
    "closing_card", "evidence_board", "red_string_board",
    "photo_parallax", "outcome_path", "milestone_waveform",
    "territory_map", "border_map", "route_map",
    "trade_route_map", "military_map",
]

REMOVED_NATIVE_FAMILIES = ["stat", "process", "timeline"]


def _data():
    return {
        "case_bible": {
            "title": "The Port",
            "timeline": [
                {"when": "June 1961", "what": "Tensions rise"},
                {"when": "13 August 1961", "what": "Border closed"},
                {"when": "9 November 1989", "what": "Wall falls"},
            ],
            "entities": [{"canonical": "Gabby Petito", "type": "person"}],
        },
        "scenes": [{
            "scene_number": 1,
            "sentences": [
                {"text": "In 1956 the first ship sailed from Newark.",
                 "sentence_number": 1},
                {"text": "First the rods bent, then the walkway fell.",
                 "sentence_number": 2},
                {"text": "The army advanced across France at dawn.",
                 "sentence_number": 3},
                {"text": "Crowds gathered at the crossing in silence.",
                 "sentence_number": 4},
            ],
        }],
        "processed_scenes": [{
            "scene": {"scene_number": 1, "sentences": []},
            "videos": [],
            "images": [
                {
                    "verification": {"accepted": True},
                    "file_path": "/x/p1.jpg", "title": "One",
                },
                {
                    "verification": {"accepted": True},
                    "file_path": "/x/p2.jpg", "title": "Two",
                },
            ],
            "asset_stats": {"missing_visuals": []},
        }],
    }


def _entry(manifest_id, index, number, text, templates, families=None,
           scenario=None):
    return {
        "manifest_id": manifest_id,
        "target": {"scene_number": 1, "sentence_index": index,
                   "sentence_number": number},
        "text": text,
        "scenario": scenario or {},
        "purpose": "coverage_fallback",
        "template_candidates": templates,
        "family_candidates": families or [],
        "theme": None,
        "fact_basis": "script_claim",
        "evidence_ref": None,
        "verified_data": None,
    }


def _manifest(entries):
    return {"version": 1, "project_id": "p0", "theme": None,
            "entries": entries}


# ---------------------------------------------------------------------------
# (1a) Registry: the removed templates do not exist anywhere
# ---------------------------------------------------------------------------

def test_removed_templates_absent_from_every_registry():
    registries = (
        engine_templates.TEMPLATES,
        templates_v2.V2_TEMPLATES,
        templates_v3.V3_TEMPLATES,
        templates_v4.V4_TEMPLATES,
    )
    for name in REMOVED_TEMPLATES:
        for registry in registries:
            assert name not in registry, name
    # The surviving vocabulary is intact at its post-removal size
    # plus the Phase 5 rebuilt treatments (2026-11-27), which
    # register under NEW names (chronology / route / locator /
    # relationship_network) — every REMOVED name above stays
    # absent, which is the pin this test exists for.
    assert len(engine_templates.TEMPLATES) == 46
    for survivor in (
        "archival_title", "document_presentation", "classified_file",
        "text_overlay", "text_highlight", "profile_reveal",
        "newspaper_montage", "newspaper_wall", "line_chart",
        "bar_chart", "stat_comparison", "data_gauge",
        "territory_expansion", "satellite_zoom", "heatmap",
        "layered_reveal", "exploded_view",
    ):
        assert survivor in engine_templates.TEMPLATES, survivor


def test_native_renderers_and_entry_points_do_not_exist():
    for attribute in (
        "_render_stat_graphic", "_render_timeline_graphic",
        "_render_process_graphic", "extract_stat", "_make_stat",
        "_terminal_candidates", "_parse_event_date",
        "_timeline_node_positions", "_timeline_label_rows",
        "_label_box", "_has_sequence_marker",
    ):
        assert not hasattr(graphics_pipeline, attribute), attribute
    # The max_stats plumbing existed solely for the native stat
    # family and is gone with it.
    signature = inspect.signature(
        graphics_pipeline.build_motion_graphics_assets
    )
    assert "max_stats" not in signature.parameters


# ---------------------------------------------------------------------------
# (1b) Assemblers: a manifest entry naming a removed template
# produces nothing — no substitution, no fallback graphic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("removed", REMOVED_TEMPLATES)
def test_manifest_entry_naming_removed_template_produces_nothing(
    tmp_path, removed
):
    entry = _entry(
        "p0_0001", 2, 3,
        "The army advanced across France at dawn.",
        templates=[removed],
        scenario={"place": "France"},
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    assert assets == []
    written = json.loads((tmp_path / "motion_graphics.json").read_text())
    outcome = written["targets"]["outcomes"][0]
    assert outcome["outcome"] == "skipped"
    assert outcome["terminal_decline"]["reason"] == (
        "terminal_graphics_decline"
    )


# ---------------------------------------------------------------------------
# (1c) Director: candidates and families never name the removed
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


def test_director_never_proposes_removed_templates():
    cue_cases = [
        ("The border was closed at dawn.", {"place": "France"}, {}),
        ("They drove from France to Germany.", {"place": "France"}, {}),
        ("The army advanced on the capital.", {"place": "France"}, {}),
        ("Trade through the port doubled.", {"place": "France"}, {}),
        ("In 1956 the first ship sailed.", {}, {}),
        ("First the rods bent, then the walkway fell.", {}, {}),
        ("Two photographs pinned the timeline.", {},
         {"scene_photo_count": 2}),
        ("The van crossed the desert.", {}, {"scene_photo_count": 1}),
        ("Levels rose 12 units, then 30 units, then 55 units.", {}, {}),
        ("It ended quietly that night.", {}, {"sentence_index": 0}),
    ]
    for text, scenario, extra in cue_cases:
        candidates = _candidates(text, scenario=scenario, **extra)
        for removed in REMOVED_TEMPLATES:
            assert removed not in candidates, (removed, text)


def test_director_families_are_schematic_only():
    row = {"primary_visual_intent": "graphic_explanation"}
    outcome = {
        "text": "In 1956 the first ship sailed, carrying 40 tons.",
        "scenario": {"place": "Newark"},
        "scene_photo_count": 3,
    }
    families = visual_director._family_candidates(row, outcome, None)
    assert families == ["event_schematic"]
    for removed in REMOVED_NATIVE_FAMILIES:
        assert removed not in families


def test_story_understanding_never_derives_timeline_intent():
    intents = story_understanding._derive_visual_intents(
        role="event",
        scenarios=[{"kind": "event", "specificity": "specific",
                    "subject": "the crossing", "era": "1961"}],
        concepts={},
        place="Berlin",
        time="13 August 1961",
        events=[{"when": "13 August 1961", "what": "Border closed"}],
    )
    kinds = {
        intent.get("kind") for intent in intents
        if isinstance(intent, dict)
    }
    assert "timeline" not in kinds


# ---------------------------------------------------------------------------
# (1d) Genre packs: no motif, affinity or assembled list names
# a removed template
# ---------------------------------------------------------------------------

def test_genre_packs_never_reference_removed_templates():
    removed = set(REMOVED_TEMPLATES)
    assert not (removed & genre_packs.ASSEMBLED_TEMPLATES)
    for pack in genre_packs.available_packs():
        assert not (removed & set(pack.get("graphics_motifs") or [])), (
            pack["id"]
        )
        profile = pack.get("visual_profile") or {}
        assert not (
            removed & set(profile.get("template_affinities") or {})
        ), pack["id"]
        failures = genre_packs.validate_visual_profile(
            profile, pack["id"]
        )
        assert failures == [], (pack["id"], failures)


# ---------------------------------------------------------------------------
# (1e) Recipe/regeneration dispatch refuses the removed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("family", REMOVED_NATIVE_FAMILIES)
def test_recipe_dispatch_refuses_removed_families(tmp_path, family):
    recipe = {
        "family": family, "renderer": "legacy", "template": None,
        "inputs": {"headline": "140 FEET", "events": [], "steps": []},
        "duration": 4.0, "regenerable": True,
        "non_regenerable_reason": None,
    }
    with pytest.raises(ValueError):
        graphics_pipeline.render_graphic_from_recipe(
            recipe, tmp_path / "regen.mp4"
        )


@pytest.mark.parametrize("removed", REMOVED_TEMPLATES)
def test_recipe_dispatch_refuses_removed_templates(tmp_path, removed):
    recipe = {
        "family": "motion_engine", "template": removed,
        "inputs": {}, "duration": 3.0, "regenerable": True,
        "non_regenerable_reason": None,
    }
    rendered, meta = graphics_pipeline.render_graphic_from_recipe(
        recipe, tmp_path / "regen.mp4"
    )
    assert rendered is None
    assert meta["reason"] == f"unknown_template:{removed}"


# ---------------------------------------------------------------------------
# (2) The guarantee's terminal outcomes: footage fallback or the
# typed decline — never a manufactured graphic
# ---------------------------------------------------------------------------

def _starved_data():
    """The shared fixture with NO verified photos anywhere — a
    beat nothing can serve (with photos present, the surviving
    layered_reveal assembler legitimately serves photo beats)."""
    data = _data()
    data["processed_scenes"][0]["images"] = []
    return data


def test_starved_beat_terminal_outcome_is_decline_only(tmp_path):
    targets = [
        {"scene_number": 1, "sentence_index": 3, "sentence_number": 4,
         "text": "Crowds gathered at the crossing in silence.",
         "scenario": {}},
    ]
    assets = graphics_pipeline.build_motion_graphics_assets(
        _starved_data(), tmp_path, mode="balanced", targets=targets,
    )
    assert assets == []
    written = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert written["targets"]["generated"] == 0
    outcome = written["targets"]["outcomes"][0]
    assert outcome["outcome"] == "skipped"
    assert not str(outcome.get("reason") or "").startswith(
        "coverage_guarantee"
    )
    decline = outcome["terminal_decline"]
    assert decline["reason"] == "terminal_graphics_decline"
    assert set(decline["attempted_families"]) <= {
        "event_schematic", "motion_engine",
    }
    # The same decline rides the skip record.
    skip = written["targets"]["skipped"][0]
    assert skip["terminal_decline"] == decline
    # No asset anywhere claims the guarantee.
    assert not [
        a for a in written["assets"] if a.get("coverage_guarantee")
    ]


def test_jobs_annotation_records_decline_for_footage_fallback(tmp_path):
    """Terminal outcome (a): the beat graphics declined is left
    to footage — jobs stamps the missing-visual record with the
    skipped outcome and its typed decline, which is what the
    tier ladder / coverage reporting then acts on."""
    targets = [
        {"scene_number": 1, "sentence_index": 3, "sentence_number": 4,
         "text": "Crowds gathered at the crossing in silence.",
         "scenario": {}},
    ]
    graphics_pipeline.build_motion_graphics_assets(
        _starved_data(), tmp_path, mode="balanced", targets=targets,
    )
    processed = [{
        "scene": {"scene_number": 1, "sentences": []},
        "videos": [], "images": [],
        "asset_stats": {
            "missing_visuals": [{
                "scene_number": 1, "sentence_number": 4,
                "reason": "no_verified_asset",
            }],
        },
    }]
    annotated = jobs._annotate_missing_visual_graphics_outcomes(
        processed, tmp_path / "motion_graphics.json"
    )
    assert annotated == 1
    entry = processed[0]["asset_stats"]["missing_visuals"][0]
    outcome = entry["graphics_outcome"]
    assert outcome["outcome"] == "skipped"
    assert outcome["terminal_decline"]["reason"] == (
        "terminal_graphics_decline"
    )


def test_archival_title_survives_only_as_directed_template(tmp_path):
    """archival_title is a survivor — but ONLY as a
    Director-proposed title treatment (a manifest entry naming
    it), never as a guarantee floor for an undirected beat."""
    entry = _entry(
        "p0_0009", 0, 1,
        "In 1956 the first ship sailed from Newark.",
        templates=["archival_title"],
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    titles = [a for a in assets if a.get("template") == "archival_title"]
    assert len(titles) == 1
    assert not titles[0].get("coverage_guarantee")


# ---------------------------------------------------------------------------
# (3) Years/dates are not statistic candidates for the surviving
# chart family
# ---------------------------------------------------------------------------

def test_years_and_dates_excluded_from_statistic_candidacy():
    year_only = [
        "The wall stood from 1961-1989 in the city.",
        "In 1961 the border closed.",
        "Between 1956 and 2020 the port changed completely.",
        "It happened in August 1961, then again in November 1989.",
    ]
    for text in year_only:
        assert graphics_pipeline.extract_comparison(text) is None, text
        assert graphics_pipeline._series_from_text(text) is None, text
        assert graphics_pipeline._percent_value(text) is None, text


# ---------------------------------------------------------------------------
# ON/OFF gate — untouched by the removal
# ---------------------------------------------------------------------------

def test_off_mode_produces_no_graphics_at_all(tmp_path):
    entry = _entry(
        "p0_off", 0, 1,
        "In 1956 the first ship sailed from Newark.",
        templates=["archival_title"],
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="off", manifest=_manifest([entry]),
    )
    assert assets == []


def test_off_gate_reads_the_runs_own_records():
    assert jobs._motion_graphics_disabled_for_run(
        {"motion_graphics": {"enabled": False}}, {}
    ) is True
    assert jobs._motion_graphics_disabled_for_run(
        {}, {"motion_graphics_enabled": False}
    ) is True
    assert jobs._motion_graphics_disabled_for_run(
        {}, {"production_config_resolved": {"graphics": {"mode": "off"}}}
    ) is True
    assert jobs._motion_graphics_disabled_for_run({}, {}) is False
