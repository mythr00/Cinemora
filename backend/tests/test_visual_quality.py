"""Unit tests for services.visual_quality (Phase 5, measurement only).

All fixtures are synthetic and exact: every expected count, runtime
and share below is computed by hand from the fixture, not derived
from the code under test.
"""

import json

from services.visual_quality import (
    canonical_asset_id,
    classify_asset_kind,
    measure_project,
    measure_visual_quality,
    timeline_items,
)


def _item(file_path, type_, duration, source="src", assignment="sentence"):
    return {
        "type": type_,
        "duration": duration,
        "file_path": file_path,
        "path": file_path,
        "source": source,
        "assignment": assignment,
        "scene_number": 1,
    }


def _fixture():
    """6 items, total runtime 60s.

    Assets: A video x3 (10+10+5=25s), B image x2 (10+15=25s),
    C motion-graphic file x1 (10s, assignment motion_graphic).
    Assignments: sentence x3, rotation x2, motion_graphic x1.
    Sources (timeline): youtube x3 (A), wikimedia x2 (B),
    cinemora_motion_graphics x1 (C).
    """
    timeline = {
        "project_id": "synthetic",
        "duration_seconds": 60.0,
        "sentence_count": 6,
        "coverage": {
            "total_sentences": 6,
            "covered_sentences": 6,
            "uncovered_sentences": [],
            "complete": True,
        },
        "scenes": [
            {
                "scene_number": 1,
                "items": [
                    _item("/p/a.mp4", "video", 10.0, source="youtube"),
                    _item("/p/b.jpg", "image", 10.0, source="wikimedia"),
                    _item("/p/a.mp4", "video", 10.0, source="youtube",
                          assignment="rotation"),
                    _item("/p/a.mp4", "video", 5.0, source="youtube",
                          assignment="rotation"),
                    _item("/p/b.jpg", "image", 15.0, source="wikimedia"),
                    _item("/p/motion_graphics/motion_graphic_x.mp4", "video",
                          10.0, source="cinemora_motion_graphics",
                          assignment="motion_graphic"),
                ],
            }
        ],
    }
    report = {
        "missing_visuals": [
            {"scene": 1, "sentence_number": 4, "reason": "no_verified_asset"},
            {"scene": 1, "sentence_number": 5, "reason": "no_verified_asset"},
        ],
        "missing_visual_count": 2,
        "rejection_reasons": {"missing_subject": 7, "missing_place": 3},
        "total_accepted_assets": 3,
        "total_rejected_assets": 10,
        "total_retrieved_assets": 20,
        "sentence_assignment_accuracy": 0.5,
        "rotation_assignments": 2,
        "generated_graphics": 1,
        "providers": {
            "funnel": {
                "youtube": {"discovered": 10, "verified": 4, "rejected": 6,
                            "downloaded": 1, "download_failed": 0},
                "wikimedia": {"discovered": 5, "verified": 2, "rejected": 3,
                              "downloaded": 1, "download_failed": 1},
            }
        },
        "candidate_records": {
            "total": 15,
            "by_outcome": {"verified": 6, "rejected": 9},
            "by_stage": {"metadata_gate": 9, "verification": 6},
            "records": [],
        },
    }
    manifest = [
        {"file_path": "/p/a.mp4", "type": "video", "source": "youtube"},
        {"file_path": "/p/b.jpg", "type": "image", "source": "wikimedia"},
        {"file_path": "/p/motion_graphics/motion_graphic_x.mp4",
         "type": "video", "source": "cinemora_motion_graphics"},
    ]
    mg = {
        "assets": [
            {"file_path": "/p/motion_graphics/motion_graphic_x.mp4"},
        ],
        "targets": {"requested": 2, "generated": 1, "skipped": []},
    }
    return timeline, report, manifest, mg


def _measure():
    timeline, report, manifest, mg = _fixture()
    return measure_visual_quality(
        timeline=timeline,
        asset_report=report,
        rights_manifest=manifest,
        motion_graphics=mg,
        run_manifest_present=False,
    )


def test_timeline_items_prefers_scenes_and_does_not_double_count():
    timeline, _, _, _ = _fixture()
    timeline["items"] = list(timeline["scenes"][0]["items"])  # same sequence
    assert len(timeline_items(timeline)) == 6


