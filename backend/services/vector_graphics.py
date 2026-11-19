"""Vector (PIL) motion graphics — the drawing engine the ffmpeg
drawtext graphics in graphics_pipeline.py cannot provide.

Currently one template: the EVENT SCHEMATIC. A barrier/checkpoint
motif drawn frame by frame with PIL ImageDraw and encoded to MP4
via ffmpeg: a wall with a checkpoint gap, a barrier arm that lifts
progressively, and human silhouettes that cross through the gap in
the second half. It exists for one narrow job — a narration
sentence that finished retrieval with no verified visual, carries
no statistic, and sits in a scene with no verified visuals at all
(see build_motion_graphics_assets in graphics_pipeline.py).

Labels are supplied by the caller and must trace verbatim to the
sentence's own analysis record (scenario event / place / era). This
module draws what it is given and invents nothing: empty labels are
simply omitted.

Style: the shared CINEMORA graphic palette and bundled DejaVu fonts
are imported from graphics_pipeline so generated graphics keep one
visual identity across renderers.
"""

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from services.graphics_pipeline import (
    GRAPHIC_BG_STAT,
    GRAPHIC_FONT_BOLD_FILE,
    GRAPHIC_FONT_FILE,
    GRAPHIC_PANEL,
    clean_text,
)

WIDTH = 1920
HEIGHT = 1080

# Geometry of the schematic scene (fixed template layout).
_WALL_LEFT = 120
_WALL_RIGHT = 1800
_WALL_TOP = 596
_GROUND = 884
_GAP_LEFT = 836
_GAP_RIGHT = 1084
_PIVOT = (836, 712)
_ARM_LENGTH = 250


def _rgb(hex_value):
    """'0x111722' (ffmpeg style constant) -> (17, 23, 34)."""
    value = int(str(hex_value), 16)
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


_BG = _rgb(GRAPHIC_BG_STAT)
_PANEL = _rgb(GRAPHIC_PANEL)
_WALL = (26, 34, 46)
_WALL_SEAM = (44, 56, 72)
_COPING = (118, 130, 148)
_TEXT = (255, 255, 255)
_MUTED = (148, 158, 174)
_FIGURE = (198, 206, 220)
_FIGURE_FAR = (140, 148, 164)
_GRID = (15, 19, 26)


def _font(path, size):
    try:
        return ImageFont.truetype(str(path), size)
    except Exception:
        return ImageFont.load_default()


def _clamp01(value):
    return max(0.0, min(1.0, value))


def _smooth(value):
    value = _clamp01(value)
    return value * value * (3.0 - 2.0 * value)


def _draw_wall(draw, x0, x1):
    """One wall segment between x0 and x1 (the gap is left open)."""
    draw.rectangle([x0, _WALL_TOP, x1, _GROUND], fill=_WALL)
    # Coping line along the top.
    draw.rectangle([x0, _WALL_TOP - 8, x1, _WALL_TOP], fill=_COPING)
    # Block seams: horizontal courses + staggered vertical joints.
    y = _WALL_TOP + 46
    row = 0
    while y < _GROUND:
        draw.line([x0, y, x1, y], fill=_WALL_SEAM, width=2)
        offset = 65 if row % 2 else 0
        x = x0 + 130 - offset
        while x < x1:
            draw.line([x, y - 46, x, y], fill=_WALL_SEAM, width=2)
            x += 130
        y += 46
        row += 1


def _draw_figure(draw, x, dy, colour):
    """A simple standing/walking human silhouette at ground level."""
    head_r = 13
    head_cy = _GROUND - 83 + dy
    draw.ellipse(
        [x - head_r, head_cy - head_r, x + head_r, head_cy + head_r],
        fill=colour,
    )
    draw.rounded_rectangle(
        [x - 12, _GROUND - 72 + dy, x + 12, _GROUND - 4 + dy],
        radius=10,
        fill=colour,
    )


