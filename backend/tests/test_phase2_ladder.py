"""Master Directive Phase 2 — the tier ladder in the pipeline
(B-Roll plan B2 + D-B3, budgets D-B2).

Behaviour pinned here, all through the real _uve_search_and_pick /
_uve_process_scene_media with only the provider search and the
downloader stubbed (the verifier, gate, pick and coverage draw are
the production code):

- escalation fires ONLY on a recorded coverage shortfall — a
  satisfied beat, and a beat the coverage draw satisfies from its
  own pool, issue zero extra searches;
- a Gilgo sentence-9-shaped starved beat (representative, pool
  exhausted) escalates T2 -> covers, and the asset/record carry
  tier + selection_via="tier_escalation";
- D-B2 budgets bind: per-tier query budgets, per-tier download
  attempts, the production search account, the wall-clock guard —
  and exhaustion is recorded, never silent;
- D-B3: an unsearched-role (hook) beat enters at T3 only, and when
  still uncovered joins missing_visuals with reason
  "unsearched_role" (Phase 1 shape contract: coverage_outcome is
  stamped by the asset report's reconciliation).
"""

import uuid
from pathlib import Path

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import media_pipeline as mp
from services import story_understanding as story
from services import tier_ladder
from services.asset_report import build_asset_report
from services.visual_registry import VisualRegistry


GILGO_S1 = (
    "In December 2010, a police officer searching for a missing "
    "woman near Gilgo Beach, Long Island, made a horrifying discovery."
)
GILGO_S9 = (
    "For months, investigators studied his phones, his movements, "
    "and his family travel records."
)
S9_CONCEPTS = {
    "activities": ["reviewing records", "reviewing phones",
                   "reviewing travel records"],
    "objects": ["phones", "travel records"],
    "settings": [], "place_types": [], "mood": [],
    "era_terms": ["2010s"],
}


def _bible():
    return story.hydrate_bible({
        "entities": [
            {"id": "rex_heuermann", "canonical": "Rex Heuermann",
             "type": "person", "aliases": ["Heuermann"]},
            {"id": "gilgo_beach", "canonical": "Gilgo Beach",
             "type": "place", "aliases": ["Gilgo"]},
        ],
        "primary_subject_id": "rex_heuermann",
        "title": "Gilgo Beach",
        "story_type": "true_crime",
        "time_period": "2010-2023",
    })


def _junk(host="junk.example.org"):
    return {
        "url": f"https://{host}/cooking",
        "title": "Cooking show highlights",
        "snippet": "recipes and kitchen fun",
        "provider": "youtube",
    }


def _downloader(succeed_markers=(), fail_all=False):
    """Downloader stub: succeeds when the candidate URL contains
    any marker; records every attempt."""
    attempts = []

    def download(candidate, scene_dir, sentence_number, asset_index):
        url = str(candidate.get("url") or "")
        attempts.append(url)
        if fail_all or not any(m in url for m in succeed_markers):
            candidate["_download_error"] = "download_failed"
            return None
        path = Path(scene_dir) / f"asset_{len(attempts)}.mp4"
        path.write_bytes(b"fake")
        asset = dict(candidate)
        asset.update({
            "file_path": str(path), "path": str(path),
            "type": "video", "visual_type": "video",
            "segment_id": f"seg_{len(attempts)}",
            "duration_seconds": 5.0,
        })
        return asset

    return download, attempts


def _run_pass(monkeypatch, tmp_path, *, search, beat, shot_need,
              scenario, registry=None, downloader=None, bible=None):
    calls = []

    def _search(scenario_, cache, pid, need):
        calls.append({"scenario": scenario_, "need": need})
        return search(scenario_, need)

    monkeypatch.setattr(mp, "_uve_scenario_search", _search)
    if downloader is not None:
        monkeypatch.setattr(mp, "_uve_download_any_candidate", downloader)
    scene_stats = {
        "candidates_considered": 0, "candidates_rejected": 0,
        "downloads_failed": 0, "providers": {}, "kinds": {},
    }
    beat_videos, beat_images = [], []
    asset = mp._uve_search_and_pick(
        scenario, shot_need,
        search_cache={}, project_id="t", bible=bible or {},
        registry=registry or VisualRegistry(), used_keys=set(),
        scene_dir=tmp_path, scene_stats=scene_stats,
        beat=beat, beat_videos=beat_videos, beat_images=beat_images,
        scenario_index=1, queries=(shot_need or {}).get("queries") or [],
        review_records=[],
    )
    return asset, scene_stats, beat_videos, beat_images, calls


