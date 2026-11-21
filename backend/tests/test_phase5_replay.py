"""Phase 5 (Master Directive) — founder-films replay (B4 baselines).

The frame-level repetition audit measured the two founder films
from their stored artefacts: Apollo post-fix 53.7% repeated
runtime (7 distinct visuals; the Moon video restarting at offset
0 three times), film 1 59.6% (5 files but only 4 actual visuals —
one photo downloaded twice).

This replay reconstructs each film's scenes from its run's OWN
records (the stored timeline's sentence windows and accepted
assets; the asset report's per-pass verified spares), then deals
them twice:

- BEFORE: the stored timeline itself, measured with the new
  film_level block — it must reproduce the audit baselines;
- AFTER: the same records dealt by the NEW code — the pipeline's
  variety arithmetic (⌈sentences/2⌉ distinct per beat, FP-4)
  draws the recorded spares the stored run left unused, and the
  REAL build_scene_timeline applies continuation + identity
  dealing — and the measured repeat share must fall below BOTH
  the stored before AND the audit baseline.

What it does NOT prove: that the spares would still download
today (they were never fetched; dummy files stand in for their
downloads, exactly as in the coverage fix's founder replay), nor
anything about search/verification — the records are taken as
the run's ground truth. The protected films and all stored
artefacts are read, never written.
"""

import json
from collections import defaultdict
from pathlib import Path

import pytest

import jobs  # noqa: F401
from services import media_pipeline as mp
from services import timeline as tl
from services import visual_quality as vq
from services.media_probe import probe_video

BACKEND = Path(__file__).resolve().parent.parent

FILMS = [
    (
        "apollo",
        BACKEND / "media" / "a798e405-1752-484b-b9f0-3b5402328968",
        "099b2fcd-bad9-447c-9d01-b70d6d72f4fd",
        0.537,
    ),
    (
        "film1",
        BACKEND / "media" / "a1145d94-bf3c-405d-854b-0db459e763ee",
        "d6a64d2a-d81c-44b4-aed4-9e97bc46cf0a",
        0.596,
    ),
]


def _artefacts_present(project_dir, run_id):
    run_dir = project_dir / "runs" / run_id
    return (
        (run_dir / "timeline.json").exists()
        and (run_dir / "asset_report.json").exists()
    )


def _accepted_asset(item):
    asset = {
        "file_path": item["file_path"], "path": item["file_path"],
        "type": item["type"], "visual_type": item["type"],
        "source_url": item.get("source_url", ""),
        "url": item.get("source_url", ""),
        "title": item.get("title", ""),
        "verification": {"accepted": True, "score": 0.9},
    }
    if item.get("segment_id"):
        asset["segment_id"] = item["segment_id"]
    if item.get("clip_start") is not None:
        asset["clip_start"] = item["clip_start"]
    if item["type"] == "video":
        probe = probe_video(item["file_path"])
        if probe and probe.get("duration_seconds"):
            asset["duration_seconds"] = float(probe["duration_seconds"])
            asset["clip_duration"] = float(probe["duration_seconds"])
    return asset


