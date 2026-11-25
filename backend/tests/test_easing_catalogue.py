"""GSAP-catalogue easing port (GitHub Skills Programme, Lane 1).

GSAP was rejected as an engine (a browser library, not a renderer);
its easing vocabulary was adopted as a spec and implemented in
pure Python in motion_registry. These tests pin the maths: exact
endpoints, monotonicity for the non-overshoot curves, the designed
overshoot of back/elastic, and — critically — that the original
four registry easings are still core's exact callables (the Phase 8
identity contract).
"""

import pytest

from services.motion_engine import core, motion_registry

SAMPLES = [i / 20 for i in range(21)]

#: Curves that must never overshoot or run backwards.
MONOTONIC = [
    "linear", "ease_in", "ease_out", "ease_in_out",
    "sine_in", "sine_out", "sine_in_out",
    "cubic_in", "cubic_out", "cubic_in_out",
    "quart_in", "quart_out", "quart_in_out",
    "quint_in", "quint_out", "quint_in_out",
    "expo_in", "expo_out", "expo_in_out",
    "circ_in", "circ_out", "circ_in_out",
]


def test_original_four_identities_preserved():
    assert motion_registry.EASINGS["ease_in"] is core.ease_in
    assert motion_registry.EASINGS["ease_out"] is core.ease_out
    assert motion_registry.EASINGS["ease_in_out"] is core.ease_in_out


def test_every_easing_hits_exact_endpoints():
    for name, fn in motion_registry.EASINGS.items():
        assert fn(0.0) == pytest.approx(0.0, abs=1e-9), name
        assert fn(1.0) == pytest.approx(1.0, abs=1e-9), name


def test_monotonic_curves_stay_in_range_and_never_reverse():
    for name in MONOTONIC:
        fn = motion_registry.EASINGS[name]
        values = [fn(t) for t in SAMPLES]
        assert all(0.0 <= v <= 1.0 for v in values), name
        assert all(b >= a - 1e-12 for a, b in zip(values, values[1:])), name


def test_bounce_out_stays_in_range_but_is_not_monotonic():
    # A bounce rises, falls back and rises again by design — it is
    # range-bounded, never monotonic.
    fn = motion_registry.EASINGS["bounce_out"]
    values = [fn(t) for t in SAMPLES]
    assert all(0.0 <= v <= 1.0 for v in values)
    assert any(b < a for a, b in zip(values, values[1:]))


def test_back_out_overshoots_by_design():
    fn = motion_registry.EASINGS["back_out"]
    assert max(fn(t) for t in SAMPLES) > 1.0


def test_elastic_out_settles_at_one():
    fn = motion_registry.EASINGS["elastic_out"]
    assert fn(1.0) == 1.0
    # It oscillates around the settle point mid-curve.
    assert max(fn(t) for t in SAMPLES) > 1.0


def test_new_entrances_reference_registered_easings():
    for name in ("spring_in", "drop_in", "sweep_in"):
        assert motion_registry.is_entrance(name)
        entry = motion_registry.entrance(name)
        assert entry["easing"] in motion_registry.EASINGS
        assert motion_registry.duration_seconds(entry["duration"]) is not None


def test_inputs_clamp_outside_unit_interval():
    for name, fn in motion_registry.EASINGS.items():
        assert fn(-0.5) == pytest.approx(fn(0.0), abs=1e-9), name
        assert fn(1.5) == pytest.approx(fn(1.0), abs=1e-9), name
