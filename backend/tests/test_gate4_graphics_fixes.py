"""Gate 4 graphics-chain fixes (07-GATE4-INSPECTION B1/B3/B4/B6).

B1 — a stat card must bind ONLY its own sentence's quantity.
     Root cause found in the Hyatt run artefacts: the assembler
     DID bind 140M (motion_graphics.json recipe), but the stat
     renderer's count-up animation displayed intermediate values
     as finished statements — 87–88M at t≈2s of the 140M card —
     so the sentence-midpoint frame contradicted the narration.
     Fixes: no count-up on factual headlines (verbatim from
     frame 0), and a sentence-binding guard in _make_stat (a
     verified fact attached to a DIFFERENT sentence is ignored;
     a verified display that disagrees with the sentence's own
     quantity is replaced by the sentence's own).
B3 — an assigned family must deliver its designed element or
     decline with a typed reason; a silent downgrade to a generic
     card is forbidden, and identical graphic payloads must not
     silently serve multiple sentences (repetition guard +
     explicit quality-defect recording; the Gate 2 terminal
     guarantee still fills the beat).
B4 — timeline / outcome_path labels wrap + shrink-to-fit and
     stay inside the frame safe area; no mid-word truncation.
B6 — internal classification/provenance labels ("DATA / SCRIPT
     CLAIM" etc.) never reach rendered text.
"""

import json
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageChops

from services import graphics_pipeline
from services.motion_engine import templates as engine_templates


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------

def _stub_validator(monkeypatch):
    monkeypatch.setattr(
        graphics_pipeline, "validate_graphic_output",
        lambda path, expected_duration=None, **kwargs: {
            "passed": True, "failure_kind": None, "detail": "",
            "checks": {}, "probe": None,
        },
    )
    # P0 removal (2026-11-27): the native stat renderer patch
    # that used to sit here is gone with the family.


def _stub_template(monkeypatch, name, refuse=False):
    def fake(data, style, out_path, duration=5.0,
             sentence_index=None, scene_number=None):
        if refuse:
            return None, {"reason": "no_text"}
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 4096)
        return str(path), {
            "generator": "motion_engine", "template": name,
            "generated": True,
        }

    monkeypatch.setitem(engine_templates.TEMPLATES, name, fake)


def _manifest_file(tmp_path):
    return json.loads((tmp_path / "motion_graphics.json").read_text())


def _frame(video_path, out_png, at):
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-ss", str(at),
         "-i", str(video_path), "-vframes", "1", str(out_png)],
        check=True,
    )
    return Image.open(out_png).convert("RGB")


def _bright_pixels_in_border(img, margin=38, threshold=180):
    """Count bright (text-like) pixels inside the outer border."""
    w, h = img.size
    px = img.load()
    count = 0
    for x in list(range(0, margin)) + list(range(w - margin, w)):
        for y in range(0, h, 4):
            r, g, b = px[x, y]
            if r > threshold and g > threshold and b > threshold:
                count += 1
    return count


# ---------------------------------------------------------------------
# B1 — sentence-bound stat + no false interim figures
# ---------------------------------------------------------------------

def _hyatt_data(entry_extra=None):
    text = "Civil claims were settled for more than 140 million dollars."
    entry = {
        "manifest_id": "g4_19",
        "target": {"scene_number": 9, "sentence_index": 0,
                   "sentence_number": 19},
        "text": text,
        "scenario": {},
        "purpose": "graphic_explanation",
        "template_candidates": [],
        "family_candidates": ["stat"],
        "theme": None,
        "fact_basis": "script_claim",
        "evidence_ref": None,
        "verified_data": None,
    }
    if entry_extra:
        entry.update(entry_extra)
    data = {
        "case_bible": {
            "title": "Hyatt Regency Walkway Collapse",
            "timeline": [],
            "entities": [],
            # Two-figure bible: 88M belongs to another sentence,
            # 140M is sentence 19's own figure.
            "script_facts": {"facts": [
                {"fact_id": "f_other", "kind": "quantity",
                 "display": "88 million dollars", "sentence_number": 5},
                {"fact_id": "f_own", "kind": "quantity",
                 "display": "140 million dollars", "sentence_number": 19},
            ]},
        },
        "scenes": [{
            "scene_number": 9,
            "sentences": [{"text": text, "sentence_number": 19}],
        }],
    }
    manifest = {"version": 1, "project_id": "g4", "theme": None,
                "entries": [entry]}
    return data, manifest


