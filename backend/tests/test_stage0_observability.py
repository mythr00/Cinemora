"""Stage 0 (Protection & Observability) regression tests.

- MA-11: production_state.json is written atomically (temp file in
  the same directory + os.replace) — a reader never sees a partial
  file, and a failed write leaves the previous state intact.
- MA-09: the persisted state never contains the run's internal
  "_partial" payload, while fail_pipeline still produces its VA-07
  failure report from the in-memory partial state.
- AUD-03: jobs write the Project row's status back (running ->
  completed / failed), failing soft when the DB is unavailable.
- AUD-30: completed_at is set when a run finishes, and the declared
  stage list matches the stages the pipeline actually runs.
- GET /projects/{id}/status serves DB status + persisted progress,
  with a clean 404 for unknown projects.
- Run metrics accumulate at the real choke points and land in the
  asset report's "execution" section with monetary_cost_usd = None
  (unknown — never fabricated as zero).
"""

import json
import os
import uuid

import pytest

import jobs
import main
from database import Base, SessionLocal, engine
from models import Project
from services import media_pipeline, run_metrics, search_provider
from services.asset_report import build_asset_report
from services.production_state import PRODUCTION_STAGES, create_production_state
from services.visual_registry import get_registry


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(bind=engine)
    yield


def _make_project(session, status="created"):
    project_id = str(uuid.uuid4())
    session.add(
        Project(
            id=project_id,
            script="A short test script.",
            category="Documentary",
            status=status,
        )
    )
    session.commit()
    return project_id


def _db_status(project_id):
    session = SessionLocal()
    try:
        project = session.get(Project, project_id)
        return project.status if project is not None else None
    finally:
        session.close()


# ----------------------------------------------------------------------
# MA-11 — atomic state writes
# ----------------------------------------------------------------------

def test_atomic_write_content_and_no_temp_leftovers(tmp_path):
    state = create_production_state()
    state_file = jobs.save_production_state(tmp_path, state)
    assert state_file == tmp_path / "production_state.json"
    loaded = json.loads(state_file.read_text(encoding="utf-8"))
    assert loaded["status"] == "running"
    assert set(loaded["stages"]) == set(PRODUCTION_STAGES)
    leftovers = [p for p in tmp_path.iterdir() if p.name != "production_state.json"]
    assert leftovers == []


