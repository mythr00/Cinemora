"""Phase 6 — graphics pipeline: D1 removal (AM-001), manifest
consumption (AM-002), verified asset-data tier (AM-009), template
assemblers (AM-010).

D1's rule, pinned: a graphic exists ONLY for a targeted
requirement. No targets -> no graphics at all (no untargeted stat
sweep, no unconditional chronology/process renders, however
stat-shaped the script or eventful the bible).
"""

import json
from pathlib import Path

import pytest

from services import graphics_pipeline


def _data():
    return {
        "case_bible": {
            "timeline": [
                {"when": "June 1961", "what": "Tensions rise"},
                {"when": "13 August 1961", "what": "Border closed"},
                {"when": "9 November 1989", "what": "Wall falls"},
            ]
        },
        "scenes": [
            {
                "scene_number": 1,
                "sentences": [
                    {"text": "The wall cost $25 million to build.",
                     "sentence_number": 1},
                    {"text": "Guards watched the border day and night.",
                     "sentence_number": 2},
                ],
            },
            {
                "scene_number": 2,
                "sentences": [
                    {"text": "The tunnel ran 140 feet beneath the wall.",
                     "sentence_number": 3},
                    {"text": "Crowds gathered at the crossing in silence.",
                     "sentence_number": 4},
                ],
            },
        ],
    }


def _fake_renders(monkeypatch):
    """The AUD-21 logic-test pattern from test_targeted_graphics:
    fake file-writing renderers + the validator stubbed at its
    seam (real validation is covered by the real-render tests)."""

    def fake_write(path, *args, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)

    def fake_timeline(path, *args, **kwargs):
        fake_write(path)
        return "proportional"

    monkeypatch.setattr(graphics_pipeline, "_render_stat_graphic", fake_write)
    monkeypatch.setattr(
        graphics_pipeline, "_render_timeline_graphic", fake_timeline
    )
    monkeypatch.setattr(
        graphics_pipeline, "_render_process_graphic", fake_write
    )
    monkeypatch.setattr(
        graphics_pipeline, "validate_graphic_output",
        lambda path, expected_duration=None, **kwargs: {
            "passed": True, "failure_kind": None, "detail": "",
            "checks": {}, "probe": None,
        },
    )


def _manifest(entries, theme=None):
    return {"version": 1, "project_id": "p6", "theme": theme,
            "entries": entries}


def _entry(manifest_id, scene, index, number, text, templates=None,
           families=None, **extra):
    entry = {
        "manifest_id": manifest_id,
        "target": {
            "scene_number": scene, "sentence_index": index,
            "sentence_number": number,
        },
        "text": text,
        "scenario": {},
        "purpose": "coverage_fallback",
        "template_candidates": templates or [],
        "family_candidates": families if families is not None else ["stat"],
        "theme": None,
        "fact_basis": "script_claim",
        "evidence_ref": None,
        "verified_data": None,
    }
    entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# AM-001 / D1: nothing targeted, nothing generated
# ---------------------------------------------------------------------------

def test_no_targets_produces_zero_graphics(tmp_path):
    # The script is full of statistics and the bible full of
    # events — under the pre-D1 sweep + unconditional renders this
    # data produced up to 5 graphics. Now: zero.
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced",
    )
    assert assets == []
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert manifest["count"] == 0
    assert manifest["targets"]["requested"] == 0


def test_empty_manifest_produces_zero_graphics(tmp_path):
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([]),
    )
    assert assets == []
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert manifest["targeting"]["mode"] == "graphics_manifest"
    assert manifest["targeting"]["manifest_entries"] == 0


