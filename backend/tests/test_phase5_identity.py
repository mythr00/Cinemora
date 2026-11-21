"""Phase 5 (Master Directive) — the ONE visual identity.

services.visual_identity is the single distinct-visual definition
behind AM-017's canonical headline count and the dedupe-by-source
-image cheap win: canonical/segment ladder first, content-hash
unification for stills whose canonical identities differ but whose
downloaded bytes are identical, file fallback last. These tests
pin the ladder and the unification rules directly.
"""

import hashlib

from services import visual_identity as vi


WIKIMEDIA = "https://upload.wikimedia.org/wikipedia/commons/x/S.jpg"
WIKIMEDIA_THUMB = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/x/S.jpg/320px-S.jpg"
)
OTHER_SOURCE = "https://images.example.org/mirror/S-copy.jpg"
YT = "https://www.youtube.com/watch?v=abc123XYZ_-"


def _still(path, url, **extra):
    record = {
        "type": "image", "file_path": str(path), "path": str(path),
        "image_url": url, "source_url": url,
    }
    record.update(extra)
    return record


def _video(path, url=YT, start=30.0, duration=8.0, **extra):
    record = {
        "type": "video", "file_path": str(path), "path": str(path),
        "video_url": url, "source_url": url,
        "clip_start": start, "duration_seconds": duration,
    }
    record.update(extra)
    return record


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------

def test_still_counts_by_canonical_source_not_file(tmp_path):
    a = _still(tmp_path / "a.jpg", WIKIMEDIA)
    b = _still(tmp_path / "b.jpg", WIKIMEDIA_THUMB)
    # Same underlying source (original vs thumbnail URL) under two
    # different files: ONE identity.
    assert vi.base_identity_key(a) == vi.base_identity_key(b)
    assert vi.base_identity_key(a).startswith("asset:")


def test_video_counts_by_segment_window(tmp_path):
    a = _video(tmp_path / "a.mp4")
    twin = _video(tmp_path / "b.mp4", url="https://youtu.be/abc123XYZ_-")
    other_window = _video(tmp_path / "c.mp4", start=100.0)
    assert vi.base_identity_key(a) == vi.base_identity_key(twin)
    assert vi.base_identity_key(a).startswith("segment:")
    assert vi.base_identity_key(a) != vi.base_identity_key(other_window)


def test_stamped_segment_id_wins(tmp_path):
    record = _video(tmp_path / "a.mp4", segment_id="seg_stamped")
    assert vi.base_identity_key(record) == "segment:seg_stamped"


def test_asset_level_counts_a_video_by_source_not_window(tmp_path):
    # The AM-017 headline level: two windows of one clip are ONE
    # distinct visual (the frame audit counts assets — Apollo's
    # Moon clip is one of its 7 visuals), while at the segment
    # (dealing) level they stay two cards.
    a = _video(tmp_path / "a.mp4", start=0.0)
    b = _video(tmp_path / "b.mp4", start=100.0)
    assert vi.base_identity_key(a) != vi.base_identity_key(b)
    assert vi.asset_identity_key(a) == vi.asset_identity_key(b)
    assert vi.asset_identity_key(a).startswith("asset:")
    assert len(vi.distinct_asset_keys([a, b])) == 1
    # A still's two levels agree (whole source either way).
    still = _still(tmp_path / "s.jpg", WIKIMEDIA)
    assert vi.asset_identity_key(still) == vi.base_identity_key(still)


def test_graphic_counts_by_file_and_identityless_falls_back(tmp_path):
    graphic = {
        "type": "video", "motion_graphic": True,
        "file_path": "/p/motion_graphics/g.mp4",
    }
    assert vi.base_identity_key(graphic) == "file:/p/motion_graphics/g.mp4"
    bare = {"type": "image"}
    assert vi.base_identity_key(bare, fallback="fb") == "fb"
    assert vi.base_identity_key(bare) == ""


# ---------------------------------------------------------------------------
# Content-hash unification (stills)
# ---------------------------------------------------------------------------

def test_identical_bytes_unify_despite_different_canonical_sources(tmp_path):
    digest = hashlib.sha256(b"the same photo").hexdigest()
    a = _still(tmp_path / "a.jpg", WIKIMEDIA, content_hash=digest)
    b = _still(tmp_path / "b.jpg", OTHER_SOURCE, content_hash=digest)
    # The ladder alone separates them (different canonical sources)…
    assert vi.base_identity_key(a) != vi.base_identity_key(b)
    # …but identical bytes make them ONE visual everywhere.
    groups = vi.resolve_identity_groups([a, b])
    assert groups[0] == groups[1]
    assert len(vi.distinct_identity_keys([a, b])) == 1


def test_different_bytes_stay_distinct(tmp_path):
    a = _still(
        tmp_path / "a.jpg", WIKIMEDIA,
        content_hash=hashlib.sha256(b"one").hexdigest(),
    )
    b = _still(
        tmp_path / "b.jpg", OTHER_SOURCE,
        content_hash=hashlib.sha256(b"two").hexdigest(),
    )
    assert len(vi.distinct_identity_keys([a, b])) == 2


def test_hash_supplied_by_callback_when_no_stamp(tmp_path):
    # measure_project's route: stored items carry no stamp, the
    # caller supplies hashes read from the files on disk.
    a = _still(tmp_path / "a.jpg", WIKIMEDIA)
    b = _still(tmp_path / "b.jpg", OTHER_SOURCE)
    digest = hashlib.sha256(b"same").hexdigest()
    groups = vi.resolve_identity_groups(
        [a, b], hash_of=lambda record: digest
    )
    assert groups[0] == groups[1]


def test_videos_never_hash_unify(tmp_path):
    # A video's identity is its source window, not its container
    # bytes: identical bytes under two DIFFERENT sources remain
    # two visuals (a re-download of the SAME segment unifies via
    # the ladder instead — see test_video_counts_by_segment_window).
    digest = hashlib.sha256(b"same container").hexdigest()
    a = _video(tmp_path / "a.mp4", content_hash=digest)
    b = _video(
        tmp_path / "b.mp4", url="https://www.youtube.com/watch?v=ZZZ999yyy_-",
        content_hash=digest,
    )
    assert len(vi.distinct_identity_keys([a, b])) == 2


def test_graphics_never_hash_unify(tmp_path):
    digest = hashlib.sha256(b"same render").hexdigest()
    a = {
        "type": "video", "motion_graphic": True,
        "file_path": "/p/motion_graphics/a.mp4", "content_hash": digest,
    }
    b = dict(a, file_path="/p/motion_graphics/b.mp4")
    assert len(vi.distinct_identity_keys([a, b])) == 2


def test_identityless_records_never_silently_merge():
    groups = vi.resolve_identity_groups([{"type": "image"}, {"type": "image"}])
    assert groups[0] != groups[1]


def test_content_hash_for_file(tmp_path):
    path = tmp_path / "x.bin"
    path.write_bytes(b"abc")
    assert vi.content_hash_for_file(path) == hashlib.sha256(b"abc").hexdigest()
    assert vi.content_hash_for_file(tmp_path / "missing.bin") is None
    assert vi.content_hash_for_file("") is None


def test_video_window_stamp_and_fallbacks():
    assert vi.video_window({"clip_start": 5, "clip_end": 9}) == (5.0, 9.0)
    assert vi.video_window(
        {"clip_start": 5, "clip_duration": 3}
    ) == (5.0, 8.0)
    assert vi.video_window({"clip_start": 5, "duration": 2}) == (5.0, 7.0)
    assert vi.video_window({}) == (0.0, None)
