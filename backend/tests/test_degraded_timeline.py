"""Quality Recovery Phase 1 — timeline-invariant tests.

Historical filename kept per the programme brief; these tests describe
timeline eligibility, bounded rotation and within-beat assignment.
Required-test numbers and canonical DM IDs are in each docstring.

Rotation after Phase 1 is a bounded, labelled fallback: per-file use
cap aligned with the selection cap (3), consecutive-reuse avoidance,
a runtime-share preference, and a fair least-used draw. When the
eligible pool is exhausted the sentence stays UNCOVERED — no relaxed
pool, no borrowing, no filler.
"""

import inspect
from collections import Counter

import degraded_fixtures as fx

from services import media_pipeline as mp
from services.timeline import build_scene_timeline


def _visual(tmp_path, name, **over):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 2048)
    visual = {
        "file_path": str(path), "path": str(path), "type": "video",
        "title": name, "verification": {"accepted": True},
    }
    visual.update(over)
    return visual


def _timings(n, step=5.0):
    return [
        {"sentence_index": i, "text": f"Sentence {i}.",
         "start": float(i * step), "end": float(i * step + 4)}
        for i in range(n)
    ]


# ---------------------------------------------------------------- test 7

def test_07_failed_verification_cannot_reach_timeline_any_path(tmp_path):
    """Required test 7 (DM-02/MA-36/MA-37): verifier-rejected,
    verdict-less and download-failed assets are excluded from direct
    assignment AND from the rotation pool; only the genuinely
    verified asset appears."""
    verified = _visual(tmp_path, "verified.mp4")
    rejected = _visual(tmp_path, "rejected.mp4", verification={"accepted": False})
    verdictless = _visual(tmp_path, "verdictless.mp4")
    verdictless.pop("verification")
    failed = _visual(tmp_path, "failed.mp4", _download_error="http_error")

    scene_media = {
        "scene_number": 1, "duration_seconds": 20,
        "videos": [rejected, failed, verdictless, verified],
        "images": [],
        "sentences": [
            {"sentence_number": 1, "videos": [rejected], "images": []},
            {"sentence_number": 2, "videos": [verdictless], "images": []},
            {"sentence_number": 3, "videos": [], "images": []},
            {"sentence_number": 4, "videos": [], "images": []},
        ],
    }
    result = build_scene_timeline(scene_media, _timings(4))
    used = {item["file_path"] for item in result["items"]}
    assert used == {verified["file_path"]}
    for item in result["items"]:
        assert item["assignment"] in ("sentence", "rotation")
        if item["assignment"] == "rotation":
            assert item["relevance_status"] == "not_verified_for_this_sentence"


def test_07_relaxed_pool_never_returns_in_pick():
    """Regression R-A: a verifier-REJECTED candidate with a high score
    is never picked (the old >= 0.15 relaxed pool stays removed)."""
    from services.visual_registry import VisualRegistry

    scored = [{
        "title": "Rejected but high scoring",
        "url": "https://www.youtube.com/watch?v=REJECTED01",
        "verification": {"accepted": False, "score": 0.95},
        "universal_score": 0.95, "relevance_score": 0.95,
    }]
    picked = mp._uve_pick(scored, fx.scenario(), VisualRegistry("ra"), set())
    assert picked == []


# ------------------------------------------------------------- caps (B)

def test_rotation_never_exceeds_per_file_cap_kohberger_profile(tmp_path):
    """Workstream B acceptance: the Kohberger profile (3 unique
    assets, 23 sentences) rebuilt through the new timeline rules —
    no file exceeds the cap of 3 on-screen uses, and the remaining
    sentences are visibly uncovered instead of looped."""
    visuals = [_visual(tmp_path, f"asset{i}.mp4") for i in range(3)]
    scene_media = {
        "scene_number": 1, "duration_seconds": 115,
        "videos": visuals, "images": [],
        "sentences": [
            {"sentence_number": i + 1, "videos": [], "images": []}
            for i in range(23)
        ],
    }
    result = build_scene_timeline(scene_media, _timings(23))
    counts = Counter(item["file_path"] for item in result["items"])
    assert counts, "some rotation is expected before the cap binds"
    assert max(counts.values()) <= 3
    assert len(result["items"]) <= 9
    assert len(result["uncovered_sentence_indexes"]) == 23 - len(result["items"])
    assert all(
        item["assignment"] == "rotation" for item in result["items"]
    )


def test_rotation_fair_draw_uses_stranded_assets_first(tmp_path):
    """VV-07/VV-08: with three eligible files and three rotation
    sentences, every file is used once before any repeat — no fixed
    A-B-C modulo loop, no stranded assets."""
    visuals = [_visual(tmp_path, f"fair{i}.mp4") for i in range(3)]
    scene_media = {
        "scene_number": 1, "duration_seconds": 15,
        "videos": visuals, "images": [],
        "sentences": [
            {"sentence_number": i + 1, "videos": [], "images": []}
            for i in range(3)
        ],
    }
    result = build_scene_timeline(scene_media, _timings(3))
    used = [item["file_path"] for item in result["items"]]
    assert len(used) == 3
    assert len(set(used)) == 3


