"""Stage 7 / R8 + R9 — segment identity, the transcript-anchored
segment index, extraction caching, and the reuse cap rebound to
segment identity (Master Architecture Review C.D.2/C.D.3, C.F.3).

Pins:
- segment_id = sha256(canonical_source_key | start_ms | end_ms);
  stills use the whole-asset form; identity is spelling-independent
  and file-independent;
- the segment index is built once per canonical source from
  chapters + transcript passages + description moments, carries
  basis + evidence text and NO verdict, and segment evidence is
  reused from the index without re-downloading a transcript —
  while verdicts stay per-beat (the reused asset's beat-relative
  fields are re-stamped, never inherited);
- extraction is cached by segment within a run; the fetch loop
  never discards (or deletes) a segment-reused asset as a
  "duplicate";
- segment_id is stamped on the ledger row's verification record
  (copied, never mutating the asset's verdict) and evaluation
  events reference it;
- the timeline screen-time cap counts SEGMENTS: two files carrying
  the same window share one allowance of 3; distinct windows of one
  source hold separate allowances; the per-source SELECTION cap
  (registry) is unchanged;
- visual_quality measures stored timelines at segment identity
  alongside the unchanged per-file numbers.
"""

import hashlib
import json
import uuid

import pytest

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from database import SessionLocal, engine
from models import AssetDecision, Base, ProductionRun, Project

from services import media_pipeline as mp
from services import rejection_diagnostics as rd
from services import run_ledger
from services import segment_index
from services import source_cache
from services import visual_quality
from services.canonical_media import canonical_source_key
from services.timeline import (
    _pick_rotation_visual,
    _visual_use_key,
    build_scene_timeline,
)
from services.visual_registry import VisualRegistry


YT_URL = "https://www.youtube.com/watch?v=abc123XYZ"
YT_URL_SHORT = "https://youtu.be/abc123XYZ"


# ---------------------------------------------------------------------------
# C.D.3 — segment identity
# ---------------------------------------------------------------------------

def test_segment_id_formula_and_stability():
    key = canonical_source_key(YT_URL)
    assert key == "youtube:abc123XYZ"
    expected = "seg_" + hashlib.sha256(
        f"{key}|30000|38000".encode("utf-8")
    ).hexdigest()
    assert segment_index.segment_id_for(key, 30.0, 38.0) == expected
    assert segment_index.segment_id_for(key, 30, 38) == expected
    # A different window of the same source is a different segment.
    assert segment_index.segment_id_for(key, 40.0, 48.0) != expected
    # The whole-asset form is distinct from every windowed form.
    assert segment_index.whole_segment_id_for(key) != expected


def test_segment_identity_is_spelling_independent():
    asset_a = {
        "type": "video", "video_url": YT_URL,
        "clip_start": 30.0, "clip_duration": 8.0,
    }
    asset_b = {
        "type": "video", "video_url": YT_URL_SHORT,
        "clip_start": 30.0, "clip_duration": 8.0,
    }
    assert (
        segment_index.segment_id_for_asset(asset_a)
        == segment_index.segment_id_for_asset(asset_b)
    )


def test_segment_id_for_still_and_graphic():
    still = {
        "type": "image",
        "image_url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/X.jpg",
    }
    assert segment_index.segment_id_for_asset(still) == (
        segment_index.whole_segment_id_for(
            canonical_source_key(still["image_url"])
        )
    )
    assert segment_index.segment_id_for_asset(
        {"type": "video", "motion_graphic": True}
    ) is None


# ---------------------------------------------------------------------------
# The index — storage, build, reuse of evidence
# ---------------------------------------------------------------------------

def test_segment_index_store_round_trip():
    key = f"youtube:stage7-{uuid.uuid4().hex[:8]}"
    source_cache.segment_index_replace(key, [
        {"start": 40.0, "end": 100.0, "basis": "chapter", "text": "Main"},
        {"start": 0.0, "end": 40.0, "basis": "chapter", "text": "Intro"},
    ])
    rows = source_cache.segment_index_for(key)
    assert [r["start"] for r in rows] == [0.0, 40.0]
    assert rows[0]["basis"] == "chapter"
    assert rows[0]["text_hash"] == hashlib.sha256(
        "Intro".encode("utf-8")
    ).hexdigest()


