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


def _polyline_window(points, f0, f1):
    """The sub-polyline of `points` between length fractions f0..f1
    (Wave 1 map overlays: flow pulses and arrow placement ride on
    it). Returns [] when the window is empty."""
    if not points or f1 <= f0:
        return []
    lengths = []
    total = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        lengths.append(seg)
        total += seg
    if total <= 0:
        return [points[0]]
    lo, hi = total * clamp01(f0), total * clamp01(f1)
    window = []
    walked = 0.0
    for idx, seg in enumerate(lengths):
        x0, y0 = points[idx]
        x1, y1 = points[idx + 1]
        seg_lo, seg_hi = walked, walked + seg
        if seg_hi < lo or seg_lo > hi:
            walked += seg
            continue
        if seg > 0:
            fa = max(0.0, (lo - seg_lo) / seg)
            fb = min(1.0, (hi - seg_lo) / seg)
        else:
            fa, fb = 0.0, 1.0
        start = (x0 + (x1 - x0) * fa, y0 + (y1 - y0) * fa)
        end = (x0 + (x1 - x0) * fb, y0 + (y1 - y0) * fb)
        if not window or window[-1] != start:
            window.append(start)
        window.append(end)
        walked += seg
    return window


def _arrowhead(draw, tip, direction, size, fill):
    """A filled arrowhead triangle at `tip`, pointing along the
    unit vector `direction`."""
    dx, dy = direction
    norm = math.hypot(dx, dy) or 1.0
    dx, dy = dx / norm, dy / norm
    px, py = -dy, dx
    base_x, base_y = tip[0] - dx * size, tip[1] - dy * size
    draw.polygon(
        [
            tip,
            (base_x + px * size * 0.45, base_y + py * size * 0.45),
            (base_x - px * size * 0.45, base_y - py * size * 0.45),
        ],
        fill=fill,
    )


# ---------------------------------------------------------------------------
# 1. Territory map
# ---------------------------------------------------------------------------

