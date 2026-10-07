import json
import sys
from pathlib import Path

from faster_whisper import WhisperModel


BASE_DIR = Path(__file__).resolve().parent.parent

MODEL_SIZE = "base"
DEVICE = "cpu"
COMPUTE_TYPE = "int8"


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

    model = WhisperModel(
        MODEL_SIZE,
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
    )

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