def test_manifest_entry_without_stat_family_gets_no_stat(tmp_path, monkeypatch):
    """A dated, numbered sentence whose manifest entry permits
    only the timeline family: no stat is attempted despite the
    number, and no process graphic appears despite the bible."""
    _fake_renders(monkeypatch)
    entry = _entry(
        "gm_0001", 1, 0, 1,
        "On 13 August 1961 the wall cost $25 million to build.",
        families=["timeline"],
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    types = {a["graphic_type"] for a in assets}
    assert "stat" not in types
    assert "process" not in types
    # The chronology graphic WAS claimed by its anchor target.
    assert types == {"timeline"}
    assert assets[0]["manifest_id"] == "gm_0001"
    assert assets[0]["targeted"] is True


# ---------------------------------------------------------------------------
# AM-002: a starved beat gets exactly its targeted graphic
# ---------------------------------------------------------------------------

def test_starved_beat_gets_exactly_its_manifest_graphic(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    entry = _entry(
        "gm_0001", 2, 0, 3,
        "The tunnel ran 140 feet beneath the wall.",
        families=["stat"],
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    assert len(assets) == 1
    asset = assets[0]
    assert asset["graphic_type"] == "stat"
    assert asset["manifest_id"] == "gm_0001"
    assert asset["targeted"] is True
    assert asset["validation"]["passed"] is True
    assert asset["delivered"] is True
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    outcomes = manifest["targets"]["outcomes"]
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "generated"
    assert outcomes[0]["manifest_id"] == "gm_0001"


def test_manifest_restricts_engine_templates(tmp_path, monkeypatch):
    """An entry whose candidates name only ring_badges must not
    produce the bar_chart the legacy feature chain would pick."""
    _fake_renders(monkeypatch)
    entry = _entry(
        "gm_0001", 1, 0, 1,
        "Exports were 40 percent, up from 25 percent before.",
        templates=["ring_badges"], families=[],
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    assert len(assets) == 1
    assert assets[0]["template"] == "ring_badges"


# ---------------------------------------------------------------------------
# AM-009: verified asset-data tier through the pipeline
# ---------------------------------------------------------------------------

def test_verified_data_stat_carries_asset_fact_basis(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    entry = _entry(
        "gm_0001", 2, 0, 3,
        "The tunnel ran beneath the wall.",
        families=["stat"],
        fact_basis="verified_asset_metadata",
        evidence_ref={"asset_id": "asset-77", "source": "wikimedia"},
        verified_data={"display": "140 FEET", "kind": "length"},
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    assert len(assets) == 1
    asset = assets[0]
    assert asset["fact_basis"] == graphics_pipeline.FACT_BASIS_ASSET
    assert asset["evidence_ref"]["asset_id"] == "asset-77"
    assert asset["stat_display"] == "140 FEET"
    assert asset["stat_source"] == "verified_asset_metadata"
    recipe_refs = asset["graphic_recipe"]["source_references"]
    assert recipe_refs[0]["type"] == "verified_asset_metadata"


def test_verified_data_without_evidence_ref_is_not_promoted(tmp_path, monkeypatch):
    """verified_data with NO evidence_ref: the verified path is
    not taken — the graphic (if any) is built from the script and
    stamped script_claim."""
    _fake_renders(monkeypatch)
    entry = _entry(
        "gm_0001", 2, 0, 3,
        "The tunnel ran 140 feet beneath the wall.",
        families=["stat"],
        verified_data={"display": "999 FEET", "kind": "length"},
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    assert len(assets) == 1
    asset = assets[0]
    assert asset["fact_basis"] == graphics_pipeline.FACT_BASIS_SCRIPT
    assert asset["stat_display"] != "999 FEET"
    assert "evidence_ref" not in asset


# ---------------------------------------------------------------------------
# AM-010: document_presentation stays disabled, explicitly
# ---------------------------------------------------------------------------

def test_document_presentation_manifest_entry_refused(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    entry = _entry(
        "gm_0001", 1, 0, 1,
        "The report documented the findings in detail.",
        templates=["document_presentation"], families=[],
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    assert assets == []
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    skips = manifest["targets"]["skipped"]
    assert len(skips) == 1
    assert skips[0]["reason"] == "document_presentation_disabled"


# ---------------------------------------------------------------------------
# AM-010: real assemblers through the real engine path
# ---------------------------------------------------------------------------

def test_territory_map_assembler_renders(tmp_path):
    """Real render, real validation: a target whose recorded
    place resolves to a dataset country produces a territory_map
    graphic through the production chain."""
    entry = _entry(
        "gm_0001", 1, 0, 1, "The army crossed France in the spring.",
        templates=["territory_map"], families=[],
        scenario={"place": "France"},
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    assert len(assets) == 1
    asset = assets[0]
    assert asset["template"] == "territory_map"
    assert asset["validation"]["passed"] is True
    assert Path(asset["file_path"]).exists()
    assert asset["manifest_id"] == "gm_0001"


def test_territory_map_unresolved_place_assembles_nothing(tmp_path):
    entry = _entry(
        "gm_0001", 1, 0, 1, "They gathered in a nameless valley.",
        templates=["territory_map"], families=[],
        scenario={"place": "Atlantis"},
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    assert assets == []


def test_evidence_board_assembler_renders(tmp_path):
    """Real render: the scene's verified photographs become an
    evidence board for the starved sentence."""
    from PIL import Image

    photo = tmp_path / "verified_photo.jpg"
    Image.new("RGB", (640, 480), (120, 60, 30)).save(photo)
    data = _data()
    data["processed_scenes"] = [
        {
            "scene": {"scene_number": 2},
            "images": [
                {
                    "verification": {"accepted": True},
                    "file_path": str(photo),
                    "title": "The tunnel entrance",
                },
            ],
            "videos": [],
        },
    ]
    entry = _entry(
        "gm_0001", 2, 1, 4,
        "Crowds gathered at the crossing in silence.",
        templates=["evidence_board"], families=[],
    )
    out = tmp_path / "graphics"
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, out, mode="balanced", manifest=_manifest([entry]),
    )
    assert len(assets) == 1
    asset = assets[0]
    assert asset["template"] == "evidence_board"
    assert asset["validation"]["passed"] is True
    assert Path(asset["file_path"]).exists()


def test_evidence_board_without_verified_photos_assembles_nothing(tmp_path):
    entry = _entry(
        "gm_0001", 2, 1, 4,
        "Crowds gathered at the crossing in silence.",
        templates=["evidence_board"], families=[],
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    assert assets == []
