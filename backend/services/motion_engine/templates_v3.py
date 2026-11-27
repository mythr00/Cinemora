"""Motion Engine templates v3 (Transformation Phase 2.5, Wave 1).

The Wave 1 families the three finish-line films need, built on
the same contract as v1/v2: every template renders ONLY
caller-supplied data and returns (None, {"reason": ...}) when its
data requirements are not met. Signature and metadata shape are
identical to services.motion_engine.templates (v1).

Families:
  F1 charts      — line_chart, pie_chart (bar/counters/comparison/
                   gauge already exist in v1/v2 + the native stat
                   family). Data comes from recorded material —
                   the Phase 2.3 structured fact table or the
                   sentence's own recorded numbers — assembled by
                   the graphics pipeline; the templates refuse
                   without numeric data.
  F4 investigation — newspaper_montage, profile_reveal
                   (the document unlock lives with its base
                   template in v1; red_string_board was removed
                   by the P0 ruling, 2026-11-27).
  F6/F7 text     — archival_title, text_overlay, text_highlight.
  F8 photo       — photo_parallax lived here; REMOVED completely
                   by the founder's P0 ruling (2026-11-27). A beat
                   with a photo is served by the photo itself as a
                   normal still visual, never by the smeared fake
                   layer separation this template produced.
"""

import math
import re
from pathlib import Path

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


def _load_image(path):
    if not path or not Path(path).exists():
        return None
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None


def _probe_draw():
    return ImageDraw.Draw(Image.new("RGBA", (10, 10)))


def _blend(c1, c2, f):
    return tuple(int(a + (b - a) * f) for a, b in zip(c1, c2))


def _series_palette(tokens, n):
    base = [
        tokens["accent"], tokens["accent2"],
        _blend(tokens["accent"], (255, 255, 255), 0.35),
        _blend(tokens["accent2"], (255, 255, 255), 0.35),
        tokens["muted"],
        _blend(tokens["accent"], (0, 0, 0), 0.3),
        _blend(tokens["accent2"], (0, 0, 0), 0.3),
        tokens["text"],
    ]
    return [base[i % len(base)] for i in range(n)]


def _numeric_items(raw, value_key="value"):
    items = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        try:
            value = float(entry.get(value_key))
        except (TypeError, ValueError):
            continue
        items.append({
            "label": _clean(entry.get("label")) or "ITEM",
            "value": value,
            "display": _clean(entry.get("display")),
        })
    return items


# ---------------------------------------------------------------------------
# F1. Line chart — a recorded series drawn as an animated line
# ---------------------------------------------------------------------------