def test_b1_fact_attached_to_other_sentence_is_not_borrowed(
        tmp_path, monkeypatch):
    """P0 removal pin (rewritten): B1 pinned that a stat card
    never borrows another sentence's figure. The stat family is
    removed completely — an entry carrying a borrowed verified
    display now produces NO graphic at all, so nothing can be
    borrowed onto a card."""
    _stub_validator(monkeypatch)
    data, manifest = _hyatt_data({
        "verified_data": {"display": "88 million dollars",
                          "kind": "money"},
        "evidence_ref": {"fact_id": "f_other", "sentence_number": 5},
    })
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=manifest)
    assert assets == []


def test_b1_conflicting_verified_display_yields_to_sentence_quantity(
        tmp_path, monkeypatch):
    """P0 removal pin (rewritten): with the stat family gone, a
    conflicting verified display has nothing to yield to and
    nothing to headline — the entry produces no graphic."""
    _stub_validator(monkeypatch)
    data, manifest = _hyatt_data({
        "verified_data": {"display": "88 million dollars",
                          "kind": "money"},
        "evidence_ref": {"fact_id": "f_other"},
    })
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=manifest)
    assert assets == []


# P0 removal (2026-11-27): test_b1_stat_headline_is_verbatim_
# from_the_first_frames deleted — it rendered through the
# native stat renderer, removed completely with R5.


# ---------------------------------------------------------------------
# B6 — internal labels never reach render inputs
# ---------------------------------------------------------------------

def _captured_render(monkeypatch):
    commands = []

    def capture(command):
        joined = " ".join(str(part) for part in command)
        for ref in re.findall(r"textfile=([^:]+)", joined):
            try:
                joined += " " + Path(ref).read_text(encoding="utf-8")
            except OSError:
                pass
        commands.append(joined)

    monkeypatch.setattr(graphics_pipeline, "run_ffmpeg", capture)
    return commands


# P0 removal (2026-11-27): test_b6_no_internal_label_in_stat_
# render_for_any_basis deleted — it rendered through the native
# stat renderer, removed completely. The B6 detector itself
# (next test) survives: it guards every surviving render path.


def test_b6_internal_label_detector_covers_the_class():
    assert graphics_pipeline._contains_internal_label("DATA / SCRIPT CLAIM")
    assert graphics_pipeline._contains_internal_label("data / verified")
    assert graphics_pipeline._contains_internal_label("SOURCE • WIKIMEDIA")
    assert not graphics_pipeline._contains_internal_label(
        "140 MILLION DOLLARS")
    assert not graphics_pipeline._contains_internal_label("Jack Gillum")


# ---------------------------------------------------------------------
# B4 — labels fit: layout contract + rendered frames
# ---------------------------------------------------------------------

HYATT_EVENTS = [
    {"when": "July 1, 1980",
     "what": "The Hyatt Regency hotel opened in Kansas City."},
    {"when": "Before July 17, 1981",
     "what": "Havens Steel proposed using two shorter rod sets "
             "instead of one continuous rod."},
    {"when": "July 17, 1981, evening",
     "what": "More than 1,600 people gathered for a Friday tea "
             "dance in the Hyatt Regency atrium."},
    {"when": "July 17, 1981, 7:05 p.m.",
     "what": "The fourth-floor walkway tore free, fell onto the "
             "second-floor walkway, and both crashed into the "
             "atrium floor."},
    {"when": "July 17, 1981–overnight",
     "what": "The collapse killed 114 people and injured 216."},
    {"when": "2015",
     "what": "A memorial to the 114 victims was dedicated."},
]


# P0 removal (2026-11-27): the B4 timeline layout/render tests
# and the outcome_path render test are deleted — both families
# (native timeline, outcome_path template) are removed
# completely. The assembler label-fitting pin below survives:
# _fit_label is a shared templates helper.


def test_b4_outcome_path_assembler_no_longer_truncates_labels():
    # Layout-data level: the fitted lines carry the full label.
    from PIL import Image, ImageDraw
    from services.motion_engine.styles import resolve_style
    _name, tokens = resolve_style("editorial")
    draw = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    label = HYATT_EVENTS[1]["what"]
    lines, _font = engine_templates._fit_label(
        draw, label, tokens, max_width=320, sizes=(27, 22, 19),
        max_lines=3)
    assert " ".join(lines) == label
    assert "..." not in " ".join(lines)


# ---------------------------------------------------------------------
# B3 — family delivers or declines; repetition guard
# ---------------------------------------------------------------------

def _two_beat_data():
    return {
        "case_bible": {"title": "The Box", "timeline": [],
                       "entities": []},
        "scenes": [{
            "scene_number": 1,
            "sentences": [
                {"text": "Malcom McLean watched the ships at Newark.",
                 "sentence_number": 1},
                {"text": "Malcom McLean counted the cranes at Newark.",
                 "sentence_number": 2},
            ],
        }],
    }


