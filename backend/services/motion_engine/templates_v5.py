"""Motion Engine templates v5 — Phase 5 core treatments rebuild
(Motion Graphics Programme, 2026-11-27).

The treatments the founder's P0 removal took out are re-served
here as NEW templates under NEW names, built to the Bible
standard the removed ones failed:

  * ``chronology`` — the rebuilt chronology (audit B1), replacing
    the removed ``timeline`` graphic. Fed ONLY by recorded
    timeline events (case-bible ``timeline`` entries: when/what).
    Requires >= 3 recorded events; every event's ``what`` must
    already be a short designed payload (the assembler gates it;
    the template re-checks fit) — a long/sentence payload is
    never printed whole. Dates sit at nodes on a horizontal axis;
    labels alternate above/below the axis in per-event columns
    and are measured with label_fit BEFORE any frame is drawn:
    an event whose date or label does not fit its column refuses
    the whole graphic with ``label_does_not_fit`` — never
    clipped, never truncated, never overlapping (the exact
    defects of the removed renderer).
  * ``route`` — the rebuilt editorial route treatment (audit
    C1a). Requires >= 2 recorded places; draws the actual route
    between them over the retained geography dataset's country
    outlines. A "route" with fewer than 2 places refuses.
  * ``locator`` — the rebuilt locator (audit C1b): ONE recorded
    place, marked in its geographic context (the country's real
    outline from the dataset when the place is / sits in a
    dataset country). No route is implied, ever.
  * ``relationship_network`` — the relationship network (audit
    B6). Nodes are recorded entities, edges are recorded
    relations ONLY. Refuses with fewer than 2 entities or 0
    recorded edges; an edge naming an unrecorded entity refuses
    — no invented edges, ever.
  * ``labelled_diagram`` — the scientific explanatory treatment
    (audit D1, Phase 6–7). Fed ONLY by the recorded ``diagram``
    contract (parts: name + short description + relative
    position/order). The ``molecular_diagram`` contract was
    assessed as its feed first: it is chemistry-specific
    (element symbols, bond orders) and carries no part names or
    descriptions, so this general treatment is a parallel
    template rather than an extension of it.

Every template renders ONLY caller-supplied recorded data and
returns (None, {"reason": ...}) when its contract is unmet —
the engine's standing honesty discipline. Geography resolves
through motion_engine.geography, which refuses invented
geography: a place that is neither in the dataset nor carrying
recorded coordinates refuses the graphic. Motion comes from
the motion_registry vocabulary (ease_out / ease_in_out reveals)
— no bounce, spin or wipe defaults. Text is measured through
motion_engine.label_fit; overflow refuses, never clips.
"""

import math

from PIL import Image, ImageDraw

from services.motion_engine import core, geography, motion_registry
from services.motion_engine.core import (
    HEIGHT, WIDTH, clamp01, load_font, progressive_polyline,
)
from services.motion_engine.label_fit import (
    LABEL_FIT_REASON, check_labels, fit_lines,
)
from services.motion_engine.styles import resolve_style
from services.motion_engine.templates import (
    TEMPLATES, _clean, _footer, _meta, _textured_dark,
)

ease_in_out = motion_registry.easing_function("ease_in_out")
ease_out = motion_registry.easing_function("ease_out")


def _probe_draw():
    return ImageDraw.Draw(Image.new("RGBA", (10, 10)))


def _fit_or_refuse(draw, labels):
    """label_fit gate shared by the v5 templates: returns the
    refusal tuple when any label overflows or any two label
    boxes collide, else None."""
    defects = check_labels(draw, labels)
    if defects:
        first = defects[0]
        return None, {
            "reason": LABEL_FIT_REASON,
            "detail": first.get("detail") or f"label collision: {first}",
        }
    return None


# ---------------------------------------------------------------------------
# B1. Chronology — recorded timeline events on a horizontal axis
# ---------------------------------------------------------------------------

