"""Phase 7 / A-11: Light vignette + Cinematic gradient must render.

Pixel-difference assertions on composited fixture frames, plus an
offered-set consistency test: every background treatment any theme
offers has a renderer in core.Canvas.
"""

from services.motion_engine import core, themes


def _frame(tokens, with_pattern=True):
    canvas = core.Canvas(
        bg=tokens["bg"],
        pattern=tokens.get("background_pattern") if with_pattern else None,
        pattern_color=tokens.get("pattern_color"),
    )
    return canvas.composite()


def test_every_offered_background_pattern_has_a_renderer():
    offered = {
        tokens["background_pattern"]
        for tokens in themes.THEMES.values()
        if isinstance(tokens, dict) and tokens.get("background_pattern")
    }
    assert offered, "expected at least the grid treatment to be offered"
    assert offered <= set(core.BACKGROUND_PATTERNS), offered


def test_light_vignette_darkens_edges_not_centre():
    tokens = themes.THEMES["light_pure"]
    assert tokens["background_pattern"] == "vignette"
    treated = _frame(tokens)
    plain = _frame(tokens, with_pattern=False)
    centre_t = treated.getpixel((960, 540))
    centre_p = plain.getpixel((960, 540))
    corner_t = treated.getpixel((5, 5))
    corner_p = plain.getpixel((5, 5))
    # Centre is (nearly) the plain white field.
    assert all(abs(a - b) <= 6 for a, b in zip(centre_t, centre_p))
    # Corners measurably darken toward the pattern colour.
    assert sum(corner_p) - sum(corner_t) > 120, (corner_t, corner_p)
    assert corner_t != corner_p


def test_cinematic_gradient_varies_top_to_bottom():
    tokens = themes.THEMES["cinematic_smoke"]
    assert tokens["background_pattern"] == "gradient"
    treated = _frame(tokens)
    plain = _frame(tokens, with_pattern=False)
    top = treated.getpixel((960, 2))
    mid = treated.getpixel((960, 540))
    bottom = treated.getpixel((960, 1077))
    assert top == plain.getpixel((960, 2)) or sum(
        abs(a - b) for a, b in zip(top, plain.getpixel((960, 2)))) <= 3
    # Bottom reaches (nearly) the pattern colour and differs
    # measurably from both the plain field and the top.
    assert all(abs(a - b) <= 3
               for a, b in zip(bottom, tokens["pattern_color"]))
    assert sum(abs(a - b) for a, b in zip(bottom, top)) > 60
    assert sum(abs(a - b) for a, b in zip(mid, top)) > 20
