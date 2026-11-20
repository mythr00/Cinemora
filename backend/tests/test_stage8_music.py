"""Stage H — music correction & provenance (M1–M10).

Covers the ONE music contract (services.music.resolve_music_
request), the explicit soundtrack states (applied / not_requested
/ disabled / requested_but_missing / failed), truthful track
identity, music in every provenance surface (asset report, rights
manifest, run manifest, ledger, both certificates, stage
outputs), the editor's revision-specific soundtrack record (M8),
final-MP4 loudness/true-peak measurement (M9) and the auditor's
provenance-mismatch verdict (M10).

Editor integration runs against the Stage 6 synthetic production
(real Piper narration, real renders) in this module's own media
root.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import subprocess
import wave
from pathlib import Path

import pytest

import stage6_fixture as fx
from services import music
from services.asset_report import build_asset_report, build_rights_manifest
from services.final_video_auditor import _audit_verdict
from services.run_ledger import enrich_manifest


# ---------------------------------------------------------------
# helpers
# ---------------------------------------------------------------

def _narration_with_gap(path, rate=22050):
    """4s narration: tone 0-1.5s, silence 1.5-2.5s, tone 2.5-4s."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = b""
        for i in range(rate * 4):
            t = i / rate
            value = (
                int(0.5 * 32767 * math.sin(2 * math.pi * 440 * t))
                if (t < 1.5 or t >= 2.5)
                else 0
            )
            frames += struct.pack("<h", value)
        wav.writeframes(frames)
    return path


@pytest.fixture(scope="module")
def tone_music(tmp_path_factory):
    out = tmp_path_factory.mktemp("stage8-music") / "tone_music.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i",
            "sine=frequency=220:duration=3:sample_rate=48000",
            str(out),
        ],
        check=True, timeout=60,
    )
    return out


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------
# M1/M3: resolve_music_request — the one contract
# ---------------------------------------------------------------

def test_resolve_music_file_wins_over_legacy(tmp_path):
    (tmp_path / "background.mp3").write_bytes(b"legacy")
    track = tmp_path / "chosen.wav"
    track.write_bytes(b"chosen")
    decision = music.resolve_music_request(
        {"music_file": str(track)}, tmp_path
    )
    assert decision["file"] == str(track)
    assert decision["requested"] is True
    assert decision["decided_by"] == "music_file"
    assert decision["source_type"] == "operator_placed"
    assert decision["disabled"] is False


def test_resolve_disabled_beats_file_and_legacy(tmp_path):
    # M1's core case (D2): a stray background.mp3 must NOT override
    # an explicit off — and neither may an explicit music_file.
    (tmp_path / "background.mp3").write_bytes(b"legacy")
    track = tmp_path / "chosen.wav"
    track.write_bytes(b"chosen")
    decision = music.resolve_music_request(
        {"music_enabled": False, "music_file": str(track)}, tmp_path
    )
    assert decision["disabled"] is True
    assert decision["file"] is None
    assert decision["decided_by"] == "music_enabled_false"


def test_resolve_legacy_pickup_only_when_undecided(tmp_path):
    (tmp_path / "background.mp3").write_bytes(b"legacy")
    decision = music.resolve_music_request({}, tmp_path)
    assert decision["file"] == str(tmp_path / "background.mp3")
    assert decision["decided_by"] == "legacy_background_mp3"
    assert decision["source_type"] == "legacy_project_file"
    assert decision["requested"] is False


def test_resolve_not_requested_by_default(tmp_path):
    decision = music.resolve_music_request({}, tmp_path)
    assert decision["file"] is None
    assert decision["decided_by"] == "none"
    assert decision["disabled"] is False


def test_resolve_legacy_music_bool_alias(tmp_path):
    # The old frontend bool is honoured as music_enabled (D9) —
    # never silently ignored.
    decision = music.resolve_music_request({"music": False}, tmp_path)
    assert decision["disabled"] is True
    decision = music.resolve_music_request({"music": True}, tmp_path)
    assert decision["disabled"] is False


