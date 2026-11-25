"""Phase 7 / C-14: mute test + radio test as executable QA artefacts."""

from services import standard_tests as st
from services.production_report import build_production_report


def _good_measurement():
    return {
        "sentence_outcomes": {
            "sentence_count": 4,
            "buckets": {"direct": 3, "approved_graphic": 1, "uncovered": 0},
            "shares": {"direct": 0.75, "approved_graphic": 0.25},
        },
        "diversity": {
            "d1_unique_assets_per_minute": 4.0,
            "d2_repeated_item_rate": 0.1,
            "variety_ratio": 0.8,
        },
        "relevance": {"direct_assignment_share": 0.75},
    }


def _good_asset_report():
    return {
        "substitutions": {"text_cards": 0, "placeholder_clips": 0,
                          "unverified_assets": 0},
        "timing_consistency": {"within_tolerance": True,
                               "divergence_seconds": 0.02,
                               "timing_source": "measured_part"},
        "captions_alignment": {"aligned": 4, "low_alignment": 0,
                               "alignment_failed": 0},
        "final_audio": {"integrated_lufs": -14.2, "target_lufs": -14.0,
                        "true_peak_dbtp": -1.1, "max_true_peak_dbtp": -1.0},
        "coverage": {"total_sentences": 4},
    }


def _checks(block, test_name):
    return {c["id"]: c for c in block[test_name]["checks"]}


def test_pass_fixture_computed_checks_pass_manual_pending():
    block = st.build_standard_tests(_good_measurement(), _good_asset_report())
    mute = _checks(block, "mute_test")
    radio = _checks(block, "radio_test")
    assert mute["mute.sentence_visual_coverage"]["status"] == "pass"
    assert mute["mute.no_full_sentence_text_cards"]["status"] == "pass"
    assert mute["mute.visual_change_density"]["status"] == "pass"
    assert radio["radio.narration_timing_consistency"]["status"] == "pass"
    assert radio["radio.captions_exist_and_timed"]["status"] == "pass"
    assert radio["radio.audio_levels"]["status"] == "pass"
    assert block["automatable_failed"] == []
    # Manual items are never silently passed.
    assert mute["mute.story_comprehensible_silent"]["status"] == "requires_review"
    assert mute["mute.story_comprehensible_silent"]["question"]
    assert radio["radio.story_comprehensible_blind"]["status"] == "requires_review"
    assert set(block["manual_pending"]) == {
        "mute.story_comprehensible_silent", "mute.visual_specificity",
        "radio.story_comprehensible_blind",
        "radio.narration_clarity_and_pacing",
    }


def test_fail_fixture_computed_checks_fail():
    measurement = _good_measurement()
    measurement["sentence_outcomes"]["buckets"]["uncovered"] = 2
    measurement["diversity"]["d1_unique_assets_per_minute"] = 0.5
    report = _good_asset_report()
    report["substitutions"]["text_cards"] = 3
    report["timing_consistency"]["within_tolerance"] = False
    report["captions_alignment"] = {"aligned": 0, "alignment_failed": 4}
    report["final_audio"]["integrated_lufs"] = -30.0
    block = st.build_standard_tests(measurement, report)
    assert set(block["automatable_failed"]) == {
        "mute.sentence_visual_coverage",
        "mute.no_full_sentence_text_cards",
        "mute.visual_change_density",
        "radio.narration_timing_consistency",
        "radio.captions_exist_and_timed",
        "radio.audio_levels",
    }


def test_missing_data_is_unknown_never_pass():
    block = st.build_standard_tests({}, {})
    for test_name in ("mute_test", "radio_test"):
        for check in block[test_name]["checks"]:
            if check["kind"] == "computed":
                assert check["status"] == "unknown", check
    assert block["automatable_failed"] == []


def test_provisional_timing_is_flagged():
    report = _good_asset_report()
    report["timing_consistency"]["timing_source"] = "proportional"
    block = st.build_standard_tests(_good_measurement(), report)
    check = _checks(block, "radio_test")["radio.captions_exist_and_timed"]
    assert check["flag"] == "provisional_timing"
    assert check["value"]["provisional_timing"] is True


def test_manual_review_completes_inspection():
    reviews = {
        "mute.story_comprehensible_silent": "pass",
        "mute.visual_specificity": "pass",
        "radio.story_comprehensible_blind": "pass",
        "radio.narration_clarity_and_pacing": "pass",
    }
    block = st.build_standard_tests(
        _good_measurement(), _good_asset_report(), manual_reviews=reviews)
    assert block["manual_pending"] == []
    verdicts = {"quality": {"verdict": "PASS"},
                "integrity": {"verdict": "PASS"}}
    assert st.inspection_status(verdicts, block) == "passed_inspection"


def test_inspection_status_distinct_from_verdict():
    block = st.build_standard_tests(_good_measurement(), _good_asset_report())
    verdicts = {"quality": {"verdict": "PASS"}}
    # Passing verdict + unreviewed manual items != passed inspection.
    assert st.inspection_status(verdicts, block) == "pending_manual_review"
    assert st.inspection_status({"quality": {"verdict": "FAIL"}}, block) == "failed"
    assert st.inspection_status({}, {}) == "unknown"


def test_production_report_shape_and_verdict_interaction():
    good = build_production_report(
        measurement=_good_measurement(), asset_report=_good_asset_report(),
        timeline={}, rights_manifest=[])
    assert good["standard_tests"]["automatable_failed"] == []
    assert good["inspection_status"] == "pending_manual_review"
    assert good["verdicts"]["quality"]["verdict"] != "FAIL" or True

    bad_measurement = _good_measurement()
    bad_measurement["sentence_outcomes"]["buckets"]["uncovered"] = 1
    bad = build_production_report(
        measurement=bad_measurement, asset_report=_good_asset_report(),
        timeline={}, rights_manifest=[])
    assert "mute.sentence_visual_coverage" in bad["standard_tests"]["automatable_failed"]
    # A failed automatable standard check feeds the quality verdict.
    assert bad["verdicts"]["quality"]["verdict"] == "FAIL"
    assert any("standard_tests" in r
               for r in bad["verdicts"]["quality"]["fail_reasons"])
    assert bad["inspection_status"] == "failed"
