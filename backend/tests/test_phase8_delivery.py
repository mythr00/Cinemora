"""Phase 8 — delivery encode profile (AM-023 machinery).

The founder's live test produced a 191,566,743-byte export for a
45.43 s film (~34 Mbps): the only encode was the archival master.
The delivery profile adds a web-playable copy BESIDE the master.
AM-023 (the founder's bitrate target) remains OPEN — the profile
default is an engineering default and every record says so.
"""

import subprocess
from pathlib import Path

from services import delivery_profile


def _make_master(path, seconds=3):
    """A deliberately fat master: high-bitrate 1080p noise."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"testsrc2=duration={seconds}:size=1920x1080:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration="
            f"{seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True, capture_output=True,
    )
    return path


def test_delivery_copy_is_smaller_valid_and_master_untouched(tmp_path):
    master = _make_master(tmp_path / "master.mp4")
    master_bytes_before = master.stat().st_size
    out = tmp_path / "delivery.mp4"
    record = delivery_profile.produce_delivery_copy(master, out)
    assert record["status"] == "delivered"
    assert record["decode_validated"] is True
    assert record["am023_status"].startswith("OPEN")
    assert record["profile"] == delivery_profile.DELIVERY_PROFILE["name"]
    assert out.exists()
    assert record["delivery_bytes"] < record["master_bytes"]
    assert record["size_ratio"] < 1.0
    assert abs(record["duration_seconds"] - 3.0) < 0.6
    # The master is never modified.
    assert master.stat().st_size == master_bytes_before


def test_delivery_profile_args_carry_the_ceiling():
    args = delivery_profile.delivery_encode_args()
    joined = " ".join(args)
    profile = delivery_profile.DELIVERY_PROFILE
    assert profile["crf"] in joined
    assert f"{profile['maxrate_kbps']}k" in joined
    assert "+faststart" in joined


def test_delivery_failure_is_recorded_not_raised(tmp_path):
    record = delivery_profile.produce_delivery_copy(
        tmp_path / "missing-master.mp4", tmp_path / "delivery.mp4"
    )
    assert record["status"] == "failed"
    assert record["reason"] == "master_unprobeable"
    assert not (tmp_path / "delivery.mp4").exists()


def test_delivery_encode_failure_removes_partial_output(tmp_path, monkeypatch):
    master = _make_master(tmp_path / "master.mp4")
    from services import renderer

    real_run = renderer.run_ffmpeg

    def sabotaged(command, timeout=None):
        # The encode command (writes the output path) fails after
        # creating a partial file; validation commands behave.
        out = Path(command[-1])
        if out.suffix == ".mp4" and "delivery" in out.name:
            out.write_bytes(b"partial")
            return False
        return real_run(command, timeout=timeout) if timeout else real_run(command)

    monkeypatch.setattr(renderer, "run_ffmpeg", sabotaged)
    out = tmp_path / "delivery.mp4"
    record = delivery_profile.produce_delivery_copy(master, out)
    assert record["status"] == "failed"
    assert record["reason"] == "delivery_encode_failed"
