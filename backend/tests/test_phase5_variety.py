"""Phase 5 (Master Directive) — AM-003 variety pass + FP-4 draw.

The variety pass runs inside _uve_search_and_pick AFTER coverage
is secured: when a beat's sentences could only be covered by
rotating fewer distinct assets than ⌈sentences/2⌉, it draws
further DISTINCT verifier-accepted candidates from the pass's own
pools (the pick_limit_reached reserves, incl. ladder items) in
selection-rank order, through the coverage draw's own machinery —
same eligibility re-checks, same fetch path — stamping every draw
selection_via="variety_pass". Rotation remains the fallback; the
pass never draws an unverified or ineligible candidate, and the
timeline's caps are untouched (pinned at the end of this file).

The pass harness mirrors tests/test_coverage_fix.py::_run_pass.
"""

from pathlib import Path

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import media_pipeline as mp
from services import timeline as tl
from services.visual_registry import VisualRegistry


def _run_pass(monkeypatch, tmp_path, *, videos=(), beat=None,
              registry=None, downloader=None):
    monkeypatch.setattr(
        mp, "_uve_scenario_search",
        lambda scenario, cache, pid, need: (list(videos), [], []),
    )
    if downloader is not None:
        monkeypatch.setattr(mp, "_uve_download_any_candidate", downloader)
    scene_stats = {
        "candidates_considered": 0, "candidates_rejected": 0,
        "downloads_failed": 0, "providers": {}, "kinds": {},
    }
    scenario = {
        "sentence_number": 1, "specificity": "representative",
        "subject": "Berlin Wall", "search_terms": ["berlin", "wall"],
        "visual": "berlin wall crowds",
    }
    beat_videos, beat_images = [], []
    asset = mp._uve_search_and_pick(
        scenario, None,
        search_cache={}, project_id="t", bible={},
        registry=registry or VisualRegistry(), used_keys=set(),
        scene_dir=tmp_path, scene_stats=scene_stats,
        beat=beat or {
            "beat_id": "beat_var", "sentence_numbers": [1],
            "text": "The Berlin Wall fell.",
        },
        beat_videos=beat_videos, beat_images=beat_images,
        scenario_index=0, queries=["berlin wall"], review_records=[],
    )
    return asset, scene_stats, beat_videos, beat_images


def _verified_video(index, host):
    return {
        "url": f"https://{host}/berlin-{index}",
        "title": f"Berlin Wall crowds {index}",
        "snippet": "berlin wall crowds",
        "provider": "youtube",
    }


