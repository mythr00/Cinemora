"""Gate 4 / B7 regression: burned caption TEXT is the script's words.

The Gate 4 inspection of the Hyatt test film found burned captions
carrying ASR corruption — "at 705 pm" for "7:05 p.m.", "1 ,600",
"friday t -dance", "The Kansas City collapsed changed" — because
_build_captions_measured built caption events by joining the raw
Whisper words of the aligned span. The script text sent to TTS is
known exactly, so ASR may supply TIMINGS only, never wording.

These tests feed a deliberately corrupted Whisper transcript whose
timings are usable and assert the caption artefact (and the ASS
file rendered from it for burning) still carries the script's
wording byte-for-byte, including number/time/hyphen formatting.
"""

import json
import re
import wave
from pathlib import Path

from services import captions
from services.caption_renderer import create_ass_file


def _make_wav(path, seconds, rate=22050):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def _write_parts(project_dir, durations):
    project_dir = Path(project_dir)
    parts_dir = project_dir / "narration_parts"
    parts = []
    for index, seconds in enumerate(durations):
        name = f"part_{index:03d}.wav"
        _make_wav(parts_dir / name, seconds)
        parts.append(
            {
                "index": index,
                "file": name,
                "hash": f"hash-{index}",
                "sentence_sha256": f"sha-{index}",
                "measured_duration_seconds": float(seconds),
                "duration_source": "ffprobe_part",
            }
        )
    manifest = {"version": 2, "model": "fixture", "parts": parts}
    (parts_dir / "parts_manifest.json").write_text(json.dumps(manifest))
    return manifest


def _word(text, start, end):
    return {"word": text, "start": start, "end": end}


SCRIPT_SENTENCES = [
    (
        "On the evening of July 17, 1981, more than 1,600 people "
        "gathered for a Friday tea dance."
    ),
    (
        "At 7:05 p.m., the fourth-floor walkway's box beams tore free."
    ),
    "The Kansas City collapse changed structural engineering forever.",
]
SCRIPT = " ".join(SCRIPT_SENTENCES)
DURATIONS = [6.0, 4.0, 4.0]


def _corrupted_whisper():
    """ASR transcript with Gate-4-grade corruption but clean,
    usable word timings spanning each measured window."""
    return {
        "audio_file": "x",
        "language": "en",
        "language_probability": 0.99,
        "duration": sum(DURATIONS),
        "segments": [
            {
                "start": 0.0, "end": 6.0,
                "text": "corrupted",
                "words": [
                    _word(w, i * 0.4, i * 0.4 + 0.4)
                    for i, w in enumerate([
                        "On", "the", "evening", "of", "July", "17,",
                        "1981,", "more", "than", "1", ",600", "people",
                        "gathered", "for", "friday", "t", "-dance",
                    ])
                ],
            },
            {
                "start": 6.0, "end": 10.0,
                "text": "corrupted",
                "words": [
                    _word(w, 6.0 + i * 0.4, 6.0 + i * 0.4 + 0.4)
                    for i, w in enumerate([
                        "at", "705", "pm", "the", "fourth", "floor",
                        "walkways", "box", "beams", "tore", "free",
                    ])
                ],
            },
            {
                "start": 10.0, "end": 14.0,
                "text": "corrupted",
                "words": [
                    _word(w, 10.0 + i * 0.45, 10.0 + i * 0.45 + 0.45)
                    for i, w in enumerate([
                        "The", "Kansas", "City", "collapsed", "changed",
                        "structural", "engineering", "forever",
                    ])
                ],
            },
        ],
    }


def _build(tmp_path, monkeypatch):
    project_dir = tmp_path / "proj"
    _write_parts(project_dir, DURATIONS)
    _make_wav(project_dir / "narration.wav", sum(DURATIONS))
    whisper = _corrupted_whisper()
    monkeypatch.setattr(
        captions, "transcribe_audio", lambda *a, **kw: whisper
    )
    return captions.build_captions(
        str(project_dir / "narration.wav"),
        SCRIPT,
        str(project_dir / "captions.json"),
    ), project_dir


