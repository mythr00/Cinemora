"""Quality Recovery Phase 1 — timeline-invariant tests.

Historical filename kept per the programme brief; these tests describe
timeline eligibility, bounded rotation and within-beat assignment.
Required-test numbers and canonical DM IDs are in each docstring.

Rotation after Phase 1 is a bounded, labelled fallback: per-file use
cap aligned with the selection cap (3), consecutive-reuse avoidance,
a runtime-share preference, and a fair least-used draw. When the
eligible pool is exhausted the sentence stays UNCOVERED — no relaxed
pool, no borrowing, no filler.
"""

import inspect
from collections import Counter

import degraded_fixtures as fx

from services import media_pipeline as mp
from services.timeline import build_scene_timeline
from services.visual_registry import VisualRegistry


def _visual(tmp_path, name, **over):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 2048)
    visual = {
        "file_path": str(path), "path": str(path), "type": "video",
        "title": name, "verification": {"accepted": True},
    }
    visual.update(over)
    return visual


def _timings(n, step=5.0):
    return [
        {"sentence_index": i, "text": f"Sentence {i}.",
         "start": float(i * step), "end": float(i * step + 4)}
        for i in range(n)
    ]


# ---------------------------------------------------------------- test 7

def test_07_failed_verification_cannot_reach_timeline_any_path(tmp_path):
    """Required test 7 (DM-02/MA-36/MA-37): verifier-rejected,
    verdict-less and download-failed assets are excluded from direct
    assignment AND from the rotation pool; only the genuinely
    verified asset appears."""
    verified = _visual(tmp_path, "verified.mp4")
    rejected = _visual(tmp_path, "rejected.mp4", verification={"accepted": False})
    verdictless = _visual(tmp_path, "verdictless.mp4")
    verdictless.pop("verification")
    failed = _visual(tmp_path, "failed.mp4", _download_error="http_error")

    scene_media = {
        "scene_number": 1, "duration_seconds": 20,
        "videos": [rejected, failed, verdictless, verified],
        "images": [],
        "sentences": [
            {"sentence_number": 1, "videos": [rejected], "images": []},
            {"sentence_number": 2, "videos": [verdictless], "images": []},
            {"sentence_number": 3, "videos": [], "images": []},
            {"sentence_number": 4, "videos": [], "images": []},
        ],
    }
    result = build_scene_timeline(scene_media, _timings(4))
    used = {item["file_path"] for item in result["items"]}
    assert used == {verified["file_path"]}
    for item in result["items"]:
        assert item["assignment"] in ("sentence", "rotation")
        if item["assignment"] == "rotation":
            assert item["relevance_status"] == "not_verified_for_this_sentence"


def test_07_relaxed_pool_never_returns_in_pick():
    """Regression R-A: a verifier-REJECTED candidate with a high score
    is never picked (the old >= 0.15 relaxed pool stays removed)."""
    from services.visual_registry import VisualRegistry

    scored = [{
        "title": "Rejected but high scoring",
        "url": "https://www.youtube.com/watch?v=REJECTED01",
        "verification": {"accepted": False, "score": 0.95},
        "universal_score": 0.95, "relevance_score": 0.95,
    }]
    picked = mp._uve_pick(scored, fx.scenario(), VisualRegistry("ra"), set())
    assert picked == []


# ------------------------------------------------------------- caps (B)

def test_rotation_never_exceeds_per_file_cap_kohberger_profile(tmp_path):
    """Workstream B acceptance: the Kohberger profile (3 unique
    assets, 23 sentences) rebuilt through the new timeline rules —
    no file exceeds the cap of 3 on-screen uses, and the remaining
    sentences are visibly uncovered instead of looped."""
    visuals = [_visual(tmp_path, f"asset{i}.mp4") for i in range(3)]
    scene_media = {
        "scene_number": 1, "duration_seconds": 115,
        "videos": visuals, "images": [],
        "sentences": [
            {"sentence_number": i + 1, "videos": [], "images": []}
            for i in range(23)
        ],
    }
    result = build_scene_timeline(scene_media, _timings(23))
    counts = Counter(item["file_path"] for item in result["items"])
    assert counts, "some rotation is expected before the cap binds"
    assert max(counts.values()) <= 3
    assert len(result["items"]) <= 9
    assert len(result["uncovered_sentence_indexes"]) == 23 - len(result["items"])
    assert all(
        item["assignment"] == "rotation" for item in result["items"]
    )


def test_rotation_fair_draw_uses_stranded_assets_first(tmp_path):
    """VV-07/VV-08: with three eligible files and three rotation
    sentences, every file is used once before any repeat — no fixed
    A-B-C modulo loop, no stranded assets."""
    visuals = [_visual(tmp_path, f"fair{i}.mp4") for i in range(3)]
    scene_media = {
        "scene_number": 1, "duration_seconds": 15,
        "videos": visuals, "images": [],
        "sentences": [
            {"sentence_number": i + 1, "videos": [], "images": []}
            for i in range(3)
        ],
    }
    result = build_scene_timeline(scene_media, _timings(3))
    used = [item["file_path"] for item in result["items"]]
    assert len(used) == 3
    assert len(set(used)) == 3


def test_rotation_avoids_consecutive_reuse_when_alternative_exists(tmp_path):
    """Workstream B: the same file never repeats back-to-back while
    another eligible file is available."""
    visuals = [_visual(tmp_path, f"alt{i}.mp4") for i in range(2)]
    scene_media = {
        "scene_number": 1, "duration_seconds": 20,
        "videos": visuals, "images": [],
        "sentences": [
            {"sentence_number": i + 1, "videos": [], "images": []}
            for i in range(4)
        ],
    }
    result = build_scene_timeline(scene_media, _timings(4))
    used = [item["file_path"] for item in result["items"]]
    assert len(used) == 4
    for first, second in zip(used, used[1:]):
        assert first != second