def territory_map(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    raw_regions = [r for r in (data.get("regions") or []) if isinstance(r, dict)]

    # Stage 5C: a region may name a dataset country ("country" or
    # "name") instead of supplying points. All dataset regions on
    # one map share ONE projection (union bbox, aspect-preserved)
    # so their relative positions stay true. Caller points always
    # win when both are present; a country missing from the dataset
    # is skipped — never approximated.
    valid = []
    outline_sources = []
    dataset_projection = None
    country_names_req = [
        r.get("country") or r.get("name")
        for r in raw_regions
        if len(r.get("points") or []) < 3 and (r.get("country") or r.get("name"))
    ]
    if country_names_req:
        try:
            from services.motion_engine import geography

            dataset_projection = geography.fit_projection(country_names_req)
        except Exception:
            dataset_projection = None
    for region in raw_regions:
        points = region.get("points") or []
        if len(points) >= 3:
            valid.append({**region, "_polygons": [[points]], "_source": "caller"})
            outline_sources.append("caller")
            continue
        cname = region.get("country") or region.get("name")
        if cname and dataset_projection is not None:
            try:
                from services.motion_engine import geography

                polygons = geography.outline_for(cname, dataset_projection)
            except Exception:
                polygons = []
            if polygons:
                entry = {**region, "_polygons": polygons, "_source": "dataset"}
                if not _clean(entry.get("label")):
                    entry["label"] = str(cname)
                valid.append(entry)
                outline_sources.append("dataset")
    if not valid:
        return None, {"reason": "no_polygon_data"}
    routes = [r for r in (data.get("routes") or []) if isinstance(r, dict) and len(r.get("points") or []) >= 2]
    markers = [
        m for m in (data.get("markers") or [])
        if isinstance(m, dict) and isinstance(m.get("at"), (list, tuple)) and len(m.get("at")) >= 2
    ]
    title = _clean(data.get("title"))
    # Wave 1 (F3): map overlay variants on the same geography —
    # "border" (polygon draw-on), "route" (tracked route polish),
    # "trade" (multi-origin flow routes), "military" (arrowed
    # movement + front lines). The variant changes the DRAWING,
    # never the data: regions/routes/markers/fronts are all
    # caller-supplied exactly as for the base map.
    variant = _clean(data.get("variant")).lower()
    if variant not in ("border", "route", "trade", "military"):
        variant = ""
    fronts = [
        f for f in (data.get("fronts") or [])
        if isinstance(f, dict) and len(f.get("points") or []) >= 2
    ]
    if variant == "military" and not routes and not fronts:
        return None, {"reason": "no_movement_data"}

    # IA-14 regional zoom (CF-02): when every region on the map is a
    # dataset region and the map carries >= 2 LABELLED places
    # (markers with a label) whose geographic positions cluster in
    # a small fraction of the country bbox (geography.is_clustered),
    # re-frame on the places' region: outlines are re-projected
    # under geography.regional_projection and every marker / route
    # point — which callers supply projected under the country fit,
    # the benchmark contract — is carried into the new frame via
    # Projection.inverse + the regional projection, so geography
    # stays true. When the places are spread (or anything about the
    # map is not dataset-geographic), NOTHING below runs and the
    # render is exactly the historical whole-country one. Any
    # failure mid-computation likewise leaves the country framing
    # untouched (all new geometry is built before anything is
    # assigned).
    framing_inputs = {}
    if (
        dataset_projection is not None
        and valid
        and all(entry["_source"] == "dataset" for entry in valid)
    ):
        labelled = [m for m in markers if _clean(m.get("label"))]
        if len(labelled) >= 2:
            try:
                from services.motion_engine import geography as _geo

                lonlats = [
                    dataset_projection.inverse(
                        float(m["at"][0]), float(m["at"][1])
                    )
                    for m in labelled
                ]
                regional = _geo.regional_projection(country_names_req, lonlats)
                if regional is not None:
                    def _remap(point):
                        lon, lat = dataset_projection.inverse(
                            float(point[0]), float(point[1])
                        )
                        return list(regional.project(lon, lat))

                    new_polygons = {}
                    for entry in valid:
                        cname = entry.get("country") or entry.get("name")
                        polygons = _geo.outline_for(cname, regional)
                        if not polygons:
                            raise ValueError("regional outline missing")
                        new_polygons[id(entry)] = polygons
                    new_markers = [
                        {**m, "at": _remap(m["at"])} for m in markers
                    ]
                    new_routes = [
                        {**r, "points": [_remap(p) for p in r["points"]]}
                        for r in routes
                    ]
                    country_box = _geo.union_bbox(country_names_req)
                    places_box = _geo.places_bbox(lonlats)
                    framing_inputs = {
                        "framing": "regional",
                        "framing_bbox": [round(v, 6) for v in regional.bbox],
                        "places_bbox": [round(v, 6) for v in places_box],
                        "country_bbox": [round(v, 6) for v in country_box],
                        "places_area_fraction": _geo.places_area_fraction(
                            places_box, country_box
                        ),
                    }
                    for entry in valid:
                        entry["_polygons"] = new_polygons[id(entry)]
                    markers = new_markers
                    routes = new_routes
            except Exception:
                framing_inputs = {}
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=11)
    fonts = {
        "title": load_font(tokens["font_bold"], 44),
        "label": load_font(tokens["font_bold"], 34),
        "small": load_font(tokens["font"], 24),
    }

    def px(points):
        return [(p[0] * WIDTH, p[1] * HEIGHT) for p in points]

    def draw_frame(t, progress):
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("base")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("map")
        d = ImageDraw.Draw(layer.image, "RGBA")
        sweep = ease_in_out(clamp01(progress * 1.15))
        for region in valid:
            polygons_px = [[px(ring) for ring in poly] for poly in region["_polygons"]]
            all_pts = [p for poly in polygons_px for ring in poly for p in ring]
            xs = [p[0] for p in all_pts]
            fill = region.get("fill") or tokens["accent"]
            if isinstance(fill, (list, tuple)):
                fill_rgb = tuple(fill[:3])
            else:
                fill_rgb = tokens["accent"]
            # Glowing sweep: reveal the filled polygon left->right.
            reveal_x = min(xs) + (max(xs) - min(xs)) * sweep if max(xs) > min(xs) else max(xs)
            # IA-14: under a regional frame the outline extends far
            # beyond the canvas, so the sweep position can leave
            # [0, WIDTH]; clamp it into the frame (PIL rejects an
            # inverted mask rectangle). Country-fit outlines lie
            # inside the canvas rect, so the clamp never binds for
            # them and their renders are unchanged.
            reveal_x = max(0.0, min(float(WIDTH), reveal_x))
            region_img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
            rd = ImageDraw.Draw(region_img)
            if variant == "border":
                # Border draw-on: the fill fades in over the
                # second half while the outlines (below) trace
                # themselves on; no left->right sweep mask.
                fill_alpha = int(178 * clamp01((progress - 0.35) * 2.0))
            else:
                fill_alpha = 178
            for poly in polygons_px:
                if poly:
                    rd.polygon(poly[0], fill=fill_rgb + (fill_alpha,))
            if variant == "border":
                layer.image.alpha_composite(region_img)
            else:
                mask = Image.new("L", (WIDTH, HEIGHT), 0)
                ImageDraw.Draw(mask).rectangle([0, 0, reveal_x, HEIGHT], fill=255)
                layer.image.paste(region_img, (0, 0), Image.composite(region_img, Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0)), mask))
            # Glow outline + white outline (every ring). Border
            # variant: the outline is DRAWN ON progressively —
            # each ring traces itself by length fraction.
            border_frac = ease_in_out(clamp01(progress * 1.3))
            for poly in polygons_px:
                for ring in poly:
                    closed = ring + [ring[0]]
                    if variant == "border":
                        for wdt, alpha in ((10, 60), (4, 160)):
                            progressive_polyline(
                                d, closed, border_frac,
                                fill=fill_rgb + (alpha,), width=wdt)
                        progressive_polyline(
                            d, closed, border_frac,
                            fill=(255, 255, 255, 235), width=2)
                    else:
                        for wdt, alpha in ((10, 60), (4, 160)):
                            d.line(closed, fill=fill_rgb + (alpha,), width=wdt, joint="curve")
                        d.line(closed, fill=(255, 255, 255, 235), width=2, joint="curve")
            label = _clean(region.get("label"))
            if label and sweep > 0.55 and polygons_px and polygons_px[0]:
                main_ring = polygons_px[0][0]
                cx = sum(p[0] for p in main_ring) / len(main_ring)
                cy = sum(p[1] for p in main_ring) / len(main_ring)
                tw = d.textlength(label.upper(), font=fonts["label"])
                d.text((cx - tw / 2, cy - 20), label.upper(), font=fonts["label"], fill=(255, 255, 255, 255))
        route_frac = ease_in_out(clamp01(progress * 1.3 - 0.1))
        for route_index, route in enumerate(routes):
            pts = px(route["points"])
            if variant == "trade":
                # Flow underlay: a soft wide bed under the route
                # so converging trade routes read as currents.
                progressive_polyline(
                    d, pts, route_frac,
                    fill=tokens["accent2"] + (70,), width=13)
            if variant == "military":
                endpoint = progressive_polyline(
                    d, pts, route_frac,
                    fill=tokens["accent"] + (255,), width=9)
                if endpoint is not None and route_frac > 0.03:
                    head = _polyline_window(
                        pts, max(0.0, route_frac - 0.06), route_frac)
                    if len(head) >= 2:
                        _arrowhead(
                            d, endpoint,
                            (head[-1][0] - head[0][0],
                             head[-1][1] - head[0][1]),
                            34, tokens["accent"] + (255,))
            elif route.get("dashed"):
                core.dashed_polyline(d, pts, route_frac,
                                     fill=(255, 255, 255, 240), width=5, dash=30, gap=20)
            else:
                endpoint = progressive_polyline(
                    d, pts, route_frac,
                    fill=tokens["accent2"] + (255,), width=6)
                # Route-tracking polish (Wave 1): a pulsing
                # tracker rides the revealed head of the route.
                if endpoint is not None and 0.02 < route_frac < 0.995:
                    pulse = 13 + 5 * math.sin(t * 5.0)
                    d.ellipse(
                        [endpoint[0] - pulse, endpoint[1] - pulse,
                         endpoint[0] + pulse, endpoint[1] + pulse],
                        outline=(255, 255, 255, 190), width=3)
                    d.ellipse(
                        [endpoint[0] - 6, endpoint[1] - 6,
                         endpoint[0] + 6, endpoint[1] + 6],
                        fill=(255, 255, 255, 255))
            if variant == "trade" and route_frac > 0.15:
                # Flow pulses: bright slugs travelling the route.
                for k in range(2):
                    p = (progress * 1.15 + k * 0.41
                         + route_index * 0.17) % 0.9
                    slug = _polyline_window(pts, p, p + 0.09)
                    if len(slug) >= 2:
                        d.line(slug, fill=(255, 255, 255, 235), width=5,
                               joint="curve")
            for (x, y) in pts:
                d.ellipse([x - 8, y - 8, x + 8, y + 8], fill=(255, 255, 255, 255))
            rlabel = _clean(route.get("label"))
            if rlabel and pts:
                d.text((pts[0][0] + 14, pts[0][1] - 34), rlabel, font=fonts["small"], fill=tokens["text"] + (255,))
            dist_label = _clean(route.get("distance_label"))
            if dist_label and pts and route_frac > 0.9:
                mx = sum(p[0] for p in pts) / len(pts)
                my = sum(p[1] for p in pts) / len(pts)
                dw = d.textlength(dist_label, font=fonts["label"])
                d.rectangle([mx - dw / 2 - 14, my + 16, mx + dw / 2 + 14, my + 66],
                            fill=(0, 0, 0, 150))
                d.text((mx - dw / 2, my + 24), dist_label, font=fonts["label"],
                       fill=(255, 255, 255, 255))
        if variant == "military":
            # Front lines: dashed curves that draw on after the
            # movement arrows, in the theme accent at half weight.
            front_frac = ease_in_out(clamp01(progress * 1.2 - 0.25))
            for front in fronts:
                fpts = px(front["points"])
                core.dashed_polyline(
                    d, fpts, front_frac,
                    fill=tokens["accent"] + (200,), width=4,
                    dash=26, gap=16)
                flabel = _clean(front.get("label"))
                if flabel and fpts and front_frac > 0.85:
                    d.text((fpts[0][0] + 14, fpts[0][1] - 32),
                           flabel, font=fonts["small"],
                           fill=tokens["text"] + (255,))
        if sweep > 0.5:
            for marker in markers:
                mx, my = marker["at"][0] * WIDTH, marker["at"][1] * HEIGHT
                d.ellipse([mx - 9, my - 9, mx + 9, my + 9], fill=(255, 255, 255, 255),
                          outline=tokens["accent"] + (255,), width=3)
                mlabel = _clean(marker.get("label"))
                if mlabel:
                    d.text((mx + 18, my - 14), mlabel, font=fonts["small"],
                           fill=tokens["text"] + (255,))
        top = canvas.new_layer("text")
        td = ImageDraw.Draw(top.image, "RGBA")
        if title:
            td.text((90, 64), title.upper(), font=fonts["title"], fill=tokens["text"] + (255,))
        _footer(td, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    _inputs = {"regions": len(valid), "routes": len(routes), "title": bool(title),
               "outline_sources": outline_sources, "markers": len(markers),
               "dashed_routes": sum(1 for r in routes if r.get("dashed")),
               **framing_inputs}
    if variant:
        # Recorded only when a variant actually rendered — a base
        # territory_map's recorded inputs keep their Phase 8 shape.
        _inputs["variant"] = variant
        _inputs["fronts"] = len(fronts)
    return path, _meta(data.get("_template") or "territory_map",
                       style_name, duration, sentence_index, scene_number,
                       _inputs)


# ---------------------------------------------------------------------------
# 1b. Map overlay variants (Wave 1, F3) — thin named wrappers over
# territory_map: each fixes the variant so the Director and the
# packs can register/order them as distinct templates while the
# geography, projection and honesty contract stay the base map's.
# ---------------------------------------------------------------------------

def _map_variant(variant, template_name):
    def _render(data, style, out_path, duration=5.0,
                sentence_index=None, scene_number=None):
        data = dict(data or {})
        data["variant"] = variant
        data["_template"] = template_name
        return territory_map(
            data, style, out_path, duration=duration,
            sentence_index=sentence_index, scene_number=scene_number,
        )

    _render.__name__ = template_name
    return _render


border_map = _map_variant("border", "border_map")
route_map = _map_variant("route", "route_map")
trade_route_map = _map_variant("trade", "trade_route_map")
military_map = _map_variant("military", "military_map")


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
        caption = _clean(entry.get("caption"))
        # Founder ruling 2026-11-23 (template-level guard): a
        # caption that is a raw file title ("File:...", anything
        # carrying an image-file extension) is provenance, not a
        # caption — it is dropped whoever supplied it.
        # MG-06-residual: source-label strings ("SOURCE •
        # WIKIMEDIA") carry neither marker — they are dropped by
        # the same rule.
        if caption.lower().startswith("file:") or re.search(
            r"\.(jpe?g|png|tiff?|webp|gif|svg)\b", caption.lower()
        ) or caption.lower().startswith("source") \
                or "wikimedia" in caption.lower():
            caption = ""
        cards.append((img, caption))
    if not cards:
        return None, {"reason": "no_valid_photos"}
    # No placeholder banner: the torn-paper title is drawn only
    # when the caller supplies a real (short, designed) title.
    # MG-06-residual: a metadata-shaped or paragraph-length
    # "title" is caller provenance/prose, not a designed title —
    # the banner stays undrawn.
    title = _clean(data.get("title"))
    if title.lower().startswith("file:") or re.search(
        r"\.(jpe?g|png|tiff?|webp|gif|svg)\b", title.lower()
    ) or "wikimedia" in title.lower() or len(title) > 80:
        title = ""
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
        canvas = core.Canvas(bg=tokens["bg"], pattern=tokens.get("background_pattern"), pattern_color=tokens.get("pattern_color"))
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
        # Wave 1 (F4): red-string connectors — caller-supplied
        # [i, j] index pairs joined pin-to-pin with a sagging
        # string once the cards have settled. The string colour
        # is the board's red (the theme accent for investigative
        # themes; a fixed evidence red otherwise).
        strings = [
            s for s in (data.get("strings") or [])
            if isinstance(s, (list, tuple)) and len(s) >= 2
        ]
        if strings and progress > 0.8:
            sd = ImageDraw.Draw(layer.image, "RGBA")
            string_fill = (198, 40, 40, 235)
            pin_points = [
                (positions[i][0] + built[i].size[0] // 2,
                 positions[i][1] + 16)
                for i in range(len(built))
            ]
            for pair in strings:
                try:
                    i, j = int(pair[0]), int(pair[1])
                except (TypeError, ValueError):
                    continue
                if not (0 <= i < len(pin_points) and 0 <= j < len(pin_points)):
                    continue
                (x0, y0), (x1, y1) = pin_points[i], pin_points[j]
                sag = 26.0
                pts = []
                for k in range(17):
                    f = k / 16
                    x = x0 + (x1 - x0) * f
                    y = y0 + (y1 - y0) * f + math.sin(f * math.pi) * sag
                    pts.append((x, y))
                sd.line(pts, fill=string_fill, width=3, joint="curve")
        top = canvas.new_layer("banner")
        td = ImageDraw.Draw(top.image, "RGBA")
        if title:
            _torn_banner(td, 90, 56, 1050, 92, fill=(250, 246, 236, 255))
            td.text((122, 78), title.upper()[:52], font=fonts["title"], fill=(30, 26, 20, 255))
        for i, lab in enumerate(labels[:3]):
            td.text((120, HEIGHT - 120 + i * 30), lab.upper(), font=fonts["label"], fill=(60, 30, 24, 255))
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta(data.get("_template") or "evidence_board",
                       style_name, duration, sentence_index, scene_number,
                       {"photos": len(built), "title": bool(title), "labels": len(labels),
                        "strings": len(data.get("strings") or [])})


def red_string_board(data, style, out_path, duration=5.0, sentence_index=None, scene_number=None):
    """Evidence board with red-string connectors (Wave 1, F4).

    Same data contract as evidence_board; when the caller does
    not name the connections, consecutive pins are strung in
    board order — a layout choice over the caller's own photos,
    recorded in the metadata either way."""
    data = dict(data or {})
    photos = data.get("photos") or []
    if not photos:
        return None, {"reason": "no_valid_photos"}
    if not data.get("strings"):
        data["strings"] = [
            [i, i + 1] for i in range(min(len(photos), 4) - 1)
        ]
    # MG-04 (Motion Graphics audit): a string asserts a
    # relationship between the photos it connects. Every
    # connected node must carry a real label — an unlabelled
    # board fabricates relationships between unrelated photos,
    # so the template refuses rather than drawing the strings.
    _labels = [_clean(x) for x in (data.get("labels") or []) if _clean(x)]
    _nodes = set()
    for _pair in data.get("strings") or []:
        if isinstance(_pair, (list, tuple)):
            _nodes.update(_pair)
    if not _nodes or len(_labels) < len(_nodes):
        return None, {"reason": "unlabelled_board_nodes"}
    data["_template"] = "red_string_board"
    return evidence_board(
        data, style, out_path, duration=duration,
        sentence_index=sentence_index, scene_number=scene_number,
    )


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
            # Gate 4 (B4): labels wrap at word boundaries and
            # shrink-to-fit a 320px box, clamped inside the frame
            # safe area (outer 2% = 38px). The historical [:34]
            # slice truncated labels mid-word ("…using two sh")
            # and centred them unclamped, so the first/last
            # labels ran off the frame edge.
            label = _clean(outcome.get("label"))
            result = _clean(outcome.get("outcome"))
            label_lines, label_font = _fit_label(
                d, label, tokens, max_width=320, sizes=(27, 22, 19),
                max_lines=3)
            result_lines, result_font = _fit_label(
                d, result, tokens, max_width=320, sizes=(24, 20, 17),
                max_lines=2, bold=False)
            if above:
                d.line([x, y - 30, x, y - 78], fill=(255, 255, 255, 160), width=2)
                _lh = label_font.size + 7
                _top = y - 84 - len(label_lines) * _lh
                for _line in label_lines:
                    _top += _lh
                    lw = d.textlength(_line, font=label_font)
                    d.text((_clamped_text_x(x, lw), _top - _lh), _line,
                           font=label_font, fill=(255, 255, 255, 255))
                _ry = y - 102
                for _line in result_lines:
                    rw = d.textlength(_line, font=result_font)
                    d.text((_clamped_text_x(x, rw), _ry), _line,
                           font=result_font, fill=tokens["accent"] + (255,))
                    _ry += result_font.size + 6
            else:
                d.line([x, y + 30, x, y + 78], fill=(255, 255, 255, 160), width=2)
                _ly = y + 92
                for _line in label_lines:
                    lw = d.textlength(_line, font=label_font)
                    d.text((_clamped_text_x(x, lw), _ly), _line,
                           font=label_font, fill=(255, 255, 255, 255))
                    _ly += label_font.size + 7
                for _line in result_lines:
                    rw = d.textlength(_line, font=result_font)
                    d.text((_clamped_text_x(x, rw), _ly), _line,
                           font=result_font, fill=tokens["accent"] + (255,))
                    _ly += result_font.size + 6
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
        # Wave 1 shape (the territory_map variant precedent).
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
    "territory_map": territory_map,
    "evidence_board": evidence_board,
    "editorial_panel": editorial_panel,
    "data_gauge": data_gauge,
    "outcome_path": outcome_path,
    "stat_comparison": stat_comparison,
    "document_presentation": document_presentation,
    # Wave 2 (F4): the classified-file variant of the document
    # presentation, registered under its own name.
    "classified_file": classified_file,
    # Wave 1 (F3/F4): named variants over the base templates.
    "border_map": border_map,
    "route_map": route_map,
    "trade_route_map": trade_route_map,
    "military_map": military_map,
    "red_string_board": red_string_board,
}

# Stage 5B: the v2 template family (reference set 2) registers into
# this dict from templates_v2 itself (one lookup point for the
# graphics chain). Registration lives there — not in a bottom
# import here — so importing templates_v2 first cannot deadlock
# on a partially initialised module; the package __init__ imports
# both modules in order.
