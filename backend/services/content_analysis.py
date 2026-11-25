"""Whole-file content analysis via ffmpeg's built-in detectors
(GitHub Skills Programme, Lane 2, 2026-11-25).

The lane's cheapest finding: ffmpeg 8.1.2 already ships
``freezedetect`` and ``blackdetect`` — Cinemora simply never ran
them. They attack two real defect families:

* a "footage" asset that is actually a static image / frozen frame
  masquerading as motion (freeze intervals), and
* black leader/filler inside a clip — dead screen time in a
  documentary (black intervals).

RECORD-ONLY: results are attached to the frame-verification record
as evidence. Nothing here changes a verdict; promotion to rejection
needs calibration on real production assets (standing rule).

Never raises: any failure returns a typed ``not_evaluated`` record.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_FREEZE_START = re.compile(r"lavfi\.freezedetect\.freeze_start:\s*([\d.]+)")
_FREEZE_END = re.compile(r"lavfi\.freezedetect\.freeze_end:\s*([\d.]+)")
_BLACK = re.compile(
    r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)"
)

#: Detector parameters (ffmpeg defaults, stated so the record is
#: interpretable): freeze = mean abs frame diff under a -60 dB /
#: 0.003 noise floor sustained >= 2s; black = >= 1.5s at >= 98%
#: pixels under the 0.10 black threshold.
_FILTERS = "freezedetect=n=0.003:d=2,blackdetect=d=1.5:pix_th=0.10"


def _intervals(starts, ends):
    out = []
    for start, end in zip(starts, ends):
        out.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
        })
    return out


def analyse_video_content(path, *, timeout=600) -> dict:
    """Freeze/black intervals for one video file. Never raises."""
    record = {
        "status": "not_evaluated",
        "detectors": ["freezedetect", "blackdetect"],
        "freeze_intervals": [],
        "black_intervals": [],
        "reason": None,
    }
    path = Path(path)
    if not path.exists():
        record["reason"] = "file_missing"
        return record
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path),
             "-vf", _FILTERS, "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        record["reason"] = "analysis_timeout"
        return record
    except Exception as error:  # fail-soft, like the frame stage
        record["reason"] = f"error: {type(error).__name__}: {error}"
        return record
    stderr = proc.stderr or ""
    freeze_starts = [float(v) for v in _FREEZE_START.findall(stderr)]
    freeze_ends = [float(v) for v in _FREEZE_END.findall(stderr)]
    record["freeze_intervals"] = _intervals(freeze_starts, freeze_ends)
    record["black_intervals"] = [
        {"start": float(s), "end": float(e), "duration": float(d)}
        for s, e, d in _BLACK.findall(stderr)
    ]
    record["status"] = "evaluated"
    return record
