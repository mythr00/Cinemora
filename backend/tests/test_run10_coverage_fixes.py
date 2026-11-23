"""Run-10 coverage fixes (2026-11-22): attempt idempotency in the
tier ladder + the T5 graphics guarantee.

Run 10 (a9a05467, Petito) died at the editor's coverage gate —
21/23 sentences, the closing beats (scene 7, sentences 22-23)
uncovered. Two machinery defects caused it, both pinned here:

1. ATTEMPT IDEMPOTENCY. The breadth-first ledger protected first
   attempts, but identical escalation batches re-executed across
   shots, beats and recovery rounds: the legal beat's related
   batch "verdict forms 2021" was searched by shot 1, AGAIN by
   shot 2, and walked twice more by recovery rounds; "phone 2021"
   and "white van 2021" repeated the same way. All 40 D-B2
   searches drained on repeats (47 of 61 tier attempts ended
   budget_exhausted), so the closing beats' tiers never executed
   a query. A completed batch (need scope + tier + normalised
   query set) is now terminal in the production ledger for its
   beat: never re-searched, never re-spent by a sibling shot or
   a recovery round; a verified batch's pool replays instead.
   Distinct beats keep their own first attempts — the pinned
   breadth-first fairness contract is untouched.

2. T5 GRAPHICS GUARANTEE. The closing beats WERE graphics
   targets, but every content family declined them (manifest
   outcome: skipped / no_schematic_labels) and the chain had no
   floor — the beats got nothing. The chain's final stage is now
   the ladder's T5: a designed closing card from the motion
   engine (the sentence's own words), validated, delivered and
   tier-stamped "graphic" like every other graphic. A beat the
   guarantee itself cannot serve is skipped with the typed
   reason, and jobs stamps that outcome onto its missing_visuals
   record.
"""

import json
import uuid
from pathlib import Path

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import graphics_pipeline
from services import media_pipeline as mp
from services import story_understanding as story
from services import tier_ladder
from services.timeline import build_scene_timeline
from services.visual_registry import VisualRegistry


# ---------------------------------------------------------------------------
# Fixtures (modelled on tests/test_phase2_ladder.py)
# ---------------------------------------------------------------------------

