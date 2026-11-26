"""Quality Recovery Phase 2 — retrieval / recovery observability tests.

Historical filename kept per the programme brief; these tests describe
canonical media identity, duplicate prevention, the reuse penalty,
facet-changing recovery, provider-failure records and retry budgets.
Required-test numbers and canonical DM IDs are in each docstring.
All synthetic and offline; retry timing uses injected fake sleeps —
no real sleeps, no live calls.
"""

import requests

import degraded_fixtures as fx

from services import media_pipeline as mp
from services.canonical_media import canonical_source_key, canonical_url
from services.http_retry import classify_error, is_transient_status, request_with_retry
from services.visual_registry import VisualRegistry


# ------------------------------------------------- canonical identity

def test_canonical_youtube_spellings_are_one_source():
    """Workstream C / M10 (canonical DM-16): youtu.be, watch, shorts
    and embed spellings of one video — with tracking parameters —
    are ONE source; a different video id is a different source."""
    a = canonical_source_key("https://youtu.be/VALIDID001?utm_source=share")
    b = canonical_source_key("https://www.youtube.com/watch?v=VALIDID001&si=xyz")
    c = canonical_source_key("https://www.youtube.com/shorts/VALIDID001?feature=share")
    d = canonical_source_key("https://www.youtube.com/embed/VALIDID001")
    assert a == b == c == d == "youtube:VALIDID001"
    assert canonical_source_key("https://youtu.be/OTHERID002") != a
    assert canonical_url("https://youtu.be/VALIDID001?utm_medium=x") == \
        "https://www.youtube.com/watch?v=VALIDID001"


def test_canonical_archive_and_wikimedia_equivalence():
    """Workstream C: archive.org details ≡ download for one item
    (different items differ); a Wikimedia thumbnail ≡ its original."""
    details = canonical_source_key("https://archive.org/details/item_alpha")
    download = canonical_source_key(
        "https://archive.org/download/item_alpha/item_alpha.mp4")
    assert details == download == "archive.org:item_alpha"
    assert canonical_source_key("https://archive.org/details/item_beta") != details

    original = canonical_source_key(
        "https://upload.wikimedia.org/wikipedia/commons/a/ab/Photo.jpg")
    thumb = canonical_source_key(
        "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Photo.jpg/800px-Photo.jpg")
    assert original == thumb


def test_canonical_tracking_params_stripped_generically():
    a = canonical_source_key("https://news.example.org/story?utm_source=x&id=7")
    b = canonical_source_key("https://news.example.org/story?id=7&fbclid=zzz")
    assert a == b
    assert canonical_source_key("https://news.example.org/story?id=8") != a


def test_candidate_identity_is_canonical():
    """M10 through the pipeline's own identity functions: the same
    video under two spellings gets ONE candidate id and ONE dedup
    key, while two segments of it stay distinct."""
    one = fx.candidate()
    two = fx.MISLEADING["M10"]()
    assert mp._candidate_id_for(one) == mp._candidate_id_for(two)
    assert mp._uve_candidate_key(one) == mp._uve_candidate_key(two)

    seg_a = dict(one, clip_start=10)
    seg_b = dict(two, clip_start=90)
    assert mp._uve_candidate_key(seg_a) != mp._uve_candidate_key(seg_b)
    assert mp._uve_candidate_key(seg_a).split("|")[0] == \
        mp._uve_candidate_key(seg_b).split("|")[0]


def test_registry_counts_canonical_source_once():
    """Workstream C: registry usage/segment bookkeeping is canonical
    — registering via one spelling is visible via the other."""
    registry = VisualRegistry("canon-reg")
    registry.register(
        url="https://www.youtube.com/watch?v=VALIDID001",
        clip_key="youtube:VALIDID001|0", start=0, end=8,
    )
    assert registry.source_usage_count("https://youtu.be/VALIDID001") == 1
    assert registry.is_source_used("https://youtu.be/VALIDID001?utm_source=x")


# ------------------------------------------------------- tests 10 / 11