def test_resolve_volume_validation(tmp_path):
    decision = music.resolve_music_request(
        {"music_volume": 0.25, "narration_volume": 1.5}, tmp_path
    )
    assert decision["music_volume"] == 0.25
    assert decision["narration_volume"] == 1.5
    with pytest.raises(ValueError):
        music.resolve_music_request({"music_volume": 2.5}, tmp_path)
    with pytest.raises(ValueError):
        music.resolve_music_request({"narration_volume": "loud"}, tmp_path)


# ---------------------------------------------------------------
# M4/M6: prepare_soundtrack states + identity
# ---------------------------------------------------------------

def test_prepare_applied_record_identity(tmp_path, tone_music):
    narration_file = _narration_with_gap(tmp_path / "narration.wav")
    out = tmp_path / "soundtrack.wav"
    record = music.prepare_soundtrack(
        str(narration_file), str(out), music_file=str(tone_music),
        source_type="operator_placed", decided_by="music_file",
        requested=True,
    )
    assert record["state"] == "applied"
    assert record["music"] == "mixed"  # legacy keys preserved
    assert record["source"] == "tone_music.wav"
    identity = record["identity"]
    assert identity["file_name"] == "tone_music.wav"
    assert identity["content_sha256"] == _sha256(tone_music)
    assert identity["duration_seconds"] == pytest.approx(3.0, abs=0.2)
    assert identity["codec"] == "pcm_s16le"
    assert identity["sample_rate"] == 48000
    assert identity["source_type"] == "operator_placed"
    # M6: licence is unknown — recorded, never invented/upgraded.
    assert identity["license"] == "unknown"
    assert identity["license_source"] is None
    assert record["requested"] is True
    assert record["decided_by"] == "music_file"


def test_prepare_states_are_distinct(tmp_path, tone_music):
    narration_file = _narration_with_gap(tmp_path / "narration.wav")

    plain = music.prepare_soundtrack(
        str(narration_file), str(tmp_path / "plain.wav")
    )
    assert plain["state"] == "not_requested"
    assert plain["identity"] is None

    disabled = music.prepare_soundtrack(
        str(narration_file), str(tmp_path / "disabled.wav"),
        disabled=True, decided_by="music_enabled_false",
    )
    assert disabled["state"] == "disabled"
    assert disabled["music"] == "absent"

    missing = music.prepare_soundtrack(
        str(narration_file), str(tmp_path / "missing.wav"),
        music_file=str(tmp_path / "nope.mp3"),
        source_type="operator_placed", decided_by="music_file",
        requested=True,
    )
    # D8 closed: requested-but-missing is visibly NOT not_requested.
    assert missing["state"] == "requested_but_missing"
    assert missing["state"] != plain["state"]
    assert missing["requested_file"].endswith("nope.mp3")
    assert "nope.mp3" in missing["error"]


def test_prepare_corrupt_music_fails_loudly(tmp_path):
    # D10 asymmetry, deliberate: missing degrades (above), corrupt
    # raises so the caller records state "failed" and fails the run.
    narration_file = _narration_with_gap(tmp_path / "narration.wav")
    corrupt = tmp_path / "corrupt.wav"
    corrupt.write_bytes(b"this is not audio at all" * 64)
    with pytest.raises(Exception):
        music.prepare_soundtrack(
            str(narration_file), str(tmp_path / "out.wav"),
            music_file=str(corrupt), requested=True,
        )
    failed = music.failed_music_record(
        requested_file=str(corrupt), source_type="operator_placed",
        decided_by="music_file", requested=True,
        error="FFmpeg failed",
    )
    assert failed["state"] == "failed"
    assert failed["identity"]["content_sha256"] == _sha256(corrupt)
    assert failed["identity"]["duration_seconds"] is None
    assert failed["identity"]["license"] == "unknown"