def test_ensure_segment_index_builds_once():
    key = f"youtube:stage7-{uuid.uuid4().hex[:8]}"
    builds = []

    def builder():
        builds.append(1)
        return [{"start": 0.0, "end": 8.0, "basis": "chapter", "text": "A"}]

    segments, built = segment_index.ensure_segment_index(key, builder)
    assert built is True and len(segments) == 1
    again, built_again = segment_index.ensure_segment_index(key, builder)
    assert built_again is False
    assert len(builds) == 1
    assert again[0]["text"] == "A"


def _fake_transcript(*passages):
    return {
        "events": [
            {"tStartMs": int(start * 1000), "segs": [{"utf8": text}]}
            for start, text in passages
        ]
    }


def test_build_segment_index_normalises_all_bases(monkeypatch):
    monkeypatch.setattr(
        mp, "_get_youtube_transcript",
        lambda key: _fake_transcript((0.0, "hello world"),
                                     (5.0, "second passage")),
    )
    slim = {
        "duration": 100,
        "chapters": [
            {"start_time": 0, "title": "Intro"},
            {"start_time": 40, "title": "Main"},
        ],
        "description": "0:07 Opening shot\n1:30 Finale",
    }
    segments = mp._build_segment_index(YT_URL, slim)
    by_basis = {}
    for seg in segments:
        by_basis.setdefault(seg["basis"], []).append(seg)
    assert [(s["start"], s["end"]) for s in by_basis["chapter"]] == [
        (0.0, 40.0), (40.0, 100.0),
    ]
    assert by_basis["chapter"][0]["text"] == "Intro"
    assert [(s["start"], s["end"]) for s in by_basis["transcript"]] == [
        (0.0, 5.0), (5.0, 35.0),
    ]
    assert [(s["start"], s["end"]) for s in by_basis["description"]] == [
        (7.0, 15.0), (90.0, 98.0),
    ]


def test_transcript_passages_come_from_the_index(monkeypatch):
    key = canonical_source_key(YT_URL)
    segment_index.store_segment_index(key, [
        {"start": 12.0, "end": 42.0, "basis": "transcript",
         "text": "the suspect fled the scene"},
    ])

    def _boom(_key):
        raise AssertionError("transcript must not be re-downloaded")

    monkeypatch.setattr(mp, "_get_youtube_transcript", _boom)
    passages = mp._transcript_passages(YT_URL)
    assert passages == [{"start": 12.0, "text": "the suspect fled the scene"}]
    # ... and segment selection works from the indexed evidence.
    start = mp._youtube_transcript_segment(YT_URL, subject="suspect")
    assert start == 12.0


# ---------------------------------------------------------------------------
# Minting + extraction cache + fetch behaviour
# ---------------------------------------------------------------------------

def _stub_video_download(monkeypatch, calls):
    def fake_download(url, output_path, start_time=None, duration=None):
        calls.append((url, start_time))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-clip-bytes")
        return True

    monkeypatch.setattr(mp, "download_video_clip", fake_download)
    monkeypatch.setattr(mp, "validate_video", lambda path: True)
    monkeypatch.setattr(mp, "probe_video", lambda path: {
        "duration_seconds": 8.0, "width": 1280, "height": 720,
        "fps": 30.0, "codec": "h264", "has_audio": True,
    })
    monkeypatch.setattr(mp, "_video_technical_failures", lambda probe: [])
    monkeypatch.setattr(
        mp, "_segment_start_detailed",
        lambda url, **kw: (30, "verified_segment"),
    )
    monkeypatch.setattr(
        mp, "_provider_duration_seconds", lambda cand: (None, "")
    )


