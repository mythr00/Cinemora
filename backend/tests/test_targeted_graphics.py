"""Stage A + B regression tests: targeted motion graphics.

Stage A — graphics generation is connected to the sentences that
finished retrieval + coverage recovery with NO verified visual
(the Asset Manager's missing-visual records):

- targeted sentences get a stat graphic carrying the exact
  scene_number + sentence_index anchor;
- targeted sentences with no defensible value are skipped and the
  skip is recorded (no invented content, no text cards);
- the timeline and process graphics are anchored and can reach the
  final timeline;
- the untargeted stat cap is a parameter, and targeted graphics are
  never blocked by it;
- a verified sentence visual still beats a targeted graphic.

Stage B — renderer quality: proportional timeline dates with
progressive reveal, the process chevron (no literal "?"), genuine
count-up in stat graphics, shared style + bundled font. The render
tests use REAL ffmpeg renders verified with ffprobe and frame
extraction — not mocks.
"""

import hashlib
import json
import subprocess

import pytest

import jobs

from services import graphics_pipeline
from services.media_probe import probe_video
from services.timeline import build_scene_timeline


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

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


def _targets():
    return [
        {"scene_number": 2, "sentence_index": 0, "sentence_number": 3,
         "text": "The tunnel ran 140 feet beneath the wall."},
        {"scene_number": 2, "sentence_index": 1, "sentence_number": 4,
         "text": "Crowds gathered at the crossing in silence."},
    ]


def _fake_renders(monkeypatch):
    """Replace the three renderers with file-writing fakes (the
    existing AUD-21 test pattern) for the logic tests."""

    def fake_write(path, *args, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)

    # P0 removal (2026-11-27): the three native renderers this
    # fixture used to fake are removed completely; only the
    # validator seam remains.
    # MG-05 (Motion Graphics programme Phase 1): registration now
    # validates rendered output before an asset is recorded. These
    # are logic tests whose fake renderers write non-media bytes,
    # so the validator is stubbed at the same seam; the real
    # validator is covered by dedicated real-file tests in
    # tests/test_motion_graphics_programme.py.
    monkeypatch.setattr(
        graphics_pipeline, "validate_graphic_output",
        lambda path, expected_duration=None, **kwargs: {
            "passed": True, "failure_kind": None, "detail": "",
            "checks": {}, "probe": None,
        },
    )



def _attach(assets):
    """Reproduce the jobs.py attach step: graphics become scene
    visuals stamped as motion graphics."""
    visuals = []
    for asset in assets:
        visual = dict(asset)
        visual["type"] = "video"
        visual["source"] = "cinemora_motion_graphics"
        visual["motion_graphic"] = True
        visual["file_path"] = asset.get("file_path") or asset.get("path")
        visual["path"] = visual["file_path"]
        visuals.append(visual)
    return visuals


# ---------------------------------------------------------------------------
# Stage A — targeting
# ---------------------------------------------------------------------------

def test_targeted_graphic_generated_with_anchor(tmp_path, monkeypatch):
    """P0 removal rewrite: the targeted native stat is gone; a
    targeted beat is served by a surviving family (here the
    engine's stat_comparison for a comparison sentence) with the
    same anchor + honesty stamps."""
    _fake_renders(monkeypatch)
    data = _data()
    data["scenes"][1]["sentences"][0] = {
        "text": "Exports reached 200 ships, compared with 120 "
                "ships a decade earlier.",
        "sentence_number": 3,
    }
    targets = [{
        "scene_number": 2, "sentence_index": 0, "sentence_number": 3,
        "text": "Exports reached 200 ships, compared with 120 "
                "ships a decade earlier.",
    }]
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", targets=targets
    )
    targeted = [a for a in assets if a.get("targeted")]
    assert len(targeted) == 1
    graphic = targeted[0]
    assert graphic["template"] == "stat_comparison"
    assert graphic["scene_number"] == 2
    assert graphic["sentence_index"] == 0
    assert graphic["sentence_number"] == 3
    # Honesty stamps are intact: generated, never footage/verified.
    assert graphic["generated"] is True
    assert graphic["visual_format"] == "graphic"
    assert graphic["visual_format_evidence"] == "generated_in_house"
    assert graphic["source"] == "cinemora_motion_graphics"


