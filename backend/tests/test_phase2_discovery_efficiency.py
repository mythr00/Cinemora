"""Phase 2 (Discovery Efficiency) — integration behaviour.

The durable discovery memory (services.discovery_memory) wired into
the pipeline: verdicts computed once and replayed across runs under
identical decision contexts; candidate-intrinsic verdicts portable
across sentences; download failures and post-download demotions
honoured by later runs without a new attempt; the scene driver's
fair initial pass (every beat's initial discovery before any beat's
extended effort); honest gap states (a never-searched sentence is
named and assessed STARVED, never confused with a genuine gap); and
the saturation record in run_metrics (measurement only).

"Runs" here are separate RunContexts over one project directory —
the same boundary a process restart crosses in production (jobs
activates a fresh context per production; the store is on disk).
"""

import copy
from pathlib import Path

import pytest

from services import asset_coverage
from services import discovery_memory
from services import media_pipeline as mp
from services import pipeline_verification as pv
from services import run_context
from services import run_metrics
from services import story_understanding as story


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _berlin_fixture():
    bible = story.hydrate_bible({
        "entities": [
            {"id": "berlin_wall", "canonical": "Berlin Wall",
             "aliases": ["the Wall"], "type": "object"},
            {"id": "east_berlin", "canonical": "East Berlin",
             "aliases": [], "type": "location"},
        ]
    })
    scenario = {
        "subject_id": "berlin_wall", "specificity": "representative",
        "search_terms": ["crowds crossing checkpoint"],
        "visual": "crowds of people crossing through an opened checkpoint",
        "era": "1989",
    }
    need = {
        "required_type": "event", "specificity": "representative",
        "visual_category": "evidence",
        "required_subject": "East Berlin", "place": "berlin",
    }
    candidates = [
        {"title": "Champagne wall rental", "snippet": "weddings",
         "url": "https://x.example/1"},
        {"title": "Berlin Wall crowds 1989", "snippet": "crowds crossing",
         "url": "https://x.example/2"},
    ]
    return candidates, scenario, bible, need


@pytest.fixture()
def memory(tmp_path, monkeypatch):
    """A durable store on a tmp project dir, bound as the pipeline's
    memory handle (the resolver serves it under activated contexts)."""
    store = discovery_memory.DiscoveryMemory.open(str(tmp_path))
    assert store is not None
    monkeypatch.setattr(mp, "_discovery_memory", store)
    monkeypatch.setattr(mp, "_discovery_memory_project_dir", str(tmp_path))
    yield store
    discovery_memory.close_all()


def _activate(run_id):
    ctx = run_context.RunContext(run_id=run_id, project_id="p2-test")
    previous = run_context.activate(ctx)
    return ctx, previous


class _CountingGate:
    def __init__(self, monkeypatch):
        self.calls = 0
        real_gate = mp.gate_shot_need

        def counting(candidate, shot_need):
            self.calls += 1
            return real_gate(candidate, shot_need)

        monkeypatch.setattr(mp, "gate_shot_need", counting)


# ---------------------------------------------------------------------------
# Verdict replay across runs (acceptance #13, verification half)
# ---------------------------------------------------------------------------

def test_verdict_replayed_across_runs_same_context(memory, monkeypatch):
    gate = _CountingGate(monkeypatch)
    candidates, scenario, bible, need = _berlin_fixture()

    _ctx, prev = _activate("p2-run-1")
    try:
        first = mp._uve_verify_all(
            copy.deepcopy(candidates), scenario, bible, shot_need=need
        )
    finally:
        run_context.deactivate(prev)
    assert gate.calls == 2
    assert memory.summary()["verdicts"] == 2

    # A fresh run (fresh context, fresh verdict cache) over the same
    # production: both evaluations replay from the durable store —
    # the gate never executes again.
    _ctx, prev = _activate("p2-run-2")
    try:
        second = mp._uve_verify_all(
            copy.deepcopy(candidates), scenario, bible, shot_need=need
        )
    finally:
        run_context.deactivate(prev)
    assert gate.calls == 2
    assert [
        (i.get("verification") or {}).get("accepted") for i in second
    ] == [
        (i.get("verification") or {}).get("accepted") for i in first
    ]