def test_video_download_mints_segment_id_and_caches_extraction(
    monkeypatch, tmp_path
):
    mp.reset_media_caches()
    calls = []
    _stub_video_download(monkeypatch, calls)
    url = "https://www.youtube.com/watch?v=stage7mint1"
    candidate = {"url": url, "title": "Some clip", "provider": "youtube"}
    asset = mp._uve_download_video_candidate(
        candidate, tmp_path, sentence_number=3, asset_index=0
    )
    assert asset is not None
    expected = segment_index.segment_id_for(
        canonical_source_key(url), 30.0, 38.0
    )
    assert asset["segment_id"] == expected
    assert candidate["segment_id"] == expected

    # A second beat needing the same segment reuses the extraction:
    # no second download, same file, same segment — with THIS beat's
    # sentence number and verdict re-stamped, not inherited.
    candidate2 = {
        "url": url, "title": "Some clip", "provider": "youtube",
        "verification": {"accepted": True, "score": 0.5,
                         "category": "contextual"},
        "candidate_id": "cand-beat-2",
    }
    asset2 = mp._uve_download_video_candidate(
        candidate2, tmp_path, sentence_number=4, asset_index=0
    )
    assert len(calls) == 1
    assert asset2["segment_reused"] is True
    assert asset2["segment_id"] == expected
    assert asset2["file_path"] == asset["file_path"]
    assert asset2["sentence_number"] == 4
    assert asset2["verification"]["category"] == "contextual"
    assert asset2["candidate_id"] == "cand-beat-2"


def test_image_download_mints_whole_segment_id(monkeypatch, tmp_path):
    def fake_download(url, output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-image")
        return True, ""

    monkeypatch.setattr(mp, "_download_image_detailed", fake_download)
    monkeypatch.setattr(mp, "probe_image", lambda path: {
        "width": 1920, "height": 1080, "format": "JPEG",
    })
    monkeypatch.setattr(mp, "_image_technical_failures", lambda probe: [])
    image_url = "https://upload.wikimedia.org/wikipedia/commons/a/ab/X.jpg"
    candidate = {
        "image_url": image_url,
        "source_url": "https://commons.wikimedia.org/wiki/File:X.jpg",
    }
    asset = mp._uve_download_image_candidate(
        candidate, tmp_path, sentence_number=1, asset_index=0
    )
    assert asset is not None
    assert asset["segment_id"] == segment_index.whole_segment_id_for(
        canonical_source_key(image_url)
    )
    assert candidate["segment_id"] == asset["segment_id"]


def test_fetch_first_never_discards_a_segment_reuse(tmp_path):
    mp.reset_media_caches()
    clip = tmp_path / "shared.mp4"
    clip.write_bytes(b"shared-clip")
    asset = {
        "file_path": str(clip), "path": str(clip),
        "video_url": YT_URL, "url": YT_URL, "type": "video",
        "clip_start": 30.0, "duration_seconds": 8.0,
        "title": "Shared segment", "segment_reused": True,
    }
    candidate = {"url": YT_URL, "provider": "youtube"}
    used_keys = {mp._uve_candidate_key(asset)}
    result = mp._uve_fetch_first(
        [candidate], lambda c, d, n, i: asset, tmp_path,
        sentence_number=5, asset_index=0,
        registry=VisualRegistry(), used_keys=used_keys,
    )
    assert result is asset
    assert clip.exists()  # the shared file is never deleted

    # Control: the same key WITHOUT the reuse mark is a duplicate
    # and is discarded (and its file removed), exactly as before.
    asset2 = dict(asset)
    asset2.pop("segment_reused")
    clip2 = tmp_path / "dupe.mp4"
    clip2.write_bytes(b"dupe")
    asset2["file_path"] = str(clip2)
    asset2["path"] = str(clip2)
    result2 = mp._uve_fetch_first(
        [candidate], lambda c, d, n, i: asset2, tmp_path,
        sentence_number=6, asset_index=0,
        registry=VisualRegistry(), used_keys=used_keys,
    )
    assert result2 is None
    assert not clip2.exists()


# ---------------------------------------------------------------------------
# Stamping — events and the ledger
# ---------------------------------------------------------------------------

def test_build_event_carries_segment_id():
    event = rd.build_event(
        candidate_id="c1", outcome="accepted", stage="download",
        segment_id="seg_abc",
    )
    assert event["segment_id"] == "seg_abc"
    legacy = rd.build_event(
        candidate_id="c1", outcome="rejected", stage="metadata_gate",
    )
    assert legacy["segment_id"] is None


@pytest.fixture(autouse=True)
def _ledger_tables():
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Project.__table__,
            ProductionRun.__table__,
            AssetDecision.__table__,
        ],
    )
    yield