def test_targeted_sentence_without_value_is_skipped_and_recorded(
    tmp_path, monkeypatch
):
    _fake_renders(monkeypatch)
    targets = [
        {"scene_number": 2, "sentence_index": 0, "sentence_number": 3,
         "text": "The pumps ran at 62 percent of capacity."},
        {"scene_number": 2, "sentence_index": 1, "sentence_number": 4,
         "text": "Crowds gathered at the crossing in silence."},
    ]
    graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", targets=targets
    )
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert manifest["targets"]["requested"] == 2
    assert manifest["targets"]["generated"] == 1
    skips = manifest["targets"]["skipped"]
    assert len(skips) == 1
    assert skips[0]["scene_number"] == 2
    assert skips[0]["sentence_index"] == 1
    # P0 rewiring: the skip carries a typed MG-08 reason (never
    # the retired no_usable_statistic the removed stat stage
    # produced) plus the terminal decline.
    assert skips[0]["reason"] in graphics_pipeline.GRAPHIC_SKIP_REASONS
    assert skips[0]["reason"] != "no_usable_statistic"
    assert skips[0]["terminal_decline"]["reason"] == (
        "terminal_graphics_decline"
    )


# P0 removal (2026-11-27): test_timeline_and_process_graphics_
# are_anchored deleted — both anchored families are removed
# completely (R1/R6).


def test_no_targets_produces_no_graphics(tmp_path, monkeypatch):
    # Phase 6 SUPERSESSION (AM-001/D1, founder-approved removal):
    # this test previously pinned the chronology graphic anchoring
    # itself to the first scene's first sentence when NO sentence
    # was targeted. D1 removed exactly that behaviour — a graphic
    # exists only for a targeted requirement — so with no targets
    # there are no graphics of any family.
    _fake_renders(monkeypatch)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced"
    )
    assert assets == []


# P0 removal (2026-11-27): test_max_stats_zero_suppresses_
# legacy_stats_not_manifest deleted — the max_stats plumbing
# existed solely for the removed native stat family and is
# gone with it (the signature pin lives in
# tests/test_p0_removal_pins.py).


def test_no_untargeted_graphics_exist(tmp_path, monkeypatch):
    # Phase 6 SUPERSESSION (AM-001/D1) + P0 removal: only the
    # targeted beat's graphic is produced; scene 1's untargeted
    # "$25 million" sentence — which the removed stat sweep once
    # served — produces nothing.
    _fake_renders(monkeypatch)
    targets = [{
        "scene_number": 2, "sentence_index": 0, "sentence_number": 3,
        "text": "The pumps ran at 62 percent of capacity.",
    }]
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", targets=targets
    )
    assert len(assets) == 1
    assert assets[0].get("targeted") is True
    assert assets[0]["scene_number"] == 2
    assert assets[0]["sentence_index"] == 0
    assert assets[0].get("template") == "data_gauge"


# ---------------------------------------------------------------------------
# Stage A — timeline integration (verified > graphic > rotation)
# ---------------------------------------------------------------------------

def _scene2_media(visuals, verified_for_first=None):
    sentences = [
        {"sentence_number": 3, "videos": [], "images": []},
        {"sentence_number": 4, "videos": [], "images": []},
    ]
    if verified_for_first is not None:
        sentences[0]["videos"] = [verified_for_first]
    return {
        "scene_number": 2,
        "duration_seconds": 9,
        "videos": visuals,
        "images": [],
        "sentences": sentences,
    }


def _scene2_timings():
    return [
        {"sentence_index": 0, "text": "The tunnel ran 140 feet beneath the wall.",
         "start": 0.0, "end": 4.0},
        {"sentence_index": 1, "text": "Crowds gathered at the crossing in silence.",
         "start": 4.5, "end": 9.0},
    ]


def test_targeted_graphic_lands_on_correct_sentence_window(
    tmp_path, monkeypatch
):
    _fake_renders(monkeypatch)
    targets = [{
        "scene_number": 2, "sentence_index": 0, "sentence_number": 3,
        "text": "The pumps ran at 62 percent of capacity.",
    }]
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", targets=targets
    )
    assert len(assets) == 1
    scene_media = _scene2_media(_attach(assets))
    result = build_scene_timeline(scene_media, _scene2_timings())
    items = result["items"]
    assert len(items) == 1
    # B5: the targeted sentence's item spans exactly its narration
    # window (extended to the next sentence's start, per timeline
    # rules) and is assigned as a generated graphic; the unserved
    # sentence stays uncovered for the footage fallback.
    assert items[0]["assignment"] == "motion_graphic"
    assert items[0]["relevance_status"] == "generated_graphic"
    assert items[0]["start"] == 0.0
    assert items[0]["end"] == 4.5
    assert result["uncovered_sentence_indexes"] == [1]


