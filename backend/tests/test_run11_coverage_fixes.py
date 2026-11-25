"""Run-11 coverage fixes (2026-11-22).

Run 11 (15915eec, Petito, project 31df4571) FAILED at the
editor's coverage gate — 20/23 sentences, with scene 3's
sentences 9, 10 and 12 uncovered. The run's own records showed
five distinct machinery defects, one per fix pinned here:

1. URL HYGIENE. Serper video results arrived as
   google.com/goto?url=<opaque token> redirect wrappers and were
   handed verbatim to yt-dlp / segment evidence: 69 of the run's
   download failures were segment_evidence/no_segment_evidence
   deaths on wrapper URLs, and google.com's download success was
   5 of 89 attempts. Wrappers are now resolved to their real
   destination at verification, before identification, gating,
   scoring or fetching; an unresolvable wrapper is rejected at
   the discovery stage with the typed unresolvable_redirector
   reason and never burns a fetch attempt.

2. DOWNLOAD RESILIENCE + PROVIDER HEALTH. Image downloads died
   on transient transport failures ("Response ended
   prematurely") with no retry, and YouTube 429s were retried
   into the ground (0 of 12 YouTube downloads succeeded).
   Transient failures now get ONE bounded retry; every download
   outcome and every observed 429 is recorded per provider
   scope, and a scope with repeated consecutive 429s is backed
   off for the rest of the production — refused before any
   request, with the typed provider_backed_off reason — while
   healthy scopes keep serving.

3. VERIFIED-POOL CONVERSION (sentence 9's break, precisely).
   Sentence 9's escalation verified 17 candidates across its
   pools and accepted ZERO. The conversion break was in the
   coverage draw: escalation items that hit a budget gate (the
   per-tier attempt cap, or the ledger's download gate — which
   included the wall-clock check) were skipped with a bare
   ``continue``: never fetched, never recorded, and
   indistinguishable in the record from a genuinely exhausted
   pool. The first draws were burned on the dead goto-wrapper
   videos at the top of the pool; everything behind them was
   silently gated away. Budget-gate skips are now RECORDED on
   the coverage record (budget_blocked, with the typed stop
   reason per candidate), the draw counts its own attempts
   against a sane per-beat cap (COVERAGE_DRAW_MAX_ATTEMPTS,
   recorded when it binds), and a batch whose tier already
   verified candidates no longer has its outcome rewritten to
   budget_exhausted (fix 4).

4. WALL-CLOCK SCOPE (sentence 12's break). Sentence 12's
   "phone 2021" batch verified 5 candidates; the NEXT batch's
   search allocation hit the wall-clock guard and the attempt
   was recorded budget_exhausted/wall_clock DESPITE the verified
   pool — and every subsequent draw from that pool was refused
   by the same wall-clock check inside can_spend_download, so
   the beat drew zero of its 17 verified candidates. The
   wall-clock guard now gates NEW searches only: drawing from
   an already-verified pool goes through
   EscalationLedger.can_draw_verified_download (production cap
   and fairness still bind; the wall clock does not).

5. T5 FLOOR COMPLETION (sentences 9, 10, 12 in graphics).
   Sentences 9 and 10 entered the graphics chain as
   Director-directed manifest entries, every directed template
   refused (no_schematic_motif), and the run-10 closing-card
   floor — gated to undirected targets only — left them
   skipped. Sentence 12 was not in the Director's manifest at
   all, so build_motion_graphics_assets discarded the caller's
   target for it and it never entered the chain
   (graphics_outcome null). Now: (a) caller targets for
   uncovered beats the manifest does not cover are appended as
   undirected targets; (b) the floor fires for ANY target that
   reaches the end of the chain unserved, directed or not —
   the direction is a preference order, not a veto — with the
   directed refusal recorded superseded by the guarantee.
"""

import base64
import json
from pathlib import Path

import pytest
import requests

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import graphics_pipeline
from services import media_pipeline as mp
from services import provider_health
from services import story_understanding as story
from services import tier_ladder
from services import url_hygiene
from services.visual_registry import VisualRegistry


