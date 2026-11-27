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
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from services.motion_engine import core, motion_registry
from services.motion_engine.core import (
    HEIGHT, WIDTH, clamp01, fit_contain, fit_cover,
    load_font, progressive_polyline, wrap_text,
)
from services.motion_engine.styles import resolve_style

# Phase 8 (motion-registry migration): easings resolve through the
# registry — the single named vocabulary — instead of importing
# core's helpers directly. The registry re-exports core's exact
# callables, so rendered motion is byte-for-byte unchanged. The
# templates' remaining numeric constants are progress-domain
# choreography multipliers (see the Phase 8 proposal for the
# per-template account of why they are not registry DURATIONS,
# which are seconds-domain layer-fade steps).
ease_in_out = motion_registry.easing_function("ease_in_out")
ease_out = motion_registry.easing_function("ease_out")


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


# Gate 4 (B4): shared label fitting for text-bearing templates.
SAFE_MARGIN = 38  # outer 2% of the 1920px frame


def _clamped_text_x(center_x, text_width):
    """Left x for text centred at center_x, clamped so the whole
    line stays inside the frame safe area."""
    return min(
        max(SAFE_MARGIN, center_x - text_width / 2),
        max(SAFE_MARGIN, WIDTH - SAFE_MARGIN - text_width),
    )


def _fit_label(draw, text, tokens, max_width=320, sizes=(27, 22, 19),
               max_lines=3, bold=True):
    """Wrap `text` at word boundaries, shrinking the font until
    the wrapped lines fit `max_lines`. Returns (lines, font).
    The full text is preserved across lines — never truncated,
    never split mid-word."""
    text = _clean(text)
    if not text:
        return [], load_font(tokens["font_bold"] if bold else tokens["font"],
                             sizes[0])
    font_path = tokens["font_bold"] if bold else tokens["font"]
    for size in sizes:
        font = load_font(font_path, size)
        lines = wrap_text(draw, text, font, max_width)
        if len(lines) <= max_lines:
            return lines, font
    font = load_font(font_path, sizes[-1])
    return wrap_text(draw, text, font, max_width), font


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


def _footer(draw, tokens, label=""):
    """Footer hook kept for call-site compatibility.

    Founder ruling 2026-11-23: NO branding or watermark text is
    burned into generated graphics — the historical
    "CINEMORA / GENERATED GRAPHIC" corner label is removed
    everywhere. A graphic's provenance lives in its asset record
    and the rights manifest, never in pixels. This hook therefore
    deliberately draws nothing; the ~35 template call sites keep
    working unchanged."""
    return None


# ---------------------------------------------------------------------------
# 1. Map family (territory_map + border/route/trade_route/military
# variants) — REMOVED completely by the founder's P0 ruling
# (2026-11-27): renderers + selection paths. The geography
# DATASET (motion_engine/geography.py) survives for the rebuild.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2. Evidence board / red-string board — REMOVED completely by the
# founder's P0 ruling (2026-11-27): templates + selection paths.
# The two helpers below survive: templates_v3/templates_v4 import
# them for the surviving newspaper treatments.
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
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
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
    # No placeholder label: the gauge's label line is drawn only
    # when the caller supplies a real (short, designed) label.
    label = _clean(data.get("label"))
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
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
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
        if label:
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

def _stat_bar_width(value, max_value, local, max_w=1050, min_w=30):
    """Bar width for stat_comparison. Proportional to the value,
    with a minimum visible stub for small-but-nonzero values: at
    extreme ratios (101 vs 2) a purely proportional bar is ~20px
    and reads as absent (v1 benchmark gap). The printed value next
    to the bar always carries the exact number, so the floor is a
    legibility device, not a data distortion. Zero stays zero."""
    if not value or not max_value:
        return 0
    proportional = int(max_w * (abs(value) / max_value) * local)
    return max(proportional, int(min_w * local))


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
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
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
            wdt = _stat_bar_width(item["value"], max_value, local, max_w=bar_max_w)
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

def _stamp(draw, layer, stamp_img, progress, page):
    """Composite the CLASSIFIED stamp onto the page (Wave 2):
    it lands at progress 0.35 with a stamp-in settle (oversized
    and faint, then full weight at its recorded angle)."""
    if stamp_img is None or progress <= 0.35:
        return
    land = clamp01((progress - 0.35) * 4.0)
    scale = 1.28 - 0.28 * ease_out(land)
    shown = stamp_img.resize(
        (int(stamp_img.size[0] * scale), int(stamp_img.size[1] * scale)),
        Image.BICUBIC)
    if land < 1.0:
        a = shown.getchannel("A").point(lambda v: int(v * land))
        shown.putalpha(a)
    cx = (page[0] + page[2]) // 2 - shown.size[0] // 2
    cy = (page[1] + page[3]) // 2 - shown.size[1] // 2
    layer.image.alpha_composite(shown, (cx, cy))


