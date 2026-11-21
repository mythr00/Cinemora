"""Stage 6 — R5 diagnostics completion + R6 calibration corpus.

R5: the rejection/selection funnel becomes fully explainable per
candidate — collect-all contributing reasons at the gate and in the
verifier (decisions unchanged), full selection-stage events for
every verified-but-not-picked candidate (ASSET-016), the closed
reason vocabulary registry + guard, the retired score-below alias,
report counters derived from the records (C.A.3.4), and the
ASSET-017 scalar definitions.

R6: genre packs carry uncalibrated expectations data that
evaluates to UNKNOWN; the calibration corpus is built from stored
artefacts with EMPTY human-label slots (labels are never
fabricated); the calibration report computes agreement/error
rates only when real labels exist and otherwise states the gaps.
"""

import json
import re
import uuid
from pathlib import Path

import pytest

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from database import SessionLocal, engine
from models import AssetDecision, Base, ProductionRun, Project
from services import asset_report, calibration, media_pipeline as mp
from services import rejection_diagnostics, run_ledger
from services import story_understanding as story
from services import visual_quality
from services.genre_packs import load_pack
from services.visual_registry import VisualRegistry


# ---------------------------------------------------------------------------
# R5 — closed reason vocabulary (C.A.4)
# ---------------------------------------------------------------------------

# The vocabulary exactly as the Architecture Review's C.A.4 lists it
# (plus this module's dedupe reasons and the Stage 6 selection
# remainder reason).
REVIEW_VOCABULARY = {
    # gate
    "banned_provider", "foreign_provenance", "contradictory_date",
    "contradictory_person", "contradictory_place", "forbidden_term",
    "missing_person", "missing_subject", "missing_place",
    # scoring
    "contradictory_event", "missing_required_subject",
    "no_topical_evidence", "below_threshold",
    # selection
    "source_usage_limit", "already_used_file", "already_used_clip",
    "segment_overlap", "already_used_source", "recent_source",
    "already_used_title", "registry_block",
    # segment / download / technical / transcript
    "no_url", "no_segment_evidence", "download_failed",
    "invalid_media", "probe_failed", "technical_below_minimum",
    "transcript_mismatch",
    # image-download path codes (in the code at HEAD; C.A.4's list
    # missed them — the registry consolidates from code as found)
    "rate_limited", "http_error", "undersized_file", "decode_failed",
    # dedupe (Stage 1 diagnostics extension)
    "duplicate_candidate", "missing_candidate_key",
    # Stage 6 / ASSET-016 selection remainder
    "pick_limit_reached",
}


def test_reason_registry_covers_review_vocabulary():
    registry = rejection_diagnostics.REJECTION_REASON_REGISTRY
    assert REVIEW_VOCABULARY <= set(registry)
    for reason in REVIEW_VOCABULARY:
        assert rejection_diagnostics.is_registered_reason(reason)
    # The retired parallel-counter alias is NOT vocabulary.
    assert "score_below_threshold" not in registry
    assert not rejection_diagnostics.is_registered_reason(
        "score_below_threshold"
    )
    # Dynamic download-exception family is registered by prefix.
    assert rejection_diagnostics.is_registered_reason(
        "download_exception: HTTPError 404"
    )
    assert not rejection_diagnostics.is_registered_reason("")
    assert not rejection_diagnostics.is_registered_reason("made_up_reason")


def test_stamp_site_literals_are_registered():
    """Guard (C.A.4b): reason literals at the live stamp sites must
    belong to the closed registry."""
    backend = Path(__file__).resolve().parents[1]
    patterns = [
        r'return False, "([a-z_]+)"',
        r'"reject_reason"\] = "([a-z_]+)"',
        r'rejection_reason = "([a-z_]+)"',
        r'failed_checks\.append\("([a-z_]+)"\)',
        r'_fail\("([a-z_]+)"\)',
    ]
    found = set()
    for module in ("media_pipeline", "story_understanding", "visual_registry"):
        text = (backend / "services" / f"{module}.py").read_text(
            encoding="utf-8"
        )
        for pattern in patterns:
            found.update(re.findall(pattern, text))
    assert found  # the scan itself must find the stamp sites
    unregistered = {
        reason for reason in found
        if not rejection_diagnostics.is_registered_reason(reason)
    }
    assert unregistered == set()


_FROZEN_MARKERS = ("_before_", "_backup", "_corrupted", "_recovered")


