"""Stage 2A — run ledger: begin/finish + decision recording parity.

The parity contract: decisions recorded from a built asset report +
timeline must reproduce the report's own counts — accepted assets by
type, rotation assignments, missing visuals, rejected candidates and
the rights split (recorded vs unknown) over the accepted real-asset
population. Aggregates and ledger rows are two views of the same
run; they must not be able to disagree.
"""

import hashlib
import uuid

import pytest

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from database import SessionLocal, engine
from models import AssetDecision, Base, ProductionRun, Project
from services import run_ledger
from services.asset_report import build_asset_report


@pytest.fixture(autouse=True)
def _ledger_tables():
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Project.__table__,
            ProductionRun.__table__,
            AssetDecision.__table__,
        ],
    )
    yield


@pytest.fixture()
def project_id():
    pid = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(
            Project(
                id=pid,
                script="Ledger test script.",
                category="Documentary",
                status="created",
            )
        )
        db.commit()
    finally:
        db.close()
    yield pid
    db = SessionLocal()
    try:
        db.query(AssetDecision).filter(
            AssetDecision.project_id == pid
        ).delete()
        db.query(ProductionRun).filter(
            ProductionRun.project_id == pid
        ).delete()
        db.query(Project).filter(Project.id == pid).delete()
        db.commit()
    finally:
        db.close()


def test_begin_and_finish_run(project_id):
    run_id = run_ledger.begin_run(
        project_id, "Some script. Two sentences.", options={"k": "v"}
    )
    assert run_id
    run = run_ledger.get_run(run_id)
    assert run["status"] == "running"
    assert run["started_at"] is not None
    # The ledger hash is the FULL sha256 (jobs.script_hash_for is a
    # truncated display hash and must not leak into the ledger).
    assert run["script_hash"] == hashlib.sha256(
        b"Some script. Two sentences."
    ).hexdigest()

    execution = {"wall_time_seconds": 12.5, "failures": 0}
    assert run_ledger.finish_run(
        run_id,
        "completed",
        execution=execution,
        manifest_path="/tmp/run_manifest.json",
    )
    run = run_ledger.get_run(run_id)
    assert run["status"] == "completed"
    assert run["finished_at"] is not None
    assert run["execution"] == execution
    assert run["manifest_path"] == "/tmp/run_manifest.json"


def test_ledger_is_fail_soft_when_database_breaks(monkeypatch, project_id):
    def exploding_session():
        raise RuntimeError("database is down")

    monkeypatch.setattr(run_ledger, "_session", exploding_session)
    assert run_ledger.begin_run(project_id, "Script.") is None
    assert run_ledger.finish_run("any", "failed") is False
    assert run_ledger.record_decisions("any", project_id, {}, None) is None
    assert run_ledger.get_run("any") is None
    assert run_ledger.is_cancel_requested("any") is False


