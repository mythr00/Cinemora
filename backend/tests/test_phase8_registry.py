"""Phase 8 — motion-registry migration of the template builders.

Phase 6 landed the registry as the named vocabulary but the 14
template builders still imported core's easing helpers directly.
Phase 8 migrates the reference: both template modules resolve
their easings THROUGH the registry. The registry re-exports
core's exact callables, so the migration is provably
rendering-neutral — these tests pin identity, not approximation.

Run-10 fix (2026-11-22): the registry grows to 15 with
"closing_card", the T5 coverage-guarantee template.

P0 removal (2026-11-27): the founder's ruling removed eleven
registered templates completely — closing_card, evidence_board,
red_string_board, photo_parallax, outcome_path,
milestone_waveform and the map family (territory_map, border_map,
route_map, trade_route_map, military_map) — shrinking the registry
from 53 to 42. This file now pins the post-removal vocabulary.
"""

from services.motion_engine import core, motion_registry, templates, templates_v2


def test_templates_resolve_easings_through_registry():
    for module in (templates, templates_v2):
        assert module.ease_out is motion_registry.EASINGS["ease_out"]
        assert module.ease_in_out is motion_registry.EASINGS["ease_in_out"]
        # And the registry's entries ARE core's implementations —
        # the same functions the templates used before migration.
        assert module.ease_out is core.ease_out
        assert module.ease_in_out is core.ease_in_out


def test_registry_covers_all_fifteen_templates():
    # Wave 1 (Phase 2.5) superseded the count: the registry grew
    # from 15 to 31 — the original fifteen plus the sixteen Wave 1
    # registrations (F1 charts, F3 map variants, F4 investigation
    # variants, F5 schematic variants, F6/F7 text treatments, F8
    # parallax). Wave 2 (breadth) grows it again, 31 to 49: the
    # eighteen Wave 2 registrations (F1 completion, F2 checklist /
    # waveform, F3 expansion / satellite zoom / heatmap, F4
    # classified file / newspaper wall / crime-scene schematic,
    # F5 construction / molecular, F7/F8 HUD / holographic /
    # before-after / layered reveal / object labels). The GitHub
    # Skills programme grows it again, 49 to 51: the two Manim-engine
    # registrations (manim_line_chart, manim_bar_chart); and 51 to
    # 53 with the two matplotlib chart registrations (mpl_line_chart,
    # mpl_bar_chart).
    # P0 removal (2026-11-27) supersedes the count again: eleven
    # low-quality templates were removed completely — 53 down to
    # 42 — and the removed names must NOT be in the vocabulary.
    # Phase 5 core treatments (2026-11-27) grow it again, 42 to
    # 46: the rebuilt treatments register under NEW names
    # (chronology / route / locator / relationship_network) —
    # the removed names stay absent, asserted below unchanged.
    # Phase 6-7 (D1): +1 labelled_diagram -> 47.
    names = set(templates.TEMPLATES) | set(templates_v2.V2_TEMPLATES)
    assert len(names) == 47
    assert "manim_line_chart" in names and "manim_bar_chart" in names
    assert "mpl_line_chart" in names and "mpl_bar_chart" in names
    for name in (
        "closing_card", "evidence_board", "red_string_board",
        "photo_parallax", "outcome_path", "milestone_waveform",
        "territory_map", "border_map", "route_map",
        "trade_route_map", "military_map",
    ):
        assert name not in names, name
    for name in (
        "line_chart", "pie_chart", "exploded_view",
        "mechanical_cutaway", "blueprint_section",
        "newspaper_montage", "profile_reveal",
        "text_overlay", "text_highlight", "archival_title",
    ):
        assert name in names, name
    for name in (
        "bubble_chart", "population_pyramid", "animated_dashboard",
        "checklist_graphic",
        "territory_expansion", "satellite_zoom", "heatmap",
        "classified_file", "newspaper_wall", "crime_scene_diagram",
        "construction_sequence", "molecular_diagram",
        "hud_overlay", "holographic_panel", "before_after",
        "layered_reveal", "object_labels",
    ):
        assert name in names, name


def test_easing_behaviour_unchanged_at_sample_points():
    for fn in (templates.ease_out, templates_v2.ease_in_out):
        assert fn(0.0) == 0.0
        assert fn(1.0) == 1.0
        assert 0.0 < fn(0.5) < 1.0