def document_presentation(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    body = _clean(data.get("body"))
    # Wave 1 (F4 unlock): a genuine retrieved document IMAGE —
    # a real document asset with recorded provenance, supplied
    # by the caller in data["image"] — is presented as itself on
    # the page (fitted, labelled with its recorded source). The
    # image path never substitutes for provenance: the pipeline
    # only assembles this data from verified document assets,
    # and the template refuses a named-but-missing file rather
    # than presenting anything else in its place.
    image_path = data.get("image")
    doc_image = None
    if image_path:
        if not Path(str(image_path)).exists():
            return None, {"reason": "document_image_missing"}
        try:
            doc_image = Image.open(str(image_path)).convert("RGB")
        except Exception:
            return None, {"reason": "document_image_unreadable"}
    if not body and doc_image is None:
        return None, {"reason": "no_document_text"}
    # Founder ruling 2026-11-23: no placeholder title and no
    # source label on the presented page — the title line is
    # drawn only when the caller supplies a real one, and the
    # header carries at most the date. Provenance lives in the
    # recorded document_source, never in pixels.
    title = _clean(data.get("title"))
    date_label = _clean(data.get("date_label"))
    highlight = _clean(data.get("highlight"))
    # Wave 2 (F4): the classified-file variant — the same genuine
    # document, presented under redaction bars and a CLASSIFIED
    # stamp. The variant changes the PRESENTATION only: the
    # document source requirement is the pipeline's, unchanged,
    # and the bars lift only part-way (the file stays redacted).
    variant = _clean(data.get("variant")).lower()
    if variant != "classified":
        variant = ""
    stamp = None
    if variant:
        stamp = Image.new("RGBA", (620, 150), (0, 0, 0, 0))
        sd = ImageDraw.Draw(stamp)
        sd.rectangle([6, 6, 614, 144], outline=(176, 32, 32, 255), width=7)
        stamp_font = load_font(tokens["font_bold"], 72)
        stamp_text = "CLASSIFIED"
        sw = sd.textlength(stamp_text, font=stamp_font)
        sd.text(((620 - sw) / 2, 34), stamp_text, font=stamp_font,
                fill=(176, 32, 32, 255))
        stamp = stamp.rotate(-12, expand=True, resample=Image.BICUBIC)
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
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg", parallax=0.2)
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("doc")
        d = ImageDraw.Draw(layer.image, "RGBA")
        rise = ease_out(clamp01(progress * 2.0))
        x0, y0, x1, y1 = 300, int(120 + (1 - rise) * 60), 1620, 960
        d.rectangle([x0 + 14, y0 + 18, x1 + 14, y1 + 18], fill=(0, 0, 0, 120))
        d.rectangle([x0, y0, x1, y1], fill=(246, 242, 230, 255))
        d.rectangle([x0, y0, x1, y0 + 6], fill=tokens["accent"] + (255,))
        header = date_label.upper() if date_label else ""
        if header:
            d.text((x0 + 52, y0 + 30), header[:80], font=fonts["small"], fill=tokens["accent"] + (255,))
        if title:
            d.text((x0 + 52, y0 + 72), title.upper()[:56], font=fonts["title"], fill=(28, 26, 22, 255))
        if doc_image is not None:
            # The genuine document, fitted inside the page under
            # its header; an accent frame sweeps around it as the
            # reveal completes (the "unlock" beat).
            area = (x0 + 52, y0 + 150, x1 - 52, y1 - 40)
            fitted = fit_contain(
                doc_image, (area[2] - area[0], area[3] - area[1]))
            ix = area[0] + (area[2] - area[0] - fitted.size[0]) // 2
            iy = area[1] + (area[3] - area[1] - fitted.size[1]) // 2
            shown = fitted.copy()
            reveal = ease_out(clamp01(progress * 1.6))
            if reveal < 1.0:
                a = shown.convert("RGBA")
                alpha = a.getchannel("A").point(
                    lambda v: int(v * reveal))
                a.putalpha(alpha)
                shown = a
            layer.image.alpha_composite(
                shown.convert("RGBA"), (ix, iy))
            frame_frac = ease_in_out(clamp01((progress - 0.3) * 1.8))
            if frame_frac > 0:
                fx0, fy0 = ix - 10, iy - 10
                fx1, fy1 = ix + fitted.size[0] + 10, iy + fitted.size[1] + 10
                perimeter = [
                    (fx0, fy0), (fx1, fy0), (fx1, fy1), (fx0, fy1),
                    (fx0, fy0),
                ]
                progressive_polyline(
                    d, perimeter, frame_frac,
                    fill=tokens["accent"] + (255,), width=5)
            if variant:
                # Redaction bars over the genuine page: they
                # thin as the "declassification" beat plays but
                # never lift entirely — the file stays redacted.
                lift = ease_in_out(clamp01((progress - 0.45) * 1.8))
                fw, fh = fitted.size
                for k in range(5):
                    by = iy + int(fh * (0.10 + 0.17 * k))
                    bh = max(6, int(fh * 0.075 * (1.0 - 0.4 * lift)))
                    bx1 = ix + int(fw * (0.55 + 0.09 * ((k * 37) % 4)))
                    d.rectangle([ix + 8, by, bx1, by + bh],
                                fill=(12, 12, 12, 255))
                _stamp(d, layer, stamp, progress, (x0, y0, x1, y1))
            _footer(d, tokens)
            return canvas.composite()
        y = y0 + 156
        for li, line in enumerate(lines):
            if highlight and highlight in line and progress > 0.35:
                start = line.find(highlight)
                prefix = line[:start]
                hx = x0 + 52 + d.textlength(prefix, font=fonts["body"])
                hw = d.textlength(highlight, font=fonts["body"]) * ease_in_out(clamp01((progress - 0.35) * 2.2))
                d.rectangle([hx - 4, y + 2, hx + hw + 4, y + 40], fill=(250, 214, 90, 190))
            d.text((x0 + 52, y), line, font=fonts["body"], fill=(36, 34, 30, 255))
            if variant and li % 2 == 0:
                # Redaction bar over the middle of alternating
                # body lines (deterministic by line index).
                lift = ease_in_out(clamp01((progress - 0.45) * 1.8))
                bx0 = x0 + 52 + int(140 + 60 * ((li * 29) % 3))
                bx1 = bx0 + int(560 * (1.0 - 0.3 * lift))
                d.rectangle([bx0, y + 4, bx1, y + 34],
                            fill=(12, 12, 12, 255))
            y += 48
        if variant:
            _stamp(d, layer, stamp, progress, (x0, y0, x1, y1))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    _doc_inputs = {"title": bool(title),
                   "date_label": bool(date_label), "highlight": bool(highlight),
                   "body_chars": len(body),
                   "image": doc_image is not None}
    if variant:
        # Recorded only when a variant actually rendered — a base
        # document_presentation's recorded inputs keep their
        # Wave 1 shape.
        _doc_inputs["variant"] = variant
    return path, _meta(data.get("_template") or "document_presentation",
                       style_name, duration, sentence_index, scene_number,
                       _doc_inputs)


# ---------------------------------------------------------------------------
# 7b. Classified file (Wave 2, F4) — a thin named wrapper over
# document_presentation: it fixes the "classified" variant so the
# Director and the packs can register/order it as its own
# template while the genuine-document requirement, provenance and
# honesty contract stay the base template's. The pipeline's
# document gate covers it exactly as it covers the base.
# ---------------------------------------------------------------------------

def classified_file(data, style, out_path, duration=5.0,
                    sentence_index=None, scene_number=None):
    data = dict(data or {})
    data["variant"] = "classified"
    data["_template"] = "classified_file"
    return document_presentation(
        data, style, out_path, duration=duration,
        sentence_index=sentence_index, scene_number=scene_number,
    )


# (Section 8, the closing card, was removed completely by the
# founder's P0 ruling, 2026-11-27 — template + every selection
# path. The T5 coverage guarantee no longer renders a floor
# graphic; see graphics_pipeline's rewired stage 6.)

TEMPLATES = {
    "editorial_panel": editorial_panel,
    "data_gauge": data_gauge,
    "stat_comparison": stat_comparison,
    "document_presentation": document_presentation,
    # Wave 2 (F4): the classified-file variant of the document
    # presentation, registered under its own name.
    "classified_file": classified_file,
}

# Stage 5B: the v2 template family (reference set 2) registers into
# this dict from templates_v2 itself (one lookup point for the
# graphics chain). Registration lives there — not in a bottom
# import here — so importing templates_v2 first cannot deadlock
# on a partially initialised module; the package __init__ imports
# both modules in order.
