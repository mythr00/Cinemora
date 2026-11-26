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

# ------------------------------------------------------------------
# Kokoro-82M — the production narration engine (founder decision
# 2026-11-26, on the TTS investigation's recommendation).
#
# Assets, installed beside the code like the Piper voices were:
# kokoro-v1.0.onnx (the model, 311 MB — too large for git, so it
# is provisioned by fetch_kokoro_model.sh / the Dockerfile's
# checksum-verified download, NOT tracked) and voices-v1.0.bin
# (the voice pack, git-tracked like the Piper models). Both are
# Apache 2.0 at every layer — code, weights and voices — verified
# from primary sources in the investigation report (§6).
#
# The two Piper voices above are RETIRED from the customer
# catalogue: their training datasets fail the commercial gate on
# the cautious reading (lessac: Blizzard 2013 research-only;
# ryan: CC BY-NC-SA NonCommercial). The Piper code path
# (_run_piper + PIPER_VOICE_CATALOGUE) is retained behind the
# engine seam so a commercially clean Piper voice set can be
# restored as a fallback later; it is not reachable from the
# customer catalogue.
# ------------------------------------------------------------------

KOKORO_MODEL = BASE_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES_FILE = BASE_DIR / "voices-v1.0.bin"

# The genuine Kokoro v1.0 voice pack: exactly 54 voices. Every
# one of them is catalogued below — the count is the pack's real
# contents, never padded (founder asked for "up to 100 if
# possible"; 54 is what genuinely exists). The first letter of
# each id is its language group; the second is f/m. A test
# (tests/test_kokoro_narration.py) asserts this list equals the
# installed voices file's contents, so the catalogue can never
# drift from the pack in either direction.
_KOKORO_GROUPS = {
    # prefix -> (language code, espeak-ng lang, region label)
    "a": ("en", "en-us", "English (US)"),
    "b": ("en", "en-gb", "English (UK)"),
    "e": ("es", "es", "Spanish"),
    "f": ("fr", "fr-fr", "French"),
    "h": ("hi", "hi", "Hindi"),
    "i": ("it", "it", "Italian"),
    "j": ("ja", "ja", "Japanese"),
    "p": ("pt", "pt-br", "Portuguese (Brazil)"),
    "z": ("zh", "cmn", "Mandarin Chinese"),
}

KOKORO_VOICE_IDS = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica",
    "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah",
    "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir",
    "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily", "bm_daniel",
    "bm_fable", "bm_george", "bm_lewis",
    "ef_dora", "em_alex", "em_santa",
    "ff_siwis",
    "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    "if_sara", "im_nicola",
    "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro",
    "jm_kumo",
    "pf_dora", "pm_alex", "pm_santa",
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi",
    "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
]


def _kokoro_meta(voice_id):
    language, espeak_lang, region = _KOKORO_GROUPS[voice_id[0]]
    return {
        "language": language,
        "espeak_lang": espeak_lang,
        "region": region,
        "gender": "female" if voice_id[1] == "f" else "male",
        "name": voice_id[3:].replace("_", " ").title(),
    }


KOKORO_VOICE_META = {vid: _kokoro_meta(vid) for vid in KOKORO_VOICE_IDS}

# The primary documentary voice: af_heart — the pack's
# creator-graded A voice and the investigation's strongest
# all-round measured performer (natural documentary pace,
# deterministic output). The default alias resolves to it.
PRIMARY_VOICE_ID = "af_heart"

# The quoted/interview-beat contrast voice for English
# productions (male, against the primary's female). Other
# languages pick their own contrast in secondary_voice_for().
SECONDARY_VOICE_ID = "am_michael"

DEFAULT_VOICE_ID = "default"

# Stage 3J (AUD-29): the voice catalogue — the voices users can
# actually choose. Every entry maps to the shared Kokoro assets
# (one model + one voices file for the whole pack). Any other
# requested id falls back to the default voice and the fallback
# is reported, never hidden.
_ordered_voice_ids = [PRIMARY_VOICE_ID] + [
    vid for vid in KOKORO_VOICE_IDS if vid != PRIMARY_VOICE_ID
]
VOICE_CATALOGUE = {
    vid: (KOKORO_MODEL, KOKORO_VOICES_FILE) for vid in _ordered_voice_ids
}
VOICE_CATALOGUE[DEFAULT_VOICE_ID] = (KOKORO_MODEL, KOKORO_VOICES_FILE)

