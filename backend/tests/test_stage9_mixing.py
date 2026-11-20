"""Stage J — professional mixing (P4) + editor music parity (P5).

Covers the ONE mix configuration (services.music.resolve_mix_
config: option > env > genre-pack mix_preset > default), bed
shaping (fades, crossfaded loops), loudness normalisation of the
mixed soundtrack (loudnorm two-pass + limiter backstop, measured
afterwards), the soundtrack record's mix section, the Stage J
gain-matched recalibration of the M10 gap detector, and the
editor parity pieces: recorded music files in the media index,
revision renders carrying the mix provenance, and the revision
certificate's mix fields.

Editor integration runs against the Stage 6 synthetic production
(real Piper narration, real renders), like Stage H's tests.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import os
import struct
import subprocess
import wave
from pathlib import Path

import pytest

import stage6_fixture as fx
from services import music
from services.plan_document import build_media_index


# ---------------------------------------------------------------
# helpers
# ---------------------------------------------------------------

def _tone(path, frequency=220, seconds=3.0, rate=48000, amplitude=0.8):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
            f"sine=frequency={frequency}:duration={seconds}"
            f":sample_rate={rate}",
            "-af", f"volume={amplitude}",
            str(path),
        ],
        check=True, timeout=60,
    )
    return path


def _silent_narration(path, seconds=6.0, rate=48000):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def _bursts_narration(path, seconds=6.0, rate=48000, amplitude=0.05):
    """Tone bursts with silence gaps at a low level (quiet overall
    loudness — the normalisation test's input)."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = b""
        for i in range(int(rate * seconds)):
            t = i / rate
            value = (
                int(amplitude * 32767 * math.sin(2 * math.pi * 440 * t))
                if (t % 2.0) < 1.0
                else 0
            )
            frames += struct.pack("<h", value)
        wav.writeframes(frames)
    return path


def _decode_samples(path):
    raw = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(path),
            "-ac", "1", "-ar", "48000", "-f", "s16le", "-",
        ],
        capture_output=True, check=True, timeout=120,
    ).stdout
    samples = array.array("h")
    samples.frombytes(raw)
    return samples


def _window_db(samples, start_s, end_s, rate=48000):
    segment = samples[int(start_s * rate):int(end_s * rate)]
    if not segment:
        return None
    rms = math.sqrt(sum(x * x for x in segment) / len(segment))
    return 20 * math.log10(rms / 32768.0) if rms > 0 else -120.0


# ---------------------------------------------------------------
# resolve_mix_config: precedence
# ---------------------------------------------------------------

def test_mix_config_defaults():
    config = music.resolve_mix_config()
    assert config["music_volume"] == music.MUSIC_VOLUME
    assert config["music_volume_source"] == "default"
    assert config["fade_in_seconds"] == 1.5
    assert config["fade_out_seconds"] == 2.5
    assert config["loop_crossfade_seconds"] == 1.0
    assert config["duck_threshold"] == 0.03
    assert config["duck_ratio"] == music.DUCK_RATIO
    assert config["duck_attack_ms"] == music.DUCK_ATTACK
    assert config["duck_release_ms"] == music.DUCK_RELEASE
    assert config["normalise"] is True
    assert config["target_lufs"] == -14.0
    assert config["max_true_peak_dbtp"] == -1.0
    assert config["preset"] == {
        "genre_pack": None, "applied": False, "name": "default",
    }


def test_mix_config_genre_preset_applies():
    config = music.resolve_mix_config(genre_pack="true_crime")
    assert config["music_volume"] == pytest.approx(0.08)
    assert config["music_volume_source"] == "genre_preset"
    assert config["fade_in_seconds"] == 1.0
    assert config["fade_out_seconds"] == 2.0
    assert config["duck_ratio"] == 10
    assert config["preset"]["name"] == "true_crime"
    assert config["preset"]["applied"] is True

    history = music.resolve_mix_config(genre_pack="history")
    assert history["fade_in_seconds"] == 2.0
    assert history["fade_out_seconds"] == 3.0
    assert history["duck_ratio"] == 8


