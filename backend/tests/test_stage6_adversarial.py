"""Stage 6 — ADVERSARIAL verification of the editor backend.

Written to BREAK the editor's guarantees, not to repeat the happy
paths of test_stage6_editor.py. Each test names the guarantee under
attack. The three defects this pass originally proved (D1
revision-0 race, D2 branch export inheritance, D3 run-certificate
absorption) were encoded as xfail(strict=False) while unfixed;
all three are now FIXED and their tests flipped to strict passing
assertions — everything in this file is a hard assertion.

Guarantees under attack:
 1. concurrent revision edits / concurrent revision-0 creation
 2. interrupted renders (cancel-before-claim, worker death, SIGKILL)
 3. revision recovery (crash between document write and DB insert,
    orphaned document files, chain reopen)
 4. timeline timing integrity under a seeded operation fuzz
 5. audio/caption synchronisation (dirty/stale flags, sidecar
    agreement, narration part reuse)
 6. replacement evidence (fresh probe, no inherited verdict,
    atomic failure, filename mimicry)
 7. rights classification sweep across every add path
 8. revision-specific provenance certificates
 9. immutability of the production and of earlier revisions
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import stage6_fixture as fx


# ---------------------------------------------------------------------
# Fixtures: three synthetic productions in ONE media root (the media
# root is a process-wide env var, so all projects share it).
# ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def media_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("stage6-adv-media")
    os.environ["CINEMORA_MEDIA_ROOT"] = str(root)
    return root


@pytest.fixture(scope="module")
def production(media_root):
    built = fx.build_synthetic_production(media_root, "proj-adv-main")
    built["root"] = media_root
    built["original_hashes"] = fx.hash_tree(built["project_dir"])
    return built


@pytest.fixture(scope="module")
def race_production(media_root):
    built = fx.build_synthetic_production(media_root, "proj-adv-race")
    built["root"] = media_root
    built["original_hashes"] = fx.hash_tree(built["project_dir"])
    return built


@pytest.fixture(scope="module")
def race0_production(media_root):
    built = fx.build_synthetic_production(media_root, "proj-adv-race0")
    built["root"] = media_root
    built["original_hashes"] = fx.hash_tree(built["project_dir"])
    return built


@pytest.fixture(scope="module")
def render_production(media_root):
    built = fx.build_synthetic_production(media_root, "proj-adv-render")
    built["root"] = media_root
    built["original_hashes"] = fx.hash_tree(built["project_dir"])
    return built


class SyncQueue:
    """Executes enqueued jobs immediately (worker stand-in)."""

    def enqueue(self, fn, *args, **kwargs):
        from types import SimpleNamespace

        kwargs.pop("at_front", None)
        # RQ consumes its job-option kwargs itself; the job
        # function never sees them (Phase 8.4 added job_timeout).
        for _opt in ("job_timeout", "job_id", "job_ttl", "result_ttl",
                     "failure_ttl", "depends_on", "job_description"):
            kwargs.pop(_opt, None)
        fn(*args, **kwargs)
        return SimpleNamespace(id="job-sync")


class RecordingQueue:
    """Records enqueued jobs without executing them."""

    def __init__(self):
        self.calls = []

    def enqueue(self, fn, *args, **kwargs):
        from types import SimpleNamespace

        self.calls.append((fn, args, kwargs))
        return SimpleNamespace(id="job-recorded")


@pytest.fixture()
def sync_queue(monkeypatch):
    import jobs

    _paired_queue = SyncQueue()
    monkeypatch.setattr(jobs, "video_queue", _paired_queue)
    monkeypatch.setattr(jobs, "video_queue_priority", _paired_queue)
    return jobs.video_queue


@pytest.fixture()
def recording_queue(monkeypatch):
    import jobs

    queue = RecordingQueue()
    monkeypatch.setattr(jobs, "video_queue", queue)
    monkeypatch.setattr(jobs, "video_queue_priority", queue)
    return queue


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _rev0(project_id):
    from services import editor

    editor.get_editor(project_id)
    for view in editor.list_revisions(project_id):
        if view["revision_number"] == 0 and view["parent_revision_id"] is None:
            return editor.get_revision(project_id, view["revision_id"])
    raise AssertionError("no revision 0")


def _items(doc, sentence=None):
    items = doc["tracks"]["visual"]
    if sentence is not None:
        items = [i for i in items if i["sentence_number"] == sentence]
    return items


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _doc_bytes(project_dir, revision_id) -> bytes:
    return (Path(project_dir) / f"revision_{revision_id}.json").read_bytes()


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def _originals_unchanged(built):
    current = fx.hash_tree(built["project_dir"])
    for relative, digest in built["original_hashes"].items():
        assert current.get(relative) == digest, (
            f"production artefact changed: {relative}"
        )


def _race(fn, n=6):
    """Run fn(i) from n threads released simultaneously; return a
    list of ("ok", value) / ("err", exception) outcomes."""
    barrier = threading.Barrier(n)
    outcomes = []

    def worker(i):
        barrier.wait()
        try:
            outcomes.append(("ok", fn(i)))
        except Exception as error:  # classified by the caller
            outcomes.append(("err", error))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


# ---------------------------------------------------------------------
# 1. Concurrent revision edits
# ---------------------------------------------------------------------


def test_race_apply_operations_same_parent(race_production):
    """Six threads apply distinct valid batches to the SAME parent.

    Hard guarantees: no exception other than EditorError may escape;
    every success is a distinct revision whose document file exists
    and parses; DB rows == document files; every child's parent link
    points at the raced parent; latest_revision is deterministic
    across reads. Characterised (asserted as observed behaviour):
    all children receive the SAME revision_number (parent + 1) —
    numbering is per-parent depth, not a project-wide sequence.
    """
    from services import editor

    view0, _doc0 = _rev0(race_production["project_id"])
    outcomes = _race(
        lambda i: editor.apply_operations(
            race_production["project_id"],
            view0["revision_id"],
            [{"type": "set_volumes", "music": round(0.11 + 0.03 * i, 2)}],
        )[0]
    )
    oks = [value for status, value in outcomes if status == "ok"]
    errs = [value for status, value in outcomes if status == "err"]
    assert all(isinstance(e, editor.EditorError) for e in errs), (
        f"non-EditorError escaped apply_operations: {errs!r}"
    )
    assert len(oks) == 6, f"expected all six batches to land: {errs!r}"
    assert len({v["revision_id"] for v in oks}) == 6

    project_dir = race_production["project_dir"]
    for view in oks:
        raw = _doc_bytes(project_dir, view["revision_id"])
        doc = json.loads(raw)
        assert doc["revision_id"] == view["revision_id"]
        assert doc["parent_revision_id"] == view0["revision_id"]
        assert view["parent_revision_id"] == view0["revision_id"]

    rows = editor.list_revisions(race_production["project_id"])
    doc_files = list(project_dir.glob("revision_*.json"))
    assert len(rows) == len(doc_files) == 7  # rev0 + six children

    # Characterisation: revision_number collides across the branch.
    assert {v["revision_number"] for v in oks} == {1}

    latest_ids = {
        editor.latest_revision(race_production["project_id"])[0]["revision_id"]
        for _ in range(5)
    }
    assert len(latest_ids) == 1
    assert latest_ids.pop() in {v["revision_id"] for v in oks}


def test_exactly_one_revision_zero_under_race(race0_production):
    """FIXED (defect D1): concurrent get_editor/create_revision_zero
    raced check-then-insert and created one revision-0 root PER
    THREAD (6 threads -> 6 roots). The single-root partial unique
    index (migration 0005, mirrored in models.ProjectRevision) now
    lets exactly one creator insert the root; the losers' inserts
    fail with an integrity error and create_revision_zero returns
    the winner's root instead. This test is the strict proof:
    exactly one root, and every racer observes that same root."""
    from services import editor

    outcomes = _race(
        lambda i: editor.get_editor(race0_production["project_id"])[0]
    )
    errs = [value for status, value in outcomes if status == "err"]
    assert all(isinstance(e, editor.EditorError) for e in errs), (
        f"non-EditorError escaped get_editor: {errs!r}"
    )
    assert not errs, f"racing get_editor calls failed: {errs!r}"
    roots = [
        r for r in editor.list_revisions(race0_production["project_id"])
        if r["revision_number"] == 0
    ]
    assert len(roots) == 1, (
        f"{len(roots)} revision-0 rows were created by the race"
    )
    oks = [value for status, value in outcomes if status == "ok"]
    assert {v["revision_id"] for v in oks} == {roots[0]["revision_id"]}


def test_race_revision_zero_no_corruption(race0_production):
    """Whatever the rev-0 race produced, the store stays coherent:
    rows == files, every document parses, every root imports the
    same run, and get_editor returns a readable revision."""
    from services import editor

    # Ensure the race above (or a plain first touch) has happened.
    editor.get_editor(race0_production["project_id"])
    rows = editor.list_revisions(race0_production["project_id"])
    doc_files = list(race0_production["project_dir"].glob("revision_*.json"))
    assert len(rows) == len(doc_files)
    for row in rows:
        doc = json.loads(_doc_bytes(
            race0_production["project_dir"], row["revision_id"]
        ))
        assert doc["run_id"] == race0_production["run_id"]
        assert len(doc["tracks"]["visual"]) == 4
    view, doc = editor.get_editor(race0_production["project_id"])
    assert view["revision_id"] in {r["revision_id"] for r in rows}


# ---------------------------------------------------------------------
# 2. Interrupted renders
# ---------------------------------------------------------------------


def test_cancel_before_claim_then_recover(
    render_production, recording_queue
):
    """(a) Cancel a render before the worker claims it: revision
    returns to draft, no output file, no export — and the same
    revision renders successfully afterwards."""
    from services import editor, run_ledger

    view0, doc0 = _rev0(render_production["project_id"])
    result = editor.request_render(
        render_production["project_id"], view0["revision_id"]
    )
    run_id = result["run_id"]
    run_ledger.request_cancel(run_id)
    outcome = editor.run_revision_render(
        render_production["project_id"], view0["revision_id"], run_id
    )
    assert outcome["status"] == "cancelled"
    view_after, doc_after = editor.get_revision(
        render_production["project_id"], view0["revision_id"]
    )
    assert view_after["status"] == "draft"
    assert doc_after["exports"] == []
    final = (
        render_production["project_dir"]
        / f"revision_{view0['revision_id']}_final.mp4"
    )
    assert not final.exists()

    # Recovery: the cancelled revision is not wedged.
    import jobs

    jobs.video_queue = jobs.video_queue_priority = SyncQueue()
    editor.request_render(render_production["project_id"], view0["revision_id"])
    view_done, doc_done = editor.get_revision(
        render_production["project_id"], view0["revision_id"]
    )
    assert view_done["status"] == "exported"
    assert len(doc_done["exports"]) == 1
    assert final.exists()
    _originals_unchanged(render_production)


def test_worker_death_mid_render_reconcile_and_rerender(
    render_production, recording_queue
):
    """(b) A worker claims a render run and dies. After stale-run
    reconciliation the revision must read as failed (never stuck at
    'rendering'), hold no export record, and render cleanly on a
    fresh request."""
    from services import editor, run_ledger

    view0, _doc0 = _rev0(render_production["project_id"])
    branch, branch_doc = editor.create_revision(
        render_production["project_id"],
        from_revision_id=view0["revision_id"],
    )
    # The branch starts with an empty export history (defect D2,
    # fixed — see test_branch_from_exported_revision_inherits_exports);
    # the interrupted-render guarantee is that no NEW export appears.
    inherited_exports = list(branch_doc["exports"])
    result = editor.request_render(
        render_production["project_id"], branch["revision_id"]
    )
    run_id = result["run_id"]
    claimed = run_ledger.claim_run(run_id)
    assert claimed is not None and claimed["status"] == "running"
    # The worker dies here: no finish_run, no status write-back.

    reconciled = run_ledger.reconcile_stale_runs()
    assert reconciled >= 1
    run_after = run_ledger.get_run(run_id)
    assert run_after["status"] in ("failed", "interrupted")

    view_after, doc_after = editor.get_revision(
        render_production["project_id"], branch["revision_id"]
    )
    assert view_after["status"] == "failed"
    assert doc_after["exports"] == inherited_exports
    final = (
        render_production["project_dir"]
        / f"revision_{branch['revision_id']}_final.mp4"
    )
    assert not final.exists()

    import jobs

    jobs.video_queue = jobs.video_queue_priority = SyncQueue()
    editor.request_render(render_production["project_id"], branch["revision_id"])
    view_done, doc_done = editor.get_revision(
        render_production["project_id"], branch["revision_id"]
    )
    assert view_done["status"] == "exported"
    assert len(doc_done["exports"]) == len(inherited_exports) + 1
    assert final.exists()
    _originals_unchanged(render_production)


def test_kill_render_subprocess_mid_flight(
    render_production, recording_queue, tmp_path
):
    """(c) SIGKILL a real render worker mid-render (its
    render_timeline is slowed so the kill lands mid-flight). The
    revision must reconcile to a consistent state with no export
    recorded, and a fresh render must succeed."""
    from services import editor, run_ledger

    view0, _doc0 = _rev0(render_production["project_id"])
    branch, branch_doc = editor.create_revision(
        render_production["project_id"],
        from_revision_id=view0["revision_id"],
    )
    inherited_exports = list(branch_doc["exports"])
    marker = tmp_path / "render_started.txt"
    script = tmp_path / "kill_worker.py"
    backend = Path(__file__).resolve().parent.parent
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(backend)!r})\n"
        "import jobs\n"
        "from services import editor, renderer\n"
        "\n"
        "class RecQ:\n"
        "    def enqueue(self, fn, *a, **k):\n"
        "        from types import SimpleNamespace\n"
        "        return SimpleNamespace(id='rec')\n"
        "\n"
        "jobs.video_queue = jobs.video_queue_priority = RecQ()\n"
        "_orig = renderer.render_timeline\n"
        "def slow(scenes, work_dir, **kw):\n"
        f"    open({str(marker)!r}, 'w').write('started')\n"
        "    time.sleep(120)\n"
        "    return _orig(scenes, work_dir, **kw)\n"
        "renderer.render_timeline = slow\n"
        f"res = editor.request_render({render_production['project_id']!r}, "
        f"{branch['revision_id']!r})\n"
        "editor.run_revision_render("
        f"{render_production['project_id']!r}, {branch['revision_id']!r}, "
        "res['run_id'])\n"
    )
    env = dict(os.environ)
    env["CINEMORA_MEDIA_ROOT"] = str(render_production["root"])
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 120
        while time.time() < deadline and not marker.exists():
            if proc.poll() is not None:
                raise AssertionError(
                    "render worker exited before the render started: "
                    + (proc.stdout.read() if proc.stdout else "")
                )
            time.sleep(0.25)
        assert marker.exists(), "render never reached the visual phase"
        runs = [
            r for r in run_ledger.list_runs_for(render_production["project_id"])
            if (r.get("options") or {}).get("revision_id")
            == branch["revision_id"]
        ]
        assert runs and runs[0]["status"] == "running"
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)

    reconciled = run_ledger.reconcile_stale_runs()
    assert reconciled >= 1
    view_after, doc_after = editor.get_revision(
        render_production["project_id"], branch["revision_id"]
    )
    assert view_after["status"] == "failed"
    assert doc_after["exports"] == inherited_exports
    # No partial output may be recorded as an export; in this kill
    # window (during visual render, before publish) none exists.
    final = (
        render_production["project_dir"]
        / f"revision_{branch['revision_id']}_final.mp4"
    )
    assert not final.exists()

    import jobs

    jobs.video_queue = jobs.video_queue_priority = SyncQueue()
    editor.request_render(render_production["project_id"], branch["revision_id"])
    view_done, doc_done = editor.get_revision(
        render_production["project_id"], branch["revision_id"]
    )
    assert view_done["status"] == "exported"
    assert len(doc_done["exports"]) == len(inherited_exports) + 1
    assert final.exists()
    _originals_unchanged(render_production)


def test_branch_from_exported_revision_inherits_exports(
    render_production, monkeypatch
):
    """FIXED (defect D2): editor.create_revision deep-copied the
    parent document without resetting its exports list, so a branch
    of an EXPORTED revision claimed the parent's export records
    (pointing at the parent's output file) while its own status was
    'draft'. create_revision now resets exports alongside
    render_state, exactly like apply_operations. A fresh branch of
    an exported revision starts with an empty export history."""
    import jobs
    from services import editor

    view0, doc0 = _rev0(render_production["project_id"])
    if not doc0["exports"]:
        _paired_queue = SyncQueue()
        monkeypatch.setattr(jobs, "video_queue", _paired_queue)
        monkeypatch.setattr(jobs, "video_queue_priority", _paired_queue)
        editor.request_render(
            render_production["project_id"], view0["revision_id"]
        )
        view0, doc0 = editor.get_revision(
            render_production["project_id"], view0["revision_id"]
        )
    assert doc0["exports"], "precondition: rev0 is exported"
    _branch, branch_doc = editor.create_revision(
        render_production["project_id"],
        from_revision_id=view0["revision_id"],
    )
    assert branch_doc["exports"] == [], (
        f"branch inherited {len(branch_doc['exports'])} export "
        "record(s) from its parent"
    )


# ---------------------------------------------------------------------
# 3. Revision recovery
# ---------------------------------------------------------------------


def test_crash_between_doc_write_and_db_insert(production, monkeypatch):
    """Force the DB insert of _persist_new_revision to fail after
    the document file was written: the orphaned file must be
    removed again and no row may exist."""
    import models
    from services import editor

    class ExplodingRevision:
        def __init__(self, **kwargs):
            raise RuntimeError("simulated DB failure after doc write")

    monkeypatch.setattr(models, "ProjectRevision", ExplodingRevision)
    from uuid import uuid4

    revision_id = str(uuid4())
    doc = {
        "revision_id": revision_id,
        "revision_number": 99,
        "status": "draft",
    }
    with pytest.raises(RuntimeError):
        editor._persist_new_revision(
            production["project_id"], production["run_id"], None, doc
        )
    monkeypatch.undo()
    orphan = production["project_dir"] / f"revision_{revision_id}.json"
    assert not orphan.exists()
    assert all(
        r["revision_id"] != revision_id
        for r in editor.list_revisions(production["project_id"])
    )


def test_orphan_document_file_does_not_corrupt_reads(production):
    """A document file with no DB row (the kill -9 window the
    cleanup path cannot cover) must be ignored by every read path:
    the DB is the index of record."""
    from services import editor
    from uuid import uuid4

    latest_before, _ = editor.get_editor(production["project_id"])
    before = editor.list_revisions(production["project_id"])
    orphan_id = str(uuid4())
    orphan = production["project_dir"] / f"revision_{orphan_id}.json"
    orphan.write_text(json.dumps({
        "revision_id": orphan_id,
        "revision_number": 42,
        "status": "draft",
        "tracks": {"visual": [], "narration": {"sentences": []},
                   "music": {}, "captions": {"segments": []}},
    }))
    try:
        after = editor.list_revisions(production["project_id"])
        assert [r["revision_id"] for r in after] == [
            r["revision_id"] for r in before
        ]
        latest_after, _doc = editor.get_editor(production["project_id"])
        assert latest_after["revision_id"] == latest_before["revision_id"]
        with pytest.raises(editor.EditorError) as error:
            editor.get_revision(production["project_id"], orphan_id)
        assert error.value.status_code == 404
    finally:
        orphan.unlink()


def test_chain_reopen_intact(production):
    """A 3-deep chain reopens with every node readable, parent
    links exact, chain ids ordered, op logs growing."""
    from services import editor

    view0, _doc0 = _rev0(production["project_id"])
    parent = view0
    chain = [view0["revision_id"]]
    for i in range(3):
        parent, _doc = editor.apply_operations(
            production["project_id"],
            parent["revision_id"],
            [{"type": "set_volumes", "music": round(0.3 + 0.05 * i, 2)}],
        )
        chain.append(parent["revision_id"])
    assert editor.revision_chain_ids(
        production["project_id"], chain[-1]
    ) == chain
    op_log_lengths = []
    for revision_id in chain:
        _view, doc = editor.get_revision(production["project_id"], revision_id)
        op_log_lengths.append(len(doc["op_log"]))
    assert op_log_lengths == sorted(op_log_lengths)
    assert len(set(op_log_lengths)) == len(op_log_lengths)
    latest, _doc = editor.get_editor(production["project_id"])
    assert latest["revision_id"] == chain[-1]


# ---------------------------------------------------------------------
# 4. Timeline timing integrity — seeded fuzz
# ---------------------------------------------------------------------


def test_timing_integrity_fuzz(production):
    """~30 randomised valid batches; after EVERY batch the whole
    document must satisfy: _check_window_conservation is silent,
    per-window items are sequential and non-overlapping (each
    start == previous end), durations positive, window sums equal
    the narration windows, and total duration == narration total.
    A rejected batch (422) must leave the document untouched."""
    from services import editor

    rng = random.Random(20261119)
    view, doc = _rev0(production["project_id"])
    windows = {
        s["sentence_number"]: (s["start"], s["end"])
        for s in doc["tracks"]["narration"]["sentences"]
    }
    total_duration = production["total_duration"]
    successes = 0
    rejections = 0

    def check_invariants(current_doc):
        assert editor._check_window_conservation(current_doc) == []
        for number, (w_start, w_end) in windows.items():
            items = _items(current_doc, number)
            assert items, f"window {number} lost all items"
            cursor = w_start
            for item in items:
                assert float(item["duration"]) > 0
                assert abs(float(item["start"]) - cursor) < 0.01, (
                    f"window {number}: item starts at {item['start']} "
                    f"but previous item ended at {cursor}"
                )
                cursor = float(item["end"])
            assert abs(cursor - w_end) < 0.05
        assert abs(current_doc["duration_seconds"] - total_duration) < 0.05

    def gen_ops(current_doc):
        ops = []
        visual = _items(current_doc)
        non_gap = [i for i in visual if i["kind"] != "gap"]
        gaps = [i for i in visual if i["kind"] == "gap"]
        choice = rng.random()
        if choice < 0.22 and non_gap:
            item = rng.choice(non_gap)
            ops.append({
                "type": "trim_clip", "item_id": item["item_id"],
                "duration": round(
                    max(0.3, float(item["duration"]) * rng.uniform(0.45, 0.85)), 3
                ),
            })
        elif choice < 0.40 and non_gap:
            item = rng.choice(non_gap)
            ops.append({
                "type": "remove_clip", "item_id": item["item_id"],
                "gap_policy": rng.choice(["leave_gap", "close_gap"]),
            })
        elif choice < 0.55:
            candidates = [
                i for i in non_gap if float(i["duration"]) > 0.8
            ]
            if candidates:
                item = rng.choice(candidates)
                ops.append({
                    "type": "split_clip", "item_id": item["item_id"],
                    "at_offset": round(
                        float(item["duration"]) * rng.uniform(0.35, 0.65), 3
                    ),
                })
        elif choice < 0.68:
            # Move within a window (anchor in the same sentence).
            moved = False
            for number in windows:
                window_items = [
                    i for i in _items(current_doc, number)
                    if i["kind"] != "gap"
                ]
                if len(window_items) >= 2:
                    item, anchor = rng.sample(window_items, 2)
                    ops.append({
                        "type": "move_clip", "item_id": item["item_id"],
                        "after_item_id": anchor["item_id"],
                    })
                    moved = True
                    break
            if not moved:
                ops.append({"type": "set_volumes",
                            "music": round(rng.uniform(0.05, 0.9), 2)})
        elif choice < 0.82 and gaps:
            gap = max(gaps, key=lambda g: float(g["duration"]))
            if float(gap["duration"]) > 0.4:
                clip_b = next(
                    i for i in _items(current_doc, 2)
                    if i["asset"] and i["asset"]["file_path"].endswith("clip_b.mp4")
                ) if any(
                    i["asset"] and i["asset"]["file_path"].endswith("clip_b.mp4")
                    for i in _items(current_doc, 2)
                ) else None
                decision_id = (
                    clip_b["asset"]["decision_id"] if clip_b else None
                )
                if decision_id:
                    ops.append({
                        "type": "add_clip",
                        "asset": {"library_decision_id": decision_id},
                        "sentence_number": gap["sentence_number"],
                        "duration": round(
                            min(float(gap["duration"]) - 0.05,
                                rng.uniform(0.3, 0.6)), 3
                        ),
                    })
        elif choice < 0.92:
            graphics = [i for i in visual if i["kind"] == "graphic"]
            if graphics:
                graphic = graphics[0]
                ops.append({
                    "type": "set_graphic_duration",
                    "item_id": graphic["item_id"],
                    "duration": round(
                        max(0.3, float(graphic["duration"])
                            - rng.uniform(0.2, 0.8)), 3
                    ),
                })
        if not ops or rng.random() < 0.35:
            ops.append({"type": "set_volumes",
                        "narration": round(rng.uniform(0.5, 1.0), 2)})
        return ops

    for _batch in range(30):
        ops = gen_ops(doc)
        try:
            view, doc = editor.apply_operations(
                production["project_id"], view["revision_id"], ops
            )
            successes += 1
        except editor.EditorError as error:
            assert error.status_code == 422, (
                f"fuzz batch rejected with non-422: {error.status_code} "
                f"{error.code}: {ops}"
            )
            rejections += 1
            # Rejection is atomic: the current revision is unchanged.
            _view, doc = editor.get_revision(
                production["project_id"], view["revision_id"]
            )
        check_invariants(doc)
    assert successes >= 20, (
        f"fuzz was nearly vacuous: {successes} successes, "
        f"{rejections} rejections"
    )


# ---------------------------------------------------------------------
# 5. Audio / caption synchronisation
# ---------------------------------------------------------------------


def test_narration_regen_marks_dirty_and_captions_stale(production):
    """Regenerating one sentence must flag that sentence dirty and
    the captions stale — never silently in sync — and the render
    plan must rebuild narration, captions and soundtrack."""
    from services import editor

    _view0, doc0 = _rev0(production["project_id"])
    view1, doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [{"type": "regenerate_narration_sentence", "sentence_number": 2,
          "text": "By midday the convoy crossed the suspension bridge."}],
    )
    narration = doc1["tracks"]["narration"]
    assert narration["dirty_sentences"] == [2]
    sentences = {s["sentence_number"]: s for s in narration["sentences"]}
    assert "suspension bridge" in sentences[2]["text"]
    assert sentences[1]["text"] == production["windows"][0]["text"]
    assert sentences[3]["text"] == production["windows"][2]["text"]
    captions = doc1["tracks"]["captions"]
    assert captions["stale"] is True
    assert "2" in (captions["stale_reason"] or "")
    plan = editor.compute_render_plan(doc0, doc1)
    assert plan["narration_rebuild"] is True
    assert plan["captions_rebuild"] is True
    assert plan["soundtrack_rebuild"] is True
    assert plan["narration_dirty_sentences"] == [2]


def test_edit_caption_render_sidecars_agree(production, sync_queue):
    """An edited caption (text + timing shift) must be what the
    render actually used: JSON, SRT, VTT and the burned ASS in the
    render work dir all agree on text and timing."""
    from services import editor

    _view0, doc0 = _rev0(production["project_id"])
    segment0 = doc0["tracks"]["captions"]["segments"][0]
    new_start = round(float(segment0["start"]) + 0.2, 3)
    new_end = round(float(segment0["end"]) + 0.2, 3)
    view1, _doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [{"type": "edit_caption", "segment_index": 0,
          "text": "ADV EDITED CAPTION", "start": new_start, "end": new_end}],
    )
    editor.request_render(production["project_id"], view1["revision_id"])
    view_after, _doc_after = editor.get_revision(
        production["project_id"], view1["revision_id"]
    )
    assert view_after["status"] == "exported"
    work = (
        production["project_dir"] / "editor_renders" / view1["revision_id"]
    )
    captions = json.loads((work / "captions.json").read_text())
    seg = captions["segments"][0]
    assert seg["text"] == "ADV EDITED CAPTION"
    assert seg["edited"] is True
    assert abs(seg["start"] - new_start) < 0.001
    assert abs(seg["end"] - new_end) < 0.001

    def parse_sidecar(path, separator):
        blocks = []
        for block in (work / path).read_text().split("\n\n"):
            lines = [ln for ln in block.strip().splitlines() if "-->" in ln
                     or (ln and not ln[0].isdigit() and ln != "WEBVTT")]
            timing = [ln for ln in block.splitlines() if "-->" in ln]
            texts = [
                ln for ln in block.splitlines()
                if "-->" not in ln and ln.strip()
                and not ln.strip().isdigit() and ln.strip() != "WEBVTT"
            ]
            if timing:
                start_s, end_s = timing[0].split(" --> ")
                def to_seconds(value):
                    value = value.replace(",", ".")
                    h, m, s = value.split(":")
                    return int(h) * 3600 + int(m) * 60 + float(s)
                blocks.append((to_seconds(start_s), to_seconds(end_s),
                               " ".join(texts)))
        return blocks

    srt = parse_sidecar("captions.srt", ",")
    vtt = parse_sidecar("captions.vtt", ".")
    assert srt[0][2] == "ADV EDITED CAPTION"
    assert vtt[0][2] == "ADV EDITED CAPTION"
    assert abs(srt[0][0] - new_start) < 0.01
    assert abs(srt[0][1] - new_end) < 0.01
    assert abs(vtt[0][0] - new_start) < 0.01
    assert abs(vtt[0][1] - new_end) < 0.01
    ass = (work / "captions.ass").read_text()
    assert "ADV EDITED CAPTION" in ass
    _originals_unchanged(production)


def test_narration_rebuild_reuses_untouched_parts(production, sync_queue):
    """After a one-sentence narration rebuild, the untouched
    sentences' audio parts are byte-identical to the production's
    (content-hash reuse), the regenerated part differs, and the
    document no longer claims dirty/stale state."""
    from services import editor

    _view0, doc0 = _rev0(production["project_id"])
    view1, _doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [{"type": "regenerate_narration_sentence", "sentence_number": 2,
          "text": "By midday the convoy crossed the suspension bridge."}],
    )
    editor.request_render(production["project_id"], view1["revision_id"])
    view_after, doc_after = editor.get_revision(
        production["project_id"], view1["revision_id"]
    )
    assert view_after["status"] == "exported"
    assert doc_after["tracks"]["narration"]["dirty_sentences"] == []
    assert doc_after["tracks"]["captions"]["stale"] is False
    segments = doc_after["tracks"]["captions"]["segments"]
    assert segments, "captions were not rebuilt"
    if doc_after["tracks"]["captions"].get("timing_source") == "proportional":
        joined = " ".join(s.get("text", "") for s in segments)
        assert "suspension" in joined

    production_parts = production["project_dir"] / "narration_parts"
    work_parts = (
        production["project_dir"] / "editor_renders"
        / view1["revision_id"] / "narration_parts"
    )
    prod_manifest = json.loads(
        (production_parts / "parts_manifest.json").read_text()
    )
    work_manifest = json.loads(
        (work_parts / "parts_manifest.json").read_text()
    )
    prod_by_index = {
        p["index"]: production_parts / p["file"]
        for p in prod_manifest["parts"]
    }
    work_by_index = {
        p["index"]: work_parts / p["file"] for p in work_manifest["parts"]
    }
    assert work_by_index[0].read_bytes() == prod_by_index[0].read_bytes()
    assert work_by_index[2].read_bytes() == prod_by_index[2].read_bytes()
    assert work_by_index[1].read_bytes() != prod_by_index[1].read_bytes()
    _originals_unchanged(production)


# ---------------------------------------------------------------------
# 6. Replacement evidence attacks
# ---------------------------------------------------------------------


def _wide_replacement(production):
    path = production["project_dir"] / "adv_wide.mp4"
    if not path.exists():
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "testsrc2=size=1280x720:rate=30",
                "-t", "10", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", str(path),
            ],
            check=True, capture_output=True,
        )
    return path


def _clip_b(doc):
    return next(
        i for i in _items(doc, 2)
        if i["asset"] and i["asset"]["file_path"].endswith("clip_b.mp4")
    )


def test_replace_different_duration_resolution_fresh_probe(production):
    """Replace a verified 640x360/6s clip with a 1280x720/10s file:
    the new item must carry the NEW file's probe data, cleared
    segment evidence, unknown rights, and a verdict that is NOT the
    original's; the original ledger row stays byte-identical."""
    from services import editor, run_ledger

    wide = _wide_replacement(production)
    _view0, doc0 = _rev0(production["project_id"])
    clip_b = _clip_b(doc0)
    original_decision_id = clip_b["asset"]["decision_id"]
    original_verdict = clip_b["asset"]["verification"]
    rows_before, _ = run_ledger.list_decisions(production["run_id"], 1000, 0)
    original_row_before = next(
        r for r in rows_before if r["id"] == original_decision_id
    )

    view1, doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [{"type": "replace_clip", "item_id": clip_b["item_id"],
          "replacement": {"file_path": str(wide)}}],
    )
    item = next(i for i in _items(doc1, 2) if i["kind"] != "gap")
    asset = item["asset"]
    assert asset["file_path"].endswith("adv_wide.mp4")
    # Fresh probe of the NEW file (10s), not the original's data.
    assert abs(float(asset["source_duration"]) - 10.0) < 0.2
    assert asset["evidence_class"] == "none"
    assert asset["verification"] != original_verdict
    assert asset["verification"]["accepted"] is False
    assert (
        asset["verification"].get("rejection_reason")
        == "manual_swap_unverified"
    )
    assert asset["rights"]["license"] == "unknown"
    assert asset["rights"]["rights_status"] == "verify_before_publish"
    assert item["classification"] == "unverified"
    assert item["duration"] == clip_b["duration"]  # slot preserved

    rows_after, _ = run_ledger.list_decisions(production["run_id"], 1000, 0)
    swapped_in = [
        r for r in rows_after
        if r["decision_type"] == "swapped_in"
        and r["revision_id"] == view1["revision_id"]
    ]
    assert len(swapped_in) == 1
    assert swapped_in[0]["content_hash"] == _sha256(wide)
    assert (swapped_in[0]["rights"] or {})["license"] == "unknown"
    original_row_after = next(
        r for r in rows_after if r["id"] == original_decision_id
    )
    assert _canonical(original_row_after) == _canonical(original_row_before)