def _s9_fixture():
    sentence = {
        "sentence_number": 9, "text": GILGO_S9, "role": "action",
        "fallback_concepts": S9_CONCEPTS, "concepts_source": "llm_plan",
        "visual_intents": {
            "primary_visual_intent": "human_activity",
            "intents_source": "llm_plan",
        },
    }
    beat = {
        "beat_id": "beat_s9", "sentence_numbers": [9],
        "text": GILGO_S9, "sentences": [sentence],
        "subject_id": None, "place": None,
    }
    scenario = {
        "sentence_number": 9, "specificity": "representative",
        "kind": "person_activity", "subject": "",
        "search_terms": ["phone records"],
        "visual": "analysts reviewing phone records",
        "queries": ["analysts reviewing phone records"],
    }
    need = {
        "beat_id": "beat_s9", "shot_index": 1,
        "shot_type": "person_activity", "specificity": "representative",
        "visual_category": "contextual", "description": "",
        "required_subject_id": None, "required_subject": "",
        "required_type": "person", "era": "2010s", "place": "",
        "event": "", "visual_requirement": "", "forbidden": [],
        "priority": 2, "tier": "direct",
        "fallback_concepts": S9_CONCEPTS, "concepts_source": "llm_plan",
        "visual_intents": sentence["visual_intents"],
        "queries": ["analysts reviewing phone records"],
    }
    return beat, scenario, need


# ---------------------------------------------------------------------------
# Escalation fires only on a recorded shortfall
# ---------------------------------------------------------------------------

def test_satisfied_beat_issues_zero_extra_searches(monkeypatch, tmp_path):
    good = {
        "url": "https://media.example.org/berlin-0",
        "title": "Berlin Wall crowds 0",
        "snippet": "berlin wall crowds",
        "provider": "youtube",
    }
    downloader, _attempts = _downloader(succeed_markers=("berlin",))
    asset, scene_stats, _v, _i, calls = _run_pass(
        monkeypatch, tmp_path,
        search=lambda scenario, need: ([good], [], []),
        beat={"beat_id": "b", "sentence_numbers": [1],
              "text": "The Berlin Wall fell."},
        shot_need=None,
        scenario={
            "sentence_number": 1, "specificity": "representative",
            "subject": "Berlin Wall", "search_terms": ["berlin", "wall"],
            "visual": "berlin wall crowds",
        },
        downloader=downloader,
    )
    assert asset is not None
    # Exactly one search: the beat's own. No coverage pass ran, no
    # ladder attempt exists anywhere.
    assert len(calls) == 1
    assert scene_stats.get("coverage_passes") in (None, [])
    assert not [
        p for p in scene_stats.get("coverage_passes") or []
        if p.get("tier_attempts")
    ]


def test_coverage_draw_satisfaction_issues_zero_extra_searches(
    monkeypatch, tmp_path
):
    """Shortfall exists, but the beat's OWN verified pool covers it
    via the coverage draw — the ladder must not fire."""
    videos = [
        {
            "url": f"https://cov.example.org/berlin-{i}",
            "title": f"Berlin Wall crowds {i}",
            "snippet": "berlin wall crowds",
            "provider": "youtube",
        }
        for i in range(6)
    ]
    downloader, _attempts = _downloader(succeed_markers=("berlin-0", "berlin-3"))
    asset, scene_stats, beat_videos, beat_images, calls = _run_pass(
        monkeypatch, tmp_path,
        search=lambda scenario, need: (list(videos), [], []),
        beat={"beat_id": "b", "sentence_numbers": [1, 2, 3, 4, 5],
              "text": "The Berlin Wall fell."},
        shot_need=None,
        scenario={
            "sentence_number": 1, "specificity": "representative",
            "subject": "Berlin Wall", "search_terms": ["berlin", "wall"],
            "visual": "berlin wall crowds",
        },
        downloader=downloader,
    )
    assert asset is not None
    assert len(beat_videos) + len(beat_images) == 2
    assert len(calls) == 1  # the T1 search only
    passes = scene_stats["coverage_passes"]
    assert passes[0]["covered_after"] is True
    assert passes[0]["tier_attempts"] == []
    assert passes[0]["escalation_accepted"] == 0