def chronology(data, style, out_path, duration=5.0,
               sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    events = []
    for entry in data.get("events") or []:
        if not isinstance(entry, dict):
            continue
        when = _clean(entry.get("when"))
        what = _clean(entry.get("what"))
        if when and what:
            events.append({"when": when, "what": what})
    if len(events) < 3:
        # A 2-event "chronology" is a date pair, not a timeline.
        return None, {"reason": "insufficient_events"}
    events = events[:8]
    title = _clean(data.get("title"))
    fonts = {
        "title": load_font(tokens["font_bold"], 40),
        "date": load_font(tokens["font_bold"], 27),
        "label": load_font(tokens["font"], 25),
    }
    probe = _probe_draw()
    axis_y = HEIGHT // 2
    x0, x1 = 150, WIDTH - 150
    span = x1 - x0
    col_w = span / len(events)
    # Per-event layout: node on the axis; the DATE sits on one
    # side of the axis and the LABEL on the other, alternating
    # per event so neighbouring columns never stack their text
    # on the same row. Boxes are computed up front and checked
    # for fit + collision before a single frame is drawn.
    layout = []
    label_specs = []
    for i, event in enumerate(events):
        cx = x0 + col_w * (i + 0.5)
        above = (i % 2 == 0)
        date_lines = fit_lines(
            probe, event["when"].upper(), fonts["date"],
            col_w - 24, max_lines=1)
        if date_lines is None:
            return None, {
                "reason": LABEL_FIT_REASON,
                "detail": f"chronology date {event['when']!r} does "
                          f"not fit its column",
            }
        label_lines = fit_lines(
            probe, event["what"], fonts["label"],
            col_w - 24, max_lines=3)
        if label_lines is None:
            return None, {
                "reason": LABEL_FIT_REASON,
                "detail": f"chronology label {event['what']!r} does "
                          f"not fit its column",
            }
        date_w = max(
            probe.textlength(line, font=fonts["date"])
            for line in date_lines)
        label_w = max(
            probe.textlength(line, font=fonts["label"])
            for line in label_lines)
        date_h = 34
        label_h = len(label_lines) * 32
        if above:
            date_box = (cx - date_w / 2, axis_y - 108 - date_h,
                        cx + date_w / 2, axis_y - 108)
            label_box = (cx - label_w / 2, axis_y + 64,
                         cx + label_w / 2, axis_y + 64 + label_h)
        else:
            date_box = (cx - date_w / 2, axis_y + 108,
                        cx + date_w / 2, axis_y + 108 + date_h)
            label_box = (cx - label_w / 2, axis_y - 64 - label_h,
                         cx + label_w / 2, axis_y - 64)
        layout.append({
            "cx": cx, "above": above,
            "date_lines": date_lines, "label_lines": label_lines,
            "date_box": date_box, "label_box": label_box,
        })
        label_specs.append({
            "key": f"date_{i}", "text": event["when"],
            "font": fonts["date"], "max_width": col_w - 24,
            "max_lines": 1, "box": date_box,
        })
        label_specs.append({
            "key": f"label_{i}", "text": event["what"],
            "font": fonts["label"], "max_width": col_w - 24,
            "max_lines": 3, "box": label_box,
        })
    refusal = _fit_or_refuse(probe, label_specs)
    if refusal is not None:
        return refusal
    if title and fit_lines(probe, title.upper(), fonts["title"],
                           1500, max_lines=1) is None:
        return None, {"reason": LABEL_FIT_REASON,
                      "detail": "chronology title does not fit"}
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=83)

    def draw_frame(t, progress):
        canvas = core.Canvas(
            bg=tokens["bg"],
            pattern=tokens.get("background_pattern"),
            pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("chronology")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, 84), title.upper(),
                   font=fonts["title"], fill=tokens["text"] + (255,))
        reveal = ease_in_out(clamp01(progress * 1.2))
        progressive_polyline(
            d, [(x0, axis_y), (x1, axis_y)], reveal,
            fill=tokens["accent"] + (255,), width=5)
        for i, item in enumerate(layout):
            local = ease_out(clamp01(progress * 2.2 - i * 0.22))
            if local <= 0:
                continue
            cx = item["cx"]
            r = 13
            d.ellipse([cx - r, axis_y - r, cx + r, axis_y + r],
                      fill=tokens["accent"] + (255,),
                      outline=(255, 255, 255, 255), width=3)
            # Connector from the node to each text block.
            if item["above"]:
                d.line([cx, axis_y - r, cx, item["date_box"][3]],
                       fill=tokens["muted"] + (255,), width=2)
                d.line([cx, axis_y + r, cx, item["label_box"][1]],
                       fill=tokens["muted"] + (255,), width=2)
            else:
                d.line([cx, axis_y + r, cx, item["date_box"][1]],
                       fill=tokens["muted"] + (255,), width=2)
                d.line([cx, axis_y - r, cx, item["label_box"][3]],
                       fill=tokens["muted"] + (255,), width=2)
            for line in item["date_lines"]:
                lw = d.textlength(line, font=fonts["date"])
                d.text((cx - lw / 2, item["date_box"][1]), line,
                       font=fonts["date"],
                       fill=tokens["accent2"] + (255,))
            ly = item["label_box"][1]
            for line in item["label_lines"]:
                lw = d.textlength(line, font=fonts["label"])
                d.text((cx - lw / 2, ly), line,
                       font=fonts["label"],
                       fill=tokens["text"] + (255,))
                ly += 32
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta(
        "chronology", style_name, duration, sentence_index,
        scene_number, {"events": len(events), "title": bool(title)})


