import json
import os
import sys
from pathlib import Path

from services.atomic_write import write_json_atomic, write_text_atomic


BASE_DIR = Path(__file__).resolve().parent.parent

MODEL_SIZE = "base"
DEVICE = "cpu"
COMPUTE_TYPE = "int8"

# D-04: the Whisper weights are PINNED — an explicit Hugging Face
# repo id plus the exact revision (commit hash) a fresh deployment
# must resolve, instead of an unpinned first-use download of
# whatever "base" points at that day. The defaults are the weights
# the local stack actually resolves today, verified 2026-11-25 from
# the local HF cache: Systran/faster-whisper-base (faster-whisper's
# own mapping for "base") at the snapshot recorded in
# ~/.cache/huggingface/hub/models--Systran--faster-whisper-base/
# refs/main. Read at import time (like llm_client's config) —
# rotation requires a process restart. The cache location itself
# stays HF_HOME's business (the Docker image sets
# HF_HOME=/data/hf-cache).
WHISPER_MODEL_ID = os.environ.get(
    "CINEMORA_WHISPER_MODEL", "Systran/faster-whisper-base"
)
WHISPER_MODEL_REVISION = os.environ.get(
    "CINEMORA_WHISPER_REVISION",
    "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
)

# Stage 3I: the Whisper model is a lazy module-level singleton —
# previously every transcribe_audio call constructed a new
# WhisperModel (a full model load per call).
_WHISPER_MODEL = None


