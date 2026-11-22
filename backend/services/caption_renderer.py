"""Caption ASS rendering + burning (Wave 1 captions upgrade, plan E).

Rendering contract:
- True ASS karaoke: ONE Dialogue event per readable caption chunk
  (re-chunked from a segment's words to at most 2 lines of ~40
  characters, word boundaries only) -- never one event per word.
- sync="word" (default): each word is emitted as {\\kf<cs>}word
  (libass smooth fill sweep). The \\kf centisecond values inside an
  event sum exactly to the event's duration in centiseconds; the
  last word absorbs rounding. A word's effective span runs to the
  next word's start, so inter-word gaps are accounted for and the
  sweep reaches each word exactly when it is spoken.
- sync="syllable": per-syllable {\\k<cs>} tags built from
  services.syllabifier over each word's MEASURED span. Syllable
  timing is DERIVED from measured word spans, never measured; the
  ASS header carries the stamp ``; SyllableTiming: derived``.
- sync="off": plain rendering, no karaoke tags, for every segment.

Colours come from the production theme token dict
(services.motion_engine.themes shape): PrimaryColour = accent
(the swept/spoken colour), SecondaryColour = text (upcoming
words). Colour-state honesty: libass karaoke has exactly TWO
states inside one event -- Secondary before the sweep, Primary
after it. There is no third in-event state, so the theme's
``muted`` token cannot be shown for already-sung words within the
same event; it is deliberately NOT faked (no per-word colour
overrides fighting the sweep). Already-sung and currently-sweeping
words both read as Primary (accent).

Honesty rules preserved from Stage 3I (MA-19):
- Segments without words render as one plain event per segment.
- A captions artefact stamped provisional (captions.py stamps a
  top-level ``provisional`` flag on the document; a per-segment
  flag is honoured too) renders PLAIN regardless of sync mode.
- A header-only ASS is a hard failure: raise, write nothing.
"""
import json
import subprocess
from pathlib import Path

from services.atomic_write import write_text_atomic
from services.syllabifier import syllable_spans

MAX_LINE_CHARS = 40
MAX_LINES_PER_CHUNK = 2
FONT_NAME = "DejaVu Sans"
FONT_SIZE = 56

_VALID_SYNC = ("word", "syllable", "off")


def run_ffmpeg(command):
    print("\nRunning FFmpeg:")
    print(" ".join(str(x) for x in command))

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    print(result.stdout)

    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed with exit code {result.returncode}"
        )


def _ass_colour(rgb):
    """(r, g, b) tuple -> ASS &H00BBGGRR colour literal."""
    try:
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    except (TypeError, ValueError, IndexError):
        r, g, b = 255, 255, 255
    return f"&H00{b:02X}{g:02X}{r:02X}"


def _resolve_theme(theme):
    """Theme token dict: caller dict, theme name, or the
    editorial_dark default when no theme is reachable."""
    from services.motion_engine import themes as themes_module

    if isinstance(theme, dict):
        return theme
    if isinstance(theme, str):
        resolved = themes_module.resolve_theme(theme)
        if resolved is not None:
            return resolved
    return themes_module.THEMES[themes_module.DEFAULT_THEME]


def _escape_ass(text):
    text = str(text or "")
    text = text.replace("{", "(").replace("}", ")")
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return text


def _is_provisional(data, segment=None):
    if isinstance(data, dict) and data.get("provisional"):
        return True
    if isinstance(segment, dict) and segment.get("provisional"):
        return True
    return False


def _chunk_words(words):
    """Re-chunk words into readable chunks: a list of chunks, each
    a list of lines, each line a list of word records. Lines hold
    ~MAX_LINE_CHARS characters at word boundaries (a single word
    longer than the limit sits alone on its line); a chunk holds
    at most MAX_LINES_PER_CHUNK lines."""
    lines = []
    current = []
    current_len = 0
    for word in words:
        word_text = str(word.get("word") or "")
        added = len(word_text) if not current else len(word_text) + 1
        if current and current_len + added > MAX_LINE_CHARS:
            lines.append(current)
            current = []
            current_len = 0
            added = len(word_text)
        current.append(word)
        current_len += added
    if current:
        lines.append(current)
    return [
        lines[i:i + MAX_LINES_PER_CHUNK]
        for i in range(0, len(lines), MAX_LINES_PER_CHUNK)
    ]


def _effective_spans(chunk_words, event_end):
    """(start, end) per word: a word's span runs to the next word's
    start (absorbing inter-word gaps), the last to the event end."""
    spans = []
    for i, word in enumerate(chunk_words):
        start = float(word.get("start", 0.0))
        if i + 1 < len(chunk_words):
            nxt = float(chunk_words[i + 1].get("start", start))
            end = nxt if nxt > start else float(word.get("end", start))
        else:
            end = float(event_end)
        if end < start:
            end = start
        spans.append((start, end))
    return spans