def test_atomic_write_uses_replace_from_same_directory(tmp_path, monkeypatch):
    calls = []
    real_replace = os.replace

    def recording_replace(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(jobs.os, "replace", recording_replace)
    jobs.save_production_state(tmp_path, create_production_state())
    assert len(calls) == 1
    src, dst = calls[0]
    # The temp file lived in the same directory and is gone after the
    # replace; the destination is the state file itself.
    assert os.path.dirname(src) == str(tmp_path)
    assert str(dst) == str(tmp_path / "production_state.json")
    assert not os.path.exists(src)
    assert os.path.exists(dst)


def test_failed_write_leaves_previous_state_intact(tmp_path):
    state = create_production_state()
    jobs.save_production_state(tmp_path, state)
    before = (tmp_path / "production_state.json").read_text(encoding="utf-8")

    broken = create_production_state()
    broken["not_serialisable"] = object()
    with pytest.raises(TypeError):
        jobs.save_production_state(tmp_path, broken)

    after = (tmp_path / "production_state.json").read_text(encoding="utf-8")
    assert after == before
    assert json.loads(after)["status"] == "running"
    leftovers = [p for p in tmp_path.iterdir() if p.name != "production_state.json"]
    assert leftovers == []


# ----------------------------------------------------------------------
# MA-09 — _partial never persisted, still available in memory
# ----------------------------------------------------------------------

def test_persisted_state_excludes_partial_but_memory_keeps_it(tmp_path):
    state = create_production_state()
    state["_partial"] = {
        "processed_scenes": [{"scene_number": 1}],
        "analysis": {"mode": "llm"},
        "timeline_result": None,
    }
    jobs.save_production_state(tmp_path, state)

    loaded = json.loads(
        (tmp_path / "production_state.json").read_text(encoding="utf-8")
    )
    assert "_partial" not in loaded
    assert not any(key.startswith("_") for key in loaded)
    # The in-memory dict is untouched: fail_pipeline depends on it.
    assert state["_partial"]["analysis"] == {"mode": "llm"}


def test_fail_pipeline_report_and_state_hygiene(tmp_path):
    run_metrics.begin_run()
    state = {
        "_partial": {
            "processed_scenes": [],
            "analysis": {"mode": "degraded"},
            "timeline_result": None,
        }
    }
    with pytest.raises(RuntimeError):
        jobs.fail_pipeline(state, "asset_manager", RuntimeError("boom"), tmp_path)

    # VA-07 behaviour preserved: the failure report is still written
    # from the in-memory partial state...
    report = json.loads((tmp_path / "asset_report.json").read_text())
    assert report["run_completed"] is False
    assert report["run_failure"] == {"stage": "asset_manager", "error": "boom"}
    assert report["analysis_mode"] == "degraded"
    # ...and now carries the execution block with honest unknown cost.
    assert report["execution"]["monetary_cost_usd"] is None
    assert "unknown" in report["execution"]["monetary_cost_note"]
    assert report["execution"]["failures"] == 1

    # The persisted state is failed + completed_at set (AUD-30), and
    # contains no run internals (MA-09).
    persisted = json.loads(
        (tmp_path / "production_state.json").read_text(encoding="utf-8")
    )
    assert persisted["status"] == "failed"
    assert persisted["completed_at"] is not None
    assert "_partial" not in persisted


# ----------------------------------------------------------------------
# AUD-03 — status write-back
# ----------------------------------------------------------------------

def test_status_writeback_transitions():
    session = SessionLocal()
    try:
        project_id = _make_project(session)
    finally:
        session.close()

    assert _db_status(project_id) == "created"
    jobs._set_project_status(project_id, "running")
    assert _db_status(project_id) == "running"
    jobs._set_project_status(project_id, "completed")
    assert _db_status(project_id) == "completed"


def test_status_writeback_failed_via_fail_pipeline(tmp_path):
    session = SessionLocal()
    try:
        project_id = _make_project(session)
    finally:
        session.close()

    jobs._set_project_status(project_id, "running")
    # fail_pipeline derives the project id from the directory name,
    # exactly as run_full_pipeline's media/<project_id> layout does.
    project_dir = tmp_path / project_id
    project_dir.mkdir()
    state = {"_partial": {"processed_scenes": [], "analysis": None}}
    with pytest.raises(RuntimeError):
        jobs.fail_pipeline(state, "voice_actor", RuntimeError("kaput"), project_dir)
    assert _db_status(project_id) == "failed"


def test_status_writeback_fails_soft(monkeypatch):
    # Unknown project: a no-op, not an error.
    jobs._set_project_status("no-such-project", "running")

    # DB layer exploding: swallowed, never raised into the pipeline.
    import database

    def exploding_session():
        raise RuntimeError("db is down")

    monkeypatch.setattr(database, "SessionLocal", exploding_session)
    jobs._set_project_status("anything", "running")


# ----------------------------------------------------------------------
# AUD-30 — declared stages are the stages that run
# ----------------------------------------------------------------------

def test_declared_stages_match_pipeline_stages():
    assert PRODUCTION_STAGES == [
        "voice_actor",
        "script_analysis",
        "asset_manager",
        "motion_designer",
        "sound_designer",
        "video_editor",
    ]


# ----------------------------------------------------------------------
# GET /projects/{id}/status
# ----------------------------------------------------------------------

def test_status_endpoint_shapes(tmp_path, monkeypatch):
    session = SessionLocal()
    try:
        project_id = _make_project(session)

        # No run yet: DB status only, progress is None.
        result = main.get_project_status(project_id, db=session)
        assert result["id"] == project_id
        assert result["status"] == "created"
        assert result["progress"] is None

        # With a persisted state file under media/<id>/, progress is
        # served — and it contains no run internals.
        # Stage 2G: media paths are anchored via CINEMORA_MEDIA_ROOT
        # (services/media_paths), no longer resolved against the CWD —
        # point the media root at this test's tmp layout, whose
        # media/<id> structure matches project_dir() exactly.
        monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
        monkeypatch.chdir(tmp_path)
        media_dir = tmp_path / "media" / project_id
        media_dir.mkdir(parents=True)
        state = create_production_state()
        state["_partial"] = {"processed_scenes": []}
        jobs.save_production_state(media_dir, state)

        result = main.get_project_status(project_id, db=session)
        assert result["progress"]["status"] == "running"
        assert set(result["progress"]["stages"]) == set(PRODUCTION_STAGES)
        assert "_partial" not in result["progress"]

        # Unknown project: a clean 404 from a real DB query.
        with pytest.raises(main.HTTPException) as excinfo:
            main.get_project_status("does-not-exist", db=session)
        assert excinfo.value.status_code == 404
    finally:
        session.close()


# ----------------------------------------------------------------------
# Run metrics — accumulation, choke points, execution block
# ----------------------------------------------------------------------

def test_metrics_accumulate_and_snapshot():
    run_metrics.begin_run()
    run_metrics.search_call("searxng", "videos")
    run_metrics.search_call("searxng", "videos")
    run_metrics.search_call("searxng", "search")
    run_metrics.search_call("searxng", "images")
    run_metrics.archive_org_search()
    run_metrics.wikimedia_search()
    run_metrics.wikimedia_search()
    run_metrics.candidates_discovered(7)
    run_metrics.candidates_discovered(3)
    run_metrics.download_attempted()
    run_metrics.download_attempted()
    run_metrics.download_attempted()
    run_metrics.download_succeeded(100)
    run_metrics.download_succeeded(250)
    run_metrics.coverage_recovery_round()
    run_metrics.stage_started("voice_actor")
    run_metrics.stage_finished("voice_actor")

    snap = run_metrics.snapshot()
    assert snap["search_calls"]["searxng"] == {"total": 4, "web": 1, "images": 1, "video": 2}
    assert snap["archive_org_searches"] == 1
    assert snap["wikimedia_searches"] == 2
    assert snap["candidates_discovered"] == 10
    assert snap["downloads"] == {
        "attempted": 3,
        "succeeded": 2,
        "failed": 1,
        "bytes_downloaded": 350,
    }
    assert snap["coverage_recovery_rounds"] == 1
    assert snap["failures"] == 0
    assert snap["stage_durations_seconds"]["voice_actor"] >= 0
    assert snap["wall_time_seconds"] is not None
    assert snap["wall_time_seconds"] >= 0
    assert snap["monetary_cost_usd"] is None
    assert "unknown" in snap["monetary_cost_note"]


def test_searxng_choke_point_counts_real_calls_by_type(monkeypatch):
    class FakeResponse:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {
                "results": [
                    {"url": "https://example.com/v", "title": "t",
                     "engine": "youtube"}
                ]
            }

    monkeypatch.setattr(
        search_provider.requests, "get", lambda *a, **k: FakeResponse()
    )
    run_metrics.begin_run()
    # A unique query guarantees a cache miss, so the call reaches
    # the HTTP choke point in _searxng_search.
    results = search_provider.search_videos(f"metrics-probe-{uuid.uuid4()}")
    assert results and results[0]["video_url"] == "https://example.com/v"
    snap = run_metrics.snapshot()
    assert snap["search_calls"]["searxng"]["video"] == 1
    assert snap["search_calls"]["searxng"]["total"] == 1


def test_fetch_first_counts_downloads_and_bytes(tmp_path):
    payload = tmp_path / "clip.mp4"
    payload.write_bytes(b"\x00" * 1234)
    registry = get_registry(f"metrics-{uuid.uuid4()}")
    run_metrics.begin_run()

    def ok_downloader(candidate, scene_dir, sentence_number, asset_index):
        return {
            "file_path": str(payload),
            "path": str(payload),
            "url": candidate.get("url"),
            "video_url": candidate.get("url"),
            "type": "video",
            "title": "clip",
        }

    asset = media_pipeline._uve_fetch_first(
        [{"url": "https://example.com/ok"}],
        ok_downloader, tmp_path, 1, 1, registry, set(), failures=[],
    )
    assert asset is not None
    snap = run_metrics.snapshot()
    assert snap["downloads"]["attempted"] == 1
    assert snap["downloads"]["succeeded"] == 1
    assert snap["downloads"]["bytes_downloaded"] == 1234

    def failing_downloader(candidate, scene_dir, sentence_number, asset_index):
        candidate["_download_error"] = "nope"
        return None

    asset = media_pipeline._uve_fetch_first(
        [{"url": "https://example.com/fails"}],
        failing_downloader, tmp_path, 1, 1, registry, set(), failures=[],
    )
    assert asset is None
    snap = run_metrics.snapshot()
    assert snap["downloads"]["attempted"] == 2
    assert snap["downloads"]["succeeded"] == 1
    assert snap["downloads"]["failed"] == 1


def test_execution_block_in_built_report():
    run_metrics.begin_run()
    run_metrics.search_call("searxng", "search")
    report = build_asset_report([], execution=run_metrics.snapshot())
    assert report["execution"]["search_calls"]["searxng"]["web"] == 1
    assert report["execution"]["monetary_cost_usd"] is None
    assert "unknown" in report["execution"]["monetary_cost_note"]

    # Callers without metrics keep the key, explicitly None.
    assert build_asset_report([])["execution"] is None
