"""Stage 5 (R4) — measured narration timing (AUDIO-001, annex D2).

Before this stage, production sentence windows were RECONSTRUCTED
from captions: Whisper word timings when available, a proportional
split in the caption writer, and — when captions carried no words —
a uniform total/N split in timeline.align_sentences_to_segments.
Only the editor measured real part durations, privately.

These tests pin the corrected contract:

- media_probe.probe_audio is the single probing authority and
  narration._audio_duration delegates to it;
- generate_narration measures every part (synthesised or reused)
  and records it on a v2 parts manifest; a v1 manifest is upgraded
  BY MEASUREMENT; an unprobeable part fails loudly;
- services.timing derives windows by cumulative sums of measured
  durations and raises timing_unavailable for anything unmeasured;
- production windows equal the measured durations EXACTLY even
  when Whisper fails outright (the Giant errata E-2 acceptance
  test) and even when a stale captions.json disagrees;
- captions use Whisper words only to place events INSIDE the
  measured windows (clamped), with explicit per-sentence
  alignment states and a run-level aggregate;
- the editor's retime consumes the same timing module (parity);
- the uniform-split fallback is GONE (source pin);
- AUDIO-003: one shared part hash — the editor has no private
  narration synthesis/hash path.
"""

from __future__ import annotations

import json
import os
import subprocess
import wave
from pathlib import Path

import pytest

import stage6_fixture as fx
from services import captions, media_probe, narration, timing
from services.timeline import build_project_timeline


# ---------------------------------------------------------------
# helpers
# ---------------------------------------------------------------

def _make_wav(path, seconds, rate=22050):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def _write_parts(project_dir, durations, version=2, measured=True):
    """Real WAV parts + a parts manifest on disk. Returns the
    manifest dict as written."""
    project_dir = Path(project_dir)
    parts_dir = project_dir / "narration_parts"
    parts = []
    for index, seconds in enumerate(durations):
        name = f"part_{index:03d}.wav"
        _make_wav(parts_dir / name, seconds)
        entry = {
            "index": index,
            "file": name,
            "hash": f"hash-{index}",
            "sentence_sha256": f"sha-{index}",
        }
        if measured:
            entry["measured_duration_seconds"] = float(seconds)
            entry["duration_source"] = "ffprobe_part"
        parts.append(entry)
    manifest = {"version": version, "model": "fixture", "parts": parts}
    (parts_dir / "parts_manifest.json").write_text(json.dumps(manifest))
    return manifest


def _visual(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 4096)
    return {
        "file_path": str(path), "path": str(path), "type": "video",
        "title": name, "verification": {"accepted": True},
    }


def _scenes(tmp_path, texts):
    visual = _visual(tmp_path, "visual.mp4")
    return [{
        "scene": {"scene_number": 1},
        "scene_number": 1,
        "duration_seconds": 10,
        "videos": [visual],
        "images": [],
        "sentences": [
            {
                "sentence_number": i + 1,
                "sentence_text": text,
                "videos": [visual],
                "images": [],
            }
            for i, text in enumerate(texts)
        ],
    }]


def _manifest_dict(durations):
    return {
        "version": 2,
        "model": "fixture",
        "parts": [
            {
                "index": i,
                "file": f"part_{i:03d}.wav",
                "hash": f"hash-{i}",
                "sentence_sha256": f"sha-{i}",
                "measured_duration_seconds": float(d),
                "duration_source": "ffprobe_part",
            }
            for i, d in enumerate(durations)
        ],
    }


# ---------------------------------------------------------------
# probe_audio: the single probing authority
# ---------------------------------------------------------------

def test_probe_audio_facts_and_none_contract(tmp_path):
    wav = _make_wav(tmp_path / "part.wav", 2.5)
    facts = media_probe.probe_audio(wav)
    assert facts is not None
    assert facts["duration_seconds"] == pytest.approx(2.5)
    assert facts["codec"] == "pcm_s16le"
    assert facts["sample_rate"] == 22050
    assert facts["channels"] == 1

    # Missing file -> None (never a fabricated duration).
    assert media_probe.probe_audio(tmp_path / "missing.wav") is None

    # Not audio at all -> None.
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"this is not audio" * 32)
    assert media_probe.probe_audio(junk) is None

    # Video without an audio stream -> None (probe_video's domain).
    video = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
            "-i", "color=c=black:size=64x64:duration=1:rate=5",
            str(video),
        ],
        check=True, timeout=60,
    )
    assert media_probe.probe_audio(video) is None


