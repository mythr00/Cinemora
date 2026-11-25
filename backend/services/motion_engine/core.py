"""Motion Engine core: a small PIL compositor.

- Canvas 1920x1080; content is drawn on separate RGBA layers.
- Keyframe tracks with easing (ease_in_out / ease_out / ease_in).
- A camera transform (scale + translate) applied at composite time,
  with a per-layer parallax factor.
- Progressive polyline drawing and reveal wipes.
- render_video / render_template: pipe RGB frames to ffmpeg
  (30 fps, libx264, yuv420p) and ffprobe-verify the result,
  following the pattern established by services/vector_graphics.py.

The core draws shapes and caller-supplied images/text only. It has
no content of its own and invents nothing.
"""

import math
import random
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1920
HEIGHT = 1080
FPS = 30

#: Background treatments Canvas.composite can actually paint,
#: named by a theme's `background_pattern` token (A-11, Phase 7):
#: "grid" (founder Grid), "vignette" (founder Light's soft dark
#: edge), "gradient" (founder Cinematic's vertical gradient).
#: Every background_pattern value offered by any theme MUST be in
#: this set — a treatment with no renderer may not be offered.
BACKGROUND_PATTERNS = ("grid", "vignette", "gradient")


# ---------------------------------------------------------------------------
# Easing / interpolation
# ---------------------------------------------------------------------------

def clamp01(value):
    return max(0.0, min(1.0, float(value)))


def ease_in_out(value):
    value = clamp01(value)
    return value * value * (3.0 - 2.0 * value)


def ease_out(value):
    value = clamp01(value)
    return 1.0 - (1.0 - value) ** 3


def ease_in(value):
    value = clamp01(value)
    return value ** 3


def lerp(a, b, fraction):
    return a + (b - a) * fraction


class KeyframeTrack:
    """(time, value) keyframes interpolated with an easing function."""

    def __init__(self, keyframes, easing=ease_in_out):
        if not keyframes:
            raise ValueError("KeyframeTrack needs at least one keyframe")
        self.keyframes = sorted((float(t), float(v)) for t, v in keyframes)
        self.easing = easing

    def value_at(self, t):
        keys = self.keyframes
        if t <= keys[0][0]:
            return keys[0][1]
        if t >= keys[-1][0]:
            return keys[-1][1]
        for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
            if t0 <= t <= t1:
                fraction = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
                return lerp(v0, v1, self.easing(fraction))
        return keys[-1][1]


# ---------------------------------------------------------------------------
# Layers + canvas + camera
# ---------------------------------------------------------------------------

class Layer:
    """One RGBA layer with a parallax factor (1.0 = moves with camera)."""

    def __init__(self, name, image=None, parallax=1.0, opacity=1.0):
        self.name = name
        self.image = image or Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        self.parallax = float(parallax)
        self.opacity = float(opacity)

    def draw(self):
        return ImageDraw.Draw(self.image)