def test_caption_text_is_script_despite_corrupted_asr(tmp_path, monkeypatch):
    data, project_dir = _build(tmp_path, monkeypatch)

    # The corrupted transcript must not leak ANY of its distinctive
    # errors into a caption event or word.
    all_text = " ".join(segment["text"] for segment in data["segments"])
    all_words = [
        word["word"]
        for segment in data["segments"]
        for word in segment["words"]
    ]
    for corrupted in ("705", "pm", "t -dance", "collapsed",
                      "walkways", "friday", "1 ,600"):
        assert corrupted not in all_text
    for corrupted in ("705", "pm", ",600", "t", "-dance", "collapsed",
                      "walkways", "friday"):
        assert corrupted not in all_words

    # Concatenating each sentence's events reproduces the script
    # sentence EXACTLY (wording, punctuation, capitalisation).
    windows = [(0.0, 6.0), (6.0, 10.0), (10.0, 14.0)]
    for sentence, (start, end) in zip(SCRIPT_SENTENCES, windows):
        events = [
            s for s in data["segments"]
            if start <= s["start"] < end or s["text"] == sentence
        ]
        joined = " ".join(s["text"] for s in events)
        assert joined == sentence

    # Timings still come from the alignment: events stay inside the
    # measured windows and carry word timings (word sync intact).
    assert data["captions_alignment"]["alignment_failed"] == 0
    for segment in data["segments"]:
        assert segment["words"], "word-sync events must carry words"
        for word in segment["words"]:
            assert 0.0 <= word["start"] <= word["end"] <= 14.0

    # The file on disk — the artefact the burn path reads — agrees.
    on_disk = json.loads((project_dir / "captions.json").read_text())
    assert " ".join(s["text"] for s in on_disk["segments"]) == all_text
    srt = (project_dir / "captions.srt").read_text()
    assert "7:05 p.m." in srt
    assert "1,600" in srt


def test_number_time_and_hyphen_formatting_render_intact(tmp_path, monkeypatch):
    data, project_dir = _build(tmp_path, monkeypatch)

    words = [
        word["word"]
        for segment in data["segments"]
        for word in segment["words"]
    ]
    # Script tokens survive as single, unsplit words.
    for token in ("1,600", "7:05", "p.m.,", "fourth-floor",
                  "walkway's", "tea", "dance.", "collapse"):
        assert token in words, token
    # No detached punctuation / split fragments anywhere.
    for fragment in (",600", "-dance", "t", "705"):
        assert fragment not in words

    # The ASS file the burner consumes: strip karaoke tags and
    # line-break markers and the script wording is intact.
    ass = project_dir / "captions.ass"
    create_ass_file(str(project_dir / "captions.json"), str(ass), sync="word")
    raw = ass.read_text(encoding="utf-8-sig")
    dialogues = [
        line.split(",", 9)[9]
        for line in raw.splitlines()
        if line.startswith("Dialogue:")
    ]
    rendered = " ".join(
        re.sub(r"\{[^}]*\}", "", d).replace(r"\N", " ") for d in dialogues
    )
    rendered = re.sub(r"\s+", " ", rendered)
    for expected in ("more than 1,600 people", "At 7:05 p.m.,",
                     "the fourth-floor walkway's box beams",
                     "for a Friday tea dance.",
                     "The Kansas City collapse changed"):
        assert expected in rendered, expected
    assert "1 ,600" not in rendered
    assert "705 pm" not in rendered


def test_standalone_path_also_uses_script_text(tmp_path, monkeypatch):
    # No parts manifest next to the narration: the standalone
    # Whisper-segment path must obey the same text rule.
    audio = _make_wav(tmp_path / "narration.wav", sum(DURATIONS))
    whisper = _corrupted_whisper()
    monkeypatch.setattr(
        captions, "transcribe_audio", lambda *a, **kw: whisper
    )
    data = captions.build_captions(
        str(audio), SCRIPT, str(tmp_path / "captions.json")
    )
    assert data["timing_source"] == "whisper"
    all_text = " ".join(segment["text"] for segment in data["segments"])
    assert all_text == SCRIPT
    assert "705" not in all_text
