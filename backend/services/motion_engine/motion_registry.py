"""Shared motion registry (Master Directive Phase 6, AM-024/Stage 12).

ONE named vocabulary for animation across the motion engine, the
graphics assemblers and the composition engine:

* EASINGS — the easing curves, implemented by services.motion_engine
  .core (the registry re-exports them so there is exactly one place
  that names them; templates and composition code resolve easings
  BY NAME through here instead of importing ad-hoc helpers).
* ENTRANCES / EXITS — named layer/graphic entrance and exit
  behaviours with the easing and nominal duration each uses.
  Composition layers reference these names; an unknown name is a
  validation failure, never a silent default.
* DURATIONS — the named duration steps (seconds) entrances/exits
  and layer animations draw from.

The registry is data + the core easing functions. It renders
nothing and reads nothing. Existing templates keep their internal
rendering code (they already draw on core's easings, which are the
functions registered here); new code — composition layers, manifest
animation stamps — must reference registry names.
"""

from services.motion_engine.core import ease_in, ease_in_out, ease_out


def _linear(value):
    return max(0.0, min(1.0, float(value)))


def _clamp01(value):
    return max(0.0, min(1.0, float(value)))


# GSAP-catalogue easings (GitHub Skills Programme, Lane 1, 2026-11-25).
# GSAP itself is REJECTED as an engine (it is a browser animation
# library, not a renderer), but its easing catalogue is the industry
# reference vocabulary; these are the standard published formulas
# (cubic-bezier-equivalent power curves, sine/expo/circ, back with
# the standard 1.70158 overshoot, elastic and bounce) implemented
# directly in Python — no dependency, no GSAP code. Inputs clamp to
# [0, 1]; outputs may overshoot [0, 1] for back/elastic by design.
def _make_power(power, mode):
    def _ease_in(t):
        return _clamp01(t) ** power

    def _ease_out(t):
        return 1.0 - (1.0 - _clamp01(t)) ** power

    def _ease_in_out(t):
        t = _clamp01(t)
        if t < 0.5:
            return (2 ** (power - 1)) * t ** power
        return 1.0 - ((-2.0 * t + 2.0) ** power) / 2.0

    return {"in": _ease_in, "out": _ease_out, "in_out": _ease_in_out}[mode]


def _sine_in(t):
    import math

    return 1.0 - math.cos(_clamp01(t) * math.pi / 2.0)


def _sine_out(t):
    import math

    return math.sin(_clamp01(t) * math.pi / 2.0)


def _sine_in_out(t):
    import math

    return -(math.cos(math.pi * _clamp01(t)) - 1.0) / 2.0


def _expo_in(t):
    t = _clamp01(t)
    return 0.0 if t == 0.0 else 2.0 ** (10.0 * t - 10.0)


def _expo_out(t):
    t = _clamp01(t)
    return 1.0 if t == 1.0 else 1.0 - 2.0 ** (-10.0 * t)


def _expo_in_out(t):
    t = _clamp01(t)
    if t == 0.0:
        return 0.0
    if t == 1.0:
        return 1.0
    if t < 0.5:
        return 2.0 ** (20.0 * t - 10.0) / 2.0
    return (2.0 - 2.0 ** (-20.0 * t + 10.0)) / 2.0


def _circ_in(t):
    import math

    t = _clamp01(t)
    return 1.0 - math.sqrt(max(0.0, 1.0 - t * t))


def _circ_out(t):
    import math

    t = _clamp01(t) - 1.0
    return math.sqrt(max(0.0, 1.0 - t * t))


def _circ_in_out(t):
    import math

    t = _clamp01(t)
    if t < 0.5:
        return (1.0 - math.sqrt(max(0.0, 1.0 - (2.0 * t) ** 2))) / 2.0
    u = -2.0 * t + 2.0
    return (math.sqrt(max(0.0, 1.0 - u * u)) + 1.0) / 2.0


_BACK_S = 1.70158


def _back_in(t):
    t = _clamp01(t)
    return (_BACK_S + 1.0) * t ** 3 - _BACK_S * t ** 2


def _back_out(t):
    u = _clamp01(t) - 1.0
    return 1.0 + (_BACK_S + 1.0) * u ** 3 + _BACK_S * u ** 2


def _back_in_out(t):
    s = _BACK_S * 1.525
    t = _clamp01(t)
    if t < 0.5:
        return ((2.0 * t) ** 2 * ((s + 1.0) * 2.0 * t - s)) / 2.0
    u = 2.0 * t - 2.0
    return (u ** 2 * ((s + 1.0) * u + s) + 2.0) / 2.0


