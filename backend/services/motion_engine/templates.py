"""Motion Engine templates (v1).

Every template renders ONLY caller-supplied data. A template whose
data requirements are not met returns (None, {"reason": ...}) —
it never invents geography, documents, evidence or numbers.
Reconstructions are labelled by the caller through the data itself
(e.g. document_presentation's source_label); the engine stamps
every asset metadata with generated=True.

Signature: template(data, style, out_path, duration=5.0,
sentence_index=None, scene_number=None)
  -> (mp4_path, metadata) | (None, {"reason": ...})
metadata: {generator: "motion_engine", template, sentence_index,
scene_number, duration, style, inputs, generated: True}
"""

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from services.motion_engine import core
from services.motion_engine.core import (
    HEIGHT, WIDTH, clamp01, ease_in_out, ease_out, fit_contain, fit_cover,
    load_font, progressive_polyline, wrap_text,
)
from services.motion_engine.styles import resolve_style


def _meta(template, style_name, duration, sentence_index, scene_number, inputs):
    return {
        "generator": "motion_engine",
        "template": template,
        "sentence_index": sentence_index,
        "scene_number": scene_number,
        "duration": float(duration),
        "style": style_name,
        "inputs": inputs,
        "generated": True,
    }


def _clean(value):
    return " ".join(str(value or "").split()).strip()


def _dark_photo(path, size=(WIDTH, HEIGHT), darkness=0.45):
    try:
        img = Image.open(path).convert("RGB")
    except Exception:
        return None
    img = fit_cover(img, size)
    if darkness < 1.0:
        black = Image.new("RGB", size, (0, 0, 0))
        img = Image.blend(img, black, 1.0 - darkness)
    return img


def _textured_dark(size, base, seed=7):
    img = Image.new("RGB", size, base)
    rng = random.Random(seed)
    noise = Image.effect_noise(size, 18).convert("L")
    noise_rgb = Image.merge("RGB", (noise, noise, noise))
    img = Image.blend(img, noise_rgb, 0.05)
    draw = ImageDraw.Draw(img)
    for gx in range(0, size[0], 120):
        draw.line([gx, 0, gx, size[1]], fill=tuple(min(255, c + 14) for c in base), width=1)
    for gy in range(0, size[1], 120):
        draw.line([0, gy, size[0], gy], fill=tuple(min(255, c + 14) for c in base), width=1)
    return img


def _footer(draw, tokens, label="CINEMORA / GENERATED GRAPHIC"):
    font = load_font(tokens["font"], 20)
    draw.text((90, HEIGHT - 52), label, font=font, fill=tokens["muted"])


# ---------------------------------------------------------------------------
# 1. Territory map
# ---------------------------------------------------------------------------

