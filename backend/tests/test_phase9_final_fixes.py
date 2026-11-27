"""Phase 9 final fixes — pins (Motion Graphics Programme,
2026-11-27).

Two findings from the creative test suite's inspection pass
(~/workspace/cinemora-audit/motion-graphics-programme/
TEST-SUITE.md), fixed here and pinned so neither can return:

FIX 1 — item 11, mechanical_cutaway FAIL. The treatment
rendered the same layered band stack as cross_section inside a
bolted frame — no mechanism, nothing cut away — under a name
that promises a cutaway: misleading presentation. It is now
UNPRODUCIBLE, following the P0 removal pattern: no registry
entry, no pipeline assembler, no Director candidacy (layers cue
or problem preference), no genre-pack motif/affinity/assembled
listing, and the recipe/regeneration dispatch refuses it. It
survives only as a specification in the programme record
(Level 2/3 — a real cutaway requires recorded subject geometry
the scenario contract does not yet carry). cross_section,
exploded_view and blueprint_section are untouched survivors.

FIX 2 — item 13, labelled_diagram PARTIAL. The callouts
floated over an empty themed background: the anchors labelled
no visible subject. The recorded ``diagram`` contract already
carries each part's relative position (``at``) or order, which
is sufficient to draw the subject as a schematic: every part
now renders as a drawn block at its recorded position / order
slot and the callout anchors to that block (no new contract
fields, no invented parts). The settled-frame test below reads
actual pixels: each part's block region must be substantially
non-background — before the fix it measured ~0 (a 17 px dot on
empty ground); it now measures ~1.0.
"""

import json
import statistics

import pytest

from services import genre_packs, graphics_pipeline, visual_director
from services.motion_engine import frame_inspection
from services.motion_engine import templates as engine_templates
from services.motion_engine import templates_v2, templates_v3, templates_v4
from services.motion_engine.templates_v5 import labelled_diagram

REMOVED = "mechanical_cutaway"
SURVIVORS = ("cross_section", "exploded_view", "blueprint_section")


# ---------------------------------------------------------------------------
# Fixtures (mirroring test_p0_removal_pins' shapes)
# ---------------------------------------------------------------------------

def _data():
    return {
        "case_bible": {
            "title": "The Port",
            "timeline": [
                {"when": "June 1961", "what": "Tensions rise"},
                {"when": "13 August 1961", "what": "Border closed"},
            ],
            "entities": [{"canonical": "Gabby Petito", "type": "person"}],
        },
        "scenes": [{
            "scene_number": 1,
            "sentences": [
                {"text": "The section shows the load path.",
                 "sentence_number": 1},
            ],
        }],
        "processed_scenes": [{
            "scene": {"scene_number": 1, "sentences": []},
            "videos": [],
            "images": [],
            "asset_stats": {"missing_visuals": []},
        }],
    }


def _entry(templates, scenario=None):
    return {
        "manifest_id": "f9_0001",
        "target": {"scene_number": 1, "sentence_index": 0,
                   "sentence_number": 1},
        "text": "The section shows the load path.",
        "scenario": scenario or {},
        "purpose": "coverage_fallback",
        "template_candidates": templates,
        "family_candidates": [],
        "theme": None,
        "fact_basis": "script_claim",
        "evidence_ref": None,
        "verified_data": None,
    }


def _manifest(entries):
    return {"version": 1, "project_id": "f9", "theme": None,
            "entries": entries}


_LAYERS = {"layers": [{"label": "Deck", "depth": 1},
                      {"label": "Keel", "depth": 4}]}


# ---------------------------------------------------------------------------
# FIX 1a — registry: the treatment does not exist anywhere
# ---------------------------------------------------------------------------

def test_mechanical_cutaway_absent_from_every_registry():
    registries = (
        engine_templates.TEMPLATES,
        templates_v2.V2_TEMPLATES,
        templates_v3.V3_TEMPLATES,
        templates_v4.V4_TEMPLATES,
    )
    for registry in registries:
        assert REMOVED not in registry
    assert not hasattr(templates_v2, REMOVED)
    # The honest schematic family survives untouched.
    for survivor in SURVIVORS:
        assert survivor in engine_templates.TEMPLATES, survivor


# ---------------------------------------------------------------------------
# FIX 1b — assembler: a manifest entry naming it produces nothing
# ---------------------------------------------------------------------------

def test_manifest_entry_naming_mechanical_cutaway_produces_nothing(
    tmp_path
):
    entry = _entry([REMOVED], scenario=_LAYERS)
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
# FIX 1c — Director: candidacy and problem preference never name it
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


def test_director_never_proposes_mechanical_cutaway():
    candidates = _candidates(
        "The section shows the load path.", scenario=_LAYERS)
    assert REMOVED not in candidates
    # The surviving schematic variants are still proposed on the
    # recorded-layers cue.
    assert "exploded_view" in candidates
    assert "blueprint_section" in candidates
    for preference in (
        visual_director.PROBLEM_TREATMENT_PREFERENCE.values()
    ):
        assert REMOVED not in preference


