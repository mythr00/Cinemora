"""Motion Engine templates v4 (Transformation Phase 2.5, Wave 2).

The Wave 2 breadth families, built on the same contract as
v1/v2/v3: every template renders ONLY caller-supplied (recorded)
data and returns (None, {"reason": ...}) when its data
requirements are not met. Signature and metadata shape are
identical to services.motion_engine.templates (v1).

Families:
  F1 charts      — bubble_chart, population_pyramid,
                   animated_dashboard (multi-stat panel).
  F2 sequences   — checklist_graphic, milestone_waveform
                   (engine presentations of the process/timeline
                   concepts; the native families are untouched).
  F3 maps        — territory_expansion, satellite_zoom, heatmap.
  F4 evidence    — newspaper_wall, crime_scene_diagram
                   (classified_file lives with its base template
                   in v1, as the Wave 1 map variants do).
  F5 technical   — construction_sequence, molecular_diagram.
  F7/F8 overlays — hud_overlay, holographic_panel, before_after,
                   layered_reveal, object_labels.
"""

import math
import re

from PIL import Image, ImageDraw, ImageFilter

from services.motion_engine import core, motion_registry
from services.motion_engine.core import (
    HEIGHT, WIDTH, clamp01, fit_contain, fit_cover,
    load_font, progressive_polyline, wrap_text,
)
from services.motion_engine.styles import resolve_style

ease_in_out = motion_registry.easing_function("ease_in_out")
ease_out = motion_registry.easing_function("ease_out")
from services.motion_engine.templates import (
    _aged_paper, _clean, _dark_photo, _footer, _meta,
    _textured_dark, _torn_banner,
)
from services.motion_engine.templates_v2 import _nice_max
from services.motion_engine.templates_v3 import (
    _blend, _load_image, _numeric_items, _probe_draw, _series_palette,
)


def _fmt(value):
    return (
        f"{value:,.0f}" if float(value).is_integer()
        else f"{value:,.1f}"
    )


# ---------------------------------------------------------------------------
# F1. Bubble chart — value on an axis, magnitude as bubble area
# ---------------------------------------------------------------------------