def test_jobs_sound_designer_sequence_missing_and_disabled(tmp_path):
    """The exact call sequence jobs.py's Sound Designer stage makes
    (resolve -> prepare), for the two record-critical cases."""
    narration_file = _narration_with_gap(tmp_path / "narration.wav")

    # Requested-but-missing completes honestly (no raise).
    decision = music.resolve_music_request(
        {"music_file": str(tmp_path / "ghost.mp3")}, tmp_path
    )
    record = music.prepare_soundtrack(
        str(narration_file), str(tmp_path / "sd1.wav"),
        music_file=decision["file"],
        source_type=decision["source_type"],
        decided_by=decision["decided_by"],
        requested=decision["requested"],
        disabled=decision["disabled"],
    )
    assert record["state"] == "requested_but_missing"

    # Disabled with a legacy file present: narration only.
    (tmp_path / "background.mp3").write_bytes(b"legacy")
    decision = music.resolve_music_request(
        {"music_enabled": False}, tmp_path
    )
    record = music.prepare_soundtrack(
        str(narration_file), str(tmp_path / "sd2.wav"),
        music_file=decision["file"],
        source_type=decision["source_type"],
        decided_by=decision["decided_by"],
        requested=decision["requested"],
        disabled=decision["disabled"],
    )
    assert record["state"] == "disabled"
    assert record["identity"] is None


# ---------------------------------------------------------------
# M9: final-file measurement
# ---------------------------------------------------------------

def test_measure_audio_levels_real_file(tmp_path, tone_music):
    narration_file = _narration_with_gap(tmp_path / "narration.wav")
    out = tmp_path / "soundtrack.wav"
    music.prepare_soundtrack(
        str(narration_file), str(out), music_file=str(tone_music)
    )
    levels = music.measure_audio_levels(out)
    assert isinstance(levels["integrated_lufs"], float)
    assert isinstance(levels["true_peak_dbtp"], float)


def test_measure_audio_levels_missing_file_is_unknown(tmp_path):
    levels = music.measure_audio_levels(tmp_path / "missing.wav")
    assert levels == {"integrated_lufs": None, "true_peak_dbtp": None}


def test_final_audio_record_targets(tmp_path, monkeypatch):
    narration_file = _narration_with_gap(tmp_path / "narration.wav")
    record = music.final_audio_record(narration_file)
    assert record["target_lufs"] == -14.0
    assert record["max_true_peak_dbtp"] == -1.0
    assert isinstance(record["integrated_lufs"], float)
    monkeypatch.setenv("CINEMORA_TARGET_LUFS", "-16")
    monkeypatch.setenv("CINEMORA_MAX_TRUE_PEAK_DBTP", "-2")
    record = music.final_audio_record(narration_file)
    assert record["target_lufs"] == -16.0
    assert record["max_true_peak_dbtp"] == -2.0


# ---------------------------------------------------------------
# M10: difference measurement + provenance assessment
# ---------------------------------------------------------------