def test_unique_assets_and_reuse_exact():
    result = _measure()
    tl = result["timeline"]
    assert tl["timeline_item_count"] == 6
    assert tl["total_runtime_seconds"] == 60.0
    assert tl["unique_canonical_assets_on_timeline"] == 3
    assert tl["max_reuse_count"] == 3
    assert tl["max_runtime_share"] == 25.0 / 60.0
    assert tl["assets_used_once"] == 1
    assert tl["assets_reused"] == 2
    assert tl["reuse_distribution"] == {"1": 1, "2": 1, "3": 1}
    by_id = {e["canonical_asset_id"]: e for e in tl["reuse_per_asset"]}
    assert by_id["/p/a.mp4"] == {
        "canonical_asset_id": "/p/a.mp4",
        "count": 3,
        "runtime_seconds": 25.0,
        "runtime_share": 25.0 / 60.0,
    }
    assert by_id["/p/b.jpg"]["count"] == 2
    assert by_id["/p/b.jpg"]["runtime_seconds"] == 25.0
    assert by_id["/p/motion_graphics/motion_graphic_x.mp4"]["count"] == 1
    assert by_id["/p/motion_graphics/motion_graphic_x.mp4"]["runtime_seconds"] == 10.0
    # Fixture order is A, B, A, A, B, C: exactly one adjacent A-A
    # boundary, and the longest run of one asset is 2.
    assert tl["consecutive_repeat_boundaries"] == 1
    assert tl["max_consecutive_run"] == 2


def test_consecutive_repeats_exact():
    timeline = {
        "scenes": [{"scene_number": 1, "items": [
            _item("/p/a.mp4", "video", 5.0),
            _item("/p/a.mp4", "video", 5.0),
            _item("/p/a.mp4", "video", 5.0),
            _item("/p/b.jpg", "image", 5.0),
        ]}],
    }
    result = measure_visual_quality(
        timeline=timeline, asset_report={}, rights_manifest=[]
    )
    assert result["timeline"]["consecutive_repeat_boundaries"] == 2
    assert result["timeline"]["max_consecutive_run"] == 3
    assert result["timeline"]["unique_canonical_assets_on_timeline"] == 2


def test_runtime_shares_primary_asset_kind_exact():
    result = _measure()
    shares = result["runtime_share"]
    # moving = A (video, non-graphics) 25s; still = B 25s; graphics = C 10s.
    assert shares["moving"] == {"seconds": 25.0, "fraction": 25.0 / 60.0}
    assert shares["still"] == {"seconds": 25.0, "fraction": 25.0 / 60.0}
    assert shares["graphics"] == {"seconds": 10.0, "fraction": 10.0 / 60.0}
    assert shares["unknown"] == {"seconds": 0.0, "fraction": 0.0}
    total_fraction = sum(v["fraction"] for v in shares.values())
    assert abs(total_fraction - 1.0) < 1e-9


def test_runtime_shares_assignment_method_and_rotation_graphic_distinction():
    # A graphics FILE reused by rotation: primary counts it as
    # graphics, the investigation's assignment method counts it moving.
    timeline = {
        "scenes": [{"scene_number": 1, "items": [
            _item("/p/motion_graphics/g.mp4", "video", 10.0,
                  source="cinemora_motion_graphics", assignment="rotation"),
            _item("/p/v.mp4", "video", 10.0, source="youtube"),
        ]}],
    }
    result = measure_visual_quality(
        timeline=timeline, asset_report={}, rights_manifest=[]
    )
    assert result["runtime_share"]["graphics"]["seconds"] == 10.0
    assert result["runtime_share"]["moving"]["seconds"] == 10.0
    assert result["runtime_share_assignment_method"]["graphics"]["seconds"] == 0.0
    assert result["runtime_share_assignment_method"]["moving"]["seconds"] == 20.0


def test_assignment_breakdown_exact():
    result = _measure()
    breakdown = result["assignment_breakdown"]
    assert breakdown["sentence"]["count"] == 3
    assert breakdown["sentence"]["runtime_seconds"] == 35.0
    assert breakdown["rotation"]["count"] == 2
    assert breakdown["rotation"]["runtime_seconds"] == 15.0
    assert breakdown["rotation"]["runtime_share"] == 15.0 / 60.0
    assert breakdown["motion_graphic"]["count"] == 1
    assert breakdown["motion_graphic"]["runtime_seconds"] == 10.0


def test_uncovered_beats_exact_and_coverage_reported_separately():
    result = _measure()
    uncovered = result["uncovered_beats"]
    assert uncovered["count"] == 2
    assert uncovered["source_field"] == "asset_report.missing_visuals"
    assert uncovered["missing_visual_count_field"] == 2
    assert uncovered["missing_visuals_list_length"] == 2
    assert uncovered["sentence_numbers"] == [4, 5]
    # Timeline coverage says 0 uncovered (rotation-filled) — reported apart.
    assert uncovered["timeline_coverage_uncovered_sentences"] == 0


def test_uncovered_beats_unknown_when_no_source():
    result = measure_visual_quality(timeline={}, asset_report={}, rights_manifest=[])
    assert result["uncovered_beats"]["count"] is None
    assert result["uncovered_beats"]["source_field"] is None


def test_rejection_reasons_from_report_exact():
    result = _measure()
    reasons = result["rejection_reasons"]
    assert reasons["source"] == "asset_report.rejection_reasons"
    assert reasons["distribution"] == {"missing_subject": 7, "missing_place": 3}
    assert reasons["total"] == 10
    assert reasons["unknown"] is False


