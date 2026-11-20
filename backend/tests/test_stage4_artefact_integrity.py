"""Stage 4 (R3) — artefact integrity: DATA-101 + DATA-004.

DATA-101: every JSON/text artefact writer in the live backend goes
through the single atomic write path (services/atomic_write —
temp file in the same directory + fsync + os.replace). Before this
stage the decisions writers were split (one atomic, one bare
``write_text`` in api_v1) and ~30 verdict-bearing or read-back
artefacts (timeline.json, asset_report.json, rights_manifest.json,
visual_quality.json, production_report.json, pipeline_result.json,
captions.json, the narration parts manifest, the story cache, ...)
were written non-atomically, so a crash mid-write truncated exactly
the files verdicts are read back from.

DATA-004: production artefacts were project-scoped while decisions
are run-scoped — a re-run overwrote the previous run's files while
its ledger rows and certificate still referenced them. New runs now
write their derived artefacts under ``runs/<run_id>/`` (Master
Review D3.9 Phases 1+2, including the production final MP4), and
readers resolve through media_paths.resolve_artefact with the
legacy project-level layout as fallback, so pre-existing projects
(Berlin/Kohberger artefacts included) read exactly as before.
"""

from __future__ import annotations

import ast
import json
import os
import uuid
from pathlib import Path

import pytest

import jobs
from database import SessionLocal, engine
from models import AssetDecision, Base, ProductionRun, Project, ProjectRevision
from services import atomic_write, media_paths, plan_document, run_ledger
from services.asset_report import write_asset_report, write_rights_manifest
from services.atomic_write import write_json_atomic, write_text_atomic
from services.production_state import create_production_state

BACKEND_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------

def test_write_json_atomic_round_trip_and_no_leftovers(tmp_path):
    target = tmp_path / "nested" / "report.json"
    returned = write_json_atomic(target, {"verdict": "PASS", "n": 3})
    assert returned == target
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "verdict": "PASS",
        "n": 3,
    }
    leftovers = [p.name for p in target.parent.iterdir()]
    assert leftovers == ["report.json"]


def test_write_json_atomic_crash_preserves_previous_file(tmp_path, monkeypatch):
    target = tmp_path / "timeline.json"
    write_json_atomic(target, {"version": 1})
    before = target.read_bytes()

    def exploding_replace(src, dst):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(atomic_write.os, "replace", exploding_replace)
    with pytest.raises(OSError):
        write_json_atomic(target, {"version": 2})
    # The previous complete file is untouched and no temp remains.
    assert target.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["timeline.json"]


def test_write_json_atomic_unserialisable_touches_nothing(tmp_path):
    target = tmp_path / "state.json"
    write_json_atomic(target, {"ok": True})
    before = target.read_bytes()
    with pytest.raises(TypeError):
        write_json_atomic(target, {"bad": object()})
    assert target.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_write_text_atomic_creates_run_shaped_dirs(tmp_path):
    target = tmp_path / "runs" / "run-1" / "notes.txt"
    write_text_atomic(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


# ---------------------------------------------------------------------
# DATA-101 — writer census: one atomic write path, no bare writers
# ---------------------------------------------------------------------

_FROZEN_MARKERS = ("_before_", "_backup", "_corrupted", "_pre_", "_recovered", ".cpython-")


def _live_modules() -> list[Path]:
    files = [
        BACKEND_ROOT / name
        for name in ("api_v1.py", "main.py", "jobs.py", "worker.py")
    ]
    for path in sorted((BACKEND_ROOT / "services").glob("*.py")):
        if any(marker in path.name for marker in _FROZEN_MARKERS):
            continue
        files.append(path)
    return files


def _write_mode(call: ast.Call):
    mode = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            mode = keyword.value.value
    return mode


def test_writer_census_no_bare_text_writes_in_live_modules():
    """The provider-ban test is the pattern: a static census that
    fails on any NEW bare writer, so the DATA-101 class cannot
    quietly regrow. Binary writes (``wb`` — media downloads/copies)
    are out of scope: they are not artefact-JSON writers."""
    offenders = []
    for path in _live_modules():
        if path.name == "atomic_write.py":
            continue  # the one sanctioned home of the raw pattern
        tree = ast.parse(path.read_bytes())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "write_text":
                offenders.append(f"{path.name}:{node.lineno} write_text()")
            if isinstance(func, ast.Attribute) and func.attr == "fdopen":
                offenders.append(f"{path.name}:{node.lineno} fdopen()")
            if isinstance(func, ast.Name) and func.id == "open":
                mode = _write_mode(node)
                if isinstance(mode, str) and any(c in mode for c in "wax") and "b" not in mode:
                    offenders.append(f"{path.name}:{node.lineno} open(mode={mode!r})")
            if isinstance(func, ast.Attribute) and func.attr == "open":
                mode = None
                if node.args and isinstance(node.args[0], ast.Constant):
                    mode = node.args[0].value
                if isinstance(mode, str) and any(c in mode for c in "wax") and "b" not in mode:
                    offenders.append(f"{path.name}:{node.lineno} Path.open(mode={mode!r})")
    assert not offenders, (
        "Bare text writers found in live modules — route them through "
        f"services.atomic_write: {offenders}"
    )


def test_atomic_write_module_is_the_single_home():
    assert callable(atomic_write.write_json_atomic)
    assert callable(atomic_write.write_text_atomic)
    # fdopen (the raw temp-file pattern) exists ONLY inside it.
    homes = []
    for path in _live_modules():
        tree = ast.parse(path.read_bytes())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "fdopen"
            ):
                homes.append(path.name)
    assert homes == ["atomic_write.py"]


