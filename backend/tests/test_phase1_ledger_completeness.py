"""Phase 1 (Master Implementation Directive) — verdict
propagation, ledger completeness, swap overrides, coverage
reporting.

AM-007: a verdict's own 11-dimension ``dimension_results`` (plus
``era_waived``) must reach the asset report's candidate records
and the ledger's rejected rows verbatim. Before Phase 1,
``build_event`` replaced them with a synthesised 3-key shape.
Gate-rejected candidates carry no verdict, so their records keep
the synthesised all-``not_evaluated`` shape — truthfully
unscored, pinned below.

AM-014 + AM-015: the append-only ledger gains row classes for
verified-but-not-picked and download-failed candidates (before
Phase 1 the certificate silently inherited their absence), and
title / licence_url / retrieved_at are carried into ledger rows
and the certificate; rights-manifest entries carry a per-asset
``warnings`` list naming gaps in their own recorded evidence.

AM-013: a swap whose fresh re-verification REJECTS the
replacement is still applied (user-supplied media workflows
depend on it) but now carries a recorded override reason —
explicit, from the decision record, or the recorded default —
surfaced through the ledger onto the certificate.

AM-018: sentence-level non-coverage always materialises into
``missing_visuals`` with its ``coverage_outcome`` recorded; the
definition lives in the report's ``scalar_definitions``. The
coverage GATE is untouched — this is reporting semantics only.
"""

import json
import shutil
import subprocess
import uuid

import pytest

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from database import Base, SessionLocal, engine
from models import AssetDecision, ProductionRun, Project
from services import media_pipeline as mp
from services import plan_document
from services import rejection_diagnostics as rd
from services import run_ledger
from services import asset_report as ar
from services.visual_registry import VisualRegistry


_DIM_KEYS = {
    "entity", "event", "activity", "location", "temporal",
    "topical", "transcript", "visual", "factual_consistency",
    "source_credibility", "media_quality",
}

_SYNTHESISED = {
    "subject": "not_evaluated",
    "topical": "not_evaluated",
    "era": "not_evaluated",
}


def _dims(**marks):
    dims = {key: "not_evaluated" for key in sorted(_DIM_KEYS)}
    dims.update(marks)
    return dims


# ---------------------------------------------------------------------------
# AM-007 — build_event carries the verdict's own dimensions
# ---------------------------------------------------------------------------

def test_build_event_carries_verdict_dimensions_verbatim():
    dims = _dims(topical="fail", entity="pass")
    record = rd.build_event(
        candidate_id="c1", url="https://media.example.org/c1",
        provider="wikimedia", kind="video", outcome="rejected",
        stage="scoring", reason="below_threshold",
        score=0.2, category="evidence", dimension_results=dims,
        era_waived=True, title="Harbour Archive Photograph",
    )
    assert record["dimension_results"] == dims
    assert record["era_waived"] is True
    assert record["title"] == "Harbour Archive Photograph"


def test_build_event_without_verdict_keeps_synthesised_shape():
    record = rd.build_event(
        candidate_id="c2", url="https://media.example.org/c2",
        provider="wikimedia", kind="video", outcome="rejected",
        stage="metadata_gate", reason="missing_subject",
    )
    assert record["dimension_results"] == _SYNTHESISED
    assert record["era_waived"] is None


