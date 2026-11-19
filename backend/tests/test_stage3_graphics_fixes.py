"""Stage 3D regression tests: graphics correctness fixes.

Covers MA-01..MA-06 + the drawtext colon defect:
- escape_drawtext for literal labels (backslash/colon/percent/
  apostrophe) and programmatic %{eif:...} strings (untouched);
- a REAL ffmpeg render of a timeline whose label contains
  "around 4:00 a.m." through the live text path;
- timeline node de-collision + label rows (MA-02);
- process steps built from "what", "when" as sublabel (MA-01);
- process anchoring with exactly one target + undelivered
  recording with zero targets (MA-03);
- schematic motif selection barrier/journey/marker/none (MA-04);
- broadened stat extraction: percent/count/range/money-from-
  analysis/comparison (MA-05);
- chronology anchor preference (MA-06);
- per-sentence schematic reachability (Stage 3D);
- the targeted fallback chain recording outcomes in the manifest.
"""

import json
import subprocess

import pytest

from services import graphics_pipeline, vector_graphics
from services.graphics_pipeline import (
    escape_drawtext, extract_comparison, extract_stat,
)
from services.media_probe import probe_video


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------

def test_escape_literal_colon_percent_backslash_apostrophe():
    assert escape_drawtext("around 4:00 a.m.") == r"around 4\:00 a.m."
    assert escape_drawtext("50% sure") == "50%% sure"
    assert escape_drawtext(r"a\b") == r"a\\b"
    assert escape_drawtext("it's") == "it'\\''s"
    # Combined: backslash escaped first, colon/percent after.
    assert escape_drawtext(r"10\: 50%") == r"10\\\: 50%%"


def test_escape_programmatic_string_untouched():
    expr = r"%{eif\:500*min(1\,t/3.200)\:d}"
    assert escape_drawtext(expr, programmatic=True) == expr
    assert "%{eif" in escape_drawtext(expr, programmatic=True)
    # ...while the same string as a literal WOULD be escaped.
    assert escape_drawtext(expr) != expr


def test_real_ffmpeg_colon_label_render(tmp_path):
    """The colon defect, end to end: a timeline label containing
    'around 4:00 a.m.' must render through the live drawtext path."""
    out = tmp_path / "timeline_colon.mp4"
    graphics_pipeline._render_timeline_graphic(
        out,
        [
            {"when": "20 April 1949", "what": "Shelling began around 4:00 a.m."},
            {"when": "31 July 1949", "what": "Escape at 100% darkness: night"},
        ],
        duration=4.0,
    )
    info = probe_video(out)
    assert info is not None
    assert info["width"] == 1920 and info["height"] == 1080
    assert info["duration_seconds"] > 0


# ---------------------------------------------------------------------------
# MA-02 — timeline de-collision
# ---------------------------------------------------------------------------

def test_timeline_nodes_min_gap_and_label_rows():
    events = [
        {"when": "January 1961", "what": "A"},
        {"when": "February 1961", "what": "B"},
        {"when": "March 1961", "what": "C"},
        {"when": "November 1989", "what": "D"},
    ]
    positions, mode, _ = graphics_pipeline._timeline_node_positions(events, 6.0)
    assert mode == "proportional"
    for prev, cur in zip(positions, positions[1:]):
        assert cur - prev >= 170.0 - 1e-6
    rows = graphics_pipeline._timeline_label_rows(positions)
    # Clustered neighbours must not share a label row.
    assert rows[0] != rows[1]
    assert rows[1] != rows[2]


# ---------------------------------------------------------------------------
# MA-01 / MA-03 — process steps + anchoring (fake renders)
# ---------------------------------------------------------------------------

def _fake_renders(monkeypatch):
    def fake_write(path, *args, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)

    def fake_timeline(path, *args, **kwargs):
        fake_write(path)
        return "proportional"

    captured = {}

    def fake_process(path, steps, *args, **kwargs):
        captured["steps"] = steps
        fake_write(path)

    monkeypatch.setattr(graphics_pipeline, "_render_stat_graphic", fake_write)
    monkeypatch.setattr(graphics_pipeline, "_render_timeline_graphic", fake_timeline)
    monkeypatch.setattr(graphics_pipeline, "_render_process_graphic", fake_process)
    return captured


def _bible_data(scenes=None):
    return {
        "case_bible": {"timeline": [
            {"when": "June 1961", "what": "Tensions rise sharply"},
            {"when": "13 August 1961", "what": "Border closed overnight"},
            {"when": "9 November 1989", "what": "Wall falls to crowds"},
        ]},
        "scenes": scenes or [
            {"scene_number": 1, "sentences": [
                {"text": "A quiet opening sentence.", "sentence_number": 1},
            ]},
        ],
    }