# ---------------------------------------------------------------------------
# C1. Editorial maps — route (>= 2 recorded places) and locator (1 place)
# ---------------------------------------------------------------------------

def _resolve_place(place):
    """One recorded place -> {name, lon, lat, country} or None.

    Coordinates come from the place's own recorded lon/lat when
    present; otherwise, when the recorded name resolves in the
    geography dataset, the country's bbox centre (a dataset
    fact, labelled as the country marker). A place with neither
    refuses — geography is never invented."""
    if not isinstance(place, dict):
        return None
    name = _clean(place.get("name"))
    if not name:
        return None
    lon, lat = place.get("lon"), place.get("lat")
    if lon is None or lat is None:
        coords = place.get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            lon, lat = coords[0], coords[1]
    if lon is not None and lat is not None:
        try:
            return {"name": name, "lon": float(lon), "lat": float(lat),
                    "country": _clean(place.get("country")) or None}
        except (TypeError, ValueError):
            return None
    bbox = geography.country_bbox(name)
    if bbox is not None:
        return {"name": name,
                "lon": (bbox[0] + bbox[2]) / 2.0,
                "lat": (bbox[1] + bbox[3]) / 2.0,
                "country": name}
    return None


def _map_projection(places):
    """A projection framing every place: the union of the
    places' dataset countries when any resolve, else the
    places' own padded bbox. Map rect leaves room for the
    title (top) and labels."""
    countries = [p["country"] for p in places if p.get("country")]
    rect = (0.06, 0.16, 0.94, 0.94)
    if countries:
        projection = geography.fit_projection(countries, rect=rect)
        if projection is not None:
            return projection, countries
    box = geography.places_bbox([(p["lon"], p["lat"]) for p in places])
    if box is None:
        return None, countries
    return geography.Projection(
        geography.padded_places_bbox(box), rect=rect), countries


def _draw_outlines(d, projection, countries, tokens):
    for name in countries:
        for polygon in geography.outline_for(name, projection):
            for ring in polygon:
                points = [(x * WIDTH, y * HEIGHT) for x, y in ring]
                if len(points) >= 2:
                    d.line(points + [points[0]],
                           fill=tokens["accent2"] + (170,), width=3)


def _place_label_specs(probe, fonts, placed):
    return [{
        "key": f"place_{i}", "text": p["name"],
        "font": fonts["label"], "max_width": 300, "max_lines": 1,
        "box": box,
    } for i, (p, box) in enumerate(placed)]


def _layout_place_labels(probe, fonts, points):
    """Collision-free label boxes for map markers via the core
    place_labels resolver (boxes are never resized). Returns
    [(place, box), ...] in input order."""
    items = []
    for i, (place, (px, py)) in enumerate(points):
        w = probe.textlength(place["name"], font=fonts["label"]) + 24
        items.append({"key": i, "cx": px, "cy": py - 34,
                      "w": w, "h": 40})
    boxes = core.place_labels(items, gap=10.0)
    return [(points[i][0], boxes[i]) for i in range(len(points))]


