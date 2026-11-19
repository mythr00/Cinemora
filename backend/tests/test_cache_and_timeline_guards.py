"""VA-14 / VA-16 regression tests.

- Successful source profiles expire: past the TTL they are reported
  stale and _youtube_slim re-analyses instead of trusting them.
- The timeline refuses assets explicitly marked rejected or failed,
  even if one is (incorrectly) present in a sentence result.
"""

import json
import time
import uuid

from services import media_pipeline as mp
from services import source_cache
from services.timeline import build_project_timeline


def test_profile_becomes_stale_after_ttl(monkeypatch):
    url = "https://www.youtube.com/watch?v=stale1"
    source_cache.profile_upsert(
        url, status="ok", duration=120, title="T",
        transcript_status="ok",
    )
    fresh = source_cache.profile_get(url)
    assert fresh["status"] == "ok"
    # Age the stored analysis beyond the TTL.
    with source_cache._db() as c:
        c.execute(
            "UPDATE source_profile SET analyzed_at=? WHERE url=?",
            (time.time() - 8 * 24 * 3600, url),
        )
    stale = source_cache.profile_get(url)
    assert stale["status"] == "stale"
    # Facts remain available as hints.
    assert stale["duration"] == 120


def test_slim_reanalyses_stale_profile(monkeypatch):
    key = "https://www.youtube.com/watch?v=stale2"
    monkeypatch.setattr(source_cache, "profile_get", lambda u: {
        "status": "stale", "duration": 120, "chapters": [],
        "title": "Old", "description": "",
    })
    monkeypatch.setattr(mp, "_youtube_info", lambda k: {
        "duration": 130, "chapters": [], "title": "Fresh", "description": "",
        "_full": True,
    })
    mp._youtube_metadata_cache.clear()
    slim = mp._youtube_slim(key)
    assert slim["duration"] == 130
    assert slim["title"] == "Fresh"


def test_slim_uses_fresh_profile_without_reanalysis(monkeypatch):
    key = "https://www.youtube.com/watch?v=fresh1"
    monkeypatch.setattr(source_cache, "profile_get", lambda u: {
        "status": "ok", "duration": 120, "chapters": [],
        "title": "Cached", "description": "",
    })

    def forbidden(k):
        raise AssertionError("yt-dlp must not run for a fresh profile")

    monkeypatch.setattr(mp, "_youtube_info", forbidden)
    mp._youtube_metadata_cache.clear()
    slim = mp._youtube_slim(key)
    assert slim["duration"] == 120


def _visual(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 4096)
    return {"file_path": str(path), "path": str(path), "type": "video", "title": name}


def test_timeline_skips_rejected_assets(tmp_path):
    project_id = f"guard-{uuid.uuid4()}"
    media_dir = tmp_path / "media" / project_id
    media_dir.mkdir(parents=True)
    segments = [
        {"start": 0.0, "end": 5.0, "text": "One."},
        {"start": 5.0, "end": 10.0, "text": "Two."},
    ]
    (media_dir / "captions.json").write_text(json.dumps(
        {"duration": 10.0, "segments": segments}
    ))
    good = _visual(tmp_path, "good.mp4")
    rejected = _visual(tmp_path, "rejected.mp4")
    rejected["verification"] = {"accepted": False}
    failed = _visual(tmp_path, "failed.mp4")
    failed["_download_error"] = "no_segment_evidence"
    scenes = [{
        "scene": {"scene_number": 1},
        "scene_number": 1,
        "duration_seconds": 10,
        "videos": [good],
        "images": [],
        "sentences": [
            {"sentence_number": 1, "sentence_text": "One.",
             "videos": [rejected, good], "images": []},
            {"sentence_number": 2, "sentence_text": "Two.",
             "videos": [failed], "images": []},
        ],
    }]
    timeline = build_project_timeline(
        project_id, scenes, output_dir=str(tmp_path / "media")
    )
    items = timeline["scenes"][0]["items"]
    used_paths = {i.get("file_path") or i.get("path") for i in items}
    assert str(tmp_path / "rejected.mp4") not in used_paths
    assert str(tmp_path / "failed.mp4") not in used_paths
    # Sentence 1 falls through to the good asset in its own list.
    first = [i for i in items if i["sentence_index"] == 0][0]
    assert first["relevance_status"] == "verified_for_sentence"