def test_uncovered_stays_uncovered_when_pool_exhausted(tmp_path):
    """Regression R-E: one eligible file, five sentences needing
    rotation — at most the cap's worth of items is built and the
    rest are recorded uncovered (no filler, no cap violation)."""
    visual = _visual(tmp_path, "only.mp4")
    scene_media = {
        "scene_number": 1, "duration_seconds": 25,
        "videos": [visual], "images": [],
        "sentences": [
            {"sentence_number": i + 1, "videos": [], "images": []}
            for i in range(5)
        ],
    }
    result = build_scene_timeline(scene_media, _timings(5))
    assert len(result["items"]) <= 3
    assert len(result["uncovered_sentence_indexes"]) == 5 - len(result["items"])


def test_no_visuals_produces_no_items_and_no_placeholder(tmp_path):
    """Regression R-C (timeline leg): a scene with no eligible media
    builds zero items — no text card or placeholder is fabricated."""
    scene_media = {
        "scene_number": 1, "duration_seconds": 10,
        "videos": [], "images": [],
        "sentences": [{"sentence_number": 1, "videos": [], "images": []}],
    }
    result = build_scene_timeline(scene_media, _timings(1))
    assert result["items"] == []


# ---------------------------------------------------------------- DM-17

def _beat_with_two_sentences():
    sentence_a = {
        "sentence_number": 1, "text": "Alpha sentence.",
        "role": "event",
        "scenarios": [{
            "visual": "alpha visual", "kind": "event",
            "specificity": "representative", "subject_id": "alpha",
            "queries": ["alpha query"], "search_terms": ["alpha"],
        }],
    }
    sentence_b = {
        "sentence_number": 2, "text": "Bravo sentence.",
        "role": "event",
        "scenarios": [{
            "visual": "bravo visual", "kind": "event",
            "specificity": "representative", "subject_id": "bravo",
            "queries": ["bravo query"], "search_terms": ["bravo"],
        }],
    }
    return {
        "beat_id": "beat_01", "searchable": True, "role": "event",
        "sentences": [sentence_a, sentence_b],
        "sentence_numbers": [1, 2], "text": "Alpha sentence. Bravo sentence.",
    }


def test_dm17_asset_dealt_only_to_owning_sentence():
    """Canonical DM-17: an asset stamped scenario_index=2 (the second
    sentence's scenario) is dealt to sentence offset 1 ONLY — never
    to offset 0 as its direct assignment (the old positional dealing
    did exactly that)."""
    beat = _beat_with_two_sentences()
    asset_b = {"file_path": "/x/bravo.mp4", "scenario_index": 2, "type": "video"}
    per_sentence = mp._distribute_beat_assets(beat, [asset_b], [])
    assert per_sentence[0][0] == []
    assert [a["file_path"] for a in per_sentence[1][0]] == ["/x/bravo.mp4"]

    asset_a = {"file_path": "/x/alpha.mp4", "scenario_index": 1, "type": "video"}
    per_sentence = mp._distribute_beat_assets(beat, [asset_a], [])
    assert [a["file_path"] for a in per_sentence[0][0]] == ["/x/alpha.mp4"]
    assert per_sentence[1][0] == []


def test_dm17_unstamped_asset_is_not_dealt_as_direct_when_linkage_exists():
    """DM-17: with linkage present, an asset whose scenario owner is
    unknown is not labelled direct for any sentence (it remains
    available to the timeline only as labelled rotation)."""
    beat = _beat_with_two_sentences()
    orphan = {"file_path": "/x/orphan.mp4", "type": "video"}
    per_sentence = mp._distribute_beat_assets(beat, [orphan], [])
    assert per_sentence[0][0] == [] and per_sentence[1][0] == []


# ---------------------------------------------------------------- DM-18

def test_dm18_evidence_subsystem_not_wired_into_selection():
    """Canonical DM-18 disposition pin: the quarantined evidence
    subsystem exists in place but the selection path
    (_uve_search_and_pick / _uve_pick) never calls it — wiring it in
    silently would be an unapproved behaviour change."""
    for fn in (mp._uve_search_and_pick, mp._uve_pick):
        src = inspect.getsource(fn)
        assert "rank_visuals_by_evidence" not in src
        assert "calculate_visual_evidence_score" not in src
    assert callable(mp.rank_visuals_by_evidence)  # still present, uncalled

# ---------------------------------------------------------------- test 9