def _centiseconds(spans, event_start, event_end):
    """Centisecond durations for spans, summing EXACTLY to the
    event duration in centiseconds (last entry absorbs rounding)."""
    event_cs = int(round((float(event_end) - float(event_start)) * 100))
    values = [
        int(round((float(end) - float(start)) * 100))
        for start, end in spans
    ]
    if values:
        values[-1] += event_cs - sum(values)
        if values[-1] < 1:
            # Degenerate timings: keep every tag positive and move
            # the deficit onto the largest earlier entry if any.
            deficit = 1 - values[-1]
            values[-1] = 1
            for i in range(len(values) - 2, -1, -1):
                if values[i] > deficit:
                    values[i] -= deficit
                    break
    return values


def _karaoke_text_word(chunk, spans, event_start, event_end):
    flat = [w for line in chunk for w in line]
    cs_values = _centiseconds(spans, event_start, event_end)
    parts = []
    idx = 0
    for line_no, line in enumerate(chunk):
        for word_no, word in enumerate(line):
            parts.append(
                f"{{\\kf{cs_values[idx]}}}"
                f"{_escape_ass(word.get('word') or '')}"
            )
            idx += 1
            if word_no < len(line) - 1:
                parts.append(" ")
        if line_no < len(chunk) - 1:
            parts.append(r"\N")
    return "".join(parts)


def _karaoke_text_syllable(chunk, spans, event_start, event_end):
    entries = []  # (text, start, end)
    flat_index = 0
    for line_no, line in enumerate(chunk):
        for word_no, word in enumerate(line):
            start, end = spans[flat_index]
            flat_index += 1
            word_text = str(word.get("word") or "")
            syl_spans = syllable_spans(word_text, start, end)
            if not syl_spans:
                syl_spans = [(word_text, start, end)]
            for syl_no, (syl, s_start, s_end) in enumerate(syl_spans):
                suffix = ""
                if syl_no == len(syl_spans) - 1:
                    if word_no < len(line) - 1:
                        suffix = " "
                    elif line_no < len(chunk) - 1:
                        suffix = r"\N"
                entries.append((syl + suffix, s_start, s_end))
    cs_values = _centiseconds(
        [(s, e) for _t, s, e in entries], event_start, event_end
    )
    return "".join(
        f"{{\\k{cs}}}{_escape_ass(text)}"
        for (text, _s, _e), cs in zip(entries, cs_values)
    )


def create_ass_file(
    captions_file,
    output_file,
    sync="word",
    theme=None,
):
    captions_file = Path(captions_file)
    output_file = Path(output_file)

    sync = str(sync or "word").lower()
    if sync not in _VALID_SYNC:
        sync = "word"

    with open(
        captions_file,
        "r",
        encoding="utf-8",
    ) as file:
        data = json.load(file)

    document_provisional = _is_provisional(data)
    effective_sync = "off" if document_provisional else sync

    tokens = _resolve_theme(theme)
    primary = _ass_colour(tokens.get("accent"))
    secondary = _ass_colour(tokens.get("text"))

    lines = []

    lines.append("[Script Info]")
    lines.append("ScriptType: v4.00+")
    if effective_sync == "syllable":
        # Syllable timing is DERIVED from measured word spans
        # (plan E.3) -- stamped in the artefact, never "measured".
        lines.append("; SyllableTiming: derived")
    lines.append("PlayResX: 1920")
    lines.append("PlayResY: 1080")
    lines.append("ScaledBorderAndShadow: yes")
    lines.append("")

    lines.append("[V4+ Styles]")
    lines.append(
        "Format: Name, Fontname, Fontsize, PrimaryColour, "
        "SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, "
        "Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding"
    )

    lines.append(
        f"Style: Default,{FONT_NAME},{FONT_SIZE},"
        f"{primary},"
        f"{secondary},"
        "&H00000000,"
        "&H99000000,"
        "1,0,0,0,100,100,0,0,1,3,2,2,80,80,110,1"
    )

    lines.append("")

    lines.append("[Events]")
    lines.append(
        "Format: Layer, Start, End, Style, Name, "
        "MarginL, MarginR, MarginV, Effect, Text"
    )

    dialogue_count = 0

    for segment in data.get("segments") or []:

        words = [
            w for w in (segment.get("words") or [])
            if isinstance(w, dict) and str(w.get("word") or "").strip()
        ]
        segment_plain = effective_sync == "off" or _is_provisional(
            data, segment
        )

        if not words or segment_plain:
            # MA-19 plain events: word-less segments, sync="off",
            # and provisional artefacts all render one plain event
            # per segment from its own start/end/text.
            text = str(segment.get("text") or "").strip()
            if not text and words:
                text = " ".join(
                    str(w.get("word") or "") for w in words
                ).strip()
            if not text:
                continue
            start_time = format_ass_time(float(segment.get("start", 0)))
            end_time = format_ass_time(float(segment.get("end", 0)))
            lines.append(
                f"Dialogue: 0,"
                f"{start_time},"
                f"{end_time},"
                f"Default,,"
                f"0,0,0,0,"
                f"{_escape_ass(text)}"
            )
            dialogue_count += 1
            continue

        for chunk in _chunk_words(words):
            flat = [w for line in chunk for w in line]
            event_start = float(flat[0].get("start", 0.0))
            event_end = float(flat[-1].get("end", event_start))
            if event_end <= event_start:
                event_end = event_start + 0.1
            spans = _effective_spans(flat, event_end)
            if effective_sync == "syllable":
                text = _karaoke_text_syllable(
                    chunk, spans, event_start, event_end
                )
            else:
                text = _karaoke_text_word(
                    chunk, spans, event_start, event_end
                )
            lines.append(
                f"Dialogue: 0,"
                f"{format_ass_time(event_start)},"
                f"{format_ass_time(event_end)},"
                f"Default,,"
                f"0,0,0,0,"
                f"{text}"
            )
            dialogue_count += 1

    # Stage 3I: a header-only ASS is a FAILURE, not a success. Raise
    # before writing anything so no empty ASS file is left behind
    # for the caller's exists() check to mistake for success.
    if dialogue_count == 0:
        raise ValueError(
            "Captions contain no dialogue events: every segment was "
            "empty or missing text/words. Refusing to write a "
            "header-only ASS file."
        )

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    write_text_atomic(
        output_file, "\n".join(lines), encoding="utf-8-sig"
    )

    print()
    print("ASS CAPTIONS CREATED")
    print(f"Dialogue events: {dialogue_count}")
    print(f"Sync: {effective_sync}")
    print(f"Output: {output_file}")


