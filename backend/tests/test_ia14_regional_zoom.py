"""IA-14 — regional map zoom (implements CF-02).

geography.regional_projection frames a territory map on the
labelled places' bbox (padded) instead of the whole country when
the places CLUSTER: their lon/lat bbox area is below
geography.CLUSTER_AREA_FRACTION (0.10) of the country-union bbox
area. When places are spread it returns None and the whole-country
fit is byte-identical to before.

Cases use real dataset geography (China) and the benchmark's own
story: the four Yangtze places (Shanghai, Nanking, Rose Island,
Woosung) cluster within ~2.7 x ~1.0 degrees; a spread set spans
the country (Beijing / Shanghai / Guangzhou / Urumqi).
"""

import pytest

from services.motion_engine import geography, templates
from services.motion_engine.core import probe_video

# (lon, lat) — the lower-Yangtze cluster from benchmark v2 piece 01.
YANGTZE_PLACES = [
    (121.474, 31.230),  # Shanghai
    (118.807, 32.060),  # Nanking
    (119.650, 32.250),  # Rose Island (approx.)
    (121.500, 31.380),  # Woosung
]
YANGTZE_LABELS = ["SHANGHAI", "NANKING", "ROSE ISLAND", "WOOSUNG"]

# (lon, lat) — places spread across the whole country.
SPREAD_PLACES = [
    (116.407, 39.904),  # Beijing
    (121.474, 31.230),  # Shanghai
    (113.264, 23.129),  # Guangzhou
    (87.617, 43.826),   # Urumqi
]
SPREAD_LABELS = ["BEIJING", "SHANGHAI", "GUANGZHOU", "URUMQI"]


def _markers(projection, places, labels):
    return [
        {"at": list(projection.project(lon, lat)), "label": label}
        for (lon, lat), label in zip(places, labels)
    ]


# ---------------------------------------------------------------------------
# Clustering definition + projection choice (geography level)
# ---------------------------------------------------------------------------


def test_area_fractions_define_the_two_cases():
    country_box = geography.country_bbox("China")
    clustered_fraction = geography.places_area_fraction(
        geography.places_bbox(YANGTZE_PLACES), country_box
    )
    spread_fraction = geography.places_area_fraction(
        geography.places_bbox(SPREAD_PLACES), country_box
    )
    # The Yangtze cluster is a tiny sliver of China; the spread set
    # covers a substantial share of it.
    assert clustered_fraction < 0.01
    assert clustered_fraction < geography.CLUSTER_AREA_FRACTION
    assert spread_fraction > geography.CLUSTER_AREA_FRACTION
    assert geography.is_clustered(
        geography.places_bbox(YANGTZE_PLACES), country_box
    ) is True
    assert geography.is_clustered(
        geography.places_bbox(SPREAD_PLACES), country_box
    ) is False


def test_clustered_places_get_regional_framing():
    regional = geography.regional_projection(["China"], YANGTZE_PLACES)
    assert regional is not None

    places_box = geography.places_bbox(YANGTZE_PLACES)
    country_box = geography.country_bbox("China")

    # The framing bbox IS the regional one: the padded places bbox,
    # not the country bbox.
    assert regional.bbox == geography.padded_places_bbox(places_box)
    assert regional.bbox != country_box
    # It strictly contains every labelled place (padding all round).
    assert regional.bbox[0] < places_box[0]
    assert regional.bbox[1] < places_box[1]
    assert regional.bbox[2] > places_box[2]
    assert regional.bbox[3] > places_box[3]
    # And it is far smaller than the country frame.
    assert (regional.bbox[2] - regional.bbox[0]) < 0.2 * (
        country_box[2] - country_box[0]
    )
    assert (regional.bbox[3] - regional.bbox[1]) < 0.2 * (
        country_box[3] - country_box[1]
    )

    # The projection scale differs decisively from the country fit:
    # the same places, a knot at country scale, spread across most
    # of the canvas under the regional fit.
    country = geography.fit_projection(["China"])
    assert regional.scale > 3 * country.scale
    regional_xs = [regional.project(lon, lat)[0] for lon, lat in YANGTZE_PLACES]
    country_xs = [country.project(lon, lat)[0] for lon, lat in YANGTZE_PLACES]
    assert max(regional_xs) - min(regional_xs) > 0.45
    assert max(country_xs) - min(country_xs) < 0.10
    # Every place projects INSIDE the default canvas rect.
    rx0, ry0, rx1, ry1 = geography.DEFAULT_RECT
    for lon, lat in YANGTZE_PLACES:
        x, y = regional.project(lon, lat)
        assert rx0 <= x <= rx1
        assert ry0 <= y <= ry1


def test_spread_places_keep_country_framing_unchanged():
    # No regional projection is offered for spread places...
    assert geography.regional_projection(["China"], SPREAD_PLACES) is None
    # ...and the country fit is exactly what it was before IA-14:
    # the union bbox of the named countries, nothing else.
    projection = geography.fit_projection(["China"])
    assert projection.bbox == geography.country_bbox("China")
    assert geography.union_bbox(["China"]) == geography.country_bbox("China")
    outline = geography.outline_for("China", projection)
    xs = [p[0] for poly in outline for ring in poly for p in ring]
    ys = [p[1] for poly in outline for ring in poly for p in ring]
    # Same spans the Stage 5C tests pin for the country fit.
    assert max(xs) - min(xs) > 0.85
    assert 0.55 < (max(ys) - min(ys)) < 0.75


