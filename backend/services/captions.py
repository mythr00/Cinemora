import json
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

MODEL_SIZE = "base"
DEVICE = "cpu"
COMPUTE_TYPE = "int8"

# Stage 3I: the Whisper model is a lazy module-level singleton —
# previously every transcribe_audio call constructed a new
# WhisperModel (a full model load per call).
_WHISPER_MODEL = None


def _get_whisper_model():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel

        _WHISPER_MODEL = WhisperModel(
            MODEL_SIZE,
            device=DEVICE,
            compute_type=COMPUTE_TYPE,
        )
    return _WHISPER_MODEL


def transcribe_audio(
    audio_file: str,
    output_json: str | None = None,
):
    audio_path = Path(audio_file)

    if not audio_path.exists():
        raise FileNotFoundError(
            f"Audio file not found: {audio_path}"
        )

    print()
    print("========================================")
    print("GENERATING WORD-LEVEL CAPTIONS")
    print("========================================")
    print(f"Audio: {audio_path}")
    print(f"Model: {MODEL_SIZE}")
    print(f"Device: {DEVICE}")
    print()

    print("Loading Whisper model...")

    model = _get_whisper_model()

    print("Model loaded.")
    print()
    print("Transcribing...")

    segments, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
    )

    caption_segments = []

    for segment in segments:

        words = []

        for word in segment.words or []:

            words.append({
                "word": word.word.strip(),
                "start": round(word.start, 3),
                "end": round(word.end, 3),
            })

        if not words:
            continue

        caption_segments.append({
            "start": round(segment.start, 3),
            "end": round(segment.end, 3),
            "text": segment.text.strip(),
            "words": words,
        })

    result = {
        "audio_file": str(audio_path),
        "language": info.language,
        "language_probability": info.language_probability,
        "duration": info.duration,
        "segments": caption_segments,
    }

    if output_json is None:
        output_json = str(
            audio_path.parent / "captions.json"
        )

    output_path = Path(output_json)

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with open(
        output_path,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            result,
            file,
            indent=2,
            ensure_ascii=False,
        )

    word_count = sum(
        len(segment["words"])
        for segment in caption_segments
    )

    print()
    print("========================================")
    print("CAPTIONS COMPLETE")
    print("========================================")
    print(f"Language: {info.language}")
    print(
        f"Language confidence: "
        f"{info.language_probability:.2f}"
    )
    print(f"Segments: {len(caption_segments)}")
    print(f"Words: {word_count}")
    print(f"Output: {output_path}")
    print()

    return result


# ============================================================
# Stage 3I: build_captions — the SINGLE caption writer
# ============================================================

def _format_timestamp(seconds, separator):
    seconds = max(float(seconds or 0.0), 0.0)
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{ms:03d}"


def _write_sidecars(segments, json_path):
    """Write captions.srt and captions.vtt next to the JSON.

    Well-formed: sequential 1-based indices (SRT), timing lines in
    HH:MM:SS,mmm (SRT) / HH:MM:SS.mmm (VTT) form.
    """
    json_path = Path(json_path)
    srt_path = json_path.parent / "captions.srt"
    vtt_path = json_path.parent / "captions.vtt"

    srt_lines = []
    vtt_lines = ["WEBVTT", ""]
    for index, segment in enumerate(segments, start=1):
        start = _format_timestamp(segment.get("start", 0), ",")
        end = _format_timestamp(segment.get("end", 0), ",")
        text = str(segment.get("text") or "").strip()
        srt_lines.append(str(index))
        srt_lines.append(f"{start} --> {end}")
        srt_lines.append(text)
        srt_lines.append("")

        vtt_lines.append(
            f"{_format_timestamp(segment.get('start', 0), '.')}"
            f" --> {_format_timestamp(segment.get('end', 0), '.')}"
        )
        vtt_lines.append(text)
        vtt_lines.append("")

    srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
    vtt_path.write_text("\n".join(vtt_lines), encoding="utf-8")
    return srt_path, vtt_path


