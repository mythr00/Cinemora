"""Motion Engine templates v6 — Animation Studio Expansion
(2026-11-28): the PIL-engine treatments that close two of the
Motion Graphics programme's documented gaps (its FINAL-REPORT
§10), built to the same standard as the v5 family:

  * ``document_examination`` — gap P1 (suite item 08 PARTIAL):
    document_presentation *presents* a genuine document, but its
    settled text sits at ~10 px — it does not EXAMINE. This
    treatment presents the same genuine document (same pipeline
    gate: a verified document asset with recorded provenance,
    nothing else), then moves a virtual camera from the full
    page into ONE recorded region (the scenario's ``examine``
    contract: relative bounds + an optional short recorded
    label), settling with that region at full page width —
    legible size. The zoom is a true crop of the source image:
    no resynthesis, no invented detail; the settled frame shows
    exactly the recorded region's own pixels, enlarged.
  * ``title_sequence`` — gap P2 (suite item 02 PARTIAL):
    archival_title is a clean title CARD; this is the staged
    multi-beat SEQUENCE the audit asked for — kicker beat, rule
    beat, title reveal beat, subtitle/date settle beat — fed by
    the same fact-guarded assembled title data. It is proposed
    only for the opening beat and only when the record carries
    staging material (a kicker, subtitle or date label); a bare
    title stays archival_title's job.

Every template renders ONLY caller-supplied recorded data and
returns (None, {"reason": ...}) when its contract is unmet —
the engine's standing honesty discipline. Text is measured
through motion_engine.label_fit; overflow refuses, never clips.
Motion uses the motion_registry vocabulary (ease_out /
ease_in_out) — no bounce, spin or wipe defaults.
"""

from PIL import Image, ImageDraw

from services.motion_engine import core, motion_registry
from services.motion_engine.core import (
    HEIGHT, WIDTH, clamp01, fit_contain, load_font,
)
from services.motion_engine.label_fit import LABEL_FIT_REASON, fit_lines
from services.motion_engine.styles import resolve_style
from services.motion_engine.templates import (
    TEMPLATES, _clean, _footer, _meta, _textured_dark,
)

ease_out = motion_registry.easing_function("ease_out")
ease_in_out = motion_registry.easing_function("ease_in_out")


def _probe_draw():
    return ImageDraw.Draw(Image.new("RGBA", (10, 10)))


# ---------------------------------------------------------------------------
# Document examination — present, then zoom to the recorded region
# ---------------------------------------------------------------------------

def _examine_region(data):
    """The recorded examine contract, normalised — or a typed
    refusal reason. The region is relative to the document image
    (0..1 on each axis) so it survives any fitting the page does."""
    region = data.get("examine")
    if not isinstance(region, dict):
        return None, "examine_region_missing"
    try:
        x = float(region.get("x"))
        y = float(region.get("y"))
        w = float(region.get("w"))
        h = float(region.get("h"))
    except (TypeError, ValueError):
        return None, "examine_region_invalid"
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
            and 0.03 <= w <= 1.0 and 0.03 <= h <= 1.0
            and x + w <= 1.001 and y + h <= 1.001):
        return None, "examine_region_invalid"
    return {"x": x, "y": y, "w": w, "h": h}, None