def test_score_below_alias_absent_from_live_services():
    services_dir = Path(__file__).resolve().parents[1] / "services"
    offenders = []
    for path in services_dir.rglob("*.py"):
        if any(marker in path.name for marker in _FROZEN_MARKERS):
            continue  # frozen snapshots: Stage 16 disposition
        if "score_below_threshold" in path.read_text(
            encoding="utf-8", errors="ignore"
        ):
            offenders.append(path.name)
    assert offenders == []


# ---------------------------------------------------------------------------
# R5 — gate collect-all (C.A.3.2): decision unchanged, failures all recorded
# ---------------------------------------------------------------------------

_PERSON_NEED = {
    "required_subject": "Bryan Kohberger",
    "required_type": "person",
    "place": "Moscow, Idaho",
    "specificity": "representative",
}


def test_gate_collect_all_records_every_failure_in_order():
    candidate = {
        "title": "Cooking show highlights",
        "snippet": "recipes and kitchen fun",
    }
    # Stage 7 supersession (Review C.B.2): a representative need is
    # CONTEXTUAL, and contextual needs no longer carry named-entity
    # absence requirements — the absence-order mechanics pinned here
    # belong to the EVIDENCE contract, so the need declares its
    # category explicitly (the Stage 7 category seam).
    need = {**_PERSON_NEED, "visual_category": "evidence"}
    failures = mp.gate_all_failures(candidate, need)
    # Person contract is evaluated before the place contract.
    assert failures == ["missing_person", "missing_place"]
    # The decision is the first failure — identical to first-fail.
    assert mp.gate_shot_need(candidate, need) == (
        False,
        "missing_person",
    )


def test_gate_collect_all_forbidden_and_date_precede_absence():
    candidate = {
        "title": "Cooking show 2020 special",
        "snippet": "recipes",
    }
    # Evidence category declared explicitly (see the Stage 7 note in
    # the test above): the absence checks pinned here are the
    # evidence contract's.
    need = {
        **_PERSON_NEED, "visual_category": "evidence",
        "forbidden": ["cooking"], "era": "1961",
    }
    failures = mp.gate_all_failures(candidate, need)
    assert failures[0] == "contradictory_date"
    assert "forbidden_term" in failures
    assert "missing_person" in failures
    assert mp.gate_shot_need(candidate, need) == (False, failures[0])


def test_gate_collect_all_passing_candidate_has_no_failures():
    candidate = {
        "title": "Bryan Kohberger in Moscow Idaho court",
        "snippet": "bryan kohberger moscow idaho hearing",
    }
    assert mp.gate_all_failures(candidate, _PERSON_NEED) == []
    assert mp.gate_shot_need(candidate, _PERSON_NEED) == (True, "")


# ---------------------------------------------------------------------------
# R5 — verifier failed_checks (collect-all at scoring)
# ---------------------------------------------------------------------------

def _berlin_bible():
    return story.hydrate_bible({
        "entities": [
            {"id": "s1", "canonical": "Berlin Wall", "aliases": [],
             "type": "place"},
            {"id": "p1", "canonical": "John Smith", "aliases": [],
             "type": "person"},
            {"id": "p2", "canonical": "Jane Doe", "aliases": [],
             "type": "person"},
        ]
    })


def test_verify_candidate_failed_checks_collect_all():
    scenario = {
        "subject_id": "s1",
        "specificity": "exact",
        "search_terms": ["berlin", "wall"],
        "visual": "berlin wall crowds",
    }
    verdict = story.verify_candidate(
        {"title": "Cooking show", "snippet": "recipes and fun"},
        scenario,
        _berlin_bible(),
    )
    assert verdict["accepted"] is False
    assert verdict["failed_checks"] == [
        "missing_required_subject",
        "no_topical_evidence",
        "below_threshold",
    ]
    # The primary is still the first failed check — decision unchanged.
    assert verdict["rejection_reason"] == verdict["failed_checks"][0]


