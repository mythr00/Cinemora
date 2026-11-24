"""Transcription — the stable internal interface (§16).

Voice AND transcription providers sit behind stable internal
interfaces: callers (today, the Phase 9 voice-to-documentary
flow) ask this module to transcribe an audio file and receive
one recorded shape back, whoever the provider is. The single
registered provider is the stack's real transcription engine —
faster-whisper, the same model services.captions uses for
caption timing (the model singleton is shared, never loaded
twice).

The returned record is honest about what the provider does and
does not supply:

- segments carry the provider's own confidence facts
  (avg_logprob, no_speech_prob) verbatim;
- "unclear" sections are DETECTED from those facts (whisper's
  conventional thresholds: no_speech_prob >= 0.6 or
  avg_logprob <= -1.0) and flagged per segment — the transcript
  text itself is never altered to hide them;
- speaker diarization is NOT something this provider performs:
  `speakers` is None and `speaker_note` says so, rather than a
  fabricated speaker list.
"""

from pathlib import Path


PROVIDER_FASTER_WHISPER = "faster_whisper"

#: Detection thresholds for unclear sections (the provider's
#: own conventional cut-offs, recorded here so a flag is always
#: explainable from the segment's recorded facts).
NO_SPEECH_THRESHOLD = 0.6
AVG_LOGPROB_THRESHOLD = -1.0


class TranscriptionError(RuntimeError):
    """The transcription provider could not transcribe the
    file. Carries a stable `code` for the API layer."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _is_unclear(segment) -> bool:
    no_speech = getattr(segment, "no_speech_prob", None)
    logprob = getattr(segment, "avg_logprob", None)
    if no_speech is not None and no_speech >= NO_SPEECH_THRESHOLD:
        return True
    if logprob is not None and logprob <= AVG_LOGPROB_THRESHOLD:
        return True
    return False


def _transcribe_faster_whisper(audio_path: Path) -> dict:
    # The model singleton lives in services.captions (Stage 3I):
    # one WhisperModel per process, shared by caption timing and
    # transcription alike.
    from services.captions import _get_whisper_model

    model = _get_whisper_model()
    segments_iter, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
    )
    segments = []
    for segment in segments_iter:
        words = [
            {
                "word": word.word.strip(),
                "start": round(word.start, 3),
                "end": round(word.end, 3),
            }
            for word in (segment.words or [])
        ]
        segments.append({
            "start": round(segment.start, 3),
            "end": round(segment.end, 3),
            "text": segment.text.strip(),
            "avg_logprob": (
                round(float(segment.avg_logprob), 4)
                if getattr(segment, "avg_logprob", None) is not None
                else None
            ),
            "no_speech_prob": (
                round(float(segment.no_speech_prob), 4)
                if getattr(segment, "no_speech_prob", None) is not None
                else None
            ),
            "unclear": _is_unclear(segment),
            "words": words,
        })
    return {
        "language": info.language,
        "language_probability": (
            round(float(info.language_probability), 4)
            if getattr(info, "language_probability", None) is not None
            else None
        ),
        "duration": info.duration,
        "segments": segments,
    }


_PROVIDERS = {
    PROVIDER_FASTER_WHISPER: _transcribe_faster_whisper,
}

DEFAULT_PROVIDER = PROVIDER_FASTER_WHISPER


def available_providers() -> list:
    return sorted(_PROVIDERS)


def transcribe(audio_file, *, provider=None) -> dict:
    """Transcribe an audio file through the named provider (the
    default provider when unnamed). Returns the recorded
    transcript shape:

      {provider, audio_file, language, language_probability,
       duration, segments: [{start, end, text, avg_logprob,
       no_speech_prob, unclear, words}], text (segments joined
       as paragraphs), unclear_sections: [segment indices],
       speakers: None, speaker_note}

    Raises TranscriptionError when the file is missing or the
    provider fails — an absent transcript is an error, never an
    empty success."""
    provider_name = provider or DEFAULT_PROVIDER
    handler = _PROVIDERS.get(provider_name)
    if handler is None:
        raise TranscriptionError(
            "transcription_provider_unknown",
            f"Unknown transcription provider: {provider_name!r}. "
            f"Available: {', '.join(available_providers())}.",
        )
    audio_path = Path(audio_file)
    if not audio_path.is_file():
        raise TranscriptionError(
            "transcription_file_missing",
            f"Audio file not found: {audio_path}",
        )
    try:
        result = handler(audio_path)
    except TranscriptionError:
        raise
    except Exception as error:
        raise TranscriptionError(
            "transcription_failed",
            f"Transcription failed: {type(error).__name__}: {error}",
        ) from error
    segments = result["segments"]
    return {
        "provider": provider_name,
        "audio_file": str(audio_path),
        "language": result["language"],
        "language_probability": result["language_probability"],
        "duration": result["duration"],
        "segments": segments,
        "text": "\n\n".join(
            s["text"] for s in segments if s["text"]
        ),
        "unclear_sections": [
            index for index, s in enumerate(segments) if s["unclear"]
        ],
        "speakers": None,
        "speaker_note": (
            "Speaker diarization is not performed by the "
            "faster-whisper provider; the transcript is a single "
            "undivided voice record."
        ),
    }