def test_09_contextual_fallback_never_labelled_direct(tmp_path):
    """Required test 9 (contextual labelling; DM-08/DM-10 family):
    beat A finds its verified asset; beat B (scarce, same scene)
    finds nothing. B's sentence is covered by A's asset ONLY as a
    labelled rotation with relevance_status
    not_verified_for_this_sentence — never as a direct/sentence
    assignment — and the asset report counts B as rotation while
    still recording B's missing verified visual."""
    from services.asset_report import build_asset_report

    asset_a = _visual(
        tmp_path, "beat-a.mp4",
        verification_status="verified", license="unknown",
        source="youtube",
    )
    scene_media = {
        "scene_number": 1, "duration_seconds": 10,
        "videos": [asset_a], "images": [],
        "sentences": [
            {"sentence_number": 1, "videos": [asset_a], "images": []},
            {"sentence_number": 2, "videos": [], "images": []},
        ],
    }
    result = build_scene_timeline(scene_media, _timings(2))
    assert len(result["items"]) == 2
    direct, contextual = result["items"]
    assert direct["sentence_index"] == 0
    assert direct["assignment"] == "sentence"
    assert direct["relevance_status"] == "verified_for_sentence"
    assert contextual["sentence_index"] == 1
    assert contextual["file_path"] == asset_a["file_path"]
    assert contextual["assignment"] == "rotation"
    assert contextual["relevance_status"] == "not_verified_for_this_sentence"

    processed = {
        "scene_number": 1, "videos": [asset_a], "images": [],
        "sentences": scene_media["sentences"],
        "asset_stats": {
            "candidates_considered": 3, "candidates_rejected": 2,
            "downloads_failed": 0,
            "missing_visuals": [
                {"sentence_number": 2, "reason": "no_verified_asset",
                 "specificity": "exact"},
            ],
        },
    }
    report = build_asset_report(
        [processed], timeline_result=result,
        renderer_audit={"placeholder_count": 0},
    )
    assert report["assignment_breakdown"] == {"sentence": 1, "rotation": 1}
    assert report["rotation_assignments"] == 1
    assert report["verification_dimensions"]["metadata_relevance"][
        "verified_for_sentence"] == 1
    assert report["verification_dimensions"]["metadata_relevance"][
        "assigned_by_rotation"] == 1
    # B's gap is still on record — rotation did not erase it.
    assert report["missing_visual_count"] == 1


def test_09_sentence_outcomes_count_contextual_never_direct(tmp_path):
    """Required test 9, Phase 3 contract leg (services/visual_quality
    sentence_outcomes): the same two-sentence scene measures as
    direct=1, contextual=1, uncovered=0 — B is contextual, NEVER
    direct — and sentence 2 still counts as a sentence without a
    verified visual (rotation is not verification for it). Depends
    on the concurrent Phase 3 sentence_outcomes landing."""
    from services.visual_quality import measure_visual_quality

    asset_a = _visual(tmp_path, "beat-a-vq.mp4")
    scene_media = {
        "scene_number": 1, "duration_seconds": 10,
        "videos": [asset_a], "images": [],
        "sentences": [
            {"sentence_number": 1, "videos": [asset_a], "images": []},
            {"sentence_number": 2, "videos": [], "images": []},
        ],
    }
    result = build_scene_timeline(scene_media, _timings(2))
    timeline = {"scenes": [{"scene_number": 1, "items": result["items"]}]}
    report = {"missing_visuals": [
        {"scene": 1, "sentence_number": 2, "reason": "no_verified_asset"},
    ]}
    measured = measure_visual_quality(
        timeline=timeline, asset_report=report, rights_manifest=[],
    )
    outcomes = measured["sentence_outcomes"]
    assert outcomes["sentence_count"] == 2
    assert outcomes["buckets"]["direct"] == 1
    assert outcomes["buckets"]["contextual"] == 1
    assert outcomes["buckets"]["approved_graphic"] == 0
    assert outcomes["buckets"]["editorial_holdover"] == 0
    assert outcomes["buckets"]["uncovered"] == 0
    assert outcomes["reconciles"] is True
    per_sentence = {
        entry["sentence_number"]: entry["outcome"]
        for entry in outcomes["per_sentence"]
    }
    assert per_sentence == {1: "direct", 2: "contextual"}
    assert outcomes["sentences_without_verified_visual"] == 1


# --------------------------------------------------------------- test 17

def test_17_project_b_can_select_url_project_a_used_or_failed(tmp_path):
    """Required test 17, positive leg (project boundaries; AUD-18):
    project A used a segment of a URL and failed another download;
    after reset_media_caches() — exactly as jobs.run_full_pipeline
    calls it between productions — project B's registry is fresh for
    that URL, B's verification/pick selects the candidate on its own
    merits, and B's fetch actually calls the downloader (the URL is
    not skipped as previously-failed-this-run)."""
    import uuid

    from services.canonical_media import canonical_source_key
    from services.visual_registry import get_registry, reset_registry

    url = "https://www.youtube.com/watch?v=SHARED0017"
    # Project A's per-run state, in the long-lived worker process.
    # Phase 3: planned segments live on the registry — seed through
    # the pipeline's planning helper, as the selectors do.
    mp._note_planned_segment(url, 0, 8)
    mp._yt_info_failed.add(url)
    mp._yt_transcript_failed.add(url)
    mp._FAILED_DOWNLOAD_URLS.add(canonical_source_key(url))
    registry_a = get_registry(f"proj-a-{uuid.uuid4()}")
    registry_a.register(url=url, clip_key="youtube:SHARED0017|0",
                        start=0, end=8)
    assert registry_a.source_usage_count(url) == 1

    # The production boundary, as jobs applies it.
    mp.reset_media_caches()
    project_b = f"proj-b-{uuid.uuid4()}"
    reset_registry(project_b)
    registry_b = get_registry(project_b)
    try:
        assert mp._segment_ranges_in_use(url) == []
        assert mp._FAILED_DOWNLOAD_URLS == set()
        assert registry_b.source_usage_count(url) == 0
        allowed, _ = registry_b.can_use(
            url=url, start=0, end=8, allow_source_reuse=True,
        )
        assert allowed

        candidate = fx.candidate(url=url, video_url=url)
        scored = mp._uve_verify_all(
            [candidate], fx.scenario(), fx.bible(),
            shot_need=fx.shot_need(),
        )
        picked = mp._uve_pick(
            scored, fx.scenario(), registry_b, set(),
            shot_need=fx.shot_need(),
        )
        assert [p["url"] for p in picked] == [url]

        calls = []

        def ok_downloader(cand, scene_dir, n, i):
            calls.append(cand.get("url"))
            return {"file_path": "/x/shared.mp4", "url": url,
                    "type": "video"}

        failures = []
        asset = mp._uve_fetch_first(
            picked, ok_downloader, str(tmp_path), 1, 1,
            registry_b, set(), failures=failures,
        )
        assert asset is not None
        assert calls == [url]  # not skipped as previously failed
    finally:
        mp.reset_media_caches()


