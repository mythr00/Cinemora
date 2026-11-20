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

    def fake_timeline(path, *args, **kwargs):
        fake_write(path)
        return "proportional"

    monkeypatch.setattr(graphics_pipeline, "_render_stat_graphic", fake_write)
    monkeypatch.setattr(
        graphics_pipeline, "_render_timeline_graphic", fake_timeline
    )
    monkeypatch.setattr(graphics_pipeline, "_render_process_graphic", fake_write)
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

def test_targeted_stat_generated_with_anchor(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", targets=_targets()
    )
    targeted = [
        a for a in assets
        if a["graphic_type"] == "stat" and a.get("targeted")
    ]
    assert len(targeted) == 1
    graphic = targeted[0]
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
    graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", targets=_targets()
    )
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert manifest["targets"]["requested"] == 2
    assert manifest["targets"]["generated"] == 1
    skips = manifest["targets"]["skipped"]
    assert len(skips) == 1
    assert skips[0]["scene_number"] == 2
    assert skips[0]["sentence_index"] == 1
    # MG-08: the historical "no_stat_value" fall-through conflated
    # several distinct causes; under the closed typed vocabulary
    # this sentence's actual condition is that it carries no usable
    # statistic (and no other graphic purpose fit).
    assert skips[0]["reason"] == "no_usable_statistic"


def test_timeline_and_process_graphics_are_anchored(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", targets=_targets()
    )
    by_type = {a["graphic_type"]: a for a in assets}
    # Timeline anchors to the FIRST target, process to the NEXT one.
    assert by_type["timeline"]["scene_number"] == 2
    assert by_type["timeline"]["sentence_index"] == 0
    assert by_type["timeline"]["timeline_spacing"] == "proportional"
    assert by_type["process"]["scene_number"] == 2
    assert by_type["process"]["sentence_index"] == 1
    # Every generated graphic is attachable (carries an anchor).
    for asset in assets:
        assert asset.get("scene_number") is not None
        assert asset.get("sentence_index") is not None


def test_no_targets_anchors_timeline_to_first_scene(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced"
    )
    by_type = {a["graphic_type"]: a for a in assets}
    assert by_type["timeline"]["scene_number"] == 1
    assert by_type["timeline"]["sentence_index"] == 0
    # Process stays unanchored when there are no targets (jobs.py
    # will skip it) rather than competing for the same sentence.
    assert by_type["process"].get("scene_number") is None


def test_max_stats_zero_suppresses_untargeted_but_not_targeted(
    tmp_path, monkeypatch
):
    _fake_renders(monkeypatch)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced",
        targets=_targets(), max_stats=0,
    )
    stats = [a for a in assets if a["graphic_type"] == "stat"]
    assert len(stats) == 1
    assert stats[0].get("targeted") is True
    assert stats[0]["scene_number"] == 2


def test_default_cap_still_allows_untargeted_stats(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", targets=_targets()
    )
    stats = [a for a in assets if a["graphic_type"] == "stat"]
    # Targeted (scene 2) + untargeted "$25 million" (scene 1).
    assert len(stats) == 2
    untargeted = [a for a in stats if not a.get("targeted")]
    assert untargeted[0]["scene_number"] == 1
    assert untargeted[0]["sentence_index"] == 0


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
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", targets=_targets()
    )
    scene_media = _scene2_media(_attach(assets))
    result = build_scene_timeline(scene_media, _scene2_timings())
    items = result["items"]
    assert len(items) == 2
    # B5: the targeted sentence's item spans exactly its narration
    # window (extended to the next sentence's start, per timeline
    # rules) and is assigned as a generated graphic.
    assert items[0]["assignment"] == "motion_graphic"
    assert items[0]["relevance_status"] == "generated_graphic"
    assert items[0]["start"] == 0.0
    assert items[0]["end"] == 4.5
    # The process graphic (anchored to sentence 1) reaches the
    # timeline on its own sentence.
    assert items[1]["assignment"] == "motion_graphic"
    process = next(a for a in assets if a["graphic_type"] == "process")
    assert items[1]["file_path"] == process["file_path"]


def test_timeline_graphic_reaches_final_timeline(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", targets=_targets()
    )
    timeline_graphic = next(
        a for a in assets if a["graphic_type"] == "timeline"
    )
    scene_media = _scene2_media(_attach([timeline_graphic]))
    result = build_scene_timeline(scene_media, _scene2_timings())
    assert result["items"][0]["assignment"] == "motion_graphic"
    assert result["items"][0]["file_path"] == timeline_graphic["file_path"]


def test_verified_visual_beats_targeted_graphic(tmp_path, monkeypatch):
    _fake_renders(monkeypatch)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", targets=_targets()
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