def test_regional_projection_guards():
    # Fewer than two distinct places cannot define a region.
    assert geography.regional_projection(["China"], [YANGTZE_PLACES[0]]) is None
    assert geography.regional_projection(
        ["China"], [YANGTZE_PLACES[0], YANGTZE_PLACES[0]]
    ) is None
    # Unknown country: never invented, never framed.
    assert geography.regional_projection(["Atlantis"], YANGTZE_PLACES) is None
    assert geography.regional_projection(["China"], []) is None
    assert geography.regional_projection(["China"], None) is None


def test_projection_inverse_round_trips():
    projection = geography.fit_projection(["China"])
    for lon, lat in YANGTZE_PLACES + SPREAD_PLACES:
        x, y = projection.project(lon, lat)
        lon2, lat2 = projection.inverse(x, y)
        assert lon2 == pytest.approx(lon, abs=1e-6)
        assert lat2 == pytest.approx(lat, abs=1e-6)


# ---------------------------------------------------------------------------
# territory_map template integration
# ---------------------------------------------------------------------------


def test_territory_map_clustered_places_zoom_to_region(tmp_path):
    country = geography.fit_projection(["China"])
    markers = _markers(country, YANGTZE_PLACES, YANGTZE_LABELS)
    route_points = [list(country.project(lon, lat)) for lon, lat in YANGTZE_PLACES]
    path, meta = templates.territory_map(
        {
            "title": "The Yangtze Incident, 1949",
            "regions": [{"country": "China", "label": "CHINA"}],
            "routes": [{"points": route_points, "dashed": True}],
            "markers": markers,
        },
        "history", tmp_path / "clustered.mp4", duration=1.2,
    )
    assert path is not None
    info = probe_video(path)
    assert info["width"] == 1920 and info["height"] == 1080
    assert meta["inputs"]["outline_sources"] == ["dataset"]
    # The render records its regional framing truthfully.
    assert meta["inputs"]["framing"] == "regional"
    regional = geography.regional_projection(["China"], YANGTZE_PLACES)
    assert meta["inputs"]["framing_bbox"] == [
        round(v, 6) for v in regional.bbox
    ]
    assert meta["inputs"]["places_bbox"] == [
        round(v, 6) for v in geography.places_bbox(YANGTZE_PLACES)
    ]
    assert meta["inputs"]["country_bbox"] == [
        round(v, 6) for v in geography.country_bbox("China")
    ]
    assert (
        meta["inputs"]["places_area_fraction"]
        < geography.CLUSTER_AREA_FRACTION
    )
    assert meta["inputs"]["framing_bbox"] != meta["inputs"]["country_bbox"]


def test_territory_map_spread_places_keep_country_frame(tmp_path):
    country = geography.fit_projection(["China"])
    markers = _markers(country, SPREAD_PLACES, SPREAD_LABELS)
    path, meta = templates.territory_map(
        {
            "title": "Across China",
            "regions": [{"country": "China", "label": "CHINA"}],
            "markers": markers,
        },
        "history", tmp_path / "spread.mp4", duration=1.2,
    )
    assert path is not None
    info = probe_video(path)
    assert info["width"] == 1920 and info["height"] == 1080
    # Byte-identical behaviour: the metadata carries exactly the
    # legacy inputs — no framing keys appear at all.
    assert "framing" not in meta["inputs"]
    assert "framing_bbox" not in meta["inputs"]
    assert set(meta["inputs"]) == {
        "regions", "routes", "title", "outline_sources", "markers",
        "dashed_routes",
    }
    assert meta["inputs"]["outline_sources"] == ["dataset"]


def test_territory_map_without_labelled_places_never_zooms(tmp_path):
    country = geography.fit_projection(["China"])
    # Clustered positions but NO labels: unlabelled markers are not
    # "labelled places", so the country frame stays.
    markers = [
        {"at": list(country.project(lon, lat))}
        for lon, lat in YANGTZE_PLACES
    ]
    path, meta = templates.territory_map(
        {
            "regions": [{"country": "China", "label": "CHINA"}],
            "markers": markers,
        },
        "history", tmp_path / "unlabelled.mp4", duration=1.2,
    )
    assert path is not None
    assert "framing" not in meta["inputs"]


def test_territory_map_caller_polygon_never_zooms(tmp_path):
    # Caller-supplied polygons are normalised canvas geometry, not
    # dataset geography: clustered labelled markers must not drag
    # them into a geographic re-frame.
    path, meta = templates.territory_map(
        {
            "regions": [{
                "label": "Testland",
                "points": [[0.2, 0.3], [0.8, 0.3], [0.8, 0.7], [0.2, 0.7]],
            }],
            "markers": [
                {"at": [0.50, 0.50], "label": "ALPHA"},
                {"at": [0.52, 0.51], "label": "BETA"},
            ],
        },
        "editorial", tmp_path / "caller.mp4", duration=1.2,
    )
    assert path is not None
    assert meta["inputs"]["outline_sources"] == ["caller"]
    assert "framing" not in meta["inputs"]