def _write_captions_json(data, json_path):
    json_path = Path(json_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
    return json_path


def build_captions(narration_file, script_text, captions_json_path):
    """Build captions.json (+ .srt/.vtt sidecars) for a narration.

    The single caption writer for the pipeline (Stage 3I):

    (a) Whisper first — word-level timings via transcribe_audio.
        On success the JSON is stamped timing_source="whisper",
        provisional=False.
    (b) If Whisper raises or yields no usable segments, fall back
        to proportional segments (narration._build_caption_segments
        logic, word-less) stamped timing_source="proportional",
        provisional=True — so downstream consumers can tell an
        estimate from measured timings, and the ASS renderer can
        still burn them (one event per segment).

    Returns the captions data dict.
    """
    narration_path = Path(narration_file)
    json_path = Path(captions_json_path)

    whisper_result = None
    try:
        whisper_result = transcribe_audio(
            str(narration_path), str(json_path)
        )
    except Exception as error:  # Whisper unavailable/failed
        print(f"Whisper transcription failed ({error}); "
              "falling back to proportional captions.")

    segments = []
    if isinstance(whisper_result, dict):
        segments = [
            segment
            for segment in (whisper_result.get("segments") or [])
            if isinstance(segment, dict)
            and segment.get("words")
            and float(segment.get("end", 0)) > float(segment.get("start", 0))
        ]

    if segments:
        data = dict(whisper_result)
        data["segments"] = segments
        data["timing_source"] = "whisper"
        data["provisional"] = False
    else:
        from services.narration import _audio_duration, _build_caption_segments

        duration = _audio_duration(narration_path)
        segments = _build_caption_segments(script_text or "", duration)
        data = {
            "audio_file": str(narration_path),
            "duration": round(float(duration), 3),
            "segments": segments,
            "timing_source": "proportional",
            "provisional": True,
        }

    _write_captions_json(data, json_path)
    _write_sidecars(data["segments"], json_path)

    print(
        f"Captions built: {json_path} "
        f"(source={data['timing_source']}, provisional={data['provisional']}, "
        f"segments={len(data['segments'])})"
    )
    return data


def write_edited_captions(data, json_path):
    """Write an EDITED caption document through the single writer
    (Stage 6 editor): the same _write_captions_json + _write_sidecars
    primitives build_captions uses, so an editor revision's
    captions.json / .srt / .vtt are produced by exactly the code
    path that produces the production's — never by a second writer.
    The caller supplies the full document (segments already edited);
    it is stamped edited=True so the artefact says a human changed
    it. Returns the written data dict."""
    data = dict(data or {})
    data["edited"] = True
    segments = [
        segment
        for segment in (data.get("segments") or [])
        if isinstance(segment, dict)
    ]
    data["segments"] = segments
    _write_captions_json(data, json_path)
    _write_sidecars(segments, json_path)
    return data


def validate_captions(captions_path, sentences):
    """Check caption coverage against the script sentences.

    Sentences are given proportional windows over the captions'
    total duration (the same weighting the proportional writer
    uses). A sentence is covered when at least one caption event
    overlaps its window; when word timings exist, a segment with
    words counts as an event for every window it overlaps too —
    i.e. the rule is uniformly "≥1 event overlapping the window".
    Returns {"ok", "events", "segments", "missing_segments"} where
    missing_segments holds the indices of uncovered sentences.
    """
    path = Path(captions_path)
    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)

    segments = [
        segment
        for segment in (data.get("segments") or [])
        if isinstance(segment, dict)
    ]
    events = 0
    for segment in segments:
        words = segment.get("words") or []
        events += len(words) if words else 1

    sentence_texts = [
        str(s.get("text") if isinstance(s, dict) else s or "")
        for s in (sentences or [])
    ]

    duration = data.get("duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        duration = max(
            (float(s.get("end", 0)) for s in segments), default=0.0
        )

    missing = []
    if sentence_texts and duration > 0:
        import re as _re

        weights = [
            max(1, len(_re.findall(r"\S+", text))) for text in sentence_texts
        ]
        total_weight = sum(weights) or 1
        current = 0.0
        for index, weight in enumerate(weights):
            if index == len(weights) - 1:
                window_end = float(duration)
            else:
                window_end = current + float(duration) * weight / total_weight
            covered = any(
                float(s.get("start", 0)) < window_end
                and float(s.get("end", 0)) > current
                for s in segments
            )
            if not covered:
                missing.append(index)
            current = window_end
    elif sentence_texts:
        missing = list(range(len(sentence_texts)))

    return {
        "ok": not missing and bool(segments),
        "events": events,
        "segments": len(segments),
        "missing_segments": missing,
    }


if __name__ == "__main__":

    if len(sys.argv) < 2:
        print(
            "Usage: python services\\captions.py "
            "<audio_file> [output_json]"
        )
        sys.exit(1)

    audio_file = sys.argv[1]

    output_json = (
        sys.argv[2]
        if len(sys.argv) > 2
        else None
    )

    transcribe_audio(
        audio_file,
        output_json,
    )
