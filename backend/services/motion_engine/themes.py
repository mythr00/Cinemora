"""Six-theme design system (Master Directive Phase 6, AM-024).

The six documentary themes as DATA. Each theme is a full motion
-engine token set (the same shape services.motion_engine.styles
produces) plus a `name`, so `resolve_style` accepts a theme dict
directly and templates record the theme name as their style.

Genre packs select the default theme through their `visual_theme`
field. Pack values predate the six-theme vocabulary ("editorial",
"history", "true_crime" — engine style families), so they map onto
themes explicitly:

    editorial  -> editorial_dark
    history    -> archival
    true_crime -> investigative

A pack may also name one of the six themes directly. An unknown
value resolves to None — the caller keeps its legacy style
behaviour and records that no theme applied; a theme is never
invented for a pack that did not select one.
"""

from services.motion_engine.styles import FONT, FONT_BOLD


def _tokens(name, bg, surface, text, muted, accent, accent2,
            paper=(216, 201, 163), brass=(176, 141, 87)):
    return {
        "name": name,
        "bg": bg,
        "surface": surface,
        "text": text,
        "muted": muted,
        "accent": accent,
        "accent2": accent2,
        "font": FONT,
        "font_bold": FONT_BOLD,
        "paper": paper,
        "brass": brass,
    }


THEMES = {
    # Near-black slate, amber accent — the general pack's default.
    "editorial_dark": _tokens(
        "editorial_dark",
        bg=(13, 18, 26), surface=(27, 36, 50),
        text=(238, 242, 247), muted=(148, 160, 176),
        accent=(240, 176, 64), accent2=(64, 190, 220),
    ),
    # Pure-white ground, near-black text, one restrained blue —
    # the clean studio explainer look.
    "documentary_clean": _tokens(
        "documentary_clean",
        bg=(246, 246, 244), surface=(255, 255, 255),
        text=(22, 24, 28), muted=(104, 110, 118),
        accent=(28, 84, 160), accent2=(196, 120, 32),
        paper=(238, 234, 224), brass=(150, 128, 84),
    ),
    # Aged paper, brass, deep red — the history pack's default.
    "archival": _tokens(
        "archival",
        bg=(24, 20, 14), surface=(216, 201, 163),
        text=(245, 238, 220), muted=(168, 156, 128),
        accent=(154, 42, 32), accent2=(176, 141, 87),
    ),
    # Deep navy data wall, cyan + amber series colours.
    "cinematic_data": _tokens(
        "cinematic_data",
        bg=(6, 12, 24), surface=(13, 26, 46),
        text=(232, 240, 250), muted=(128, 148, 172),
        accent=(56, 198, 236), accent2=(246, 178, 64),
        paper=(196, 208, 220), brass=(150, 160, 170),
    ),
    # Blueprint blue, white line work, yellow annotation accent.
    "technical_blueprint": _tokens(
        "technical_blueprint",
        bg=(10, 42, 84), surface=(16, 58, 112),
        text=(240, 246, 255), muted=(150, 180, 220),
        accent=(255, 205, 48), accent2=(120, 220, 255),
        paper=(200, 220, 245), brass=(170, 190, 215),
    ),
    # Near-black board, off-white cards, red accent — the
    # true-crime pack's default.
    "investigative": _tokens(
        "investigative",
        bg=(8, 8, 10), surface=(232, 230, 224),
        text=(240, 238, 232), muted=(150, 148, 142),
        accent=(198, 40, 40), accent2=(120, 120, 118),
    ),
}

#: Pre-theme pack `visual_theme` values and the theme each selects.
LEGACY_THEME_MAP = {
    "editorial": "editorial_dark",
    "history": "archival",
    "true_crime": "investigative",
}

DEFAULT_THEME = "editorial_dark"


def resolve_theme(name):
    """The theme dict for a theme name or a legacy pack
    `visual_theme` value, or None when the value names no theme."""
    if not isinstance(name, str) or not name:
        return None
    if name in THEMES:
        return THEMES[name]
    mapped = LEGACY_THEME_MAP.get(name)
    if mapped is not None:
        return THEMES[mapped]
    return None


def theme_for_pack(genre_pack):
    """The theme a genre pack selects via `visual_theme`, or None
    when the pack selects none (no pack, or an unmapped value)."""
    if not isinstance(genre_pack, dict):
        return None
    return resolve_theme(genre_pack.get("visual_theme"))