def test_mix_config_option_beats_preset_and_env(monkeypatch):
    monkeypatch.setenv("CINEMORA_MUSIC_FADE_IN_SECONDS", "0.7")
    env_only = music.resolve_mix_config(genre_pack="true_crime")
    assert env_only["fade_in_seconds"] == 0.7  # env beats preset (1.0)

    config = music.resolve_mix_config(
        options={"music_fade_in_seconds": 0.5, "music_volume": 0.3},
        genre_pack="true_crime",
    )
    assert config["fade_in_seconds"] == 0.5  # option beats env
    assert config["music_volume"] == pytest.approx(0.3)
    assert config["music_volume_source"] == "option"
    # Untouched parameters still come from the preset.
    assert config["duck_ratio"] == 10


def test_mix_config_explicit_volume_argument_wins():
    config = music.resolve_mix_config(
        genre_pack="true_crime", music_volume=0.25
    )
    assert config["music_volume"] == pytest.approx(0.25)
    assert config["music_volume_source"] == "option"


# ---------------------------------------------------------------
# Bed shaping: fades + crossfaded loop
# ---------------------------------------------------------------

def test_fades_shape_the_bed(tmp_path):
    narration = _silent_narration(tmp_path / "narration.wav", seconds=6.0)
    track = _tone(tmp_path / "track.wav", seconds=8.0)
    record = music.prepare_soundtrack(
        str(narration), str(tmp_path / "soundtrack.wav"),
        music_file=str(track), music_volume=0.5,
    )
    assert record["mix"]["loop"]["mode"] == "none"  # longer track: no loop
    samples = _decode_samples(tmp_path / "soundtrack.wav")
    head = _window_db(samples, 0.0, 0.25)
    middle = _window_db(samples, 2.75, 3.25)
    tail = _window_db(samples, 5.75, 6.0)
    # The fades are measurable in the bed itself: head and tail sit
    # far below the full-level middle (normalisation scales the
    # whole soundtrack uniformly, so the ratios survive it).
    assert head < middle - 12.0
    assert tail < middle - 12.0


def test_crossfaded_loop_has_no_seam_click(tmp_path):
    # 220 Hz for 2.03 s = 446.6 cycles: a hard loop restarts
    # mid-cycle and clicks; the crossfaded loop must not.
    track = _tone(tmp_path / "short.wav", seconds=2.03)
    config = music.resolve_mix_config()
    bed, info = music._build_music_bed(
        track, 7.0, config, tmp_path / "work"
    )
    assert info["mode"] == "crossfade"
    assert info["crossfade_seconds"] == pytest.approx(2.03 * 0.4, abs=0.01)
    probe = music.probe_audio_file(bed)
    assert probe["duration_seconds"] >= 7.0

    hard = tmp_path / "hard.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-stream_loop", "-1",
            "-i", str(track), "-af", "atrim=0:7,asetpts=N/SR/TB",
            str(hard),
        ],
        check=True, timeout=60,
    )

    def max_jump(path):
        samples = _decode_samples(path)
        return max(
            abs(samples[i + 1] - samples[i]) for i in range(len(samples) - 1)
        )

    assert max_jump(bed) < 0.6 * max_jump(hard)


def test_prepare_records_crossfade_loop(tmp_path):
    narration = _silent_narration(tmp_path / "narration.wav", seconds=7.0)
    track = _tone(tmp_path / "short.wav", seconds=2.03)
    record = music.prepare_soundtrack(
        str(narration), str(tmp_path / "soundtrack.wav"),
        music_file=str(track),
    )
    loop = record["mix"]["loop"]
    assert loop["mode"] == "crossfade"
    assert loop["track_duration_seconds"] == pytest.approx(2.03, abs=0.05)
    assert loop["bed_duration_seconds"] == pytest.approx(7.0)


# ---------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------

def test_normalisation_hits_target_for_quiet_narration(tmp_path):
    narration = _bursts_narration(tmp_path / "narration.wav", amplitude=0.05)
    record = music.prepare_soundtrack(
        str(narration), str(tmp_path / "soundtrack.wav")
    )
    normalisation = record["mix"]["normalisation"]
    assert normalisation["applied"] is True
    assert normalisation["method"] == "loudnorm_two_pass_linear"
    assert normalisation["pre_integrated_lufs"] < -20.0
    levels = music.measure_audio_levels(tmp_path / "soundtrack.wav")
    assert abs(levels["integrated_lufs"] - (-14.0)) <= 1.0
    assert levels["true_peak_dbtp"] <= -1.0 + 0.1


