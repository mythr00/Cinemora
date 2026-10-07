import json
import re
import subprocess
import sys
import wave
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

PIPER_MODEL = BASE_DIR / "en_US-lessac-medium.onnx"
PIPER_CONFIG = BASE_DIR / "en_US-lessac-medium.onnx.json"


def _audio_duration(audio_file):
    with wave.open(str(audio_file), "rb") as wav:
        frames = wav.getnframes()
        rate = wav.getframerate()

    if rate <= 0:
        raise RuntimeError("Invalid narration sample rate.")

    return frames / float(rate)


def _build_caption_segments(text, duration):
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", text.strip())
        if sentence.strip()
    ]

    if not sentences:
        sentences = [text.strip()]

    weights = [
        max(1, len(re.findall(r"\S+", sentence)))
        for sentence in sentences
    ]

    total_weight = sum(weights)

    if total_weight <= 0:
        return []

    segments = []
    current = 0.0

    for index, sentence in enumerate(sentences):
        if index == len(sentences) - 1:
            end = float(duration)
        else:
            end = current + (
                float(duration) * weights[index] / total_weight
            )

        if end <= current:
            continue

        segments.append({
            "start": round(current, 3),
            "end": round(end, 3),
            "text": sentence,
            "words": [],
        })

        current = end

    return segments


def _write_captions(text, audio_file):
    duration = _audio_duration(audio_file)

    segments = _build_caption_segments(
        text,
        duration,
    )

    caption_file = audio_file.parent / "captions.json"

    data = {
        "audio_file": str(audio_file),
        "duration": round(duration, 3),
        "segments": segments,
    }

    with open(
        caption_file,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print(f"Captions: {caption_file}")
    print(f"Caption segments: {len(segments)}")

    return caption_file


def generate_narration(
    text: str,
    output_path: str,
    voice_id=None,
):
    if not text or not text.strip():
        raise ValueError("Narration text cannot be empty.")

    if not PIPER_MODEL.exists():
        raise FileNotFoundError(
            f"Piper model not found: {PIPER_MODEL}"
        )

    if not PIPER_CONFIG.exists():
        raise FileNotFoundError(
            f"Piper config not found: {PIPER_CONFIG}"
        )

    output_file = Path(output_path)

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    print()
    print("========================================")
    print("GENERATING NARRATION")
    print("========================================")
    print(f"Python: {sys.executable}")
    print(f"Model: {PIPER_MODEL}")
    print(f"Output: {output_file}")
    print()

    command = [
        sys.executable,
        "-m",
        "piper",
        "-m",
        str(PIPER_MODEL),
        "-c",
        str(PIPER_CONFIG),
        "-f",
        str(output_file),
    ]

    print("Running Piper...")
    print()

    result = subprocess.run(
        command,
        input=text,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )

    if result.stdout:
        print(result.stdout)

    if result.stderr:
        print(result.stderr)

    if result.returncode != 0:
        raise RuntimeError(
            f"Piper failed with exit code {result.returncode}."
        )

    if not output_file.exists():
        raise RuntimeError(
            "Piper finished but no audio file was created."
        )

    file_size = output_file.stat().st_size

    if file_size == 0:
        raise RuntimeError(
            "Piper created an empty audio file."
        )

    print()
    print("========================================")
    print("NARRATION COMPLETE")
    print("========================================")
    print(f"Audio: {output_file}")
    print(f"Size: {file_size:,} bytes")

    _write_captions(
        text,
        output_file,
    )

    print()

    return str(output_file)


if __name__ == "__main__":

    petrov_script = """
September 26th, 1983.

Deep inside a Soviet command center, the warning system suddenly came alive.

A computer reported that the United States had launched five nuclear missiles toward the Soviet Union.

The officer on duty was Stanislav Petrov.

His job was simple: report a confirmed nuclear attack to his superiors.

But something about the warning didn't make sense.

Petrov knew that if America launched a real first strike, it would make little sense to send only five missiles.

Instead of immediately reporting the alarm as a nuclear attack, Petrov decided it was a false alarm.

He was right.

The Soviet satellite system had mistaken sunlight reflecting off high-altitude clouds for the heat signatures of American missile launches.

Petrov's decision prevented a false warning from escalating through the Soviet military command structure.

The incident remained largely unknown for years.

But on that night, one man's decision helped prevent a terrifying mistake from becoming something far worse.
"""

    output = (
        BASE_DIR
        / "media"
        / "15e0f37d-c3a3-4001-a801-e9f7007cb627"
        / "narration.wav"
    )

    generate_narration(
        text=petrov_script,
        output_path=str(output),
    )