def _scored(url, score, **over):
    cand = {
        "title": "Verified candidate", "url": url, "video_url": url,
        "verification": {"accepted": True, "score": score},
        "universal_score": score, "relevance_score": score,
    }
    cand.update(over)
    return cand


def test_10_reuse_penalty_prefers_fresh_near_equal_source():
    """Required test 10 (workstream C): with the registry holding 2
    uses of source A, a fresh near-equal source B wins; with the
    penalty at 0 the relevance order is exactly as before; the hard
    cap stays absolute either way."""
    url_a = "https://www.youtube.com/watch?v=REUSED0001"
    url_b = "https://news.example.org/fresh-clip"
    registry = VisualRegistry("t10")
    for i in range(2):
        registry.register(url=url_a, clip_key=f"youtube:REUSED0001|{i * 30}",
                          start=i * 30, end=i * 30 + 8)

    pool = [_scored(url_a, 0.90), _scored(url_b, 0.89)]
    picked = mp._uve_pick(pool, fx.scenario(), registry, set())
    assert picked[0]["url"] == url_b  # fresh source preferred at near-tie
    assert picked[0].get("reuse_penalty") == 0.0
    assert pool[0].get("reuse_penalty") == 0.10  # 2 uses x 0.05, recorded

    pool = [_scored(url_a, 0.90), _scored(url_b, 0.89)]
    picked = mp._uve_pick(
        pool, fx.scenario(), registry, set(), reuse_penalty_per_use=0)
    assert picked[0]["url"] == url_a  # penalty off: pure relevance order

    registry.register(url=url_a, clip_key="youtube:REUSED0001|90",
                      start=90, end=98)
    pool = [_scored(url_a, 0.99), _scored(url_b, 0.50)]
    picked = mp._uve_pick(
        pool, fx.scenario(), registry, set(), reuse_penalty_per_use=0)
    assert [p["url"] for p in picked] == [url_b]  # cap 3 is absolute
    assert pool[0].get("reject_reason") == "source_usage_limit"


def test_10_penalty_never_overrides_material_relevance_gap():
    """Workstream C bound: a materially more relevant reused source
    (gap > 0.10, the maximum possible penalty differential) still
    beats a fresh weak one."""
    url_a = "https://www.youtube.com/watch?v=REUSED0002"
    registry = VisualRegistry("t10b")
    for i in range(2):
        registry.register(url=url_a, clip_key=f"youtube:REUSED0002|{i * 30}",
                          start=i * 30, end=i * 30 + 8)
    pool = [_scored(url_a, 0.90), _scored("https://news.example.org/weak", 0.70)]
    picked = mp._uve_pick(pool, fx.scenario(), registry, set())
    assert picked[0]["url"] == url_a


def test_11_legitimate_segment_reuse_remains_possible():
    """Required test 11 / M12: a second, NON-overlapping verified
    segment of one canonical source is allowed (spelled differently);
    an overlapping segment is blocked as segment_overlap."""
    registry = VisualRegistry("t11")
    registry.register(
        url="https://www.youtube.com/watch?v=SEGMENT001",
        clip_key="youtube:SEGMENT001|0", start=0, end=8,
    )
    allowed, _ = registry.can_use(
        url="https://youtu.be/SEGMENT001", start=100, end=108,
        allow_source_reuse=True,
    )
    assert allowed
    blocked, why = registry.can_use(
        url="https://youtu.be/SEGMENT001", start=4, end=12,
        allow_source_reuse=True,
    )
    assert not blocked and why == "segment_overlap"


def test_11_penalty_never_promotes_unverified_over_verified_reuse():
    """Workstream C bound: the pick pool is accepted-only — an
    unverified fresh candidate cannot beat a verified reused one,
    whatever the penalty."""
    url_a = "https://www.youtube.com/watch?v=REUSED0003"
    registry = VisualRegistry("t11b")
    for i in range(2):
        registry.register(url=url_a, clip_key=f"youtube:REUSED0003|{i * 30}",
                          start=i * 30, end=i * 30 + 8)
    unverified = _scored("https://news.example.org/fresh", 0.99)
    unverified["verification"] = {"accepted": False, "score": 0.99}
    pool = [_scored(url_a, 0.80), unverified]
    picked = mp._uve_pick(pool, fx.scenario(), registry, set())
    assert [p["url"] for p in picked] == [url_a]


