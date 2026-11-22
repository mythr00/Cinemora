"""Phase 8 — motion-registry migration of the template builders.

Phase 6 landed the registry as the named vocabulary but the 14
template builders still imported core's easing helpers directly.
Phase 8 migrates the reference: both template modules resolve
their easings THROUGH the registry. The registry re-exports
core's exact callables, so the migration is provably
rendering-neutral — these tests pin identity, not approximation.

Run-10 fix (2026-11-22): the registry grows to 15 with
"closing_card", the T5 coverage-guarantee template.
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
    # Wave 1 (Phase 2.5) supersedes the count: the registry grew
    # from 15 to 31 — the original fifteen plus the sixteen Wave 1
    # registrations (F1 charts, F3 map variants, F4 investigation
    # variants, F5 schematic variants, F6/F7 text treatments, F8
    # parallax). The fifteen-template vocabulary itself is intact.
    names = set(templates.TEMPLATES) | set(templates_v2.V2_TEMPLATES)
    assert len(names) == 31
    # The run-10 T5 guarantee template is part of the vocabulary.
    assert "closing_card" in names
    for name in (
        "line_chart", "pie_chart", "border_map", "route_map",
        "trade_route_map", "military_map", "exploded_view",
        "mechanical_cutaway", "blueprint_section", "red_string_board",
        "newspaper_montage", "profile_reveal", "photo_parallax",
        "text_overlay", "text_highlight", "archival_title",
    ):
        assert name in names, name


def test_easing_behaviour_unchanged_at_sample_points():
    for fn in (templates.ease_out, templates_v2.ease_in_out):
        assert fn(0.0) == 0.0
        assert fn(1.0) == 1.0
        assert 0.0 < fn(0.5) < 1.0
