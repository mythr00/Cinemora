"""Matplotlib chart templates (GitHub Skills Programme, Lane 1).

Lane 1 classified matplotlib INSTALL: correct animated charts
(real axes, scales and tick maths the hand-rolled PIL charts
approximate) rendered IN-PROCESS — no browser, no subprocess, no
new system dependencies — at seconds per clip. It slots between
the two existing chart engines:

* Manim (richer, ~15-25 s/render, Director-only, refuses <3
  points) — the storytelling treatment;
* matplotlib (this module) — the correct-axes treatment, cheap
  enough for the undirected fallback chain, and the only engine
  that takes a 2-point series;
* PIL (stylised house charts) — the final fall-through.

Same data contract, same template signature, same house output
(1920x1080, 30 fps, libx264 via core.render_video) as every other
template. matplotlib is imported lazily inside the render —
importing this module only registers the templates. The figure
renders with matplotlib's bundled DejaVu Sans, the same family
the PIL compositor ships.
"""

from __future__ import annotations

import importlib.util

from services.motion_engine import core, motion_registry, styles
from services.motion_engine.templates import TEMPLATES, _clean, _meta
from services.motion_engine.templates_v3 import _numeric_items

_MAX_POINTS = 60
_MAX_BARS = 12


def mpl_available() -> bool:
    return importlib.util.find_spec("matplotlib") is not None


def _recorded_chart_guard(data, units):
    """The recorded-data chart contract (Phase 5 B5/B7), applied
    identically in every chart engine (Animation Studio Expansion,
    mission §9 — chart source-line consistency). A series the
    pipeline assembled from recorded data (marked ``recorded``)
    MUST carry recorded units and a recorded source: either
    missing refuses with the PIL family's typed reasons, never a
    unit-less or source-less rendering. Returns the refusal tuple
    or None."""
    if not data.get("recorded"):
        return None
    if not units:
        return None, {"reason": "chart_units_missing",
                      "detail": "recorded series carries no units"}
    if not _clean(data.get("source")):
        return None, {"reason": "chart_source_missing",
                      "detail": "recorded series carries no source"}
    return None


def _source_line(data):
    """The small source line text, or "" — the recorded source of
    the data, drawn as data attribution (never a file name or an
    asset/provider label); estimates are labelled estimated. Same
    wording and 72-char cap as the PIL chart family."""
    source = _clean(data.get("source"))
    if not source:
        return ""
    line = f"Source — {source}"
    uncertainty = data.get("uncertainty")
    estimated = bool(
        data.get("estimated")
        or (isinstance(uncertainty, dict) and uncertainty.get("estimated"))
    )
    if estimated:
        line += " · estimated"
    return line[:72]


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(c) for c in rgb[:3])


def _new_figure(tokens):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.family"] = "DejaVu Sans"
    fig = plt.figure(figsize=(19.2, 10.8), dpi=100)
    fig.patch.set_facecolor(_hex(tokens["bg"]))
    return plt, fig


def _style_axes(ax, tokens, title, series_label):
    ax.set_facecolor(_hex(tokens["bg"]))
    for spine in ax.spines.values():
        spine.set_color(_hex(tokens["muted"]))
    ax.tick_params(colors=_hex(tokens["muted"]), labelsize=17)
    ax.yaxis.label.set_color(_hex(tokens["muted"]))
    ax.xaxis.label.set_color(_hex(tokens["muted"]))
    if title:
        ax.set_title(title.upper()[:60], color=_hex(tokens["text"]),
                     fontsize=25, fontweight="bold", pad=26)
    if series_label:
        ax.set_ylabel(series_label[:40], fontsize=17)


def _frame_image(fig):
    import numpy as np
    from PIL import Image

    fig.canvas.draw()
    buffer = np.asarray(fig.canvas.buffer_rgba())[:, :, :3]
    return Image.fromarray(buffer.copy(), "RGB")