def _reconstruct(project_dir, run_id, tmp_path):
    """(stored_timeline, rebuilt_timeline) from the run's records."""
    run_dir = project_dir / "runs" / run_id
    stored = json.loads((run_dir / "timeline.json").read_text())
    report = json.loads((run_dir / "asset_report.json").read_text())
    records = report["candidate_records"]["records"]

    # Per-pass pools, exactly as recorded (scene-attributed by the
    # report itself): accepted candidates and the rank-ordered
    # verified spares the stored run never drew.
    passes = defaultdict(
        lambda: {"accepted": set(), "spares": [], "verified": set()}
    )
    for record in records:
        key = (
            record.get("scene"),
            record.get("beat_id"),
            tuple(record.get("sentence_numbers") or []),
        )
        pool = passes[key]
        if record.get("outcome") == "verified":
            pool["verified"].add(record["candidate_id"])
        if record.get("outcome") == "accepted":
            pool["accepted"].add(record["candidate_id"])
        if (
            record.get("stage") == "selection"
            and record.get("reason") == "pick_limit_reached"
        ):
            pool["spares"].append(record)
    for pool in passes.values():
        pool["spares"] = sorted(
            (
                s for s in pool["spares"]
                if s["candidate_id"] in pool["verified"]
            ),
            key=lambda s: s.get("selection_rank")
            if s.get("selection_rank") is not None else 1 << 30,
        )

    rebuilt_scenes = []
    for scene in stored["scenes"]:
        scene_number = scene["scene_number"]
        items = sorted(scene["items"], key=lambda i: i["sentence_index"])
        # Accepted assets of the scene, from the stored items.
        assets_by_path = {}
        for item in items:
            if item["file_path"] not in assets_by_path:
                assets_by_path[item["file_path"]] = _accepted_asset(item)
        # The variety arithmetic per recorded pass of this scene:
        # draw toward ⌈sentences/2⌉ distinct accepted assets.
        drawn = []
        scene_passes = [
            (key, pool) for key, pool in passes.items()
            if key[0] == scene_number and key[1]
        ]
        for (_scene, beat_id, numbers), pool in scene_passes:
            if len(numbers) < 2:
                continue
            target = -(-len(numbers) // 2)
            need = target - len(pool["accepted"])
            for spare in pool["spares"][: max(0, need)]:
                path = (
                    tmp_path
                    / f"spare_s{scene_number}_{spare['candidate_id']}.bin"
                )
                path.write_bytes(
                    f"spare:{spare['candidate_id']}".encode()
                )
                drawn.append({
                    "file_path": str(path), "path": str(path),
                    "type": spare["kind"], "visual_type": spare["kind"],
                    "source_url": spare["url"], "url": spare["url"],
                    "title": spare["url"],
                    "duration_seconds": 30.0, "clip_duration": 30.0,
                    "verification": {"accepted": True, "score": 0.9},
                })
        sentence_beat = {}
        beats = []
        for (_scene, beat_id, numbers), pool in scene_passes:
            beats.append({
                "beat_id": beat_id, "role": "background",
                "sentence_numbers": list(numbers),
            })
            for number in numbers:
                sentence_beat[number] = beat_id
        sentences = []
        for item in items:
            number = item["sentence_index"] + 1
            result = {"sentence_number": number, "videos": [], "images": []}
            if number in sentence_beat:
                result["beat_id"] = sentence_beat[number]
            if item.get("assignment") == "sentence":
                asset = assets_by_path[item["file_path"]]
                result[
                    "videos" if asset["type"] == "video" else "images"
                ].append(asset)
            sentences.append(result)
        aggregates = list(assets_by_path.values()) + drawn
        scene_media = {
            "scene_number": scene_number,
            "duration_seconds": scene["duration_seconds"],
            "videos": [a for a in aggregates if a["type"] == "video"],
            "images": [a for a in aggregates if a["type"] != "video"],
            "sentences": sentences,
            "beats": beats,
        }
        timings = [
            {
                "sentence_index": item["sentence_index"],
                "start": item["start"], "end": item["end"],
                "text": item.get("sentence", ""),
            }
            for item in items
        ]
        built = tl.build_scene_timeline(
            scene_media, sentence_timings=timings
        )
        rebuilt_scenes.append({
            "scene_number": scene_number,
            "items": built["items"],
        })
    rebuilt = {"scenes": rebuilt_scenes}
    return stored, rebuilt


@pytest.mark.parametrize(
    "name,project_dir,run_id,baseline", FILMS,
    ids=[film[0] for film in FILMS],
)
def test_founder_film_replay_reduces_repeat_share(
    name, project_dir, run_id, baseline, tmp_path
):
    if not _artefacts_present(project_dir, run_id):
        pytest.skip(f"{name} stored artefacts not present in this workspace")
    stored, rebuilt = _reconstruct(project_dir, run_id, tmp_path)

    before = vq.measure_visual_quality(
        timeline=stored, asset_report={}, rights_manifest=[]
    )["film_level"]
    after = vq.measure_visual_quality(
        timeline=rebuilt, asset_report={}, rights_manifest=[]
    )["film_level"]

    # The new measurement on the stored records reproduces the
    # frame audit's baseline (within reconstruction tolerance) —
    # the before number is the audit's own film, not a strawman.
    assert before["repeat_share"] == pytest.approx(baseline, abs=0.02)
    # The new dealing measurably reduces repetition on the same
    # records: below the stored before AND below the audit
    # baseline, with strictly more distinct visuals on screen.
    assert after["repeat_share"] < before["repeat_share"]
    assert after["repeat_share"] < baseline
    assert after["distinct_visuals"] > before["distinct_visuals"]


def test_apollo_moon_video_continues_instead_of_restarting(tmp_path):
    name, project_dir, run_id, _baseline = FILMS[0]
    if not _artefacts_present(project_dir, run_id):
        pytest.skip("apollo stored artefacts not present in this workspace")
    stored, rebuilt = _reconstruct(project_dir, run_id, tmp_path)
    stored_scene2 = next(
        s for s in stored["scenes"] if s["scene_number"] == 2
    )
    stored_starts = [
        i.get("clip_start") for i in stored_scene2["items"]
        if i["type"] == "video"
    ]
    # The stored film restarted the Moon segment at 0 every time.
    assert len(stored_starts) >= 3
    assert all(s == 0.0 for s in stored_starts)

    rebuilt_scene2 = next(
        s for s in rebuilt["scenes"] if s["scene_number"] == 2
    )
    moon_items = [
        i for i in rebuilt_scene2["items"]
        if i["type"] == "video" and "Moon" in i["file_path"]
    ]
    assert len(moon_items) >= 2
    # The rebuild continues into the segment's unused seconds:
    # at least one later showing starts past 0 and is stamped.
    assert any(i.get("clip_continued") for i in moon_items)
    assert any(i["clip_start"] > 0.0 for i in moon_items)
    # And every recorded window stays inside the segment's real
    # duration (6.133s, probed from the stored file itself).
    assert all(i["clip_end"] <= 6.133 + 1e-6 for i in moon_items)