def test_replace_corrupt_file_is_fully_atomic(production):
    """A corrupt replacement must change NOTHING: no new revision,
    parent document bytes identical, ledger rows identical."""
    from services import editor, run_ledger

    _view0, doc0 = _rev0(production["project_id"])
    clip_b = _clip_b(doc0)
    corrupt = production["project_dir"] / "adv_corrupt.mp4"
    corrupt.write_bytes(b"\x00\x01\x02 not a media file" * 64)
    try:
        parent_bytes_before = _doc_bytes(
            production["project_dir"], doc0["revision_id"]
        )
        rows_before, _ = run_ledger.list_decisions(
            production["run_id"], 1000, 0
        )
        revisions_before = editor.list_revisions(production["project_id"])
        with pytest.raises(editor.EditorError) as error:
            editor.apply_operations(
                production["project_id"],
                doc0["revision_id"],
                [{"type": "replace_clip", "item_id": clip_b["item_id"],
                  "replacement": {"file_path": str(corrupt)}}],
            )
        assert error.value.status_code == 422
        assert _doc_bytes(
            production["project_dir"], doc0["revision_id"]
        ) == parent_bytes_before
        rows_after, _ = run_ledger.list_decisions(
            production["run_id"], 1000, 0
        )
        assert _canonical(rows_after) == _canonical(rows_before)
        revisions_after = editor.list_revisions(production["project_id"])
        assert [r["revision_id"] for r in revisions_after] == [
            r["revision_id"] for r in revisions_before
        ]
    finally:
        corrupt.unlink()


