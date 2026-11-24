"""Theme design system (Master Directive Phase 6, AM-024;
extended by Phase 9 with the founder theme vocabulary, §13.3).

The documentary themes as DATA. Each theme is a full motion
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
    # --- Phase 9 (Pre-Production Configuration Studio): the
    # founder theme vocabulary (§13.3) as real engine token sets.
    # Each founder theme below is a full token set the engine
    # renders from — selecting one changes backgrounds, surfaces,
    # typography colours and accents on every graphic.
    # Deep navy solid — founder "Blue".
    "deep_blue": _tokens(
        "deep_blue",
        bg=(8, 20, 44), surface=(14, 32, 64),
        text=(235, 242, 252), muted=(140, 165, 200),
        accent=(64, 150, 255), accent2=(240, 190, 80),
        paper=(196, 208, 220), brass=(150, 160, 170),
    ),
    # Pure white ground — founder "Light" (the founder reference
    # pairs the white field with a soft dark vignette edge; the
    # token set governs the field and type colours, which is what
    # the engine paints from).
    "light_pure": _tokens(
        "light_pure",
        bg=(255, 255, 255), surface=(244, 244, 246),
        text=(18, 20, 24), muted=(110, 114, 120),
        accent=(30, 90, 200), accent2=(200, 110, 30),
        paper=(240, 238, 230), brass=(150, 128, 84),
    ),
    # Black field with fine grid lines — founder "Grid". The
    # `background_pattern` token is honoured by the compositor
    # (services.motion_engine.core.Canvas): the base frame is
    # painted with a fine grid in `pattern_color` before any
    # layer is drawn.
    "grid_mono": {
        **_tokens(
            "grid_mono",
            bg=(0, 0, 0), surface=(10, 10, 10),
            text=(245, 245, 245), muted=(150, 150, 150),
            accent=(255, 255, 255), accent2=(120, 120, 120),
            paper=(230, 230, 230), brass=(170, 170, 170),
        ),
        "background_pattern": "grid",
        "pattern_color": (64, 64, 64),
    },
    # Restraint as a treatment — founder "Minimal": near-white
    # field, near-monochrome type, hairline surfaces, no colour
    # accent competing with the content.
    "minimal_mono": _tokens(
        "minimal_mono",
        bg=(250, 250, 250), surface=(255, 255, 255),
        text=(30, 30, 30), muted=(140, 140, 140),
        accent=(30, 30, 30), accent2=(160, 160, 160),
        paper=(244, 244, 244), brass=(150, 150, 150),
    ),
    # Newsprint — founder "Newspaper": warm paper field, black
    # type, a single press red.
    "newsprint": _tokens(
        "newsprint",
        bg=(242, 238, 228), surface=(252, 250, 244),
        text=(20, 18, 14), muted=(110, 102, 88),
        accent=(178, 34, 34), accent2=(30, 60, 120),
        paper=(242, 238, 228), brass=(120, 100, 70),
    ),
    # The dossier — founder "Archive": mid-sepia field with the
    # paper surface dominant and an amber accent; distinct from
    # `archival` (near-black field, deep red) so Archive and
    # Vintage are not the same treatment twice.
    "archive_dossier": _tokens(
        "archive_dossier",
        bg=(46, 38, 26), surface=(216, 201, 163),
        text=(250, 244, 228), muted=(180, 164, 128),
        accent=(232, 150, 52), accent2=(140, 42, 32),
    ),
    # Dark smoky brown — founder "Cinematic" (the reference's
    # textured gradient is approximated by the engine's textured
    # background painters over this brown field).
    "cinematic_smoke": _tokens(
        "cinematic_smoke",
        bg=(26, 20, 16), surface=(44, 34, 26),
        text=(244, 238, 228), muted=(168, 150, 128),
        accent=(232, 164, 76), accent2=(120, 140, 160),
    ),
}

#: Phase 9 (§13.3): the founder theme vocabulary and the engine
#: theme each name selects. "custom" is deliberately absent: a
#: custom theme is a base theme plus validated token overrides,
#: resolved by services.production_config into a concrete token
#: dict (resolve_theme passes token dicts through).
FOUNDER_THEMES = {
    "blue": "deep_blue",
    "light": "light_pure",
    "dark": "editorial_dark",
    "vintage": "archival",
    "grid": "grid_mono",
    "minimal": "minimal_mono",
    "documentary": "documentary_clean",
    "newspaper": "newsprint",
    "archive": "archive_dossier",
    "cinematic": "cinematic_smoke",
}


def founder_theme_engine_name(founder_id):
    """The engine theme name a founder theme id (§13.3) selects,
    or None when the id names no founder theme (including
    "custom", which resolves to a token dict, not a name)."""
    if not isinstance(founder_id, str):
        return None
    return FOUNDER_THEMES.get(founder_id.strip().lower())

#: Pre-theme pack `visual_theme` values and the theme each selects.
LEGACY_THEME_MAP = {
    "editorial": "editorial_dark",
    "history": "archival",
    "true_crime": "investigative",
}

DEFAULT_THEME = "editorial_dark"


def resolve_theme(name):
    """The theme dict for a theme name or a legacy pack
    `visual_theme` value, or None when the value names no theme.

    A token dict (a Phase 9 custom theme: a base theme's tokens
    with validated overrides, carrying its own `name` and `bg`)
    passes through unchanged — the dict IS the theme."""
    if isinstance(name, dict):
        if name.get("name") and isinstance(name.get("bg"), (tuple, list)):
            return name
        return None
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
