import hashlib
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


def _split_sentences(text):
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", text.strip())
        if sentence.strip()
    ]

    if not sentences:
        sentences = [text.strip()]

    return sentences


def _build_caption_segments(text, duration):
    sentences = _split_sentences(text)

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


def _run_piper(text, output_file, voice_id=None, speed=None):
    """Run Piper for a single piece of text.

    This is the ONLY place in this module that runs a subprocess.
    ``voice_id`` is threaded through (and recorded by callers in the
    parts manifest hash) but does not change the model: narration
    always uses PIPER_MODEL. When ``speed`` is provided, Piper's
    --length-scale is set to 1.0 / speed; the default command
    (voice_id=None, speed=None) is identical to the historical one.
    """
    output_file = Path(output_file)

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

    if speed is not None:
        command.extend(["--length-scale", str(1.0 / speed)])

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

    if output_file.stat().st_size == 0:
        raise RuntimeError(
            "Piper created an empty audio file."
        )


def _part_hash(sentence, voice_id=None, speed=None):
    payload = "|".join(
        [
            sentence,
            str(voice_id),
            str(speed),
            PIPER_MODEL.name,
            PIPER_CONFIG.name,
        ]
    )

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sentence_sha256(sentence):
    return hashlib.sha256(sentence.encode("utf-8")).hexdigest()


def _load_parts_manifest(manifest_file):
    """Return {index: entry} from an existing parts manifest, or {}."""
    try:
        with open(manifest_file, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return {}

    entries = {}

    if isinstance(data, dict):
        for entry in data.get("parts") or []:
            if isinstance(entry, dict) and "index" in entry:
                entries[entry["index"]] = entry

    return entries


def _write_parts_manifest(manifest_file, parts):
    data = {
        "version": 1,
        "model": PIPER_MODEL.name,
        "parts": parts,
    }

    with open(manifest_file, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def _concatenate_parts(part_files, output_file):
    """Join WAV parts sample-exactly, in order, with no gaps."""
    params = None

    with wave.open(str(output_file), "wb") as out_wav:
        for part_file in part_files:
            with wave.open(str(part_file), "rb") as part_wav:
                part_params = (
                    part_wav.getnchannels(),
                    part_wav.getsampwidth(),
                    part_wav.getframerate(),
                )

                if params is None:
                    params = part_params
                    out_wav.setnchannels(part_params[0])
                    out_wav.setsampwidth(part_params[1])
                    out_wav.setframerate(part_params[2])
                elif part_params != params:
                    raise RuntimeError(
                        "Narration parts have mismatched audio "
                        f"parameters: {part_file} has {part_params}, "
                        f"expected {params}."
                    )

                out_wav.writeframes(
                    part_wav.readframes(part_wav.getnframes())
                )


def generate_narration(
    text: str,
    output_path: str,
    voice_id=None,
    speed=None,
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

    sentences = _split_sentences(text)

    parts_dir = output_file.parent / "narration_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    manifest_file = parts_dir / "parts_manifest.json"
    existing_entries = _load_parts_manifest(manifest_file)

    print(f"Sentences: {len(sentences)}")
    print(f"Parts: {parts_dir}")
    print()

    manifest_parts = []
    part_files = []

    for index, sentence in enumerate(sentences):
        part_name = f"part_{index:03d}.wav"
        part_file = parts_dir / part_name
        part_hash = _part_hash(sentence, voice_id=voice_id, speed=speed)

        entry = {
            "index": index,
            "file": part_name,
            "hash": part_hash,
            "sentence_sha256": _sentence_sha256(sentence),
        }

        existing = existing_entries.get(index)
        reused = (
            part_file.exists()
            and part_file.stat().st_size > 0
            and existing is not None
            and existing.get("hash") == part_hash
        )

        if reused:
            print(
                f"Part {index + 1}/{len(sentences)}: "
                f"reusing {part_name}"
            )
        else:
            print(
                f"Part {index + 1}/{len(sentences)}: "
                f"synthesizing {part_name}"
            )

            last_error = None

            for attempt in range(1, 4):
                try:
                    _run_piper(
                        sentence,
                        part_file,
                        voice_id=voice_id,
                        speed=speed,
                    )
                    last_error = None
                    break
                except Exception as error:  # noqa: BLE001 - retried below
                    last_error = error
                    print(
                        f"Part {index + 1}/{len(sentences)}: "
                        f"attempt {attempt}/3 failed: {error}"
                    )

            if last_error is not None:
                raise last_error

        manifest_parts.append(entry)
        part_files.append(part_file)

        # Persist progress after every part so a crashed run can
        # resume from the parts already synthesized.
        _write_parts_manifest(manifest_file, manifest_parts)

    print()
    print("Joining narration parts...")

    _concatenate_parts(part_files, output_file)

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