def test_replace_same_filename_mimic_still_fresh(production, tmp_path):
    """A replacement file NAMED exactly like the original
    (clip_b.mp4, in another directory) must still earn completely
    fresh evidence — identity follows content/probe, not filename."""
    from services import editor, run_ledger

    mimic_dir = tmp_path / "mimic"
    mimic_dir.mkdir()
    mimic = mimic_dir / "clip_b.mp4"
    mimic.write_bytes(production["replacement"].read_bytes())

    _view0, doc0 = _rev0(production["project_id"])
    clip_b = _clip_b(doc0)
    view1, doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [{"type": "replace_clip", "item_id": clip_b["item_id"],
          "replacement": {"file_path": str(mimic)}}],
    )
    item = next(i for i in _items(doc1, 2) if i["kind"] != "gap")
    asset = item["asset"]
    assert asset["file_path"] == str(mimic)
    assert asset["evidence_class"] == "none"
    assert asset["verification"]["accepted"] is False
    assert (
        asset["verification"].get("rejection_reason")
        == "manual_swap_unverified"
    )
    assert asset["rights"]["license"] == "unknown"
    assert item["classification"] == "unverified"
    rows, _ = run_ledger.list_decisions(production["run_id"], 1000, 0)
    swapped_in = [
        r for r in rows
        if r["decision_type"] == "swapped_in"
        and r["revision_id"] == view1["revision_id"]
    ]
    assert len(swapped_in) == 1
    assert swapped_in[0]["content_hash"] == _sha256(mimic)