def territory_map(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    regions = data.get("regions") or []
    valid = [r for r in regions if isinstance(r, dict) and len(r.get("points") or []) >= 3]
    if not valid:
        return None, {"reason": "no_polygon_data"}
    routes = [r for r in (data.get("routes") or []) if isinstance(r, dict) and len(r.get("points") or []) >= 2]
    title = _clean(data.get("title"))
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=11)
    fonts = {
        "title": load_font(tokens["font_bold"], 44),
        "label": load_font(tokens["font_bold"], 34),
        "small": load_font(tokens["font"], 24),
    }

    def px(points):
        return [(p[0] * WIDTH, p[1] * HEIGHT) for p in points]

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("base")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("map")
        d = ImageDraw.Draw(layer.image, "RGBA")
        sweep = ease_in_out(clamp01(progress * 1.15))
        for region in valid:
            pts = px(region["points"])
            xs = [p[0] for p in pts]
            fill = region.get("fill") or tokens["accent"]
            if isinstance(fill, (list, tuple)):
                fill_rgb = tuple(fill[:3])
            else:
                fill_rgb = tokens["accent"]
            # Glowing sweep: reveal the filled polygon left->right.
            reveal_x = min(xs) + (max(xs) - min(xs)) * sweep if max(xs) > min(xs) else max(xs)
            region_img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
            rd = ImageDraw.Draw(region_img)
            rd.polygon(pts, fill=fill_rgb + (178,))
            mask = Image.new("L", (WIDTH, HEIGHT), 0)
            ImageDraw.Draw(mask).rectangle([0, 0, reveal_x, HEIGHT], fill=255)
            layer.image.paste(region_img, (0, 0), Image.composite(region_img, Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0)), mask))
            # Glow outline + white outline.
            for wdt, alpha in ((10, 60), (4, 160)):
                d.line(pts + [pts[0]], fill=fill_rgb + (alpha,), width=wdt, joint="curve")
            d.line(pts + [pts[0]], fill=(255, 255, 255, 235), width=2, joint="curve")
            label = _clean(region.get("label"))
            if label and sweep > 0.55:
                cx = sum(xs) / len(xs)
                ys = [p[1] for p in pts]
                cy = sum(ys) / len(ys)
                tw = d.textlength(label.upper(), font=fonts["label"])
                d.text((cx - tw / 2, cy - 20), label.upper(), font=fonts["label"], fill=(255, 255, 255, 255))
        for route in routes:
            pts = px(route["points"])
            progressive_polyline(d, pts, ease_in_out(clamp01(progress * 1.3 - 0.1)),
                                 fill=tokens["accent2"] + (255,), width=6)
            for (x, y) in pts:
                d.ellipse([x - 8, y - 8, x + 8, y + 8], fill=(255, 255, 255, 255))
            rlabel = _clean(route.get("label"))
            if rlabel and pts:
                d.text((pts[0][0] + 14, pts[0][1] - 34), rlabel, font=fonts["small"], fill=tokens["text"] + (255,))
        top = canvas.new_layer("text")
        td = ImageDraw.Draw(top.image, "RGBA")
        if title:
            td.text((90, 64), title.upper(), font=fonts["title"], fill=tokens["text"] + (255,))
        _footer(td, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("territory_map", style_name, duration, sentence_index, scene_number,
                       {"regions": len(valid), "routes": len(routes), "title": bool(title)})


# ---------------------------------------------------------------------------
# 2. Evidence board
# ---------------------------------------------------------------------------

def _aged_paper(size, tokens, seed=3):
    w, h = size
    base = tokens.get("paper", (216, 201, 163))
    img = Image.new("RGB", size, base)
    noise = Image.effect_noise(size, 22).convert("L")
    img = Image.blend(img, Image.merge("RGB", (noise, noise, noise)), 0.08)
    draw = ImageDraw.Draw(img)
    for y in range(140, h, 46):
        draw.line([40, y, w - 40, y], fill=tuple(max(0, c - 28) for c in base), width=1)
    # Vignette.
    vignette = Image.new("L", size, 0)
    vd = ImageDraw.Draw(vignette)
    vd.ellipse([-w * 0.25, -h * 0.25, w * 1.25, h * 1.25], fill=255)
    vignette = vignette.filter(ImageFilter.GaussianBlur(180))
    dark = Image.new("RGB", size, tuple(max(0, c - 70) for c in base))
    img = Image.composite(img, dark, vignette)
    return img


def _torn_banner(draw, x, y, w, h, fill, seed=5):
    rng = random.Random(seed)
    points_top = [(x + i * 28, y + rng.randint(0, 10)) for i in range(int(w / 28) + 1)]
    points_bottom = [(x + w - i * 28, y + h - rng.randint(0, 10)) for i in range(int(w / 28) + 1)]
    draw.polygon(points_top + points_bottom, fill=fill)


def evidence_board(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    cards = []
    for entry in data.get("photos") or []:
        if not isinstance(entry, dict):
            continue
        p = entry.get("path")
        if not p or not Path(p).exists():
            continue  # Missing photo file -> skip that card.
        try:
            img = Image.open(p).convert("RGB")
        except Exception:
            continue
        cards.append((img, _clean(entry.get("caption"))))
    if not cards:
        return None, {"reason": "no_valid_photos"}
    title = _clean(data.get("title")) or "EVIDENCE"
    labels = [_clean(x) for x in (data.get("labels") or []) if _clean(x)]
    paper = _aged_paper((WIDTH, HEIGHT), tokens)
    fonts = {
        "title": load_font(tokens["font_bold"], 46),
        "caption": load_font(tokens["font"], 24),
        "label": load_font(tokens["font_bold"], 22),
    }
    # Pre-build polaroid cards.
    built = []
    card_w, card_h = 560, 470
    for idx, (img, caption) in enumerate(cards[:4]):
        photo = fit_cover(img, (card_w - 36, card_h - 110))
        card = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
        shadow = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rectangle([10, 14, card_w - 4, card_h - 2], fill=(0, 0, 0, 110))
        shadow = shadow.filter(ImageFilter.GaussianBlur(10))
        card.alpha_composite(shadow, (0, 0))
        cd = ImageDraw.Draw(card)
        cd.rectangle([0, 0, card_w - 14, card_h - 14], fill=(245, 243, 236, 255))
        card.paste(photo, (18, 18))
        if caption:
            cd.text((24, card_h - 82), caption[:44], font=fonts["caption"], fill=(40, 38, 34, 255))
        angle = (-4, 3, -2, 4)[idx % 4]
        built.append(card.rotate(angle, expand=True, resample=Image.BICUBIC))
    positions = [(110, 300), (700, 280), (1290, 300), (420, 560)][:len(built)]

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("paper", parallax=0.3)
        base.image = paper.convert("RGBA")
        layer = canvas.new_layer("cards")
        for idx, card in enumerate(built):
            local = ease_out(clamp01(progress * 3.0 - idx * 0.55))
            if local <= 0:
                continue
            x, y = positions[idx]
            y = int(y + (1 - local) * 90)
            faded = card.copy()
            if local < 1.0:
                a = faded.getchannel("A").point(lambda v: int(v * local))
                faded.putalpha(a)
            layer.image.alpha_composite(faded, (x, y))
            # Brass pin.
            d = ImageDraw.Draw(layer.image, "RGBA")
            cx, cy = x + card.size[0] // 2, y + 16
            d.ellipse([cx - 15, cy - 15, cx + 15, cy + 15], fill=tokens["brass"] + (255,), outline=(90, 70, 30, 255), width=3)
            d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=(240, 220, 170, 255))
        top = canvas.new_layer("banner")
        td = ImageDraw.Draw(top.image, "RGBA")
        _torn_banner(td, 90, 56, 1050, 92, fill=(250, 246, 236, 255))
        td.text((122, 78), title.upper()[:52], font=fonts["title"], fill=(30, 26, 20, 255))
        for i, lab in enumerate(labels[:3]):
            td.text((120, HEIGHT - 120 + i * 30), lab.upper(), font=fonts["label"], fill=(60, 30, 24, 255))
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("evidence_board", style_name, duration, sentence_index, scene_number,
                       {"photos": len(built), "title": bool(title), "labels": len(labels)})


# ---------------------------------------------------------------------------
# 3. Editorial panel
# ---------------------------------------------------------------------------

def editorial_panel(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    photo_path = data.get("photo")
    bg_img = _dark_photo(photo_path, darkness=0.5) if photo_path else None
    if bg_img is None:
        return None, {"reason": "no_photo"}
    title = _clean(data.get("title"))
    if not title:
        return None, {"reason": "no_title"}
    options = [_clean(o) for o in (data.get("options") or []) if _clean(o)]
    footer = _clean(data.get("footer"))
    fonts = {
        "title": load_font(tokens["font_bold"], 48),
        "opt": load_font(tokens["font"], 32),
        "small": load_font(tokens["font"], 22),
    }
    bg_rgba = bg_img.convert("RGBA")

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("photo", parallax=0.4)
        base.image = bg_rgba.copy()
        layer = canvas.new_layer("panel")
        d = ImageDraw.Draw(layer.image, "RGBA")
        slide = ease_out(clamp01(progress * 2.2))
        panel_w, panel_h, panel_y = 820, 760, 150
        panel_x = int(-panel_w - 40 + slide * (panel_w + 130))
        d.rectangle([panel_x, panel_y, panel_x + panel_w, panel_y + panel_h],
                    fill=tokens["surface"] + (238,))
        d.rectangle([panel_x, panel_y, panel_x + panel_w, panel_y + 6], fill=tokens["accent"] + (255,))
        if slide > 0.6:
            tx = panel_x + 48
            y = panel_y + 52
            probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
            for line in wrap_text(probe, title, fonts["title"], panel_w - 96)[:3]:
                d.text((tx, y), line, font=fonts["title"], fill=tokens["text"] + (255,))
                y += 60
            y += 28
            for i, opt in enumerate(options[:5]):
                local = clamp01(progress * 3.2 - 0.7 - i * 0.35)
                if local <= 0:
                    continue
                d.rectangle([tx, y + 4, tx + 30, y + 34], outline=tokens["accent"] + (255,), width=3)
                d.text((tx + 52, y), opt[:46], font=fonts["opt"], fill=tokens["text"] + (255,))
                y += 62
            if footer:
                d.text((tx, panel_y + panel_h - 58), footer[:60], font=fonts["small"], fill=tokens["muted"] + (255,))
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("editorial_panel", style_name, duration, sentence_index, scene_number,
                       {"photo": True, "options": len(options), "title": True})


# ---------------------------------------------------------------------------
# 4. Data gauge
# ---------------------------------------------------------------------------

def data_gauge(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    percent = data.get("percent")
    try:
        percent = float(percent)
    except (TypeError, ValueError):
        return None, {"reason": "no_percent_value"}
    percent = max(0.0, min(100.0, percent))
    label = _clean(data.get("label")) or "VALUE"
    sublabel = _clean(data.get("sublabel"))
    photo_path = data.get("photo")
    bg_img = _dark_photo(photo_path, darkness=0.35) if photo_path else None
    if bg_img is None:
        bg_img = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=13)
    fonts = {
        "big": load_font(tokens["font_bold"], 120),
        "label": load_font(tokens["font_bold"], 38),
        "small": load_font(tokens["font"], 26),
    }
    bg_rgba = bg_img.convert("RGBA")

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg", parallax=0.4)
        base.image = bg_rgba.copy()
        layer = canvas.new_layer("gauge")
        d = ImageDraw.Draw(layer.image, "RGBA")
        cx, cy, r = WIDTH // 2, HEIGHT // 2 - 30, 300
        box = [cx - r, cy - r, cx + r, cy + r]
        d.ellipse(box, outline=(255, 255, 255, 50), width=30)
        sweep = ease_in_out(clamp01(progress * 1.25)) * percent
        if sweep > 0:
            d.arc(box, start=-90, end=-90 + sweep * 3.6, fill=tokens["accent"] + (255,), width=30)
        value_text = f"{int(round(sweep))}%"
        tw = d.textlength(value_text, font=fonts["big"])
        d.text((cx - tw / 2, cy - 78), value_text, font=fonts["big"], fill=(255, 255, 255, 255))
        lw = d.textlength(label.upper(), font=fonts["label"])
        d.text((cx - lw / 2, cy + r + 48), label.upper()[:52], font=fonts["label"], fill=tokens["text"] + (255,))
        if sublabel:
            sw = d.textlength(sublabel, font=fonts["small"])
            d.text((cx - sw / 2, cy + r + 104), sublabel[:70], font=fonts["small"], fill=tokens["muted"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("data_gauge", style_name, duration, sentence_index, scene_number,
                       {"percent": percent, "label": bool(label), "photo": bool(photo_path)})


# ---------------------------------------------------------------------------
# 5. Outcome path
# ---------------------------------------------------------------------------

def outcome_path(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    outcomes = [o for o in (data.get("outcomes") or []) if isinstance(o, dict) and _clean(o.get("label"))]
    if not outcomes:
        return None, {"reason": "no_outcomes"}
    outcomes = outcomes[:6]
    title_left = _clean(data.get("title_left"))
    title_right = _clean(data.get("title_right"))
    bg = _textured_dark((WIDTH, HEIGHT), (5, 5, 8), seed=17)
    fonts = {
        "title": load_font(tokens["font_bold"], 36),
        "label": load_font(tokens["font_bold"], 27),
        "small": load_font(tokens["font"], 24),
        "num": load_font(tokens["font_bold"], 30),
    }
    points = []
    for i in range(121):
        x = 120 + (WIDTH - 240) * i / 120
        y = HEIGHT / 2 + math.sin(i / 120 * math.pi * 2.2) * 150
        points.append((x, y))

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=(5, 5, 8))
        base = canvas.new_layer("bg", parallax=0.2)
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("path")
        d = ImageDraw.Draw(layer.image, "RGBA")
        frac = ease_in_out(clamp01(progress * 1.15))
        for wdt, alpha in ((14, 50), (7, 120)):
            progressive_polyline(d, points, frac, fill=tokens["accent"] + (alpha,), width=wdt)
        progressive_polyline(d, points, frac, fill=(255, 255, 255, 235), width=3)
        n = len(outcomes)
        for i, outcome in enumerate(outcomes):
            at = (i + 0.5) / n
            if frac < at:
                continue
            # Point on sine path at fraction `at`.
            idx = min(len(points) - 1, int(at * (len(points) - 1)))
            x, y = points[idx]
            above = (i % 2 == 0)
            d.ellipse([x - 30, y - 30, x + 30, y + 30], fill=tokens["bg"] + (255,), outline=tokens["accent"] + (255,), width=4)
            num = str(i + 1)
            nw = d.textlength(num, font=fonts["num"])
            d.text((x - nw / 2, y - 19), num, font=fonts["num"], fill=(255, 255, 255, 255))
            label = _clean(outcome.get("label"))[:34]
            result = _clean(outcome.get("outcome"))[:40]
            if above:
                d.line([x, y - 30, x, y - 78], fill=(255, 255, 255, 160), width=2)
                lw = d.textlength(label, font=fonts["label"])
                d.text((x - lw / 2, y - 138), label, font=fonts["label"], fill=(255, 255, 255, 255))
                if result:
                    rw = d.textlength(result, font=fonts["small"])
                    d.text((x - rw / 2, y - 102), result, font=fonts["small"], fill=tokens["accent"] + (255,))
            else:
                d.line([x, y + 30, x, y + 78], fill=(255, 255, 255, 160), width=2)
                lw = d.textlength(label, font=fonts["label"])
                d.text((x - lw / 2, y + 92), label, font=fonts["label"], fill=(255, 255, 255, 255))
                if result:
                    rw = d.textlength(result, font=fonts["small"])
                    d.text((x - rw / 2, y + 128), result, font=fonts["small"], fill=tokens["accent"] + (255,))
        top = canvas.new_layer("titles")
        td = ImageDraw.Draw(top.image, "RGBA")
        if title_left:
            td.text((90, 70), title_left.upper()[:40], font=fonts["title"], fill=(255, 255, 255, 255))
        if title_right:
            rw = td.textlength(title_right.upper(), font=fonts["title"])
            td.text((WIDTH - 90 - rw, 70), title_right.upper()[:40], font=fonts["title"], fill=tokens["accent"] + (255,))
        _footer(td, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("outcome_path", style_name, duration, sentence_index, scene_number,
                       {"outcomes": len(outcomes)})


# ---------------------------------------------------------------------------
# 6. Stat comparison
# ---------------------------------------------------------------------------

def stat_comparison(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    items = []
    for entry in data.get("items") or []:
        if not isinstance(entry, dict):
            continue
        try:
            value = float(entry.get("value"))
        except (TypeError, ValueError):
            continue
        items.append({"label": _clean(entry.get("label")) or "ITEM",
                      "value": value,
                      "display": _clean(entry.get("display"))})
    if not items:
        return None, {"reason": "no_numeric_items"}
    items = items[:6]
    title = _clean(data.get("title"))
    max_value = max(abs(i["value"]) for i in items) or 1.0
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=19)
    fonts = {
        "title": load_font(tokens["font_bold"], 42),
        "label": load_font(tokens["font"], 30),
        "value": load_font(tokens["font_bold"], 34),
    }

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("bars")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if title:
            d.text((110, 80), title.upper()[:60], font=fonts["title"], fill=tokens["text"] + (255,))
        y = 230
        for i, item in enumerate(items):
            local = ease_out(clamp01(progress * 1.6 - i * 0.18))
            d.text((110, y), item["label"][:36], font=fonts["label"], fill=tokens["text"] + (255,))
            bar_x, bar_max_w = 560, 1050
            d.rectangle([bar_x, y + 2, bar_x + bar_max_w, y + 36], fill=(255, 255, 255, 28))
            wdt = int(bar_max_w * (abs(item["value"]) / max_value) * local)
            colour = tokens["accent"] if i == 0 else tokens["accent2"]
            d.rectangle([bar_x, y + 2, bar_x + wdt, y + 36], fill=colour + (255,))
            if local >= 0.98 and item["display"]:
                shown = item["display"]
            else:
                shown = f"{item['value'] * local:,.0f}" if float(item["value"]).is_integer() else f"{item['value'] * local:,.1f}"
            d.text((bar_x + wdt + 22, y), shown[:24], font=fonts["value"], fill=(255, 255, 255, 255))
            y += 118
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("stat_comparison", style_name, duration, sentence_index, scene_number,
                       {"items": len(items), "title": bool(title)})


# ---------------------------------------------------------------------------
# 7. Document presentation
# ---------------------------------------------------------------------------

def document_presentation(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    body = _clean(data.get("body"))
    if not body:
        return None, {"reason": "no_document_text"}
    title = _clean(data.get("title")) or "DOCUMENT"
    source_label = _clean(data.get("source_label"))
    date_label = _clean(data.get("date_label"))
    highlight = _clean(data.get("highlight"))
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=23)
    fonts = {
        "title": load_font(tokens["font_bold"], 40),
        "small": load_font(tokens["font_bold"], 22),
        "body": load_font(tokens["font"], 31),
    }
    # Pre-wrap body once against a probe draw.
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    lines = []
    for para_line in wrap_text(probe, body, fonts["body"], 1180):
        lines.append(para_line)
    lines = lines[:13]

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"])
        base = canvas.new_layer("bg", parallax=0.2)
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("doc")
        d = ImageDraw.Draw(layer.image, "RGBA")
        rise = ease_out(clamp01(progress * 2.0))
        x0, y0, x1, y1 = 300, int(120 + (1 - rise) * 60), 1620, 960
        d.rectangle([x0 + 14, y0 + 18, x1 + 14, y1 + 18], fill=(0, 0, 0, 120))
        d.rectangle([x0, y0, x1, y1], fill=(246, 242, 230, 255))
        d.rectangle([x0, y0, x1, y0 + 6], fill=tokens["accent"] + (255,))
        header = "  ·  ".join(p for p in (source_label.upper() if source_label else "", date_label.upper() if date_label else "") if p)
        if header:
            d.text((x0 + 52, y0 + 30), header[:80], font=fonts["small"], fill=tokens["accent"] + (255,))
        d.text((x0 + 52, y0 + 72), title.upper()[:56], font=fonts["title"], fill=(28, 26, 22, 255))
        y = y0 + 156
        for line in lines:
            if highlight and highlight in line and progress > 0.35:
                start = line.find(highlight)
                prefix = line[:start]
                hx = x0 + 52 + d.textlength(prefix, font=fonts["body"])
                hw = d.textlength(highlight, font=fonts["body"]) * ease_in_out(clamp01((progress - 0.35) * 2.2))
                d.rectangle([hx - 4, y + 2, hx + hw + 4, y + 40], fill=(250, 214, 90, 190))
            d.text((x0 + 52, y), line, font=fonts["body"], fill=(36, 34, 30, 255))
            y += 48
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("document_presentation", style_name, duration, sentence_index, scene_number,
                       {"title": True, "source_label": bool(source_label),
                        "date_label": bool(date_label), "highlight": bool(highlight),
                        "body_chars": len(body)})


TEMPLATES = {
    "territory_map": territory_map,
    "evidence_board": evidence_board,
    "editorial_panel": editorial_panel,
    "data_gauge": data_gauge,
    "outcome_path": outcome_path,
    "stat_comparison": stat_comparison,
    "document_presentation": document_presentation,
}