def test_difference_and_provenance(tmp_path, tone_music):
    narration_file = _narration_with_gap(tmp_path / "narration.wav")
    mixed = tmp_path / "mixed.wav"
    music.prepare_soundtrack(
        str(narration_file), str(mixed), music_file=str(tone_music)
    )
    plain = tmp_path / "plain.wav"
    music.prepare_soundtrack(str(narration_file), str(plain))

    # Stage J deliberate update: the narration-only soundtrack is
    # now loudness-NORMALISED (a pure gain change), so the RAW
    # global difference against the un-normalised narration
    # measures the gain, not the content — it is reported, never
    # decisive (see audio_difference_analysis). Content identity
    # is asserted through the gain-matched gap statistic below:
    # the plain soundtrack's gaps differ from the narration's by
    # codec noise only.
    identical = music.audio_difference_rms_dbfs(plain, narration_file)
    assert identical is not None  # measurable; value is gain-dominated
    # The GLOBAL difference is deliberately NOT the detector (a
    # ducked bed dilutes into it); the narration-gap statistic is.
    analysis = music.audio_difference_analysis(mixed, narration_file)
    assert analysis["detection_basis"] == "narration_gaps"
    assert analysis["gap_difference_rms_dbfs"] > -65.0
    plain_analysis = music.audio_difference_analysis(plain, narration_file)
    assert plain_analysis["gap_difference_rms_dbfs"] < -65.0

    ok = music.assess_audio_provenance("applied", mixed, narration_file)
    assert ok["music_detected"] is True
    assert ok["consistent"] is True

    # Records claim music; the audio has none -> mismatch.
    bad = music.assess_audio_provenance("applied", plain, narration_file)
    assert bad["music_detected"] is False
    assert bad["consistent"] is False

    # Audio has music; records deny it -> mismatch the other way.
    bad2 = music.assess_audio_provenance(
        "not_requested", mixed, narration_file
    )
    assert bad2["consistent"] is False

    ok2 = music.assess_audio_provenance("disabled", plain, narration_file)
    assert ok2["consistent"] is True

    # Unverifiable is None, never a guessed verdict.
    unknown = music.assess_audio_provenance(
        "applied", tmp_path / "gone.mp4", narration_file
    )
    assert unknown["consistent"] is None
    assert unknown["music_detected"] is None


# ---------------------------------------------------------------
# M9/M10: the auditor's audio verdict section
# ---------------------------------------------------------------

def _minimal_report(**extra):
    report = {
        "coverage": {"complete": True, "uncovered_sentences": []},
        "substitutions": {"text_cards": 0, "placeholder_clips": 0},
        "segment_retrieval": [],
        "timeline_items": 0,
        "rotation_assignments": 0,
        "missing_visuals": [],
    }
    report.update(extra)
    return report


def test_verdict_fails_on_provenance_mismatch():
    verdict = _audit_verdict(_minimal_report(
        soundtrack={"state": "applied"},
        final_audio={
            "integrated_lufs": -17.0, "true_peak_dbtp": -3.0,
            "target_lufs": -14.0, "max_true_peak_dbtp": -1.0,
        },
        audio_provenance={
            "claimed_state": "applied", "music_detected": False,
            "difference_rms_dbfs": -58.0, "gap_difference_rms_dbfs": -90.0,
            "threshold_dbfs": -65.0,
            "consistent": False,
        },
    ))
    assert verdict["passed"] is False
    assert any(
        f.startswith("provenance_mismatch") for f in verdict["failures"]
    )
    assert verdict["checks"]["audio"]["provenance_consistent"] is False


def test_verdict_loudness_value_never_fails():
    # A very quiet film with consistent records passes: loudness is
    # measured and reported (M9), never judged (Stage J's job).
    verdict = _audit_verdict(_minimal_report(
        soundtrack={"state": "not_requested"},
        final_audio={
            "integrated_lufs": -42.0, "true_peak_dbtp": -20.0,
            "target_lufs": -14.0, "max_true_peak_dbtp": -1.0,
        },
        audio_provenance={
            "claimed_state": "not_requested", "music_detected": False,
            "difference_rms_dbfs": -60.0, "gap_difference_rms_dbfs": -88.0,
            "threshold_dbfs": -65.0,
            "consistent": True,
        },
    ))
    assert verdict["passed"] is True
    assert verdict["checks"]["audio"]["final_loudness_lufs"] == -42.0


def test_verdict_unverifiable_provenance_warns_not_fails():
    verdict = _audit_verdict(_minimal_report(
        soundtrack={"state": "applied"},
        final_audio={
            "integrated_lufs": -15.0, "true_peak_dbtp": -2.0,
            "target_lufs": -14.0, "max_true_peak_dbtp": -1.0,
        },
        audio_provenance={
            "claimed_state": "applied", "music_detected": None,
            "difference_rms_dbfs": None, "gap_difference_rms_dbfs": None,
            "threshold_dbfs": -65.0,
            "consistent": None,
        },
    ))
    assert verdict["passed"] is True
    assert any(
        "provenance could not be verified" in w
        for w in verdict["warnings"]
    )


