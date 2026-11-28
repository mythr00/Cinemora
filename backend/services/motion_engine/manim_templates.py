"""Motion Engine templates — Manim render engine (GitHub Skills
Research & Enhancement Programme, 2026-11-25).

A SECOND render engine behind the exact v1 template contract.
The PIL compositor (templates / templates_v2..v4) draws frame by
frame in Python; Manim (ManimCE, MIT) renders mathematically
typeset animation — smooth draw-on curves, growing bars, staged
reveals — that the PIL engine cannot match for pure data scenes.
Both engines consume the SAME recorded engine data and return the
SAME (rendered, metadata) / (None, {"reason": ...}) results, so
the graphics pipeline, the Director's candidate ordering and the
editor's recipe regeneration treat them identically.

Design rules (the house disciplines, unchanged):

* Templates render ONLY caller-supplied recorded data. The scene
  sources below are fixed code; production data reaches them as a
  JSON file (env CINEMORA_MANIM_DATA) — data is never interpolated
  into code, so a crafted label cannot inject scene code.
* Manim's partial-movie cache is DISABLED on every render: the
  cache key covers scene code, not the external JSON data, so a
  cached fragment could silently serve a previous production's
  numbers. Correctness over render speed, always.
* Fail-soft: when Manim is not installed (or is disabled via
  CINEMORA_MANIM_ENABLED=0) the templates refuse with the typed
  reason "manim_unavailable" and the pipeline falls through to
  the PIL candidates — a host without Manim loses the richer
  treatment, never the graphic.
* The expensive engine is gated twice: the Director proposes
  these templates only on recorded-evidence conditions (a real
  series / comparison on the record), and each template refuses
  small payloads the PIL engine already serves well (a series
  under 3 points, a comparison under 3 items) BEFORE any
  subprocess starts, so the ~15-25 s render is never spent on a
  two-bar chart.
* Output is conformed to the house format with one ffmpeg pass
  (1920x1080, 30 fps, libx264 crf 20, yuv420p, exact requested
  duration) so validate_graphic_output sees the same contract
  every other template satisfies.
* Text is Pango (Manim Text) in the bundled DejaVu Sans — no
  LaTeX anywhere, so no TeX system dependency exists. A
  fontconfig file written per render points Pango at the bundled
  font directory, so the scenes render identically on hosts
  without system DejaVu.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from services.motion_engine.styles import _FONT_DIR, resolve_style
from services.motion_engine.templates import _clean, _meta
from services.motion_engine.templates_v3 import _numeric_items

#: Subprocess budget for one Manim render. A 5 s 1080p30 chart
#: renders in ~15-25 s on the reference VM (1.5 shared CPUs);
#: the cap is generous so a loaded host degrades to a recorded
#: refusal, never a hung worker.
RENDER_TIMEOUT_SECONDS = 900

_MIN_SERIES_POINTS = 3
_MIN_BAR_ITEMS = 3
_MAX_SERIES_POINTS = 24
_MAX_BAR_ITEMS = 8


def manim_available():
    """True when the Manim engine can be used on this host:
    not disabled by the operator and the package is importable.
    Uses find_spec — the (heavy) manim package is never imported
    into the worker process; renders run in a subprocess."""
    if os.environ.get("CINEMORA_MANIM_ENABLED", "1").strip() == "0":
        return False
    try:
        return importlib.util.find_spec("manim") is not None
    except Exception:
        return False


def _recorded_chart_guard(data):
    """The recorded-data chart contract (Phase 5 B5/B7), applied
    identically in every chart engine (Animation Studio Expansion,
    mission §9 — chart source-line consistency). A series the
    pipeline assembled from recorded data (marked ``recorded``)
    MUST carry recorded units and a recorded source: either
    missing refuses with the PIL family's typed reasons BEFORE
    any subprocess starts, never a unit-less or source-less
    rendering. Returns the refusal tuple or None."""
    if not data.get("recorded"):
        return None
    if not _clean(data.get("units") or data.get("unit")):
        return None, {"reason": "chart_units_missing",
                      "detail": "recorded series carries no units"}
    if not _clean(data.get("source")):
        return None, {"reason": "chart_source_missing",
                      "detail": "recorded series carries no source"}
    return None


def _estimated_flag(data):
    uncertainty = data.get("uncertainty")
    return bool(
        data.get("estimated")
        or (isinstance(uncertainty, dict) and uncertainty.get("estimated"))
    )


def _manim_version():
    try:
        from importlib.metadata import version

        return version("manim")
    except Exception:
        return None


def _hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(*(int(c) for c in rgb[:3]))


# ---------------------------------------------------------------------------
# Fixed scene sources. Data arrives ONLY via the JSON file named by
# the CINEMORA_MANIM_DATA environment variable.
# ---------------------------------------------------------------------------

_SCENE_COMMON = '''
import json
import os

from manim import *

with open(os.environ["CINEMORA_MANIM_DATA"], "r", encoding="utf-8") as fh:
    DATA = json.load(fh)

C = DATA["colors"]
DURATION = float(DATA.get("duration") or 5.0)
FONT = "DejaVu Sans"


def _title(text, size=40):
    return Text(text[:60], font=FONT, weight=BOLD, color=C["text"],
                font_size=size)


def _label(text, size=22, color=None, bold=False):
    return Text(text, font=FONT, color=color or C["muted"], font_size=size,
                weight=BOLD if bold else NORMAL)


def _source_line(top=False):
    """The small source line (chart source-line consistency,
    mission §9): the recorded source of the data as data
    attribution — never a file name or provider label; estimates
    are labelled estimated. Same wording and 72-char cap as the
    PIL/mpl chart families. None when no source is recorded.
    Placement is per-scene: the bar chart's bottom is free once
    its group lifts; the line chart's axes own its bottom edge,
    so its line sits under the title block instead — presence and
    wording are the consistency contract, not the corner."""
    source = (DATA.get("source") or "").strip()
    if not source:
        return None
    line = f"Source — {source}"
    if DATA.get("estimated"):
        line += " · estimated"
    mob = _label(line[:72], size=18)
    if top:
        mob.to_edge(UP, buff=1.02)
        mob.to_edge(LEFT, buff=0.9)
    else:
        mob.to_edge(DOWN, buff=0.32)
        mob.to_edge(LEFT, buff=0.55)
    return mob
'''

_LINE_CHART_SCENE = _SCENE_COMMON + '''

class CinemoraLineChart(Scene):
    def construct(self):
        self.camera.background_color = C["bg"]
        points = DATA["points"]
        values = [float(p["value"]) for p in points]
        v_max = max(values) if values else 1.0
        v_min = min(0.0, min(values)) if values else 0.0
        span = (v_max - v_min) or 1.0
        y_top = v_min + span * 1.15

        t_title = max(0.4, 0.14 * DURATION)
        t_axes = max(0.5, 0.18 * DURATION)
        t_line = max(0.8, 0.42 * DURATION)
        t_marks = max(0.3, 0.10 * DURATION)
        spent = t_title + t_axes + t_line + t_marks
        t_hold = max(0.3, DURATION - spent)

        title = _title((DATA.get("title") or "").upper())
        title.to_edge(UP, buff=0.55)
        if DATA.get("series_label"):
            sub = _label(DATA["series_label"].upper()[:56], size=24,
                         color=C["accent2"], bold=True)
            sub.next_to(title, DOWN, buff=0.18)

        axes = Axes(
            x_range=[0, max(1, len(points) - 1), 1],
            y_range=[v_min, y_top, span / 4.0],
            x_length=12.6, y_length=6.4,
            axis_config={"color": C["muted"], "stroke_width": 3,
                         "include_ticks": True},
            tips=False,
        )
        axes.next_to(title, DOWN, buff=0.75)

        # Y tick labels as Pango text (Axes' own numbers need LaTeX).
        y_labels = VGroup()
        for i in range(5):
            frac = i / 4.0
            value = v_min + span * frac
            text = f"{value:,.0f}" if float(value).is_integer() \\
                else f"{value:,.1f}"
            lab = _label(text, size=20)
            lab.next_to(axes.c2p(0, v_min + (y_top - v_min) * frac),
                        LEFT, buff=0.18)
            y_labels.add(lab)
        x_labels = VGroup()
        for i, p in enumerate(points):
            lab = _label(str(p.get("label") or "")[:12], size=19)
            lab.next_to(axes.c2p(i, v_min), DOWN, buff=0.22)
            x_labels.add(lab)

        graph = axes.plot_line_graph(
            x_values=list(range(len(points))),
            y_values=values,
            line_color=C["accent"], stroke_width=6,
            add_vertex_dots=True,
            vertex_dot_radius=0.055,
            vertex_dot_style={"fill_color": C["text"],
                              "stroke_color": C["accent"],
                              "stroke_width": 3},
        )
        value_labels = VGroup()
        for i, p in enumerate(points):
            shown = p.get("display") or (
                f"{values[i]:,.0f}" if float(values[i]).is_integer()
                else f"{values[i]:,.1f}")
            lab = _label(str(shown)[:16], size=23, color=C["text"],
                         bold=True)
            lab.next_to(axes.c2p(i, values[i]), UP, buff=0.28)
            value_labels.add(lab)

        source_mob = _source_line(top=True)
        self.play(FadeIn(title, shift=DOWN * 0.3), run_time=t_title)
        if DATA.get("series_label"):
            self.play(Create(axes), FadeIn(y_labels), FadeIn(x_labels),
                      FadeIn(sub), run_time=t_axes)
        else:
            self.play(Create(axes), FadeIn(y_labels), FadeIn(x_labels),
                      run_time=t_axes)
        if source_mob is not None:
            self.add(source_mob)
        self.play(Create(graph["line_graph"]), run_time=t_line)
        self.play(FadeIn(graph["vertex_dots"]), FadeIn(value_labels),
                  run_time=t_marks)
        self.wait(t_hold)
'''

_BAR_CHART_SCENE = _SCENE_COMMON + '''

class CinemoraBarChart(Scene):
    def construct(self):
        self.camera.background_color = C["bg"]
        items = DATA["items"]
        values = [float(i["value"]) for i in items]
        v_max = max([abs(v) for v in values] + [1.0])
        y_top = v_max * 1.18

        t_title = max(0.4, 0.14 * DURATION)
        t_axis = max(0.4, 0.14 * DURATION)
        t_bars = max(0.8, 0.52 * DURATION)
        spent = t_title + t_axis + t_bars
        t_hold = max(0.3, DURATION - spent)

        title = _title((DATA.get("title") or "").upper())
        title.to_edge(UP, buff=0.55)

        chart_w = 12.8
        chart_h = 6.2
        base_y = -2.9
        axis = Line(LEFT * (chart_w / 2), RIGHT * (chart_w / 2),
                    color=C["muted"], stroke_width=3)
        axis.move_to([0, base_y, 0])
        grid = VGroup()
        for i in range(1, 5):
            frac = i / 4.0
            y = base_y + chart_h * frac
            grid.add(Line(LEFT * (chart_w / 2), RIGHT * (chart_w / 2))
                     .move_to([0, y, 0])
                     .set_stroke(C["muted"], width=1, opacity=0.35))
            text = f"{y_top * frac:,.0f}" if float(
                y_top * frac).is_integer() else f"{y_top * frac:,.1f}"
            lab = _label(text, size=19)
            # Inside the frame's left edge: anchored LEFT of the
            # axis these labels ran off-frame and rendered clipped
            # (leading digit cut — visible in rendered evidence).
            lab.next_to([-7.0, y, 0], RIGHT, buff=0.1)
            grid.add(lab)

        n = len(items)
        slot = chart_w / n
        bar_w = min(1.15, slot * 0.58)
        bars = VGroup()
        value_labels = VGroup()
        cat_labels = VGroup()
        for i, item in enumerate(items):
            h = chart_h * (abs(values[i]) / y_top)
            x = -(chart_w / 2) + slot * (i + 0.5)
            bar = Rectangle(width=bar_w, height=max(h, 0.02),
                            fill_color=C["accent"], fill_opacity=0.92,
                            stroke_width=0)
            bar.move_to([x, base_y + max(h, 0.02) / 2.0, 0])
            bars.add(bar)
            shown = item.get("display") or (
                f"{values[i]:,.0f}" if float(values[i]).is_integer()
                else f"{values[i]:,.1f}")
            vlab = _label(str(shown)[:16], size=24, color=C["text"],
                          bold=True)
            vlab.next_to(bar, UP, buff=0.16)
            value_labels.add(vlab)
            clab = _label(str(item.get("label") or "")[:14], size=21)
            clab.next_to([x, base_y, 0], DOWN, buff=0.24)
            cat_labels.add(clab)

        source_mob = _source_line()
        if source_mob is not None:
            # Same clearance rule as the line chart: the category
            # labels own the bottom — lift the chart group so the
            # source line sits clear beneath them.
            VGroup(axis, grid, bars, value_labels,
                   cat_labels).shift(UP * 0.42)
        self.play(FadeIn(title, shift=DOWN * 0.3), run_time=t_title)
        self.play(Create(axis), FadeIn(grid), FadeIn(cat_labels),
                  run_time=t_axis)
        if source_mob is not None:
            self.add(source_mob)
        per_bar = t_bars / max(1, n)
        for bar, vlab in zip(bars, value_labels):
            self.play(GrowFromEdge(bar, DOWN), FadeIn(vlab, shift=UP * 0.2),
                      run_time=per_bar)
        self.wait(t_hold)
'''


# ---------------------------------------------------------------------------
# Render driver
# ---------------------------------------------------------------------------

def _fonts_conf(font_dir, cache_dir):
    return (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
        "<fontconfig>\n"
        "  <dir>/usr/share/fonts</dir>\n"
        f"  <dir>{font_dir}</dir>\n"
        f"  <cachedir>{cache_dir}</cachedir>\n"
        "</fontconfig>\n"
    )


def _conform_format(source, out_path, duration):
    """One ffmpeg pass to the house graphic format: 1920x1080,
    30 fps, libx264 crf 20 / yuv420p (core.render_video's encoder
    settings), exactly `duration` seconds (clone-pad if the scene
    ran short, trim if it ran long). Returns True on success."""
    command = [
        "ffmpeg", "-y", "-v", "error", "-i", str(source),
        "-vf",
        "scale=1920:1080,fps=30,tpad=stop_mode=clone:stop_duration=120",
        "-t", f"{float(duration):.3f}",
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
        "-pix_fmt", "yuv420p", str(out_path),
    ]
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=600,
    )
    return result.returncode == 0 and Path(out_path).is_file()


def _render_scene(scene_source, scene_name, payload, out_path, duration):
    """Render one fixed Manim scene with `payload` as its JSON data.
    Returns (True, "") or (False, reason)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cinemora-manim-") as tmp:
        tmp_dir = Path(tmp)
        scene_file = tmp_dir / "cinemora_scene.py"
        scene_file.write_text(scene_source, encoding="utf-8")
        data_file = tmp_dir / "data.json"
        data_file.write_text(json.dumps(payload), encoding="utf-8")
        fonts_conf = tmp_dir / "fonts.conf"
        fonts_conf.write_text(
            _fonts_conf(_FONT_DIR, tmp_dir / "fontconfig-cache"),
            encoding="utf-8",
        )
        media_dir = tmp_dir / "media"
        env = dict(os.environ)
        env["CINEMORA_MANIM_DATA"] = str(data_file)
        env["FONTCONFIG_FILE"] = str(fonts_conf)
        command = [
            sys.executable, "-m", "manim", "render",
            "-qh", "--fps", "30",
            "--disable_caching",
            "--media_dir", str(media_dir),
            str(scene_file), scene_name,
        ]
        try:
            result = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=RENDER_TIMEOUT_SECONDS, env=env,
                cwd=str(tmp_dir),
            )
        except subprocess.TimeoutExpired:
            print(f"MANIM RENDER TIMEOUT ({scene_name})")
            return False, "manim_render_timeout"
        except Exception as error:
            print(f"MANIM RENDER ERROR ({scene_name}): {error}")
            return False, "manim_render_failed"
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "")[-600:]
            print(f"MANIM RENDER FAILED ({scene_name}): {tail}")
            return False, "manim_render_failed"
        produced = sorted(
            p for p in media_dir.rglob("*.mp4")
            if "partial_movie_files" not in p.parts
        )
        if not produced:
            print(f"MANIM RENDER PRODUCED NO FILE ({scene_name})")
            return False, "manim_render_failed"
        if not _conform_format(produced[0], out_path, duration):
            print(f"MANIM CONFORM FAILED ({scene_name})")
            return False, "manim_render_failed"
    return True, ""