S9_TEXT = (
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


def _good(marker="phones-evidence"):
    return {
        "url": f"https://media.example.org/{marker}",
        "title": "Investigators examining phones and travel records",
        "snippet": "phones travel records evidence examination",
        "provider": "youtube",
    }


def _downloader(succeed_markers=(), fail_all=False):
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


def _beat_and_scenario():
    sentence = {
        "sentence_number": 9, "text": S9_TEXT, "role": "action",
        "fallback_concepts": S9_CONCEPTS, "concepts_source": "llm_plan",
        "visual_intents": {
            "primary_visual_intent": "human_activity",
            "intents_source": "llm_plan",
        },
    }
    beat = {
        "beat_id": "beat_s9", "sentence_numbers": [9],
        "text": S9_TEXT, "sentences": [sentence],
        "subject_id": None, "place": None,
    }
    scenario = {
        "sentence_number": 9, "specificity": "representative",
        "kind": "person_activity", "subject": "",
        "search_terms": ["phone records"],
        "visual": "analysts reviewing phone records",
        "queries": ["analysts reviewing phone records"],
    }
    return beat, scenario


def _need(shot_index):
    return {
        "beat_id": "beat_s9", "shot_index": shot_index,
        "shot_type": "person_activity", "specificity": "representative",
        "visual_category": "contextual", "description": "",
        "required_subject_id": None, "required_subject": "",
        "required_type": "person", "era": "2010s", "place": "",
        "event": "", "visual_requirement": "", "forbidden": [],
        "priority": 2, "tier": "direct",
        "fallback_concepts": S9_CONCEPTS, "concepts_source": "llm_plan",
        "visual_intents": {
            "primary_visual_intent": "human_activity",
            "intents_source": "llm_plan",
        },
        "queries": ["analysts reviewing phone records"],
    }


def _run_shot(monkeypatch, tmp_path, *, search, beat, scenario, need,
              scenario_index, registry, downloader=None,
              beat_videos=None, beat_images=None, used_keys=None):
    """One _uve_search_and_pick pass for one shot of a beat, with
    only the provider search and the downloader stubbed (the
    production pattern from test_phase2_ladder)."""
    calls = []

    def _search(scenario_, cache, pid, need_):
        calls.append({
            "tier": (need_ or {}).get("tier"),
            "queries": tuple((need_ or {}).get("queries") or ()),
        })
        return search(scenario_, need_)

    monkeypatch.setattr(mp, "_uve_scenario_search", _search)
    if downloader is not None:
        monkeypatch.setattr(mp, "_uve_download_any_candidate", downloader)
    scene_stats = {
        "candidates_considered": 0, "candidates_rejected": 0,
        "downloads_failed": 0, "providers": {}, "kinds": {},
    }
    asset = mp._uve_search_and_pick(
        scenario, need,
        search_cache={}, project_id="t", bible=_bible(),
        registry=registry, used_keys=used_keys if used_keys is not None else set(),
        scene_dir=tmp_path, scene_stats=scene_stats,
        beat=beat,
        beat_videos=beat_videos if beat_videos is not None else [],
        beat_images=beat_images if beat_images is not None else [],
        scenario_index=scenario_index,
        queries=need.get("queries") or [],
        review_records=[],
    )
    return asset, scene_stats, calls


def _escalation_calls(calls):
    return [c for c in calls if c["tier"] not in (None, "direct")]


# ---------------------------------------------------------------------------
# Fix 1 — ledger attempt registry (units)
# ---------------------------------------------------------------------------

NEED_SHOT_1 = ("beat_legal", 1, 22)
NEED_SHOT_2 = ("beat_legal", 2, 22)
NEED_OTHER_BEAT = ("beat_other", 1, 22)


def test_attempt_registry_records_terminal_batches_only():
    ledger = tier_ladder.EscalationLedger()
    # Nothing recorded yet.
    assert ledger.completed_attempt(
        "related", ["verdict forms 2021"], need_key=NEED_SHOT_1
    ) is None
    # A non-terminal outcome is not recorded.
    ledger.record_attempt(
        "related", ["verdict forms 2021"], outcome="budget_exhausted",
        need_key=NEED_SHOT_1,
    )
    assert ledger.completed_attempt(
        "related", ["verdict forms 2021"], need_key=NEED_SHOT_1
    ) is None
    # A terminal pool_exhausted batch is recorded with an empty pool.
    ledger.record_attempt(
        "related", ["verdict forms 2021"], outcome="pool_exhausted",
        verified=0, need_key=NEED_SHOT_1,
    )
    record = ledger.completed_attempt(
        "related", ["verdict forms 2021"], need_key=NEED_SHOT_1
    )
    assert record is not None
    assert record["outcome"] == "pool_exhausted"
    assert record["pool_items"] == []
    assert record["need_key"] == NEED_SHOT_1
    assert record["need_scope"] == ("beat_legal", 22)
    # First completion wins — a later record attempt cannot rewrite it.
    ledger.record_attempt(
        "related", ["verdict forms 2021"], outcome="verified",
        verified=3, pool_items=[{"candidate_id": "x"}],
        need_key=NEED_SHOT_1,
    )
    assert ledger.completed_attempt(
        "related", ["verdict forms 2021"], need_key=NEED_SHOT_1
    )["outcome"] == "pool_exhausted"
    # The summary carries the idempotency accounting.
    summary = ledger.summary()
    assert summary["attempts_recorded"] == 1
    assert summary["attempt_repeats"] >= 1


def test_attempt_scope_shares_shots_but_not_beats():
    """The fairness boundary of the registry: a sibling SHOT of
    the same beat (scenario_index differs) recognises the batch
    as a repeat; a DIFFERENT beat deriving the identical batch
    does not — every distinct need keeps its own first attempts
    (the pinned Phase-2 breadth-first contract)."""
    ledger = tier_ladder.EscalationLedger()
    ledger.record_attempt(
        "related", ["verdict forms 2021"], outcome="pool_exhausted",
        verified=0, need_key=NEED_SHOT_1,
    )
    assert ledger.completed_attempt(
        "related", ["verdict forms 2021"], need_key=NEED_SHOT_2
    ) is not None
    assert ledger.completed_attempt(
        "related", ["verdict forms 2021"], need_key=NEED_OTHER_BEAT
    ) is None
    assert tier_ladder.attempt_scope(NEED_SHOT_1) == ("beat_legal", 22)
    assert tier_ladder.attempt_scope(NEED_SHOT_2) == ("beat_legal", 22)
    assert tier_ladder.attempt_scope(None) is None


def test_attempt_key_normalises_word_order_and_case():
    a = tier_ladder.attempt_key(NEED_SHOT_1, "related", ["Verdict Forms 2021"])
    b = tier_ladder.attempt_key(NEED_SHOT_2, "related", ["2021 verdict forms"])
    assert a is not None and a == b
    # A different tier, query set, or beat is a different batch.
    assert tier_ladder.attempt_key(
        NEED_SHOT_1, "atmospheric", ["Verdict Forms 2021"]
    ) != a
    assert tier_ladder.attempt_key(
        NEED_SHOT_1, "related", ["courtroom bench 2021"]
    ) != a
    assert tier_ladder.attempt_key(
        NEED_OTHER_BEAT, "related", ["Verdict Forms 2021"]
    ) != a
    # No usable query -> no identity (nothing executed, nothing to repeat).
    assert tier_ladder.attempt_key(NEED_SHOT_1, "related", []) is None
    assert tier_ladder.attempt_key(NEED_SHOT_1, "related", ["a b"]) is None


def test_attempt_registry_replay_pool_is_isolated():
    ledger = tier_ladder.EscalationLedger()
    pool = [{"candidate_id": "c1", "verification": {"accepted": True}}]
    ledger.record_attempt(
        "related", ["phones 2010s"], outcome="verified", verified=1,
        pool_items=pool, need_key=NEED_SHOT_1,
    )
    first = ledger.completed_attempt(
        "related", ["phones 2010s"], need_key=NEED_SHOT_1
    )
    first["pool_items"][0]["candidate_id"] = "mutated"
    second = ledger.completed_attempt(
        "related", ["phones 2010s"], need_key=NEED_SHOT_2
    )
    assert second["pool_items"][0]["candidate_id"] == "c1"
    assert second["repeats"] == 2


# ---------------------------------------------------------------------------
# Fix 1 — the run-10 pattern in the pipeline
# ---------------------------------------------------------------------------

def test_sibling_shot_rewalk_spends_nothing_and_preserves_budget(
    monkeypatch, tmp_path
):
    """Run 10's shape: two shots of one beat derive IDENTICAL
    escalation batches. Shot 1's walk executes and exhausts them;
    shot 2's walk must re-execute NOTHING (and spend nothing),
    while a later, different need still gets its first attempts —
    the budget the repeats used to drain survives for it."""
    beat, scenario = _beat_and_scenario()
    registry = VisualRegistry()
    ledger = tier_ladder.ledger_for(registry)

    def search(scenario_, need_):
        return ([_junk()], [], [])

    # Shot 1: the full ladder walk executes (all tiers exhausted).
    _asset1, stats1, calls1 = _run_shot(
        monkeypatch, tmp_path, search=search, beat=beat,
        scenario=scenario, need=_need(1), scenario_index=1,
        registry=registry,
    )
    esc1 = _escalation_calls(calls1)
    assert esc1, "shot 1 must execute escalation searches"
    spent_after_shot1 = ledger.searches
    assert spent_after_shot1 > 0
    attempts1 = stats1["coverage_passes"][0]["tier_attempts"]
    assert attempts1
    assert all(a["outcome"] == "pool_exhausted" for a in attempts1)
    assert not any(a.get("repeat") for a in attempts1)

    # Shot 2 (a different need key — same beat, shot_index 2): its
    # own direct search runs, but the ladder re-executes nothing.
    _asset2, stats2, calls2 = _run_shot(
        monkeypatch, tmp_path, search=search, beat=beat,
        scenario=scenario, need=_need(2), scenario_index=2,
        registry=registry,
    )
    assert _escalation_calls(calls2) == []
    assert ledger.searches == spent_after_shot1
    attempts2 = stats2["coverage_passes"][0]["tier_attempts"]
    assert attempts2
    assert all(a.get("repeat") is True for a in attempts2)
    assert all(a["queries"] == [] for a in attempts2)
    assert all(a["outcome"] == "pool_exhausted" for a in attempts2)
    assert ledger.attempt_repeats >= len(attempts2)

    # A later need with NEW queries (the closing-beat position)
    # still gets its first attempts executed — the repeats did not
    # drain its budget.
    other_sentence = {
        "sentence_number": 22,
        "text": "Gabby Petito never got a trial, or a verdict.",
        "role": "legal",
        "fallback_concepts": {
            "activities": ["sitting in an empty courtroom"],
            "objects": ["gavel", "courtroom bench"],
            "settings": ["empty courtroom"], "place_types": [],
            "mood": [], "era_terms": ["2021"],
        },
        "concepts_source": "llm_plan",
        "visual_intents": {
            "primary_visual_intent": "establishing",
            "intents_source": "llm_plan",
        },
    }
    other_beat = {
        "beat_id": "beat_legal", "sentence_numbers": [22],
        "text": other_sentence["text"], "sentences": [other_sentence],
        "subject_id": None, "place": None,
    }
    other_scenario = {
        "sentence_number": 22, "specificity": "generic",
        "kind": "place", "subject": "",
        "search_terms": ["empty courtroom"],
        "visual": "An empty courtroom",
        "queries": ["empty courtroom"],
    }
    other_need = {
        "beat_id": "beat_legal", "shot_index": 1,
        "shot_type": "place", "specificity": "generic",
        "visual_category": "environmental", "description": "",
        "required_subject_id": None, "required_subject": "",
        "required_type": "location", "era": "2021", "place": "",
        "event": "", "visual_requirement": "", "forbidden": [],
        "priority": 3, "tier": "direct",
        "fallback_concepts": other_sentence["fallback_concepts"],
        "concepts_source": "llm_plan",
        "visual_intents": other_sentence["visual_intents"],
        "queries": ["empty courtroom"],
    }
    _asset3, _stats3, calls3 = _run_shot(
        monkeypatch, tmp_path, search=search, beat=other_beat,
        scenario=other_scenario, need=other_need, scenario_index=1,
        registry=registry,
    )
    assert _escalation_calls(calls3), (
        "the late need's NEW batches must still execute"
    )
    assert ledger.searches > spent_after_shot1


def test_verified_batch_replays_pool_without_research(
    monkeypatch, tmp_path
):
    """Run 10 passes 16->17 shape: shot 1's related batch verifies
    candidates but its draw cannot fetch them (downloader down);
    shot 2 must NOT re-search the batch — the recorded pool
    replays, and the draw fetches from it."""
    beat, scenario = _beat_and_scenario()
    registry = VisualRegistry()
    ledger = tier_ladder.ledger_for(registry)
    # Unique markers: _FAILED_DOWNLOAD_URLS is process-global, and
    # this test deliberately fails these downloads in shot 1 — the
    # URLs must not collide with any other test's candidates.
    goods = [_good(f"run10-replay-{i}") for i in range(3)]

    def search(scenario_, need_):
        if (need_ or {}).get("tier") == "related":
            return (list(goods), [], [])
        return ([_junk()], [], [])

    beat_videos, beat_images = [], []
    used_keys = set()

    failing, _log1 = _downloader(fail_all=True)
    asset1, stats1, calls1 = _run_shot(
        monkeypatch, tmp_path, search=search, beat=beat,
        scenario=scenario, need=_need(1), scenario_index=1,
        registry=registry, downloader=failing,
        beat_videos=beat_videos, beat_images=beat_images,
        used_keys=used_keys,
    )
    assert asset1 is None  # nothing could be fetched
    related_calls_1 = [
        c for c in calls1 if c["tier"] == "related"
    ]
    assert related_calls_1
    attempts1 = stats1["coverage_passes"][0]["tier_attempts"]
    assert attempts1[0]["outcome"] == "verified"
    spent_after_shot1 = ledger.searches
    assert spent_after_shot1 > 0

    working, _log2 = _downloader(
        succeed_markers=("run10-replay",)
    )
    asset2, stats2, calls2 = _run_shot(
        monkeypatch, tmp_path, search=search, beat=beat,
        scenario=scenario, need=_need(2), scenario_index=2,
        registry=registry, downloader=working,
        beat_videos=beat_videos, beat_images=beat_images,
        used_keys=used_keys,
    )
    # The batch was NOT re-searched and nothing more was spent...
    assert [c for c in calls2 if c["tier"] == "related"] == []
    assert ledger.searches == spent_after_shot1
    # ...yet the replayed pool covered the shot, stamped honestly.
    assert asset2 is not None
    assert asset2["tier"] == "related"
    assert asset2["selection_via"] == "tier_escalation"
    attempts2 = stats2["coverage_passes"][0]["tier_attempts"]
    assert attempts2[0]["tier"] == "related"
    assert attempts2[0].get("repeat") is True
    assert attempts2[0]["queries"] == []
    assert attempts2[0]["outcome"] == "verified"


def test_unsearched_entry_batch_is_recorded_and_not_respent(
    monkeypatch, tmp_path
):
    """The D-B3 entry's executed batch joins the same registry:
    a second identical entry (a sibling unsearched beat with the
    same merged concepts) spends nothing and records the repeat."""
    registry = VisualRegistry()
    ledger = tier_ladder.ledger_for(registry)
    calls = []

    def _search(scenario_, cache, pid, need_):
        calls.append((need_ or {}).get("tier"))
        return ([_junk()], [], [])

    monkeypatch.setattr(mp, "_uve_scenario_search", _search)
    downloader, _log = _downloader(fail_all=True)
    monkeypatch.setattr(mp, "_uve_download_any_candidate", downloader)

    sentence = {
        "sentence_number": 1, "text": S9_TEXT, "role": "hook",
        "scenarios": [], "fallback_concepts": S9_CONCEPTS,
        "concepts_source": "llm_plan",
    }
    beat = {
        "beat_id": "beat_hook", "subject_id": None, "place": None,
        "searchable": False, "role": "hook",
        "sentence_numbers": [1], "sentences": [sentence],
        "text": S9_TEXT, "shot_needs": [],
    }

    def _entry():
        scene_stats = {
            "candidates_considered": 0, "candidates_rejected": 0,
            "downloads_failed": 0, "providers": {}, "kinds": {},
        }
        return mp._uve_unsearched_ladder_entry(
            beat, bible=_bible(), registry=registry, search_cache={},
            project_id=f"run10-entry-{uuid.uuid4()}",
            used_keys=set(), scene_dir=tmp_path,
            scene_stats=scene_stats, beat_videos=[], beat_images=[],
            review_records=[],
        )

    first = _entry()
    assert first[0]["outcome"] == "pool_exhausted"
    assert not first[0].get("repeat")
    spent = ledger.searches
    assert spent > 0
    calls_after_first = len(calls)

    second = _entry()
    assert second[0].get("repeat") is True
    assert second[0]["prior_outcome"] == "pool_exhausted"
    assert second[0]["queries"] == []
    assert ledger.searches == spent
    assert len(calls) == calls_after_first


# ---------------------------------------------------------------------------
# Fix 2 — T5 graphics guarantee
# ---------------------------------------------------------------------------

CLOSING_TEXT = "Gabby Petito never got a trial, or a verdict."


def _graphics_data():
    return {
        "case_bible": {"title": "Gabby Petito", "timeline": []},
        "scenes": [
            {
                "scene_number": 7,
                "sentences": [
                    {"text": CLOSING_TEXT, "sentence_number": 22},
                ],
            }
        ],
        # Run-10 shape: scene 7 was processed and held NO genuine
        # verified visual for these beats (its coverage records are
        # what made the schematic stage decidable — and its labels
        # absent, hence no_schematic_labels).
        "processed_scenes": [
            {
                "scene": {
                    "scene_number": 7,
                    "sentences": [
                        {"text": CLOSING_TEXT, "sentence_number": 22},
                    ],
                },
                "videos": [],
                "images": [],
            }
        ],
    }


def _stub_validator(monkeypatch):
    monkeypatch.setattr(
        graphics_pipeline, "validate_graphic_output",
        lambda path, expected_duration=None, **kwargs: {
            "passed": True, "failure_kind": None, "detail": "",
            "checks": {}, "probe": None,
        },
    )


def _stub_closing_card(monkeypatch, refuse=False):
    from services.motion_engine import templates as engine_templates

    def fake_card(data, style, out_path, duration=5.0,
                  sentence_index=None, scene_number=None):
        if refuse:
            return None, {"reason": "no_text"}
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 4096)
        return str(path), {
            "generator": "motion_engine", "template": "closing_card",
            "generated": True,
        }

    monkeypatch.setitem(
        engine_templates.TEMPLATES, "closing_card", fake_card
    )