def test_17_foreign_provenance_candidate_is_rejected_for_project_b():
    """Required test 17, negative leg (project boundaries; AUD-18 /
    plan §3.3 invalid provenance): a candidate record hand-stamped
    with project A's provenance (project_id of another project) is
    rejected as invalid provenance in project B's chain and never
    reaches B's pick pool — hence never B's timeline or report."""
    candidate = fx.candidate()
    candidate["project_id"] = "project-a-foreign"
    scored = mp._uve_verify_all(
        [candidate], fx.scenario(), fx.bible(), shot_need=fx.shot_need(),
    )
    assert scored and scored[0].get("reject_reason") in (
        "invalid_provenance", "foreign_provenance", "wrong_project",
    )
    picked = mp._uve_pick(
        scored, fx.scenario(), VisualRegistry("t17b"), set(),
        shot_need=fx.shot_need(),
    )
    assert picked == []


# --------------------------------------------------------------- test 18

def _run_asset(tmp_path, name, candidate_id, run_id, subject_id):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 2048)
    return {
        "file_path": str(path), "path": str(path), "type": "video",
        "title": name, "candidate_id": candidate_id,
        "run_id": run_id, "scenario_subject_id": subject_id,
        "verification": {"accepted": True, "score": 0.9},
        "verification_status": "verified",
        "license": "unknown", "source": "youtube",
        "video_url": f"https://www.youtube.com/watch?v={candidate_id}",
        "segment_evidence": "verified_segment", "clip_start": 0,
        "duration_seconds": 8.0,
    }


def test_18_injected_run1_asset_is_not_run2_direct(tmp_path):
    """Required test 18, timeline leg (production boundaries): a
    run-1 asset record (verified against run 1's scenario, stamped
    with run 1's id) injected into run 2's scene media must NOT be
    counted as run 2's direct asset — run 2's sentence is served by
    run 2's own asset or not at all."""
    run1_asset = _run_asset(
        tmp_path, "run1.mp4", "cand-run1-foreign", "run-1",
        "bryan_kohberger",
    )
    run2_asset = _run_asset(
        tmp_path, "run2.mp4", "cand-run2-own", "run-2",
        "kaylee_goncalves",
    )
    scene_media = {
        "scene_number": 1, "duration_seconds": 5,
        "videos": [run2_asset, run1_asset], "images": [],
        "sentences": [
            {"sentence_number": 1, "videos": [run1_asset], "images": []},
        ],
    }
    result = build_scene_timeline(scene_media, _timings(1))
    used = {item["file_path"] for item in result["items"]}
    assert run1_asset["file_path"] not in used
    for item in result["items"]:
        assert not (
            item["assignment"] == "sentence"
            and item["file_path"] == run1_asset["file_path"]
        )


def _ledger_project():
    """Create the ledger tables + one project row (the
    test_stage2_ledger pattern); returns (project_id, cleanup)."""
    import uuid

    from database import SessionLocal, engine
    from models import AssetDecision, Base, ProductionRun, Project

    Base.metadata.create_all(
        bind=engine,
        tables=[Project.__table__, ProductionRun.__table__,
                AssetDecision.__table__],
    )
    pid = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(Project(id=pid, script="Boundary test script.",
                       category="Documentary", status="created"))
        db.commit()
    finally:
        db.close()

    def cleanup():
        db = SessionLocal()
        try:
            db.query(AssetDecision).filter(
                AssetDecision.project_id == pid).delete()
            db.query(ProductionRun).filter(
                ProductionRun.project_id == pid).delete()
            db.query(Project).filter(Project.id == pid).delete()
            db.commit()
        finally:
            db.close()

    return pid, cleanup


def test_18_run2_ledger_references_only_run2_candidates(tmp_path):
    """Required test 18, ledger leg (production boundaries): run 2's
    asset report is built with a run-1 asset injected into run 2's
    scene aggregates; run_ledger.record_decisions for run 2 must
    reference ONLY run 2's candidates — the foreign asset's
    candidate_id and source URL appear in no run-2 ledger row."""
    from services import run_ledger
    from services.asset_report import build_asset_report

    pid, cleanup = _ledger_project()
    try:
        run1_asset = _run_asset(
            tmp_path, "run1-led.mp4", "cand-run1-foreign", "run-1",
            "bryan_kohberger",
        )
        run2_asset = _run_asset(
            tmp_path, "run2-led.mp4", "cand-run2-own", "run-2",
            "kaylee_goncalves",
        )
        run2_id = run_ledger.begin_run(pid, "Run two script.")
        assert run2_id
        run2_scenes = [{
            "scene_number": 1,
            "videos": [run2_asset, run1_asset], "images": [],
            "sentences": [
                {"sentence_number": 1, "videos": [run2_asset],
                 "images": []},
            ],
            "asset_stats": {
                "candidates_considered": 2, "candidates_rejected": 1,
                "downloads_failed": 0,
                "candidate_records": [
                    {"candidate_id": "cand-run2-own",
                     "url": run2_asset["video_url"], "provider": "youtube",
                     "outcome": "accepted", "stage": "accepted",
                     "reason": ""},
                    {"candidate_id": "cand-run2-rej",
                     "url": "https://example.com/rejected-run2",
                     "provider": "youtube", "outcome": "rejected",
                     "stage": "verification", "reason": "missing_subject"},
                ],
                "missing_visuals": [],
            },
        }]
        timeline2 = {"scenes": [{"scene_number": 1, "items": [{
            "assignment": "sentence", "scene_number": 1,
            "sentence_index": 0, "file_path": run2_asset["file_path"],
            "start": 0.0, "duration": 5.0, "type": "video",
            "source": "youtube",
            "source_url": run2_asset["video_url"],
            "segment_evidence": "verified_segment",
        }]}]}
        report2 = build_asset_report(
            run2_scenes, timeline_result=timeline2,
            renderer_audit={"placeholder_count": 0},
        )
        written = run_ledger.record_decisions(
            run2_id, pid, report2, timeline2,
            processed_scenes=run2_scenes,
        )
        assert written is not None and written > 0
        rows, total = run_ledger.list_decisions(run2_id, limit=100)
        assert total > 0
        asset_keys = {row["asset_key"] for row in rows}
        assert "cand-run2-own" in asset_keys
        assert "cand-run1-foreign" not in asset_keys
        assert all(
            row["source_url"] != run1_asset["video_url"] for row in rows
        )
    finally:
        cleanup()


