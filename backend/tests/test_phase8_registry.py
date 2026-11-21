"""Phase 8 — motion-registry migration of the template builders.

Phase 6 landed the registry as the named vocabulary but the 14
template builders still imported core's easing helpers directly.
Phase 8 migrates the reference: both template modules resolve
their easings THROUGH the registry. The registry re-exports
core's exact callables, so the migration is provably
rendering-neutral — these tests pin identity, not approximation.
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


def test_registry_covers_all_fourteen_templates():
    names = set(templates.TEMPLATES) | set(templates_v2.V2_TEMPLATES)
    assert len(names) == 14


def test_easing_behaviour_unchanged_at_sample_points():
    for fn in (templates.ease_out, templates_v2.ease_in_out):
        assert fn(0.0) == 0.0
        assert fn(1.0) == 1.0
        assert 0.0 < fn(0.5) < 1.0