def test_parse_event_date():
    assert graphics_pipeline._parse_event_date("9 November 1989") == (
        pytest.approx(1989 + 10 / 12)
    )
    assert graphics_pipeline._parse_event_date("June 1961") == (
        pytest.approx(1961 + 5 / 12)
    )
    assert graphics_pipeline._parse_event_date("1989") == 1989.0
    assert graphics_pipeline._parse_event_date("NOW") is None


def test_timeline_positions_proportional_to_dates():
    events = [{"when": "1961"}, {"when": "1975"}, {"when": "1989"}]
    positions, mode, reveals = graphics_pipeline._timeline_node_positions(
        events, 6.0
    )
    assert mode == "proportional"
    assert positions[0] == pytest.approx(190.0)
    assert positions[1] == pytest.approx(190 + 0.5 * 1540)
    assert positions[2] == pytest.approx(190 + 1540)
    assert reveals[0] < reveals[1] < reveals[2]


def test_timeline_positions_even_fallback_without_dates():
    events = [{"when": "NOW"}, {"when": "THEN"}]
    positions, mode, _ = graphics_pipeline._timeline_node_positions(
        events, 6.0
    )
    assert mode == "even_fallback"
    assert positions == [190.0, 190.0 + 1540.0]


# ---------------------------------------------------------------------------
# Stage B — REAL renders, ffprobe-verified, with frame evidence
# ---------------------------------------------------------------------------

def _frame_hash(path, at):
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )
    assert result.stdout, "no frame extracted"
    return hashlib.md5(result.stdout).hexdigest()


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    out = tmp_path_factory.mktemp("rendered")
    stat = out / "stat.mp4"
    graphics_pipeline._render_stat_graphic(
        stat, "$5 MILLION", "It cost $5 million to build.", duration=4.0
    )
    stat_decimal = out / "stat_decimal.mp4"
    graphics_pipeline._render_stat_graphic(
        stat_decimal, "$1.2 BILLION", "The deal was worth $1.2 billion.",
        duration=4.0,
    )
    timeline = out / "timeline.mp4"
    spacing = graphics_pipeline._render_timeline_graphic(
        timeline,
        [
            {"when": "June 1961", "what": "Tensions rise"},
            {"when": "13 August 1961", "what": "Border closed"},
            {"when": "9 November 1989", "what": "Wall falls"},
        ],
        duration=6.0,
    )
    process = out / "process.mp4"
    graphics_pipeline._render_process_graphic(
        process, ["JUNE 1961", "AUGUST 1961", "NOVEMBER 1989"],
        duration=5.0,
    )
    return {
        "stat": stat, "stat_decimal": stat_decimal,
        "timeline": timeline, "process": process, "spacing": spacing,
    }


def test_real_renders_probe_ok(rendered):
    for key in ("stat", "stat_decimal", "timeline", "process"):
        info = probe_video(rendered[key])
        assert info is not None, key
        assert info["width"] == 1920, key
        assert info["height"] == 1080, key
        assert info["duration_seconds"] > 0, key
    assert probe_video(rendered["stat"])["duration_seconds"] == (
        pytest.approx(4.0, abs=0.35)
    )
    assert probe_video(rendered["timeline"])["duration_seconds"] == (
        pytest.approx(6.0, abs=0.35)
    )
    assert probe_video(rendered["process"])["duration_seconds"] == (
        pytest.approx(5.0, abs=0.35)
    )
    # The real chronology events carry parseable dates, so the
    # proportional spacing path must have been used.
    assert rendered["spacing"] == "proportional"


def test_countup_frames_change(rendered):
    # Integer value (eif expression path): early frame shows a
    # smaller number than the final frame.
    assert _frame_hash(rendered["stat"], 0.3) != _frame_hash(
        rendered["stat"], 3.8
    )
    # Decimal value (stepped-layer path): same requirement.
    assert _frame_hash(rendered["stat_decimal"], 0.3) != _frame_hash(
        rendered["stat_decimal"], 3.8
    )


def test_timeline_reveal_frames_change(rendered):
    # Progressive reveal: the first node appears early, the last one
    # only near the end — first and last frames must differ.
    assert _frame_hash(rendered["timeline"], 0.3) != _frame_hash(
        rendered["timeline"], 5.6
    )


def test_process_frames_change(rendered):
    # Progressive step reveal: later steps appear over time.
    assert _frame_hash(rendered["process"], 0.2) != _frame_hash(
        rendered["process"], 4.6
    )


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

    monkeypatch.setattr(graphics_pipeline, "_render_stat_graphic", fake_write)
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