def bubble_chart(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    items = []
    size_source = "recorded"
    for entry in data.get("items") or []:
        if not isinstance(entry, dict):
            continue
        try:
            value = float(entry.get("value"))
        except (TypeError, ValueError):
            continue
        size = entry.get("size")
        try:
            size = float(size) if size is not None else None
        except (TypeError, ValueError):
            size = None
        items.append({
            "label": _clean(entry.get("label")) or "ITEM",
            "value": value,
            "display": _clean(entry.get("display")),
            "size": size,
        })
    items = [i for i in items if i["value"] > 0]
    if len(items) < 2:
        return None, {"reason": "insufficient_items"}
    if all(i["size"] is None for i in items):
        # No second recorded dimension: bubble area encodes the
        # same recorded value as position (a legitimate single-
        # measure bubble chart), and the record says so.
        size_source = "value"
        for item in items:
            item["size"] = item["value"]
    else:
        for item in items:
            if item["size"] is None or item["size"] <= 0:
                item["size"] = item["value"]
    items = items[:6]
    title = _clean(data.get("title"))
    units = _clean(data.get("units"))
    colours = _series_palette(tokens, len(items))
    axis_max = _nice_max(max(i["value"] for i in items))
    max_size = max(i["size"] for i in items)
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=83)
    fonts = {
        "title": load_font(tokens["font_bold"], 44),
        "label": load_font(tokens["font"], 26),
        "value": load_font(tokens["font_bold"], 27),
        "tick": load_font(tokens["font"], 22),
    }
    x0, x1 = 200, WIDTH - 170
    top, lane_h = 265, 108
    axis_y = top + lane_h * len(items) + 30

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("chart")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, 84), title.upper()[:60],
                   font=fonts["title"], fill=tokens["text"] + (255,))
        for tick_i in range(6):
            frac = tick_i / 5
            gx = x0 + (x1 - x0) * frac
            d.line([gx, top - 18, gx, axis_y], fill=(255, 255, 255, 24), width=1)
            tick_text = _fmt(axis_max * frac)
            tw = d.textlength(tick_text, font=fonts["tick"])
            d.text((gx - tw / 2, axis_y + 12), tick_text,
                   font=fonts["tick"], fill=tokens["muted"] + (255,))
        d.line([x0, axis_y, x1, axis_y], fill=(255, 255, 255, 150), width=2)
        if units:
            uw = d.textlength(units.upper(), font=fonts["tick"])
            d.text(((x0 + x1) / 2 - uw / 2, axis_y + 48), units.upper()[:48],
                   font=fonts["tick"], fill=tokens["muted"] + (255,))
        for i, item in enumerate(items):
            local = ease_out(clamp01(progress * 1.9 - i * 0.28))
            if local <= 0:
                continue
            cy = top + i * lane_h + lane_h // 2
            cx = x0 + (x1 - x0) * (item["value"] / axis_max)
            radius = (24 + 52 * math.sqrt(item["size"] / max_size)) * local
            d.line([x0, cy, x1, cy], fill=(255, 255, 255, 30), width=1)
            d.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                      fill=colours[i] + (120,), outline=colours[i] + (255,),
                      width=3)
            shown = item["display"] or _fmt(item["value"])
            vw = d.textlength(shown, font=fonts["value"])
            d.text((cx - vw / 2, cy - 15), shown, font=fonts["value"],
                   fill=(255, 255, 255, 255))
            label = item["label"][:24]
            lw = d.textlength(label, font=fonts["label"])
            ly = cy + radius + 8
            if ly > axis_y - 30:
                ly = cy - radius - 36
            d.text((cx - lw / 2, ly), label, font=fonts["label"],
                   fill=tokens["text"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("bubble_chart", style_name, duration, sentence_index,
                       scene_number,
                       {"items": len(items), "title": bool(title),
                        "size_source": size_source})


# ---------------------------------------------------------------------------
# F1. Population pyramid — mirrored bars for two recorded groups
# ---------------------------------------------------------------------------

def population_pyramid(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    groups = []
    for entry in data.get("groups") or data.get("items") or []:
        if not isinstance(entry, dict):
            continue
        try:
            left = float(entry.get("left"))
            right = float(entry.get("right"))
        except (TypeError, ValueError):
            continue
        groups.append({
            "label": _clean(entry.get("label")) or "GROUP",
            "left": left, "right": right,
        })
    if not groups:
        return None, {"reason": "no_paired_items"}
    if len(groups) < 2:
        return None, {"reason": "insufficient_groups"}
    groups = groups[:9]
    left_label = _clean(data.get("left_label"))
    right_label = _clean(data.get("right_label"))
    title = _clean(data.get("title"))
    axis_max = _nice_max(max(
        max(g["left"] for g in groups), max(g["right"] for g in groups)))
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=89)
    fonts = {
        "title": load_font(tokens["font_bold"], 44),
        "label": load_font(tokens["font_bold"], 25),
        "value": load_font(tokens["font"], 24),
        "legend": load_font(tokens["font_bold"], 26),
    }
    cx = WIDTH // 2
    half = 620
    top = 250
    row_h = min(76, (HEIGHT - top - 150) // len(groups))

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("pyramid")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, 84), title.upper()[:60],
                   font=fonts["title"], fill=tokens["text"] + (255,))
        if left_label or right_label:
            if left_label:
                lw = d.textlength(left_label.upper(), font=fonts["legend"])
                d.text((cx - 90 - lw, 176), left_label.upper()[:30],
                       font=fonts["legend"], fill=tokens["accent2"] + (255,))
            if right_label:
                d.text((cx + 90, 176), right_label.upper()[:30],
                       font=fonts["legend"], fill=tokens["accent"] + (255,))
        d.line([cx, top - 14, cx, top + row_h * len(groups) + 8],
               fill=(255, 255, 255, 170), width=2)
        for i, group in enumerate(groups):
            local = ease_out(clamp01(progress * 1.8 - i * 0.18))
            if local <= 0:
                continue
            y = top + i * row_h
            bar_h = row_h - 18
            lw_ = int(half * (group["left"] / axis_max) * local)
            rw_ = int(half * (group["right"] / axis_max) * local)
            d.rectangle([cx - lw_, y, cx, y + bar_h],
                        fill=tokens["accent2"] + (235,))
            d.rectangle([cx, y, cx + rw_, y + bar_h],
                        fill=tokens["accent"] + (235,))
            label = group["label"][:14]
            tw = d.textlength(label, font=fonts["label"])
            d.text((cx - tw / 2, y + 2), label, font=fonts["label"],
                   fill=tokens["text"] + (255,))
            if local >= 0.97:
                lv = _fmt(group["left"])
                vw = d.textlength(lv, font=fonts["value"])
                d.text((cx - lw_ - vw - 12, y + 4), lv,
                       font=fonts["value"], fill=tokens["text"] + (255,))
                rv = _fmt(group["right"])
                d.text((cx + rw_ + 12, y + 4), rv,
                       font=fonts["value"], fill=tokens["text"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("population_pyramid", style_name, duration,
                       sentence_index, scene_number,
                       {"groups": len(groups), "title": bool(title),
                        "axis_max": axis_max})


# ---------------------------------------------------------------------------
# F1. Animated dashboard — a multi-stat panel from recorded facts
# ---------------------------------------------------------------------------

def animated_dashboard(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    stats = []
    for entry in data.get("stats") or data.get("items") or []:
        if not isinstance(entry, dict):
            continue
        try:
            value = float(entry.get("value"))
        except (TypeError, ValueError):
            continue
        stats.append({
            "label": _clean(entry.get("label")) or "STAT",
            "value": value,
            "display": _clean(entry.get("display")),
        })
    if len(stats) < 3:
        return None, {"reason": "insufficient_stats"}
    stats = stats[:6]
    series = _numeric_items(data.get("series") or data.get("points"))
    series = series if len(series) >= 2 else []
    title = _clean(data.get("title"))
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=97)
    fonts = {
        "title": load_font(tokens["font_bold"], 46),
        "label": load_font(tokens["font_bold"], 23),
        "value": load_font(tokens["font_bold"], 54),
    }
    cols = 3
    rows = (len(stats) + cols - 1) // cols
    card_w, card_h, gap = 520, 240, 36
    grid_w = cols * card_w + (cols - 1) * gap
    grid_x = (WIDTH - grid_w) // 2
    grid_y = 240
    colours = _series_palette(tokens, len(stats))

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("panel")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, 88), title.upper()[:58],
                   font=fonts["title"], fill=tokens["text"] + (255,))
        for i, stat in enumerate(stats):
            local = ease_out(clamp01(progress * 2.0 - i * 0.22))
            if local <= 0:
                continue
            cx = grid_x + (i % cols) * (card_w + gap)
            cy = grid_y + (i // cols) * (card_h + gap)
            cy = int(cy + (1 - local) * 34)
            alpha = int(255 * local)
            d.rectangle([cx, cy, cx + card_w, cy + card_h],
                        fill=tokens["surface"] + (alpha,))
            d.rectangle([cx, cy, cx + card_w, cy + 8],
                        fill=colours[i] + (alpha,))
            d.text((cx + 30, cy + 30), stat["label"].upper()[:26],
                   font=fonts["label"], fill=tokens["muted"] + (alpha,))
            shown_value = stat["value"] * local
            shown = stat["display"] if local >= 0.97 and stat["display"] \
                else _fmt(shown_value)
            d.text((cx + 30, cy + 78), shown[:20], font=fonts["value"],
                   fill=tokens["text"] + (alpha,))
        if series:
            # A recorded series rides under the cards as a thin
            # sparkline — same data contract as line_chart.
            sx0, sx1 = grid_x, grid_x + grid_w
            sy1 = HEIGHT - 120
            sy0 = sy1 - 90
            values = [p["value"] for p in series]
            v_max = max(values) if max(values) > min(values) else min(values) + 1
            v_min = min(0.0, min(values))
            coords = [
                (sx0 + (sx1 - sx0) * (j / (len(values) - 1)),
                 sy1 - (sy1 - sy0) * ((v - v_min) / (v_max - v_min)))
                for j, v in enumerate(values)
            ]
            frac = ease_in_out(clamp01(progress * 1.3))
            if frac > 0:
                progressive_polyline(
                    d, coords, frac, fill=tokens["accent2"] + (220,),
                    width=4)
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("animated_dashboard", style_name, duration,
                       sentence_index, scene_number,
                       {"stats": len(stats), "title": bool(title),
                        "series_points": len(series)})


# ---------------------------------------------------------------------------
# F2. Checklist graphic — recorded items with recorded check states
# ---------------------------------------------------------------------------

def checklist_graphic(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    items = []
    for entry in data.get("items") or []:
        if not isinstance(entry, dict):
            continue
        label = _clean(entry.get("label") or entry.get("text"))
        if not label:
            continue
        state = _clean(entry.get("state")).lower()
        if state not in ("done", "active", "pending"):
            state = "done" if entry.get("done") is True else "pending"
        items.append({"label": label, "state": state})
    if not items:
        return None, {"reason": "no_items"}
    items = items[:7]
    title = _clean(data.get("title"))
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=101)
    fonts = {
        "title": load_font(tokens["font_bold"], 44),
        "item": load_font(tokens["font"], 34),
        "count": load_font(tokens["font_bold"], 26),
    }
    probe = _probe_draw()
    x0 = 330
    top = 250
    row_h = min(96, (HEIGHT - top - 130) // len(items))
    done_count = sum(1 for i in items if i["state"] == "done")

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("list")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if title:
            d.text((x0, 110), title.upper()[:56], font=fonts["title"],
                   fill=tokens["text"] + (255,))
        count_text = f"{done_count} OF {len(items)} COMPLETE"
        cw = d.textlength(count_text, font=fonts["count"])
        d.text((WIDTH - 130 - cw, 122), count_text, font=fonts["count"],
               fill=tokens["accent2"] + (255,))
        for i, item in enumerate(items):
            local = ease_out(clamp01(progress * 2.2 - i * 0.3))
            if local <= 0:
                continue
            y = top + i * row_h
            box = [x0, y, x0 + 46, y + 46]
            if item["state"] == "done":
                d.rectangle(box, fill=tokens["accent"] + (255,))
                check = clamp01((local - 0.55) * 2.4)
                if check > 0:
                    pts = [(x0 + 10, y + 24), (x0 + 20, y + 35),
                           (x0 + 37, y + 12)]
                    progressive_polyline(
                        d, pts, check, fill=(255, 255, 255, 255), width=6)
                fill = tokens["text"]
            elif item["state"] == "active":
                pulse = 3 + int(2 * (1 + math.sin(t * 4.0)) / 2)
                d.rectangle(box, outline=tokens["accent"] + (255,),
                            width=pulse + 2)
                fill = tokens["text"]
            else:
                d.rectangle(box, outline=tokens["muted"] + (200,), width=3)
                fill = tokens["muted"]
            label = item["label"]
            while label and probe.textlength(
                    label, font=fonts["item"]) > WIDTH - x0 - 260:
                label = label[:-1]
            d.text((x0 + 74, y + 4), label, font=fonts["item"],
                   fill=fill + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("checklist_graphic", style_name, duration,
                       sentence_index, scene_number,
                       {"items": len(items), "done": done_count,
                        "title": bool(title)})


# ---------------------------------------------------------------------------
# F2. Milestone waveform — recorded events on an amplitude track
# ---------------------------------------------------------------------------

def milestone_waveform(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    events = []
    amplitude_source = "recorded"
    for entry in data.get("events") or []:
        if not isinstance(entry, dict):
            continue
        what = _clean(entry.get("what") or entry.get("label"))
        if not what:
            continue
        amplitude = entry.get("amplitude", entry.get("weight"))
        try:
            amplitude = float(amplitude) if amplitude is not None else None
        except (TypeError, ValueError):
            amplitude = None
        events.append({
            "when": _clean(entry.get("when") or entry.get("date")),
            "what": what,
            "amplitude": amplitude,
        })
    if len(events) < 3:
        return None, {"reason": "insufficient_events"}
    events = events[:7]
    if any(e["amplitude"] is None for e in events):
        # No recorded magnitude for at least one event: stem
        # heights become presentational (an ordinal pattern), and
        # the record says so — the waveform never invents a
        # magnitude and stamps it as data.
        amplitude_source = "ordinal"
        pattern = [0.55, 0.9, 0.4, 1.0, 0.5, 0.8, 0.65]
        for i, event in enumerate(events):
            if event["amplitude"] is None:
                event["amplitude"] = pattern[i % len(pattern)]
    peak = max(e["amplitude"] for e in events) or 1.0
    for event in events:
        event["amplitude"] = event["amplitude"] / peak
    title = _clean(data.get("title"))
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=103)
    fonts = {
        "title": load_font(tokens["font_bold"], 44),
        "when": load_font(tokens["font_bold"], 25),
        "what": load_font(tokens["font"], 24),
    }
    probe = _probe_draw()
    x0, x1 = 170, WIDTH - 170
    axis_y = 640
    max_h = 300

    def _points(frac):
        pts = [(x0 - 60, axis_y)]
        for i, event in enumerate(events):
            ex = x0 + (x1 - x0) * (i / (len(events) - 1))
            pts.append((ex, axis_y - event["amplitude"] * max_h))
        pts.append((x1 + 60, axis_y))
        return pts

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("wave")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, 84), title.upper()[:58],
                   font=fonts["title"], fill=tokens["text"] + (255,))
        d.line([x0 - 60, axis_y, x1 + 60, axis_y],
               fill=(255, 255, 255, 160), width=3)
        frac = ease_in_out(clamp01(progress * 1.25))
        if frac > 0:
            progressive_polyline(
                d, _points(frac), frac, fill=tokens["accent"] + (255,),
                width=5)
        for i, event in enumerate(events):
            node_frac = i / (len(events) - 1)
            if frac < node_frac:
                continue
            ex = x0 + (x1 - x0) * node_frac
            ey = axis_y - event["amplitude"] * max_h
            d.line([ex, axis_y, ex, ey], fill=tokens["accent2"] + (190,),
                   width=3)
            d.ellipse([ex - 10, ey - 10, ex + 10, ey + 10],
                      fill=(255, 255, 255, 255),
                      outline=tokens["accent"] + (255,), width=3)
            if event["when"]:
                ww = d.textlength(event["when"].upper(), font=fonts["when"])
                d.text((ex - ww / 2, ey - 52), event["when"].upper()[:22],
                       font=fonts["when"], fill=tokens["text"] + (255,))
            label = event["what"]
            while label and probe.textlength(
                    label, font=fonts["what"]) > 250:
                label = label[:-1]
            lw = d.textlength(label, font=fonts["what"])
            row_y = axis_y + 34 if i % 2 == 0 else axis_y + 96
            d.text((ex - lw / 2, row_y), label, font=fonts["what"],
                   fill=tokens["muted"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("milestone_waveform", style_name, duration,
                       sentence_index, scene_number,
                       {"events": len(events), "title": bool(title),
                        "amplitude_source": amplitude_source})


# ---------------------------------------------------------------------------
# F3. Territory expansion — recorded region states, grown in order
# ---------------------------------------------------------------------------

def _state_polygons(states):
    """Resolve every state's regions to polygon sets under ONE
    shared projection (dataset countries) or the caller's own
    projected points. Returns (per-state polygon lists, sources)
    or (None, reason)."""
    from services.motion_engine import geography

    names = []
    for state in states:
        for region in state["regions"]:
            if len(region.get("points") or []) < 3:
                cname = region.get("country") or region.get("name")
                if cname:
                    names.append(cname)
    projection = None
    if names:
        try:
            projection = geography.fit_projection(names)
        except Exception:
            projection = None
    resolved = []
    for state in states:
        polygons = []
        for region in state["regions"]:
            points = region.get("points") or []
            if len(points) >= 3:
                polygons.append([points])
                continue
            cname = region.get("country") or region.get("name")
            if cname and projection is not None:
                try:
                    found = geography.outline_for(cname, projection)
                except Exception:
                    found = []
                polygons.extend(found)
        if not polygons:
            return None, "no_polygon_data"
        resolved.append(polygons)
    return resolved, None


def territory_expansion(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    states = []
    for entry in data.get("states") or []:
        if not isinstance(entry, dict):
            continue
        regions = [r for r in (entry.get("regions") or [])
                   if isinstance(r, dict)]
        if regions:
            states.append({
                "label": _clean(entry.get("label") or entry.get("date")),
                "regions": regions,
            })
    if len(states) < 2:
        return None, {"reason": "insufficient_states"}
    states = states[:5]
    resolved, reason = _state_polygons(states)
    if resolved is None:
        return None, {"reason": reason}
    title = _clean(data.get("title"))
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=107)
    fonts = {
        "title": load_font(tokens["font_bold"], 44),
        "state": load_font(tokens["font_bold"], 30),
    }

    def px(points):
        return [(p[0] * WIDTH, p[1] * HEIGHT) for p in points]

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("map")
        d = ImageDraw.Draw(layer.image, "RGBA")
        # Each state owns an equal slice of the timeline; the
        # newest state sweeps in while earlier states stay as the
        # paler ground the territory grew from.
        span = 1.0 / len(states)
        for si, polygons in enumerate(resolved):
            local = clamp01((progress - si * span) / span)
            if local <= 0:
                continue
            colour = tokens["accent"] if si == len(states) - 1 \
                else tokens["accent2"]
            alpha = 200 if si == len(states) - 1 else 110
            for poly in polygons:
                for ring in poly:
                    pts = px(ring)
                    if si == len(states) - 1 or si > 0:
                        sweep = ease_in_out(local)
                        xs = [p[0] for p in pts]
                        if max(xs) > min(xs):
                            reveal_x = min(xs) + (max(xs) - min(xs)) * sweep
                            clip = Image.new("L", (WIDTH, HEIGHT), 0)
                            ImageDraw.Draw(clip).rectangle(
                                [0, 0, reveal_x, HEIGHT], fill=255)
                            fill_img = Image.new(
                                "RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
                            ImageDraw.Draw(fill_img).polygon(
                                pts, fill=colour + (alpha,))
                            layer.image.paste(
                                fill_img, (0, 0),
                                Image.composite(
                                    fill_img,
                                    Image.new("RGBA", (WIDTH, HEIGHT),
                                              (0, 0, 0, 0)),
                                    clip))
                        else:
                            d.polygon(pts, fill=colour + (alpha,))
                    else:
                        d.polygon(pts, fill=colour + (alpha,))
                    closed = pts + [pts[0]]
                    d.line(closed, fill=(255, 255, 255, 200), width=2,
                           joint="curve")
        # State labels along the bottom, lit as their state lands.
        lx = 120
        for si, state in enumerate(states):
            if not state["label"]:
                continue
            local = clamp01((progress - si * span) / span)
            fill = tokens["text"] if local >= 1 else tokens["muted"]
            if local > 0:
                d.text((lx, HEIGHT - 118), state["label"].upper()[:24],
                       font=fonts["state"], fill=fill + (255,))
                lx += int(d.textlength(
                    state["label"].upper()[:24], font=fonts["state"])) + 60
        if title:
            d.text((90, 64), title.upper()[:56], font=fonts["title"],
                   fill=tokens["text"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("territory_expansion", style_name, duration,
                       sentence_index, scene_number,
                       {"states": len(states), "title": bool(title),
                        "state_labels": [s["label"] for s in states]})


# ---------------------------------------------------------------------------
# F3. Satellite zoom — a 2.5D scale move over a recorded place frame
# ---------------------------------------------------------------------------

def satellite_zoom(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    image = _load_image(data.get("image") or data.get("photo"))
    if image is None:
        return None, {"reason": "no_image"}
    focus = data.get("focus") or [0.5, 0.5]
    try:
        fx = min(0.95, max(0.05, float(focus[0])))
        fy = min(0.95, max(0.05, float(focus[1])))
    except (TypeError, ValueError, IndexError):
        fx, fy = 0.5, 0.5
    focus_source = _clean(data.get("focus_source")) or (
        "recorded" if data.get("focus") else "frame_centre")
    try:
        zoom = float(data.get("zoom") or 1.7)
    except (TypeError, ValueError):
        zoom = 1.7
    zoom = min(2.6, max(1.2, zoom))
    label = _clean(data.get("label") or data.get("title"))
    cover = fit_cover(image, (WIDTH, HEIGHT))
    fonts = {"label": load_font(tokens["font_bold"], 38)}

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("image")
        scale = 1.0 + (zoom - 1.0) * ease_in_out(clamp01(progress * 1.1))
        win_w = WIDTH / scale
        win_h = HEIGHT / scale
        cx = min(max(fx * WIDTH, win_w / 2), WIDTH - win_w / 2)
        cy = min(max(fy * HEIGHT, win_h / 2), HEIGHT - win_h / 2)
        frame = cover.crop((
            int(cx - win_w / 2), int(cy - win_h / 2),
            int(cx + win_w / 2), int(cy + win_h / 2),
        )).resize((WIDTH, HEIGHT), Image.LANCZOS)
        base.image = frame.convert("RGBA")
        layer = canvas.new_layer("annotation")
        d = ImageDraw.Draw(layer.image, "RGBA")
        # The focus ring lands once the zoom has mostly arrived.
        ring = ease_out(clamp01((progress - 0.55) * 2.4))
        if ring > 0:
            r = 64 * ring + 10
            px_, py_ = fx * WIDTH, fy * HEIGHT
            # After the crop, the focus sits at the frame centre
            # of the window; when the window clamped at an edge
            # the focus shifts — recompute its screen position.
            sx = (fx * WIDTH - (cx - win_w / 2)) * scale
            sy = (fy * HEIGHT - (cy - win_h / 2)) * scale
            d.ellipse([sx - r, sy - r, sx + r, sy + r],
                      outline=(255, 255, 255, 235), width=4)
            d.ellipse([sx - 7, sy - 7, sx + 7, sy + 7],
                      fill=tokens["accent"] + (255,))
            if label:
                tw = d.textlength(label.upper(), font=fonts["label"])
                bx = min(max(sx - tw / 2, 30), WIDTH - tw - 60)
                by = min(sy + r + 26, HEIGHT - 120)
                d.rectangle([bx - 14, by - 8, bx + tw + 14, by + 54],
                            fill=(0, 0, 0, 140))
                d.text((bx, by), label.upper()[:44], font=fonts["label"],
                       fill=(255, 255, 255, 255))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("satellite_zoom", style_name, duration,
                       sentence_index, scene_number,
                       {"image": True, "focus": [fx, fy],
                        "focus_source": focus_source, "zoom": zoom,
                        "label": bool(label)})


# ---------------------------------------------------------------------------
# F3. Heatmap — a recorded density field over the map projection
# ---------------------------------------------------------------------------

def heatmap(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    points = []
    for entry in data.get("points") or []:
        if not isinstance(entry, dict):
            continue
        at = entry.get("at")
        if not (isinstance(at, (list, tuple)) and len(at) >= 2):
            continue
        try:
            x, y = float(at[0]), float(at[1])
            weight = float(entry.get("weight") or 1.0)
        except (TypeError, ValueError):
            continue
        points.append({"at": (x, y), "weight": max(0.05, weight),
                       "label": _clean(entry.get("label"))})
    if len(points) < 3:
        return None, {"reason": "insufficient_points"}
    points = points[:60]
    title = _clean(data.get("title"))
    weight_label = _clean(data.get("weight_label"))
    # Optional recorded region outlines for geographic context —
    # the same dataset contract as territory_map.
    outlines = []
    region_names = [
        r.get("country") or r.get("name")
        for r in (data.get("regions") or [])
        if isinstance(r, dict) and (r.get("country") or r.get("name"))
    ]
    if region_names:
        try:
            from services.motion_engine import geography

            projection = geography.fit_projection(region_names)
            for name in region_names:
                outlines.extend(geography.outline_for(name, projection))
        except Exception:
            outlines = []
    # Density field on a small buffer, colourised by intensity.
    # Gaussian splats accumulate in a float array (PIL's "F" mode
    # cannot be blurred on this stack); each recorded point adds
    # its weight as a smooth kernel — overlapping points genuinely
    # accumulate density.
    bw, bh = 240, 135
    max_w = max(p["weight"] for p in points)
    try:
        import numpy as np

        acc = np.zeros((bh, bw), dtype="float32")
        for point in points:
            bx, by = point["at"][0] * bw, point["at"][1] * bh
            sigma = 6.0 + 8.0 * (point["weight"] / max_w)
            span = int(sigma * 3)
            x0, x1 = max(0, int(bx) - span), min(bw, int(bx) + span + 1)
            y0, y1 = max(0, int(by) - span), min(bh, int(by) + span + 1)
            yy, xx = np.mgrid[y0:y1, x0:x1]
            acc[y0:y1, x0:x1] += (point["weight"] / max_w) * np.exp(
                -(((xx - bx) ** 2) + ((yy - by) ** 2))
                / (2.0 * sigma * sigma))
        field_values = [float(v) for v in acc.flatten()]
    except Exception:
        field_img = Image.new("L", (bw, bh), 0)
        fd = ImageDraw.Draw(field_img)
        for point in points:
            bx, by = point["at"][0] * bw, point["at"][1] * bh
            radius = 13 + 17 * (point["weight"] / max_w)
            fd.ellipse(
                [bx - radius, by - radius, bx + radius, by + radius],
                fill=int(255 * point["weight"] / max_w))
        field_img = field_img.filter(ImageFilter.GaussianBlur(7))
        field_values = [v / 255.0 for v in field_img.getdata()]
    peak = max(field_values) or 1.0
    ramp = [
        (0, 0, 0, 0),
        tokens["accent2"] + (90,),
        tokens["accent"] + (170,),
        (250, 226, 160, 235),
    ]

    def _colourise(intensity):
        if intensity <= 0.02:
            return ramp[0]
        pos = min(0.999, intensity) * (len(ramp) - 1)
        lo = int(pos)
        return _blend(ramp[lo][0:3], ramp[lo + 1][0:3], pos - lo) + (
            int(ramp[lo][3] + (ramp[lo + 1][3] - ramp[lo][3]) * (pos - lo)),)

    lut = [_colourise(i / 255.0) for i in range(256)]
    density = Image.new("RGBA", (bw, bh))
    density.putdata([
        lut[min(255, int(v / peak * 255))] for v in field_values
    ])
    density = density.resize((WIDTH, HEIGHT), Image.BICUBIC)
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=109)
    fonts = {
        "title": load_font(tokens["font_bold"], 44),
        "small": load_font(tokens["font"], 23),
    }

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("heat")
        reveal = ease_in_out(clamp01(progress * 1.2))
        shown = density.copy()
        if reveal < 1.0:
            a = shown.getchannel("A").point(lambda v: int(v * reveal))
            shown.putalpha(a)
        layer.image.alpha_composite(shown)
        d = ImageDraw.Draw(layer.image, "RGBA")
        for poly in outlines:
            for ring in poly:
                pts = [(p[0] * WIDTH, p[1] * HEIGHT) for p in ring]
                closed = pts + [pts[0]]
                d.line(closed, fill=(255, 255, 255, 150), width=2,
                       joint="curve")
        if title:
            d.text((90, 64), title.upper()[:56], font=fonts["title"],
                   fill=tokens["text"] + (255,))
        # Legend: the ramp with its recorded extremes.
        lx, ly = WIDTH - 470, HEIGHT - 104
        for i in range(200):
            colour = _colourise(i / 199.0)
            d.line([lx + i * 2, ly, lx + i * 2, ly + 22],
                   fill=colour[0:3] + (255,))
        legend = weight_label or "DENSITY"
        d.text((lx, ly - 34), legend.upper()[:40], font=fonts["small"],
               fill=tokens["muted"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("heatmap", style_name, duration, sentence_index,
                       scene_number,
                       {"points": len(points), "title": bool(title),
                        "outlines": len(outlines),
                        "max_weight": max_w})


# ---------------------------------------------------------------------------
# F4. Newspaper wall — the montage's grid composition (board family)
# ---------------------------------------------------------------------------

def newspaper_wall(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    articles = []
    for entry in data.get("articles") or []:
        if not isinstance(entry, dict):
            continue
        headline = _clean(entry.get("headline"))
        if not headline:
            continue
        articles.append({
            "headline": headline,
            # Founder ruling 2026-11-23: an article's "source"
            # field is never read or drawn — provenance is not
            # screen content, whatever a caller supplies.
            "date": _clean(entry.get("date")),
        })
    if len(articles) < 3:
        return None, {"reason": "insufficient_articles"}
    articles = articles[:6]
    title = _clean(data.get("title"))
    paper = _aged_paper((WIDTH, HEIGHT), tokens)
    fonts = {
        "headline": load_font(tokens["font_bold"], 31),
        "small": load_font(tokens["font"], 20),
        "title": load_font(tokens["font_bold"], 44),
    }
    probe = _probe_draw()
    card_w, card_h = 540, 330
    built = []
    for idx, article in enumerate(articles):
        card = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
        cd = ImageDraw.Draw(card)
        cd.rectangle([0, 0, card_w, card_h], fill=(246, 242, 230, 255))
        cd.rectangle([0, 0, card_w, 7], fill=tokens["accent"] + (255,))
        y = 26
        for line in wrap_text(
                probe, article["headline"], fonts["headline"],
                card_w - 52)[:5]:
            cd.text((26, y), line, font=fonts["headline"],
                    fill=(30, 28, 24, 255))
            y += 40
        if article["date"]:
            cd.text((26, card_h - 40), article["date"][:36],
                    font=fonts["small"], fill=(90, 84, 70, 255))
        # A pin dot at the top corner — the board family's mark.
        cd.ellipse([card_w - 44, 12, card_w - 20, 36],
                   fill=tokens["accent"] + (255,),
                   outline=(255, 255, 255, 255), width=2)
        angle = (-2, 1.5, -1, 2, -1.5, 1)[idx % 6]
        built.append(card.rotate(angle, expand=True, resample=Image.BICUBIC))
    cols = 3
    positions = []
    for idx in range(len(built)):
        positions.append((
            90 + (idx % cols) * (card_w + 55),
            250 + (idx // cols) * (card_h + 45),
        ))

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("paper", parallax=0.3)
        base.image = paper.convert("RGBA")
        layer = canvas.new_layer("cards")
        for idx, card in enumerate(built):
            local = ease_out(clamp01(progress * 2.6 - idx * 0.32))
            if local <= 0:
                continue
            x, y = positions[idx]
            y = int(y + (1 - local) * 60)
            shown = card.copy()
            if local < 1.0:
                a = shown.getchannel("A").point(lambda v: int(v * local))
                shown.putalpha(a)
            layer.image.alpha_composite(shown, (x, y))
        top = canvas.new_layer("title")
        td = ImageDraw.Draw(top.image, "RGBA")
        if title:
            _torn_banner(td, 90, 56, 1050, 92, fill=(250, 246, 236, 255))
            td.text((122, 78), title.upper()[:52], font=fonts["title"],
                    fill=(30, 26, 20, 255))
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("newspaper_wall", style_name, duration,
                       sentence_index, scene_number,
                       {"articles": len(built), "title": bool(title)})


# ---------------------------------------------------------------------------
# F4. Crime scene diagram — a top-down SCHEMATIC of recorded markers.
# It is a diagram, never a depiction of the real scene: the frame
# carries a scene-tape band stamped SCHEMATIC so no viewer can
# mistake it for the place itself.
# ---------------------------------------------------------------------------

_TAPE_YELLOW = (238, 196, 52)


def crime_scene_diagram(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    markers = []
    for entry in data.get("markers") or []:
        if not isinstance(entry, dict):
            continue
        at = entry.get("at")
        if not (isinstance(at, (list, tuple)) and len(at) >= 2):
            continue
        try:
            x, y = float(at[0]), float(at[1])
        except (TypeError, ValueError):
            continue
        markers.append({
            "label": _clean(entry.get("label")) or "EVIDENCE",
            "at": (min(0.98, max(0.02, x)), min(0.98, max(0.02, y))),
        })
    if not markers:
        return None, {"reason": "no_markers"}
    markers = markers[:10]
    rooms = []
    for entry in data.get("rooms") or []:
        if not isinstance(entry, dict):
            continue
        try:
            rooms.append({
                "x": float(entry.get("x") or 0), "y": float(entry.get("y") or 0),
                "w": float(entry.get("w") or 0), "h": float(entry.get("h") or 0),
                "label": _clean(entry.get("label")),
            })
        except (TypeError, ValueError):
            continue
    rooms = [r for r in rooms if r["w"] > 0.02 and r["h"] > 0.02][:8]
    if not rooms:
        # No recorded plan: one plain plot outline — layout, not
        # data (it asserts nothing about the real place).
        rooms = [{"x": 0.05, "y": 0.08, "w": 0.62, "h": 0.84, "label": ""}]
    title = _clean(data.get("title"))
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=113)
    fonts = {
        "title": load_font(tokens["font_bold"], 40),
        "room": load_font(tokens["font"], 22),
        "legend": load_font(tokens["font"], 26),
        "num": load_font(tokens["font_bold"], 24),
        "tape": load_font(tokens["font_bold"], 25),
    }
    plot = (90, 150, 1290, 950)

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("plan")
        d = ImageDraw.Draw(layer.image, "RGBA")
        plan_frac = ease_in_out(clamp01(progress * 1.3))
        for room in rooms:
            rx0 = plot[0] + room["x"] * (plot[2] - plot[0])
            ry0 = plot[1] + room["y"] * (plot[3] - plot[1])
            rx1 = rx0 + room["w"] * (plot[2] - plot[0])
            ry1 = ry0 + room["h"] * (plot[3] - plot[1])
            corners = [(rx0, ry0), (rx1, ry0), (rx1, ry1), (rx0, ry1),
                       (rx0, ry0)]
            progressive_polyline(
                d, corners, plan_frac, fill=(255, 255, 255, 210), width=3)
            if room["label"] and plan_frac > 0.8:
                d.text((rx0 + 14, ry0 + 10), room["label"].upper()[:24],
                       font=fonts["room"], fill=tokens["muted"] + (255,))
        # Evidence markers: numbered cones that pop in sequence.
        for i, marker in enumerate(markers):
            local = ease_out(clamp01(progress * 2.4 - 0.5 - i * 0.3))
            if local <= 0:
                continue
            mx = plot[0] + marker["at"][0] * (plot[2] - plot[0])
            my = plot[1] + marker["at"][1] * (plot[3] - plot[1])
            size = 21 * local
            d.polygon(
                [(mx, my - size), (mx - size * 0.72, my + size * 0.8),
                 (mx + size * 0.72, my + size * 0.8)],
                fill=_TAPE_YELLOW + (255,),
                outline=(20, 20, 20, 255), width=2)
            num = str(i + 1)
            nw = d.textlength(num, font=fonts["num"])
            d.text((mx - nw / 2, my - 13), num, font=fonts["num"],
                   fill=(20, 20, 20, 255))
        # Legend: the recorded marker labels, numbered to match.
        lx = 1360
        ly = 210
        for i, marker in enumerate(markers):
            local = ease_out(clamp01(progress * 2.4 - 0.5 - i * 0.3))
            if local <= 0:
                continue
            d.rectangle([lx, ly, lx + 34, ly + 34],
                        fill=_TAPE_YELLOW + (255,))
            num = str(i + 1)
            nw = d.textlength(num, font=fonts["num"])
            d.text((lx + 17 - nw / 2, ly + 3), num, font=fonts["num"],
                   fill=(20, 20, 20, 255))
            d.text((lx + 50, ly + 3), marker["label"][:30],
                   font=fonts["legend"], fill=tokens["text"] + (255,))
            ly += 52
        if title:
            d.text((90, 70), title.upper()[:52], font=fonts["title"],
                   fill=tokens["text"] + (255,))
        # Scene-tape band: striped, and stamped with what this is.
        tape_y = HEIGHT - 86
        d.rectangle([0, tape_y, WIDTH, tape_y + 44], fill=_TAPE_YELLOW + (255,))
        for sx in range(-40, WIDTH, 56):
            d.polygon(
                [(sx, tape_y + 44), (sx + 22, tape_y),
                 (sx + 40, tape_y), (sx + 18, tape_y + 44)],
                fill=(18, 18, 18, 255))
        tape_text = "SCHEMATIC DIAGRAM — NOT THE ACTUAL SCENE"
        tw = d.textlength(tape_text, font=fonts["tape"])
        d.rectangle([(WIDTH - tw) / 2 - 16, tape_y + 6,
                     (WIDTH + tw) / 2 + 16, tape_y + 40],
                    fill=_TAPE_YELLOW + (255,))
        d.text(((WIDTH - tw) / 2, tape_y + 9), tape_text,
               font=fonts["tape"], fill=(18, 18, 18, 255))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("crime_scene_diagram", style_name, duration,
                       sentence_index, scene_number,
                       {"markers": len(markers), "rooms": len(rooms),
                        "title": bool(title), "schematic": True})


# ---------------------------------------------------------------------------
# F5. Construction sequence — a staged build schematic from
# recorded scenario stages (each stage adds its recorded band)
# ---------------------------------------------------------------------------

def construction_sequence(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    stages = []
    for entry in data.get("stages") or []:
        if not isinstance(entry, dict):
            continue
        label = _clean(entry.get("label") or entry.get("name"))
        if not label:
            continue
        height = entry.get("height")
        try:
            height = float(height) if height is not None else None
        except (TypeError, ValueError):
            height = None
        stages.append({"label": label, "height": height})
    if len(stages) < 2:
        return None, {"reason": "insufficient_stages"}
    stages = stages[:8]
    height_source = "recorded"
    if any(s["height"] is None for s in stages):
        height_source = "equal"
        for stage in stages:
            if stage["height"] is None:
                stage["height"] = 1.0
    total_h = sum(s["height"] for s in stages) or 1.0
    title = _clean(data.get("title"))
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=127)
    fonts = {
        "title": load_font(tokens["font_bold"], 44),
        "stage": load_font(tokens["font"], 28),
        "num": load_font(tokens["font_bold"], 24),
    }
    ground_y = HEIGHT - 170
    top_y = 210
    frame_x0, frame_x1 = 420, 1080

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("build")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, 84), title.upper()[:56],
                   font=fonts["title"], fill=tokens["text"] + (255,))
        d.line([180, ground_y, WIDTH - 180, ground_y],
               fill=(255, 255, 255, 190), width=4)
        span = 1.0 / len(stages)
        y_cursor = ground_y
        built_top = ground_y
        for i, stage in enumerate(stages):
            band_h = (ground_y - top_y) * (stage["height"] / total_h)
            local = ease_out(clamp01((progress - i * span) / span))
            y_cursor -= band_h
            if local <= 0:
                continue
            shown_h = band_h * local
            by1 = y_cursor + band_h
            by0 = by1 - shown_h
            # Frame: two columns + the stage's slab.
            d.line([frame_x0, by1, frame_x0, by0],
                   fill=tokens["accent2"] + (255,), width=7)
            d.line([frame_x1, by1, frame_x1, by0],
                   fill=tokens["accent2"] + (255,), width=7)
            d.rectangle([frame_x0 - 14, by0, frame_x1 + 14, by0 + 12],
                        fill=tokens["accent"] + (255,))
            built_top = by0
            # Stage label at the right, numbered.
            ly = by0 + band_h / 2 - 18
            d.ellipse([1160, ly, 1196, ly + 36],
                      fill=tokens["accent"] + (255,))
            num = str(i + 1)
            nw = d.textlength(num, font=fonts["num"])
            d.text((1178 - nw / 2, ly + 4), num, font=fonts["num"],
                   fill=(255, 255, 255, 255))
            d.text((1224, ly + 2), stage["label"][:34],
                   font=fonts["stage"], fill=tokens["text"] + (255,))
        # Crane: a mast + jib outline standing over the built top
        # — schematic machinery, drawn only once building starts.
        if progress > 0.05:
            mast_x = frame_x1 + 90
            d.line([mast_x, ground_y, mast_x, 150],
                   fill=tokens["muted"] + (220,), width=5)
            d.line([mast_x, 150, frame_x0 - 60, 150],
                   fill=tokens["muted"] + (220,), width=5)
            d.line([mast_x, 150, frame_x0 + 200, built_top - 8],
                   fill=tokens["muted"] + (140,), width=2)
            d.line([frame_x0 + 200, 150, frame_x0 + 200, built_top - 8],
                   fill=tokens["muted"] + (180,), width=3)
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("construction_sequence", style_name, duration,
                       sentence_index, scene_number,
                       {"stages": len(stages), "title": bool(title),
                        "height_source": height_source})


# ---------------------------------------------------------------------------
# F5. Molecular diagram — parametric ball-and-stick from a recorded
# structure (atoms at recorded positions, recorded bonds)
# ---------------------------------------------------------------------------

_ELEMENT_COLOURS = {
    "H": (240, 240, 240), "O": (214, 48, 48), "C": (72, 72, 72),
    "N": (48, 80, 214), "S": (230, 190, 40), "P": (230, 130, 30),
    "CL": (60, 200, 80), "FE": (200, 110, 50), "NA": (150, 90, 200),
}
_ELEMENT_RADII = {"H": 20, "O": 34, "C": 32, "N": 32, "S": 40,
                  "P": 38, "CL": 40}


def molecular_diagram(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    structure = data.get("molecule") if isinstance(
        data.get("molecule"), dict) else data
    atoms = []
    for entry in structure.get("atoms") or []:
        if not isinstance(entry, dict):
            continue
        at = entry.get("at")
        element = _clean(entry.get("element") or entry.get("symbol")).upper()
        if not element or not (
                isinstance(at, (list, tuple)) and len(at) >= 2):
            continue
        try:
            x, y = float(at[0]), float(at[1])
        except (TypeError, ValueError):
            continue
        atoms.append({"element": element,
                      "at": (min(0.97, max(0.03, x)),
                             min(0.97, max(0.03, y)))})
    if len(atoms) < 2:
        return None, {"reason": "insufficient_atoms"}
    atoms = atoms[:24]
    bonds = []
    for entry in structure.get("bonds") or []:
        if not (isinstance(entry, (list, tuple)) and len(entry) >= 2):
            continue
        try:
            i, j = int(entry[0]), int(entry[1])
            order = int(entry[2]) if len(entry) > 2 else 1
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(atoms) and 0 <= j < len(atoms) and i != j:
            bonds.append((i, j, max(1, min(3, order))))
    title = _clean(data.get("title") or structure.get("name"))
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=131)
    fonts = {
        "title": load_font(tokens["font_bold"], 44),
        "atom": load_font(tokens["font_bold"], 26),
    }
    area = (140, 170, WIDTH - 140, HEIGHT - 150)

    def _xy(atom):
        return (
            area[0] + atom["at"][0] * (area[2] - area[0]),
            area[1] + atom["at"][1] * (area[3] - area[1]),
        )

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("molecule")
        d = ImageDraw.Draw(layer.image, "RGBA")
        bond_frac = ease_in_out(clamp01(progress * 1.4))
        for (i, j, order) in bonds:
            ax, ay = _xy(atoms[i])
            bx, by = _xy(atoms[j])
            dx, dy = bx - ax, by - ay
            norm = math.hypot(dx, dy) or 1.0
            px_, py_ = -dy / norm, dx / norm
            for k in range(order):
                off = (k - (order - 1) / 2) * 11
                pts = [(ax + px_ * off, ay + py_ * off),
                       (bx + px_ * off, by + py_ * off)]
                progressive_polyline(
                    d, pts, bond_frac, fill=(235, 235, 235, 235), width=7)
        for i, atom in enumerate(atoms):
            local = ease_out(clamp01(progress * 2.2 - i * 0.12))
            if local <= 0:
                continue
            cx, cy = _xy(atom)
            radius = _ELEMENT_RADII.get(atom["element"], 30) * local
            colour = _ELEMENT_COLOURS.get(
                atom["element"], tokens["accent"])
            d.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                      fill=colour + (255,),
                      outline=(255, 255, 255, 220), width=2)
            if local > 0.7:
                symbol = atom["element"].title() \
                    if len(atom["element"]) > 1 else atom["element"]
                sw = d.textlength(symbol, font=fonts["atom"])
                dark = sum(colour) > 380
                d.text((cx - sw / 2, cy - 15), symbol, font=fonts["atom"],
                       fill=(20, 20, 20, 255) if dark
                       else (255, 255, 255, 255))
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, 70), title.upper()[:52],
                   font=fonts["title"], fill=tokens["text"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("molecular_diagram", style_name, duration,
                       sentence_index, scene_number,
                       {"atoms": len(atoms), "bonds": len(bonds),
                        "title": bool(title)})


# ---------------------------------------------------------------------------
# F7. HUD overlay — recorded readouts over a recorded photo (the
# full-frame graphic form of the scan_grid aesthetic)
# ---------------------------------------------------------------------------

def hud_overlay(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    photo = _load_image(data.get("photo") or data.get("image"))
    if photo is None:
        return None, {"reason": "no_photo"}
    readouts = []
    for entry in data.get("readouts") or []:
        if not isinstance(entry, dict):
            continue
        label = _clean(entry.get("label"))
        value = _clean(entry.get("value") or entry.get("display"))
        if label and value:
            readouts.append({"label": label, "value": value})
    if not readouts:
        return None, {"reason": "no_readouts"}
    readouts = readouts[:5]
    title = _clean(data.get("title"))
    bg_img = _dark_photo(data.get("photo") or data.get("image"),
                         darkness=0.62)
    fonts = {
        "title": load_font(tokens["font_bold"], 34),
        "label": load_font(tokens["font_bold"], 21),
        "value": load_font(tokens["font_bold"], 32),
    }
    bg_rgba = bg_img.convert("RGBA")
    accent = tokens["accent2"]

    def _bracket(d, x, y, w, h, arm, fill, width=4):
        d.line([x, y, x + arm, y], fill=fill, width=width)
        d.line([x, y, x, y + arm], fill=fill, width=width)
        d.line([x + w, y, x + w - arm, y], fill=fill, width=width)
        d.line([x + w, y, x + w, y + arm], fill=fill, width=width)
        d.line([x, y + h, x + arm, y + h], fill=fill, width=width)
        d.line([x, y + h, x, y + h - arm], fill=fill, width=width)
        d.line([x + w, y + h, x + w - arm, y + h], fill=fill, width=width)
        d.line([x + w, y + h, x + w, y + h - arm], fill=fill, width=width)

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg")
        base.image = bg_rgba.copy()
        layer = canvas.new_layer("hud")
        d = ImageDraw.Draw(layer.image, "RGBA")
        fade = ease_out(clamp01(progress * 1.6))
        alpha = int(235 * fade)
        _bracket(d, 54, 54, WIDTH - 108, HEIGHT - 108, 46,
                 accent + (alpha,))
        # Sweep line: a soft horizontal scan crossing the frame.
        sweep_y = 80 + (HEIGHT - 160) * ((t * 0.14) % 1.0)
        d.line([70, sweep_y, WIDTH - 70, sweep_y], fill=accent + (36,),
               width=10)
        if title:
            d.text((96, 84), title.upper()[:52], font=fonts["title"],
                   fill=tokens["text"] + (alpha,))
        # Reticle at the frame's centre-left.
        rcx, rcy = 620, 560
        rr = 92 + 8 * math.sin(t * 2.2)
        d.ellipse([rcx - rr, rcy - rr, rcx + rr, rcy + rr],
                  outline=accent + (alpha,), width=3)
        d.line([rcx - rr - 26, rcy, rcx - rr + 30, rcy],
               fill=accent + (alpha,), width=3)
        d.line([rcx + rr - 30, rcy, rcx + rr + 26, rcy],
               fill=accent + (alpha,), width=3)
        d.line([rcx, rcy - rr - 26, rcx, rcy - rr + 30],
               fill=accent + (alpha,), width=3)
        d.line([rcx, rcy + rr - 30, rcx, rcy + rr + 26],
               fill=accent + (alpha,), width=3)
        # Readout stack at the right, each row ticked to a leader.
        rx = 1290
        ry = 250
        for i, readout in enumerate(readouts):
            local = ease_out(clamp01(progress * 2.2 - 0.3 - i * 0.3))
            if local <= 0:
                continue
            d.rectangle([rx, ry, rx + 470, ry + 96],
                        fill=(6, 10, 14, int(150 * local)))
            d.rectangle([rx, ry, rx + 6, ry + 96],
                        fill=accent + (int(255 * local),))
            d.text((rx + 26, ry + 12), readout["label"].upper()[:26],
                   font=fonts["label"], fill=tokens["muted"] + (alpha,))
            d.text((rx + 26, ry + 42), readout["value"][:24],
                   font=fonts["value"], fill=tokens["text"] + (alpha,))
            ry += 122
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("hud_overlay", style_name, duration,
                       sentence_index, scene_number,
                       {"photo": True, "readouts": len(readouts),
                        "title": bool(title)})


# ---------------------------------------------------------------------------
# F7. Holographic panel — recorded items on a translucent 2.5D
# panel (scan_grid / cinematic_data aesthetic, layered parallax)
# ---------------------------------------------------------------------------

def holographic_panel(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    items = []
    for entry in data.get("items") or data.get("stats") or []:
        if not isinstance(entry, dict):
            continue
        label = _clean(entry.get("label"))
        value = _clean(entry.get("value") or entry.get("display"))
        if isinstance(entry.get("value"), (int, float)) and not value:
            value = _fmt(entry["value"])
        if label and value:
            items.append({"label": label, "value": value})
    if len(items) < 2:
        return None, {"reason": "insufficient_items"}
    items = items[:6]
    title = _clean(data.get("title"))
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=137)
    fonts = {
        "title": load_font(tokens["font_bold"], 40),
        "label": load_font(tokens["font_bold"], 23),
        "value": load_font(tokens["font_bold"], 34),
    }
    panel = (330, 190, WIDTH - 330, HEIGHT - 170)
    accent = tokens["accent2"]

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg", parallax=0.25)
        base.image = bg.convert("RGBA")
        glow = canvas.new_layer("glow", parallax=0.55)
        gd = ImageDraw.Draw(glow.image, "RGBA")
        fade = ease_out(clamp01(progress * 1.5))
        # Wireframe ring motif behind the panel (hologram base).
        rcx, rcy = WIDTH // 2, (panel[1] + panel[3]) // 2
        for k, rr in enumerate((330, 262, 196)):
            gd.ellipse([rcx - rr, rcy - rr * 0.36, rcx + rr, rcy + rr * 0.36],
                       outline=accent + (int(50 * fade),), width=2)
        layer = canvas.new_layer("panel", parallax=0.8)
        d = ImageDraw.Draw(layer.image, "RGBA")
        d.rectangle(panel, fill=accent + (int(26 * fade),))
        d.rectangle(panel, outline=accent + (int(220 * fade),), width=3)
        # Scanlines across the panel.
        for sy in range(panel[1] + 8, panel[3], 9):
            d.line([panel[0], sy, panel[2], sy], fill=accent + (14,),
                   width=1)
        if title:
            d.text((panel[0] + 44, panel[1] + 34), title.upper()[:48],
                   font=fonts["title"], fill=tokens["text"] + (255,))
            d.line([panel[0] + 44, panel[1] + 96, panel[2] - 44,
                    panel[1] + 96], fill=accent + (160,), width=2)
        y = panel[1] + 132
        for i, item in enumerate(items):
            local = ease_out(clamp01(progress * 2.0 - 0.25 - i * 0.24))
            if local <= 0:
                continue
            alpha = int(255 * local)
            d.text((panel[0] + 44, y), item["label"].upper()[:30],
                   font=fonts["label"], fill=tokens["muted"] + (alpha,))
            vw = d.textlength(item["value"][:24], font=fonts["value"])
            d.text((panel[2] - 44 - vw, y - 8), item["value"][:24],
                   font=fonts["value"], fill=tokens["text"] + (alpha,))
            y += 74
        camera = {"scale": 1.0 + 0.03 * progress,
                  "x": -14 + 28 * progress}
        frame = canvas.composite(camera=camera)
        return frame

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("holographic_panel", style_name, duration,
                       sentence_index, scene_number,
                       {"items": len(items), "title": bool(title)})


# ---------------------------------------------------------------------------
# F8. Before-and-after — a wipe across a recorded pair of images
# ---------------------------------------------------------------------------

def before_after(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    before = _load_image(data.get("before"))
    after = _load_image(data.get("after"))
    if before is None or after is None:
        return None, {"reason": "missing_pair"}
    before_label = _clean(data.get("before_label")) or "BEFORE"
    after_label = _clean(data.get("after_label")) or "AFTER"
    title = _clean(data.get("title"))
    before_img = fit_cover(before, (WIDTH, HEIGHT)).convert("RGBA")
    after_img = fit_cover(after, (WIDTH, HEIGHT)).convert("RGBA")
    fonts = {
        "label": load_font(tokens["font_bold"], 30),
        "title": load_font(tokens["font_bold"], 40),
    }

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("before")
        base.image = before_img.copy()
        wipe = ease_in_out(clamp01((progress - 0.12) * 1.35))
        divider = int(WIDTH * wipe)
        layer = canvas.new_layer("after")
        if divider > 0:
            layer.image.alpha_composite(
                after_img.crop((0, 0, divider, HEIGHT)), (0, 0))
        d = ImageDraw.Draw(layer.image, "RGBA")
        if 0 < divider < WIDTH:
            d.rectangle([divider - 4, 0, divider + 4, HEIGHT],
                        fill=tokens["accent"] + (255,))
            d.ellipse([divider - 30, HEIGHT // 2 - 30,
                       divider + 30, HEIGHT // 2 + 30],
                      fill=tokens["accent"] + (255,),
                      outline=(255, 255, 255, 255), width=3)
        def _tag(x, text, align_right=False):
            tw = d.textlength(text.upper(), font=fonts["label"])
            bx = x - tw - 28 if align_right else x
            d.rectangle([bx - 14, 66, bx + tw + 14, 122],
                        fill=(0, 0, 0, 150))
            d.text((bx, 76), text.upper()[:26], font=fonts["label"],
                   fill=(255, 255, 255, 255))
        if divider < WIDTH - 320:
            _tag(WIDTH - 60, before_label, align_right=True)
        if divider > 200:
            _tag(60, after_label)
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, HEIGHT - 108), title.upper()[:52],
                   font=fonts["title"], fill=tokens["text"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("before_after", style_name, duration,
                       sentence_index, scene_number,
                       {"pair": True, "title": bool(title),
                        "before_label": before_label,
                        "after_label": after_label})


# ---------------------------------------------------------------------------
# F8. Layered story reveal — recorded photos entering as depth
# layers, each with its recorded caption
# ---------------------------------------------------------------------------

def layered_reveal(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    photos = []
    for entry in data.get("photos") or data.get("layers") or []:
        if not isinstance(entry, dict):
            continue
        img = _load_image(entry.get("path") or entry.get("photo"))
        if img is None:
            continue
        caption = _clean(entry.get("caption") or entry.get("title"))
        # Founder ruling 2026-11-23 (template-level guard): a
        # caption that is a raw file title is provenance, not a
        # caption — dropped whoever supplied it.
        if caption.lower().startswith("file:") or re.search(
            r"\.(jpe?g|png|tiff?|webp|gif|svg)\b", caption.lower()
        ):
            caption = ""
        photos.append({
            "image": img,
            "caption": caption,
        })
    if len(photos) < 2:
        return None, {"reason": "insufficient_photos"}
    photos = photos[:4]
    title = _clean(data.get("title"))
    fonts = {
        "title": load_font(tokens["font_bold"], 42),
        "caption": load_font(tokens["font_bold"], 28),
    }
    base_img = fit_cover(photos[0]["image"], (WIDTH, HEIGHT))
    black = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    base_img = Image.blend(base_img, black, 0.25)
    cards = []
    slots = [(880, 120, 920, 640), (90, 430, 860, 560), (1010, 470, 820, 520)]
    for i, photo in enumerate(photos[1:]):
        x, y, w, h = slots[i % len(slots)]
        card = Image.new("RGBA", (w + 24, h + 24), (0, 0, 0, 0))
        cd = ImageDraw.Draw(card)
        cd.rectangle([0, 0, w + 24, h + 24], fill=(245, 243, 236, 255))
        card.alpha_composite(
            fit_cover(photo["image"], (w, h)).convert("RGBA"), (12, 12))
        cards.append((card, x - 12, y - 12, photo["caption"]))

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("base", parallax=0.4)
        base.image = base_img.convert("RGBA")
        layer = canvas.new_layer("cards", parallax=0.85)
        for i, (card, x, y, caption) in enumerate(cards):
            local = ease_out(clamp01(progress * 2.4 - 0.35 - i * 0.55))
            if local <= 0:
                continue
            shown = card.copy()
            slide_x = int(x + (1 - local) * (140 if i % 2 == 0 else -140))
            if local < 1.0:
                a = shown.getchannel("A").point(lambda v: int(v * local))
                shown.putalpha(a)
            layer.image.alpha_composite(shown, (slide_x, y))
            if caption and local > 0.6:
                d = ImageDraw.Draw(layer.image, "RGBA")
                tw = d.textlength(caption.upper()[:44], font=fonts["caption"])
                bx = min(slide_x + 12, WIDTH - tw - 50)
                by = y + card.size[1] + 10
                if by < HEIGHT - 70:
                    d.rectangle([bx - 12, by - 6, bx + tw + 12, by + 44],
                                fill=(0, 0, 0, 150))
                    d.text((bx, by), caption.upper()[:44],
                           font=fonts["caption"],
                           fill=(255, 255, 255, 255))
        top = canvas.new_layer("title")
        td = ImageDraw.Draw(top.image, "RGBA")
        if photos[0]["caption"]:
            td.rectangle([86, 84, 94, 150], fill=tokens["accent"] + (255,))
            td.text((118, 92), photos[0]["caption"].upper()[:52],
                    font=fonts["caption"], fill=tokens["text"] + (255,))
        if title:
            td.text((90, HEIGHT - 96), title.upper()[:56],
                    font=fonts["title"], fill=tokens["text"] + (255,))
        camera = {"scale": 1.04 + 0.03 * progress}
        return canvas.composite(camera=camera)

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("layered_reveal", style_name, duration,
                       sentence_index, scene_number,
                       {"photos": len(photos), "title": bool(title)})


# ---------------------------------------------------------------------------
# F7. Object labels — callouts anchored to RECORDED points on a
# recorded photo (bounded: no tracking, no invented anchors)
# ---------------------------------------------------------------------------

def object_labels(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    photo = _load_image(data.get("photo") or data.get("image"))
    if photo is None:
        return None, {"reason": "no_photo"}
    labels = []
    for entry in data.get("labels") or []:
        if not isinstance(entry, dict):
            continue
        at = entry.get("at")
        text = _clean(entry.get("text") or entry.get("label"))
        if not text or not (
                isinstance(at, (list, tuple)) and len(at) >= 2):
            continue
        try:
            x, y = float(at[0]), float(at[1])
        except (TypeError, ValueError):
            continue
        labels.append({"text": text,
                       "at": (min(0.97, max(0.03, x)),
                              min(0.97, max(0.03, y)))})
    if not labels:
        return None, {"reason": "no_labels"}
    labels = labels[:6]
    title = _clean(data.get("title"))
    bg_img = _dark_photo(data.get("photo") or data.get("image"),
                         darkness=0.72)
    fonts = {
        "label": load_font(tokens["font_bold"], 27),
        "title": load_font(tokens["font_bold"], 38),
    }
    bg_rgba = bg_img.convert("RGBA")

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg")
        base.image = bg_rgba.copy()
        layer = canvas.new_layer("labels")
        d = ImageDraw.Draw(layer.image, "RGBA")
        for i, label in enumerate(labels):
            local = ease_out(clamp01(progress * 2.4 - 0.2 - i * 0.35))
            if local <= 0:
                continue
            ax, ay = label["at"][0] * WIDTH, label["at"][1] * HEIGHT
            pulse = 12 + 4 * math.sin(t * 4.0 + i)
            d.ellipse([ax - pulse, ay - pulse, ax + pulse, ay + pulse],
                      outline=(255, 255, 255, 220), width=3)
            d.ellipse([ax - 6, ay - 6, ax + 6, ay + 6],
                      fill=tokens["accent"] + (255,))
            # Callout box offset away from the frame centre, so
            # the leader never crosses its own anchor's region.
            tw = d.textlength(label["text"].upper()[:36],
                              font=fonts["label"])
            box_w = tw + 36
            box_h = 52
            bx = ax + 54 if ax < WIDTH / 2 else ax - 54 - box_w
            by = ay - 90 if ay > HEIGHT / 2 else ay + 40
            bx = min(max(bx, 24), WIDTH - box_w - 24)
            by = min(max(by, 24), HEIGHT - box_h - 70)
            edge_x = bx if ax < WIDTH / 2 else bx + box_w
            d.line([ax, ay, edge_x, by + box_h / 2],
                   fill=(255, 255, 255, 200), width=2)
            d.rectangle([bx, by, bx + box_w, by + box_h],
                        fill=(8, 10, 12, int(185 * local)))
            d.rectangle([bx, by, bx + 5, by + box_h],
                        fill=tokens["accent"] + (int(255 * local),))
            d.text((bx + 18, by + 11), label["text"].upper()[:36],
                   font=fonts["label"], fill=tokens["text"] + (255,))
        if title:
            d.text((90, 60), title.upper()[:52], font=fonts["title"],
                   fill=tokens["text"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("object_labels", style_name, duration,
                       sentence_index, scene_number,
                       {"photo": True, "labels": len(labels),
                        "title": bool(title),
                        "anchors": "recorded"})


V4_TEMPLATES = {
    "bubble_chart": bubble_chart,
    "population_pyramid": population_pyramid,
    "animated_dashboard": animated_dashboard,
    "checklist_graphic": checklist_graphic,
    "milestone_waveform": milestone_waveform,
    "territory_expansion": territory_expansion,
    "satellite_zoom": satellite_zoom,
    "heatmap": heatmap,
    "newspaper_wall": newspaper_wall,
    "crime_scene_diagram": crime_scene_diagram,
    "construction_sequence": construction_sequence,
    "molecular_diagram": molecular_diagram,
    "hud_overlay": hud_overlay,
    "holographic_panel": holographic_panel,
    "before_after": before_after,
    "layered_reveal": layered_reveal,
    "object_labels": object_labels,
}

# Self-registration into the v1 registry, as with v2/v3.
from services.motion_engine import templates as _v1_templates  # noqa: E402

_v1_templates.TEMPLATES.update(V4_TEMPLATES)