class Canvas:
    """A 1920x1080 stack of layers composited under a camera.

    camera = {"scale": float, "x": float, "y": float} — scale is
    applied about the frame centre; x/y are pixel translations.
    A layer's effective translation is camera x/y * its parallax.
    """

    def __init__(self, bg=(0, 0, 0), pattern=None, pattern_color=None):
        self.bg = bg
        # Phase 9: an optional background pattern named by the
        # active theme's tokens (today: "grid" — the founder Grid
        # theme's fine lines, painted onto the base frame in
        # composite() before any layer is drawn). None (every
        # legacy style and non-Grid theme) paints the plain field
        # exactly as before.
        self.pattern = pattern
        self.pattern_color = pattern_color
        self.layers = []

    def add(self, layer):
        self.layers.append(layer)
        return layer

    def new_layer(self, name, parallax=1.0, opacity=1.0):
        return self.add(Layer(name, parallax=parallax, opacity=opacity))

    def composite(self, camera=None):
        camera = camera or {}
        scale = float(camera.get("scale", 1.0))
        cam_x = float(camera.get("x", 0.0))
        cam_y = float(camera.get("y", 0.0))
        base = Image.new("RGBA", (WIDTH, HEIGHT), tuple(self.bg) + (255,))
        if self.pattern == "gradient":
            # A-11: vertical gradient from the bg field (top) to
            # pattern_color (bottom), painted on the base so every
            # layer composes over the same graded field.
            top = tuple(self.bg)
            bottom = tuple(self.pattern_color or self.bg)
            strip = Image.new("RGB", (1, HEIGHT))
            for y in range(HEIGHT):
                f = y / max(1, HEIGHT - 1)
                strip.putpixel((0, y), tuple(
                    int(round(top[i] + (bottom[i] - top[i]) * f))
                    for i in range(3)
                ))
            base = strip.resize((WIDTH, HEIGHT)).convert("RGBA")
        elif self.pattern == "vignette":
            # A-11: soft dark vignette edge — the centre keeps the
            # field colour; toward the corners the frame darkens
            # toward pattern_color (blended, never a hard edge).
            from PIL import ImageFilter

            edge = tuple(self.pattern_color or (0, 0, 0))
            mask = Image.new("L", (WIDTH, HEIGHT), 255)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.ellipse(
                [int(WIDTH * 0.08), int(HEIGHT * 0.08),
                 int(WIDTH * 0.92), int(HEIGHT * 0.92)], fill=0)
            mask = mask.filter(ImageFilter.GaussianBlur(180))
            solid = Image.new(
                "RGBA", (WIDTH, HEIGHT), edge + (255,))
            darkened = Image.composite(solid, base, mask)
            base = Image.blend(base, darkened, 0.62)
        if self.pattern == "grid":
            # The founder Grid theme (§13.3): fine lines over the
            # field, painted on the base so every layer composes
            # over the same grid. Spacing 60px at 1920x1080.
            line = tuple(self.pattern_color or (64, 64, 64)) + (255,)
            draw = ImageDraw.Draw(base)
            for gx in range(0, WIDTH, 60):
                draw.line([gx, 0, gx, HEIGHT], fill=line, width=1)
            for gy in range(0, HEIGHT, 60):
                draw.line([0, gy, WIDTH, gy], fill=line, width=1)
        for layer in self.layers:
            img = layer.image
            if layer.opacity < 1.0:
                img = img.copy()
                alpha = img.getchannel("A").point(
                    lambda a: int(a * layer.opacity)
                )
                img.putalpha(alpha)
            if scale != 1.0:
                new_size = (
                    max(1, int(WIDTH * scale)),
                    max(1, int(HEIGHT * scale)),
                )
                img = img.resize(new_size, Image.LANCZOS)
                offset_x = int((WIDTH - new_size[0]) / 2 + cam_x * layer.parallax)
                offset_y = int((HEIGHT - new_size[1]) / 2 + cam_y * layer.parallax)
            else:
                offset_x = int(cam_x * layer.parallax)
                offset_y = int(cam_y * layer.parallax)
            base.alpha_composite(img, (offset_x, offset_y))
        return base.convert("RGB")


# ---------------------------------------------------------------------------
# Drawing primitives
# ---------------------------------------------------------------------------

def progressive_polyline(draw, points, fraction, fill, width=4):
    """Draw the first `fraction` of a polyline (by segment count and
    a partial final segment). Returns the revealed endpoint."""
    if not points or fraction <= 0:
        return None
    fraction = clamp01(fraction)
    if len(points) == 1:
        draw.ellipse(
            [points[0][0] - width, points[0][1] - width,
             points[0][0] + width, points[0][1] + width], fill=fill)
        return points[0]
    # Total length for an even reveal speed.
    lengths = []
    total = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        lengths.append(seg)
        total += seg
    if total <= 0:
        return points[-1]
    target = total * fraction
    walked = 0.0
    revealed = [points[0]]
    endpoint = points[-1]
    for idx, seg in enumerate(lengths):
        x0, y0 = points[idx]
        x1, y1 = points[idx + 1]
        if walked + seg <= target:
            revealed.append((x1, y1))
            walked += seg
            endpoint = (x1, y1)
        else:
            f = 0.0 if seg == 0 else (target - walked) / seg
            endpoint = (x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)
            revealed.append(endpoint)
            break
    if len(revealed) >= 2:
        draw.line(revealed, fill=fill, width=width, joint="curve")
    return endpoint


