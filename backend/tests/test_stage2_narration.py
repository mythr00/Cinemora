"""Stage 2D: chunked, resumable narration (services/narration.py).

``narration._run_piper`` is monkeypatched with a fake that writes a
REAL tiny WAV via the wave module (22050 Hz, 1 channel, 2-byte width,
1000 + index*100 frames of silence) and records every call. The fake
never needs the real Piper binary, but generate_narration's own
PIPER_MODEL / PIPER_CONFIG existence checks run for real — the model
files live in backend/, so those checks pass unpatched.
"""

import inspect
import json
import wave
from pathlib import Path

import pytest

from services import narration
from services.narration import generate_narration

SENTENCES = [
    "Alpha bravo charlie delta.",
    "Echo foxtrot golf hotel.",
    "India juliet kilo lima.",
    "Mike november oscar papa.",
]
TEXT = " ".join(SENTENCES)


def _frames_for(index):
    return 1000 + index * 100


def _wav_frames(path):
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes()


class FakePiper:
    """Stand-in for narration._run_piper.

    fail_plan maps a sentence index to either a number of failures to
    raise before succeeding, or "always" to fail on every call.
    """

    def __init__(self, fail_plan=None):
        self.calls = []
        self.fail_plan = dict(fail_plan or {})

    def __call__(self, text, output_file, voice_id=None, speed=None):
        output_file = Path(output_file)
        index = int(output_file.stem.split("_")[1])
        self.calls.append(
            {
                "index": index,
                "text": text,
                "output_file": output_file,
                "voice_id": voice_id,
                "speed": speed,
            }
        )

        failures = self.fail_plan.get(index, 0)
        if failures == "always" or failures > 0:
            if failures != "always":
                self.fail_plan[index] = failures - 1
            raise RuntimeError(f"fake piper failure for sentence {index}")

        with wave.open(str(output_file), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(22050)
            wav.writeframes(b"\x00\x00" * _frames_for(index))

    def called_indices(self):
        return [call["index"] for call in self.calls]

    def clear(self):
        self.calls.clear()


@pytest.fixture
def install_fake(monkeypatch):
    def _install(fail_plan=None):
        fake = FakePiper(fail_plan=fail_plan)
        monkeypatch.setattr(narration, "_run_piper", fake)
        return fake

    return _install


def test_crash_resume_only_synthesizes_missing_parts(tmp_path, install_fake):
    output = tmp_path / "narration.wav"
    parts_dir = tmp_path / "narration_parts"

    fake = install_fake(fail_plan={2: "always"})

    with pytest.raises(RuntimeError):
        generate_narration(text=TEXT, output_path=str(output))

    # Parts synthesized before the crash survive on disk.
    assert (parts_dir / "part_000.wav").exists()
    assert (parts_dir / "part_000.wav").stat().st_size > 0
    assert (parts_dir / "part_001.wav").exists()
    assert (parts_dir / "part_001.wav").stat().st_size > 0

    # Run 2 with a healthy fake resumes where run 1 stopped.
    fake.fail_plan = {}
    fake.clear()

    result = generate_narration(text=TEXT, output_path=str(output))

    # Stage 3J: generate_narration returns voice metadata (was the
    # output path string).
    assert isinstance(result, dict)
    assert result["output_file"] == str(output)
    assert fake.called_indices() == [2, 3]

    assert output.exists()
    expected_frames = sum(_frames_for(i) for i in range(4))
    assert _wav_frames(output) == expected_frames

    # Stage 3I: narration produces audio ONLY — captions.json is
    # written solely by services.captions.build_captions, so it must
    # NOT appear here (previously this asserted the proportional
    # captions narration used to write itself).
    assert not (tmp_path / "captions.json").exists()


def test_hash_stability_and_selective_invalidation(tmp_path, install_fake):
    output = tmp_path / "narration.wav"
    fake = install_fake()

    generate_narration(text=TEXT, output_path=str(output))
    assert fake.called_indices() == [0, 1, 2, 3]

    # Identical re-run: every part is reused, zero synthesis calls.
    fake.clear()
    generate_narration(text=TEXT, output_path=str(output))
    assert fake.calls == []

    # Changing ONE sentence re-synthesizes exactly that part.
    changed_sentences = list(SENTENCES)
    changed_sentences[1] = "Echo foxtrot golf changed."
    changed_text = " ".join(changed_sentences)

    fake.clear()
    generate_narration(text=changed_text, output_path=str(output))
    assert fake.called_indices() == [1]
    assert fake.calls[0]["text"] == "Echo foxtrot golf changed."


def test_speed_change_resynthesizes_all_parts(tmp_path, install_fake):
    output = tmp_path / "narration.wav"
    fake = install_fake()

    generate_narration(text=TEXT, output_path=str(output))
    assert fake.called_indices() == [0, 1, 2, 3]

    fake.clear()
    generate_narration(text=TEXT, output_path=str(output), speed=1.25)

    assert fake.called_indices() == [0, 1, 2, 3]
    assert all(call["speed"] == 1.25 for call in fake.calls)


def test_retry_succeeds_within_three_attempts(tmp_path, install_fake):
    output = tmp_path / "narration.wav"
    fake = install_fake(fail_plan={0: 2})

    result = generate_narration(text=TEXT, output_path=str(output))

    # Stage 3J: returns voice metadata dict (was the path string).
    assert isinstance(result, dict)
    assert result["output_file"] == str(output)
    assert output.exists()
    sentence_zero_calls = [
        call for call in fake.calls if call["index"] == 0
    ]
    assert len(sentence_zero_calls) == 3


def test_backward_compatible_signature_and_keyword_call(
    tmp_path, install_fake
):
    signature = inspect.signature(narration.generate_narration)
    parameters = list(signature.parameters.values())

    assert [p.name for p in parameters] == [
        "text",
        "output_path",
        "voice_id",
        "speed",
        # Phase 2.4: optional per-sentence voices — appended with
        # a default, so every historical call shape still works.
        "sentence_voices",
    ]
    assert parameters[2].default is None
    assert parameters[3].default is None
    assert parameters[4].default is None

    install_fake()
    output = tmp_path / "narration.wav"

    # Called exactly as jobs.py calls it; single-sentence text works.
    result = generate_narration(
        text="Only one sentence here.",
        output_path=str(output),
    )

    # Stage 3J: returns voice metadata dict (was the path string).
    assert isinstance(result, dict)
    assert result["output_file"] == str(output)
    assert output.exists()
    assert _wav_frames(output) == _frames_for(0)