def _closing_target():
    # Run 10's manifest entries for the closing beats verbatim in
    # shape: gm_0006/gm_0007, purpose coverage_fallback, empty
    # template candidates, event_schematic the only family.
    return [{
        "scene_number": 7, "sentence_index": 0, "sentence_number": 22,
        "text": CLOSING_TEXT,
        "purpose": "coverage_fallback",
        "template_candidates": [],
        "family_candidates": ["event_schematic"],
    }]


def test_unservable_target_gets_guarantee_closing_card(
    tmp_path, monkeypatch
):
    """The run-10 closing beat: no stat, no date, no sequence, no
    schematic labels, no engine data — every content family
    declines. The T5 floor serves it with a designed closing
    card instead of nothing."""
    _stub_validator(monkeypatch)
    _stub_closing_card(monkeypatch)
    monkeypatch.setattr(
        graphics_pipeline, "_render_stat_graphic",
        lambda path, *a, **k: Path(path).write_bytes(b"\x00" * 2048),
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _graphics_data(), tmp_path, mode="balanced",
        targets=_closing_target(),
    )
    cards = [a for a in assets if a["graphic_type"] == "closing_card"]
    assert len(cards) == 1
    card = cards[0]
    assert card["coverage_guarantee"] is True
    assert card["family"] == "motion_engine"
    assert card["delivered"] is True
    assert card["targeted"] is True
    assert card["scene_number"] == 7
    assert card["sentence_index"] == 0
    assert card["sentence_number"] == 22
    # Honesty stamps: generated in-house, never footage.
    assert card["generated"] is True
    assert card["visual_format"] == "graphic"
    assert card["source"] == "cinemora_motion_graphics"
    # The recipe regenerates the same template.
    assert card["graphic_recipe"]["template"] == "closing_card"
    # PIN CHANGED (founder defect ruling 2026-11-23): the card's
    # payload is the short element SELECTED from the sentence —
    # the person's name — never the sentence itself (the old pin
    # expected the full narration sentence as the card's text,
    # which is the full-sentence-card defect the founder banned).
    assert card["engine_inputs"]["quote"] == "Gabby Petito"
    assert card["engine_inputs"]["kicker"] == "Gabby Petito"

    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert manifest["targets"]["generated"] == 1
    assert manifest["targets"]["skipped"] == []
    outcome = manifest["targets"]["outcomes"][0]
    assert outcome["outcome"] == "generated"
    assert outcome["family"] == "motion_engine"
    assert outcome["reason"] == "coverage_guarantee:closing_card"


