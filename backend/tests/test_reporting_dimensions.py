"""VA-07 / VA-09 / VA-10 regression tests.

- Failed runs still produce an asset report naming the stage + error.
- The report keeps technical validity, metadata relevance, segment
  evidence and rights as separate dimensions, with an explicit
  rights disclaimer (allow_all is not legal clearance).
- Timeline items state their per-sentence relevance status; rotation
  assignments are counted in the report.
"""

import json
import uuid
from pathlib import Path

import pytest

import jobs
from services.asset_report import build_asset_report
from services.timeline import build_project_timeline


def test_run_failure_recorded_in_report():
    report = build_asset_report(
        [], run_failure={"stage": "render", "error": "timeline mismatch"}
    )
    assert report["run_completed"] is False
    assert report["run_failure"]["stage"] == "render"
    ok = build_asset_report([])
    assert ok["run_completed"] is True
    assert ok["run_failure"] is None


def test_verification_dimensions_kept_separate():
    scenes = [{
        "scene_number": 1,
        "asset_stats": {"candidates_considered": 3, "candidates_rejected": 1},
        "videos": [{
            "file_path": "/tmp/v.mp4", "type": "video", "provider": "youtube",
            "license": "unknown", "segment_evidence": "positional_no_contract",
            "technical_checks": {"passed": True},
            "verification": {"accepted": True},
        }],
        "images": [],
    }]
    timeline_result = {
        "scenes": [{"items": [
            {"assignment": "sentence"}, {"assignment": "rotation"},
            {"assignment": "rotation"},
        ]}],
    }
    report = build_asset_report(scenes, timeline_result=timeline_result)
    dims = report["verification_dimensions"]
    assert dims["technical_validity"]["accepted_with_technical_checks"] == 1
    assert dims["metadata_relevance"]["verified_for_sentence"] == 1
    assert dims["metadata_relevance"]["assigned_by_rotation"] == 2
    assert dims["segment_evidence"]["positional_no_contract"] == 1
    assert dims["rights"]["unknown_licences"] == 1
    assert dims["rights"]["recorded_licences"] == 0
    assert "NOT" in report["rights_note"]
    assert "wikimedia_stills" in report["provider_rights_policies"]
    assert report["rotation_assignments"] == 2


def test_fail_pipeline_writes_failure_report(tmp_path):
    state = {
        "_partial": {
            "processed_scenes": [],
            "analysis": {"mode": "degraded"},
            "timeline_result": None,
        }
    }
    with pytest.raises(RuntimeError):
        jobs.fail_pipeline(state, "media_retrieval", RuntimeError("kaput"), tmp_path)
    report_file = tmp_path / "asset_report.json"
    assert report_file.exists()
    report = json.loads(report_file.read_text())
    assert report["run_completed"] is False
    assert report["run_failure"] == {"stage": "media_retrieval", "error": "kaput"}
    assert report["analysis_mode"] == "degraded"


def _visual(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 4096)
    return {"file_path": str(path), "path": str(path), "type": "video", "title": name}


def test_timeline_items_carry_relevance_status(tmp_path):
    project_id = f"rel-{uuid.uuid4()}"
    media_dir = tmp_path / "media" / project_id
    media_dir.mkdir(parents=True)
    segments = [
        {"start": 0.0, "end": 5.0, "text": "The wall divided the city."},
        {"start": 5.0, "end": 10.0, "text": "Guards watched the border."},
    ]
    (media_dir / "captions.json").write_text(json.dumps(
        {"segments": segments, "words": []}
    ))
    v = _visual(tmp_path, "only.mp4")
    scenes = [{
        "scene": {"scene_number": 1},
        "scene_number": 1,
        "duration_seconds": 10,
        "videos": [v],
        "images": [],
        "sentences": [
            {"sentence_number": 1, "sentence_text": segments[0]["text"],
             "videos": [v], "images": []},
            {"sentence_number": 2, "sentence_text": segments[1]["text"],
             "videos": [], "images": []},
        ],
    }]
    timeline = build_project_timeline(
        project_id, scenes, output_dir=str(tmp_path / "media")
    )
    items = timeline["scenes"][0]["items"]
    by_sentence = {i["sentence_index"]: i for i in items}
    assert by_sentence[0]["relevance_status"] == "verified_for_sentence"
    assert by_sentence[1]["assignment"] == "rotation"
    assert by_sentence[1]["relevance_status"] == "not_verified_for_this_sentence"
