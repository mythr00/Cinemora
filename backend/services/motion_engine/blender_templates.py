"""Blender 3D production department (Animation Studio
Expansion, mission §4) — genuine Blender scenes rendered
headlessly into house-format motion graphics, integrated with
the same discipline as the Manim engine:

  * scene code is FIXED production code in ``blender_scenes/``
    (a shared rig/material library plus one module per
    department scene); production data reaches a scene ONLY
    through a JSON payload file — never interpolated into code;
  * renders run in a Blender subprocess (``blender -b``), so a
    scene crash can never take the pipeline down;
  * availability is probed (binary discovery + version run);
    an unavailable engine refuses typed (``blender_unavailable``)
    and the chain falls through to the next treatment;
  * output is a PNG sequence conformed with ONE ffmpeg pass to
    the house format (1920x1080, 30 fps, exact duration);
  * every result is stamped ``visualisation: illustrative`` —
    Blender scenes visualise recorded descriptions; they are
    never evidence, and the Director's factual-reconstruction
    decline is untouched;
  * contracts are recorded data only: an exploded view needs a
    recorded geometry contract, a terrain a recorded elevation
    grid, a building a recorded structure, a gear train
    recorded gears. No contract -> typed refusal, no scene.

Render engine: Cycles CPU (EEVEE needs a system EGL the host
class does not guarantee). Internal renders run at reduced
resolution (default 960x540) and 15 fps with OpenImageDenoise
and capped light paths — measured on this host class
(2026-11-28), where Blender's own factory-default cube scene
needs ~8 s/frame at 640x360 — conformed up to the house
format; resolution-aware rendering per mission §10, with the
Director's contract-gating keeping these expensive treatments
rare. Switches: CINEMORA_BLENDER_ENABLED (0 disables),
CINEMORA_BLENDER_BIN, CINEMORA_BLENDER_WIDTH / _HEIGHT /
_SAMPLES / _FPS, CINEMORA_BLENDER_TIMEOUT.
"""

from __future__ import annotations

import functools
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from services.motion_engine.templates import (
    TEMPLATES, _clean, _meta,
)
from services.motion_engine.styles import resolve_style

_SCENES_DIR = Path(__file__).resolve().parent / "blender_scenes"
_RENDER_TIMEOUT = int(os.environ.get("CINEMORA_BLENDER_TIMEOUT", "1800"))
_INTERNAL_FPS = int(os.environ.get("CINEMORA_BLENDER_FPS", "15"))

_DRIVER = """\
import importlib
import os
import sys

sys.path.insert(0, os.environ["CINEMORA_BLENDER_SCENES"])
module = importlib.import_module(
    "blender_scenes." + os.environ["CINEMORA_BLENDER_SCENE"])
module.main()
"""


def _blender_disabled() -> bool:
    return (os.environ.get("CINEMORA_BLENDER_ENABLED", "1").strip()
            .lower() in ("0", "false", "no", "off"))


def blender_binary() -> str | None:
    override = os.environ.get("CINEMORA_BLENDER_BIN")
    if override:
        return override if Path(override).exists() else None
    on_path = shutil.which("blender")
    if on_path:
        return on_path
    tools = sorted(glob.glob(
        os.path.expanduser("~/workspace/tools/blender-*/blender")))
    return tools[-1] if tools else None


@functools.lru_cache(maxsize=1)
def _probe() -> tuple[bool, str]:
    if _blender_disabled():
        return False, ""
    binary = blender_binary()
    if not binary:
        return False, ""
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True,
            timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if proc.returncode != 0:
        return False, ""
    first = (proc.stdout or "").splitlines()
    version = first[0].replace("Blender", "").strip() if first else ""
    return True, version


def blender_available() -> bool:
    return _probe()[0]


def _blender_version() -> str:
    return _probe()[1]


def _hex(value) -> str:
    """Theme tokens are RGB tuples (the themes system's shape);
    format them as hex, mirroring the Manim engine's helper.
    Strings pass through; anything else falls back to grey."""
    if isinstance(value, str):
        return value
    try:
        return "#{:02x}{:02x}{:02x}".format(
            *(int(c) for c in value[:3]))
    except (TypeError, ValueError, IndexError):
        return "#888888"


