"""Tracked annotation — the advanced bridge item (Motion
Graphics Programme Phase 6–7, audit G2), Level 2.

A REAL detect + track implementation, not a label on a still:
given a real video clip and a RECORDED anchor region (the
region on the clip's first frame that the annotation belongs
to), the anchor patch is template-matched frame by frame
across the clip (OpenCV matchTemplate, windowed search with a
full-frame fallback), and the annotation — a marker box at the
tracked region plus its recorded label — is drawn at the
TRACKED position on every output frame.

Honesty rules, enforced in code:

* The anchor patch must be trackable: a near-featureless patch
  (a plain background region) refuses with
  ``anchor_not_trackable`` — there is nothing to track.
* Every frame's match must clear the score threshold. When it
  does not, the track is LOST at that frame: the loss is
  recorded in the metadata (``track_lost_at``), no annotation
  is drawn on any later frame (the annotation never pretends
  to follow what it lost), and a track lost almost immediately
  (before max(3, 25% of the clip's frames)) refuses the whole
  graphic with ``track_lost``.
* The marker box keeps the anchor's recorded size — template
  matching estimates position, not scale, and the render does
  not claim otherwise.

The recorded anchor + label are the contract; nothing about
the tracked subject is inferred or invented. Output is a
standard 1920x1080 / 30 fps engine graphic (the source frames
fit-covered), so it registers through the same validation as
every other template.
"""

import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from services.motion_engine import core
from services.motion_engine.core import HEIGHT, WIDTH, fit_cover, load_font
from services.motion_engine.label_fit import LABEL_FIT_REASON, fit_lines
from services.motion_engine.styles import resolve_style
from services.motion_engine.templates import _clean, _meta

#: Minimum normalised match score for a frame's track to be
#: believed. Below it the track is declared lost — the tracker
#: never coasts on a guess.
TRACK_SCORE_THRESHOLD = 0.5
#: Tracking runs on frames downscaled to at most this width —
#: the match geometry is scale-invariant for this purpose and
#: the recorded positions are relative.
ANALYSIS_WIDTH = 480
#: Upper bound on source frames analysed (longer clips are
#: sampled by stride, and the stride is recorded).
MAX_ANALYSIS_FRAMES = 300


def track_positions(gray_frames, anchor_px):
    """Track one anchor patch across grayscale frames.

    `gray_frames`: list of same-size uint8 arrays. `anchor_px`:
    (x, y, w, h) of the patch on frame 0. Returns (centres,
    lost_at): centres[i] is the tracked patch centre as
    RELATIVE (x, y) floats for frame i, or None for frames at
    and after a loss; lost_at is the first lost frame index or
    None when the track held for the whole clip."""
    if not gray_frames:
        return [], None
    height, width = gray_frames[0].shape[:2]
    x, y, w, h = anchor_px
    x = int(min(max(0, x), max(0, width - 1)))
    y = int(min(max(0, y), max(0, height - 1)))
    w = int(min(max(1, w), width - x))
    h = int(min(max(1, h), height - y))
    template = gray_frames[0][y:y + h, x:x + w]
    centres = [( (x + w / 2) / width, (y + h / 2) / height )]
    lost_at = None
    last = (x, y)
    for i in range(1, len(gray_frames)):
        frame = gray_frames[i]
        # Windowed search around the last position first (the
        # subject moves continuously); a full-frame search is
        # the fallback when the window finds nothing believable.
        wx0 = max(0, last[0] - w)
        wy0 = max(0, last[1] - h)
        wx1 = min(width, last[0] + 2 * w)
        wy1 = min(height, last[1] + 2 * h)
        window = frame[wy0:wy1, wx0:wx1]
        best = None
        if window.shape[0] >= h and window.shape[1] >= w:
            result = cv2.matchTemplate(
                window, template, cv2.TM_CCOEFF_NORMED)
            _min, score, _minloc, loc = cv2.minMaxLoc(result)
            if score >= TRACK_SCORE_THRESHOLD:
                best = (wx0 + loc[0], wy0 + loc[1], score)
        if best is None:
            result = cv2.matchTemplate(
                frame, template, cv2.TM_CCOEFF_NORMED)
            _min, score, _minloc, loc = cv2.minMaxLoc(result)
            if score >= TRACK_SCORE_THRESHOLD:
                best = (loc[0], loc[1], score)
        if best is None:
            lost_at = i
            centres.extend([None] * (len(gray_frames) - i))
            break
        last = (best[0], best[1])
        centres.append((
            (best[0] + w / 2) / width,
            (best[1] + h / 2) / height,
        ))
    return centres, lost_at


def _analysis_frames(clip_path):
    """Read the clip's frames as small grayscale arrays for
    tracking. Returns (frames, src_fps, src_size, stride) or
    None when the clip cannot be read."""
    capture = cv2.VideoCapture(str(clip_path))
    if not capture.isOpened():
        return None
    src_fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    stride = 1
    if total > MAX_ANALYSIS_FRAMES:
        stride = int(np.ceil(total / MAX_ANALYSIS_FRAMES))
    frames = []
    src_size = None
    index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if src_size is None:
            src_size = (frame.shape[1], frame.shape[0])
        if index % stride == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            scale = min(1.0, ANALYSIS_WIDTH / gray.shape[1])
            if scale < 1.0:
                gray = cv2.resize(
                    gray,
                    (int(gray.shape[1] * scale),
                     int(gray.shape[0] * scale)),
                    interpolation=cv2.INTER_AREA,
                )
            frames.append(gray)
        index += 1
    capture.release()
    if not frames or src_size is None:
        return None
    return frames, src_fps, src_size, stride