def test_rejection_reasons_fallback_to_candidate_records_exact():
    report = {
        "candidate_records": {
            "records": [
                {"outcome": "rejected", "reason": "missing_subject"},
                {"outcome": "rejected", "reason": "missing_subject"},
                {"outcome": "rejected", "reason": "below_threshold"},
                {"outcome": "verified", "reason": ""},
                {"outcome": "rejected", "reason": ""},
            ]
        }
    }
    result = measure_visual_quality(timeline={}, asset_report=report, rights_manifest=[])
    reasons = result["rejection_reasons"]
    assert reasons["distribution"] == {"missing_subject": 2, "below_threshold": 1}
    assert reasons["total"] == 3
    assert reasons["unknown"] is False


def test_rejection_reasons_unknown_never_invented():
    result = measure_visual_quality(timeline={}, asset_report={}, rights_manifest=[])
    reasons = result["rejection_reasons"]
    assert reasons["distribution"] is None
    assert reasons["total"] is None
    assert reasons["unknown"] is True
    assert reasons["source"] is None


def test_source_distribution_exact():
    result = _measure()
    dist = result["source_distribution"]
    timeline = dist["timeline"]
    assert timeline["youtube"] == {
        "count": 3, "runtime_seconds": 25.0,
        "runtime_share": 25.0 / 60.0, "item_share": 3 / 6,
    }
    assert timeline["wikimedia"]["count"] == 2
    assert timeline["wikimedia"]["runtime_seconds"] == 25.0
    assert timeline["cinemora_motion_graphics"]["count"] == 1
    accepted = dist["accepted_manifest"]
    assert accepted["youtube"] == {"count": 1, "share_of_accepted": 1 / 3}
    assert accepted["wikimedia"]["count"] == 1
    assert accepted["cinemora_motion_graphics"]["count"] == 1
    assert dist["accepted_type_distribution"] == {"image": 1, "video": 2}
    funnel = dist["provider_funnel"]
    assert funnel["provider_count"] == 2
    assert funnel["totals"] == {
        "discovered": 15, "download_failed": 1, "downloaded": 2,
        "rejected": 9, "verified": 6,
    }
    assert funnel["per_provider"]["youtube"]["discovered"] == 10
    assert dist["candidate_records"]["total"] == 15
    assert dist["candidate_records"]["by_outcome"] == {"verified": 6, "rejected": 9}


def test_targeted_graphics_exact():
    result = _measure()
    targeted = result["targeted_graphics"]
    assert targeted["targets_requested"] == 2
    assert targeted["generated_assets"] == 1
    assert targeted["motion_graphic_assignment_items"] == 1
    assert targeted["targeted_graphics_on_timeline"] == 1
    assert targeted["report_generated_graphics"] == 1


def test_canonical_identity_fallbacks_and_limitation_metadata():
    assert canonical_asset_id({"file_path": "/x.mp4", "path": "/y.mp4"}, "fb") == "/x.mp4"
    assert canonical_asset_id({"path": "/y.mp4"}, "fb") == "/y.mp4"
    assert canonical_asset_id({"video_url": "https://v"}, "fb") == "https://v"
    assert canonical_asset_id({}, "fb") == "fb"
    assert classify_asset_kind({"type": "video", "source": "s"}, "/p/motion_graphic_a.mp4") == "graphics"
    result = _measure()
    assert "no stable asset id" in result["metadata"]["canonical_identity_limitation"]
    assert result["metadata"]["artefacts"]["run_manifest_present"] is False


def test_missing_identifier_items_counted_distinctly():
    timeline = {"scenes": [{"scene_number": 1, "items": [
        {"type": "image", "duration": 5.0},
        {"type": "image", "duration": 5.0},
    ]}]}
    result = measure_visual_quality(timeline=timeline, asset_report={}, rights_manifest=[])
    assert result["timeline"]["missing_identifier_items"] == 2
    assert result["timeline"]["unique_canonical_assets_on_timeline"] == 2


def test_empty_timeline_is_zero_not_error():
    result = measure_visual_quality(timeline={}, asset_report={}, rights_manifest=[])
    assert result["timeline"]["timeline_item_count"] == 0
    assert result["timeline"]["unique_canonical_assets_on_timeline"] == 0
    assert result["timeline"]["max_reuse_count"] == 0
    assert result["timeline"]["max_runtime_share"] == 0.0
    assert result["runtime_share"]["moving"]["fraction"] == 0.0


def test_measure_project_reads_stored_artefacts(tmp_path):
    timeline, report, manifest, mg = _fixture()
    (tmp_path / "timeline.json").write_text(json.dumps(timeline))
    (tmp_path / "asset_report.json").write_text(json.dumps(report))
    (tmp_path / "rights_manifest.json").write_text(json.dumps(manifest))
    mg_dir = tmp_path / "motion_graphics"
    mg_dir.mkdir()
    (mg_dir / "motion_graphics.json").write_text(json.dumps(mg))
    result = measure_project(tmp_path)
    assert result["project_id"] == "synthetic"
    assert result["timeline"]["unique_canonical_assets_on_timeline"] == 3
    assert result["metadata"]["artefacts"]["run_manifest_present"] is False