def test_b3_repeated_beats_decline_without_duplicate_payloads(
        tmp_path, monkeypatch):
    """P0 rewiring (rewritten): two beats whose only payload was
    the same name used to be served two closing cards with the
    repetition guard moving the second to a distinct payload.
    The floor is removed — both beats decline, no payload is
    ever duplicated, and no duplicate-payload defect is
    recorded."""
    _stub_validator(monkeypatch)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _two_beat_data(), tmp_path, mode="balanced",
        targets=[
            {"scene_number": 1, "sentence_index": 0,
             "sentence_number": 1,
             "text": "Malcom McLean watched the ships at Newark.",
             "scenario": {}},
            {"scene_number": 1, "sentence_index": 1,
             "sentence_number": 2,
             "text": "Malcom McLean counted the cranes at Newark.",
             "scenario": {}},
        ])
    assert assets == []
    manifest = _manifest_file(tmp_path)
    dupes = [d for d in manifest["graphic_quality_defects"]
             if d["type"] == "duplicate_graphic_payload"]
    assert dupes == []
    assert len(manifest["targets"]["skipped"]) == 2


def test_b3_assigned_schematic_declines_without_substitution(
        tmp_path, monkeypatch):
    """P0 rewiring (rewritten): an entry assigned the schematic
    family whose stage declines used to be filled by the
    guarantee floor with the substitution recorded. The floor
    is removed — the beat is simply skipped with the terminal
    decline; no family silently (or recorded-ly) substitutes
    for the assigned one."""
    _stub_validator(monkeypatch)
    entry = {
        "manifest_id": "g4_schem",
        "target": {"scene_number": 1, "sentence_index": 0,
                   "sentence_number": 1},
        "text": "Malcom McLean watched the ships at Newark.",
        "scenario": {},
        "purpose": "graphic_explanation",
        "template_candidates": [],
        "family_candidates": ["event_schematic"],
        "theme": None, "fact_basis": "script_claim",
        "evidence_ref": None, "verified_data": None,
    }
    data = _two_beat_data()
    manifest = {"version": 1, "project_id": "g4", "theme": None,
                "entries": [entry]}
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", manifest=manifest)
    assert assets == []
    outcome = _manifest_file(tmp_path)["targets"]["outcomes"][0]
    assert outcome["outcome"] == "skipped"
    assert outcome["terminal_decline"]["reason"] == (
        "terminal_graphics_decline"
    )


def test_b3_schematic_renders_a_real_diagram_when_motif_fits(
        tmp_path, monkeypatch):
    _stub_validator(monkeypatch)
    _stub_template(monkeypatch, "closing_card")
    import services.vector_graphics as vg

    def fake_render(path, event_label="", place_label="",
                    era_label="", duration=6.0):
        Path(path).write_bytes(b"\x00" * 4096)
        return str(path)

    monkeypatch.setattr(vg, "render_event_schematic", fake_render)
    data = {
        "case_bible": {"title": "Divided City", "timeline": [],
                       "entities": []},
        "scenes": [{
            "scene_number": 1,
            "sentences": [{"text": "Families crossed the border "
                                   "checkpoint at night.",
                           "sentence_number": 1}],
        }],
        "processed_scenes": [{
            "scene": {"scene_number": 1, "sentences": []},
            "videos": [], "images": [],
        }],
    }
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced",
        targets=[{
            "scene_number": 1, "sentence_index": 0, "sentence_number": 1,
            "text": "Families crossed the border checkpoint at night.",
            "scenario": {"event": "border crossing",
                         "place": "Berlin", "era": "1961"},
        }])
    schematics = [a for a in assets
                  if a.get("family") == "event_schematic"]
    assert len(schematics) == 1
    assert schematics[0]["schematic_motif"] == "barrier"


def test_b3_title_only_card_is_not_a_beat_payload(tmp_path, monkeypatch):
    """S21 shape: the only 'payload' available is the production's
    own title — the terminal sweep must not serve a title-only
    card for it."""
    _stub_validator(monkeypatch)
    _stub_template(monkeypatch, "closing_card")
    _stub_template(monkeypatch, "archival_title")
    text = ("It became the defining case study in responsibility "
            "for everyone who followed.")
    data = {
        "case_bible": {
            "title": "Hyatt Regency Walkway Collapse",
            "timeline": [],
            "entities": [
                {"canonical": "Hyatt Regency Walkway Collapse",
                 "type": "event"},
            ],
        },
        "scenes": [{
            "scene_number": 1,
            "sentences": [{"text": text, "sentence_number": 21}],
        }],
    }
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced",
        targets=[{"scene_number": 1, "sentence_index": 0,
                  "sentence_number": 21, "text": text, "scenario": {}}])
    for asset in assets:
        quote = (asset.get("engine_inputs") or {}).get("quote") or ""
        assert quote.lower() != "hyatt regency walkway collapse"