def mpl_line_chart(data, style, out_path, duration=5.0,
                   sentence_index=None, scene_number=None):
    """Animated line chart with real axes (matplotlib engine)."""
    style_name, tokens = styles.resolve_style(style)
    data = data or {}
    points = _numeric_items(data.get("points") or data.get("items"))
    if len(points) < 2:
        return None, {"reason": "insufficient_points"}
    if not mpl_available():
        return None, {"reason": "matplotlib_unavailable"}
    points = points[:_MAX_POINTS]
    title = _clean(data.get("title"))
    units = _clean(data.get("units")) or _clean(data.get("unit"))
    guard = _recorded_chart_guard(data, units)
    if guard is not None:
        return guard
    series_label = _clean(data.get("series_label")) or units
    labels = [p["label"] for p in points]
    values = [p["value"] for p in points]
    ease = motion_registry.easing_function("ease_out")

    plt, fig = _new_figure(tokens)
    source_line = _source_line(data)
    if source_line:
        fig.text(0.09, 0.035, source_line, color=_hex(tokens["muted"]),
                 fontsize=14)
    ax = fig.add_axes([0.09, 0.14, 0.87, 0.72])
    v_min, v_max = min(0.0, min(values)), max(values)
    pad = (v_max - v_min) * 0.12 or 1.0
    y_limits = (v_min - pad * 0.4, v_max + pad)
    tick_step = max(1, len(labels) // 10)
    tick_idx = list(range(0, len(labels), tick_step))

    def _setup():
        ax.clear()
        _style_axes(ax, tokens, title, series_label)
        ax.set_xlim(-0.4, len(values) - 0.6)
        ax.set_ylim(*y_limits)
        ax.grid(axis="y", color=_hex(tokens["muted"]), alpha=0.35,
                linewidth=0.8)
        ax.set_xticks(tick_idx)
        ax.set_xticklabels([labels[i][:14] for i in tick_idx],
                           rotation=0, fontsize=14)

    def draw_frame(t, progress):
        _setup()
        span = ease(min(1.0, progress * 1.15)) * (len(values) - 1)
        whole = int(span)
        xs = list(range(whole + 1))
        ys = values[: whole + 1]
        if whole < len(values) - 1:
            frac = span - whole
            xs.append(whole + frac)
            ys.append(values[whole] + (values[whole + 1] - values[whole]) * frac)
        ax.plot(xs, ys, color=_hex(tokens["accent"]), linewidth=4.5,
                solid_capstyle="round", zorder=3)
        ax.scatter(range(whole + 1), values[: whole + 1],
                   color=_hex(tokens["accent2"]), s=52, zorder=4)
        return _frame_image(fig)

    try:
        rendered = core.render_video(draw_frame, out_path, duration=duration)
    finally:
        plt.close(fig)
    meta = _meta("mpl_line_chart", style_name, duration,
                 sentence_index, scene_number, data)
    meta["engine"] = "matplotlib"
    return rendered, meta


def mpl_bar_chart(data, style, out_path, duration=5.0,
                  sentence_index=None, scene_number=None):
    """Animated bar chart with real axes (matplotlib engine)."""
    style_name, tokens = styles.resolve_style(style)
    data = data or {}
    items = _numeric_items(data.get("items") or data.get("points"))
    if len(items) < 2:
        return None, {"reason": "insufficient_items"}
    if not mpl_available():
        return None, {"reason": "matplotlib_unavailable"}
    items = items[:_MAX_BARS]
    title = _clean(data.get("title"))
    units = _clean(data.get("units")) or _clean(data.get("unit"))
    guard = _recorded_chart_guard(data, units)
    if guard is not None:
        return guard
    labels = [p["label"] for p in items]
    values = [p["value"] for p in items]
    displays = [p["display"] for p in items]
    ease = motion_registry.easing_function("ease_out")

    plt, fig = _new_figure(tokens)
    source_line = _source_line(data)
    if source_line:
        fig.text(0.09, 0.035, source_line, color=_hex(tokens["muted"]),
                 fontsize=14)
    ax = fig.add_axes([0.09, 0.14, 0.87, 0.72])
    y_top = max(values) * 1.18 or 1.0

    def _setup():
        ax.clear()
        _style_axes(ax, tokens, title, units)
        ax.set_ylim(0, y_top)
        ax.grid(axis="y", color=_hex(tokens["muted"]), alpha=0.35,
                linewidth=0.8)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels([label[:16] for label in labels], fontsize=15)

    def draw_frame(t, progress):
        _setup()
        growth = ease(min(1.0, progress * 1.2))
        colours = [_hex(tokens["accent"])] + [_hex(tokens["accent2"])] * (len(values) - 1)
        ax.bar(range(len(values)), [v * growth for v in values],
               color=colours, width=0.58, zorder=3)
        if progress > 0.85:
            for i, (value, display) in enumerate(zip(values, displays)):
                ax.text(i, value * growth + max(values) * 0.03, display,
                        ha="center", color=_hex(tokens["text"]),
                        fontsize=17, fontweight="bold")
        return _frame_image(fig)

    try:
        rendered = core.render_video(draw_frame, out_path, duration=duration)
    finally:
        plt.close(fig)
    meta = _meta("mpl_bar_chart", style_name, duration,
                 sentence_index, scene_number, data)
    meta["engine"] = "matplotlib"
    return rendered, meta


TEMPLATES.update({
    "mpl_line_chart": mpl_line_chart,
    "mpl_bar_chart": mpl_bar_chart,
})
