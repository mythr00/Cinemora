"""Phase 8 — reliability: failure injection across the pipeline.

D2's contract, pinned at the seams where it is implemented: a
failure is RECORDED with its stage and reason, the work that can
continue does continue, and a production that cannot complete
says so — no silent placeholder, no false success, no swallowed
exception presented as a finished film.

Seams exercised:
* provider error mid-search -> discovery-failure records with the
  full failure shape; the search returns and the beat proceeds
  with the surviving providers' (empty) pools;
* download failure mid-scene -> the next candidate is fetched;
  the failure is recorded with its reason and the failed source
  is guarded for the rest of the run;
* graphics failure -> the target outcome is a recorded skip with
  a typed reason; the other targets still generate;
* render failure -> a scene with no genuine media fails LOUDLY
  (RuntimeError naming the scene/item), never a placeholder;
* stage failure -> jobs.fail_pipeline persists an honest
  terminal state (failed stage + error + completion stamp + a
  failure asset report from partial state) and re-raises;
* resume (PERF-001 measurement pin): run_full_pipeline has no
  resume/checkpoint parameter — an interrupted run restarts from
  the first stage. The policy choice stays the founder's; this
  pins the measured current behaviour so it cannot drift
  silently.
"""

import inspect

import pytest

import jobs
from services import graphics_pipeline, media_pipeline as mp, renderer


# ---------------------------------------------------------------------------
# Provider error mid-search
# ---------------------------------------------------------------------------

def test_provider_error_mid_search_is_recorded_and_search_continues(
    monkeypatch,
):
    mp.drain_discovery_failures()

    def exploding_videos(query, limit=8):
        raise RuntimeError("serper exploded")

    def exploding_web(query, limit=5):
        raise ConnectionError("web search unreachable")

    monkeypatch.setattr(mp, "search_videos_multi", exploding_videos)
    monkeypatch.setattr(mp, "search_web_sources", exploding_web)
    monkeypatch.setattr(mp, "search_image_sources", lambda q, limit=5: [])
    monkeypatch.setattr(
        mp, "search_wikimedia_images", lambda q, limit=3: []
    )
    scenario = {"queries": ["berlin wall 1961"], "format_intent": "any"}
    # Must not raise: the beat proceeds with empty pools.
    mp._uve_scenario_search(scenario, {}, "p8-reliability")
    failures = mp.drain_discovery_failures()
    providers = {f["provider"] for f in failures}
    assert "serper-videos" in providers
    assert "serper-web" in providers
    for failure in failures:
        assert failure["query"] == "berlin wall 1961"
        assert failure["error_class"]
        assert "transient" in failure
        assert failure["attempt"] == 1


# ---------------------------------------------------------------------------
# Download failure mid-scene
# ---------------------------------------------------------------------------

def test_download_failure_falls_to_next_candidate_and_is_recorded(
    tmp_path,
):
    candidates = [
        {
            "candidate_id": "cand_dead",
            "provider": "wikimedia",
            "url": "https://example.invalid/dead-p8-reliability.jpg",
        },
        {
            "candidate_id": "cand_good",
            "provider": "wikimedia",
            "url": "https://example.invalid/good-p8-reliability.jpg",
        },
    ]

    def downloader(candidate, scene_dir, sentence_number, asset_index):
        if candidate["candidate_id"] == "cand_dead":
            candidate["_download_error"] = "http_404"
            return None
        return {"file_path": "/media/good.jpg",
                "candidate_id": "cand_good"}

    failures = []
    try:
        asset = mp._uve_fetch_first(
            candidates, downloader, tmp_path, 1, 0,
            registry=None, used_keys=set(), failures=failures,
        )
    finally:
        mp._FAILED_DOWNLOAD_URLS.discard(
            mp.canonical_source_key(candidates[0]["url"])
        )
    assert asset is not None
    assert asset["candidate_id"] == "cand_good"
    assert len(failures) == 1
    assert failures[0]["reason"] == "http_404"
    assert failures[0]["candidate_id"] == "cand_dead"


def test_all_downloads_fail_returns_none_with_every_failure_recorded(
    tmp_path,
):
    candidates = [
        {
            "candidate_id": f"cand_dead_{i}",
            "provider": "archive.org",
            "url": f"https://example.invalid/dead-{i}-p8.jpg",
        }
        for i in range(2)
    ]

    def downloader(candidate, scene_dir, sentence_number, asset_index):
        candidate["_download_error"] = "connection_reset"
        return None

    failures = []
    try:
        asset = mp._uve_fetch_first(
            candidates, downloader, tmp_path, 1, 0,
            registry=None, used_keys=set(), failures=failures,
        )
    finally:
        for candidate in candidates:
            mp._FAILED_DOWNLOAD_URLS.discard(
                mp.canonical_source_key(candidate["url"])
            )
    assert asset is None
    assert len(failures) == 2
    assert all(f["reason"] == "connection_reset" for f in failures)


# ---------------------------------------------------------------------------
# Graphics failure
# ---------------------------------------------------------------------------