def route(data, style, out_path, duration=5.0,
          sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    places = []
    for raw in data.get("places") or []:
        resolved = _resolve_place(raw)
        if resolved is None:
            return None, {"reason": "unknown_place",
                          "detail": f"place {raw!r} is not in the "
                                    f"geography dataset and carries "
                                    f"no recorded coordinates"}
        places.append(resolved)
    if len(places) < 2:
        # A route with fewer than two recorded places is not a
        # route — the locator treatment serves a single place.
        return None, {"reason": "insufficient_places"}
    places = places[:6]
    title = _clean(data.get("title"))
    fonts = {
        "title": load_font(tokens["font_bold"], 40),
        "label": load_font(tokens["font_bold"], 27),
    }
    probe = _probe_draw()
    projection, countries = _map_projection(places)
    if projection is None:
        return None, {"reason": "unknown_place"}
    points = []
    for place in places:
        nx, ny = projection.project(place["lon"], place["lat"])
        points.append((place, (nx * WIDTH, ny * HEIGHT)))
    placed = _layout_place_labels(probe, fonts, points)
    refusal = _fit_or_refuse(
        probe, _place_label_specs(probe, fonts, placed))
    if refusal is not None:
        return refusal
    if title and fit_lines(probe, title.upper(), fonts["title"],
                           1500, max_lines=1) is None:
        return None, {"reason": LABEL_FIT_REASON,
                      "detail": "route title does not fit"}
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=89)
    route_points = [pt for _place, pt in points]

    def draw_frame(t, progress):
        canvas = core.Canvas(
            bg=tokens["bg"],
            pattern=tokens.get("background_pattern"),
            pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("route")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, 64), title.upper(),
                   font=fonts["title"], fill=tokens["text"] + (255,))
        _draw_outlines(d, projection, countries, tokens)
        reveal = ease_in_out(clamp01(progress * 1.25))
        if reveal > 0:
            core.dashed_polyline(
                d, route_points, reveal,
                fill=tokens["accent"] + (255,), width=6)
        for i, (place, (px, py)) in enumerate(points):
            local = ease_out(clamp01(progress * 2.0 - i * 0.25))
            if local <= 0:
                continue
            r = 12
            d.ellipse([px - r, py - r, px + r, py + r],
                      fill=tokens["accent"] + (255,),
                      outline=(255, 255, 255, 255), width=3)
        for place, box in placed:
            lw = d.textlength(place["name"], font=fonts["label"])
            d.text((box[0] + (box[2] - box[0] - lw) / 2,
                    box[1] + 6), place["name"],
                   font=fonts["label"], fill=tokens["text"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta(
        "route", style_name, duration, sentence_index, scene_number,
        {"places": len(places), "countries": len(countries),
         "title": bool(title)})


def locator(data, style, out_path, duration=5.0,
            sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    raw_places = data.get("places") or []
    if data.get("place"):
        raw_places = [data["place"]] + list(raw_places)
    if not raw_places:
        return None, {"reason": "no_places"}
    place = _resolve_place(raw_places[0])
    if place is None:
        return None, {"reason": "unknown_place",
                      "detail": "place is not in the geography "
                                "dataset and carries no recorded "
                                "coordinates"}
    title = _clean(data.get("title"))
    fonts = {
        "title": load_font(tokens["font_bold"], 40),
        "label": load_font(tokens["font_bold"], 30),
    }
    probe = _probe_draw()
    projection, countries = _map_projection([place])
    if projection is None:
        return None, {"reason": "unknown_place"}
    nx, ny = projection.project(place["lon"], place["lat"])
    marker = (nx * WIDTH, ny * HEIGHT)
    placed = _layout_place_labels(probe, fonts, [(place, marker)])
    refusal = _fit_or_refuse(
        probe, _place_label_specs(probe, fonts, placed))
    if refusal is not None:
        return refusal
    if title and fit_lines(probe, title.upper(), fonts["title"],
                           1500, max_lines=1) is None:
        return None, {"reason": LABEL_FIT_REASON,
                      "detail": "locator title does not fit"}
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=97)

    def draw_frame(t, progress):
        canvas = core.Canvas(
            bg=tokens["bg"],
            pattern=tokens.get("background_pattern"),
            pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("locator")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, 64), title.upper(),
                   font=fonts["title"], fill=tokens["text"] + (255,))
        _draw_outlines(d, projection, countries, tokens)
        # A locator marks ONE place: a ring + pin, no route line.
        pulse = ease_out(clamp01(progress * 1.6))
        px, py = marker
        ring_r = 18 + 26 * pulse
        d.ellipse([px - ring_r, py - ring_r, px + ring_r, py + ring_r],
                  outline=tokens["accent"] + (255,), width=4)
        d.ellipse([px - 11, py - 11, px + 11, py + 11],
                  fill=tokens["accent"] + (255,),
                  outline=(255, 255, 255, 255), width=3)
        for _place, box in placed:
            lw = d.textlength(place["name"], font=fonts["label"])
            d.text((box[0] + (box[2] - box[0] - lw) / 2,
                    box[1] + 5), place["name"],
                   font=fonts["label"], fill=tokens["text"] + (255,))
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta(
        "locator", style_name, duration, sentence_index,
        scene_number, {"place": place["name"],
                       "countries": len(countries),
                       "title": bool(title)})


# ---------------------------------------------------------------------------
# B6. Relationship network — recorded entities + recorded relations only
# ---------------------------------------------------------------------------

def relationship_network(data, style, out_path, duration=5.0,
                         sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    nodes = []
    for entry in data.get("nodes") or data.get("entities") or []:
        name = _clean(
            entry.get("name") or entry.get("canonical")
            if isinstance(entry, dict) else entry)
        if name and name not in nodes:
            nodes.append(name)
    if len(nodes) < 2:
        return None, {"reason": "insufficient_entities"}
    edges = []
    for entry in data.get("edges") or data.get("relations") or []:
        if not isinstance(entry, dict):
            continue
        source = _clean(entry.get("from") or entry.get("source"))
        target = _clean(entry.get("to") or entry.get("target"))
        if source not in nodes or target not in nodes:
            # An edge naming an unrecorded entity is an invented
            # edge — refuse the graphic rather than draw it.
            return None, {"reason": "edge_endpoint_unrecorded",
                          "detail": f"edge {source!r} -> {target!r} "
                                    f"names an entity outside the "
                                    f"recorded node set"}
        if source == target:
            continue
        edges.append({
            "from": source, "to": target,
            "label": _clean(entry.get("label")
                            or entry.get("relation")),
        })
    if not edges:
        return None, {"reason": "no_recorded_edges"}
    nodes = nodes[:8]
    edges = edges[:12]
    title = _clean(data.get("title"))
    fonts = {
        "title": load_font(tokens["font_bold"], 40),
        "node": load_font(tokens["font_bold"], 27),
        "edge": load_font(tokens["font"], 22),
    }
    probe = _probe_draw()
    cx, cy = WIDTH // 2, HEIGHT // 2 + 30
    radius = 310 if len(nodes) > 2 else 260
    positions = {}
    for i, name in enumerate(nodes):
        if len(nodes) == 2:
            angle = math.pi if i == 0 else 0.0
        else:
            angle = -math.pi / 2 + 2 * math.pi * i / len(nodes)
        positions[name] = (
            cx + radius * math.cos(angle) * 1.45,
            cy + radius * math.sin(angle),
        )
    label_specs = []
    node_boxes = {}
    for name in nodes:
        px, py = positions[name]
        lines = fit_lines(probe, name, fonts["node"], 300,
                          max_lines=2)
        if lines is None:
            return None, {"reason": LABEL_FIT_REASON,
                          "detail": f"node label {name!r} does not fit"}
        w = max(probe.textlength(line, font=fonts["node"])
                for line in lines) + 44
        h = len(lines) * 34 + 30
        box = (px - w / 2, py - h / 2, px + w / 2, py + h / 2)
        node_boxes[name] = (box, lines)
        label_specs.append({
            "key": f"node_{name}", "text": name,
            "font": fonts["node"], "max_width": 300, "max_lines": 2,
            "box": box,
        })
    for j, edge in enumerate(edges):
        if not edge["label"]:
            continue
        ax, ay = positions[edge["from"]]
        bx, by = positions[edge["to"]]
        mx, my = (ax + bx) / 2, (ay + by) / 2
        if fit_lines(probe, edge["label"], fonts["edge"], 220,
                     max_lines=1) is None:
            return None, {"reason": LABEL_FIT_REASON,
                          "detail": f"edge label {edge['label']!r} "
                                    f"does not fit"}
        w = probe.textlength(edge["label"], font=fonts["edge"]) + 16
        label_specs.append({
            "key": f"edge_{j}", "text": edge["label"],
            "font": fonts["edge"], "max_width": 220, "max_lines": 1,
            "box": (mx - w / 2, my - 14, mx + w / 2, my + 14),
        })
    refusal = _fit_or_refuse(probe, label_specs)
    if refusal is not None:
        return refusal
    if title and fit_lines(probe, title.upper(), fonts["title"],
                           1500, max_lines=1) is None:
        return None, {"reason": LABEL_FIT_REASON,
                      "detail": "network title does not fit"}
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=103)

    def draw_frame(t, progress):
        canvas = core.Canvas(
            bg=tokens["bg"],
            pattern=tokens.get("background_pattern"),
            pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("network")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, 72), title.upper(),
                   font=fonts["title"], fill=tokens["text"] + (255,))
        reveal = ease_in_out(clamp01(progress * 1.25))
        for edge in edges:
            ax, ay = positions[edge["from"]]
            bx, by = positions[edge["to"]]
            progressive_polyline(
                d, [(ax, ay), (bx, by)], reveal,
                fill=tokens["muted"] + (255,), width=3)
            if edge["label"] and reveal > 0.85:
                mx, my = (ax + bx) / 2, (ay + by) / 2
                lw = d.textlength(edge["label"], font=fonts["edge"])
                d.rectangle([mx - lw / 2 - 8, my - 15,
                             mx + lw / 2 + 8, my + 15],
                            fill=tokens["bg"] + (235,))
                d.text((mx - lw / 2, my - 12), edge["label"],
                       font=fonts["edge"],
                       fill=tokens["accent2"] + (255,))
        for i, name in enumerate(nodes):
            local = ease_out(clamp01(progress * 2.0 - i * 0.15))
            if local <= 0:
                continue
            box, lines = node_boxes[name]
            d.rounded_rectangle(
                box, radius=14, fill=tokens["surface"] + (255,),
                outline=tokens["accent"] + (255,), width=3)
            ly = box[1] + 15
            for line in lines:
                lw = d.textlength(line, font=fonts["node"])
                d.text(((box[0] + box[2] - lw) / 2, ly), line,
                       font=fonts["node"],
                       fill=tokens["text"] + (255,))
                ly += 34
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta(
        "relationship_network", style_name, duration,
        sentence_index, scene_number,
        {"nodes": len(nodes), "edges": len(edges),
         "title": bool(title)})


