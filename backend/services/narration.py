import hashlib
import json
import re
import subprocess
import sys
import wave
from pathlib import Path

from services import media_probe
from services.atomic_write import write_json_atomic


BASE_DIR = Path(__file__).resolve().parent.parent

PIPER_MODEL = BASE_DIR / "en_US-lessac-medium.onnx"
PIPER_CONFIG = BASE_DIR / "en_US-lessac-medium.onnx.json"

# Transformation Phase 2.4 (§2.4): the second voice — a
# contrasting local Piper model (en_US-ryan-medium: male, against
# lessac's female), downloaded from the Piper voices repository
# and installed beside the primary model, same pattern. No
# service dependency, no cloud TTS. It exists for quoted /
# interview beats only (see assign_sentence_voices); narration
# proper stays on the primary voice.
SECONDARY_MODEL = BASE_DIR / "en_US-ryan-medium.onnx"
SECONDARY_CONFIG = BASE_DIR / "en_US-ryan-medium.onnx.json"
SECONDARY_VOICE_ID = "en_US-ryan-medium"

# Stage 3J (AUD-29): the voice catalogue. HONEST CONTENTS: two
# real Piper voice models are installed in backend/
# (en_US-lessac-medium and en_US-ryan-medium, each with its .json
# config). The piper package ships other .onnx files, but they are
# Hebrew diacritics models, not voices, and are NOT catalogued.
# Any other requested id falls back to the default voice and the
# fallback is reported, never hidden.
VOICE_CATALOGUE = {
    "en_US-lessac-medium": (PIPER_MODEL, PIPER_CONFIG),
    "en_US-ryan-medium": (SECONDARY_MODEL, SECONDARY_CONFIG),
    "default": (PIPER_MODEL, PIPER_CONFIG),
}

DEFAULT_VOICE_ID = "default"


def resolve_voice(voice_id):
    """Resolve a requested voice id to (model_path, config_path,
    resolved_id, fell_back). Unknown/empty ids fall back to the
    default voice with fell_back=True (empty/None request counts as
    the default itself, not a fallback)."""
    if voice_id is None or str(voice_id).strip() == "":
        model, config = VOICE_CATALOGUE[DEFAULT_VOICE_ID]
        return model, config, DEFAULT_VOICE_ID, False
    requested = str(voice_id).strip()
    if requested in VOICE_CATALOGUE:
        model, config = VOICE_CATALOGUE[requested]
        return model, config, requested, False
    model, config = VOICE_CATALOGUE[DEFAULT_VOICE_ID]
    return model, config, DEFAULT_VOICE_ID, True


def secondary_voice_available():
    """True when the secondary voice's model + config are actually
    installed (a catalogue entry pointing at missing files must
    never be assigned)."""
    return SECONDARY_MODEL.exists() and SECONDARY_CONFIG.exists()


_QUOTED_SPAN_RE = re.compile(r"[“\"][^”\"]{10,}[”\"]|[‘'][^’']{10,}[’']")
_QUOTE_ROLES = frozenset({
    "quote", "quoted", "interview", "interviewee", "testimony",
})


def _is_quoted_or_interview(sentence):
    """True when a sentence is quoted speech or an interview beat:
    an explicit role marker on a sentence record, or a quoted span
    of at least ten characters in the text itself."""
    if isinstance(sentence, dict):
        role = str(
            sentence.get("voice_role") or sentence.get("role") or ""
        ).strip().lower()
        if role in _QUOTE_ROLES or sentence.get("quoted") is True:
            return True
        text = str(sentence.get("text") or "")
    else:
        text = str(sentence or "")
    return bool(_QUOTED_SPAN_RE.search(text))


def assign_sentence_voices(sentences, primary_voice_id=None,
                           secondary_available=None):
    """Per-sentence voice assignment (Phase 2.4): the secondary
    voice for quoted / interview beats ONLY; the primary voice
    everywhere else. `sentences` may be a raw script string (split
    with the narration splitter, so the result is parallel to the
    parts generate_narration will synthesise) or a list of
    sentence strings / records.

    Returns the list of voice ids, or None when no sentence
    qualifies (or the secondary model is not installed) — the
    caller then keeps the exact single-voice path."""
    if isinstance(sentences, str):
        items = _split_sentences(sentences)
    else:
        items = list(sentences or [])
    if not items:
        return None
    if secondary_available is None:
        secondary_available = secondary_voice_available()
    if not secondary_available:
        return None
    primary = primary_voice_id or DEFAULT_VOICE_ID
    voices = [
        SECONDARY_VOICE_ID if _is_quoted_or_interview(item) else primary
        for item in items
    ]
    if SECONDARY_VOICE_ID not in voices:
        return None
    return voices