def _get_whisper_model():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel

        _WHISPER_MODEL = WhisperModel(
            WHISPER_MODEL_ID,
            device=DEVICE,
            compute_type=COMPUTE_TYPE,
            revision=WHISPER_MODEL_REVISION,
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

    # Atomic (Stage 4): captions.json is the timeline builder's
    # timing source — never observable half-written.
    write_json_atomic(output_path, result)

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
# Stage 5 (R4): sentence <-> word alignment — CAPTIONS ONLY
# ============================================================
# Moved from services.timeline (annex D2.3): word alignment is no
# longer a timing source for anything. This helper answers one
# question for build_captions — which Whisper words belong to
# which script sentence, and how well did they match
# (alignment_score)? Sentence WINDOWS come exclusively from
# services.timing (measured part durations); the start/end values
# in these records are raw word times and must never be used as
# windows.


def _align_sentences_to_segments(
    sentences,
    caption_segments,
    narration_duration,
):
    """
    Align script sentences to Whisper word timestamps.

    Whisper may:
    - combine multiple script sentences into one segment
    - split one script sentence across multiple segments
    - omit punctuation
    - slightly alter wording

    Therefore alignment is performed against the complete
    Whisper word sequence rather than assuming one segment
    equals one script sentence.
    """

    import difflib
    import re

    def normalize_token(value):
        value = str(value or "").lower()

        value = re.sub(
            r"[^a-z0-9']+",
            "",
            value,
        )

        return value

    # ---------------------------------------------------------
    # 1. Normalize script sentences
    # ---------------------------------------------------------

    normalized_sentences = []

    script_tokens = []

    for index, sentence in enumerate(sentences or []):

        if isinstance(sentence, dict):
            sentence_text = str(
                sentence.get("text", "")
            ).strip()
        else:
            sentence_text = str(sentence).strip()

        tokens = [
            normalize_token(token)
            for token in sentence_text.split()
        ]

        tokens = [
            token
            for token in tokens
            if token
        ]

        start_token = len(script_tokens)

        script_tokens.extend(tokens)

        end_token = len(script_tokens)

        normalized_sentences.append(
            {
                "index": index,
                "text": sentence_text,
                "tokens": tokens,
                "token_start": start_token,
                "token_end": end_token,
            }
        )

    # ---------------------------------------------------------
    # 2. Flatten Whisper words
    # ---------------------------------------------------------

    whisper_words = []

    for segment_index, segment in enumerate(caption_segments or []):

        for word in segment.get("words", []) or []:

            raw_word = str(
                word.get("word", "")
            ).strip()

            normalized = normalize_token(
                raw_word
            )

            if not normalized:
                continue

            start_time = float(
                word.get("start", 0.0)
            )

            end_time = float(
                word.get(
                    "end",
                    start_time,
                )
            )

            if end_time < start_time:
                end_time = start_time

            whisper_words.append(
                {
                    "text": raw_word,
                    "normalized": normalized,
                    "start": start_time,
                    "end": end_time,
                    # Stage 5: build_captions groups a sentence's
                    # words back into per-segment caption events.
                    "segment_index": segment_index,
                }
            )

    whisper_tokens = [
        word["normalized"]
        for word in whisper_words
    ]

    total_script_tokens = len(
        script_tokens
    )

    total_whisper_words = len(
        whisper_words
    )

    if not normalized_sentences:
        return []

    if not whisper_words:

        # Stage 5 (R4, annex D2.2.7): NO uniform fallback. The
        # pre-R4 branch split the narration duration evenly across
        # sentences (total / N) and presented the result as
        # sentence windows — the AUDIO-001 defect. Alignment now
        # serves captions only and yields no timings: with no
        # usable words, every sentence is reported unaligned and
        # the caller places its caption event on the sentence's
        # MEASURED window (services.timing) instead.
        return [
            {
                "sentence_index": sentence["index"],
                "text": sentence["text"],
                "word_start": None,
                "word_end": None,
                "alignment_score": 0.0,
                "alignment_method": "no_words",
            }
            for sentence in normalized_sentences
        ]

    # ---------------------------------------------------------
    # 3. Global token alignment
    # ---------------------------------------------------------

    matcher = difflib.SequenceMatcher(
        None,
        script_tokens,
        whisper_tokens,
        autojunk=False,
    )

    matching_blocks = matcher.get_matching_blocks()

    # Map each script token position to a Whisper word index.
    script_to_whisper = {}

    for block in matching_blocks:

        script_start = block.a
        whisper_start = block.b
        size = block.size

        for offset in range(size):

            script_index = (
                script_start + offset
            )

            whisper_index = (
                whisper_start + offset
            )

            script_to_whisper[
                script_index
            ] = whisper_index

    # ---------------------------------------------------------
    # 4. Interpolate unmatched script tokens
    # ---------------------------------------------------------

    matched_positions = sorted(
        script_to_whisper.keys()
    )

    def estimate_whisper_position(
        script_position,
    ):

        if not matched_positions:
            return int(
                (
                    script_position
                    / max(
                        1,
                        total_script_tokens,
                    )
                )
                * total_whisper_words
            )

        if script_position <= matched_positions[0]:

            return script_to_whisper[
                matched_positions[0]
            ]

        if script_position >= matched_positions[-1]:

            return min(
                total_whisper_words - 1,
                script_to_whisper[
                    matched_positions[-1]
                ]
                + (
                    script_position
                    - matched_positions[-1]
                ),
            )

        left = matched_positions[0]
        right = matched_positions[-1]

        for i in range(
            len(matched_positions) - 1
        ):

            current = matched_positions[i]
            following = matched_positions[i + 1]

            if (
                current
                <= script_position
                <= following
            ):

                left = current
                right = following
                break

        left_whisper = script_to_whisper[
            left
        ]

        right_whisper = script_to_whisper[
            right
        ]

        span_script = (
            right - left
        )

        if span_script <= 0:
            return left_whisper

        ratio = (
            script_position - left
        ) / span_script

        estimate = (
            left_whisper
            + ratio
            * (
                right_whisper
                - left_whisper
            )
        )

        return int(round(estimate))

    # ---------------------------------------------------------
    # 5. Build sentence timing windows
    # ---------------------------------------------------------

    results = []

    previous_end_time = 0.0

    for sentence_position, sentence in enumerate(
        normalized_sentences
    ):

        token_start = sentence[
            "token_start"
        ]

        token_end = sentence[
            "token_end"
        ]

        if token_end <= token_start:

            start_time = previous_end_time
            end_time = start_time

            results.append(
                {
                    "sentence_index": sentence["index"],
                    "text": sentence["text"],
                    "start": round(
                        start_time,
                        3,
                    ),
                    "end": round(
                        end_time,
                        3,
                    ),
                    "duration": 0.0,
                    "word_start": 0,
                    "word_end": 0,
                    "alignment_score": 0.0,
                    "alignment_method": "empty_sentence",
                }
            )

            continue

        mapped_indices = []

        for script_position in range(
            token_start,
            token_end,
        ):

            if script_position in script_to_whisper:

                mapped_indices.append(
                    script_to_whisper[
                        script_position
                    ]
                )

        # -----------------------------------------------------
        # Direct matched words
        # -----------------------------------------------------

        if mapped_indices:

            word_start = min(
                mapped_indices
            )

            word_end = max(
                mapped_indices
            ) + 1

        # -----------------------------------------------------
        # No direct match: estimate from neighboring
        # sentence boundaries.
        # -----------------------------------------------------

        else:

            estimated_start = (
                estimate_whisper_position(
                    token_start
                )
            )

            estimated_end = (
                estimate_whisper_position(
                    max(
                        token_start,
                        token_end - 1,
                    )
                )
                + 1
            )

            word_start = max(
                0,
                min(
                    total_whisper_words - 1,
                    estimated_start,
                ),
            )

            word_end = max(
                word_start + 1,
                min(
                    total_whisper_words,
                    estimated_end,
                ),
            )

        # -----------------------------------------------------
        # Clamp to chronological sequence
        # -----------------------------------------------------

        if results:

            minimum_word = results[-1][
                "word_end"
            ]

            if word_start < minimum_word:

                word_start = minimum_word

            if word_end <= word_start:

                word_end = min(
                    total_whisper_words,
                    word_start + 1,
                )

        if word_start >= total_whisper_words:

            word_start = max(
                0,
                total_whisper_words - 1,
            )

        if word_end > total_whisper_words:

            word_end = total_whisper_words

        selected_words = whisper_words[
            word_start:word_end
        ]

        # -----------------------------------------------------
        # Timestamp calculation
        # -----------------------------------------------------

        if selected_words:

            start_time = selected_words[0][
                "start"
            ]

            end_time = selected_words[-1][
                "end"
            ]

        else:

            start_time = previous_end_time
            end_time = previous_end_time

        if start_time < previous_end_time:

            start_time = previous_end_time

        if end_time < start_time:

            end_time = start_time

        # -----------------------------------------------------
        # Alignment score
        # -----------------------------------------------------

        expected = set(
            sentence["tokens"]
        )

        actual = set(
            word["normalized"]
            for word in selected_words
        )

        if expected:

            alignment_score = (
                len(
                    expected & actual
                )
                / len(expected)
            )

        else:

            alignment_score = 0.0

        results.append(
            {
                "sentence_index": sentence["index"],
                "text": sentence["text"],
                "start": round(
                    start_time,
                    3,
                ),
                "end": round(
                    end_time,
                    3,
                ),
                "duration": round(
                    max(
                        0.0,
                        end_time - start_time,
                    ),
                    3,
                ),
                "word_start": word_start,
                "word_end": word_end,
                "alignment_score": round(
                    alignment_score,
                    4,
                ),
                "alignment_method": "global_token_alignment",
            }
        )

        previous_end_time = end_time

    # ---------------------------------------------------------
    # 6. Final boundary
    # ---------------------------------------------------------

    narration_duration = float(
        narration_duration or 0.0
    )

    if results:

        results[-1]["end"] = round(
            narration_duration,
            3,
        )

        if (
            results[-1]["start"]
            > results[-1]["end"]
        ):

            results[-1]["start"] = results[-1][
                "end"
            ]

        results[-1]["duration"] = round(
            max(
                0.0,
                results[-1]["end"]
                - results[-1]["start"],
            ),
            3,
        )

    # ---------------------------------------------------------
    # 7. Final chronological safety
    # ---------------------------------------------------------

    for i in range(
        1,
        len(results),
    ):

        if (
            results[i]["start"]
            < results[i - 1]["end"]
        ):

            results[i]["start"] = results[
                i - 1
            ]["end"]

        if (
            results[i]["end"]
            < results[i]["start"]
        ):

            results[i]["end"] = results[
                i
            ]["start"]

        results[i]["duration"] = round(
            max(
                0.0,
                results[i]["end"]
                - results[i]["start"],
            ),
            3,
        )

    return results


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

    write_text_atomic(srt_path, "\n".join(srt_lines))
    write_text_atomic(vtt_path, "\n".join(vtt_lines))
    return srt_path, vtt_path


def _write_captions_json(data, json_path):
    write_json_atomic(json_path, data)
    return Path(json_path)


# Stage 5 (R4, annex D2.2.6): a sentence whose word-alignment
# score reaches this constant is classified "aligned". The
# constant is recorded in the captions artefact
# ("alignment_threshold") and classifies CAPTION quality only —
# it never gates timing, which is measured, and never fails a run.
ALIGNMENT_SCORE_THRESHOLD = 0.5


def _resolve_parts_manifest(narration_path, parts_manifest):
    """Return the measured parts manifest data governing caption
    placement, or None when no manifest is available.

    Resolution: an explicit argument (dict, or a path loaded and
    upgraded by measurement via narration.load_parts_manifest_data)
    wins; otherwise the conventional location next to the
    narration file — where generate_narration always writes it in
    both the production and editor flows.
    """
    from services import narration as narration_service

    if isinstance(parts_manifest, dict):
        return parts_manifest
    if parts_manifest is not None:
        return narration_service.load_parts_manifest_data(parts_manifest)
    conventional = (
        Path(narration_path).parent
        / "narration_parts"
        / "parts_manifest.json"
    )
    if conventional.is_file():
        return narration_service.load_parts_manifest_data(conventional)
    return None


def _flatten_whisper_words(caption_segments):
    """Flatten Whisper words exactly as _align_sentences_to_segments
    does (same skip rule: a word whose normalised form is empty is
    not an alignment word), keeping each word's segment index so a
    sentence's words can be regrouped into per-segment events."""
    import re

    words = []
    for segment_index, segment in enumerate(caption_segments or []):
        for word in segment.get("words", []) or []:
            raw_word = str(word.get("word", "")).strip()
            normalized = re.sub(r"[^a-z0-9']+", "", raw_word.lower())
            if not normalized:
                continue
            start_time = float(word.get("start", 0.0))
            end_time = float(word.get("end", start_time))
            if end_time < start_time:
                end_time = start_time
            words.append(
                {
                    "text": raw_word,
                    "start": start_time,
                    "end": end_time,
                    "segment_index": segment_index,
                }
            )
    return words


def _sentence_events(sentence_words, window):
    """Build a sentence's caption events from its aligned words:
    one event per contributing Whisper segment (segment boundaries
    may subdivide the window), every time clamped INTO the
    sentence's measured window — a boundary may never move
    (annex D2.2.5). Returns [] when no usable event survives."""
    groups = []
    for word in sentence_words:
        if groups and groups[-1][0] == word["segment_index"]:
            groups[-1][1].append(word)
        else:
            groups.append((word["segment_index"], [word]))

    events = []
    for _segment_index, group in groups:
        clamped = [
            {
                "word": word["text"],
                "start": round(
                    min(max(word["start"], window["start"]), window["end"]), 3
                ),
                "end": round(
                    min(max(word["end"], window["start"]), window["end"]), 3
                ),
            }
            for word in group
        ]
        start = min(word["start"] for word in clamped)
        end = max(word["end"] for word in clamped)
        if end <= start:
            continue
        events.append(
            {
                "start": start,
                "end": end,
                "text": " ".join(word["word"] for word in clamped),
                "words": clamped,
            }
        )
    return events


def _build_captions_measured(
    narration_path, script_text, whisper_result, whisper_segments, manifest
):
    """Caption placement under measured timing (Stage 5 / R4).

    Sentence windows come from services.timing (measured part
    durations) — never from Whisper. Whisper words only place
    caption EVENTS inside those windows. Per sentence the artefact
    records alignment_score + alignment_state (aligned /
    low_alignment / alignment_failed, annex D2.2.6); a sentence
    that is not aligned gets a single event spanning its measured
    window and the document is stamped provisional. A manifest
    whose parts are unmeasured raises timing_unavailable (via
    services.timing) — captions in a production flow are built on
    the film's clock, and an unmeasurable clock fails loudly.
    """
    from services import narration as narration_service
    from services import timing as timing_service

    sentences = narration_service._split_sentences(script_text or "")
    windows = timing_service.sentence_windows(manifest)
    if len(windows) != len(sentences):
        raise timing_service.TimingUnavailableError(
            "timing_unavailable: parts manifest describes "
            f"{len(windows)} sentences but the script has "
            f"{len(sentences)} — caption placement cannot proceed "
            "on a mismatched clock."
        )
    total = timing_service.total_duration(manifest)

    aligned = (
        _align_sentences_to_segments(sentences, whisper_segments, total)
        if whisper_segments
        else []
    )
    flat_words = _flatten_whisper_words(whisper_segments)

    sentence_records = []
    events = []
    counts = {"aligned": 0, "low_alignment": 0, "alignment_failed": 0}
    for index, sentence in enumerate(sentences):
        window = windows[index]
        record = aligned[index] if index < len(aligned) else None
        score = (
            float(record.get("alignment_score", 0.0))
            if record is not None
            else 0.0
        )
        if not whisper_segments:
            state = "alignment_failed"
        elif score >= ALIGNMENT_SCORE_THRESHOLD:
            state = "aligned"
        else:
            state = "low_alignment"
        counts[state] += 1

        sentence_events = []
        if (
            state == "aligned"
            and record is not None
            and record.get("word_start") is not None
        ):
            sentence_events = _sentence_events(
                flat_words[record["word_start"]:record["word_end"]],
                window,
            )
        if not sentence_events:
            # Single-event fallback ON the measured window (never a
            # reconstructed window): the sentence's whole caption is
            # one event spanning exactly its measured span.
            sentence_events = [
                {
                    "start": window["start"],
                    "end": window["end"],
                    "text": sentence,
                    "words": [],
                }
            ]
        events.extend(sentence_events)
        sentence_records.append(
            {
                "sentence_index": index,
                "text": sentence,
                "start": window["start"],
                "end": window["end"],
                "duration": window["duration"],
                "timing_source": "measured_part",
                "alignment_score": round(score, 4),
                "alignment_state": state,
            }
        )

    if not whisper_segments:
        timing_source = "proportional"
        provisional = True
    else:
        timing_source = "whisper"
        provisional = counts["aligned"] != len(sentences)

    data = {
        "audio_file": str(narration_path),
        "duration": round(total, 3),
        "segments": events,
        "sentences": sentence_records,
        "captions_alignment": counts,
        "alignment_threshold": ALIGNMENT_SCORE_THRESHOLD,
        "timing_source": timing_source,
        "provisional": provisional,
    }
    if isinstance(whisper_result, dict):
        for key in ("language", "language_probability"):
            if key in whisper_result:
                data[key] = whisper_result[key]
    return data


def build_captions(narration_file, script_text, captions_json_path,
                   parts_manifest=None):
    """Build captions.json (+ .srt/.vtt sidecars) for a narration.

    The single caption writer for the pipeline (Stage 3I), under
    measured timing since Stage 5 (R4, annex D2):

    (a) When a narration parts manifest is available — always the
        case in the production and editor flows, where
        generate_narration runs first — sentence windows are the
        MEASURED part durations (services.timing) and Whisper word
        timings only place caption events inside them, clamped.
        Per-sentence alignment states and their aggregate are
        recorded in the artefact. Whisper failure degrades caption
        PLACEMENT (single events on the measured windows,
        provisional) — never the windows themselves.
    (b) Standalone calls with no parts manifest keep the Stage 3I
        behaviour (Whisper segments as transcribed; proportional
        word-less segments when Whisper is unusable, stamped
        timing_source="proportional", provisional=True). Captions
        are presentation-only: nothing downstream takes timing
        from captions.json since R4, so this path can label but
        never corrupt the film's clock.

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

    manifest = _resolve_parts_manifest(narration_path, parts_manifest)

    if manifest is not None:
        data = _build_captions_measured(
            narration_path, script_text, whisper_result, segments, manifest
        )
    elif segments:
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