# ---------------------------------------------------------------------------
# D1. Labelled diagram — a scenario-fed scientific explanatory
# treatment (Phase 6–7). Fed ONLY by the recorded ``diagram``
# contract: parts carrying a name, a short description, and a
# relative position or an order. Renders node markers at the
# parts' positions with leader lines to measured label boxes.
#
# Phase 9 final fix (2026-11-27): the item-13 inspection found
# the callouts floating over an empty themed background — the
# anchors labelled no visible subject. Each recorded part is
# now DRAWN as a schematic block (a tinted plate) at its
# recorded position / order slot, and the callout anchors to
# that visible block. The block is presentation only — the
# same discipline the position comment below already states:
# the parts, their positions and their order are the record;
# no part, geometry or scale is invented, and no new contract
# field was needed (positions/order were already recorded).
#
# Decision (recorded): the existing ``molecular_diagram``
# scenario contract was assessed first, per the work item. Its
# contract is chemistry-specific — atoms keyed by ELEMENT symbol
# with element radii/colours and bond orders; it carries no part
# name, no description, and no general position/order semantics.
# Extending it would have corrupted a working chemistry
# treatment's contract, so the general explanatory treatment is
# a parallel template on the same scenario-contract precedent.
# ---------------------------------------------------------------------------

def labelled_diagram(data, style, out_path, duration=5.0,
                     sentence_index=None, scene_number=None):
    style_name, tokens = resolve_style(style)
    data = data or {}
    parts = []
    for entry in data.get("parts") or []:
        if not isinstance(entry, dict):
            continue
        name = _clean(entry.get("name"))
        if not name:
            continue
        at = entry.get("at")
        position = None
        if isinstance(at, (list, tuple)) and len(at) >= 2:
            try:
                position = (
                    min(0.97, max(0.03, float(at[0]))),
                    min(0.97, max(0.03, float(at[1]))),
                )
            except (TypeError, ValueError):
                position = None
        parts.append({
            "name": name,
            "description": _clean(entry.get("description") or ""),
            "at": position,
        })
    if len(parts) < 2:
        # A diagram of one part explains nothing — and a part
        # without a recorded name can never be labelled.
        return None, {"reason": "insufficient_parts"}
    parts = parts[:8]
    title = _clean(data.get("title"))
    fonts = {
        "title": load_font(tokens["font_bold"], 40),
        "name": load_font(tokens["font_bold"], 27),
        "desc": load_font(tokens["font"], 22),
    }
    probe = _probe_draw()
    area = (130, 190, WIDTH - 130, HEIGHT - 150)
    # Positions: the recorded relative positions when EVERY part
    # records one; otherwise the recorded ORDER laid out on a
    # grid — positions are presentation, the parts and their
    # order are the record.
    recorded_positions = all(p["at"] is not None for p in parts)
    if recorded_positions:
        centres = [
            (area[0] + p["at"][0] * (area[2] - area[0]),
             area[1] + p["at"][1] * (area[3] - area[1]))
            for p in parts
        ]
    else:
        cols = math.ceil(math.sqrt(len(parts)))
        rows = math.ceil(len(parts) / cols)
        cell_w = (area[2] - area[0]) / cols
        cell_h = (area[3] - area[1]) / rows
        centres = [
            (area[0] + cell_w * (i % cols + 0.5),
             area[1] + cell_h * (i // cols + 0.5))
            for i in range(len(parts))
        ]
    # Subject blocks (Phase 9 final fix): one drawn schematic
    # plate per recorded part, centred on the part's position
    # and clamped into the diagram area. Label placement below
    # treats the blocks as obstacles, so a callout never covers
    # the part it — or another callout — labels.
    block_w, block_h = 168, 92
    blocks = []
    for cx, cy in centres:
        bx0 = min(max(cx - block_w / 2, area[0]), area[2] - block_w)
        by0 = min(max(cy - block_h / 2, area[1]), area[3] - block_h)
        blocks.append((bx0, by0, bx0 + block_w, by0 + block_h))
    # Label boxes: beside each part's block, leader line from
    # the anchor to box. Placement searches the part's free
    # side first, then the other side, then vertical offsets —
    # the first placement colliding with no label box and no
    # subject block wins. Name + description are measured with
    # label_fit BEFORE any frame is drawn; a part whose label
    # fits nowhere refuses the graphic.
    from services.motion_engine.label_fit import boxes_overlap

    box_w = 400
    label_gap = block_w / 2 + 16
    layout = []
    label_specs = []
    placed_boxes = []
    for i, part in enumerate(parts):
        cx, cy = centres[i]
        name_lines = fit_lines(
            probe, part["name"], fonts["name"], box_w - 30,
            max_lines=2)
        if name_lines is None:
            return None, {
                "reason": LABEL_FIT_REASON,
                "detail": f"diagram part name {part['name']!r} "
                          f"does not fit its label box",
            }
        desc_lines = []
        if part["description"]:
            desc_lines = fit_lines(
                probe, part["description"], fonts["desc"],
                box_w - 30, max_lines=2)
            if desc_lines is None:
                return None, {
                    "reason": LABEL_FIT_REASON,
                    "detail": f"diagram description for "
                              f"{part['name']!r} does not fit "
                              f"its label box",
                }
        box_h = 26 + len(name_lines) * 34 + len(desc_lines) * 28
        sides = (1, -1) if cx <= WIDTH / 2 else (-1, 1)
        box = None
        for side in sides:
            for dy in (0, box_h + 18, -(box_h + 18)):
                cand_x = (cx + label_gap if side > 0
                          else cx - label_gap - box_w)
                if cand_x < area[0] or cand_x + box_w > area[2]:
                    continue
                cand_y = min(
                    max(cy - box_h / 2 + dy, area[1]),
                    area[3] - box_h)
                candidate = (
                    cand_x, cand_y, cand_x + box_w, cand_y + box_h)
                if any(
                    boxes_overlap(candidate, other)
                    for other in placed_boxes
                ):
                    continue
                if any(
                    boxes_overlap(candidate, block)
                    for block in blocks
                ):
                    continue
                box = candidate
                break
            if box is not None:
                break
        if box is None:
            return None, {
                "reason": LABEL_FIT_REASON,
                "detail": f"diagram label for {part['name']!r} "
                          f"collides at every placement",
            }
        placed_boxes.append(box)
        layout.append({
            "cx": cx, "cy": cy, "box": box, "block": blocks[i],
            "name_lines": name_lines, "desc_lines": desc_lines,
        })
        label_specs.append({
            "key": f"part_{i}", "text": part["name"],
            "font": fonts["name"], "max_width": box_w - 30,
            "max_lines": 2, "box": box,
        })
    refusal = _fit_or_refuse(probe, label_specs)
    if refusal is not None:
        return refusal
    if title and fit_lines(probe, title.upper(), fonts["title"],
                           1500, max_lines=1) is None:
        return None, {"reason": LABEL_FIT_REASON,
                      "detail": "diagram title does not fit"}
    bg = _textured_dark((WIDTH, HEIGHT), tokens["bg"], seed=97)

    def draw_frame(t, progress):
        canvas = core.Canvas(
            bg=tokens["bg"],
            pattern=tokens.get("background_pattern"),
            pattern_color=tokens.get("pattern_color"))
        base = canvas.new_layer("bg")
        base.image = bg.convert("RGBA")
        layer = canvas.new_layer("diagram")
        d = ImageDraw.Draw(layer.image, "RGBA")
        if title:
            tw = d.textlength(title.upper(), font=fonts["title"])
            d.text(((WIDTH - tw) / 2, 84), title.upper(),
                   font=fonts["title"], fill=tokens["text"] + (255,))
        # The subject (Phase 9 final fix): every recorded part
        # drawn as a schematic block at its recorded position,
        # revealed with its anchor. The callouts below label
        # THESE blocks — a label with no visible part under its
        # anchor is the item-13 defect this fixes.
        for i, item in enumerate(layout):
            local = ease_out(clamp01(progress * 2.0 - i * 0.12))
            if local <= 0:
                continue
            bx0, by0, bx1, by1 = item["block"]
            bcx, bcy = (bx0 + bx1) / 2, (by0 + by1) / 2
            hw = (bx1 - bx0) / 2 * local
            hh = (by1 - by0) / 2 * local
            d.rounded_rectangle(
                [bcx - hw, bcy - hh, bcx + hw, bcy + hh],
                radius=14, fill=tokens["accent"] + (44,),
                outline=tokens["accent"] + (255,), width=3)
            # An inner plate line so the block reads as a drawn
            # part of a schematic, not a plain card.
            if local > 0.6:
                inset = 10
                d.rounded_rectangle(
                    [bx0 + inset, by0 + inset,
                     bx1 - inset, by1 - inset],
                    radius=9,
                    outline=tokens["accent2"] + (170,), width=2)
        # Reading-order connectors between consecutive parts
        # (the contract records the order; the line is that
        # order drawn, nothing more).
        for i in range(len(layout) - 1):
            local = ease_in_out(clamp01(progress * 1.6 - i * 0.15))
            if local <= 0:
                continue
            a = layout[i]
            b = layout[i + 1]
            progressive_polyline(
                d, [(a["cx"], a["cy"]), (b["cx"], b["cy"])], local,
                fill=tokens["muted"] + (160,), width=3)
        for i, item in enumerate(layout):
            local = ease_out(clamp01(progress * 2.0 - i * 0.12))
            if local <= 0:
                continue
            cx, cy = item["cx"], item["cy"]
            box = item["box"]
            # Leader line from the part's anchor to its label box.
            anchor_x = box[0] if box[0] >= cx else box[2]
            d.line([cx, cy, anchor_x, (box[1] + box[3]) / 2],
                   fill=tokens["accent2"] + (200,), width=2)
            d.rounded_rectangle(
                box, radius=12, fill=tokens["surface"] + (255,),
                outline=tokens["accent2"] + (255,), width=2)
            ly = box[1] + 13
            for line in item["name_lines"]:
                lw = d.textlength(line, font=fonts["name"])
                d.text((box[0] + (box[2] - box[0] - lw) / 2, ly),
                       line, font=fonts["name"],
                       fill=tokens["text"] + (255,))
                ly += 34
            for line in item["desc_lines"]:
                lw = d.textlength(line, font=fonts["desc"])
                d.text((box[0] + (box[2] - box[0] - lw) / 2, ly),
                       line, font=fonts["desc"],
                       fill=tokens["muted"] + (255,))
                ly += 28
            # The anchor dot sits ON the part's block, drawn last
            # so it stays visible at the block's centre.
            radius = 17 * local
            d.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius],
                fill=tokens["accent"] + (255,),
                outline=(255, 255, 255, 220), width=2)
        _footer(d, tokens)
        return canvas.composite()

    path = core.render_template(draw_frame, out_path, duration=duration)
    return path, _meta(
        "labelled_diagram", style_name, duration,
        sentence_index, scene_number,
        {"parts": len(parts), "title": bool(title),
         "subject_blocks": True,
         "positions": "recorded" if recorded_positions else "ordered_grid"})


# Phase 6–7 (G2): tracked annotation — a genuine detect+track
# template (services.motion_engine.tracking), registered here
# with the v5 family. It renders over a REAL clip, following a
# recorded anchor region frame by frame; a lost track is
# recorded, never faked.
from services.motion_engine import tracking as _tracking  # noqa: E402

TEMPLATES.update({
    "chronology": chronology,
    "route": route,
    "locator": locator,
    "relationship_network": relationship_network,
    "labelled_diagram": labelled_diagram,
    "tracked_annotation": _tracking.render_tracked_annotation,
})