def test_verdict_pre_stageh_report_skips_audio():
    verdict = _audit_verdict(_minimal_report())
    assert verdict["checks"]["audio"] == {}
    assert verdict["passed"] is True


# ---------------------------------------------------------------
# M7: provenance surfaces (report / manifest / ledger / certs)
# ---------------------------------------------------------------

def _applied_record(tmp_path, tone_music):
    narration_file = _narration_with_gap(tmp_path / "narration.wav")
    return music.prepare_soundtrack(
        str(narration_file), str(tmp_path / "soundtrack.wav"),
        music_file=str(tone_music), source_type="operator_placed",
        decided_by="music_file", requested=True,
    )


def test_asset_report_carries_final_audio_and_provenance(tmp_path, tone_music):
    record = _applied_record(tmp_path, tone_music)
    final_audio = {"integrated_lufs": -15.5, "true_peak_dbtp": -1.2,
                   "target_lufs": -14.0, "max_true_peak_dbtp": -1.0,
                   "file_name": "final.mp4"}
    provenance = {"claimed_state": "applied", "music_detected": True,
                  "difference_rms_dbfs": -25.0, "consistent": True}
    report = build_asset_report(
        [], soundtrack=record, final_audio=final_audio,
        audio_provenance=provenance,
    )
    assert report["soundtrack"]["state"] == "applied"
    assert report["final_audio"] == final_audio
    assert report["audio_provenance"] == provenance


def test_rights_manifest_music_entry(tmp_path, tone_music):
    record = _applied_record(tmp_path, tone_music)
    manifest = build_rights_manifest([], soundtrack=record)
    assert len(manifest) == 1
    entry = manifest[0]
    assert entry["type"] == "music"
    assert entry["license"] == "unknown"
    assert entry["source"] == "operator_placed"
    assert entry["content_sha256"] == _sha256(tone_music)
    # Narration-only states add no entry: the manifest still covers
    # exactly the accepted assets.
    quiet = build_rights_manifest(
        [], soundtrack={"state": "not_requested", "identity": None}
    )
    assert quiet == []


def test_enrich_manifest_music_summary(tmp_path, tone_music):
    record = _applied_record(tmp_path, tone_music)
    report = build_asset_report(
        [], soundtrack=record,
        final_audio={"integrated_lufs": -15.0, "true_peak_dbtp": -2.0,
                     "target_lufs": -14.0, "max_true_peak_dbtp": -1.0},
    )
    enriched = enrich_manifest(
        {"run_id": "r"}, status="completed", asset_report=report,
        stage_outputs=["soundtrack.wav", "soundtrack.json"],
    )
    assert enriched["music"]["state"] == "applied"
    assert enriched["music"]["file_name"] == "tone_music.wav"
    assert enriched["music"]["content_sha256"] == _sha256(tone_music)
    assert enriched["music"]["license"] == "unknown"
    assert enriched["final_audio"]["integrated_lufs"] == -15.0
    assert "soundtrack.json" in enriched["stage_outputs"]


@pytest.fixture()
def ledger_run():
    from uuid import uuid4

    from database import SessionLocal, engine
    from models import Base, Project

    Base.metadata.create_all(engine)
    project_id = f"proj-stage8-{uuid4().hex[:8]}"
    db = SessionLocal()
    try:
        db.add(Project(id=project_id, script="One. Two.",
                       category="history", status="completed"))
        db.commit()
    finally:
        db.close()
    from services import run_ledger

    run_id = run_ledger.begin_run(project_id, "One. Two.", options={})
    run_ledger.finish_run(run_id, "completed")
    return run_id, project_id


