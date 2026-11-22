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
    """AM-010 stands: document_presentation is never produced —
    the directed template refuses with
    document_presentation_disabled. Run-11 fix 5b supersedes the
    OLD second half of this test (the refusal used to be the
    beat's final outcome): the Director's direction is a
    preference order, not a veto over the T5 closing-card floor,
    so a directed entry whose templates all refuse is served by
    the floor and the refusal is recorded in graphic_failures,
    superseded by the guarantee — exactly the run-11 sentences
    9/10 pattern."""
    _fake_renders(monkeypatch)

    from services.motion_engine import templates as engine_templates

    def fake_card(data, style, out_path, duration=5.0,
                  sentence_index=None, scene_number=None):
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 4096)
        return str(path), {
            "generator": "motion_engine", "template": "closing_card",
            "generated": True,
        }

    monkeypatch.setitem(
        engine_templates.TEMPLATES, "closing_card", fake_card
    )
    entry = _entry(
        "gm_0001", 1, 0, 1,
        "The report documented the findings in detail.",
        templates=["document_presentation"], families=[],
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    # The disabled template itself is never produced...
    assert [a["template"] for a in assets] == ["closing_card"]
    assert assets[0]["coverage_guarantee"] is True
    assert assets[0]["manifest_id"] == "gm_0001"
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    # ...the beat is not skipped...
    assert manifest["targets"]["skipped"] == []
    # ...and the directed refusal stands recorded, superseded by
    # the floor that served the beat.
    refusals = [
        f for f in manifest["graphic_failures"]
        if f["reason"] == "document_presentation_disabled"
    ]
    assert len(refusals) == 1
    assert refusals[0]["superseded_by"] == "coverage_guarantee:closing_card"


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


def _refuse_closing_card(monkeypatch):
    """Keep the T5 floor out of an assembler test: the floor is
    the final stage for any unserved target (run-11 fix 5b), so a
    test pinning an ASSEMBLER's refusal stubs the card to refuse
    too — the beat then stays unserved, as the test asserts."""
    from services.motion_engine import templates as engine_templates

    monkeypatch.setitem(
        engine_templates.TEMPLATES, "closing_card",
        lambda data, style, out_path, **kwargs: (
            None, {"reason": "no_text"}
        ),
    )


def test_territory_map_unresolved_place_assembles_nothing(tmp_path, monkeypatch):
    _refuse_closing_card(monkeypatch)
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


def test_evidence_board_with_path_objects_in_scene_records(tmp_path):
    """Regression (live failure, Petito production 2026-11-21):
    production scene records carry Path objects for file_path
    (process_scene_media), where the fixtures above use strings.
    The assembler copied the raw Path into the engine data, the
    asset record carried it into motion_graphics.json, and the
    manifest write died with 'Object of type PosixPath is not JSON
    serializable', failing the whole motion_designer stage. The
    assembled photo paths must be text, and the manifest must be
    written."""
    import json

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
                    "file_path": photo,  # a Path, as production hands over
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
    written."""
    import json

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


def test_evidence_board_without_verified_photos_assembles_nothing(
    tmp_path, monkeypatch
):
    _refuse_closing_card(monkeypatch)
    entry = _entry(
        "gm_0001", 2, 1, 4,
        "Crowds gathered at the crossing in silence.",
        templates=["evidence_board"], families=[],
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", manifest=_manifest([entry]),
    )
    assert assets == []
