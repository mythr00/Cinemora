"""Web render engine (Animation Studio Expansion, mission
§5) — genuine web-technology motion graphics rendered through
headless Chromium, integrated with the same discipline as the
Manim and Blender engines:

  * the harness (``backend/web_engine/harness.mjs``) is FIXED
    production code; production data reaches it ONLY through a
    job JSON file — never interpolated into code;
  * rendering runs in a Node subprocess; frames are captured
    deterministically by seeking each scene's timeline, then
    conformed with ONE ffmpeg pass to the house format
    (1920x1080, 30 fps, exact duration);
  * availability is probed (Node + an engine home carrying
    node_modules); an unavailable engine refuses typed
    (``web_engine_unavailable``) and the chain falls through;
  * contracts are recorded data only: the globe and the Lottie
    route need recorded places that resolve through the
    geography dataset (never invented coordinates); the kinetic
    type treatment needs a recorded SHORT quoted passage — the
    same payload discipline as text_highlight (founder ruling:
    no full-sentence text cards).

Engine home: CINEMORA_WEB_ENGINE_HOME, else
``~/workspace/tools/webengines`` when it carries node_modules,
else ``backend/web_engine`` (after ``npm install`` there).
Switches: CINEMORA_WEB_ENABLED (0 disables),
CINEMORA_WEB_TIMEOUT, CINEMORA_WEB_WIDTH/_HEIGHT (internal
capture size; frames conform up to 1920x1080).
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from services.motion_engine import geography
from services.motion_engine.templates import TEMPLATES, _clean, _meta
from services.motion_engine.styles import resolve_style

_REPO_ENGINE = Path(__file__).resolve().parents[2] / "web_engine"
_TOOLS_ENGINE = Path.home() / "workspace" / "tools" / "webengines"
_RENDER_TIMEOUT = int(os.environ.get("CINEMORA_WEB_TIMEOUT", "900"))
_FPS = 30
_W = int(os.environ.get("CINEMORA_WEB_WIDTH", "1280"))
_H = int(os.environ.get("CINEMORA_WEB_HEIGHT", "720"))


def _engine_home() -> Path | None:
    """The directory carrying the installed node_modules (the
    harness serves them); the harness file itself is canonical
    in the repo and is told this home via the environment."""
    override = os.environ.get("CINEMORA_WEB_ENGINE_HOME")
    candidates = [Path(override)] if override else []
    candidates += [_TOOLS_ENGINE, _REPO_ENGINE]
    for home in candidates:
        if (home / "node_modules" / "puppeteer").is_dir() \
                and (home / "node_modules" / "three").is_dir():
            return home
    return None


def _harness_path() -> Path:
    return _REPO_ENGINE / "harness.mjs"


def _disabled() -> bool:
    return (os.environ.get("CINEMORA_WEB_ENABLED", "1").strip()
            .lower() in ("0", "false", "no", "off"))


@functools.lru_cache(maxsize=1)
def _probe() -> bool:
    if _disabled():
        return False
    if shutil.which("node") is None:
        return False
    return _engine_home() is not None


def web_engine_available() -> bool:
    return _probe()


def _hex(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return "#{:02x}{:02x}{:02x}".format(
            *(int(c) for c in value[:3]))
    except (TypeError, ValueError, IndexError):
        return "#888888"


def _colors(tokens):
    return {key: _hex(tokens[key]) for key in
            ("bg", "surface", "text", "muted", "accent", "accent2")}


def _conform(frames_dir: Path, out_path, duration: float) -> bool:
    vf = (
        "scale=1920:1080:flags=lanczos,"
        f"tpad=stop_mode=clone:stop_duration={float(duration):.3f},"
        f"trim=duration={float(duration):.3f},setpts=PTS-STARTPTS,"
        "format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-framerate", str(_FPS),
        "-i", str(frames_dir / "frame_%04d.png"),
        "-vf", vf, "-r", "30",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=600)
    return proc.returncode == 0 and Path(out_path).exists()


def _render_job(scene, data, tokens, duration, out_path):
    """Returns (stats, None) or (None, reason)."""
    if not web_engine_available():
        return None, "web_engine_unavailable"
    home = _engine_home()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
            prefix="cinemora-web-") as tmp:
        tmp_dir = Path(tmp)
        frames_dir = tmp_dir / "frames"
        frames_dir.mkdir()
        job = {
            "scene": scene, "duration": float(duration), "fps": _FPS,
            "width": _W, "height": _H, "colors": _colors(tokens),
            "data": data, "outDir": str(frames_dir),
        }
        job_file = tmp_dir / "job.json"
        job_file.write_text(json.dumps(job), encoding="utf-8")
        env = dict(os.environ)
        env["CINEMORA_WEB_ENGINE_HOME"] = str(home)
        try:
            proc = subprocess.run(
                ["node", str(_harness_path()), str(job_file)],
                capture_output=True, text=True,
                timeout=_RENDER_TIMEOUT, cwd=str(home), env=env)
        except subprocess.TimeoutExpired:
            return None, "web_render_timeout"
        except OSError:
            return None, "web_engine_unavailable"
        frames = sorted(frames_dir.glob("frame_*.png"))
        if proc.returncode != 0 or not frames:
            return None, "web_render_failed"
        if not _conform(frames_dir, out_path, duration):
            return None, "web_conform_failed"
        return {
            "frames_rendered": len(frames),
            "render_seconds": round(time.monotonic() - started, 2),
            "internal_resolution": [_W, _H],
        }, None


def _finish(template, scene, style_name, duration, sentence_index,
            scene_number, inputs, data, tokens, out_path, raw):
    stats, reason = _render_job(scene, data, tokens, duration,
                                out_path)
    if stats is None:
        return None, {"reason": reason, "engine": "web",
                      "template": template}
    meta = _meta(template, style_name, duration, sentence_index,
                 scene_number, raw)
    meta["engine"] = "web"
    meta["web_scene"] = scene
    meta["inputs"] = inputs
    meta["render_stats"] = stats
    return str(out_path), meta


# ---------------------------------------------------------------------------
# Lottie document generation — recorded route -> Lottie JSON
# ---------------------------------------------------------------------------

def _lottie_route_document(points, colors, duration):
    """A Lottie document (data, generated from the recorded
    route's projected points): a faint full path, the accent
    path drawing on via trim-paths, endpoint markers that pop
    when the draw reaches them, and a pulse travelling the
    path. Colours are the resolved theme tokens."""
    frames = max(1, int(round(duration * _FPS)))
    draw_end = max(2, int(frames * 0.7))

    def _kf(t, s, e=None):
        entry = {"t": t, "s": s}
        if e is not None:
            entry["e"] = e
            entry["i"] = {"x": [0.4], "y": [1.0]}
            entry["o"] = {"x": [0.6], "y": [0.0]}
        return entry

    def _rgb(hex_value):
        h = hex_value.lstrip("#")
        return [int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
                int(h[4:6], 16) / 255.0, 1.0]

    path_shape = {
        "ty": "sh",
        "ks": {"a": 0, "k": {
            "i": [[0, 0]] * (len(points) - 1) + [[0, 0]],
            "o": [[0, 0]] * (len(points) - 1) + [[0, 0]],
            "v": [[float(x), float(y)] for x, y in points],
            "c": False}},
    }

    def _path_layer(name, color, width_px, opacity, trim):
        items = [path_shape,
                 {"ty": "st", "c": {"a": 0, "k": _rgb(color)},
                  "o": {"a": 0, "k": 100}, "w": {"a": 0, "k": width_px},
                  "lc": 2, "lj": 2}]
        if trim:
            items.append({
                "ty": "tm",
                "s": {"a": 0, "k": 0},
                "e": {"a": 1, "k": [_kf(0, [0], [100]),
                                   _kf(draw_end, [100])]},
                "o": {"a": 0, "k": 0}, "m": 1})
        items.append({"ty": "tr", "p": {"a": 0, "k": [0, 0]},
                      "a": {"a": 0, "k": [0, 0]},
                      "s": {"a": 0, "k": [100, 100]},
                      "r": {"a": 0, "k": 0},
                      "o": {"a": 0, "k": opacity}})
        return {"ddd": 0, "ind": 1,
                "ty": 4, "nm": name,
                "ks": {"o": {"a": 0, "k": 100},
                       "r": {"a": 0, "k": 0},
                       "p": {"a": 0, "k": [0, 0, 0]},
                       "a": {"a": 0, "k": [0, 0, 0]},
                       "s": {"a": 0, "k": [100, 100, 100]}},
                "shapes": [{"ty": "gr", "it": items}],
                "ip": 0, "op": frames, "st": 0}

    def _marker_layer(name, point, at_frame):
        return {"ddd": 0, "ind": 2,
                "ty": 4, "nm": name,
                "ks": {"o": {"a": 0, "k": 100},
                       "r": {"a": 0, "k": 0},
                       "p": {"a": 0, "k": [float(point[0]),
                                          float(point[1]), 0]},
                       "a": {"a": 0, "k": [0, 0, 0]},
                       "s": {"a": 1, "k": [
                           _kf(at_frame, [0, 0, 100],
                               [130, 130, 100]),
                           _kf(min(frames - 1, at_frame + 8),
                               [130, 130, 100], [100, 100, 100])]}},
                "shapes": [{"ty": "gr", "it": [
                    {"ty": "el", "p": {"a": 0, "k": [0, 0]},
                     "s": {"a": 0, "k": [26, 26]}},
                    {"ty": "fl", "c": {"a": 0, "k": _rgb(colors["accent"])},
                     "o": {"a": 0, "k": 100}},
                    {"ty": "tr", "p": {"a": 0, "k": [0, 0]},
                     "a": {"a": 0, "k": [0, 0]},
                     "s": {"a": 0, "k": [100, 100]},
                     "r": {"a": 0, "k": 0},
                     "o": {"a": 0, "k": 100}}]}],
                "ip": 0, "op": frames, "st": 0}

    pulse_travel = []
    n = len(points)
    for i, point in enumerate(points):
        t = int(draw_end * i / (n - 1)) if n > 1 else 0
        pulse_travel.append(_kf(
            t, [float(point[0]), float(point[1]), 0],
            [float(points[min(i + 1, n - 1)][0]),
             float(points[min(i + 1, n - 1)][1]), 0]
            if i + 1 < n else None))
    pulse = {"ddd": 0, "ind": 999, "ty": 4, "nm": "pulse",
             "ks": {"o": {"a": 0, "k": 100}, "r": {"a": 0, "k": 0},
                    "p": {"a": 1, "k": pulse_travel},
                    "a": {"a": 0, "k": [0, 0, 0]},
                    "s": {"a": 0, "k": [100, 100, 100]}},
             "shapes": [{"ty": "gr", "it": [
                 {"ty": "el", "p": {"a": 0, "k": [0, 0]},
                  "s": {"a": 0, "k": [15, 15]}},
                 {"ty": "fl",
                  "c": {"a": 0, "k": _rgb(colors["accent2"])},
                  "o": {"a": 0, "k": 100}},
                 {"ty": "tr", "p": {"a": 0, "k": [0, 0]},
                  "a": {"a": 0, "k": [0, 0]},
                  "s": {"a": 0, "k": [100, 100]},
                  "r": {"a": 0, "k": 0}, "o": {"a": 0, "k": 100}}]}],
             "ip": 0, "op": frames, "st": 0}

    layers = [
        _path_layer("route-draw", colors["accent"], 6, 100, True),
        _path_layer("route-underlay", colors["muted"], 5, 30, False),
        _marker_layer("marker-start", points[0], 0),
        _marker_layer("marker-end", points[-1], draw_end),
        pulse,
    ]
    for index, layer in enumerate(layers):
        layer["ind"] = index + 1
    return {"v": "5.7.4", "fr": _FPS, "ip": 0, "op": frames,
            "w": _W, "h": _H, "nm": "cinemora-route",
            "layers": layers}


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def _resolved_places(data):
    from services.motion_engine.templates_v5 import _resolve_place
    raw = data.get("places")
    if not isinstance(raw, list):
        return []
    resolved = []
    for place in raw:
        one = _resolve_place(place)
        if one is not None:
            resolved.append(one)
    return resolved


def three_globe(data, style, out_path, duration=5.0,
                sentence_index=None, scene_number=None):
    places = _resolved_places(data)
    if not places:
        return None, {"reason": "globe_places_missing"}
    if not web_engine_available():
        return None, {"reason": "web_engine_unavailable"}
    style_name, tokens = resolve_style(style)
    scene_data = {"places": [
        {"name": p["name"], "lon": p["lon"], "lat": p["lat"]}
        for p in places[:8]]}
    return _finish(
        "three_globe", "globe", style_name, duration,
        sentence_index, scene_number,
        {"places": len(scene_data["places"]),
         "arcs": max(0, len(scene_data["places"]) - 1)},
        scene_data, tokens, out_path, data)


def gsap_kinetic_type(data, style, out_path, duration=5.0,
                      sentence_index=None, scene_number=None):
    text = _clean(data.get("text"))
    if not text or len(text) > 90 or len(text.split()) < 2:
        return None, {"reason": "kinetic_text_missing"}
    if not web_engine_available():
        return None, {"reason": "web_engine_unavailable"}
    style_name, tokens = resolve_style(style)
    scene_data = {"text": text,
                  "attribution": _clean(data.get("attribution"))[:60]}
    return _finish(
        "gsap_kinetic_type", "kinetic", style_name, duration,
        sentence_index, scene_number,
        {"words": len(text.split()),
         "attribution": bool(scene_data["attribution"])},
        scene_data, tokens, out_path, data)


def lottie_route(data, style, out_path, duration=5.0,
                 sentence_index=None, scene_number=None):
    places = _resolved_places(data)
    if len(places) < 2:
        return None, {"reason": "lottie_route_places_missing"}
    if not web_engine_available():
        return None, {"reason": "web_engine_unavailable"}
    style_name, tokens = resolve_style(style)
    box = geography.places_bbox([(p["lon"], p["lat"]) for p in places])
    if box is None:
        return None, {"reason": "lottie_route_places_missing"}
    projection = geography.Projection(
        geography.padded_places_bbox(box),
        rect=(0.08, 0.14, 0.92, 0.86))
    points = [projection.project(p["lon"], p["lat"]) for p in places]
    # The projection works in normalised canvas fractions;
    # scale into the Lottie document's capture space.
    points = [(x * _W, y * _H) for x, y in points]
    colors = _colors(tokens)
    document = _lottie_route_document(points, colors, duration)
    return _finish(
        "lottie_route", "lottie", style_name, duration,
        sentence_index, scene_number,
        {"places": len(places), "lottie_layers": len(document["layers"])},
        {"lottie": document}, tokens, out_path, data)


TEMPLATES.update({
    "three_globe": three_globe,
    "gsap_kinetic_type": gsap_kinetic_type,
    "lottie_route": lottie_route,
})
