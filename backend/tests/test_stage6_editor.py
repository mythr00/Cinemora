"""Stage 6 — editor backend acceptance tests.

Everything runs against a SYNTHETIC production (tests/stage6_fixture.py):
real lavfi clips, real Piper narration, real captions/music writers,
a real ledger run. Editor renders are REAL renders through the
existing renderer; the production's own artefacts are hash-checked
byte-identical at the end.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

import stage6_fixture as fx


@pytest.fixture(scope="module")
def production(tmp_path_factory):
    root = tmp_path_factory.mktemp("stage6-media")
    os.environ["CINEMORA_MEDIA_ROOT"] = str(root)
    built = fx.build_synthetic_production(root, "proj-editor")
    built["root"] = root
    built["original_hashes"] = fx.hash_tree(built["project_dir"])
    return built


@pytest.fixture(scope="module")
def noinput_production(production):
    # Same media root as the main fixture: CINEMORA_MEDIA_ROOT is a
    # single env var, so a second root would strand the first
    # project's documents mid-module.
    root = production["root"]
    os.environ["CINEMORA_MEDIA_ROOT"] = str(root)
    built = fx.build_synthetic_production(
        root, "proj-noinput", graphic_inputs=False
    )
    built["root"] = root
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


def _rev0(project_id):
    """Revision 0's (view, doc), addressed explicitly — get_editor
    returns the LATEST revision, which other tests move forward."""
    from services import editor

    editor.get_editor(project_id)  # ensures revision 0 exists
    for view in editor.list_revisions(project_id):
        if view["revision_number"] == 0:
            return editor.get_revision(project_id, view["revision_id"])
    raise AssertionError("no revision 0")


def _items(doc, sentence=None):
    items = doc["tracks"]["visual"]
    if sentence is not None:
        items = [i for i in items if i["sentence_number"] == sentence]
    return items


def _probe_duration(path):
    from services import media_probe

    probe = media_probe.probe_video(path)
    assert probe, f"no probe for {path}"
    return probe["duration_seconds"]


def _originals_unchanged(production):
    current = fx.hash_tree(production["project_dir"])
    for relative, digest in production["original_hashes"].items():
        assert current.get(relative) == digest, f"production artefact changed: {relative}"


# ---------------------------------------------------------------------
# Import (revision 0)
# ---------------------------------------------------------------------


def test_import_revision_zero(production):
    from services import editor

    view, doc = editor.get_editor(production["project_id"])
    assert view["revision_number"] == 0
    assert view["status"] == "draft"
    assert doc["run_id"] == production["run_id"]

    items = _items(doc)
    assert len(items) == 4
    by_file = {Path(i["asset"]["file_path"]).name: i for i in items}
    assert by_file["clip_a.mp4"]["classification"] == "verified_footage"
    assert by_file["clip_a.mp4"]["asset"]["evidence_class"] == "verified_segment"
    assert by_file["clip_a.mp4"]["asset"]["decision_id"]  # ledger-matched
    assert by_file["clip_a.mp4"]["asset"]["rights"]["license"] == "CC BY 4.0"
    assert by_file["still.png"]["classification"] == "still_image"
    assert by_file["clip_b.mp4"]["classification"] == "verified_footage"
    graphic = by_file["graphic.mp4"]
    assert graphic["kind"] == "graphic"
    assert graphic["classification"] == "generated_graphic"
    assert graphic["graphic"]["template"] == "data_gauge"
    assert graphic["graphic"]["inputs"] == production["graphic_inputs"]

    narration = doc["tracks"]["narration"]
    assert [s["sentence_number"] for s in narration["sentences"]] == [1, 2, 3]
    assert doc["tracks"]["music"]["file"].endswith("music.wav")
    assert len(doc["tracks"]["captions"]["segments"]) == 3
    assert doc["tracks"]["captions"]["mode"] == "burn"
    assert abs(doc["duration_seconds"] - production["total_duration"]) < 0.05
    _originals_unchanged(production)


# ---------------------------------------------------------------------
# Remove: leave_gap blocks, then exports flagged; close_gap absorbs
# ---------------------------------------------------------------------


def test_remove_leave_gap_blocks_then_exports_flagged(production, sync_queue):
    from services import editor

    _view0, doc0 = _rev0(production["project_id"])
    clip_b = next(
        i for i in _items(doc0, 2) if i["asset"]["file_path"].endswith("clip_b.mp4")
    )
    view1, doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [{"type": "remove_clip", "item_id": clip_b["item_id"],
          "gap_policy": "leave_gap"}],
    )
    gaps = [i for i in _items(doc1, 2) if i["kind"] == "gap"]
    assert len(gaps) == 1
    assert "removed_leave_gap" in gaps[0]["flags"]

    with pytest.raises(editor.EditorError) as blocked:
        editor.request_render(production["project_id"], view1["revision_id"])
    assert blocked.value.status_code == 422
    assert blocked.value.code == "unresolved_gaps"

    result = editor.request_render(
        production["project_id"], view1["revision_id"], allow_gaps=True
    )
    assert result["status"] == "rendering"
    view_after, doc_after = editor.get_revision(
        production["project_id"], view1["revision_id"]
    )
    assert view_after["status"] == "exported"
    assert len(doc_after["exports"]) == 1
    export = doc_after["exports"][0]
    assert export["allow_gaps"] is True
    assert [g["sentence_number"] for g in export["gaps_exported"]] == [2]
    # The gap was covered by a recorded holdover of the nearest
    # genuine visual — never by invented footage.
    assert len(export["holdovers"]) == 1
    assert export["holdovers"][0]["gap_item_id"] == gaps[0]["item_id"]
    assert export["holdovers"][0]["held_file"].endswith("still.png")
    final = Path(export["file"])
    assert final.name == f"revision_{view1['revision_id']}_final.mp4"
    assert abs(_probe_duration(final) - doc_after["duration_seconds"]) < 0.75
    _originals_unchanged(production)


def test_remove_close_gap_absorbs_into_still(production, sync_queue):
    from services import editor

    _view0, doc0 = _rev0(production["project_id"])
    clip_a = next(
        i for i in _items(doc0, 1) if i["asset"]["file_path"].endswith("clip_a.mp4")
    )
    window = production["windows"][0]
    view1, doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [{"type": "remove_clip", "item_id": clip_a["item_id"],
          "gap_policy": "close_gap"}],
    )
    items = _items(doc1, 1)
    assert len(items) == 1 and items[0]["kind"] != "gap"
    assert items[0]["asset"]["file_path"].endswith("still.png")
    assert abs(items[0]["duration"] - (window["end"] - window["start"])) < 0.05

    editor.request_render(production["project_id"], view1["revision_id"])
    view_after, doc_after = editor.get_revision(
        production["project_id"], view1["revision_id"]
    )
    assert view_after["status"] == "exported"
    assert doc_after["exports"][0]["gaps_exported"] == []
    _originals_unchanged(production)


# ---------------------------------------------------------------------
# Replace: fresh evidence, original preserved, failure is atomic
# ---------------------------------------------------------------------


def test_replace_clip_fresh_evidence(production):
    from services import editor, run_ledger

    _view0, doc0 = _rev0(production["project_id"])
    clip_b = next(
        i for i in _items(doc0, 2) if i["asset"]["file_path"].endswith("clip_b.mp4")
    )
    original_decision_id = clip_b["asset"]["decision_id"]
    before = run_ledger.list_decisions(production["run_id"], 1000, 0)[0]

    view1, doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [{"type": "replace_clip", "item_id": clip_b["item_id"],
          "replacement": {"file_path": str(production["replacement"])}}],
    )
    item = next(i for i in _items(doc1, 2) if i["kind"] != "gap")
    asset = item["asset"]
    assert asset["file_path"].endswith("replacement.mp4")
    # Fresh evidence — NOT inherited from the verified original.
    assert asset["evidence_class"] == "none"
    assert asset["verification"]["accepted"] is False
    assert asset["verification"]["rejection_reason"] == "manual_swap_unverified"
    assert asset["rights"]["license"] == "unknown"
    assert asset["rights"]["rights_status"] == "verify_before_publish"
    assert item["classification"] == "unverified"
    assert asset["decision_id"] and asset["decision_id"] != original_decision_id
    assert item["duration"] == clip_b["duration"]

    after = run_ledger.list_decisions(production["run_id"], 1000, 0)[0]
    assert len(after) == len(before) + 2
    new_rows = [r for r in after if r["revision_id"] == view1["revision_id"]]
    assert {r["decision_type"] for r in new_rows} == {"swapped_out", "swapped_in"}
    # The original decision row is untouched.
    original_rows = [r for r in after if r["id"] == original_decision_id]
    assert len(original_rows) == 1
    assert original_rows[0]["revision_id"] is None
    _originals_unchanged(production)


def test_replace_unprobeable_is_atomic_422(production):
    from services import editor

    _view0, doc0 = _rev0(production["project_id"])
    bad = production["project_dir"] / "not_media.txt"
    bad.write_text("this is not media")
    count_before = len(editor.list_revisions(production["project_id"]))
    clip_b = next(
        i for i in _items(doc0, 2) if i["asset"]["file_path"].endswith("clip_b.mp4")
    )
    with pytest.raises(editor.EditorError) as error:
        editor.apply_operations(
            production["project_id"],
            doc0["revision_id"],
            [{"type": "replace_clip", "item_id": clip_b["item_id"],
              "replacement": {"file_path": str(bad)}}],
        )
    assert error.value.status_code == 422
    assert len(editor.list_revisions(production["project_id"])) == count_before
    bad.unlink()


def test_batch_atomicity_one_bad_op_rejects_all(production):
    from services import editor

    _view0, doc0 = _rev0(production["project_id"])
    count_before = len(editor.list_revisions(production["project_id"]))
    clip_a = next(
        i for i in _items(doc0, 1) if i["asset"]["file_path"].endswith("clip_a.mp4")
    )
    with pytest.raises(editor.EditorError) as error:
        editor.apply_operations(
            production["project_id"],
            doc0["revision_id"],
            [
                {"type": "trim_clip", "item_id": clip_a["item_id"],
                 "duration": 0.5},
                {"type": "trim_clip", "item_id": "itm_missing",
                 "duration": 0.5},
            ],
        )
    assert error.value.status_code == 422
    assert len(editor.list_revisions(production["project_id"])) == count_before


# ---------------------------------------------------------------------
# Trim / move / split
# ---------------------------------------------------------------------


def test_trim_move_split(production):
    from services import editor

    _view0, doc0 = _rev0(production["project_id"])
    clip_a = next(
        i for i in _items(doc0, 1) if i["asset"]["file_path"].endswith("clip_a.mp4")
    )
    clip_b = next(
        i for i in _items(doc0, 2) if i["asset"]["file_path"].endswith("clip_b.mp4")
    )
    still = next(
        i for i in _items(doc0, 1) if i["asset"]["file_path"].endswith("still.png")
    )

    # Out-of-bounds trim -> 422 (clip sources are 6s).
    with pytest.raises(editor.EditorError) as error:
        editor.apply_operations(
            production["project_id"], doc0["revision_id"],
            [{"type": "trim_clip", "item_id": clip_a["item_id"],
              "clip_start": 5.5, "duration": 2.0}],
        )
    assert error.value.code == "trim_out_of_bounds"

    # Trim frees gap time; reorder still before clip_a; split clip_b.
    view1, doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [
            {"type": "trim_clip", "item_id": clip_a["item_id"],
             "duration": round(clip_a["duration"] - 0.5, 3)},
            {"type": "move_clip", "item_id": clip_a["item_id"],
             "after_item_id": still["item_id"]},
            {"type": "split_clip", "item_id": clip_b["item_id"],
             "at_offset": round(clip_b["duration"] / 2, 3)},
        ],
    )
    window1 = _items(doc1, 1)
    # The freed time stays as a gap at the position where it was
    # freed (after clip_a's original slot); moving clip_a later in
    # the window does not drag the gap along.
    assert [Path(i["asset"]["file_path"]).name if i["kind"] != "gap" else "gap"
            for i in window1] == ["gap", "still.png", "clip_a.mp4"]
    halves = _items(doc1, 2)
    assert len(halves) == 2
    assert halves[0]["asset"]["decision_id"] == halves[1]["asset"]["decision_id"]
    assert halves[1]["asset"]["clip_start"] > halves[0]["asset"]["clip_start"]
    total = sum(i["duration"] for i in halves)
    assert abs(total - clip_b["duration"]) < 0.01
    # Window conservation holds across the whole document.
    for number in (1, 2, 3):
        window = production["windows"][number - 1]
        assert abs(
            sum(i["duration"] for i in _items(doc1, number))
            - (window["end"] - window["start"])
        ) < 0.05


# ---------------------------------------------------------------------
# Uploads + add_clip
# ---------------------------------------------------------------------


def test_upload_and_add_clip(production):
    from services import editor, run_ledger

    upload = editor.store_upload(
        production["project_id"],
        "extra.mp4",
        io.BytesIO(production["clip_c"].read_bytes()),
    )
    assert upload["user_supplied"] is True
    assert upload["rights"]["license"] == "unknown"
    assert upload["decision_id"]
    rows = run_ledger.list_decisions(production["run_id"], 1000, 0)[0]
    upload_rows = [
        r for r in rows
        if r["decision_type"] == "user_supplied" and r["id"] == upload["decision_id"]
    ]
    assert len(upload_rows) == 1

    _view0, doc0 = _rev0(production["project_id"])
    clip_a = next(
        i for i in _items(doc0, 1) if i["asset"]["file_path"].endswith("clip_a.mp4")
    )
    # No gap time yet: adding must refuse rather than compress.
    with pytest.raises(editor.EditorError) as error:
        editor.apply_operations(
            production["project_id"], doc0["revision_id"],
            [{"type": "add_clip",
              "asset": {"upload_id": upload["upload_id"]},
              "sentence_number": 1, "duration": 0.5}],
        )
    assert error.value.code == "insufficient_gap_time"

    freed = round(clip_a["duration"] - 0.4, 3)
    add_duration = round(freed - 0.1, 3)
    view1, doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [
            {"type": "trim_clip", "item_id": clip_a["item_id"],
             "duration": 0.4},
            {"type": "add_clip",
             "asset": {"upload_id": upload["upload_id"]},
             "sentence_number": 1, "duration": add_duration,
             "after_item_id": clip_a["item_id"]},
        ],
    )
    added = [
        i for i in _items(doc1, 1)
        if i["kind"] != "gap" and i["asset"].get("upload_id") == upload["upload_id"]
    ]
    assert len(added) == 1
    assert added[0]["classification"] == "user_supplied"
    assert added[0]["duration"] == add_duration


def test_add_from_library_keeps_its_evidence(production):
    from services import editor

    _view0, doc0 = _rev0(production["project_id"])
    clip_b = next(
        i for i in _items(doc0, 2) if i["asset"]["file_path"].endswith("clip_b.mp4")
    )
    graphic = next(i for i in _items(doc0, 3) if i["kind"] == "graphic")
    view1, doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [
            {"type": "set_graphic_duration", "item_id": graphic["item_id"],
             "duration": round(graphic["duration"] - 1.0, 3)},
            {"type": "add_clip",
             "asset": {"library_decision_id": clip_b["asset"]["decision_id"]},
             "sentence_number": 3, "duration": 0.8},
        ],
    )
    added = [
        i for i in _items(doc1, 3)
        if i["kind"] != "gap" and i["asset"]["file_path"].endswith("clip_b.mp4")
    ]
    assert len(added) == 1
    # The library asset keeps ITS OWN verdict; it is flagged as not
    # verified for THIS sentence rather than silently re-verified.
    assert added[0]["asset"]["verification"]["accepted"] is True
    assert "not_verified_for_this_sentence" in added[0]["flags"]


def test_unprobeable_upload_rejected(production, tmp_path):
    from services import editor

    with pytest.raises(editor.EditorError) as error:
        editor.store_upload(
            production["project_id"], "notes.txt",
            io.BytesIO(b"plain text, not media"),
        )
    assert error.value.status_code == 422
    uploads_dir = production["project_dir"] / "uploads"
    leftovers = [
        p for p in uploads_dir.iterdir()
        if p.name != "index.json" and "notes" in p.name
    ]
    assert leftovers == []


# ---------------------------------------------------------------------
# Captions: edit flows into the rendered outputs
# ---------------------------------------------------------------------


def test_edit_caption_rendered_outputs(production, sync_queue):
    from services import editor

    _view0, doc0 = _rev0(production["project_id"])
    view1, _doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [{"type": "edit_caption", "segment_index": 0,
          "text": "EDITED CAPTION LINE"}],
    )
    editor.request_render(production["project_id"], view1["revision_id"])
    view_after, doc_after = editor.get_revision(
        production["project_id"], view1["revision_id"]
    )
    assert view_after["status"] == "exported"
    work = (
        production["project_dir"] / "editor_renders" / view1["revision_id"]
    )
    srt = (work / "captions.srt").read_text()
    captions = json.loads((work / "captions.json").read_text())
    assert "EDITED CAPTION LINE" in srt
    assert captions["segments"][0]["text"] == "EDITED CAPTION LINE"
    assert captions["segments"][0]["edited"] is True
    _originals_unchanged(production)


# ---------------------------------------------------------------------
# Render plan dependency rules
# ---------------------------------------------------------------------


def test_music_change_rebuilds_soundtrack_only(production, sync_queue):
    from services import editor

    _view0, doc0 = _rev0(production["project_id"])
    view1, doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [
            {"type": "set_music",
             "music_file": str(production["music_alt"])},
            {"type": "set_volumes", "music": 0.2},
        ],
    )
    plan = editor.compute_render_plan(doc0, doc1)
    assert plan["soundtrack_rebuild"] is True
    assert plan["narration_rebuild"] is False
    assert plan["captions_rebuild"] is False
    assert plan["visual"]["changed_item_ids"] == []

    editor.request_render(production["project_id"], view1["revision_id"])
    view_after, _doc_after = editor.get_revision(
        production["project_id"], view1["revision_id"]
    )
    assert view_after["status"] == "exported"
    work = production["project_dir"] / "editor_renders" / view1["revision_id"]
    # Narration was NOT rebuilt: the work-dir narration is a byte
    # copy of the production's.
    assert (work / "narration.wav").read_bytes() == (
        production["project_dir"] / "narration.wav"
    ).read_bytes()
    _originals_unchanged(production)


def test_narration_regen_rebuilds_and_reuses_parts(production, sync_queue):
    from services import editor

    _view0, doc0 = _rev0(production["project_id"])
    view1, doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [{"type": "regenerate_narration_sentence", "sentence_number": 2,
          "text": "By midday the convoy crossed the river safely."}],
    )
    assert doc1["tracks"]["captions"]["stale"] is True
    plan = editor.compute_render_plan(doc0, doc1)
    assert plan["narration_rebuild"] is True
    assert plan["captions_rebuild"] is True
    assert plan["soundtrack_rebuild"] is True

    editor.request_render(production["project_id"], view1["revision_id"])
    view_after, doc_after = editor.get_revision(
        production["project_id"], view1["revision_id"]
    )
    assert view_after["status"] == "exported"
    # Captions were rebuilt from the new narration — no longer stale.
    assert doc_after["tracks"]["captions"]["stale"] is False
    assert doc_after["tracks"]["narration"]["dirty_sentences"] == []
    # Unchanged sentences' parts are byte-identical (hash reuse);
    # the regenerated sentence's part differs.
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
        p["index"]: production_parts / p["file"] for p in prod_manifest["parts"]
    }
    work_by_index = {
        p["index"]: work_parts / p["file"] for p in work_manifest["parts"]
    }
    assert work_by_index[0].read_bytes() == prod_by_index[0].read_bytes()
    assert work_by_index[1].read_bytes() != prod_by_index[1].read_bytes()
    final = Path(doc_after["exports"][0]["file"])
    assert abs(_probe_duration(final) - doc_after["duration_seconds"]) < 0.75
    _originals_unchanged(production)


# ---------------------------------------------------------------------
# Graphics: regenerate from recorded inputs; refuse without them
# ---------------------------------------------------------------------


def test_regenerate_graphic_from_recorded_inputs(production):
    from services import editor, run_ledger

    _view0, doc0 = _rev0(production["project_id"])
    graphic = next(i for i in _items(doc0, 3) if i["kind"] == "graphic")
    view1, doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [{"type": "regenerate_graphic", "item_id": graphic["item_id"]}],
    )
    item = next(i for i in _items(doc1, 3) if i["kind"] == "graphic")
    assert item["asset"]["generated"] is True
    assert "editor_media" in item["asset"]["file_path"]
    assert Path(item["asset"]["file_path"]).is_file()
    rows = run_ledger.list_decisions(production["run_id"], 1000, 0)[0]
    generated = [
        r for r in rows
        if r["decision_type"] == "generated"
        and r["revision_id"] == view1["revision_id"]
    ]
    assert len(generated) == 1


def test_regenerate_graphic_without_inputs_refused(noinput_production):
    from services import editor

    _view0, doc0 = _rev0(noinput_production["project_id"])
    graphic = next(i for i in _items(doc0, 3) if i["kind"] == "graphic")
    assert graphic["graphic"]["inputs"] is None
    with pytest.raises(editor.EditorError) as error:
        editor.apply_operations(
            noinput_production["project_id"],
            doc0["revision_id"],
            [{"type": "regenerate_graphic", "item_id": graphic["item_id"]}],
        )
    assert error.value.code == "graphic_inputs_not_recorded"


# ---------------------------------------------------------------------
# Chain, cancel, certificate
# ---------------------------------------------------------------------


def test_revision_chain_branching(production):
    from services import editor

    view0, _doc0 = _rev0(production["project_id"])
    branch_a, _ = editor.create_revision(
        production["project_id"], from_revision_id=view0["revision_id"]
    )
    branch_b, _ = editor.create_revision(
        production["project_id"], from_revision_id=view0["revision_id"]
    )
    assert branch_a["parent_revision_id"] == view0["revision_id"]
    assert branch_b["parent_revision_id"] == view0["revision_id"]
    chain = editor.revision_chain_ids(
        production["project_id"], branch_a["revision_id"]
    )
    assert chain == [view0["revision_id"], branch_a["revision_id"]]
    numbers = {
        r["revision_id"]: r["revision_number"]
        for r in editor.list_revisions(production["project_id"])
    }
    assert numbers[branch_a["revision_id"]] == 1


def test_cancelled_render_leaves_revision_consistent(production, monkeypatch):
    import jobs
    from services import editor, run_ledger

    _paired_queue = RecordingQueue()
    monkeypatch.setattr(jobs, "video_queue", _paired_queue)
    monkeypatch.setattr(jobs, "video_queue_priority", _paired_queue)
    _view0, doc0 = _rev0(production["project_id"])
    result = editor.request_render(
        production["project_id"], doc0["revision_id"]
    )
    run_id = result["run_id"]
    run_ledger.request_cancel(run_id)
    outcome = editor.run_revision_render(
        production["project_id"], doc0["revision_id"], run_id
    )
    assert outcome["status"] == "cancelled"
    view_after, doc_after = editor.get_revision(
        production["project_id"], doc0["revision_id"]
    )
    assert view_after["status"] == "draft"
    assert doc_after["exports"] == []
    assert not (
        production["project_dir"]
        / f"revision_{doc0['revision_id']}_final.mp4"
    ).exists()
    _originals_unchanged(production)


def test_certificate_reflects_final_revision(production):
    from services import editor, plan_document, run_ledger

    _view0, doc0 = _rev0(production["project_id"])
    clip_b = next(
        i for i in _items(doc0, 2) if i["asset"]["file_path"].endswith("clip_b.mp4")
    )
    view1, doc1 = editor.apply_operations(
        production["project_id"],
        doc0["revision_id"],
        [{"type": "replace_clip", "item_id": clip_b["item_id"],
          "replacement": {"file_path": str(production["replacement"])}}],
    )
    chain = editor.revision_chain_ids(production["project_id"], view1["revision_id"])
    decisions = run_ledger.list_decisions_for_revision(
        production["project_id"], production["run_id"], chain
    )
    counts = run_ledger.counts_for_decisions(decisions)
    run_view = run_ledger.get_run(production["run_id"])
    certificate = plan_document.build_revision_certificate(
        {"id": production["project_id"], "title": "Fixture",
         "category": "history"},
        run_view,
        view1,
        decisions,
        counts,
        editor.final_assets_for_certificate(doc1),
    )
    assert certificate["certificate_version"] == 2
    assert certificate["revision"]["revision_id"] == view1["revision_id"]
    names = {
        a["file_name"] for a in certificate["final_revision_assets"]
    }
    assert "replacement.mp4" in names
    assert "clip_b.mp4" not in names
    assert any(
        "EDITED REVISION" in d for d in certificate["disclaimers"]
    )
    # The Stage 4 run certificate is unchanged in shape.
    run_decisions, _ = run_ledger.list_decisions(production["run_id"], 1000, 0)
    run_counts = run_ledger.decision_counts(production["run_id"])
    original = plan_document.build_certificate(
        {"id": production["project_id"], "title": "Fixture",
         "category": "history"},
        run_view, run_decisions, run_counts,
    )
    assert "revision" not in original
    assert "final_revision_assets" not in original
