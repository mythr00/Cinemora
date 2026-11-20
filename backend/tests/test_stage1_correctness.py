"""Stage 1 (Critical Evidence & Correctness Fixes) regression tests.

Covers MASTER_AUDIT items:

- MA-36 / MA-37 — the timeline rotation pool and every other
  timeline path apply ONE eligibility standard: rejected assets,
  failed downloads AND assets with no verification record at all
  never reach the screen; the same missing-verdict standard applies
  to the schematic trigger's genuine-visual count.
- MA-27 — plan_script's analysis mode is derived from actual
  outcomes ("llm" / "partial_llm" / "degraded"), never copied from
  the Case Bible alone.
- MA-14 — the final video auditor returns a structured verdict:
  coverage/substitution/evidence failures FAIL; rotation and
  positional shares only WARN (env-configurable limits). Berlin v5
  and Kohberger calibration profiles both pass.
- MA-35 — positional_no_contract assignments carry their evidence
  class on the timeline item, are counted at assignment level in
  the asset report, and warn in the auditor above the share limit.
- MA-33 / MA-34 — review swaps never inherit evidence: fresh probe,
  cleared segment evidence, re-stamped rights, replaced verdict,
  per-sentence decision keying, aggregates synced.
- MA-30 — verify_candidate: a subject-only match with an EMPTY
  topical set is rejected as no_topical_evidence at every
  specificity level (thresholds unchanged).
- MA-29 — _normalize_scenario emits the resolved subject label and
  gate-vocabulary required_type, so the gate's missing-person /
  missing-subject branches evaluate production-shaped scenarios.
"""

import json
import shutil
import subprocess

import pytest

from services import graphics_pipeline
from services import media_pipeline as mp
from services import story_understanding as story
from services.asset_report import build_asset_report
from services.final_video_auditor import validate_final_video
from services.timeline import build_scene_timeline


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _write_visual(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 4096)
    return path


def _asset(path, **extra):
    asset = {"file_path": str(path), "path": str(path), "type": "video"}
    asset.update(extra)
    return asset