def line_chart(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    points = _numeric_items(data.get("points") or data.get("items"))
    if len(points) < 2:
        return None, {"reason": "insufficient_points"}
    points = points[:24]
    title = _clean(data.get("title"))
    units = _clean(data.get("units"))
    series_label = _clean(data.get("series_label"))
    from services.motion_engine.templates_v2 import _nice_max

    values = [p["value"] for p in points]
    v_min = min(0.0, min(values))
    v_max = _nice_max(max(values) - v_min) + v_min if max(values) > v_min else v_min + 1.0
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=59)
    fonts = {
        "title": load_font(tokens["font_bold"], 44),
        "label": load_font(tokens["font"], 23),
        "value": load_font(tokens["font_bold"], 26),
        "tick": load_font(tokens["font"], 22),
    }
    x0, x1 = 240, WIDTH - 150
    y_top, y_bot = 250, HEIGHT - 210

    def _xy(i, value):
        x = x0 + (x1 - x0) * (i / (len(points) - 1))
        frac = 0.0 if v_max == v_min else (value - v_min) / (v_max - v_min)
        y = y_bot - (y_bot - y_top) * frac
        return (x, y)

    coords = [_xy(i, p["value"]) for i, p in enumerate(points)]
    probe = _probe_draw()

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("chart")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, 84), title.upper()[:60],
                   font=fonts["title"], fill=tokens["text"] + (255,))
        if series_label:
            d.text((x0, 168), series_label.upper()[:56], font=fonts["value"],
                   fill=tokens["accent2"] + (255,))
        # Gridlines + axes + ticks.
        for tick_i in range(5):
            frac = tick_i / 4
            gy = y_bot - (y_bot - y_top) * frac
            d.line([x0, gy, x1, gy], fill=(255, 255, 255, 26), width=1)
            tick_value = v_min + (v_max - v_min) * frac
            tick_text = (
                f"{tick_value:,.0f}" if float(tick_value).is_integer()
                else f"{tick_value:,.1f}"
            )
            tw = d.textlength(tick_text, font=fonts["tick"])
            d.text((x0 - 18 - tw, gy - 12), tick_text, font=fonts["tick"],
                   fill=tokens["muted"] + (255,))
        d.line([x0, y_top - 14, x0, y_bot], fill=(255, 255, 255, 150), width=2)
        d.line([x0, y_bot, x1, y_bot], fill=(255, 255, 255, 150), width=2)
        if units:
            uw = d.textlength(units.upper(), font=fonts["tick"])
            d.text(((x0 + x1) / 2 - uw / 2, y_bot + 64), units.upper()[:48],
                   font=fonts["tick"], fill=tokens["muted"] + (255,))
        frac = ease_in_out(clamp01(progress * 1.25))
        # Area under the line (revealed with it).
        if frac > 0:
            shown = _window_points(coords, frac)
            if len(shown) >= 2:
                area = shown + [(shown[-1][0], y_bot), (shown[0][0], y_bot)]
                d.polygon(area, fill=tokens["accent"] + (36,))
                progressive_polyline(
                    d, coords, frac, fill=tokens["accent"] + (255,), width=5)
        # Point markers + labels as the line reaches them.
        for i, (px_, py_) in enumerate(coords):
            point_frac = 0.0 if len(coords) == 1 else i / (len(coords) - 1)
            if frac < point_frac:
                continue
            d.ellipse([px_ - 8, py_ - 8, px_ + 8, py_ + 8],
                      fill=(255, 255, 255, 255),
                      outline=tokens["accent"] + (255,), width=3)
            point = points[i]
            shown = point["display"] or (
                f"{point['value']:,.0f}"
                if float(point["value"]).is_integer()
                else f"{point['value']:,.1f}"
            )
            vw = d.textlength(shown, font=fonts["value"])
            d.text((px_ - vw / 2, py_ - 46), shown, font=fonts["value"],
                   fill=(255, 255, 255, 255))
            label = point["label"][:20]
            lw = d.textlength(label, font=fonts["label"])
            d.text((px_ - lw / 2, y_bot + 14), label, font=fonts["label"],
                   fill=tokens["text"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("line_chart", style_name, duration, sentence_index, scene_number,
                       {"points": len(points), "title": bool(title),
                        "units": bool(units)})


def _window_points(coords, frac):
    """The polyline prefix up to length fraction `frac` (the same
    reveal geometry as core.progressive_polyline, returned as
    points for the area fill)."""
    if not coords:
        return []
    if len(coords) == 1:
        return list(coords)
    lengths = []
    total = 0.0
    for (ax, ay), (bx, by) in zip(coords, coords[1:]):
        seg = math.hypot(bx - ax, by - ay)
        lengths.append(seg)
        total += seg
    if total <= 0:
        return list(coords)
    target = total * clamp01(frac)
    shown = [coords[0]]
    walked = 0.0
    for idx, seg in enumerate(lengths):
        ax, ay = coords[idx]
        bx, by = coords[idx + 1]
        if walked + seg <= target:
            shown.append((bx, by))
            walked += seg
        else:
            f = 0.0 if seg == 0 else (target - walked) / seg
            shown.append((ax + (bx - ax) * f, ay + (by - ay) * f))
            break
    return shown


# ---------------------------------------------------------------------------
# F1. Pie chart — recorded shares as animated slices
# ---------------------------------------------------------------------------

def pie_chart(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    items = _numeric_items(data.get("items") or data.get("points"))
    items = [i for i in items if i["value"] > 0]
    if not items:
        return None, {"reason": "no_numeric_items"}
    if len(items) < 2:
        return None, {"reason": "insufficient_items"}
    items = items[:8]
    total = sum(i["value"] for i in items)
    if total <= 0:
        return None, {"reason": "no_positive_total"}
    title = _clean(data.get("title"))
    colours = _series_palette(tokens, len(items))
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=61)
    fonts = {
        "title": load_font(tokens["font_bold"], 44),
        "legend": load_font(tokens["font"], 30),
        "value": load_font(tokens["font_bold"], 32),
    }
    cx, cy, r = 620, HEIGHT // 2 + 30, 330
    starts = []
    angle = -90.0
    for item in items:
        sweep = 360.0 * item["value"] / total
        starts.append((angle, angle + sweep))
        angle += sweep

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("pie")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, 84), title.upper()[:60],
                   font=fonts["title"], fill=tokens["text"] + (255,))
        box = [cx - r, cy - r, cx + r, cy + r]
        for i, (item, (a0, a1)) in enumerate(zip(items, starts)):
            local = ease_out(clamp01(progress * 1.7 - i * 0.22))
            if local <= 0:
                continue
            d.pieslice(box, start=a0, end=a0 + (a1 - a0) * local,
                       fill=colours[i] + (255,),
                       outline=(10, 10, 12, 255), width=3)
        # Legend: colour chip, label, value + share.
        lx = 1120
        ly = cy - (len(items) * 74) // 2
        for i, item in enumerate(items):
            local = ease_out(clamp01(progress * 1.7 - i * 0.22))
            if local <= 0:
                continue
            d.rectangle([lx, ly + 4, lx + 34, ly + 38], fill=colours[i] + (255,))
            share = 100.0 * item["value"] / total
            value_text = item["display"] or (
                f"{item['value']:,.0f}"
                if float(item["value"]).is_integer()
                else f"{item['value']:,.1f}"
            )
            d.text((lx + 54, ly), item["label"][:26], font=fonts["legend"],
                   fill=tokens["text"] + (255,))
            d.text((lx + 54, ly + 38), f"{value_text} · {share:.0f}%",
                   font=fonts["value"], fill=tokens["muted"] + (255,))
            ly += 88
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("pie_chart", style_name, duration, sentence_index, scene_number,
                       {"items": len(items), "title": bool(title),
                        "total": total})


