"""Phase 5 (X3) — visual validation harness pins.

Motion Graphics Programme gap X3: validation used to be purely
technical (exists / decodes / 1920x1080 / duration), so graphics
with overlapping text (timeline), mid-word truncation
(milestone_waveform), overflowing labels (process) and one-word
cards (closing_card) all passed and shipped in release-grade
films. These tests pin the new visual-validation layer:

1. Label fit (services.motion_engine.label_fit): a label is
   measured against its box before drawing; a label that does
   not fit refuses the graphic with the typed
   `label_does_not_fit` reason — never rendered clipped, never
   truncated mid-word; label boxes that collide are flagged.
2. Frame inspection (services.motion_engine.frame_inspection):
   a rendered frame is checked for gross defects — near-empty
   frames, content clipped at the frame edge, text lines
   overrunning into each other. The programme's evidence frames
   are the regression fixtures; the acceptable archival_title
   frame must pass.
3. Build-path wiring: a graphic failing visual validation is
   not registered/delivered — it becomes a skip with the typed
   `visual_validation_failed` reason and the beat falls back
   per the post-P0 rules (footage or terminal decline).
"""

import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from services import graphics_pipeline
from services.motion_engine import frame_inspection, label_fit
from services.motion_engine.styles import FONT, FONT_BOLD
from services.motion_engine.core import load_font

EVIDENCE_DIR = (
    Path(__file__).resolve().parents[3]
    / "cinemora-audit" / "motion-graphics-programme" / "evidence"
)

pytestmark = pytest.mark.skipif(
    not EVIDENCE_DIR.is_dir(),
    reason="programme evidence frames not present",
)


def _draw():
    return ImageDraw.Draw(Image.new("RGBA", (1920, 1080)))


def _defect_types(verdict):
    return {d["type"] for d in verdict["defects"]}


# ---------------------------------------------------------------------------
# 1. Label fit
# ---------------------------------------------------------------------------

def test_fit_lines_preserves_full_text_when_it_fits():
    draw = _draw()
    font = load_font(FONT_BOLD, 27)
    text = "The Pharos stood for more than a thousand years"
    lines = label_fit.fit_lines(draw, text, font, 320, max_lines=3)
    assert lines is not None
    assert " ".join(lines) == text  # nothing truncated, ever


def test_fit_lines_refuses_a_word_wider_than_the_box():
    draw = _draw()
    font = load_font(FONT_BOLD, 27)
    assert label_fit.fit_lines(
        draw, "Antidisestablishmentarianism", font, 120,
    ) is None


def test_fit_lines_refuses_when_the_wrap_needs_more_lines():
    draw = _draw()
    font = load_font(FONT_BOLD, 27)
    text = "one two three four five six seven eight nine ten"
    assert label_fit.fit_lines(draw, text, font, 200, max_lines=1) is None
    assert label_fit.fit_lines(draw, text, font, 200, max_lines=6) is not None


def test_fit_font_shrinks_then_refuses():
    draw = _draw()
    tokens = {"font": FONT, "font_bold": FONT_BOLD}
    fitted = label_fit.fit_font(
        draw, "Eddystone Lighthouse", tokens, 320,
        max_lines=2, sizes=(40, 27, 19),
    )
    assert fitted is not None
    lines, _font = fitted
    assert " ".join(lines) == "Eddystone Lighthouse"
    # A box no offered size can satisfy: refusal, not overflow.
    assert label_fit.fit_font(
        draw, "Eddystone Lighthouse", tokens, 60,
        max_lines=1, sizes=(40, 27, 19),
    ) is None


def test_layout_collisions_flags_the_timeline_overlap_geometry():
    # Synthetic equivalent of the timeline-lighthouses defect:
    # the second event's body-text box overlaps the first and
    # third events' boxes (staggered columns, shared rows).
    boxes = [
        {"key": "event1_body", "box": (38, 583, 330, 660)},
        {"key": "event2_body", "box": (290, 610, 562, 765)},
        {"key": "event3_body", "box": (445, 583, 740, 690)},
        {"key": "header", "box": (120, 145, 320, 175)},
    ]
    collisions = label_fit.layout_collisions(boxes)
    pairs = {tuple(sorted((c["a"], c["b"]))) for c in collisions}
    assert ("event1_body", "event2_body") in pairs
    assert ("event2_body", "event3_body") in pairs
    assert ("event1_body", "header") not in pairs


def test_layout_collisions_clean_layout_has_none():
    boxes = [
        {"key": "a", "box": (0, 0, 100, 40)},
        {"key": "b", "box": (0, 60, 100, 100)},
        {"key": "c", "box": (200, 0, 300, 40)},
    ]
    assert label_fit.layout_collisions(boxes) == []