# Alias structure, declared here rather than derived from shared
# files: with one model file backing every voice, file-pair
# inference cannot tell an alias from a voice. "default" is the
# only alias; it names the primary voice.
VOICE_ALIASES = {DEFAULT_VOICE_ID: PRIMARY_VOICE_ID}

# The RETIRED Piper registry — kept only so the retained _run_piper
# fallback path stays callable. NOT the customer catalogue: its
# two voices carry the dataset-licence defect described above and
# must not be re-exposed without per-voice licence verification.
PIPER_VOICE_CATALOGUE = {
    "en_US-lessac-medium": (PIPER_MODEL, PIPER_CONFIG),
    "en_US-ryan-medium": (SECONDARY_MODEL, SECONDARY_CONFIG),
    "default": (PIPER_MODEL, PIPER_CONFIG),
}


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


def canonical_voice_id(voice_id):
    """The real voice an id resolves to: the requested id resolved
    through the catalogue, with the "default" alias dereferenced
    to the primary voice it names."""
    _model, _config, resolved_id, _fell_back = resolve_voice(voice_id)
    return VOICE_ALIASES.get(resolved_id, resolved_id)


def voice_language(voice_id):
    """The language code (services.languages vocabulary) a voice
    genuinely speaks, e.g. "es" for ef_dora."""
    return KOKORO_VOICE_META[canonical_voice_id(voice_id)]["language"]


def default_voice_for_language(language_code):
    """The primary installed voice for a language — the first
    voice of that language in catalogue order (for English, the
    primary documentary voice) — or None when the language has
    no installed voice."""
    for voice_id in _ordered_voice_ids:
        if KOKORO_VOICE_META[voice_id]["language"] == language_code:
            return voice_id
    return None


# Preferred quoted-beat contrast per language, where the pack
# offers a natural pairing (a male voice against the language's
# female primary, or the reverse). Languages with a single voice
# (French) have no entry and no contrast — assign_sentence_voices
# then keeps the exact single-voice path.
_SECONDARY_PREFERENCE = {
    "en": "am_michael",
    "es": "em_alex",
    "hi": "hm_omega",
    "it": "im_nicola",
    "ja": "jm_kumo",
    "pt": "pm_alex",
    "zh": "zm_yunyang",
}


def secondary_voice_for(primary_voice_id):
    """The contrasting voice for quoted / interview beats, in the
    PRIMARY voice's own language — a Spanish production's quotes
    are never read by an English voice. Returns None when the
    language's pack offers no second voice."""
    canonical = canonical_voice_id(primary_voice_id)
    meta = KOKORO_VOICE_META[canonical]
    candidates = [
        vid
        for vid in _ordered_voice_ids
        if vid != canonical
        and KOKORO_VOICE_META[vid]["language"] == meta["language"]
    ]
    if not candidates:
        return None
    preferred = _SECONDARY_PREFERENCE.get(meta["language"])
    if preferred in candidates:
        return preferred
    for candidate in candidates:
        if KOKORO_VOICE_META[candidate]["gender"] != meta["gender"]:
            return candidate
    return candidates[0]


def secondary_voice_available():
    """True when the narration assets are actually installed (a
    catalogue entry pointing at missing files must never be
    assigned)."""
    return KOKORO_MODEL.exists() and KOKORO_VOICES_FILE.exists()


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
    # The contrast voice belongs to the primary voice's language
    # (secondary_voice_for); a language with a single installed
    # voice has no contrast and keeps the single-voice path.
    secondary = secondary_voice_for(primary)
    if secondary is None:
        return None
    voices = [
        secondary if _is_quoted_or_interview(item) else primary
        for item in items
    ]
    if secondary not in voices:
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


def _resolve_piper_voice(voice_id):
    """Resolve an id through the RETAINED Piper registry (see
    PIPER_VOICE_CATALOGUE). Same fallback semantics as
    resolve_voice, over the Piper entries only."""
    if voice_id is None or str(voice_id).strip() == "":
        model, config = PIPER_VOICE_CATALOGUE[DEFAULT_VOICE_ID]
        return model, config, DEFAULT_VOICE_ID, False
    requested = str(voice_id).strip()
    if requested in PIPER_VOICE_CATALOGUE:
        model, config = PIPER_VOICE_CATALOGUE[requested]
        return model, config, requested, False
    model, config = PIPER_VOICE_CATALOGUE[DEFAULT_VOICE_ID]
    return model, config, DEFAULT_VOICE_ID, True


