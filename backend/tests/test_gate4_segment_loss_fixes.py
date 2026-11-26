"""Gate 4 segment-loss fixes (Production Recovery, diagnosis 12).

Shapes taken from Hyatt test run 4 (3d8d33bc), where the asset
stage accepted ZERO video segments:

A. Every attempted segment download died inside download_video_clip:
   9 of the 12 leftover ``*_source.*`` intermediates were complete,
   valid 8s clips whose yt-dlp wrapper was killed at the 60s
   ceiling around/after the bytes arrived, and were discarded
   solely for the wrapper's timeout. A timeout that leaves a valid
   file covering the requested window now promotes that file
   through the normal normalisation path; a timeout that leaves
   only a truncated stub (the run's 801-byte bot-check remnants) or
   a short partial section still fails honestly as
   download_failed/timeout, and the dead intermediate is cleaned
   up stem-scoped.

B. The one clip that completed a download — the run's best video,
   0.888 — was rejected as channel_card_frame on line_counts
   [0, 1, 0]: a single OCR line in one of three sampled frames.
   The channel-card detector now needs more than a single stray
   line of evidence in total; a genuine transient card (multiple
   lines, or recurring across frames) still rejects.

C. The B2 place/date-only guard built its place-token set solely
   from bible entities; the Hyatt bible has no "United States"
   entity, so an eBay coin listing titled "UNITED STATES 1981"
   scored topical overlap 1.00 and was accepted as related for
   sentence 5. The scenario's own place and a static country
   gazetteer now count as place tokens for the guard; a candidate
   with genuine subject content (the run's 0.628 newspaper-headline
   image shape) still passes.
"""

import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from services import frame_verification as fv
from services import media_pipeline
from services import story_understanding as story


# ------------------------------------------------------------- A. salvage

def _make_section_video(path, seconds=4):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i",
            f"testsrc2=size=320x240:rate=10:duration={seconds}",
            "-t", str(seconds), "-c:v", "libx264", "-preset",
            "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
    )
    return Path(path)


def _fake_timeout_fetch(section_writer):
    """A run_command_safe stand-in: the yt-dlp call writes whatever
    section_writer leaves at the output template, then reports the
    wrapper timeout kill (-999); the normalisation ffmpeg call runs
    for real."""
    real_run = media_pipeline.run_command_safe

    def fake_run(command, timeout, label):
        if label == "yt-dlp":
            template = command[command.index("-o") + 1]
            section_writer(template)
            return -999, "", "yt-dlp timed out"
        return real_run(command, timeout, label)

    return fake_run


def test_timeout_with_valid_section_file_promotes_segment(monkeypatch, tmp_path):
    section = _make_section_video(tmp_path / "section.mp4", seconds=4)

    def write_section(template):
        target = Path(template.replace("%(ext)s", "mp4"))
        target.write_bytes(section.read_bytes())

    monkeypatch.setattr(
        media_pipeline, "run_command_safe", _fake_timeout_fetch(write_section)
    )
    out = tmp_path / "clip.mp4"
    ok = media_pipeline.download_video_clip(
        "https://www.youtube.com/watch?v=abc123", out, start_time=95, duration=4
    )
    assert ok is True
    probe = media_pipeline.probe_video(out)
    assert probe is not None
    assert probe["duration_seconds"] >= 3.5
    provenance = media_pipeline.DOWNLOAD_PROVENANCE[str(out)]
    assert provenance["method"] == "yt-dlp-download-sections"
    assert provenance["salvaged_after_timeout"] is True


def test_timeout_with_truncated_stub_still_fails(monkeypatch, tmp_path):
    def write_stub(template):
        target = Path(template.replace("%(ext)s", "webm"))
        target.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 797)  # 801-byte stub

    monkeypatch.setattr(
        media_pipeline, "run_command_safe", _fake_timeout_fetch(write_stub)
    )
    out = tmp_path / "clip.mp4"
    ok = media_pipeline.download_video_clip(
        "https://www.youtube.com/watch?v=abc123", out, start_time=79, duration=4
    )
    assert ok is False
    error = media_pipeline.DOWNLOAD_LAST_ERROR[str(out)]
    assert error["failure_class"] == "timeout"
    # The dead intermediate is cleaned up (F4), not left to be
    # miscounted as a landed download.
    assert not list(tmp_path.glob("*_source.*"))


def test_timeout_with_short_partial_section_still_fails(monkeypatch, tmp_path):
    section = _make_section_video(tmp_path / "partial.mp4", seconds=1)

    def write_partial(template):
        target = Path(template.replace("%(ext)s", "mp4"))
        target.write_bytes(section.read_bytes())

    monkeypatch.setattr(
        media_pipeline, "run_command_safe", _fake_timeout_fetch(write_partial)
    )
    out = tmp_path / "clip.mp4"
    ok = media_pipeline.download_video_clip(
        "https://www.youtube.com/watch?v=abc123", out, start_time=0, duration=4
    )
    # A valid but 1s file does not cover the requested 4s window.
    assert ok is False
    assert media_pipeline.DOWNLOAD_LAST_ERROR[str(out)]["failure_class"] == "timeout"