def test_pass_records_carry_verdict_dimensions(monkeypatch, tmp_path):
    candidates = [
        {
            "url": f"https://www.youtube.com/watch?v=b{index}",
            "title": f"Berlin Wall crowds {index}",
            "snippet": "berlin wall crowds",
            "provider": "youtube",
        }
        for index in range(2)
    ]
    # No metadata at all: its gate failures are absence-only, so it
    # now proceeds to the scorer and is rejected there on zero
    # topical evidence — its record carries the real verdict
    # dimensions plus the gate failures, not the synthesised shape.
    candidates.append({
        "url": "https://www.youtube.com/watch?v=empty",
        "title": "", "snippet": "", "provider": "youtube",
    })
    monkeypatch.setattr(
        mp, "_uve_scenario_search",
        lambda scenario, cache, pid, need: (list(candidates), [], []),
    )

    def download(candidate, scene_dir, sentence_number, asset_index):
        path = tmp_path / "asset_0.mp4"
        path.write_bytes(b"fake")
        asset = dict(candidate)
        asset.update({
            "file_path": str(path), "path": str(path),
            "type": "video", "visual_type": "video",
            "duration_seconds": 5.0,
        })
        return asset

    monkeypatch.setattr(mp, "_uve_download_any_candidate", download)
    scene_stats = {
        "candidates_considered": 0, "candidates_rejected": 0,
        "downloads_failed": 0, "providers": {}, "kinds": {},
    }
    sentence = {
        "sentence_number": 1, "text": "The Berlin Wall fell in 1989.",
        "role": "action", "subject_id": None,
        "scenarios": [{
            "visual": "berlin wall crowds", "kind": "archival",
            "subject_id": None, "subject": "Berlin Wall",
            "specificity": "representative",
            "search_terms": ["berlin wall"], "queries": ["berlin wall"],
            "place": None, "era": None,
        }],
    }
    beat = mp.build_story_beats([sentence], {})[0]
    shot_need = beat["shot_needs"][0]
    scenario = mp._beat_scenarios(beat)[0]
    asset = mp._uve_search_and_pick(
        scenario, shot_need,
        search_cache={}, project_id="t", bible={},
        registry=VisualRegistry(), used_keys=set(),
        scene_dir=tmp_path, scene_stats=scene_stats,
        beat=beat, beat_videos=[], beat_images=[],
        scenario_index=1, queries=["berlin wall"], review_records=[],
    )
    assert asset is not None
    records = scene_stats["candidate_records"]
    verdict_records = [
        r for r in records if r["outcome"] in ("verified", "accepted")
    ]
    assert verdict_records
    for record in verdict_records:
        assert set(record["dimension_results"]) == _DIM_KEYS
        assert "era_waived" in record
    empty_records = [
        r for r in records if r["url"].endswith("v=empty")
    ]
    assert empty_records
    for record in empty_records:
        assert record["stage"] == "scoring"
        assert record["reason"] == "no_topical_evidence"
        assert set(record["dimension_results"]) == _DIM_KEYS


# ---------------------------------------------------------------------------
# Ledger fixtures (pattern: test_stage2_ledger)
# ---------------------------------------------------------------------------

@pytest.fixture()
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
    with SessionLocal() as session:
        session.query(AssetDecision).delete()
        session.query(ProductionRun).delete()
        session.query(Project).delete()
        session.commit()


def _project_and_run():
    project_id = str(uuid.uuid4())
    with SessionLocal() as session:
        session.add(Project(
            id=project_id, script="One. Two.",
            category="Documentary", status="created",
        ))
        session.commit()
    run_id = run_ledger.begin_run(project_id, "One. Two.")
    return project_id, run_id