def test_record_music_decision_idempotent_and_filtered(
    tmp_path, tone_music, ledger_run
):
    from services import run_ledger

    run_id, project_id = ledger_run
    record = _applied_record(tmp_path, tone_music)
    first = run_ledger.record_music_decision(
        run_id, project_id, record
    )
    second = run_ledger.record_music_decision(
        run_id, project_id, record
    )
    assert first is not None and first == second

    rows, total = run_ledger.list_decisions(
        run_id, 100, 0, include_revision_decisions=False
    )
    music_rows = [r for r in rows if r["decision_type"] == "music"]
    assert len(music_rows) == 1
    assert music_rows[0]["rights"]["license"] == "unknown"
    assert music_rows[0]["verification"]["status"] == "applied"
    assert music_rows[0]["content_hash"] == _sha256(tone_music)

    # A revision-tagged music row is invisible to the run's own
    # decision list (D3 discipline) but present in the chain list.
    run_ledger.record_music_decision(
        run_id, project_id, record, revision_id="rev-x"
    )
    rows, _ = run_ledger.list_decisions(
        run_id, 100, 0, include_revision_decisions=False
    )
    assert len([r for r in rows if r["decision_type"] == "music"]) == 1
    chain = run_ledger.list_decisions_for_revision(
        project_id, run_id, ["rev-x"]
    )
    assert len([r for r in chain if r["decision_type"] == "music"]) == 2


def test_certificate_music_section(tmp_path, tone_music, ledger_run):
    from services import plan_document, run_ledger

    run_id, project_id = ledger_run
    record = _applied_record(tmp_path, tone_music)
    run_ledger.record_music_decision(run_id, project_id, record)
    decisions, _ = run_ledger.list_decisions(
        run_id, 100, 0, include_revision_decisions=False
    )
    counts = run_ledger.decision_counts(
        run_id, include_revision_decisions=False
    )
    project = {"id": project_id, "title": "T", "category": "history"}
    run = run_ledger.get_run(run_id)
    cert = plan_document.build_certificate(project, run, decisions, counts)
    assert cert["music"]["state"] == "applied"
    assert cert["music"]["file_name"] == "tone_music.wav"
    assert cert["music"]["content_sha256"] == _sha256(tone_music)
    assert cert["music"]["license"] == "unknown"
    assert cert["music"]["source_type"] == "operator_placed"
    # The music row is certified in its section, not listed as a
    # per-sentence visual asset.
    assert all(
        row["decision_type"] != "music" for row in cert["assets"]
    )

    empty = plan_document.build_certificate(project, run, [], counts)
    assert empty["music"] == {"state": "not_recorded"}

    revised = plan_document.build_revision_certificate(
        project, run, {"revision_id": "rev-1"}, decisions, counts, [],
        music={"state": "disabled", "file_name": None,
               "license": "unknown"},
    )
    assert revised["music"]["state"] == "disabled"
    assert revised["certificate_version"] == 2


# ---------------------------------------------------------------
# M5: the dead duplicate is gone
# ---------------------------------------------------------------

def test_single_mix_implementation():
    assert not hasattr(music, "add_background_music")
    assert hasattr(music, "prepare_soundtrack")


# ---------------------------------------------------------------
# Editor integration (M1/M7/M8) — Stage 6 synthetic production
# ---------------------------------------------------------------

class SyncQueue:
    """Executes enqueued jobs immediately (worker stand-in)."""

    def enqueue(self, fn, *args, **kwargs):
        from types import SimpleNamespace

        kwargs.pop("at_front", None)
        fn(*args, **kwargs)
        return SimpleNamespace(id="job-sync")


@pytest.fixture()
def sync_queue(monkeypatch):
    import jobs

    monkeypatch.setattr(jobs, "video_queue", SyncQueue())
    return jobs.video_queue


@pytest.fixture(scope="module")
def production(tmp_path_factory):
    root = tmp_path_factory.mktemp("stage8-media")
    os.environ["CINEMORA_MEDIA_ROOT"] = str(root)
    built = fx.build_synthetic_production(root, "proj-stage8-editor")
    built["root"] = root
    return built