# P0 removal (2026-11-27): test_timeline_graphic_reaches_final_
# timeline deleted — the timeline graphic is removed completely
# (R1); a surviving targeted graphic reaching the timeline is
# pinned by the window test above.


def test_verified_visual_beats_targeted_graphic(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    targets = [{
        "scene_number": 2, "sentence_index": 0, "sentence_number": 3,
        "text": "The pumps ran at 62 percent of capacity.",
    }]
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", targets=targets
    )
    verified = tmp_path / "verified.mp4"
    verified.write_bytes(b"\x00" * 2048)
    verified_asset = {
        "file_path": str(verified), "path": str(verified), "type": "video",
        # MA-37: a verified asset carries its acceptance record.
        "verification": {"accepted": True},
    }
    scene_media = _scene2_media(
        _attach(assets) + [verified_asset],
        verified_for_first=verified_asset,
    )
    result = build_scene_timeline(scene_media, _scene2_timings())
    item = result["items"][0]
    assert item["assignment"] == "sentence"
    assert item["file_path"] == str(verified)


# ---------------------------------------------------------------------------
# Stage B — date parsing / proportional positions (pure logic)
# ---------------------------------------------------------------------------

# P0 removal (2026-11-27): the Stage B sections are deleted —
# _parse_event_date / _timeline_node_positions were exclusive
# helpers of the removed timeline renderer (R1), and the REAL
# renders fixture rendered only the removed native families
# (stat count-up, timeline reveal, process steps).


# ---------------------------------------------------------------------------
# IA-03 — jobs.py target builder: scenario propagation
#
# Regression: the production builder read _sentence.get("scenario")
# (singular), a key sentence records never carry — they carry
# "scenarios" (a list, per story_understanding._normalize_scenario)
# — so every production target arrived with scenario={}. The tests
# below call the ACTUAL builder jobs._build_graphic_targets (no
# mirror), mirroring plan_document._proposed_visual's normalisation:
# scenarios[0] when the list is non-empty and its first entry is a
# dict, else {}.
# ---------------------------------------------------------------------------

def _scenario(event, place, era):
    """One scenario in the shape story_understanding produces."""
    return {
        "visual": f"archival material about {event}",
        "kind": "event",
        "subject_id": None,
        "subject": None,
        "required_type": None,
        "specificity": "representative",
        "search_terms": [event],
        "event": event,
        "place": place,
        "era": era,
        "format_intent": "archival",
        "format_intent_confidence": 0.8,
    }


def _processed_scenes(sentences, missing_numbers, scene_number=1):
    return [
        {
            "scene": {
                "scene_number": scene_number,
                "sentences": sentences,
            },
            "videos": [],
            "images": [],
            "asset_stats": {
                "missing_visuals": [
                    {"sentence_number": n} for n in missing_numbers
                ]
            },
        }
    ]


def test_jobs_target_carries_sentence_scenario():
    scenario = _scenario("opening of the Berlin Wall", "Berlin", "1989")
    other = _scenario("construction of the Berlin Wall", "Berlin", "1961")
    sentences = [
        {
            "text": "Crowds poured through the opened checkpoint.",
            "sentence_number": 1,
            "scenarios": [scenario, other],
        }
    ]
    targets = jobs._build_graphic_targets(
        _processed_scenes(sentences, [1])
    )
    assert len(targets) == 1
    target = targets[0]
    assert target["scene_number"] == 1
    assert target["sentence_index"] == 0
    assert target["sentence_number"] == 1
    # The FIRST scenario, with its own real fields preserved.
    assert target["scenario"] == scenario
    assert target["scenario"]["event"] == "opening of the Berlin Wall"
    assert target["scenario"]["place"] == "Berlin"
    assert target["scenario"]["era"] == "1989"


def test_jobs_targets_keep_each_sentences_own_scenario():
    scenario_a = _scenario("opening of the Berlin Wall", "Berlin", "1989")
    scenario_b = _scenario("signing of the peace treaty", "Paris", "1919")
    sentences = [
        {"text": "Crowds poured through the checkpoint.",
         "sentence_number": 1, "scenarios": [scenario_a]},
        {"text": "Delegates gathered for the signing.",
         "sentence_number": 2, "scenarios": [scenario_b]},
    ]
    targets = jobs._build_graphic_targets(
        _processed_scenes(sentences, [1, 2])
    )
    assert len(targets) == 2
    # No cross-contamination: each target carries its own sentence's
    # scenario, in sentence order.
    assert targets[0]["sentence_number"] == 1
    assert targets[0]["scenario"] == scenario_a
    assert targets[1]["sentence_number"] == 2
    assert targets[1]["scenario"] == scenario_b
    assert targets[0]["scenario"] != targets[1]["scenario"]