def _elastic_out(t):
    import math

    t = _clamp01(t)
    if t == 0.0:
        return 0.0
    if t == 1.0:
        return 1.0
    return 2.0 ** (-10.0 * t) * math.sin(
        (t * 10.0 - 0.75) * (2.0 * math.pi / 3.0)
    ) + 1.0


def _bounce_out(t):
    t = _clamp01(t)
    n1, d1 = 7.5625, 2.75
    if t < 1.0 / d1:
        return n1 * t * t
    if t < 2.0 / d1:
        t -= 1.5 / d1
        return n1 * t * t + 0.75
    if t < 2.5 / d1:
        t -= 2.25 / d1
        return n1 * t * t + 0.9375
    t -= 2.625 / d1
    return n1 * t * t + 0.984375


#: Named easing curves (single source: core's implementations for
#: the original four; the GSAP-catalogue additions live here).
EASINGS = {
    "linear": _linear,
    "ease_in": ease_in,
    "ease_out": ease_out,
    "ease_in_out": ease_in_out,
    "sine_in": _sine_in,
    "sine_out": _sine_out,
    "sine_in_out": _sine_in_out,
    "cubic_in": _make_power(3, "in"),
    "cubic_out": _make_power(3, "out"),
    "cubic_in_out": _make_power(3, "in_out"),
    "quart_in": _make_power(4, "in"),
    "quart_out": _make_power(4, "out"),
    "quart_in_out": _make_power(4, "in_out"),
    "quint_in": _make_power(5, "in"),
    "quint_out": _make_power(5, "out"),
    "quint_in_out": _make_power(5, "in_out"),
    "expo_in": _expo_in,
    "expo_out": _expo_out,
    "expo_in_out": _expo_in_out,
    "circ_in": _circ_in,
    "circ_out": _circ_out,
    "circ_in_out": _circ_in_out,
    "back_in": _back_in,
    "back_out": _back_out,
    "back_in_out": _back_in_out,
    "elastic_out": _elastic_out,
    "bounce_out": _bounce_out,
}

#: Named duration steps, in seconds.
DURATIONS = {
    "instant": 0.0,
    "short": 0.4,
    "medium": 0.8,
    "long": 1.2,
}

#: Named entrance behaviours for composition layers and graphic
#: overlays. `easing` and `duration` name registry entries above.
ENTRANCES = {
    "fade_in": {"easing": "ease_out", "duration": "short",
                "description": "Opacity 0 -> layer opacity."},
    "rise_in": {"easing": "ease_out", "duration": "medium",
                "description": "Rises 24px into place while fading in."},
    "slide_in_left": {"easing": "ease_out", "duration": "medium",
                      "description": "Slides in from the left edge."},
    "pop_in": {"easing": "ease_out", "duration": "short",
               "description": "Scales 0.92 -> 1.0 while fading in."},
    "cut_in": {"easing": "linear", "duration": "instant",
               "description": "Appears with the frame, no animation."},
    # GSAP-catalogue additions (GitHub Skills Programme, 2026-11-25):
    # entrances that spend the new easings.
    "spring_in": {"easing": "back_out", "duration": "medium",
                  "description": "Scales 0.85 -> 1.0 with a back-out overshoot settle while fading in."},
    "drop_in": {"easing": "bounce_out", "duration": "long",
                "description": "Drops 32px into place with a bounce-out settle while fading in."},
    "sweep_in": {"easing": "expo_out", "duration": "medium",
                 "description": "Slides in from the left edge with an expo-out deceleration."},
}

#: Named exit behaviours.
EXITS = {
    "fade_out": {"easing": "ease_in", "duration": "short",
                 "description": "Opacity -> 0."},
    "fall_out": {"easing": "ease_in", "duration": "medium",
                 "description": "Drops 24px while fading out."},
    "slide_out_left": {"easing": "ease_in", "duration": "medium",
                       "description": "Slides out to the left edge."},
    "cut_out": {"easing": "linear", "duration": "instant",
                "description": "Disappears with the frame, no animation."},
}


def easing_function(name):
    """The easing callable registered under `name`, or None."""
    return EASINGS.get(name)


def duration_seconds(name):
    """The seconds registered under a duration name, or None."""
    return DURATIONS.get(name)


def entrance(name):
    return ENTRANCES.get(name)


def exit_behaviour(name):
    return EXITS.get(name)


def is_entrance(name):
    return name in ENTRANCES


def is_exit(name):
    return name in EXITS


def animation_names():
    """Every animation name a layer may reference (validation)."""
    return {
        "entrances": sorted(ENTRANCES),
        "exits": sorted(EXITS),
        "easings": sorted(EASINGS),
        "durations": sorted(DURATIONS),
    }
