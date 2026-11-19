"""Real-outline geography for the Motion Engine (Stage 5C).

Loads the bundled Natural Earth-derived countries dataset
(data/countries.geojson — public domain, see data/SOURCE.md) and
offers:

- country lookup by name (exact, then a small display-name alias
  table, then case-insensitive match),
- point-in-polygon / point-in-country tests (ray casting, holes
  honoured),
- a Projection that fits one or more countries' lon/lat bounding
  box into a normalised canvas rect, preserving aspect ratio with
  a cos(mean-latitude) correction so shapes are not stretched,
- normalised outline rings for the territory_map template.

Nothing here invents geography: a country that is not in the
dataset returns None, and callers fall back to caller-supplied
polygons or refuse.
"""

import json
import math
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent / "data" / "countries.geojson"

# Default normalised canvas rect (x0, y0, x1, y1) a fitted outline
# occupies — margins keep labels and glow off the frame edges. The
# territory_map template and any caller projecting routes/markers
# for the same map MUST share this rect to stay consistent.
DEFAULT_RECT = (0.05, 0.07, 0.95, 0.96)

# Display-name aliases -> dataset names. Geography lookup only;
# this is not entity alias matching (VA-02 is untouched).
_ALIASES = {
    "usa": "United States of America",
    "us": "United States of America",
    "united states": "United States of America",
    "uk": "United Kingdom",
    "great britain": "United Kingdom",
    "south korea": "South Korea",
    "north korea": "North Korea",
    "russia": "Russia",
    "vietnam": "Vietnam",
    "iran": "Iran",
    "syria": "Syria",
    "laos": "Laos",
    "bolivia": "Bolivia",
    "venezuela": "Venezuela",
    "tanzania": "Tanzania",
    "moldova": "Moldova",
    "brunei": "Brunei",
    "palestine": "Palestine",
    "ivory coast": "Ivory Coast",
    "czech republic": "Czech Republic",
    "democratic republic of the congo": "Democratic Republic of the Congo",
    "republic of the congo": "Republic of the Congo",
}

_CACHE = None


def load_dataset():
    """The parsed FeatureCollection (cached after first load)."""
    global _CACHE
    if _CACHE is None:
        _CACHE = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    return _CACHE


def country_names():
    return [
        (f.get("properties") or {}).get("name")
        for f in load_dataset().get("features") or []
    ]


def _feature_for(name):
    if not name:
        return None
    wanted = str(name).strip()
    features = load_dataset().get("features") or []
    for feature in features:
        if (feature.get("properties") or {}).get("name") == wanted:
            return feature
    alias = _ALIASES.get(wanted.lower())
    if alias:
        for feature in features:
            if (feature.get("properties") or {}).get("name") == alias:
                return feature
    lowered = wanted.lower()
    for feature in features:
        fname = (feature.get("properties") or {}).get("name") or ""
        if fname.lower() == lowered:
            return feature
    return None


def country_geometry(name):
    """GeoJSON geometry dict for a country, or None when the
    country is not in the dataset (never approximated)."""
    feature = _feature_for(name)
    if feature is None:
        return None
    return feature.get("geometry") or None


def country_polygons(name):
    """List of polygons for a country; each polygon is a list of
    rings; each ring is a list of (lon, lat) tuples. Ring 0 is the
    outer ring, later rings are holes. [] when unknown."""
    geometry = country_geometry(name)
    if not geometry:
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        raw_polygons = [coords]
    elif gtype == "MultiPolygon":
        raw_polygons = coords
    else:
        return []
    polygons = []
    for raw in raw_polygons:
        rings = [
            [(float(p[0]), float(p[1])) for p in ring if len(p) >= 2]
            for ring in raw
        ]
        rings = [r for r in rings if len(r) >= 3]
        if rings:
            polygons.append(rings)
    return polygons


def country_bbox(name):
    """(min_lon, min_lat, max_lon, max_lat) or None."""
    polygons = country_polygons(name)
    if not polygons:
        return None
    lons = [p[0] for poly in polygons for ring in poly for p in ring]
    lats = [p[1] for poly in polygons for ring in poly for p in ring]
    return (min(lons), min(lats), max(lons), max(lats))


def point_in_ring(lon, lat, ring):
    """Ray-casting point-in-polygon for one ring of (lon, lat)."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def point_in_country(lon, lat, name):
    """True when (lon, lat) is inside any of the country's polygons
    (outer ring minus holes). False for unknown countries."""
    for polygon in country_polygons(name):
        if not polygon:
            continue
        if point_in_ring(lon, lat, polygon[0]):
            if any(point_in_ring(lon, lat, hole) for hole in polygon[1:]):
                continue
            return True
    return False


class Projection:
    """Fits a lon/lat bbox into a normalised rect, preserving
    aspect ratio with a cos(mean-latitude) x-correction."""

    def __init__(self, bbox, rect=DEFAULT_RECT):
        self.bbox = bbox  # (min_lon, min_lat, max_lon, max_lat)
        self.rect = rect
        min_lon, min_lat, max_lon, max_lat = bbox
        mean_lat = (min_lat + max_lat) / 2.0
        self._lat_correction = max(0.2, math.cos(math.radians(mean_lat)))
        span_lon = max(1e-9, (max_lon - min_lon) * self._lat_correction)
        span_lat = max(1e-9, max_lat - min_lat)
        rx0, ry0, rx1, ry1 = rect
        rect_w, rect_h = rx1 - rx0, ry1 - ry0
        scale = min(rect_w / span_lon, rect_h / span_lat)
        self._scale = scale
        used_w = span_lon * scale
        used_h = span_lat * scale
        self._off_x = rx0 + (rect_w - used_w) / 2.0
        self._off_y = ry0 + (rect_h - used_h) / 2.0

    def project(self, lon, lat):
        """(lon, lat) -> normalised (x, y), y growing downward."""
        min_lon, min_lat, max_lon, max_lat = self.bbox
        x = self._off_x + (lon - min_lon) * self._lat_correction * self._scale
        y = self._off_y + (max_lat - lat) * self._scale
        return (x, y)


def fit_projection(names, rect=DEFAULT_RECT):
    """A Projection covering the union bbox of the named countries,
    or None when none of them is in the dataset."""
    boxes = [country_bbox(n) for n in names or []]
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    bbox = (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )
    return Projection(bbox, rect=rect)


def _ring_area(ring):
    area = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
        area += x0 * y1 - x1 * y0
    return abs(area) / 2.0


def outline_for(name, projection):
    """Normalised outline rings for a country under `projection`:
    a list of polygons, each a list of rings of (x, y) points.
    Tiny islets (outer-ring area < 0.2% of the country's largest
    ring) are dropped for render legibility — the mainland shape
    is never altered. [] when the country is unknown."""
    polygons = country_polygons(name)
    if not polygons:
        return []
    largest = max(_ring_area(poly[0]) for poly in polygons)
    # Mainland (largest outer ring) first — renderers anchor labels
    # and sweeps on the first polygon.
    polygons = sorted(polygons, key=lambda p: -_ring_area(p[0]))
    result = []
    for polygon in polygons:
        if _ring_area(polygon[0]) < largest * 0.002:
            continue
        projected = [
            [projection.project(lon, lat) for lon, lat in ring]
            for ring in polygon
        ]
        result.append(projected)
    return result
