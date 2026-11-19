"""VA-12 / VA-13 regression tests: technical minimums and image
failure reason codes.

Technical rejection is a separate judgement from relevance: the
recorded facts and failed checks must travel with the candidate, and
the rejection must never be confused with a gate/verifier rejection.
Image downloads must report WHY they failed, like the video path.
"""

import io

import pytest
from PIL import Image

from services import media_pipeline as mp


def _png_bytes(size):
    buf = io.BytesIO()
    Image.new("RGB", size, (120, 40, 200)).save(buf, format="PNG")
    # noise so the file clears the 1000-byte floor like real photos
    import random

    random.seed(7)
    img = Image.effect_noise(size, 40).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _image_candidate(tmp_path, monkeypatch, payload, size=(640, 480)):
    if payload == "ok":
        data = _png_bytes(size)
        monkeypatch.setattr(
            mp, "download_image",
            lambda url, path: open(path, "wb").write(data),
        )
    return {
        "url": "https://example.org/pic.jpg",
        "image_url": "https://example.org/pic.jpg",
        "provider": "web",
    }


def test_image_http_error_reason(monkeypatch, tmp_path):
    def boom(url, path):
        raise RuntimeError("404 Client Error")

    monkeypatch.setattr(mp, "download_image", boom)
    candidate = {"image_url": "https://example.org/x.jpg", "provider": "web"}
    assert mp._uve_download_image_candidate(candidate, tmp_path, 1, 0) is None
    assert candidate["_download_error"] == "http_error"
    assert candidate["rejection_stage"] == "download"


def test_image_rate_limited_reason(monkeypatch, tmp_path):
    def boom(url, path):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(mp, "download_image", boom)
    candidate = {"image_url": "https://example.org/x.jpg", "provider": "web"}
    assert mp._uve_download_image_candidate(candidate, tmp_path, 1, 0) is None
    assert candidate["_download_error"] == "rate_limited"


def test_image_decode_failed_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(
        mp, "download_image",
        lambda url, path: open(path, "wb").write(b"x" * 5000),
    )
    candidate = {"image_url": "https://example.org/x.jpg", "provider": "web"}
    assert mp._uve_download_image_candidate(candidate, tmp_path, 1, 0) is None
    assert candidate["_download_error"] == "decode_failed"
    assert candidate["rejection_stage"] == "technical_validation"


def test_image_undersized_technical_rejection(monkeypatch, tmp_path):
    candidate = _image_candidate(tmp_path, monkeypatch, "ok", size=(100, 80))
    assert mp._uve_download_image_candidate(candidate, tmp_path, 1, 0) is None
    assert candidate["_download_error"] == "technical_below_minimum"
    rejection = candidate["_technical_rejection"]
    assert "width_below_minimum" in rejection["failed"]
    assert rejection["facts"]["width"] == 100
    assert candidate["rejection_stage"] == "technical_validation"


def test_image_at_minimum_accepted_with_technical_checks(monkeypatch, tmp_path):
    candidate = _image_candidate(tmp_path, monkeypatch, "ok", size=(320, 240))
    asset = mp._uve_download_image_candidate(candidate, tmp_path, 1, 0)
    assert asset is not None
    assert asset["technical_checks"]["passed"] is True
    assert asset["technical_checks"]["facts"]["width"] == 320


def test_video_technical_failures_unit():
    minimums = mp._technical_minimums()
    good = {"duration_seconds": 4.0, "width": 640, "height": 360}
    assert mp._video_technical_failures(good, minimums) == []
    tiny = {"duration_seconds": 0.4, "width": 160, "height": 120}
    failures = mp._video_technical_failures(tiny, minimums)
    assert "duration_below_minimum" in failures
    assert "width_below_minimum" in failures
    assert "height_below_minimum" in failures
    # No fps check exists: a low-fps archival probe passes.
    archival = {"duration_seconds": 5.0, "width": 320, "height": 240, "fps": 12.0}
    assert mp._video_technical_failures(archival, minimums) == []


def test_video_technical_rejection_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(mp, "_segment_start_detailed", lambda url, **k: (0, "whole_asset"))
    monkeypatch.setattr(
        mp, "download_video_clip",
        lambda *a, **k: open(a[1], "wb").write(b"v" * 20000) or True,
    )
    monkeypatch.setattr(mp, "validate_video", lambda path: True)
    monkeypatch.setattr(mp, "probe_video", lambda path: {
        "duration_seconds": 3.0, "width": 160, "height": 120,
        "fps": 25.0, "codec": "h264", "has_audio": False,
        "has_video": True, "size_bytes": 20000,
    })
    candidate = {"url": "https://example.org/v.mp4", "provider": "web"}
    assert mp._uve_download_video_candidate(candidate, tmp_path, 1, 0) is None
    assert candidate["_download_error"] == "technical_below_minimum"
    assert candidate["_technical_rejection"]["facts"]["width"] == 160
    assert candidate["rejection_stage"] == "technical_validation"


def test_download_valid_image_wrapper_still_works(monkeypatch, tmp_path):
    data = _png_bytes((320, 240))
    monkeypatch.setattr(
        mp, "download_image", lambda url, path: open(path, "wb").write(data)
    )
    out = tmp_path / "ok.png"
    assert mp.download_valid_image("https://example.org/a.png", out) is True
    assert out.exists()
