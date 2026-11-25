"""Transformation Phase 4.2 — institutional source hints.

Behaviour pinned here:

- the engineering / industrial packs carry a source_hints block
  that validates and resolves; packs without the block resolve to
  no hints, and an invalid block fails closed to no hints with the
  failures recorded;
- formatting substitutes the need's anchors, skips templates
  whose anchors are missing, dedupes and caps per channel;
- build_shot_needs stamps formatted hint queries on needs when
  the bible's research_meta names a pack with hints — and stamps
  an empty list otherwise (pre-4.2 behaviour is byte-identical);
- _uve_scenario_search executes hint queries on their channels,
  merges their hits under the shared dedupe, logs them under
  source "source_hint" for the AM-021 diagnostics, fires them
  ONLY on direct-tier passes, and isolates a failing hint;
- the asset report aggregates the hint funnel (discovered /
  verified attributable to hints vs baseline) from the AM-021
  query diagnostics, and states "no_hints" when none ran.
"""

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import genre_packs
from services import media_pipeline as mp
from services.asset_report import build_asset_report


# ---------------------------------------------------------------------------
# Pack data: validation + resolution
# ---------------------------------------------------------------------------

def test_engineering_and_industrial_packs_carry_valid_hints():
    for pack_id in ("engineering", "industrial"):
        pack = genre_packs.load_pack(pack_id)
        assert pack is not None
        assert genre_packs.validate_source_hints(
            pack.get("source_hints")
        ) == []
        resolved = genre_packs.resolve_source_hints(pack)
        assert resolved["failures"] == []
        hints = resolved["hints"]
        # The plan's two template families, present per pack.
        assert hints["wikimedia_category"]
        assert hints["archive"]
        assert hints["web"]


def test_packs_without_hints_resolve_empty():
    # Gate 2 (2026-11-26): general / history / true_crime gained
    # source_hints blocks (covered in
    # test_gate2_discovery_wiring.py) — the empty-resolution
    # contract now applies to packs/inputs without a block.
    assert genre_packs.resolve_source_hints(None) == {
        "hints": {}, "failures": [],
    }
    assert genre_packs.resolve_source_hints({}) == {
        "hints": {}, "failures": [],
    }
    assert genre_packs.resolve_source_hints({"id": "x"}) == {
        "hints": {}, "failures": [],
    }


def test_source_hint_validation_rules():
    assert genre_packs.validate_source_hints(None) == []
    assert genre_packs.validate_source_hints("nope") == [
        "source_hints_not_an_object"
    ]
    assert genre_packs.validate_source_hints({"bogus": []}) == [
        "source_hints_unknown_channels:bogus"
    ]
    # A template with no anchor would fire identically for every
    # beat in the genre — a flood, not a hint.
    assert genre_packs.validate_source_hints(
        {"web": ["site:ntsb.gov"]}
    ) == ["source_hints_web_template_unanchored"]
    assert genre_packs.validate_source_hints(
        {"web": ["{query} site:{planet}.gov"]}
    ) == ["source_hints_web_unknown_placeholder:{planet}"]
    assert genre_packs.validate_source_hints(
        {"archive": ["{subject}"] * 5}
    ) == ["source_hints_archive_too_many"]
    assert genre_packs.validate_source_hints(
        {"archive": "{subject}"}
    ) == ["source_hints_archive_not_a_list"]


def test_invalid_block_fails_closed_to_no_hints():
    pack = {"id": "x", "source_hints": {"web": ["site:ntsb.gov"]}}
    resolved = genre_packs.resolve_source_hints(pack)
    assert resolved["hints"] == {}
    assert resolved["failures"] == [
        "source_hints_web_template_unanchored"
    ]


# ---------------------------------------------------------------------------
# Formatting against a need's anchors
# ---------------------------------------------------------------------------

def test_format_source_hint_queries():
    hints = {
        "wikimedia_category": [
            'incategory:"Ports" {subject}',
            'incategory:"Ports" {subject}',  # duplicate template
        ],
        "archive": [
            "collection:prelinger {subject}",
            "{subject} {place} harbour",  # needs place
        ],
        "web": ["{query} port authority"],
    }
    out = genre_packs.format_source_hint_queries(
        hints,
        subject="container terminal",
        place="",
        era="",
        query="container ship unloading",
    )
    by_channel = {}
    for entry in out:
        by_channel.setdefault(entry["channel"], []).append(entry)
    # Duplicate template formatted once.
    assert len(by_channel["wikimedia_category"]) == 1
    assert by_channel["wikimedia_category"][0]["query"] == (
        'incategory:"Ports" container terminal'
    )
    # The {place} template is skipped — its anchor is empty.
    assert [e["query"] for e in by_channel["archive"]] == [
        "collection:prelinger container terminal"
    ]
    assert by_channel["web"][0]["query"] == (
        "container ship unloading port authority"
    )
    # The originating template is preserved on every record.
    assert all(e["template"] for e in out)


