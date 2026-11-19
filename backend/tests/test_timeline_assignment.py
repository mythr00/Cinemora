"""AUD-14 regression tests: sentence-level timeline assignment.

The media pipeline verifies a specific visual for each sentence. The
timeline previously discarded those assignments and paired scene visuals
to sentences round-robin (visuals[i % len(visuals)]), so a clip verified
for sentence A could illustrate sentence B. These tests pin the corrected
behaviour: each sentence keeps its own verified visual; rotation survives
only as an explicitly marked fallback for sentences with no assignment.
"""

from services.timeline import build_scene_timeline


def _visual(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 2048)
    return {"file_path": str(path), "path": str(path), "type": "video", "title": name}


def _timings(n):
    return [
        {
            "sentence_index": i,
            "text": f"Sentence {i}.",
            "start": float(i * 5),
            "end": float(i * 5 + 4),
        }
        for i in range(n)
    ]


def test_each_sentence_keeps_its_own_verified_visual(tmp_path):
    v_a = _visual(tmp_path, "alpha.mp4")
    v_b = _visual(tmp_path, "bravo.mp4")
    v_c = _visual(tmp_path, "charlie.mp4")

    scene_media = {
        "scene_number": 1,
        "duration_seconds": 15,
        # Scene aggregates deliberately in a DIFFERENT order: round-robin
        # would pair sentence 0->charlie, 1->alpha, 2->bravo.
        "videos": [v_c, v_a, v_b],
        "images": [],
        "sentences": [
            {"sentence_number": 1, "videos": [v_a], "images": []},
            {"sentence_number": 2, "videos": [v_b], "images": []},
            {"sentence_number": 3, "videos": [v_c], "images": []},
        ],
    }

    result = build_scene_timeline(scene_media, _timings(3))
    items = result["items"]
    assert [item["file_path"] for item in items] == [
        v_a["file_path"],
        v_b["file_path"],
        v_c["file_path"],
    ]
    assert all(item["assignment"] == "sentence" for item in items)


def test_sentence_without_assignment_falls_back_explicitly(tmp_path):
    v_a = _visual(tmp_path, "alpha.mp4")
    v_b = _visual(tmp_path, "bravo.mp4")

    scene_media = {
        "scene_number": 1,
        "duration_seconds": 15,
        "videos": [v_a, v_b],
        "images": [],
        "sentences": [
            {"sentence_number": 1, "videos": [v_a], "images": []},
            {"sentence_number": 2, "videos": [], "images": []},
            {"sentence_number": 3, "videos": [v_b], "images": []},
        ],
    }

    result = build_scene_timeline(scene_media, _timings(3))
    items = result["items"]
    assert items[0]["file_path"] == v_a["file_path"]
    assert items[0]["assignment"] == "sentence"
    assert items[2]["file_path"] == v_b["file_path"]
    assert items[2]["assignment"] == "sentence"
    # The middle sentence has no verified visual of its own: rotation is
    # allowed but must be honestly marked as such.
    assert items[1]["assignment"] == "rotation"


def test_assignments_work_when_scene_aggregates_are_empty(tmp_path):
    v_a = _visual(tmp_path, "alpha.mp4")

    scene_media = {
        "scene_number": 1,
        "duration_seconds": 5,
        "videos": [],
        "images": [],
        "sentences": [
            {"sentence_number": 1, "videos": [v_a], "images": []},
        ],
    }

    result = build_scene_timeline(scene_media, _timings(1))
    assert len(result["items"]) == 1
    assert result["items"][0]["file_path"] == v_a["file_path"]
    assert result["items"][0]["assignment"] == "sentence"


def test_image_assignment_is_used_when_no_video(tmp_path):
    img = _visual(tmp_path, "still.jpg")
    img["type"] = "image"

    scene_media = {
        "scene_number": 1,
        "duration_seconds": 5,
        "videos": [],
        "images": [img],
        "sentences": [
            {"sentence_number": 1, "videos": [], "images": [img]},
        ],
    }

    result = build_scene_timeline(scene_media, _timings(1))
    assert result["items"][0]["file_path"] == img["file_path"]
    assert result["items"][0]["assignment"] == "sentence"
