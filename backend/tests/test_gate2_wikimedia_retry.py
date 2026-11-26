"""Gate 2 — Wikimedia retry-before-backoff in the production
download paths of media_pipeline (footage-discovery directive).

provider_health + licensed_media already gave Wikimedia Commons
scopes bounded delayed retries before a 429 reaches the backoff
accounting (sibling workstream, commit 743553a), but the paths
that actually download production assets — _download_image_detailed
(stills; Commons thumburls live on upload.wikimedia.org),
download_direct_video (Commons video derivatives are direct files
on upload.wikimedia.org) and the yt-dlp extraction in
download_video_clip (Commons page URLs) — still recorded a 429
immediately and never retried. That was the Container run's
dominant download loss (31/36 failures wikimedia-labelled).

Pinned here:
- a Wikimedia 429-then-200 image download SUCCEEDS, with the
  delayed retry recorded and NO rate limit recorded;
- a persistent Wikimedia 429 still records the rate limit —
  exactly once, after the bounded retries are spent;
- a non-Wikimedia 429 is byte-identical to the old behaviour:
  no retry, recorded immediately;
- the same retry-before-record shape holds on the direct-video
  streamed path and the yt-dlp extraction path.
"""

import requests
from PIL import Image

from services import media_pipeline as mp
from services import provider_health


class _Resp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def _http_error(status_code, headers=None):
    error = requests.HTTPError(f"HTTP {status_code}")
    error.response = _Resp(status_code, headers)
    return error


class _HealthSpy:
    """Replaces provider_health's recording entry points so the
    test never touches Redis-backed shared state."""

    def __init__(self, monkeypatch):
        self.delayed = []
        self.rate_limits = []
        monkeypatch.setattr(
            provider_health, "record_delayed_retry",
            lambda scope, delay: self.delayed.append((scope, delay)),
        )
        monkeypatch.setattr(
            provider_health, "record_rate_limit",
            lambda scope: self.rate_limits.append(scope),
        )


def _no_sleep(monkeypatch):
    monkeypatch.setattr(mp._time, "sleep", lambda seconds: None)


WIKIMEDIA_IMAGE = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/"
    "a/ab/Example.jpg/640px-Example.jpg"
)


# ---------------------------------------------------------------------------
# Image path (_download_image_detailed)
# ---------------------------------------------------------------------------

def test_wikimedia_image_429_then_success(monkeypatch, tmp_path):
    spy = _HealthSpy(monkeypatch)
    _no_sleep(monkeypatch)
    calls = []

    def fake_download(url, path):
        calls.append(url)
        if len(calls) == 1:
            raise _http_error(429, {"Retry-After": "30"})
        Image.new("RGB", (64, 64), "red").save(path, format="PNG")
        # Pad past the 1000-byte floor with a second save of a
        # larger canvas if needed.
        import os
        if os.path.getsize(path) < 1000:
            Image.new("RGB", (400, 400), "blue").save(path, format="PNG")

    monkeypatch.setattr(mp, "download_image", fake_download)
    ok, reason = mp._download_image_detailed(
        WIKIMEDIA_IMAGE, tmp_path / "img.png"
    )
    assert (ok, reason) == (True, "")
    assert len(calls) == 2
    # One delayed retry, recorded, with the Retry-After honoured
    # but capped at the Wikimedia maximum — and NO rate limit was
    # recorded for a burst the retry rode out.
    assert len(spy.delayed) == 1
    scope, delay = spy.delayed[0]
    assert scope == "upload.wikimedia.org"
    assert delay == provider_health.WIKIMEDIA_MAX_RETRY_DELAY_SECONDS
    assert spy.rate_limits == []