def _rev0(project_id):
    from services import editor

    editor.get_editor(project_id)
    for view in editor.list_revisions(project_id):
        if view["revision_number"] == 0:
            return editor.get_revision(project_id, view["revision_id"])
    raise AssertionError("no revision 0")


def _run_certificate(project_id, run_id):
    from services import plan_document, run_ledger

    decisions, _ = run_ledger.list_decisions(
        run_id, 100_000, 0, include_revision_decisions=False
    )
    counts = run_ledger.decision_counts(
        run_id, include_revision_decisions=False
    )
    cert = plan_document.build_certificate(
        {"id": project_id, "title": "Fixture", "category": "history"},
        run_ledger.get_run(run_id),
        decisions,
        counts,
    )
    return json.dumps(cert, sort_keys=True, default=str)


def test_rev0_import_carries_music_state_and_record(production):
    _view, doc = _rev0(production["project_id"])
    track = doc["tracks"]["music"]
    assert track["file"].endswith("music.wav")
    assert track["state"] == "applied"
    assert track["license"] == "unknown"
    record = track["soundtrack_record"]
    assert record["state"] == "applied"
    assert record["identity"]["content_sha256"] == _sha256(
        production["music"]
    )


def test_editor_music_render_records_everywhere(production, sync_queue):
    from services import editor, run_ledger

    project_id = production["project_id"]
    run_id = production["run_id"]

    # Simulate the production-side ledger record (jobs does this
    # after record_decisions), then snapshot the run certificate.
    production_record = json.loads(
        (production["project_dir"] / "soundtrack.json").read_text()
    )
    run_ledger.record_music_decision(run_id, project_id, production_record)
    cert_before = _run_certificate(project_id, run_id)
    cert = json.loads(cert_before)
    assert cert["music"]["state"] == "applied"
    assert cert["music"]["file_name"] == "music.wav"

    # Change the music in a revision and render it.
    _view0, doc0 = _rev0(project_id)
    view1, _doc1 = editor.apply_operations(
        project_id,
        doc0["revision_id"],
        [{"type": "set_music",
          "music_file": str(production["music_alt"])}],
    )
    result = editor.request_render(project_id, view1["revision_id"])
    assert result["status"] == "rendering"
    view_after, doc_after = editor.get_revision(
        project_id, view1["revision_id"]
    )
    assert view_after["status"] == "exported"
    export = doc_after["exports"][-1]

    # M8: the revision has its OWN soundtrack record — in its work
    # dir and on its document — naming the NEW track.
    work_dir = (
        production["project_dir"] / "editor_renders"
        / view1["revision_id"]
    )
    revision_record = json.loads(
        (work_dir / "soundtrack.json").read_text()
    )
    assert revision_record["state"] == "applied"
    assert revision_record["identity"]["file_name"] == "music_alt.wav"
    assert revision_record["identity"]["content_sha256"] == _sha256(
        production["music_alt"]
    )
    assert revision_record["identity"]["license"] == "unknown"
    assert revision_record["source_type"] == "project_file"
    doc_record = doc_after["tracks"]["music"]["soundtrack_record"]
    assert doc_record["identity"]["file_name"] == "music_alt.wav"

    # The production's soundtrack.json still describes the
    # production's track — the editor never rewrites it.
    production_record_after = json.loads(
        (production["project_dir"] / "soundtrack.json").read_text()
    )
    assert production_record_after["identity"]["file_name"] == "music.wav"

    # M9: the export record carries the exported file's measured
    # audio; M10: its provenance assessment is consistent.
    levels = export["audio_levels"]
    assert isinstance(levels["integrated_lufs"], float)
    assert isinstance(levels["true_peak_dbtp"], float)
    assert export["audio_provenance"]["consistent"] is True
    assert export["audio_provenance"]["music_detected"] is True

    # M7: the revision's music decision is in the ledger, tagged
    # with the revision; the revision certificate names the new
    # track; the ORIGINAL run certificate is byte-identical.
    chain = run_ledger.list_decisions_for_revision(
        project_id, run_id,
        editor.revision_chain_ids(project_id, view1["revision_id"]),
    )
    revision_music = [
        r for r in chain
        if r["decision_type"] == "music"
        and r["revision_id"] == view1["revision_id"]
    ]
    assert len(revision_music) == 1
    assert revision_music[0]["rights"]["license"] == "unknown"

    from services import plan_document

    counts = run_ledger.counts_for_decisions(chain)
    revision_cert = plan_document.build_revision_certificate(
        {"id": project_id, "title": "Fixture", "category": "history"},
        run_ledger.get_run(run_id),
        view_after,
        chain,
        counts,
        editor.final_assets_for_certificate(doc_after),
        music=editor.music_certificate_data(doc_after),
    )
    assert revision_cert["music"]["file_name"] == "music_alt.wav"
    assert revision_cert["music"]["license"] == "unknown"
    assert _run_certificate(project_id, run_id) == cert_before


