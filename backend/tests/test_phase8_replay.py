"""Phase 8 — offline multi-genre replay validation.

Replays the stored production records through the CURRENT code —
the diversity measurement (visual_quality.film_level) for every
stored production, and the Visual Director's Pass-2 reconciliation
for the productions whose stored plan + run records support it —
in the Phase 5 replay style: the stored records are the run's
ground truth, read and never written; nothing here proves
anything about live search/verification today, and fresh
productions remain key-gated (final report runbook).

Baselines carried from the recorded audits: Apollo post-fix
stored film 53.7% repeats / 7 distinct (frame audit), film 1
59.6% / 4 distinct. Phase 5's replay of the same records through
the then-new dealing measured 40.5% / 9 and 55.2% / 5 — the
stored-timeline measurements below are the CURRENT code reading
the STORED timelines (not re-dealt), so they sit beside, not on
top of, the Phase 5 re-dealt numbers.

Two reconstruction limits, recorded honestly: (1) timeline items
span multiple sentence windows, so (scene, index) coverage is a
LOWER bound — the Director replay is a behaviour check (one
typed decision per sentence, no silent drops), not a coverage
audit; (2) the stored plans predate the Phase 2 plan contract,
so their v1 rows carry no graphic_fallback permission — on
legacy plans the Director therefore decides starved beats
UNRESOLVED with the reason recorded rather than manifesting
graphics (the AM-002 register limitation: the Director must
stay additive for plan-less legacy runs). Both findings are
Phase 8 replay RESULTS, reported in the proposal — the Director
was not loosened to make the replay look better.
"""

import json
from pathlib import Path

import pytest

import jobs  # noqa: F401  (suite-order import guard, as Phase 5)
from services import plan_document, visual_director, visual_quality as vq

BACKEND = Path(__file__).resolve().parent.parent
AUDIT_MEDIA = BACKEND.parent.parent / "cinemora-audit" / "e2e-media" / "media"

# (name, genre, timeline_path, asset_report_path, plan_path|None)
PRODUCTIONS = [
    (
        "berlin_v5", "history",
        AUDIT_MEDIA / "e2e-five-minute-berlin" / "timeline.json",
        AUDIT_MEDIA / "e2e-five-minute-berlin" / "asset_report.json",
        None,
    ),
    (
        "kohberger_v1", "true_crime",
        AUDIT_MEDIA / "e2e-truecrime-kohberger" / "timeline.json",
        AUDIT_MEDIA / "e2e-truecrime-kohberger" / "asset_report.json",
        None,
    ),
    (
        "apollo_fixed", "science",
        BACKEND / "media" / "a798e405-1752-484b-b9f0-3b5402328968"
        / "runs" / "099b2fcd-bad9-447c-9d01-b70d6d72f4fd"
        / "timeline.json",
        BACKEND / "media" / "a798e405-1752-484b-b9f0-3b5402328968"
        / "runs" / "099b2fcd-bad9-447c-9d01-b70d6d72f4fd"
        / "asset_report.json",
        BACKEND / "media" / "a798e405-1752-484b-b9f0-3b5402328968"
        / "plan.json",
    ),
    (
        "apollo_prefix", "science",
        BACKEND / "media" / "34fbe725-eca0-4edc-b935-9b7cb403761f"
        / "runs" / "0d50fdf9-6929-4dd1-9dbb-c820b1294d1d"
        / "timeline.json",
        BACKEND / "media" / "34fbe725-eca0-4edc-b935-9b7cb403761f"
        / "runs" / "0d50fdf9-6929-4dd1-9dbb-c820b1294d1d"
        / "asset_report.json",
        BACKEND / "media" / "34fbe725-eca0-4edc-b935-9b7cb403761f"
        / "plan.json",
    ),
    (
        "film1", "true_crime",
        BACKEND / "media" / "a1145d94-bf3c-405d-854b-0db459e763ee"
        / "runs" / "d6a64d2a-d81c-44b4-aed4-9e97bc46cf0a"
        / "timeline.json",
        BACKEND / "media" / "a1145d94-bf3c-405d-854b-0db459e763ee"
        / "runs" / "d6a64d2a-d81c-44b4-aed4-9e97bc46cf0a"
        / "asset_report.json",
        BACKEND / "media" / "a1145d94-bf3c-405d-854b-0db459e763ee"
        / "plan.json",
    ),
    (
        "gilgo_run1", "true_crime",
        BACKEND / "media" / "e4a49481-02c5-45a2-9575-cbb50c1d50b5"
        / "runs" / "c7741a8d-1eab-4c00-914c-3e91e6000881"
        / "timeline.json",
        BACKEND / "media" / "e4a49481-02c5-45a2-9575-cbb50c1d50b5"
        / "runs" / "c7741a8d-1eab-4c00-914c-3e91e6000881"
        / "asset_report.json",
        BACKEND / "media" / "e4a49481-02c5-45a2-9575-cbb50c1d50b5"
        / "plan.json",
    ),
]