def dashed_polyline(draw, points, fraction, fill, width=4, dash=30.0, gap=20.0):
    """Draw a dashed polyline, revealed progressively by length
    (the first `fraction` of the total path length). The dash
    pattern is anchored at the path start so the reveal grows the
    pattern instead of sliding it. Returns the revealed endpoint."""
    if not points or fraction <= 0:
        return None
    fraction = clamp01(fraction)
    if len(points) == 1:
        draw.ellipse(
            [points[0][0] - width, points[0][1] - width,
             points[0][0] + width, points[0][1] + width], fill=fill)
        return points[0]
    lengths = []
    total = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        lengths.append(seg)
        total += seg
    if total <= 0:
        return points[-1]
    target = total * fraction
    walked = 0.0
    endpoint = points[-1]
    for idx, seg in enumerate(lengths):
        if walked >= target:
            break
        x0, y0 = points[idx]
        x1, y1 = points[idx + 1]
        seg_end = min(walked + seg, target)
        # Walk this segment in dash/gap steps (pattern anchored at
        # the global walked distance).
        cursor = walked
        while cursor < seg_end:
            phase = cursor % (dash + gap)
            if phase < dash:
                step_end = min(cursor + (dash - phase), seg_end)
            else:
                step_end = min(cursor + (dash + gap - phase), seg_end)
                cursor = step_end
                continue
            f0 = 0.0 if seg == 0 else (cursor - walked) / seg
            f1 = 0.0 if seg == 0 else (step_end - walked) / seg
            draw.line(
                [(x0 + (x1 - x0) * f0, y0 + (y1 - y0) * f0),
                 (x0 + (x1 - x0) * f1, y0 + (y1 - y0) * f1)],
                fill=fill, width=width,
            )
            cursor = step_end
        if seg > 0:
            f = (seg_end - walked) / seg
            endpoint = (x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)
        walked += seg
    return endpoint


def reveal_wipe(base_image, overlay_image, fraction, direction="left"):
    """Return overlay revealed over base by a straight wipe."""
    fraction = clamp01(fraction)
    out = base_image.copy()
    if fraction <= 0:
        return out
    w, h = base_image.size
    if direction == "left":
        box = (0, 0, int(w * fraction), h)
    elif direction == "right":
        box = (int(w * (1 - fraction)), 0, w, h)
    elif direction == "up":
        box = (0, int(h * (1 - fraction)), w, h)
    else:  # down
        box = (0, 0, w, int(h * fraction))
    if box[2] > box[0] and box[3] > box[1]:
        region = overlay_image.crop(box)
        out.paste(region, (box[0], box[1]), region)
    return out


def load_font(path, size):
    try:
        return ImageFont.truetype(str(path), size)
    except Exception:
        return ImageFont.load_default()


def wrap_text(draw, text, font, max_width):
    words = str(text or "").split()
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def fit_cover(image, size):
    """Resize + centre-crop an image to exactly `size` (cover)."""
    target_w, target_h = size
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    resized = image.resize(
        (max(1, int(src_w * scale)), max(1, int(src_h * scale))),
        Image.LANCZOS,
    )
    x = (resized.size[0] - target_w) // 2
    y = (resized.size[1] - target_h) // 2
    return resized.crop((x, y, x + target_w, y + target_h))


def fit_contain(image, size):
    target_w, target_h = size
    src_w, src_h = image.size
    scale = min(target_w / src_w, target_h / src_h)
    return image.resize(
        (max(1, int(src_w * scale)), max(1, int(src_h * scale))),
        Image.LANCZOS,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def probe_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"ffprobe could not read '{path}': {result.stderr.strip()[:200]}")
    return float(result.stdout.strip())