def _timings(n, step=5.0):
    return [
        {
            "sentence_index": i,
            "text": f"Sentence {i + 1}.",
            "start": i * step,
            "end": (i + 1) * step,
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# MA-36 / MA-37 — timeline eligibility
# ---------------------------------------------------------------------------

def test_rotation_pool_excludes_rejected_failed_and_verdictless(tmp_path):
    verified = _asset(
        _write_visual(tmp_path, "verified.mp4"),
        verification={"accepted": True},
    )
    rejected = _asset(
        _write_visual(tmp_path, "rejected.mp4"),
        verification={"accepted": False},
    )
    failed = _asset(
        _write_visual(tmp_path, "failed.mp4"),
        verification={"accepted": True},
        _download_error="http_error",
    )
    verdictless = _asset(_write_visual(tmp_path, "verdictless.mp4"))

    scene_media = {
        "scene_number": 1,
        "duration_seconds": 10,
        "videos": [rejected, failed, verdictless, verified],
        "images": [],
        "sentences": [
            {"sentence_number": 1, "videos": [], "images": []},
            {"sentence_number": 2, "videos": [], "images": []},
        ],
    }
    result = build_scene_timeline(scene_media, _timings(2))
    items = result["items"]
    assert items, "the verified asset must still rotate"
    used = {item["file_path"] for item in items}
    assert used == {str(tmp_path / "verified.mp4")}
    for item in items:
        assert item["assignment"] == "rotation"
        # Honest label retained: rotation is never "verified".
        assert item["relevance_status"] == "not_verified_for_this_sentence"


def test_verdictless_sentence_asset_is_not_assigned(tmp_path):
    verified = _asset(
        _write_visual(tmp_path, "verified.mp4"),
        verification={"accepted": True},
    )
    verdictless = _asset(_write_visual(tmp_path, "verdictless.mp4"))

    scene_media = {
        "scene_number": 1,
        "duration_seconds": 10,
        "videos": [verified, verdictless],
        "images": [],
        "sentences": [
            # Sentence 1's only candidate has NO verdict: it must
            # not be assigned as this sentence's verified visual.
            {"sentence_number": 1, "videos": [verdictless], "images": []},
            {"sentence_number": 2, "videos": [verified], "images": []},
        ],
    }
    result = build_scene_timeline(scene_media, _timings(2))
    by_index = {item["sentence_index"]: item for item in result["items"]}
    assert by_index[1]["assignment"] == "sentence"
    assert by_index[1]["relevance_status"] == "verified_for_sentence"
    # Sentence 1 falls back to labelled rotation of the verified
    # pool asset — never to its own verdict-less candidate.
    assert by_index[0]["file_path"] == str(tmp_path / "verified.mp4")
    assert by_index[0]["assignment"] == "rotation"
    used = {item["file_path"] for item in result["items"]}
    assert str(tmp_path / "verdictless.mp4") not in used


def test_verdictless_visual_does_not_suppress_event_schematic(
    tmp_path, monkeypatch
):
    """MA-37 at the graphics_pipeline site: a verdict-less visual in
    the scene must not count as a 'genuine verified visual' that
    suppresses the event schematic."""
    calls = []

    def fake_render(path, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)
        calls.append(kwargs)
        return str(path)

    monkeypatch.setattr(
        "services.vector_graphics.render_event_schematic", fake_render
    )

    def fake_write(path, *args, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 2048)

    monkeypatch.setattr(graphics_pipeline, "_render_stat_graphic", fake_write)

    # MG-05 (Motion Graphics programme Phase 1): registration now
    # validates rendered output before an asset is recorded. These
    # are logic tests whose fake renderers write non-media bytes,
    # so the validator is stubbed at the same seam; the real
    # validator is covered by dedicated real-file tests in
    # tests/test_motion_graphics_programme.py.
    monkeypatch.setattr(
        graphics_pipeline, "validate_graphic_output",
        lambda path, expected_duration=None, **kwargs: {
            "passed": True, "failure_kind": None, "detail": "",
            "checks": {}, "probe": None,
        },
    )

    sentence = {
        "text": "Crowds poured through the opened checkpoint at dawn.",
        "sentence_number": 3,
        "event": "opening of the Berlin Wall",
        "locations": ["Berlin"],
        "dates": ["November 9, 1989"],
        "scenarios": [
            {
                "visual": "Crowds pouring through an opened checkpoint.",
                "kind": "event",
                "event": "opening of the Berlin Wall",
                "place": "berlin",
                "era": "1989",
            }
        ],
    }
    verdictless = {"file_path": "/x/unreviewed.mp4", "type": "video"}
    data = {
        "case_bible": {"timeline": []},
        "scenes": [{"scene_number": 2, "sentences": [sentence]}],
        "processed_scenes": [
            {
                "scene": {"scene_number": 2, "sentences": [sentence]},
                "videos": [verdictless],
                "images": [],
                "asset_stats": {"missing_visuals": []},
            }
        ],
    }
    target = {
        "scene_number": 2,
        "sentence_index": 0,
        "sentence_number": 3,
        "text": sentence["text"],
    }
    assets = graphics_pipeline.build_motion_graphics_assets(
        data, tmp_path, mode="balanced", targets=[target]
    )
    assert [
        a for a in assets if a["graphic_type"] == "event_schematic"
    ], "a verdict-less visual must not count as verified coverage"


# ---------------------------------------------------------------------------
# MA-27 — analysis-mode honesty
# ---------------------------------------------------------------------------

def test_analysis_mode_matrix():
    plan_ok = {"degraded": False}
    plan_bad = {"degraded": True}
    assert story._analysis_mode({"mode": "llm"}, [plan_ok, plan_ok]) == "llm"
    assert story._analysis_mode({"mode": "llm"}, [plan_ok, plan_bad]) == "partial_llm"
    assert story._analysis_mode({"mode": "degraded"}, [plan_ok]) == "partial_llm"
    assert story._analysis_mode({"mode": "degraded"}, [plan_bad]) == "degraded"
    assert story._analysis_mode({"mode": "degraded"}, []) == "degraded"


_BIBLE_JSON = {
    "title": "Ada",
    "story_type": "history",
    "time_period": "1840s",
    "primary_subject_id": "ada",
    "entities": [
        {
            "id": "ada",
            "canonical": "Ada Lovelace",
            "type": "person",
            "role": "main subject",
            "aliases": [],
            "pronoun": "she",
            "description": "Mathematician.",
        }
    ],
    "timeline": [],
}

_SENTENCES = ["Ada Lovelace designed an engine.", "She published notes.", "The engine was never built."]


def _fake_llm(fail_sentences=False):
    def llm(system, user):
        if system.startswith("You are a documentary researcher"):
            return json.dumps(_BIBLE_JSON)
        if fail_sentences:
            raise RuntimeError("provider down")
        # A plan only counts as LLM-produced when the LLM supplied
        # real scenarios (normalize_plan marks scenario-less plans
        # degraded), so the fake returns complete plans.
        return json.dumps(
            [
                {
                    "idx": i,
                    "subject_id": "ada",
                    "scenarios": [
                        {
                            "visual": "Ada Lovelace with the analytical engine",
                            "kind": "person_activity",
                            "subject_id": "ada",
                            "specificity": "representative",
                            "search_terms": ["analytical engine"],
                        }
                    ],
                }
                for i in range(3)
            ]
        )

    return llm


def test_plan_script_partial_llm_when_sentence_batches_fail():
    bible, plans, mode = story.plan_script(
        " ".join(_SENTENCES), _SENTENCES, _fake_llm(fail_sentences=True)
    )
    # The bible genuinely came from the LLM...
    assert bible["mode"] == "llm"
    # ...but every sentence plan fell back, so the run must NOT be
    # reported as "llm" (the old code copied the bible's mode).
    assert all(plan["degraded"] for plan in plans)
    assert mode == "partial_llm"
    assert mode != "llm"


def test_plan_script_all_llm_reports_llm():
    bible, plans, mode = story.plan_script(
        " ".join(_SENTENCES), _SENTENCES, _fake_llm()
    )
    assert bible["mode"] == "llm"
    assert not any(plan["degraded"] for plan in plans)
    assert mode == "llm"


def test_plan_script_all_fallback_reports_degraded():
    bible, plans, mode = story.plan_script(
        " ".join(_SENTENCES), _SENTENCES, None
    )
    # One vocabulary: the fallback bible is "degraded", never the
    # old "heuristic" string.
    assert bible["mode"] == "degraded"
    assert mode == "degraded"


# ---------------------------------------------------------------------------
# MA-30 — verifier loophole (empty topical set)
# ---------------------------------------------------------------------------

_BIBLE_ADA = {
    "by_id": {
        "ada": {
            "id": "ada",
            "canonical": "Ada Lovelace",
            "type": "person",
            "aliases": [],
        }
    }
}


@pytest.mark.parametrize("specificity", ["exact", "representative", "generic"])
def test_subject_only_empty_topical_set_rejected(specificity):
    scenario = {
        "subject_id": "ada",
        "specificity": specificity,
        "search_terms": [],
        "visual": "",
        "era": None,
    }
    candidate = {
        "title": "Ada Lovelace documentary portrait",
        "snippet": "A film about her remarkable life",
    }
    verdict = story.verify_candidate(candidate, scenario, _BIBLE_ADA)
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "no_topical_evidence"


@pytest.mark.parametrize("specificity", ["exact", "representative", "generic"])
def test_subject_and_era_empty_topical_set_rejected(specificity):
    # Subject + era year scores 0.55 — enough to clear even the
    # exact threshold before the fix. Still no topical evidence.
    scenario = {
        "subject_id": "ada",
        "specificity": specificity,
        "search_terms": [],
        "visual": "",
        "era": "1843",
    }
    candidate = {
        "title": "Ada Lovelace in 1843",
        "snippet": "A portrait from 1843",
    }
    verdict = story.verify_candidate(candidate, scenario, _BIBLE_ADA)
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "no_topical_evidence"


def test_subject_plus_topical_still_passes():
    scenario = {
        "subject_id": "ada",
        "specificity": "exact",
        "search_terms": ["analytical engine"],
        "visual": "Ada Lovelace with the analytical engine",
        "era": None,
    }
    candidate = {
        "title": "Ada Lovelace and the analytical engine",
        "snippet": "Ada Lovelace designs the analytical engine",
    }
    verdict = story.verify_candidate(candidate, scenario, _BIBLE_ADA)
    assert verdict["accepted"] is True


# ---------------------------------------------------------------------------
# MA-29 — scenario contract
# ---------------------------------------------------------------------------

_BY_ID = {
    "ada": {"id": "ada", "canonical": "Ada Lovelace", "type": "person", "aliases": []},
    "berlin": {"id": "berlin", "canonical": "Berlin", "type": "place", "aliases": []},
}
_BIBLE_FULL = {"entities": list(_BY_ID.values()), "by_id": _BY_ID}


def _normalized(raw, sentence_text):
    return story._normalize_scenario(
        raw, {"sentence": sentence_text}, _BY_ID, None, sentence_text
    )


def test_normalize_scenario_emits_subject_and_required_type():
    person = _normalized(
        {
            "visual": "Ada Lovelace at her desk",
            "kind": "person_activity",
            "subject_id": "ada",
            "specificity": "exact",
            "search_terms": ["Ada Lovelace desk"],
        },
        "Ada Lovelace worked at her desk.",
    )
    assert person["subject"] == "Ada Lovelace"
    assert person["required_type"] == "person"

    place = _normalized(
        {
            "visual": "Berlin street in 1961",
            "kind": "place",
            "subject_id": "berlin",
            "specificity": "exact",
            "search_terms": ["Berlin 1961"],
        },
        "Berlin was divided.",
    )
    assert place["subject"] == "Berlin"
    assert place["required_type"] == "location"


def test_gate_evaluates_person_and_location_branches():
    person_scenario = _normalized(
        {
            "visual": "Ada Lovelace at her desk",
            "kind": "person_activity",
            "subject_id": "ada",
            "specificity": "exact",
            "search_terms": ["Ada Lovelace desk"],
        },
        "Ada Lovelace worked at her desk.",
    )
    sentence = {
        "sentence": "Ada Lovelace worked at her desk.",
        "scenarios": [person_scenario],
    }
    beat = {"searchable": True, "subject_id": "ada", "sentences": [sentence]}
    needs = mp.build_shot_needs(beat, _BIBLE_FULL)
    assert needs, "the production-shaped scenario must yield a shot need"
    need = needs[0]
    assert need["required_type"] == "person"
    assert need["required_subject"] == "Ada Lovelace"

    ok, reason = mp.gate_shot_need(
        {"title": "Crowded market street", "snippet": "shoppers and stalls"},
        need,
    )
    assert (ok, reason) == (False, "missing_person")
    ok, reason = mp.gate_shot_need(
        {"title": "Ada Lovelace at her desk", "snippet": "the countess writes"},
        need,
    )
    assert ok is True, f"matching candidate must pass the gate, got {reason}"

    place_scenario = _normalized(
        {
            "visual": "Berlin street in 1961",
            "kind": "place",
            "subject_id": "berlin",
            "specificity": "exact",
            "search_terms": ["Berlin 1961"],
        },
        "Berlin was divided.",
    )
    sentence2 = {"sentence": "Berlin was divided.", "scenarios": [place_scenario]}
    beat2 = {"searchable": True, "subject_id": "berlin", "sentences": [sentence2]}
    needs2 = mp.build_shot_needs(beat2, _BIBLE_FULL)
    assert needs2
    need2 = needs2[0]
    assert need2["required_type"] == "location"
    ok, reason = mp.gate_shot_need(
        {"title": "A quiet village lane", "snippet": "fields and hedges"},
        need2,
    )
    assert (ok, reason) == (False, "missing_subject")


# ---------------------------------------------------------------------------
# MA-14 — auditor verdict (+ MA-35 auditor warnings)
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


@pytest.fixture(scope="module")
def film_file(tmp_path_factory):
    folder = tmp_path_factory.mktemp("film")
    # probe_final_video requires the production 1920x1080 frame.
    return _make_video(folder / "film.mp4", size="1920x1080", seconds=2)


def _berlin_report():
    """The Berlin v5 production profile: complete coverage, zero
    substitutions, rotation 18/43, rights fully accounted."""
    return {
        "coverage": {
            "total_sentences": 43,
            "covered_sentences": 43,
            "uncovered_sentences": [],
            "complete": True,
        },
        "total_accepted_assets": 30,
        "generated_graphics": 3,
        "verified_moving_footage": 11,
        "verified_images": 14,
        "authentic": True,
        "authenticity": {
            "substitutions": {
                "text_cards": 0,
                "placeholder_clips": 0,
                "holdover_clips": 0,
                "motion_graphics": 3,
            }
        },
        "substitutions": {
            "text_cards": 0,
            "placeholder_clips": 0,
            "holdover_clips": 0,
            "motion_graphics": 3,
        },
        "rights": {
            "recorded_licences": 6,
            "unknown_licences": 21,
            "assets_with_creator": 20,
        },
        "segment_retrieval": [
            {"url": f"https://example.org/footage/{i}", "partial": True}
            for i in range(11)
        ],
        "timeline_items": 43,
        "rotation_assignments": 18,
        "missing_visuals": [{"sentence_number": n} for n in range(14)],
        "verification_dimensions": {
            "segment_evidence": {
                "positional_assignments": 0,
                "positional_no_contract": 0,
            }
        },
        "analysis_mode": "llm",
    }


def _kohberger_report():
    """The true-crime profile: complete coverage reached via heavy
    rotation (21/23), zero substitutions, rights complete."""
    return {
        "coverage": {
            "total_sentences": 23,
            "covered_sentences": 23,
            "uncovered_sentences": [],
            "complete": True,
        },
        "total_accepted_assets": 3,
        "generated_graphics": 0,
        "verified_moving_footage": 1,
        "verified_images": 2,
        "authentic": True,
        "authenticity": {
            "substitutions": {
                "text_cards": 0,
                "placeholder_clips": 0,
                "holdover_clips": 0,
                "motion_graphics": 0,
            }
        },
        "substitutions": {
            "text_cards": 0,
            "placeholder_clips": 0,
            "holdover_clips": 0,
            "motion_graphics": 0,
        },
        "rights": {
            "recorded_licences": 1,
            "unknown_licences": 2,
            "assets_with_creator": 3,
        },
        "segment_retrieval": [
            {"url": "https://example.org/news/clip", "partial": True}
        ],
        "timeline_items": 23,
        "rotation_assignments": 21,
        "missing_visuals": [{"sentence_number": n} for n in range(18)],
        "verification_dimensions": {
            "segment_evidence": {
                "positional_assignments": 0,
                "positional_no_contract": 0,
            }
        },
        "analysis_mode": "llm",
    }


def _verdict(film_file, report):
    result = validate_final_video(str(film_file), asset_report=report)
    return result["verdict"]


def test_auditor_berlin_profile_passes(film_file):
    verdict = _verdict(film_file, _berlin_report())
    assert verdict["evaluated"] is True
    assert verdict["passed"] is True
    assert verdict["failures"] == []
    # Rotation 18/43 ≈ 0.42 is below the 0.5 default limit: no
    # rotation warning either.
    assert not any("rotation_share" in w for w in verdict["warnings"])


def test_auditor_kohberger_profile_passes_with_rotation_warning(film_file):
    verdict = _verdict(film_file, _kohberger_report())
    # Rotation is honest and labelled — a warning, never a failure.
    assert verdict["passed"] is True
    assert verdict["failures"] == []
    assert any("rotation_share" in w for w in verdict["warnings"])


def test_auditor_fails_on_incomplete_coverage(film_file):
    report = _berlin_report()
    report["coverage"] = {
        "total_sentences": 43,
        "covered_sentences": 40,
        "uncovered_sentences": [
            {"sentence_number": 41, "scene_number": 2},
            {"sentence_number": 42, "scene_number": 2},
            {"sentence_number": 43, "scene_number": 2},
        ],
        "complete": False,
    }
    verdict = _verdict(film_file, report)
    assert verdict["passed"] is False
    assert any("coverage_incomplete" in f for f in verdict["failures"])


def test_auditor_fails_on_text_cards(film_file):
    report = _berlin_report()
    report["substitutions"]["text_cards"] = 1
    report["authenticity"]["substitutions"]["text_cards"] = 1
    verdict = _verdict(film_file, report)
    assert verdict["passed"] is False
    assert any("text_cards" in f for f in verdict["failures"])


def test_auditor_fails_on_placeholder_clips(film_file):
    report = _berlin_report()
    report["substitutions"]["placeholder_clips"] = 2
    report["authenticity"]["substitutions"]["placeholder_clips"] = 2
    verdict = _verdict(film_file, report)
    assert verdict["passed"] is False
    assert any("placeholder_clips" in f for f in verdict["failures"])


def test_auditor_fails_on_missing_source_url(film_file):
    report = _berlin_report()
    report["segment_retrieval"][0]["url"] = ""
    verdict = _verdict(film_file, report)
    assert verdict["passed"] is False
    assert any("no source URL" in f for f in verdict["failures"])


def test_auditor_fails_on_rights_shortfall(film_file):
    report = _berlin_report()
    report["rights"] = {
        "recorded_licences": 0,
        "unknown_licences": 4,
        "assets_with_creator": 4,
    }
    verdict = _verdict(film_file, report)
    assert verdict["passed"] is False
    assert any("rights manifest" in f for f in verdict["failures"])


def test_auditor_rotation_limit_is_configurable(film_file, monkeypatch):
    monkeypatch.setenv("CINEMORA_MAX_ROTATION_SHARE", "0.95")
    verdict = _verdict(film_file, _kohberger_report())
    assert verdict["passed"] is True
    assert not any("rotation_share" in w for w in verdict["warnings"])


# ---------------------------------------------------------------------------
# MA-35 — positional segments: item stamp, report count, auditor warning
# ---------------------------------------------------------------------------

def test_positional_item_stamp_and_report_count(tmp_path):
    positional = _asset(
        _write_visual(tmp_path, "positional.mp4"),
        verification={"accepted": True},
        segment_evidence="positional_no_contract",
    )
    scene_media = {
        "scene_number": 1,
        "duration_seconds": 5,
        "videos": [positional],
        "images": [],
        "sentences": [
            {"sentence_number": 1, "videos": [positional], "images": []},
        ],
    }
    result = build_scene_timeline(scene_media, _timings(1))
    item = result["items"][0]
    assert item["assignment"] == "sentence"
    # The item itself carries the evidence class.
    assert item["segment_evidence"] == "positional_no_contract"

    scenes = [
        {
            "asset_stats": {
                "candidates_considered": 1,
                "candidates_rejected": 0,
                "downloads_failed": 0,
                "text_cards": 0,
            },
            "videos": [positional],
            "images": [],
            "sentences": [],
        }
    ]
    timeline_result = {"scenes": [{"scene_number": 1, "items": result["items"]}]}
    report = build_asset_report(scenes, timeline_result=timeline_result)
    dims = report["verification_dimensions"]["segment_evidence"]
    assert dims["positional_assignments"] == 1
    assert dims["positional_assignment_share"] == 1.0


def test_auditor_positional_warning_above_and_below_limit(film_file):
    report = _berlin_report()
    report["verification_dimensions"]["segment_evidence"][
        "positional_assignments"
    ] = 20  # 20/43 ≈ 0.47 > 0.25 default limit
    verdict = _verdict(film_file, report)
    assert verdict["passed"] is True  # warning only, never a fail
    assert any("positional_share" in w for w in verdict["warnings"])

    report = _berlin_report()
    report["verification_dimensions"]["segment_evidence"][
        "positional_assignments"
    ] = 5  # 5/43 ≈ 0.12 < 0.25
    verdict = _verdict(film_file, report)
    assert verdict["passed"] is True
    assert not any("positional_share" in w for w in verdict["warnings"])


# ---------------------------------------------------------------------------
# MA-33 / MA-34 — evidence-safe review swaps
# ---------------------------------------------------------------------------

_SCENARIO_ADA = {
    "visual": "Ada Lovelace with the analytical engine",
    "kind": "person_activity",
    "subject_id": "ada",
    "specificity": "representative",
    "search_terms": ["analytical engine"],
}


def _original_asset():
    return {
        "file_path": "/orig/verified.mp4",
        "path": "/orig/verified.mp4",
        "type": "video",
        "candidate_id": "cand_original",
        "verification": {"accepted": True, "score": 0.91, "marker": "ORIGINAL"},
        "verification_status": "verified",
        "segment_evidence": "verified_segment",
        "segment_evidence_basis": "transcript_match",
        "clip_start": 42.0,
        "source_duration_seconds": 600,
        "download_method": "yt-dlp-download-sections",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "creator": "Some Archive",
        "source": "wikimedia",
        "source_url": "https://commons.wikimedia.org/wiki/File:Original",
        "rights_status": "recorded",
        "duration_seconds": 999,
        "width": 1920,
        "height": 1080,
    }


def _swap_result(sentence, aggregates_from):
    return {
        "scene_number": 1,
        "beats": [{"beat_id": "b1", "sentence_numbers": [1]}],
        "sentences": [sentence],
        "videos": [dict(a) for a in aggregates_from],
        "images": [],
        "visuals": [dict(a, visual_type="video") for a in aggregates_from],
    }


def _write_decisions(project_dir, rows):
    (project_dir / "scene_1_decisions.json").write_text(
        json.dumps(rows), encoding="utf-8"
    )


def _sentence_result(original, with_sentence=True):
    result = {
        "sentence_number": 1,
        "beat_id": "b1",
        "videos": [original],
        "images": [],
        "visuals": [],
    }
    if with_sentence:
        result["sentence"] = {
            "sentence": "Ada Lovelace designed an engine.",
            "scenarios": [dict(_SCENARIO_ADA)],
        }
    return result


def test_swap_refreshes_all_evidence(tmp_path):
    replacement = _make_video(tmp_path / "replacement.mp4", size="640x360", seconds=2)
    original = _original_asset()
    result = _swap_result(_sentence_result(original), [original])
    _write_decisions(
        tmp_path,
        [
            {
                "sentence_number": 1,
                "beat_id": "b1",
                "approve": True,
                "swap_path": str(replacement),
                "title": "Cooking show episode",
                "snippet": "recipes and kitchens",
            }
        ],
    )
    out = mp.apply_review_decisions(result, tmp_path, bible=_BIBLE_FULL)
    swapped = out["sentences"][0]["videos"][0]

    assert swapped["file_path"] == str(replacement)
    # Fresh probe facts — not the inherited 999s / 1920x1080.
    assert swapped["duration_seconds"] == pytest.approx(2.0, abs=0.6)
    assert swapped["width"] == 640
    assert swapped["height"] == 360
    # Segment evidence cleared.
    assert swapped["segment_evidence"] == "none"
    assert "clip_start" not in swapped
    assert "download_method" not in swapped
    assert "segment_evidence_basis" not in swapped
    # Rights re-stamped honestly.
    assert swapped["license"] == "unknown"
    assert swapped["creator"] == "user_supplied"
    assert swapped["source"] == "user_supplied"
    assert swapped["rights_status"] == "verify_before_publish"
    # Verdict REPLACED by re-verification: the unrelated replacement
    # does not inherit the original's acceptance.
    assert swapped["verification"]["accepted"] is False
    assert "marker" not in swapped["verification"]
    assert swapped["verification_status"] == "swapped"
    # History preserved: replaced asset's identity is recorded.
    assert swapped["swapped_from"] == {
        "file_path": "/orig/verified.mp4",
        "candidate_id": "cand_original",
    }
    outcomes = out["review_decision_outcomes"]
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "swapped"
    assert outcomes[0]["previous_candidate_id"] == "cand_original"
    assert outcomes[0]["verification_accepted"] is False
    # Aggregates reference the swapped file — the rotation pool
    # cannot resurrect the pre-swap original.
    assert out["videos"][0]["file_path"] == str(replacement)
    assert out["visuals"][0]["file_path"] == str(replacement)


def test_swap_reverification_can_genuinely_pass(tmp_path):
    replacement = _make_video(tmp_path / "replacement.mp4")
    original = _original_asset()
    result = _swap_result(_sentence_result(original), [original])
    _write_decisions(
        tmp_path,
        [
            {
                "sentence_number": 1,
                "approve": True,
                "swap_path": str(replacement),
                "title": "Ada Lovelace and the analytical engine",
                "snippet": "Ada Lovelace designs the analytical engine",
            }
        ],
    )
    out = mp.apply_review_decisions(result, tmp_path, bible=_BIBLE_FULL)
    swapped = out["sentences"][0]["videos"][0]
    # Re-verification genuinely passed on the replacement's own
    # metadata — only then may it carry a verified status.
    assert swapped["verification"]["accepted"] is True
    assert "marker" not in swapped["verification"]
    assert swapped["verification_status"] == "verified"


def test_swap_without_reachable_scenario_is_unverified(tmp_path):
    replacement = _make_video(tmp_path / "replacement.mp4")
    original = _original_asset()
    sentence = _sentence_result(original, with_sentence=False)
    result = _swap_result(sentence, [original])
    _write_decisions(
        tmp_path,
        [
            {
                "sentence_number": 1,
                "approve": True,
                "swap_path": str(replacement),
                "title": "Ada Lovelace and the analytical engine",
                "snippet": "Ada Lovelace designs the analytical engine",
            }
        ],
    )
    out = mp.apply_review_decisions(result, tmp_path, bible=_BIBLE_FULL)
    swapped = out["sentences"][0]["videos"][0]
    assert swapped["verification"]["accepted"] is False
    assert swapped["verification"]["reason"] == "manual_swap_unverified"
    assert swapped["verification_status"] == "swapped"


def test_swap_probe_failure_leaves_original_untouched(tmp_path):
    bogus = tmp_path / "bogus.mp4"
    bogus.write_bytes(b"this is not a video file at all" * 64)
    original = _original_asset()
    result = _swap_result(_sentence_result(original), [original])
    _write_decisions(
        tmp_path,
        [
            {
                "sentence_number": 1,
                "approve": True,
                "swap_path": str(bogus),
            }
        ],
    )
    out = mp.apply_review_decisions(result, tmp_path, bible=_BIBLE_FULL)
    kept = out["sentences"][0]["videos"][0]
    assert kept["file_path"] == "/orig/verified.mp4"
    assert kept["verification"]["accepted"] is True
    assert kept["verification"].get("marker") == "ORIGINAL"
    outcomes = out["review_decision_outcomes"]
    assert outcomes[0]["outcome"] == "swap_failed_probe"
    # Aggregates still reference the original too.
    assert out["videos"][0]["file_path"] == "/orig/verified.mp4"


def test_swap_decision_keyed_per_sentence(tmp_path):
    replacement = _make_video(tmp_path / "replacement.mp4")
    first = _original_asset()
    second = _original_asset()
    second["file_path"] = "/orig/second.mp4"
    second["path"] = "/orig/second.mp4"
    second["candidate_id"] = "cand_second"
    s1 = _sentence_result(first)
    s2 = _sentence_result(second)
    s2["sentence_number"] = 2
    result = _swap_result(s1, [first, second])
    result["sentences"] = [s1, s2]
    # A decision naming sentence 2 only — the old beat-keyed code
    # would have overwritten BOTH sentences of the beat.
    _write_decisions(
        tmp_path,
        [
            {
                "sentence_number": 2,
                "beat_id": "b1",
                "approve": True,
                "swap_path": str(replacement),
                "title": "Cooking show episode",
                "snippet": "recipes and kitchens",
            }
        ],
    )
    out = mp.apply_review_decisions(result, tmp_path, bible=_BIBLE_FULL)
    assert out["sentences"][0]["videos"][0]["file_path"] == "/orig/verified.mp4"
    assert out["sentences"][0]["videos"][0]["verification"].get("marker") == "ORIGINAL"
    assert out["sentences"][1]["videos"][0]["file_path"] == str(replacement)
    aggregate_paths = [v["file_path"] for v in out["videos"]]
    assert aggregate_paths == ["/orig/verified.mp4", str(replacement)]