def test_verdict_not_replayed_under_different_bible(memory, monkeypatch):
    """The bible fingerprint is part of the durable identity: a new
    script cut (different bible) voids the record — the evaluation
    runs again rather than replaying a verdict earned under another
    bible's entities."""
    gate = _CountingGate(monkeypatch)
    candidates, scenario, bible, need = _berlin_fixture()

    _ctx, prev = _activate("p2-run-1")
    try:
        mp._uve_verify_all(
            copy.deepcopy(candidates), scenario, bible, shot_need=need
        )
    finally:
        run_context.deactivate(prev)
    assert gate.calls == 2

    other_bible = story.hydrate_bible({
        "entities": [
            {"id": "berlin_wall", "canonical": "Berlin Wall",
             "aliases": ["the Wall"], "type": "object"},
            {"id": "east_berlin", "canonical": "East Berlin",
             "aliases": [], "type": "location"},
            {"id": "checkpoint_charlie", "canonical": "Checkpoint Charlie",
             "aliases": [], "type": "location"},
        ]
    })
    _ctx, prev = _activate("p2-run-2")
    try:
        mp._uve_verify_all(
            copy.deepcopy(candidates), scenario, other_bible,
            shot_need=need,
        )
    finally:
        run_context.deactivate(prev)
    assert gate.calls == 4  # re-evaluated under the new bible


def test_intrinsic_verdict_portable_across_sentences(memory, monkeypatch):
    """A meme-format candidate is intrinsically dead: scored once,
    its verdict replays under a different sentence's context without
    a second scorer call (the gate still runs — VA-11 accounting is
    unchanged). Person-mismatch and other contextual judgements are
    NOT shared this way; only the candidate-only reasons are."""
    gate = _CountingGate(monkeypatch)
    scorer_calls = []
    real_scorer = story.verify_candidate

    def counting_scorer(candidate, scenario, bible):
        scorer_calls.append(1)
        return real_scorer(candidate, scenario, bible)

    monkeypatch.setattr(pv.story, "verify_candidate", counting_scorer)
    _candidates, _scenario, bible, _need = _berlin_fixture()
    meme = {
        "title": "POV: you are at the Berlin Wall",
        "snippet": "crowds",
        "url": "https://media.example.org/meme-1",
        "provider": "youtube",
    }
    scenario_a = {
        "subject_id": "berlin_wall", "specificity": "representative",
        "search_terms": ["berlin wall"], "visual": "the wall",
        "era": "1989",
    }
    scenario_b = {
        "subject_id": "east_berlin", "specificity": "representative",
        "search_terms": ["east berlin streets"],
        "visual": "east berlin streets", "era": "1989",
    }

    _ctx, prev = _activate("p2-run-1")
    try:
        first = mp._uve_verify_all(
            [copy.deepcopy(meme)], scenario_a, bible, shot_need=None
        )
    finally:
        run_context.deactivate(prev)
    assert len(scorer_calls) == 1
    assert first[0]["reject_reason"] == story.MEME_FORMAT_REASON

    _ctx, prev = _activate("p2-run-2")
    try:
        second = mp._uve_verify_all(
            [copy.deepcopy(meme)], scenario_b, bible, shot_need=None
        )
    finally:
        run_context.deactivate(prev)
    assert len(scorer_calls) == 1  # never re-scored
    assert gate.calls == 2  # the gate ran in both runs
    assert second[0]["reject_reason"] == story.MEME_FORMAT_REASON
    assert second[0]["rejection_stage"] == "scoring"


# ---------------------------------------------------------------------------
# Download memory across runs (acceptance #13, download half)
# ---------------------------------------------------------------------------