def format_ass_time(seconds):
    hours = int(seconds // 3600)

    minutes = int(
        (seconds % 3600) // 60
    )

    secs = int(
        seconds % 60
    )

    centiseconds = int(
        round(
            (seconds - int(seconds))
            * 100
        )
    )

    if centiseconds >= 100:
        centiseconds = 0
        secs += 1

    if secs >= 60:
        secs = 0
        minutes += 1

    if minutes >= 60:
        minutes = 0
        hours += 1

    return (
        f"{hours}:"
        f"{minutes:02d}:"
        f"{secs:02d}."
        f"{centiseconds:02d}"
    )


def burn_captions(
    video_file,
    ass_file,
    output_file,
):
    video_file = Path(video_file)
    ass_file = Path(ass_file)
    output_file = Path(output_file)

    if not video_file.exists():
        raise FileNotFoundError(
            f"Video not found: {video_file}"
        )

    if not ass_file.exists():
        raise FileNotFoundError(
            f"ASS file not found: {ass_file}"
        )

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # FFmpeg's ASS filter treats ":" as an option separator.
    # Use a path relative to the backend working directory whenever possible.
    try:
        ass_filter_path = ass_file.resolve().relative_to(
            Path.cwd().resolve()
        ).as_posix()
    except ValueError:
        ass_filter_path = ass_file.resolve().as_posix()
        ass_filter_path = ass_filter_path.replace(":", r"\:")

    ass_filter = f"ass={ass_filter_path}"

    # When the style names the bundled DejaVu face, point libass at
    # the bundled fonts directory so the bundled font is actually
    # used instead of whatever fontconfig happens to substitute.
    try:
        ass_text = ass_file.read_text(encoding="utf-8-sig")
    except OSError:
        ass_text = ""
    if FONT_NAME in ass_text:
        from services.motion_engine.styles import FONT

        fonts_dir = Path(FONT).resolve().parent.as_posix()
        ass_filter = f"{ass_filter}:fontsdir={fonts_dir}"

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_file),
        "-vf",
        ass_filter,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "20",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(output_file),
    ]

    run_ffmpeg(command)

    print()
    print("========================================")
    print("CAPTIONS BURNED")
    print("========================================")
    print(f"Output: {output_file}")


if __name__ == "__main__":

    project_dir = Path(
        "media/15e0f37d-c3a3-4001-a801-e9f7007cb627"
    )

    captions_json = (
        project_dir / "captions.json"
    )

    ass_file = (
        project_dir / "captions.ass"
    )

    input_video = (
        project_dir / "documentary_final.mp4"
    )

    output_video = (
        project_dir / "documentary_captioned.mp4"
    )

    create_ass_file(
        captions_json,
        ass_file,
    )

    burn_captions(
        input_video,
        ass_file,
        output_video,
    )