def test_audio_duration_delegates_to_probe(tmp_path):
    wav = _make_wav(tmp_path / "part.wav", 1.5)
    assert narration._audio_duration(wav) == pytest.approx(1.5)
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not a wav" * 64)
    with pytest.raises(RuntimeError):
        narration._audio_duration(junk)


# ---------------------------------------------------------------
# services.timing: cumulative measured windows, loud refusal
# ---------------------------------------------------------------

def test_sentence_windows_are_cumulative_measured_sums():
    windows = timing.sentence_windows(_manifest_dict([2.0, 3.5, 1.5]))
    assert [
        (w["sentence_index"], w["start"], w["end"], w["duration"])
        for w in windows
    ] == [
        (0, 0.0, 2.0, 2.0),
        (1, 2.0, 5.5, 3.5),
        (2, 5.5, 7.0, 1.5),
    ]
    assert all(
        w["timing_source"] == "measured_part" for w in windows
    )
    assert timing.total_duration(_manifest_dict([2.0, 3.5, 1.5])) == 7.0
    # A bare parts list is accepted too.
    bare = _manifest_dict([1.0, 1.0])["parts"]
    assert timing.total_duration(bare) == 2.0


def test_sentence_windows_refuse_unmeasured_or_gapped_parts():
    unmeasured = _manifest_dict([2.0, 3.5])
    del unmeasured["parts"][1]["measured_duration_seconds"]
    with pytest.raises(timing.TimingUnavailableError) as exc:
        timing.sentence_windows(unmeasured)
    assert "timing_unavailable" in str(exc.value)

    gapped = _manifest_dict([2.0, 3.5])
    gapped["parts"][1]["index"] = 7
    with pytest.raises(timing.TimingUnavailableError):
        timing.sentence_windows(gapped)

    with pytest.raises(timing.TimingUnavailableError):
        timing.sentence_windows({"version": 2, "parts": []})
    with pytest.raises(timing.TimingUnavailableError):
        timing.total_duration(None)


def test_timing_consistency_check_fires_on_truncated_concat(tmp_path):
    # Σ parts = 7.0s; the concatenated file was truncated to 5.0s.
    concat = _make_wav(tmp_path / "narration.wav", 5.0)
    probed = media_probe.probe_audio(concat)["duration_seconds"]
    record = timing.timing_consistency(7.0, probed)
    assert record["within_tolerance"] is False
    assert record["divergence_seconds"] == pytest.approx(2.0)
    assert record["parts_total_seconds"] == 7.0
    # Within tolerance (concat/frame rounding) -> no warning.
    assert timing.timing_consistency(7.0, 7.1)["within_tolerance"] is True


# ---------------------------------------------------------------
# generate_narration: manifest v2 with measured durations
# ---------------------------------------------------------------

def _stub_piper_by_words(monkeypatch):
    def fake_piper(text, output_file, voice_id=None, speed=None):
        words = max(1, len(str(text).split()))
        _make_wav(Path(output_file), seconds=0.5 * words)

    monkeypatch.setattr(narration, "_run_piper", fake_piper)


def test_generate_narration_records_measured_durations_v2(
    tmp_path, monkeypatch
):
    _stub_piper_by_words(monkeypatch)
    out = tmp_path / "narration.wav"
    narration.generate_narration(
        "One. Two words here. Four word sentence here.", str(out)
    )
    manifest = json.loads(
        (tmp_path / "narration_parts" / "parts_manifest.json").read_text()
    )
    assert manifest["version"] == 2
    durations = [
        entry["measured_duration_seconds"] for entry in manifest["parts"]
    ]
    assert durations == [0.5, 1.5, 2.0]
    assert all(
        entry["duration_source"] == "ffprobe_part"
        for entry in manifest["parts"]
    )

    # Reuse path: Piper must not run again, and the reloaded
    # manifest still records a measured duration for every part.
    def forbidden_piper(*args, **kwargs):
        raise AssertionError("Piper must not run for reused parts")

    monkeypatch.setattr(narration, "_run_piper", forbidden_piper)
    narration.generate_narration(
        "One. Two words here. Four word sentence here.", str(out)
    )
    manifest = json.loads(
        (tmp_path / "narration_parts" / "parts_manifest.json").read_text()
    )
    assert [
        entry["measured_duration_seconds"] for entry in manifest["parts"]
    ] == [0.5, 1.5, 2.0]