@pytest.fixture()
def project_id():
    pid = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(Project(
            id=pid, script="Stage 7 ledger script.",
            category="Documentary", status="created",
        ))
        db.commit()
    finally:
        db.close()
    yield pid
    db = SessionLocal()
    try:
        db.query(AssetDecision).filter(
            AssetDecision.project_id == pid
        ).delete()
        db.query(ProductionRun).filter(
            ProductionRun.project_id == pid
        ).delete()
        db.query(Project).filter(Project.id == pid).delete()
        db.commit()
    finally:
        db.close()


def _rows_for(run_id):
    db = SessionLocal()
    try:
        return (
            db.query(AssetDecision)
            .filter(AssetDecision.run_id == run_id)
            .all()
        )
    finally:
        db.close()


def test_ledger_row_verification_carries_segment_id(project_id, tmp_path):
    clip = tmp_path / "seg.mp4"
    clip.write_bytes(b"segment-bytes")
    verdict = {
        "accepted": True, "score": 0.9, "category": "contextual",
        "dimension_results": {"entity": "not_required"},
    }
    asset = {
        "candidate_id": "cand-seg-1",
        "file_path": str(clip),
        "type": "video",
        "provider": "youtube",
        "source": "youtube",
        "source_url": YT_URL,
        "video_url": YT_URL,
        "segment_evidence": "verified_segment",
        "clip_start": 30.0,
        "duration_seconds": 8.0,
        "segment_id": "seg_stage7",
        "verification": verdict,
    }
    sentences = [
        {"sentence_number": 1, "videos": [asset], "images": []},
    ]
    processed_scenes = [{
        "scene_number": 1,
        "scene": {"scene_number": 1, "sentences": sentences},
        "sentences": sentences,
        "videos": [asset],
        "images": [],
        "asset_stats": {},
    }]
    run_id = run_ledger.begin_run(project_id, "Stage 7 ledger script.")
    written = run_ledger.record_decisions(
        run_id, project_id, {}, None, processed_scenes=processed_scenes,
    )
    assert written and written >= 1
    rows = [r for r in _rows_for(run_id) if r.decision_type == "selected"]
    assert len(rows) == 1
    recorded = rows[0].verification
    if isinstance(recorded, str):
        recorded = json.loads(recorded)
    assert recorded["segment_id"] == "seg_stage7"
    assert recorded["dimension_results"] == {"entity": "not_required"}
    assert rows[0].interval_start == 30.0
    # The asset's own verdict object was NOT mutated by recording.
    assert "segment_id" not in verdict


def test_ledger_timeline_reconstruction_prefers_clip_start(
    project_id, tmp_path
):
    clip = tmp_path / "recon.mp4"
    clip.write_bytes(b"recon-bytes")
    timeline = {
        "items": [{
            "file_path": str(clip), "type": "video",
            "source_url": YT_URL, "video_url": YT_URL,
            "start": 55.0, "clip_start": 30.0, "duration": 8.0,
            "scene_number": 1, "assignment": "sentence",
        }],
    }
    run_id = run_ledger.begin_run(project_id, "Stage 7 ledger script.")
    run_ledger.record_decisions(run_id, project_id, {}, timeline)
    rows = _rows_for(run_id)
    assert len(rows) == 1
    # The recorded interval is the SOURCE window (clip_start), not
    # the item's timeline position (start).
    assert rows[0].interval_start == 30.0


# ---------------------------------------------------------------------------
# R9 — the cap counts segments
# ---------------------------------------------------------------------------

def _video_visual(path, url=YT_URL, start=30.0, duration=8.0):
    return {
        "file_path": str(path), "path": str(path),
        "type": "video", "video_url": url, "source_url": url,
        "clip_start": start, "duration_seconds": duration,
        "verification": {"accepted": True},
    }


def test_use_key_groups_segment_twins_and_splits_windows(tmp_path):
    twin_a = _video_visual(tmp_path / "a.mp4")
    twin_b = _video_visual(tmp_path / "b.mp4", url=YT_URL_SHORT)
    other_window = _video_visual(tmp_path / "c.mp4", start=100.0)
    assert _visual_use_key(twin_a) == _visual_use_key(twin_b)
    assert _visual_use_key(twin_a) != _visual_use_key(other_window)
    still_a = {
        "file_path": str(tmp_path / "s1.jpg"), "type": "image",
        "image_url": "https://upload.wikimedia.org/x/S.jpg",
        "verification": {"accepted": True},
    }
    still_b = dict(still_a)
    still_b["file_path"] = str(tmp_path / "s2.jpg")
    assert _visual_use_key(still_a) == _visual_use_key(still_b)


