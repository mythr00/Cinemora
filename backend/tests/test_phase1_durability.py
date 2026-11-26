"""Phase 1 — Durability (Master Production Recovery & Architecture
Upgrade, directive §11 Phase 1; acceptance tests §14 #1, #3, #4, #5
insofar as they are unit/integration-testable without a live
production).

What this file pins:

1. Same-run resume at the ledger: a heartbeat-stale run is
   REQUEUED in place (same row, dispatch stamp cleared, resume
   budget incremented, billing NOT settled/restored) instead of
   being failed at 180s; only a run that exhausts MAX_RESUMES is
   failed "interrupted" with restoration, exactly as before.
2. A requeued run is claimable again and its lifecycle completes
   normally; concurrent claims still produce exactly one winner.
3. Self-resume planning (services.stage_resume.plan_self_resume):
   a run's own completed stage records validate with the cross-run
   rules (artefact hashes; corruption stops the prefix).
4. Asset-stage scene checkpoints (record/load/clear): committed
   scenes + registry state round-trip; a tampered selected asset
   or a script-hash mismatch refuses the checkpoint.
5. End-to-end same-run resume through jobs.run_full_pipeline with
   stubbed stage workers: a kill inside scene 2 of asset_manager,
   the row reset to the state a real kill leaves (running + stale
   heartbeat), reconciliation, and re-entry with the SAME run id —
   voice/analysis are self-adopted, scene 1 is NOT re-processed
   (checkpoint), scene 2 executes, the run completes.
6. Dispatcher wedge catch-all: a dispatched job in an inert RQ
   state (deferred/scheduled/canceled/stopped) older than the
   freshness grace gets its stamp cleared; a fresh one, or one
   whose run started executing, is left alone.
7. Per-stage resource instrumentation (run_metrics): a closed
   stage records cpu_seconds + process peak RSS in the snapshot.
"""

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from database import Base, SessionLocal, engine
from models import AssetDecision, ProductionRun, Project
from services import dispatcher, run_ledger, run_metrics, stage_resume
from services.production_state import create_production_state


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
                script="Phase 1 test script.",
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


def _unique_script(tag):
    return f"Phase 1 {tag} script {uuid.uuid4().hex}. One sentence."


def _set_heartbeat(run_id, when):
    db = SessionLocal()
    try:
        row = db.get(ProductionRun, str(run_id))
        progress = dict(row.progress or {})
        progress["heartbeat_at"] = when.isoformat()
        row.progress = progress
        db.commit()
    finally:
        db.close()


def _stale(run_id, seconds=3600):
    _set_heartbeat(
        run_id, datetime.now(timezone.utc) - timedelta(seconds=seconds)
    )


# ---------------------------------------------------------------------
# 1. Ledger: stale -> requeue (resume), not fail
# ---------------------------------------------------------------------


def test_stale_run_is_requeued_in_place(project_id):
    run_id = run_ledger.begin_run(project_id, _unique_script("resume"))
    # Give the run a dispatch stamp, as a promoted execution would.
    db = SessionLocal()
    try:
        row = db.get(ProductionRun, run_id)
        options = dict(row.options or {})
        options["dispatched_job_id"] = "job-dead"
        options["dispatched_at"] = datetime.now(timezone.utc).isoformat()
        row.options = options
        db.commit()
    finally:
        db.close()
    _stale(run_id)

    reconciled = run_ledger.reconcile_stale_runs(stale_after_seconds=180)
    assert reconciled >= 1
    run = run_ledger.get_run(run_id)
    assert run["status"] == "queued"
    assert run["error"] is None
    assert run["finished_at"] is None
    assert run["options"]["resume_count"] == 1
    assert "dispatched_job_id" not in run["options"]
    # Re-promotion is deliberately delayed a little (no hot loop).
    not_before = dispatcher._parse_iso(run["options"]["retry_not_before"])
    assert not_before > datetime.now(timezone.utc)
    # Presence of the dead worker is gone; the loss is on record.
    assert run["progress"]["resume_pending"] is True
    assert run["progress"]["previous_worker_id"] is not None