def test_guarantee_failure_is_recorded_with_typed_reason(
    tmp_path, monkeypatch
):
    """When even the floor cannot serve the beat, the skip is
    recorded with a typed MG-08 reason — for the run-10 sentence
    shape, the schematic stage's no_schematic_labels (the reason
    run 10 itself recorded) — never a silent skip."""
    _stub_validator(monkeypatch)
    _stub_closing_card(monkeypatch, refuse=True)
    monkeypatch.setattr(
        graphics_pipeline, "_render_stat_graphic",
        lambda path, *a, **k: Path(path).write_bytes(b"\x00" * 2048),
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _graphics_data(), tmp_path, mode="balanced",
        targets=_closing_target(),
    )
    assert assets == []
    manifest = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert manifest["targets"]["generated"] == 0
    skips = manifest["targets"]["skipped"]
    assert len(skips) == 1
    assert skips[0]["sentence_number"] == 22
    assert skips[0]["reason"] in graphics_pipeline.GRAPHIC_SKIP_REASONS
    assert skips[0]["reason"] == "no_schematic_labels"


def test_guarantee_graphic_covers_sentence_on_timeline(
    tmp_path, monkeypatch
):
    """End to end for the beat: graphics asset -> jobs attach
    shape -> scene timeline. The sentence's only visual is the
    guarantee card; coverage is complete and the item is stamped
    assignment=motion_graphic, tier=graphic."""
    _stub_validator(monkeypatch)
    _stub_closing_card(monkeypatch)
    monkeypatch.setattr(
        graphics_pipeline, "_render_stat_graphic",
        lambda path, *a, **k: Path(path).write_bytes(b"\x00" * 2048),
    )
    assets = graphics_pipeline.build_motion_graphics_assets(
        _graphics_data(), tmp_path, mode="balanced",
        targets=_closing_target(),
    )
    assert len(assets) == 1
    visual = dict(assets[0])
    visual["type"] = "video"
    visual["source"] = "cinemora_motion_graphics"
    visual["motion_graphic"] = True
    visual["file_path"] = assets[0].get("file_path") or assets[0].get("path")
    visual["path"] = visual["file_path"]
    scene_media = {
        "scene_number": 7,
        "duration_seconds": 3.0,
        "videos": [visual],
        "images": [],
        "sentences": [
            {"sentence_number": 22, "videos": [], "images": []},
        ],
    }
    timings = [{
        "sentence_index": 0, "text": CLOSING_TEXT,
        "start": 0.0, "end": 2.7,
    }]
    result = build_scene_timeline(scene_media, timings)
    assert result["uncovered_sentence_indexes"] == []
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["assignment"] == "motion_graphic"
    assert item["tier"] == "graphic"