# ------------------------------------------------------- B. channel card

def _photo():
    # The phase-1 tests' gradient photo shape: textured enough that
    # no other detector (fill / motion / story card) fires on it.
    import numpy as np

    yy, xx = np.mgrid[0:720, 0:1280]
    rng = np.random.default_rng(3)
    base = 120 - yy * 0.05 + np.sin(xx / 200.0) * 10 + rng.normal(
        0, 3, (720, 1280)
    )
    return Image.fromarray(
        np.clip(base, 0, 255).astype("uint8")
    ).convert("RGB")


_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _font(size):
    if not Path(_FONT_PATH).exists():
        pytest.skip("DejaVu font unavailable for synthetic frames")
    return ImageFont.truetype(_FONT_PATH, size)


def _card_frame(lines):
    frame = _photo()
    draw = ImageDraw.Draw(frame)
    for index, text in enumerate(lines):
        draw.text((200, 250 + index * 170), text, fill=(245, 245, 245),
                  font=_font(130))
    return frame


def test_single_stray_line_in_one_frame_is_not_a_channel_card():
    # Run 4's exact shape: line_counts [0, 1, 0] on the run's best
    # video. One OCR line in one sampled frame is incidental
    # on-screen text, not a channel card.
    record = fv.verify_frames(
        [_photo(), _card_frame(["MARRON"]), _photo()], kind="video",
        scenario=None, bible=None, ocr_text=None,
    )
    assert record["detectors"]["channel_card"]["line_counts"] == [0, 1, 0]
    assert record["detectors"]["channel_card"]["fired"] is False
    assert record["outcome"] == "pass"


def test_genuine_channel_card_still_rejected():
    # A real transient card: multiple display lines in the card
    # frame (channel name + call to action), absent elsewhere.
    record = fv.verify_frames(
        [_photo(), _card_frame(["MARRON", "SUBSCRIBE"]), _photo()],
        kind="video", scenario=None, bible=None, ocr_text=None,
    )
    assert record["detectors"]["channel_card"]["fired"] is True
    assert record["outcome"] == "reject"
    assert record["reason"] == fv.CHANNEL_CARD_REASON


# ------------------------------------------------- C. B2 place/date guard

def _hyatt_bible_without_us():
    # The run's bible: places are the hotel, its atrium and Kansas
    # City — there is NO "United States" entity.
    return story.hydrate_bible({
        "entities": [
            {"id": "collapse", "canonical": "Hyatt Regency walkway collapse",
             "aliases": ["Kansas City collapse", "collapse", "walkway collapse"],
             "type": "event"},
            {"id": "hotel", "canonical": "Hyatt Regency Kansas City",
             "aliases": ["Hyatt Regency hotel", "the hotel"], "type": "place"},
            {"id": "kc", "canonical": "Kansas City, Missouri",
             "aliases": ["Kansas City"], "type": "place"},
        ]
    })


def _s5_scenario(**over):
    sc = {
        "subject_id": "collapse",
        "specificity": "representative",
        "search_terms": ["United States 1981"],
        "visual": "United States 1981",
        "era": "1981",
        "place": "United States",
    }
    sc.update(over)
    return sc


def test_coin_listing_place_date_only_rejected_without_bible_us_entity():
    # The run's accepted S5 asset: an eBay banknote listing whose
    # title offers nothing beyond the country and the year.
    verdict = story.verify_candidate(
        {
            "title": "UNITED STATES 1981. 1 DOLLAR. COLLECTOR'S "
                     "MISPRINT NOTE .RARE EXTRA FLAP .  aEF | eBay",
            "snippet": "UNITED STATES 1981 one dollar collector note",
        },
        _s5_scenario(),
        _hyatt_bible_without_us(),
    )
    assert verdict["accepted"] is False
    assert verdict["rejection_reason"] == "no_topical_evidence"
    assert "topical evidence is place/date only" in verdict["reasons"]


def test_newspaper_headline_genuine_content_still_passes():
    # The counter-example shape: the run's 0.628 Rare Newspapers
    # headline image — genuine subject content beyond place/date.
    verdict = story.verify_candidate(
        {
            "title": "1981 Hyatt Regency walkway collapse newspaper "
                     "headline | RareNewspapers.com",
            "snippet": "Original 1981 newspaper reporting the Hyatt "
                       "Regency walkway collapse in Kansas City, "
                       "the deadliest structural failure in United "
                       "States history at the time",
        },
        _s5_scenario(
            search_terms=[
                "1981 newspaper Hyatt collapse headline United States",
            ],
            visual="A newspaper headline from 1981 describing the "
                   "Hyatt Regency collapse as the worst structural "
                   "disaster in American history",
        ),
        _hyatt_bible_without_us(),
    )
    assert verdict["accepted"] is True
    assert verdict["contributions"]["subject"] > 0