# ---------------------------------------------------------------------------
# FIX 1d — genre packs: no motif, affinity or assembled listing
# ---------------------------------------------------------------------------

def test_genre_packs_never_reference_mechanical_cutaway():
    assert REMOVED not in genre_packs.ASSEMBLED_TEMPLATES
    for pack in genre_packs.available_packs():
        assert REMOVED not in (pack.get("graphics_motifs") or []), (
            pack["id"]
        )
        profile = pack.get("visual_profile") or {}
        assert REMOVED not in (profile.get("template_affinities") or {}), (
            pack["id"]
        )
        failures = genre_packs.validate_visual_profile(
            profile, pack["id"]
        )
        assert failures == [], (pack["id"], failures)
    # Survivors keep their pack presence.
    assert "exploded_view" in genre_packs.ASSEMBLED_TEMPLATES
    assert "blueprint_section" in genre_packs.ASSEMBLED_TEMPLATES


# ---------------------------------------------------------------------------
# FIX 1e — recipe/regeneration dispatch refuses it
# ---------------------------------------------------------------------------

def test_recipe_dispatch_refuses_mechanical_cutaway(tmp_path):
    recipe = {
        "family": "motion_engine", "template": REMOVED,
        "inputs": {"layers": [{"label": "Deck"}]},
        "duration": 3.0, "regenerable": True,
        "non_regenerable_reason": None,
    }
    rendered, meta = graphics_pipeline.render_graphic_from_recipe(
        recipe, tmp_path / "regen.mp4"
    )
    assert rendered is None
    assert meta["reason"] == f"unknown_template:{REMOVED}"


# ---------------------------------------------------------------------------
# FIX 2 — labelled_diagram draws the subject its callouts label
# ---------------------------------------------------------------------------

_PARTS = [
    {"name": "Piston", "description": "Drives the shaft",
     "at": [0.25, 0.4]},
    {"name": "Valve", "description": "Admits steam",
     "at": [0.6, 0.4]},
    {"name": "Flywheel", "description": "Smooths the stroke",
     "at": [0.8, 0.65]},
]

# The template's own layout constants (templates_v5): the
# diagram area and the subject-block size. The test recomputes
# each part's block from the recorded positions exactly as the
# template lays it out, then reads the rendered pixels there.
_AREA = (130, 190, 1920 - 130, 1080 - 150)
_BLOCK_W, _BLOCK_H = 168, 92


def _block_for(at):
    cx = _AREA[0] + at[0] * (_AREA[2] - _AREA[0])
    cy = _AREA[1] + at[1] * (_AREA[3] - _AREA[1])
    bx0 = min(max(cx - _BLOCK_W / 2, _AREA[0]), _AREA[2] - _BLOCK_W)
    by0 = min(max(cy - _BLOCK_H / 2, _AREA[1]), _AREA[3] - _BLOCK_H)
    return cx, cy, bx0, by0


def _non_background_fraction(frame, cx, cy, bx0, by0, bg):
    """Fraction of a part's block interior (inset, anchor dot
    excluded) whose pixels differ from the frame background by
    more than the background texture's own variation (grid
    +14, noise ~±6; threshold 25). Before Fix 2 this measured
    ~0 — a lone dot on empty ground."""
    pixels = frame.load()
    total = hit = 0
    for y in range(int(by0) + 8, int(by0) + _BLOCK_H - 8):
        for x in range(int(bx0) + 8, int(bx0) + _BLOCK_W - 8):
            if (x - cx) ** 2 + (y - cy) ** 2 < 28 ** 2:
                continue
            r, g, b = pixels[x, y]
            total += 1
            if max(abs(r - bg[0]), abs(g - bg[1]),
                   abs(b - bg[2])) > 25:
                hit += 1
    return hit / total if total else 0.0


def test_labelled_diagram_settled_frame_has_a_visible_subject(tmp_path):
    path, meta = labelled_diagram(
        {"parts": _PARTS, "title": "Steam Engine"},
        "editorial", tmp_path / "diagram.mp4", duration=2.0)
    assert path is not None
    assert meta["inputs"]["subject_blocks"] is True
    frame = frame_inspection.extract_frame(path, at_fraction=0.9)
    assert frame is not None
    pixels = frame.load()
    channels = ([], [], [])
    for y in range(10, 50):
        for x in range(10, 90):
            for i, value in enumerate(pixels[x, y]):
                channels[i].append(value)
    bg = tuple(statistics.median(c) for c in channels)
    for part in _PARTS:
        cx, cy, bx0, by0 = _block_for(part["at"])
        fraction = _non_background_fraction(frame, cx, cy, bx0, by0, bg)
        assert fraction >= 0.5, (part["name"], fraction)