def test_verify_candidate_contradiction_above_threshold_not_below():
    # The candidate names a DIFFERENT bible person (contradiction)
    # but is otherwise strong: topical 0.45 + era 0.10, minus the
    # 0.30 wrong-person penalty = 0.25, above the generic 0.22 bar —
    # so below_threshold did NOT independently fail.
    scenario = {
        "subject_id": "p1",
        "specificity": "generic",
        "search_terms": ["court", "trial"],
        "visual": "court trial",
        "era": "2024",
    }
    verdict = story.verify_candidate(
        {
            "title": "Jane Doe court trial 2024",
            "snippet": "jane doe court trial 2024",
        },
        scenario,
        _berlin_bible(),
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "contradictory_person"
    assert verdict["failed_checks"] == ["contradictory_person"]


def test_verify_candidate_accepted_has_no_failed_checks():
    scenario = {
        "subject_id": "s1",
        "specificity": "representative",
        "search_terms": ["berlin", "wall"],
        "visual": "berlin wall crowds",
    }
    verdict = story.verify_candidate(
        {"title": "Berlin Wall crowds", "snippet": "berlin wall crowds"},
        scenario,
        _berlin_bible(),
    )
    assert verdict["accepted"] is True
    assert verdict["failed_checks"] == []


# ---------------------------------------------------------------------------
# R5 — event schema completion
# ---------------------------------------------------------------------------

def test_build_event_dimension_results_and_contributing():
    gate_event = rejection_diagnostics.build_event(
        candidate_id="c1", outcome="rejected", stage="metadata_gate",
        reason="missing_place", score=0.0,
        contributing_reasons=["missing_subject"],
    )
    assert gate_event["dimension_results"] == {
        "subject": "not_evaluated",
        "topical": "not_evaluated",
        "era": "not_evaluated",
    }
    assert gate_event["contributing_reasons"] == ["missing_subject"]
    assert gate_event["score_basis"] == "gate_pre_scoring"

    scored_event = rejection_diagnostics.build_event(
        candidate_id="c2", outcome="rejected", stage="scoring",
        reason="below_threshold", score=0.2,
        contributions={"subject": 0.45, "topical": 0.0, "era": 0.0,
                       "wrong_person": 0.0},
    )
    assert scored_event["dimension_results"] == {
        "subject": "pass",
        "topical": "fail",
        "era": "not_evaluated",  # no era in the scenario
    }
    era_event = rejection_diagnostics.build_event(
        candidate_id="c3", outcome="rejected", stage="scoring",
        reason="below_threshold", score=0.2,
        contributions={"subject": 0.0, "topical": 0.2, "era": 0.0,
                       "wrong_person": 0.0},
        era_applicable=True,
    )
    assert era_event["dimension_results"]["era"] == "fail"


def test_aggregate_funnel_counts_machine_contributing_first():
    records = [
        rejection_diagnostics.build_event(
            candidate_id="c1", outcome="rejected", stage="metadata_gate",
            reason="missing_place",
            contributing_reasons=["missing_subject"],
            verdict_reasons=["prose evidence"],
        ),
        # Legacy record: no machine-readable contributing list —
        # its verdict_reasons evidence is counted as-is.
        rejection_diagnostics.build_event(
            candidate_id="c2", outcome="rejected", stage="scoring",
            reason="below_threshold", score=0.1,
            verdict_reasons=["prose evidence"],
        ),
    ]
    funnel = rejection_diagnostics.aggregate_funnel(records)
    assert funnel["contributing_reasons"] == {
        "missing_subject": 1,
        "prose evidence": 1,
    }
    assert funnel["primary_rejections"]["total"] == 2


# ---------------------------------------------------------------------------
# R5 — full passes: ASSET-016 selection reasons + record-derived counters
# ---------------------------------------------------------------------------

def _run_pass(monkeypatch, tmp_path, *, videos=(), web=(), images=(),
              registry=None, downloader=None):
    monkeypatch.setattr(
        mp, "_uve_scenario_search",
        lambda scenario, cache, pid, need: (
            list(videos), list(web), list(images)
        ),
    )
    if downloader is not None:
        monkeypatch.setattr(mp, "_uve_download_any_candidate", downloader)
    scene_stats = {
        "candidates_considered": 0, "candidates_rejected": 0,
        "downloads_failed": 0, "providers": {}, "kinds": {},
    }
    scenario = {
        "sentence_number": 1, "specificity": "representative",
        "subject": "Berlin Wall", "search_terms": ["berlin", "wall"],
        "visual": "berlin wall crowds",
    }
    asset = mp._uve_search_and_pick(
        scenario, None,
        search_cache={}, project_id="t", bible={},
        registry=registry or VisualRegistry(), used_keys=set(),
        scene_dir=tmp_path, scene_stats=scene_stats,
        beat={"sentence_numbers": [1], "text": "The Berlin Wall fell."},
        beat_videos=[], beat_images=[], scenario_index=0,
        queries=["berlin wall"], review_records=[],
    )
    return asset, scene_stats


def _verified_video(index, host="videos.example.org"):
    return {
        "url": f"https://{host}/berlin-{index}",
        "title": f"Berlin Wall crowds {index}",
        "snippet": "berlin wall crowds",
        "provider": "youtube",
    }


def test_pick_limit_remainder_gets_full_selection_events(
    monkeypatch, tmp_path
):
    videos = [_verified_video(i) for i in range(5)]
    asset, scene_stats = _run_pass(
        monkeypatch, tmp_path, videos=videos,
        downloader=lambda candidate, scene_dir, n, idx: None,
    )
    assert asset is None
    records = scene_stats["candidate_records"]
    verified = [r for r in records if r["outcome"] == "verified"]
    assert len(verified) == 5
    selection = [
        r for r in records
        if r["outcome"] == "rejected" and r["stage"] == "selection"
    ]
    assert len(selection) == 2
    for record in selection:
        assert record["reason"] == "pick_limit_reached"
        assert record["primary_rejection_reason"] == "pick_limit_reached"
        # Full events, not the old minimal dicts.
        assert record["score"] is not None
        assert record["specificity"] == "representative"
        assert record["category"] == "contextual"
        assert record["sentence_numbers"] == [1]
        assert record["selection_rank"] in (3, 4)
        assert rejection_diagnostics.is_registered_reason(record["reason"])
    downloads = [r for r in records if r["outcome"] == "download_failed"]
    assert len(downloads) == 3
    # The scene counter is derived from the records (C.A.3.4).
    assert scene_stats["rejection_reasons"] == {"pick_limit_reached": 2}

    report = asset_report.build_asset_report(
        [{"scene_number": 1, "asset_stats": scene_stats,
          "videos": [], "images": []}],
        None,
    )
    assert report["rejection_funnel"]["primary_rejections"]["total"] == 2
    assert report["rejection_reasons"] == {"pick_limit_reached": 2}
    # ASSET-017: shot-level vs attempt-level download counts differ
    # and both are defined.
    assert report["failed_downloads"] == 1
    assert report["download_attempt_failures"] == 3
    assert report["candidates_evaluated"] == 5
    assert report["total_retrieved_assets"] == 5
    assert "failed_downloads" in report["scalar_definitions"]


def test_source_cap_block_is_a_full_selection_event(monkeypatch, tmp_path):
    registry = VisualRegistry()
    for _ in range(3):
        registry.register(url="https://videos.example.org/capped")
    asset, scene_stats = _run_pass(
        monkeypatch, tmp_path,
        videos=[{
            "url": "https://videos.example.org/capped",
            "title": "Berlin Wall crowds capped",
            "snippet": "berlin wall crowds",
            "provider": "youtube",
        }],
        registry=registry,
        downloader=lambda candidate, scene_dir, n, idx: None,
    )
    assert asset is None
    selection = [
        r for r in scene_stats["candidate_records"]
        if r["outcome"] == "rejected" and r["stage"] == "selection"
    ]
    assert len(selection) == 1
    record = selection[0]
    assert record["reason"] == "source_usage_limit"
    assert record["score"] is not None
    assert record["contributions"] is not None
    assert scene_stats["downloads_failed"] == 0


def test_fetch_failure_record_carries_segment_cause(monkeypatch, tmp_path):
    def downloader(candidate, scene_dir, n, idx):
        candidate["_download_error"] = "no_segment_evidence"
        candidate["_segment_failure_cause"] = "duration_unknown"
        candidate["rejection_stage"] = "segment_evidence"
        return None

    asset, scene_stats = _run_pass(
        monkeypatch, tmp_path,
        videos=[_verified_video(0, host="seg.example.org")],
        downloader=downloader,
    )
    assert asset is None
    failures = [
        r for r in scene_stats["candidate_records"]
        if r["outcome"] == "download_failed"
    ]
    assert len(failures) == 1
    record = failures[0]
    assert record["stage"] == "segment_evidence"
    assert record["reason"] == "no_segment_evidence"
    assert record["segment_failure_cause"] == "duration_unknown"
    # Full event via the pool join: the verdict rides along.
    assert record["score"] is not None
    assert record["contributions"] is not None


def test_gate_rejection_event_carries_contributing_failures(
    monkeypatch, tmp_path
):
    # A shot need whose person + place are both absent from the
    # candidate: the gate's primary is missing_person and the
    # contributing list must carry missing_place (C.A.3.2's
    # headline causal question). Stage 7 supersession (C.B.2): the
    # absence contract is the EVIDENCE profile's, so the need
    # declares visual_category "evidence" explicitly — a plain
    # representative (contextual) need no longer rejects at the gate
    # for absent names.
    monkeypatch.setattr(
        mp, "_uve_scenario_search",
        lambda scenario, cache, pid, need: (
            [{
                "url": "https://videos.example.org/cooking",
                "title": "Cooking show highlights",
                "snippet": "recipes and kitchen fun",
                "provider": "youtube",
            }],
            [],
            [],
        ),
    )
    scene_stats = {
        "candidates_considered": 0, "candidates_rejected": 0,
        "downloads_failed": 0, "providers": {}, "kinds": {},
    }
    scenario = {
        "sentence_number": 1, "specificity": "representative",
        "search_terms": ["kohberger"], "visual": "court hearing",
    }
    mp._uve_search_and_pick(
        scenario, {**_PERSON_NEED, "visual_category": "evidence"},
        search_cache={}, project_id="t", bible={},
        registry=VisualRegistry(), used_keys=set(),
        scene_dir=tmp_path, scene_stats=scene_stats,
        beat={"sentence_numbers": [1], "text": "The hearing began."},
        beat_videos=[], beat_images=[], scenario_index=0,
        queries=["kohberger"], review_records=[],
    )
    gate_records = [
        r for r in scene_stats["candidate_records"]
        if r["stage"] == "metadata_gate"
    ]
    assert len(gate_records) == 1
    record = gate_records[0]
    assert record["primary_rejection_reason"] == "missing_person"
    assert record["contributing_reasons"] == ["missing_place"]
    assert record["score_basis"] == "gate_pre_scoring"
    assert record["dimension_results"]["subject"] == "not_evaluated"


# ---------------------------------------------------------------------------
# R5 — report counter derivation + ASSET-017 (asset_report)
# ---------------------------------------------------------------------------

def test_report_rejection_reasons_derived_from_records():
    scene = {
        "scene_number": 1,
        "asset_stats": {
            "candidates_considered": 2, "candidates_rejected": 1,
            "downloads_failed": 0,
            # Legacy parallel counter, deliberately divergent.
            "rejection_reasons": {"missing_subject": 51},
            "candidate_records": [
                {"candidate_id": "cand_a", "outcome": "rejected",
                 "stage": "metadata_gate", "reason": "missing_place",
                 "primary_rejection_reason": "missing_place",
                 "provider": "web", "kind": "web"},
            ],
        },
        "videos": [], "images": [],
    }
    report = asset_report.build_asset_report([scene], None)
    # Records win over the legacy counter whenever records exist.
    assert report["rejection_reasons"] == {"missing_place": 1}
    assert report["download_attempt_failures"] == 0


def test_report_legacy_counter_fallback_without_records():
    scene = {
        "scene_number": 1,
        "asset_stats": {
            "candidates_considered": 57, "candidates_rejected": 57,
            "downloads_failed": 0,
            "rejection_reasons": {"missing_subject": 51},
        },
        "videos": [], "images": [],
    }
    report = asset_report.build_asset_report([scene], None)
    assert report["rejection_reasons"] == {"missing_subject": 51}
    # No records -> attempt-level count is unknown, not zero.
    assert report["download_attempt_failures"] is None
    definitions = report["scalar_definitions"]
    for key in (
        "total_retrieved_assets", "failed_downloads",
        "download_attempt_failures", "source_reuse.distinct_sources",
        "source_reuse.distinct_source_urls",
    ):
        assert key in definitions


def test_report_source_reuse_url_level_count():
    scene = {
        "scene_number": 1,
        "asset_stats": {"candidates_considered": 3},
        "videos": [
            {"file_path": "/m/a.mp4", "type": "video",
             "source": "youtube", "url": "https://youtu.be/same",
             "verification_status": "verified"},
            {"file_path": "/m/b.mp4", "type": "video",
             "source": "youtube", "url": "https://youtu.be/same",
             "verification_status": "verified"},
        ],
        "images": [
            {"file_path": "/m/c.jpg", "type": "image",
             "source": "wikimedia", "url": "https://commons.org/c",
             "verification_status": "verified"},
        ],
    }
    report = asset_report.build_asset_report([scene], None)
    reuse = report["source_reuse"]
    # Two distinct source LABELS (youtube, wikimedia) ...
    assert reuse["distinct_sources"] == 2
    # ... and two distinct source URLs (the shared youtu.be URL + commons).
    assert reuse["distinct_source_urls"] == 2
    assert reuse["max_uses_of_single_source_url"] == 2


# ---------------------------------------------------------------------------
# R5 — ledger carries the full event (C.A.5)
# ---------------------------------------------------------------------------

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
def ledger_project():
    pid = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(Project(
            id=pid, script="Stage 6 ledger script.",
            category="Documentary", status="created",
        ))
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


def test_ledger_rejected_row_carries_full_event(ledger_project):
    run_id = run_ledger.begin_run(ledger_project, "Stage 6 ledger script.")
    assert run_id
    report = {
        "candidate_records": {
            "records": [{
                "candidate_id": "cand_full", "outcome": "rejected",
                "stage": "scoring", "reason": "below_threshold",
                "primary_rejection_reason": "below_threshold",
                "url": "https://example.org/x", "provider": "youtube",
                "kind": "video", "scene": 2, "score": 0.2,
                "score_basis": "verifier_score",
                "contributions": {"subject": 0.45, "topical": 0.0,
                                  "era": 0.0, "wrong_person": 0.0},
                "contributing_reasons": ["no_topical_evidence"],
                "specificity": "representative",
                "matched_query": "berlin wall",
                "sentence_numbers": [7],
                "category": "contextual",
            }],
        },
        "missing_visuals": [],
    }
    written = run_ledger.record_decisions(
        run_id, ledger_project, report, {}, processed_scenes=[]
    )
    assert written == 1
    db = SessionLocal()
    try:
        rows = (
            db.query(AssetDecision)
            .filter(AssetDecision.run_id == str(run_id))
            .all()
        )
    finally:
        db.close()
    assert len(rows) == 1
    row = rows[0]
    assert row.decision_type == "rejected"
    assert row.sentence_number == 7
    verification = row.verification
    assert verification["primary_rejection_reason"] == "below_threshold"
    assert verification["score"] == 0.2
    assert verification["contributions"]["subject"] == 0.45
    assert verification["contributing_reasons"] == ["no_topical_evidence"]
    assert verification["specificity"] == "representative"
    assert verification["matched_query"] == "berlin wall"
    assert verification["category"] == "contextual"


# ---------------------------------------------------------------------------
# R6 — genre expectations (C.G.2)
# ---------------------------------------------------------------------------

def test_all_packs_carry_uncalibrated_expectations():
    for pack_id in ("general", "history", "true_crime"):
        pack = load_pack(pack_id)
        expectations = calibration.pack_expectations(pack)
        assert set(expectations) == set(calibration.EXPECTATION_FIELDS)
        for field in calibration.EXPECTATION_FIELDS:
            assert expectations[field] == {
                "range": None, "basis": "uncalibrated",
            }
        evaluated = calibration.evaluate_expectations(
            pack,
            {field: 0.5 for field in calibration.EXPECTATION_FIELDS},
        )
        for field in calibration.EXPECTATION_FIELDS:
            assert evaluated[field]["evaluation"] == "UNKNOWN"
            assert evaluated[field]["basis"] == "uncalibrated"


def test_evaluate_expectations_with_calibrated_range():
    pack = {
        "id": "history",
        "expectations": {
            "moving_share": {"range": [0.3, 0.6], "basis": "PROVISIONAL"},
        },
    }
    evaluated = calibration.evaluate_expectations(
        pack, {"moving_share": 0.5}
    )
    assert evaluated["moving_share"]["evaluation"] == "WITHIN_RANGE"
    assert evaluated["moving_share"]["basis"] == "PROVISIONAL"
    evaluated = calibration.evaluate_expectations(
        pack, {"moving_share": 0.9}
    )
    assert evaluated["moving_share"]["evaluation"] == "OUTSIDE_RANGE"
    # Missing measurement stays UNKNOWN even with a range.
    evaluated = calibration.evaluate_expectations(pack, {})
    assert evaluated["moving_share"]["evaluation"] == "UNKNOWN"


def test_visual_quality_genre_expectations_block():
    timeline = {
        "project_id": "t",
        "sentence_count": 1,
        "items": [{
            "file_path": "/m/v1.mp4", "type": "video", "duration": 60.0,
            "assignment": "sentence", "sentence_number": 1,
            "source": "youtube",
        }],
    }
    report = {
        "genre_pack": {"id": "history", "source": "test"},
        "coverage": {"total_sentences": 1},
    }
    result = visual_quality.measure_visual_quality(
        timeline=timeline, asset_report=report, rights_manifest=[]
    )
    block = result["genre_expectations"]
    assert block["state"] == "recorded"
    assert block["genre_pack"] == "history"
    moving = block["fields"]["moving_share"]
    assert moving["measured"] == 1.0
    assert moving["evaluation"] == "UNKNOWN"
    assert moving["basis"] == "uncalibrated"
    # No per-asset formats in this report -> document share unmeasured.
    assert block["fields"]["document_share"]["measured"] is None

    result = visual_quality.measure_visual_quality(
        timeline=timeline, asset_report={}, rights_manifest=[]
    )
    assert result["genre_expectations"]["state"] == "not_recorded"


# ---------------------------------------------------------------------------
# R6 — corpus from stored artefacts + C.A.6 recompute
# ---------------------------------------------------------------------------

def _stored_production(tmp_path):
    project = tmp_path / "prod"
    project.mkdir()
    report = {
        "candidate_records": {
            "records": [{
                "candidate_id": "cand_x", "outcome": "rejected",
                "stage": "metadata_gate", "reason": "missing_place",
                "provider": "youtube", "url": "https://youtu.be/x",
                "scene": 1,
            }],
        },
        "rejection_reasons": {"missing_subject": 99},
        "providers": {"funnel": {"youtube": {"discovered": 5}}},
        "total_retrieved_assets": 4,
    }
    (project / "asset_report.json").write_text(json.dumps(report))
    review = [{
        "beat_id": "b1",
        "shot_needs": [{
            "shot_index": 1, "shot_type": "place",
            "specificity": "representative",
            "required_subject": "Berlin", "required_type": "place",
            "place": "Berlin", "era": "1961",
        }],
        "records": [{
            "shot_index": 1, "queries": ["berlin 1961"],
            "rejected": [{
                "candidate_id": "cand_x", "title": "Some video",
                "reason": "missing_place", "stage": "metadata_gate",
            }],
            "picked": ["Berlin Wall photo"],
        }],
    }]
    (project / "scene_1_review.json").write_text(json.dumps(review))
    (project / "timeline.json").write_text(json.dumps({}))
    (project / "rights_manifest.json").write_text(json.dumps([]))
    return project, report, {1: review}


def test_recompute_stored_funnel_publishes_discrepancy(tmp_path):
    _, report, reviews = _stored_production(tmp_path)
    recomputed = rejection_diagnostics.recompute_stored_funnel(
        report, reviews
    )
    funnel = recomputed["rejection_funnel"]
    assert funnel["primary_rejections"]["by_reason"] == {"missing_place": 1}
    assert recomputed["per_shot"]["joined_rejected_candidates"] == 1
    assert recomputed["per_shot"]["by_shot_type_reason"] == {
        "place:missing_place": 1
    }
    assert recomputed["per_shot"]["by_specificity_reason"] == {
        "representative:missing_place": 1
    }
    discrepancies = recomputed["counter_discrepancies"]
    # Published side by side, never resolved by fiat.
    assert discrepancies["stored_rejection_reasons_total"] == 99
    assert discrepancies["records_derived_total"] == 1
    assert discrepancies["stored_total_retrieved_assets"] == 4
    assert discrepancies["candidate_records_total"] == 1
    assert discrepancies["provider_funnel_discovered_total"] == 5
    assert recomputed["not_recoverable"]


def test_corpus_build_and_sampling(tmp_path):
    project, _, _ = _stored_production(tmp_path)
    corpus = calibration.build_corpus([{
        "production_id": "p1", "genre": "history",
        "project_dir": str(project),
    }])
    assert corpus["labelling_state"] == "unlabelled"
    entries = corpus["entries"]
    assert len(entries) == 2
    rejected = [e for e in entries if e["pipeline_outcome"] == "rejected"]
    accepted = [e for e in entries if e["pipeline_outcome"] == "accepted"]
    assert len(rejected) == 1 and len(accepted) == 1
    entry = rejected[0]
    assert entry["shot_type"] == "place"
    assert entry["specificity"] == "representative"
    assert entry["provider"] == "youtube"  # joined from report records
    assert entry["primary_reason"] == "missing_place"
    assert entry["genre"] == "history"
    # Label slots exist and are EMPTY — never fabricated.
    for question in calibration.LABEL_QUESTIONS:
        assert entry["labels"][question] is None
    assert entry["labels"]["labeller"] is None
    assert accepted[0]["title"] == "Berlin Wall photo"

    sampled = calibration.stratified_sample(
        json.loads(json.dumps(corpus)), per_stratum_cap=25, seed=0
    )
    assert sampled["sampling"]["sampled_entries"] == 2
    # Reproducible: a fresh build + same seed flags the same entries.
    corpus2 = calibration.build_corpus([{
        "production_id": "p1", "genre": "history",
        "project_dir": str(project),
    }])
    sampled2 = calibration.stratified_sample(
        corpus2, per_stratum_cap=25, seed=0
    )
    assert {
        e["entry_id"] for e in sampled["entries"] if e["in_labelling_sample"]
    } == {
        e["entry_id"] for e in sampled2["entries"] if e["in_labelling_sample"]
    }


def test_calibration_report_unlabelled_states_gaps(tmp_path):
    project, _, _ = _stored_production(tmp_path)
    corpus = calibration.build_corpus([{
        "production_id": "p1", "genre": "history",
        "project_dir": str(project),
    }])
    report = calibration.calibration_report(
        corpus,
        measurements_by_genre={"history": {"moving_share": 0.44}},
    )
    assert report["labelling_state"] == "unlabelled"
    assert report["calibration_state"] == "uncalibrated"
    assert report["labelled_entries"] == 0
    for question in calibration.LABEL_QUESTIONS:
        agreement = report["inter_rater_agreement"][question]
        assert agreement["state"] == "not_measurable"
    assert (
        report["expectations_by_genre"]["history"]["moving_share"][
            "evaluation"
        ]
        == "UNKNOWN"
    )
    gaps = " ".join(report["gaps"])
    assert "human labels" in gaps
    assert "third genre" in gaps


def test_calibration_report_with_two_labellers():
    def entry(entry_id, outcome):
        return {
            "entry_id": entry_id, "genre": "history",
            "pipeline_outcome": outcome, "in_labelling_sample": True,
        }

    corpus = {
        "corpus_version": 1,
        "entries": [
            entry("e1", "accepted"), entry("e2", "rejected"),
            entry("e3", "accepted"), entry("e4", "rejected"),
        ],
    }
    q = "depicts_named_entity"

    def lab(labeller, value):
        return {"labeller": labeller, q: value}

    labels = {
        "e1": [lab("alice", True), lab("bob", True)],
        "e2": [lab("alice", False), lab("bob", False)],
        "e3": [lab("alice", True), lab("bob", False)],
        "e4": [lab("alice", True), lab("bob", True)],
    }
    report = calibration.calibration_report(corpus, labels=labels)
    assert report["labelling_state"] == "labelled"
    assert report["calibration_state"] == "calibrated"
    agreement = report["inter_rater_agreement"][q]
    assert agreement["state"] == "measured"
    # Pairs (T,T),(F,F),(T,F),(T,T): observed 0.75, expected 0.5.
    assert agreement["cohens_kappa"] == 0.5
    confusion = report["pipeline_vs_labels"][q]
    assert confusion["agree_admit"] == 2
    assert confusion["agree_reject"] == 1
    assert confusion["false_reject"] == 1
    assert "false_admit" not in confusion


# ---------------------------------------------------------------------------
# R6 — stored-production measurements (frozen C.G.1 definitions)
# ---------------------------------------------------------------------------

_BERLIN_DIR = (
    Path.home()
    / "workspace/cinemora-audit/e2e-media/media/e2e-five-minute-berlin"
)
_KOHBERGER_DIR = (
    Path.home()
    / "workspace/cinemora-audit/e2e-media/media/e2e-truecrime-kohberger"
)


@pytest.mark.skipif(
    not _BERLIN_DIR.exists(), reason="stored Berlin production absent"
)
def test_stored_berlin_measurements_match_frozen_definitions():
    measured = calibration.production_measurements(_BERLIN_DIR)
    # Primary asset-kind view from visual_quality (the review's
    # C.G.1 baselines under this view: moving/still ~0.44 each).
    assert 0.42 < measured["moving_share"] < 0.47
    assert 0.42 < measured["still_share"] < 0.47
    assert measured["uncovered_share"] == 0.0
    # The stored report predates accepted_asset_formats, so the
    # document share is honestly unmeasured — not zero, not guessed.
    assert measured["document_share"] is None


@pytest.mark.skipif(
    not _KOHBERGER_DIR.exists(), reason="stored Kohberger production absent"
)
def test_stored_kohberger_measurements_match_review_baseline():
    measured = calibration.production_measurements(_KOHBERGER_DIR)
    # Review C.G.1 measured baseline: moving 34.8% / still 65.2%.
    assert abs(measured["moving_share"] - 0.348) < 0.01
    assert abs(measured["still_share"] - 0.652) < 0.01
    assert measured["graphic_share"] == 0.0
