"""Coverage-aware picking fix (founder-approved 2026-11-21, founder
live test 2).

The defect: a production run failed TIMELINE COVERAGE INCOMPLETE
(sentences 5, 6, 10 uncovered) while 22 verifier-ACCEPTED candidates
sat unused, rejected at selection with pick_limit_reached — the
per-beat pick limit (3) was a starvation cause, and a background
beat's single accepted asset was blocked by the segment-use cap
from spanning its own beat.

The fix, in three parts:
(a) coverage pass — after the normal pick/fetch in
    _uve_search_and_pick, if the beat's fetched assets cannot cover
    the beat's sentences under the timeline cap, further
    verifier-accepted candidates are drawn from the beat's own pool
    in selection-rank order until coverage is possible or the pool
    is genuinely exhausted;
(b) beat spanning — in build_scene_timeline, a background beat's
    asset serving a sentence of its OWN beat is never charged
    against, and never blocked by, the segment-use cap (value 3
    unchanged; the cap governs cross-beat reuse);
(c) honest records — a coverage-picked candidate's record shows an
    accepted event stamped selection_via="coverage_pass", never a
    standing pick_limit_reached rejection; exhaustion is recorded
    in scene_stats["coverage_passes"] and the asset report.
"""

import json
from pathlib import Path

import pytest

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import media_pipeline as mp
from services import timeline as tl
from services.visual_registry import VisualRegistry


# ---------------------------------------------------------------------------
# Pass-level harness (mirrors tests/test_stage6_diagnostics.py::_run_pass)
# ---------------------------------------------------------------------------

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
            "beat_id": "beat_cov", "sentence_numbers": [1],
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


