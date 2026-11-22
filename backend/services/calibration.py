"""Stage 6 / R6 — labelled corpus + calibration evidence.

Implements the Master Architecture Review's calibration protocol
(C.B.5 + C.G.2/C.G.3) as infrastructure plus the subset the stored
workspace data honestly supports:

- ``build_corpus`` assembles the candidate corpus from stored
  productions: per-shot rejected candidates (review files joined
  to their shot needs) and picked titles, each row carrying the
  pipeline's recorded outcome and the two C.B.5 category questions
  as EMPTY label slots. Labels are judgements; this module never
  fills them. A corpus with no labels is an unlabelled corpus,
  and every downstream number that would need labels is reported
  as not measurable rather than estimated. When labels exist they
  carry their labeller's identity as provenance: the Phase 4.3
  batch (Transformation Plan §4.3, 2026-11-22) is AGENT labels,
  recorded honestly as agent labels under the takeover mandate —
  which superseded the previous era's founder-gate on labelling —
  and never as founder or human labels.
- ``stratified_sample`` flags a deterministic, reproducible
  labelling worklist (stratified by genre x primary reason x shot
  type x specificity, plus the accepted exemplars).
- ``calibration_report`` computes whatever the labels present
  support: labelled coverage, inter-rater agreement (Cohen's
  kappa) when two or more independent labellers labelled the same
  entries, and pipeline false-admit / false-reject rates against
  the labels per category question — overall and per stratum
  (``rates_by_stratum``: a rejected entry's stratum is its
  primary rejection reason; accepted entries form the "accepted"
  stratum, the false-admit direction). With no labels it reports
  the corpus state and the gaps — it invents nothing.
- ``evaluate_expectations`` reads a genre pack's ``expectations``
  data (C.G.2) the way quality_verdict reads its THRESHOLDS
  registry: every evaluation carries its basis label, and an
  uncalibrated expectation (range None) evaluates to UNKNOWN.

Thresholds and category-profile floors are NEVER written by this
module. Calibration output is evidence and report text; applying
any number it suggests is decided outside this module on the
reported evidence (Transformation Phase 4.3: the default is no
change, and a change is reported with its numbers first).
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from services import rejection_diagnostics
from services.atomic_write import write_json_atomic

CORPUS_VERSION = 1

# The C.G.2 expectation fields, in pack order.
EXPECTATION_FIELDS = (
    "moving_share",
    "still_share",
    "document_share",
    "graphic_share",
    "uncovered_share",
)

# The two C.B.5 category questions. They have different ground
# truths and never share a label.
LABEL_QUESTIONS = (
    "depicts_named_entity",
    "depicts_activity_or_place_type",
)

# Default per-stratum cap for the labelling worklist.
DEFAULT_STRATUM_CAP = 25


# ---------------------------------------------------------------------------
# Genre expectations (C.G.2)
# ---------------------------------------------------------------------------

def pack_expectations(pack):
    """The pack's expectations dict, normalised to the five fields.

    Missing fields/packs yield the honest uncalibrated shape —
    range None, basis "uncalibrated" — never a default number."""
    raw = (pack or {}).get("expectations") or {}
    normalised = {}
    for field in EXPECTATION_FIELDS:
        entry = raw.get(field) or {}
        normalised[field] = {
            "range": entry.get("range"),
            "basis": entry.get("basis") or "uncalibrated",
        }
    return normalised


def evaluate_expectations(pack, measurements):
    """Evaluate measured shares against a pack's expectations.

    ``measurements`` maps field -> measured share (float) or None
    when the production's artefacts cannot support the measure.
    Every field returns {measured, range, basis, evaluation} where
    evaluation is "UNKNOWN" whenever the expectation is
    uncalibrated (range None) or the measurement is missing —
    an uncalibrated expectation never produces a pass/fail."""
    expectations = pack_expectations(pack)
    measurements = measurements or {}
    evaluated = {}
    for field in EXPECTATION_FIELDS:
        expectation = expectations[field]
        measured = measurements.get(field)
        value_range = expectation["range"]
        if value_range is None or measured is None:
            evaluation = "UNKNOWN"
        else:
            lo, hi = value_range
            evaluation = (
                "WITHIN_RANGE" if lo <= measured <= hi else "OUTSIDE_RANGE"
            )
        evaluated[field] = {
            "measured": measured,
            "range": value_range,
            "basis": expectation["basis"],
            "evaluation": evaluation,
        }
    return evaluated


# ---------------------------------------------------------------------------
# Stored productions
# ---------------------------------------------------------------------------

def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_stored_production(project_dir):
    """Parse one stored production's artefacts (read-only).

    Returns {asset_report, scene_reviews, timeline, rights_manifest}
    with scene_reviews keyed by scene number. Missing artefacts are
    None / absent keys — never fabricated."""
    project_dir = Path(project_dir)
    scene_reviews = {}
    for review_path in sorted(project_dir.glob("scene_*_review.json")):
        try:
            scene_number = int(review_path.name.split("_")[1])
        except (IndexError, ValueError):
            continue
        parsed = _load_json(review_path)
        if isinstance(parsed, list):
            scene_reviews[scene_number] = parsed
    return {
        "asset_report": _load_json(project_dir / "asset_report.json") or {},
        "scene_reviews": scene_reviews,
        "timeline": _load_json(project_dir / "timeline.json") or {},
        "rights_manifest": _load_json(project_dir / "rights_manifest.json") or [],
    }


def production_measurements(project_dir):
    """The C.G.1 share vector for a stored production, measured by
    the frozen definitions in services.visual_quality (the
    definition of record — this module redefines nothing)."""
    from services import visual_quality

    stored = load_stored_production(project_dir)
    measured = visual_quality.measure_visual_quality(
        timeline=stored["timeline"],
        asset_report=stored["asset_report"],
        rights_manifest=stored["rights_manifest"],
    )
    runtime_share = measured.get("runtime_share") or {}

    def _share(kind_name):
        entry = runtime_share.get(kind_name) or {}
        return entry.get("fraction")

    format_block = measured.get("runtime_share_by_format") or {}
    document_share = None
    if format_block.get("state") == "recorded":
        doc = (format_block.get("by_format") or {}).get("document") or {}
        document_share = doc.get("runtime_share")
    outcomes = measured.get("sentence_outcomes") or {}
    uncovered_share = (outcomes.get("shares") or {}).get("uncovered")
    return {
        "moving_share": _share("moving"),
        "still_share": _share("still"),
        "document_share": document_share,
        "graphic_share": _share("graphics"),
        "uncovered_share": uncovered_share,
    }


# ---------------------------------------------------------------------------
# Corpus construction (C.B.5 step 1)
# ---------------------------------------------------------------------------

def _entry_id(production_id, scene, shot_index, candidate_key):
    raw = f"{production_id}|{scene}|{shot_index}|{candidate_key}"
    return "corp_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _empty_labels():
    return {
        question: None for question in LABEL_QUESTIONS
    } | {"labeller": None, "labelled_at": None}


def build_corpus(productions):
    """Assemble the labelling corpus from stored productions.

    ``productions`` is a list of {"production_id", "genre",
    "project_dir"} — the genre is operator-supplied production
    metadata (the stored reports predate genre_pack recording) and
    is carried on every entry with its provenance, never inferred
    from content.

    One entry per (shot, candidate): rejected candidates from the
    review files (joined to their shot needs and, by candidate_id,
    to the report record for provider/url) and the shots' picked
    titles as accepted exemplars. Verified-but-not-picked candidates
    cannot be attributed to shots from the stored artefacts (the
    stored records carry no shot linkage) and are therefore absent
    — a recorded gap, not a silent one (see calibration_report).
    """
    entries = []
    production_summaries = []
    for production in productions or []:
        production_id = str(production.get("production_id") or "")
        genre = str(production.get("genre") or "")
        stored = load_stored_production(production.get("project_dir"))
        report = stored["asset_report"]
        records_by_id = {}
        for record in (report.get("candidate_records") or {}).get("records") or []:
            if isinstance(record, dict) and record.get("candidate_id"):
                records_by_id.setdefault(record["candidate_id"], record)
        produced = rejection_diagnostics.recompute_stored_funnel(
            report, stored["scene_reviews"]
        )
        entry_count_before = len(entries)
        for scene_number, review_entries in sorted(
            stored["scene_reviews"].items()
        ):
            for beat_entry in review_entries or []:
                if not isinstance(beat_entry, dict):
                    continue
                needs = {
                    need.get("shot_index"): need
                    for need in beat_entry.get("shot_needs") or []
                    if isinstance(need, dict)
                }
                for shot in beat_entry.get("records") or []:
                    if not isinstance(shot, dict):
                        continue
                    shot_index = shot.get("shot_index")
                    need = needs.get(shot_index) or {}
                    base = {
                        "production_id": production_id,
                        "genre": genre,
                        "genre_provenance": "operator_supplied",
                        "scene": scene_number,
                        "beat_id": beat_entry.get("beat_id"),
                        "shot_index": shot_index,
                        "shot_type": need.get("shot_type"),
                        "specificity": need.get("specificity"),
                        "required_subject": need.get("required_subject"),
                        "required_type": need.get("required_type"),
                        "place": need.get("place"),
                        "era": need.get("era"),
                        "queries": list(shot.get("queries") or []),
                    }
                    for rejected in shot.get("rejected") or []:
                        if not isinstance(rejected, dict):
                            continue
                        candidate_id = rejected.get("candidate_id")
                        record = records_by_id.get(candidate_id) or {}
                        entries.append({
                            **base,
                            "entry_id": _entry_id(
                                production_id, scene_number, shot_index,
                                candidate_id or rejected.get("title") or "",
                            ),
                            "candidate_id": candidate_id,
                            "title": rejected.get("title"),
                            "provider": record.get("provider"),
                            "url": record.get("url"),
                            "pipeline_outcome": "rejected",
                            "primary_reason": rejected.get("reason"),
                            "stage": rejected.get("stage"),
                            "labels": _empty_labels(),
                            "in_labelling_sample": False,
                        })
                    for title in shot.get("picked") or []:
                        entries.append({
                            **base,
                            "entry_id": _entry_id(
                                production_id, scene_number, shot_index,
                                f"picked:{title}",
                            ),
                            "candidate_id": None,
                            "title": title,
                            "provider": None,
                            "url": None,
                            "pipeline_outcome": "accepted",
                            "primary_reason": None,
                            "stage": "accepted",
                            "labels": _empty_labels(),
                            "in_labelling_sample": False,
                        })
        production_summaries.append({
            "production_id": production_id,
            "genre": genre,
            "project_dir": str(production.get("project_dir")),
            "corpus_entries": len(entries) - entry_count_before,
            "funnel_recompute": produced,
        })
    return {
        "corpus_version": CORPUS_VERSION,
        "label_questions": list(LABEL_QUESTIONS),
        "labelling_state": "unlabelled",
        "entries": entries,
        "productions": production_summaries,
    }


def stratified_sample(corpus, per_stratum_cap=DEFAULT_STRATUM_CAP, seed=0):
    """Flag the deterministic labelling worklist on the corpus.

    Strata: (genre, pipeline_outcome, primary_reason, shot_type,
    specificity) for rejected entries; accepted exemplars form
    their own strata by (genre, shot_type, specificity). Selection
    within a stratum is a seeded shuffle — reproducible from the
    corpus alone. Mutates and returns the corpus (the flags are
    part of the corpus artefact)."""
    rng = random.Random(seed)
    strata = defaultdict(list)
    for entry in (corpus or {}).get("entries") or []:
        if entry.get("pipeline_outcome") == "accepted":
            key = (
                entry.get("genre"), "accepted", None,
                entry.get("shot_type"), entry.get("specificity"),
            )
        else:
            key = (
                entry.get("genre"), "rejected",
                entry.get("primary_reason"),
                entry.get("shot_type"), entry.get("specificity"),
            )
        strata[key].append(entry)
    for key in sorted(strata, key=lambda k: tuple(str(x) for x in k)):
        members = strata[key]
        rng.shuffle(members)
        for entry in members[:per_stratum_cap]:
            entry["in_labelling_sample"] = True
    corpus["sampling"] = {
        "method": "stratified_by_genre_outcome_reason_shot_type_specificity",
        "per_stratum_cap": per_stratum_cap,
        "seed": seed,
        "strata": len(strata),
        "sampled_entries": sum(
            1
            for entry in corpus.get("entries") or []
            if entry.get("in_labelling_sample")
        ),
    }
    return corpus


# ---------------------------------------------------------------------------
# Per-stratum rates (Transformation Phase 4.3)
# ---------------------------------------------------------------------------

def stratum_key_for(entry):
    """The labelling stratum an entry belongs to: a rejected
    entry's stratum is its primary rejection reason; accepted
    entries form the single "accepted" stratum (the false-admit
    direction — the only stratum where a false admit can occur).
    """
    if (entry or {}).get("pipeline_outcome") == "accepted":
        return "accepted"
    return str((entry or {}).get("primary_reason") or "unknown_reason")


def _confusion_key(admitted, truth):
    return (
        "false_admit" if admitted and not truth
        else "false_reject" if not admitted and truth
        else "agree_admit" if admitted
        else "agree_reject"
    )


def _rates_from_confusion(confusion_counts):
    """Labelled counts + rates for one confusion counter.

    A rate's base is the entries LABELLED on that question in
    that population — false_admit_rate is the share of labelled
    decisions that were false admits, false_reject_rate the
    share that were false rejects. Rates are None (not 0.0) when
    nothing was labelled: unmeasured is not zero."""
    labelled = sum(confusion_counts.values())
    return {
        "labelled": labelled,
        "false_admit": confusion_counts.get("false_admit", 0),
        "false_reject": confusion_counts.get("false_reject", 0),
        "agree_admit": confusion_counts.get("agree_admit", 0),
        "agree_reject": confusion_counts.get("agree_reject", 0),
        "false_admit_rate": (
            round(confusion_counts.get("false_admit", 0) / labelled, 4)
            if labelled else None
        ),
        "false_reject_rate": (
            round(confusion_counts.get("false_reject", 0) / labelled, 4)
            if labelled else None
        ),
    }


def rates_by_stratum(corpus, labels=None):
    """False-admit / false-reject rates per stratum, per question.

    ``labels`` has calibration_report's shape (entry_id ->
    {question: bool, "labeller": str} or a list of such dicts);
    with several labellers the FIRST labeller's value is the
    entry's truth, exactly as calibration_report's confusion
    counts resolve it. Strata with no labelled entries report
    labelled 0 and null rates — present, never silently dropped,
    so the labelled coverage of every stratum is visible.
    """
    corpus = corpus or {}
    labels = labels or {}
    strata = {}
    for entry in corpus.get("entries") or []:
        key = stratum_key_for(entry)
        bucket = strata.setdefault(
            key, {q: Counter() for q in LABEL_QUESTIONS}
        )
        raw = labels.get(entry.get("entry_id")) or []
        entry_labels = [
            lab for lab in (raw if isinstance(raw, list) else [raw])
            if isinstance(lab, dict) and lab.get("labeller")
        ]
        if not entry_labels:
            continue
        admitted = entry.get("pipeline_outcome") in ("accepted", "verified")
        for question in LABEL_QUESTIONS:
            values = [
                lab.get(question) for lab in entry_labels
                if isinstance(lab.get(question), bool)
            ]
            if values:
                bucket[question][
                    _confusion_key(admitted, values[0])
                ] += 1
    return {
        key: {
            question: _rates_from_confusion(bucket[question])
            for question in LABEL_QUESTIONS
        }
        for key, bucket in sorted(strata.items())
    }


# ---------------------------------------------------------------------------
# Calibration report (C.B.5 steps 2-3, C.G.3)
# ---------------------------------------------------------------------------

def _cohens_kappa(pairs):
    """Cohen's kappa over (a, b) boolean label pairs; None when
    agreement is undefined (no pairs, or a single category used by
    both raters throughout)."""
    if not pairs:
        return None
    n = len(pairs)
    observed = sum(1 for a, b in pairs if a == b) / n
    a_true = sum(1 for a, _ in pairs if a) / n
    b_true = sum(1 for _, b in pairs if b) / n
    expected = a_true * b_true + (1 - a_true) * (1 - b_true)
    if expected >= 1.0:
        return None
    return round((observed - expected) / (1 - expected), 4)


def calibration_report(corpus, labels=None, measurements_by_genre=None):
    """The R6 calibration report over a corpus + whatever labels
    exist.

    ``labels`` maps entry_id -> {question: bool, "labeller": str}
    (several labellers per entry are supported by passing a list
    of such dicts per entry). ``measurements_by_genre`` maps genre
    -> the C.G.1 share vector (production_measurements). The
    report states plainly what is unmeasured and why; it never
    fills a gap with a plausible number, and it never recommends
    applying a threshold — observations are report text only.
    """
    corpus = corpus or {}
    entries = corpus.get("entries") or []
    labels = labels or {}

    def _labels_for(entry):
        raw = labels.get(entry.get("entry_id")) or []
        return raw if isinstance(raw, list) else [raw]

    labelled_entries = 0
    labellers = set()
    per_question_pairs = defaultdict(list)  # question -> [(a, b)]
    # Pipeline-vs-label confusion per question: the pipeline
    # "admits" an entry iff its outcome is accepted/verified.
    confusion = {
        q: Counter() for q in LABEL_QUESTIONS
    }
    for entry in entries:
        entry_labels = [
            lab for lab in _labels_for(entry)
            if isinstance(lab, dict) and lab.get("labeller")
        ]
        if not entry_labels:
            continue
        labelled_entries += 1
        for lab in entry_labels:
            labellers.add(lab["labeller"])
        admitted = entry.get("pipeline_outcome") in ("accepted", "verified")
        for question in LABEL_QUESTIONS:
            values = [
                lab.get(question) for lab in entry_labels
                if isinstance(lab.get(question), bool)
            ]
            if len(values) >= 2:
                per_question_pairs[question].append((values[0], values[1]))
            if values:
                truth = values[0]
                key = (
                    "false_admit" if admitted and not truth
                    else "false_reject" if not admitted and truth
                    else "agree_admit" if admitted
                    else "agree_reject"
                )
                confusion[question][key] += 1

    agreement = {}
    for question in LABEL_QUESTIONS:
        pairs = per_question_pairs.get(question) or []
        if len(labellers) < 2 or not pairs:
            agreement[question] = {
                "state": "not_measurable",
                "reason": (
                    "inter-rater agreement requires the same entries "
                    "labelled independently by at least two human "
                    "labellers; the corpus does not yet carry them"
                ),
                "paired_labels": len(pairs),
            }
        else:
            agreement[question] = {
                "state": "measured",
                "cohens_kappa": _cohens_kappa(pairs),
                "paired_labels": len(pairs),
            }

    genres = sorted({e.get("genre") for e in entries if e.get("genre")})
    expectations_status = {}
    for genre in genres:
        pack_id = genre if genre in ("general", "history", "true_crime") else None
        pack = None
        if pack_id:
            from services import genre_packs

            pack = genre_packs.load_pack(pack_id)
        expectations_status[genre] = evaluate_expectations(
            pack, (measurements_by_genre or {}).get(genre)
        )

    fully_labelled = labelled_entries == len(entries) and entries

    # Transformation Phase 4.3: measured rates — overall per
    # question and per stratum — from the same confusion counts.
    label_rates = {
        question: _rates_from_confusion(confusion[question])
        for question in LABEL_QUESTIONS
    }

    if labelled_entries:
        agent_only = all(
            str(labeller).startswith("agent:")
            for labeller in labellers
        )
        gaps = [
            (
                "labels: "
                f"{labelled_entries} of {len(entries)} entries carry "
                f"labels from {sorted(labellers)}; the false-admit / "
                "false-reject rates are measured against those labels "
                "only — unlabelled entries contribute nothing. "
                + (
                    "The labeller is an agent acting under the "
                    "takeover mandate (Transformation Phase 4.3): "
                    "these are agent labels, recorded as such, not "
                    "founder or human labels. "
                    if agent_only else ""
                )
                + "A single labeller means inter-rater agreement "
                "stays not_measurable."
            ),
            "third genre / unseen subject: the stored workspace "
            "productions cover history and true_crime only; a "
            "third-genre corpus arm requires a fresh production "
            "(Stage 15, founder keys) and cannot be reconstructed "
            "from stored artefacts",
            "verified-but-not-picked candidates are not attributable "
            "to shots in the stored artefacts (stored records carry "
            "no shot linkage), so the corpus's per-shot population "
            "is rejected candidates + picked exemplars",
            "no threshold or category-profile floor is changed by "
            "this report; under Transformation Phase 4.3 a change "
            "is decided at coordination level on this evidence, "
            "defaulting to no change",
        ]
    else:
        gaps = [
            "human labels: the corpus ships with empty label slots; "
            "false-admit/false-reject rates and inter-rater agreement "
            "are measurable only after independent human labelling "
            "of the stratified sample",
            "third genre / unseen subject: the stored workspace "
            "productions cover history and true_crime only; a "
            "third-genre corpus arm requires a fresh production "
            "(Stage 15, founder keys) and cannot be reconstructed "
            "from stored artefacts",
            "verified-but-not-picked candidates are not attributable "
            "to shots in the stored artefacts (stored records carry "
            "no shot linkage), so the corpus's per-shot population "
            "is rejected candidates + picked exemplars",
            "no threshold or category-profile floor is changed by "
            "this report; any calibration-derived number is a "
            "founder decision (VA-02 exclusion)",
        ]

    return {
        "corpus_version": corpus.get("corpus_version"),
        "entries_total": len(entries),
        "entries_by_genre": dict(
            Counter(e.get("genre") or "unknown" for e in entries)
        ),
        "entries_by_outcome": dict(
            Counter(e.get("pipeline_outcome") or "unknown" for e in entries)
        ),
        "sampled_entries": sum(
            1 for e in entries if e.get("in_labelling_sample")
        ),
        "labelled_entries": labelled_entries,
        "labellers": sorted(labellers),
        "labelling_state": (
            "labelled" if fully_labelled
            else "partially_labelled" if labelled_entries
            else "unlabelled"
        ),
        "inter_rater_agreement": agreement,
        "pipeline_vs_labels": {
            question: dict(confusion[question])
            for question in LABEL_QUESTIONS
        },
        # Transformation Phase 4.3: the measured rates — overall
        # per question, and per stratum (rejection reason, plus
        # the accepted stratum for the false-admit direction).
        "label_rates": label_rates,
        "rates_by_stratum": rates_by_stratum(corpus, labels),
        "expectations_by_genre": expectations_status,
        "calibration_state": (
            "calibrated" if fully_labelled and len(labellers) >= 2
            else "uncalibrated"
        ),
        "gaps": gaps,
    }


def write_corpus(corpus, out_dir):
    """Persist the corpus artefact atomically; returns the path."""
    path = Path(out_dir) / "calibration_corpus.json"
    write_json_atomic(path, corpus)
    return path