def test_closing_card_template_renders_and_refuses_honestly(tmp_path):
    """The real template: renders a valid MP4 from a short
    designed payload (the REAL validator passes it), and refuses
    — with typed reasons — for empty text and for a full
    narration sentence.

    PIN CHANGED (founder defect ruling 2026-11-23): the render
    case previously fed the whole closing sentence as the card's
    quote; that IS the banned full-sentence card. The render
    case now uses the selected short payload, and the sentence
    itself is a refusal case."""
    from services.motion_engine import templates as engine_templates

    out = tmp_path / "card.mp4"
    rendered, meta = engine_templates.TEMPLATES["closing_card"](
        {"quote": "Gabby Petito", "kicker": "Gabby Petito"},
        "editorial", out, duration=3.0,
        sentence_index=0, scene_number=7,
    )
    assert rendered is not None
    assert Path(rendered).exists()
    verdict = graphics_pipeline.validate_graphic_output(
        rendered, expected_duration=3.0
    )
    assert verdict["passed"], verdict

    refused, meta = engine_templates.TEMPLATES["closing_card"](
        {"quote": "  "}, "editorial", tmp_path / "none.mp4",
        duration=3.0,
    )
    assert refused is None
    assert meta["reason"] == "no_text"

    refused, meta = engine_templates.TEMPLATES["closing_card"](
        {"quote": CLOSING_TEXT, "kicker": "Gabby Petito"},
        "editorial", tmp_path / "sentence.mp4",
        duration=3.0,
    )
    assert refused is None
    assert meta["reason"] == "payload_not_short"


