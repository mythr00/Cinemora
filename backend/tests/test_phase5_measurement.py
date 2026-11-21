"""Phase 5 (Master Directive) — film-level measurement + AM-017.

visual_quality's film_level block (B4): repeat share of runtime,
distinct visuals (canonical), still/moving runtime shares (a
zoomed still is NOT moving; a frozen/looped-identical video
window adds no unique moving seconds), consecutive-identical
pairs, longest single-visual stretch, source concentration —
all at the ONE identity defined in services.visual_identity.

AM-017: the same headline number must come out of visual_quality,
the asset report and the production report for the same film.
"""

import json

import pytest

from services import asset_report as ar
from services import production_report as pr
from services import visual_quality as vq

YT_A = "https://www.youtube.com/watch?v=abc123XYZ_-"


def _video_item(start, end, duration, *, clip_start, clip_end=None, source=YT_A):
    item = {
        "type": "video", "duration": duration,
        "file_path": "/p/v.mp4", "path": "/p/v.mp4",
        "source": "youtube", "source_url": source, "video_url": source,
        "assignment": "sentence", "scene_number": 1,
        "clip_start": clip_start, "start": start, "end": end,
    }
    if clip_end is not None:
        item["clip_end"] = clip_end
    return item


def _fixture_timeline():
    """Hand-computed film, 20s total.

    V (one YouTube segment, windows stamped as the Phase 5
    timeline records them): [0,4) 4s fresh, [4,8) 4s fresh
    (continuation), [0,4) 4s again — a restart, all repeat.
    S (one still) 3s + 3s — the second showing is repeat.
    G (one graphic) 2s.
    Repeat = 4 (V restart) + 3 (S) = 7s -> share 0.35.
    Unique moving = 8s. Distinct visuals = 3.
    """
    items = [
        _video_item(0, 4, 4.0, clip_start=0.0, clip_end=4.0),
        _video_item(4, 8, 4.0, clip_start=4.0, clip_end=8.0),
        _video_item(8, 12, 4.0, clip_start=0.0, clip_end=4.0),
        {
            "type": "image", "duration": 3.0,
            "file_path": "/p/s.jpg", "path": "/p/s.jpg",
            "source": "wikimedia",
            "source_url": "https://upload.wikimedia.org/x/S.jpg",
            "image_url": "https://upload.wikimedia.org/x/S.jpg",
            "assignment": "sentence", "scene_number": 1,
            "start": 12, "end": 15,
        },
        {
            "type": "image", "duration": 3.0,
            "file_path": "/p/s.jpg", "path": "/p/s.jpg",
            "source": "wikimedia",
            "source_url": "https://upload.wikimedia.org/x/S.jpg",
            "image_url": "https://upload.wikimedia.org/x/S.jpg",
            "assignment": "rotation", "scene_number": 1,
            "start": 15, "end": 18,
        },
        {
            "type": "video", "duration": 2.0,
            "file_path": "/p/motion_graphics/g.mp4",
            "path": "/p/motion_graphics/g.mp4",
            "source": "cinemora_motion_graphics",
            "assignment": "motion_graphic", "scene_number": 1,
            "start": 18, "end": 20,
        },
    ]
    return {"scenes": [{"scene_number": 1, "items": items}]}


def test_film_level_block_exact():
    result = vq.measure_visual_quality(
        timeline=_fixture_timeline(), asset_report={}, rights_manifest=[]
    )
    block = result["film_level"]
    assert block["distinct_visuals"] == 3
    assert block["repeat_runtime_seconds"] == 7.0
    assert block["repeat_share"] == pytest.approx(0.35)
    assert block["still_runtime_seconds"] == 6.0
    assert block["moving_runtime_seconds"] == 12.0
    assert block["graphics_runtime_seconds"] == 2.0
    assert block["unique_moving_seconds"] == 8.0
    # V,V,V then S,S: three adjacent identical boundaries, and the
    # longest single-visual stretch is V's 12s run.
    assert block["consecutive_identical_pairs"] == 3
    assert block["longest_single_visual_stretch_seconds"] == 12.0
    concentration = block["source_concentration"]
    assert concentration["top_source"] == "youtube:abc123XYZ_-"
    assert concentration["top_source_runtime_seconds"] == 12.0
    assert concentration["top_source_runtime_share"] == pytest.approx(0.6)
    # The headline and D1 are the same number by construction.
    assert result["timeline"]["unique_canonical_assets_on_timeline"] == 3
    assert result["diversity"]["d1_unique_assets"] == 3