# ---------------------------------------------------------------------
# 7. Rights classification sweep
# ---------------------------------------------------------------------


def test_rights_classification_sweep(production, monkeypatch):
    """Exercise every add path (upload, local file, URL via a
    stubbed download, library) plus a graphic regeneration, then
    scan the final document and the ledger: no editor-introduced
    asset may carry a fabricated licence — only the fixture's own
    pre-existing library evidence (CC BY 4.0 / CC0 1.0) may appear,
    and only on the fixture's own files."""
    from services import editor, run_ledger

    project_id = production["project_id"]
    _view0, doc0 = _rev0(project_id)

    upload = editor.store_upload(
        project_id, "sweep_upload.mp4",
        io.BytesIO(production["clip_c"].read_bytes()),
    )
    assert upload["user_supplied"] is True
    assert upload["rights"]["license"] == "unknown"
    rows, _ = run_ledger.list_decisions(production["run_id"], 1000, 0)
    upload_row = next(r for r in rows if r["id"] == upload["decision_id"])
    assert upload_row["decision_type"] == "user_supplied"
    assert (upload_row["rights"] or {})["license"] == "unknown"
    assert upload_row["provider"] == "user_upload"

    def gap_duration(doc, number):
        return sum(
            float(i["duration"]) for i in _items(doc, number)
            if i["kind"] == "gap"
        )

    def add_into(doc, view, number, spec, want=0.5):
        available = gap_duration(doc, number)
        assert available > 0.3, f"no gap time in sentence {number}"
        return editor.apply_operations(
            project_id, view["revision_id"],
            [{"type": "add_clip", "asset": spec,
              "sentence_number": number,
              "duration": round(min(available - 0.05, want), 3)}],
        )

    # Window 1: trim clip_a -> add the upload.
    clip_a = next(
        i for i in _items(doc0, 1)
        if i["asset"]["file_path"].endswith("clip_a.mp4")
    )
    view1, doc1 = editor.apply_operations(
        project_id, doc0["revision_id"],
        [{"type": "trim_clip", "item_id": clip_a["item_id"],
          "duration": 0.4}],
    )
    view2, doc2 = add_into(
        doc1, view1, 1, {"upload_id": upload["upload_id"]}
    )
    added_upload = next(
        i for i in _items(doc2, 1)
        if i["asset"] and i["asset"].get("upload_id") == upload["upload_id"]
    )
    assert added_upload["classification"] == "user_supplied"
    assert added_upload["asset"]["user_supplied"] is True
    assert added_upload["asset"]["rights"]["license"] == "unknown"

    # Window 2: trim clip_b -> add a local file.
    clip_b = _clip_b(doc2)
    view3, doc3 = editor.apply_operations(
        project_id, view2["revision_id"],
        [{"type": "trim_clip", "item_id": clip_b["item_id"],
          "duration": 0.4}],
    )
    view4, doc4 = add_into(
        doc3, view3, 2, {"file_path": str(production["clip_c"])}
    )
    added_local = next(
        i for i in _items(doc4, 2)
        if i["asset"] and i["asset"]["file_path"].endswith("clip_c.mp4")
    )
    assert added_local["asset"]["rights"]["license"] == "unknown"
    assert added_local["classification"] == "unverified"

    # Window 3: shrink the graphic -> regenerate it -> add via URL
    # (download stubbed to a local copy; the point is the rights
    # stamping of the URL path, not the network).
    graphic = next(i for i in _items(doc4, 3) if i["kind"] == "graphic")
    view5, doc5 = editor.apply_operations(
        project_id, view4["revision_id"],
        [
            {"type": "set_graphic_duration", "item_id": graphic["item_id"],
             "duration": round(float(graphic["duration"]) - 1.0, 3)},
            {"type": "regenerate_graphic", "item_id": graphic["item_id"]},
        ],
    )
    regenerated = next(i for i in _items(doc5, 3) if i["kind"] == "graphic")
    # Observation (asserted as-is): a regenerated graphic carries
    # rights=None — an ABSENT record, not a fabricated licence.
    assert regenerated["asset"]["rights"] is None
    assert regenerated["classification"] == "generated_graphic"

    def fake_download(url, dest_dir):
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "download_stub.mp4"
        dest.write_bytes(production["clip_c"].read_bytes())
        return dest

    monkeypatch.setattr(editor, "_download_url", fake_download)
    view6, doc6 = add_into(
        doc5, view5, 3, {"source_url": "https://example.test/clip_c.mp4"}
    )
    added_url = next(
        i for i in _items(doc6, 3)
        if i["asset"] and "download_stub" in i["asset"]["file_path"]
    )
    assert added_url["asset"]["rights"]["license"] == "unknown"
    assert added_url["asset"]["source_url"] == "https://example.test/clip_c.mp4"

    # Library path: the asset keeps ITS OWN pre-existing evidence.
    clip_b_now = _clip_b(doc6)
    library_decision = clip_b_now["asset"]["decision_id"]
    view7, doc7 = editor.apply_operations(
        project_id, view6["revision_id"],
        [{"type": "trim_clip", "item_id": clip_b_now["item_id"],
          "duration": 0.3}],
    )
    view8, doc8 = add_into(
        doc7, view7, 3, {"library_decision_id": library_decision}, want=0.35
    )
    added_library = next(
        i for i in _items(doc8, 3)
        if i["asset"] and i["asset"]["file_path"].endswith("clip_b.mp4")
    )
    assert added_library["asset"]["rights"]["license"] == "CC BY 4.0"
    assert "not_verified_for_this_sentence" in added_library["flags"]

    # The sweep: every licence anywhere in the final document or
    # the project's ledger must be "unknown", absent, or one of the
    # fixture's own pre-existing licences on its own files.
    allowed = {"unknown", "CC BY 4.0", "CC0 1.0", None}
    fixture_files = {"clip_a.mp4", "clip_b.mp4", "still.png"}
    for item in _items(doc8):
        asset = item.get("asset") or {}
        licence = (asset.get("rights") or {}).get("license")
        assert licence in allowed, f"fabricated licence: {licence}"
        if licence not in ("unknown", None):
            assert Path(asset["file_path"]).name in fixture_files
    chain = editor.revision_chain_ids(project_id, view8["revision_id"])
    decisions = run_ledger.list_decisions_for_revision(
        project_id, production["run_id"], chain
    )
    assert decisions
    for row in decisions:
        licence = (row.get("rights") or {}).get("license")
        assert licence in allowed, (
            f"ledger row {row['id']} carries fabricated licence {licence}"
        )
    _originals_unchanged(production)


