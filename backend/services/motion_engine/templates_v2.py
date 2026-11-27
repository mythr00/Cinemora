"""Motion Engine templates v2 (Stage 5B — founder reference set 2).

Seven more templates in the @smpkta engineering-documentary motion
language: split photo + dark panel with bullets revealing one by
one, a scan-point grid sweeping a textured surface, photo cards on
a drawn spine, glowing ring badges, an animated technical
cross-section, a proper bar chart with axis and units, and
satellite-style annotation over a caller image.

Same contract as v1: every template renders ONLY caller-supplied
data and returns (None, {"reason": ...}) when its data
requirements are not met. Signature and metadata shape are
identical to services.motion_engine.templates (v1).
"""

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from services.motion_engine import core, motion_registry
from services.motion_engine.core import (
    HEIGHT, WIDTH, clamp01, fit_cover, load_font,
    progressive_polyline, wrap_text,
)
from services.motion_engine.styles import resolve_style

# Phase 8 (motion-registry migration): easings resolve through
# the registry, as in templates.py — same callables, unchanged
# rendering; progress-domain choreography multipliers stay
# template-internal by design (Phase 8 proposal).
ease_in_out = motion_registry.easing_function("ease_in_out")
ease_out = motion_registry.easing_function("ease_out")
from services.motion_engine.templates import (
    _clean, _dark_photo, _footer, _meta, _textured_dark,
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


# ---------------------------------------------------------------------------
# 8. Split panel — photo + dark side panel, bullets reveal one by one
# ---------------------------------------------------------------------------

def split_panel(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    photo = _load_image(data.get("photo"))
    if photo is None:
        return None, {"reason": "no_photo"}
    title = _clean(data.get("title"))
    if not title:
        return None, {"reason": "no_title"}
    bullets = [_clean(b) for b in (data.get("bullets") or []) if _clean(b)]
    if not bullets:
        return None, {"reason": "no_bullets"}
    bullets = bullets[:6]
    caption = _clean(data.get("caption"))
    photo_w = int(WIDTH * 0.52)
    cover = fit_cover(photo, (photo_w, HEIGHT))
    fonts = {
        "title": load_font(tokens["font_bold"], 46),
        "bullet": load_font(tokens["font"], 31),
        "small": load_font(tokens["font"], 22),
    }
    probe = _probe_draw()
    title_lines = wrap_text(probe, title.upper(), fonts["title"], WIDTH - photo_w - 170)[:3]
    bullet_lines = [
        wrap_text(probe, b, fonts["bullet"], WIDTH - photo_w - 210)[:3]
        for b in bullets
    ]

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
        photo_layer = canvas.new_layer("photo")
        # Subtle Ken Burns: slow push-in on the still.
        zoom = 1.0 + 0.07 * ease_in_out(progress)
        zw, zh = int(photo_w * zoom), int(HEIGHT * zoom)
        zoomed = cover.resize((zw, zh), Image.LANCZOS)
        photo_layer.image.paste(zoomed.convert("RGBA"), (-(zw - photo_w) // 2, -(zh - HEIGHT) // 2))
        layer = canvas.new_layer("panel")
        d = ImageDraw.Draw(layer.image, "RGBA")
        d.rectangle([photo_w, 0, WIDTH, HEIGHT], fill=tokens["bg"] + (255,))
        d.rectangle([photo_w, 0, photo_w + 4, HEIGHT], fill=tokens["accent2"] + (255,))
        tx = photo_w + 72
        y = 150
        for line in title_lines:
            d.text((tx, y), line, font=fonts["title"], fill=tokens["text"] + (255,))
            y += 58
        d.rectangle([tx, y + 8, tx + 120, y + 13], fill=tokens["accent2"] + (255,))
        y += 56
        for i, lines in enumerate(bullet_lines):
            local = ease_out(clamp01(progress * 2.6 - 0.45 - i * 0.42))
            if local <= 0:
                continue
            alpha = int(255 * local)
            slide = int((1 - local) * 26)
            d.ellipse([tx + slide, y + 12, tx + slide + 10, y + 22],
                      fill=tokens["accent2"] + (alpha,))
            for line in lines:
                d.text((tx + 34 + slide, y), line, font=fonts["bullet"],
                       fill=tokens["text"] + (alpha,))
                y += 42
            y += 26
        if caption:
            d.text((tx, HEIGHT - 84), caption[:64], font=fonts["small"],
                   fill=tokens["muted"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("split_panel", style_name, duration, sentence_index, scene_number,
                       {"photo": True, "bullets": len(bullets), "title": True})


# ---------------------------------------------------------------------------
# 9. Scan grid — dot grid sweeping a textured surface
# ---------------------------------------------------------------------------

def _rock_texture(size, seed=31):
    img = Image.effect_noise(size, 26).convert("L")
    base = Image.new("RGB", size, (104, 86, 62))
    noise_rgb = Image.merge("RGB", (img, img, img))
    img = Image.blend(base, noise_rgb, 0.16)
    rng = random.Random(seed)
    d = ImageDraw.Draw(img)
    for _ in range(2600):
        x, y = rng.randrange(size[0]), rng.randrange(size[1])
        shade = rng.choice([(88, 72, 50), (124, 104, 76), (96, 78, 54)])
        d.point((x, y), fill=shade)
    return img


def scan_grid(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    title = _clean(data.get("title"))
    caption = _clean(data.get("caption"))
    report_label = _clean(data.get("report_label"))
    image = _load_image(data.get("image"))
    if image is None and not title and not caption:
        return None, {"reason": "no_scan_content"}
    if image is not None:
        bg = fit_cover(image, (WIDTH, HEIGHT))
        black = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        bg = Image.blend(bg, black, 0.25)
    else:
        bg = _rock_texture((WIDTH, HEIGHT))
    grid = data.get("grid") or {}
    cols = max(2, min(24, int(grid.get("cols") or 10)))
    rows = max(2, min(20, int(grid.get("rows") or 8)))
    gx0, gx1 = int(WIDTH * 0.30), int(WIDTH * 0.74)
    gy0, gy1 = int(HEIGHT * 0.30), int(HEIGHT * 0.80)
    fonts = {
        "title": load_font(tokens["font_bold"], 44),
        "caption": load_font(tokens["font"], 27),
        "small": load_font(tokens["font_bold"], 20),
    }
    probe = _probe_draw()
    title_lines = wrap_text(probe, title.upper(), fonts["title"], 900)[:2] if title else []

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("scan")
        d = ImageDraw.Draw(layer.image, "RGBA")
        sweep = ease_in_out(clamp01(progress * 1.25))
        scan_x = gx0 + (gx1 - gx0) * sweep
        # Leading scan band.
        d.rectangle([scan_x - 3, gy0 - 26, scan_x + 3, gy1 + 26],
                    fill=tokens["accent2"] + (110,))
        for c in range(cols):
            cx = gx0 + (gx1 - gx0) * (c / (cols - 1))
            if cx > scan_x:
                continue
            leading = (scan_x - cx) < 46
            r = 7 if leading else 5
            for r_i in range(rows):
                cy = gy0 + (gy1 - gy0) * (r_i / (rows - 1))
                d.ellipse([cx - r, cy - r, cx + r, cy + r],
                          fill=tokens["accent2"] + (255 if leading else 205,))
        tx, ty = 300, 300
        if title_lines:
            d.rectangle([tx - 22, ty, tx - 14, ty + len(title_lines) * 54 + 44],
                        fill=tokens["accent2"] + (255,))
            for line in title_lines:
                d.text((tx, ty), line, font=fonts["title"], fill=(255, 255, 255, 255))
                ty += 54
            ty += 8
        if caption:
            d.text((tx, ty), caption[:72], font=fonts["caption"],
                   fill=(238, 238, 232, 235))
        if report_label:
            d.text((300, HEIGHT - 120), report_label.upper()[:64],
                   font=fonts["small"], fill=(255, 255, 255, 140))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("scan_grid", style_name, duration, sentence_index, scene_number,
                       {"image": image is not None, "cols": cols, "rows": rows,
                        "title": bool(title), "caption": bool(caption)})


# ---------------------------------------------------------------------------
# 10. Photo node diagram — labelled photo cards on a drawn spine
# ---------------------------------------------------------------------------

def photo_node_diagram(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    nodes = []
    for entry in data.get("nodes") or []:
        if not isinstance(entry, dict):
            continue
        img = _load_image(entry.get("path"))
        if img is None:
            continue
        nodes.append((img, _clean(entry.get("label"))))
    if len(nodes) < 2:
        return None, {"reason": "insufficient_nodes"}
    nodes = nodes[:4]
    title = _clean(data.get("title"))
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=37)
    fonts = {
        "title": load_font(tokens["font_bold"], 40),
        "label": load_font(tokens["font_bold"], 27),
    }
    card_w, card_h = 400, 300
    built = [fit_cover(img, (card_w, card_h)) for img, _ in nodes]
    spine_y = int(HEIGHT * 0.52)
    n = len(nodes)
    centres = [int((i + 0.5) * WIDTH / n) for i in range(n)]

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("diagram")
        d = ImageDraw.Draw(layer.image, "RGBA")
        spine_frac = ease_in_out(clamp01(progress * 1.5))
        progressive_polyline(
            d, [(70, spine_y), (WIDTH - 70, spine_y)], spine_frac,
            fill=tokens["accent"] + (255,), width=8)
        for i, (card, (_, label)) in enumerate(zip(built, nodes)):
            cx = centres[i]
            local = ease_out(clamp01(progress * 2.6 - 0.35 - i * 0.5))
            if local <= 0:
                continue
            above = (i % 2 == 0)
            card_bottom = spine_y - 46 if above else spine_y + 46 + card_h
            card_top = card_bottom - card_h
            # Connector tick from spine to card.
            tick_end = card_bottom if above else card_top
            d.line([cx, spine_y, cx, tick_end], fill=tokens["accent"] + (255,), width=8)
            scale = 0.86 + 0.14 * local
            cw, ch = int(card_w * scale), int(card_h * scale)
            shown = card.resize((cw, ch), Image.LANCZOS).convert("RGBA")
            if local < 1.0:
                a = shown.getchannel("A").point(lambda v: int(v * local))
                shown.putalpha(a)
            x = cx - cw // 2
            y = card_bottom - ch if above else card_top + (card_h - ch)
            layer.image.alpha_composite(shown, (x, y))
            d.rectangle([x, y, x + cw, y + ch], outline=(255, 255, 255, 200), width=3)
            if label:
                lw = d.textlength(label.upper(), font=fonts["label"])
                ly = y - 44 if above else y + ch + 14
                d.text((cx - lw / 2, ly), label.upper()[:40], font=fonts["label"],
                       fill=tokens["text"] + (255,))
        if title:
            td = ImageDraw.Draw(layer.image, "RGBA")
            td.text((90, 64), title.upper()[:56], font=fonts["title"],
                    fill=tokens["text"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("photo_node_diagram", style_name, duration, sentence_index, scene_number,
                       {"nodes": len(nodes), "title": bool(title)})


# ---------------------------------------------------------------------------
# 11. Ring badges — glowing rings with big values on dark ground
# ---------------------------------------------------------------------------

def ring_badges(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    badges = []
    for entry in data.get("badges") or []:
        if not isinstance(entry, dict):
            continue
        value = _clean(entry.get("value") or entry.get("text"))
        label = _clean(entry.get("label"))
        if value:
            badges.append({"value": value, "label": label})
    if not badges:
        return None, {"reason": "no_badges"}
    badges = badges[:3]
    title = _clean(data.get("title"))
    bg = _textured_dark((WIDTH, HEIGHT), (6, 6, 8), seed=41)
    fonts = {
        "title": load_font(tokens["font_bold"], 40),
        "value": load_font(tokens["font_bold"], 76),
        "label": load_font(tokens["font_bold"], 34),
    }
    probe = _probe_draw()
    n = len(badges)
    radius = 235 if n == 1 else 205
    cy = HEIGHT // 2 + 20

    # Labels live INSIDE the ring: wrap them to the ring's inner
    # width (shrinking the font until they fit) — a long label
    # drawn on one line spills past the ring on both sides, as the
    # benchmark's "RELIEF ATTEMPTS REPELLED" badge showed.
    label_layout = []
    for badge in badges:
        lines, font = [], fonts["label"]
        if badge["label"]:
            text = badge["label"].upper()[:52]
            for size in (34, 29, 24):
                font = load_font(tokens["font_bold"], size)
                lines = wrap_text(probe, text, font, radius * 1.5)[:2]
                if all(probe.textlength(line, font=font) <= radius * 1.6
                       for line in lines):
                    break
        label_layout.append((lines, font))

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=(6, 6, 8))
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("badges")
        d = ImageDraw.Draw(layer.image, "RGBA")
        for i, badge in enumerate(badges):
            cx = int((i + 0.5) * WIDTH / n)
            local = ease_in_out(clamp01(progress * 1.7 - i * 0.3))
            if local <= 0:
                continue
            box = [cx - radius, cy - radius, cx + radius, cy + radius]
            # Glow: stacked arcs at growing widths / falling alpha.
            for wdt, alpha in ((26, 26), (14, 52), (6, 110)):
                d.arc(box, start=-90, end=-90 + 360 * local,
                      fill=(255, 255, 255, alpha), width=wdt)
            d.arc(box, start=-90, end=-90 + 360 * local,
                  fill=(255, 255, 255, 255), width=3)
            if local > 0.55:
                lines = wrap_text(probe, badge["value"].upper(), fonts["value"], radius * 1.5)[:2]
                label_lines, label_font = label_layout[i]
                total_h = len(lines) * 88 + len(label_lines) * 42
                y = cy - total_h // 2
                for line in lines:
                    lw = d.textlength(line, font=fonts["value"])
                    d.text((cx - lw / 2, y), line, font=fonts["value"],
                           fill=(255, 255, 255, 255))
                    y += 88
                for line in label_lines:
                    lw = d.textlength(line, font=label_font)
                    d.text((cx - lw / 2, y), line, font=label_font,
                           fill=tokens["accent2"] + (255,))
                    y += 42
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, 74), title.upper()[:56], font=fonts["title"],
                   fill=tokens["text"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("ring_badges", style_name, duration, sentence_index, scene_number,
                       {"badges": len(badges), "title": bool(title)})


# ---------------------------------------------------------------------------
# 12. Cross section — layered side-view schematic from caller layer data
# ---------------------------------------------------------------------------

def cross_section(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    layers = []
    for entry in data.get("layers") or []:
        if not isinstance(entry, dict):
            continue
        raw_depth = entry.get("depth")
        try:
            depth = 1.0 if raw_depth is None else float(raw_depth)
        except (TypeError, ValueError):
            depth = 1.0
        if depth <= 0:
            continue
        colour = entry.get("color")
        if isinstance(colour, (list, tuple)) and len(colour) >= 3:
            colour = tuple(int(c) for c in colour[:3])
        else:
            colour = None
        layers.append({
            "label": _clean(entry.get("label")),
            "depth": depth,
            "color": colour,
            "kind": _clean(entry.get("kind")).lower(),
        })
    if not layers:
        return None, {"reason": "no_layer_data"}
    # Wave 1 (F5): schematic variants on the same layer data —
    # "exploded" (layers separate with leader-labelled gaps),
    # "cutaway" (layers clipped inside a drawn machine housing),
    # "blueprint" (line-work rendering: outlined, hatched bands
    # on blueprint ground). The variant changes the DRAWING only;
    # layers/markers/headline stay caller-supplied.
    variant = _clean(data.get("variant")).lower()
    if variant not in ("exploded", "cutaway", "blueprint"):
        variant = ""
    markers = [m for m in (data.get("markers") or []) if isinstance(m, dict)]
    headline = data.get("headline") if isinstance(data.get("headline"), dict) else {}
    try:
        head_value = float(headline.get("value"))
    except (TypeError, ValueError):
        head_value = None
    head_suffix = _clean(headline.get("suffix"))
    head_label = _clean(headline.get("label"))
    title = _clean(data.get("title"))
    palette = [(74, 116, 158), (52, 88, 128), (122, 96, 62), (84, 66, 44), (60, 58, 54)]
    total_depth = sum(l["depth"] for l in layers)
    diag_top, diag_bottom = 250, HEIGHT - 90
    fonts = {
        "big": load_font(tokens["font_bold"], 92),
        "head_label": load_font(tokens["font_bold"], 34),
        "layer": load_font(tokens["font_bold"], 30),
        "small": load_font(tokens["font_bold"], 21),
        "title": load_font(tokens["font_bold"], 38),
    }
    # Precompute band boundaries.
    bands = []
    y = float(diag_top)
    for i, layer_def in enumerate(layers):
        h = (diag_bottom - diag_top) * layer_def["depth"] / total_depth
        bands.append((y, y + h, layer_def, layer_def["color"] or palette[i % len(palette)]))
        y += h

    def _ship(d, cx, water_y, scale=1.6):
        # Light silhouette so the marker reads against both the
        # water band below and the dark ground above.
        hull = [(cx - 90 * scale, water_y), (cx + 90 * scale, water_y),
                (cx + 62 * scale, water_y + 26 * scale), (cx - 62 * scale, water_y + 26 * scale)]
        d.polygon(hull, fill=(196, 200, 208, 255))
        d.rectangle([cx - 40 * scale, water_y - 24 * scale, cx + 30 * scale, water_y],
                    fill=(196, 200, 208, 255))
        d.rectangle([cx + 2 * scale, water_y - 46 * scale, cx + 12 * scale, water_y - 24 * scale],
                    fill=(196, 200, 208, 255))
        d.line([cx - 58 * scale, water_y - 52 * scale, cx - 58 * scale, water_y],
               fill=(196, 200, 208, 255), width=max(2, int(4 * scale)))

    # Exploded view: bands drift apart from the stack centre.
    n_bands = len(bands)
    mid_band = (n_bands - 1) / 2.0

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg")
        if variant == "blueprint":
            # Blueprint ground: the theme's own blue when the
            # production theme IS technical_blueprint, else the
            # canonical blueprint blue — line work on blue either
            # way, never the caller's data restyled as fact.
            bp_bg = tokens["bg"] if style_name == "technical_blueprint" else (10, 42, 84)
            bg_img = Image.new("RGB", (WIDTH, HEIGHT), bp_bg)
            gd = ImageDraw.Draw(bg_img)
            grid_colour = tuple(min(255, c + 26) for c in bp_bg)
            for gx in range(0, WIDTH, 80):
                gd.line([gx, 0, gx, HEIGHT], fill=grid_colour, width=1)
            for gy in range(0, HEIGHT, 80):
                gd.line([0, gy, WIDTH, gy], fill=grid_colour, width=1)
            base.image = bg_img.convert("RGBA")
        else:
            base.image = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=43).convert("RGBA")
        layer = canvas.new_layer("section")
        d = ImageDraw.Draw(layer.image, "RGBA")
        explode_frac = ease_in_out(clamp01(progress * 1.5 - 0.25)) if variant == "exploded" else 0.0
        band_offsets = [
            (i - mid_band) * 54 * explode_frac for i in range(n_bands)
        ]
        if variant == "cutaway":
            # Machine housing: a filled casing ring around the
            # section window, drawn under the bands; the bands
            # themselves are clipped to the window below.
            d.rectangle([64, 196, WIDTH - 64, HEIGHT - 36],
                        fill=tokens["surface"] + (255,))
            d.rectangle([64, 196, WIDTH - 64, HEIGHT - 36],
                        outline=(255, 255, 255, 200), width=4)
            d.rectangle([150, 250, WIDTH - 150, HEIGHT - 90],
                        fill=tokens["bg"] + (255,))
            for bx, by in ((110, 232), (WIDTH - 110, 232),
                           (110, HEIGHT - 72), (WIDTH - 110, HEIGHT - 72)):
                d.ellipse([bx - 13, by - 13, bx + 13, by + 13],
                          outline=(255, 255, 255, 220), width=3)
                d.line([bx - 7, by, bx + 7, by], fill=(255, 255, 255, 220), width=3)
        for i, (y0, y1, layer_def, colour) in enumerate(bands):
            local = ease_out(clamp01(progress * 1.8 - i * 0.22))
            if local <= 0:
                continue
            y0d, y1d = y0 + band_offsets[i], y1 + band_offsets[i]
            if variant == "cutaway":
                x0d = 150
                x1 = 150 + (WIDTH - 300) * local
            else:
                x0d = 110
                x1 = 110 + (WIDTH - 220) * local
            if variant == "blueprint":
                d.rectangle([x0d, y0d, x1, y1d], outline=(240, 246, 255, 255), width=3)
                # Hatch the band interior (clipped by construction:
                # segments computed inside the band rect).
                hatch = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
                hd = ImageDraw.Draw(hatch)
                step = 30
                band_h = y1d - y0d
                for hx in range(int(x0d - band_h), int(x1), step):
                    hd.line([hx, y1d, hx + band_h, y0d],
                            fill=(180, 210, 245, 90), width=2)
                band_mask = Image.new("L", (WIDTH, HEIGHT), 0)
                ImageDraw.Draw(band_mask).rectangle(
                    [x0d, y0d, x1, y1d], fill=255)
                layer.image.paste(
                    hatch, (0, 0),
                    Image.composite(hatch, Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0)), band_mask))
                d = ImageDraw.Draw(layer.image, "RGBA")
            else:
                d.rectangle([x0d, y0d, x1, y1d], fill=colour + (255,))
            if layer_def["kind"] == "water":
                # Wavy surface line on the water band's top edge.
                pts = []
                for x in range(int(x0d), int(x1), 14):
                    pts.append((x, y0d + math.sin(x / 46.0) * 4))
                if len(pts) >= 2:
                    d.line(pts, fill=(190, 220, 245, 220), width=3)
            if layer_def["label"] and local > 0.6 and (y1 - y0) > 44:
                # The label rides its band (y0d already carries
                # the exploded offset), so a separated band stays
                # labelled in place — no leader lines: the bands
                # span nearly the full width, leaving no margin
                # for a leader to land in.
                d.text((x0d + 30, y0d + (y1 - y0) / 2 - 19), layer_def["label"][:44],
                       font=fonts["layer"], fill=(255, 255, 255, 235))
        for marker in markers:
            _mx0, _mx1 = (150, WIDTH - 150) if variant == "cutaway" else (110, WIDTH - 110)
            try:
                mx = _mx0 + (_mx1 - _mx0) * float(marker.get("x") or 0.5)
            except (TypeError, ValueError):
                mx = WIDTH / 2
            layer_idx = marker.get("layer")
            if isinstance(layer_idx, int) and 0 <= layer_idx < len(bands):
                my = bands[layer_idx][0] + band_offsets[layer_idx]
            else:
                try:
                    my = diag_top + (diag_bottom - diag_top) * float(marker.get("y") or 0.5)
                except (TypeError, ValueError):
                    my = (diag_top + diag_bottom) / 2
            if progress < 0.45:
                continue
            if _clean(marker.get("kind")).lower() == "ship":
                _ship(d, mx, my)
            else:
                d.ellipse([mx - 9, my - 9, mx + 9, my + 9], fill=(255, 255, 255, 255))
            mlabel = _clean(marker.get("label"))
            if mlabel:
                lw = d.textlength(mlabel, font=fonts["small"])
                # A marker on the top band labels BELOW its glyph —
                # above the band is the headline zone and the two
                # texts collide there.
                label_y = my + 48 if my <= diag_top + 4 else my - 66
                d.text((mx - lw / 2, label_y), mlabel[:36], font=fonts["small"],
                       fill=(255, 255, 255, 255))
        top = canvas.new_layer("headline")
        td = ImageDraw.Draw(top.image, "RGBA")
        if title:
            # The title always renders (top-left); the count-up
            # headline is centred and does not replace it.
            td.text((110, 84), title.upper()[:56], font=fonts["title"],
                    fill=tokens["text"] + (255,))
        if head_value is not None:
            shown = head_value * ease_out(clamp01(progress * 1.4))
            text = f"{shown:,.0f}{head_suffix}"
            tw = td.textlength(text, font=fonts["big"])
            td.text(((WIDTH - tw) / 2, 66), text, font=fonts["big"],
                    fill=(255, 255, 255, 255))
            if head_label:
                lw = td.textlength(head_label.upper(), font=fonts["head_label"])
                td.text(((WIDTH - lw) / 2, 172), head_label.upper()[:48],
                        font=fonts["head_label"], fill=tokens["accent2"] + (255,))
        stamp = "SCHEMATIC — NOT TO SCALE"
        sw = td.textlength(stamp, font=fonts["small"])
        td.text((WIDTH - 110 - sw, 84), stamp, font=fonts["small"],
                fill=tokens["accent"] + (255,))
        _footer(td, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta(data.get("_template") or "cross_section",
                       style_name, duration, sentence_index, scene_number,
                       {"layers": len(layers), "markers": len(markers),
                        "schematic": True, "headline": head_value is not None,
                        "variant": variant or "base"})


# ---------------------------------------------------------------------------
# 12b. Schematic variants (Wave 1, F5) — named wrappers over
# cross_section: each fixes the variant so the Director and the
# packs can register/order them as distinct templates.
# ---------------------------------------------------------------------------

def _section_variant(variant, template_name):
    def _render(data, style, out_path, duration=5.0,
                sentence_index=None, scene_number=None):
        data = dict(data or {})
        data["variant"] = variant
        data["_template"] = template_name
        return cross_section(
            data, style, out_path, duration=duration,
            sentence_index=sentence_index, scene_number=scene_number,
        )

    _render.__name__ = template_name
    return _render


exploded_view = _section_variant("exploded", "exploded_view")
# Phase 9 final fix (2026-11-27): mechanical_cutaway REMOVED as a
# selectable treatment. The creative test suite's item-11 inspection
# found it rendered the same layered band stack as cross_section
# inside a bolted frame — no mechanism, nothing cut away — under a
# name that promises a cutaway: misleading presentation. Following
# the programme's P0 pattern it is unproducible until a real cutaway
# exists; the treatment survives only as a specification in the
# programme record (Level 2/3, requires recorded subject geometry
# the scenario contract does not yet carry). Removal pinned in
# tests/test_phase9_final_fixes.py. cross_section itself is
# untouched: it honestly labels itself "SCHEMATIC — NOT TO SCALE".
blueprint_section = _section_variant("blueprint", "blueprint_section")


# ---------------------------------------------------------------------------
# 13. Bar chart — axis, units, animated growth (multi-value comparisons)
# ---------------------------------------------------------------------------

def _nice_max(value):
    if value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    fraction = value / (10 ** exponent)
    for step in (1.0, 1.25, 1.6, 2.0, 2.5, 4.0, 5.0, 8.0, 10.0):
        if fraction <= step:
            return step * (10 ** exponent)
    return 10.0 * (10 ** exponent)


def bar_chart(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
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
    items = items[:8]
    title = _clean(data.get("title"))
    units = _clean(data.get("units"))
    axis_max = _nice_max(max(abs(i["value"]) for i in items))
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=47)
    fonts = {
        "title": load_font(tokens["font_bold"], 44),
        "label": load_font(tokens["font"], 29),
        "value": load_font(tokens["font_bold"], 32),
        "tick": load_font(tokens["font"], 22),
    }

    def _blend(c1, c2, f):
        return tuple(int(a + (b - a) * f) for a, b in zip(c1, c2))

    palette = [
        tokens["accent"], tokens["accent2"],
        _blend(tokens["accent"], (255, 255, 255), 0.35),
        _blend(tokens["accent2"], (255, 255, 255), 0.35),
        tokens["muted"],
    ]
    x0, x1 = 330, WIDTH - 190
    top = 235
    row_h = min(118, (HEIGHT - top - 170) // max(1, len(items)))
    axis_y = top + row_h * len(items) + 26

    # Row labels are right-aligned into the column left of x0; a
    # long label at full size would run off the canvas edge (seen
    # in benchmark v2: "Relief attempts repelled" clipped). Fit
    # each label: shrink the font, then truncate as a last resort.
    _measure = _probe_draw()
    fitted_labels = []
    for item in items:
        text = item["label"][:30]
        font = fonts["label"]
        for size in (29, 26, 23, 20):
            font = load_font(tokens["font"], size)
            if _measure.textlength(text, font=font) <= x0 - 40:
                break
        while text and _measure.textlength(text, font=font) > x0 - 40:
            text = text[:-1]
        fitted_labels.append((text, font))

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("chart")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, 84), title.upper()[:60], font=fonts["title"],
                   fill=tokens["text"] + (255,))
        # Gridlines + axis.
        for tick_i in range(6):
            frac = tick_i / 5
            gx = x0 + (x1 - x0) * frac
            d.line([gx, top - 16, gx, axis_y], fill=(255, 255, 255, 26), width=1)
        d.line([x0, top - 16, x0, axis_y], fill=(255, 255, 255, 150), width=2)
        d.line([x0, axis_y, x1, axis_y], fill=(255, 255, 255, 150), width=2)
        for tick_i in range(6):
            frac = tick_i / 5
            gx = x0 + (x1 - x0) * frac
            tick_value = axis_max * frac
            tick_text = f"{tick_value:,.0f}" if float(tick_value).is_integer() else f"{tick_value:,.1f}"
            tw = d.textlength(tick_text, font=fonts["tick"])
            d.text((gx - tw / 2, axis_y + 12), tick_text, font=fonts["tick"],
                   fill=tokens["muted"] + (255,))
        if units:
            uw = d.textlength(units.upper(), font=fonts["tick"])
            d.text(((x0 + x1) / 2 - uw / 2, axis_y + 46), units.upper()[:48],
                   font=fonts["tick"], fill=tokens["muted"] + (255,))
        for i, item in enumerate(items):
            y = top + i * row_h
            local = ease_out(clamp01(progress * 1.6 - i * 0.16))
            label, label_font = fitted_labels[i]
            lw = d.textlength(label, font=label_font)
            d.text((x0 - 24 - lw, y + 4), label, font=label_font,
                   fill=tokens["text"] + (255,))
            bar_w = int((x1 - x0) * (abs(item["value"]) / axis_max) * local)
            colour = palette[i % len(palette)]
            d.rectangle([x0, y, x0 + bar_w, y + 42], fill=colour + (255,))
            if local >= 0.97 and item["display"]:
                shown = item["display"]
            else:
                shown = f"{item['value'] * local:,.0f}" if float(item["value"]).is_integer() else f"{item['value'] * local:,.1f}"
            d.text((x0 + bar_w + 18, y + 3), shown[:24], font=fonts["value"],
                   fill=(255, 255, 255, 255))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("bar_chart", style_name, duration, sentence_index, scene_number,
                       {"items": len(items), "title": bool(title), "units": bool(units),
                        "axis_max": axis_max})


# ---------------------------------------------------------------------------
# 14. Satellite annotation — caller image + drawn route + leader label
# ---------------------------------------------------------------------------

def satellite_annotation(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    image = _load_image(data.get("image"))
    if image is None:
        return None, {"reason": "no_image"}
    route = [
        (float(p[0]), float(p[1]))
        for p in (data.get("route") or [])
        if isinstance(p, (list, tuple)) and len(p) >= 2
    ]
    label = data.get("label") if isinstance(data.get("label"), dict) else {}
    label_text = _clean(label.get("text"))
    if len(route) < 2 and not label_text:
        return None, {"reason": "no_annotation"}
    bg = fit_cover(image, (WIDTH, HEIGHT))
    # Deterministic hand-drawn jitter for the route.
    rng = random.Random(53)
    jitter = [(rng.uniform(-3.5, 3.5), rng.uniform(-3.5, 3.5)) for _ in route]
    fonts = {
        "label": load_font(tokens["font_bold"], 40),
        "small": load_font(tokens["font"], 22),
    }
    title = _clean(data.get("title"))

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("image")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("annotation")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if len(route) >= 2:
            pts = [(x * WIDTH + jx, y * HEIGHT + jy)
                   for (x, y), (jx, jy) in zip(route, jitter)]
            frac = ease_in_out(clamp01(progress * 1.3))
            # Casing + light core: a dark-only route vanishes over
            # dark terrain (benchmark v2's first satellite frame);
            # the reference draws a light hand line on the imagery.
            progressive_polyline(d, pts, frac, fill=(12, 12, 14, 235), width=13)
            progressive_polyline(d, pts, frac, fill=(255, 240, 210, 255), width=6)
        if label_text:
            at = label.get("at") or [0.08, 0.12]
            point = label.get("point") or (route[-1] if route else [0.5, 0.5])
            lx, ly = float(at[0]) * WIDTH, float(at[1]) * HEIGHT
            px_, py_ = float(point[0]) * WIDTH, float(point[1]) * HEIGHT
            if progress > 0.55:
                tw = d.textlength(label_text.upper(), font=fonts["label"])
                d.rectangle([lx - 14, ly - 10, lx + tw + 14, ly + 56],
                            fill=(0, 0, 0, 130))
                d.text((lx, ly), label_text.upper()[:44], font=fonts["label"],
                       fill=(255, 255, 255, 255))
                # Leader line from the label box edge to the anchor.
                start = (lx + tw + 14, ly + 24)
                d.line([start, (px_, py_)], fill=(255, 255, 255, 235), width=3)
                d.ellipse([px_ - 11, py_ - 11, px_ + 11, py_ + 11],
                          fill=(255, 255, 255, 255), outline=(10, 10, 10, 255), width=3)
        if title:
            d.text((90, 60), title.upper()[:52], font=fonts["label"],
                   fill=(255, 255, 255, 255))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta("satellite_annotation", style_name, duration, sentence_index, scene_number,
                       {"image": True, "route_points": len(route),
                        "label": bool(label_text)})


V2_TEMPLATES = {
    "split_panel": split_panel,
    "scan_grid": scan_grid,
    "photo_node_diagram": photo_node_diagram,
    "ring_badges": ring_badges,
    "cross_section": cross_section,
    "bar_chart": bar_chart,
    "satellite_annotation": satellite_annotation,
    # Wave 1 (F5): schematic variants over cross_section.
    # (Phase 9 final fix: mechanical_cutaway removed — see the
    # note at the wrapper definitions above.)
    "exploded_view": exploded_view,
    "blueprint_section": blueprint_section,
}

# Self-registration into the v1 registry: one lookup point for the
# graphics chain, and no import-order trap (this module's top-level
# import of templates' helpers completes first in both orders).
from services.motion_engine import templates as _v1_templates  # noqa: E402

_v1_templates.TEMPLATES.update(V2_TEMPLATES)