def test_process_steps_built_from_what(tmp_path, monkeypatch):
    captured = _fake_renders(monkeypatch)
    graphics_pipeline.build_motion_graphics_assets(
        _bible_data(), tmp_path, mode="balanced",
        targets=[{"scene_number": 1, "sentence_index": 0, "sentence_number": 1,
                  "text": "A quiet opening sentence."}],
    )
    steps = captured["steps"]
    labels = [s["label"] if isinstance(s, dict) else s for s in steps]
    assert any("Tensions rise" in label for label in labels)
    assert not any(label.startswith("June 1961") for label in labels)
    sublabels = [s.get("sublabel", "") for s in steps if isinstance(s, dict)]
    assert "June 1961" in sublabels


def test_process_anchors_to_single_target(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _bible_data(), tmp_path, mode="balanced",
        targets=[{"scene_number": 1, "sentence_index": 0, "sentence_number": 1,
                  "text": "A quiet opening sentence."}],
    )
    process = next(a for a in assets if a["graphic_type"] == "process")
    assert process["scene_number"] == 1
    assert process["sentence_index"] == 0
    assert process.get("delivered") is True


def test_process_undelivered_recorded_with_zero_targets(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _bible_data(), tmp_path, mode="balanced",
    )
    process = next(a for a in assets if a["graphic_type"] == "process")
    assert process.get("scene_number") is None
    assert process.get("delivered") is False
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert any(u["graphic_type"] == "process" for u in manifest["undelivered"])


# ---------------------------------------------------------------------------
# MA-06 — chronology anchor preference
# ---------------------------------------------------------------------------

def test_timeline_anchor_prefers_date_sentence(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    targets = [
        {"scene_number": 1, "sentence_index": 0, "sentence_number": 1,
         "text": "Crowds gathered in silence at the square."},
        {"scene_number": 1, "sentence_index": 1, "sentence_number": 2,
         "text": "On 9 November 1989 the border opened."},
    ]
    data = _bible_data(scenes=[{"scene_number": 1, "sentences": [
        {"text": targets[0]["text"], "sentence_number": 1},
        {"text": targets[1]["text"], "sentence_number": 2},
    ]}])
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", targets=targets,
    )
    timeline = next(a for a in assets if a["graphic_type"] == "timeline")
    assert timeline["sentence_index"] == 1
    assert timeline["anchor_reason"] == "chronology_sentence"


# ---------------------------------------------------------------------------
# MA-04 — motif selection
# ---------------------------------------------------------------------------

def test_motif_selection():
    assert vector_graphics.select_schematic_motif("opening of the Berlin Wall", "Berlin") == "barrier"
    assert vector_graphics.select_schematic_motif("checkpoint at the border", "Berlin") == "barrier"
    assert vector_graphics.select_schematic_motif("escape journey downriver", "Woosung") == "journey"
    assert vector_graphics.select_schematic_motif("voyage to the fleet", "Yangtze") == "journey"
    assert vector_graphics.select_schematic_motif("signing of the treaty", "Paris") == "marker"
    assert vector_graphics.select_schematic_motif("arrival of the delegation", "Paris") == "marker"
    assert vector_graphics.select_schematic_motif("quiet afternoon in the village", "Kent") is None


def test_schematic_no_motif_returns_none_and_no_file(tmp_path):
    out = tmp_path / "none.mp4"
    result = vector_graphics.render_event_schematic(
        out, event_label="quiet afternoon in the village",
        place_label="Kent", era_label="1949", duration=0.5,
    )
    assert result is None
    assert not out.exists()


# ---------------------------------------------------------------------------
# MA-05 — stat extraction
# ---------------------------------------------------------------------------

def test_extract_stat_percent():
    info = extract_stat("About 62 percent of the crew were below deck.")
    assert info["kind"] == "percent" and info["display"] == "62%"
    info = extract_stat("Roughly 62% survived the night.")
    assert info["kind"] == "percent"


def test_extract_stat_plain_counts():
    info = extract_stat("More than 3,000 people lined the banks.")
    assert info["kind"] == "count" and "3,000" in info["display"]
    info = extract_stat("A fleet of 200 ships waited offshore.")
    assert info["kind"] == "count" and "200 ships" in info["display"]


def test_extract_stat_range_uses_first_value():
    info = extract_stat("Between 100 and 200 sailors were on watch.")
    assert info["kind"] == "range"
    assert info["display"].startswith("100")
    assert info["range_end"] == "200"


