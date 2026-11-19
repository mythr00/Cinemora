"""VA-08 regression tests: candidate traceability.

Every candidate gets a stable per-run id; every rejection point
records its stage; the per-shot review log is not capped; and the
asset report's candidate aggregates are computed from the individual
records, so the two can never disagree.
"""

from services import asset_report, media_pipeline as mp
from services.visual_registry import VisualRegistry


def test_candidate_id_is_stable_and_url_derived():
    a = {"url": "https://example.org/clip.mp4"}
    b = {"video_url": "https://example.org/clip.mp4"}
    assert mp._candidate_id_for(a) == mp._candidate_id_for(b)
    assert mp._candidate_id_for(a).startswith("cand_")
    assert mp._candidate_id_for(a) != mp._candidate_id_for(
        {"url": "https://example.org/other.mp4"}
    )


def test_verify_all_stamps_ids_on_rejected_and_verified():
    scenario = {
        "specificity": "representative",
        "subject": "Berlin Wall",
        "search_terms": ["berlin", "wall"],
        "visual": "berlin wall crowds",
    }
    candidates = [
        {"url": "https://a.org/1", "title": "Berlin Wall crowds 1989",
         "snippet": "berlin wall", "provider": "web"},
        {"url": "https://a.org/2", "title": "Wedding flowers sale",
         "snippet": "roses", "provider": "web"},
    ]
    scored = mp._uve_verify_all(candidates, scenario, {})
    assert len(scored) == 2
    for item in scored:
        assert item["candidate_id"].startswith("cand_")
    rejected = [i for i in scored if i.get("reject_reason")]
    assert len(rejected) == 1
    assert rejected[0]["rejection_stage"] in ("scoring", "metadata_gate")


def _run_one_pass(monkeypatch, tmp_path, images):
    monkeypatch.setattr(
        mp, "_uve_scenario_search",
        lambda scenario, cache, pid, need: ([], [], images),
    )
    scene_stats = {
        "candidates_considered": 0, "candidates_rejected": 0,
        "downloads_failed": 0, "providers": {}, "kinds": {},
    }
    review_records = []
    scenario = {
        "sentence_number": 1, "specificity": "representative",
        "subject": "Berlin Wall", "search_terms": ["berlin", "wall"],
        "visual": "berlin wall crowds",
    }
    asset = mp._uve_search_and_pick(
        scenario, None,
        search_cache={}, project_id="t", bible={},
        registry=VisualRegistry(), used_keys=set(),
        scene_dir=tmp_path, scene_stats=scene_stats,
        beat={"sentence_numbers": [1], "text": "The Berlin Wall fell."},
        beat_videos=[], beat_images=[], scenario_index=0,
        queries=["berlin wall"], review_records=review_records,
    )
    return asset, scene_stats, review_records


def test_review_log_not_capped_and_records_complete(monkeypatch, tmp_path):
    images = [
        {"url": f"https://spam.org/{i}", "image_url": f"https://spam.org/{i}",
         "title": f"Unrelated thing {i}", "snippet": "nothing relevant",
         "provider": "web", "kind": "image"}
        for i in range(15)
    ]
    asset, scene_stats, review_records = _run_one_pass(
        monkeypatch, tmp_path, images
    )
    assert asset is None
    rejected = review_records[0]["rejected"]
    assert len(rejected) == 15  # was capped at 12 before VA-08
    assert all(r["candidate_id"] for r in rejected)
    assert all(r["stage"] for r in rejected)
    records = scene_stats["candidate_records"]
    assert len(records) == 15
    assert all(r["outcome"] == "rejected" for r in records)


def test_report_candidate_aggregates_match_records():
    records = [
        {"candidate_id": "cand_a", "outcome": "rejected", "stage": "scoring",
         "reason": "no_topical_evidence", "url": "https://x/1", "provider": "web"},
        {"candidate_id": "cand_b", "outcome": "rejected", "stage": "metadata_gate",
         "reason": "missing_subject", "url": "https://x/2", "provider": "web"},
        {"candidate_id": "cand_c", "outcome": "verified", "stage": "verification",
         "reason": "", "url": "https://x/3", "provider": "youtube"},
        {"candidate_id": "cand_d", "outcome": "download_failed", "stage": "segment_evidence",
         "reason": "no_segment_evidence", "url": "https://x/4", "provider": "youtube"},
        {"candidate_id": "cand_e", "outcome": "accepted", "stage": "accepted",
         "reason": "", "url": "https://x/5", "provider": "archive.org"},
    ]
    production_result = [{
        "scene_number": 1,
        "asset_stats": {
            "candidates_considered": 5, "candidates_rejected": 2,
            "downloads_failed": 1, "candidate_records": records,
            "download_failures": [
                {"provider": "youtube", "url": "https://x/4",
                 "reason": "no_segment_evidence"},
            ],
        },
        "videos": [], "images": [],
    }]
    report = asset_report.build_asset_report(production_result, None)
    section = report["candidate_records"]
    assert section["total"] == 5
    assert section["by_outcome"] == {
        "rejected": 2, "verified": 1, "download_failed": 1, "accepted": 1,
    }
    assert section["by_stage"]["segment_evidence"] == 1
    assert len(section["records"]) == 5
    # Aggregates agree with the individual failure records.
    assert section["by_outcome"]["download_failed"] == len(
        report["download_failures"]
    )
