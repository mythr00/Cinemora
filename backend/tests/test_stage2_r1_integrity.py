"""Stage 2 (R1) — completion and startup integrity.

Covers the five R1 findings from the Master Architecture Review /
Ultimate Component Audit:

- INT-101: run_full_pipeline marked the project "completed" BEFORE
  run_ledger.finish_run closed the run; a crash in the window left
  a completed project behind a still-"running" run, which the next
  worker-start reconciliation then marked FAILED.
- INT-102: the completion gate consulted the final auditor's
  verdict only when it was evaluated, so the auditor's unevaluated
  shape (no asset report -> passed defaulted True) bypassed the
  gate and the run completed unaudited. The same shape sat in the
  editor's revision-export decision.
- BLOCK-001: services/llm_provider raised at IMPORT time when
  BREWKEG_API_KEY was unset, so the API (and every transitive
  importer) could not start without an LLM key.
- BLOCK-002: worker.py started without the IA-09 schema gate the
  API applies at startup (database.check_schema).
- PIPE-101: every enqueue passed at_front=True, making the queue
  LIFO instead of FIFO.

The INT-101 ordering invariant is asserted on the pipeline source
because run_full_pipeline is a monolith whose stages cannot be
driven in a unit test (precedent: inspect.getsource in
test_degraded_timeline.py); the invariant asserted is the exact
one the fix establishes — the ledger run is finished before any
"completed" marking in the success path.
"""

import inspect
import os
import subprocess
import sys

import pytest

import jobs
from services import final_video_auditor, llm_provider

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------
# INT-102 — the completion gate is total over verdict shapes
# ---------------------------------------------------------------------

def test_gate_allows_evaluated_pass():
    audit = {"verdict": {"evaluated": True, "passed": True, "failures": []}}
    # Phase 4: the gate returns the failure split (both classes
    # empty for a clean pass); existing callers ignore it.
    assert jobs._honour_final_audit(audit) == {"technical": [], "release": []}


def test_gate_blocks_evaluated_failure_with_reasons():
    # Phase 4: a TECHNICAL-class failure still blocks completion
    # with its reasons. (coverage_incomplete — this test's original
    # failure — is RELEASE class now and no longer blocks; that is
    # pinned by tests/test_phase4_gates_auditor.py.)
    audit = {
        "verdict": {
            "evaluated": True,
            "passed": False,
            "failures": [
                "missing_mandatory_evidence: 1 accepted footage "
                "asset(s) have no source URL"
            ],
        }
    }
    with pytest.raises(RuntimeError, match="Final video audit FAILED"):
        jobs._honour_final_audit(audit)


def test_gate_blocks_unevaluated_verdict_even_when_passed_defaults_true():
    # The exact INT-102 bypass shape: the auditor's verdict for a
    # missing report used to carry passed=True with evaluated=False,
    # and the old gate skipped it entirely.
    audit = {"verdict": {"evaluated": False, "passed": True, "failures": []}}
    with pytest.raises(RuntimeError, match="NOT EVALUATED"):
        jobs._honour_final_audit(audit)


def test_gate_blocks_missing_or_malformed_verdicts():
    for audit in (None, {}, {"verdict": None}, {"verdict": {}}, {"verdict": {"passed": True}}):
        with pytest.raises(RuntimeError, match="NOT EVALUATED"):
            jobs._honour_final_audit(audit)


def test_gate_blocks_evaluated_verdict_without_passed_flag():
    audit = {"verdict": {"evaluated": True, "failures": ["unspecified"]}}
    with pytest.raises(RuntimeError, match="Final video audit FAILED"):
        jobs._honour_final_audit(audit)


def test_pipeline_completion_path_uses_the_total_gate():
    source = inspect.getsource(jobs.run_full_pipeline)
    assert "_honour_final_audit(final_audit)" in source


# ---------------------------------------------------------------------
# INT-102 — the auditor's unevaluated shape is not a pass
# ---------------------------------------------------------------------

def test_unevaluated_verdict_shape_is_not_passed():
    for bad_report in (None, "not-a-report", 42):
        verdict = final_video_auditor._audit_verdict(bad_report)
        assert verdict["evaluated"] is False
        assert verdict["passed"] is False
        assert any(
            "audit_not_evaluated" in failure
            for failure in verdict["failures"]
        )