def test_18_run1_report_and_ledger_unchanged_by_run2(tmp_path):
    """Required test 18, append-only leg (production boundaries):
    after run 2 is built and recorded, run 1's asset report (built
    from run 1's artefacts) is byte-identical and run 1's ledger
    decision counts are unchanged — provenance is append-only and
    a later run never rewrites an earlier one."""
    import json as _json

    from services import run_ledger
    from services.asset_report import build_asset_report

    pid, cleanup = _ledger_project()
    try:
        run1_asset = _run_asset(
            tmp_path, "run1-own.mp4", "cand-run1-own", "run-1",
            "bryan_kohberger",
        )
        run1_scenes = [{
            "scene_number": 1, "videos": [run1_asset], "images": [],
            "sentences": [
                {"sentence_number": 1, "videos": [run1_asset],
                 "images": []},
            ],
            "asset_stats": {
                "candidates_considered": 1, "candidates_rejected": 0,
                "downloads_failed": 0, "candidate_records": [],
                "missing_visuals": [],
            },
        }]
        timeline1 = {"scenes": [{"scene_number": 1, "items": [{
            "assignment": "sentence", "scene_number": 1,
            "sentence_index": 0, "file_path": run1_asset["file_path"],
            "start": 0.0, "duration": 5.0, "type": "video",
            "source": "youtube",
            "source_url": run1_asset["video_url"],
            "segment_evidence": "verified_segment",
        }]}]}
        report1 = build_asset_report(
            run1_scenes, timeline_result=timeline1,
            renderer_audit={"placeholder_count": 0},
        )
        report1_snapshot = _json.dumps(report1, sort_keys=True)
        run1_id = run_ledger.begin_run(pid, "Run one script.")
        assert run1_id
        assert run_ledger.record_decisions(
            run1_id, pid, report1, timeline1,
            processed_scenes=run1_scenes,
        )
        counts_before = run_ledger.decision_counts(run1_id)

        # Run 2 happens: its own asset, its own report, its own rows.
        run2_asset = _run_asset(
            tmp_path, "run2-own.mp4", "cand-run2-own", "run-2",
            "kaylee_goncalves",
        )
        run2_scenes = [{
            "scene_number": 1, "videos": [run2_asset], "images": [],
            "sentences": [
                {"sentence_number": 1, "videos": [run2_asset],
                 "images": []},
            ],
            "asset_stats": {
                "candidates_considered": 1, "candidates_rejected": 0,
                "downloads_failed": 0, "candidate_records": [],
                "missing_visuals": [],
            },
        }]
        timeline2 = {"scenes": [{"scene_number": 1, "items": [{
            "assignment": "sentence", "scene_number": 1,
            "sentence_index": 0, "file_path": run2_asset["file_path"],
            "start": 0.0, "duration": 5.0, "type": "video",
            "source": "youtube",
            "source_url": run2_asset["video_url"],
            "segment_evidence": "verified_segment",
        }]}]}
        report2 = build_asset_report(
            run2_scenes, timeline_result=timeline2,
            renderer_audit={"placeholder_count": 0},
        )
        run2_id = run_ledger.begin_run(pid, "Run two script.")
        assert run2_id
        assert run_ledger.record_decisions(
            run2_id, pid, report2, timeline2,
            processed_scenes=run2_scenes,
        )

        rebuilt1 = build_asset_report(
            run1_scenes, timeline_result=timeline1,
            renderer_audit={"placeholder_count": 0},
        )
        assert _json.dumps(rebuilt1, sort_keys=True) == report1_snapshot
        assert run_ledger.decision_counts(run1_id) == counts_before
    finally:
        cleanup()


def _write_measured_parts(media_dir, durations):
    """Stage 5 (R4): production windows derive from MEASURED
    narration parts — real WAV part files plus a v2 parts manifest
    whose recorded durations match the files exactly."""
    import json as _json
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
    (parts_dir / "parts_manifest.json").write_text(_json.dumps(
        {"version": 2, "model": "fixture", "parts": parts}
    ))


# --------------------------------------------------------------- test 19