def test_v1_manifest_upgraded_by_measurement(tmp_path):
    project_dir = tmp_path / "proj"
    _write_parts(project_dir, [2.0, 3.0], version=1, measured=False)
    data = narration.load_parts_manifest_data(
        project_dir / "narration_parts" / "parts_manifest.json"
    )
    assert data["version"] == 2
    assert [
        entry["measured_duration_seconds"] for entry in data["parts"]
    ] == [2.0, 3.0]
    assert all(
        entry["duration_source"] == "ffprobe_part" for entry in data["parts"]
    )
    windows = timing.sentence_windows(data)
    assert [(w["start"], w["end"]) for w in windows] == [(0.0, 2.0), (2.0, 5.0)]

    # A v1 entry whose file is gone stays unmeasured — and timing
    # then refuses it loudly rather than estimating.
    (project_dir / "narration_parts" / "part_001.wav").unlink()
    data = narration.load_parts_manifest_data(
        project_dir / "narration_parts" / "parts_manifest.json"
    )
    with pytest.raises(timing.TimingUnavailableError):
        timing.sentence_windows(data)


def test_unprobeable_part_fails_loudly(tmp_path, monkeypatch):
    def garbage_piper(text, output_file, voice_id=None, speed=None):
        Path(output_file).write_bytes(b"definitely not a wav" * 64)

    monkeypatch.setattr(narration, "_run_piper", garbage_piper)
    with pytest.raises(RuntimeError) as exc:
        narration.generate_narration(
            "One sentence.", str(tmp_path / "narration.wav")
        )
    assert "could not be probed" in str(exc.value)


# ---------------------------------------------------------------
# Production: measured windows survive total Whisper failure
# (the Giant errata E-2 acceptance test)
# ---------------------------------------------------------------

_TEXTS = [
    "Alpha opens the story.",
    "Bravo continues the tale now.",
    "Charlie closes it.",
]
_DURATIONS = [2.0, 3.5, 1.5]


def test_whisper_failure_production_windows_stay_measured(
    tmp_path, monkeypatch
):
    project_id = "stage5-e2"
    media_root = tmp_path / "media"
    project_dir = media_root / project_id
    _write_parts(project_dir, _DURATIONS)
    _make_wav(project_dir / "narration.wav", sum(_DURATIONS))
    # A stale captions.json with WRONG (uniform, inflated) timings
    # sits in the project dir: the timeline must ignore it — its
    # windows come from the measured parts, not from captions.
    (project_dir / "captions.json").write_text(json.dumps({
        "duration": 99.0,
        "segments": [
            {"start": 0.0, "end": 33.0, "text": _TEXTS[0]},
            {"start": 33.0, "end": 66.0, "text": _TEXTS[1]},
            {"start": 66.0, "end": 99.0, "text": _TEXTS[2]},
        ],
    }))

    def whisper_down(*args, **kwargs):
        raise RuntimeError("whisper unavailable")

    monkeypatch.setattr(captions, "transcribe_audio", whisper_down)
    data = captions.build_captions(
        str(project_dir / "narration.wav"),
        " ".join(_TEXTS),
        str(project_dir / "captions.json"),
    )
    # Captions degrade honestly: every sentence alignment_failed,
    # single events ON the measured windows, provisional.
    assert data["captions_alignment"] == {
        "aligned": 0, "low_alignment": 0, "alignment_failed": 3,
    }
    assert data["provisional"] is True
    assert data["timing_source"] == "proportional"
    assert data["duration"] == 7.0
    assert [
        (s["start"], s["end"]) for s in data["segments"]
    ] == [(0.0, 2.0), (2.0, 5.5), (5.5, 7.0)]
    assert all(
        s["alignment_state"] == "alignment_failed"
        for s in data["sentences"]
    )

    result = build_project_timeline(
        project_id, _scenes(tmp_path, _TEXTS), output_dir=str(media_root)
    )
    assert result["timing_source"] == "measured_part"
    assert result["duration_seconds"] == 7.0
    assert result["narration_duration_seconds"] == 7.0
    scene_timings = result["scenes"][0]["sentence_timings"]
    assert [
        (t["start"], t["end"], t["duration"]) for t in scene_timings
    ] == [(0.0, 2.0, 2.0), (2.0, 5.5, 3.5), (5.5, 7.0, 1.5)]
    assert all(
        t["timing_source"] == "measured_part" for t in scene_timings
    )
    # No window is the uniform split (7 / 3 = 2.333...).
    assert all(t["duration"] != pytest.approx(7.0 / 3) for t in scene_timings)