def test_rotation_avoids_consecutive_reuse_when_alternative_exists(tmp_path):
    """Workstream B: the same file never repeats back-to-back while
    another eligible file is available."""
    visuals = [_visual(tmp_path, f"alt{i}.mp4") for i in range(2)]
    scene_media = {
        "scene_number": 1, "duration_seconds": 20,
        "videos": visuals, "images": [],
        "sentences": [
            {"sentence_number": i + 1, "videos": [], "images": []}
            for i in range(4)
        ],
    }
    result = build_scene_timeline(scene_media, _timings(4))
    used = [item["file_path"] for item in result["items"]]
    assert len(used) == 4
    for first, second in zip(used, used[1:]):
        assert first != second


def test_uncovered_stays_uncovered_when_pool_exhausted(tmp_path):
    """Regression R-E: one eligible file, five sentences needing
    rotation — at most the cap's worth of items is built and the
    rest are recorded uncovered (no filler, no cap violation)."""
    visual = _visual(tmp_path, "only.mp4")
    scene_media = {
        "scene_number": 1, "duration_seconds": 25,
        "videos": [visual], "images": [],
        "sentences": [
            {"sentence_number": i + 1, "videos": [], "images": []}
            for i in range(5)
        ],
    }
    result = build_scene_timeline(scene_media, _timings(5))
    assert len(result["items"]) <= 3
    assert len(result["uncovered_sentence_indexes"]) == 5 - len(result["items"])


def test_no_visuals_produces_no_items_and_no_placeholder(tmp_path):
    """Regression R-C (timeline leg): a scene with no eligible media
    builds zero items — no text card or placeholder is fabricated."""
    scene_media = {
        "scene_number": 1, "duration_seconds": 10,
        "videos": [], "images": [],
        "sentences": [{"sentence_number": 1, "videos": [], "images": []}],
    }
    result = build_scene_timeline(scene_media, _timings(1))
    assert result["items"] == []


# ---------------------------------------------------------------- DM-17

def _beat_with_two_sentences():
    sentence_a = {
        "sentence_number": 1, "text": "Alpha sentence.",
        "role": "event",
        "scenarios": [{
            "visual": "alpha visual", "kind": "event",
            "specificity": "representative", "subject_id": "alpha",
            "queries": ["alpha query"], "search_terms": ["alpha"],
        }],
    }
    sentence_b = {
        "sentence_number": 2, "text": "Bravo sentence.",
        "role": "event",
        "scenarios": [{
            "visual": "bravo visual", "kind": "event",
            "specificity": "representative", "subject_id": "bravo",
            "queries": ["bravo query"], "search_terms": ["bravo"],
        }],
    }
    return {
        "beat_id": "beat_01", "searchable": True, "role": "event",
        "sentences": [sentence_a, sentence_b],
        "sentence_numbers": [1, 2], "text": "Alpha sentence. Bravo sentence.",
    }


def test_dm17_asset_dealt_only_to_owning_sentence():
    """Canonical DM-17: an asset stamped scenario_index=2 (the second
    sentence's scenario) is dealt to sentence offset 1 ONLY — never
    to offset 0 as its direct assignment (the old positional dealing
    did exactly that)."""
    beat = _beat_with_two_sentences()
    asset_b = {"file_path": "/x/bravo.mp4", "scenario_index": 2, "type": "video"}
    per_sentence = mp._distribute_beat_assets(beat, [asset_b], [])
    assert per_sentence[0][0] == []
    assert [a["file_path"] for a in per_sentence[1][0]] == ["/x/bravo.mp4"]

    asset_a = {"file_path": "/x/alpha.mp4", "scenario_index": 1, "type": "video"}
    per_sentence = mp._distribute_beat_assets(beat, [asset_a], [])
    assert [a["file_path"] for a in per_sentence[0][0]] == ["/x/alpha.mp4"]
    assert per_sentence[1][0] == []


def test_dm17_unstamped_asset_is_not_dealt_as_direct_when_linkage_exists():
    """DM-17: with linkage present, an asset whose scenario owner is
    unknown is not labelled direct for any sentence (it remains
    available to the timeline only as labelled rotation)."""
    beat = _beat_with_two_sentences()
    orphan = {"file_path": "/x/orphan.mp4", "type": "video"}
    per_sentence = mp._distribute_beat_assets(beat, [orphan], [])
    assert per_sentence[0][0] == [] and per_sentence[1][0] == []


# ---------------------------------------------------------------- DM-18

def test_dm18_evidence_subsystem_not_wired_into_selection():
    """Canonical DM-18 disposition pin: the quarantined evidence
    subsystem exists in place but the selection path
    (_uve_search_and_pick / _uve_pick) never calls it — wiring it in
    silently would be an unapproved behaviour change."""
    for fn in (mp._uve_search_and_pick, mp._uve_pick):
        src = inspect.getsource(fn)
        assert "rank_visuals_by_evidence" not in src
        assert "calculate_visual_evidence_score" not in src
    assert callable(mp.rank_visuals_by_evidence)  # still present, uncalled