@pytest.fixture(scope="module")
def tiny_film(tmp_path_factory):
    folder = tmp_path_factory.mktemp("r1-film")
    film = folder / "film.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30:d=1",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-shortest", "-c:v", "libx264", "-c:a", "aac",
            str(film),
        ],
        check=True,
    )
    return film


def test_validate_final_video_without_report_is_unevaluated_not_passed(tiny_film):
    result = final_video_auditor.validate_final_video(str(tiny_film))
    assert result["verdict"]["evaluated"] is False
    assert result["verdict"]["passed"] is False


def test_editor_export_decision_treats_unevaluated_as_not_passed():
    # Source-level pin for the editor seam (services/editor.py):
    # the revision render's export decision must derive "passed"
    # from an evaluated verdict only.
    from services import editor

    source = inspect.getsource(editor)
    assert 'passed = evaluated and bool(verdict.get("passed", False))' in source
    assert 'passed = (not evaluated)' not in source


# ---------------------------------------------------------------------
# INT-101 — the ledger run finishes before completion is recorded
# ---------------------------------------------------------------------

def test_completion_ordering_finish_run_before_completed_marking():
    source = inspect.getsource(jobs.run_full_pipeline)
    finish_idx = source.rindex("run_ledger.finish_run(")
    state_idx = source.index('state["status"] = "completed"')
    project_idx = source.index('_set_project_status(project_id, "completed")')
    assert finish_idx < state_idx
    assert finish_idx < project_idx


# ---------------------------------------------------------------------
# BLOCK-001 — the LLM key is required at first use, not at import
# ---------------------------------------------------------------------

def _env_without_llm_key():
    env = dict(os.environ)
    env.pop("BREWKEG_API_KEY", None)
    return env