# ---------------------------------------------------------------------------
# The Gilgo sentence-9 shape: starved beat escalates and covers
# ---------------------------------------------------------------------------

def test_gilgo_s9_starved_beat_escalates_and_covers(
    monkeypatch, tmp_path
):
    beat, scenario, need = _s9_fixture()
    bible = _bible()
    good = {
        "url": "https://media.example.org/phones-evidence",
        "title": "Investigators examining phones and travel records",
        "snippet": "phones travel records evidence examination",
        "provider": "youtube",
    }

    def search(scenario_, need_):
        if (need_ or {}).get("tier") == "related":
            return ([good], [], [])
        return ([_junk()], [], [])

    downloader, _attempts = _downloader(succeed_markers=("phones-evidence",))
    registry = VisualRegistry()
    asset, scene_stats, _v, _i, calls = _run_pass(
        monkeypatch, tmp_path,
        search=search, beat=beat, shot_need=need, scenario=scenario,
        registry=registry, downloader=downloader, bible=bible,
    )
    assert asset is not None
    # The covering asset is the ladder's: tier + route stamped.
    assert asset["tier"] == "related"
    assert asset["selection_via"] == "tier_escalation"
    # The T1 search ran once; the ladder then searched T2 only
    # (T2 produced a verified candidate — T3/T4 never ran).
    tiers_searched = [
        (c["need"] or {}).get("tier") for c in calls
    ]
    assert tiers_searched[0] == "direct"
    assert set(tiers_searched[1:]) == {"related"}

    passes = scene_stats["coverage_passes"]
    assert len(passes) == 1
    record = passes[0]
    assert record["covered_after"] is True
    assert record["pool_exhausted"] is False
    assert record["escalation_accepted"] == 1
    attempts = record["tier_attempts"]
    assert len(attempts) == 1
    assert attempts[0]["tier"] == "related"
    assert attempts[0]["outcome"] == "verified"
    assert 1 <= len(attempts[0]["queries"]) <= 2  # D-B2 T2 budget

    # The accepted candidate's event record carries the same truth.
    accepted = [
        r for r in scene_stats["candidate_records"]
        if r.get("outcome") == "accepted"
    ]
    assert len(accepted) == 1
    assert accepted[0]["tier"] == "related"
    assert accepted[0]["selection_via"] == "tier_escalation"

    # The production ledger spent exactly the recorded queries.
    ledger = tier_ladder.ledger_for(registry)
    assert ledger.searches == len(attempts[0]["queries"])
    assert ledger.downloads == 1


def test_escalation_walks_tiers_in_order(monkeypatch, tmp_path):
    """T2 yields nothing usable; T3 (activity) covers. The attempt
    records show the walk, in order, with per-tier budgets."""
    beat, scenario, need = _s9_fixture()
    good = {
        "url": "https://media.example.org/reviewing-records",
        "title": "Investigators reviewing records at a desk",
        "snippet": "reviewing records investigators office",
        "provider": "youtube",
    }

    def search(scenario_, need_):
        tier = (need_ or {}).get("tier")
        if tier == "activity_broll":
            return ([good], [], [])
        return ([_junk()], [], [])

    downloader, _a = _downloader(succeed_markers=("reviewing-records",))
    asset, scene_stats, _v, _i, _calls = _run_pass(
        monkeypatch, tmp_path,
        search=search, beat=beat, shot_need=need, scenario=scenario,
        downloader=downloader, bible=_bible(),
    )
    assert asset is not None
    assert asset["tier"] == "activity_broll"
    attempts = scene_stats["coverage_passes"][0]["tier_attempts"]
    assert [a["tier"] for a in attempts] == ["related", "activity_broll"]
    assert attempts[0]["outcome"] == "pool_exhausted"
    assert attempts[1]["outcome"] == "verified"
    assert len(attempts[0]["queries"]) <= 2
    assert len(attempts[1]["queries"]) <= 3


# ---------------------------------------------------------------------------
# D-B2 budgets
# ---------------------------------------------------------------------------