def test_format_source_hint_caps_per_channel():
    hints = {
        "archive": [f"{{subject}} fragment {i}" for i in range(4)],
    }
    out = genre_packs.format_source_hint_queries(
        hints, subject="dam failure",
    )
    assert len(out) == genre_packs.SOURCE_HINT_CAPS["archive"]


# ---------------------------------------------------------------------------
# build_shot_needs stamps the hints from the bible's pack
# ---------------------------------------------------------------------------

def _beat():
    sentence = {
        "sentence_number": 1,
        "text": "The walkway fell into the crowded atrium.",
        "role": "action",
        "scenarios": [{
            "sentence_number": 1,
            "specificity": "representative",
            "kind": "place",
            "subject": "Hyatt Regency Kansas City",
            "place": "Kansas City",
            "era": "1981",
            "visual": "hotel atrium walkways",
            "queries": ["Hyatt Regency Kansas City walkway"],
        }],
    }
    return {
        "beat_id": "beat_h1",
        "sentence_numbers": [1],
        "text": sentence["text"],
        "sentences": [sentence],
        "subject_id": None,
        "place": "Kansas City",
    }


def _bible(pack_id):
    bible = {
        "entities": [],
        "story_era": "1981",
        "research_meta": {"genre_pack": pack_id},
    }
    return bible


def test_build_shot_needs_stamps_engineering_hints():
    needs = mp.build_shot_needs(_beat(), _bible("engineering"))
    assert len(needs) == 1
    hint_queries = needs[0]["source_hint_queries"]
    channels = {h["channel"] for h in hint_queries}
    assert "archive" in channels
    assert "wikimedia_category" in channels
    assert "web" in channels
    archive_queries = [
        h["query"] for h in hint_queries if h["channel"] == "archive"
    ]
    assert any(
        "National Transportation Safety Board" in q
        and "Hyatt Regency Kansas City" in q
        for q in archive_queries
    )
    web_queries = [
        h["query"] for h in hint_queries if h["channel"] == "web"
    ]
    assert web_queries == [
        "Hyatt Regency Kansas City walkway "
        "site:ntsb.gov OR site:nist.gov OR site:loc.gov"
    ]


def test_build_shot_needs_no_pack_no_hints():
    needs = mp.build_shot_needs(_beat(), {"entities": []})
    assert needs[0]["source_hint_queries"] == []
    # Gate 2 (2026-11-26): the general pack now carries hints, so
    # naming it stamps hint queries like every other pack.
    needs = mp.build_shot_needs(_beat(), _bible("general"))
    assert needs[0]["source_hint_queries"] != []


# ---------------------------------------------------------------------------
# _uve_scenario_search executes the hints
# ---------------------------------------------------------------------------

def _hit(kind, url, query):
    base = {
        "title": f"hit for {query}",
        "snippet": "institutional footage",
        "matched_query": query,
    }
    if kind == "video":
        base.update({"video_url": url, "url": url, "source": "archive.org"})
    elif kind == "image":
        base.update({"image_url": url, "url": url, "source": "wikimedia"})
    else:
        base.update({"url": url, "source": "loc.gov"})
    return base


def _stub_all(monkeypatch, *, fail_channels=()):
    calls = []

    def _make(name, kind):
        def _fn(query, limit=5):
            calls.append((name, query))
            if name in fail_channels:
                raise RuntimeError(f"{name} down")
            return [_hit(kind, f"https://{name}.example/{abs(hash((name, query))) % 99999}", query)]
        return _fn

    monkeypatch.setattr(mp, "search_videos_multi", _make("videos_multi", "video"))
    monkeypatch.setattr(mp, "search_web_sources", _make("web_sources", "web"))
    monkeypatch.setattr(mp, "search_image_sources", _make("image_sources", "image"))
    monkeypatch.setattr(mp, "search_wikimedia_images", _make("wikimedia_images", "image"))
    monkeypatch.setattr(mp, "search_wikimedia_videos", _make("wikimedia_videos", "video"))
    monkeypatch.setattr(mp, "search_archive_org_videos", _make("archive_videos", "video"))
    return calls


def _scenario():
    return {
        "sentence_number": 1,
        "specificity": "representative",
        "subject": "Hyatt Regency Kansas City",
        "visual": "hotel atrium walkways",
        "queries": ["Hyatt Regency Kansas City walkway"],
    }