def _make_downloader(succeed_indexes=(), fail_all=False):
    """Downloader stub with an attempt log. ``succeed_indexes`` are
    the berlin-{i} suffixes whose download succeeds; every other
    attempt fails with a recorded download error."""
    attempts = []

    def download(candidate, scene_dir, sentence_number, asset_index):
        url = str(candidate.get("url") or "")
        attempts.append(url)
        try:
            index = int(url.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            index = -1
        if fail_all or index not in succeed_indexes:
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


# ---------------------------------------------------------------------------
# (a) Coverage pass
# ---------------------------------------------------------------------------

def test_coverage_pass_draws_spares_in_rank_order(monkeypatch, tmp_path):
    host = "cov1.example.org"
    videos = [_verified_video(i, host) for i in range(6)]
    # Primary pick succeeds at rank 0; the first reserve (rank 3)
    # succeeds; ranks 4-5 must never be attempted once covered.
    downloader, attempts = _make_downloader(succeed_indexes={0, 3})
    beat = {
        "beat_id": "beat_cov", "sentence_numbers": [1, 2, 3, 4, 5],
        "text": "The Berlin Wall fell.",
    }
    asset, scene_stats, beat_videos, beat_images = _run_pass(
        monkeypatch, tmp_path, videos=videos, beat=beat,
        downloader=downloader,
    )
    assert asset is not None
    # Rank order: the primary fetch stops at rank 0's success; the
    # coverage pass then attempts rank 3.
    # Phase 5 SUPERSESSION: after coverage is secured, the VARIETY
    # pass (AM-003/FP-4) draws toward ⌈5/2⌉ = 3 distinct assets and
    # so also attempts ranks 4 and 5 — both fail to download here,
    # which the variety record shows honestly. Pre-Phase-5 this
    # asserted attempts == [rank 0, rank 3] and pick_limit records
    # standing for ranks 4–5.
    assert attempts == [
        videos[0]["url"], videos[3]["url"],
        videos[4]["url"], videos[5]["url"],
    ]
    assert len(beat_videos) + len(beat_images) == 2

    # (c) the drawn spare's record is an acceptance via the coverage
    # pass at its recorded rank — no standing pick_limit rejection.
    drawn_records = _records_for(scene_stats, videos[3]["url"])
    accepted = [r for r in drawn_records if r["outcome"] == "accepted"]
    assert len(accepted) == 1
    assert accepted[0]["selection_via"] == "coverage_pass"
    assert accepted[0]["selection_rank"] == 3
    assert not [
        r for r in drawn_records
        if r["outcome"] == "rejected" and r["stage"] == "selection"
    ]
    # The variety-attempted reserves carry download_failed records
    # (Phase 5 supersession — they no longer stand as pick_limit).
    for index in (4, 5):
        records = _records_for(scene_stats, videos[index]["url"])
        assert [
            r for r in records if r["outcome"] == "download_failed"
        ]
        assert not [
            r for r in records
            if r["outcome"] == "rejected" and r["stage"] == "selection"
        ]
    assert scene_stats["rejection_reasons"] == {}
    variety = scene_stats["variety_passes"]
    assert len(variety) == 1
    assert variety[0]["distinct_before"] == 2
    assert variety[0]["target_distinct"] == 3
    assert variety[0]["accepted"] == 0
    assert variety[0]["distinct_after"] == 2
    assert variety[0]["pool_exhausted"] is True
    assert len(variety[0]["drawn"]) == 2

    passes = scene_stats["coverage_passes"]
    assert len(passes) == 1
    assert passes[0]["drawn"] == [accepted[0]["candidate_id"]]
    assert passes[0]["accepted"] == 1
    assert passes[0]["pool_exhausted"] is False
    assert passes[0]["covered_after"] is True
    assert passes[0]["sentence_count"] == 5


def test_coverage_pass_never_admits_verifier_rejected_candidate(
    monkeypatch, tmp_path
):
    host = "cov2.example.org"
    videos = [_verified_video(i, host) for i in range(5)]
    spam = {
        "url": f"https://{host}/cooking",
        "title": "Cooking show highlights",
        "snippet": "recipes and kitchen fun",
        "provider": "youtube",
    }
    # The downloader would succeed for ANYTHING — the rejected
    # candidate must still never be attempted.
    downloader, attempts = _make_downloader(succeed_indexes={0, 1, 2, 3, 4})
    beat = {
        "beat_id": "beat_cov", "sentence_numbers": [1, 2, 3, 4, 5],
        "text": "The Berlin Wall fell.",
    }
    asset, scene_stats, beat_videos, beat_images = _run_pass(
        monkeypatch, tmp_path, videos=videos + [spam], beat=beat,
        downloader=downloader,
    )
    assert asset is not None
    assert spam["url"] not in attempts
    # Phase 5 SUPERSESSION: primary (rank 0) + coverage (rank 3) +
    # variety (rank 4, toward ⌈5/2⌉ = 3 distinct) = 3 assets; the
    # verifier-rejected spam candidate is still never attempted by
    # ANY pass. Pre-Phase-5 this asserted 2 assets.
    assert len(beat_videos) + len(beat_images) == 3
    variety_records = _records_for(scene_stats, videos[4]["url"])
    variety_accepted = [
        r for r in variety_records if r["outcome"] == "accepted"
    ]
    assert len(variety_accepted) == 1
    assert variety_accepted[0]["selection_via"] == "variety_pass"
    spam_records = _records_for(scene_stats, spam["url"])
    assert spam_records
    assert all(r["outcome"] == "rejected" for r in spam_records)
    assert not [
        r for r in scene_stats["candidate_records"]
        if r["outcome"] == "accepted" and r.get("url") == spam["url"]
    ]


def test_coverage_pass_respects_source_cap_at_draw_time(
    monkeypatch, tmp_path
):
    host = "cov3.example.org"
    videos = [_verified_video(i, host) for i in range(4)]
    registry = VisualRegistry()
    # The only reserve's source is already at the registry cap.
    # _uve_pick never examines it (the pick breaks at the limit),
    # so it carries pick_limit_reached into the coverage pass —
    # whose own eligibility check must block it truthfully, without
    # a download attempt, and the shortfall must stay visible.
    for _ in range(3):
        registry.register(url=videos[3]["url"])
    downloader, attempts = _make_downloader(succeed_indexes={0})
    beat = {
        "beat_id": "beat_cov", "sentence_numbers": [1, 2, 3, 4, 5],
        "text": "The Berlin Wall fell.",
    }
    asset, scene_stats, beat_videos, beat_images = _run_pass(
        monkeypatch, tmp_path, videos=videos, beat=beat,
        registry=registry, downloader=downloader,
    )
    assert asset is not None
    assert attempts == [videos[0]["url"]]
    assert len(beat_videos) + len(beat_images) == 1
    blocked = [
        r for r in _records_for(scene_stats, videos[3]["url"])
        if r["outcome"] == "rejected" and r["stage"] == "selection"
    ]
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "source_usage_limit"
    passes = scene_stats["coverage_passes"]
    assert passes[0]["blocked"] == [{
        "candidate_id": blocked[0]["candidate_id"],
        "reason": "source_usage_limit",
    }]
    assert passes[0]["drawn"] == []
    assert passes[0]["accepted"] == 0
    assert passes[0]["pool_exhausted"] is True
    assert passes[0]["covered_after"] is False


def test_coverage_pass_pool_exhaustion_keeps_failure_honest(
    monkeypatch, tmp_path
):
    host = "cov4.example.org"
    videos = [_verified_video(i, host) for i in range(5)]
    downloader, attempts = _make_downloader(fail_all=True)
    beat = {
        "beat_id": "beat_cov", "sentence_numbers": [1, 2, 3, 4, 5],
        "text": "The Berlin Wall fell.",
    }
    asset, scene_stats, beat_videos, beat_images = _run_pass(
        monkeypatch, tmp_path, videos=videos, beat=beat,
        downloader=downloader,
    )
    # Every verified candidate was attempted — primary picks first,
    # then the coverage draws in rank order — and the failure stays
    # a failure: no asset, no accepted records, exhaustion visible.
    assert asset is None
    assert beat_videos == [] and beat_images == []
    assert attempts == [v["url"] for v in videos]
    records = scene_stats["candidate_records"]
    assert len([r for r in records if r["outcome"] == "download_failed"]) == 5
    assert not [
        r for r in records
        if r["outcome"] == "rejected" and r["stage"] == "selection"
    ]
    assert not [r for r in records if r["outcome"] == "accepted"]
    assert scene_stats["rejection_reasons"] == {}
    assert scene_stats["downloads_failed"] == 1
    passes = scene_stats["coverage_passes"]
    assert len(passes) == 1
    assert passes[0]["accepted"] == 0
    assert passes[0]["pool_exhausted"] is True
    assert passes[0]["covered_after"] is False
    assert len(passes[0]["drawn"]) == 2


# ---------------------------------------------------------------------------
# (b) Beat spanning in the timeline
# ---------------------------------------------------------------------------

def _image_asset(path, url):
    return {
        "file_path": str(path), "path": str(path),
        "type": "image", "visual_type": "image",
        "source_url": url, "url": url, "title": url,
        "verification": {"accepted": True, "score": 0.9},
    }


def _timings(count, first_index=0, start=0.0, step=4.0):
    return [
        {
            "sentence_index": first_index + i,
            "start": start + i * step,
            "end": start + (i + 1) * step,
            "text": f"sentence {first_index + i + 1}",
        }
        for i in range(count)
    ]


def _scene_media(backdrop, *, sentence_beats, beats, duration):
    sentences = []
    for position, beat_id in enumerate(sentence_beats):
        sentences.append({
            "sentence_number": position + 1,
            "beat_id": beat_id,
            "videos": [],
            "images": [backdrop] if position == 0 else [],
        })
    return {
        "scene_number": 2,
        "duration_seconds": duration,
        "images": [backdrop],
        "videos": [],
        "sentences": sentences,
        "beats": beats,
    }


def test_background_backdrop_spans_its_own_beat(tmp_path):
    backdrop_path = tmp_path / "backdrop.jpg"
    backdrop_path.write_bytes(b"x")
    backdrop = _image_asset(backdrop_path, "https://img.example.org/backdrop")
    scene_media = _scene_media(
        backdrop,
        sentence_beats=["beat_bg"] * 5,
        beats=[{
            "beat_id": "beat_bg", "role": "background",
            "sentence_numbers": [1, 2, 3, 4, 5],
        }],
        duration=20.0,
    )
    result = tl.build_scene_timeline(
        scene_media, sentence_timings=_timings(5)
    )
    # One backdrop, five sentences, all covered — pre-fix the cap
    # stopped it at 3 uses and sentences 4-5 went uncovered.
    assert result["uncovered_sentence_indexes"] == []
    assert len(result["items"]) == 5
    assert all(
        item["file_path"] == str(backdrop_path) for item in result["items"]
    )


def test_spanning_requires_background_role(tmp_path):
    backdrop_path = tmp_path / "backdrop.jpg"
    backdrop_path.write_bytes(b"x")
    backdrop = _image_asset(backdrop_path, "https://img.example.org/backdrop")
    scene_media = _scene_media(
        backdrop,
        sentence_beats=["beat_bg"] * 5,
        beats=[{"beat_id": "beat_bg", "sentence_numbers": [1, 2, 3, 4, 5]}],
        duration=20.0,
    )
    result = tl.build_scene_timeline(
        scene_media, sentence_timings=_timings(5)
    )
    # No role on the beat record: the pre-fix accounting applies
    # unchanged — cap 3, sentences 4 and 5 (indexes 3, 4) uncovered.
    assert result["uncovered_sentence_indexes"] == [3, 4]
    assert len(result["items"]) == 3


def test_cross_beat_cap_unchanged_for_background_backdrop(tmp_path):
    backdrop_path = tmp_path / "backdrop.jpg"
    backdrop_path.write_bytes(b"x")
    backdrop = _image_asset(backdrop_path, "https://img.example.org/backdrop")
    scene_media = _scene_media(
        backdrop,
        sentence_beats=["beat_b1"] * 2 + ["beat_b2"] * 6,
        beats=[
            {"beat_id": "beat_b1", "role": "background",
             "sentence_numbers": [1, 2]},
            {"beat_id": "beat_b2", "role": "background",
             "sentence_numbers": [3, 4, 5, 6, 7, 8]},
        ],
        duration=32.0,
    )
    result = tl.build_scene_timeline(
        scene_media, sentence_timings=_timings(8)
    )
    # The backdrop spans its own 2 sentences freely, then serves
    # the neighbouring beat under the unchanged cap: exactly 3
    # cross-beat uses, after which the cap binds again.
    assert len(result["items"]) == 5
    assert result["uncovered_sentence_indexes"] == [5, 6, 7]


def test_own_beat_spanning_does_not_consume_cross_beat_allowance(tmp_path):
    backdrop_path = tmp_path / "backdrop.jpg"
    backdrop_path.write_bytes(b"x")
    backdrop = _image_asset(backdrop_path, "https://img.example.org/backdrop")
    scene_media = _scene_media(
        backdrop,
        sentence_beats=["beat_b1"] * 5 + ["beat_b2"] * 3,
        beats=[
            {"beat_id": "beat_b1", "role": "background",
             "sentence_numbers": [1, 2, 3, 4, 5]},
            {"beat_id": "beat_b2", "role": "background",
             "sentence_numbers": [6, 7, 8]},
        ],
        duration=32.0,
    )
    result = tl.build_scene_timeline(
        scene_media, sentence_timings=_timings(8)
    )
    # 5 own-beat uses (uncharged) + the full cross-beat allowance
    # of 3: all 8 sentences covered by the single backdrop.
    assert result["uncovered_sentence_indexes"] == []
    assert len(result["items"]) == 8


# ---------------------------------------------------------------------------
# Regression: founder live test 2 replay from the run's own records
# ---------------------------------------------------------------------------

FOUNDER_PROJECT_DIR = (
    Path(__file__).resolve().parent.parent
    / "media" / "34fbe725-eca0-4edc-b935-9b7cb403761f"
)
FOUNDER_RUN_DIR = (
    FOUNDER_PROJECT_DIR / "runs" / "0d50fdf9-6929-4dd1-9dbb-c820b1294d1d"
)
FOUNDER_ACCEPTED_FILES = [
    FOUNDER_PROJECT_DIR / "scene_2" / "sentence_0002_image_01_Moon.jpg",
    FOUNDER_PROJECT_DIR / "scene_3" / "sentence_0007_image_01_NASA.jpg",
]

pytestmark_replay = pytest.mark.skipif(
    not (
        (FOUNDER_RUN_DIR / "asset_report.json").exists()
        and (FOUNDER_RUN_DIR / "timeline.json").exists()
        and all(p.exists() for p in FOUNDER_ACCEPTED_FILES)
    ),
    reason="founder live test 2 artefacts not present in this workspace",
)


def _replay_asset(record, path):
    return {
        "file_path": str(path), "path": str(path),
        "type": record["kind"], "visual_type": record["kind"],
        "source_url": record["url"], "url": record["url"],
        "title": record["url"],
        "verification": {"accepted": True, "score": record.get("score")},
    }


@pytestmark_replay
def test_founder_run_replay(tmp_path):
    """Reconstruction from the founder run's recorded pools.

    What this proves: (1) the recorded pools for the two starved
    beats contained verifier-accepted spares, in recorded
    selection-rank order; (2) the coverage pass's shortfall
    arithmetic draws exactly the first of those spares for each
    beat; (3) feeding the recorded accepted asset — with and
    without the drawn spare — through the REAL build_scene_timeline
    covers sentences 5, 6 and 10, while the same reconstruction
    WITHOUT the fix's beat information reproduces the founder
    failure exactly ([4, 5] and [9] uncovered, as stored in the
    run's own timeline.json).

    What it does NOT prove: that the spares would still download
    today (they were never fetched; their URLs' liveness is not
    re-tested), nor anything about search/verification — the
    records are taken as the run's ground truth, exactly as the
    pipeline recorded them.
    """
    report = json.loads((FOUNDER_RUN_DIR / "asset_report.json").read_text())
    stored_timeline = json.loads(
        (FOUNDER_RUN_DIR / "timeline.json").read_text()
    )
    stored_uncovered = {
        scene["scene_number"]: scene["uncovered_sentence_indexes"]
        for scene in stored_timeline["scenes"]
    }
    assert stored_uncovered[2] == [4, 5]
    assert stored_uncovered[3] == [9]
    records = report["candidate_records"]["records"]

    # (scene, beat, sentences, accepted file, timings, duration)
    scenes = [
        (
            2, "beat_01_background_moon_none", [2, 3, 4, 5, 6],
            FOUNDER_ACCEPTED_FILES[0],
            [(1, 5.4, 9.27), (2, 9.27, 12.55), (3, 12.55, 15.488),
             (4, 15.488, 22.106), (5, 22.106, 26.065)],
            20.665,
        ),
        (
            3, "beat_01_background_nasa_none", [7, 8, 9, 10],
            FOUNDER_ACCEPTED_FILES[1],
            [(6, 26.065, 28.77), (7, 28.77, 30.87),
             (8, 30.87, 36.142), (9, 36.142, 42.168)],
            16.103,
        ),
    ]

    for (scene_number, beat_id, sentence_numbers, accepted_file,
         windows, duration) in scenes:
        events = [
            r for r in records
            if r.get("beat_id") == beat_id
            and r.get("sentence_numbers") == sentence_numbers
        ]
        verified_ids = {
            r["candidate_id"] for r in events if r.get("outcome") == "verified"
        }
        accepted = [r for r in events if r.get("outcome") == "accepted"]
        spares = sorted(
            (
                r for r in events
                if r.get("stage") == "selection"
                and r.get("reason") == "pick_limit_reached"
            ),
            key=lambda r: r["selection_rank"],
        )
        # The pool held exactly one accepted asset and a rank-ordered
        # bench of verifier-accepted spares; no spare was ever
        # rejected by the gate or the scorer.
        assert len(accepted) == 1
        assert spares, f"{beat_id}: no recorded spares"
        for spare in spares:
            assert spare["candidate_id"] in verified_ids
            assert not [
                r for r in events
                if r.get("candidate_id") == spare["candidate_id"]
                and r.get("outcome") == "rejected"
                and r.get("stage") != "selection"
            ]

        # The coverage pass's own arithmetic: how many spares the
        # amended pipeline draws for this beat.
        needed = mp._uve_coverage_shortfall(
            len(sentence_numbers), len(accepted)
        )
        assert needed == 1
        drawn = spares[:needed]
        assert len(drawn) == needed  # pool not exhausted

        accepted_asset = _replay_asset(accepted[0], accepted_file)
        spare_assets = [
            _replay_asset(
                rec, tmp_path / f"scene{scene_number}_spare{i}.bin"
            )
            for i, rec in enumerate(drawn)
        ]
        for spare_asset in spare_assets:
            Path(spare_asset["file_path"]).write_bytes(b"x")
        timings = [
            {"sentence_index": idx, "start": start, "end": end,
             "text": f"sentence {idx + 1}"}
            for idx, start, end in windows
        ]

        def build(*, with_beats, with_spares):
            assets = [accepted_asset] + (spare_assets if with_spares else [])
            sentences = []
            for position, number in enumerate(sentence_numbers):
                result = {
                    "sentence_number": number,
                    "videos": [],
                    "images": [accepted_asset] if position == 0 else [],
                }
                if with_beats:
                    result["beat_id"] = beat_id
                sentences.append(result)
            scene_media = {
                "scene_number": scene_number,
                "duration_seconds": duration,
                "images": assets,
                "videos": [],
                "sentences": sentences,
            }
            if with_beats:
                scene_media["beats"] = [{
                    "beat_id": beat_id, "role": "background",
                    "sentence_numbers": sentence_numbers,
                }]
            return tl.build_scene_timeline(
                scene_media, sentence_timings=timings
            )

        # Control: pre-fix information shape reproduces the exact
        # founder failure recorded in the run's own timeline.json.
        control = build(with_beats=False, with_spares=False)
        assert control["uncovered_sentence_indexes"] == (
            stored_uncovered[scene_number]
        )
        # (b) alone: the accepted backdrop spans its own beat.
        spanning = build(with_beats=True, with_spares=False)
        assert spanning["uncovered_sentence_indexes"] == []
        assert len(spanning["items"]) == len(sentence_numbers)
        # (a) + (b): the drawn spare joins the scene and serves.
        coverage = build(with_beats=True, with_spares=True)
        assert coverage["uncovered_sentence_indexes"] == []
        assert len(coverage["items"]) == len(sentence_numbers)
        spare_paths = {a["file_path"] for a in spare_assets}
        assert any(
            item["file_path"] in spare_paths for item in coverage["items"]
        )