def _text(value, cap: int) -> str:
    """Whitespace-cleaned, length-capped recorded text."""
    return _clean(value)[:cap]


def _payload(data, tokens, duration):
    return {
        "duration": float(duration),
        "fps": _INTERNAL_FPS,
        "width": int(os.environ.get("CINEMORA_BLENDER_WIDTH", "960")),
        "height": int(os.environ.get("CINEMORA_BLENDER_HEIGHT", "540")),
        "samples": int(os.environ.get("CINEMORA_BLENDER_SAMPLES", "12")),
        "colors": {
            "bg": _hex(tokens["bg"]),
            "surface": _hex(tokens["surface"]),
            "text": _hex(tokens["text"]),
            "muted": _hex(tokens["muted"]),
            "accent": _hex(tokens["accent"]),
            "accent2": _hex(tokens["accent2"]),
        },
        "data": data,
    }


def _conform(frames_dir: Path, out_path, duration: float,
             transparent: bool) -> bool:
    """One ffmpeg pass: PNG sequence -> house format. Opaque
    renders scale/pad to 1920x1080 at 30 fps for exactly the
    requested duration (fps conversion duplicates frames — the
    slow department camera moves carry it)."""
    vf = (
        f"fps=30,scale=1920:1080:flags=lanczos,"
        f"tpad=stop_mode=clone:stop_duration={float(duration):.3f},"
        f"trim=duration={float(duration):.3f},setpts=PTS-STARTPTS,"
        "format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-framerate", str(_INTERNAL_FPS),
        "-i", str(frames_dir / "frame_%04d.png"),
        "-vf", vf, "-r", "30",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=600)
    return proc.returncode == 0 and Path(out_path).exists()


def _render_scene(scene_name, payload, out_path, duration):
    """Returns (stats, None) on success or (None, reason)."""
    if not blender_available():
        return None, "blender_unavailable"
    binary = blender_binary()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
            prefix="cinemora-blender-") as tmp:
        tmp_dir = Path(tmp)
        frames_dir = tmp_dir / "frames"
        frames_dir.mkdir()
        data_file = tmp_dir / "payload.json"
        data_file.write_text(json.dumps(payload), encoding="utf-8")
        driver = tmp_dir / "driver.py"
        driver.write_text(_DRIVER, encoding="utf-8")
        env = dict(os.environ)
        env["CINEMORA_BLENDER_DATA"] = str(data_file)
        env["CINEMORA_BLENDER_OUT"] = str(frames_dir)
        env["CINEMORA_BLENDER_SCENES"] = str(_SCENES_DIR.parent)
        env["CINEMORA_BLENDER_SCENE"] = scene_name
        try:
            proc = subprocess.run(
                [binary, "-b", "--factory-startup", "-P", str(driver)],
                capture_output=True, text=True, timeout=_RENDER_TIMEOUT,
                env=env)
        except subprocess.TimeoutExpired:
            return None, "blender_render_timeout"
        except OSError:
            return None, "blender_unavailable"
        frames = sorted(frames_dir.glob("frame_*.png"))
        if proc.returncode != 0 or not frames:
            return None, "blender_render_failed"
        if not _conform(frames_dir, out_path, duration, False):
            return None, "blender_conform_failed"
        return {
            "frames_rendered": len(frames),
            "render_seconds": round(time.monotonic() - started, 2),
            "internal_resolution": [
                payload["width"], payload["height"]],
            "internal_fps": payload["fps"],
            "samples": payload["samples"],
        }, None


def _finish(template, scene_name, style_name, duration,
            sentence_index, scene_number, inputs, payload, out_path,
            data):
    stats, reason = _render_scene(scene_name, payload, out_path,
                                  duration)
    if stats is None:
        return None, {"reason": reason, "engine": "blender",
                      "template": template}
    meta = _meta(template, style_name, duration, sentence_index,
                 scene_number, data)
    meta["engine"] = "blender"
    meta["blender_version"] = _blender_version()
    meta["visualisation"] = "illustrative"
    meta["scene"] = scene_name
    meta["inputs"] = inputs
    meta["render_stats"] = stats
    return str(out_path), meta


