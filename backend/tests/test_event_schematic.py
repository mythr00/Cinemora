"""Event schematic + harness targeting regression tests.

Covers the chain the v4 E2E run exposed as untested:

- the five-minute harness now builds graphics targets from the
  Asset Manager's missing-visual records with the SAME logic as
  jobs.run_full_pipeline (build_graphic_targets);
- a targeted sentence with no statistic, in a scene with NO
  verified visuals at all, whose own analysis record carries
  event/place labels, receives an EVENT SCHEMATIC graphic
  (services/vector_graphics.py) anchored to its exact sentence;
- the trigger is conservative: a scene with verified visuals, or
  missing labels, produces no schematic and the skip is recorded;
- the schematic flows through attachment into build_scene_timeline
  as a motion_graphic assignment referencing its real file;
- the real PIL renderer produces a valid, animated MP4
  (ffprobe + frame-difference evidence).
"""

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from services import graphics_pipeline
from services.media_probe import probe_video
from services.timeline import build_scene_timeline


# ---------------------------------------------------------------------------
# Harness target builder (tests/e2e_five_minute.py)
# ---------------------------------------------------------------------------

def _load_harness():
    """Import the E2E harness module (its pipeline is __main__-
    guarded) while restoring the env/cwd its import touches."""
    harness_path = Path(__file__).resolve().parent / "e2e_five_minute.py"
    env_snapshot = dict(os.environ)
    cwd_snapshot = os.getcwd()
    try:
        spec = importlib.util.spec_from_file_location(
            "e2e_five_minute", harness_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(env_snapshot)
        os.chdir(cwd_snapshot)


def test_harness_build_graphic_targets_mirrors_production():
    harness = _load_harness()
    processed_scenes = [
        {
            "scene": {
                "scene_number": 1,
                "sentences": [
                    {"sentence_number": 1, "text": "Alpha opening."},
                    {
                        "sentence_number": 2,
                        "text": "Beta missing sentence.",
                        "scenario": {"event": "Beta event"},
                    },
                    {"sentence_number": 3, "text": "Gamma covered."},
                ],
            },
            "asset_stats": {
                "missing_visuals": [
                    {"sentence_number": 2},
                    {"sentence_number": None},
                    {"sentence_number": 99},
                ]
            },
            "videos": [],
            "images": [],
        }
    ]
    targets = harness.build_graphic_targets(processed_scenes)
    assert len(targets) == 1
    target = targets[0]
    assert target["scene_number"] == 1
    # Scene-local, 0-based index of sentence_number 2.
    assert target["sentence_index"] == 1
    assert target["sentence_number"] == 2
    assert target["text"] == "Beta missing sentence."
    assert target["scenario"] == {"event": "Beta event"}


# ---------------------------------------------------------------------------
# Event schematic trigger
# ---------------------------------------------------------------------------

_SENTENCE_TEXT = "Crowds poured through the opened checkpoint at dawn."


def _sentence():
    return {
        "text": _SENTENCE_TEXT,
        "sentence_number": 3,
        "event": "opening of the Berlin Wall",
        "locations": ["Berlin"],
        "dates": ["November 9, 1989"],
        "scenarios": [
            {
                "visual": "Crowds pouring through an opened checkpoint.",
                "kind": "event",
                "event": "opening of the Berlin Wall",
                "place": "berlin",
                "era": "1989",
            }
        ],
    }


def _data(processed_videos=None, sentence=None):
    sentence = sentence if sentence is not None else _sentence()
    return {
        "case_bible": {"timeline": []},
        "scenes": [{"scene_number": 2, "sentences": [sentence]}],
        "processed_scenes": [
            {
                "scene": {"scene_number": 2, "sentences": [sentence]},
                "videos": processed_videos or [],
                "images": [],
                "asset_stats": {"missing_visuals": []},
            }
        ],
    }


def _target():
    # Mirrors production reality: jobs.py's target builder finds no
    # singular "scenario" on sentence records, so none is passed.
    return {
        "scene_number": 2,
        "sentence_index": 0,
        "sentence_number": 3,
        "text": _SENTENCE_TEXT,
    }


def _fake_schematic(monkeypatch):
    calls = []

    def fake_render(path, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)
        calls.append(kwargs)
        return str(path)

    monkeypatch.setattr(
        "services.vector_graphics.render_event_schematic", fake_render
    )
    return calls


def _fake_stat_and_summary(monkeypatch):
    # No timeline events in the fixture data, so only the stat
    # renderer needs a fake (it is never reached for our sentence,
    # but keep the pattern consistent with the Stage A tests).
    def fake_write(path, *args, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)

    monkeypatch.setattr(graphics_pipeline, "_render_stat_graphic", fake_write)


def test_schematic_generated_for_empty_scene_with_labels(
    tmp_path, monkeypatch
):
    calls = _fake_schematic(monkeypatch)
    _fake_stat_and_summary(monkeypatch)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", targets=[_target()]
    )
    schematics = [
        a for a in assets if a["graphic_type"] == "event_schematic"
    ]
    assert len(schematics) == 1
    graphic = schematics[0]
    assert graphic["scene_number"] == 2
    assert graphic["sentence_index"] == 0
    assert graphic["sentence_number"] == 3
    assert graphic["targeted"] is True
    # Honesty stamps: generated, never footage/verified.
    assert graphic["generated"] is True
    assert graphic["visual_format"] == "graphic"
    assert graphic["visual_format_evidence"] == "generated_in_house"
    assert graphic["source"] == "cinemora_motion_graphics"
    # Labels resolved from the sentence's own scenario record.
    assert len(calls) == 1
    assert calls[0]["event_label"] == "opening of the Berlin Wall"
    assert calls[0]["place_label"] == "Berlin"
    assert calls[0]["era_label"] == "1989"
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert manifest["targets"]["requested"] == 1
    assert manifest["targets"]["generated"] == 1
    assert manifest["targets"]["skipped"] == []


