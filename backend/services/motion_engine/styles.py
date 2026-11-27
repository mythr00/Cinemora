"""Style tokens for the Cinematic Motion Graphics Engine.

Phase 5 (A5): this module is a THIN ALIAS LAYER over the one
typography/theme system, services.motion_engine.themes. Two
style systems used to coexist — three legacy styles here, 13
themes there — and a target naming a legacy style silently
rendered in the legacy tokens while a manifest target rendered
in theme tokens, so one film mixed two visual systems.

Now the three legacy names survive as NAMES only. Each resolves
through themes.LEGACY_THEME_MAP to the theme the genre packs
already used:

    history    -> archival
    true_crime -> investigative
    editorial  -> editorial_dark

STYLES maps each legacy name to the theme's own token dict (the
same object — there is exactly one token set per visual
system), and resolve_style returns the THEME's name and tokens
for every input, so what a target requested is what its record
shows. An unknown name raises ValueError: no target may
silently render in a different visual system than the one it
asked for.

Fonts are the bundled DejaVu pair — no downloads, ever.
"""
from pathlib import Path

from services.motion_engine import themes as _themes

_FONT_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"
FONT = _FONT_DIR / "DejaVuSans.ttf"
FONT_BOLD = _FONT_DIR / "DejaVuSans-Bold.ttf"

#: The legacy style names and the theme each one aliases (the
#: themes system's own mapping — one definition, two readers).
LEGACY_STYLE_ALIASES = dict(_themes.LEGACY_THEME_MAP)

#: Backwards-compatible name -> tokens mapping. The values ARE
#: the themes system's token dicts, so existing readers
#: (genre-pack validation, the package export) see the unified
#: tokens under the legacy names.
STYLES = {
    name: _themes.THEMES[theme_name]
    for name, theme_name in LEGACY_STYLE_ALIASES.items()
}

#: The default when a caller requests no style at all: the
#: historical default name, resolved through the themes system
#: like every other name (history -> archival).
DEFAULT_STYLE = "history"


def resolve_style(style):
    """Accept a style/theme name or a token dict; always return
    (name, tokens) resolved through the themes system.

    - A token dict (a theme, or a Phase 9 custom theme carrying
      validated overrides) passes through with its own name.
    - A legacy style name returns its mapped theme.
    - A theme name, or a founder theme id (§13.3), returns that
      theme.
    - None / empty returns the default (history -> archival).
    - Anything else raises ValueError — silently falling back
      to a different visual system is the divergence this alias
      layer exists to remove."""
    if isinstance(style, dict):
        return style.get("name", "custom"), style
    if style is None or (isinstance(style, str) and not style.strip()):
        theme = _themes.resolve_theme(DEFAULT_STYLE)
        return theme["name"], theme
    name = str(style).strip()
    theme = _themes.resolve_theme(name)
    if theme is None:
        founder = _themes.founder_theme_engine_name(name)
        if founder is not None:
            theme = _themes.THEMES[founder]
    if theme is None:
        raise ValueError(
            f"unknown motion-engine style/theme: {name!r} — "
            "expected a theme name, a founder theme id, or one "
            f"of the legacy aliases {sorted(LEGACY_STYLE_ALIASES)}"
        )
    return theme["name"], theme