def _payload(data, tokens, duration, extra):
    payload = {
        "duration": float(duration),
        "colors": {
            "bg": _hex(tokens["bg"]),
            "surface": _hex(tokens["surface"]),
            "text": _hex(tokens["text"]),
            "muted": _hex(tokens["muted"]),
            "accent": _hex(tokens["accent"]),
            "accent2": _hex(tokens["accent2"]),
        },
    }
    payload.update(extra)
    return payload


def _finish(template, style_name, duration, sentence_index, scene_number,
            data, out_path):
    meta = _meta(template, style_name, duration, sentence_index,
                 scene_number, data)
    meta["engine"] = "manim"
    meta["manim_version"] = _manim_version()
    return str(out_path), meta


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def manim_line_chart(data, style, out_path, duration=5.0,
                     sentence_index=None, scene_number=None):
    """A recorded series as a Manim animated line chart: axes draw
    in, the line draws on with eased motion, points and value
    labels land as the line reaches them. Same data contract as
    the PIL line_chart; requires >= 3 points (below that the PIL
    chart is the right, cheaper treatment)."""
    style_name, tokens = resolve_style(style)
    data = data or {}
    points = _numeric_items(data.get("points") or data.get("items"))
    if len(points) < _MIN_SERIES_POINTS:
        return None, {"reason": "insufficient_points"}
    if not manim_available():
        return None, {"reason": "manim_unavailable"}
    guard = _recorded_chart_guard(data)
    if guard is not None:
        return guard
    points = points[:_MAX_SERIES_POINTS]
    payload = _payload(data, tokens, duration, {
        "title": _clean(data.get("title")),
        "units": _clean(data.get("units") or data.get("unit")),
        "series_label": _clean(data.get("series_label")),
        "source": _clean(data.get("source")),
        "estimated": _estimated_flag(data),
        "points": points,
    })
    ok, reason = _render_scene(
        _LINE_CHART_SCENE, "CinemoraLineChart", payload, out_path, duration)
    if not ok:
        return None, {"reason": reason}
    return _finish("manim_line_chart", style_name, duration,
                   sentence_index, scene_number, data, out_path)