# ------------------------------------------------------------ test 14

def _recovery_beat():
    return {
        "beat_id": "beat_09", "subject_id": "bryan_kohberger",
        "place": "Moscow, Idaho", "searchable": True,
        "sentences": [], "sentence_numbers": [], "text": "Bryan Kohberger arrest",
        "shot_needs": [{
            "shot_index": 1, "shot_type": "event", "specificity": "exact",
            "required_subject": "Bryan Kohberger", "required_type": "person",
            "place": "Moscow, Idaho", "era": "2022", "event": "arrest",
            "queries": ["Bryan Kohberger arrest 2022"],
        }],
    }


def test_14_recovery_reformulation_changes_facet_set():
    """Required test 14 (workstream D; DM-06): (a) no executed
    recovery query repeats a tried word-set signature; (b) at least
    one recovery query differs from every tried query by FACET SET —
    and a pure rewording (same facets, near-identical words) is
    recognised as tried and never executed."""
    beat = _recovery_beat()
    tried = [
        "Bryan Kohberger arrest 2022",
        "Bryan Kohberger arrest",
        "Bryan Kohberger Moscow",
        # Pure rewording of the (subject, place, era) template:
        # same facet set {person, place, date}, Jaccard 0.8.
        "Bryan Kohberger Moscow Idaho 2022",
    ]
    queries = mp._recovery_queries(beat, tried, broad=False)
    assert queries, "recovery must still produce untried queries"

    tried_sigs = {mp._query_signature(q) for q in tried}
    for query in queries:
        assert mp._query_signature(query) not in tried_sigs
    assert "Bryan Kohberger Moscow 2022" not in queries  # the rewording

    tried_facets = {mp._query_facets(q, beat) for q in tried}
    assert any(
        mp._query_facets(q, beat) not in tried_facets for q in queries
    ), "at least one recovery query must reformulate by facet set"


def test_14_recovery_attempts_record_facet_signatures():
    """Workstream D: the facet signature of a query is a stable,
    recorded value — same query, same signature; broader queries
    carry the evidence facet."""
    beat = _recovery_beat()
    sig = mp._facet_signature("Bryan Kohberger arrest Moscow 2022", beat)
    assert sig == mp._facet_signature("Bryan Kohberger arrest Moscow 2022", beat)
    assert "person" in sig and "place" in sig and "date" in sig
    broad = mp._facet_signature("Bryan Kohberger photograph", beat)
    assert "evidence" in broad


# ------------------------------------------------------------ test 13

def test_13_provider_failure_records_are_complete_and_typed():
    """Required test 13 (record leg; DM-09): a discovery failure
    record carries provider, query, attempt, error class, HTTP
    status, transient classification, retry count and breaker state
    — and is never collapsed with an empty result (an empty drain is
    the recorded-none state, distinct from a failure)."""
    mp.drain_discovery_failures()

    response = requests.Response()
    response.status_code = 500
    error = requests.HTTPError("Server Error", response=response)
    mp._record_discovery_failure("serper-videos", "some query", error)
    records = mp.drain_discovery_failures()
    assert len(records) == 1
    record = records[0]
    assert record["provider"] == "serper-videos"
    assert record["query"] == "some query"
    assert record["attempt"] == 1
    assert record["error_class"] == "http_500"
    assert record["http_status"] == 500
    assert record["transient"] is True
    assert record["retry_count"] == 0
    assert "tripped" in record["breaker_state"]

    response404 = requests.Response()
    response404.status_code = 404
    mp._record_discovery_failure(
        "wikimedia", "q", requests.HTTPError("Not Found", response=response404))
    record = mp.drain_discovery_failures()[0]
    assert record["error_class"] == "http_404"
    assert record["transient"] is False

    assert mp.drain_discovery_failures() == []  # recorded: none occurred


# ------------------------------------------------------------ test 15

class _FakeResponse:
    def __init__(self, status):
        self.status_code = status
        self.headers = {}