@pytest.fixture(autouse=True)
def _clean_provider_state():
    # reset_media_caches is the production per-run reset: it
    # clears the failed-download set, the segment caches, the
    # redirect-resolution cache and provider health, so no test
    # here inherits (or leaks) another test's run state.
    #
    # Phase 9 triage: since Phase 8.4 the provider-health backoff
    # is SHARED Redis state, and provider_health.reset()
    # deliberately does NOT clear it (it is a platform resource
    # with its own time bounds). This file's simulated 429s were
    # therefore written to the suite's real Redis (DB 15), tripping
    # youtube.com / media.example.org platform-wide for
    # BACKOFF_SECONDS and poisoning later tests in this file, the
    # transcript tests, and any subsequent suite run inside the
    # TTL. The Phase 8 test seam is the isolation (the pattern
    # test_metadata_absence_bypass.py already uses): a fakeredis
    # client for the duration of each test here.
    import fakeredis

    provider_health.set_redis_client(
        fakeredis.FakeRedis(decode_responses=True)
    )
    mp.reset_media_caches()
    yield
    mp.reset_media_caches()
    provider_health.reset_redis_client()


# ---------------------------------------------------------------------------
# Fixtures (modelled on tests/test_phase2_ladder.py)
# ---------------------------------------------------------------------------

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