# ---------------------------------------------------------------------
# 8. Revision-specific certificates
# ---------------------------------------------------------------------


def test_revision_specific_certificates(production):
    """Certificates for rev0 / post-replace / post-upload-add must
    each carry their own revision id and exactly their own final
    asset set; the original Stage 4 run certificate must be
    byte-identical before and after all editing."""
    from services import editor, plan_document, run_ledger

    project_id = production["project_id"]
    project = {"id": project_id, "title": "Fixture", "category": "history"}
    run_view = run_ledger.get_run(production["run_id"])

    def run_certificate():
        # Mirrors api_v1.get_certificate exactly: the run
        # certificate is built from the run's OWN decisions only
        # (include_revision_decisions=False — defect D3 fix).
        decisions, _total = run_ledger.list_decisions(
            production["run_id"], 1000, 0,
            include_revision_decisions=False,
        )
        counts = run_ledger.decision_counts(
            production["run_id"], include_revision_decisions=False
        )
        return _canonical(plan_document.build_certificate(
            project, run_view, decisions, counts
        ))

    def revision_certificate(view, doc):
        chain = editor.revision_chain_ids(project_id, view["revision_id"])
        decisions = run_ledger.list_decisions_for_revision(
            project_id, production["run_id"], chain
        )
        counts = run_ledger.counts_for_decisions(decisions)
        return plan_document.build_revision_certificate(
            project, run_view, view, decisions, counts,
            editor.final_assets_for_certificate(doc),
        )

    run_cert_before = run_certificate()

    view0, doc0 = _rev0(project_id)
    cert0 = revision_certificate(view0, doc0)
    assert cert0["revision"]["revision_id"] == view0["revision_id"]
    names0 = {a["file_name"] for a in cert0["final_revision_assets"]}
    assert "clip_b.mp4" in names0
    assert "replacement.mp4" not in names0

    clip_b = _clip_b(doc0)
    view1, doc1 = editor.apply_operations(
        project_id, view0["revision_id"],
        [{"type": "replace_clip", "item_id": clip_b["item_id"],
          "replacement": {"file_path": str(production["replacement"])}}],
    )
    cert1 = revision_certificate(view1, doc1)
    assert cert1["revision"]["revision_id"] == view1["revision_id"]
    assert cert1["revision"]["revision_id"] != cert0["revision"]["revision_id"]
    names1 = {a["file_name"] for a in cert1["final_revision_assets"]}
    assert "replacement.mp4" in names1
    assert "clip_b.mp4" not in names1
    assert any("EDITED REVISION" in d for d in cert1["disclaimers"])

    upload = editor.store_upload(
        project_id, "cert_upload.mp4",
        io.BytesIO(production["clip_c"].read_bytes()),
    )
    clip_a = next(
        i for i in _items(doc1, 1)
        if i["asset"]["file_path"].endswith("clip_a.mp4")
    )
    view2, doc2 = editor.apply_operations(
        project_id, view1["revision_id"],
        [
            {"type": "trim_clip", "item_id": clip_a["item_id"],
             "duration": 0.4},
            {"type": "add_clip",
             "asset": {"upload_id": upload["upload_id"]},
             "sentence_number": 1, "duration": 0.5,
             "after_item_id": clip_a["item_id"]},
        ],
    )
    cert2 = revision_certificate(view2, doc2)
    assert cert2["revision"]["revision_id"] == view2["revision_id"]
    names2 = {a["file_name"] for a in cert2["final_revision_assets"]}
    assert "replacement.mp4" in names2
    upload_names = {
        a["file_name"] for a in cert2["final_revision_assets"]
        if a["classification"] == "user_supplied"
    }
    assert len(upload_names) == 1
    assert upload_names.pop().endswith("cert_upload.mp4")
    assert "clip_b.mp4" not in names2

    # The original decisions inside the run certificate are never
    # rewritten: every asset row of the before-certificate survives
    # verbatim in the after-certificate. And since defect D3 was
    # fixed, NOTHING is added either: the editor's appended
    # decisions (swapped_out + swapped_in for the replace,
    # user_supplied x2 for the upload record + timeline add) are
    # revision-tagged and excluded from the run certificate's
    # population — the certificate is byte-identical, totals
    # included (see also the strict test below).
    from collections import Counter

    cert_after = json.loads(run_certificate())
    cert_before = json.loads(run_cert_before)
    rows_before = Counter(
        _canonical(row) for row in cert_before["assets"]
    )
    rows_after = Counter(
        _canonical(row) for row in cert_after["assets"]
    )
    assert rows_before - rows_after == Counter(), (
        "an original decision row in the run certificate changed"
    )
    added = rows_after - rows_before
    assert added == Counter(), (
        f"editor decisions leaked into the run certificate: {added!r}"
    )
    assert cert_after["summary"]["total"] == cert_before["summary"]["total"]
    assert run_certificate() == run_cert_before
    _originals_unchanged(production)