def test_llm_client_imports_without_key():
    completed = subprocess.run(
        [sys.executable, "-c", "import services.llm_provider; print('import-ok')"],
        cwd=BACKEND_DIR,
        env=_env_without_llm_key(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "import-ok" in completed.stdout


def test_llm_call_without_key_fails_at_use_with_clear_error():
    completed = subprocess.run(
        [
            sys.executable, "-c",
            "from services import llm_provider; llm_provider.complete_text('sys', 'user')",
        ],
        cwd=BACKEND_DIR,
        env=_env_without_llm_key(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode != 0
    assert "BREWKEG_API_KEY is not configured" in completed.stderr


def test_llm_request_without_key_raises_before_any_http(monkeypatch):
    monkeypatch.setattr(llm_provider, "BREWKEG_API_KEY", "")

    def forbidden_post(*args, **kwargs):
        raise AssertionError("HTTP must not be attempted without a key")

    monkeypatch.setattr(llm_provider.requests, "post", forbidden_post)
    with pytest.raises(RuntimeError, match="BREWKEG_API_KEY is not configured"):
        llm_provider._request("sys", "user")
    # get_llm() itself must not raise — only calling the returned
    # callable may fail.
    llm = llm_provider.get_llm()
    with pytest.raises(RuntimeError, match="BREWKEG_API_KEY is not configured"):
        llm("sys", "user")


# ---------------------------------------------------------------------
# BLOCK-002 — the worker applies the schema gate at startup
# ---------------------------------------------------------------------

import database  # noqa: E402
import worker  # noqa: E402


def test_worker_gate_fails_loud_when_schema_not_ready(monkeypatch):
    def failing_check():
        raise database.SchemaVersionError("missing table(s): projects")

    monkeypatch.setattr(database, "check_schema", failing_check)
    with pytest.raises(SystemExit) as excinfo:
        worker.ensure_schema_ready()
    assert excinfo.value.code == 1


def test_worker_gate_allows_ready_schema(monkeypatch):
    monkeypatch.setattr(
        database,
        "check_schema",
        lambda: {"status": "ok", "expected_head": "0005", "missing_tables": []},
    )
    result = worker.ensure_schema_ready()
    assert result["status"] == "ok"


def test_worker_gate_allows_unstamped_legacy_like_the_api(monkeypatch):
    monkeypatch.setattr(
        database,
        "check_schema",
        lambda: {
            "status": "unstamped_legacy",
            "current_revision": None,
            "expected_head": "0005",
            "missing_tables": [],
        },
    )
    result = worker.ensure_schema_ready()
    assert result["status"] == "unstamped_legacy"


def test_worker_process_refuses_to_start_on_unmigrated_database(tmp_path):
    empty_db = tmp_path / "unmigrated.sqlite3"
    empty_db.touch()
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{empty_db}"
    completed = subprocess.run(
        [sys.executable, "worker.py"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1
    assert "worker cannot start" in output
    assert "missing" in output


# ---------------------------------------------------------------------
# PIPE-101 — enqueues are FIFO (no at_front anywhere)
# ---------------------------------------------------------------------

class _RecordingQueue:
    """Stands in for jobs.video_queue and records enqueue calls."""

    def __init__(self):
        self.calls = []

    def enqueue(self, *args, **kwargs):
        self.calls.append((args, kwargs))

        class _Job:
            id = "recorded-job"

        return _Job()


def _record_enqueues(monkeypatch):
    queue = _RecordingQueue()
    monkeypatch.setattr(jobs, "video_queue", queue)
    monkeypatch.setattr(jobs, "video_queue_priority", queue)
    return queue


def _assert_fifo(queue):
    assert queue.calls, "expected at least one enqueue call"
    for _args, kwargs in queue.calls:
        assert "at_front" not in kwargs


def test_enqueue_test_job_is_fifo(monkeypatch):
    queue = _record_enqueues(monkeypatch)
    jobs.enqueue_test_job("project-1")
    _assert_fifo(queue)


def test_enqueue_full_pipeline_is_fifo(monkeypatch):
    queue = _record_enqueues(monkeypatch)
    monkeypatch.setattr(
        jobs.run_ledger,
        "submit_production",
        lambda *a, **k: {"run_id": "run-1", "deduplicated": False},
    )
    # Phase 8.4: a new submission enters the dispatcher backlog;
    # the promotion seam is stubbed to promote immediately through
    # the REAL jobs.enqueue_promoted_run, so this test keeps
    # asserting the enqueue discipline itself (no at_front, FIFO)
    # while dispatch policy is covered by
    # tests/test_phase8_concurrency.py.
    from types import SimpleNamespace

    from services import dispatcher

    monkeypatch.setattr(
        dispatcher,
        "dispatch_once",
        lambda: [
            (
                "run-1",
                jobs.enqueue_promoted_run(
                    SimpleNamespace(id="run-1", options={}),
                    SimpleNamespace(id="project-1", script="a script"),
                    False,
                ),
            )
        ],
    )
    jobs.enqueue_full_pipeline("project-1", "a script")
    _assert_fifo(queue)


def test_enqueue_analysis_phase_is_fifo(monkeypatch):
    queue = _record_enqueues(monkeypatch)
    jobs.enqueue_analysis_phase("project-1", "a script", run_id="run-1")
    _assert_fifo(queue)


def test_enqueue_revision_render_is_fifo(monkeypatch):
    queue = _record_enqueues(monkeypatch)
    jobs.enqueue_revision_render("project-1", "rev-1", "run-1")
    _assert_fifo(queue)


def test_enqueue_script_analysis_and_media_processing_are_fifo(monkeypatch):
    queue = _record_enqueues(monkeypatch)
    jobs.enqueue_script_analysis("project-1", "a script")
    jobs.enqueue_media_processing("project-1", [{"scene": 1}])
    _assert_fifo(queue)


def test_analyze_project_script_followup_enqueue_is_fifo(monkeypatch, tmp_path):
    queue = _record_enqueues(monkeypatch)
    monkeypatch.setattr(
        jobs,
        "build_production_scenes",
        lambda script, project_id="", project_dir=None: {
            "scenes": [{"scene": 1}],
            "case_bible_path": "",
        },
    )
    monkeypatch.setattr(
        jobs.media_paths, "project_dir", lambda project_id: tmp_path
    )
    jobs.analyze_project_script("project-1", "a script")
    _assert_fifo(queue)


def test_no_live_enqueue_site_passes_at_front():
    # Belt-and-braces source pin over the whole jobs module: the
    # only at_front mentions allowed are the PIPE-101 policy
    # comment at the queue definition.
    source = inspect.getsource(jobs)
    offenders = [
        line for line in source.splitlines()
        if "at_front=True" in line and not line.lstrip().startswith("#")
    ]
    assert offenders == []