def test_19_no_random_or_unrelated_fallback_is_selected(tmp_path):
    """Required test 19 (no unrelated fallback; DM-10 family): a
    B12-shaped beat whose own pool is only M06 (high-quality
    unrelated). M06 is rejected at the gate/scoring stage; genuine
    media from a SIBLING scene and verified assets in a global
    registry never appear on the beat's sentences as assignments;
    the beat's scene resolves only via labelled rotation of its OWN
    scene's eligible media or a recorded gap — here it has no
    eligible media of its own, so the production records the gap,
    the renderer fabricates nothing, and a separate own-scene
    variant shows the rotation resolution using only its own
    scene's asset."""
    import json as _json
    import uuid

    from services import renderer
    from services.asset_report import build_asset_report
    from services.timeline import build_project_timeline
    from services.visual_registry import VisualRegistry

    # M06 never survives verification for the beat's contract.
    # (Metadata-absence bypass, run-6345 fix 2026-11-23: M06's gate
    # failures are absence-only, so the gate no longer terminates
    # it; the scorer's contradictory_geography verdict is the
    # recorded rejection. It is still rejected, still unpickable.)
    scored = mp._uve_verify_all(
        [fx.MISLEADING["M06"]()], fx.scenario(), fx.bible(),
        shot_need=fx.shot_need(),
    )
    assert scored[0].get("reject_reason") == "contradictory_geography"
    assert scored[0].get("rejection_stage") == "scoring"
    assert mp._uve_pick(
        scored, fx.scenario(), VisualRegistry("t19"), set(),
        shot_need=fx.shot_need(),
    ) == []

    sibling = _visual(tmp_path, "sibling.mp4",
                      verification_status="verified", license="unknown",
                      source="youtube")
    # A global verified pool exists (another production's registry).
    global_registry = VisualRegistry(f"t19-global-{uuid.uuid4()}")
    global_registry.register(url="https://www.youtube.com/watch?v=GLOBAL0019",
                             clip_key="youtube:GLOBAL0019|0",
                             start=0, end=8)
    assert global_registry.source_usage_count(
        "https://www.youtube.com/watch?v=GLOBAL0019") == 1

    project_id = f"t19-{uuid.uuid4()}"
    media_dir = tmp_path / "media" / project_id
    media_dir.mkdir(parents=True)
    segments = [
        {"start": 0.0, "end": 5.0, "text": "The wall divided the city."},
        {"start": 5.0, "end": 10.0, "text": "The closing line has no visual."},
    ]
    (media_dir / "captions.json").write_text(_json.dumps(
        {"segments": segments, "words": [], "duration": 10.0}
    ))
    _write_measured_parts(media_dir, [5.0, 5.0])
    scene1 = {
        "scene": {"scene_number": 1}, "scene_number": 1,
        "duration_seconds": 5, "videos": [sibling], "images": [],
        "sentences": [
            {"sentence_number": 1,
             "sentence_text": "The wall divided the city.",
             "videos": [sibling], "images": []},
        ],
        "asset_stats": {"candidates_considered": 1,
                        "candidates_rejected": 0, "downloads_failed": 0},
    }
    scene2 = {
        "scene": {"scene_number": 2}, "scene_number": 2,
        "duration_seconds": 5, "videos": [], "images": [],
        "sentences": [
            {"sentence_number": 2,
             "sentence_text": "The closing line has no visual.",
             "videos": [], "images": []},
        ],
        "asset_stats": {
            "candidates_considered": 1, "candidates_rejected": 1,
            "downloads_failed": 0,
            "missing_visuals": [
                {"sentence_number": 2, "reason": "no_verified_asset",
                 "specificity": "exact"},
            ],
        },
    }
    timeline = build_project_timeline(
        project_id, [scene1, scene2], output_dir=str(tmp_path / "media"),
    )
    scene1_items = timeline["scenes"][0]["items"]
    scene2_items = timeline["scenes"][1]["items"]
    assert [i["file_path"] for i in scene1_items] == [sibling["file_path"]]
    # The sibling asset was NOT borrowed across the scene boundary.
    assert scene2_items == []
    assert timeline["coverage"]["complete"] is False
    assert [u["sentence_number"] for u in
            timeline["coverage"]["uncovered_sentences"]] == [2]

    report = build_asset_report(
        [scene1, scene2], timeline_result=timeline,
        renderer_audit={"placeholder_count": 0},
    )
    assert report["total_accepted_assets"] == 1
    assert report["missing_visual_count"] == 1
    assert report["placeholder_clips"] == 0

    # Renderer: an empty beat scene renders nothing and fabricates
    # no placeholder.
    clips, _cursor, audit = renderer.render_scene(
        {"scene_number": 2, "items": []}, str(tmp_path),
    )
    assert clips == [] and audit["placeholder_count"] == 0

    # Own-scene resolution: when the beat's OWN scene has eligible
    # media, the sentence resolves via labelled rotation of THAT
    # asset — still never the sibling's.
    own = _visual(tmp_path, "own-scene.mp4")
    own_scene = {
        "scene_number": 2, "duration_seconds": 5,
        "videos": [own], "images": [],
        "sentences": [
            {"sentence_number": 1, "videos": [], "images": []},
        ],
    }
    own_result = build_scene_timeline(own_scene, _timings(1))
    assert len(own_result["items"]) == 1
    assert own_result["items"][0]["file_path"] == own["file_path"]
    assert own_result["items"][0]["assignment"] == "rotation"
    assert own_result["items"][0]["file_path"] != sibling["file_path"]


# --------------------------------------------------------------- test 20

def _t20_asset(tmp_path, name, type_, **over):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 2048)
    asset = {
        "file_path": str(path), "path": str(path), "type": type_,
        "title": name, "verification": {"accepted": True},
        "verification_status": "verified", "license": "unknown",
        "source": "youtube", "duration_seconds": 8.0,
        "segment_evidence": "verified_segment",
    }
    asset.update(over)
    return asset


_T20_TEXTS = [
    "Police arrested the suspect outside the house.",
    "Officers searched the house for evidence.",
    "A photograph showed the street that morning.",
    "Crowds gathered and celebrated in the square.",
    "The town waited for news from the court.",
    "The case involved 42 witnesses in total.",
    "Detectives reviewed the timeline of that night.",
    "No verified visual exists for this closing line.",
]