# ---------------------------------------------------------------------------
# F4. Newspaper montage — recorded headlines on aged paper cards
# ---------------------------------------------------------------------------

def newspaper_montage(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
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
    if not articles:
        return None, {"reason": "no_articles"}
    articles = articles[:4]
    title = _clean(data.get("title"))
    paper = _aged_paper((WIDTH, HEIGHT), tokens)
    fonts = {
        "headline": load_font(tokens["font_bold"], 40),
        "small": load_font(tokens["font"], 22),
        "title": load_font(tokens["font_bold"], 44),
    }
    probe = _probe_draw()
    card_w, card_h = 600, 400
    built = []
    for idx, article in enumerate(articles):
        card = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
        cd = ImageDraw.Draw(card)
        cd.rectangle([0, 0, card_w, card_h], fill=(246, 242, 230, 255))
        cd.rectangle([0, 0, card_w, 8], fill=tokens["accent"] + (255,))
        y = 30
        for line in wrap_text(probe, article["headline"], fonts["headline"], card_w - 60)[:4]:
            cd.text((30, y), line, font=fonts["headline"], fill=(30, 28, 24, 255))
            y += 50
        if article["date"]:
            cd.text((30, card_h - 46), article["date"][:40],
                    font=fonts["small"], fill=(90, 84, 70, 255))
        angle = (-3, 2, -2, 3)[idx % 4]
        built.append(card.rotate(angle, expand=True, resample=Image.BICUBIC))
    positions = [(100, 260), (680, 220), (1280, 260), (420, 600)][:len(built)]

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("paper", parallax=0.3)
        base.image = paper.convert("RGBA")
        layer = canvas.new_layer("cards")
        for idx, card in enumerate(built):
            local = ease_out(clamp01(progress * 2.8 - idx * 0.5))
            if local <= 0:
                continue
            x, y = positions[idx]
            y = int(y + (1 - local) * 80)
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
    return path, _meta("newspaper_montage", style_name, duration, sentence_index, scene_number,
                       {"articles": len(built), "title": bool(title)})


# ---------------------------------------------------------------------------
# F4. Profile reveal — portrait card + recorded profile fields
# ---------------------------------------------------------------------------

def profile_reveal(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    photo = _load_image(data.get("photo"))
    if photo is None:
        return None, {"reason": "no_photo"}
    title = _designed_text(data.get("title") or data.get("name"))
    if not title:
        return None, {"reason": "no_title"}
    subtitle = _designed_text(data.get("subtitle"))
    fields = []
    for entry in data.get("fields") or []:
        if not isinstance(entry, dict):
            continue
        label = _clean(entry.get("label"))
        value = _clean(entry.get("value"))
        if label and value:
            fields.append({"label": label, "value": value})
    fields = fields[:5]
    card_w, card_h = 600, 780
    portrait = fit_cover(photo, (card_w - 28, card_h - 28))
    fonts = {
        "title": load_font(tokens["font_bold"], 54),
        "subtitle": load_font(tokens["font"], 28),
        "label": load_font(tokens["font_bold"], 22),
        "value": load_font(tokens["font"], 32),
    }
    probe = _probe_draw()
    title_lines = wrap_text(probe, title, fonts["title"], 900)[:2]

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg")
        base.image = _textured_dark(
            (WIDTH, HEIGHT), tokens["bg"], seed=67).convert("RGBA")
        layer = canvas.new_layer("profile")
        d = ImageDraw.Draw(layer.image, "RGBA")
        slide = ease_out(clamp01(progress * 1.9))
        card_x = int(-card_w - 60 + slide * (card_w + 170))
        card_y = 150
        d.rectangle([card_x + 12, card_y + 16, card_x + card_w + 12,
                     card_y + card_h + 16], fill=(0, 0, 0, 120))
        d.rectangle([card_x, card_y, card_x + card_w, card_y + card_h],
                    fill=(245, 243, 236, 255))
        layer.image.alpha_composite(
            portrait.convert("RGBA"), (card_x + 14, card_y + 14))
        if slide > 0.55:
            tx = card_x + card_w + 84
            y = card_y + 30
            d.rectangle([tx, y, tx + 120, y + 6], fill=tokens["accent"] + (255,))
            y += 36
            for line in title_lines:
                d.text((tx, y), line, font=fonts["title"],
                       fill=tokens["text"] + (255,))
                y += 66
            if subtitle:
                d.text((tx, y + 6), subtitle[:60], font=fonts["subtitle"],
                       fill=tokens["muted"] + (255,))
                y += 58
            y += 22
            for i, field in enumerate(fields):
                local = clamp01(progress * 3.0 - 0.8 - i * 0.35)
                if local <= 0:
                    continue
                d.text((tx, y), field["label"].upper()[:30],
                       font=fonts["label"], fill=tokens["accent2"] + (255,))
                d.text((tx, y + 30), field["value"][:52],
                       font=fonts["value"], fill=tokens["text"] + (255,))
                y += 92
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("profile_reveal", style_name, duration, sentence_index, scene_number,
                       {"photo": True, "fields": len(fields),
                        "subtitle": bool(subtitle)})


# ---------------------------------------------------------------------------
# F6. Archival title sequence — themed full-frame title card
# ---------------------------------------------------------------------------

def archival_title(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    title = _clean(data.get("title"))
    if not title:
        return None, {"reason": "no_title"}
    subtitle = _clean(data.get("subtitle"))
    date_label = _clean(data.get("date_label"))
    kicker = _clean(data.get("kicker"))
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=71)
    fonts = {
        "title": load_font(tokens["font_bold"], 84),
        "subtitle": load_font(tokens["font"], 32),
        "kicker": load_font(tokens["font_bold"], 26),
        "date": load_font(tokens["font_bold"], 30),
    }
    probe = _probe_draw()
    # Phase 5 (X3) label fit: every text element is measured
    # against the width it will be drawn at BEFORE any frame is
    # drawn. An element that does not fit refuses the graphic
    # with the typed label_does_not_fit reason — the historical
    # [:64]/[:80]/[:40] character slices truncated text mid-word
    # instead, which is exactly the defect class this refuses.
    from services.motion_engine.label_fit import (
        LABEL_FIT_REASON, fit_lines,
    )

    title_lines = fit_lines(probe, title.upper(), fonts["title"],
                            1400, max_lines=3)
    if title_lines is None:
        return None, {"reason": LABEL_FIT_REASON,
                      "detail": "title does not fit 1400px in 3 lines"}
    for _key, _text, _font, _width in (
        ("kicker", kicker.upper(), fonts["kicker"], 1400),
        ("subtitle", subtitle, fonts["subtitle"], 1500),
        ("date_label", date_label.upper(), fonts["date"], 1000),
    ):
        if _text and fit_lines(probe, _text, _font, _width,
                               max_lines=1) is None:
            return None, {"reason": LABEL_FIT_REASON,
                          "detail": f"{_key} does not fit {_width}px"}

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg", parallax=0.3)
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("title")
        d = ImageDraw.Draw(layer.image, "RGBA")
        fade = ease_out(clamp01(progress * 1.5))
        alpha = int(255 * fade)
        cx = WIDTH // 2
        total_h = len(title_lines) * 104 + (54 if subtitle else 0) \
            + (86 if date_label else 0) + (50 if kicker else 0)
        y = (HEIGHT - total_h) // 2 + int((1 - fade) * 22)
        if kicker:
            kw = d.textlength(kicker.upper(), font=fonts["kicker"])
            d.text((cx - kw / 2, y), kicker.upper(),
                   font=fonts["kicker"], fill=tokens["accent"] + (alpha,))
            y += 50
        for line in title_lines:
            lw = d.textlength(line, font=fonts["title"])
            d.text((cx - lw / 2, y), line, font=fonts["title"],
                   fill=tokens["text"] + (alpha,))
            y += 104
        rule_w = int(180 * fade)
        d.rectangle([cx - rule_w // 2, y + 6, cx + rule_w // 2, y + 12],
                    fill=tokens["accent"] + (alpha,))
        y += 44
        if subtitle:
            sw = d.textlength(subtitle, font=fonts["subtitle"])
            d.text((cx - sw / 2, y), subtitle, font=fonts["subtitle"],
                   fill=tokens["muted"] + (alpha,))
            y += 54
        if date_label:
            dw = d.textlength(date_label.upper(), font=fonts["date"])
            pad = 26
            d.rectangle([cx - dw / 2 - pad, y, cx + dw / 2 + pad, y + 56],
                        outline=tokens["accent2"] + (alpha,), width=3)
            d.text((cx - dw / 2, y + 10), date_label.upper(),
                   font=fonts["date"], fill=tokens["text"] + (alpha,))
        _footer(d, tokens)
        camera = {"scale": 1.0 + 0.045 * progress}
        return canvas.composite(camera=camera)

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("archival_title", style_name, duration, sentence_index, scene_number,
                       {"title": True, "subtitle": bool(subtitle),
                        "date_label": bool(date_label), "kicker": bool(kicker)})


# ---------------------------------------------------------------------------
# F7. Text overlay — generalised positioned text layer (full-frame
# graphic form of the composition lower-third primitive)
# ---------------------------------------------------------------------------

def text_overlay(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    text = _clean(data.get("text") or data.get("title"))
    if not text:
        return None, {"reason": "no_text"}
    # Founder ruling 2026-11-23: an overlay is a short label, not
    # a sentence — sentence-length text is refused at the
    # template so no caller can turn this into a text card.
    if len(text) > 64 or len(text.split()) > 8:
        return None, {"reason": "payload_not_short"}
    subtext = _clean(data.get("subtext"))
    position = _clean(data.get("position")).lower()
    if position not in ("lower", "center", "upper"):
        position = "lower"
    photo = _load_image(data.get("photo"))
    if photo is not None:
        bg_img = _dark_photo(data.get("photo"), darkness=0.55)
    else:
        bg_img = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=73)
    fonts = {
        "text": load_font(tokens["font_bold"], 52),
        "sub": load_font(tokens["font"], 28),
    }
    probe = _probe_draw()
    lines = wrap_text(probe, text, fonts["text"], 1560)[:3]
    bg_rgba = bg_img.convert("RGBA")

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg")
        base.image = bg_rgba.copy()
        layer = canvas.new_layer("overlay")
        d = ImageDraw.Draw(layer.image, "RGBA")
        fade = ease_out(clamp01(progress * 1.8))
        alpha = int(255 * fade)
        block_h = len(lines) * 66 + (44 if subtext else 0)
        if position == "center":
            y = (HEIGHT - block_h) // 2
            x = WIDTH // 2
            centred = True
        elif position == "upper":
            y = 120
            x = 110
            centred = False
        else:
            y = HEIGHT - 150 - block_h
            x = 110
            centred = False
        d.rectangle([x if not centred else x - 60, y - 18,
                     (x if not centred else x - 60) + 10, y + block_h],
                    fill=tokens["accent"] + (alpha,))
        if centred:
            # Centre position reads as a title: rule above instead.
            d.rectangle([x - 60, y - 26, x + 60, y - 20],
                        fill=tokens["accent"] + (alpha,))
        for line in lines:
            if centred:
                lw = d.textlength(line, font=fonts["text"])
                d.text((x - lw / 2, y), line, font=fonts["text"],
                       fill=tokens["text"] + (alpha,))
            else:
                d.text((x + 34, y), line, font=fonts["text"],
                       fill=tokens["text"] + (alpha,))
            y += 66
        if subtext:
            if centred:
                sw = d.textlength(subtext, font=fonts["sub"])
                d.text((x - sw / 2, y), subtext[:80], font=fonts["sub"],
                       fill=tokens["muted"] + (alpha,))
            else:
                d.text((x + 34, y), subtext[:80], font=fonts["sub"],
                       fill=tokens["muted"] + (alpha,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("text_overlay", style_name, duration, sentence_index, scene_number,
                       {"text": True, "photo": photo is not None,
                        "position": position, "subtext": bool(subtext)})


# ---------------------------------------------------------------------------
# F7. Text highlight — a highlight pass over recorded quote/document
# text (read-along sweep; a named phrase holds when supplied)
# ---------------------------------------------------------------------------

def text_highlight(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    text = _clean(data.get("text") or data.get("quote"))
    if not text:
        return None, {"reason": "no_text"}
    # Founder ruling 2026-11-23: the page presents a SHORT
    # excerpt (a quoted passage), never a full narration
    # sentence dressed as a document.
    if len(text) > 200:
        return None, {"reason": "payload_not_short"}
    phrase = _clean(data.get("highlight"))
    title = _clean(data.get("title"))
    # Founder ruling 2026-11-23: no source/header label is drawn
    # on the page — a supplied source_label is ignored entirely
    # (the historical header burned an entity name, or the
    # placeholder word "Document", over the excerpt).
    fonts = {
        "body": load_font(tokens["font"], 34),
        "title": load_font(tokens["font_bold"], 36),
        "small": load_font(tokens["font_bold"], 22),
    }
    probe = _probe_draw()
    page = (300, 130, 1620, 950)
    lines = wrap_text(probe, text, fonts["body"], page[2] - page[0] - 104)[:12]
    # Word boxes per line, for the highlight rectangles.
    line_words = []
    for line in lines:
        words = []
        x = page[0] + 52
        for word in line.split():
            w = probe.textlength(word + " ", font=fonts["body"])
            words.append((word, x, w))
            x += w
        line_words.append(words)
    total_words = sum(len(ws) for ws in line_words)
    phrase_words = set()
    if phrase:
        lowered = [w.lower() for ws in line_words for w, _, _ in ws]
        needle = [w.lower() for w in phrase.split()]
        for start in range(len(lowered) - len(needle) + 1):
            if lowered[start:start + len(needle)] == needle:
                phrase_words = set(range(start, start + len(needle)))
                break

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg")
        base.image = _textured_dark(
            (WIDTH, HEIGHT), tokens["bg"], seed=79).convert("RGBA")
        layer = canvas.new_layer("page")
        d = ImageDraw.Draw(layer.image, "RGBA")
        x0, y0, x1, y1 = page
        d.rectangle([x0 + 14, y0 + 18, x1 + 14, y1 + 18], fill=(0, 0, 0, 120))
        d.rectangle([x0, y0, x1, y1], fill=(246, 242, 230, 255))
        d.rectangle([x0, y0, x1, y0 + 6], fill=tokens["accent"] + (255,))
        if title:
            d.text((x0 + 52, y0 + 62), title.upper()[:56],
                   font=fonts["title"], fill=(28, 26, 22, 255))
        # Highlight cursor: read-along across every word, or —
        # with a named phrase — a sweep that lands on the phrase
        # and holds it.
        if phrase_words:
            cursor = min(phrase_words) + (
                max(phrase_words) - min(phrase_words) + 1
            ) * ease_in_out(clamp01((progress - 0.25) * 1.6)) \
                if progress > 0.25 else 0
            lit = set(range(min(phrase_words), int(cursor) + 1)) \
                & phrase_words if progress > 0.25 else set()
            if progress > 0.75:
                lit = phrase_words
        else:
            lit_count = int(
                ease_in_out(clamp01(progress * 1.12)) * total_words)
            lit = set(range(min(lit_count, total_words)))
        y = y0 + 150
        index = 0
        for words in line_words:
            for word, wx, ww in words:
                if index in lit:
                    d.rectangle([wx - 3, y + 3, wx + ww - 4, y + 42],
                                fill=(250, 214, 90, 200))
                index += 1
            for word, wx, ww in words:
                d.text((wx, y), word, font=fonts["body"],
                       fill=(36, 34, 30, 255))
            y += 52
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("text_highlight", style_name, duration, sentence_index, scene_number,
                       {"text": True, "words": total_words,
                        "phrase": bool(phrase_words)})


# ---------------------------------------------------------------------------
# F8. Photo parallax — a still separated into depth layers under
# the core camera (2.5D photo treatment)
# ---------------------------------------------------------------------------

def _designed_text(value, max_len=64):
    """MG-06-residual (template-level guard): text a photo
    template may typeset is a designed identifier (place / date /
    name), never caller-supplied provenance. A value that looks
    like file metadata ("File:..." archive titles, anything
    carrying an image-file extension, provider/source strings)
    or that runs past a headline's length is dropped — whoever
    supplied it, including a replayed recipe."""
    text = _clean(value)
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("file:") or lowered.startswith("source"):
        return ""
    if re.search(r"\.(jpe?g|png|tiff?|webp|gif|svg)\b", lowered):
        return ""
    if "wikimedia" in lowered or "•" in text:
        return ""
    if len(text) > max_len:
        return ""
    return text


V3_TEMPLATES = {
    "line_chart": line_chart,
    "pie_chart": pie_chart,
    "newspaper_montage": newspaper_montage,
    "profile_reveal": profile_reveal,
    "archival_title": archival_title,
    "text_overlay": text_overlay,
    "text_highlight": text_highlight,
}

# Self-registration into the v1 registry, as with v2.
from services.motion_engine import templates as _v1_templates  # noqa: E402

_v1_templates.TEMPLATES.update(V3_TEMPLATES)