# ---------------------------------------------------------------------------
# Contract validation — recorded data only
# ---------------------------------------------------------------------------

def _vec3(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        return [float(v) for v in value]
    except (TypeError, ValueError):
        return None


def _geometry_parts(data):
    parts = data.get("parts") or data.get("geometry")
    if not isinstance(parts, list) or len(parts) < 2:
        return None
    cleaned = []
    for part in parts[:24]:
        if not isinstance(part, dict):
            return None
        position = _vec3(part.get("position"))
        if position is None:
            return None
        size = part.get("size")
        if not isinstance(size, (list, tuple)) or not size:
            return None
        try:
            size = [float(v) for v in size]
        except (TypeError, ValueError):
            return None
        cleaned.append({
            "name": _text(part.get("name"), 60) or "part",
            "shape": (part.get("shape") if part.get("shape") in (
                "box", "cylinder", "sphere", "cone") else "box"),
            "size": size,
            "position": position,
            "explode": _vec3(part.get("explode")) or [0.0, 0.0, 0.0],
            "material": (part.get("material") if part.get("material")
                         in ("body", "accent", "accent2", "muted",
                               "glass") else "body"),
        })
    return cleaned


def _gears(data):
    gears = data.get("gears")
    if not isinstance(gears, list) or not gears:
        return None
    cleaned = []
    for gear in gears[:8]:
        if not isinstance(gear, dict):
            return None
        position = _vec3(gear.get("position"))
        try:
            radius = float(gear.get("radius"))
            teeth = int(gear.get("teeth"))
        except (TypeError, ValueError):
            return None
        if position is None or radius <= 0 or teeth < 6:
            return None
        cleaned.append({
            "name": _text(gear.get("name"), 60) or "gear",
            "radius": radius, "teeth": teeth,
            "thickness": float(gear.get("thickness") or 0.5),
            "position": position,
            "speed": float(gear.get("speed") or 6.0),
            "phase": float(gear.get("phase") or 0.0),
            "direction": -1 if float(gear.get("direction") or 1) < 0
            else 1,
            "material": (gear.get("material") if gear.get("material")
                         in ("body", "accent", "accent2", "muted")
                         else "accent"),
        })
    shafts = []
    for shaft in data.get("shafts") or []:
        if isinstance(shaft, dict) and _vec3(shaft.get("position")):
            shafts.append({
                "position": _vec3(shaft["position"]),
                "length": float(shaft.get("length") or 6.0),
                "radius": float(shaft.get("radius") or 0.18),
            })
    return {"gears": cleaned, "shafts": shafts,
            "camera": data.get("camera")}


def _structure(data):
    structure = data.get("structure")
    if not isinstance(structure, dict):
        return None
    try:
        floors = int(structure.get("floors"))
    except (TypeError, ValueError):
        return None
    if floors < 1 or floors > 40:
        return None
    return {
        "floors": floors,
        "width": float(structure.get("width") or 10.0),
        "depth": float(structure.get("depth") or 8.0),
        "floor_height": float(structure.get("floor_height") or 1.1),
        "lit_fraction": min(1.0, max(0.0, float(
            structure.get("lit_fraction") or 0.35))),
        "material": (structure.get("material")
                     if structure.get("material") in (
                         "body", "accent", "accent2", "muted")
                     else "body"),
    }


def _terrain(data):
    terrain = data.get("terrain")
    if not isinstance(terrain, dict):
        return None
    grid = terrain.get("grid")
    if (not isinstance(grid, list) or len(grid) < 4
            or not all(isinstance(row, list) and len(row) >= 4
                       for row in grid)):
        return None
    try:
        cleaned = [[min(1.0, max(0.0, float(v))) for v in row]
                   for row in grid]
    except (TypeError, ValueError):
        return None
    if any(len(row) != len(cleaned[0]) for row in cleaned):
        return None
    markers = []
    for marker in terrain.get("markers") or []:
        if isinstance(marker, dict):
            markers.append({
                "x": min(1.0, max(0.0, float(marker.get("x") or 0.5))),
                "y": min(1.0, max(0.0, float(marker.get("y") or 0.5))),
            })
    return {
        "grid": cleaned, "markers": markers[:12],
        "relief": float(terrain.get("relief") or 3.2),
        "provenance": _text(terrain.get("provenance"), 120),
    }


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def blender_exploded_view(data, style, out_path, duration=5.0,
                          sentence_index=None, scene_number=None):
    parts = _geometry_parts(data)
    if parts is None:
        return None, {"reason": "blender_geometry_missing"}
    if not blender_available():
        return None, {"reason": "blender_unavailable"}
    style_name, tokens = resolve_style(style)
    scene_data = {"parts": parts, "camera": data.get("camera")}
    return _finish(
        "blender_exploded_view", "exploded_view", style_name,
        duration, sentence_index, scene_number,
        {"parts": len(parts)}, _payload(scene_data, tokens, duration),
        out_path, data)


def blender_mechanical(data, style, out_path, duration=5.0,
                       sentence_index=None, scene_number=None):
    gears = _gears(data)
    if gears is None:
        return None, {"reason": "blender_gears_missing"}
    if not blender_available():
        return None, {"reason": "blender_unavailable"}
    style_name, tokens = resolve_style(style)
    return _finish(
        "blender_mechanical", "mechanical", style_name, duration,
        sentence_index, scene_number,
        {"gears": len(gears["gears"])},
        _payload(gears, tokens, duration), out_path, data)


def blender_building(data, style, out_path, duration=5.0,
                     sentence_index=None, scene_number=None):
    structure = _structure(data)
    if structure is None:
        return None, {"reason": "blender_structure_missing"}
    if not blender_available():
        return None, {"reason": "blender_unavailable"}
    style_name, tokens = resolve_style(style)
    scene_data = {"structure": structure, "camera": data.get("camera")}
    return _finish(
        "blender_building", "building", style_name, duration,
        sentence_index, scene_number,
        {"floors": structure["floors"]},
        _payload(scene_data, tokens, duration), out_path, data)


def blender_terrain(data, style, out_path, duration=5.0,
                    sentence_index=None, scene_number=None):
    terrain = _terrain(data)
    if terrain is None:
        return None, {"reason": "blender_terrain_missing"}
    if not blender_available():
        return None, {"reason": "blender_unavailable"}
    style_name, tokens = resolve_style(style)
    scene_data = {"terrain": terrain, "camera": data.get("camera")}
    return _finish(
        "blender_terrain", "terrain", style_name, duration,
        sentence_index, scene_number,
        {"grid": [len(terrain["grid"]), len(terrain["grid"][0])],
         "markers": len(terrain["markers"]),
         "provenance": terrain["provenance"] or None},
        _payload(scene_data, tokens, duration), out_path, data)


def blender_title_3d(data, style, out_path, duration=5.0,
                     sentence_index=None, scene_number=None):
    title = _text(data.get("title"), 60)
    if not title:
        return None, {"reason": "no_title"}
    if not blender_available():
        return None, {"reason": "blender_unavailable"}
    style_name, tokens = resolve_style(style)
    scene_data = {
        "title": title,
        "kicker": _text(data.get("kicker"), 80),
        "date_label": _text(data.get("date_label"), 40),
    }
    return _finish(
        "blender_title_3d", "title_3d", style_name, duration,
        sentence_index, scene_number,
        {"title": True, "kicker": bool(scene_data["kicker"]),
         "date_label": bool(scene_data["date_label"])},
        _payload(scene_data, tokens, duration), out_path, data)


TEMPLATES.update({
    "blender_exploded_view": blender_exploded_view,
    "blender_mechanical": blender_mechanical,
    "blender_building": blender_building,
    "blender_terrain": blender_terrain,
    "blender_title_3d": blender_title_3d,
})