def test_wikimedia_image_persistent_429_records_rate_limit(
    monkeypatch, tmp_path
):
    spy = _HealthSpy(monkeypatch)
    _no_sleep(monkeypatch)
    calls = []

    def fake_download(url, path):
        calls.append(url)
        raise _http_error(429)

    monkeypatch.setattr(mp, "download_image", fake_download)
    ok, reason = mp._download_image_detailed(
        WIKIMEDIA_IMAGE, tmp_path / "img.png"
    )
    assert (ok, reason) == (False, "rate_limited")
    # 1 initial attempt + the bounded delayed retries, then the
    # rate limit is recorded exactly once (per exhausted request,
    # the licensed_media shape).
    assert len(calls) == 1 + provider_health.WIKIMEDIA_RETRY_ATTEMPTS
    assert len(spy.delayed) == provider_health.WIKIMEDIA_RETRY_ATTEMPTS
    assert spy.rate_limits == ["upload.wikimedia.org"]


def test_non_wikimedia_image_429_is_never_retried(monkeypatch, tmp_path):
    spy = _HealthSpy(monkeypatch)
    _no_sleep(monkeypatch)
    calls = []

    def fake_download(url, path):
        calls.append(url)
        raise _http_error(429)

    monkeypatch.setattr(mp, "download_image", fake_download)
    ok, reason = mp._download_image_detailed(
        "https://media.example.org/pic.jpg", tmp_path / "img.png"
    )
    assert (ok, reason) == (False, "rate_limited")
    assert len(calls) == 1
    assert spy.delayed == []
    assert spy.rate_limits == ["media.example.org"]


# ---------------------------------------------------------------------------
# Direct-video streamed path (download_direct_video)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield b"x" * 20000


class _FakeSession:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def get(self, url, timeout=None, stream=False):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _patch_direct_video_io(monkeypatch):
    """ffmpeg range-seek fails (so the streamed path runs); the
    trim succeeds; validation always passes."""
    def fake_run(command, timeout, label):
        if any(str(part).startswith("http") for part in command):
            return 1, "", "range seek unsupported"
        return 0, "", ""

    monkeypatch.setattr(mp, "run_command_safe", fake_run)
    monkeypatch.setattr(mp, "validate_video", lambda path: True)


def test_wikimedia_direct_video_429_then_success(monkeypatch, tmp_path):
    spy = _HealthSpy(monkeypatch)
    _no_sleep(monkeypatch)
    _patch_direct_video_io(monkeypatch)
    session = _FakeSession([_http_error(429), _FakeResponse()])
    monkeypatch.setattr(mp, "_http", lambda: session)
    ok = mp.download_direct_video(
        "https://upload.wikimedia.org/wikipedia/commons/transcoded/"
        "a/ab/Clip.webm/Clip.webm.480p.vp9.webm",
        tmp_path / "clip.mp4",
        duration=8,
        start_time=0,
    )
    assert ok is True
    assert session.calls == 2
    assert len(spy.delayed) == 1
    assert spy.delayed[0][0] == "upload.wikimedia.org"
    assert spy.rate_limits == []


def test_wikimedia_direct_video_persistent_429_records(
    monkeypatch, tmp_path
):
    spy = _HealthSpy(monkeypatch)
    _no_sleep(monkeypatch)
    _patch_direct_video_io(monkeypatch)
    session = _FakeSession([_http_error(429)] * 5)
    monkeypatch.setattr(mp, "_http", lambda: session)
    ok = mp.download_direct_video(
        "https://upload.wikimedia.org/wikipedia/commons/transcoded/"
        "a/ab/Clip.webm/Clip.webm.480p.vp9.webm",
        tmp_path / "clip.mp4",
        duration=8,
        start_time=0,
    )
    assert ok is False
    # 1 initial attempt + the bounded retries, then recorded once.
    assert session.calls == 1 + provider_health.WIKIMEDIA_RETRY_ATTEMPTS
    assert len(spy.delayed) == provider_health.WIKIMEDIA_RETRY_ATTEMPTS
    assert spy.rate_limits == ["upload.wikimedia.org"]