def test_missing_visuals_annotated_with_graphics_outcomes(tmp_path):
    """jobs stamps the graphics stage's outcome onto the
    missing_visuals records the asset report aggregates — served
    beats and genuinely-unservable beats alike carry the result
    (and its reason) in the canonical missing record."""
    record = {
        "targets": {
            "outcomes": [
                {"scene_number": 7, "sentence_index": 0,
                 "sentence_number": 22, "outcome": "generated",
                 "family": "motion_engine",
                 "reason": "coverage_guarantee:closing_card"},
                {"scene_number": 7, "sentence_index": 1,
                 "sentence_number": 23, "outcome": "skipped",
                 "family": None, "reason": "no_schematic_labels"},
            ]
        }
    }
    manifest_file = tmp_path / "motion_graphics.json"
    manifest_file.write_text(json.dumps(record))
    missing = [
        {"sentence_number": 22, "reason": "no_verified_asset"},
        {"sentence_number": 23, "reason": "unsearched_role"},
    ]
    processed = [{
        "scene": {"scene_number": 7},
        "asset_stats": {"missing_visuals": missing},
    }]
    annotated = jobs._annotate_missing_visual_graphics_outcomes(
        processed, manifest_file
    )
    assert annotated == 2
    assert missing[0]["graphics_outcome"] == {
        "outcome": "generated", "family": "motion_engine",
        "reason": "coverage_guarantee:closing_card",
    }
    assert missing[1]["graphics_outcome"] == {
        "outcome": "skipped", "family": None,
        "reason": "no_schematic_labels",
    }
    # Fail-soft: an unreadable graphics record annotates nothing.
    assert jobs._annotate_missing_visual_graphics_outcomes(
        processed, tmp_path / "does_not_exist.json"
    ) == 0