def _t20_production(tmp_path):
    """The synthetic production for required test 20: sentences
    1-2 direct video, 3 direct still (B10), 4 a still covering a
    footage-intent beat (B11), 5 contextual/rotation, 6 a targeted
    motion graphic (B04), 7 a positional-segment direct assignment,
    8 an honest gap in its own medialess scene."""
    import json as _json
    import uuid

    from services.timeline import build_project_timeline

    v1 = _t20_asset(tmp_path, "t20-v1.mp4", "video")
    v2 = _t20_asset(tmp_path, "t20-v2.mp4", "video")
    still1 = _t20_asset(
        tmp_path, "t20-still1.jpg", "image", source="wikimedia",
        visual_format="photograph", license="CC BY 4.0",
        segment_evidence="not_applicable",
    )
    still2 = _t20_asset(
        tmp_path, "t20-still2.jpg", "image", source="wikimedia",
        visual_format="photograph",
        segment_evidence="not_applicable",
    )
    contextual = _t20_asset(tmp_path, "t20-context.mp4", "video")
    graphic = _t20_asset(
        tmp_path, "motion_graphics/motion_graphic_t20.mp4", "video",
        source="cinemora_motion_graphics", motion_graphic=True,
        sentence_index=5, segment_evidence="not_applicable",
    )
    graphic.pop("verification")
    graphic.pop("verification_status")
    # Phase 1 / AM-012 supersession: a generated graphic must now
    # carry a passing validation verdict and not be undelivered to
    # be timeline-eligible at all (this fixture previously relied
    # on the wholesale graphics exemption in timeline_eligible).
    # The stamps below are the honest post-MG-07 attach shape —
    # every graphic the jobs attach gate admits carries them — so
    # the class distinctions test 20 reconciles are unchanged.
    graphic["validation"] = {"passed": True}
    graphic["delivered"] = True
    positional = _t20_asset(
        tmp_path, "t20-positional.mp4", "video",
        segment_evidence="positional_no_contract", clip_start=20,
    )

    def sentence(number, videos=(), images=()):
        return {
            "sentence_number": number,
            "sentence_text": _T20_TEXTS[number - 1],
            "videos": list(videos), "images": list(images),
        }

    scene1 = {
        "scene": {"scene_number": 1}, "scene_number": 1,
        "duration_seconds": 35,
        # Aggregates order matters for the rotation draw at
        # sentence 5: the contextual asset precedes the (still
        # unused) positional asset in the pool.
        "videos": [contextual, v1, v2, positional, graphic],
        "images": [still1, still2],
        "sentences": [
            sentence(1, videos=[v1]),
            sentence(2, videos=[v2]),
            sentence(3, images=[still1]),
            sentence(4, images=[still2]),
            sentence(5),
            sentence(6),
            sentence(7, videos=[positional]),
        ],
        "asset_stats": {
            "candidates_considered": 9, "candidates_rejected": 2,
            "downloads_failed": 0, "missing_visuals": [],
        },
    }
    scene2 = {
        "scene": {"scene_number": 2}, "scene_number": 2,
        "duration_seconds": 5, "videos": [], "images": [],
        "sentences": [sentence(8)],
        "asset_stats": {
            "candidates_considered": 1, "candidates_rejected": 1,
            "downloads_failed": 0,
            "missing_visuals": [
                {"sentence_number": 8, "reason": "no_verified_asset",
                 "specificity": "exact"},
            ],
        },
    }
    project_id = f"t20-{uuid.uuid4()}"
    media_dir = tmp_path / "media" / project_id
    media_dir.mkdir(parents=True)
    segments = [
        {"start": float(i * 5), "end": float(i * 5 + 5),
         "text": _T20_TEXTS[i]}
        for i in range(8)
    ]
    (media_dir / "captions.json").write_text(_json.dumps(
        {"segments": segments, "words": [], "duration": 40.0}
    ))
    _write_measured_parts(media_dir, [5.0] * 8)
    timeline = build_project_timeline(
        project_id, [scene1, scene2], output_dir=str(tmp_path / "media"),
    )
    assets = {
        "v1": v1, "v2": v2, "still1": still1, "still2": still2,
        "contextual": contextual, "graphic": graphic,
        "positional": positional,
    }
    return [scene1, scene2], timeline, assets