def test_require_labels_fit_raises_the_typed_refusal():
    draw = _draw()
    font = load_font(FONT_BOLD, 30)
    labels = [{
        "key": "headline", "text": "x" * 120, "font": font,
        "max_width": 300, "max_lines": 1,
    }]
    with pytest.raises(label_fit.LabelFitError) as excinfo:
        label_fit.require_labels_fit(draw, labels)
    assert excinfo.value.reason == "label_does_not_fit"
    assert label_fit.LABEL_FIT_REASON in graphics_pipeline.GRAPHIC_SKIP_REASONS
    assert "visual_validation_failed" in graphics_pipeline.GRAPHIC_SKIP_REASONS


# ---------------------------------------------------------------------------
# 2. Frame inspection — evidence fixtures
# ---------------------------------------------------------------------------

def test_closing_card_frames_are_near_empty():
    for name in ("closing_card-lighthouses.png", "closing_card-coffee.png"):
        verdict = frame_inspection.inspect_frame(EVIDENCE_DIR / name)
        assert verdict["passed"] is False, name
        assert "near_empty_frame" in _defect_types(verdict), name


def test_milestone_waveform_frame_is_clipped_at_the_edge():
    verdict = frame_inspection.inspect_frame(
        EVIDENCE_DIR / "milestone_waveform-coffee.png")
    assert verdict["passed"] is False
    assert "content_clipped_at_frame_edge" in _defect_types(verdict)


def test_process_frame_headlines_overrun():
    verdict = frame_inspection.inspect_frame(
        EVIDENCE_DIR / "process-coffee.png")
    assert verdict["passed"] is False
    assert "text_line_overrun" in _defect_types(verdict)


def test_archival_title_frame_passes():
    # The one acceptable frame from the judged films: the checks
    # must not flag a clean, legible, well-spaced title card.
    verdict = frame_inspection.inspect_frame(
        EVIDENCE_DIR / "archival_title-coffee.png")
    assert verdict["passed"] is True, verdict["defects"]


def test_other_settled_evidence_frames_have_no_false_positives():
    for name in ("stat-lighthouses.png", "route_map-coffee.png",
                 "evidence_board-lighthouses.png",
                 "outcome_path-lighthouses.png",
                 "timeline-lighthouses.png"):
        verdict = frame_inspection.inspect_frame(EVIDENCE_DIR / name)
        assert verdict["passed"] is True, (name, verdict["defects"])


# ---------------------------------------------------------------------------
# 2b. Frame inspection — synthetic frames
# ---------------------------------------------------------------------------

def _blank_frame():
    return Image.new("RGB", (1920, 1080), (24, 20, 14))


def test_synthetic_one_word_card_is_near_empty():
    # The closing_card shape: a small-print kicker, a hairline
    # rule and ONE small word on an empty field.
    frame = _blank_frame()
    draw = ImageDraw.Draw(frame)
    kicker_font = load_font(FONT_BOLD, 20)
    word_font = load_font(FONT_BOLD, 44)
    draw.text((850, 390), "THE PORT", font=kicker_font, fill=(154, 42, 32))
    draw.rectangle([910, 440, 1010, 444], fill=(154, 42, 32))
    draw.text((830, 500), "Alexandria", font=word_font,
              fill=(245, 238, 220))
    verdict = frame_inspection.inspect_frame(frame)
    assert verdict["passed"] is False
    assert "near_empty_frame" in _defect_types(verdict)


def test_synthetic_one_word_display_title_is_not_near_empty():
    # The legitimate sparse title: one word, but at DISPLAY size
    # (the archival_title treatment). Ink fraction alone is in
    # the closing_card range — the check must not flag it.
    frame = _blank_frame()
    draw = ImageDraw.Draw(frame)
    font = load_font(FONT_BOLD, 84)
    draw.text((800, 480), "Coffee", font=font, fill=(245, 238, 220))
    verdict = frame_inspection.inspect_frame(frame)
    assert "near_empty_frame" not in _defect_types(verdict)


def test_synthetic_edge_clipped_label_is_flagged():
    frame = _blank_frame()
    draw = ImageDraw.Draw(frame)
    font = load_font(FONT_BOLD, 40)
    # A full title block (so the frame is not near-empty) plus a
    # label running off the left edge — the waveform shape.
    draw.text((760, 300), "THE COFFEE TRADE", font=font,
              fill=(245, 238, 220))
    draw.text((760, 700), "ESTABLISHED 1555", font=font,
              fill=(245, 238, 220))
    draw.text((-30, 500), "RADITIONAL ORIGIN STO", font=font,
              fill=(245, 238, 220))
    verdict = frame_inspection.inspect_frame(frame)
    assert verdict["passed"] is False
    assert "content_clipped_at_frame_edge" in _defect_types(verdict)