def _need_with_hints(tier="direct"):
    return {
        "beat_id": "beat_h1",
        "shot_index": 1,
        "tier": tier,
        "queries": ["Hyatt Regency Kansas City walkway"],
        "query_sources": [
            {"query": "Hyatt Regency Kansas City walkway",
             "source": "scenario"},
        ],
        "source_hint_queries": [
            {"query": "incategory:\"Ports\" container terminal",
             "channel": "wikimedia_category", "template": "t1"},
            {"query": "collection:prelinger container terminal",
             "channel": "archive", "template": "t2"},
            {"query": "container ship unloading site:loc.gov",
             "channel": "web", "template": "t3"},
        ],
    }


def _run_search(mp_module, scenario, need):
    log = []
    mp_module._QUERY_LOG_STACK.append(log)
    try:
        result = mp_module._uve_scenario_search(
            scenario, {}, "proj", need,
        )
    finally:
        mp_module._QUERY_LOG_STACK.pop()
    return result, log


def test_scenario_search_executes_hints_and_logs_them(monkeypatch):
    calls = _stub_all(monkeypatch)
    (videos, web, images), log = _run_search(
        mp, _scenario(), _need_with_hints()
    )
    hint_calls = [
        q for name, q in calls
        if "prelinger" in q or "incategory" in q or "site:loc.gov" in q
    ]
    # wikimedia hint: images + videos (2); archive hint: 1;
    # web hint: videos + web + images (3).
    assert len(hint_calls) == 2 + 1 + 3
    # Every hint query is in the AM-021 log under source_hint.
    hint_log = [e for e in log if e["source"] == "source_hint"]
    assert {e["query"] for e in hint_log} == {
        'incategory:"Ports" container terminal',
        "collection:prelinger container terminal",
        "container ship unloading site:loc.gov",
    }
    assert all(e["hits"] >= 1 for e in hint_log)
    # Hint hits merged into the pools.
    urls = {
        str(v.get("video_url") or v.get("url") or "") for v in videos
    }
    assert any("archive_videos" in u for u in urls)


def test_scenario_search_hints_fire_only_on_direct_tier(monkeypatch):
    calls = _stub_all(monkeypatch)
    _run_search(mp, _scenario(), _need_with_hints(tier="related"))
    assert not [
        q for _name, q in calls
        if "prelinger" in q or "incategory" in q
    ]


def test_scenario_search_hint_failure_is_isolated(monkeypatch):
    _stub_all(monkeypatch, fail_channels={"archive_videos"})
    (videos, web, images), log = _run_search(
        mp, _scenario(), _need_with_hints()
    )
    # The archive hint died; the wikimedia + web hints still landed,
    # and the failure was recorded, not raised.
    hint_log = {e["query"] for e in log if e["source"] == "source_hint"}
    assert 'incategory:"Ports" container terminal' in hint_log
    failures = mp.drain_discovery_failures()
    assert any(
        f["provider"] == "source_hint" for f in failures
    )


def test_scenario_search_without_hints_unchanged(monkeypatch):
    calls = _stub_all(monkeypatch)
    need = _need_with_hints()
    need["source_hint_queries"] = []
    _result, log = _run_search(mp, _scenario(), need)
    assert [e for e in log if e["source"] == "source_hint"] == []


# ---------------------------------------------------------------------------
# Asset report: the hint funnel vs baseline
# ---------------------------------------------------------------------------

def _scene_with_diagnostics():
    return {
        "scene_number": 1,
        "asset_stats": {
            "query_diagnostics": [{
                "beat_id": "beat_h1",
                "shot_index": 1,
                "pass": "primary",
                "tier": "direct",
                "queries": [
                    {"query": "base q", "source": "composed",
                     "hits": 10, "candidates": 8, "verified": 3},
                    {"query": "hint q", "source": "source_hint",
                     "hits": 4, "candidates": 4, "verified": 2},
                ],
            }],
        },
        "sentences": [],
    }


def test_asset_report_source_hints_summary():
    report = build_asset_report(
        [_scene_with_diagnostics()],
        timeline_result=None,
        genre_pack={"id": "engineering"},
    )
    summary = report["source_hints"]
    assert summary["state"] == "recorded"
    assert summary["genre_pack"] == "engineering"
    assert summary["hints"] == {
        "queries": 1, "hits": 4, "candidates": 4, "verified": 2,
    }
    assert summary["baseline"] == {
        "queries": 1, "hits": 10, "candidates": 8, "verified": 3,
    }


def test_asset_report_source_hints_absent_state():
    report = build_asset_report(
        [{"scene_number": 1, "asset_stats": {}, "sentences": []}],
        timeline_result=None,
    )
    summary = report["source_hints"]
    assert summary["state"] == "no_hints"
    assert summary["hints"]["queries"] == 0
