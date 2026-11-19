"""Stage 2G — robustness tests: portable process management (2G-a)
and the anchored media root (2G-b)."""

import inspect
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from services import media_paths, process_utils
from services import media_pipeline as mp
from services import timeline


# ----------------------------------------------------------------------
# 2G-a — process_utils
# ----------------------------------------------------------------------

def test_process_alive_for_current_process():
    assert process_utils.process_alive(os.getpid()) is True


def test_process_alive_rejects_invalid_pids():
    assert process_utils.process_alive(None) is False
    assert process_utils.process_alive(0) is False
    assert process_utils.process_alive(-1) is False


def test_process_alive_for_reaped_child_is_false():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    assert process_utils.process_alive(proc.pid) is False


def test_kill_process_tree_terminates_live_child():
    proc = subprocess.Popen(["sleep", "30"])
    try:
        assert process_utils.process_alive(proc.pid) is True
        process_utils.kill_process_tree(proc)
        deadline = time.time() + 5
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_kill_process_tree_never_raises():
    process_utils.kill_process_tree(None)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    process_utils.kill_process_tree(proc)  # already dead — no raise


def test_media_pipeline_kill_process_tree_delegates():
    proc = subprocess.Popen(["sleep", "30"])
    try:
        mp.kill_process_tree(proc)
        deadline = time.time() + 5
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# ----------------------------------------------------------------------
# 2G-b — media_paths
# ----------------------------------------------------------------------

def test_media_root_is_absolute_and_backend_anchored():
    root = media_paths.media_root()
    assert root.is_absolute()
    expected = Path(media_paths.__file__).resolve().parent.parent / "media"
    assert root == expected


def test_media_root_env_override_read_at_call_time(monkeypatch, tmp_path):
    override = tmp_path / "custom-media"
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(override))
    assert media_paths.media_root() == override
    assert media_paths.project_dir("p1") == override / "p1"
    monkeypatch.delenv("CINEMORA_MEDIA_ROOT")
    assert media_paths.media_root() != override


def test_project_dir_is_cwd_independent(monkeypatch, tmp_path):
    before = media_paths.project_dir("proj-9")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    after = media_paths.project_dir("proj-9")
    assert before == after
    assert after == media_paths.media_root() / "proj-9"
    assert after.is_absolute()


def test_timeline_defaults_resolve_to_media_root(monkeypatch, tmp_path):
    # Signature: default is None (meaning media_root()).
    assert inspect.signature(timeline.load_caption_data).parameters[
        "output_dir"
    ].default is None
    assert inspect.signature(timeline.build_project_timeline).parameters[
        "output_dir"
    ].default is None

    # Behaviour: with the env override, the default load reads from
    # media_root()/project_id without any explicit output_dir.
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(tmp_path))
    project = tmp_path / "proj-x"
    project.mkdir()
    (project / "captions.json").write_text(
        json.dumps(
            {
                "duration": 5,
                "segments": [
                    {"start": 0, "end": 5, "text": "hello", "words": []}
                ],
            }
        ),
        encoding="utf-8",
    )
    data = timeline.load_caption_data("proj-x")
    assert data["duration"] == 5.0
    assert data["segments"][0]["text"] == "hello"

    # Explicit string output_dir is still honoured.
    other = tmp_path / "other"
    (other / "proj-y").mkdir(parents=True)
    (other / "proj-y" / "captions.json").write_text(
        json.dumps({"duration": 9, "segments": []}), encoding="utf-8"
    )
    data = timeline.load_caption_data("proj-y", output_dir=str(other))
    assert data["duration"] == 9.0