def test_normalisation_respects_true_peak_ceiling(tmp_path):
    narration = _bursts_narration(tmp_path / "narration.wav", amplitude=0.95)
    music.prepare_soundtrack(
        str(narration), str(tmp_path / "soundtrack.wav")
    )
    levels = music.measure_audio_levels(tmp_path / "soundtrack.wav")
    assert levels["true_peak_dbtp"] <= -1.0 + 0.1
    assert abs(levels["integrated_lufs"] - (-14.0)) <= 1.0


def test_normalisation_can_be_disabled_and_records_it(tmp_path):
    narration = _bursts_narration(tmp_path / "narration.wav", amplitude=0.05)
    config = music.resolve_mix_config(options={"music_normalise": False})
    record = music.prepare_soundtrack(
        str(narration), str(tmp_path / "soundtrack.wav"), mix_config=config
    )
    normalisation = record["mix"]["normalisation"]
    assert normalisation["applied"] is False
    assert normalisation["reason"] == "disabled_by_configuration"
    levels = music.measure_audio_levels(tmp_path / "soundtrack.wav")
    assert levels["integrated_lufs"] < -20.0  # left quiet, as mixed


def test_record_mix_section_carries_preset_and_ducking(tmp_path):
    narration = _silent_narration(tmp_path / "narration.wav", seconds=4.0)
    track = _tone(tmp_path / "track.wav", seconds=5.0)
    config = music.resolve_mix_config(genre_pack="true_crime")
    record = music.prepare_soundtrack(
        str(narration), str(tmp_path / "soundtrack.wav"),
        music_file=str(track), mix_config=config,
    )
    mix = record["mix"]
    assert mix["preset"]["name"] == "true_crime"
    assert mix["music_volume"] == pytest.approx(0.08)
    assert mix["music_volume_source"] == "genre_preset"
    assert mix["ducking"] == {
        "threshold": 0.03, "ratio": 10,
        "attack_ms": music.DUCK_ATTACK, "release_ms": music.DUCK_RELEASE,
    }
    assert mix["gain_staging"]["bed_in_gaps"] == (
        "ducking_release_to_pre_duck_level"
    )
    assert record["music_volume"] == pytest.approx(0.08)


# ---------------------------------------------------------------
# M10 detector on the new chain (gain-matched recalibration)
# ---------------------------------------------------------------

def test_detector_ignores_pure_gain_change(tmp_path):
    """A narration-only soundtrack is gain-adjusted by
    normalisation; the detector must still read it as NO music
    (the Stage J failure mode: raw gap difference -51.8 dBFS on a
    disabled editor render, above the -65 threshold)."""
    narration = _bursts_narration(
        tmp_path / "narration.wav", seconds=6.0, amplitude=0.2
    )
    plain = tmp_path / "plain.wav"
    music.prepare_soundtrack(str(narration), str(plain))
    analysis = music.audio_difference_analysis(plain, narration)
    assert analysis["gap_difference_rms_dbfs"] < music.MUSIC_GAP_THRESHOLD_DBFS
    assert analysis["reference_gain_db"] is not None
    verdict = music.assess_audio_provenance("disabled", plain, narration)
    assert verdict["music_detected"] is False
    assert verdict["consistent"] is True


def test_detector_still_hears_the_bed_on_the_new_chain(tmp_path):
    narration = _bursts_narration(
        tmp_path / "narration.wav", seconds=6.0, amplitude=0.2
    )
    track = _tone(tmp_path / "track.wav", seconds=3.0)
    mixed = tmp_path / "mixed.wav"
    music.prepare_soundtrack(
        str(narration), str(mixed), music_file=str(track), music_volume=0.2
    )
    verdict = music.assess_audio_provenance("applied", mixed, narration)
    assert verdict["music_detected"] is True
    assert verdict["consistent"] is True


# ---------------------------------------------------------------
# P5: media index serves recorded music files
# ---------------------------------------------------------------

def _media_key(project_dir, path):
    relative = Path(path).resolve().relative_to(
        Path(project_dir).resolve()
    ).as_posix()
    return hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20]