class _CountingSession:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        return _FakeResponse(self.statuses.pop(0))


def test_15_retry_budgets_and_transient_classification():
    """Required test 15 (workstream D): transient failures are
    retried within the attempt budget (fake sleep — deterministic,
    no real delay); a non-transient 404 is returned immediately,
    never retried."""
    sleeps = []
    session = _CountingSession([500, 500, 200])
    response = request_with_retry(
        "GET", "https://provider.example/search",
        session=session, max_attempts=3, sleep=sleeps.append,
    )
    assert response.status_code == 200
    assert session.calls == 3
    assert len(sleeps) == 2

    session = _CountingSession([404, 200])
    response = request_with_retry(
        "GET", "https://provider.example/search",
        session=session, max_attempts=3, sleep=sleeps.append,
    )
    assert response.status_code == 404
    assert session.calls == 1  # non-transient: no retry

    assert is_transient_status(429) and is_transient_status(503)
    assert not is_transient_status(404) and not is_transient_status(400)
    assert classify_error(requests.ConnectTimeout())["transient"] is True


def test_15_circuit_breaker_still_trips_at_eight():
    """Workstream D: the search circuit breaker is preserved and
    test-pinned — 8 consecutive failures trip it."""
    mp._search_failure_state["consecutive_failures"] = 0
    try:
        for i in range(7):
            mp._record_search_failure(RuntimeError(f"boom {i}"))
        assert mp._search_failure_state["consecutive_failures"] == 7
        try:
            mp._record_search_failure(RuntimeError("boom 8"))
            raise AssertionError("breaker should have tripped")
        except mp.SearchCircuitBreakerTripped:
            pass
    finally:
        mp._search_failure_state["consecutive_failures"] = 0


def test_15_failed_candidate_never_redownloaded_under_respelling():
    """Workstream D / Phase 2 guard: a candidate whose download
    failed is skipped under a DIFFERENT spelling of the same source
    — the downloader runs once for the canonical source per run."""
    mp._FAILED_DOWNLOAD_URLS.clear()
    calls = []

    def failing_downloader(candidate, scene_dir, n, i):
        calls.append(candidate.get("url"))
        return None

    registry = VisualRegistry("t15dl")
    first = fx.candidate()
    second = fx.MISLEADING["M10"]()
    failures = []
    result = mp._uve_fetch_first(
        [first], failing_downloader, "/tmp", 1, 1, registry, set(),
        failures=failures,
    )
    assert result is None and len(calls) == 1
    result = mp._uve_fetch_first(
        [second], failing_downloader, "/tmp", 1, 2, registry, set(),
        failures=failures,
    )
    assert result is None and len(calls) == 1  # not re-downloaded
    assert failures[-1]["reason"] == "previously_failed_this_run"
    mp._FAILED_DOWNLOAD_URLS.clear()


# ------------------------------------------------------------ test 16

def test_16_recovered_candidates_pass_the_same_verification_chain():
    """Required test 16 (workstream D): recovery is discovery-only —
    candidates found late face the identical chain, including Phase 1
    contradiction and hard-veto rejection. A vetoed or
    contradictory 'recovery' candidate is rejected exactly like an
    initial one, and a genuinely valid one is accepted.

    (M11's veto was the Pexels provider ban until the founder lifted
    it on 2026-11-26, Annex B; the fixture now carries the surviving
    social-platform veto, canonical reason social_platform_source.)"""
    for bad in (fx.MISLEADING["M01"](), fx.MISLEADING["M11"]()):
        scored = mp._uve_verify_all(
            [bad], fx.scenario(), fx.bible(), shot_need=fx.shot_need())
        assert scored[0].get("reject_reason") in (
            "contradictory_person", "social_platform_source")
        assert not (scored[0].get("verification") or {}).get("accepted")

    scored = mp._uve_verify_all(
        [fx.candidate()], fx.scenario(), fx.bible(), shot_need=fx.shot_need())
    assert (scored[0].get("verification") or {}).get("accepted") is True

# ------------------------------------------------------------ test 12