def test_timeline_without_measured_manifest_is_timing_unavailable(tmp_path):
    project_id = "stage5-nomanifest"
    media_root = tmp_path / "media"
    project_dir = media_root / project_id
    project_dir.mkdir(parents=True)
    (project_dir / "captions.json").write_text(json.dumps({
        "duration": 10.0,
        "segments": [
            {"start": 0.0, "end": 5.0, "text": "One."},
            {"start": 5.0, "end": 10.0, "text": "Two."},
        ],
    }))
    with pytest.raises(timing.TimingUnavailableError) as exc:
        build_project_timeline(
            project_id,
            _scenes(tmp_path, ["One.", "Two."]),
            output_dir=str(media_root),
        )
    assert "timing_unavailable" in str(exc.value)

    # Manifest present but describing a different sentence count.
    _write_parts(project_dir, [5.0])
    with pytest.raises(timing.TimingUnavailableError):
        build_project_timeline(
            project_id,
            _scenes(tmp_path, ["One.", "Two."]),
            output_dir=str(media_root),
        )


# ---------------------------------------------------------------
# Captions: alignment states + clamping into measured windows
# ---------------------------------------------------------------

def test_alignment_states_and_window_clamping(tmp_path, monkeypatch):
    project_dir = tmp_path / "proj"
    _write_parts(project_dir, [2.0, 2.0, 2.0])
    _make_wav(project_dir / "narration.wav", 6.0)

    def word(text, start, end):
        return {"word": text, "start": start, "end": end}

    whisper = {
        "audio_file": "x",
        "language": "en",
        "language_probability": 0.99,
        "duration": 6.0,
        "segments": [
            {
                "start": 0.0, "end": 2.6,
                "text": "Alpha bravo charlie.",
                "words": [
                    word("Alpha", 0.0, 0.7),
                    word("bravo", 0.7, 1.4),
                    # Whisper's clock overruns the measured window:
                    # the event must be CLAMPED to the window end.
                    word("charlie.", 1.4, 2.6),
                ],
            },
            {
                "start": 2.0, "end": 4.0,
                "text": "Quantum photon nebula",
                "words": [
                    word("Quantum", 2.0, 2.7),
                    word("photon", 2.7, 3.3),
                    word("nebula", 3.3, 4.0),
                ],
            },
            {
                "start": 4.0, "end": 6.0,
                "text": "Gamma delta zeta",
                "words": [
                    word("Gamma", 4.0, 4.6),
                    word("delta", 4.6, 5.2),
                    word("zeta", 5.2, 6.0),
                ],
            },
        ],
    }
    monkeypatch.setattr(
        captions, "transcribe_audio", lambda *a, **kw: whisper
    )
    script = "Alpha bravo charlie. Beta kappa lambda. Gamma delta epsilon."
    data = captions.build_captions(
        str(project_dir / "narration.wav"),
        script,
        str(project_dir / "captions.json"),
    )

    assert data["timing_source"] == "whisper"
    assert data["duration"] == 6.0
    assert data["captions_alignment"] == {
        "aligned": 2, "low_alignment": 1, "alignment_failed": 0,
    }
    # One non-aligned sentence -> the document is provisional, and
    # the threshold that classified it is recorded in the artefact.
    assert data["provisional"] is True
    assert data["alignment_threshold"] == captions.ALIGNMENT_SCORE_THRESHOLD

    states = {s["sentence_index"]: s for s in data["sentences"]}
    assert states[0]["alignment_state"] == "aligned"
    assert states[0]["alignment_score"] == 1.0
    assert states[1]["alignment_state"] == "low_alignment"
    assert states[1]["alignment_score"] == 0.0
    assert states[2]["alignment_state"] == "aligned"
    assert states[2]["alignment_score"] == pytest.approx(2 / 3, abs=0.001)
    # Windows are the measured ones in ALL three states.
    assert [
        (s["start"], s["end"]) for s in data["sentences"]
    ] == [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]

    # Sentence 1's event was clamped INTO its window (Whisper said
    # the last word ends at 2.6; the measured window ends at 2.0).
    first_events = [
        s for s in data["segments"] if s["start"] < 2.0
    ]
    assert first_events
    assert max(s["end"] for s in first_events) == 2.0
    assert first_events[0]["words"][-1]["end"] == 2.0
    # The low-alignment sentence got ONE event spanning exactly its
    # measured window, with no word timings claimed.
    low_events = [
        s for s in data["segments"] if s["start"] == 2.0 and s["end"] == 4.0
    ]
    assert len(low_events) == 1
    assert low_events[0]["words"] == []
    assert low_events[0]["text"] == "Beta kappa lambda."


# ---------------------------------------------------------------
# The uniform-split fallback is gone (source pin)
# ---------------------------------------------------------------

_FROZEN_MARKERS = ("_before_", "_backup", "_corrupted", "_recovered")