def test_production_search_budget_exhaustion_is_recorded(
    monkeypatch, tmp_path
):
    beat, scenario, need = _s9_fixture()
    registry = VisualRegistry()
    ledger = tier_ladder.ledger_for(registry)
    ledger.spend_searches(tier_ladder.PRODUCTION_MAX_EXTRA_SEARCHES)

    def search(scenario_, need_):
        return ([_junk()], [], [])

    asset, scene_stats, _v, _i, calls = _run_pass(
        monkeypatch, tmp_path,
        search=search, beat=beat, shot_need=need, scenario=scenario,
        registry=registry, bible=_bible(),
    )
    assert asset is None
    # Only the beat's own T1 search ran — the ladder spent nothing.
    assert len(calls) == 1
    attempts = scene_stats["coverage_passes"][0]["tier_attempts"]
    assert attempts
    assert attempts[0]["outcome"] == "budget_exhausted"
    assert attempts[0]["stop_reason"] == "production_search_budget"
    assert scene_stats["coverage_passes"][0]["pool_exhausted"] is True


def test_wall_clock_guard_stops_escalation(monkeypatch, tmp_path):
    beat, scenario, need = _s9_fixture()
    registry = VisualRegistry()
    ledger = tier_ladder.EscalationLedger(
        clock=lambda: 10000.0, stage_started_at=0.0,
    )
    ledger.escalation_seconds = 3000.0  # > 25% of the 10000s stage
    registry._tier_escalation_ledger = ledger

    asset, scene_stats, _v, _i, calls = _run_pass(
        monkeypatch, tmp_path,
        search=lambda s, n: ([_junk()], [], []),
        beat=beat, shot_need=need, scenario=scenario,
        registry=registry, bible=_bible(),
    )
    assert asset is None
    assert len(calls) == 1
    attempts = scene_stats["coverage_passes"][0]["tier_attempts"]
    assert attempts
    assert all(a["outcome"] == "budget_exhausted" for a in attempts)
    assert all(a["stop_reason"] == "wall_clock" for a in attempts)


def test_per_tier_download_attempts_cap(monkeypatch, tmp_path):
    """Three verified T2 candidates, a persistent shortfall, and a
    downloader that fails everything: the ladder may attempt at
    most TIER_DOWNLOAD_ATTEMPTS (2) downloads at that tier."""
    beat, scenario, need = _s9_fixture()
    beat = {**beat, "sentence_numbers": [9, 10, 11, 12, 13]}
    goods = [
        {
            "url": f"https://media.example.org/phones-evidence-{i}",
            "title": "Investigators examining phones and travel records",
            "snippet": "phones travel records evidence examination",
            "provider": "youtube",
        }
        for i in range(3)
    ]

    def search(scenario_, need_):
        if (need_ or {}).get("tier") == "related":
            return (list(goods), [], [])
        return ([_junk()], [], [])

    downloader, attempts_log = _downloader(fail_all=True)
    registry = VisualRegistry()
    asset, scene_stats, _v, _i, _calls = _run_pass(
        monkeypatch, tmp_path,
        search=search, beat=beat, shot_need=need, scenario=scenario,
        registry=registry, downloader=downloader, bible=_bible(),
    )
    assert asset is None
    escalation_attempts = [
        url for url in attempts_log if "phones-evidence" in url
    ]
    assert len(escalation_attempts) == tier_ladder.TIER_DOWNLOAD_ATTEMPTS
    ledger = tier_ladder.ledger_for(registry)
    assert ledger.downloads == tier_ladder.TIER_DOWNLOAD_ATTEMPTS
    assert scene_stats["coverage_passes"][0]["pool_exhausted"] is True


# ---------------------------------------------------------------------------
# D-B3: unsearched-role entry at T3
# ---------------------------------------------------------------------------

def _hook_beats():
    hook_sentence = {
        "sentence_number": 1, "text": GILGO_S1, "role": "hook",
        "scenarios": [],
    }
    beats = [{
        "beat_id": "beat_01_hook", "subject_id": None, "place": None,
        "searchable": False, "role": "hook",
        "sentence_numbers": [1], "sentences": [hook_sentence],
        "text": GILGO_S1, "shot_needs": [],
    }]
    return beats, hook_sentence


