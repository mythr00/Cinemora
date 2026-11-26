"""Phase 1 durability: atomic publication of rendered media.

Before this change the renderer encoded ffmpeg output DIRECTLY
onto final paths (``-y`` + ``+faststart``), so a process killed
mid-encode left an unplayable file (no moov atom) sitting at the
final clip path; ``_render_timeline_impl`` deleted the existing
master ``documentary.mp4`` BEFORE the ~900s concat re-encode; and
``concatenate_clips`` unlinked the target before ``replace()``,
so a crash between the two lost the master entirely.

These tests pin the new contract:

- ffmpeg always encodes to a ``.partial-*`` sibling in the same
  directory; the final path is only ever populated by
  ``os.replace()`` after the encode succeeds (and, for the master,
  after frame-count validation passes).
- A failed/killed encode leaves NO file at the final path and no
  ``.partial-*`` leftovers, and never disturbs a pre-existing
  final file.
- ``audio_mixer.add_narration`` follows the same pattern.
"""

import subprocess

import pytest

from services import audio_mixer, renderer


PAYLOAD = b"\x00\x00\x00\x18ftypmp42" + b"P" * 4096
GARBAGE = b"partial-moov-less-garbage" * 100


def _partials(directory):
    return sorted(p.name for p in directory.glob("*.partial-*"))


def _fake_ffmpeg_ok(command, timeout=None):
    """Stub runner: 'encode' succeeds, writing a plausible file."""
    out = command[-1]
    with open(out, "wb") as handle:
        handle.write(PAYLOAD)
    return True


def _fake_ffmpeg_garbage_then_fail(command, timeout=None):
    """Stub runner: writes partial garbage to the target, then fails
    (the killed-mid-encode shape)."""
    out = command[-1]
    with open(out, "wb") as handle:
        handle.write(GARBAGE)
    return False


@pytest.fixture()
def source_media(tmp_path):
    src = tmp_path / "source.bin"
    src.write_bytes(PAYLOAD)
    return src


# ------------------------------------------------------------------
# create_image_clip / create_video_clip
# ------------------------------------------------------------------

@pytest.mark.parametrize("factory", ["image", "video"])
def test_failed_clip_encode_leaves_no_file_at_final_path(
    tmp_path, monkeypatch, source_media, factory
):
    final = tmp_path / "clips" / "scene_1" / "clip_0001.mp4"
    monkeypatch.setattr(renderer, "run_ffmpeg", _fake_ffmpeg_garbage_then_fail)

    create = renderer.create_image_clip if factory == "image" else renderer.create_video_clip
    assert create(source_media, 1.0, final, frame_count=30) is False

    assert not final.exists()
    assert _partials(final.parent) == []


@pytest.mark.parametrize("factory", ["image", "video"])
def test_failed_clip_encode_preserves_existing_final(
    tmp_path, monkeypatch, source_media, factory
):
    final = tmp_path / "clip_0001.mp4"
    final.write_bytes(b"PREVIOUS GOOD CLIP" * 100)
    before = final.read_bytes()
    monkeypatch.setattr(renderer, "run_ffmpeg", _fake_ffmpeg_garbage_then_fail)

    create = renderer.create_image_clip if factory == "image" else renderer.create_video_clip
    assert create(source_media, 1.0, final, frame_count=30) is False

    assert final.read_bytes() == before
    assert _partials(tmp_path) == []


@pytest.mark.parametrize("factory", ["image", "video"])
def test_successful_clip_encode_publishes_final(
    tmp_path, monkeypatch, source_media, factory
):
    final = tmp_path / "clip_0001.mp4"
    seen_targets = []

    def recording_ffmpeg(command, timeout=None):
        seen_targets.append(command[-1])
        return _fake_ffmpeg_ok(command, timeout)

    monkeypatch.setattr(renderer, "run_ffmpeg", recording_ffmpeg)

    create = renderer.create_image_clip if factory == "image" else renderer.create_video_clip
    assert create(source_media, 1.0, final, frame_count=30) is True

    assert final.read_bytes() == PAYLOAD
    # ffmpeg was pointed at a partial sibling, never at the final path.
    assert seen_targets and seen_targets[0] != str(final)
    assert ".partial-" in seen_targets[0]
    assert _partials(tmp_path) == []


# ------------------------------------------------------------------
# concatenate_clips
# ------------------------------------------------------------------

@pytest.fixture()
def master(tmp_path):
    master = tmp_path / "documentary.mp4"
    master.write_bytes(b"PREVIOUS MASTER" * 500)
    clip = tmp_path / "clip_a.mp4"
    clip.write_bytes(PAYLOAD)
    return master, clip


def _stub_concat_probe(monkeypatch, frame_count):
    monkeypatch.setattr(
        renderer,
        "probe_media",
        lambda path: {"frame_count": frame_count, "duration": 1.0},
    )