def test_editor_disabled_render_ignores_project_music_files(
    production, sync_queue
):
    from services import editor

    project_id = production["project_id"]
    # A stray legacy file in the project dir must not leak into a
    # revision whose music was explicitly removed (M1).
    (production["project_dir"] / "background.mp3").write_bytes(
        b"not even audio"
    )
    _view0, doc0 = _rev0(project_id)
    view1, doc1 = editor.apply_operations(
        project_id,
        doc0["revision_id"],
        [{"type": "set_music", "music_file": None}],
    )
    assert doc1["tracks"]["music"]["state"] == "disabled"
    editor.request_render(project_id, view1["revision_id"])
    view_after, doc_after = editor.get_revision(
        project_id, view1["revision_id"]
    )
    assert view_after["status"] == "exported"
    export = doc_after["exports"][-1]
    work_dir = (
        production["project_dir"] / "editor_renders"
        / view1["revision_id"]
    )
    record = json.loads((work_dir / "soundtrack.json").read_text())
    assert record["state"] == "disabled"
    assert record["identity"] is None
    # The exported audio really is narration-only, and the
    # provenance assessment agrees with the record.
    assert export["audio_provenance"]["music_detected"] is False
    assert export["audio_provenance"]["consistent"] is True


def test_editor_copy_path_guard_rebuilds_on_track_change(
    production, sync_queue
):
    """Regression for the copy-path honesty guard: a child revision
    whose only change is a caption edit inherits its parent's music
    SELECTION — if that selection differs from the production's
    soundtrack, reusing the production soundtrack.wav would put
    the WRONG track in the film. The render must rebuild and the
    revision record must name the selected track."""
    from services import editor

    project_id = production["project_id"]
    _view0, doc0 = _rev0(project_id)
    view1, _doc1 = editor.apply_operations(
        project_id,
        doc0["revision_id"],
        [{"type": "set_music",
          "music_file": str(production["music_alt"])}],
    )
    editor.request_render(project_id, view1["revision_id"])
    _view1, doc1 = editor.get_revision(project_id, view1["revision_id"])

    view2, _doc2 = editor.apply_operations(
        project_id,
        doc1["revision_id"],
        [{"type": "edit_caption", "segment_index": 0,
          "text": "The river crossing began at dawn."}],
    )
    editor.request_render(project_id, view2["revision_id"])
    view_after, doc_after = editor.get_revision(
        project_id, view2["revision_id"]
    )
    assert view_after["status"] == "exported"
    work_dir = (
        production["project_dir"] / "editor_renders"
        / view2["revision_id"]
    )
    record = json.loads((work_dir / "soundtrack.json").read_text())
    assert record["state"] == "applied"
    assert record["identity"]["file_name"] == "music_alt.wav"
    assert doc_after["exports"][-1]["audio_provenance"]["consistent"] is True
