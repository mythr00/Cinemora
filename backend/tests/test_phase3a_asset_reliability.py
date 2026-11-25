"""Master Directive Phase 3a — Asset Manager core reliability.

C-01: coverage-aware stage adoption + budget fairness.
C-02: download-failure taxonomy.
C-12: bounded download timeout / retry waste.

All hermetic: downloads and HTTP are stubbed exactly as the
existing pipeline tests stub them; nothing touches the network
or a real provider, and no production is run.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import jobs
from database import Base, SessionLocal, engine
from services import asset_coverage, download_taxonomy, stage_resume
from services import tier_ladder
from services import media_pipeline as mp
from services.visual_registry import VisualRegistry

from test_phase5_stage_resume import (
    _record,
    _write_source_run,
    pipeline,
    _latest_state,
    _run,
)


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(bind=engine)
    yield


# ---------------------------------------------------------------------------
# C-01 module level: coverage assessment + adoption gate
# ---------------------------------------------------------------------------

STARVED_SCENES = [{
    "scene_number": 7,
    "videos": [], "images": [],
    "sentences": [
        {"sentence_number": 22, "videos": [{"path": "x.mp4"}], "images": []},
        {"sentence_number": 23, "videos": [], "images": []},
    ],
    "beats": [{"beat_id": "b7", "sentence_numbers": [22, 23]}],
    "asset_stats": {
        "missing_visuals": [{
            "sentence_number": 23,
            "reason": "unsearched_role",
            "tier_attempts": [{
                "tier": "activity_broll",
                "queries": [],
                "outcome": "budget_exhausted",
                "stop_reason": "production_download_budget",
            }],
        }],
    },
}]

COMPLETE_SCENES = [{
    "scene_number": 1,
    "videos": [], "images": [],
    "sentences": [
        {"sentence_number": 1, "videos": [{"path": "x.mp4"}], "images": []},
        {"sentence_number": 2, "videos": [], "images": []},
        {"sentence_number": 3, "videos": [], "images": []},
    ],
    "beats": [{"beat_id": "b1", "sentence_numbers": [1, 2, 3]}],
    "asset_stats": {
        "missing_visuals": [
            # Searched (primary + recovery) and honestly unresolved.
            {"sentence_number": 2, "reason": "no_verified_asset",
             "recovery_rounds": 2},
            # Graphics fallback decision recorded.
            {"sentence_number": 3, "reason": "no_verified_asset",
             "coverage_outcome": "covered_by_graphic"},
        ],
    },
}]


def test_coverage_assessment_classifies_starved_and_complete():
    assessment = asset_coverage.assess_asset_coverage(STARVED_SCENES)
    assert assessment["complete"] is False
    assert assessment["per_sentence"][22] == "covered"
    assert assessment["per_sentence"][23] == "starved"
    assert asset_coverage.coverage_incomplete_sentences(STARVED_SCENES) == [23]

    assessment = asset_coverage.assess_asset_coverage(COMPLETE_SCENES)
    assert assessment["complete"] is True
    assert assessment["per_sentence"] == {
        1: "covered", 2: "unresolved_searched", 3: "graphics_fallback",
    }


def test_coverage_assessment_unrecorded_sentence_is_incomplete():
    scenes = [{
        "scene_number": 1, "videos": [], "images": [],
        "sentences": [{"sentence_number": 5, "videos": [], "images": []}],
        "beats": [{"beat_id": "b", "sentence_numbers": [5]}],
        "asset_stats": {},
    }]
    assessment = asset_coverage.assess_asset_coverage(scenes)
    assert assessment["complete"] is False
    assert assessment["per_sentence"][5] == "unrecorded"


def _source_run_with_asset_payload(tmp_path, processed_scenes):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    run_dir, state = _write_source_run(
        project_dir, "run-src", "hash-1", {"captions": "off"},
        ["voice_actor", "script_analysis", "asset_manager"],
    )
    _record(project_dir, run_dir, state, "voice_actor",
            {"narration.wav": b"audio", "captions.json": b"{}"},
            {"state": {"voice": {"model": "stub"}}})
    _record(project_dir, run_dir, state, "script_analysis",
            {"case_bible.json": b"{}"},
            {"production_data": {"analysis": {}, "sentences": [], "scenes": []}})
    clip = project_dir / "scene_1" / "clip.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"clip-bytes")
    _record(project_dir, run_dir, state, "asset_manager", {},
            {"processed_scenes": processed_scenes, "registry": {}})
    return project_dir


def test_adoption_refused_when_sentence_starved(tmp_path):
    project_dir = _source_run_with_asset_payload(tmp_path, STARVED_SCENES)
    plan = stage_resume.plan_resume(
        project_dir, "run-new", "hash-1", {"captions": "off"}
    )
    assert plan["source_run_id"] == "run-src"
    # Earlier stages still adopt; the asset stage (and therefore
    # everything after it) does not.
    assert sorted(plan["stages"]) == ["script_analysis", "voice_actor"]
    assert "coverage_incomplete" in (plan["fallback"] or "")
    assert "23" in (plan["fallback"] or "")


def test_adoption_succeeds_when_coverage_complete(tmp_path):
    project_dir = _source_run_with_asset_payload(tmp_path, COMPLETE_SCENES)
    plan = stage_resume.plan_resume(
        project_dir, "run-new", "hash-1", {"captions": "off"}
    )
    assert sorted(plan["stages"]) == [
        "asset_manager", "script_analysis", "voice_actor",
    ]


# ---------------------------------------------------------------------------
# C-01 pipeline level: the retry re-searches the missing sentence
# ---------------------------------------------------------------------------

def _coverage_stub(workers, starved):
    """A process_scene_media stub whose recorded coverage is either
    starved (sentence 2 budget-starved, never searched) or complete."""
    def process_scene_media(scene, project_dir):
        workers.calls.append("process_scene_media")
        scene_dir = Path(project_dir) / "scene_1"
        scene_dir.mkdir(parents=True, exist_ok=True)
        clip = scene_dir / "clip.mp4"
        clip.write_bytes(b"clip-bytes")
        if starved["value"]:
            missing = [{
                "sentence_number": 2, "reason": "unsearched_role",
                "tier_attempts": [{
                    "tier": "activity_broll", "queries": [],
                    "outcome": "budget_exhausted",
                    "stop_reason": "production_download_budget",
                }],
            }]
            sentences = [
                {"sentence_number": 1,
                 "videos": [{"path": str(clip)}], "images": []},
                {"sentence_number": 2, "videos": [], "images": []},
            ]
        else:
            missing = [{
                "sentence_number": 2, "reason": "no_verified_asset",
                "recovery_rounds": 2,
            }]
            sentences = [
                {"sentence_number": 1,
                 "videos": [{"path": str(clip)}], "images": []},
                {"sentence_number": 2, "videos": [], "images": []},
            ]
        return {
            "scene_number": 1,
            "images": [],
            "videos": [{"path": str(clip), "title": "stub clip"}],
            "sentences": sentences,
            "beats": [{"beat_id": "b1", "sentence_numbers": [1, 2]}],
            "asset_stats": {"missing_visuals": missing},
        }
    return process_scene_media


def test_retry_researches_starved_sentence(pipeline, monkeypatch):
    project_id, project_dir, workers = pipeline
    starved = {"value": True}
    monkeypatch.setattr(
        jobs, "process_scene_media", _coverage_stub(workers, starved)
    )
    workers.fail_at = "video_editor"
    with pytest.raises(RuntimeError, match="injected kill"):
        _run(project_id)

    workers.fail_at = None
    workers.calls.clear()
    result = _run(project_id)
    assert result["status"] == "completed"
    # The defective asset stage was NOT adopted: its worker ran
    # again (the missing sentence is re-searched on the retry),
    # while the healthy earlier stages were adopted.
    assert workers.count("process_scene_media") == 1
    assert workers.count("generate_narration") == 0
    state = _latest_state(project_dir)
    assert state["stages"]["asset_manager"].get("adopted") is not True
    assert "asset_manager" in state["resume"]["executed_stages"]
    assert "coverage_incomplete" in (state["resume"]["fallback"] or "")


def test_retry_adopts_complete_asset_stage(pipeline, monkeypatch):
    project_id, project_dir, workers = pipeline
    starved = {"value": False}
    monkeypatch.setattr(
        jobs, "process_scene_media", _coverage_stub(workers, starved)
    )
    workers.fail_at = "video_editor"
    with pytest.raises(RuntimeError, match="injected kill"):
        _run(project_id)

    workers.fail_at = None
    workers.calls.clear()
    result = _run(project_id)
    assert result["status"] == "completed"
    assert workers.count("process_scene_media") == 0
    state = _latest_state(project_dir)
    assert state["stages"]["asset_manager"]["adopted"] is True


# ---------------------------------------------------------------------------
# C-01 fairness: early failures cannot consume later allocations
# ---------------------------------------------------------------------------

def test_search_fairness_reserve_protects_late_need():
    ledger = tier_ladder.EscalationLedger()
    ledger.register_need("early")
    ledger.register_need("late")
    ledger.spend_searches(1, need_key="early")
    # Drive the account near the cap with early deepening spends.
    ledger.searches = 30
    # Deepening for the early need is limited by the reserve held
    # for the late need (1 unserved + lookahead).
    assert ledger.allocate_searches(5, need_key="early") == 1
    # The late need's protected first search still executes.
    assert ledger.allocate_searches(5, need_key="late") >= 1


def test_download_fairness_reserve_protects_late_need():
    ledger = tier_ladder.EscalationLedger()
    ledger.register_need("early")
    ledger.register_need("late")
    ledger.spend_download(need_key="early")  # early breadth
    for _ in range(13):
        ledger.spend_download()
    assert ledger.downloads == 14
    # Early deepening may spend while the reserve (late's slot +
    # lookahead) survives the spend...
    assert ledger.can_spend_download(need_key="early")
    ledger.spend_download(need_key="early")
    # ...but not past it: 15 + 1 + reserve(1 + 4) > 20.
    assert not ledger.can_spend_download(need_key="early")
    # The late need's first (breadth) download is still available.
    assert ledger.can_spend_download(need_key="late")
    assert ledger.fairness_reserve_downloads() == (
        1 + tier_ladder.FAIRNESS_LOOKAHEAD_DOWNLOADS
    )
    # Caps unchanged (D-B2 standing rule).
    assert tier_ladder.PRODUCTION_MAX_EXTRA_SEARCHES == 40
    assert tier_ladder.PRODUCTION_MAX_EXTRA_DOWNLOADS == 20


# ---------------------------------------------------------------------------
# C-02 taxonomy: classes recorded + aggregated distinctly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason,kwargs,expected", [
    ("download_exception: Connection reset by peer", {}, "network_error"),
    ("http_error", {"http_status": 503}, "http_error"),
    ("download_failed", {"detail": "HTTP Error 404: Not Found"}, "http_error"),
    ("timeout", {}, "timeout"),
    ("download_failed", {"detail": "yt-dlp timed out"}, "timeout"),
    ("rights_rejected", {}, "rights_rejected"),
    ("license_rejected", {}, "rights_rejected"),
    ("technical_below_minimum", {}, "verification_failed"),
    ("probe_failed", {}, "verification_failed"),
    ("previously_failed_this_run", {}, "duplicate"),
    ("speculative_download_budget", {}, "budget_exhausted"),
    ("production_download_budget", {}, "budget_exhausted"),
    ("no_segment_evidence", {}, "source_unavailable"),
    ("something_unheard_of", {}, "unknown"),
])
def test_taxonomy_classes(reason, kwargs, expected):
    record = download_taxonomy.classify_download_failure(reason, **kwargs)
    assert record["failure_class"] == expected
    assert record["failure_class"] in download_taxonomy.FAILURE_CLASSES


def test_taxonomy_http_status_and_check_ride_along():
    record = download_taxonomy.classify_download_failure(
        "download_failed", detail="HTTP Error 404: Not Found"
    )
    assert record["http_status"] == 404
    assert record["retryable"] is False
    record = download_taxonomy.classify_download_failure(
        "technical_below_minimum"
    )
    assert record["check"] == "technical_below_minimum"


def test_taxonomy_aggregation_is_distinct_and_complete():
    failures = [
        {"provider": "youtube", "reason": "download_failed",
         "detail": "HTTP Error 404: Not Found"},
        {"provider": "youtube", "reason": "timeout"},
        {"provider": "wikimedia", "reason": "technical_below_minimum"},
        {"provider": "wikimedia", "reason": "previously_failed_this_run"},
        {"provider": "youtube", "reason": "speculative_download_budget"},
        {"provider": "archive", "reason": "no_segment_evidence"},
    ]
    aggregate = download_taxonomy.aggregate_failure_classes(failures)
    assert aggregate["total"] == 6
    assert aggregate["by_class"] == {
        "http_error": 1, "timeout": 1, "verification_failed": 1,
        "duplicate": 1, "budget_exhausted": 1, "source_unavailable": 1,
    }
    assert aggregate["by_class_status"]["http_error:404"] == 1


def test_fetch_first_records_taxonomy_on_failures(tmp_path):
    mp.reset_media_caches()
    registry = VisualRegistry(project_id="phase3a-taxonomy")
    candidate = {
        "candidate_id": "cand-1", "provider": "youtube",
        "url": "https://youtu.be/abc123", "title": "clip",
    }

    def doomed(cand, scene_dir, sentence_number, asset_index):
        cand["_download_error"] = "download_failed"
        mp._stamp_download_failure(cand, "http_error", http_status=404)
        return None

    failures = []
    asset = mp._uve_fetch_first(
        [candidate], doomed, str(tmp_path), 23, 1, registry, set(),
        failures=failures,
    )
    assert asset is None
    assert failures[0]["failure_class"] == "http_error"
    assert failures[0]["http_status"] == 404
    assert failures[0]["sentence_number"] == 23
    assert failures[0]["candidate_id"] == "cand-1"

    # Second fetch of the same candidate: never re-downloaded —
    # recorded as a duplicate-class failure.
    failures2 = []
    asset = mp._uve_fetch_first(
        [dict(candidate)], doomed, str(tmp_path), 23, 1, registry,
        set(), failures=failures2,
    )
    assert asset is None
    assert failures2[0]["reason"] == "previously_failed_this_run"
    assert failures2[0]["failure_class"] == "duplicate"


def test_asset_report_aggregates_failures_by_class():
    from services.asset_report import build_asset_report

    scenes = [{
        "scene_number": 1, "videos": [], "images": [],
        "asset_stats": {
            "download_failures": [
                {"provider": "youtube", "reason": "download_failed",
                 "detail": "HTTP Error 404: Not Found"},
                {"provider": "youtube", "reason": "timeout"},
                {"provider": "wikimedia",
                 "reason": "technical_below_minimum"},
                {"provider": "youtube",
                 "reason": "previously_failed_this_run"},
            ],
        },
    }]
    report = build_asset_report(scenes)
    taxonomy = report["download_failure_taxonomy"]
    assert taxonomy["total"] == 4
    assert taxonomy["by_class"] == {
        "http_error": 1, "timeout": 1,
        "verification_failed": 1, "duplicate": 1,
    }


# ---------------------------------------------------------------------------
# C-12: retry policy + bounded per-candidate worst case
# ---------------------------------------------------------------------------

def test_retry_policy_doomed_never_transient_once():
    assert download_taxonomy.should_retry(
        download_taxonomy.classify_download_failure(
            "download_failed", detail="HTTP Error 404: Not Found"
        )
    ) is False
    assert download_taxonomy.should_retry(
        download_taxonomy.classify_download_failure("rights_rejected")
    ) is False
    transient = download_taxonomy.classify_download_failure("timeout")
    assert download_taxonomy.should_retry(transient, retries_done=0) is True
    assert download_taxonomy.should_retry(transient, retries_done=1) is False
    assert download_taxonomy.MAX_TRANSIENT_RETRIES == 1


def test_per_candidate_worst_case_is_bounded_by_config():
    # YouTube path: yt-dlp ceiling + normalisation ffmpeg.
    youtube_worst = mp.YT_DLP_TIMEOUT + mp.DOWNLOAD_FFMPEG_TIMEOUT
    # Direct path: range-seek ffmpeg + streamed read + trim ffmpeg.
    direct_worst = (
        mp.DOWNLOAD_FFMPEG_TIMEOUT
        + mp.DIRECT_HTTP_TIMEOUT[1]
        + mp.DOWNLOAD_FFMPEG_TIMEOUT
    )
    # The pre-fix worst cases were 270s / 240s (~5 min per doomed
    # candidate); the configured bounds must sit well below that.
    assert youtube_worst <= 120
    assert direct_worst <= 120
    assert mp.YT_DLP_TIMEOUT == 60
    assert mp.DOWNLOAD_FFMPEG_TIMEOUT == 45


def test_ytdlp_command_uses_bounded_retries_and_timeout(monkeypatch):
    captured = {}

    def fake_run(command, timeout, label):
        captured["command"] = command
        captured["timeout"] = timeout
        return 1, "", "HTTP Error 404: Not Found"

    monkeypatch.setattr(mp, "run_command_safe", fake_run)
    ok = mp.download_video_clip(
        "https://youtu.be/abc123", Path("/tmp/phase3a/clip.mp4"),
        start_time=0, duration=8,
    )
    assert ok is False
    assert captured["timeout"] == mp.YT_DLP_TIMEOUT
    command = captured["command"]
    assert command[command.index("--retries") + 1] == str(mp.YTDLP_RETRIES)
    assert command[command.index("--socket-timeout") + 1] == str(
        mp.YTDLP_SOCKET_TIMEOUT
    )
    # The 404 is classified, not conflated.
    last = mp.DOWNLOAD_LAST_ERROR["/tmp/phase3a/clip.mp4"]
    assert last["failure_class"] == "http_error"
    assert last["http_status"] == 404


def _http_error(status):
    error = requests.exceptions.HTTPError(f"{status} Client Error")
    error.response = SimpleNamespace(status_code=status)
    return error


def test_direct_video_doomed_candidate_not_retried(monkeypatch, tmp_path):
    calls = {"get": 0}

    class DeadSession:
        def get(self, url, timeout=None, stream=False):
            calls["get"] += 1
            raise _http_error(404)

    monkeypatch.setattr(mp, "_http", lambda: DeadSession())
    monkeypatch.setattr(
        mp, "run_command_safe", lambda *a, **k: (1, "", "no range")
    )
    ok = mp.download_direct_video(
        "https://example.com/clip.mp4", tmp_path / "out.mp4", 8, 0
    )
    assert ok is False
    assert calls["get"] == 1  # 404: deterministic — never retried
    last = mp.DOWNLOAD_LAST_ERROR[str(tmp_path / "out.mp4")]
    assert last["failure_class"] == "http_error"
    assert last["http_status"] == 404


def test_direct_video_transient_retried_at_most_once(monkeypatch, tmp_path):
    calls = {"get": 0}

    class FlakySession:
        def get(self, url, timeout=None, stream=False):
            calls["get"] += 1
            raise requests.exceptions.ConnectionError("reset by peer")

    monkeypatch.setattr(mp, "_http", lambda: FlakySession())
    monkeypatch.setattr(
        mp, "run_command_safe", lambda *a, **k: (1, "", "no range")
    )
    ok = mp.download_direct_video(
        "https://example.com/clip.mp4", tmp_path / "out.mp4", 8, 0
    )
    assert ok is False
    assert calls["get"] == 2  # initial attempt + exactly one retry
    last = mp.DOWNLOAD_LAST_ERROR[str(tmp_path / "out.mp4")]
    assert last["failure_class"] == "network_error"


def test_image_download_retry_bounds(monkeypatch, tmp_path):
    attempts = {"n": 0}

    def dead(url, output_path):
        attempts["n"] += 1
        raise _http_error(410)

    monkeypatch.setattr(mp, "download_image", dead)
    ok, reason = mp._download_image_detailed(
        "https://example.com/gone.jpg", tmp_path / "i.jpg"
    )
    assert ok is False
    assert reason == "http_error"
    assert attempts["n"] == 1  # 410 Gone: never retried

    attempts["n"] = 0

    def flaky(url, output_path):
        attempts["n"] += 1
        raise requests.exceptions.ConnectionError("reset")

    monkeypatch.setattr(mp, "download_image", flaky)
    ok, reason = mp._download_image_detailed(
        "https://example.com/flaky.jpg", tmp_path / "i2.jpg"
    )
    assert ok is False
    assert attempts["n"] == 2  # one transient retry, no more
