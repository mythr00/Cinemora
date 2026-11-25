"""Phase 7 / A-06 preparation: operator-supplied music catalogue,
verified end-to-end at fixture level.

A directory holding a synthesized track + a catalogue.json, with
CINEMORA_MUSIC_CATALOGUE pointing at the catalogue file:
production_config loads it, selection (by id and by mood) resolves
to the real file, the flat projection carries it, and
prepare_soundtrack mixes it under narration at the configured
volume — proven by the record AND by measuring the mixed audio
against the narration in its quiet gap.
"""

import hashlib
import json
import math
import struct
import wave
from pathlib import Path

import pytest

from services import music as music_service
from services import production_config


def _write_wav(path, seconds, *, freq, gap=None, rate=22050, amp=0.5):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = b""
        for i in range(int(rate * seconds)):
            t = i / rate
            silent = gap is not None and gap[0] <= t < gap[1]
            value = 0 if silent else int(
                amp * 32767 * math.sin(2 * math.pi * freq * t))
            frames += struct.pack("<h", value)
        wav.writeframes(frames)
    return path


@pytest.fixture()
def catalogue_dir(tmp_path, monkeypatch):
    root = tmp_path / "catalogue"
    root.mkdir()
    track = _write_wav(root / "calm_bed.wav", 6, freq=220, amp=0.6)
    sha = hashlib.sha256(Path(track).read_bytes()).hexdigest()
    catalogue = {
        "catalogue_version": 1,
        "tracks": [{
            "track_id": "calm-bed",
            "content_sha256": sha,
            "title": "Calm Bed (fixture)",
            "composer": None,
            "source_class": "user_supplied",
            "moods": ["calm"],
            "genres": [],
            "duration_seconds": 6.0,
            "probe": {"codec": "pcm_s16le", "sample_rate": 22050,
                      "channels": 1},
            "assets": {"full": "calm_bed.wav", "preview": None},
        }],
    }
    catalogue_file = root / "catalogue.json"
    catalogue_file.write_text(json.dumps(catalogue), encoding="utf-8")
    monkeypatch.setenv("CINEMORA_MUSIC_CATALOGUE", str(catalogue_file))
    return root


def _library_config(**music_kwargs):
    music = {"enabled": True, "source": "library"}
    music.update(music_kwargs)
    return production_config.clean_config({"music": music})


def test_catalogue_loads_and_library_state(catalogue_dir):
    state = production_config.library_state()
    assert state["configured"] is True
    assert state["available"] is True
    assert [t["track_id"] for t in state["tracks"]] == ["calm-bed"]
    assert state["tracks"][0]["licence_state"] == "unknown"


def test_selection_by_id_and_by_mood(catalogue_dir):
    track, path = production_config.resolve_library_track(
        _library_config(track_id="calm-bed"))
    assert track.track_id == "calm-bed"
    assert path == catalogue_dir / "calm_bed.wav"
    assert path.is_file()
    track2, path2 = production_config.resolve_library_track(
        _library_config(mood="calm"))
    assert track2.track_id == "calm-bed"
    assert path2 == path


def test_flat_projection_carries_library_track(catalogue_dir):
    config = _library_config(track_id="calm-bed", volume=0.2)
    flat = production_config.flat_options(config)
    assert flat["music_file"] == str(catalogue_dir / "calm_bed.wav")
    assert flat["music_track_id"] == "calm-bed"
    assert flat["music_volume"] == 0.2


def test_catalogue_track_mixes_under_narration(catalogue_dir, tmp_path):
    config = _library_config(track_id="calm-bed", volume=0.2)
    flat = production_config.flat_options(config)
    narration = _write_wav(
        tmp_path / "narration.wav", 4, freq=440, gap=(1.5, 2.5), amp=0.5)
    out = tmp_path / "soundtrack.wav"
    record = music_service.prepare_soundtrack(
        str(narration), str(out),
        music_file=flat["music_file"],
        music_volume=flat["music_volume"],
        source_type="library_track",
        decided_by="music_file",
        requested=True,
    )
    assert record["state"] == "applied"
    assert record["identity"]["file_name"] == "calm_bed.wav"
    assert record["identity"]["content_sha256"] == hashlib.sha256(
        (catalogue_dir / "calm_bed.wav").read_bytes()).hexdigest()
    assert out.is_file()
    # The bed is measurable in the narration's quiet gap: the
    # record and the audio agree (M10 provenance check).
    provenance = music_service.assess_audio_provenance(
        "applied", str(out), str(narration))
    assert provenance["music_detected"] is True
    assert provenance["consistent"] is True