def _fixture_run(tmp_path):
    """A mutually consistent (processed_scenes, timeline) pair, run
    through the REAL build_asset_report."""
    video_file = tmp_path / "clip1.mp4"
    video_file.write_bytes(b"video-one-bytes")
    image_file = tmp_path / "still1.jpg"
    image_file.write_bytes(b"image-one-bytes")
    graphic_file = tmp_path / "graphic1.mp4"
    graphic_file.write_bytes(b"graphic-bytes")
    swapped_file = tmp_path / "swapped.mp4"
    swapped_file.write_bytes(b"swapped-bytes")
    original_file = tmp_path / "original.mp4"
    original_file.write_bytes(b"original-bytes")

    video_asset = {
        "candidate_id": "cand-vid-1",
        "file_path": str(video_file),
        "type": "video",
        "provider": "archive_org",
        "source": "archive_org",
        "source_url": "https://archive.org/details/clip1",
        "video_url": "https://archive.org/download/clip1.mp4",
        "license": "CC0",
        "rights_status": "recorded",
        "creator": "Archive",
        "segment_evidence": "verified_segment",
        "clip_start": 12.0,
        "duration_seconds": 8.0,
        "verification": {"accepted": True, "score": 0.9},
        "verification_status": "verified",
    }
    image_asset = {
        "candidate_id": "cand-img-1",
        "file_path": str(image_file),
        "type": "image",
        "provider": "wikimedia_commons",
        "source": "wikimedia_commons",
        "source_url": "https://commons.wikimedia.org/wiki/File:Still.jpg",
        # No license key at all -> unknown, honestly.
        "verification": {"accepted": True, "score": 0.8},
        "verification_status": "verified",
    }
    graphic_asset = {
        "file_path": str(graphic_file),
        "type": "video",
        "source": "cinemora_motion_graphics",
        "motion_graphic": True,
        "verification_status": "generated",
    }
    swapped_asset = {
        "candidate_id": "cand-swap-new",
        "file_path": str(swapped_file),
        "type": "video",
        "provider": "youtube",
        "source": "youtube",
        "source_url": "https://youtube.com/watch?v=swap",
        "license": "CC BY",
        "segment_evidence": "none",
        "verification": {"accepted": False, "reason": "manual_swap_unverified"},
        "verification_status": "swapped",
        "swapped_from": {
            "file_path": str(original_file),
            "candidate_id": "cand-orig",
        },
    }

    sentences = [
        {"sentence_number": 1, "videos": [video_asset], "images": []},
        {"sentence_number": 2, "videos": [], "images": [image_asset]},
        {"sentence_number": 3, "videos": [graphic_asset], "images": []},
        {"sentence_number": 4, "videos": [swapped_asset], "images": []},
    ]
    processed_scenes = [
        {
            "scene_number": 1,
            "scene": {"scene_number": 1, "sentences": sentences},
            "sentences": sentences,
            "videos": [video_asset, graphic_asset, swapped_asset],
            "images": [image_asset],
            "asset_stats": {
                "candidates_considered": 7,
                "candidates_rejected": 2,
                "downloads_failed": 0,
                "candidate_records": [
                    {
                        "candidate_id": "cand-rej-1",
                        "url": "https://example.com/rejected1",
                        "provider": "youtube",
                        "outcome": "rejected",
                        "stage": "verification",
                        "reason": "no_topical_evidence",
                    },
                    {
                        "candidate_id": "cand-rej-2",
                        "url": "https://example.com/rejected2",
                        "provider": "serper_web",
                        "outcome": "rejected",
                        "stage": "selection",
                        "reason": "below_threshold",
                    },
                    {
                        "candidate_id": "cand-vid-1",
                        "url": "https://archive.org/details/clip1",
                        "provider": "archive_org",
                        "outcome": "accepted",
                        "stage": "accepted",
                        "reason": "",
                    },
                ],
                "missing_visuals": [
                    {"sentence_number": 5, "sentence_text": "Uncovered."}
                ],
            },
        }
    ]
    timeline = {
        "scenes": [
            {
                "scene_number": 1,
                "items": [
                    {
                        "assignment": "sentence",
                        "scene_number": 1,
                        "sentence_index": 0,
                        "file_path": str(video_file),
                        "start": 0.0,
                        "duration": 5.0,
                        "type": "video",
                        "source": "archive_org",
                        "source_url": "https://archive.org/details/clip1",
                        "segment_evidence": "verified_segment",
                    },
                    {
                        "assignment": "rotation",
                        "scene_number": 1,
                        "sentence_index": 1,
                        "file_path": str(video_file),
                        "start": 5.0,
                        "duration": 5.0,
                        "type": "video",
                        "source": "archive_org",
                        "source_url": "https://archive.org/details/clip1",
                        "segment_evidence": "verified_segment",
                    },
                    {
                        "assignment": "motion_graphic",
                        "scene_number": 1,
                        "sentence_index": 2,
                        "file_path": str(graphic_file),
                        "start": 10.0,
                        "duration": 5.0,
                        "type": "video",
                        "source": "cinemora_motion_graphics",
                    },
                ],
            }
        ],
        "coverage": {"complete": True, "total_sentences": 5},
    }
    report = build_asset_report(
        processed_scenes, timeline_result=timeline, analysis_mode="llm"
    )
    return processed_scenes, timeline, report