def _frame(t, duration, fonts, event_label, place_label, era_label):
    img = Image.new("RGB", (WIDTH, HEIGHT), _BG)
    draw = ImageDraw.Draw(img)

    # Subtle grid, matching the house style of the ffmpeg graphics.
    for gx in range(0, WIDTH, 120):
        draw.line([gx, 0, gx, HEIGHT], fill=_GRID, width=1)
    for gy in range(0, HEIGHT, 120):
        draw.line([0, gy, WIDTH, gy], fill=_GRID, width=1)

    # Header labels — verbatim caller data, empty lines omitted.
    if event_label:
        draw.text(
            (120, 78), event_label.upper(), font=fonts["event"], fill=_TEXT
        )
    sub = " · ".join(p for p in (place_label, era_label) if p)
    if sub:
        draw.text((120, 154), sub, font=fonts["sub"], fill=_MUTED)

    # Wall with the checkpoint gap.
    _draw_wall(draw, _WALL_LEFT, _GAP_LEFT)
    _draw_wall(draw, _GAP_RIGHT, _WALL_RIGHT)
    # Ground line.
    draw.line([120, _GROUND, 1800, _GROUND], fill=(90, 102, 120), width=3)

    # Checkpoint booth on the far side of the gap.
    draw.rectangle([1116, 668, 1248, _GROUND], fill=(32, 42, 56))
    draw.rectangle([1104, 648, 1260, 668], fill=_COPING)
    draw.rectangle([1136, 696, 1228, 756], fill=(168, 182, 200))

    # Two figures waiting on the far side.
    _draw_figure(draw, 1662, 0, _FIGURE_FAR)
    _draw_figure(draw, 1736, 0, _FIGURE_FAR)

    # Barrier arm: closed at first, lifts between 1.0s and 3.4s.
    import math

    p_arm = _smooth((t - 1.0) / 2.4)
    angle = -1.35 * p_arm  # radians; negative = raised
    # Pivot post.
    draw.rectangle([824, _PIVOT[1], 848, _GROUND], fill=_COPING)
    segments = 5
    for i in range(segments):
        f0 = i / segments
        f1 = (i + 1) / segments
        x0 = _PIVOT[0] + math.cos(angle) * _ARM_LENGTH * f0
        y0 = _PIVOT[1] + math.sin(angle) * _ARM_LENGTH * f0
        x1 = _PIVOT[0] + math.cos(angle) * _ARM_LENGTH * f1
        y1 = _PIVOT[1] + math.sin(angle) * _ARM_LENGTH * f1
        colour = (238, 240, 244) if i % 2 == 0 else (110, 118, 132)
        draw.line([x0, y0, x1, y1], fill=colour, width=16)
    draw.ellipse(
        [_PIVOT[0] - 13, _PIVOT[1] - 13, _PIVOT[0] + 13, _PIVOT[1] + 13],
        fill=(230, 234, 240),
    )

    # Figures cross through the gap in the second half.
    for i in range(5):
        start_x = 296 + i * 74
        end_x = 1312 + i * 68
        delay = 2.3 + i * 0.38
        p = _smooth((t - delay) / 2.9)
        if p <= 0.0:
            x = start_x
        else:
            x = start_x + (end_x - start_x) * p
        bob = 0.0
        if 0.0 < p < 1.0:
            bob = -abs(math.sin(t * 9.0 + i * 1.3)) * 3.0
        _draw_figure(draw, x, bob, _FIGURE)

    # Progress track, as in the other CINEMORA graphics.
    draw.rectangle([120, 984, 1800, 987], fill=(52, 62, 76))
    draw.rectangle(
        [120, 984, 120 + 1680 * _clamp01(t / duration), 987],
        fill=(235, 238, 242),
    )
    draw.text(
        (120, 1014),
        "CINEMORA / GENERATED SCHEMATIC",
        font=fonts["footer"],
        fill=_MUTED,
    )
    return img


def render_event_schematic(
    output_file,
    event_label="",
    place_label="",
    era_label="",
    duration=6.0,
    fps=30,
):
    """Render the event schematic to an MP4 at output_file.

    Raises on failure (callers treat one failing graphic as
    skippable, per AUD-21). Returns the output path as a string.
    """
    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    fonts = {
        "event": _font(GRAPHIC_FONT_BOLD_FILE, 54),
        "sub": _font(GRAPHIC_FONT_FILE, 30),
        "footer": _font(GRAPHIC_FONT_FILE, 22),
    }
    event_label = clean_text(event_label)
    place_label = clean_text(place_label)
    era_label = clean_text(era_label)

    total_frames = max(1, int(duration * fps))
    command = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{WIDTH}x{HEIGHT}", "-r", str(fps),
        "-i", "-",
        "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(output_file),
    ]
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        for i in range(total_frames):
            t = i / fps
            frame = _frame(t, duration, fonts, event_label, place_label,
                           era_label)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    stderr = proc.stderr.read() if proc.stderr else b""
    return_code = proc.wait()
    if return_code != 0:
        raise RuntimeError(
            "event schematic ffmpeg encode failed: "
            + stderr.decode("utf-8", "replace")[:300]
        )
    return str(output_file)
