"""Phase 5 (Master Directive) — video continuation + dedupe dealing.

Repetition cheap win 1: when a video segment is reused inside a
scene, the timeline item continues into the segment's unused
seconds (its clip_start advances past the previously shown
window) instead of restarting at the head, up to the segment's
real duration; the item records the ACTUAL source window shown
(clip_start / clip_end), including the tail-shorter-than-window
case. The use cap is untouched — continuation changes WHICH
seconds show, never how many uses are allowed.

Repetition cheap win 2 (dealing side): two files carrying the
same still (identical bytes under different canonical sources)
are dealt and cap-accounted as ONE visual.
"""

import hashlib

import jobs  # noqa: F401
from services import timeline as tl


def _video_visual(path, *, duration=10.0, start=0.0, url="https://vid.example.org/moon"):
    return {
        "file_path": str(path), "path": str(path),
        "type": "video", "visual_type": "video",
        "source_url": url, "url": url, "video_url": url,
        "clip_start": start,
        "clip_duration": duration, "duration_seconds": duration,
        "verification": {"accepted": True, "score": 0.9},
    }


def _timings(count, step=4.0):
    return [
        {
            "sentence_index": i, "start": i * step, "end": (i + 1) * step,
            "text": f"sentence {i + 1}",
        }
        for i in range(count)
    ]


def _scene(visual, *, direct_positions, count, duration):
    sentences = []
    for position in range(count):
        sentences.append({
            "sentence_number": position + 1,
            "videos": [visual] if position in direct_positions else [],
            "images": [],
        })
    return {
        "scene_number": 1, "duration_seconds": duration,
        "videos": [visual], "images": [],
        "sentences": sentences,
    }


def test_continuation_advances_offsets_and_records_true_windows(tmp_path):
    path = tmp_path / "moon.mp4"
    path.write_bytes(b"v")
    visual = _video_visual(path, duration=10.0)
    result = tl.build_scene_timeline(
        _scene(visual, direct_positions={0, 1, 2, 3}, count=4, duration=16.0),
        sentence_timings=_timings(4),
    )
    items = result["items"]
    assert len(items) == 4
    windows = [(i["clip_start"], i["clip_end"]) for i in items]
    # 10s segment, 4s windows: continue 0→4→8, the third window
    # stops at the segment's real end (tail shorter than window),
    # and the fourth use — segment fully consumed — restarts at
    # the head, recorded exactly so.
    assert windows == [(0.0, 4.0), (4.0, 8.0), (8.0, 10.0), (0.0, 4.0)]
    assert "clip_continued" not in items[0]
    assert items[1]["clip_continued"] is True
    assert items[2]["clip_continued"] is True
    assert "clip_continued" not in items[3]


def test_continuation_applies_to_rotation_reuse(tmp_path):
    path = tmp_path / "moon.mp4"
    path.write_bytes(b"v")
    visual = _video_visual(path, duration=10.0)
    result = tl.build_scene_timeline(
        _scene(visual, direct_positions=set(), count=2, duration=8.0),
        sentence_timings=_timings(2),
    )
    items = result["items"]
    assert [i["assignment"] for i in items] == ["rotation", "rotation"]
    assert [(i["clip_start"], i["clip_end"]) for i in items] == [
        (0.0, 4.0), (4.0, 8.0),
    ]
    assert items[1]["clip_continued"] is True


def test_continuation_respects_nonzero_segment_start(tmp_path):
    path = tmp_path / "seg.mp4"
    path.write_bytes(b"v")
    visual = _video_visual(path, duration=6.0, start=30.0)
    result = tl.build_scene_timeline(
        _scene(visual, direct_positions={0, 1}, count=2, duration=8.0),
        sentence_timings=_timings(2),
    )
    items = result["items"]
    # The segment lives at [30, 36) in its source: windows advance
    # within it, never before its head or past its end.
    assert [(i["clip_start"], i["clip_end"]) for i in items] == [
        (30.0, 34.0), (34.0, 36.0),
    ]


def test_no_continuation_without_segment_duration(tmp_path):
    path = tmp_path / "mystery.mp4"
    path.write_bytes(b"v")
    visual = _video_visual(path)
    del visual["clip_duration"]
    del visual["duration_seconds"]
    result = tl.build_scene_timeline(
        _scene(visual, direct_positions={0, 1}, count=2, duration=8.0),
        sentence_timings=_timings(2),
    )
    items = result["items"]
    # The segment's real length is unknown: no invented advance —
    # the offset stays at the recorded head and the window is the
    # item's own shown span.
    assert [(i["clip_start"], i["clip_end"]) for i in items] == [
        (0.0, 4.0), (0.0, 4.0),
    ]
    assert all("clip_continued" not in i for i in items)


