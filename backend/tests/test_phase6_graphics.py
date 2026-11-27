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
    """The validator stubbed at its seam (real validation is
    covered by the real-render tests). (P0 removal, 2026-11-27:
    this fixture used to also fake the native stat/timeline/
    process renderers — those families are removed completely,
    so there is nothing left to fake.)"""
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
        "family_candidates": families if families is not None else [],
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


def test_manifest_entry_naming_removed_families_gets_no_graphic(
    tmp_path, monkeypatch
):
    """P0 removal pin (replaces the pre-removal stat/timeline
    family test): a dated, numbered sentence whose manifest entry
    permits only REMOVED families (timeline/stat/process) gets NO
    graphic at all — the families no longer exist, nothing is
    substituted, and the beat's outcome is a typed terminal
    decline."""
    _fake_renders(monkeypatch)
    entry = _entry(
        "gm_0001", 1, 0, 1,
        "On 13 August 1961 the wall cost $25 million to build.",
        families=["timeline", "stat", "process"],
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    assert assets == []
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    outcomes = manifest["targets"]["outcomes"]
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "skipped"
    assert outcomes[0]["terminal_decline"]["reason"] == (
        "terminal_graphics_decline"
    )


# ---------------------------------------------------------------------------
# AM-002: a starved beat gets exactly its targeted graphic
# ---------------------------------------------------------------------------

def test_starved_beat_gets_exactly_its_manifest_graphic(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    entry = _entry(
        "gm_0001", 2, 0, 3,
        "The pumps ran at 62 percent of capacity.",
        templates=["data_gauge"], families=[],
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    assert len(assets) == 1
    asset = assets[0]
    assert asset["template"] == "data_gauge"
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

def test_verified_data_no_longer_produces_a_stat_graphic(
    tmp_path, monkeypatch
):
    """P0 removal pin (replaces the AM-009 stat tests): the
    verified-data path fed the native stat family, which is
    removed completely. An entry carrying verified_data +
    evidence_ref naming the stat family produces NO graphic —
    the verified value is never promoted onto a card — and the
    beat declines with the typed terminal decline."""
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
    assert assets == []
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    outcomes = manifest["targets"]["outcomes"]
    assert outcomes[0]["outcome"] == "skipped"
    assert outcomes[0]["terminal_decline"]["reason"] == (
        "terminal_graphics_decline"
    )


def test_verified_data_without_evidence_ref_is_not_promoted(
    tmp_path, monkeypatch
):
    """verified_data with NO evidence_ref was never promoted even
    before the removal; after it, the stat family does not exist
    at all, so the entry produces no graphic and no asset carries
    a stat display or an evidence promotion."""
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
    assert assets == []
    assert all("stat_display" not in a for a in assets)


# ---------------------------------------------------------------------------
# AM-010: document_presentation stays disabled, explicitly
# ---------------------------------------------------------------------------

def test_document_presentation_manifest_entry_refused(tmp_path, monkeypatch):
    """AM-010 stands: document_presentation is never produced —
    the directed template refuses with
    document_presentation_disabled. P0 rewiring supersedes the
    OLD second half of this test (a closing-card floor used to
    serve the beat anyway): there is no floor anymore, so the
    beat is skipped with the typed terminal decline and the
    directed refusal stands recorded in graphic_failures,
    NOT superseded by anything."""
    _fake_renders(monkeypatch)
    entry = _entry(
        "gm_0001", 1, 0, 1,
        "The report documented the findings in detail.",
        templates=["document_presentation"], families=[],
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    # The disabled template is never produced, and no floor
    # graphic replaces it.
    assert assets == []
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    # The beat is skipped: the explicit founder-rule refusal is
    # the recorded skip reason, and the terminal decline rides
    # on the same record — nothing supersedes either.
    assert len(manifest["targets"]["skipped"]) == 1
    skip = manifest["targets"]["skipped"][0]
    assert skip["reason"] == "document_presentation_disabled"
    assert skip["terminal_decline"]["reason"] == (
        "terminal_graphics_decline"
    )


# ---------------------------------------------------------------------------
# AM-010: real assemblers through the real engine path
# ---------------------------------------------------------------------------

def test_layered_reveal_assembler_renders(tmp_path):
    """Real render: the scene's verified photographs become a
    layered reveal for the starved sentence. (P0 removal,
    2026-11-27: this test previously rendered evidence_board,
    removed completely; the surviving photo-backed assembler
    carries the same real-render pin.)"""
    from PIL import Image

    photo = tmp_path / "verified_photo.jpg"
    Image.new("RGB", (640, 480), (120, 60, 30)).save(photo)
    photo2 = tmp_path / "verified_photo2.jpg"
    Image.new("RGB", (640, 480), (30, 60, 120)).save(photo2)
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
                {
                    "verification": {"accepted": True},
                    "file_path": str(photo2),
                    "title": "The crossing",
                },
            ],
            "videos": [],
        },
    ]
    entry = _entry(
        "gm_0001", 2, 1, 4,
        "Crowds gathered at the crossing in silence.",
        templates=["layered_reveal"], families=[],
    )
    out = tmp_path / "graphics"
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, out, mode="balanced", manifest=_manifest([entry]),
    )
    assert len(assets) == 1
    asset = assets[0]
    assert asset["template"] == "layered_reveal"
    assert asset["validation"]["passed"] is True
    assert Path(asset["file_path"]).exists()