def test_synthetic_clean_title_frame_passes():
    frame = _blank_frame()
    draw = ImageDraw.Draw(frame)
    title_font = load_font(FONT_BOLD, 84)
    kicker_font = load_font(FONT_BOLD, 26)
    draw.text((860, 380), "ARCHIVE", font=kicker_font, fill=(154, 42, 32))
    draw.text((660, 440), "THE COFFEE HOUSE", font=title_font,
              fill=(245, 238, 220))
    draw.text((700, 560), "LONDON, 1652", font=kicker_font,
              fill=(245, 238, 220))
    verdict = frame_inspection.inspect_frame(frame)
    assert verdict["passed"] is True, verdict["defects"]


# ---------------------------------------------------------------------------
# 3. Template-level label-fit refusal
# ---------------------------------------------------------------------------

def test_archival_title_refuses_an_unfittable_kicker(tmp_path):
    from services.motion_engine.templates_v3 import archival_title

    rendered, meta = archival_title(
        {"title": "Coffee", "kicker": "x" * 200},
        "editorial", tmp_path / "t.mp4", duration=1.0,
    )
    assert rendered is None
    assert meta["reason"] == "label_does_not_fit"


def test_archival_title_refuses_an_unfittable_title_word(tmp_path):
    from services.motion_engine.templates_v3 import archival_title

    rendered, meta = archival_title(
        {"title": "x" * 60}, "editorial", tmp_path / "t.mp4",
        duration=1.0,
    )
    assert rendered is None
    assert meta["reason"] == "label_does_not_fit"


# ---------------------------------------------------------------------------
# 4. Build-path wiring
# ---------------------------------------------------------------------------

def _data(title="The Port"):
    return {
        "case_bible": {"title": title, "timeline": [], "entities": []},
        "scenes": [{
            "scene_number": 1,
            "sentences": [
                {"text": "In 1956 the first ship sailed from Newark.",
                 "sentence_number": 1},
            ],
        }],
        "processed_scenes": [],
    }


def _entry(templates, scenario=None):
    return {
        "manifest_id": "x3_0001",
        "target": {"scene_number": 1, "sentence_index": 0,
                   "sentence_number": 1},
        "text": "In 1956 the first ship sailed from Newark.",
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
    return {"version": 1, "project_id": "x3", "theme": None,
            "entries": entries}


def _build_record(tmp_path):
    return json.loads(
        (tmp_path / "motion_graphics.json").read_text())


def test_visual_failure_blocks_registration_and_skips_typed(
        tmp_path, monkeypatch):
    def _failing_inspection(path, at_fraction=0.7):
        return {
            "passed": False,
            "defects": [{
                "type": "near_empty_frame",
                "detail": "content occupies 0.4% of the frame",
            }],
            "metrics": {"ink_fraction": 0.004},
            "detail": "",
        }

    monkeypatch.setattr(
        frame_inspection, "inspect_video", _failing_inspection)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced",
        manifest=_manifest([_entry(["archival_title"])]),
    )
    assert assets == []
    record = _build_record(tmp_path)
    assert record["count"] == 0
    skipped = record["targets"]["skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "visual_validation_failed"
    # The terminal outcome is the post-P0 decline, not a floor
    # graphic: the outcome carries the terminal decline record.
    outcomes = record["targets"]["outcomes"]
    assert outcomes[0]["outcome"] == "skipped"
    assert outcomes[0]["terminal_decline"]["reason"] == (
        "terminal_graphics_decline")
    failures = record["graphic_failures"]
    assert any(
        f["reason"] == "visual_validation_failed"
        for f in failures
    )


def test_passing_graphic_carries_its_visual_verdict(tmp_path):
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced",
        manifest=_manifest([_entry(["archival_title"])]),
    )
    titles = [a for a in assets if a.get("template") == "archival_title"]
    assert len(titles) == 1
    visual = titles[0]["validation"]["visual"]
    assert visual["passed"] is True
    assert visual["defects"] == []


def test_label_fit_refusal_flows_to_the_typed_skip_reason(tmp_path):
    # A case-bible title whose single word cannot fit the title
    # measure: the template refuses, and the skip record carries
    # label_does_not_fit — not a generic missing-input reason.
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(title="x" * 60), tmp_path, mode="balanced",
        manifest=_manifest([_entry(["archival_title"])]),
    )
    assert assets == []
    record = _build_record(tmp_path)
    skipped = record["targets"]["skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "label_does_not_fit"