def _make_downloader(succeed_indexes=()):
    attempts = []

    def download(candidate, scene_dir, sentence_number, asset_index):
        url = str(candidate.get("url") or "")
        attempts.append(url)
        try:
            index = int(url.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            index = -1
        if index not in succeed_indexes:
            candidate["_download_error"] = "download_failed"
            return None
        path = Path(scene_dir) / f"asset_{index}.mp4"
        path.write_bytes(b"fake")
        asset = dict(candidate)
        asset.update({
            "file_path": str(path), "path": str(path),
            "type": "video", "visual_type": "video",
            "segment_id": f"seg_test_{index}",
            "duration_seconds": 5.0,
        })
        return asset

    return download, attempts


def _records_for(scene_stats, url):
    return [
        r for r in scene_stats["candidate_records"]
        if r.get("url") == url
    ]


_BEAT6 = {
    "beat_id": "beat_var", "sentence_numbers": [1, 2, 3, 4, 5, 6],
    "text": "The Berlin Wall fell.",
}


def test_variety_pass_prefers_distinct_verified_asset_and_stamps_it(
    monkeypatch, tmp_path
):
    host = "var1.example.org"
    videos = [_verified_video(i, host) for i in range(8)]
    # Primary rank 0; coverage draws rank 3 (2 distinct cover 6
    # sentences under cap 3); variety then draws rank 4 toward the
    # ⌈6/2⌉ = 3-distinct target — and stops there.
    downloader, attempts = _make_downloader(succeed_indexes={0, 3, 4})
    asset, scene_stats, beat_videos, beat_images = _run_pass(
        monkeypatch, tmp_path, videos=videos, beat=_BEAT6,
        downloader=downloader,
    )
    assert asset is not None
    assert attempts == [videos[0]["url"], videos[3]["url"], videos[4]["url"]]
    assert len(beat_videos) + len(beat_images) == 3

    # The variety draw is stamped, at its recorded rank, and its
    # asset carries the route too.
    records = _records_for(scene_stats, videos[4]["url"])
    accepted = [r for r in records if r["outcome"] == "accepted"]
    assert len(accepted) == 1
    assert accepted[0]["selection_via"] == "variety_pass"
    assert accepted[0]["selection_rank"] == 4
    assert not [
        r for r in records
        if r["outcome"] == "rejected" and r["stage"] == "selection"
    ]
    variety_assets = [
        a for a in beat_videos + beat_images
        if a.get("selection_via") == "variety_pass"
    ]
    assert len(variety_assets) == 1

    # The coverage record is unchanged and precedes variety.
    passes = scene_stats["coverage_passes"]
    assert len(passes) == 1
    assert passes[0]["covered_after"] is True
    variety = scene_stats["variety_passes"]
    assert len(variety) == 1
    assert variety[0]["distinct_before"] == 2
    assert variety[0]["target_distinct"] == 3
    assert variety[0]["accepted"] == 1
    assert variety[0]["distinct_after"] == 3
    assert variety[0]["pool_exhausted"] is False
    assert variety[0]["drawn"] == [accepted[0]["candidate_id"]]


def test_variety_pass_never_draws_unverified_candidate(
    monkeypatch, tmp_path
):
    host = "var2.example.org"
    videos = [_verified_video(i, host) for i in range(6)]
    spam = {
        "url": f"https://{host}/cooking",
        "title": "Cooking show highlights",
        "snippet": "recipes and kitchen fun",
        "provider": "youtube",
    }
    downloader, attempts = _make_downloader(
        succeed_indexes={0, 1, 2, 3, 4, 5}
    )
    asset, scene_stats, beat_videos, beat_images = _run_pass(
        monkeypatch, tmp_path, videos=videos + [spam], beat=_BEAT6,
        downloader=downloader,
    )
    assert asset is not None
    # The downloader would succeed for anything; the rejected
    # candidate is in NO pass's pool, variety included.
    assert spam["url"] not in attempts
    assert not [
        r for r in scene_stats["candidate_records"]
        if r["outcome"] == "accepted" and r.get("url") == spam["url"]
    ]


def test_variety_pass_blocked_reserve_is_recorded_not_drawn(
    monkeypatch, tmp_path
):
    host = "var3.example.org"
    videos = [_verified_video(i, host) for i in range(6)]
    registry = VisualRegistry()
    # Rank 4 — the only reserve variety could reach — is at the
    # registry source cap by draw time: blocked truthfully, never
    # downloaded, and the unmet target stays visible.
    for _ in range(3):
        registry.register(url=videos[4]["url"])
    downloader, attempts = _make_downloader(succeed_indexes={0, 3})
    asset, scene_stats, beat_videos, beat_images = _run_pass(
        monkeypatch, tmp_path, videos=videos, beat=_BEAT6,
        registry=registry, downloader=downloader,
    )
    assert asset is not None
    # Rank 4 is blocked WITHOUT a download attempt; the draw moves
    # on to rank 5, whose download fails (not in the succeed set).
    assert attempts == [
        videos[0]["url"], videos[3]["url"], videos[5]["url"]
    ]
    assert len(beat_videos) + len(beat_images) == 2
    variety = scene_stats["variety_passes"]
    assert len(variety) == 1
    assert variety[0]["accepted"] == 0
    assert variety[0]["pool_exhausted"] is True
    assert variety[0]["blocked"] == [{
        "candidate_id": variety[0]["blocked"][0]["candidate_id"],
        "reason": "source_usage_limit",
    }]
    blocked_records = [
        r for r in _records_for(scene_stats, videos[4]["url"])
        if r["outcome"] == "rejected" and r["stage"] == "selection"
    ]
    assert len(blocked_records) == 1
    assert blocked_records[0]["reason"] == "source_usage_limit"


def test_variety_pass_records_empty_pool_honestly(monkeypatch, tmp_path):
    host = "var4.example.org"
    videos = [_verified_video(i, host) for i in range(4)]
    downloader, attempts = _make_downloader(succeed_indexes={0, 3})
    asset, scene_stats, beat_videos, beat_images = _run_pass(
        monkeypatch, tmp_path, videos=videos, beat=_BEAT6,
        downloader=downloader,
    )
    assert asset is not None
    # Coverage secured with 2 distinct; the ⌈6/2⌉ target is unmet
    # and the pool holds no further reserves — recorded, not silent.
    variety = scene_stats["variety_passes"]
    assert len(variety) == 1
    assert variety[0]["drawn"] == []
    assert variety[0]["accepted"] == 0
    assert variety[0]["distinct_after"] == 2
    assert variety[0]["pool_exhausted"] is True


def test_no_variety_when_target_already_met(monkeypatch, tmp_path):
    host = "var5.example.org"
    videos = [_verified_video(i, host) for i in range(6)]
    downloader, attempts = _make_downloader(succeed_indexes={0, 3})
    beat = {
        "beat_id": "beat_var", "sentence_numbers": [1, 2, 3, 4],
        "text": "The Berlin Wall fell.",
    }
    asset, scene_stats, beat_videos, beat_images = _run_pass(
        monkeypatch, tmp_path, videos=videos, beat=beat,
        downloader=downloader,
    )
    assert asset is not None
    # Coverage leaves 2 distinct = ⌈4/2⌉: no variety entry at all.
    assert "variety_passes" not in scene_stats
    assert attempts == [videos[0]["url"], videos[3]["url"]]


def test_no_variety_when_coverage_unsecured(monkeypatch, tmp_path):
    host = "var6.example.org"
    videos = [_verified_video(i, host) for i in range(5)]
    downloader, attempts = _make_downloader(succeed_indexes=set())
    asset, scene_stats, beat_videos, beat_images = _run_pass(
        monkeypatch, tmp_path, videos=videos, beat=_BEAT6,
        downloader=downloader,
    )
    # Everything failed: coverage exhausted, variety never fires —
    # it may not manufacture coverage from an unsecured beat.
    assert asset is None
    assert "variety_passes" not in scene_stats
    assert scene_stats["coverage_passes"][0]["pool_exhausted"] is True


# ---------------------------------------------------------------------------
# Caps still bind at the timeline (values AND semantics unchanged)
# ---------------------------------------------------------------------------

def test_timeline_cap_still_binds_with_distinct_assets_available(tmp_path):
    paths = []
    visuals = []
    for i in range(2):
        path = tmp_path / f"still_{i}.jpg"
        path.write_bytes(b"x")
        paths.append(str(path))
        visuals.append({
            "file_path": str(path), "path": str(path),
            "type": "image", "visual_type": "image",
            "source_url": f"https://img.example.org/{i}",
            "url": f"https://img.example.org/{i}",
            "verification": {"accepted": True, "score": 0.9},
        })
    timings = [
        {
            "sentence_index": i, "start": i * 4.0, "end": (i + 1) * 4.0,
            "text": f"sentence {i + 1}",
        }
        for i in range(7)
    ]
    scene_media = {
        "scene_number": 1, "duration_seconds": 28.0,
        "images": visuals, "videos": [],
        "sentences": [
            {"sentence_number": i + 1, "videos": [], "images": []}
            for i in range(7)
        ],
    }
    result = tl.build_scene_timeline(scene_media, sentence_timings=timings)
    # Two DISTINCT verified assets exist the whole time; the cap
    # (3 uses each) still binds: 6 items, sentence 7 uncovered.
    # Variety widens the pool — it never loosens the cap.
    assert len(result["items"]) == 6
    assert result["uncovered_sentence_indexes"] == [6]
    assert tl.TIMELINE_MAX_SEGMENT_USES == 3