def probe_video(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for '{path}': {result.stderr.strip()[:200]}")
    import json
    info = json.loads(result.stdout or "{}")
    streams = info.get("streams") or [{}]
    return {
        "width": streams[0].get("width"),
        "height": streams[0].get("height"),
        "duration": float((info.get("format") or {}).get("duration") or 0),
    }


def render_video(draw_frame_fn, out_path, duration=5.0, fps=FPS):
    """draw_frame_fn(t, progress) -> PIL Image (RGB/RGBA, 1920x1080).
    Pipes frames to ffmpeg and ffprobe-verifies the MP4."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration = float(duration)
    total_frames = max(1, int(duration * fps))
    command = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{WIDTH}x{HEIGHT}", "-r", str(fps),
        "-i", "-",
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
        "-pix_fmt", "yuv420p", str(out_path),
    ]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for i in range(total_frames):
            t = i / fps
            progress = clamp01(t / duration) if duration else 1.0
            frame = draw_frame_fn(t, progress)
            if frame.mode != "RGB":
                frame = frame.convert("RGB")
            if frame.size != (WIDTH, HEIGHT):
                frame = frame.resize((WIDTH, HEIGHT), Image.LANCZOS)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    stderr = proc.stderr.read() if proc.stderr else b""
    if proc.wait() != 0:
        raise RuntimeError("motion engine ffmpeg encode failed: " + stderr.decode("utf-8", "replace")[:300])
    info = probe_video(out_path)
    if info["width"] != WIDTH or info["height"] != HEIGHT or info["duration"] <= 0:
        raise RuntimeError(f"motion engine output failed verification: {info}")
    return str(out_path)


# render_template is the template-facing name for the same verified path.
def render_template(draw_frame_fn, out_path, duration=5.0, fps=FPS):
    return render_video(draw_frame_fn, out_path, duration=duration, fps=fps)


# ---------------------------------------------------------------------------
# Layout: safe areas, camera mapping, label placement (Stage 5,
# Motion Graphics V2 brief — the v1 benchmark's multilayer piece
# cropped its title and stat panel at the frame edges, and the
# technical diagram's labels collided; this machinery is the fix)
# ---------------------------------------------------------------------------

# 5% title-safe margins for the 1920x1080 canvas. Content that must
# survive a centre zoom is composed inside this rect.
SAFE_MARGIN_X = 96
SAFE_MARGIN_Y = 54


def safe_rect():
    """(x0, y0, x1, y1) of the title-safe area."""
    return (SAFE_MARGIN_X, SAFE_MARGIN_Y,
            WIDTH - SAFE_MARGIN_X, HEIGHT - SAFE_MARGIN_Y)


def rect_after_camera(rect, scale=1.0, cam_x=0.0, cam_y=0.0, parallax=1.0):
    """Map a layer-space rect through Canvas.composite's camera
    transform (the layer is scaled about the frame centre; the
    camera x/y shift is scaled by the layer's parallax). Returns
    the frame-space rect the content lands in."""
    x0, y0, x1, y1 = rect
    off_x = (WIDTH - WIDTH * scale) / 2 + cam_x * parallax
    off_y = (HEIGHT - HEIGHT * scale) / 2 + cam_y * parallax
    return (x0 * scale + off_x, y0 * scale + off_y,
            x1 * scale + off_x, y1 * scale + off_y)


def rect_in_frame(rect, width=WIDTH, height=HEIGHT):
    x0, y0, x1, y1 = rect
    return x0 >= 0 and y0 >= 0 and x1 <= width and y1 <= height


def max_centre_zoom(rect):
    """Largest centre zoom (no camera translation) under which a
    layer-space rect stays inside the frame. For the safe rect
    this is ~1.11 — content composed inside the safe area survives
    the zooms the engine actually uses (<= 1.10)."""
    x0, y0, x1, y1 = rect
    dx = max(abs(x0 - WIDTH / 2), abs(x1 - WIDTH / 2))
    dy = max(abs(y0 - HEIGHT / 2), abs(y1 - HEIGHT / 2))
    limits = []
    if dx > 0:
        limits.append((WIDTH / 2) / dx)
    if dy > 0:
        limits.append((HEIGHT / 2) / dy)
    return min(limits) if limits else float("inf")


def _boxes_overlap(a, b, gap=0.0):
    return not (
        a[2] + gap <= b[0] or b[2] + gap <= a[0]
        or a[3] + gap <= b[1] or b[3] + gap <= a[1]
    )


def place_labels(items, bounds=None, gap=10.0):
    """Collision-resolving label placement.

    items: [{"key", "cx", "cy", "w", "h"}] — desired centre and
    box size per label. Returns {key: (x0, y0, x1, y1)} with every
    box clamped inside `bounds` (default: the safe rect) and no
    two boxes overlapping. Boxes are NEVER resized — collisions
    are resolved by offsetting/stacking only, so text is never
    shrunk to fit. Deterministic: input order is preserved and
    the offset search order is fixed (down, up, then sideways,
    in growing steps)."""
    bounds = bounds or safe_rect()
    bx0, by0, bx1, by1 = (float(v) for v in bounds)
    placed = []
    result = {}
    for item in items:
        w, h = float(item["w"]), float(item["h"])
        cx, cy = float(item["cx"]), float(item["cy"])

        def box_at(centre_x, centre_y):
            x0 = min(max(centre_x - w / 2, bx0), bx1 - w)
            y0 = min(max(centre_y - h / 2, by0), by1 - h)
            return (x0, y0, x0 + w, y0 + h)

        candidates = [box_at(cx, cy)]
        step = h + gap
        for k in range(1, 7):
            candidates.append(box_at(cx, cy + k * step))
            candidates.append(box_at(cx, cy - k * step))
        for k in range(1, 5):
            candidates.append(box_at(cx + k * (w / 2 + gap), cy))
            candidates.append(box_at(cx - k * (w / 2 + gap), cy))
        chosen = None
        for candidate in candidates:
            if not any(_boxes_overlap(candidate, other, gap)
                       for other in placed):
                chosen = candidate
                break
        if chosen is None:
            # No free slot in the search pattern: keep the clamped
            # anchor position (still inside bounds, size intact).
            chosen = candidates[0]
        placed.append(chosen)
        result[item["key"]] = chosen
    return result

