"""Retrieval resilience + coverage recovery regression tests.

Written for the 2026-11-19 five-minute production failure: a 42-sentence
Berlin documentary completed 40 sentences, then its final scene (three
sentences, one searchable beat) found no verified visual — noisy queries
("flowers champagne wall fall") returned wedding-rental spam, the strict
subject gate rejected every on-topic candidate whose metadata lacked the
required subject tokens, Wikimedia downloads died on single HTTP 429s
with no retry, and no recovery pass existed. The timeline came to
306.5s against 329.49s of narration and the renderer correctly refused.

These tests pin the fixes WITHOUT weakening verification:
* bounded HTTP retry with exponential backoff + Retry-After
  (services/http_retry) wired into the Wikimedia paths;
* provider failure isolation + discovery-failure classification;
* duplicate candidate suppression across queries;
* the subject gate still rejecting irrelevant results;
* bounded coverage recovery: alternative targeted queries, re-verified
  through the same acceptance rules, before a beat may be declared
  uncovered — and honest missing-visual records when recovery fails;
* timeline coverage validation naming the exact uncovered sentences.
"""

import json
import uuid
from pathlib import Path

import pytest
import requests
from PIL import Image

from services import http_retry, licensed_media, media_pipeline
from services.asset_report import build_asset_report
from services.timeline import build_project_timeline