def test_media_index_includes_recorded_music(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    production_music = _tone(project_dir / "music.wav", seconds=2.0)
    revision_music = _tone(project_dir / "music_alt.wav", seconds=2.0)
    unrecorded = _tone(project_dir / "stray.wav", seconds=2.0)
    (project_dir / "soundtrack.json").write_text(json.dumps({
        "state": "applied",
        "identity": {"file_path": str(production_music)},
    }))
    (project_dir / "revision_abc.json").write_text(json.dumps({
        "revision_id": "abc",
        "exports": [],
        "tracks": {
            "music": {
                "file": str(revision_music),
                "soundtrack_record": None,
            },
        },
    }))
    index = build_media_index("proj", project_dir)
    assert index[_media_key(project_dir, production_music)]["kind"] == "music"
    assert (
        index[_media_key(project_dir, revision_music)]["kind"]
        == "revision_music"
    )
    assert _media_key(project_dir, unrecorded) not in index


# ---------------------------------------------------------------
# P5: editor integration — changed music, mix provenance, preview
# ---------------------------------------------------------------

class SyncQueue:
    """Executes enqueued jobs immediately (worker stand-in)."""

    def enqueue(self, fn, *args, **kwargs):
        from types import SimpleNamespace

        kwargs.pop("at_front", None)
        fn(*args, **kwargs)
        return SimpleNamespace(id="job-sync")


@pytest.fixture()
def sync_queue(monkeypatch):
    import jobs

    monkeypatch.setattr(jobs, "video_queue", SyncQueue())
    return jobs.video_queue


@pytest.fixture(scope="module")
def production(tmp_path_factory):
    root = tmp_path_factory.mktemp("stage9-media")
    os.environ["CINEMORA_MEDIA_ROOT"] = str(root)
    built = fx.build_synthetic_production(root, "proj-stage9-editor")
    built["root"] = root
    return built


def _rev0(project_id):
    from services import editor

    editor.get_editor(project_id)
    for view in editor.list_revisions(project_id):
        if view["revision_number"] == 0:
            return editor.get_revision(project_id, view["revision_id"])
    raise AssertionError("no revision 0")


def test_editor_changed_music_render_mix_provenance_and_preview(
    production, sync_queue
):
    from services import editor

    project_id = production["project_id"]
    project_dir = production["project_dir"]
    _view0, doc0 = _rev0(project_id)

    # Change the music to a project file that is NOT an upload:
    # before Stage J the media index could not serve it.
    view1, _doc1 = editor.apply_operations(
        project_id,
        doc0["revision_id"],
        [{"type": "set_music",
          "music_file": str(production["music_alt"])}],
    )
    editor.request_render(project_id, view1["revision_id"])
    view_after, doc_after = editor.get_revision(
        project_id, view1["revision_id"]
    )
    assert view_after["status"] == "exported"
    export = doc_after["exports"][-1]

    # The revision's own soundtrack record carries the Stage J mix.
    work_dir = project_dir / "editor_renders" / view1["revision_id"]
    record = json.loads((work_dir / "soundtrack.json").read_text())
    assert record["state"] == "applied"
    assert record["identity"]["file_name"] == "music_alt.wav"
    mix = record["mix"]
    assert mix["preset"] is not None
    assert mix["ducking"]["ratio"] == music.DUCK_RATIO
    assert mix["normalisation"]["applied"] is True
    assert mix["loop"]["mode"] in ("none", "crossfade", "hard")

    # The export record states target + measured + normalised.
    assert export["audio_levels"]["normalised"] is True
    assert export["audio_levels"]["target_lufs"] == -14.0
    assert export["audio_levels"]["integrated_lufs"] is not None
    assert export["audio_provenance"]["consistent"] is True

    # The revision certificate describes the revision's mix.
    cert_music = editor.music_certificate_data(doc_after)
    assert cert_music["file_name"] == "music_alt.wav"
    assert cert_music["normalised"] is True
    assert cert_music["mix_preset"] is not None

    # Preview: the recorded revision music file is in the media
    # index under the key the frontend derives.
    index = build_media_index(project_id, project_dir)
    key = _media_key(project_dir, production["music_alt"])
    assert key in index
    assert index[key]["kind"] == "revision_music"
