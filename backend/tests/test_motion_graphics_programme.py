"""Merged Motion Graphics programme — Phase 1 + Phase 2 tests.

Phase 1 (graphics truthfulness and admission control):
- MG-05: ONE post-render validation contract for every graphic
  family; invalid graphics are never registered, never reach the
  timeline, and the failure is recorded with its original detail.
- MG-07: jobs attach only graphics explicitly marked delivered AND
  valid.
- MG-08: skip reasons come from a closed typed vocabulary; each
  failure path records its own reason (the historical
  "no_stat_value" conflation is gone).
- MG-09: stat cards are stamped by fact basis (script claims are
  never stamped VERIFIED); ranges preserve both endpoints or are
  refused; the document template is refused without a genuine
  document source.
- MG-11: editor regeneration exceptions map to the structured 422
  contract.

Phase 2 (recorded inputs and editor regeneration):
- MG-03: every production graphic records a complete regeneration
  recipe; the recipe survives builder -> attach -> timeline ->
  serialisation -> editor revision; a production-shaped graphic
  regenerates in the editor from its recorded inputs, at the
  recorded timeline window, validated, with the previous valid
  asset preserved on failure.

The validator tests use REAL files (real ffmpeg renders, real
corrupt files) — never the stubbed validator used by the logic
fixtures in the older graphics test files.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import jobs
from services import graphics_pipeline
from services.graphics_pipeline import (
    GRAPHIC_SKIP_REASONS,
    extract_comparison,
    validate_graphic_output,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_video(path, size="1920x1080", seconds=2, rate=30, pattern="testsrc2"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
            "-i", f"{pattern}=size={size}:rate={rate}",
            "-t", str(seconds), "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, capture_output=True,
    )
    return path


def _stub_validator(monkeypatch):
    """The logic-test seam (see the older graphics fixtures): fake
    renderers write non-media bytes, so the validator is stubbed;
    the REAL validator is exercised by the tests below."""
    monkeypatch.setattr(
        graphics_pipeline, "validate_graphic_output",
        lambda path, expected_duration=None, **kwargs: {
            "passed": True, "failure_kind": None, "detail": "",
            "checks": {}, "probe": None,
        },
    )


def _fake_write(path, *args, **kwargs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 2048)


def _refuse_closing_card(monkeypatch):
    # P0 removal (2026-11-27): the T5 closing-card floor this
    # helper used to refuse no longer exists — the guarantee was
    # rewired to render nothing, so an unserved target exhausts
    # the chain exactly as these tests assert, with no stub
    # needed. The helper remains as a no-op so the MG-05/MG-08
    # tests below keep their original shape; the decline
    # behaviour is pinned in tests/test_p0_removal_pins.py.
    return None


# ---------------------------------------------------------------------------
# MG-05 — the validator itself (real files)
# ---------------------------------------------------------------------------

def test_validator_accepts_a_real_graphic(tmp_path):
    video = _make_video(tmp_path / "ok.mp4", seconds=2)
    verdict = validate_graphic_output(video, expected_duration=2.0)
    assert verdict["passed"] is True
    assert verdict["failure_kind"] is None
    assert verdict["checks"]["width"] == 1920
    assert verdict["checks"]["height"] == 1080
    assert verdict["checks"]["decodable"] is True


def test_validator_rejects_missing_file(tmp_path):
    verdict = validate_graphic_output(tmp_path / "nope.mp4")
    assert verdict["passed"] is False
    assert verdict["failure_kind"] == "invalid_graphic_output"


def test_validator_rejects_empty_and_tiny_files(tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert validate_graphic_output(empty)["failure_kind"] == (
        "invalid_graphic_output"
    )
    tiny = tmp_path / "tiny.mp4"
    tiny.write_bytes(b"\x00" * 2048)
    verdict = validate_graphic_output(tiny)
    assert verdict["passed"] is False
    assert verdict["failure_kind"] == "invalid_graphic_output"


def test_validator_rejects_garbage_bytes(tmp_path):
    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(b"\x00" * 60000)
    verdict = validate_graphic_output(garbage)
    assert verdict["passed"] is False
    assert verdict["failure_kind"] == "invalid_graphic_output"


def test_validator_rejects_wrong_dimensions(tmp_path):
    video = _make_video(tmp_path / "small.mp4", size="640x360", seconds=2)
    verdict = validate_graphic_output(video, expected_duration=2.0)
    assert verdict["passed"] is False
    assert verdict["failure_kind"] == "validation_failed"
    assert "640x360" in verdict["detail"]


def test_validator_rejects_wrong_framerate(tmp_path):
    video = _make_video(tmp_path / "slow.mp4", seconds=2, rate=10)
    verdict = validate_graphic_output(video, expected_duration=2.0)
    assert verdict["passed"] is False
    assert verdict["failure_kind"] == "validation_failed"


def test_validator_rejects_duration_mismatch(tmp_path):
    video = _make_video(tmp_path / "short.mp4", seconds=1)
    verdict = validate_graphic_output(video, expected_duration=4.0)
    assert verdict["passed"] is False
    assert verdict["failure_kind"] == "validation_failed"
    assert "duration" in verdict["detail"]


def test_validator_rejects_truncated_file(tmp_path):
    video = _make_video(tmp_path / "full.mp4", seconds=3)
    data = video.read_bytes()
    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes(data[: len(data) // 3])
    verdict = validate_graphic_output(truncated)
    assert verdict["passed"] is False
    assert verdict["failure_kind"] in (
        "invalid_graphic_output", "validation_failed"
    )


# ---------------------------------------------------------------------------
# MG-05 — registration refuses invalid graphics (builder level)
# ---------------------------------------------------------------------------

def _stat_data():
    return {
        "case_bible": {"timeline": []},
        "scenes": [
            {
                "scene_number": 1,
                "sentences": [
                    {"text": "The wall cost $25 million to build.",
                     "sentence_number": 1},
                ],
            }
        ],
    }


def test_corrupt_engine_graphic_is_never_registered(tmp_path, monkeypatch):
    # The renderer "succeeds" but writes garbage: with the REAL
    # validator active, the graphic must not be registered, the
    # failure must be recorded, and no asset may carry the file.
    # (P0 removal, 2026-11-27: re-pointed from the removed native
    # stat renderer to a directed engine template — the
    # never-register-corrupt-output contract is family-agnostic.)
    from services.motion_engine import templates as engine_templates

    monkeypatch.setitem(
        engine_templates.TEMPLATES, "line_chart",
        lambda data, style, out_path, **kwargs: (
            _fake_write(out_path), (str(out_path), {"template": "line_chart"})
        )[1],
    )
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
    entry = {
        "manifest_id": "mg_corrupt",
        "target": {"scene_number": 1, "sentence_index": 0,
                   "sentence_number": 1},
        "text": "Throughput climbed year after year.",
        "scenario": {}, "purpose": "coverage_fallback",
        "template_candidates": ["line_chart"],
        "family_candidates": ["event_schematic"],
        "theme": None, "fact_basis": "script_claim",
        "evidence_ref": None, "verified_data": None,
    }
    manifest_in = {"version": 1, "project_id": "mg", "theme": None,
                   "entries": [entry]}
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=manifest_in,
    )
    assert assets == []
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    failures = manifest["graphic_failures"]
    assert len(failures) == 1
    assert failures[0]["graphic_type"] == "line_chart"
    assert failures[0]["stage"] == "validation"
    assert failures[0]["reason"] == "invalid_graphic_output"
    skip = manifest["targets"]["skipped"][0]
    assert skip["reason"] == "invalid_graphic_output"


def test_corrupt_schematic_is_never_registered(tmp_path, monkeypatch):
    def fake_schematic(path, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 60000)  # large enough, still garbage
        return str(path)

    monkeypatch.setattr(
        "services.vector_graphics.render_event_schematic", fake_schematic
    )
    sentence = {
        "text": "Crowds poured through the opened checkpoint at dawn.",
        "sentence_number": 3,
        "event": "opening of the Berlin Wall",
        "locations": ["Berlin"],
        "dates": ["November 9, 1989"],
        "scenarios": [{
            "event": "opening of the Berlin Wall",
            "place": "berlin", "era": "1989",
        }],
    }
    data = {
        "case_bible": {"timeline": []},
        "scenes": [{"scene_number": 2, "sentences": [sentence]}],
        "processed_scenes": [{
            "scene": {"scene_number": 2, "sentences": [sentence]},
            "videos": [], "images": [],
            "asset_stats": {"missing_visuals": []},
        }],
    }
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced",
        targets=[{
            "scene_number": 2, "sentence_index": 0, "sentence_number": 3,
            "text": sentence["text"],
        }],
    )
    assert not [a for a in assets if a["graphic_type"] == "event_schematic"]
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert any(
        f["graphic_type"] == "event_schematic"
        and f["reason"] == "invalid_graphic_output"
        for f in manifest["graphic_failures"]
    )


# ---------------------------------------------------------------------------
# MG-07 — jobs attach matrix
# ---------------------------------------------------------------------------

def _attach_scene():
    return [{
        "scene": {"scene_number": 1, "sentences": []},
        "videos": [], "images": [],
    }]


def _graphic(tmp_path, name="g.mp4", **overrides):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 6000)
    graphic = {
        "motion_graphic_id": "mg_0001",
        "graphic_type": "stat",
        "scene_number": 1,
        "sentence_index": 0,
        "file_path": str(path),
        "path": str(path),
        "delivered": True,
        "validation": {"passed": True},
    }
    graphic.update(overrides)
    return graphic


def test_attach_delivered_and_valid(tmp_path):
    scenes = _attach_scene()
    attached, skipped = jobs._attach_motion_graphics(
        [_graphic(tmp_path)], scenes
    )
    assert attached == 1
    assert skipped == []
    assert scenes[0]["videos"][0]["motion_graphic"] is True


def test_attach_refuses_undelivered(tmp_path):
    scenes = _attach_scene()
    attached, skipped = jobs._attach_motion_graphics(
        [_graphic(tmp_path, delivered=False)], scenes
    )
    assert attached == 0
    assert skipped[0]["reason"] == "not_delivered"
    assert scenes[0]["videos"] == []


def test_attach_refuses_unmarked_delivery(tmp_path):
    # No delivered field at all (pre-Phase-1 record shape): the
    # graphic must NOT attach — delivered must be explicit.
    graphic = _graphic(tmp_path)
    del graphic["delivered"]
    attached, skipped = jobs._attach_motion_graphics([graphic], _attach_scene())
    assert attached == 0
    assert skipped[0]["reason"] == "not_delivered"


def test_attach_refuses_failed_validation(tmp_path):
    graphic = _graphic(tmp_path, validation={"passed": False})
    attached, skipped = jobs._attach_motion_graphics([graphic], _attach_scene())
    assert attached == 0
    assert skipped[0]["reason"] == "graphic_validation_failed"


def test_attach_refuses_missing_file(tmp_path):
    graphic = _graphic(tmp_path)
    Path(graphic["file_path"]).unlink()
    attached, skipped = jobs._attach_motion_graphics([graphic], _attach_scene())
    assert attached == 0
    assert skipped[0]["reason"] == "missing_graphic_file"


def test_attach_refuses_unknown_scene(tmp_path):
    graphic = _graphic(tmp_path, scene_number=99)
    attached, skipped = jobs._attach_motion_graphics([graphic], _attach_scene())
    assert attached == 0
    assert skipped[0]["reason"] == "missing_scene_association"


def test_attach_refuses_no_scene_association(tmp_path):
    graphic = _graphic(tmp_path, scene_number=None)
    attached, skipped = jobs._attach_motion_graphics([graphic], _attach_scene())
    assert attached == 0
    assert skipped[0]["reason"] == "missing_scene_association"


# ---------------------------------------------------------------------------
# MG-08 — typed skip reasons per failure path
# ---------------------------------------------------------------------------

def test_skip_vocabulary_is_closed_and_covers_failure_paths():
    for reason in (
        "no_usable_statistic", "range_not_representable",
        "missing_required_input", "unsupported_template",
        "missing_document_source", "no_schematic_labels",
        "no_schematic_motif", "schematic_cap_reached",
        "no_eligible_graphic_purpose", "comparison_not_rendered",
        "rendering_failure", "validation_failed",
        "invalid_graphic_output", "no_sentence_text",
    ):
        assert reason in GRAPHIC_SKIP_REASONS
    # The retired conflation string must never come back.
    assert "no_stat_value" not in GRAPHIC_SKIP_REASONS


def test_statless_beat_skips_with_typed_decline(tmp_path, monkeypatch):
    # P0 removal (2026-11-27) replaces
    # test_skip_reason_no_usable_statistic: the native stat stage
    # that produced that reason is removed completely, so a beat
    # with no usable statistic now exhausts the surviving chain
    # and skips with the generic typed reason PLUS the terminal
    # decline record.
    _stub_validator(monkeypatch)
    graphics_pipeline.build_motion_graphics_assets(
        _stat_data(), tmp_path, mode="balanced",
        targets=[{
            "scene_number": 1, "sentence_index": 0, "sentence_number": 1,
            "text": "Guards watched the border day and night.",
        }],
    )
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    skip = manifest["targets"]["skipped"][0]
    assert skip["reason"] == "no_eligible_graphic_purpose"
    assert skip["reason"] != "no_usable_statistic"
    assert skip["terminal_decline"]["reason"] == (
        "terminal_graphics_decline"
    )


def test_skip_reason_rendering_failure(tmp_path, monkeypatch):
    # P0 removal: re-pointed from the removed native stat
    # renderer to a directed engine template that raises — the
    # chain records the same typed rendering_failure.
    _stub_validator(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("ffmpeg exploded")

    from services.motion_engine import templates as engine_templates

    monkeypatch.setitem(engine_templates.TEMPLATES, "data_gauge", boom)
    entry = {
        "manifest_id": "mg_boom",
        "target": {"scene_number": 1, "sentence_index": 0,
                   "sentence_number": 1},
        "text": "The pumps ran at 62 percent of capacity.",
        "scenario": {}, "purpose": "coverage_fallback",
        "template_candidates": ["data_gauge"],
        "family_candidates": [],
        "theme": None, "fact_basis": "script_claim",
        "evidence_ref": None, "verified_data": None,
    }
    data = {
        "case_bible": {"timeline": []},
        "scenes": [{
            "scene_number": 1,
            "sentences": [{
                "text": "The pumps ran at 62 percent of capacity.",
                "sentence_number": 1,
            }],
        }],
    }
    graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced",
        manifest={"version": 1, "project_id": "mg", "theme": None,
                  "entries": [entry]},
    )
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    skip = manifest["targets"]["skipped"][0]
    assert skip["reason"] == "rendering_failure"
    assert "ffmpeg exploded" in skip["detail"]


def test_skip_reason_missing_document_source(tmp_path, monkeypatch):
    _stub_validator(monkeypatch)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _stat_data(), tmp_path, mode="balanced",
        targets=[{
            "scene_number": 1, "sentence_index": 0, "sentence_number": 1,
            "text": "The report described the crossing in detail.",
        }],
    )
    # No fabricated document graphic may be produced.
    assert not [
        a for a in assets if a["graphic_type"] == "document_presentation"
    ]
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert manifest["targets"]["skipped"][0]["reason"] == (
        "missing_document_source"
    )


def test_skip_reason_no_sentence_text(tmp_path, monkeypatch):
    _stub_validator(monkeypatch)
    graphics_pipeline.build_motion_graphics_assets(
        _stat_data(), tmp_path, mode="balanced",
        targets=[{
            "scene_number": 1, "sentence_index": 0, "sentence_number": 1,
            "text": "",
        }],
    )
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert manifest["targets"]["skipped"][0]["reason"] == "no_sentence_text"


def test_engine_refusal_reason_is_typed_with_detail(tmp_path, monkeypatch):
    _stub_validator(monkeypatch)
    # A comparison sentence whose comparison template refuses its
    # data: the chain records missing_required_input and preserves
    # the template's own reason as detail.
    from services.motion_engine import templates as engine_templates

    def refusing(data, style, out_path, **kwargs):
        return None, {"reason": "no_numeric_items"}

    monkeypatch.setattr(engine_templates, "stat_comparison", refusing)
    monkeypatch.setitem(engine_templates.TEMPLATES, "stat_comparison", refusing)
    graphics_pipeline.build_motion_graphics_assets(
        _stat_data(), tmp_path, mode="balanced",
        targets=[{
            "scene_number": 1, "sentence_index": 0, "sentence_number": 1,
            "text": "The north side held 40 guards while the south held 90.",
        }],
    )
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    skip = manifest["targets"]["skipped"][0]
    assert skip["reason"] in (
        "missing_required_input", "comparison_not_rendered"
    )
    assert skip["reason"] in GRAPHIC_SKIP_REASONS


# ---------------------------------------------------------------------------
# MG-09 — ranges and year spans are not statistics (P0 pin)
#
# The native stat extraction that MG-09 originally pinned
# (extract_stat, range displays, range refusal) was removed
# completely with the stat family (P0 R5, 2026-11-27). What
# survives is the candidacy rule for the engine chart family:
# years, dates and year spans are NEVER statistic candidates.
# ---------------------------------------------------------------------------

def test_year_span_is_not_a_statistic():
    assert extract_comparison(
        "The wall stood from 1961-1989 in the city.") is None
    assert extract_comparison("In 1961-08 the border closed.") is None
    assert graphics_pipeline._series_from_text(
        "The wall stood from 1961-1989 in the city.") is None
    assert graphics_pipeline._percent_value(
        "In 1961 the border closed.") is None


# ---------------------------------------------------------------------------
# MG-09 stat-card evidence stamps — REMOVED with the native stat
# family (P0 R5, 2026-11-27). The stat card, its burned-in stamp
# tests and its fact-basis downgrade test tested a renderer that
# no longer exists; fact-basis recording for the surviving
# families is pinned by test_engine_graphic_records_full_inputs
# below and the Phase 6 director/graphics tests.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 2 — recipes are recorded for every family
# ---------------------------------------------------------------------------

def test_recipes_recorded_for_all_families(tmp_path, monkeypatch):
    # P0 removal (2026-11-27): the bible-driven timeline/process
    # families and the native stat family are removed completely;
    # recipes are now recorded for the SURVIVING families —
    # event_schematic and motion_engine — and for nothing else.
    _stub_validator(monkeypatch)
    monkeypatch.setattr(
        "services.vector_graphics.render_event_schematic",
        lambda path, **kwargs: (_fake_write(path), str(path))[1],
    )
    data = {
        "case_bible": {"timeline": [
            {"when": "June 1961", "what": "Tensions rise"},
            {"when": "13 August 1961", "what": "Border closed"},
            {"when": "9 November 1989", "what": "Wall falls"},
        ]},
        "scenes": [{
            "scene_number": 1,
            "sentences": [
                {"text": "The north side held 40 guards while the "
                         "south held 90.",
                 "sentence_number": 1},
                {"text": "Crowds poured through the opened checkpoint.",
                 "sentence_number": 2,
                 "event": "opening of the Berlin Wall",
                 "locations": ["Berlin"], "dates": ["November 9, 1989"],
                 "scenarios": [{
                     "event": "opening of the Berlin Wall",
                     "place": "berlin", "era": "1989",
                 }]},
            ],
        }],
        "processed_scenes": [{
            "scene": {"scene_number": 1, "sentences": []},
            "videos": [], "images": [],
            "asset_stats": {"missing_visuals": []},
        }],
    }
    targets = [
        {"scene_number": 1, "sentence_index": 0, "sentence_number": 1,
         "text": "The north side held 40 guards while the south held 90."},
        {"scene_number": 1, "sentence_index": 1, "sentence_number": 2,
         "text": "Crowds poured through the opened checkpoint."},
    ]
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", targets=targets
    )
    by_family = {}
    for asset in assets:
        recipe = asset.get("graphic_recipe")
        assert recipe is not None, asset["graphic_type"]
        assert recipe["regenerable"] is True
        assert recipe["duration"] == asset["duration"]
        assert recipe["scene_number"] == asset["scene_number"]
        by_family[recipe["family"]] = recipe
    assert {"motion_engine", "event_schematic"} <= set(by_family)
    assert set(by_family) <= {"motion_engine", "event_schematic"}
    assert by_family["motion_engine"]["template"] == "stat_comparison"
    assert by_family["event_schematic"]["inputs"]["event_label"] == (
        "opening of the Berlin Wall"
    )
    # Every registered asset carries its validation verdict and an
    # explicit delivered flag.
    for asset in assets:
        assert asset["validation"]["passed"] is True
        assert asset["delivered"] is True


def test_engine_graphic_records_full_inputs(tmp_path, monkeypatch):
    _stub_validator(monkeypatch)
    from services.motion_engine import templates as engine_templates

    seen = {}

    def fake_comparison(data, style, out_path, duration=5.0,
                        sentence_index=None, scene_number=None):
        seen.update(data)
        _fake_write(out_path)
        return str(out_path), {"template": "stat_comparison"}

    monkeypatch.setitem(
        engine_templates.TEMPLATES, "stat_comparison", fake_comparison
    )
    # A comparison sentence routes to the Motion Engine in the
    # chain (the native stat step is skipped for comparisons).
    assets = graphics_pipeline.build_motion_graphics_assets(
        _stat_data(), tmp_path, mode="balanced",
        targets=[{
            "scene_number": 1, "sentence_index": 0, "sentence_number": 1,
            "text": "The north side held 40 guards while the south held 90.",
        }],
    )
    engine_assets = [a for a in assets if a.get("family") == "motion_engine"]
    assert engine_assets, "expected an engine graphic in the chain"
    asset = engine_assets[0]
    # The FULL inputs are recorded — not the summary the old code kept.
    assert asset["engine_inputs"] == seen
    assert asset["graphic_recipe"]["inputs"] == seen
    assert asset["graphic_recipe"]["template"] == "stat_comparison"
    assert asset["graphic_recipe"]["style"] == asset["style"]
    assert asset["delivered"] is True
    assert asset["validation"]["passed"] is True


# ---------------------------------------------------------------------------
# MG-11 + editor regeneration behaviour (direct op calls)
# ---------------------------------------------------------------------------

def _editor_ctx(monkeypatch, tmp_path, project_id="proj-mg"):
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path))
    from services import editor

    doc = {"run_id": "run-mg", "tracks": {"visual": []}}
    ctx = editor._OpContext(project_id, doc, "rev-mg")
    ctx.media_dir.mkdir(parents=True, exist_ok=True)
    return editor, ctx


def _graphic_item(**graphic_overrides):
    graphic = {
        "template": "data_gauge",
        "family": "motion_engine",
        "style": "editorial",
        "inputs": {"percent": 42, "label": "X", "sublabel": "Y"},
        "engine_metadata": {},
        "recipe": None,
    }
    graphic.update(graphic_overrides)
    return {
        "item_id": "item-1",
        "kind": "graphic",
        "scene_number": 1,
        "sentence_number": 1,
        "duration": 3.0,
        "asset": {"file_path": "/old/graphic.mp4", "title": "t"},
        "graphic": graphic,
        "classification": "generated_graphic",
        "flags": [],
    }


def test_regenerate_missing_inputs_refused(monkeypatch, tmp_path):
    editor, ctx = _editor_ctx(monkeypatch, tmp_path)
    item = _graphic_item(template=None, inputs=None)
    doc = {"tracks": {"visual": [item]}}
    with pytest.raises(editor.EditorError) as error:
        editor._op_regenerate_graphic(doc, {"item_id": "item-1"}, ctx)
    assert error.value.status_code == 422
    assert error.value.code == "graphic_inputs_not_recorded"


def test_regenerate_unknown_template_refused(monkeypatch, tmp_path):
    editor, ctx = _editor_ctx(monkeypatch, tmp_path)
    item = _graphic_item(template="no_such_template")
    doc = {"tracks": {"visual": [item]}}
    with pytest.raises(editor.EditorError) as error:
        editor._op_regenerate_graphic(doc, {"item_id": "item-1"}, ctx)
    assert error.value.status_code == 422
    assert error.value.code == "unknown_template"


def test_regenerate_template_exception_maps_to_422(monkeypatch, tmp_path):
    # MG-11: a template that RAISES must produce the structured
    # 422 graphic_render_failed — never an unhandled exception —
    # and the item must keep its previous asset.
    editor, ctx = _editor_ctx(monkeypatch, tmp_path)
    from services.motion_engine import templates as engine_templates

    def exploding(data, style, out_path, **kwargs):
        raise RuntimeError("template blew up internally")

    monkeypatch.setitem(engine_templates.TEMPLATES, "data_gauge", exploding)
    item = _graphic_item()
    doc = {"tracks": {"visual": [item]}}
    with pytest.raises(editor.EditorError) as error:
        editor._op_regenerate_graphic(doc, {"item_id": "item-1"}, ctx)
    assert error.value.status_code == 422
    assert error.value.code == "graphic_render_failed"
    # No internals leak into the client-facing message.
    assert "blew up" not in error.value.message
    # Previous asset preserved; no promotion happened.
    assert item["asset"]["file_path"] == "/old/graphic.mp4"
    assert not (ctx.media_dir / "graphic_item-1.mp4").exists()


def test_regenerate_refusal_maps_to_422(monkeypatch, tmp_path):
    editor, ctx = _editor_ctx(monkeypatch, tmp_path)
    from services.motion_engine import templates as engine_templates

    def refusing(data, style, out_path, **kwargs):
        return None, {"reason": "no_percent_value"}

    monkeypatch.setitem(engine_templates.TEMPLATES, "data_gauge", refusing)
    item = _graphic_item()
    doc = {"tracks": {"visual": [item]}}
    with pytest.raises(editor.EditorError) as error:
        editor._op_regenerate_graphic(doc, {"item_id": "item-1"}, ctx)
    assert error.value.code == "graphic_render_failed"
    assert "no_percent_value" in error.value.message
    assert item["asset"]["file_path"] == "/old/graphic.mp4"


def test_regenerate_invalid_output_preserves_previous(monkeypatch, tmp_path):
    # The template "renders" garbage: validation refuses promotion
    # and the previous valid asset survives untouched.
    editor, ctx = _editor_ctx(monkeypatch, tmp_path)
    from services.motion_engine import templates as engine_templates

    def garbage(data, style, out_path, **kwargs):
        Path(out_path).write_bytes(b"\x00" * 60000)
        return str(out_path), {}

    monkeypatch.setitem(engine_templates.TEMPLATES, "data_gauge", garbage)
    item = _graphic_item()
    doc = {"tracks": {"visual": [item]}}
    with pytest.raises(editor.EditorError) as error:
        editor._op_regenerate_graphic(doc, {"item_id": "item-1"}, ctx)
    assert error.value.code == "graphic_render_failed"
    assert item["asset"]["file_path"] == "/old/graphic.mp4"
    assert not (ctx.media_dir / "graphic_item-1.mp4").exists()
    assert not (ctx.media_dir / "graphic_item-1.regenerating.mp4").exists()


def test_regenerate_non_regenerable_recipe_refused(monkeypatch, tmp_path):
    editor, ctx = _editor_ctx(monkeypatch, tmp_path)
    recipe = {
        "family": "stat", "renderer": "ffmpeg_stat", "template": None,
        "template_version": None, "inputs": {}, "style": None,
        "duration": 4.0, "purpose": "quantitative_explanation",
        "regenerable": False,
        "non_regenerable_reason": "inputs were not retained by its producer",
    }
    item = _graphic_item(template=None, inputs=None, recipe=recipe)
    doc = {"tracks": {"visual": [item]}}
    with pytest.raises(editor.EditorError) as error:
        editor._op_regenerate_graphic(doc, {"item_id": "item-1"}, ctx)
    assert error.value.code == "graphic_not_regenerable"
    assert "not retained" in error.value.message


def test_regenerate_removed_family_recipe_refused(monkeypatch, tmp_path):
    # P0 removal pin (replaces test_regenerate_native_recipe_
    # success): a recorded recipe from a REMOVED native family
    # (stat, and likewise timeline/process) can no longer be
    # regenerated — the shared dispatcher raises on the unknown
    # family, the editor maps it to the structured 422, and the
    # item keeps its previous asset untouched.
    editor, ctx = _editor_ctx(monkeypatch, tmp_path)
    recipe = {
        "family": "stat", "renderer": "ffmpeg_stat", "template": None,
        "template_version": None,
        "inputs": {
            "headline": "$25 MILLION", "subtext": "The wall cost money.",
            "fact_basis": "script_claim",
            "extraction": {"kind": "money", "source": "regex_money"},
        },
        "style": None, "duration": 4.0,
        "purpose": "quantitative_explanation",
        "scene_number": 1, "sentence_index": 0, "sentence_number": 1,
        "source_references": [],
        "regenerable": True, "non_regenerable_reason": None,
    }
    item = _graphic_item(template=None, inputs=None, recipe=recipe)
    item["duration"] = 4.0
    doc = {"tracks": {"visual": [item]}}
    with pytest.raises(editor.EditorError) as error:
        editor._op_regenerate_graphic(doc, {"item_id": "item-1"}, ctx)
    assert error.value.status_code == 422
    assert error.value.code == "graphic_render_failed"
    assert item["asset"]["file_path"] == "/old/graphic.mp4"



# ---------------------------------------------------------------------------
# Phase 2 ACCEPTANCE — production-shaped end-to-end regeneration
#
# A graphic is generated through the REAL production graphics
# builder (real Motion Engine renders),
# registered, attached by the REAL jobs attach path, built into a
# REAL timeline, serialised to timeline.json, loaded by the REAL
# editor import into revision 0, and regenerated from the RECORDED
# recipe. If any stage drops the recipe fields, this test fails.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def production_shape(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        root = tmp_path_factory.mktemp("mg-acceptance")
        mp.setenv("CINEMORA_MEDIA_ROOT", str(root))

        from database import Base, SessionLocal, engine as db_engine
        from models import Project

        Base.metadata.create_all(db_engine)

        from services import run_ledger

        project_id = "proj-mg-acceptance"
        project_dir = root / project_id
        project_dir.mkdir(parents=True)
        # P0 removal (2026-11-27): the first beat was originally
        # served by the native stat family ("cost $25 million");
        # that family is removed, so the beat is now a percent
        # sentence the surviving data_gauge template serves.
        stat_text = "The barrier ran at 85 percent capacity."
        comparison_text = (
            "The north checkpoint processed 40 crossings daily "
            "while the south handled 90."
        )
        db = SessionLocal()
        try:
            db.add(Project(
                id=project_id,
                script=f"{stat_text} {comparison_text}",
                category="history", status="completed",
            ))
            db.commit()
        finally:
            db.close()
        run_id = run_ledger.begin_run(
            project_id, f"{stat_text} {comparison_text}", options={}
        )
        run_ledger.finish_run(run_id, "completed")

        sentences = [
            {"text": stat_text, "sentence_number": 1},
            {"text": comparison_text, "sentence_number": 2},
        ]
        data = {
            "project_id": project_id,
            "script_text": f"{stat_text} {comparison_text}",
            "case_bible": {"timeline": []},
            "scenes": [{"scene_number": 1, "sentences": sentences}],
        }
        targets = [
            {"scene_number": 1, "sentence_index": 0, "sentence_number": 1,
             "text": stat_text},
            {"scene_number": 1, "sentence_index": 1, "sentence_number": 2,
             "text": comparison_text},
        ]
        # REAL builder, REAL renders, REAL validation.
        graphics = graphics_pipeline.build_motion_graphics_assets(
            data, project_dir / "motion_graphics",
            mode="balanced", targets=targets,
        )
        assert len(graphics) == 2, graphics
        for graphic in graphics:
            assert graphic["delivered"] is True
            assert graphic["validation"]["passed"] is True
            assert graphic["graphic_recipe"] is not None

        # REAL jobs attach path.
        processed = [{
            "scene": {"scene_number": 1, "sentences": sentences},
            "videos": [], "images": [],
        }]
        attached, skipped = jobs._attach_motion_graphics(graphics, processed)
        assert attached == 2 and skipped == []

        # REAL timeline construction.
        from services.timeline import build_scene_timeline

        scene_media = {
            "scene_number": 1,
            "duration_seconds": 12.0,
            "videos": processed[0]["videos"],
            "images": [],
            "sentences": sentences,
        }
        timings = [
            {"sentence_number": 1, "sentence_index": 0, "text": stat_text,
             "start": 0.0, "end": 6.0},
            {"sentence_number": 2, "sentence_index": 1,
             "text": comparison_text, "start": 6.0, "end": 12.0},
        ]
        scene_timeline = build_scene_timeline(scene_media, timings)
        graphic_items = [
            item for item in scene_timeline["items"]
            if item.get("motion_graphic")
        ]
        assert len(graphic_items) == 2
        for item in graphic_items:
            # The recipe survived timeline construction.
            assert item.get("graphic_recipe"), item

        # Serialise exactly as the production state does.
        timeline_doc = {
            "scenes": [scene_timeline],
            "duration_seconds": 12.0,
        }
        (project_dir / "timeline.json").write_text(
            json.dumps(timeline_doc)
        )
        (project_dir / "production_state.json").write_text(json.dumps({
            "status": "completed", "captions_mode": "burn",
        }))

        yield {
            "root": root,
            "project_id": project_id,
            "project_dir": project_dir,
            "run_id": run_id,
            "graphics": graphics,
        }


def test_production_shaped_regeneration(production_shape):
    from services import editor

    project_id = production_shape["project_id"]
    view, doc = editor.create_revision_zero(
        project_id, production_shape["run_id"]
    )
    graphic_items = [
        item for item in doc["tracks"]["visual"] if item["kind"] == "graphic"
    ]
    assert len(graphic_items) == 2
    for item in graphic_items:
        # MG-03 acceptance: the editor sees the complete recipe.
        assert item["graphic"]["recipe"] is not None, item
        assert item["graphic"]["recipe"]["regenerable"] is True
        # P0 removal: every production graphic is now an engine
        # graphic (the native families are gone), so a template
        # name is always recorded.
        assert item["graphic"]["template"]

    current_revision_id = doc["revision_id"]
    for item in graphic_items:
        window = item["duration"]
        _view, new_doc = editor.apply_operations(
            project_id, current_revision_id,
            [{"type": "regenerate_graphic", "item_id": item["item_id"]}],
        )
        current_revision_id = new_doc["revision_id"]
        regenerated = next(
            i for i in new_doc["tracks"]["visual"]
            if i["item_id"] == item["item_id"]
        )
        # The regenerated file exists, passes the Phase 1 contract,
        # and matches the recorded timeline window.
        path = Path(regenerated["asset"]["file_path"])
        assert path.exists()
        verdict = validate_graphic_output(path, expected_duration=window)
        assert verdict["passed"] is True, verdict
        # The recipe is still recorded on the regenerated item, and
        # the item remains a usable graphic in the revision.
        assert regenerated["graphic"]["recipe"] is not None
        assert regenerated["classification"] == "generated_graphic"
        assert "regenerated" in regenerated["flags"]

    # The ORIGINAL production graphics are untouched by the edits.
    for graphic in production_shape["graphics"]:
        assert Path(graphic["file_path"]).exists()