def _run_piper(text, output_file, voice_id=None):
    """Run Piper for a single piece of text.

    RETAINED FALLBACK PATH (not the production engine since the
    2026-11-26 Kokoro switch): kept callable behind the engine
    seam so a commercially clean Piper voice set can be restored
    later. This is the only place in this module that runs a
    subprocess. ``voice_id`` is resolved through the retained
    Piper registry: the resolved model's path is what reaches
    the Piper command. Narration is always synthesised at the
    provider's native rate: there is deliberately NO speed /
    length-scale parameter anywhere in this call path (founder
    ruling, 2026-11-22 — the voice model is third-party and
    speed control is unsupported).
    """
    output_file = Path(output_file)
    model, config, _resolved_id, _fell_back = _resolve_piper_voice(voice_id)

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


_kokoro_engine = None


def _kokoro():
    """The per-process Kokoro engine instance (lazy singleton).

    Loading the model takes ~2–4 s (TTS investigation §1); a
    worker process loads it once and reuses it for every sentence
    of every job it runs. The engine holds no per-job state, and
    kokoro-onnx serialises its espeak phonemisation internally.
    """
    global _kokoro_engine
    if _kokoro_engine is None:
        from kokoro_onnx import Kokoro

        _kokoro_engine = Kokoro(
            str(KOKORO_MODEL), str(KOKORO_VOICES_FILE)
        )
    return _kokoro_engine


_SAINT_RE = re.compile(r"\bSt\.(?=\s+[A-Z])")


def _normalize_for_synthesis(text):
    """Caller-side text normalisation for the synthesiser ONLY
    (TTS investigation §3): espeak reads the abbreviation "St."
    literally, so "St. Louis" is never pronounced "Saint Louis"
    by either engine. Expanding it when a capitalised name
    follows fixes the demonstrated failure. The script text
    itself — and therefore captions and timing — is never
    altered; only the string handed to the synthesiser changes.
    """
    return _SAINT_RE.sub("Saint", text)


def _run_kokoro(text, output_file, voice_id=None):
    """Synthesise a single piece of text with Kokoro-82M.

    Speed is pinned to the provider's native rate: the literal
    1.0 below is the ONLY speed value in this call path and it is
    never read from configuration, options or the environment
    (founder ruling, 2026-11-22 — no narration speed adjustment
    anywhere). Misaki bracket pronunciation overrides are NOT
    used: kokoro-onnx reads them aloud as gibberish (TTS
    investigation §3); caller-side respelling in the script is
    the supported correction surface.
    """
    import numpy as np

    output_file = Path(output_file)
    canonical = canonical_voice_id(voice_id)
    meta = KOKORO_VOICE_META[canonical]

    if not KOKORO_MODEL.exists():
        raise FileNotFoundError(
            f"Kokoro model not found: {KOKORO_MODEL}"
        )

    if not KOKORO_VOICES_FILE.exists():
        raise FileNotFoundError(
            f"Kokoro voices file not found: {KOKORO_VOICES_FILE}"
        )

    samples, sample_rate = _kokoro().create(
        _normalize_for_synthesis(text),
        voice=canonical,
        speed=1.0,
        lang=meta["espeak_lang"],
    )

    if samples is None or len(samples) == 0:
        raise RuntimeError(
            "Kokoro finished but produced no audio."
        )

    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")

    with wave.open(str(output_file), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(int(sample_rate))
        wav_file.writeframes(pcm.tobytes())

    if output_file.stat().st_size == 0:
        raise RuntimeError(
            "Kokoro created an empty audio file."
        )


def _synthesize_sentence(text, output_file, voice_id=None):
    """The engine seam (TTS investigation §9): every narration
    part is synthesised through here and nowhere else. Kokoro-82M
    is the production engine; _run_piper remains behind this seam
    as the retained fallback path for a future commercially
    clean Piper voice set."""
    _run_kokoro(text, output_file, voice_id=voice_id)


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
        "model": model_name or KOKORO_MODEL.name,
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
        "model": data.get("model") or KOKORO_MODEL.name,
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
            f"Narration model not found: {model}"
        )

    if not config.exists():
        raise FileNotFoundError(
            f"Narration voice data not found: {config}"
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
                    f"Narration model not found for sentence voice "
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
                    _synthesize_sentence(
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
            "Narration finished but no audio file was created."
        )

    file_size = output_file.stat().st_size

    if file_size == 0:
        raise RuntimeError(
            "Narration created an empty audio file."
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
        "engine": "kokoro",
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