def test_resume_budget_exhaustion_fails_interrupted(project_id):
    run_id = run_ledger.begin_run(project_id, _unique_script("exhaust"))
    db = SessionLocal()
    try:
        row = db.get(ProductionRun, run_id)
        options = dict(row.options or {})
        options["resume_count"] = run_ledger.MAX_RESUMES
        row.options = options
        db.commit()
    finally:
        db.close()
    _stale(run_id)

    reconciled = run_ledger.reconcile_stale_runs(stale_after_seconds=180)
    assert reconciled >= 1
    run = run_ledger.get_run(run_id)
    assert run["status"] == "failed"
    assert run["error"] == "interrupted"
    assert run["finished_at"] is not None


def test_repeated_stale_cycles_consume_budget_then_fail(project_id):
    run_id = run_ledger.begin_run(project_id, _unique_script("cycles"))
    for expected_count in range(1, run_ledger.MAX_RESUMES + 1):
        _stale(run_id)
        run_ledger.reconcile_stale_runs(stale_after_seconds=180)
        run = run_ledger.get_run(run_id)
        assert run["status"] == "queued"
        assert run["options"]["resume_count"] == expected_count
        # Simulate the resume being claimed and dying again.
        db = SessionLocal()
        try:
            row = db.get(ProductionRun, run_id)
            row.status = "running"
            row.progress = {
                "worker_id": "dead:1:beef",
                "claimed_at": datetime.now(timezone.utc).isoformat(),
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
            db.commit()
        finally:
            db.close()
    _stale(run_id)
    run_ledger.reconcile_stale_runs(stale_after_seconds=180)
    assert run_ledger.get_run(run_id)["status"] == "failed"


def test_resume_does_not_settle_billing_but_terminal_failure_does(
    project_id, monkeypatch
):
    from services.billing import generation_billing

    settled = []
    monkeypatch.setattr(
        generation_billing,
        "settle_for_run",
        lambda run_id, status, execution=None: settled.append(
            (str(run_id), status)
        ),
    )
    run_id = run_ledger.begin_run(project_id, _unique_script("billing"))
    _stale(run_id)
    run_ledger.reconcile_stale_runs(stale_after_seconds=180)
    assert run_ledger.get_run(run_id)["status"] == "queued"
    # A resumed run is not terminal: no settlement, no restoration.
    assert settled == []

    # Exhaust the budget: now the terminal path restores as before.
    db = SessionLocal()
    try:
        row = db.get(ProductionRun, run_id)
        row.status = "running"
        options = dict(row.options or {})
        options["resume_count"] = run_ledger.MAX_RESUMES
        row.options = options
        progress = dict(row.progress or {})
        progress["heartbeat_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=3600)
        ).isoformat()
        row.progress = progress
        db.commit()
    finally:
        db.close()
    run_ledger.reconcile_stale_runs(stale_after_seconds=180)
    assert run_ledger.get_run(run_id)["status"] == "failed"
    assert settled == [(str(run_id), "failed")]


# ---------------------------------------------------------------------
# 2. Requeue -> claim -> complete; concurrent claims stay single-winner
# ---------------------------------------------------------------------


def test_requeued_run_claims_and_finishes(project_id):
    run_id = run_ledger.begin_run(project_id, _unique_script("lifecycle"))
    _stale(run_id)
    run_ledger.reconcile_stale_runs(stale_after_seconds=180)
    assert run_ledger.get_run(run_id)["status"] == "queued"

    claimed = run_ledger.claim_run(run_id, worker_id="worker-b")
    assert claimed is not None
    run = run_ledger.get_run(run_id)
    assert run["status"] == "running"
    assert run["progress"]["worker_id"] == "worker-b"
    assert run_ledger.finish_run(run_id, "completed") is True
    assert run_ledger.get_run(run_id)["status"] == "completed"


def test_concurrent_claims_exactly_one_winner(project_id):
    submission = run_ledger.submit_production(
        project_id, _unique_script("race")
    )
    run_id = submission["run_id"]
    results = []
    barrier = threading.Barrier(8)

    def _claim(index):
        barrier.wait(timeout=30)
        results.append(
            run_ledger.claim_run(run_id, worker_id=f"racer-{index}")
        )

    threads = [
        threading.Thread(target=_claim, args=(i,)) for i in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    winners = [r for r in results if r is not None]
    assert len(results) == 8
    assert len(winners) == 1
    assert run_ledger.get_run(run_id)["status"] == "running"


# ---------------------------------------------------------------------
# 3. Self-resume planning
# ---------------------------------------------------------------------


def _fabricate_prior_state(project_dir, artefact_dir, stages):
    """A prior execution's state with real stage records recorded
    through the production recorder (hashes included)."""
    state = create_production_state()
    for stage in stages:
        state["stages"][stage]["status"] = "completed"
        stage_resume.record_stage_completion(
            state, stage, project_dir, artefact_dir,
            payload={"stage": stage},
        )
    return state


def test_self_resume_validates_own_prefix(tmp_path):
    project_dir = tmp_path / "proj"
    artefact_dir = project_dir / "runs" / "run-self"
    artefact_dir.mkdir(parents=True)
    (project_dir / "narration.wav").write_bytes(b"narration")
    (project_dir / "captions.json").write_text("{}")
    state = _fabricate_prior_state(
        project_dir, artefact_dir, ["voice_actor", "script_analysis"]
    )
    plan = stage_resume.plan_self_resume(project_dir, "run-self", state)
    assert plan["self_resume"] is True
    assert plan["source_run_id"] == "run-self"
    assert sorted(plan["stages"]) == ["script_analysis", "voice_actor"]
    payload = stage_resume.load_stage_payload(plan, "voice_actor")
    assert payload == {"stage": "voice_actor"}


def test_self_resume_stops_at_corrupted_artefact(tmp_path):
    project_dir = tmp_path / "proj"
    artefact_dir = project_dir / "runs" / "run-self"
    artefact_dir.mkdir(parents=True)
    narration = project_dir / "narration.wav"
    narration.write_bytes(b"narration")
    (project_dir / "captions.json").write_text("{}")
    state = _fabricate_prior_state(
        project_dir, artefact_dir, ["voice_actor", "script_analysis"]
    )
    narration.write_bytes(b"tampered")
    plan = stage_resume.plan_self_resume(project_dir, "run-self", state)
    assert plan["stages"] == {}
    assert "artefact_hash_mismatch" in (plan["fallback"] or "")


def test_self_resume_without_prior_state_is_empty(tmp_path):
    plan = stage_resume.plan_self_resume(tmp_path, "run-x", None)
    assert plan["stages"] == {}
    assert plan["fallback"] == "no_prior_state"


# ---------------------------------------------------------------------
# 4. Asset-stage scene checkpoints
# ---------------------------------------------------------------------


def _scene_entry(project_dir, number, filename, content=b"clip"):
    scene_dir = Path(project_dir) / f"scene_{number}"
    scene_dir.mkdir(parents=True, exist_ok=True)
    clip = scene_dir / filename
    clip.write_bytes(content)
    return {
        "scene_number": number,
        "scene": {"scene_number": number},
        "images": [],
        "videos": [{"path": str(clip), "title": "stub"}],
        "asset_stats": {},
    }


def test_asset_progress_round_trip(tmp_path):
    project_dir = tmp_path / "proj"
    artefact_dir = project_dir / "runs" / "run-1"
    artefact_dir.mkdir(parents=True)
    entry = _scene_entry(project_dir, 1, "clip.mp4")
    registry_state = {"used_sources": ["src-a"], "usage_counts": {"src-a": 1}}
    assert stage_resume.record_asset_progress(
        artefact_dir, project_dir, script_hash="hash-1",
        processed_scenes=[entry], registry_state=registry_state,
    )
    loaded = stage_resume.load_asset_progress(
        artefact_dir, project_dir, script_hash="hash-1"
    )
    assert loaded is not None
    assert loaded["committed_scenes"] == [1]
    assert loaded["registry"]["usage_counts"] == {"src-a": 1}
    assert loaded["processed_scenes"][0]["scene_number"] == 1

    stage_resume.clear_asset_progress(artefact_dir)
    assert stage_resume.load_asset_progress(
        artefact_dir, project_dir, script_hash="hash-1"
    ) is None


def test_asset_progress_refused_on_tamper_or_wrong_script(tmp_path):
    project_dir = tmp_path / "proj"
    artefact_dir = project_dir / "runs" / "run-1"
    artefact_dir.mkdir(parents=True)
    entry = _scene_entry(project_dir, 1, "clip.mp4")
    assert stage_resume.record_asset_progress(
        artefact_dir, project_dir, script_hash="hash-1",
        processed_scenes=[entry], registry_state={},
    )
    # Wrong production: refused.
    assert stage_resume.load_asset_progress(
        artefact_dir, project_dir, script_hash="hash-2"
    ) is None
    # The selected asset changed after the crash: refused, so the
    # scene is re-executed rather than skipped over changed files.
    clip = Path(project_dir) / "scene_1" / "clip.mp4"
    clip.write_bytes(b"replaced-by-someone-else")
    assert stage_resume.load_asset_progress(
        artefact_dir, project_dir, script_hash="hash-1"
    ) is None
    # Deleted outright: also refused.
    clip.write_bytes(b"clip")
    assert stage_resume.record_asset_progress(
        artefact_dir, project_dir, script_hash="hash-1",
        processed_scenes=[entry], registry_state={},
    )
    clip.unlink()
    assert stage_resume.load_asset_progress(
        artefact_dir, project_dir, script_hash="hash-1"
    ) is None


# ---------------------------------------------------------------------
# 6. Dispatcher inert-job catch-all
# ---------------------------------------------------------------------


class _InertJob:
    is_failed = False
    is_finished = False
    exc_info = None

    def __init__(self, status):
        self._status = status

    def get_status(self):
        return self._status


def _queued_dispatched_run(project_id, job_id, dispatched_at, started=False):
    db = SessionLocal()
    try:
        run = ProductionRun(
            id=str(uuid.uuid4()),
            project_id=project_id,
            owner_id=None,
            script_hash=run_ledger.script_sha256("One. Two."),
            status="queued",
            options={
                "dispatched_job_id": job_id,
                "dispatched_at": dispatched_at.isoformat(),
            },
            progress=(
                {"claimed_at": datetime.now(timezone.utc).isoformat()}
                if started
                else {}
            ),
            started_at=(
                datetime.now(timezone.utc) if started else None
            ),
            created_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _patch_fetch(monkeypatch, job):
    def fake_fetch(cls, job_id, connection=None):
        return job

    monkeypatch.setattr("rq.job.Job.fetch", classmethod(fake_fetch))


def _sweep():
    db = SessionLocal()
    try:
        dispatcher._clear_stale_dispatches(db)
    finally:
        db.close()


def test_inert_dispatched_job_is_unwedged(project_id, monkeypatch):
    run_id = _queued_dispatched_run(
        project_id, "job-inert",
        datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    _patch_fetch(monkeypatch, _InertJob("deferred"))
    _sweep()
    run = run_ledger.get_run(run_id)
    assert run["status"] == "queued"
    assert "dispatched_job_id" not in (run["options"] or {})


def test_fresh_inert_job_is_left_alone(project_id, monkeypatch):
    run_id = _queued_dispatched_run(
        project_id, "job-fresh", datetime.now(timezone.utc)
    )
    _patch_fetch(monkeypatch, _InertJob("canceled"))
    _sweep()
    run = run_ledger.get_run(run_id)
    assert (run["options"] or {}).get("dispatched_job_id") == "job-fresh"


def test_inert_job_for_started_run_is_left_alone(project_id, monkeypatch):
    run_id = _queued_dispatched_run(
        project_id, "job-started",
        datetime.now(timezone.utc) - timedelta(minutes=10),
        started=True,
    )
    _patch_fetch(monkeypatch, _InertJob("stopped"))
    _sweep()
    run = run_ledger.get_run(run_id)
    assert (run["options"] or {}).get("dispatched_job_id") == "job-started"


# ---------------------------------------------------------------------
# 7. Per-stage resource instrumentation
# ---------------------------------------------------------------------


def test_stage_resources_recorded_in_snapshot():
    run_metrics.begin_run("phase1-metrics")
    try:
        run_metrics.stage_started("asset_manager")
        run_metrics.stage_finished("asset_manager")
        snapshot = run_metrics.snapshot("phase1-metrics")
    finally:
        run_metrics.begin_run(None)
    assert "asset_manager" in snapshot["stage_durations_seconds"]
    resources = snapshot["stage_resources"]["asset_manager"]
    assert resources["cpu_seconds"] >= 0
    assert resources["process_peak_rss_mb"] > 0


# ---------------------------------------------------------------------
# 5. End-to-end: kill during discovery -> reconcile -> SAME run resumes
#    (§14 acceptance test #1, at integration level with stubbed
#    stage workers — no production is submitted)
# ---------------------------------------------------------------------

SCRIPT = "First sentence of the film. Second sentence follows it."


class _SceneWorkers:
    """Call-counting stubs for the expensive stage workers, with a
    two-scene script analysis and a per-scene asset worker that
    can be told to die inside one scene."""

    def __init__(self, project_dir):
        self.project_dir = Path(project_dir)
        self.calls = []
        self.scene_calls = []
        self.fail_scene = None

    def count(self, name):
        return self.calls.count(name)

    def generate_narration(self, text, output_path, **kwargs):
        self.calls.append("generate_narration")
        out = Path(output_path)
        out.write_bytes(b"narration-bytes")
        parts = out.parent / "narration_parts"
        parts.mkdir(parents=True, exist_ok=True)
        (parts / "parts_manifest.json").write_text(
            json.dumps({"version": 2, "parts": []})
        )
        (parts / "part_001.wav").write_bytes(b"part")
        return {"model": "stub-voice"}

    def build_captions(self, audio, script, out, parts_manifest=None):
        self.calls.append("build_captions")
        Path(out).write_text("{}")
        Path(out).with_suffix(".srt").write_text("")
        Path(out).with_suffix(".vtt").write_text("")
        return {"captions_alignment": {"state": "stub"}}

    def build_production_scenes(self, script, project_id="",
                                project_dir=None, genre_pack=None):
        self.calls.append("build_production_scenes")
        sentences = [
            {"text": "First sentence of the film."},
            {"text": "Second sentence follows it."},
        ]
        scenes = [
            {"scene_number": 1, "sentence_count": 1,
             "sentences": sentences[:1], "text": script},
            {"scene_number": 2, "sentence_count": 1,
             "sentences": sentences[1:], "text": script},
        ]
        return {
            "analysis": {"mode": "stub", "planner": {}, "bible": {},
                         "sentences": sentences},
            "sentences": sentences,
            "scenes": scenes,
            "scene_governor": None,
            "script_hash": jobs.script_hash_for(script),
            "case_bible_path": "",
        }

    def process_scene_media(self, scene, project_dir):
        number = scene["scene_number"]
        self.scene_calls.append(number)
        self.calls.append("process_scene_media")
        if self.fail_scene == number:
            raise RuntimeError(f"injected kill: scene {number}")
        scene_dir = Path(project_dir) / f"scene_{number}"
        scene_dir.mkdir(parents=True, exist_ok=True)
        clip = scene_dir / "clip.mp4"
        clip.write_bytes(f"clip-bytes-{number}".encode())
        return {
            "scene_number": number,
            "images": [],
            "videos": [{"path": str(clip), "title": "stub clip"}],
            "asset_stats": {},
        }

    def build_motion_graphics_assets(self, *args, **kwargs):
        self.calls.append("build_motion_graphics_assets")
        return []

    def prepare_soundtrack(self, narration_file, output_file, **kwargs):
        self.calls.append("prepare_soundtrack")
        Path(output_file).write_bytes(b"soundtrack-bytes")
        return {"state": "ok"}

    def build_project_timeline(self, project_id, processed_scenes,
                               output_dir=None, artefact_dir=None,
                               parts_manifest=None):
        self.calls.append("build_project_timeline")
        timeline_file = Path(artefact_dir) / "timeline.json"
        timeline_file.write_text(json.dumps(
            {"scenes": [{"scene_number": 1}], "duration_seconds": 12.0}
        ))
        return {
            "timeline_file": str(timeline_file),
            "duration_seconds": 12.0,
            "coverage": {"complete": True, "total_sentences": 2,
                         "uncovered_sentences": []},
        }

    def render_timeline(self, scenes, project_dir, expected_duration=None):
        self.calls.append("render_timeline")
        Path(project_dir, "documentary.mp4").write_bytes(b"raw-video")

    def add_narration(self, raw, soundtrack, narrated):
        self.calls.append("add_narration")
        Path(narrated).write_bytes(b"narrated-video")


@pytest.fixture()
def scene_pipeline(monkeypatch, tmp_path):
    Base.metadata.create_all(bind=engine)
    project_id = f"phase1-resume-{uuid.uuid4().hex[:12]}"
    project_dir = tmp_path / project_id
    project_dir.mkdir(parents=True)
    db = SessionLocal()
    try:
        db.add(Project(id=project_id, script=SCRIPT, status="draft"))
        db.commit()
    finally:
        db.close()

    workers = _SceneWorkers(project_dir)
    monkeypatch.setattr(
        jobs.media_paths, "project_dir", lambda pid: project_dir
    )
    monkeypatch.setattr(jobs, "generate_narration", workers.generate_narration)
    monkeypatch.setattr(jobs, "build_captions", workers.build_captions)
    monkeypatch.setattr(
        jobs, "validate_captions", lambda *a, **k: {"ok": True}
    )
    monkeypatch.setattr(
        jobs, "build_production_scenes", workers.build_production_scenes
    )
    monkeypatch.setattr(
        jobs, "process_scene_media", workers.process_scene_media
    )
    monkeypatch.setattr(
        jobs, "build_motion_graphics_assets",
        workers.build_motion_graphics_assets,
    )
    monkeypatch.setattr(jobs, "prepare_soundtrack", workers.prepare_soundtrack)
    monkeypatch.setattr(
        jobs, "build_project_timeline", workers.build_project_timeline
    )
    monkeypatch.setattr(jobs, "render_timeline", workers.render_timeline)
    monkeypatch.setattr(jobs, "add_narration", workers.add_narration)
    monkeypatch.setattr(
        jobs, "validate_final_video",
        lambda *a, **k: {
            "verdict": {"evaluated": True, "passed": True},
            "duration_seconds": 12.0,
        },
    )
    monkeypatch.setattr(
        jobs, "build_asset_report",
        lambda *a, **k: {
            "total_accepted_assets": 1,
            "verified_moving_footage": 1,
            "verified_images": 0,
            "substitutions": 0,
            "authentic": True,
        },
    )
    monkeypatch.setattr(jobs, "build_rights_manifest", lambda *a, **k: [])
    monkeypatch.setattr(
        jobs.narration_service, "assign_sentence_voices", lambda *a, **k: None
    )
    monkeypatch.setattr(
        jobs.narration_service, "load_parts_manifest_data",
        lambda path: {"parts": [], "total_duration_seconds": 1.0},
    )
    monkeypatch.setattr(jobs.timing_service, "total_duration", lambda data: 1.0)
    monkeypatch.setattr(
        jobs.timing_service, "timing_consistency",
        lambda a, b: {
            "within_tolerance": True,
            "parts_total_seconds": 1.0,
            "concatenated_duration_seconds": 1.0,
            "divergence_seconds": 0.0,
            "tolerance_seconds": 0.5,
        },
    )
    monkeypatch.setattr(
        jobs.media_probe, "probe_audio",
        lambda path: {"duration_seconds": 1.0},
    )
    monkeypatch.setattr(
        jobs.music_service, "resolve_music_request",
        lambda options, project_dir: {
            "file": None, "music_volume": None, "source_type": "none",
            "decided_by": "test", "requested": False, "disabled": True,
            "narration_volume": None,
        },
    )
    monkeypatch.setattr(
        jobs.music_service, "resolve_mix_config",
        lambda **kwargs: {"music_volume": 0.5},
    )
    monkeypatch.setattr(
        jobs.music_service, "final_audio_record", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        jobs.music_service, "assess_audio_provenance", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        jobs.visual_quality_service, "measure_visual_quality",
        lambda **kwargs: {},
    )
    monkeypatch.setattr(
        jobs.production_report_service, "build_production_report",
        lambda **kwargs: {"verdicts": {"quality": {"verdict": "pass"}}},
    )
    from services import delivery_profile, evidence_producer
    from services import plan_document, visual_director

    monkeypatch.setattr(
        delivery_profile, "produce_delivery_copy",
        lambda src, dst: (
            Path(dst).write_bytes(b"delivery"),
            {"status": "ok", "delivery_bytes": 8, "master_bytes": 10},
        )[1],
    )
    monkeypatch.setattr(
        evidence_producer, "build_evidence", lambda *a, **k: ({}, {})
    )
    monkeypatch.setattr(
        visual_director, "outcomes_from_processed_scenes", lambda scenes: {}
    )
    monkeypatch.setattr(
        visual_director, "reconcile_visual_plan",
        lambda *a, **k: ({"summary": {}}, {"entries": []}),
    )
    for name in (
        "write_plan_document", "write_visual_plan_document",
        "write_visual_plan_v2_document", "write_graphics_manifest_document",
    ):
        monkeypatch.setattr(plan_document, name, lambda *a, **k: None)
    monkeypatch.setattr(
        plan_document, "visual_plan_from_analysis",
        lambda *a, **k: {"version": 1},
    )
    monkeypatch.setattr(
        jobs.run_ledger, "record_decisions", lambda *a, **k: None
    )
    monkeypatch.setattr(
        jobs.run_ledger, "record_music_decision", lambda *a, **k: None
    )
    return project_id, project_dir, workers


def _enter(project_id, run_id):
    return jobs.run_full_pipeline(
        project_id, SCRIPT, run_id=run_id, options={"captions": "off"}
    )


def test_kill_during_discovery_same_run_resumes_from_checkpoint(
    scene_pipeline,
):
    project_id, project_dir, workers = scene_pipeline
    submission = run_ledger.submit_production(project_id, SCRIPT)
    run_id = submission["run_id"]

    # Entry 1: the worker dies inside scene 2 of the asset stage.
    workers.fail_scene = 2
    with pytest.raises(RuntimeError, match="injected kill: scene 2"):
        _enter(project_id, run_id)
    assert workers.scene_calls == [1, 2]

    # A real kill never runs fail_pipeline: the row is left
    # "running" with a heartbeat that goes stale. Restore exactly
    # that state (entry 1's graceful failure finish is undone).
    db = SessionLocal()
    try:
        row = db.get(ProductionRun, run_id)
        row.status = "running"
        row.error = None
        row.finished_at = None
        row.progress = {
            "worker_id": "dead:1:beef",
            "claimed_at": (
                datetime.now(timezone.utc) - timedelta(seconds=3700)
            ).isoformat(),
            "heartbeat_at": (
                datetime.now(timezone.utc) - timedelta(seconds=3600)
            ).isoformat(),
        }
        db.commit()
    finally:
        db.close()

    # The reconciler requeues the SAME run (no new row, no retry
    # child) instead of failing it.
    run_ledger.reconcile_stale_runs(stale_after_seconds=180)
    run = run_ledger.get_run(run_id)
    assert run["status"] == "queued"
    assert run["options"]["resume_count"] == 1

    # Entry 2: same run id, claimed again.
    workers.fail_scene = None
    workers.calls.clear()
    workers.scene_calls.clear()
    result = _enter(project_id, run_id)
    assert result["status"] == "completed"

    # Voice + analysis were self-adopted from the run's own
    # records — their workers never ran again.
    assert workers.count("generate_narration") == 0
    assert workers.count("build_production_scenes") == 0
    # Scene 1 was committed before the kill and is NOT
    # re-processed; only scene 2 executes (§14 #1).
    assert workers.scene_calls == [2]

    state = json.loads(
        (project_dir / "runs" / run_id / "production_state.json")
        .read_text()
    )
    assert state["resume"]["self_resume"] is True
    assert state["resume"]["adopted_stages"] == [
        "voice_actor", "script_analysis",
    ]
    assert state["stages"]["voice_actor"]["adopted_from_run"] == run_id
    # The incremental checkpoint was superseded by the completed
    # stage payload and cleared.
    assert not stage_resume.asset_progress_path(
        project_dir / "runs" / run_id
    ).exists()
    assert run_ledger.get_run(run_id)["status"] == "completed"