def test_extract_stat_money_from_analysis_record():
    info = extract_stat(
        "The refit was expensive.",
        sentence_record={"money": ["$5 million"]},
    )
    assert info["kind"] == "money" and info["display"] == "$5 million"
    assert info["source"] == "analysis_money_field"


def test_extract_comparison_two_values():
    items = extract_comparison(
        "Exports reached 200 ships, compared with 120 ships a decade earlier."
    )
    assert items is not None and len(items) == 2
    assert items[0]["value"] == 200 and items[1]["value"] == 120


def test_stat_kind_recorded_on_asset(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    data = {"case_bible": {"timeline": []}, "scenes": [
        {"scene_number": 1, "sentences": [
            {"text": "About 62 percent of the crew were below deck.", "sentence_number": 1},
        ]},
    ]}
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced",
        targets=[{"scene_number": 1, "sentence_index": 0, "sentence_number": 1,
                  "text": "About 62 percent of the crew were below deck."}],
    )
    stats = [a for a in assets if a["graphic_type"] == "stat"]
    assert stats and stats[0]["stat_kind"] == "percent"


# ---------------------------------------------------------------------------
# Per-sentence schematic reachability
# ---------------------------------------------------------------------------

def test_schematic_fires_when_only_own_sentence_uncovered(tmp_path, monkeypatch):
    calls = []

    def fake_render(path, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)
        calls.append(kwargs)
        return str(path)

    monkeypatch.setattr("services.vector_graphics.render_event_schematic", fake_render)

    def fake_write(path, *args, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)

    monkeypatch.setattr(graphics_pipeline, "_render_stat_graphic", fake_write)

    sentence_a = {"text": "The admiral inspected the fleet at anchor.",
                  "sentence_number": 1}
    sentence_b = {
        "text": "Crowds poured through the opened checkpoint at dawn.",
        "sentence_number": 2, "event": "opening of the Berlin Wall",
        "locations": ["Berlin"], "dates": ["November 9, 1989"],
        "scenarios": [{"event": "opening of the Berlin Wall",
                       "place": "berlin", "era": "1989"}],
    }
    # Sentence 0 has an explicitly LINKED verified visual; sentence 1
    # (the target) has none — the scene is NOT empty, yet the
    # schematic must still fire for the uncovered sentence.
    verified_for_a = {
        "file_path": "/x/a.mp4", "type": "video", "sentence_index": 0,
        "verification": {"accepted": True},
    }
    data = {
        "case_bible": {"timeline": []},
        "scenes": [{"scene_number": 2, "sentences": [sentence_a, sentence_b]}],
        "processed_scenes": [{
            "scene": {"scene_number": 2, "sentences": [sentence_a, sentence_b]},
            "videos": [verified_for_a], "images": [],
            "asset_stats": {"missing_visuals": []},
        }],
    }
    target = {"scene_number": 2, "sentence_index": 1, "sentence_number": 2,
              "text": sentence_b["text"]}
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", targets=[target],
    )
    schematics = [a for a in assets if a["graphic_type"] == "event_schematic"]
    assert len(schematics) == 1
    assert schematics[0]["schematic_motif"] == "barrier"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Fallback chain: comparison -> stat_comparison, outcomes recorded
# ---------------------------------------------------------------------------

def test_fallback_chain_comparison_and_outcomes(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    engine_calls = []

    def fake_engine(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\x00" * 2048)
        engine_calls.append(data)
        return str(out_path), {"generator": "motion_engine", "template": "stat_comparison",
                                "generated": True}

    monkeypatch.setattr(
        "services.motion_engine.templates.stat_comparison", fake_engine)
    # The chain looks templates up through the TEMPLATES dict.
    from services.motion_engine import templates as engine_templates
    monkeypatch.setitem(engine_templates.TEMPLATES, "stat_comparison", fake_engine)

    text = "Exports reached 200 ships, compared with 120 ships a decade earlier."
    data = {"case_bible": {"timeline": []}, "scenes": [
        {"scene_number": 1, "sentences": [{"text": text, "sentence_number": 1}]},
    ]}
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced",
        targets=[{"scene_number": 1, "sentence_index": 0, "sentence_number": 1,
                  "text": text}],
    )
    comparisons = [a for a in assets if a["graphic_type"] == "stat_comparison"]
    assert len(comparisons) == 1
    assert comparisons[0]["family"] == "motion_engine"
    assert engine_calls and len(engine_calls[0]["items"]) == 2
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    outcomes = manifest["targets"]["outcomes"]
    assert outcomes and outcomes[0]["outcome"] == "generated"
    assert outcomes[0]["family"] == "motion_engine"