def _load(path):
    return json.loads(Path(path).read_text())


def replay_film_level(timeline_path, asset_report_path):
    timeline = _load(timeline_path)
    report = _load(asset_report_path) if asset_report_path.exists() else {}
    measured = vq.measure_visual_quality(
        timeline=timeline, asset_report={}, rights_manifest=[]
    )
    film = measured["film_level"]
    coverage = report.get("coverage") or {}
    return {
        "distinct_visuals": film["distinct_visuals"],
        "repeat_share": film["repeat_share"],
        "items": film.get("items"),
        "coverage_covered": coverage.get("covered_sentences"),
        "coverage_total": coverage.get("total_sentences"),
        "missing_visual_count": report.get("missing_visual_count"),
    }


@pytest.mark.parametrize(
    "name,genre,timeline_path,report_path,plan_path",
    PRODUCTIONS, ids=[p[0] for p in PRODUCTIONS],
)
def test_stored_production_film_level_replay(
    name, genre, timeline_path, report_path, plan_path
):
    if not timeline_path.exists():
        pytest.skip(f"{name} stored timeline not present in this workspace")
    result = replay_film_level(timeline_path, report_path)
    assert result["distinct_visuals"] >= 1
    assert 0.0 <= result["repeat_share"] <= 1.0


def _director_replay(plan_path, timeline_path, report_path):
    """Rebuild Director outcomes from stored records: a sentence
    is covered when the stored timeline holds an item at its
    (scene, index); starved when the stored asset report names
    it missing/uncovered. Runs the CURRENT Director over the
    stored plan (via the same v1 builder jobs uses)."""
    plan = _load(plan_path)
    timeline = _load(timeline_path)
    report = _load(report_path)
    covered_keys = set()
    for scene in timeline.get("scenes") or []:
        for item in scene.get("items") or []:
            covered_keys.add(
                (item.get("scene_number"), item.get("sentence_index"))
            )
    starved_numbers = {
        entry.get("sentence_number")
        for entry in report.get("missing_visuals") or []
        if isinstance(entry, dict)
    }
    for entry in (report.get("coverage") or {}).get(
        "uncovered_sentences"
    ) or []:
        if isinstance(entry, dict):
            starved_numbers.add(entry.get("sentence_number"))
    sentences = plan.get("sentences") or []
    by_scene = {}
    for sentence in sentences:
        by_scene.setdefault(sentence.get("scene_number"), []).append(
            sentence
        )
    outcomes = {}
    for scene_number, scene_sentences in by_scene.items():
        for index, sentence in enumerate(scene_sentences):
            number = sentence.get("sentence_number")
            outcomes[number] = {
                "sentence_number": number,
                "scene_number": scene_number,
                "sentence_index": index,
                "text": sentence.get("text") or "",
                "scenario": {},
                "covered": (scene_number, index) in covered_keys,
                "starved": number in starved_numbers,
                "tier": None,
                "asset_title": None,
                "asset_file": None,
                "scene_photo_count": 0,
                "secondary_video": None,
            }
    plan_v1 = plan_document.visual_plan_from_analysis(
        plan, plan.get("project_id")
    )
    plan_v2, manifest = visual_director.reconcile_visual_plan(
        plan_v1, outcomes
    )
    return plan_v2, manifest, outcomes


@pytest.mark.parametrize(
    "name,genre,timeline_path,report_path,plan_path",
    [p for p in PRODUCTIONS if p[4] is not None],
    ids=[p[0] for p in PRODUCTIONS if p[4] is not None],
)
def test_stored_production_director_replay(
    name, genre, timeline_path, report_path, plan_path
):
    if not (plan_path.exists() and timeline_path.exists()):
        pytest.skip(f"{name} stored plan/timeline not present")
    plan_v2, manifest, outcomes = _director_replay(
        plan_path, timeline_path, report_path
    )
    treatments = plan_v2["summary"]["treatments"]
    # Every planned sentence receives exactly one typed decision.
    assert sum(treatments.values()) == len(plan_v2["sentences"])
    # Starved sentences are decided, never silently dropped. A
    # starved sentence the stored timeline ALSO covers (rotation
    # filled its window — the AM-018 distinction between
    # sentence-level non-coverage and no verified asset) is
    # decided FOOTAGE, the Director's own precedence; a starved
    # sentence with no covering item is manifested as a graphic
    # or recorded unresolved with a reason.
    starved = {n for n, o in outcomes.items() if o["starved"]}
    decided = {
        row["sentence_number"]: row["decision"]
        for row in plan_v2["sentences"]
    }
    for number in starved:
        if outcomes[number]["covered"]:
            assert decided[number]["treatment"] == "footage"
        else:
            assert decided[number]["treatment"] in (
                "graphic", "unresolved"
            )
            if decided[number]["treatment"] == "unresolved":
                assert decided[number]["reason"]
    assert len(manifest["entries"]) == treatments["graphic"]