def manim_bar_chart(data, style, out_path, duration=5.0,
                    sentence_index=None, scene_number=None):
    """A recorded comparison as a Manim animated bar chart: bars
    grow from the axis in sequence with their value labels. Same
    data contract as the PIL bar_chart; requires >= 3 items."""
    style_name, tokens = resolve_style(style)
    data = data or {}
    items = _numeric_items(data.get("items") or data.get("points"))
    if len(items) < _MIN_BAR_ITEMS:
        return None, {"reason": "insufficient_items"}
    if not manim_available():
        return None, {"reason": "manim_unavailable"}
    guard = _recorded_chart_guard(data)
    if guard is not None:
        return guard
    items = items[:_MAX_BAR_ITEMS]
    payload = _payload(data, tokens, duration, {
        "title": _clean(data.get("title")),
        "units": _clean(data.get("units") or data.get("unit")),
        "source": _clean(data.get("source")),
        "estimated": _estimated_flag(data),
        "items": items,
    })
    ok, reason = _render_scene(
        _BAR_CHART_SCENE, "CinemoraBarChart", payload, out_path, duration)
    if not ok:
        return None, {"reason": reason}
    return _finish("manim_bar_chart", style_name, duration,
                   sentence_index, scene_number, data, out_path)


MANIM_TEMPLATES = {
    "manim_line_chart": manim_line_chart,
    "manim_bar_chart": manim_bar_chart,
}

# Self-registration into the v1 registry — the same pattern as
# templates_v2/v3/v4: one lookup point for the graphics chain,
# and recipe regeneration finds these by name identically.
from services.motion_engine import templates as _v1_templates  # noqa: E402

_v1_templates.TEMPLATES.update(MANIM_TEMPLATES)