def test_looped_window_adds_no_unique_moving_seconds():
    # One 10s item whose recorded window is only [0,4): the
    # renderer loops the 4s window — 6 of the 10 seconds are
    # repeats by construction, and unique moving stays 4.
    timeline = {"scenes": [{"scene_number": 1, "items": [
        _video_item(0, 10, 10.0, clip_start=0.0, clip_end=4.0),
    ]}]}
    block = vq.measure_visual_quality(
        timeline=timeline, asset_report={}, rights_manifest=[]
    )["film_level"]
    assert block["unique_moving_seconds"] == 4.0
    assert block["repeat_runtime_seconds"] == 6.0
    assert block["repeat_share"] == pytest.approx(0.6)


def test_zoomed_still_is_not_moving():
    timeline = {"scenes": [{"scene_number": 1, "items": [
        {
            "type": "image", "duration": 5.0, "motion": "zoom",
            "file_path": "/p/s.jpg", "path": "/p/s.jpg",
            "source": "wikimedia",
            "source_url": "https://upload.wikimedia.org/x/S.jpg",
            "assignment": "sentence", "scene_number": 1,
        },
    ]}]}
    block = vq.measure_visual_quality(
        timeline=timeline, asset_report={}, rights_manifest=[]
    )["film_level"]
    assert block["moving_runtime_seconds"] == 0.0
    assert block["unique_moving_seconds"] == 0.0
    assert block["still_runtime_seconds"] == 5.0


# ---------------------------------------------------------------------------
# Identical bytes at two paths count ONCE — in every distinct measure
# ---------------------------------------------------------------------------

def _dup_project(tmp_path):
    """A stored-style project: the same photo downloaded twice
    (different files, different source URLs, identical bytes)
    plus one genuinely different photo."""
    first = tmp_path / "one.jpg"
    first.write_bytes(b"the duplicated building photo")
    second = tmp_path / "two.jpg"
    second.write_bytes(b"the duplicated building photo")
    third = tmp_path / "three.jpg"
    third.write_bytes(b"a different photo entirely")

    def _item(path, url, start):
        return {
            "type": "image", "duration": 4.0,
            "file_path": str(path), "path": str(path),
            "source": "wikimedia", "source_url": url, "image_url": url,
            "assignment": "sentence", "scene_number": 1,
            "start": start, "end": start + 4.0,
        }

    timeline = {
        "project_id": "dup",
        "scenes": [{"scene_number": 1, "items": [
            _item(first, "https://a.example.org/building", 0.0),
            _item(second, "https://b.example.org/building-copy", 4.0),
            _item(third, "https://c.example.org/gates", 8.0),
        ]}],
    }
    (tmp_path / "timeline.json").write_text(json.dumps(timeline))
    (tmp_path / "rights_manifest.json").write_text("[]")
    return timeline


def test_identical_bytes_count_once_in_visual_quality(tmp_path):
    _dup_project(tmp_path)
    result = vq.measure_project(tmp_path)
    # 3 files, 2 visuals — everywhere distinctness is reported.
    assert result["timeline"]["unique_canonical_assets_on_timeline"] == 2
    assert result["diversity"]["d1_unique_assets"] == 2
    assert result["film_level"]["distinct_visuals"] == 2
    assert len(result["timeline"]["reuse_per_asset"]) == 2
    assert result["film_level"]["repeat_runtime_seconds"] == 4.0


def test_am017_headline_consistent_across_three_reports(tmp_path):
    timeline = _dup_project(tmp_path)
    report = ar.build_asset_report([], timeline_result=timeline)
    (tmp_path / "asset_report.json").write_text(json.dumps(report))

    measurement = vq.measure_project(tmp_path)
    production = pr.build_production_report_for_project(tmp_path)

    headline = measurement["timeline"]["unique_canonical_assets_on_timeline"]
    assert headline == 2
    assert report["distinct_visuals_on_timeline"] == headline
    assert production["questions"]["q08"]["answer"] == headline
    # The asset report's repeat count agrees: the duplicate's
    # second showing is a repeat, the different photo is not.
    assert report["repeated_footage"] == 1
    # The definition change is recorded where the number lives.
    assert "AM-017" in report["scalar_definitions"][
        "distinct_visuals_on_timeline"
    ]