def test_concat_ffmpeg_failure_leaves_master_untouched(tmp_path, monkeypatch, master):
    master_path, clip = master
    before = master_path.read_bytes()
    monkeypatch.setattr(renderer, "run_ffmpeg", _fake_ffmpeg_garbage_then_fail)

    assert renderer.concatenate_clips([str(clip)], master_path, expected_frames=30) is False

    assert master_path.read_bytes() == before
    assert _partials(tmp_path) == []
    assert not (tmp_path / "documentary_repaired.mp4").exists()


def test_concat_frame_validation_failure_leaves_master_byte_identical(
    tmp_path, monkeypatch, master
):
    master_path, clip = master
    before = master_path.read_bytes()
    monkeypatch.setattr(renderer, "run_ffmpeg", _fake_ffmpeg_ok)
    _stub_concat_probe(monkeypatch, frame_count=999)  # expected 30 -> mismatch

    with pytest.raises(RuntimeError, match="RENDERER FRAME REPAIR FAILED"):
        renderer.concatenate_clips([str(clip)], master_path, expected_frames=30)

    assert master_path.read_bytes() == before
    assert _partials(tmp_path) == []
    assert not (tmp_path / "documentary_repaired.mp4").exists()


def test_concat_success_publishes_over_existing_master(tmp_path, monkeypatch, master):
    master_path, clip = master
    seen_targets = []

    def recording_ffmpeg(command, timeout=None):
        seen_targets.append(command[-1])
        return _fake_ffmpeg_ok(command, timeout)

    monkeypatch.setattr(renderer, "run_ffmpeg", recording_ffmpeg)
    _stub_concat_probe(monkeypatch, frame_count=30)

    assert renderer.concatenate_clips([str(clip)], master_path, expected_frames=30) is True

    assert master_path.read_bytes() == PAYLOAD
    assert seen_targets and ".partial-" in seen_targets[0]
    assert _partials(tmp_path) == []
    assert not (tmp_path / "documentary_repaired.mp4").exists()


# ------------------------------------------------------------------
# _render_timeline_impl: master is not deleted before concat
# ------------------------------------------------------------------

def test_render_impl_does_not_delete_master_when_concat_fails(tmp_path, monkeypatch):
    master_path = tmp_path / "documentary.mp4"
    master_path.write_bytes(b"PREVIOUS MASTER" * 500)
    before = master_path.read_bytes()

    audit = {
        "scene": 1,
        "items": [{"actual_frames": 30}],
        "placeholder_count": 0,
        "holdover_count": 0,
    }
    monkeypatch.setattr(
        renderer,
        "render_scene",
        lambda scene, project_dir, *a, **k: (["clip.mp4"], 30, audit),
    )
    monkeypatch.setattr(renderer, "concatenate_clips", lambda *a, **k: False)

    scenes = [{"scene_number": 1, "items": [{"sentence": "hello"}]}]
    with pytest.raises(RuntimeError, match="Could not create documentary.mp4"):
        renderer._render_timeline_impl(scenes, tmp_path)

    assert master_path.read_bytes() == before


# ------------------------------------------------------------------
# audio_mixer.add_narration
# ------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_media(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("mixer")
    video = workdir / "video.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=30",
            "-c:v", "mpeg4", str(video),
        ],
        check=True, timeout=60,
    )
    narration = workdir / "narration.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            str(narration),
        ],
        check=True, timeout=60,
    )
    return video, narration


def test_add_narration_failure_leaves_no_output(tmp_path, monkeypatch, real_media):
    video, narration = real_media
    output = tmp_path / "documentary_with_narration.mp4"

    def failing_ffmpeg(command):
        with open(command[-1], "wb") as handle:
            handle.write(GARBAGE)
        raise RuntimeError("FFmpeg failed with exit code 1")

    monkeypatch.setattr(audio_mixer, "run_ffmpeg", failing_ffmpeg)

    with pytest.raises(RuntimeError, match="FFmpeg failed"):
        audio_mixer.add_narration(video, narration, output)

    assert not output.exists()
    assert _partials(tmp_path) == []


def test_add_narration_success_publishes_output(tmp_path, monkeypatch, real_media):
    video, narration = real_media
    output = tmp_path / "documentary_with_narration.mp4"
    seen_targets = []

    def ok_ffmpeg(command):
        seen_targets.append(command[-1])
        with open(command[-1], "wb") as handle:
            handle.write(PAYLOAD)
        return None

    monkeypatch.setattr(audio_mixer, "run_ffmpeg", ok_ffmpeg)

    result = audio_mixer.add_narration(video, narration, output)

    assert result == {"status": "audio_mixed", "output_file": str(output)}
    assert output.read_bytes() == PAYLOAD
    assert seen_targets and ".partial-" in seen_targets[0]
    assert _partials(tmp_path) == []