def test_download_failure_seeds_next_run_guard(memory, tmp_path,
                                                monkeypatch):
    candidate = {
        "title": "Berlin Wall crowds 1989", "snippet": "crowds",
        "url": "https://media.example/berlin-1", "provider": "youtube",
        "candidate_id": "cand-berlin-1",
    }
    source_key = mp.canonical_source_key(candidate["url"])

    def failing_downloader(cand, scene_dir, sentence_number, asset_index):
        cand["_download_error"] = "http_404"
        cand["_download_failure"] = {
            "failure_class": "source_unavailable",
            "retryable": False,
            "reason": "http_404",
        }
        return None

    _ctx, prev = _activate("p2-run-1")
    try:
        failures = []
        asset = mp._uve_fetch_first(
            [dict(candidate)], failing_downloader, str(tmp_path), 1, 1,
            _Registry(), set(), failures=failures,
        )
    finally:
        run_context.deactivate(prev)
    assert asset is None
    assert memory.download_failure("source", source_key) is not None

    # Next run: fresh guards; opening the production's memory seeds
    # them, and the fetch loop refuses the URL without any download.
    _ctx, prev = _activate("p2-run-2")
    try:
        assert source_key not in mp._FAILED_DOWNLOAD_URLS
        mp._open_discovery_memory(str(tmp_path))
        assert source_key in mp._FAILED_DOWNLOAD_URLS
        calls = []

        def counting_downloader(cand, scene_dir, n, i):
            calls.append(1)
            return {"file_path": "x"}

        failures = []
        asset = mp._uve_fetch_first(
            [dict(candidate)], counting_downloader, str(tmp_path), 1, 1,
            _Registry(), set(), failures=failures,
        )
    finally:
        run_context.deactivate(prev)
    assert asset is None
    assert calls == []
    assert failures[0]["reason"] == "previously_failed_this_run"


def test_dispatcher_terminal_failure_survives_runs(memory, tmp_path,
                                                   monkeypatch):
    candidate = {
        "_pool_kind": "image",
        "title": "Berlin Wall photo", "snippet": "wall",
        "url": "https://page.example/berlin-photo",
        "image_url": "https://img.example/berlin-photo.jpg",
        "provider": "wikimedia",
    }
    dl_key = mp._download_url_key_for(candidate, True)
    assert dl_key

    def failing_image(cand, scene_dir, sentence_number, asset_index):
        cand["_download_failure"] = {
            "failure_class": "source_unavailable",
            "retryable": False,
            "reason": "http_404",
        }
        return None

    monkeypatch.setattr(mp, "_uve_download_image_candidate", failing_image)
    _ctx, prev = _activate("p2-run-1")
    try:
        assert mp._uve_download_any_candidate(
            dict(candidate), str(tmp_path), 1, 1
        ) is None
    finally:
        run_context.deactivate(prev)
    assert memory.download_failure("download_url", dl_key) is not None

    calls = []

    def counting_image(cand, scene_dir, sentence_number, asset_index):
        calls.append(1)
        return {"file_path": "x"}

    monkeypatch.setattr(mp, "_uve_download_image_candidate", counting_image)
    _ctx, prev = _activate("p2-run-2")
    try:
        mp._open_discovery_memory(str(tmp_path))
        again = dict(candidate)
        assert mp._uve_download_any_candidate(
            again, str(tmp_path), 1, 1
        ) is None
        assert again["_download_error"] == "previously_failed_this_run"
    finally:
        run_context.deactivate(prev)
    assert calls == []


class _Registry:
    def register(self, **kwargs):
        return None