def test_layered_reveal_with_path_objects_in_scene_records(tmp_path):
    """Regression (live failure, Petito production 2026-11-21):
    production scene records carry Path objects for file_path
    (process_scene_media), where the fixtures above use strings.
    An assembler copying the raw Path into the engine data put it
    in the asset record and motion_graphics.json, and the
    manifest write died with 'Object of type PosixPath is not JSON
    serializable', failing the whole motion_designer stage. The
    assembled photo paths must be text, and the manifest must be
    written. (P0 removal, 2026-11-27: re-pointed from the removed
    evidence_board assembler to the surviving layered_reveal
    assembler, which carries the same discipline.)"""
    import json

    from PIL import Image

    photo = tmp_path / "verified_photo.jpg"
    Image.new("RGB", (640, 480), (120, 60, 30)).save(photo)
    photo2 = tmp_path / "verified_photo2.jpg"
    Image.new("RGB", (640, 480), (30, 60, 120)).save(photo2)
    data = _data()
    data["processed_scenes"] = [
        {
            "scene": {"scene_number": 2},
            "images": [
                {
                    "verification": {"accepted": True},
                    "file_path": photo,  # a Path, as production hands over
                    "title": "The tunnel entrance",
                },
                {
                    "verification": {"accepted": True},
                    "file_path": photo2,  # a Path, as production hands over
                    "title": "The crossing",
                },
            ],
            "videos": [],
        },
    ]
    entry = _entry(
        "gm_0001", 2, 1, 4,
        "Crowds gathered at the crossing in silence.",
        templates=["layered_reveal"], families=[],
    )
    out = tmp_path / "graphics"
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, out, mode="balanced", manifest=_manifest([entry]),
    )
    assert len(assets) == 1
    photos = assets[0]["engine_inputs"]["photos"]
    assert photos and all(isinstance(p["path"], str) for p in photos)
    manifest = json.loads((out / "motion_graphics.json").read_text())
    assert manifest["count"] == 1


def test_engine_record_style_is_json_safe_with_theme_tokens(tmp_path):
    """Regression (live failure, Petito production 2026-11-21, second
    leak of the PosixPath class): in manifest mode a target's style
    is the resolved theme TOKEN DICT, whose font/font_bold values
    are Path objects. The engine record stamped it verbatim (and
    the recipe embedded it), so motion_graphics.json died with
    'Object of type PosixPath is not JSON serializable' even after
    the evidence-board photo paths were stringified. Records must
    carry the style with text font paths, and the manifest must be
    written. (P0 removal, 2026-11-27: re-pointed from the removed
    evidence_board assembler to layered_reveal.)"""
    import json

    from PIL import Image

    photo = tmp_path / "verified_photo.jpg"
    Image.new("RGB", (640, 480), (120, 60, 30)).save(photo)
    photo2 = tmp_path / "verified_photo2.jpg"
    Image.new("RGB", (640, 480), (30, 60, 120)).save(photo2)
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
                {
                    "verification": {"accepted": True},
                    "file_path": str(photo2),
                    "title": "The crossing",
                },
            ],
            "videos": [],
        },
    ]
    entry = _entry(
        "gm_0001", 2, 1, 4,
        "Crowds gathered at the crossing in silence.",
        templates=["layered_reveal"], families=[],
    )
    out = tmp_path / "graphics"
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, out, mode="balanced",
        manifest=_manifest([entry], theme="editorial_dark"),
    )
    assert len(assets) == 1
    style = assets[0]["style"]
    assert isinstance(style, dict)
    assert isinstance(style["font"], str)
    assert isinstance(style["font_bold"], str)
    recipe_style = assets[0]["graphic_recipe"]["style"]
    assert isinstance(recipe_style["font"], str)
    manifest = json.loads((out / "motion_graphics.json").read_text())
    assert manifest["count"] == 1


def test_layered_reveal_without_verified_photos_assembles_nothing(
    tmp_path
):
    # No floor serves the beat either (P0 rewiring): an assembler
    # refusal is terminal — the beat is left to footage.
    entry = _entry(
        "gm_0001", 2, 1, 4,
        "Crowds gathered at the crossing in silence.",
        templates=["layered_reveal"], families=[],
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    assert assets == []
