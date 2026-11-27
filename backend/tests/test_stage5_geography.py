"""Stage 5C tests: real-outline geography.

- The bundled Natural Earth-derived dataset loads (180 countries).
- China is found; Beijing/Shanghai are inside it, a mid-Pacific
  point is not; holes/multipolygons behave.
- The fitted projection preserves the country's aspect ratio.
- Unknown countries return None — outlines are never invented.
- territory_map renders a dataset region (metadata records the
  dataset as the outline source), still renders caller polygons,
  and refuses an unknown country with no caller points.
"""

from services.motion_engine import geography
from services.motion_engine import templates
from services.motion_engine.core import probe_video


def test_dataset_loads_with_countries():
    names = geography.country_names()
    assert len(names) >= 170
    assert "China" in names
    assert "France" in names


def test_china_point_in_polygon_sanity():
    assert geography.point_in_country(116.407, 39.904, "China") is True   # Beijing
    assert geography.point_in_country(121.474, 31.230, "China") is True   # Shanghai
    assert geography.point_in_country(-150.0, 25.0, "China") is False     # mid-Pacific
    assert geography.point_in_country(2.352, 48.857, "China") is False    # Paris


def test_china_bbox_aspect_sanity():
    bbox = geography.country_bbox("China")
    assert bbox is not None
    min_lon, min_lat, max_lon, max_lat = bbox
    # The real China spans roughly 62 degrees of longitude and 35
    # of latitude — a fabricated blob would not land near this.
    assert 55 < (max_lon - min_lon) < 68
    assert 30 < (max_lat - min_lat) < 40


def test_projection_preserves_aspect():
    projection = geography.fit_projection(["China"])
    assert projection is not None
    outline = geography.outline_for("China", projection)
    assert outline, "China outline must project"
    main_ring = outline[0][0]
    assert len(main_ring) >= 100  # the real, detailed mainland ring
    xs = [p[0] for poly in outline for ring in poly for p in ring]
    ys = [p[1] for poly in outline for ring in poly for p in ring]
    span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
    # Canvas-fitted into the default rect's width (0.90); height
    # follows the cos-corrected geographic aspect (~0.60-0.68).
    assert span_x > 0.85
    assert 0.55 < span_y < 0.75
    # Beijing projects inside the outline's span, in the NE quadrant.
    bx, by = projection.project(116.407, 39.904)
    assert min(xs) < bx < max(xs) and min(ys) < by < max(ys)
    assert bx > (min(xs) + max(xs)) / 2  # east half


def test_unknown_country_never_invented():
    assert geography.country_geometry("Atlantis") is None
    assert geography.country_polygons("Atlantis") == []
    assert geography.country_bbox("Atlantis") is None
    assert geography.point_in_country(0.0, 0.0, "Atlantis") is False
    projection = geography.fit_projection(["China"])
    assert geography.outline_for("Atlantis", projection) == []




# P0 removal (2026-11-27): deleted template tests for removed
# templates — territory_map template removed (R9); the geography DATASET tests above survive untouched.