def test_demotion_survives_runs(memory, tmp_path, monkeypatch):
    """A candidate demoted by the transcript check after a successful
    download is never re-downloaded under the same context in a
    later run (the Phase 0 demotion leak)."""
    monkeypatch.setattr(pv, "_apply_frame_check", lambda *a, **k: None)
    monkeypatch.setattr(
        mp, "_apply_transcript_check",
        lambda asset, scenario, bible: "transcript_mismatch",
    )
    media_file = tmp_path / "asset.mp4"
    media_file.write_bytes(b"fake")
    candidate = {
        "title": "Berlin Wall crowds", "snippet": "crowds",
        "url": "https://media.example/berlin-2", "provider": "youtube",
        "candidate_id": "cand-berlin-2",
    }
    scenario = {
        "subject_id": "berlin_wall", "specificity": "representative",
        "search_terms": ["berlin wall"], "visual": "the wall",
        "era": "1989",
    }

    def ok_downloader(cand, scene_dir, sentence_number, asset_index):
        return {
            "file_path": str(media_file), "path": str(media_file),
            "type": "video", "visual_type": "video",
            "url": cand["url"], "candidate_id": cand["candidate_id"],
        }

    _ctx, prev = _activate("p2-run-1")
    try:
        failures = []
        asset = mp._uve_fetch_first(
            [dict(candidate)], ok_downloader, str(tmp_path), 1, 1,
            _Registry(), set(), failures=failures,
            transcript_scenario=scenario, transcript_bible={},
        )
    finally:
        run_context.deactivate(prev)
    assert asset is None  # demoted
    assert failures[0]["reason"] == "transcript_mismatch"

    calls = []

    def counting_downloader(cand, scene_dir, n, i):
        calls.append(1)
        return ok_downloader(cand, scene_dir, n, i)

    _ctx, prev = _activate("p2-run-2")
    try:
        failures = []
        asset = mp._uve_fetch_first(
            [dict(candidate)], counting_downloader, str(tmp_path), 1, 1,
            _Registry(), set(), failures=failures,
            transcript_scenario=scenario, transcript_bible={},
        )
    finally:
        run_context.deactivate(prev)
    assert asset is None
    assert calls == []  # never re-downloaded
    assert failures[0]["reason"] == "transcript_mismatch"
    assert failures[0]["previously_demoted"] is True


# ---------------------------------------------------------------------------
# Fair initial pass + honest gap states (scene driver)
# ---------------------------------------------------------------------------

def _scene_fixture(monkeypatch, tmp_path, events):
    """Two searchable beats; the search stub records initial-phase
    searches, the extended wrapper records extended phases."""
    sentences = [
        {"sentence_number": 1, "text": "Berlin Wall crowds gather."},
        {"sentence_number": 2, "text": "Checkpoint crowds cross."},
    ]

    def _need(beat_id, query):
        return {
            "beat_id": beat_id, "shot_index": 1,
            "shot_type": "event", "specificity": "representative",
            "visual_category": "contextual", "description": "",
            "required_subject_id": None, "required_subject": "",
            "required_type": "place", "era": "", "place": "",
            "event": "", "visual_requirement": "", "forbidden": [],
            "priority": 2, "tier": "direct",
            "fallback_concepts": {}, "concepts_source": "test",
            "visual_intents": None, "queries": [query],
        }

    def _beat(beat_id, number, query):
        return {
            "beat_id": beat_id, "subject_id": None, "place": None,
            "searchable": True, "role": "event",
            "sentence_numbers": [number],
            "sentences": [sentences[number - 1]],
            "text": sentences[number - 1]["text"],
            "shot_needs": [_need(beat_id, query)],
        }

    beats = [_beat("beat_1", 1, "berlin wall crowds"),
             _beat("beat_2", 2, "checkpoint crowds")]
    scenarios = {
        "beat_1": [{
            "sentence_number": 1, "specificity": "representative",
            "search_terms": ["berlin", "wall"],
            "visual": "berlin wall crowds",
            "queries": ["berlin wall crowds"],
        }],
        "beat_2": [{
            "sentence_number": 2, "specificity": "representative",
            "search_terms": ["checkpoint"],
            "visual": "checkpoint crowds",
            "queries": ["checkpoint crowds"],
        }],
    }

    def fake_search(scenario_, cache, pid, shot_need=None):
        events.append(("initial", scenario_["visual"]))
        good = {
            "url": f"https://media.example.org/{scenario_['visual']}",
            "title": f"{scenario_['visual']} 1989",
            "snippet": scenario_["visual"],
            "provider": "youtube",
        }
        return ([good], [], [])

    def download(candidate, scene_dir, sentence_number, asset_index):
        path = Path(scene_dir) / f"asset-{sentence_number}.mp4"
        path.write_bytes(b"fake")
        asset = dict(candidate)
        asset.update({
            "file_path": str(path), "path": str(path),
            "type": "video", "visual_type": "video",
            "segment_id": f"seg_{sentence_number}",
            "duration_seconds": 5.0,
        })
        return asset

    real_extended = mp._uve_search_and_pick_extended

    def recording_extended(state):
        events.append(("extended", state["beat"]["beat_id"]))
        return real_extended(state)

    monkeypatch.setattr(mp, "build_story_beats", lambda s, b: beats)
    monkeypatch.setattr(mp, "_load_case_bible", lambda s, p: {})
    monkeypatch.setattr(
        mp, "_beat_scenarios", lambda b: scenarios[b["beat_id"]]
    )
    monkeypatch.setattr(mp, "_uve_scenario_search", fake_search)
    monkeypatch.setattr(mp, "_uve_download_any_candidate", download)
    monkeypatch.setattr(
        mp, "_uve_search_and_pick_extended", recording_extended
    )
    scene = {
        "scene_number": 1,
        "project_id": "p2-fair-test",
        "sentences": sentences,
    }
    return scene


