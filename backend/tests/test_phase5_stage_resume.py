"""Transformation Phase 5.1 — stage-level resume (PERF-001) +
AM-020 registry persistence.

Two levels of verification, neither of which submits a production:

1. Module level (services.stage_resume): a fabricated prior run's
   stages validate in order; corruption / a missing artefact / a
   script-hash or options mismatch stops adoption exactly where it
   should; an unserialisable payload makes its stage unadoptable
   without harming the run; the visual registry's exported state
   restores with its enforcement intact.

2. Pipeline level (jobs.run_full_pipeline with every stage worker
   stubbed): failure injection at the two expensive boundaries the
   plan names — a kill inside asset_manager and a kill inside
   video_editor. The follow-up run of the same script must adopt
   the validated stages (their workers are NOT called again),
   execute the rest, and complete; the run's production state must
   show adopted vs executed stages. Corrupting one adopted
   artefact between runs forces a clean re-run from stage one.
"""

import json
from pathlib import Path

import pytest

import jobs
from database import Base, SessionLocal, engine
from models import Project
from services import stage_resume
from services.production_state import create_production_state
from services.visual_registry import VisualRegistry


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(bind=engine)
    yield


# ---------------------------------------------------------------------------
# Module level
# ---------------------------------------------------------------------------


def _write_source_run(project_dir, run_id, script_hash, options, stages):
    """Fabricate a prior run: manifest + state files on disk."""
    run_dir = Path(project_dir) / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_manifest.json").write_text(json.dumps({
        "run_id": run_id,
        "script_hash": script_hash,
        "options": options,
        "status": "failed",
    }))
    state = create_production_state()
    state["status"] = "failed"
    for stage in stages:
        state["stages"][stage]["status"] = "completed"
    (run_dir / "production_state.json").write_text(json.dumps(state))
    return run_dir, state