def test_uniform_fallback_absent_from_live_services():
    services_dir = Path(__file__).resolve().parents[1] / "services"
    offenders = []
    for path in services_dir.rglob("*.py"):
        if any(marker in path.name for marker in _FROZEN_MARKERS):
            continue  # frozen snapshots: Stage 16 disposition
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "uniform_fallback" in text:
            offenders.append(path.name)
    assert offenders == []
    # The alignment helper lives in captions now, not in timeline.
    timeline_source = (services_dir / "timeline.py").read_text(
        encoding="utf-8"
    )
    assert "def align_sentences_to_segments" not in timeline_source


# ---------------------------------------------------------------
# AUDIO-003: one shared part hash (editor delegates to narration)
# ---------------------------------------------------------------

def test_single_part_hash_shared_by_editor_rebuild():
    script = (
        "The river crossing began at dawn. "
        "By noon the convoy reached the far bank."
    )
    production_sentences = narration._split_sentences(script)
    # run_revision_render joins the revision's sentence texts and
    # re-splits through generate_narration — the per-sentence part
    # hashes must agree with production's, or reuse silently breaks.
    rejoined = " ".join(s.strip() for s in production_sentences)
    editor_sentences = narration._split_sentences(rejoined)
    assert editor_sentences == production_sentences
    for produced, rejoined_sentence in zip(
        production_sentences, editor_sentences
    ):
        assert narration._part_hash(produced) == narration._part_hash(
            rejoined_sentence
        )

    # The editor carries no private Piper/hash path of its own: its
    # only narration synthesis is narration.generate_narration.
    import inspect

    from services import editor

    source = inspect.getsource(editor)
    assert "_run_piper" not in source
    assert "narration_service.generate_narration" in source


# ---------------------------------------------------------------
# Editor / production parity on a real synthetic production
# ---------------------------------------------------------------

@pytest.fixture(scope="module")
def parity_production(tmp_path_factory):
    root = tmp_path_factory.mktemp("stage5-media")
    os.environ["CINEMORA_MEDIA_ROOT"] = str(root)
    built = fx.build_synthetic_production(root, "proj-stage5-timing")
    built["root"] = root
    return built


class _SyncQueue:
    """Executes enqueued jobs immediately (worker stand-in)."""

    def enqueue(self, fn, *args, **kwargs):
        from types import SimpleNamespace

        kwargs.pop("at_front", None)
        fn(*args, **kwargs)
        return SimpleNamespace(id="job-sync")


@pytest.fixture()
def sync_queue(monkeypatch):
    import jobs

    monkeypatch.setattr(jobs, "video_queue", _SyncQueue())
    return jobs.video_queue


def test_editor_retime_windows_equal_production_timing(
    parity_production, sync_queue
):
    import inspect

    from services import editor
    from services import timeline as timeline_module

    project_id = parity_production["project_id"]
    editor.get_editor(project_id)  # ensures revision 0 exists
    view0 = next(
        v for v in editor.list_revisions(project_id)
        if v["revision_number"] == 0
    )
    _view, doc0 = editor.get_revision(project_id, view0["revision_id"])
    view1, _doc1 = editor.apply_operations(
        project_id,
        doc0["revision_id"],
        [{
            "type": "regenerate_narration_sentence",
            "sentence_number": 2,
            "text": "By midday the convoy crossed the river safely.",
        }],
    )
    editor.request_render(project_id, view1["revision_id"])
    view_after, doc_after = editor.get_revision(
        project_id, view1["revision_id"]
    )
    assert view_after["status"] == "exported"

    # The revision's sentence windows after the retime are EXACTLY
    # timing.sentence_windows over the revision's parts manifest —
    # the same authority production's timeline build consumes.
    work_dir = (
        parity_production["project_dir"]
        / "editor_renders"
        / view1["revision_id"]
    )
    manifest = narration.load_parts_manifest_data(
        work_dir / "narration_parts" / "parts_manifest.json"
    )
    windows = timing.sentence_windows(manifest)
    sentences = doc_after["tracks"]["narration"]["sentences"]
    assert len(sentences) == len(windows)
    for sentence, window in zip(sentences, windows):
        assert sentence["start"] == pytest.approx(window["start"])
        assert sentence["end"] == pytest.approx(window["end"])

    # Parity is structural, not parallel: both paths call the one
    # timing module. Pin the call sites so they cannot re-diverge.
    assert "timing_service.sentence_windows" in inspect.getsource(
        timeline_module.build_project_timeline
    )
    assert "timing_service.sentence_windows" in inspect.getsource(
        editor.run_revision_render
    )
