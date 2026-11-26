"""Gate 4 media-pipeline seams (Production Recovery, 2026-11-26).

Diagnosis 08 §6–§7 evidenced two defects whose seams live in
media_pipeline's dispatcher / download memory:

1. Cross-beat duplicate content — sentences S03 and S06 of the
   Hyatt run were served byte-identical stills fetched via
   different source pages/URLs. Dedupe keyed on URL; content-hash
   unification only counted within a beat, never blocked across
   beats. The block now sits in _uve_download_any_candidate, the
   commit point every run download passes through.
2. Duplicate download re-attempts — 21 of 57 download-failure
   events were re-attempts of URLs that had already failed in the
   same run. The fetch loop's older guard keys on the candidate's
   page/source identity, so the same image FILE under a different
   page, and a dead video re-presented at a different segment,
   were both re-requested. The dispatcher now keeps per-run
   attempted-URL memory keyed by the actual download URL, using
   the taxonomy's own retryable classification: terminal failures
   are never re-attempted within the run; transient ones keep
   their existing retry policy; a succeeded URL resolves to the
   already-downloaded file.
"""

import pytest

from services import media_pipeline as mp
from services.visual_registry import VisualRegistry


@pytest.fixture(autouse=True)
def _fresh_run_state():
    mp.reset_media_caches()
    yield
    mp.reset_media_caches()


# ---------------------------------------------------------------------------
# Image download stub: per-URL payloads / failures, call-counted.
# ---------------------------------------------------------------------------

def _stub_image_downloads(monkeypatch, payloads, failures=None):
    """payloads: {image_url: bytes}; failures: {image_url: reason}.
    Returns the list of requested URLs (one entry per network attempt)."""
    calls = []

    def fake_download(image_url, output_path):
        calls.append(image_url)
        if failures and image_url in failures:
            return False, failures[image_url]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(payloads[image_url])
        return True, ""

    monkeypatch.setattr(mp, "_download_image_detailed", fake_download)
    monkeypatch.setattr(mp, "probe_image", lambda path: {
        "width": 1920, "height": 1080, "format": "JPEG",
    })
    monkeypatch.setattr(mp, "_image_technical_failures", lambda probe: [])
    return calls


def _image_candidate(page_url, image_url, **extra):
    candidate = {
        "_pool_kind": "image",
        "url": page_url,
        "source_url": page_url,
        "image_url": image_url,
        "provider": "wikimedia",
        "matched_query": "hyatt atrium",
    }
    candidate.update(extra)
    return candidate


PAGE_A = "https://en.wikipedia.org/wiki/Hyatt_Regency_walkway_collapse"
PAGE_B = "https://commons.wikimedia.org/wiki/File:Hyatt_atrium.jpg"
PAGE_C = "https://commons.wikimedia.org/wiki/File:Hyatt_rescue.jpg"
IMG_A = "https://upload.wikimedia.org/wikipedia/en/a/aa/Atrium.jpg"
IMG_B = "https://upload.wikimedia.org/wikipedia/commons/b/bb/Atrium.jpg"
IMG_C = "https://upload.wikimedia.org/wikipedia/commons/c/cc/Rescue.jpg"

SHARED_BYTES = b"the-same-atrium-photograph-bytes" * 64
DISTINCT_BYTES = b"a-genuinely-different-rescue-photograph" * 64


# ---------------------------------------------------------------------------
# Defect 1 — cross-beat duplicate content
# ---------------------------------------------------------------------------

def test_identical_bytes_via_different_urls_blocked_for_second_beat(
    monkeypatch, tmp_path
):
    """The Hyatt S03/S06 shape: two URLs, identical bytes, two
    beats. The second beat's fetch must fall through to its
    next-best distinct candidate — never the identical bytes."""
    _stub_image_downloads(monkeypatch, {
        IMG_A: SHARED_BYTES, IMG_B: SHARED_BYTES, IMG_C: DISTINCT_BYTES,
    })
    registry = VisualRegistry("gate4-dup-content")
    used_keys = set()

    first = mp._uve_fetch_first(
        [_image_candidate(PAGE_A, IMG_A)],
        mp._uve_download_any_candidate, tmp_path,
        sentence_number=3, asset_index=1,
        registry=registry, used_keys=used_keys, failures=[],
    )
    assert first is not None
    assert first["content_hash"]

    dup_candidate = _image_candidate(PAGE_B, IMG_B)
    distinct_candidate = _image_candidate(PAGE_C, IMG_C)
    failures = []
    second = mp._uve_fetch_first(
        [dup_candidate, distinct_candidate],
        mp._uve_download_any_candidate, tmp_path,
        sentence_number=6, asset_index=1,
        registry=registry, used_keys=used_keys, failures=failures,
    )
    assert second is not None
    assert second["image_url"] == IMG_C
    assert second["content_hash"] != first["content_hash"]
    # The duplicate pick was blocked with its typed reason, and its
    # freshly downloaded file was removed — no second copy on disk.
    assert dup_candidate["_download_error"] == "duplicate_content_this_run"
    assert dup_candidate["duplicate_content"]["first_sentence_number"] == 3
    assert any(
        f["reason"] == "duplicate_content_this_run" for f in failures
    )
    on_disk = [p for p in tmp_path.iterdir() if p.is_file()]
    assert len(on_disk) == 2  # beat 3's file + beat 6's distinct file


