"""Editor end-to-end contract verification (API v1 level ONLY).

This file verifies the editor exactly as the frontend drives it:
every edit is one POST to the operations endpoint against the
current revision, the response is the NEXT revision node, and the
page re-reads that revision from the server before rendering
anything (frontend/app/projects/[id]/edit/page.tsx). Nothing here
claims browser verification — all assertions are on HTTP responses
from the real api_v1 routes (TestClient, same harness as
test_stage6_api.py) backed by the synthetic production fixture.

Covered:
- All 12 operations end-to-end on a branched revision chain, each
  re-read from the server and asserted changed as expected, with
  narration-window conservation checked after every step (window
  totals preserved; freed time appears only as explicit gap items).
- Revision branching from a non-head revision, parent chain, and
  the D3 guarantee: the original run certificate is byte-identical
  before/after all editing.
- Undo/redo: there is NO undo/redo endpoint. The mechanism is
  history navigation — Undo = GET the parent revision, Redo = GET
  the child revision again (the frontend keeps a client-side redo
  stack); editing from an undone state branches. Verified as such.
- Render/export: gaps gate (422 by default), allow_gaps=true
  renders with the gap covered by HOLDOVER and NAMED in the export
  record; a gapless revision renders and the export file probes.
- Per-revision certificate: certificate_version 2 describing the
  REVISION, differing appropriately from the original certificate.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import stage6_fixture as fx

TOKEN = "e2e-contract-token"
AUTH = {"X-API-Key": TOKEN}
PROJECT = "proj-e2e"


class SyncQueue:
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


@pytest.fixture(scope="module")
def production(tmp_path_factory):
    root = tmp_path_factory.mktemp("editor-e2e-media")
    # MonkeyPatch.context() so CINEMORA_MEDIA_ROOT is restored when
    # this module finishes — a bare os.environ assignment leaks into
    # later test files (it broke test_stage2_robustness's media-root
    # anchoring assertion in combined runs; same leak class as the
    # IA-04 fixture fix in Phases 1+2).
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CINEMORA_MEDIA_ROOT", str(root))
        yield fx.build_synthetic_production(root, PROJECT)


@pytest.fixture()
def client(monkeypatch, production):
    monkeypatch.setenv("CINEMORA_API_TOKEN", TOKEN)
    monkeypatch.setenv("CINEMORA_ALLOW_OPERATOR_TOKEN", "1")  # E-21: explicit opt-in
    import jobs

    _paired_queue = SyncQueue()
    monkeypatch.setattr(jobs, "video_queue", _paired_queue)
    monkeypatch.setattr(jobs, "video_queue_priority", _paired_queue)
    from main import app

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------
# Helpers — the frontend's exact call pattern
# ---------------------------------------------------------------------


def _get_editor(client):
    response = client.get(f"/api/v1/projects/{PROJECT}/editor", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def _get_revision(client, revision_id):
    response = client.get(
        f"/api/v1/projects/{PROJECT}/revisions/{revision_id}", headers=AUTH
    )
    assert response.status_code == 200, response.text
    return response.json()


def _apply(client, revision_id, operations):
    """One edit batch, as the frontend sends it; returns the new
    revision VIEW (the operations endpoint returns the view only —
    the document is then re-read with a separate GET)."""
    response = client.post(
        f"/api/v1/projects/{PROJECT}/revisions/{revision_id}/operations",
        json={"operations": operations},
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    view = response.json()
    assert view["parent_revision_id"] == revision_id
    return view


def _branch(client, from_revision_id):
    response = client.post(
        f"/api/v1/projects/{PROJECT}/revisions",
        json={"from_revision_id": from_revision_id},
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    view = response.json()
    assert view["parent_revision_id"] == from_revision_id
    return view


def _rev0(client):
    body = _get_editor(client)
    if body["revision_number"] == 0:
        return body
    listing = client.get(
        f"/api/v1/projects/{PROJECT}/revisions", headers=AUTH
    ).json()["revisions"]
    rev0 = next(r for r in listing if r["revision_number"] == 0)
    return _get_revision(client, rev0["revision_id"])


def _items(doc, sentence=None):
    items = doc["tracks"]["visual"]
    if sentence is not None:
        items = [i for i in items if i["sentence_number"] == sentence]
    return items


def _by_name(doc, name, sentence=None):
    return next(
        i for i in _items(doc, sentence)
        if i["kind"] != "gap" and i["asset"]["file_path"].endswith(name)
    )


def _assert_window_conservation(doc):
    """Every narration window's items (clips + explicit gaps) sum to
    the window duration, and the document total is the last window's
    end — the invariant every operation must preserve."""
    windows = {
        s["sentence_number"]: s for s in doc["tracks"]["narration"]["sentences"]
    }
    for number, window in windows.items():
        total = sum(float(i["duration"]) for i in _items(doc, number))
        expected = float(window["end"]) - float(window["start"])
        assert abs(total - expected) < 0.05, (
            f"sentence {number}: items sum {total:.3f} != window {expected:.3f}"
        )
    assert abs(
        doc["duration_seconds"] - max(w["end"] for w in windows.values())
    ) < 0.05


def _original_certificate_bytes(client):
    response = client.get(
        f"/api/v1/projects/{PROJECT}/certificate", headers=AUTH
    )
    assert response.status_code == 200, response.text
    return response.content


def _probe_duration(path):
    from services import media_probe

    probe = media_probe.probe_video(path)
    assert probe, f"no probe for {path}"
    return probe["duration_seconds"]


# ---------------------------------------------------------------------
# All 12 operations end-to-end on one branched chain
# ---------------------------------------------------------------------


def test_all_twelve_operations_end_to_end(client, production):
    rev0 = _rev0(client)
    head = rev0["revision_id"]
    doc = rev0["document"]
    _assert_window_conservation(doc)
    original_cert_before = _original_certificate_bytes(client)

    def step(operations):
        """Apply one batch from the current head, re-read the new
        revision from the server, check chain + conservation."""
        nonlocal head, doc
        view = _apply(client, head, operations)
        assert view["revision_number"] == doc["revision_number"] + 1
        reread = _get_revision(client, view["revision_id"])
        assert reread["document"]["revision_id"] == view["revision_id"]
        assert reread["document"]["parent_revision_id"] == head
        _assert_window_conservation(reread["document"])
        head, doc = view["revision_id"], reread["document"]
        return doc

    # 1. trim_clip — clip_a shrinks; freed time becomes an explicit gap.
    clip_a = _by_name(doc, "clip_a.mp4", sentence=1)
    trimmed_to = round(clip_a["duration"] - 0.5, 3)
    doc = step([{"type": "trim_clip", "item_id": clip_a["item_id"],
                 "duration": trimmed_to}])
    assert _by_name(doc, "clip_a.mp4", 1)["duration"] == trimmed_to
    gaps1 = [i for i in _items(doc, 1) if i["kind"] == "gap"]
    assert len(gaps1) == 1 and abs(gaps1[0]["duration"] - 0.5) < 0.01

    # 2. add_clip — upload through the real uploads endpoint, then add
    #    into the freed gap time of sentence 1.
    upload = client.post(
        f"/api/v1/projects/{PROJECT}/uploads",
        files={"file": ("extra.mp4",
                        io.BytesIO(production["clip_c"].read_bytes()),
                        "video/mp4")},
        headers=AUTH,
    )
    assert upload.status_code == 201, upload.text
    upload_id = upload.json()["upload_id"]
    clip_a = _by_name(doc, "clip_a.mp4", sentence=1)
    doc = step([{"type": "add_clip",
                 "asset": {"upload_id": upload_id},
                 "sentence_number": 1, "duration": 0.4,
                 "after_item_id": clip_a["item_id"]}])
    added = [i for i in _items(doc, 1)
             if i["kind"] != "gap"
             and i["asset"].get("upload_id") == upload_id]
    assert len(added) == 1
    assert added[0]["classification"] == "user_supplied"
    assert added[0]["duration"] == 0.4

    # 3. move_clip — move the added clip after the still, same window.
    still = _by_name(doc, "still.png", sentence=1)
    added = [i for i in _items(doc, 1)
             if i["kind"] != "gap"
             and i["asset"].get("upload_id") == upload_id][0]
    doc = step([{"type": "move_clip", "item_id": added["item_id"],
                 "after_item_id": still["item_id"]}])
    order = [Path(i["asset"]["file_path"]).name
             for i in _items(doc, 1) if i["kind"] != "gap"]
    assert order == ["clip_a.mp4", "still.png", Path(added["asset"]["file_path"]).name]

    # 4. split_clip — clip_b becomes two items sharing one decision.
    clip_b = _by_name(doc, "clip_b.mp4", sentence=2)
    doc = step([{"type": "split_clip", "item_id": clip_b["item_id"],
                 "at_offset": round(clip_b["duration"] / 2, 3)}])
    halves = _items(doc, 2)
    assert len(halves) == 2
    assert halves[0]["asset"]["decision_id"] == halves[1]["asset"]["decision_id"]
    assert abs(sum(i["duration"] for i in halves) - clip_b["duration"]) < 0.01

    # 5. replace_clip — first half swapped for replacement.mp4; the
    #    replacement carries FRESH (unverified) evidence, same duration.
    first_half = _items(doc, 2)[0]
    doc = step([{"type": "replace_clip", "item_id": first_half["item_id"],
                 "replacement": {"file_path": str(production["replacement"])}}])
    replaced = _items(doc, 2)[0]
    assert replaced["asset"]["file_path"].endswith("replacement.mp4")
    assert replaced["duration"] == first_half["duration"]
    assert replaced["asset"]["evidence_class"] == "none"
    assert replaced["asset"]["verification"]["accepted"] is False
    assert replaced["classification"] == "unverified"

    # 6. remove_clip (leave_gap) — second half leaves an explicit,
    #    named gap of exactly its duration.
    second_half = _items(doc, 2)[1]
    removed_duration = second_half["duration"]
    doc = step([{"type": "remove_clip", "item_id": second_half["item_id"],
                 "gap_policy": "leave_gap"}])
    gaps2 = [i for i in _items(doc, 2) if i["kind"] == "gap"]
    assert len(gaps2) == 1
    assert abs(gaps2[0]["duration"] - removed_duration) < 0.01
    assert "removed_leave_gap" in gaps2[0]["flags"]

    # 7. set_graphic_duration — graphic shrinks; gap appears in S3.
    graphic = next(i for i in _items(doc, 3) if i["kind"] == "graphic")
    shorter = round(graphic["duration"] - 1.0, 3)
    doc = step([{"type": "set_graphic_duration",
                 "item_id": graphic["item_id"], "duration": shorter}])
    graphic = next(i for i in _items(doc, 3) if i["kind"] == "graphic")
    assert graphic["duration"] == shorter
    assert any(i["kind"] == "gap" for i in _items(doc, 3))

    # 8. regenerate_graphic — re-rendered from recorded inputs into
    #    editor_media, flagged regenerated.
    old_graphic_path = graphic["asset"]["file_path"]
    doc = step([{"type": "regenerate_graphic",
                 "item_id": graphic["item_id"]}])
    graphic = next(i for i in _items(doc, 3) if i["kind"] == "graphic")
    assert graphic["asset"]["file_path"] != old_graphic_path
    assert "editor_media" in graphic["asset"]["file_path"]
    assert Path(graphic["asset"]["file_path"]).is_file()
    assert graphic["asset"]["generated"] is True
    assert "regenerated" in graphic["flags"]

    # 9. edit_caption — segment 0 text replaced and marked edited.
    doc = step([{"type": "edit_caption", "segment_index": 0,
                 "text": "E2E EDITED CAPTION"}])
    segment = doc["tracks"]["captions"]["segments"][0]
    assert segment["text"] == "E2E EDITED CAPTION"
    assert segment["edited"] is True

    # 10. regenerate_narration_sentence — new text recorded, sentence
    #     marked dirty, captions flagged stale (rebuild at render).
    doc = step([{"type": "regenerate_narration_sentence",
                 "sentence_number": 2,
                 "text": "By midday the convoy crossed safely."}])
    sentence2 = next(s for s in doc["tracks"]["narration"]["sentences"]
                     if s["sentence_number"] == 2)
    assert sentence2["text"] == "By midday the convoy crossed safely."
    assert 2 in doc["tracks"]["narration"]["dirty_sentences"]
    assert doc["tracks"]["captions"]["stale"] is True

    # 11. set_music — music track now points at the alternate file.
    doc = step([{"type": "set_music",
                 "music_file": str(production["music_alt"])}])
    assert doc["tracks"]["music"]["file"].endswith("music_alt.wav")
    assert doc["tracks"]["music"]["state"] == "applied"

    # 12. set_volumes — both gains recorded on the document.
    doc = step([{"type": "set_volumes", "narration": 0.9, "music": 0.25}])
    assert doc["volumes"] == {"narration": 0.9, "music": 0.25}

    # D3 guarantee: after all twelve operations (13 revisions incl.
    # revision 0), the ORIGINAL run certificate is byte-identical.
    assert _original_certificate_bytes(client) == original_cert_before


# ---------------------------------------------------------------------
# Branching from a non-head revision + parent chain
# ---------------------------------------------------------------------


def test_branching_from_non_head_revision(client, production):
    rev0 = _rev0(client)
    # Advance a head chain two steps so rev0 is NOT the head.
    doc0 = rev0["document"]
    clip_a = _by_name(doc0, "clip_a.mp4", sentence=1)
    mid = _apply(client, rev0["revision_id"],
                 [{"type": "set_volumes", "music": 0.3}])
    head = _apply(client, mid["revision_id"],
                  [{"type": "edit_caption", "segment_index": 1,
                    "text": "HEAD CAPTION"}])

    original_cert_before = _original_certificate_bytes(client)

    # Branch from the NON-head middle revision.
    branch = _branch(client, mid["revision_id"])
    branch_doc = _get_revision(client, branch["revision_id"])["document"]
    mid_doc = _get_revision(client, mid["revision_id"])["document"]
    assert branch_doc["parent_revision_id"] == mid["revision_id"]
    # The branch is a faithful copy of its parent's document…
    assert branch_doc["tracks"]["visual"] == mid_doc["tracks"]["visual"]
    assert branch_doc["volumes"] == mid_doc["volumes"]
    # …and does NOT contain the head revision's later edit.
    assert branch_doc["tracks"]["captions"]["segments"][1]["text"] != "HEAD CAPTION"

    # Parent chain via the API listing: branch -> mid -> rev0 -> None.
    listing = {r["revision_id"]: r for r in client.get(
        f"/api/v1/projects/{PROJECT}/revisions", headers=AUTH
    ).json()["revisions"]}
    assert listing[branch["revision_id"]]["parent_revision_id"] == mid["revision_id"]
    assert listing[mid["revision_id"]]["parent_revision_id"] == rev0["revision_id"]
    assert listing[rev0["revision_id"]]["parent_revision_id"] is None
    assert listing[head["revision_id"]]["parent_revision_id"] == mid["revision_id"]

    # Editing the branch leaves the head revision untouched.
    _apply(client, branch["revision_id"],
           [{"type": "set_volumes", "narration": 0.5}])
    head_doc = _get_revision(client, head["revision_id"])["document"]
    assert head_doc["volumes"]["narration"] == 1.0
    assert head_doc["tracks"]["captions"]["segments"][1]["text"] == "HEAD CAPTION"

    assert _original_certificate_bytes(client) == original_cert_before


# ---------------------------------------------------------------------
# Undo/redo = revision history navigation (no dedicated endpoint)
# ---------------------------------------------------------------------


def test_undo_redo_is_history_navigation(client, production):
    rev0 = _rev0(client)
    # There is no undo/redo endpoint on the API: both verbs 404.
    for verb in ("undo", "redo"):
        for method in ("get", "post"):
            response = getattr(client, method)(
                f"/api/v1/projects/{PROJECT}/revisions/"
                f"{rev0['revision_id']}/{verb}",
                headers=AUTH,
                **({"json": {}} if method == "post" else {}),
            )
            assert response.status_code in (404, 405), (verb, method, response.text)

    # Build a two-step chain: A (trim) -> B (volumes).
    doc0 = rev0["document"]
    clip_a = _by_name(doc0, "clip_a.mp4", sentence=1)
    rev_a = _apply(client, rev0["revision_id"],
                   [{"type": "trim_clip", "item_id": clip_a["item_id"],
                     "duration": round(clip_a["duration"] - 0.3, 3)}])
    rev_b = _apply(client, rev_a["revision_id"],
                   [{"type": "set_volumes", "music": 0.42}])

    # UNDO, exactly as the frontend does it (page.tsx onUndo):
    # navigate to the current revision's parent and re-read it.
    current = _get_revision(client, rev_b["revision_id"])
    assert current["parent_revision_id"] == rev_a["revision_id"]
    undone = _get_revision(client, current["parent_revision_id"])
    assert undone["document"]["volumes"]["music"] != 0.42  # B's edit gone
    assert _by_name(undone["document"], "clip_a.mp4", 1)["duration"] == \
        _by_name(_get_revision(client, rev_a["revision_id"])["document"],
                 "clip_a.mp4", 1)["duration"]  # A's edit present

    # REDO (page.tsx onRedo): the redo target must be a child of the
    # current revision; navigate to it and re-read — B's edit is back.
    listing = client.get(
        f"/api/v1/projects/{PROJECT}/revisions", headers=AUTH
    ).json()["revisions"]
    redo_target = next(r for r in listing
                       if r["revision_id"] == rev_b["revision_id"])
    assert redo_target["parent_revision_id"] == rev_a["revision_id"]
    redone = _get_revision(client, redo_target["revision_id"])
    assert redone["document"]["volumes"]["music"] == 0.42

    # Editing from the UNDONE state branches: the new revision's
    # parent is A, it is a sibling of B, and B survives unchanged.
    rev_c = _apply(client, rev_a["revision_id"],
                   [{"type": "edit_caption", "segment_index": 0,
                     "text": "BRANCHED FROM UNDONE"}])
    assert rev_c["revision_id"] != rev_b["revision_id"]
    b_after = _get_revision(client, rev_b["revision_id"])
    assert b_after["document"]["volumes"]["music"] == 0.42
    assert b_after["document"]["tracks"]["captions"]["segments"][0]["text"] \
        != "BRANCHED FROM UNDONE"


# ---------------------------------------------------------------------
# Render/export: gap gate, holdover naming, gapless render
# ---------------------------------------------------------------------


def test_render_gap_gate_and_holdover_export(client, production):
    rev0 = _rev0(client)
    branch = _branch(client, rev0["revision_id"])
    branch_doc = _get_revision(client, branch["revision_id"])["document"]
    clip_b = _by_name(branch_doc, "clip_b.mp4", sentence=2)
    gapped = _apply(client, branch["revision_id"],
                    [{"type": "remove_clip", "item_id": clip_b["item_id"],
                      "gap_policy": "leave_gap"}])
    gapped_doc = _get_revision(client, gapped["revision_id"])["document"]

    # Default: the gap gates the export with 422 unresolved_gaps.
    blocked = client.post(
        f"/api/v1/projects/{PROJECT}/revisions/{gapped['revision_id']}/render",
        json={}, headers=AUTH,
    )
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["error"]["code"] == "unresolved_gaps"
    exports = client.get(
        f"/api/v1/projects/{PROJECT}/revisions/{gapped['revision_id']}/exports",
        headers=AUTH,
    ).json()["exports"]
    assert exports == []

    # allow_gaps=true renders; the export record NAMES the gap and
    # records the HOLDOVER that covered it.
    accepted = client.post(
        f"/api/v1/projects/{PROJECT}/revisions/{gapped['revision_id']}/render",
        json={"allow_gaps": True}, headers=AUTH,
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["render_plan"]["assembly"] is True

    exports = client.get(
        f"/api/v1/projects/{PROJECT}/revisions/{gapped['revision_id']}/exports",
        headers=AUTH,
    ).json()["exports"]
    assert len(exports) == 1
    export = exports[0]
    assert export["allow_gaps"] is True
    assert [g["sentence_number"] for g in export["gaps_exported"]] == [2]
    assert len(export["holdovers"]) == 1
    assert export["holdovers"][0]["held_file"].endswith("still.png")
    final = Path(export["file"])
    assert final.is_file()
    assert abs(_probe_duration(final) - gapped_doc["duration_seconds"]) < 0.75


def test_render_gapless_revision_exports_file(client, production):
    rev0 = _rev0(client)
    branch = _branch(client, rev0["revision_id"])
    edited = _apply(client, branch["revision_id"],
                    [{"type": "edit_caption", "segment_index": 0,
                      "text": "GAPLESS EXPORT CAPTION"}])
    edited_doc = _get_revision(client, edited["revision_id"])["document"]
    assert not [i for i in edited_doc["tracks"]["visual"] if i["kind"] == "gap"]

    accepted = client.post(
        f"/api/v1/projects/{PROJECT}/revisions/{edited['revision_id']}/render",
        json={}, headers=AUTH,
    )
    assert accepted.status_code == 202, accepted.text

    exports = client.get(
        f"/api/v1/projects/{PROJECT}/revisions/{edited['revision_id']}/exports",
        headers=AUTH,
    ).json()["exports"]
    assert len(exports) == 1
    assert exports[0]["gaps_exported"] == []
    final = Path(exports[0]["file"])
    assert final.is_file()
    assert abs(_probe_duration(final) - edited_doc["duration_seconds"]) < 0.75
    view = _get_revision(client, edited["revision_id"])
    assert view["status"] == "exported"


# ---------------------------------------------------------------------
# Per-revision certificate vs the original run certificate
# ---------------------------------------------------------------------


def test_revision_certificate_describes_the_revision(client, production):
    rev0 = _rev0(client)
    branch = _branch(client, rev0["revision_id"])
    branch_doc = _get_revision(client, branch["revision_id"])["document"]
    clip_b = _by_name(branch_doc, "clip_b.mp4", sentence=2)
    edited = _apply(client, branch["revision_id"],
                    [{"type": "replace_clip", "item_id": clip_b["item_id"],
                      "replacement": {"file_path": str(production["replacement"])}}])

    response = client.get(
        f"/api/v1/projects/{PROJECT}/revisions/{edited['revision_id']}/certificate",
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    cert = response.json()
    assert cert["certificate_version"] == 2
    assert cert["revision"]["revision_id"] == edited["revision_id"]
    assert cert["revision"]["parent_revision_id"] == branch["revision_id"]
    names = {a["file_name"] for a in cert["final_revision_assets"]}
    assert "replacement.mp4" in names
    assert "clip_b.mp4" not in names
    assert any("EDITED REVISION" in d for d in cert["disclaimers"])

    # The original run certificate is a different document: Stage 4
    # shape (no revision section), still describing the production
    # as produced — clip_b present, replacement absent.
    original = client.get(
        f"/api/v1/projects/{PROJECT}/certificate", headers=AUTH
    )
    assert original.status_code == 200
    original_body = original.json()
    assert "revision" not in original_body
    assert "final_revision_assets" not in original_body
    original_names = {
        Path(a.get("asset_key") or "").name
        for a in original_body.get("assets", [])
    }
    assert "clip_b.mp4" in original_names
    assert "replacement.mp4" not in original_names
    assert original_body["certificate_version"] == 1
    assert cert != original_body