def test_non_wikimedia_direct_video_429_is_never_retried(
    monkeypatch, tmp_path
):
    spy = _HealthSpy(monkeypatch)
    _no_sleep(monkeypatch)
    _patch_direct_video_io(monkeypatch)
    session = _FakeSession([_http_error(429), _FakeResponse()])
    monkeypatch.setattr(mp, "_http", lambda: session)
    ok = mp.download_direct_video(
        "https://media.example.org/clip.mp4",
        tmp_path / "clip.mp4",
        duration=8,
        start_time=0,
    )
    assert ok is False
    assert session.calls == 1
    assert spy.delayed == []
    assert spy.rate_limits == ["media.example.org"]


# ---------------------------------------------------------------------------
# yt-dlp extraction path (download_video_clip)
#
# Routing note (verified while writing these tests): a Commons
# File: page whose URL ends in a media extension (.webm/.mp4) is
# classified as a DIRECT media URL and takes the
# download_direct_video path above; the yt-dlp path serves
# Commons URLs without a media extension, like the one below.
# Both paths carry the retry, so both shapes are covered.
# ---------------------------------------------------------------------------

def _patch_ytdlp(monkeypatch, stderrs):
    state = {"extraction_calls": 0, "queue": list(stderrs)}

    def fake_run(command, timeout, label):
        if any("yt_dlp" in str(part) for part in command):
            state["extraction_calls"] += 1
            stderr = state["queue"].pop(0) if state["queue"] else ""
            return (1, "", stderr) if stderr else (0, "", "")
        return 0, "", ""

    monkeypatch.setattr(mp, "run_command_safe", fake_run)
    return state


def test_wikimedia_ytdlp_429_retried_before_recording(
    monkeypatch, tmp_path
):
    spy = _HealthSpy(monkeypatch)
    _no_sleep(monkeypatch)
    state = _patch_ytdlp(monkeypatch, [
        "ERROR: HTTP Error 429: Too Many Requests",
        "ERROR: HTTP Error 403: Forbidden",
    ])
    ok = mp.download_video_clip(
        "https://commons.wikimedia.org/wiki/File:Example",
        tmp_path / "clip.mp4",
        start_time=0,
        duration=8,
    )
    assert ok is False
    # The 429 was retried once with a recorded delay; the final
    # failure is a 403, so no rate limit is recorded at all.
    assert state["extraction_calls"] == 2
    assert len(spy.delayed) == 1
    assert spy.delayed[0][0] == "commons.wikimedia.org"
    assert spy.rate_limits == []


def test_wikimedia_ytdlp_persistent_429_records_once(
    monkeypatch, tmp_path
):
    spy = _HealthSpy(monkeypatch)
    _no_sleep(monkeypatch)
    state = _patch_ytdlp(monkeypatch, [
        "ERROR: HTTP Error 429: Too Many Requests",
    ] * 5)
    ok = mp.download_video_clip(
        "https://commons.wikimedia.org/wiki/File:Example",
        tmp_path / "clip.mp4",
        start_time=0,
        duration=8,
    )
    assert ok is False
    assert state["extraction_calls"] == (
        1 + provider_health.WIKIMEDIA_RETRY_ATTEMPTS
    )
    assert len(spy.delayed) == provider_health.WIKIMEDIA_RETRY_ATTEMPTS
    assert spy.rate_limits == ["commons.wikimedia.org"]


def test_non_wikimedia_ytdlp_429_is_never_retried(monkeypatch, tmp_path):
    spy = _HealthSpy(monkeypatch)
    _no_sleep(monkeypatch)
    state = _patch_ytdlp(monkeypatch, [
        "ERROR: HTTP Error 429: Too Many Requests",
    ] * 3)
    ok = mp.download_video_clip(
        "https://www.youtube.com/watch?v=abc123XYZ",
        tmp_path / "clip.mp4",
        start_time=0,
        duration=8,
    )
    assert ok is False
    assert state["extraction_calls"] == 1
    assert spy.delayed == []
    assert spy.rate_limits == ["youtube.com"]