def test_12_no_acceptable_media_beat_records_honest_gap(tmp_path, monkeypatch):
    """Required test 12 (honest-gap behaviour; DM-08/DM-09 family):
    B12 — a beat whose pool is ONLY misleading candidates (M01 wrong
    person, M04 generic stock, M06 high-quality unrelated) — ends
    with NO accepted asset, a missing_visuals record for EVERY
    sentence of the beat carrying the typed outcome reason
    "no_verified_asset", zero text cards and zero placeholder clips,
    and the asset report shows the gap. The renderer/timeline legs
    of this honesty contract are pinned by
    test_no_placeholders::test_renderer_refuses_when_scene_has_no_genuine_media
    and test_degraded_timeline::test_no_visuals_produces_no_items_and_no_placeholder;
    the renderer leg below ties the same loud failure to THIS beat's
    scene shape."""
    import uuid

    import pytest

    from services import renderer
    from services.asset_report import build_asset_report

    sentences = [
        {"sentence_number": 1,
         "text": "Police arrested Bryan Kohberger in Moscow in 2022."},
        {"sentence_number": 2,
         "text": "The arrest closed the search for the suspect."},
    ]
    beat = {
        "beat_id": "beat_12", "subject_id": "bryan_kohberger",
        "place": "Moscow, Idaho", "searchable": True, "role": "event",
        "sentence_numbers": [1, 2], "sentences": sentences,
        "text": " ".join(s["text"] for s in sentences),
        "shot_needs": [fx.shot_need()],
    }
    pool_factories = (
        fx.MISLEADING["M01"], fx.MISLEADING["M04"], fx.MISLEADING["M06"],
    )

    def fake_search(scenario, cache, pid, shot_need=None):
        pool = [factory() for factory in pool_factories]
        return (pool, [], [])

    monkeypatch.setattr(mp, "build_story_beats", lambda s, b: [beat])
    monkeypatch.setattr(mp, "_load_case_bible", lambda s, p: fx.bible())
    monkeypatch.setattr(mp, "_beat_scenarios", lambda b: [fx.scenario()])
    monkeypatch.setattr(mp, "_uve_scenario_search", fake_search)

    scene = {
        "scene_number": 1,
        "project_id": f"b12-{uuid.uuid4()}",
        "sentences": sentences,
    }
    result = mp._uve_process_scene_media(scene, str(tmp_path))

    # No asset was accepted — for the scene or for any sentence.
    assert result["videos"] == [] and result["images"] == []
    for sentence in result["sentences"]:
        assert not sentence.get("videos") and not sentence.get("images")

    stats = result["asset_stats"]
    # One typed missing-visual record per sentence of the beat.
    missing = stats["missing_visuals"]
    assert [m["sentence_number"] for m in missing] == [1, 2]
    for record in missing:
        assert record["reason"] == "no_verified_asset"
        assert record["specificity"] == "exact"
        assert record["recovery_rounds"] >= 1  # recovery ran first
    # Nothing fabricated anywhere.
    assert stats["text_cards"] == 0
    assert not list(tmp_path.rglob("card_*.jpg"))

    report = build_asset_report(
        [result], renderer_audit={"placeholder_count": 0},
    )
    assert report["placeholder_clips"] == 0
    assert report["text_cards"] == 0
    assert report["total_accepted_assets"] == 0
    # Genuine coverage shows the gap, by sentence number.
    assert report["missing_visual_count"] == 2
    assert [m["sentence_number"] for m in report["missing_visuals"]] == [1, 2]

    # Renderer leg (this beat's shape): a scene whose only timeline
    # item points at media that does not exist has NO genuine media
    # to hold over, so the renderer fails loudly instead of
    # fabricating a placeholder.
    broken_scene = {
        "scene_number": 1,
        "items": [{
            "type": "image",
            "file_path": str(tmp_path / "never-downloaded.png"),
            "start": 0.0, "end": 1.0, "duration": 1.0,
            "sentence_index": 0, "sentence": sentences[0]["text"],
        }],
    }
    with pytest.raises(RuntimeError, match="refusing to substitute a placeholder"):
        renderer.render_scene(broken_scene, str(tmp_path))