def _good(marker="phones-evidence"):
    return {
        "url": f"https://media.example.org/{marker}",
        "title": "Investigators examining phones and travel records",
        "snippet": "phones travel records evidence examination",
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


def _berlin_candidate(index):
    return {
        "url": f"https://media.example.org/berlin-{index}",
        "title": f"Berlin Wall crowds {index}",
        "snippet": "berlin wall crowds",
        "provider": "youtube",
    }


_BERLIN_SCENARIO = {
    "sentence_number": 1, "specificity": "representative",
    "subject": "Berlin Wall", "search_terms": ["berlin", "wall"],
    "visual": "berlin wall crowds",
}


# ---------------------------------------------------------------------------
# Fix 1 — URL hygiene
# ---------------------------------------------------------------------------

def test_redirector_wrappers_decode_locally(monkeypatch):
    """Wrappers that CARRY their destination are decoded without
    any HTTP: google /url?q=, DuckDuckGo /l/?uddg=, Facebook
    l.php?u=, Bing /ck/a?u=a1<base64url>."""
    def _no_http(url, timeout=6.0):
        raise AssertionError("local decode must not touch HTTP")

    monkeypatch.setattr(url_hygiene, "_http_follow", _no_http)
    assert url_hygiene.is_redirector_url(
        "https://www.google.com/goto?url=abc123"
    )
    assert url_hygiene.is_redirector_url(
        "https://l.facebook.com/l.php?u=https%3A%2F%2Fx.org"
    )
    assert not url_hygiene.is_redirector_url(
        "https://media.example.org/berlin-0"
    )
    assert not url_hygiene.is_redirector_url(
        "https://www.google.com/search?q=berlin+wall"
    )
    resolved, status = url_hygiene.resolve_candidate_url(
        "https://www.google.com/url?q="
        "https%3A%2F%2Fexample.org%2Fpage&sa=U&ved=0"
    )
    assert (resolved, status) == ("https://example.org/page", "decoded")
    resolved, status = url_hygiene.resolve_candidate_url(
        "https://duckduckgo.com/l/?uddg="
        "https%3A%2F%2Fexample.org%2Fduck"
    )
    assert (resolved, status) == ("https://example.org/duck", "decoded")
    token = base64.urlsafe_b64encode(
        b"https://example.org/bing"
    ).decode().rstrip("=")
    resolved, status = url_hygiene.resolve_candidate_url(
        f"https://www.bing.com/ck/a?u=a1{token}"
    )
    assert (resolved, status) == ("https://example.org/bing", "decoded")
    # A non-wrapper passes through untouched.
    resolved, status = url_hygiene.resolve_candidate_url(
        "https://media.example.org/berlin-0"
    )
    assert (resolved, status) == (
        "https://media.example.org/berlin-0", "not_redirector"
    )


def test_opaque_goto_token_resolves_via_http_follow(monkeypatch):
    """Run 11's actual wrapper shape: the google.com/goto token
    is opaque (it base64-decodes to binary, no URL inside), so
    resolution follows the redirect over HTTP — stubbed here."""
    followed = []

    def _follow(url, timeout=6.0):
        followed.append(url)
        return "https://media.example.org/berlin-real"

    monkeypatch.setattr(url_hygiene, "_http_follow", _follow)
    wrapper = "https://www.google.com/goto?url=CgtaZWRpYS1vcGFxdWU"
    resolved, status = url_hygiene.resolve_candidate_url(wrapper)
    assert (resolved, status) == (
        "https://media.example.org/berlin-real", "resolved"
    )
    assert followed == [wrapper]
    # The resolution is cached: a second lookup does not re-follow.
    resolved2, _ = url_hygiene.resolve_candidate_url(wrapper)
    assert resolved2 == resolved
    assert followed == [wrapper]


def test_redirect_wrapped_candidate_resolved_before_fetch(
    monkeypatch, tmp_path
):
    """The run-11 video shape end to end: the ONLY candidate is a
    goto-wrapped video. It is resolved before verification, the
    downloader is attempted with the REAL destination, and the
    wrapper itself never reaches the fetcher."""
    monkeypatch.setattr(
        url_hygiene, "_http_follow",
        lambda url, timeout=6.0: "https://media.example.org/berlin-real",
    )
    wrapper = "https://www.google.com/goto?url=CgtaZWRpYS1vcGFxdWU"
    candidate = {
        "url": wrapper,
        "title": "Berlin Wall crowds",
        "snippet": "berlin wall crowds",
        "provider": "youtube",
    }
    downloader, attempts = _downloader(succeed_markers=("berlin-real",))
    asset, scene_stats, _v, _i, _calls = _run_pass(
        monkeypatch, tmp_path,
        search=lambda scenario, need: ([candidate], [], []),
        beat={"beat_id": "b", "sentence_numbers": [1],
              "text": "The Berlin Wall fell."},
        shot_need=None,
        scenario=dict(_BERLIN_SCENARIO),
        downloader=downloader,
    )
    assert asset is not None
    assert attempts == ["https://media.example.org/berlin-real"]
    assert asset["url"] == "https://media.example.org/berlin-real"
    assert asset["url_resolved_from"] == wrapper
    assert "unresolvable_redirector" not in (
        scene_stats.get("rejection_reasons") or {}
    )


def test_unresolvable_wrapper_rejected_at_discovery(
    monkeypatch, tmp_path
):
    """A wrapper whose destination cannot be recovered is
    rejected at the discovery stage with the typed reason — no
    gate call, no scoring, and above all no download attempt is
    burned on it (run 11 burned 69 segment-evidence attempts
    this way)."""
    monkeypatch.setattr(
        url_hygiene, "_http_follow", lambda url, timeout=6.0: None
    )
    candidate = {
        "url": "https://www.google.com/goto?url=CgtaZWRpYS1vcGFxdWU",
        "title": "Berlin Wall crowds",
        "snippet": "berlin wall crowds",
        "provider": "youtube",
    }
    downloader, attempts = _downloader(succeed_markers=("berlin",))
    asset, scene_stats, _v, _i, _calls = _run_pass(
        monkeypatch, tmp_path,
        search=lambda scenario, need: ([candidate], [], []),
        beat={"beat_id": "b", "sentence_numbers": [1],
              "text": "The Berlin Wall fell."},
        shot_need=None,
        scenario=dict(_BERLIN_SCENARIO),
        downloader=downloader,
    )
    assert asset is None
    assert attempts == []
    records = [
        r for r in scene_stats.get("candidate_records") or []
        if r.get("primary_rejection_reason")
        == "unresolvable_redirector"
        or r.get("reason") == "unresolvable_redirector"
    ]
    assert len(records) == 1
    assert records[0]["stage"] == "discovery"
    assert records[0]["outcome"] == "rejected"
    assert (
        scene_stats["rejection_reasons"]["unresolvable_redirector"] == 1
    )


# ---------------------------------------------------------------------------
# Fix 2 — download resilience + provider health
# ---------------------------------------------------------------------------

def test_provider_health_trips_after_repeated_429s():
    scope = "youtube.com"
    for _ in range(provider_health.RATE_LIMIT_TRIP_COUNT - 1):
        provider_health.record_rate_limit(scope)
    assert not provider_health.is_backed_off(scope)
    provider_health.record_rate_limit(scope)
    assert provider_health.is_backed_off(scope)
    snap = provider_health.snapshot()[scope]
    assert snap["rate_limited"] == provider_health.RATE_LIMIT_TRIP_COUNT
    assert snap["backed_off"] is True
    # A success resets the consecutive-429 run on a healthy scope.
    other = "commons.wikimedia.org"
    provider_health.record_rate_limit(other)
    provider_health.record_rate_limit(other)
    provider_health.record_download_outcome(other, True)
    provider_health.record_rate_limit(other)
    provider_health.record_rate_limit(other)
    assert not provider_health.is_backed_off(other)
    # Scope normalisation: www. and bare host are the same scope.
    assert provider_health.scope_for_url(
        "https://www.YouTube.com/watch?v=1"
    ) == "youtube.com"


def test_backed_off_provider_is_refused_before_fetch(
    monkeypatch, tmp_path
):
    """A candidate on a backed-off scope is refused BEFORE any
    request — the downloader is never invoked for it, the refusal
    is recorded with the typed provider_backed_off reason, and
    the URL is not blacklisted as a failed download (the provider
    may serve a later production)."""
    for _ in range(provider_health.RATE_LIMIT_TRIP_COUNT):
        provider_health.record_rate_limit("media.example.org")
    assert provider_health.is_backed_off("media.example.org")

    blocked = _berlin_candidate(0)
    healthy = {
        "url": "https://upload.wikimedia.org/berlin-1",
        "title": "Berlin Wall crowds 1",
        "snippet": "berlin wall crowds",
        "provider": "wikimedia",
    }
    # The healthy candidate's download FAILS, so the fetch walk
    # reaches the backed-off candidate whatever the pick order:
    # the refusal must be recorded, and no request may be made
    # for the backed-off URL.
    downloader, attempts = _downloader(fail_all=True)
    asset, scene_stats, _v, _i, _calls = _run_pass(
        monkeypatch, tmp_path,
        search=lambda scenario, need: ([blocked, healthy], [], []),
        beat={"beat_id": "b", "sentence_numbers": [1],
              "text": "The Berlin Wall fell."},
        shot_need=None,
        scenario=dict(_BERLIN_SCENARIO),
        downloader=downloader,
    )
    assert asset is None
    # The ONLY real download attempt was the healthy scope's.
    assert attempts == ["https://upload.wikimedia.org/berlin-1"]
    failures = [
        f for f in scene_stats.get("download_failures") or []
        if f.get("reason") == "provider_backed_off"
    ]
    assert len(failures) == 1
    assert failures[0]["url"] == "https://media.example.org/berlin-0"
    assert failures[0]["stage"] == "download"
    # The refusal is not a download failure: the URL is NOT
    # blacklisted for the rest of the run.
    assert "https://media.example.org/berlin-0" not in (
        mp._FAILED_DOWNLOAD_URLS
    )
    # And the event record carries the same truth.
    assert any(
        r.get("outcome") == "download_failed"
        and (
            r.get("primary_rejection_reason") == "provider_backed_off"
            or r.get("reason") == "provider_backed_off"
        )
        for r in scene_stats.get("candidate_records") or []
    )


def test_image_download_retries_transient_failure(monkeypatch, tmp_path):
    """The run-11 image shape: the first attempt dies mid-body
    ("Response ended prematurely" — a ChunkedEncodingError); the
    bounded retry succeeds and the image is fetched and valid."""
    import random

    from PIL import Image

    calls = []

    def flaky_download(url, output_path, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise requests.exceptions.ChunkedEncodingError(
                "Response ended prematurely"
            )
        # Noise pixels: an incompressible, comfortably
        # over-the-1000-byte-floor JPEG.
        rng = random.Random(20261122)
        img = Image.new("RGB", (256, 256))
        img.putdata([
            (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            for _ in range(256 * 256)
        ])
        img.save(output_path, "JPEG", quality=90)

    monkeypatch.setattr(mp, "download_image", flaky_download)
    ok, reason = mp._download_image_detailed(
        "https://upload.wikimedia.org/moab.jpg", tmp_path / "img.jpg"
    )
    assert (ok, reason) == (True, "")
    assert len(calls) == 2


def test_image_download_429_is_not_retried_and_is_recorded(
    monkeypatch, tmp_path
):
    """A 429 is a rate limit, not a transient error: no retry,
    the typed rate_limited reason, and the observation lands in
    provider health for the scope."""
    calls = []

    def limited_download(url, output_path, **kwargs):
        calls.append(url)
        response = requests.Response()
        response.status_code = 429
        raise requests.exceptions.HTTPError(
            "429 Client Error", response=response
        )

    monkeypatch.setattr(mp, "download_image", limited_download)
    ok, reason = mp._download_image_detailed(
        "https://upload.wikimedia.org/moab.jpg", tmp_path / "img.jpg"
    )
    assert (ok, reason) == (False, "rate_limited")
    assert len(calls) == 1
    snap = provider_health.snapshot()
    assert snap["upload.wikimedia.org"]["rate_limited"] == 1


# ---------------------------------------------------------------------------
# Fix 3 — verified-pool conversion
# ---------------------------------------------------------------------------

def test_coverage_draw_keeps_drawing_past_failures(
    monkeypatch, tmp_path
):
    """The primary pick burns its three fetches on dead
    candidates; the verified pool's pick-limited reserves remain.
    The coverage draw walks them — failure, SUCCESS, failure —
    until the pool is genuinely exhausted: a failed fetch never
    ends the draw the way the primary pick's limit ended the
    primary fetch."""
    candidates = [_berlin_candidate(i) for i in range(6)]
    downloader, attempts = _downloader(succeed_markers=("berlin-4",))
    asset, scene_stats, _v, _i, _calls = _run_pass(
        monkeypatch, tmp_path,
        search=lambda scenario, need: (list(candidates), [], []),
        beat={"beat_id": "b", "sentence_numbers": [1, 2, 3, 4],
              "text": "The Berlin Wall fell."},
        shot_need=None,
        scenario=dict(_BERLIN_SCENARIO),
        downloader=downloader,
    )
    assert asset is not None
    assert asset["url"] == "https://media.example.org/berlin-4"
    # Primary attempts (3 picks) then the draw's three reserves,
    # in pool order — the draw reached berlin-4 BEHIND a failure.
    assert attempts == [
        f"https://media.example.org/berlin-{i}"
        for i in (0, 1, 2, 3, 4, 5)
    ]
    record = scene_stats["coverage_passes"][0]
    assert record["accepted"] == 1
    assert record["download_attempts"] == 3
    assert record["budget_blocked"] == []


def test_tier_draw_budget_skips_are_recorded(monkeypatch, tmp_path):
    """Sentence 9's precise break: FOUR verified T2 candidates,
    a downloader that fails everything. The D-B2 per-tier cap
    allows 2 attempts; the other two verified candidates used to
    vanish behind a bare ``continue``. They are now named on the
    coverage record's budget_blocked with the typed stop reason,
    so a budget-stopped draw is never mistaken for an exhausted
    pool."""
    beat, scenario, need = _s9_fixture()
    beat = {**beat, "sentence_numbers": [9, 10, 11, 12, 13]}
    goods = [_good(f"phones-evidence-{i}") for i in range(4)]

    def search(scenario_, need_):
        if (need_ or {}).get("tier") == "related":
            return (list(goods), [], [])
        return ([_junk()], [], [])

    downloader, attempts_log = _downloader(fail_all=True)
    asset, scene_stats, _v, _i, _calls = _run_pass(
        monkeypatch, tmp_path,
        search=search, beat=beat, shot_need=need, scenario=scenario,
        registry=VisualRegistry(), downloader=downloader,
        bible=_bible(),
    )
    assert asset is None
    record = scene_stats["coverage_passes"][0]
    assert record["download_attempts"] == tier_ladder.TIER_DOWNLOAD_ATTEMPTS
    blocked = record["budget_blocked"]
    assert len(blocked) == 2
    assert all(
        b["reason"] == tier_ladder.STOP_TIER_DOWNLOAD_BUDGET
        for b in blocked
    )
    assert all(b["tier"] == "related" for b in blocked)
    blocked_ids = {b["candidate_id"] for b in blocked}
    assert len(blocked_ids) == 2
    assert not blocked_ids.intersection(record["drawn"])


def test_beat_draw_attempt_cap_binds_and_is_recorded(
    monkeypatch, tmp_path
):
    """A pathological pool of dead URLs cannot loop a beat's draw
    forever: after COVERAGE_DRAW_MAX_ATTEMPTS fetches the draw
    stops, and the stop is recorded with its typed reason."""
    candidates = [_berlin_candidate(i) for i in range(24)]
    downloader, attempts = _downloader(fail_all=True)
    asset, scene_stats, _v, _i, _calls = _run_pass(
        monkeypatch, tmp_path,
        search=lambda scenario, need: (list(candidates), [], []),
        beat={"beat_id": "b", "sentence_numbers": [1],
              "text": "The Berlin Wall fell."},
        shot_need=None,
        scenario=dict(_BERLIN_SCENARIO),
        downloader=downloader,
    )
    assert asset is None
    record = scene_stats["coverage_passes"][0]
    assert record["download_attempts"] == mp.COVERAGE_DRAW_MAX_ATTEMPTS
    assert record["download_attempt_cap"] == mp.COVERAGE_DRAW_MAX_ATTEMPTS
    assert any(
        b["reason"] == mp.STOP_BEAT_DOWNLOAD_ATTEMPT_CAP
        for b in record["budget_blocked"]
    )
    # 3 primary pick attempts + the capped draw.
    assert len(attempts) == 3 + mp.COVERAGE_DRAW_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Fix 4 — wall-clock scope
# ---------------------------------------------------------------------------

def test_verified_pool_survives_wall_clock_trip(monkeypatch, tmp_path):
    """Sentence 12's shape: the T2 batch verifies a candidate and
    the wall-clock guard trips immediately afterwards (the stage
    ran long). NEW searches stay gated — but the verified pool
    keeps its draw rights, so the beat still covers from the
    candidate it already paid to verify."""
    beat, scenario, need = _s9_fixture()
    registry = VisualRegistry()
    ledger = tier_ladder.EscalationLedger(
        clock=lambda: 10000.0, stage_started_at=0.0,
    )
    registry._tier_escalation_ledger = ledger

    def search(scenario_, need_):
        if (need_ or {}).get("tier") == "related":
            # The guard trips as this batch completes: the stage
            # has now spent beyond the escalation fraction.
            ledger.escalation_seconds = 3000.0
            return ([_good()], [], [])
        return ([_junk()], [], [])

    downloader, _attempts = _downloader(
        succeed_markers=("phones-evidence",)
    )
    asset, scene_stats, _v, _i, _calls = _run_pass(
        monkeypatch, tmp_path,
        search=search, beat=beat, shot_need=need, scenario=scenario,
        registry=registry, downloader=downloader, bible=_bible(),
    )
    assert ledger.wall_clock_exceeded()
    assert asset is not None
    assert asset["tier"] == "related"
    assert asset["selection_via"] == "tier_escalation"
    record = scene_stats["coverage_passes"][0]
    assert record["covered_after"] is True
    assert ledger.downloads == 1
    # And NEW work remains gated by the same tripped guard.
    assert ledger.allocate_searches(1, need_key="later") == 0
    assert not ledger.can_spend_download(need_key="later")
    assert ledger.can_draw_verified_download(need_key="later")


def test_verified_attempt_outcome_not_rewritten_by_budget_stop(
    monkeypatch, tmp_path
):
    """Sentence 12's record shape: batch 1 of the related tier
    verifies candidates; batch 2's allocation finds the
    production search account spent. The attempt's outcome stays
    the pool's truth (verified) and only stop_reason names the
    budget cause — the old code overwrote the outcome to
    budget_exhausted, erasing the verified pool from the record."""
    beat, scenario, need = _s9_fixture()
    registry = VisualRegistry()
    ledger = tier_ladder.ledger_for(registry)
    ledger.spend_searches(tier_ladder.PRODUCTION_MAX_EXTRA_SEARCHES - 1)

    def search(scenario_, need_):
        if (need_ or {}).get("tier") == "related":
            return ([_good()], [], [])
        return ([_junk()], [], [])

    downloader, _a = _downloader(fail_all=True)
    asset, scene_stats, _v, _i, _calls = _run_pass(
        monkeypatch, tmp_path,
        search=search, beat=beat, shot_need=need, scenario=scenario,
        registry=registry, downloader=downloader, bible=_bible(),
    )
    attempts = scene_stats["coverage_passes"][0]["tier_attempts"]
    related = [a for a in attempts if a["tier"] == "related"]
    assert len(related) == 1
    assert related[0]["verified"] >= 1
    assert related[0]["outcome"] == "verified"
    assert related[0]["stop_reason"] == (
        tier_ladder.STOP_PRODUCTION_SEARCH_BUDGET
    )


def test_wall_clock_gates_searches_not_verified_draws():
    """The ledger contract itself: with the guard tripped,
    can_spend_download (new escalation work) refuses while
    can_draw_verified_download (fetching an already-verified
    pool) still honours the production cap and fairness."""
    ledger = tier_ladder.EscalationLedger(
        clock=lambda: 10000.0, stage_started_at=0.0,
    )
    ledger.escalation_seconds = 3000.0
    assert ledger.wall_clock_exceeded()
    assert not ledger.can_spend_download()
    assert ledger.can_draw_verified_download()
    # The production download cap binds BOTH gates identically.
    ledger.downloads = tier_ladder.PRODUCTION_MAX_EXTRA_DOWNLOADS
    assert not ledger.can_draw_verified_download()
    assert not ledger.can_spend_download()


# ---------------------------------------------------------------------------
# Fix 5 — T5 floor completion
# ---------------------------------------------------------------------------

def _graphics_data():
    return {
        "case_bible": {"title": "Gabby Petito", "timeline": []},
        "scenes": [
            {
                "scene_number": 3,
                "sentences": [
                    {"text": "The message arrived late that night.",
                     "sentence_number": 12},
                    {"text": "They drove on through the desert town.",
                     "sentence_number": 10},
                ],
            },
            {
                "scene_number": 7,
                "sentences": [
                    {"text": "Gabby Petito never got a trial, "
                             "or a verdict.",
                     "sentence_number": 22},
                ],
            },
        ],
        "processed_scenes": [
            {
                "scene": {
                    "scene_number": 3,
                    "sentences": [
                        {"text": "The message arrived late that night.",
                         "sentence_number": 12},
                        {"text": "They drove on through the desert town.",
                         "sentence_number": 10},
                    ],
                },
                "videos": [],
                "images": [],
            },
            {
                "scene": {
                    "scene_number": 7,
                    "sentences": [
                        {"text": "Gabby Petito never got a trial, "
                                 "or a verdict.",
                         "sentence_number": 22},
                    ],
                },
                "videos": [],
                "images": [],
            },
        ],
    }


def _stub_graphics(monkeypatch):
    monkeypatch.setattr(
        graphics_pipeline, "validate_graphic_output",
        lambda path, expected_duration=None, **kwargs: {
            "passed": True, "failure_kind": None, "detail": "",
            "checks": {}, "probe": None,
        },
    )
    from services.motion_engine import templates as engine_templates

    def fake_card(data, style, out_path, duration=5.0,
                  sentence_index=None, scene_number=None):
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
    monkeypatch.setattr(
        graphics_pipeline, "_render_stat_graphic",
        lambda path, *a, **k: Path(path).write_bytes(b"\x00" * 2048),
    )


def _manifest_entry(manifest_id, scene, index, number, text, **extra):
    entry = {
        "manifest_id": manifest_id,
        "target": {
            "scene_number": scene, "sentence_index": index,
            "sentence_number": number,
        },
        "text": text,
        "scenario": {},
        "purpose": "finding",
        "template_candidates": [],
        "family_candidates": ["event_schematic"],
        "theme": None,
        "fact_basis": "script_claim",
        "evidence_ref": None,
        "verified_data": None,
    }
    entry.update(extra)
    return entry


def test_directed_but_refused_target_served_by_floor(
    tmp_path, monkeypatch
):
    """Run 11 sentences 9/10: a Director-directed manifest entry
    whose chain refuses it (the schematic stage finds no labels)
    used to stay skipped because the floor served only
    undirected targets. The direction is a preference order, not
    a veto: the floor serves the beat, and the directed refusal
    stands recorded in graphic_failures, superseded by the
    guarantee."""
    _stub_graphics(monkeypatch)
    entry = _manifest_entry(
        "gm_0010", 3, 1, 10, "They drove on through the desert town."
    )
    manifest = {"version": 1, "project_id": "r11", "entries": [entry]}
    assets = graphics_pipeline.build_motion_graphics_assets(
        _graphics_data(), tmp_path, mode="balanced", manifest=manifest,
    )
    cards = [a for a in assets if a["graphic_type"] == "closing_card"]
    assert len(cards) == 1
    assert cards[0]["manifest_id"] == "gm_0010"
    assert cards[0]["coverage_guarantee"] is True
    assert cards[0]["sentence_number"] == 10
    written = json.loads((tmp_path / "motion_graphics.json").read_text())
    assert written["targets"]["skipped"] == []
    superseded = [
        f for f in written["graphic_failures"]
        if f.get("superseded_by") == "coverage_guarantee:closing_card"
    ]
    assert len(superseded) == 1
    assert superseded[0]["reason"] in (
        "no_schematic_labels", "no_schematic_motif"
    )
    assert superseded[0]["sentence_number"] == 10


def test_unmanifested_uncovered_beat_enters_graphics_chain(
    tmp_path, monkeypatch
):
    """Run 11 sentence 12: the Director's manifest covers the
    closing beat (sentence 22) but NOT sentence 12, which
    retrieval left uncovered. The caller's target for sentence
    12 used to be discarded when a manifest existed, so the beat
    never entered the chain and its graphics_outcome stayed
    null. It is now appended as an undirected target and served
    by the floor; the manifested beat is not duplicated."""
    _stub_graphics(monkeypatch)
    entry = _manifest_entry(
        "gm_0022", 7, 0, 22,
        "Gabby Petito never got a trial, or a verdict.",
        purpose="coverage_fallback",
    )
    manifest = {"version": 1, "project_id": "r11", "entries": [entry]}
    targets = [
        {
            "scene_number": 7, "sentence_index": 0,
            "sentence_number": 22,
            "text": "Gabby Petito never got a trial, or a verdict.",
            "purpose": "coverage_fallback",
            "template_candidates": [],
            "family_candidates": ["event_schematic"],
        },
        {
            "scene_number": 3, "sentence_index": 0,
            "sentence_number": 12,
            "text": "The message arrived late that night.",
            "purpose": "coverage_fallback",
            "template_candidates": [],
            "family_candidates": ["event_schematic"],
        },
    ]
    assets = graphics_pipeline.build_motion_graphics_assets(
        _graphics_data(), tmp_path, mode="balanced",
        targets=targets, manifest=manifest,
    )
    served = {
        a["sentence_number"] for a in assets
        if a["graphic_type"] == "closing_card"
    }
    assert served == {22, 12}
    card12 = [
        a for a in assets if a["sentence_number"] == 12
    ][0]
    assert card12.get("manifest_id") is None
    assert card12["coverage_guarantee"] is True
    written = json.loads((tmp_path / "motion_graphics.json").read_text())
    outcomes = written["targets"]["outcomes"]
    by_sentence = {o["sentence_number"]: o for o in outcomes}
    assert set(by_sentence) == {22, 12}
    assert by_sentence[12]["outcome"] == "generated"
    assert by_sentence[22]["outcome"] == "generated"
    assert written["targets"]["skipped"] == []