def test_run_certificate_byte_identical_after_editing(race_production):
    """FIXED (defect D3): the Stage 4 run certificate absorbed
    editor decisions — list_decisions/decision_counts filtered only
    by run_id, so revision-tagged rows (swaps, uploads, adds) were
    counted into the ORIGINAL production's certificate (observed:
    totals 19 -> 23 after one replace + one upload + one add).
    run_ledger.list_decisions/decision_counts now take
    include_revision_decisions=False, and api_v1.get_certificate
    (mirrored by the helper below) uses it: the run certificate is
    built from the run's own decisions (revision_id IS NULL) only.
    The original Stage 4 run certificate for the source run must be
    byte-identical before and after editor activity."""
    from services import editor, plan_document, run_ledger

    project_id = race_production["project_id"]
    project = {"id": project_id, "title": "Fixture", "category": "history"}
    run_view = run_ledger.get_run(race_production["run_id"])

    def run_certificate():
        decisions, _total = run_ledger.list_decisions(
            race_production["run_id"], 1000, 0,
            include_revision_decisions=False,
        )
        counts = run_ledger.decision_counts(
            race_production["run_id"], include_revision_decisions=False
        )
        return _canonical(plan_document.build_certificate(
            project, run_view, decisions, counts
        ))

    before = run_certificate()
    _view0, doc0 = _rev0(project_id)
    clip_b = _clip_b(doc0)
    editor.apply_operations(
        project_id, doc0["revision_id"],
        [{"type": "replace_clip", "item_id": clip_b["item_id"],
          "replacement": {"file_path": str(race_production["replacement"])}}],
    )
    assert run_certificate() == before


