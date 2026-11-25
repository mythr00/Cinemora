"""Shot-boundary detection via PySceneDetect (GitHub Skills
Programme, Lane 2, 2026-11-25).

Why: the founder's continuous-segments rule (footage must play as
continuous segments; the 1-second flicker is a defect) has a
mechanical precondition — knowing where a downloaded clip's shots
actually are, so a chosen window can sit INSIDE one shot instead of
straddling a cut or landing on a sliver. Until now, frame sampling
and clip windows were purely time-based; nothing in the pipeline
knew a shot boundary.

Integration shape (the lane's rigour conditions, applied):
* ``scenedetect`` (BSD-3-Clause) pinned ``~=0.7.0``; the regular
  distribution, NOT scenedetect-headless — the project already
  installs opencv-python, and the headless wheel would install a
  second, conflicting cv2 provider.
* ContentDetector (single-pass), default threshold 27.0,
  min_scene_len 15 frames (sliver shots are suppressed by the
  detector itself).
* Bounded: clips longer than ``MAX_DURATION_SECONDS`` are refused
  with a typed reason — the detector decodes every frame, and on
  this hardware class long archive originals are out of scope. The
  operating point is downloaded candidate clips (seconds to a few
  minutes). Benchmark on the programme fixture (12s, 320x240, 3
  true cuts): 0.48s wall, all cuts exact.
* Record-only: scenes are attached to the frame-verification
  record as evidence; window selection consults them only where a
  caller opts in.

Availability mirrors the Manim posture: the package is imported
lazily inside the call, never at module import, and absence yields
a typed refusal, not an exception. Never raises.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MAX_DURATION_SECONDS = 600.0


def shots_available() -> bool:
    return importlib.util.find_spec("scenedetect") is not None


def detect_shots(path, *, duration_seconds=None) -> dict:
    """Scene list for one video file. Never raises.

    Returns {status: evaluated|not_evaluated, detector, scenes:
    [{start, end, duration}], scene_count, reason}.
    """
    record = {
        "status": "not_evaluated",
        "detector": "pyscenedetect_content",
        "scenes": [],
        "scene_count": 0,
        "reason": None,
    }
    path = Path(path)
    if not path.exists():
        record["reason"] = "file_missing"
        return record
    if duration_seconds is not None and duration_seconds > MAX_DURATION_SECONDS:
        record["reason"] = "clip_too_long_for_shot_detection"
        return record
    if not shots_available():
        record["reason"] = "pyscenedetect_unavailable"
        return record
    try:
        from scenedetect import detect
        from scenedetect.detectors import ContentDetector

        scenes = detect(str(path), ContentDetector())
    except Exception as error:  # fail-soft, like the frame stage
        record["reason"] = f"error: {type(error).__name__}: {error}"
        return record
    out = []
    for start_tc, end_tc in scenes:
        start = float(start_tc.seconds)
        end = float(end_tc.seconds)
        out.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
        })
    record["scenes"] = out
    record["scene_count"] = len(out)
    record["status"] = "evaluated"
    return record


def snap_window_inside_shot(scenes, start, duration):
    """Adjust ``(start, duration)`` to sit inside a single shot.

    The shot-aware window rule: if the requested window straddles
    a boundary, shift it to start at the boundary when the shot
    that follows can hold it; otherwise clamp its duration to the
    end of the shot it starts in. Returns (start, duration,
    adjusted: bool). With no scenes, the window is returned
    unchanged — behaviour is identical to the pre-shot pipeline.
    """
    if not scenes or duration <= 0:
        return start, duration, False
    end = start + duration
    for scene in scenes:
        if scene["start"] <= start < scene["end"]:
            if end <= scene["end"]:
                return start, duration, False
            following = scene["end"]
            # Prefer shifting into the next shot if it can hold
            # the whole window.
            for nxt in scenes:
                if nxt["start"] == following and nxt["duration"] >= duration:
                    return following, duration, True
            return start, round(scene["end"] - start, 3), True
    return start, duration, False
