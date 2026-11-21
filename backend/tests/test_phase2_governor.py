"""Master Directive Phase 2 — FP-2 scene governor and FP-5 timing
hygiene.

FP-2: derive_scenes never emits a 1-sentence scene when a
neighbour can absorb it (the Gilgo lone-opening-sentence shape),
and scenes whose MEASURED narration exceeds the threshold are
split at beat boundaries after narration. The threshold is a
founder editorial parameter — a named constant
(SCENE_SPLIT_THRESHOLD_SECONDS = 20.0), stamped into split
children and into the production's governor record — not a tuned
optimum.

FP-5: sentence-window timeline items record duration as
end - start (never the pre-clamp floored value), and a window the
MIN_SENTENCE_DURATION floor extended is stamped padded. The floor
itself is unchanged.
"""

import json
from pathlib import Path

import jobs
from services import scene_structure
from services import timeline as tl


# ---------------------------------------------------------------------------
# FP-2 merge: no 1-sentence scenes from derive_scenes
# ---------------------------------------------------------------------------

def _sentence(number, *, subject=None, place=None, year=None):
    record = {
        "sentence_number": number,
        "text": f"Sentence {number}.",
        "subject_id": subject,
        "locations": [place] if place else [],
        "dates": [str(year)] if year else [],
        "scenarios": [],
    }
    return record


def test_gilgo_shape_lone_opening_sentence_merges():
    sentences = [
        _sentence(1, place="Gilgo Beach", year=2010),
        _sentence(2, subject="rex", place="Long Island", year=2023),
        _sentence(3, subject="rex", place="Long Island", year=2023),
    ]
    scenes = scene_structure.derive_scenes(sentences, {})
    # Pre-governor: [1] [2,3] — a starved 1-sentence opening scene.
    assert len(scenes) == 1
    assert scenes[0]["sentence_count"] == 3
    assert scenes[0]["sentences"] == sentences


def test_mid_list_singleton_merges_into_previous():
    sentences = [
        _sentence(1, subject="a"), _sentence(2, subject="a"),
        _sentence(3, subject="b"),
        _sentence(4, subject="a"), _sentence(5, subject="a"),
    ]
    scenes = scene_structure.derive_scenes(sentences, {})
    assert [s["sentence_count"] for s in scenes] == [3, 2]
    assert all(s["sentence_count"] > 1 for s in scenes)


def test_alternating_singletons_collapse():
    sentences = [
        _sentence(1, subject="a"), _sentence(2, subject="b"),
        _sentence(3, subject="a"), _sentence(4, subject="b"),
    ]
    scenes = scene_structure.derive_scenes(sentences, {})
    assert len(scenes) == 1
    assert scenes[0]["sentence_count"] == 4


def test_single_sentence_script_keeps_its_scene():
    scenes = scene_structure.derive_scenes([_sentence(1, subject="a")], {})
    assert len(scenes) == 1
    assert scenes[0]["sentence_count"] == 1


def test_no_singletons_in_mixed_structure():
    sentences = (
        [_sentence(1, subject="a")]
        + [_sentence(i, subject="b") for i in (2, 3)]
        + [_sentence(4, subject="c")]
        + [_sentence(i, subject="d") for i in (5, 6, 7)]
    )
    scenes = scene_structure.derive_scenes(sentences, {})
    assert all(s["sentence_count"] > 1 for s in scenes)
    assert sum(s["sentence_count"] for s in scenes) == 7


# ---------------------------------------------------------------------------
# FP-2 split: post-narration, at beat boundaries, threshold stamped
# ---------------------------------------------------------------------------

def test_split_threshold_is_the_named_constant():
    assert scene_structure.SCENE_SPLIT_THRESHOLD_SECONDS == 20.0


def _scene(records, number=1):
    return {
        "scene_number": number,
        "sentences": records,
        "text": " ".join(r["text"] for r in records),
        "sentence_count": len(records),
        "project_id": "", "script_hash": "", "bible": {},
        "case_bible_path": "",
    }


def test_long_scene_splits_at_beat_boundaries():
    records = (
        [_sentence(i, subject="a") for i in (1, 2)]
        + [_sentence(i, subject="b") for i in (3, 4)]
        + [_sentence(i, subject="a") for i in (5, 6)]
    )
    scenes = scene_structure.split_scenes_by_narration(
        [_scene(records)], [8.0] * 6,
    )
    # 48s total; beat groups of 16s each pack one per child under
    # the 20s threshold (two groups would be 32s > 20s).
    assert len(scenes) == 3
    assert [s["sentence_count"] for s in scenes] == [2, 2, 2]
    assert [s["scene_number"] for s in scenes] == [1, 2, 3]
    for child in scenes:
        stamp = child["narration_split"]
        assert stamp["threshold_seconds"] == 20.0
        assert stamp["parent_scene_number"] == 1
        assert stamp["narration_seconds"] == 16.0
    # Sentence order is preserved across the split.
    flat = [r for s in scenes for r in s["sentences"]]
    assert flat == records


def test_scene_under_threshold_is_untouched():
    records = [_sentence(i, subject="a") for i in (1, 2, 3)]
    scenes = scene_structure.split_scenes_by_narration(
        [_scene(records)], [5.0, 5.0, 5.0],
    )
    assert len(scenes) == 1
    assert "narration_split" not in scenes[0]


def test_single_beat_long_scene_is_never_cut():
    records = [_sentence(i, subject="a") for i in range(1, 7)]
    scenes = scene_structure.split_scenes_by_narration(
        [_scene(records)], [8.0] * 6,
    )
    # One beat group: no boundary to cut at — the scene stays
    # whole rather than being split mid-beat.
    assert len(scenes) == 1
    assert scenes[0]["sentence_count"] == 6


