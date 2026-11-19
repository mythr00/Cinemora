"""Stage 3I: captions consolidation (AUD-27/MA-19).

Root cause pinned by these tests: narration's proportional segments
carry "words": [] and caption_renderer used to build dialogue ONLY
from segment["words"] — a header-only ASS that "succeeded" and
burned zero captions. captions.build_captions is now the single
writer (Whisper stamped non-provisional; proportional fallback
stamped provisional) and create_ass_file renders word-less segments
and RAISES on zero dialogue events.
"""

import json
import re
import subprocess
import wave
from pathlib import Path

import pytest

from services import captions
from services.caption_renderer import burn_captions, create_ass_file
from services import renderer


def _make_wav(path, seconds=4.0, rate=22050):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def _whisper_result():
    return {
        "audio_file": "x",
        "language": "en",
        "language_probability": 0.99,
        "duration": 4.0,
        "segments": [
            {
                "start": 0.0,
                "end": 2.0,
                "text": "Alpha bravo charlie.",
                "words": [
                    {"word": "Alpha", "start": 0.0, "end": 0.6},
                    {"word": "bravo", "start": 0.6, "end": 1.2},
                    {"word": "charlie.", "start": 1.2, "end": 2.0},
                ],
            },
            {
                "start": 2.0,
                "end": 4.0,
                "text": "Delta echo foxtrot.",
                "words": [
                    {"word": "Delta", "start": 2.0, "end": 2.6},
                    {"word": "echo", "start": 2.6, "end": 3.2},
                    {"word": "foxtrot.", "start": 3.2, "end": 4.0},
                ],
            },
        ],
    }


SCRIPT = "Alpha bravo charlie. Delta echo foxtrot."


def _parse_srt(text):
    blocks = [b for b in text.strip().split("\n\n") if b.strip()]
    parsed = []
    for block in blocks:
        lines = block.strip().splitlines()
        assert lines[0].isdigit()
        match = re.match(
            r"(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})",
            lines[1],
        )
        assert match, f"bad SRT timing line: {lines[1]}"
        parsed.append((int(lines[0]), match.group(1), match.group(2)))
    return parsed


def _parse_vtt(text):
    assert text.startswith("WEBVTT")
    timings = re.findall(
        r"(\d{2}:\d{2}:\d{2}\.\d{3}) --> (\d{2}:\d{2}:\d{2}\.\d{3})", text
    )
    assert timings
    return timings


# ---------------------------------------------------------------
# build_captions: Whisper path
# ---------------------------------------------------------------

def test_whisper_timings_survive_and_are_stamped(tmp_path, monkeypatch):
    audio = _make_wav(tmp_path / "narration.wav")
    monkeypatch.setattr(
        captions, "transcribe_audio", lambda *a, **kw: _whisper_result()
    )
    out_json = tmp_path / "captions.json"
    data = captions.build_captions(str(audio), SCRIPT, str(out_json))

    assert data["timing_source"] == "whisper"
    assert data["provisional"] is False
    assert data["segments"][0]["words"][0]["word"] == "Alpha"

    on_disk = json.loads(out_json.read_text())
    assert on_disk["timing_source"] == "whisper"
    assert on_disk["provisional"] is False

    srt = _parse_srt((tmp_path / "captions.srt").read_text())
    assert [idx for idx, _, _ in srt] == [1, 2]
    vtt = _parse_vtt((tmp_path / "captions.vtt").read_text())
    assert len(vtt) == 2


# ---------------------------------------------------------------
# build_captions: proportional fallback paths
# ---------------------------------------------------------------

def test_whisper_empty_falls_back_to_provisional(tmp_path, monkeypatch):
    audio = _make_wav(tmp_path / "narration.wav")
    monkeypatch.setattr(
        captions,
        "transcribe_audio",
        lambda *a, **kw: {
            "audio_file": "x", "language": "en",
            "language_probability": 0.0, "duration": 4.0, "segments": [],
        },
    )
    out_json = tmp_path / "captions.json"
    data = captions.build_captions(str(audio), SCRIPT, str(out_json))

    assert data["timing_source"] == "proportional"
    assert data["provisional"] is True
    assert len(data["segments"]) == 2
    assert all(seg["words"] == [] for seg in data["segments"])
    # Segments cover the whole narration, in order.
    assert data["segments"][0]["start"] == 0.0
    assert data["segments"][-1]["end"] == pytest.approx(4.0)

    srt = _parse_srt((tmp_path / "captions.srt").read_text())
    assert [idx for idx, _, _ in srt] == [1, 2]
    _parse_vtt((tmp_path / "captions.vtt").read_text())


def test_whisper_failure_falls_back_to_provisional(tmp_path, monkeypatch):
    audio = _make_wav(tmp_path / "narration.wav")

    def boom(*a, **kw):
        raise RuntimeError("whisper unavailable")

    monkeypatch.setattr(captions, "transcribe_audio", boom)
    data = captions.build_captions(
        str(audio), SCRIPT, str(tmp_path / "captions.json")
    )
    assert data["provisional"] is True
    assert data["timing_source"] == "proportional"