def _audio_duration(audio_file):
    """Duration of an audio file, measured by the single probing
    authority (Stage 5 / R4, annex D2.1): media_probe.probe_audio
    (ffprobe). The WAV-header reader this used to be was a second
    measuring authority and is retired; an unprobeable file raises,
    exactly as a corrupt narration file already failed loudly."""
    facts = media_probe.probe_audio(audio_file)
    if facts is None:
        raise RuntimeError(
            f"Could not measure audio duration (unprobeable): {audio_file}"
        )
    return facts["duration_seconds"]


def _measure_part_duration(part_file):
    """Measured duration of one narration part via probe_audio, or
    None when the part cannot be probed (corrupt — the caller
    fails loudly; a duration is never estimated)."""
    facts = media_probe.probe_audio(part_file)
    if facts is None:
        return None
    return facts["duration_seconds"]


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

    write_json_atomic(caption_file, data)

    print(f"Captions: {caption_file}")
    print(f"Caption segments: {len(segments)}")

    return caption_file


def _run_piper(text, output_file, voice_id=None):
    """Run Piper for a single piece of text.

    This is the ONLY place in this module that runs a subprocess.
    ``voice_id`` is resolved through the voice catalogue (Stage 3J):
    the resolved model's path is what reaches the Piper command.
    Narration is always synthesised at the provider's native rate:
    there is deliberately NO speed / length-scale parameter
    anywhere in this call path (founder ruling, 2026-11-22 — the
    voice model is third-party and speed control is unsupported).
    """
    output_file = Path(output_file)
    model, config, _resolved_id, _fell_back = resolve_voice(voice_id)

    command = [
        sys.executable,
        "-m",
        "piper",
        "-m",
        str(model),
        "-c",
        str(config),
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

    if output_file.stat().st_size == 0:
        raise RuntimeError(
            "Piper created an empty audio file."
        )


def _part_hash(sentence, voice_id=None):
    # Stage 3J: the hash keys on the RESOLVED voice (id + its model
    # files), so a request that falls back to the default voice and
    # a direct default request share the same parts — and a genuine
    # voice change still re-synthesizes. (The Phase 2.4 speed input
    # was removed from this payload by the 2026-11-22 founder
    # ruling; parts synthesised under the old payload simply miss
    # the cache once and are re-synthesised at the native rate.)
    model, config, resolved_id, _fell_back = resolve_voice(voice_id)
    payload = "|".join(
        [
            sentence,
            str(resolved_id),
            model.name,
            config.name,
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


def _write_parts_manifest(manifest_file, parts, model_name=None):
    data = {
        # Stage 5 (R4): manifest v2 — every entry carries its
        # measured duration (see generate_narration); v1 readers
        # ignore the new fields, and v1 manifests are upgraded by
        # measurement on load (load_parts_manifest_data).
        "version": 2,
        "model": model_name or PIPER_MODEL.name,
        "parts": parts,
    }

    # Atomic (Stage 4): the parts manifest is the narration resume
    # record — a truncated copy would corrupt resume decisions.
    write_json_atomic(manifest_file, data)


def load_parts_manifest_data(manifest_file):
    """Load a parts manifest as its full data dict, upgraded to
    measured (v2) form.

    A v1 manifest — or any entry lacking a recorded measurement —
    is upgraded BY MEASURING the existing part files through
    probe_audio (annex D2.2.2: files are on disk, so migration is
    measurement, never estimation). An entry whose file is missing
    or unprobeable is left unmeasured; services.timing then raises
    timing_unavailable for it, loudly, at the consumer. Returns
    None when the manifest file does not exist or cannot be read.
    """
    manifest_file = Path(manifest_file)
    try:
        with open(manifest_file, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    parts = []
    for entry in data.get("parts") or []:
        if not isinstance(entry, dict):
            continue
        entry = dict(entry)
        if entry.get("measured_duration_seconds") is None:
            part_file = manifest_file.parent / str(entry.get("file") or "")
            measured = (
                _measure_part_duration(part_file)
                if entry.get("file") and part_file.is_file()
                else None
            )
            if measured is not None:
                entry["measured_duration_seconds"] = round(measured, 3)
                entry["duration_source"] = "ffprobe_part"
        parts.append(entry)

    return {
        "version": 2,
        "model": data.get("model") or PIPER_MODEL.name,
        "parts": parts,
    }


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
    sentence_voices=None,
):
    if not text or not text.strip():
        raise ValueError("Narration text cannot be empty.")

    # Stage 3J: resolve the requested voice through the catalogue.
    model, config, resolved_voice_id, voice_fell_back = resolve_voice(voice_id)

    if not model.exists():
        raise FileNotFoundError(
            f"Piper model not found: {model}"
        )

    if not config.exists():
        raise FileNotFoundError(
            f"Piper config not found: {config}"
        )

    # Phase 2.4: per-sentence voices (quoted/interview beats on
    # the secondary voice) resolve and check out up front too — a
    # missing secondary model fails here with its name, not three
    # retries into the parts loop.
    if sentence_voices:
        for _part_voice in dict.fromkeys(sentence_voices):
            _pmodel, _pconfig, _presolved, _ = resolve_voice(_part_voice)
            if not _pmodel.exists() or not _pconfig.exists():
                raise FileNotFoundError(
                    f"Piper model not found for sentence voice "
                    f"{_part_voice}: {_pmodel}"
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
    print(f"Model: {model}")
    print(f"Voice requested: {voice_id} -> used: {resolved_voice_id} (fallback: {voice_fell_back})")
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
    voices_used = set()

    for index, sentence in enumerate(sentences):
        part_name = f"part_{index:03d}.wav"
        part_file = parts_dir / part_name
        # Phase 2.4: this part's voice — the per-sentence
        # assignment when one was supplied for this index, else
        # the narration's requested voice. The part hash keys on
        # the part's own resolved voice (via _part_hash), so a
        # voice change re-synthesises exactly the affected parts.
        part_voice_id = voice_id
        if sentence_voices and index < len(sentence_voices):
            part_voice_id = sentence_voices[index]
        _pv_model, _pv_config, part_resolved_voice, _pv_fb = (
            resolve_voice(part_voice_id)
        )
        voices_used.add(part_resolved_voice)
        part_hash = _part_hash(sentence, voice_id=part_voice_id)

        entry = {
            "index": index,
            "file": part_name,
            "hash": part_hash,
            "sentence_sha256": _sentence_sha256(sentence),
            "voice_id": part_resolved_voice,
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
                        voice_id=part_voice_id,
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

        # Stage 5 (R4, annex D2.1/D2.2): measure the part — whether
        # just synthesised or reused — through the single probing
        # authority, and record the measurement on the manifest
        # entry. An unprobeable part is corrupt narration: fail the
        # stage loudly here rather than let an unmeasured duration
        # flow into timing downstream.
        measured = _measure_part_duration(part_file)
        if measured is None:
            raise RuntimeError(
                f"Narration part {part_name} could not be probed "
                "for a measured duration; refusing to record an "
                "unmeasured part."
            )
        entry["measured_duration_seconds"] = round(measured, 3)
        entry["duration_source"] = "ffprobe_part"

        manifest_parts.append(entry)
        part_files.append(part_file)

        # Persist progress after every part so a crashed run can
        # resume from the parts already synthesized.
        _write_parts_manifest(manifest_file, manifest_parts, model_name=model.name)

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

    # Stage 3I: narration produces AUDIO ONLY. Captions are written
    # solely by services.captions.build_captions (the single caption
    # writer); the proportional _write_captions call that used to
    # run here — and silently overwrite/conflict with Whisper
    # captions depending on call order — is removed.

    print()

    # Stage 3J: voice metadata. Historically this returned
    # str(output_file); callers that need the path can use
    # output_path — jobs records this dict in state["voice"].
    return {
        "voice_id_requested": voice_id,
        "voice_id_used": resolved_voice_id,
        "voice_fallback": voice_fell_back,
        # Phase 2.4: every voice actually used across the parts
        # (more than one only when sentence_voices assigned the
        # secondary voice to quoted/interview beats).
        "voice_ids_used": sorted(voices_used),
        "model": model.name,
        "output_file": str(output_file),
    }


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