def test_generated_graphic_restarts_by_design(tmp_path):
    path = tmp_path / "motion_graphics" / "g.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"v")
    graphic = {
        "file_path": str(path), "path": str(path),
        "type": "video", "visual_type": "video",
        "motion_graphic": True, "sentence_index": 0,
        "clip_duration": 10.0, "duration_seconds": 10.0,
        # Phase 1 / AM-012: a graphic is eligible only with a
        # passing post-render validation record.
        "validation": {"passed": True},
    }
    scene = {
        "scene_number": 1, "duration_seconds": 8.0,
        "videos": [graphic], "images": [],
        "sentences": [
            {"sentence_number": 1, "videos": [graphic], "images": []},
            {"sentence_number": 2, "videos": [graphic], "images": []},
        ],
    }
    result = tl.build_scene_timeline(scene, sentence_timings=_timings(2))
    items = result["items"]
    assert len(items) == 2
    assert all(i["clip_start"] == 0.0 for i in items)
    assert all("clip_continued" not in i for i in items)


def test_still_items_carry_no_clip_window(tmp_path):
    path = tmp_path / "s.jpg"
    path.write_bytes(b"x")
    still = {
        "file_path": str(path), "path": str(path),
        "type": "image", "visual_type": "image",
        "source_url": "https://img.example.org/s",
        "verification": {"accepted": True, "score": 0.9},
    }
    scene = {
        "scene_number": 1, "duration_seconds": 4.0,
        "videos": [], "images": [still],
        "sentences": [
            {"sentence_number": 1, "videos": [], "images": [still]},
        ],
    }
    result = tl.build_scene_timeline(scene, sentence_timings=_timings(1))
    (item,) = result["items"]
    assert "clip_start" not in item
    assert "clip_end" not in item


# ---------------------------------------------------------------------------
# AM-003 at the dealing level: rotation prefers a never-shown
# distinct asset over repeating an already-shown one, even when
# the shown asset's uses were uncharged spanning uses.
# ---------------------------------------------------------------------------

def test_rotation_prefers_never_shown_distinct_asset(tmp_path):
    def _still(name, url):
        path = tmp_path / name
        path.write_bytes(b"x")
        return {
            "file_path": str(path), "path": str(path),
            "type": "image", "visual_type": "image",
            "source_url": url, "url": url,
            "verification": {"accepted": True, "score": 0.9},
        }

    backdrop = _still("a.jpg", "https://img.example.org/a")
    second = _still("b.jpg", "https://img.example.org/b")
    spare = _still("c.jpg", "https://img.example.org/c")
    scene = {
        "scene_number": 1, "duration_seconds": 12.0,
        "videos": [], "images": [backdrop, second, spare],
        "sentences": [
            {"sentence_number": 1, "beat_id": "beat_bg",
             "videos": [], "images": [backdrop]},
            {"sentence_number": 2, "beat_id": "beat_bg",
             "videos": [], "images": [second]},
            {"sentence_number": 3, "beat_id": "beat_bg",
             "videos": [], "images": []},
        ],
        "beats": [{
            "beat_id": "beat_bg", "role": "background",
            "sentence_numbers": [1, 2, 3],
        }],
    }
    result = tl.build_scene_timeline(scene, sentence_timings=_timings(3))
    items = result["items"]
    assert len(items) == 3
    # Sentences 1-2 span their own beat's assets (uncharged);
    # sentence 3's rotation must deal the never-shown spare, not
    # repeat a shown asset on a pool-order tie.
    assert items[2]["assignment"] == "rotation"
    assert items[2]["file_path"] == spare["file_path"]


# ---------------------------------------------------------------------------
# Dedupe by source image — the dealing side
# ---------------------------------------------------------------------------

def test_identical_bytes_dealt_as_one_visual(tmp_path):
    digest = hashlib.sha256(b"the same building photo").hexdigest()
    visuals = []
    for i, url in enumerate(
        ("https://a.example.org/building", "https://b.example.org/building-copy")
    ):
        path = tmp_path / f"building_{i}.jpg"
        path.write_bytes(b"the same building photo")
        visuals.append({
            "file_path": str(path), "path": str(path),
            "type": "image", "visual_type": "image",
            "source_url": url, "url": url,
            "content_hash": digest,
            "verification": {"accepted": True, "score": 0.9},
        })
    scene = {
        "scene_number": 1, "duration_seconds": 16.0,
        "videos": [], "images": visuals,
        "sentences": [
            {"sentence_number": i + 1, "videos": [], "images": []}
            for i in range(4)
        ],
    }
    result = tl.build_scene_timeline(scene, sentence_timings=_timings(4))
    # Two FILES, one visual: the shared identity's cap (3) binds
    # across both files — the fourth sentence stays uncovered
    # instead of "rotating" onto the duplicate download.
    assert len(result["items"]) == 3
    assert result["uncovered_sentence_indexes"] == [3]