# ---------------------------------------------------------------------
# 9. Immutability under fire
# ---------------------------------------------------------------------


def test_parent_documents_immutable_under_children(production):
    """Creating child revisions — including a replace with ledger
    appends — must not modify any earlier revision's document."""
    from services import editor

    view0, _doc0 = _rev0(production["project_id"])
    view1, _doc1 = editor.apply_operations(
        production["project_id"], view0["revision_id"],
        [{"type": "set_volumes", "music": 0.42}],
    )
    view2, doc2 = editor.apply_operations(
        production["project_id"], view1["revision_id"],
        [{"type": "edit_caption", "segment_index": 1,
          "text": "IMMUTABILITY PROBE"}],
    )
    watched = [view0["revision_id"], view1["revision_id"],
               view2["revision_id"]]
    hashes_before = {
        rid: _sha256(production["project_dir"] / f"revision_{rid}.json")
        for rid in watched
    }
    clip_b = _clip_b(doc2)
    editor.apply_operations(
        production["project_id"], view2["revision_id"],
        [{"type": "replace_clip", "item_id": clip_b["item_id"],
          "replacement": {"file_path": str(production["replacement"])}}],
    )
    editor.apply_operations(
        production["project_id"], view2["revision_id"],
        [{"type": "remove_clip",
          "item_id": _items(doc2, 1)[0]["item_id"],
          "gap_policy": "leave_gap"}],
    )
    for rid, digest in hashes_before.items():
        assert _sha256(
            production["project_dir"] / f"revision_{rid}.json"
        ) == digest, f"revision document {rid} was modified by a child edit"
    _originals_unchanged(production)


def test_originals_unchanged_after_all_scenarios(
    production, race_production, race0_production, render_production
):
    """Final sweep: after every scenario above, all four synthetic
    productions' original artefacts are byte-identical (editor
    activity may only ADD files, never modify production files)."""
    for built in (
        production, race_production, race0_production, render_production
    ):
        _originals_unchanged(built)