def render_tracked_annotation(data, style, out_path, duration=5.0,
                              sentence_index=None, scene_number=None):
    """Engine-template entry point: render the tracked
    annotation for one recorded contract.

    data: {"clip": path to a real video clip,
           "anchor": {"x", "y", "w", "h"} — the anchor region on
           the clip's first frame, RELATIVE coordinates 0..1,
           "label": the recorded annotation text}"""
    style_name, tokens = resolve_style(style)
    data = data or {}
    clip = data.get("clip")
    if not clip or not Path(clip).exists():
        return None, {"reason": "no_clip"}
    label = _clean(data.get("label"))
    if not label:
        return None, {"reason": "no_label"}
    anchor = data.get("anchor")
    if not isinstance(anchor, dict):
        return None, {"reason": "no_anchor"}
    try:
        rel = tuple(float(anchor[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None, {"reason": "no_anchor"}
    if rel[2] <= 0 or rel[3] <= 0:
        return None, {"reason": "no_anchor"}

    fonts = {"label": load_font(tokens["font_bold"], 30)}
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    if fit_lines(probe, label, fonts["label"], 560, max_lines=2) is None:
        return None, {
            "reason": LABEL_FIT_REASON,
            "detail": f"tracked annotation label {label!r} "
                      f"does not fit",
        }

    analysis = _analysis_frames(clip)
    if analysis is None:
        return None, {"reason": "clip_unreadable"}
    frames, src_fps, src_size, stride = analysis
    a_h, a_w = frames[0].shape[:2]
    anchor_px = (
        rel[0] * a_w, rel[1] * a_h, rel[2] * a_w, rel[3] * a_h,
    )
    patch = frames[0][
        int(anchor_px[1]):int(anchor_px[1] + anchor_px[3]),
        int(anchor_px[0]):int(anchor_px[0] + anchor_px[2]),
    ]
    if patch.size == 0 or float(patch.std()) < 3.0:
        # A featureless patch cannot be tracked — matching it
        # would "succeed" anywhere and follow nothing.
        return None, {"reason": "anchor_not_trackable"}
    centres, lost_at = track_positions(frames, anchor_px)
    tracked_frames = lost_at if lost_at is not None else len(frames)
    if lost_at is not None and lost_at < max(3, int(0.25 * len(frames))):
        return None, {
            "reason": "track_lost",
            "detail": f"track lost at analysis frame {lost_at} "
                      f"of {len(frames)} — too early to annotate",
        }

    duration = float(duration or 5.0)
    fps = core.FPS
    total_out = max(1, int(duration * fps))
    src_duration = len(frames) * stride / max(src_fps, 1.0)

    def _source_centre(out_index):
        """The tracked centre for one output frame: map the
        output timestamp into analysis-frame time; frames past
        the clip's end hold the last tracked position; frames
        at/after a loss get NO annotation (None)."""
        t = out_index / fps
        if src_duration > 0:
            idx = int(t / src_duration * len(frames))
        else:
            idx = 0
        idx = min(idx, len(frames) - 1)
        return centres[idx]

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{WIDTH}x{HEIGHT}", "-r", str(fps),
        "-i", "-",
        "-an", "-c:v", "libx264", "-preset", "ultrafast",
        "-crf", "20", "-pix_fmt", "yuv420p", str(out_path),
    ]
    proc = subprocess.Popen(
        command, stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    capture = cv2.VideoCapture(str(clip))
    box_w = rel[2] * WIDTH
    box_h = rel[3] * HEIGHT
    label_w = probe.textlength(label, font=fonts["label"])
    try:
        current = None
        current_idx = -1
        for out_index in range(total_out):
            t = out_index / fps
            want = int(t * src_fps)
            while current_idx < want:
                ok, frame = capture.read()
                if not ok:
                    break
                current = frame
                current_idx += 1
            if current is None:
                break
            image = Image.fromarray(
                cv2.cvtColor(current, cv2.COLOR_BGR2RGB))
            image = fit_cover(image, (WIDTH, HEIGHT)).convert("RGBA")
            centre = _source_centre(out_index)
            if centre is not None:
                d = ImageDraw.Draw(image, "RGBA")
                cx, cy = centre[0] * WIDTH, centre[1] * HEIGHT
                x0, y0 = cx - box_w / 2, cy - box_h / 2
                x1, y1 = cx + box_w / 2, cy + box_h / 2
                d.rectangle(
                    [x0, y0, x1, y1],
                    outline=tokens["accent"] + (255,), width=4)
                ly = max(8, y0 - 52)
                d.rectangle(
                    [cx - label_w / 2 - 12, ly,
                     cx + label_w / 2 + 12, ly + 42],
                    fill=(0, 0, 0, 160))
                d.text((cx - label_w / 2, ly + 5), label,
                       font=fonts["label"],
                       fill=(255, 255, 255, 255))
            proc.stdin.write(image.convert("RGB").tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    finally:
        capture.release()
    stderr = proc.stderr.read() if proc.stderr else b""
    if proc.wait() != 0:
        raise RuntimeError(
            "tracked annotation encode failed: "
            + stderr.decode("utf-8", "replace")[:300])
    info = core.probe_video(out_path)
    if info["width"] != WIDTH or info["height"] != HEIGHT \
            or info["duration"] <= 0:
        raise RuntimeError(
            f"tracked annotation output failed verification: {info}")
    return str(out_path), _meta(
        "tracked_annotation", style_name, duration,
        sentence_index, scene_number,
        {
            "label": label,
            "source_frames": len(frames),
            "tracked_frames": tracked_frames,
            "track_lost_at": lost_at,
            "stride": stride,
            "positions": [
                [round(c[0], 3), round(c[1], 3)] if c else None
                for c in centres
            ],
        })
