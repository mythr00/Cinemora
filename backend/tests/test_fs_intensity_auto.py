"""FS-3 / FS-E — graphics intensity is backend-auto.

The founder requirement: the INTENSITY selector (Subtle /
Moderate / Dynamic) is removed from the user product; the
backend derives the graphics treatment from the production's
own context. These tests pin the contract:

1. graphics_build_params returns a valid bundle with NO
   intensity configured anywhere.
2. A user-supplied intensity (payload, stored config, preset
   bundle) never changes the derived params — auto wins.
3. Different production contexts derive different bundles per
   the documented rule in
   production_config.derive_graphics_intensity, deterministically.
4. Stored configs carrying the legacy key still load and
   resolve into a run config without error.
"""

from services import production_config


SUBTLE = {
    "enabled": True, "mode": "balanced", "max_stats": 1,
    "compositions": False,
    "intensity": "subtle", "intensity_source": "auto",
}
MODERATE = {
    "enabled": True, "mode": "balanced", "max_stats": None,
    "compositions": True,
    "intensity": "moderate", "intensity_source": "auto",
}
DYNAMIC = {
    "enabled": True, "mode": "heavy", "max_stats": None,
    "compositions": True,
    "intensity": "dynamic", "intensity_source": "auto",
}
OFF = {
    "enabled": False, "mode": "off", "max_stats": 0,
    "compositions": False,
    "intensity": None, "intensity_source": "auto",
}


def test_params_with_no_intensity_configured_anywhere():
    # A bare cleaned config (defaults only) and even a raw
    # empty document both yield the standard bundle.
    config = production_config.clean_config({})
    assert config["motion_graphics"]["intensity"] == "auto"
    assert production_config.graphics_build_params(config) == MODERATE
    assert production_config.graphics_build_params({}) == MODERATE
    assert production_config.graphics_build_params(None) == MODERATE


def test_user_supplied_intensity_never_changes_derived_params():
    context = {
        "graphic_target_count": 2, "sentence_count": 20,
        "timeline_event_count": 1,
    }
    baseline = production_config.graphics_build_params(
        production_config.clean_config({}), context
    )
    assert baseline == MODERATE
    for supplied in ("subtle", "moderate", "dynamic"):
        config = production_config.clean_config(
            {"motion_graphics": {"enabled": True,
                                 "intensity": supplied}}
        )
        # The supplied value is normalised away at cleaning…
        assert config["motion_graphics"]["intensity"] == "auto"
        # …and the derived params are identical to the baseline.
        assert production_config.graphics_build_params(
            config, context
        ) == baseline
    # Even an uncleaned document handed straight to the params
    # builder cannot steer it: the stored value is never read.
    raw = {"motion_graphics": {"enabled": True,
                               "intensity": "dynamic"}}
    assert production_config.graphics_build_params(
        raw, context
    ) == baseline
    # A nonsense value is ignored, not a crash.
    cleaned = production_config.clean_config(
        {"motion_graphics": {"intensity": "loud"}}
    )
    assert cleaned["motion_graphics"]["intensity"] == "auto"


def test_context_derives_each_bundle_deterministically():
    config = production_config.clean_config({})
    # No beat needs a graphic -> subtle (lightest bundle).
    no_targets = {
        "graphic_target_count": 0, "sentence_count": 31,
        "timeline_event_count": 3,
    }
    assert production_config.graphics_build_params(
        config, no_targets
    ) == SUBTLE
    # An ordinary share of beats needs graphics -> moderate.
    ordinary = {
        "graphic_target_count": 3, "sentence_count": 31,
        "timeline_event_count": 3,
    }
    assert production_config.graphics_build_params(
        config, ordinary
    ) == MODERATE
    # >= 25% of sentences need graphics -> dynamic.
    graphics_led = {
        "graphic_target_count": 8, "sentence_count": 31,
        "timeline_event_count": 3,
    }
    assert production_config.graphics_build_params(
        config, graphics_led
    ) == DYNAMIC
    # A chronology-dense bible (>= 8 dated events) -> dynamic
    # even with a modest target share.
    chronology_led = {
        "graphic_target_count": 2, "sentence_count": 31,
        "timeline_event_count": 8,
    }
    assert production_config.graphics_build_params(
        config, chronology_led
    ) == DYNAMIC
    # Determinism: same context, same params, every time.
    for context in (no_targets, ordinary, graphics_led,
                    chronology_led):
        first = production_config.derive_graphics_intensity(context)
        for _ in range(3):
            assert production_config.derive_graphics_intensity(
                context
            ) == first


def test_toggle_off_wins_over_every_context():
    config = production_config.clean_config(
        {"motion_graphics": {"enabled": False}}
    )
    heavy_context = {
        "graphic_target_count": 30, "sentence_count": 31,
        "timeline_event_count": 20,
    }
    assert production_config.graphics_build_params(
        config, heavy_context
    ) == OFF


def test_legacy_stored_config_loads_and_resolves():
    # A project options document written by the pre-FS-3 build:
    # the stored config carries a user-chosen intensity.
    legacy_options = {
        "production_config": {
            "version": 1,
            "preset": "custom",
            "motion_graphics": {"enabled": True,
                                "intensity": "dynamic"},
            "theme": {"id": None},
            "music": {"enabled": False, "source": "none"},
            "narration": {"source": "ai"},
            "captions": {"mode": "burn", "sync": "word"},
        }
    }
    stored = production_config.stored_config(legacy_options)
    assert stored is not None
    assert stored["motion_graphics"]["intensity"] == "auto"

    # Run resolution over the legacy document succeeds and the
    # resolved snapshot records auto-derived graphics params.
    resolved = production_config.resolve_run_options(
        None, dict(legacy_options)
    )
    graphics = resolved["production_config_resolved"]["graphics"]
    assert graphics == MODERATE
    assert resolved["motion_graphics_intensity"] == "auto"
    assert resolved["motion_graphics_enabled"] is True


def test_preset_bundles_keep_identity_but_not_intensity():
    # A-1 reading: presets may keep storing the key as inert
    # data; applying one must not restore a user-chosen effect,
    # and the preset's honest label must survive (presets are
    # distinguished by theme/music/captions, not intensity).
    for preset_id in production_config.PRESETS:
        bundle = production_config.preset_config(preset_id)
        assert bundle["motion_graphics"]["intensity"] == "auto"
        assert production_config.matching_preset(bundle) == preset_id
    cinematic = production_config.preset_config("cinematic")
    context = {
        "graphic_target_count": 0, "sentence_count": 12,
        "timeline_event_count": 0,
    }
    # The cinematic bundle historically carried "dynamic"; with
    # a zero-demand context the derived bundle is subtle.
    assert production_config.graphics_build_params(
        cinematic, context
    ) == SUBTLE