def _run_hook_scene(monkeypatch, tmp_path, *, search, downloader=None):
    beats, hook_sentence = _hook_beats()
    calls = []

    def _search(scenario_, cache, pid, need):
        calls.append(need)
        return search(scenario_, need)

    monkeypatch.setattr(mp, "build_story_beats", lambda s, b: beats)
    monkeypatch.setattr(mp, "_load_case_bible", lambda s, p: _bible())
    monkeypatch.setattr(mp, "_uve_scenario_search", _search)
    if downloader is not None:
        monkeypatch.setattr(mp, "_uve_download_any_candidate", downloader)
    scene = {
        "scene_number": 1,
        "project_id": f"p2-hook-{uuid.uuid4()}",
        "sentences": [hook_sentence],
    }
    result = mp._uve_process_scene_media(scene, str(tmp_path))
    return result, calls


def test_unsearched_hook_enters_at_t3_and_covers(monkeypatch, tmp_path):
    good = {
        "url": "https://media.example.org/officer-searching-beach",
        "title": "Police officer searching beach shoreline",
        "snippet": "officer searching beach",
        "provider": "youtube",
    }
    downloader, _a = _downloader(
        succeed_markers=("officer-searching-beach",)
    )
    result, calls = _run_hook_scene(
        monkeypatch, tmp_path,
        search=lambda s, n: ([good], [], []),
        downloader=downloader,
    )
    stats = result["asset_stats"]
    # B0 naming still happens.
    assert stats["uncovered_unsearched"][0]["sentence_number"] == 1
    # The beat ENTERED at T3 (its first search is the entry, with
    # the entry stamp) — an unsearched role is never searched at
    # T1/T2. (The ladder may walk on to T4 inside the entry pass;
    # D-B3 constrains the entry, not the walk.)
    assert calls, "the ladder entry never searched"
    assert calls[0]["tier"] == "activity_broll"
    assert calls[0]["derived_from"]["entry"] == "unsearched_role"
    assert not [
        c for c in calls if (c or {}).get("tier") in ("direct", "related")
    ]
    # Covered: no missing entry, and the sentence result holds media.
    assert stats.get("missing_visuals") in (None, [])
    sentence_result = result["sentences"][0]
    assert sentence_result["videos"] or sentence_result["images"]
    asset = (sentence_result["videos"] or sentence_result["images"])[0]
    assert asset["tier"] == "activity_broll"


def test_unsearched_hook_uncovered_joins_missing_visuals(
    monkeypatch, tmp_path
):
    result, calls = _run_hook_scene(
        monkeypatch, tmp_path,
        search=lambda s, n: ([], [], []),
    )
    stats = result["asset_stats"]
    missing = stats["missing_visuals"]
    assert len(missing) == 1
    entry = missing[0]
    assert entry["sentence_number"] == 1
    assert entry["reason"] == "unsearched_role"
    assert entry["role"] == "hook"
    attempts = entry["tier_attempts"]
    assert attempts[0]["tier"] == "activity_broll"
    assert attempts[0]["entry"] == "unsearched_role"
    assert len(attempts[0]["queries"]) <= 3  # D-B2 T3 budget
    # The entry was at T3; nothing searched this beat at T1/T2.
    assert calls and calls[0]["tier"] == "activity_broll"
    assert not [
        c for c in calls if (c or {}).get("tier") in ("direct", "related")
    ]

    # Phase 1 shape contract: the SAME list, reconciled by the
    # asset report — coverage_outcome present (None without a
    # timeline), and the ladder's totals surface at report level.
    report = build_asset_report([result], timeline_result=None)
    report_missing = report["missing_visuals"]
    assert len(report_missing) == 1
    assert report_missing[0]["reason"] == "unsearched_role"
    assert report_missing[0]["coverage_outcome"] is None
    assert report["tier_escalation"]["attempts"] >= 1
    # The entry attempt is among the report's ladder records (the
    # entry pass's own T4 continuation may also be recorded — the
    # ladder walks on from its T3 entry when the pool stays empty).
    assert any(
        attempt["tier"] == "activity_broll"
        and attempt.get("entry") == "unsearched_role"
        for attempt in report["tier_attempts"]
    )
