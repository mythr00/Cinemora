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


#: Named easing curves (single source: core's implementations).
EASINGS = {
    "linear": _linear,
    "ease_in": ease_in,
    "ease_out": ease_out,
    "ease_in_out": ease_in_out,
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