def test_20_final_reports_distinguish_classes(tmp_path):
    """Required test 20 (report-class separation; DM-08 family):
    the production-shaped timeline + asset report state each class
    separately and reconcile — assignment breakdown, format mix,
    positional share, the honest gap — with no class merged into
    another. The B11 still is a still in the format facts AND a
    direct assignment in the relevance facts, without contradiction.
    (The visual_quality sentence_outcomes leg is the companion test
    below.)"""
    from services.asset_report import build_asset_report

    scenes, timeline, assets = _t20_production(tmp_path)
    report = build_asset_report(
        scenes, timeline_result=timeline,
        renderer_audit={"placeholder_count": 0},
    )

    # Fact 1 — assignment breakdown (relevance classes), reconciled
    # with the timeline item total: 5 direct + 1 rotation + 1 graphic.
    assert report["assignment_breakdown"] == {
        "sentence": 5, "rotation": 1, "motion_graphic": 1,
    }
    assert report["timeline_items"] == 7
    assert sum(report["assignment_breakdown"].values()) == \
        report["timeline_items"]
    assert report["rotation_assignments"] == 1

    # Fact 2 — the timeline items themselves carry the classes.
    items = timeline["items"]
    by_sentence = {i["sentence_index"] + 1: i for i in items}
    assert by_sentence[1]["assignment"] == "sentence"
    assert by_sentence[1]["type"] == "video"
    assert by_sentence[2]["assignment"] == "sentence"
    assert by_sentence[5]["assignment"] == "rotation"
    assert by_sentence[5]["file_path"] == assets["contextual"]["file_path"]
    assert by_sentence[5]["relevance_status"] == \
        "not_verified_for_this_sentence"
    assert by_sentence[6]["assignment"] == "motion_graphic"
    assert by_sentence[7]["segment_evidence"] == "positional_no_contract"

    # Fact 3 — format mix (composition classes), reconciled with
    # the accepted-asset total: 4 footage + 2 photographs + 1 graphic.
    assert report["total_accepted_assets"] == 7
    mix = report["format_mix"]["accepted"]
    assert mix.get("footage") == 4
    assert mix.get("photograph") == 2
    assert mix.get("graphic") == 1
    assert sum(mix.values()) == report["total_accepted_assets"]
    assert report["generated_graphics"] == 1
    assert report["verified_moving_footage"] == 4
    assert report["verified_images"] == 2

    # Fact 4 — the B11 still (sentence 4): a STILL in the format
    # facts and a DIRECT assignment in the relevance facts.
    assert by_sentence[4]["type"] == "image"
    assert by_sentence[4]["assignment"] == "sentence"
    assert by_sentence[4]["relevance_status"] == "verified_for_sentence"
    assert by_sentence[3]["type"] == "image"
    assert by_sentence[3]["assignment"] == "sentence"

    # Fact 5 — positional share is its own number, at both levels.
    seg = report["verification_dimensions"]["segment_evidence"]
    assert seg["positional_no_contract"] == 1
    assert seg["positional_assignments"] == 1
    assert seg["positional_assignment_share"] == round(1 / 7, 4)

    # Fact 6 — the honest gap: in missing_visuals AND in timeline
    # coverage as uncovered; no item was built for sentence 8.
    assert report["missing_visual_count"] == 1
    assert report["missing_visuals"][0]["sentence_number"] == 8
    assert report["missing_visuals"][0]["reason"] == "no_verified_asset"
    assert 8 not in by_sentence
    assert [u["sentence_number"] for u in
            timeline["coverage"]["uncovered_sentences"]] == [8]
    assert report["text_cards"] == 0
    assert report["placeholder_clips"] == 0


def test_20_sentence_outcomes_reconcile_with_report_classes(tmp_path):
    """Required test 20, Phase 3 contract leg (services/visual_quality
    sentence_outcomes + diversity): the same production measures as
    buckets direct=5, approved_graphic=1, contextual=1,
    editorial_holdover=0, uncovered=1 over sentence_count=8,
    reconciling with the report's assignment breakdown (7 items)
    plus the one uncovered sentence; per-sentence outcomes name
    each class; diversity reports unique-assets-per-minute (tied to
    the module's own timeline totals) and a zero repeated-item rate
    for this all-unique production. Depends on the concurrent
    Phase 3 sentence_outcomes/diversity landing."""
    import pytest

    from services.asset_report import (
        build_asset_report, build_rights_manifest,
    )
    from services.visual_quality import measure_visual_quality

    scenes, timeline, assets = _t20_production(tmp_path)
    report = build_asset_report(
        scenes, timeline_result=timeline,
        renderer_audit={"placeholder_count": 0},
    )
    manifest = build_rights_manifest(scenes)
    measured = measure_visual_quality(
        timeline=timeline, asset_report=report,
        rights_manifest=manifest,
        motion_graphics={
            "assets": [{"file_path": assets["graphic"]["file_path"]}],
            "targets": {"requested": 1},
        },
    )
    outcomes = measured["sentence_outcomes"]
    assert outcomes["sentence_count"] == 8
    # Stage 7 / R7 (C.B.3): three additive profile buckets
    # (direct_*_profile) exist since Stage 7; this production's
    # items carry no verification_category stamps, so all three
    # count zero and every pre-Stage-7 count is unchanged.
    assert outcomes["buckets"] == {
        "direct": 5, "approved_graphic": 1, "contextual": 1,
        "editorial_holdover": 0, "uncovered": 1,
        "direct_contextual_profile": 0,
        "direct_environmental_profile": 0,
        "direct_atmosphere_profile": 0,
    }
    assert sum(outcomes["buckets"].values()) == \
        outcomes["sentence_count"]
    assert outcomes["reconciles"] is True
    assert outcomes["shares"]["direct"] == pytest.approx(5 / 8)
    assert outcomes["shares"]["uncovered"] == pytest.approx(1 / 8)
    assert outcomes["sentences_without_verified_visual"] == 1
    per_sentence = {
        entry["sentence_number"]: entry["outcome"]
        for entry in outcomes["per_sentence"]
    }
    assert per_sentence == {
        1: "direct", 2: "direct", 3: "direct", 4: "direct",
        5: "contextual", 6: "approved_graphic", 7: "direct",
        8: "uncovered",
    }
    # Cross-reconciliation with the asset report's own classes:
    # direct+graphic+contextual items == report timeline items, and
    # adding the uncovered sentence reaches the sentence total.
    assert (
        outcomes["buckets"]["direct"]
        + outcomes["buckets"]["approved_graphic"]
        + outcomes["buckets"]["contextual"]
        + outcomes["buckets"]["editorial_holdover"]
    ) == report["timeline_items"]
    assert measured["timeline"]["timeline_item_count"] == 7

    diversity = measured["diversity"]
    tl = measured["timeline"]
    expected_d1 = (
        tl["unique_canonical_assets_on_timeline"] * 60.0
        / tl["total_runtime_seconds"]
    )
    assert diversity["d1_unique_assets_per_minute"] == \
        pytest.approx(expected_d1)
    assert diversity["d2_repeated_item_rate"] == 0.0