# ---------------------------------------------------------------------------
# 1 + 3: HTTP retry, backoff, Retry-After, attempt limits
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code=200, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _ScriptedSession:
    """Session whose .get returns scripted outcomes in order."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_retry_after_header_is_honoured():
    session = _ScriptedSession(
        [_Resp(429, headers={"Retry-After": "7"}), _Resp(200)]
    )
    delays = []
    response = http_retry.request_with_retry(
        "GET", "https://example.org/x", session=session, sleep=delays.append
    )
    assert response.status_code == 200
    assert session.calls == 2
    assert delays == [7.0]


def test_exponential_backoff_and_attempt_limit():
    session = _ScriptedSession([_Resp(429), _Resp(429), _Resp(429), _Resp(200)])
    delays = []
    response = http_retry.request_with_retry(
        "GET",
        "https://example.org/x",
        session=session,
        max_attempts=3,
        base_delay=1.0,
        sleep=delays.append,
    )
    # Budget spent: exactly 3 attempts, backoff 1s then 2s, and the final
    # 429 response is RETURNED (not hidden) so the caller records it.
    assert session.calls == 3
    assert delays == [1.0, 2.0]
    assert response.status_code == 429


def test_retry_after_is_capped():
    session = _ScriptedSession(
        [_Resp(503, headers={"Retry-After": "3600"}), _Resp(200)]
    )
    delays = []
    http_retry.request_with_retry(
        "GET",
        "https://example.org/x",
        session=session,
        max_delay=60.0,
        sleep=delays.append,
    )
    assert delays == [60.0]


def test_transport_errors_retry_then_raise():
    session = _ScriptedSession(
        [requests.ConnectionError("down"), _Resp(200)]
    )
    delays = []
    response = http_retry.request_with_retry(
        "GET", "https://example.org/x", session=session, sleep=delays.append
    )
    assert response.status_code == 200

    failing = _ScriptedSession(
        [requests.ConnectionError("down"), requests.ConnectionError("down")]
    )
    with pytest.raises(requests.ConnectionError):
        http_retry.request_with_retry(
            "GET",
            "https://example.org/x",
            session=failing,
            max_attempts=2,
            sleep=lambda s: None,
        )
    assert failing.calls == 2


def test_wikimedia_search_survives_a_429(monkeypatch):
    payload = {
        "query": {
            "pages": {
                "1": {
                    "title": "File:Berlin Wall 1989.jpg",
                    "imageinfo": [
                        {
                            "url": "https://upload.wikimedia.org/wall.jpg",
                            "thumburl": "https://upload.wikimedia.org/wall_thumb.jpg",
                            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Berlin_Wall_1989.jpg",
                            "mime": "image/jpeg",
                            "extmetadata": {
                                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0/"},
                                "Artist": {"value": "Commons Photographer"},
                                "ImageDescription": {"value": "Crowds at the wall"},
                            },
                        }
                    ],
                }
            }
        }
    }
    session = _ScriptedSession(
        [_Resp(429, headers={"Retry-After": "0"}), _Resp(200, payload=payload)]
    )
    monkeypatch.setattr(licensed_media, "_session", lambda: session)
    results = licensed_media.search_wikimedia("berlin wall 1989", limit=3)
    assert session.calls == 2  # the 429 was retried, not fatal
    assert len(results) == 1
    assert results[0]["license"] == "CC BY-SA 4.0"


# ---------------------------------------------------------------------------
# 2 + 4 + 8: provider isolation, multi-provider discovery, dedupe
# ---------------------------------------------------------------------------


def _stub_search_env(monkeypatch, *, videos=None, web=None, images=None, wikimedia=None):
    monkeypatch.setattr(
        media_pipeline, "search_videos_multi", videos or (lambda q, limit=8: [])
    )
    monkeypatch.setattr(
        media_pipeline, "search_web_sources", web or (lambda q, limit=5: [])
    )
    monkeypatch.setattr(
        media_pipeline, "search_image_sources", images or (lambda q, limit=5: [])
    )
    monkeypatch.setattr(
        media_pipeline,
        "search_wikimedia_images",
        wikimedia or (lambda q, limit=3: []),
    )
    monkeypatch.setattr(
        media_pipeline, "search_archive_org_videos", lambda q, limit=3: []
    )
    monkeypatch.setattr(
        media_pipeline, "search_wikimedia_videos", lambda q, limit=5: []
    )


def test_provider_failure_is_isolated_and_recorded(monkeypatch):
    def boom(query, limit=8):
        raise RuntimeError("serper down")

    _stub_search_env(
        monkeypatch,
        videos=boom,
        web=lambda q, limit=5: [
            {"title": "Guardian report", "url": "https://www.theguardian.com/x", "provider": "theguardian.com"}
        ],
        images=lambda q, limit=5: [
            {"title": "Wall photo", "image_url": "https://cdn.example.org/w.jpg", "provider": "example.org"}
        ],
    )
    media_pipeline.drain_discovery_failures()
    videos, web, images = media_pipeline._uve_scenario_search(
        {"queries": ["berlin wall"]}, {}, "proj-isolation"
    )
    assert videos == []
    assert len(web) == 1 and len(images) == 1  # other providers unaffected
    failures = media_pipeline.drain_discovery_failures()
    assert any(f["provider"] == "serper-videos" for f in failures)
    assert all("error" in f for f in failures)


def test_discovery_spans_multiple_providers(monkeypatch):
    _stub_search_env(
        monkeypatch,
        videos=lambda q, limit=8: [
            {"title": "V", "video_url": "https://vimeo.com/1", "url": "https://vimeo.com/1", "provider": "vimeo"}
        ],
        web=lambda q, limit=5: [
            {"title": "W", "url": "https://www.bbc.com/story", "provider": "bbc.com"}
        ],
        images=lambda q, limit=5: [
            {"title": "I", "image_url": "https://cdn.example.org/i.jpg", "provider": "example.org"}
        ],
    )
    videos, web, images = media_pipeline._uve_scenario_search(
        {"queries": ["berlin wall"]}, {}, "proj-multi"
    )
    providers = {
        (pool[0].get("provider") if pool else None) for pool in (videos, web, images)
    }
    assert providers == {"vimeo", "bbc.com", "example.org"}


def test_duplicate_candidates_suppressed_across_queries(monkeypatch):
    hit = {
        "title": "Same video",
        "video_url": "https://vimeo.com/dup",
        "url": "https://vimeo.com/dup",
        "provider": "vimeo",
    }
    _stub_search_env(monkeypatch, videos=lambda q, limit=8: [dict(hit)])
    videos, _, _ = media_pipeline._uve_scenario_search(
        {"queries": ["berlin wall", "berlin wall fall 1989"]}, {}, "proj-dup"
    )
    assert len(videos) == 1


# ---------------------------------------------------------------------------
# 7: the strict gate still rejects irrelevant results (never weakened)
# ---------------------------------------------------------------------------


def test_gate_rejects_irrelevant_spam_and_accepts_on_subject_candidate():
    need = {
        "required_type": "event",
        "specificity": "representative",
        "required_subject": "East Berlin",
        "place": "berlin",
        "era": "1989",
    }
    spam = {
        "title": "Champagne Wall Berlin — stylish event display rental",
        "snippet": "Perfect for weddings and parties in Berlin — rent today",
    }
    ok, reason = media_pipeline.gate_shot_need(spam, need)
    assert not ok
    assert reason == "missing_subject"

    relevant = {
        "title": "East Berliners cross at Bornholmer Strasse, 1989",
        "snippet": "Crowds pour through the opened checkpoint in Berlin",
    }
    ok, _ = media_pipeline.gate_shot_need(relevant, need)
    assert ok


# ---------------------------------------------------------------------------
# 5 + 6 + 12: coverage recovery at the beat level
# ---------------------------------------------------------------------------


def _write_png(path, size=(320, 240)):
    Image.effect_noise(size, 64).convert("RGB").save(path)


def _score_by_marker(candidates, scenario, bible, shot_need=None):
    scored = []
    for c in candidates:
        accepted = bool(c.get("_test_accept"))
        score = 0.9 if accepted else 0.0
        item = dict(c)
        item["verification"] = {"accepted": accepted, "score": score}
        item["universal_score"] = score
        item["relevance_score"] = score
        if not accepted:
            item["reject_reason"] = "missing_subject"
        scored.append(item)
    return scored


def _recovery_beat_env(tmp_path, monkeypatch, *, recovery_succeeds):
    sentence = {
        "sentence_number": 43,
        "text": "East Berliners poured through the opened checkpoint, waving flowers.",
    }
    beat = {
        "beat_id": 3,
        "subject_id": "east_berlin",
        "place": "Berlin",
        "searchable": True,
        "sentence_numbers": [43],
        "sentences": [sentence],
        "shot_needs": [
            {
                "shot_index": 1,
                "shot_type": "event",
                "specificity": "representative",
                "required_subject": "East Berlin",
                "place": "Berlin",
                "era": "1989",
                "queries": ["flowers champagne wall fall berlin 1989"],
            }
        ],
        "text": sentence["text"],
        "role": "event",
    }
    scenario = {
        "visual": "crowds crossing checkpoint",
        "kind": "event",
        "specificity": "representative",
        "queries": ["flowers champagne wall fall berlin 1989"],
        "subject_id": "east_berlin",
        "format_intent": "footage",
        "format_intent_confidence": 0.6,
    }
    spam = {
        "title": "Champagne Wall rental",
        "video_url": "https://example.org/spam",
        "url": "https://example.org/spam",
        "provider": "example.org",
        "source": "example.org",
        "snippet": "wedding decor",
        "license": "unknown",
        "_test_accept": False,
    }
    good_image = {
        "title": "East Berliners cross at Bornholmer Strasse, 1989",
        "image_url": "https://cdn.example.org/east-berlin.jpg",
        "url": "https://cdn.example.org/east-berlin.jpg",
        "source_url": "https://news.example.org/story",
        "provider": "news.example.org",
        "source": "news.example.org",
        "snippet": "Crowds at the Berlin checkpoint",
        "license": "unknown",
        "matched_query": "east berlin",
        "_test_accept": True,
    }

    def fake_search(scenario_arg, cache, pid, shot_need=None):
        queries = " ".join(scenario_arg.get("queries") or []).lower()
        if "champagne" in queries:
            return ([dict(spam)], [], [])
        if recovery_succeeds:
            return ([], [], [dict(good_image)])
        return ([dict(spam)], [], [])

    monkeypatch.setattr(media_pipeline, "build_story_beats", lambda s, b: [beat])
    monkeypatch.setattr(media_pipeline, "_load_case_bible", lambda s, p: {})
    monkeypatch.setattr(media_pipeline, "_beat_scenarios", lambda b: [scenario])
    monkeypatch.setattr(media_pipeline, "_uve_scenario_search", fake_search)
    monkeypatch.setattr(media_pipeline, "_uve_verify_all", _score_by_marker)
    monkeypatch.setattr(
        media_pipeline,
        "_segment_start_detailed",
        lambda url, **kw: (0, "whole_asset"),
    )
    monkeypatch.setattr(
        media_pipeline,
        "download_image",
        lambda url, output_path: _write_png(output_path),
    )
    scene = {
        "scene_number": 2,
        "project_id": f"recovery-{uuid.uuid4()}",
        "sentences": [sentence],
    }
    return media_pipeline._uve_process_scene_media(scene, str(tmp_path))


def test_coverage_recovery_finds_visual_after_initial_rejections(tmp_path, monkeypatch):
    result = _recovery_beat_env(tmp_path, monkeypatch, recovery_succeeds=True)
    sentence = result["sentences"][0]
    assert len(sentence.get("images") or []) == 1  # recovered with a REAL verified image
    stats = result["asset_stats"]
    assert stats.get("missing_visuals") in (None, [])
    attempts = stats.get("recovery_attempts") or []
    assert attempts and attempts[0]["outcome"] == "covered"
    assert attempts[0]["round"] == 1


def test_beat_stays_uncovered_when_recovery_is_exhausted(tmp_path, monkeypatch):
    result = _recovery_beat_env(tmp_path, monkeypatch, recovery_succeeds=False)
    sentence = result["sentences"][0]
    assert not sentence.get("images") and not sentence.get("videos")
    stats = result["asset_stats"]
    missing = stats.get("missing_visuals") or []
    assert len(missing) == 1
    assert missing[0]["sentence_number"] == 43
    # Recovery was genuinely attempted (bounded rounds) before declaring
    # the beat uncovered — and no placeholder of any kind was created.
    assert missing[0]["recovery_rounds"] == media_pipeline.MAX_COVERAGE_RECOVERY_ROUNDS
    attempts = stats.get("recovery_attempts") or []
    assert len(attempts) == media_pipeline.MAX_COVERAGE_RECOVERY_ROUNDS
    assert all(a["outcome"] == "no_asset" for a in attempts)
    assert stats.get("text_cards", 0) == 0
    # Rejection reasons were classified, not lost.
    assert (stats.get("rejection_reasons") or {}).get("missing_subject", 0) >= 1


def test_failed_download_urls_are_not_retried_within_a_run():
    media_pipeline._FAILED_DOWNLOAD_URLS.clear()
    calls = []

    def failing_downloader(candidate, scene_dir, sentence_number, asset_index):
        calls.append(candidate["url"])
        candidate["_download_error"] = "download_failed"
        return None

    candidate = {"url": "https://cdn.example.org/dead.jpg", "provider": "example.org"}
    failures = []
    registry = media_pipeline.get_registry(f"failed-url-{uuid.uuid4()}")
    media_pipeline._uve_fetch_first(
        [candidate], failing_downloader, ".", 1, 1, registry, set(), failures=failures
    )
    media_pipeline._uve_fetch_first(
        [candidate], failing_downloader, ".", 1, 2, registry, set(), failures=failures
    )
    assert calls == ["https://cdn.example.org/dead.jpg"]  # requested once only
    assert failures[-1]["reason"] == "previously_failed_this_run"
    media_pipeline._FAILED_DOWNLOAD_URLS.clear()


# ---------------------------------------------------------------------------
# 9 + 10 + 11 + exact-failure reproduction: timeline coverage validation
# ---------------------------------------------------------------------------


def _write_captions(media_root, project_id, segments):
    project_dir = Path(media_root) / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    duration = max(s["end"] for s in segments)
    (project_dir / "captions.json").write_text(
        json.dumps({"duration": duration, "segments": segments}), encoding="utf-8"
    )


def _visual(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 4096)
    # MA-37: fixtures model pipeline-accepted assets and carry the
    # acceptance record; a missing verdict now means unverified.
    return {
        "file_path": str(path), "path": str(path), "type": "video",
        "title": name, "verification": {"accepted": True},
    }


def _project_scenes(tmp_path, *, final_scene_visuals):
    """Two scenes: scene 1 fully visualised; scene 2 mirrors the failed
    run — two sentences rely on the scene pool and its only searchable
    beat produced nothing, so the scene has no visuals at all."""
    v = _visual(tmp_path, "scene1.mp4")
    scene1 = {
        "scene": {"scene_number": 1},
        "scene_number": 1,
        "duration_seconds": 10,
        "videos": [v],
        "images": [],
        "sentences": [
            {"sentence_number": 1, "sentence_text": "The wall divided the city.", "videos": [v], "images": []},
            {"sentence_number": 2, "sentence_text": "Guards watched the border.", "videos": [v], "images": []},
        ],
    }
    scene2_sentences = [
        {"sentence_number": 3, "sentence_text": "And so the city healed.", "videos": [], "images": []},
        {"sentence_number": 4, "sentence_text": "Today the wall is memory.", "videos": [], "images": []},
        {"sentence_number": 5, "sentence_text": "Crowds crossed at last.", "videos": [], "images": []},
    ]
    scene2 = {
        "scene": {"scene_number": 2},
        "scene_number": 2,
        "duration_seconds": 15,
        "videos": list(final_scene_visuals),
        "images": [],
        "sentences": scene2_sentences,
    }
    if final_scene_visuals:
        scene2_sentences[2]["videos"] = list(final_scene_visuals)
    return [scene1, scene2]


_SEGMENTS = [
    {"start": 0.0, "end": 5.0, "text": "The wall divided the city."},
    {"start": 5.0, "end": 10.0, "text": "Guards watched the border."},
    {"start": 10.0, "end": 15.0, "text": "And so the city healed."},
    {"start": 15.0, "end": 20.0, "text": "Today the wall is memory."},
    {"start": 20.0, "end": 25.0, "text": "Crowds crossed at last."},
]
_TEXTS = [s["text"] for s in _SEGMENTS]


def test_timeline_coverage_names_the_uncovered_final_scene(tmp_path):
    project_id = f"coverage-{uuid.uuid4()}"
    _write_captions(tmp_path, project_id, _SEGMENTS)
    scenes = _project_scenes(tmp_path, final_scene_visuals=[])
    timeline = build_project_timeline(project_id, scenes, output_dir=str(tmp_path))

    coverage = timeline["coverage"]
    assert coverage["complete"] is False
    assert coverage["total_sentences"] == 5
    uncovered = coverage["uncovered_sentences"]
    # The exact failure signature: only the final scene's sentences are
    # uncovered, identified by number and scene — not a bare frame delta.
    assert [u["sentence_number"] for u in uncovered] == [3, 4, 5]
    assert all(u["scene_number"] == 2 for u in uncovered)
    # Timeline duration still equals narration duration; the gap is a
    # coverage gap, not a timing error, and nothing was stretched.
    assert timeline["duration_seconds"] == pytest.approx(25.0)


def test_timeline_coverage_complete_and_synchronized(tmp_path):
    project_id = f"coverage-ok-{uuid.uuid4()}"
    _write_captions(tmp_path, project_id, _SEGMENTS)
    v2 = _visual(tmp_path, "scene2.mp4")
    scenes = _project_scenes(tmp_path, final_scene_visuals=[v2])
    timeline = build_project_timeline(project_id, scenes, output_dir=str(tmp_path))

    coverage = timeline["coverage"]
    assert coverage["complete"] is True
    assert coverage["covered_sentences"] == 5
    # Synchronization: every item sits inside its sentence's narration
    # window, and items tile the narration without gaps or overlaps.
    items = sorted(timeline["items"], key=lambda i: i["project_start"])
    assert items[0]["project_start"] == pytest.approx(0.0)
    for prev, nxt in zip(items, items[1:]):
        assert nxt["project_start"] == pytest.approx(prev["project_end"], abs=0.05)
    assert items[-1]["project_end"] == pytest.approx(25.0, abs=0.05)


def test_asset_report_surfaces_coverage_rejections_and_recovery():
    scene = {
        "scene_number": 2,
        "videos": [],
        "images": [],
        "asset_stats": {
            "candidates_considered": 57,
            "candidates_rejected": 57,
            "downloads_failed": 0,
            "text_cards": 0,
            "missing_visuals": [
                {"sentence_number": 43, "reason": "no_verified_asset", "recovery_rounds": 2}
            ],
            "rejection_reasons": {"missing_subject": 51, "score_below_threshold": 6},
            "discovery_failures": [
                {"provider": "wikimedia", "query": "east berlin", "error": "HTTPError: 429"}
            ],
            "recovery_attempts": [
                {"beat_id": 3, "round": 1, "queries": ["East Berlin Berlin 1989"], "outcome": "no_asset"},
                {"beat_id": 3, "round": 2, "queries": ["East Berlin photograph"], "outcome": "no_asset"},
            ],
        },
    }
    timeline = {
        "scenes": [{"items": []}],
        "coverage": {
            "total_sentences": 43,
            "covered_sentences": 42,
            "uncovered_sentences": [{"sentence_number": 43, "scene_number": 2}],
            "complete": False,
        },
    }
    report = build_asset_report([scene], timeline, renderer_audit={"placeholder_count": 0})
    assert report["missing_visual_count"] == 1
    assert report["rejection_reasons"]["missing_subject"] == 51
    assert report["discovery_failures"][0]["provider"] == "wikimedia"
    assert len(report["recovery_attempts"]) == 2
    assert report["coverage"]["complete"] is False
    assert report["text_cards"] == 0 and report["placeholder_clips"] == 0