def _record(candidate_id, outcome, **overrides):
    record = {
        "candidate_id": candidate_id,
        "url": f"https://media.example.org/{candidate_id}",
        "provider": "wikimedia", "kind": "video",
        "outcome": outcome, "stage": "verification",
        "scene": 1, "sentence_numbers": [1],
        "score": 0.8, "score_basis": "verifier_score",
        "category": "contextual", "specificity": "representative",
        "tier": "direct", "dimension_results": _dims(),
        "era_waived": False, "title": "",
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# AM-007 (ledger half) + AM-014 + AM-015
# ---------------------------------------------------------------------------

def test_ledger_completeness_and_certificate_carry(_ledger_tables, tmp_path):
    project_id, run_id = _project_and_run()
    records = [
        _record(
            "cand_rej", "rejected", stage="scoring",
            primary_rejection_reason="below_threshold", score=0.2,
            dimension_results=_dims(topical="fail"),
            title="Harbour Archive Photograph",
        ),
        _record(
            "cand_vnp", "verified",
            dimension_results=_dims(topical="pass"), era_waived=True,
            title="Verified Creek Photograph", sentence_numbers=[2],
        ),
        _record("cand_acc", "verified"),
        _record("cand_acc", "accepted", stage="selection"),
        _record("cand_dl", "verified"),
        _record(
            "cand_dl", "download_failed", stage="download",
            reason="download_failed",
            segment_failure_cause="no_segment_evidence",
        ),
    ]
    report = {
        "candidate_records": {"records": records},
        "missing_visuals": [
            {"scene": 1, "sentence_number": 5,
             "reason": "no_verified_asset"},
        ],
    }
    media = tmp_path / "accepted.mp4"
    media.write_bytes(b"\x00" * 64)
    accepted_asset = {
        "file_path": str(media), "path": str(media),
        "type": "video", "visual_type": "video",
        "candidate_id": "cand_acc", "title": "Accepted Harbour Footage",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "creator": "Harbour Archive", "source": "wikimedia",
        "source_url": "https://commons.wikimedia.org/wiki/File:Harbour",
        "retrieved_at": "2026-11-21T00:00:00+00:00",
        "rights_status": "recorded",
        "verification": {"accepted": True, "score": 0.9,
                         "category": "contextual"},
    }
    scenes = [{
        "scene_number": 1,
        "videos": [accepted_asset], "images": [],
        "sentences": [{
            "sentence_number": 1,
            "videos": [accepted_asset], "images": [],
        }],
    }]
    inserted = run_ledger.record_decisions(
        run_id, project_id, report, {}, processed_scenes=scenes,
    )
    # selected + missing + rejected + verified_not_picked +
    # download_failed.
    assert inserted == 5

    rows, _total = run_ledger.list_decisions(run_id)
    by_key = {(r["decision_type"], r["asset_key"]): r for r in rows}

    # AM-007: the rejected row carries the verdict's own
    # dimensions, era flag and title — not the synthesised shape.
    rejected = by_key[("rejected", "cand_rej")]
    assert rejected["verification"]["dimension_results"] == _dims(
        topical="fail"
    )
    assert rejected["verification"]["era_waived"] is False
    assert rejected["verification"]["title"] == (
        "Harbour Archive Photograph"
    )

    # AM-015: the verified-but-not-picked candidate has a row of
    # its own class; candidates that were accepted or whose
    # download failed do NOT also appear under it (classes are
    # disjoint by candidate).
    vnp = by_key[("verified_not_picked", "cand_vnp")]
    assert vnp["verification"]["accepted"] is True
    assert vnp["verification"]["title"] == "Verified Creek Photograph"
    assert vnp["verification"]["era_waived"] is True
    assert vnp["rights"] is None
    assert ("verified_not_picked", "cand_acc") not in by_key
    assert ("verified_not_picked", "cand_dl") not in by_key
    failed = by_key[("download_failed", "cand_dl")]
    assert failed["verification"]["segment_failure_cause"] == (
        "no_segment_evidence"
    )

    # AM-014: the accepted row's rights carry title, licence URL
    # and retrieval timestamp.
    accepted_rows = [
        r for r in rows if r["decision_type"] in ("selected", "still")
    ]
    assert len(accepted_rows) == 1
    rights = accepted_rows[0]["rights"]
    assert rights["title"] == "Accepted Harbour Footage"
    assert rights["license_url"] == (
        "https://creativecommons.org/licenses/by/4.0/"
    )
    assert rights["retrieved_at"] == "2026-11-21T00:00:00+00:00"

    # The new classes are counted as their own types and never
    # enter the film-asset rights populations.
    counts = run_ledger.decision_counts(run_id)
    assert counts["by_type"]["verified_not_picked"] == 1
    assert counts["by_type"]["download_failed"] == 1
    assert counts["rights_recorded"] == 1
    assert counts["rights_unknown"] == 0

    # Idempotent per run: a second recording adds nothing.
    assert run_ledger.record_decisions(
        run_id, project_id, report, {}, processed_scenes=scenes,
    ) == 0
    assert len(run_ledger.list_decisions(run_id)[0]) == len(rows)

    # The certificate tells the same complete story.
    cert = plan_document.build_certificate(
        {"id": project_id, "title": "Phase 1",
         "category": "Documentary"},
        {"run_id": run_id, "status": "completed"},
        rows, counts,
    )
    cert_rows = {a["decision_type"]: a for a in cert["assets"]}
    assert cert_rows["selected"]["title"] == "Accepted Harbour Footage"
    assert cert_rows["selected"]["rights"]["license_url"] == (
        "https://creativecommons.org/licenses/by/4.0/"
    )
    assert cert_rows["selected"]["rights"]["retrieved_at"] == (
        "2026-11-21T00:00:00+00:00"
    )
    assert cert_rows["rejected"]["title"] == "Harbour Archive Photograph"
    assert "verified_not_picked" in cert_rows
    assert "download_failed" in cert_rows


def test_am014_rights_manifest_warnings():
    full = {
        "file_path": "/m/full.mp4", "type": "video", "title": "Full",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "creator": "Archive", "source_url": "https://example.org/full",
        "retrieved_at": "2026-11-21T00:00:00+00:00",
    }
    bare = {"file_path": "/m/bare.mp4", "type": "video",
            "license": "unknown"}
    manifest = ar.build_rights_manifest([{
        "scene_number": 1, "videos": [full, bare], "images": [],
        "sentences": [],
    }])
    entries = {e["file_path"]: e for e in manifest}
    assert entries["/m/full.mp4"]["warnings"] == []
    warnings = entries["/m/bare.mp4"]["warnings"]
    assert "unknown_licence" in warnings
    assert "licence_url_not_recorded" in warnings
    assert "creator_not_recorded" in warnings
    assert "retrieved_at_not_recorded" in warnings
    assert "source_url_not_recorded" in warnings
    # A warning never upgrades the recorded licence.
    assert entries["/m/bare.mp4"]["license"] == "unknown"


# ---------------------------------------------------------------------------
# AM-013 — rejected swaps are applied WITH a recorded override
# ---------------------------------------------------------------------------

def _make_video(path, size="640x360", seconds=2):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    subprocess.run(
        [
            ffmpeg, "-y", "-f", "lavfi",
            "-i", f"testsrc=duration={seconds}:size={size}:rate=10",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True, capture_output=True,
    )
    return path


_BY_ID = {
    "ada": {"id": "ada", "canonical": "Ada Lovelace", "aliases": [],
            "type": "person"},
}
_BIBLE = {"entities": list(_BY_ID.values()), "by_id": _BY_ID}
_SCENARIO = {
    "visual": "Ada Lovelace with the analytical engine",
    "kind": "person_activity", "subject_id": "ada",
    "specificity": "representative",
    "search_terms": ["analytical engine"],
}


def _original_asset():
    return {
        "file_path": "/orig/verified.mp4", "path": "/orig/verified.mp4",
        "type": "video", "candidate_id": "cand_original",
        "verification": {"accepted": True, "score": 0.91},
        "verification_status": "verified",
        "license": "CC BY 4.0", "creator": "Some Archive",
        "source": "wikimedia",
    }


def _run_swap(tmp_path, decision):
    replacement = _make_video(tmp_path / "replacement.mp4")
    decision = dict(decision, swap_path=str(replacement))
    (tmp_path / "scene_1_decisions.json").write_text(
        json.dumps([decision]), encoding="utf-8"
    )
    sentence = {
        "sentence_number": 1, "beat_id": "b1",
        "videos": [_original_asset()], "images": [], "visuals": [],
        "sentence": {
            "sentence": "Ada Lovelace designed an engine.",
            "scenarios": [dict(_SCENARIO)],
        },
    }
    result = {
        "scene_number": 1,
        "beats": [{"beat_id": "b1", "sentence_numbers": [1]}],
        "sentences": [sentence],
        "videos": [_original_asset()], "images": [],
        "visuals": [dict(_original_asset(), visual_type="video")],
    }
    return mp.apply_review_decisions(result, tmp_path, bible=_BIBLE)


def test_am013_rejected_swap_records_explicit_override(tmp_path):
    out = _run_swap(tmp_path, {
        "sentence_number": 1, "beat_id": "b1", "approve": True,
        "title": "Cooking show episode",
        "snippet": "recipes and kitchens",
        "override_reason": "Founder supplied this exact clip",
    })
    swapped = out["sentences"][0]["videos"][0]
    # The swap is still applied (no hard block)…
    assert swapped["verification"]["accepted"] is False
    assert swapped["verification_status"] == "swapped"
    # …but the override is recorded, with the fresh verdict's own
    # rejection reason beside the human reason.
    stamp = swapped["swap_override"]
    assert stamp["applied_despite_rejection"] is True
    assert stamp["override_reason"] == "Founder supplied this exact clip"
    verdict = swapped["verification"]
    assert stamp["rejection_reason"] == (
        verdict.get("rejection_reason") or verdict.get("reason")
    )
    assert stamp["rejection_reason"]
    assert out["review_decision_outcomes"][0]["swap_override"] == stamp


def test_am013_rejected_swap_records_default_override(tmp_path):
    out = _run_swap(tmp_path, {
        "sentence_number": 1, "beat_id": "b1", "approve": True,
        "title": "Cooking show episode",
        "snippet": "recipes and kitchens",
    })
    swapped = out["sentences"][0]["videos"][0]
    assert swapped["verification"]["accepted"] is False
    assert swapped["swap_override"]["override_reason"] == (
        mp.SWAP_OVERRIDE_DEFAULT_REASON
    )


def test_am013_accepted_swap_carries_no_override(tmp_path):
    out = _run_swap(tmp_path, {
        "sentence_number": 1, "beat_id": "b1", "approve": True,
        "title": "Ada Lovelace and the analytical engine",
        "snippet": "Ada Lovelace designs the analytical engine",
    })
    swapped = out["sentences"][0]["videos"][0]
    assert swapped["verification"]["accepted"] is True
    assert swapped.get("swap_override") is None
    assert out["review_decision_outcomes"][0]["swap_override"] is None


def test_am013_override_reaches_ledger_and_certificate(
    _ledger_tables, tmp_path
):
    project_id, run_id = _project_and_run()
    media = tmp_path / "swap.mp4"
    media.write_bytes(b"\x00" * 32)
    refreshed = {
        "file_path": str(media), "path": str(media), "type": "video",
        "candidate_id": "swap_clip", "license": "unknown",
        "creator": "user_supplied", "source": "user_supplied",
        "verification": {
            "accepted": False, "score": 0.1,
            "rejection_reason": "below_threshold",
        },
        "swap_override": {
            "applied_despite_rejection": True,
            "rejection_reason": "below_threshold",
            "override_reason": "Founder supplied this exact clip",
        },
    }
    row_id = run_ledger.record_swap_decisions(
        run_id, project_id, scene_number=1, sentence_number=1,
        original={
            "candidate_id": "cand_original",
            "file_path": "/orig/verified.mp4",
        },
        refreshed=refreshed,
    )
    assert row_id is not None
    rows, _total = run_ledger.list_decisions(run_id)
    row = [r for r in rows if r["decision_type"] == "swapped_in"][0]
    assert row["verification"]["swap_override"]["override_reason"] == (
        "Founder supplied this exact clip"
    )
    counts = run_ledger.decision_counts(run_id)
    cert = plan_document.build_certificate(
        {"id": project_id, "title": "Phase 1",
         "category": "Documentary"},
        {"run_id": run_id, "status": "completed"},
        rows, counts,
    )
    cert_row = [
        a for a in cert["assets"] if a["decision_type"] == "swapped_in"
    ][0]
    assert cert_row["verification"]["swap_override"][
        "applied_despite_rejection"
    ] is True


# ---------------------------------------------------------------------------
# AM-018 — coverage materialises into missing_visuals, with outcomes
# ---------------------------------------------------------------------------

def _stats_scene(missing):
    return {
        "scene_number": 1, "videos": [], "images": [], "sentences": [],
        "asset_stats": {
            "missing_visuals": missing, "candidate_records": [],
            "candidates_considered": 0, "candidates_rejected": 0,
            "downloads_failed": 0, "providers": {}, "kinds": {},
        },
    }


def _timeline(items, uncovered, total=3):
    return {
        "duration_seconds": 15.0, "sentence_count": total,
        "items": items,
        "scenes": [{
            "scene_number": 1,
            "sentence_timings": [
                {"start": 0.0, "end": 5.0},
                {"start": 5.0, "end": 10.0},
                {"start": 10.0, "end": 15.0},
            ],
        }],
        "coverage": {
            "total_sentences": total,
            "covered_sentences": total - len(uncovered),
            "complete": not uncovered,
            "uncovered_sentences": uncovered,
        },
    }


def test_am018_uncovered_sentences_materialise_with_outcomes():
    # The pre-fix Apollo shape: retrieval recorded nothing missing
    # for the starved sentences, yet the timeline's coverage block
    # listed them uncovered — the two records contradicted each
    # other. Plus a film-1 shape: a retrieval-missing sentence the
    # rotation pool went on to cover.
    report = ar.build_asset_report(
        [_stats_scene([
            {"sentence_number": 2, "reason": "no_verified_asset"},
        ])],
        timeline_result=_timeline(
            [{
                "scene_number": 1, "sentence_index": 1,
                "assignment": "rotation", "duration": 5.0,
                "file_path": "/m/rot.mp4", "type": "video",
            }],
            [{"sentence_number": 3, "scene_number": 1,
              "start": 10.0, "end": 15.0}],
        ),
    )
    by_sentence = {
        e["sentence_number"]: e for e in report["missing_visuals"]
    }
    assert by_sentence[2]["coverage_outcome"] == "covered_by_rotation"
    assert by_sentence[3]["reason"] == "no_timeline_coverage"
    assert by_sentence[3]["coverage_outcome"] == "uncovered"
    assert by_sentence[3]["start"] == 10.0
    assert report["missing_visual_count"] == 2
    # The rule is written down where the report defines its
    # scalars.
    assert "missing_visuals" in report["scalar_definitions"]


def test_am018_graphic_cover_is_marked_as_such():
    report = ar.build_asset_report(
        [_stats_scene([
            {"sentence_number": 1, "reason": "no_verified_asset"},
        ])],
        timeline_result=_timeline(
            [{
                "scene_number": 1, "sentence_index": 0,
                "assignment": "motion_graphic", "duration": 5.0,
                "file_path": "/m/graphic.mp4", "type": "video",
            }],
            [],
        ),
    )
    assert report["missing_visuals"][0]["coverage_outcome"] == (
        "covered_by_graphic"
    )
    assert report["missing_visual_count"] == 1


def test_am018_without_timeline_outcomes_are_unknown_not_invented():
    report = ar.build_asset_report(
        [_stats_scene([
            {"sentence_number": 2, "reason": "no_verified_asset"},
        ])],
        timeline_result=None,
    )
    assert report["missing_visual_count"] == 1
    assert report["missing_visuals"][0]["coverage_outcome"] is None


def test_am018_complete_coverage_adds_nothing():
    report = ar.build_asset_report(
        [_stats_scene([])],
        timeline_result=_timeline([], []),
    )
    assert report["missing_visuals"] == []
    assert report["missing_visual_count"] == 0