def test_append_plan_decision_accumulates_valid_json(tmp_path, monkeypatch):
    import api_v1

    replaced = []
    real_replace = os.replace

    def recording_replace(src, dst):
        replaced.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)
    api_v1._append_plan_decision(tmp_path, {"sentence_number": 1, "approve": True})
    api_v1._append_plan_decision(tmp_path, {"sentence_number": 2, "approve": False})
    payload = json.loads(
        (tmp_path / "plan_decisions.json").read_text(encoding="utf-8")
    )
    assert payload["version"] == 1
    assert [d["sentence_number"] for d in payload["decisions"]] == [1, 2]
    # Both appends went through the atomic replace path.
    assert len(replaced) == 2
    assert [p.name for p in tmp_path.iterdir()] == ["plan_decisions.json"]


# ---------------------------------------------------------------------
# DATA-004 — resolution rules
# ---------------------------------------------------------------------

def _write(path: Path, payload, mtime_ns=None):
    write_json_atomic(path, payload)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))


def test_resolve_artefact_explicit_run_and_legacy_fallback(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    run_a = media_paths.run_subdir(project_dir, "run-a")
    _write(project_dir / "timeline.json", {"origin": "legacy"})
    _write(run_a / "timeline.json", {"origin": "run-a"})

    resolved_a = media_paths.resolve_artefact(project_dir, "timeline.json", "run-a")
    assert json.loads(resolved_a.read_text()) == {"origin": "run-a"}
    # A run without its own copy falls back to the legacy file.
    resolved_b = media_paths.resolve_artefact(project_dir, "timeline.json", "run-b")
    assert json.loads(resolved_b.read_text()) == {"origin": "legacy"}
    # A project with no runs at all resolves to the project path.
    plain = tmp_path / "plain"
    plain.mkdir()
    _write(plain / "timeline.json", {"origin": "legacy"})
    assert media_paths.resolve_artefact(plain, "timeline.json") == (
        plain / "timeline.json"
    )
    # Missing everywhere: the legacy path is returned for the
    # caller's existing absence handling.
    assert media_paths.resolve_artefact(plain, "nope.json") == plain / "nope.json"


def test_resolve_artefact_latest_run_view_prefers_freshest_run_copy(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    run_a = media_paths.run_subdir(project_dir, "run-a")
    run_b = media_paths.run_subdir(project_dir, "run-b")
    _write(project_dir / "asset_report.json", {"origin": "legacy"}, mtime_ns=1_000_000_000)
    _write(run_a / "asset_report.json", {"origin": "run-a"}, mtime_ns=2_000_000_000)
    _write(run_b / "asset_report.json", {"origin": "run-b"}, mtime_ns=3_000_000_000)

    resolved = media_paths.resolve_artefact(project_dir, "asset_report.json")
    assert json.loads(resolved.read_text()) == {"origin": "run-b"}
    # Freshness is per artefact: rewriting run A's copy makes it
    # the latest-run view for that artefact.
    _write(run_a / "asset_report.json", {"origin": "run-a-new"}, mtime_ns=4_000_000_000)
    resolved = media_paths.resolve_artefact(project_dir, "asset_report.json")
    assert json.loads(resolved.read_text()) == {"origin": "run-a-new"}


def test_run_scoped_names_cover_the_pipeline_writers():
    # The names jobs.run_full_pipeline writes run-scoped (D3.9
    # Phases 1+2) — pinned so a future artefact cannot silently
    # revert to the shared project directory.
    assert media_paths.RUN_SCOPED_ARTEFACTS == {
        "timeline.json",
        "asset_report.json",
        "rights_manifest.json",
        "visual_quality.json",
        "production_report.json",
        "run_manifest.json",
        "production_state.json",
        "pipeline_result.json",
        "documentary_final_output.mp4",
    }


# ---------------------------------------------------------------------
# DATA-004 — two-run isolation through the real writers
# ---------------------------------------------------------------------

def _write_run_artefacts(project_dir: Path, run_id: str, marker: str):
    run_dir = media_paths.run_subdir(project_dir, run_id)
    state = create_production_state()
    state["_run_id"] = run_id
    state["status"] = "completed"
    jobs.save_production_state(project_dir, state)
    write_asset_report(run_dir, {"marker": marker})
    write_rights_manifest(run_dir, [{"marker": marker}])
    written = run_ledger.write_manifest(run_dir, {"run_id": run_id, "marker": marker})
    assert written is not None
    write_json_atomic(run_dir / "timeline.json", {"marker": marker})
    write_json_atomic(run_dir / "pipeline_result.json", {"marker": marker})
    return run_dir


def test_two_runs_never_overwrite_each_others_artefacts(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    run_a_dir = _write_run_artefacts(project_dir, "run-a", "A")
    snapshot_a = {p.name: p.read_bytes() for p in run_a_dir.iterdir()}
    assert snapshot_a  # the run actually wrote its artefacts

    run_b_dir = _write_run_artefacts(project_dir, "run-b", "B")

    # Run B's writes left run A's artefacts byte-identical...
    assert {p.name: p.read_bytes() for p in run_a_dir.iterdir()} == snapshot_a
    # ...and nothing leaked into the shared project directory.
    for name in media_paths.RUN_SCOPED_ARTEFACTS:
        assert not (project_dir / name).exists(), name
    # Each run resolves its own artefacts.
    def _marker(payload):
        if isinstance(payload, list):
            return payload[0].get("marker") if payload else None
        return payload.get("marker") or payload.get("status")

    for name in (
        "timeline.json",
        "asset_report.json",
        "rights_manifest.json",
        "run_manifest.json",
        "production_state.json",
        "pipeline_result.json",
    ):
        payload = json.loads(
            media_paths.resolve_artefact(project_dir, name, "run-a").read_text()
        )
        assert _marker(payload) in ("A", "completed"), (name, payload)
        payload_b = json.loads(
            media_paths.resolve_artefact(project_dir, name, "run-b").read_text()
        )
        assert _marker(payload_b) in ("B", "completed"), (name, payload_b)
    assert run_b_dir != run_a_dir


def test_save_production_state_scoping(tmp_path):
    plain = create_production_state()
    returned = jobs.save_production_state(tmp_path, plain)
    assert returned == tmp_path / "production_state.json"

    scoped = create_production_state()
    scoped["_run_id"] = "run-9"
    returned = jobs.save_production_state(tmp_path, scoped)
    assert returned == tmp_path / "runs" / "run-9" / "production_state.json"
    # The persisted file carries no run internals (MA-09 preserved).
    persisted = json.loads(returned.read_text(encoding="utf-8"))
    assert "_run_id" not in persisted


# ---------------------------------------------------------------------
# DATA-004 — failure path writes into the failed run's directory
# ---------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _ledger_tables():
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Project.__table__,
            ProductionRun.__table__,
            AssetDecision.__table__,
            ProjectRevision.__table__,
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
                script="Stage 4 test script.",
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
        db.query(AssetDecision).filter(AssetDecision.project_id == pid).delete()
        db.query(ProductionRun).filter(ProductionRun.project_id == pid).delete()
        db.query(Project).filter(Project.id == pid).delete()
        db.commit()
    finally:
        db.close()


def test_fail_pipeline_writes_into_the_failed_runs_dir(project_id, tmp_path):
    project_dir = tmp_path / project_id
    project_dir.mkdir()
    run_a = run_ledger.begin_run(project_id, "stage4 script a")
    run_b = run_ledger.begin_run(project_id, "stage4 script b")

    def _fail(run_id, message):
        state = {
            "_run_id": run_id,
            "_partial": {
                "processed_scenes": [],
                "analysis": {"mode": "degraded"},
                "timeline_result": None,
            },
        }
        with pytest.raises(RuntimeError):
            jobs.fail_pipeline(
                state, "asset_manager", RuntimeError(message), project_dir
            )

    _fail(run_a, "boom-a")
    report_a = media_paths.run_subdir(project_dir, run_a) / "asset_report.json"
    assert report_a.exists()
    bytes_a = report_a.read_bytes()
    parsed_a = json.loads(bytes_a)
    assert parsed_a["run_failure"] == {"stage": "asset_manager", "error": "boom-a"}

    _fail(run_b, "boom-b")
    # Run B's failure did not touch run A's failure record...
    assert report_a.read_bytes() == bytes_a
    # ...and no failure report leaked to the project level.
    assert not (project_dir / "asset_report.json").exists()
    report_b = media_paths.run_subdir(project_dir, run_b) / "asset_report.json"
    assert json.loads(report_b.read_bytes())["run_failure"]["error"] == "boom-b"


# ---------------------------------------------------------------------
# DATA-004 — timeline builder artefact_dir
# ---------------------------------------------------------------------

def _timeline_fixture(tmp_path, project_id):
    media_dir = tmp_path / "media" / project_id
    media_dir.mkdir(parents=True)
    segments = [
        {"start": 0.0, "end": 5.0, "text": "One."},
        {"start": 5.0, "end": 10.0, "text": "Two."},
    ]
    (media_dir / "captions.json").write_text(
        json.dumps({"duration": 10.0, "segments": segments})
    )
    visual_path = tmp_path / "visual.mp4"
    visual_path.write_bytes(b"\x00" * 4096)
    visual = {
        "file_path": str(visual_path), "path": str(visual_path),
        "type": "video", "title": "visual.mp4",
        "verification": {"accepted": True},
    }
    scenes = [{
        "scene": {"scene_number": 1},
        "scene_number": 1,
        "duration_seconds": 10,
        "videos": [visual],
        "images": [],
        "sentences": [
            {"sentence_number": 1, "sentence_text": "One.",
             "videos": [visual], "images": []},
            {"sentence_number": 2, "sentence_text": "Two.",
             "videos": [visual], "images": []},
        ],
    }]
    return media_dir, scenes


def test_timeline_written_into_run_artefact_dir(tmp_path):
    from services.timeline import build_project_timeline

    project_id = f"s4-{uuid.uuid4()}"
    media_dir, scenes = _timeline_fixture(tmp_path, project_id)
    run_dir = media_dir / "runs" / "run-t"
    result = build_project_timeline(
        project_id,
        scenes,
        output_dir=str(tmp_path / "media"),
        artefact_dir=run_dir,
    )
    assert result["timeline_file"] == str(run_dir / "timeline.json")
    assert (run_dir / "timeline.json").exists()
    assert not (media_dir / "timeline.json").exists()


def test_timeline_legacy_location_without_artefact_dir(tmp_path):
    from services.timeline import build_project_timeline

    project_id = f"s4-{uuid.uuid4()}"
    media_dir, scenes = _timeline_fixture(tmp_path, project_id)
    result = build_project_timeline(
        project_id, scenes, output_dir=str(tmp_path / "media")
    )
    assert result["timeline_file"] == str(media_dir / "timeline.json")
    assert (media_dir / "timeline.json").exists()


# ---------------------------------------------------------------------
# DATA-004 — readers serve the run they describe
# ---------------------------------------------------------------------

def test_quality_section_serves_the_certified_run(tmp_path, monkeypatch):
    import api_v1

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
        project_id = "proj-quality"
        project_dir = media_paths.project_dir(project_id)

        def _report(marker):
            return {
                "schema": 1,
                "verdicts": {"quality": {"verdict": marker}},
                "reconciliation": {
                    "all_reconciled": True,
                    "failures": [],
                    "checks": [],
                },
            }

        for run_id, marker in (("run-a", "PASS-A"), ("run-b", "FAIL-B")):
            run_dir = media_paths.run_subdir(project_dir, run_id)
            write_json_atomic(run_dir / "production_report.json", _report(marker))
            write_json_atomic(run_dir / "visual_quality.json", {})

        section_a = api_v1._quality_section(project_id, run_id="run-a")
        assert section_a["state"] == "recorded"
        assert section_a["verdicts"] == {"quality": {"verdict": "PASS-A"}}
        section_b = api_v1._quality_section(project_id, run_id="run-b")
        assert section_b["verdicts"] == {"quality": {"verdict": "FAIL-B"}}

        # Legacy fallback: a run with no scoped artefacts still
        # reads the project-level report.
        legacy_dir = media_paths.project_dir("proj-legacy")
        write_json_atomic(legacy_dir / "production_report.json", _report("LEGACY"))
        write_json_atomic(legacy_dir / "visual_quality.json", {})
        section = api_v1._quality_section("proj-legacy", run_id="run-a")
        assert section["state"] == "recorded"
        assert section["verdicts"] == {"quality": {"verdict": "LEGACY"}}


def test_media_index_resolves_run_scoped_outputs(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    run_dir = media_paths.run_subdir(project_dir, "run-m")
    film = run_dir / "documentary_final_output.mp4"
    film.parent.mkdir(parents=True, exist_ok=True)
    film.write_bytes(b"\x00" * 2048)
    write_json_atomic(
        run_dir / "pipeline_result.json",
        {"video": str(film), "timeline": str(run_dir / "timeline.json")},
    )
    write_json_atomic(run_dir / "asset_report.json", {"marker": "M"})

    index = plan_document.build_media_index("proj", project_dir)
    paths = {entry["path"] for entry in index.values()}
    assert film in paths
    assert run_dir / "asset_report.json" in paths
    film_entries = [
        entry
        for entry in index.values()
        if entry["file_name"] == "documentary_final_output.mp4"
    ]
    assert film_entries and all(
        entry["path"] == film for entry in film_entries
    )


def test_editor_import_uses_the_serving_runs_timeline(tmp_path, monkeypatch, project_id):
    from services import editor

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path / "media"))
        project_dir = media_paths.project_dir(project_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        run_a = run_ledger.begin_run(project_id, "stage4 import a")
        run_b = run_ledger.begin_run(project_id, "stage4 import b")
        # Only run A produced a timeline, in its own artefact dir.
        write_json_atomic(
            media_paths.run_subdir(project_dir, run_a) / "timeline.json",
            {"scenes": [], "items": []},
        )
        doc = editor.import_production(project_id, {"run_id": run_a})
        assert doc["run_id"] == run_a
        # Run B has no timeline of its own and must NOT silently
        # import run A's (the pre-Stage-4 project-level behaviour).
        with pytest.raises(editor.EditorError):
            editor.import_production(project_id, {"run_id": run_b})


def test_legacy_project_layout_still_measures(tmp_path):
    from services.visual_quality import measure_project

    project_dir = tmp_path / "legacy"
    project_dir.mkdir()
    write_json_atomic(
        project_dir / "timeline.json",
        {"project_id": "legacy-1", "items": [], "scenes": []},
    )
    write_json_atomic(project_dir / "asset_report.json", {})
    write_json_atomic(project_dir / "rights_manifest.json", [])
    result = measure_project(project_dir)
    assert result["project_id"] == "legacy-1"