def test_jobs_target_scenario_empty_when_scenarios_missing_or_empty():
    sentences = [
        {"text": "No scenarios key at all.", "sentence_number": 1},
        {"text": "Empty scenarios list.", "sentence_number": 2,
         "scenarios": []},
        {"text": "Non-dict first scenario.", "sentence_number": 3,
         "scenarios": ["not-a-scenario"]},
    ]
    targets = jobs._build_graphic_targets(
        _processed_scenes(sentences, [1, 2, 3])
    )
    assert len(targets) == 3
    for target in targets:
        assert target["scenario"] == {}


def test_jobs_target_scenario_reaches_graphics_builder_and_timeline(
    tmp_path, monkeypatch
):
    """Integration: the scenario the jobs builder puts on a target is
    the ONLY source of schematic labels here — the scene sentence
    record handed to build_motion_graphics_assets is bare (no
    scenarios, no event/locations/dates), so the fallback in
    _schematic_labels cannot supply them. The schematic must be
    labelled from the target's scenario, and it must land on the
    timeline as a motion graphic for its sentence."""
    scenario = _scenario("opening of the Berlin Wall", "Berlin", "1989")
    text = "Crowds poured through the opened checkpoint at dawn."
    analysed_sentence = {
        "text": text,
        "sentence_number": 3,
        "scenarios": [scenario],
    }
    targets = jobs._build_graphic_targets(
        _processed_scenes([analysed_sentence], [3], scene_number=2)
    )
    assert len(targets) == 1
    assert targets[0]["scenario"] == scenario

    calls = []

    def fake_render(path, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)
        calls.append(kwargs)
        return str(path)

    monkeypatch.setattr(
        "services.vector_graphics.render_event_schematic", fake_render
    )

    def fake_write(path, *args, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)

    # MG-05 (Motion Graphics programme Phase 1): registration now
    # validates rendered output before an asset is recorded. These
    # are logic tests whose fake renderers write non-media bytes,
    # so the validator is stubbed at the same seam; the real
    # validator is covered by dedicated real-file tests in
    # tests/test_motion_graphics_programme.py.
    monkeypatch.setattr(
        graphics_pipeline, "validate_graphic_output",
        lambda path, expected_duration=None, **kwargs: {
            "passed": True, "failure_kind": None, "detail": "",
            "checks": {}, "probe": None,
        },
    )


    bare_sentence = {"text": text, "sentence_number": 3}
    data = {
        "case_bible": {"timeline": []},
        "scenes": [{"scene_number": 2, "sentences": [bare_sentence]}],
        "processed_scenes": [
            {
                "scene": {"scene_number": 2, "sentences": [bare_sentence]},
                "videos": [],
                "images": [],
                "asset_stats": {"missing_visuals": []},
            }
        ],
    }
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", targets=targets
    )
    schematics = [
        a for a in assets if a["graphic_type"] == "event_schematic"
    ]
    assert len(schematics) == 1
    # Labels came from the target's scenario payload.
    assert len(calls) == 1
    assert calls[0]["event_label"] == "opening of the Berlin Wall"
    assert calls[0]["place_label"] == "Berlin"
    assert calls[0]["era_label"] == "1989"

    # Attach (the jobs.py step) + timeline assignment.
    schematic = schematics[0]
    visual = dict(schematic)
    visual["type"] = "video"
    visual["source"] = "cinemora_motion_graphics"
    visual["motion_graphic"] = True
    visual["sentence_index"] = schematic.get("sentence_index")
    visual["file_path"] = schematic.get("file_path") or schematic.get("path")
    visual["path"] = visual["file_path"]
    scene_media = {
        "scene_number": 2,
        "duration_seconds": 9,
        "videos": [visual],
        "images": [],
        "sentences": [
            {"sentence_number": 3, "videos": [], "images": []},
            {"sentence_number": 4, "videos": [], "images": []},
        ],
    }
    timings = [
        {"sentence_index": 0, "text": text, "start": 0.0, "end": 4.0},
        {"sentence_index": 1, "text": "Neighbours embraced strangers.",
         "start": 4.5, "end": 9.0},
    ]
    result = build_scene_timeline(scene_media, timings)
    item = result["items"][0]
    assert item["assignment"] == "motion_graphic"
    assert item["relevance_status"] == "generated_graphic"
    assert item["file_path"] == schematic["file_path"]