def _record(project_dir, run_dir, state, stage, files, payload):
    for name, content in files.items():
        path = Path(project_dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    stage_resume.record_stage_completion(
        state, stage, Path(project_dir), Path(run_dir), payload
    )
    (Path(run_dir) / "production_state.json").write_text(json.dumps(state))


def test_plan_adopts_validated_prefix_and_stops_at_corruption(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    run_dir, state = _write_source_run(
        project_dir, "run-src", "hash-1", {"captions": "off"},
        ["voice_actor", "script_analysis", "asset_manager"],
    )
    _record(project_dir, run_dir, state, "voice_actor",
            {"narration.wav": b"audio", "captions.json": b"{}",
             "narration_parts/parts_manifest.json": b"{}"},
            {"state": {"voice": {"model": "stub"}}})
    _record(project_dir, run_dir, state, "script_analysis",
            {"case_bible.json": b"{}"},
            {"production_data": {"analysis": {}, "sentences": [], "scenes": []}})
    clip = project_dir / "scene_1" / "clip.mp4"
    _record(project_dir, run_dir, state, "asset_manager", {},
            {"processed_scenes": [{"videos": [{"path": str(clip)}]}],
             "registry": {}})
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"original-bytes")
    # The asset stage's artefact manifest was recorded BEFORE the
    # media file existed in this fabrication, so record it again
    # now that the file is on disk.
    stage_resume.record_stage_completion(
        state, "asset_manager", project_dir, run_dir,
        {"processed_scenes": [{"videos": [{"path": str(clip)}]}],
         "registry": {}},
    )
    (run_dir / "production_state.json").write_text(json.dumps(state))

    plan = stage_resume.plan_resume(
        project_dir, "run-new", "hash-1", {"captions": "off"}
    )
    assert plan["source_run_id"] == "run-src"
    assert sorted(plan["stages"]) == [
        "asset_manager", "script_analysis", "voice_actor",
    ]
    assert stage_resume.load_stage_payload(plan, "voice_actor") == {
        "state": {"voice": {"model": "stub"}}
    }

    # Corrupt the media file: the asset stage stops validating and
    # the adoptable prefix shrinks to the two earlier stages.
    clip.write_bytes(b"tampered-bytes")
    plan = stage_resume.plan_resume(
        project_dir, "run-new", "hash-1", {"captions": "off"}
    )
    assert sorted(plan["stages"]) == ["script_analysis", "voice_actor"]
    assert "asset_manager" in (plan["fallback"] or "")
    assert "artefact_hash_mismatch" in (plan["fallback"] or "")


def test_plan_rejects_script_and_options_mismatch(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    run_dir, state = _write_source_run(
        project_dir, "run-src", "hash-1", {"captions": "off"},
        ["voice_actor"],
    )
    _record(project_dir, run_dir, state, "voice_actor",
            {"narration.wav": b"audio", "captions.json": b"{}"},
            {"state": {}})

    plan = stage_resume.plan_resume(
        project_dir, "run-new", "hash-OTHER", {"captions": "off"}
    )
    assert plan["stages"] == {}
    plan = stage_resume.plan_resume(
        project_dir, "run-new", "hash-1", {"captions": "burn"}
    )
    assert plan["stages"] == {}
    assert "options_mismatch" in (plan["fallback"] or "")


def test_retry_bookkeeping_keys_do_not_block_adoption(tmp_path):
    """Phase 8.6 battery find: the dispatcher's retry sweep stamps
    retry_of/retry_attempt onto the child's options; those keys
    are ledger bookkeeping, not production options, so a retry
    child must still adopt its parent's validated stages."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    run_dir, state = _write_source_run(
        project_dir, "run-src", "hash-1", {"captions": "off"},
        ["voice_actor"],
    )
    _record(project_dir, run_dir, state, "voice_actor",
            {"narration.wav": b"audio", "captions.json": b"{}"},
            {"state": {}})

    plan = stage_resume.plan_resume(
        project_dir, "run-retry", "hash-1",
        {"captions": "off", "entrypoint": "enqueue_full_pipeline",
         "retry_of": "run-src", "retry_attempt": 1},
    )
    assert sorted(plan["stages"]) == ["voice_actor"]


def test_unserialisable_payload_makes_stage_unadoptable(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    run_dir, state = _write_source_run(
        project_dir, "run-src", "hash-1", {}, ["voice_actor"],
    )
    _record(project_dir, run_dir, state, "voice_actor",
            {"narration.wav": b"audio", "captions.json": b"{}"},
            {"state": {"bad": object()}})
    record = state["stages"]["voice_actor"]
    assert record["resume_payload"] is None
    assert "resume_payload_error" in record
    plan = stage_resume.plan_resume(project_dir, "run-new", "hash-1", {})
    assert plan["stages"] == {}


def test_registry_export_restore_round_trip():
    registry = VisualRegistry(project_id="p1")
    registry.register(path="/media/a.mp4", url="https://youtu.be/abc",
                      title="A title here", start=1.0, end=4.0)
    registry.register(url="https://youtu.be/abc", start=10.0, end=12.0)
    restored = VisualRegistry(project_id="p1").restore_state(
        json.loads(json.dumps(registry.export_state()))
    )
    assert restored.is_file_used("/media/a.mp4")
    assert restored.source_usage_count("https://youtu.be/abc") == 2
    assert restored.is_segment_used("https://youtu.be/abc", 2.0, 3.0)
    assert not restored.is_segment_used("https://youtu.be/abc", 20.0, 22.0)
    assert restored.summary() == registry.summary()


# ---------------------------------------------------------------------------
# Pipeline level — failure injection with stubbed stage workers
# ---------------------------------------------------------------------------

SCRIPT = "First sentence of the film. Second sentence follows it."
OTHER_SCRIPT = "An entirely different script. Nothing shared at all."


class _Workers:
    """Call-counting stubs for every expensive stage worker."""

    def __init__(self, project_dir):
        self.project_dir = Path(project_dir)
        self.calls = []
        self.fail_at = None  # "asset_manager" | "video_editor" | None

    def count(self, name):
        return self.calls.count(name)

    # voice_actor workers
    def generate_narration(self, text, output_path, **kwargs):
        self.calls.append("generate_narration")
        out = Path(output_path)
        out.write_bytes(b"narration-bytes")
        parts = out.parent / "narration_parts"
        parts.mkdir(parents=True, exist_ok=True)
        (parts / "parts_manifest.json").write_text(json.dumps(
            {"version": 2, "parts": []}
        ))
        (parts / "part_001.wav").write_bytes(b"part")
        return {"model": "stub-voice"}

    def build_captions(self, audio, script, out, parts_manifest=None):
        self.calls.append("build_captions")
        Path(out).write_text("{}")
        Path(out).with_suffix(".srt").write_text("")
        Path(out).with_suffix(".vtt").write_text("")
        return {"captions_alignment": {"state": "stub"}}

    # script_analysis worker
    def build_production_scenes(self, script, project_id="", project_dir=None,
                                genre_pack=None):
        self.calls.append("build_production_scenes")
        sentences = [{"text": "First sentence of the film."},
                     {"text": "Second sentence follows it."}]
        return {
            "analysis": {"mode": "stub", "planner": {}, "bible": {},
                         "sentences": sentences},
            "sentences": sentences,
            "scenes": [{"scene_number": 1, "sentence_count": 2,
                        "sentences": sentences, "text": script}],
            "scene_governor": None,
            "script_hash": jobs.script_hash_for(script),
            "case_bible_path": "",
        }

    # asset_manager worker
    def process_scene_media(self, scene, project_dir):
        self.calls.append("process_scene_media")
        if self.fail_at == "asset_manager":
            raise RuntimeError("injected kill: asset_manager")
        scene_dir = Path(project_dir) / "scene_1"
        scene_dir.mkdir(parents=True, exist_ok=True)
        clip = scene_dir / "clip.mp4"
        clip.write_bytes(b"clip-bytes")
        return {
            "scene_number": 1,
            "images": [],
            "videos": [{"path": str(clip), "title": "stub clip"}],
            "asset_stats": {},
        }

    # motion_designer worker
    def build_motion_graphics_assets(self, *args, **kwargs):
        self.calls.append("build_motion_graphics_assets")
        return []

    # sound_designer worker
    def prepare_soundtrack(self, narration_file, output_file, **kwargs):
        self.calls.append("prepare_soundtrack")
        Path(output_file).write_bytes(b"soundtrack-bytes")
        return {"state": "ok"}

    # video_editor workers
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
        if self.fail_at == "video_editor":
            raise RuntimeError("injected kill: video_editor")
        Path(project_dir, "documentary.mp4").write_bytes(b"raw-video")

    def add_narration(self, raw, soundtrack, narrated):
        self.calls.append("add_narration")
        Path(narrated).write_bytes(b"narrated-video")


@pytest.fixture()
def pipeline(monkeypatch, tmp_path):
    import uuid

    project_id = f"phase5-resume-{uuid.uuid4().hex[:12]}"
    project_dir = tmp_path / project_id
    project_dir.mkdir(parents=True)
    db = SessionLocal()
    try:
        db.add(Project(id=project_id, script=SCRIPT, status="draft"))
        db.commit()
    finally:
        db.close()

    workers = _Workers(project_dir)
    monkeypatch.setattr(
        jobs.media_paths, "project_dir", lambda pid: project_dir
    )
    monkeypatch.setattr(jobs, "generate_narration", workers.generate_narration)
    monkeypatch.setattr(jobs, "build_captions", workers.build_captions)
    monkeypatch.setattr(jobs, "validate_captions", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        jobs, "build_production_scenes", workers.build_production_scenes
    )
    monkeypatch.setattr(jobs, "process_scene_media", workers.process_scene_media)
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


def _run(project_id, script=SCRIPT):
    return jobs.run_full_pipeline(
        project_id, script, options={"captions": "off"}
    )


def _latest_state(project_dir):
    states = sorted(
        Path(project_dir).glob("runs/*/production_state.json"),
        key=lambda p: p.stat().st_mtime,
    )
    return json.loads(states[-1].read_text())


def test_kill_at_asset_manager_then_resume_adopts_voice_and_analysis(pipeline):
    project_id, project_dir, workers = pipeline
    workers.fail_at = "asset_manager"
    with pytest.raises(RuntimeError, match="injected kill"):
        _run(project_id)

    workers.fail_at = None
    workers.calls.clear()
    result = _run(project_id)
    assert result["status"] == "completed"
    # Adopted stages' workers never ran again.
    assert workers.count("generate_narration") == 0
    assert workers.count("build_production_scenes") == 0
    # The rest executed.
    assert workers.count("process_scene_media") == 1
    assert workers.count("render_timeline") == 1

    state = _latest_state(project_dir)
    assert state["stages"]["voice_actor"]["adopted"] is True
    assert state["stages"]["script_analysis"]["adopted"] is True
    assert state["stages"]["asset_manager"].get("adopted") is not True
    assert state["resume"]["adopted_stages"] == [
        "voice_actor", "script_analysis",
    ]
    assert "asset_manager" in state["resume"]["executed_stages"]


def test_kill_at_video_editor_then_resume_adopts_through_sound(pipeline):
    project_id, project_dir, workers = pipeline
    workers.fail_at = "video_editor"
    with pytest.raises(RuntimeError, match="injected kill"):
        _run(project_id)

    workers.fail_at = None
    workers.calls.clear()
    result = _run(project_id)
    assert result["status"] == "completed"
    assert workers.count("generate_narration") == 0
    assert workers.count("build_production_scenes") == 0
    assert workers.count("process_scene_media") == 0
    assert workers.count("build_motion_graphics_assets") == 0
    assert workers.count("prepare_soundtrack") == 0
    assert workers.count("render_timeline") == 1

    state = _latest_state(project_dir)
    assert state["resume"]["adopted_stages"] == [
        "voice_actor", "script_analysis", "asset_manager",
        "motion_designer", "sound_designer",
    ]
    assert state["stages"]["video_editor"].get("adopted") is not True


def test_corrupted_artefact_forces_clean_rerun(pipeline):
    project_id, project_dir, workers = pipeline
    workers.fail_at = "video_editor"
    with pytest.raises(RuntimeError, match="injected kill"):
        _run(project_id)

    # Tamper with the narration between runs: hash validation must
    # refuse adoption at the very first stage — a clean run.
    (project_dir / "narration.wav").write_bytes(b"tampered")
    workers.fail_at = None
    workers.calls.clear()
    result = _run(project_id)
    assert result["status"] == "completed"
    assert workers.count("generate_narration") == 1
    state = _latest_state(project_dir)
    assert state["resume"]["adopted_stages"] == []
    assert "artefact_hash_mismatch" in (state["resume"]["fallback"] or "")


def test_different_script_never_adopts(pipeline):
    project_id, project_dir, workers = pipeline
    workers.fail_at = "video_editor"
    with pytest.raises(RuntimeError, match="injected kill"):
        _run(project_id)

    workers.fail_at = None
    workers.calls.clear()
    result = _run(project_id, script=OTHER_SCRIPT)
    assert result["status"] == "completed"
    assert workers.count("generate_narration") == 1
    state = _latest_state(project_dir)
    assert state["resume"]["adopted_stages"] == []


def test_completed_run_is_fully_adopted_by_identical_rerun(pipeline):
    project_id, project_dir, workers = pipeline
    result = _run(project_id)
    assert result["status"] == "completed"
    first_video = Path(result["video"]).read_bytes()

    workers.calls.clear()
    result2 = _run(project_id)
    assert result2["status"] == "completed"
    # No stage worker ran at all — every stage adopted, including
    # the editor; the delivered film is byte-identical.
    assert workers.calls == []
    assert Path(result2["video"]).read_bytes() == first_video
    state = _latest_state(project_dir)
    assert state["resume"]["adopted_stages"] == [
        "voice_actor", "script_analysis", "asset_manager",
        "motion_designer", "sound_designer", "video_editor",
    ]
