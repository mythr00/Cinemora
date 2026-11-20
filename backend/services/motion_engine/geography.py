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
- normalised outline rings for the territory_map template,
- IA-14 regional framing: when a map's labelled places cluster in
  a small fraction of the country bbox, regional_projection frames
  on the places' (padded) bbox instead of the whole country.

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

    @property
    def scale(self):
        """Normalised-canvas units per degree of latitude (the fit
        scale; longitude degrees are additionally cos-corrected)."""
        return self._scale

    def project(self, lon, lat):
        """(lon, lat) -> normalised (x, y), y growing downward."""
        min_lon, min_lat, max_lon, max_lat = self.bbox
        x = self._off_x + (lon - min_lon) * self._lat_correction * self._scale
        y = self._off_y + (max_lat - lat) * self._scale
        return (x, y)

    def inverse(self, x, y):
        """Normalised (x, y) -> (lon, lat): the exact inverse of
        project() for THIS projection's bbox/rect. IA-14 uses it to
        recover the geographic position of a point that a caller
        projected under one fit so it can be re-projected under
        another (the regional fit)."""
        min_lon, min_lat, max_lon, max_lat = self.bbox
        lon = min_lon + (x - self._off_x) / (self._lat_correction * self._scale)
        lat = max_lat - (y - self._off_y) / self._scale
        return (lon, lat)


def union_bbox(names):
    """The union (min_lon, min_lat, max_lon, max_lat) of the named
    countries' bboxes, or None when none is in the dataset."""
    boxes = [country_bbox(n) for n in names or []]
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def fit_projection(names, rect=DEFAULT_RECT):
    """A Projection covering the union bbox of the named countries,
    or None when none of them is in the dataset."""
    bbox = union_bbox(names)
    if bbox is None:
        return None
    return Projection(bbox, rect=rect)


# ---------------------------------------------------------------------------
# IA-14 — regional zoom framing
# ---------------------------------------------------------------------------
# Whole-country framing is illegible when a story's labelled places
# cluster in one small region (CF-02: the benchmark's four Yangtze
# places span ~2.7 x ~1.0 degrees inside China's ~62 x ~35 degree
# bbox — markers knot and labels collide at country scale).
#
# Clustering is DEFINED by area: the labelled places' lon/lat
# bounding box area as a fraction of the country-union bounding box
# area. Below CLUSTER_AREA_FRACTION the places count as clustered
# and the map frames on the places' bbox instead of the country's.
# The fraction uses plain degree areas for both boxes: any latitude
# correction would apply a near-identical factor to places inside
# the same country and cancels in the ratio.
#
# The regional frame is the places' bbox padded on every side by
# REGIONAL_PAD_FRACTION of that side's span, with a floor of
# REGIONAL_MIN_PAD_DEG degrees per side (so a tight cluster still
# gets geographic context, and a degenerate zero-span cluster still
# frames a finite area: the minimum padded span is therefore
# 2 * REGIONAL_MIN_PAD_DEG = 1.0 degree). The padded bbox is NOT
# clamped to the country bbox: places near a border keep their
# cross-border context, and the country outline simply extends
# beyond the frame, as on any zoomed map.

CLUSTER_AREA_FRACTION = 0.10
REGIONAL_PAD_FRACTION = 0.30
REGIONAL_MIN_PAD_DEG = 0.5


def places_bbox(places):
    """(min_lon, min_lat, max_lon, max_lat) of an iterable of
    (lon, lat) places, or None when empty / non-numeric."""
    points = []
    for place in places or []:
        try:
            lon, lat = float(place[0]), float(place[1])
        except (TypeError, ValueError, IndexError, KeyError):
            return None
        points.append((lon, lat))
    if not points:
        return None
    return (
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )


def bbox_area(bbox):
    """Plain degree area of a (min_lon, min_lat, max_lon, max_lat)
    bbox (0.0 for None / degenerate boxes)."""
    if not bbox:
        return 0.0
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def places_area_fraction(places_box, country_box):
    """The places bbox area as a fraction of the country bbox area,
    or None when either box is missing/degenerate."""
    country_area = bbox_area(country_box)
    if not country_area or places_box is None:
        return None
    return bbox_area(places_box) / country_area


def is_clustered(places_box, country_box):
    """True when the places' bbox covers less than
    CLUSTER_AREA_FRACTION of the country bbox's area."""
    fraction = places_area_fraction(places_box, country_box)
    return fraction is not None and fraction < CLUSTER_AREA_FRACTION


def padded_places_bbox(places_box):
    """The regional framing bbox: `places_box` padded per the
    IA-14 rule documented above."""
    min_lon, min_lat, max_lon, max_lat = places_box
    pad_lon = max(
        REGIONAL_PAD_FRACTION * (max_lon - min_lon), REGIONAL_MIN_PAD_DEG
    )
    pad_lat = max(
        REGIONAL_PAD_FRACTION * (max_lat - min_lat), REGIONAL_MIN_PAD_DEG
    )
    return (
        min_lon - pad_lon,
        min_lat - pad_lat,
        max_lon + pad_lon,
        max_lat + pad_lat,
    )


def regional_projection(names, places, rect=DEFAULT_RECT):
    """A Projection framed on the labelled places' region, or None.

    Returns a Projection over padded_places_bbox(places) when the
    named countries exist in the dataset, at least two DISTINCT
    places are given, and those places are clustered (see
    is_clustered). Returns None in every other case — the caller
    then keeps the whole-country fit_projection exactly as before.
    """
    country_box = union_bbox(names)
    if country_box is None:
        return None
    points = []
    for place in places or []:
        try:
            points.append((float(place[0]), float(place[1])))
        except (TypeError, ValueError, IndexError, KeyError):
            return None
    if len(set(points)) < 2:
        return None
    places_box = places_bbox(points)
    if not is_clustered(places_box, country_box):
        return None
    return Projection(padded_places_bbox(places_box), rect=rect)


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