def test_identical_bytes_with_no_distinct_candidate_is_explicit(
    monkeypatch, tmp_path
):
    """When the duplicate is the second beat's ONLY pick, the
    outcome is an explicit no-distinct-asset result (fetch returns
    None with the duplicate recorded), never the identical bytes."""
    _stub_image_downloads(monkeypatch, {
        IMG_A: SHARED_BYTES, IMG_B: SHARED_BYTES,
    })
    registry = VisualRegistry("gate4-dup-only")
    used_keys = set()
    first = mp._uve_fetch_first(
        [_image_candidate(PAGE_A, IMG_A)],
        mp._uve_download_any_candidate, tmp_path,
        sentence_number=3, asset_index=1,
        registry=registry, used_keys=used_keys, failures=[],
    )
    assert first is not None

    failures = []
    second = mp._uve_fetch_first(
        [_image_candidate(PAGE_B, IMG_B)],
        mp._uve_download_any_candidate, tmp_path,
        sentence_number=6, asset_index=1,
        registry=registry, used_keys=used_keys, failures=failures,
    )
    assert second is None
    assert failures[-1]["reason"] == "duplicate_content_this_run"
    assert failures[-1]["failure_class"] == "duplicate"


def test_same_url_cross_beat_blocked_without_any_download(
    monkeypatch, tmp_path
):
    """The same image URL re-presented to a DIFFERENT beat is
    identical content by construction: blocked at the dispatcher,
    with no second network attempt at all."""
    calls = _stub_image_downloads(monkeypatch, {IMG_A: SHARED_BYTES})
    registry = VisualRegistry("gate4-dup-same-url")
    used_keys = set()
    first = mp._uve_fetch_first(
        [_image_candidate(PAGE_A, IMG_A)],
        mp._uve_download_any_candidate, tmp_path,
        sentence_number=3, asset_index=1,
        registry=registry, used_keys=used_keys, failures=[],
    )
    assert first is not None
    assert calls == [IMG_A]

    candidate = _image_candidate(PAGE_B, IMG_A)
    second = mp._uve_fetch_first(
        [candidate], mp._uve_download_any_candidate, tmp_path,
        sentence_number=6, asset_index=1,
        registry=registry, used_keys=used_keys, failures=[],
    )
    assert second is None
    assert calls == [IMG_A]  # never re-requested
    assert candidate["_download_error"] == "duplicate_content_this_run"


def test_within_beat_reuse_of_same_content_still_allowed(
    monkeypatch, tmp_path
):
    """Within-beat semantics are preserved: the SAME beat
    re-presenting its committed image (a recovery round) is not
    blocked by the cross-beat rule."""
    _stub_image_downloads(monkeypatch, {
        IMG_A: SHARED_BYTES, IMG_B: SHARED_BYTES,
    })
    registry = VisualRegistry("gate4-within-beat")
    used_keys = set()
    first = mp._uve_fetch_first(
        [_image_candidate(PAGE_A, IMG_A)],
        mp._uve_download_any_candidate, tmp_path,
        sentence_number=3, asset_index=1,
        registry=registry, used_keys=used_keys, failures=[],
    )
    assert first is not None
    again = mp._uve_download_any_candidate(
        _image_candidate(PAGE_B, IMG_B), tmp_path,
        sentence_number=3, asset_index=2,
    )
    assert again is not None
    assert again["content_hash"] == first["content_hash"]


# ---------------------------------------------------------------------------
# Defect 2 — per-run attempted-URL memory
# ---------------------------------------------------------------------------

def test_terminally_failed_image_url_not_reattempted_via_other_page(
    monkeypatch, tmp_path
):
    """Same failing image FILE presented via two different source
    pages in two rounds: exactly one network attempt. (The fetch
    loop's page-keyed guard cannot see this; the dispatcher's
    download-URL memory can.)"""
    calls = _stub_image_downloads(
        monkeypatch, {}, failures={IMG_A: "http_error"},
    )
    registry = VisualRegistry("gate4-failed-url")
    used_keys = set()
    failures = []
    first = mp._uve_fetch_first(
        [_image_candidate(PAGE_A, IMG_A)],
        mp._uve_download_any_candidate, tmp_path,
        sentence_number=3, asset_index=1,
        registry=registry, used_keys=used_keys, failures=failures,
    )
    assert first is None

    second_candidate = _image_candidate(PAGE_B, IMG_A)
    second = mp._uve_fetch_first(
        [second_candidate],
        mp._uve_download_any_candidate, tmp_path,
        sentence_number=6, asset_index=1,
        registry=registry, used_keys=used_keys, failures=failures,
    )
    assert second is None
    assert calls == [IMG_A]  # one network attempt across both rounds
    assert (
        second_candidate["_download_error"]
        == "previously_failed_this_run"
    )


