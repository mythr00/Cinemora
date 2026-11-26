"""Gate 4 / B5 regression tests: timeline freeze prevention.

The Hyatt test run's S01 was an 8.0 s verified segment placed in an
11.517 s sentence slot; the renderer filled the remainder by
holding the segment's last frame frozen (tpad clone) — a founder-
ruled defect (footage must play as continuous segments). The fix
lives in services.timeline.build_scene_timeline:

- a sentence-assigned video whose remaining segment falls short
  of its slot by more than FREEZE_TOLERANCE_SECONDS (0.5 s) is
  replaced by the sentence's next verified segment that covers;
- when no candidate covers, the item keeps the pick and carries
  an explicit ``timeline_defect`` (aggregated into the project
  timeline's ``timeline_defects``) instead of freezing silently;
- narration timing (item start/end) is never touched.
"""

import json
import uuid

from services.timeline import (
    FREEZE_TOLERANCE_SECONDS,
    build_project_timeline,
    build_scene_timeline,
)


def _video(tmp_path, name, segment_seconds):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 2048)
    visual = {
        "file_path": str(path), "path": str(path), "type": "video",
        "title": name, "verification": {"accepted": True},
    }
    if segment_seconds is not None:
        visual["clip_duration"] = float(segment_seconds)
        visual["duration_seconds"] = float(segment_seconds)
    return visual


def _scene(tmp_path, sentence_candidates, slot=11.5):
    aggregate = [c for c in sentence_candidates]
    return {
        "scene_number": 1,
        "duration_seconds": slot,
        "videos": aggregate,
        "images": [],
        "sentences": [
            {"sentence_number": 1, "videos": sentence_candidates,
             "images": []},
        ],
    }


def _timings(slot=11.5):
    return [{
        "sentence_index": 0, "text": "Sentence one.",
        "start": 0.0, "end": slot,
    }]


def test_short_segment_long_slot_records_explicit_defect(tmp_path):
    short = _video(tmp_path, "short.mp4", 8.0)
    result = build_scene_timeline(_scene(tmp_path, [short]), _timings())
    (item,) = result["items"]
    # The pick stands (no covering option exists) and the timing is
    # the narration's, untouched.
    assert item["file_path"] == short["file_path"]
    assert item["start"] == 0.0 and item["end"] == 11.5
    assert item["clip_start"] == 0.0 and item["clip_end"] == 8.0
    defect = item["timeline_defect"]
    assert defect["kind"] == "segment_shorter_than_slot"
    assert defect["freeze_seconds"] == 3.5
    assert defect["tolerance_seconds"] == FREEZE_TOLERANCE_SECONDS
    assert defect["segment_seconds"] == 8.0
    assert "coverage_substitution" not in item


def test_covering_alternate_is_substituted(tmp_path):
    short = _video(tmp_path, "short.mp4", 8.0)
    covering = _video(tmp_path, "covering.mp4", 12.0)
    result = build_scene_timeline(
        _scene(tmp_path, [short, covering]), _timings()
    )
    (item,) = result["items"]
    assert item["file_path"] == covering["file_path"]
    assert item["assignment"] == "sentence"
    assert item["relevance_status"] == "verified_for_sentence"
    assert item["clip_end"] - item["clip_start"] == 11.5
    assert "timeline_defect" not in item
    substitution = item["coverage_substitution"]
    assert substitution["reason"] == "segment_shorter_than_slot"
    assert substitution["original_file"] == short["file_path"]
    assert substitution["original_coverage_seconds"] == 8.0
    # Narration timing untouched by the swap.
    assert item["start"] == 0.0 and item["end"] == 11.5


def test_non_covering_alternate_does_not_substitute(tmp_path):
    short = _video(tmp_path, "short.mp4", 8.0)
    also_short = _video(tmp_path, "also_short.mp4", 9.0)
    result = build_scene_timeline(
        _scene(tmp_path, [short, also_short]), _timings()
    )
    (item,) = result["items"]
    assert item["file_path"] == short["file_path"]
    assert "coverage_substitution" not in item
    assert item["timeline_defect"]["freeze_seconds"] == 3.5


def test_shortfall_within_tolerance_is_not_a_defect(tmp_path):
    near = _video(tmp_path, "near.mp4", 11.2)
    covering = _video(tmp_path, "covering.mp4", 12.0)
    result = build_scene_timeline(
        _scene(tmp_path, [near, covering]), _timings()
    )
    (item,) = result["items"]
    # 0.3 s shortfall is inside the 0.5 s tolerance: the pick
    # stands, no substitution, no defect.
    assert item["file_path"] == near["file_path"]
    assert "timeline_defect" not in item
    assert "coverage_substitution" not in item


def test_covering_segment_is_untouched(tmp_path):
    covering = _video(tmp_path, "covering.mp4", 12.0)
    result = build_scene_timeline(
        _scene(tmp_path, [covering]), _timings()
    )
    (item,) = result["items"]
    assert item["file_path"] == covering["file_path"]
    assert "timeline_defect" not in item
    assert "coverage_substitution" not in item


def test_unknown_segment_length_is_not_judged(tmp_path):
    unknown = _video(tmp_path, "unknown.mp4", None)
    result = build_scene_timeline(
        _scene(tmp_path, [unknown]), _timings()
    )
    (item,) = result["items"]
    assert item["file_path"] == unknown["file_path"]
    assert "timeline_defect" not in item


def _write_measured_parts(media_dir, durations):
    import wave

    parts_dir = media_dir / "narration_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    for index, seconds in enumerate(durations):
        name = f"part_{index:03d}.wav"
        with wave.open(str(parts_dir / name), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(22050)
            wav.writeframes(b"\x00\x00" * int(22050 * seconds))
        parts.append({
            "index": index, "file": name,
            "hash": f"fixture-{index}",
            "sentence_sha256": f"fixture-{index}",
            "measured_duration_seconds": float(seconds),
            "duration_source": "ffprobe_part",
        })
    (parts_dir / "parts_manifest.json").write_text(json.dumps(
        {"version": 2, "model": "fixture", "parts": parts}
    ))


def test_project_timeline_aggregates_defects_into_manifest(tmp_path):
    project_id = f"gate4-{uuid.uuid4()}"
    media_dir = tmp_path / "media" / project_id
    media_dir.mkdir(parents=True)
    _write_measured_parts(media_dir, [11.5])
    short = _video(tmp_path, "short.mp4", 8.0)
    scenes = [{
        "scene": {"scene_number": 1},
        "scene_number": 1,
        "duration_seconds": 11.5,
        "videos": [short],
        "images": [],
        "sentences": [
            {"sentence_number": 1, "sentence_text": "Sentence one.",
             "videos": [short], "images": []},
        ],
    }]
    timeline = build_project_timeline(
        project_id, scenes, output_dir=str(tmp_path / "media")
    )
    (defect,) = timeline["timeline_defects"]
    assert defect["kind"] == "segment_shorter_than_slot"
    assert defect["scene_number"] == 1
    assert defect["sentence_index"] == 0
    assert defect["freeze_seconds"] == 3.5