def test_schematic_not_generated_when_scene_has_verified_visuals(
    tmp_path, monkeypatch
):
    _fake_schematic(monkeypatch)
    _fake_stat_and_summary(monkeypatch)
    verified = {"file_path": "/x/verified.mp4", "type": "video"}
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(processed_videos=[verified]),
        tmp_path, mode="balanced", targets=[_target()],
    )
    assert not [
        a for a in assets if a["graphic_type"] == "event_schematic"
    ]
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert manifest["targets"]["generated"] == 0
    assert manifest["targets"]["skipped"][0]["reason"] == "no_stat_value"


def test_schematic_skipped_and_recorded_without_labels(
    tmp_path, monkeypatch
):
    _fake_schematic(monkeypatch)
    _fake_stat_and_summary(monkeypatch)
    bare = {"text": _SENTENCE_TEXT, "sentence_number": 3}
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(sentence=bare), tmp_path, mode="balanced",
        targets=[_target()],
    )
    assert not [
        a for a in assets if a["graphic_type"] == "event_schematic"
    ]
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert manifest["targets"]["skipped"][0]["reason"] == (
        "no_schematic_labels"
    )


def test_schematic_capped_at_one_per_scene(tmp_path, monkeypatch):
    _fake_schematic(monkeypatch)
    _fake_stat_and_summary(monkeypatch)
    data = _data()
    second = dict(_sentence())
    second["text"] = "Neighbours embraced strangers on the bridge."
    second["sentence_number"] = 4
    data["scenes"][0]["sentences"].append(second)
    data["processed_scenes"][0]["scene"]["sentences"].append(second)
    targets = [
        _target(),
        {
            "scene_number": 2,
            "sentence_index": 1,
            "sentence_number": 4,
            "text": second["text"],
        },
    ]
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", targets=targets
    )
    schematics = [
        a for a in assets if a["graphic_type"] == "event_schematic"
    ]
    assert len(schematics) == 1
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    reasons = [s["reason"] for s in manifest["targets"]["skipped"]]
    assert reasons == ["schematic_cap_reached"]


# ---------------------------------------------------------------------------
# Full chain: schematic -> attach -> timeline assignment
# ---------------------------------------------------------------------------

def test_schematic_reaches_timeline_as_motion_graphic(
    tmp_path, monkeypatch
):
    _fake_schematic(monkeypatch)
    _fake_stat_and_summary(monkeypatch)
    assets = graphics_pipeline.build_motion_graphics_assets(
        _data(), tmp_path, mode="balanced", targets=[_target()]
    )
    schematic = next(
        a for a in assets if a["graphic_type"] == "event_schematic"
    )
    # Production attach step (jobs.py): the graphic becomes a scene
    # visual stamped as a motion graphic.
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
        {"sentence_index": 0, "text": _SENTENCE_TEXT,
         "start": 0.0, "end": 4.0},
        {"sentence_index": 1, "text": "Neighbours embraced strangers.",
         "start": 4.5, "end": 9.0},
    ]
    result = build_scene_timeline(scene_media, timings)
    item = result["items"][0]
    assert item["assignment"] == "motion_graphic"
    assert item["relevance_status"] == "generated_graphic"
    assert item["file_path"] == schematic["file_path"]
    assert item["start"] == 0.0
    assert item["end"] == 4.5


# ---------------------------------------------------------------------------
# Real PIL render: ffprobe + animation evidence
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


def test_real_schematic_render_probes_and_animates(tmp_path):
    from services.vector_graphics import render_event_schematic

    out = tmp_path / "schematic.mp4"
    render_event_schematic(
        out,
        event_label="opening of the Berlin Wall",
        place_label="Berlin",
        era_label="1989",
        duration=6.0,
    )
    info = probe_video(out)
    assert info is not None
    assert info["width"] == 1920
    assert info["height"] == 1080
    assert info["duration_seconds"] == pytest.approx(6.0, abs=0.35)
    # The barrier arm lifts and figures cross: early and late
    # frames must differ.
    assert _frame_hash(out, 0.4) != _frame_hash(out, 5.4)