def _graphics_data():
    return {
        "case_bible": {"timeline": []},
        "scenes": [{
            "scene_number": 1,
            "sentences": [
                {"text": "Exports rose 42 percent.",
                 "sentence_number": 1},
                {"text": "Imports rose 17 percent.",
                 "sentence_number": 2},
            ],
        }],
    }


def _manifest_entry(manifest_id, index, number, text):
    return {
        "manifest_id": manifest_id,
        "target": {
            "scene_number": 1, "sentence_index": index,
            "sentence_number": number,
        },
        "text": text, "scenario": {}, "purpose": "coverage_fallback",
        "template_candidates": [], "family_candidates": ["stat"],
        "theme": None, "fact_basis": "script_claim",
        "evidence_ref": None, "verified_data": None,
    }


def test_graphics_failure_is_a_recorded_skip_not_a_dead_stage(
    tmp_path, monkeypatch,
):
    calls = {"count": 0}

    def flaky_write(path, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("renderer exploded")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)

    monkeypatch.setattr(
        graphics_pipeline, "_render_stat_graphic", flaky_write
    )
    monkeypatch.setattr(
        graphics_pipeline, "validate_graphic_output",
        lambda path, expected_duration=None, **kwargs: {
            "passed": True, "failure_kind": None, "detail": "",
            "checks": {}, "probe": None,
        },
    )
    manifest = {
        "version": 1, "project_id": "p8", "theme": None,
        "entries": [
            _manifest_entry("gm_0001", 0, 1, "Exports rose 42 percent."),
            _manifest_entry("gm_0002", 1, 2, "Imports rose 17 percent."),
        ],
    }
    assets = graphics_pipeline.build_motion_graphics_assets(
        _graphics_data(), tmp_path, mode="balanced", manifest=manifest,
    )
    # One graphic generated; the failed one is a recorded skip
    # with the typed rendering_failure reason.
    assert len(assets) == 1
    import json

    written = json.loads((tmp_path / "motion_graphics.json").read_text())
    outcomes = {
        o["manifest_id"]: o for o in written["targets"]["outcomes"]
    }
    assert outcomes["gm_0001"]["outcome"] == "skipped"
    assert outcomes["gm_0001"]["reason"] == "rendering_failure"
    assert outcomes["gm_0002"]["outcome"] == "generated"


# ---------------------------------------------------------------------------
# Render failure
# ---------------------------------------------------------------------------

def test_render_scene_with_no_genuine_media_fails_loudly(tmp_path):
    scene = {
        "scene_number": 9,
        "items": [{
            "sentence_index": 0, "duration": 2.0,
            "start": 0.0, "end": 2.0,
            "file_path": str(tmp_path / "missing.mp4"),
            "type": "video",
        }],
    }
    with pytest.raises(RuntimeError) as excinfo:
        renderer.render_scene(scene, tmp_path, 0)
    assert "no genuine visual to hold over" in str(excinfo.value)
    assert "refusing to substitute a placeholder" in str(excinfo.value)


def test_concatenate_with_no_clips_is_an_honest_false(tmp_path):
    assert renderer.concatenate_clips([], tmp_path / "out.mp4") is False


# ---------------------------------------------------------------------------
# Stage failure -> honest terminal state
# ---------------------------------------------------------------------------

def test_fail_pipeline_persists_honest_terminal_state(tmp_path):
    project_dir = tmp_path / "projp8"
    project_dir.mkdir()
    state = {"project_id": "projp8", "stages": {}}
    jobs.run_stage(state, "asset_manager", project_dir)
    with pytest.raises(RuntimeError, match="provider exploded"):
        jobs.fail_pipeline(
            state, "asset_manager",
            RuntimeError("provider exploded"), project_dir,
        )
    import json

    saved = json.loads(
        (project_dir / "production_state.json").read_text()
    )
    assert saved["status"] == "failed"
    assert saved["current_stage"] == "asset_manager"
    assert saved["error"] == "provider exploded"
    assert saved["stages"]["asset_manager"]["status"] == "failed"
    assert saved["stages"]["asset_manager"]["error"] == (
        "provider exploded"
    )
    assert saved["completed_at"]
    # VA-07: a failure still leaves its asset report, built from
    # partial state — diagnosable, never a success document.
    report = json.loads((project_dir / "asset_report.json").read_text())
    assert report["run_completed"] is False


# ---------------------------------------------------------------------------
# PERF-001 measurement pin (policy stays the founder's)
# ---------------------------------------------------------------------------

def test_pipeline_has_no_resume_entry_point():
    """Measured behaviour (Phase 8): run_full_pipeline accepts no
    resume/checkpoint argument and its first stage is always the
    voice stage — an interrupted run restarts from zero. PERF-001
    (resume vs formally accept safe-fail) is the founder's
    decision; this pin records the status quo so a future change
    is a deliberate, visible act."""
    params = inspect.signature(jobs.run_full_pipeline).parameters
    assert not any(
        "resume" in name or "checkpoint" in name for name in params
    )
    source = inspect.getsource(jobs.run_full_pipeline)
    first_stage = source.split("run_stage(")[1].split(")")[0]
    assert "voice" in first_stage
