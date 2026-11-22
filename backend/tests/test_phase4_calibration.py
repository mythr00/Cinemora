"""Transformation Phase 4.3 — calibration labelling machinery.

Behaviour pinned here (the labelling itself is a data artefact —
see ~/workspace/cinemora-audit/phase4-evidence/ — this file pins
the measurement machinery that turns labels into rates):

- stratum_key_for: rejected entries stratify by primary reason;
  accepted entries form the "accepted" stratum;
- rates_by_stratum computes false-admit / false-reject counts
  and rates per stratum per question, with null (not zero) rates
  for strata nothing was labelled in;
- calibration_report gains label_rates + rates_by_stratum, its
  labelling_state moves to partially_labelled with agent labels,
  and the gaps state the labels' provenance honestly — while the
  unlabelled report keeps its historical wording verbatim.
"""

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import calibration

Q1 = "depicts_named_entity"
Q2 = "depicts_activity_or_place_type"
AGENT = "agent:claude-phase4.3"


def _entry(entry_id, outcome, reason=None):
    return {
        "entry_id": entry_id,
        "genre": "history",
        "pipeline_outcome": outcome,
        "primary_reason": reason,
        "in_labelling_sample": True,
    }


def _corpus():
    return {
        "corpus_version": 1,
        "entries": [
            _entry("r1", "rejected", "missing_subject"),
            _entry("r2", "rejected", "missing_subject"),
            _entry("r3", "rejected", "below_threshold"),
            _entry("a1", "accepted"),
            _entry("a2", "accepted"),
            _entry("u1", "rejected", "missing_place"),  # unlabelled
        ],
    }


def _labels():
    return {
        # Rejected but actually depicts the entity: false reject.
        "r1": {"labeller": AGENT, Q1: True, Q2: True},
        # Rejected and truly does not depict it: agree reject.
        "r2": {"labeller": AGENT, Q1: False, Q2: True},
        # Rejected; depicts the activity type but not the entity.
        "r3": {"labeller": AGENT, Q1: False, Q2: True},
        # Accepted and depicts: agree admit.
        "a1": {"labeller": AGENT, Q1: True, Q2: True},
        # Accepted but does not depict the entity: false admit.
        "a2": {"labeller": AGENT, Q1: False, Q2: False},
    }


def test_stratum_key_for():
    assert calibration.stratum_key_for(
        _entry("x", "rejected", "missing_place")
    ) == "missing_place"
    assert calibration.stratum_key_for(
        _entry("x", "accepted")
    ) == "accepted"
    assert calibration.stratum_key_for(
        _entry("x", "rejected", None)
    ) == "unknown_reason"


def test_rates_by_stratum_counts_and_rates():
    rates = calibration.rates_by_stratum(_corpus(), _labels())

    subject = rates["missing_subject"][Q1]
    assert subject["labelled"] == 2
    assert subject["false_reject"] == 1
    assert subject["agree_reject"] == 1
    assert subject["false_reject_rate"] == 0.5
    assert subject["false_admit_rate"] == 0.0

    accepted = rates["accepted"][Q1]
    assert accepted["labelled"] == 2
    assert accepted["false_admit"] == 1
    assert accepted["agree_admit"] == 1
    assert accepted["false_admit_rate"] == 0.5

    # Q2 disagrees with Q1 where the judgements differ: r3's
    # rejection was a false reject on the activity question only.
    below = rates["below_threshold"]
    assert below[Q1]["agree_reject"] == 1
    assert below[Q2]["false_reject"] == 1

    # The unlabelled stratum is present with null rates — stated,
    # not dropped and not zero.
    place = rates["missing_place"][Q1]
    assert place["labelled"] == 0
    assert place["false_reject_rate"] is None
    assert place["false_admit_rate"] is None


def test_rates_by_stratum_no_labels():
    rates = calibration.rates_by_stratum(_corpus(), {})
    assert rates["accepted"][Q1]["labelled"] == 0
    assert rates["accepted"][Q1]["false_admit_rate"] is None


def test_report_with_agent_labels_measures_rates():
    report = calibration.calibration_report(_corpus(), labels=_labels())
    assert report["labelling_state"] == "partially_labelled"
    assert report["labelled_entries"] == 5
    assert report["labellers"] == [AGENT]
    # Single labeller: agreement honestly stays not measurable,
    # and the corpus is not "calibrated" on one rater.
    assert report["calibration_state"] == "uncalibrated"
    for question in calibration.LABEL_QUESTIONS:
        assert (
            report["inter_rater_agreement"][question]["state"]
            == "not_measurable"
        )
    rates = report["label_rates"][Q1]
    assert rates["labelled"] == 5
    assert rates["false_admit"] == 1
    assert rates["false_reject"] == 1
    assert rates["false_admit_rate"] == 0.2
    assert rates["false_reject_rate"] == 0.2
    assert report["rates_by_stratum"]["accepted"][Q1][
        "false_admit"
    ] == 1
    # The gaps state the provenance: agent labels, not founder's.
    gaps = " ".join(report["gaps"])
    assert "agent labels" in gaps
    assert "not founder or human labels" in gaps
    assert "agent acting under the" in gaps


def test_report_unlabelled_wording_unchanged():
    report = calibration.calibration_report(_corpus())
    assert report["labelling_state"] == "unlabelled"
    assert report["label_rates"][Q1]["labelled"] == 0
    assert report["label_rates"][Q1]["false_admit_rate"] is None
    gaps = " ".join(report["gaps"])
    assert "human labels" in gaps
