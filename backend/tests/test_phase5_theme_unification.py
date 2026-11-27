"""Phase 5 (A5) — one typography/theme system pins.

Two style systems used to coexist: motion_engine/styles.py (3
legacy styles, DEFAULT_STYLE="history") and motion_engine/
themes.py (13 themes); a target naming a legacy style silently
rendered in the legacy tokens while a manifest target rendered
in theme tokens — two visual systems in one film. The legacy
module is now a thin alias layer over the themes system:

  * each legacy name resolves to the theme themes.LEGACY_THEME_MAP
    maps it to, with the theme's own tokens and the theme's name;
  * STYLES values ARE the theme dicts (one token set per system);
  * theme names and founder theme ids resolve through themes;
  * an unknown name raises instead of silently falling back.
"""

import pytest

from services.motion_engine import styles, themes

LEGACY_EXPECTED = {
    "history": "archival",
    "true_crime": "investigative",
    "editorial": "editorial_dark",
}


@pytest.mark.parametrize("legacy,theme_name", LEGACY_EXPECTED.items())
def test_legacy_style_names_resolve_to_their_mapped_theme(legacy, theme_name):
    name, tokens = styles.resolve_style(legacy)
    assert name == theme_name
    assert tokens is themes.THEMES[theme_name]


@pytest.mark.parametrize("legacy,theme_name", LEGACY_EXPECTED.items())
def test_styles_mapping_values_are_the_theme_dicts(legacy, theme_name):
    assert styles.STYLES[legacy] is themes.THEMES[theme_name]


def test_legacy_alias_map_is_the_themes_map():
    assert styles.LEGACY_STYLE_ALIASES == themes.LEGACY_THEME_MAP


@pytest.mark.parametrize("theme_name", sorted(themes.THEMES))
def test_every_theme_name_resolves_to_itself(theme_name):
    name, tokens = styles.resolve_style(theme_name)
    assert name == theme_name
    assert tokens is themes.THEMES[theme_name]


@pytest.mark.parametrize("founder_id,theme_name", themes.FOUNDER_THEMES.items())
def test_founder_theme_ids_resolve_to_their_engine_theme(founder_id, theme_name):
    name, tokens = styles.resolve_style(founder_id)
    assert name == theme_name
    assert tokens is themes.THEMES[theme_name]


def test_theme_dicts_pass_through_with_their_name():
    for theme_name, theme in themes.THEMES.items():
        name, tokens = styles.resolve_style(theme)
        assert name == theme_name
        assert tokens is theme


def test_no_request_defaults_through_the_themes_system():
    name, tokens = styles.resolve_style(None)
    assert name == "archival"  # DEFAULT_STYLE history -> archival
    assert tokens is themes.THEMES["archival"]
    name, tokens = styles.resolve_style("")
    assert name == "archival"


def test_unknown_style_name_raises_instead_of_silent_fallback():
    with pytest.raises(ValueError):
        styles.resolve_style("no_such_style")
    with pytest.raises(ValueError):
        styles.resolve_style("histroy")  # a typo is not a theme


def test_resolved_tokens_carry_the_full_engine_token_shape():
    for legacy in LEGACY_EXPECTED:
        _name, tokens = styles.resolve_style(legacy)
        for key in ("bg", "surface", "text", "muted", "accent",
                    "accent2", "font", "font_bold", "paper", "brass"):
            assert key in tokens


def test_templates_record_the_theme_name_for_a_legacy_request(tmp_path):
    # Render-level pin (values, not pixels): a template asked
    # for the legacy "history" style renders with — and records
    # — the mapped theme.
    from services.motion_engine.templates_v3 import archival_title

    rendered, meta = archival_title(
        {"title": "Coffee"}, "history", tmp_path / "t.mp4",
        duration=1.0,
    )
    assert rendered is not None
    assert meta["style"] == "archival"