def test_fair_initial_pass_ordering(monkeypatch, tmp_path):
    events = []
    scene = _scene_fixture(monkeypatch, tmp_path, events)
    run_metrics.begin_run("p2-fair-test")
    result = mp._uve_process_scene_media(scene, str(tmp_path))
    initials = [i for i, e in enumerate(events) if e[0] == "initial"]
    extendeds = [i for i, e in enumerate(events) if e[0] == "extended"]
    assert len(initials) == 2 and len(extendeds) == 2
    # Every beat's initial discovery precedes ANY beat's extended
    # effort — the fair-initial-pass guarantee.
    assert max(initials) < min(extendeds)
    # Both beats covered, and the saturation record carries the
    # passes' evaluation evidence for both sentences.
    assert len(result["videos"]) == 2
    saturation = {
        entry["sentence_number"]: entry
        for entry in run_metrics.snapshot()["sentence_saturation"]
    }
    assert saturation[1]["evaluations"] >= 1
    assert saturation[2]["evaluations"] >= 1
    assert saturation[1]["new_verified"] >= 1


def test_never_searched_beat_is_named_and_starved(monkeypatch, tmp_path):
    """Skip-bug fix + gap honesty: a beat with no scenarios is
    never searched (the initial pass has nothing to run and
    recovery requires scenarios); its sentences are named in
    uncovered_unsearched with the no_scenarios state, its missing
    entries carry the state, and the coverage assessment reads
    STARVED — while a sibling beat whose SHOT NEED carries queries
    the scenario lacks IS searched (the pre-Phase-2 silent skip:
    the scenario's empty query list used to skip it before the
    need was ever consulted)."""
    sentences = [
        {"sentence_number": 1, "text": "A quiet harbour at dawn."},
        {"sentence_number": 2, "text": "Cranes load a ship."},
    ]
    bare_need = {
        "beat_id": "beat_quiet", "shot_index": 1,
        "shot_type": "place", "specificity": "generic",
        "visual_category": "contextual", "description": "",
        "required_subject_id": None, "required_subject": "",
        "required_type": "place", "era": "", "place": "",
        "event": "", "visual_requirement": "", "forbidden": [],
        "priority": 2, "tier": "direct",
        "fallback_concepts": {}, "concepts_source": "test",
        "visual_intents": None, "queries": [],
    }
    need_with_queries = dict(bare_need)
    need_with_queries["beat_id"] = "beat_cranes"
    need_with_queries["queries"] = ["harbour cranes loading ship"]
    beats = [
        {
            "beat_id": "beat_quiet", "subject_id": None, "place": None,
            "searchable": True, "role": "event",
            "sentence_numbers": [1], "sentences": [sentences[0]],
            "text": sentences[0]["text"], "shot_needs": [bare_need],
        },
        {
            "beat_id": "beat_cranes", "subject_id": None, "place": None,
            "searchable": True, "role": "event",
            "sentence_numbers": [2], "sentences": [sentences[1]],
            "text": sentences[1]["text"],
            "shot_needs": [need_with_queries],
        },
    ]
    scenarios = {
        # No scenarios at all: nothing can search this beat — the
        # initial pass has nothing to run and coverage recovery
        # (which requires scenarios) never fires for it either.
        "beat_quiet": [],
        "beat_cranes": [{
            "sentence_number": 2, "specificity": "representative",
            "search_terms": ["cranes"], "visual": "harbour cranes",
        }],
    }
    searched = []

    def fake_search(scenario_, cache, pid, shot_need=None):
        searched.append(list(scenario_.get("queries") or []))
        good = {
            "url": "https://media.example.org/cranes",
            "title": "Harbour cranes loading a ship",
            "snippet": "cranes loading",
            "provider": "youtube",
        }
        return ([good], [], [])

    def download(candidate, scene_dir, sentence_number, asset_index):
        path = Path(scene_dir) / "asset.mp4"
        path.write_bytes(b"fake")
        asset = dict(candidate)
        asset.update({
            "file_path": str(path), "path": str(path),
            "type": "video", "visual_type": "video",
            "segment_id": "seg_cranes", "duration_seconds": 5.0,
        })
        return asset

    monkeypatch.setattr(mp, "build_story_beats", lambda s, b: beats)
    monkeypatch.setattr(mp, "_load_case_bible", lambda s, p: {})
    monkeypatch.setattr(
        mp, "_beat_scenarios", lambda b: scenarios[b["beat_id"]]
    )
    monkeypatch.setattr(mp, "_uve_scenario_search", fake_search)
    monkeypatch.setattr(mp, "_uve_download_any_candidate", download)

    scene = {
        "scene_number": 1, "project_id": "p2-gap-test",
        "sentences": sentences,
    }
    result = mp._uve_process_scene_media(scene, str(tmp_path))
    stats = result["asset_stats"]

    # The cranes beat was searched with its NEED's queries even
    # though its scenario carried none (the skip-bug fix).
    assert searched == [["harbour cranes loading ship"]]
    # The quiet beat's sentence is named as never searched.
    unsearched = stats.get("uncovered_unsearched") or []
    assert any(
        entry["sentence_number"] == 1
        and entry["reason"] == "no_scenarios"
        for entry in unsearched
    )
    # Its missing entry carries the discovery state, and the
    # coverage assessment classifies it STARVED, not
    # UNRESOLVED_SEARCHED.
    missing = stats.get("missing_visuals") or []
    quiet_missing = [
        e for e in missing if e.get("sentence_number") == 1
    ]
    assert quiet_missing
    assert quiet_missing[0]["discovery_state"] == "no_scenarios"
    assessment = asset_coverage.assess_asset_coverage([result])
    assert assessment["per_sentence"][1] == asset_coverage.STARVED