def test_rotation_cap_is_per_segment(tmp_path):
    twin_a = _video_visual(tmp_path / "a.mp4")
    twin_b = _video_visual(tmp_path / "b.mp4")
    other = _video_visual(tmp_path / "c.mp4", start=100.0)
    usage_counts = {_visual_use_key(twin_a): 3}
    picked = _pick_rotation_visual(
        [twin_a, twin_b, other], usage_counts, {}, "", 5.0, 60.0
    )
    # Both twins are the SAME segment and it is at the cap; the
    # distinct window is drawn instead.
    assert picked is other
    usage_counts[_visual_use_key(other)] = 3
    assert _pick_rotation_visual(
        [twin_a, twin_b, other], usage_counts, {}, "", 5.0, 60.0
    ) is None


def test_scene_timeline_segment_cap_end_to_end(tmp_path):
    files = {}
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        path = tmp_path / name
        path.write_bytes(b"v")
        files[name] = path
    twin_a = _video_visual(files["a.mp4"])
    twin_b = _video_visual(files["b.mp4"])
    other = _video_visual(files["c.mp4"], start=100.0)
    scene_media = {
        "scene_number": 1,
        "duration_seconds": 40,
        "videos": [twin_a, twin_b, other],
        "images": [],
        "sentences": [],
    }
    timings = [
        {"sentence_index": i, "start": i * 5.0, "end": i * 5.0 + 5.0,
         "text": f"Sentence {i}."}
        for i in range(7)
    ]
    result = build_scene_timeline(scene_media, timings)
    items = result["items"]
    twin_uses = sum(
        1 for item in items
        if item["file_path"] in {str(files["a.mp4"]), str(files["b.mp4"])}
    )
    # One segment, one allowance: the twins together appear at most
    # 3 times (per-file accounting would have allowed 6).
    assert twin_uses == 3
    assert len(items) == 6  # 3 (segment) + 3 (other window)
    assert result["uncovered_sentence_indexes"] == [6]


def test_registry_source_cap_unchanged():
    # The SELECTION cap stays per-source at 3 (R9 changes the
    # timeline cap's identity, not this cap's value or level).
    assert VisualRegistry().max_source_uses == 3


# ---------------------------------------------------------------------------
# R9 — measurement of stored timelines at segment identity (C.F.3)
# ---------------------------------------------------------------------------

def test_visual_quality_segment_reuse_measurement():
    def _video_item(path, start, duration=8.0):
        return {
            "type": "video", "file_path": path,
            "video_url": YT_URL, "source_url": YT_URL,
            "clip_start": start, "duration": duration,
            "assignment": "sentence",
        }

    timeline = {
        "scenes": [{"items": [
            _video_item("/media/one.mp4", 30.0),
            _video_item("/media/two.mp4", 30.0),   # same segment, new file
            _video_item("/media/three.mp4", 100.0),  # distinct window
            {
                "type": "image", "file_path": "/media/still.jpg",
                "image_url": "https://upload.wikimedia.org/x/S.jpg",
                "source_url": "https://commons.wikimedia.org/wiki/File:S.jpg",
                "duration": 5.0, "assignment": "sentence",
            },
        ]}],
    }
    result = visual_quality.measure_visual_quality(
        timeline=timeline, asset_report={}, rights_manifest=[],
    )
    seg = result["timeline"]["segment_reuse"]
    assert seg["unique_segments"] == 3
    assert seg["max_segment_uses"] == 2
    assert seg["segments_reused"] == 1
    assert seg["exact_segment_reuse_items"] == 1
    assert seg["items_without_segment_identity"] == 0
    assert seg["distinct_segments_per_source"]["youtube:abc123XYZ"] == 2
    # The per-file numbers are unchanged and visibly DIFFERENT:
    # four files, one of them notionally "reused" zero times.
    assert result["timeline"]["unique_canonical_assets_on_timeline"] == 4
    assert result["timeline"]["assets_reused"] == 0