def test_transient_image_failure_keeps_existing_retry_behaviour(
    monkeypatch, tmp_path
):
    """A transient failure (timeout — taxonomy retryable) is NOT
    entered into the terminal block: a later round may re-attempt
    it, exactly as the pipeline behaved before this memory."""
    calls = _stub_image_downloads(
        monkeypatch, {}, failures={IMG_A: "timeout"},
    )
    candidate1 = _image_candidate(PAGE_A, IMG_A)
    assert mp._uve_download_any_candidate(
        candidate1, tmp_path, sentence_number=3, asset_index=1,
    ) is None
    candidate2 = _image_candidate(PAGE_B, IMG_A)
    assert mp._uve_download_any_candidate(
        candidate2, tmp_path, sentence_number=6, asset_index=1,
    ) is None
    assert calls == [IMG_A, IMG_A]  # re-attempted: transient, not terminal
    assert candidate2["_download_error"] == "timeout"


def test_successful_image_url_resolves_to_downloaded_file(
    monkeypatch, tmp_path
):
    """Same successful URL presented twice for the same beat: one
    download, two references to the already-downloaded file."""
    calls = _stub_image_downloads(monkeypatch, {IMG_A: SHARED_BYTES})
    registry = VisualRegistry("gate4-success-url")
    used_keys = set()
    first = mp._uve_fetch_first(
        [_image_candidate(PAGE_A, IMG_A)],
        mp._uve_download_any_candidate, tmp_path,
        sentence_number=3, asset_index=1,
        registry=registry, used_keys=used_keys, failures=[],
    )
    assert first is not None

    second = mp._uve_fetch_first(
        [_image_candidate(PAGE_B, IMG_A)],
        mp._uve_download_any_candidate, tmp_path,
        sentence_number=3, asset_index=2,
        registry=registry, used_keys=used_keys, failures=[],
    )
    assert calls == [IMG_A]  # downloaded once
    assert second is not None
    assert second["download_reused"] is True
    assert second["file_path"] == first["file_path"]


def test_terminally_failed_video_url_not_reattempted_at_new_segment(
    monkeypatch, tmp_path
):
    """A video URL that failed terminally (404) is not re-attempted
    when a later round re-presents it at a DIFFERENT segment — the
    case the segment-granular guard structurally cannot catch."""
    starts = iter([30, 90])
    monkeypatch.setattr(
        mp, "_segment_start_detailed",
        lambda url, **kw: (next(starts), "verified_segment"),
    )
    monkeypatch.setattr(
        mp, "_provider_duration_seconds", lambda cand: (None, "")
    )
    calls = []

    def fake_clip_download(url, output_path, start_time=None, duration=None):
        calls.append((url, start_time))
        mp.DOWNLOAD_LAST_ERROR[str(output_path)] = {
            "failure_class": "source_unavailable",
            "reason": "http_error",
            "http_status": 404,
            "check": None,
            "retryable": False,
            "detail": None,
        }
        return False

    monkeypatch.setattr(mp, "download_video_clip", fake_clip_download)
    url = "https://www.youtube.com/watch?v=gate4dead1"
    candidate1 = {"_pool_kind": "video", "url": url, "provider": "youtube"}
    assert mp._uve_download_any_candidate(
        candidate1, tmp_path, sentence_number=3, asset_index=1,
    ) is None
    assert calls == [(url, 30)]

    candidate2 = {"_pool_kind": "video", "url": url, "provider": "youtube"}
    assert mp._uve_download_any_candidate(
        candidate2, tmp_path, sentence_number=6, asset_index=1,
    ) is None
    assert calls == [(url, 30)]  # the 90s segment was never requested
    assert (
        candidate2["_download_error"] == "previously_failed_this_run"
    )


def test_download_memory_is_scoped_to_the_run_context():
    """The new containers live on the RunContext like every other
    per-run store: a fresh context starts empty, and the module
    globals rebind to it on activation."""
    from services import run_context

    mp._DOWNLOAD_URL_FAILURES["k"] = {"retryable": False}
    fresh = run_context.RunContext()
    assert fresh.download_url_failures == {}
    assert fresh.downloaded_assets_by_url == {}
    assert fresh.committed_content_owners == {}
    with run_context.use_context(fresh):
        assert mp._DOWNLOAD_URL_FAILURES is fresh.download_url_failures
        assert mp._DOWNLOADED_ASSETS_BY_URL is fresh.downloaded_assets_by_url
        assert mp._COMMITTED_CONTENT_OWNERS is fresh.committed_content_owners
    assert mp._DOWNLOAD_URL_FAILURES["k"] == {"retryable": False}