# ---------------------------------------------------------------------------
# Saturation record + coverage-evidence precedence (units)
# ---------------------------------------------------------------------------

def test_saturation_metrics_accumulate():
    run_metrics.begin_run("p2-saturation-test")
    run_metrics.sentence_pass(
        [7], evaluations=10,
        verified_candidate_keys={"cand-a"},
        query_signatures={"berlin wall"},
    )
    run_metrics.sentence_pass(
        [7], evaluations=6,
        verified_candidate_keys={"cand-a"},  # not new yield
        query_signatures={"berlin wall", "checkpoint"},
    )
    entry = run_metrics.snapshot()["sentence_saturation"][0]
    assert entry["sentence_number"] == 7
    assert entry["evaluations"] == 16
    assert entry["new_verified"] == 1
    assert entry["evaluations_since_new_verified"] == 6
    assert entry["distinct_queries"] == 2
    assert entry["saturated"] is False
    run_metrics.sentence_recovery_exhausted([7])
    entry = run_metrics.snapshot()["sentence_saturation"][0]
    assert entry["saturated"] is True


def test_coverage_evidence_discovery_state_precedence():
    evidence = asset_coverage._entry_search_evidence
    # The Phase 2 conflation hole: never-searched, legacy reason.
    assert evidence({
        "reason": "no_verified_asset", "discovery_state": "no_queries",
    }) == (False, True)
    assert evidence({
        "reason": "no_verified_asset", "discovery_state": "searched",
    }) == (True, False)
    # Legacy payloads (no state recorded) keep their old reading.
    assert evidence({"reason": "no_verified_asset"}) == (True, False)
    assert evidence({"reason": "unsearched_role"}) == (False, True)