def test_split_packs_beats_greedily():
    records = (
        [_sentence(1, subject="a")]
        + [_sentence(i, subject="b") for i in (2, 3)]
        + [_sentence(4, subject="c")]
    )
    # Beats: a=6s, b=12s, c=6s (total 24s > 20s). Greedy packing:
    # [a+b=18s] [c=6s].
    scenes = scene_structure.split_scenes_by_narration(
        [_scene(records)], [6.0, 6.0, 6.0, 6.0],
    )
    assert [s["sentence_count"] for s in scenes] == [3, 1]
    assert scenes[0]["narration_split"]["narration_seconds"] == 18.0


# ---------------------------------------------------------------------------
# FP-2 governor wiring in jobs.build_production_scenes' helper
# ---------------------------------------------------------------------------

def test_apply_scene_governor_with_measured_manifest(tmp_path):
    records = (
        [_sentence(i, subject="a") for i in (1, 2)]
        + [_sentence(i, subject="b") for i in (3, 4)]
        + [_sentence(i, subject="a") for i in (5, 6)]
    )
    scenes = [_scene(records)]
    parts_dir = tmp_path / "narration_parts"
    parts_dir.mkdir()
    (parts_dir / "parts_manifest.json").write_text(json.dumps({
        "parts": [
            {"index": i, "measured_duration_seconds": 8.0}
            for i in range(6)
        ],
    }))
    governed, governor = jobs._apply_scene_governor(scenes, tmp_path, 6)
    assert len(governed) == 3
    assert governor["split_threshold_seconds"] == 20.0
    assert governor["narration_split_applied"] is True
    assert governor["scenes_split"] == 1
    assert governor["reason"] is None


def test_apply_scene_governor_without_manifest_is_honest(tmp_path):
    records = [_sentence(i, subject="a") for i in (1, 2)]
    scenes = [_scene(records)]
    governed, governor = jobs._apply_scene_governor(scenes, tmp_path, 2)
    assert governed == scenes
    assert governor["split_threshold_seconds"] == 20.0
    assert governor["narration_split_applied"] is False
    assert governor["reason"] == "narration_manifest_unavailable"


def test_apply_scene_governor_count_mismatch_leaves_scenes(tmp_path):
    records = [_sentence(i, subject="a") for i in (1, 2)]
    scenes = [_scene(records)]
    parts_dir = tmp_path / "narration_parts"
    parts_dir.mkdir()
    (parts_dir / "parts_manifest.json").write_text(json.dumps({
        "parts": [{"index": 0, "measured_duration_seconds": 8.0}],
    }))
    governed, governor = jobs._apply_scene_governor(scenes, tmp_path, 2)
    assert governed == scenes
    assert governor["reason"] == "narration_manifest_unavailable"


# ---------------------------------------------------------------------------
# FP-5: duration is end - start; floor-extended items are padded
# ---------------------------------------------------------------------------

def _image_asset(path, url):
    return {
        "file_path": str(path), "path": str(path),
        "type": "image", "visual_type": "image",
        "source_url": url, "url": url, "title": url,
        "verification": {"accepted": True, "score": 0.9},
    }


def _fp5_scene(tmp_path, duration):
    first = _image_asset(tmp_path / "a.jpg", "https://img.example.org/a")
    second = _image_asset(tmp_path / "b.jpg", "https://img.example.org/b")
    for asset in (first, second):
        Path(asset["file_path"]).write_bytes(b"x")
    return {
        "scene_number": 1,
        "duration_seconds": duration,
        "images": [first, second],
        "videos": [],
        "sentences": [
            {"sentence_number": 1, "beat_id": "b1",
             "videos": [], "images": [first]},
            {"sentence_number": 2, "beat_id": "b1",
             "videos": [], "images": [second]},
        ],
        "beats": [{
            "beat_id": "b1", "role": "background",
            "sentence_numbers": [1, 2],
        }],
    }


def test_fp5_floored_item_records_true_window_and_padded(tmp_path):
    scene_media = _fp5_scene(tmp_path, duration=10.0)
    timings = [
        # Sentence 1's natural window is 1.0s (next start 1.0):
        # the 2.0s floor extends it — padded.
        {"sentence_index": 0, "start": 0.0, "end": 0.5,
         "text": "sentence 1"},
        {"sentence_index": 1, "start": 1.0, "end": 9.0,
         "text": "sentence 2"},
    ]
    result = tl.build_scene_timeline(scene_media, sentence_timings=timings)
    assert len(result["items"]) == 2
    first, second = result["items"]
    assert first["duration"] == round(first["end"] - first["start"], 3)
    assert first["padded"] is True
    assert first["end"] - first["start"] >= tl.MIN_SENTENCE_DURATION
    # Sentence 2's window is natural: no stamp, duration consistent.
    assert "padded" not in second
    assert second["duration"] == round(second["end"] - second["start"], 3)


def test_fp5_clamped_item_never_claims_the_floored_duration(tmp_path):
    scene_media = _fp5_scene(tmp_path, duration=1.5)
    timings = [
        {"sentence_index": 0, "start": 0.0, "end": 0.5,
         "text": "sentence 1"},
        {"sentence_index": 1, "start": 1.0, "end": 1.5,
         "text": "sentence 2"},
    ]
    result = tl.build_scene_timeline(scene_media, sentence_timings=timings)
    assert result["items"]
    for item in result["items"]:
        # The pre-fix defect: duration recorded the floored 2.0s
        # while the clamped window was shorter. Now the record is
        # always the window itself.
        assert item["duration"] == round(item["end"] - item["start"], 3)
        assert item["duration"] <= 1.5
    assert result["items"][0]["padded"] is True
