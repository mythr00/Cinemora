"""Wave 1 captions upgrade (plan E): true ASS karaoke rendering,
theme-driven style, derived syllable mode, and the syllabifier.

Contracts pinned here:
(a) word mode: one Dialogue event per readable chunk (<=2 lines,
    ~40 chars/line), \\kf centisecond sums == event durations;
(b) syllable mode: per-syllable \\k tags over the measured word
    spans + the ``; SyllableTiming: derived`` header stamp;
(c) sync="off": zero karaoke tags;
(d) a provisional artefact renders plain even in word mode;
(e) syllabifier known words / exceptions / single-syllable words;
(f) a real burn: mid-word frame shows accent pixels in the caption
    zone, a frame before the word starts does not.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from services.caption_renderer import burn_captions, create_ass_file
from services.syllabifier import syllabify, syllable_spans


def _write_captions(tmp_path, data):
    path = tmp_path / "captions.json"
    path.write_text(json.dumps(data))
    return path


def _dialogues(ass_path):
    text = Path(ass_path).read_text(encoding="utf-8-sig")
    out = []
    for line in text.splitlines():
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            out.append(
                {"start": parts[1], "end": parts[2], "text": parts[9]}
            )
    return text, out


def _to_cs(timestr):
    h, m, rest = timestr.split(":")
    s, cs = rest.split(".")
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 100 + int(cs)


def _strip_tags(text):
    return re.sub(r"\{[^}]*\}", "", text)


def _timed_words(texts_times):
    words = []
    for text, start, end in texts_times:
        words.append({"word": text, "start": start, "end": end})
    return words


LONG_SEGMENT = {
    "start": 0.0,
    "end": 7.6,
    "text": (
        "The investigation into the documentary evidence "
        "revealed a hidden pattern that nobody had expected "
        "to find there"
    ),
    "words": _timed_words([
        ("The", 0.0, 0.4), ("investigation", 0.4, 1.2),
        ("into", 1.2, 1.6), ("the", 1.6, 1.9),
        ("documentary", 1.9, 2.7), ("evidence", 2.7, 3.4),
        ("revealed", 3.4, 4.0), ("a", 4.0, 4.2),
        ("hidden", 4.2, 4.6), ("pattern", 4.6, 5.0),
        ("that", 5.0, 5.3), ("nobody", 5.3, 5.8),
        ("had", 5.8, 6.0), ("expected", 6.0, 6.6),
        ("to", 6.6, 6.8), ("find", 6.8, 7.2),
        ("there", 7.2, 7.6),
    ]),
}


# ---------------------------------------------------------------
# (a) word-mode ASS structure
# ---------------------------------------------------------------

def test_word_mode_chunked_karaoke_structure(tmp_path):
    captions = _write_captions(tmp_path, {
        "provisional": False,
        "segments": [LONG_SEGMENT],
    })
    ass = tmp_path / "captions.ass"
    create_ass_file(str(captions), str(ass), sync="word")
    _full, dialogues = _dialogues(ass)

    word_count = len(LONG_SEGMENT["words"])
    # One event per chunk -- emphatically not one per word.
    assert 1 < len(dialogues) < word_count
    for event in dialogues:
        duration_cs = _to_cs(event["end"]) - _to_cs(event["start"])
        kf_values = [int(v) for v in re.findall(r"\\kf(\d+)", event["text"])]
        assert kf_values, "word mode must carry \\kf tags"
        assert abs(sum(kf_values) - duration_cs) <= 1
        lines = _strip_tags(event["text"]).split(r"\N")
        assert len(lines) <= 2
        for line in lines:
            assert len(line) <= 40
    # Chunk events span first-word start to last-word end overall.
    assert _to_cs(dialogues[0]["start"]) == 0
    assert _to_cs(dialogues[-1]["end"]) == 760
    # Themed style: bundled DejaVu, documentary-readable size.
    full = Path(ass).read_text(encoding="utf-8-sig")
    assert "DejaVu Sans" in full


def test_word_mode_theme_colours(tmp_path):
    captions = _write_captions(tmp_path, {
        "provisional": False,
        "segments": [LONG_SEGMENT],
    })
    ass = tmp_path / "captions.ass"
    theme = {
        "name": "custom", "accent": (198, 40, 40),
        "text": (240, 238, 232), "muted": (150, 148, 142),
    }
    create_ass_file(str(captions), str(ass), sync="word", theme=theme)
    full = Path(ass).read_text(encoding="utf-8-sig")
    style = [l for l in full.splitlines() if l.startswith("Style:")][0]
    # Primary = accent (198,40,40) as &H00BBGGRR; Secondary = text.
    assert "&H002828C6" in style
    assert "&H00E8EEF0" in style


# ---------------------------------------------------------------
# (b) syllable mode
# ---------------------------------------------------------------

def test_syllable_mode_tags_and_derived_stamp(tmp_path):
    segment = {
        "start": 0.0, "end": 2.5,
        "text": "documentary investigation",
        "words": _timed_words([
            ("documentary", 0.0, 1.0), ("investigation", 1.0, 2.5),
        ]),
    }
    captions = _write_captions(tmp_path, {
        "provisional": False, "segments": [segment],
    })
    ass = tmp_path / "captions.ass"
    create_ass_file(str(captions), str(ass), sync="syllable")
    full, dialogues = _dialogues(ass)

    assert "; SyllableTiming: derived" in full
    assert len(dialogues) == 1
    text = dialogues[0]["text"]
    assert r"\kf" not in text
    k_values = [int(v) for v in re.findall(r"\\k(?!f)(\d+)", text)]
    expected = len(syllabify("documentary")) + len(
        syllabify("investigation")
    )
    assert len(k_values) == expected
    duration_cs = _to_cs(dialogues[0]["end"]) - _to_cs(dialogues[0]["start"])
    assert abs(sum(k_values) - duration_cs) <= 1
    # Per-word syllable sums track the words' measured spans
    # (documentary: 1.0s of the event, investigation: the rest,
    # including its run to the event end).
    n_doc = len(syllabify("documentary"))
    assert abs(sum(k_values[:n_doc]) - 100) <= n_doc
    assert abs(sum(k_values[n_doc:]) - 150) <= len(k_values) - n_doc
    assert _strip_tags(text).replace(" ", "") == "documentaryinvestigation"


# ---------------------------------------------------------------
# (c) off mode
# ---------------------------------------------------------------

def test_off_mode_has_no_karaoke_tags(tmp_path):
    captions = _write_captions(tmp_path, {
        "provisional": False, "segments": [LONG_SEGMENT],
    })
    ass = tmp_path / "captions.ass"
    create_ass_file(str(captions), str(ass), sync="off")
    _full, dialogues = _dialogues(ass)
    assert len(dialogues) == 1
    assert r"\k" not in dialogues[0]["text"]
    assert "investigation" in dialogues[0]["text"]


# ---------------------------------------------------------------
# (d) provisional renders plain
# ---------------------------------------------------------------

def test_provisional_renders_plain_even_in_word_mode(tmp_path):
    captions = _write_captions(tmp_path, {
        "provisional": True, "segments": [LONG_SEGMENT],
    })
    ass = tmp_path / "captions.ass"
    create_ass_file(str(captions), str(ass), sync="word")
    _full, dialogues = _dialogues(ass)
    assert dialogues
    for event in dialogues:
        assert r"\k" not in event["text"]
    assert "investigation" in dialogues[0]["text"]


def test_provisional_syllable_mode_has_no_derived_stamp(tmp_path):
    captions = _write_captions(tmp_path, {
        "provisional": True, "segments": [LONG_SEGMENT],
    })
    ass = tmp_path / "captions.ass"
    create_ass_file(str(captions), str(ass), sync="syllable")
    full = Path(ass).read_text(encoding="utf-8-sig")
    assert "; SyllableTiming: derived" not in full
    assert r"\k" not in full.split("[Events]")[1]


# ---------------------------------------------------------------
# (e) syllabifier
# ---------------------------------------------------------------

@pytest.mark.parametrize("word,count", [
    ("documentary", 5),
    ("investigation", 5),
    ("water", 2),
    ("evidence", 3),
    ("analysis", 4),
    ("syllable", 3),
])
def test_syllabify_known_words(word, count):
    parts = syllabify(word)
    assert len(parts) == count
    assert "".join(parts) == word


def test_syllabify_exact_splits():
    assert syllabify("water") == ["wa", "ter"]
    assert syllabify("hello") == ["hel", "lo"]


@pytest.mark.parametrize("word,parts", [
    ("business", ["busi", "ness"]),
    ("every", ["ev", "ery"]),
    ("family", ["fam", "ily"]),
    ("comfortable", ["com", "forta", "ble"]),
    ("chocolate", ["choco", "late"]),
    ("wednesday", ["wednes", "day"]),
])
def test_syllabify_exceptions(word, parts):
    assert syllabify(word) == parts


@pytest.mark.parametrize("word", ["strength", "world", "the", "make"])
def test_syllabify_single_syllable(word):
    assert syllabify(word) == [word]


def test_syllable_spans_contiguous_and_exact():
    spans = syllable_spans("documentary", 1.0, 2.0)
    assert len(spans) == 5
    assert spans[0][1] == 1.0
    assert spans[-1][2] == 2.0
    for prev, nxt in zip(spans, spans[1:]):
        assert prev[2] == nxt[1]
    total = sum(end - start for _s, start, end in spans)
    assert total == pytest.approx(1.0)
    # Weighted by character length: "ter" (3) outlasts "wa" (2).
    wa, ter = syllable_spans("water", 0.0, 1.0)
    assert ter[2] - ter[1] > wa[2] - wa[1]


# ---------------------------------------------------------------
# (f) real burn: accent sweep visible mid-word only
# ---------------------------------------------------------------

def test_burn_karaoke_accent_pixels_mid_word(tmp_path):
    video = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
            "-i", "color=c=black:size=1920x1080:duration=4:rate=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
        ],
        check=True, timeout=120,
    )
    captions = _write_captions(tmp_path, {
        "provisional": False,
        "segments": [{
            "start": 1.0, "end": 3.0, "text": "Hello world",
            "words": _timed_words([
                ("Hello", 1.0, 2.0), ("world", 2.0, 3.0),
            ]),
        }],
    })
    ass = tmp_path / "captions.ass"
    create_ass_file(str(captions), str(ass), sync="word")
    burned = tmp_path / "burned.mp4"
    burn_captions(str(video), str(ass), str(burned))
    assert burned.exists()

    from PIL import Image

    def accent_pixels(moment, name):
        frame = tmp_path / name
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-ss", str(moment),
                "-i", str(burned), "-frames:v", "1", str(frame),
            ],
            check=True, timeout=60,
        )
        im = Image.open(frame).convert("RGB")
        zone = im.crop((0, 800, im.width, im.height))
        count = 0
        for r, g, b in zone.getdata():
            # editorial_dark accent (240,176,64): warm amber --
            # high red, mid green, low blue (white text excluded).
            if r > 150 and 100 < g < 220 and b < 120 and r - b > 80:
                count += 1
        return count

    before = accent_pixels(0.5, "before.png")
    mid_word = accent_pixels(1.5, "mid.png")
    assert before == 0
    assert mid_word > 20