def test_whisper_model_is_lazy_singleton(monkeypatch):
    import faster_whisper

    built = []

    class FakeModel:
        def __init__(self, *a, **kw):
            built.append(1)

    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    monkeypatch.setattr(captions, "_WHISPER_MODEL", None)
    first = captions._get_whisper_model()
    second = captions._get_whisper_model()
    assert first is second
    assert len(built) == 1
    monkeypatch.setattr(captions, "_WHISPER_MODEL", None)


# ---------------------------------------------------------------
# validate_captions
# ---------------------------------------------------------------

def test_validate_captions_ok_and_missing(tmp_path, monkeypatch):
    audio = _make_wav(tmp_path / "narration.wav")
    monkeypatch.setattr(
        captions,
        "transcribe_audio",
        lambda *a, **kw: {
            "audio_file": "x", "language": "en",
            "language_probability": 0.0, "duration": 4.0, "segments": [],
        },
    )
    out_json = tmp_path / "captions.json"
    captions.build_captions(str(audio), SCRIPT, str(out_json))
    result = captions.validate_captions(
        str(out_json), ["Alpha bravo charlie.", "Delta echo foxtrot."]
    )
    assert result["ok"] is True
    assert result["segments"] == 2
    assert result["events"] == 2
    assert result["missing_segments"] == []

    # Captions covering only the first half: sentence 2 uncovered.
    partial = {
        "duration": 4.0,
        "segments": [{"start": 0.0, "end": 1.9, "text": "Alpha.", "words": []}],
    }
    partial_path = tmp_path / "partial.json"
    partial_path.write_text(json.dumps(partial))
    result = captions.validate_captions(
        str(partial_path), ["Alpha bravo charlie.", "Delta echo foxtrot."]
    )
    assert result["ok"] is False
    assert result["missing_segments"] == [1]


# ---------------------------------------------------------------
# create_ass_file
# ---------------------------------------------------------------

def test_ass_from_wordless_segments_produces_dialogue(tmp_path):
    captions_json = tmp_path / "captions.json"
    captions_json.write_text(json.dumps({
        "duration": 4.0,
        "segments": [
            {"start": 0.0, "end": 2.0, "text": "Alpha bravo charlie.", "words": []},
            {"start": 2.0, "end": 4.0, "text": "Delta echo foxtrot.", "words": []},
        ],
    }))
    ass = tmp_path / "captions.ass"
    create_ass_file(str(captions_json), str(ass))
    text = ass.read_text(encoding="utf-8-sig")
    dialogues = [l for l in text.splitlines() if l.startswith("Dialogue:")]
    assert len(dialogues) == 2
    assert "Alpha bravo charlie." in dialogues[0]


def test_ass_from_empty_segments_raises_and_writes_nothing(tmp_path):
    captions_json = tmp_path / "captions.json"
    captions_json.write_text(json.dumps({"duration": 0.0, "segments": []}))
    ass = tmp_path / "captions.ass"
    with pytest.raises(ValueError):
        create_ass_file(str(captions_json), str(ass))
    assert not ass.exists()


def test_ass_from_word_segments_still_works(tmp_path):
    captions_json = tmp_path / "captions.json"
    captions_json.write_text(json.dumps(_whisper_result()))
    ass = tmp_path / "captions.ass"
    create_ass_file(str(captions_json), str(ass))
    dialogues = [
        l for l in ass.read_text(encoding="utf-8-sig").splitlines()
        if l.startswith("Dialogue:")
    ]
    assert len(dialogues) == 6  # one per word


# ---------------------------------------------------------------
# burn: pixels actually change in the caption band
# ---------------------------------------------------------------

def test_burned_captions_change_bottom_band(tmp_path):
    # Tiny generated video (renderer image clip from a lavfi frame).
    pattern = tmp_path / "pattern.png"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=black:size=1280x720:duration=1:rate=1",
            "-frames:v", "1", str(pattern),
        ],
        check=True, timeout=60,
    )
    video = tmp_path / "clip.mp4"
    assert renderer.create_image_clip(
        str(pattern), 2.0, str(video), frame_count=60, motion="zoom_in"
    )

    captions_json = tmp_path / "captions.json"
    captions_json.write_text(json.dumps({
        "duration": 2.0,
        "segments": [
            {"start": 0.0, "end": 2.0, "text": "Hello caption world.", "words": []},
        ],
    }))
    ass = tmp_path / "captions.ass"
    create_ass_file(str(captions_json), str(ass))

    burned = tmp_path / "burned.mp4"
    burn_captions(str(video), str(ass), str(burned))
    assert burned.exists()

    def bottom_band_mean(video_path, name):
        frame = tmp_path / name
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-ss", "1.0",
                "-i", str(video_path), "-frames:v", "1", str(frame),
            ],
            check=True, timeout=60,
        )
        from PIL import Image

        im = Image.open(frame).convert("L")
        band = im.crop((0, int(im.height * 0.75), im.width, im.height))
        px = list(band.getdata())
        return sum(px) / len(px)

    plain_mean = bottom_band_mean(video, "plain.png")
    burned_mean = bottom_band_mean(burned, "burned.png")
    # White caption text on a black background raises the band mean.
    assert burned_mean > plain_mean + 1.0
