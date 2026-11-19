"""Style tokens for the Cinematic Motion Graphics Engine.

Each style is a plain dict of RGB tuples + bundled-font paths.
Templates render ONLY caller-supplied data; styles carry no content.
Fonts are the bundled DejaVu pair — no downloads, ever.
"""
from pathlib import Path

_FONT_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"
FONT = _FONT_DIR / "DejaVuSans.ttf"
FONT_BOLD = _FONT_DIR / "DejaVuSans-Bold.ttf"


def _tokens(bg, surface, text, muted, accent, accent2):
    return {
        "bg": bg,
        "surface": surface,
        "text": text,
        "muted": muted,
        "accent": accent,
        "accent2": accent2,
        "font": FONT,
        "font_bold": FONT_BOLD,
        "paper": (216, 201, 163),
        "brass": (176, 141, 87),
    }


STYLES = {
    # Aged paper / brass / cream / deep red — founder reference 1/4 family.
    "history": _tokens(
        bg=(24, 20, 14),
        surface=(216, 201, 163),
        text=(245, 238, 220),
        muted=(168, 156, 128),
        accent=(154, 42, 32),
        accent2=(176, 141, 87),
    ),
    # Near-black board, off-white cards, red accent — reference 1/3/5 family.
    "true_crime": _tokens(
        bg=(8, 8, 10),
        surface=(232, 230, 224),
        text=(240, 238, 232),
        muted=(150, 148, 142),
        accent=(198, 40, 40),
        accent2=(120, 120, 118),
    ),
    # Clean dark slate, single amber accent — reference 2/4 family.
    "editorial": _tokens(
        bg=(13, 18, 26),
        surface=(27, 36, 50),
        text=(238, 242, 247),
        muted=(148, 160, 176),
        accent=(240, 176, 64),
        accent2=(64, 190, 220),
    ),
}

DEFAULT_STYLE = "history"


def resolve_style(style):
    """Accept a style name or a token dict; always return (name, tokens)."""
    if isinstance(style, dict):
        return style.get("name", "custom"), style
    name = str(style or DEFAULT_STYLE)
    return name, STYLES.get(name, STYLES[DEFAULT_STYLE])