def document_examination(data, style, out_path, duration=5.0,
                         sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    # The genuine-document gate, identical to document_presentation:
    # a named-but-missing file refuses; nothing is ever substituted.
    image_path = data.get("image")
    if not image_path:
        return None, {"reason": "document_image_missing"}
    from pathlib import Path

    if not Path(str(image_path)).exists():
        return None, {"reason": "document_image_missing"}
    try:
        doc_image = Image.open(str(image_path)).convert("RGB")
    except Exception:
        return None, {"reason": "document_image_unreadable"}
    region, reason = _examine_region(data)
    if region is None:
        return None, {"reason": reason}
    title = _clean(data.get("title"))
    date_label = _clean(data.get("date_label"))
    label = _clean(data.get("examine_label")) or _clean(
        (data.get("examine") or {}).get("label"))
    fonts = {
        "title": load_font(tokens["font_bold"], 40),
        "small": load_font(tokens["font_bold"], 22),
        "label": load_font(tokens["font_bold"], 30),
    }
    probe = _probe_draw()
    for _key, _text, _font, _width in (
        ("title", title.upper(), fonts["title"], 1216),
        ("date_label", date_label.upper(), fonts["small"], 1216),
        ("examine_label", label, fonts["label"], 1400),
    ):
        if _text and fit_lines(probe, _text, _font, _width,
                               max_lines=1) is None:
            return None, {"reason": LABEL_FIT_REASON,
                          "detail": f"{_key} does not fit {_width}px"}
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=23)
    iw, ih = doc_image.size
    # The recorded region in source pixels.
    rx0, ry0 = region["x"] * iw, region["y"] * ih
    rx1, ry1 = (region["x"] + region["w"]) * iw, \
        (region["y"] + region["h"]) * ih

    def draw_frame(t, progress):
        canvas = core.Canvas(
            bg=tokens["bg"],
            pattern=tokens.get("background_pattern"),
            pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg", parallax=0.2)
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("doc")
        d = ImageDraw.Draw(layer.image, "RGBA")
        rise = ease_out(clamp01(progress * 2.0))
        x0, y0, x1, y1 = 300, int(120 + (1 - rise) * 60), 1620, 960
        d.rectangle([x0 + 14, y0 + 18, x1 + 14, y1 + 18],
                    fill=(0, 0, 0, 120))
        d.rectangle([x0, y0, x1, y1], fill=(246, 242, 230, 255))
        d.rectangle([x0, y0, x1, y0 + 6], fill=tokens["accent"] + (255,))
        if date_label:
            d.text((x0 + 52, y0 + 30), date_label.upper()[:80],
                   font=fonts["small"], fill=tokens["accent"] + (255,))
        if title:
            d.text((x0 + 52, y0 + 72), title.upper()[:56],
                   font=fonts["title"], fill=(28, 26, 22, 255))
        area = (x0 + 52, y0 + 150, x1 - 52, y1 - 40)
        area_w, area_h = area[2] - area[0], area[3] - area[1]
        # Virtual camera: full page -> recorded region, eased over
        # the middle of the slot; settled for the final quarter.
        zoom = ease_in_out(clamp01((progress - 0.30) / 0.45))
        cx0 = rx0 * zoom
        cy0 = ry0 * zoom
        cx1 = iw - (iw - rx1) * zoom
        cy1 = ih - (ih - ry1) * zoom
        crop_box = (int(cx0), int(cy0), max(int(cx0) + 2, int(cx1)),
                    max(int(cy0) + 2, int(cy1)))
        crop = doc_image.crop(crop_box)
        fitted = fit_contain(crop, (area_w, area_h))
        dx = area[0] + (area_w - fitted.size[0]) // 2
        dy = area[1] + (area_h - fitted.size[1]) // 2
        layer.image.alpha_composite(fitted.convert("RGBA"), (dx, dy))
        # The region's outline, mapped through the camera: drawn
        # while the region is not yet the whole view, so the eye
        # follows the exact patch of page being examined.
        if zoom < 0.985:
            sx = fitted.size[0] / max(1, crop_box[2] - crop_box[0])
            sy = fitted.size[1] / max(1, crop_box[3] - crop_box[1])
            ox0 = dx + (rx0 - crop_box[0]) * sx
            oy0 = dy + (ry0 - crop_box[1]) * sy
            ox1 = dx + (rx1 - crop_box[0]) * sx
            oy1 = dy + (ry1 - crop_box[1]) * sy
            d.rectangle([ox0, oy0, ox1, oy1],
                        outline=tokens["accent"] + (255,), width=5)
        elif label:
            # Settled: the recorded label for the examined region,
            # under the page — a caption for the region, never a
            # sentence card (fit-checked above, one line).
            lw = d.textlength(label, font=fonts["label"])
            d.text(((WIDTH - lw) / 2, y1 + 26), label,
                   font=fonts["label"], fill=tokens["text"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    # Legibility evidence: how much larger the region reads in the
    # settled frame than in the full-page presentation.
    full_fit = fit_contain(doc_image, (1216, 650))
    settled_fit = fit_contain(
        doc_image.crop((int(rx0), int(ry0), int(rx1), int(ry1))),
        (1216, 650))
    zoom_factor = round(
        (settled_fit.size[0] / max(1, region["w"] * full_fit.size[0])), 2)
    return path, _meta(
        "document_examination", style_name, duration,
        sentence_index, scene_number,
        {"image": True, "examine_region": region,
         "label": bool(label), "zoom_factor": zoom_factor})


# ---------------------------------------------------------------------------
# Title sequence — the staged multi-beat opening title
# ---------------------------------------------------------------------------

def title_sequence(data, style, out_path, duration=5.0,
                   sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    title = _clean(data.get("title"))
    if not title:
        return None, {"reason": "no_title"}
    subtitle = _clean(data.get("subtitle"))
    date_label = _clean(data.get("date_label"))
    kicker = _clean(data.get("kicker"))
    if not (kicker or subtitle or date_label):
        # A sequence needs stages: with nothing but a title there
        # is nothing to stage — archival_title's card is the
        # right treatment and this refusal lets the chain use it.
        return None, {"reason": "title_sequence_needs_stages"}
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=71)
    fonts = {
        "title": load_font(tokens["font_bold"], 84),
        "subtitle": load_font(tokens["font"], 32),
        "kicker": load_font(tokens["font_bold"], 26),
        "date": load_font(tokens["font_bold"], 30),
    }
    probe = _probe_draw()
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
        canvas = core.Canvas(
            bg=tokens["bg"],
            pattern=tokens.get("background_pattern"),
            pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg", parallax=0.3)
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("title")
        d = ImageDraw.Draw(layer.image, "RGBA")
        cx = WIDTH // 2
        total_h = len(title_lines) * 104 + (54 if subtitle else 0) \
            + (86 if date_label else 0) + (50 if kicker else 0)
        y = (HEIGHT - total_h) // 2
        # Beat 1 (0.00–0.30): the kicker — the sequence's first
        # staged element, alone on the ground.
        if kicker:
            a = ease_out(clamp01((progress - 0.02) / 0.22))
            kw = d.textlength(kicker.upper(), font=fonts["kicker"])
            d.text((cx - kw / 2, y + int((1 - a) * 14)), kicker.upper(),
                   font=fonts["kicker"],
                   fill=tokens["accent"] + (int(255 * a),))
            y += 50
        # Beat 2 (0.28–0.68): the title lines rise in, staggered;
        # the rule grows beneath them as they land.
        title_top = y
        for i, line in enumerate(title_lines):
            a = ease_out(clamp01((progress - 0.28 - i * 0.09) / 0.30))
            lw = d.textlength(line, font=fonts["title"])
            d.text((cx - lw / 2, y + int((1 - a) * 30)), line,
                   font=fonts["title"],
                   fill=tokens["text"] + (int(255 * a),))
            y += 104
        rule_a = ease_in_out(clamp01((progress - 0.45) / 0.25))
        rule_w = int(180 * rule_a)
        if rule_w > 0:
            d.rectangle([cx - rule_w // 2, y + 6, cx + rule_w // 2,
                         y + 12], fill=tokens["accent"] + (255,))
        y += 44
        # Beat 3 (0.66–1.00): subtitle, then the date chip settles.
        if subtitle:
            a = ease_out(clamp01((progress - 0.66) / 0.20))
            sw = d.textlength(subtitle, font=fonts["subtitle"])
            d.text((cx - sw / 2, y + int((1 - a) * 12)), subtitle,
                   font=fonts["subtitle"],
                   fill=tokens["muted"] + (int(255 * a),))
            y += 54
        if date_label:
            a = ease_out(clamp01((progress - 0.78) / 0.20))
            dw = d.textlength(date_label.upper(), font=fonts["date"])
            pad = 26
            alpha = int(255 * a)
            d.rectangle([cx - dw / 2 - pad, y, cx + dw / 2 + pad,
                         y + 56],
                        outline=tokens["accent2"] + (alpha,), width=3)
            d.text((cx - dw / 2, y + 10), date_label.upper(),
                   font=fonts["date"],
                   fill=tokens["text"] + (alpha,))
        _footer(d, tokens)
        camera = {"scale": 1.0 + 0.05 * progress}
        return canvas.composite(camera=camera)

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta(
        "title_sequence", style_name, duration,
        sentence_index, scene_number,
        {"title": True, "subtitle": bool(subtitle),
         "date_label": bool(date_label), "kicker": bool(kicker),
         "stages": 3})


TEMPLATES.update({
    "document_examination": document_examination,
    "title_sequence": title_sequence,
})