def test_record_decisions_parity_with_report(project_id, tmp_path):
    processed_scenes, timeline, report = _fixture_run(tmp_path)
    run_id = run_ledger.begin_run(project_id, "Parity script.")
    assert run_id

    written = run_ledger.record_decisions(
        run_id,
        project_id,
        report,
        timeline,
        processed_scenes=processed_scenes,
    )
    assert written is not None and written > 0

    counts = run_ledger.decision_counts(run_id)
    by_type = counts["by_type"]

    # Accepted-asset parity: every asset the report accepted has
    # exactly one acceptance-class decision row.
    accepted_rows = sum(
        by_type.get(kind, 0)
        for kind in ("selected", "still", "generated", "swapped_in")
    )
    assert accepted_rows == report["total_accepted_assets"] == 4
    assert by_type["selected"] == 1
    assert by_type["still"] == 1
    assert by_type["generated"] == 1
    assert by_type["swapped_in"] == 1
    assert by_type["swapped_out"] == 1

    # Assignment / coverage parity.
    assert by_type["rotated"] == report["rotation_assignments"] == 1
    assert by_type["missing"] == report["missing_visual_count"] == 1
    assert (
        by_type["rejected"]
        == report["candidate_records"]["by_outcome"]["rejected"]
        == 2
    )

    # Rights parity over the accepted real-asset population.
    assert counts["rights_recorded"] == report["rights"]["recorded_licences"]
    assert counts["rights_unknown"] == report["rights"]["unknown_licences"]

    # Idempotent: a second recording for the same run adds nothing.
    assert (
        run_ledger.record_decisions(
            run_id,
            project_id,
            report,
            timeline,
            processed_scenes=processed_scenes,
        )
        == 0
    )
    assert run_ledger.decision_counts(run_id)["total"] == counts["total"]


def test_swap_linkage_and_evidence(project_id, tmp_path):
    processed_scenes, timeline, report = _fixture_run(tmp_path)
    run_id = run_ledger.begin_run(project_id, "Swap script.")
    run_ledger.record_decisions(
        run_id, project_id, report, timeline,
        processed_scenes=processed_scenes,
    )

    db = SessionLocal()
    try:
        rows = (
            db.query(AssetDecision)
            .filter(AssetDecision.run_id == run_id)
            .all()
        )
    finally:
        db.close()
    by_type = {row.decision_type: row for row in rows}

    swapped_out = by_type["swapped_out"]
    swapped_in = by_type["swapped_in"]
    # The replacement points back at the replaced asset's decision.
    assert swapped_in.previous_decision_id == swapped_out.id
    # The swapped_out row carries the ORIGINAL's identity...
    assert swapped_out.asset_key == "cand-orig"
    assert swapped_out.content_hash == hashlib.sha256(
        b"original-bytes"
    ).hexdigest()
    # ...but NOT the replacement's verdict or rights (Stage 1: a swap
    # never inherits evidence in either direction).
    assert swapped_out.verification is None
    assert swapped_out.rights is None
    # The swapped_in row carries the replacement's OWN (unverified)
    # verdict, exactly as Stage 1 stamped it.
    assert swapped_in.verification == {
        "accepted": False,
        "reason": "manual_swap_unverified",
    }
    assert swapped_in.content_hash == hashlib.sha256(
        b"swapped-bytes"
    ).hexdigest()

    # Selected row: evidence + rights recorded verbatim.
    selected = by_type["selected"]
    assert selected.evidence_class == "verified_segment"
    assert selected.interval_start == 12.0
    assert selected.rights["license"] == "CC0"
    assert selected.sentence_number == 1

    # Rotated row resolves the global sentence number via the scene's
    # sentence order (scene-local index 1 -> sentence_number 2).
    assert by_type["rotated"].sentence_number == 2

    # Missing row: identity-less by design.
    missing = by_type["missing"]
    assert missing.sentence_number == 5
    assert missing.asset_key is None
    assert missing.content_hash is None
