"""Phase 9 — Pre-Production Configuration Studio (§§13–17).

The configuration a production runs with, as ONE structured
document (`production_config`), stored on the project (inside
its options, beside the flat keys it projects to) and snapshotted
onto every run at submission (§14.2: stored on the project +
production job and respected by every stage — never
frontend-only).

The enforcement model: the pipeline consumes FLAT option keys
(voice_id, captions, the Stage H music contract, the mix keys).
This module is the single place the structured configuration is
resolved into those keys:

- at project creation and on every configuration save, the flat
  projection is written onto the project's options next to the
  document, so every existing reader (templates' field audit,
  the project view) sees the same truth;
- at run submission, `resolve_run_options` re-resolves from the
  stored document authoritatively and stamps the resolved
  snapshot into the run's options — the ledger/manifest record
  the Versioning surface renders as the run's configuration
  history;
- `validate_config` is the pre-generation gate (§15): a run
  whose configuration the pipeline cannot honour (a voice that
  would silently fall back, an uploaded track that is not
  there, a library that does not exist) is REFUSED with the
  specific error, never started.

New pipeline surface this phase adds, all consumed by real
stages: motion-graphics enable + backend-auto intensity (FS-3:
jobs' motion_designer stage derives the treatment bundle from
the production's own context — see derive_graphics_intensity;
no user-facing intensity setting exists), the founder
visual themes (the motion engine's theme system), music
placement sections (the ONE mix implementation in
services.music), and the voice-to-documentary flow
(services.voice_documentary, over services.transcription).

Deliberate exclusions, each because nothing consumes it:
- narration/voice SPEED in any form — retired by founder
  ruling 2026-11-22; the key is refused by name wherever it
  appears in a submitted configuration;
- an SFX toggle — the pipeline has no SFX stage, so a stored
  switch would be inert;
- narration source "uploaded_voice" (the user's own recording
  AS the film's narration) — the narration adapter's interface
  is per-sentence synthesis (services.narration); an uploaded
  recording feeds the SCRIPT through voice-to-documentary
  (§13.4) instead. The value is refused with its reason, never
  silently accepted.
"""

import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from services import media_paths
from services.motion_engine import themes as themes_module


CONFIG_KEY = "production_config"
DEFAULTS_SECTION = "production_config"

#: The three graphics-treatment bundles remain the INTERNAL
#: vocabulary of graphics_build_params, but since FS-3 the
#: bundle is no longer a user setting: derive_graphics_intensity
#: picks one from the production's own context at run time, and
#: a stored/submitted configuration carries only the AUTO
#: marker in its place.
INTENSITIES = ("subtle", "moderate", "dynamic")
AUTO_INTENSITY = "auto"
CAPTION_MODES = ("burn", "sidecar", "off")
#: C-13 (Phase 7): syllable sync is DERIVED timing (stamped as
#: such by the caption renderer) and is no longer a user-facing
#: option. CAPTION_SYNCS is the user-offered set; the legacy value
#: "syllable" stays ACCEPTED at cleaning (existing clients and
#: stored documents must not 422) and is normalised to "word" —
#: accept-and-ignore, exactly like FS-3 graphics intensity. The
#: renderer's internal derived-syllable mechanism is untouched
#: and remains visible in admin diagnostics (the ASS header stamp).
CAPTION_SYNCS = ("word", "off")
CAPTION_SYNCS_ACCEPTED = ("word", "syllable", "off")
MUSIC_SOURCES = ("none", "upload", "library")
MUSIC_PLACEMENTS = ("throughout", "sections")
NARRATION_SOURCES = ("ai",)
TOKEN_KEYS = (
    "bg", "surface", "text", "muted", "accent", "accent2",
    "paper", "brass",
)
# The Stage J mix keys a configuration may carry (the same keys
# ProjectOptionsIn bounds and services.music.resolve_mix_config
# consumes).
MIX_KEYS = (
    "music_fade_in_seconds", "music_fade_out_seconds",
    "music_loop_crossfade_seconds", "duck_threshold", "duck_ratio",
    "duck_attack_ms", "duck_release_ms",
)
_MIX_BOUNDS = {
    "music_fade_in_seconds": (0.0, 30.0),
    "music_fade_out_seconds": (0.0, 30.0),
    "music_loop_crossfade_seconds": (0.0, 10.0),
    "duck_threshold": (0.0, 1.0),
    "duck_ratio": (1.0, 100.0),
    "duck_attack_ms": (0.0, 10000.0),
    "duck_release_ms": (0.0, 60000.0),
}

#: §14 presets: named bundles of REAL settings — every field is
#: a field this module resolves and a stage consumes. Applying a
#: preset fills exactly these fields; everything stays editable
#: afterwards (the stored document then reads preset "custom"
#: unless it still equals the bundle verbatim).
#:
#: FS-3 note: the bundles below still carry an "intensity"
#: entry as inert bundle data (A-1 reading) — clean_config
#: normalises it to the AUTO marker, so applying a preset can
#: never restore a user-chosen graphics intensity; presets are
#: distinguished by their theme/music/captions fields.
PRESETS = {
    "clean": {
        "label": "Clean Documentary",
        "config": {
            "motion_graphics": {"enabled": True, "intensity": "subtle"},
            "theme": {"id": "documentary"},
            "music": {"enabled": False, "source": "none"},
            "narration": {"source": "ai"},
            "captions": {"mode": "burn", "sync": "word"},
        },
    },
    "cinematic": {
        "label": "Cinematic Documentary",
        "config": {
            "motion_graphics": {"enabled": True, "intensity": "dynamic"},
            "theme": {"id": "cinematic"},
            "music": {"enabled": True, "source": "none"},
            "narration": {"source": "ai"},
            "captions": {"mode": "burn", "sync": "word"},
        },
    },
    "vintage": {
        "label": "Vintage Documentary",
        "config": {
            "motion_graphics": {"enabled": True, "intensity": "moderate"},
            "theme": {"id": "vintage"},
            "music": {"enabled": True, "source": "none"},
            "narration": {"source": "ai"},
            "captions": {"mode": "burn", "sync": "word"},
        },
    },
    "minimal": {
        "label": "Minimal Production",
        "config": {
            "motion_graphics": {"enabled": True, "intensity": "subtle"},
            "theme": {"id": "minimal"},
            "music": {"enabled": False, "source": "none"},
            "narration": {"source": "ai"},
            "captions": {"mode": "burn", "sync": "word"},
        },
    },
}


class ConfigError(Exception):
    """A configuration refusal in the API's error shape."""

    def __init__(self, status_code, code, message):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _refuse(code, message):
    raise ConfigError(422, code, message)


# ---------------------------------------------------------------------
# Cleaning: a submitted document -> the stored document, or a refusal
# ---------------------------------------------------------------------


def _check_retired_keys(raw, path="production_config"):
    """Founder ruling 2026-11-22: narration speed exists
    nowhere. The key is refused BY NAME wherever it appears in a
    submitted configuration, at any depth."""
    if isinstance(raw, dict):
        for key, value in raw.items():
            if str(key).strip().lower() in ("narration_speed", "speed"):
                _refuse(
                    "retired_option",
                    f"{path}.{key}: narration speed control was "
                    "retired by founder ruling (2026-11-22) — the "
                    "voice model is third-party and speed control "
                    "is unsupported. Remove the key.",
                )
            _check_retired_keys(value, f"{path}.{key}")
    elif isinstance(raw, list):
        for index, value in enumerate(raw):
            _check_retired_keys(value, f"{path}[{index}]")


def _bool(section, key, value):
    if not isinstance(value, bool):
        _refuse("invalid_config", f"{section}.{key} must be a boolean.")
    return value


def _enum(section, key, value, choices):
    if value not in choices:
        _refuse(
            "invalid_config",
            f"{section}.{key} must be one of: {', '.join(choices)}.",
        )
    return value


def _number(section, key, value, *, lo, hi):
    try:
        number = float(value)
    except (TypeError, ValueError):
        _refuse("invalid_config", f"{section}.{key} must be a number.")
    if not (lo <= number <= hi):
        _refuse(
            "invalid_config",
            f"{section}.{key} must be between {lo} and {hi}.",
        )
    return number


def _section(raw, name, allowed_keys):
    section = raw.get(name)
    if section is None:
        return {}
    if not isinstance(section, dict):
        _refuse("invalid_config", f"{name} must be an object.")
    unknown = sorted(set(section) - set(allowed_keys))
    if unknown:
        _refuse(
            "unknown_config_key",
            f"{name} carries unknown key(s): {', '.join(unknown)}. "
            "Only settings the pipeline consumes may be stored.",
        )
    return section


def _clean_rgb(value, *, section, key):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 3
        or any(
            not isinstance(c, int) or isinstance(c, bool)
            or not (0 <= c <= 255)
            for c in value
        )
    ):
        _refuse(
            "invalid_config",
            f"{section}.{key} must be an [r, g, b] triple of "
            "integers between 0 and 255.",
        )
    return [int(c) for c in value]


def _clean_upload_record(value, *, section):
    """An upload record is system-written (by the upload
    endpoint), never user-authored: a submitted record is kept
    only in its recorded shape."""
    if value is None:
        return None
    if not isinstance(value, dict) or not value.get("file_path"):
        _refuse("invalid_config", f"{section} must be an upload record.")
    return {
        "id": value.get("id"),
        "file_name": value.get("file_name"),
        "file_path": value.get("file_path"),
        "content_sha256": value.get("content_sha256"),
        "duration_seconds": value.get("duration_seconds"),
        "codec": value.get("codec"),
        "uploaded_at": value.get("uploaded_at"),
    }


def clean_config(raw) -> dict:
    """Validate + normalise a submitted configuration document.
    Returns the stored shape (defaults filled). Raises
    ConfigError(422) with a specific code on any problem —
    unknown keys are refused, never silently kept, because a
    stored-but-inert key is exactly the failure §14.2 forbids."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _refuse("invalid_config", "production_config must be an object.")
    _check_retired_keys(raw)
    unknown = sorted(
        set(raw)
        - {"version", "preset", "motion_graphics", "theme", "music",
           "narration", "captions"}
    )
    if unknown:
        _refuse(
            "unknown_config_key",
            f"production_config carries unknown key(s): "
            f"{', '.join(unknown)}.",
        )

    config = {"version": 1}

    preset = raw.get("preset") or "custom"
    if preset != "custom" and preset not in PRESETS:
        _refuse(
            "invalid_config",
            f"preset must be 'custom' or one of: "
            f"{', '.join(sorted(PRESETS))}.",
        )
    config["preset"] = preset

    mg = _section(raw, "motion_graphics", {"enabled", "intensity"})
    config["motion_graphics"] = {
        "enabled": _bool("motion_graphics", "enabled", mg.get("enabled", True)),
        # FS-3: graphics intensity is backend-auto, never a user
        # choice. The "intensity" key stays an accepted member of
        # the section so legacy clients, stored documents and
        # preset bundles keep loading, but any supplied value is
        # discarded here — the stored document carries the AUTO
        # marker, and the real bundle is derived per production
        # at run time by derive_graphics_intensity(). The
        # sibling "enabled" toggle is a real user setting and is
        # preserved untouched.
        "intensity": AUTO_INTENSITY,
    }

    theme = _section(raw, "theme", {"id", "base", "tokens"})
    theme_id = theme.get("id")
    if theme_id is not None:
        theme_id = str(theme_id).strip().lower()
        if theme_id != "custom" and theme_id not in themes_module.FOUNDER_THEMES:
            _refuse(
                "unknown_theme",
                f"theme.id {theme_id!r} names no visual theme. "
                f"Known: {', '.join(sorted(themes_module.FOUNDER_THEMES))}, "
                "custom.",
            )
    cleaned_theme = {"id": theme_id}
    if theme_id == "custom":
        base = theme.get("base") or "dark"
        base_engine = themes_module.founder_theme_engine_name(base)
        if base_engine is None and base not in themes_module.THEMES:
            _refuse(
                "unknown_theme",
                f"theme.base {base!r} names no theme to build on.",
            )
        cleaned_theme["base"] = base
        tokens_raw = theme.get("tokens") or {}
        if not isinstance(tokens_raw, dict):
            _refuse("invalid_config", "theme.tokens must be an object.")
        unknown_tokens = sorted(set(tokens_raw) - set(TOKEN_KEYS))
        if unknown_tokens:
            _refuse(
                "unknown_config_key",
                f"theme.tokens carries unknown key(s): "
                f"{', '.join(unknown_tokens)}. Known tokens: "
                f"{', '.join(TOKEN_KEYS)}.",
            )
        cleaned_theme["tokens"] = {
            key: _clean_rgb(value, section="theme.tokens", key=key)
            for key, value in tokens_raw.items()
        }
    elif theme.get("tokens") or theme.get("base"):
        _refuse(
            "invalid_config",
            "theme.tokens/theme.base belong to the custom theme "
            "only; set theme.id to 'custom' to use them.",
        )
    config["theme"] = cleaned_theme

    music = _section(
        raw, "music",
        {"enabled", "source", "upload", "track_id", "mood", "volume",
         "placement", "sections", "mix"},
    )
    cleaned_music = {
        "enabled": _bool("music", "enabled", music.get("enabled", True)),
        "source": _enum(
            "music", "source", music.get("source", "none"), MUSIC_SOURCES
        ),
        "upload": _clean_upload_record(music.get("upload"), section="music.upload"),
        "track_id": (
            str(music["track_id"]).strip() if music.get("track_id") else None
        ),
        "mood": (
            str(music["mood"]).strip().lower() if music.get("mood") else None
        ),
        "volume": (
            _number("music", "volume", music["volume"], lo=0.0, hi=2.0)
            if music.get("volume") is not None else None
        ),
        "placement": _enum(
            "music", "placement",
            music.get("placement", "throughout"), MUSIC_PLACEMENTS,
        ),
        "sections": music.get("sections") or [],
    }
    if cleaned_music["placement"] == "sections":
        # The mix's own validator is the authority on ranges;
        # reuse it so the stored shape is always mix-legal.
        from services import music as music_service

        try:
            music_service._validated_music_sections({
                "music_placement": "sections",
                "music_sections": cleaned_music["sections"],
            })
        except ValueError as error:
            _refuse("invalid_music_sections", str(error))
        cleaned_music["sections"] = [
            {"start": float(s["start"]), "end": float(s["end"])}
            for s in cleaned_music["sections"]
        ]
    mix_raw = music.get("mix") or {}
    if not isinstance(mix_raw, dict):
        _refuse("invalid_config", "music.mix must be an object.")
    unknown_mix = sorted(set(mix_raw) - set(MIX_KEYS))
    if unknown_mix:
        _refuse(
            "unknown_config_key",
            f"music.mix carries unknown key(s): {', '.join(unknown_mix)}.",
        )
    cleaned_music["mix"] = {
        key: _number("music.mix", key, mix_raw[key], lo=lo, hi=hi)
        for key, (lo, hi) in _MIX_BOUNDS.items()
        if mix_raw.get(key) is not None
    }
    config["music"] = cleaned_music

    narration = _section(
        raw, "narration",
        {"source", "voice_id", "voice_recording", "language"},
    )
    source = narration.get("source", "ai")
    if source == "uploaded_voice":
        _refuse(
            "narration_source_unsupported",
            "narration.source 'uploaded_voice' is not supported: "
            "the narration adapter synthesises per sentence, so "
            "an uploaded recording cannot serve as the film's "
            "narration. Upload a recording to turn it into the "
            "script instead (voice-to-documentary).",
        )
    # Item 6: the production language, chosen before generation
    # and authoritative. Unknown codes refuse here; known codes
    # without narration support clean fine and are refused at
    # validate_config / run submission with the honest reason —
    # never silently replaced with English.
    from services import languages as languages_service

    language = str(narration.get("language") or "en").strip().lower()
    if not languages_service.is_known(language):
        _refuse(
            "language_unknown",
            f"narration.language {language!r} is not a language "
            "Cinemora supports. Supported languages: "
            + ", ".join(sorted(languages_service.LANGUAGE_NAMES))
            + ".",
        )
    cleaned_narration = {
        "language": language,
        "source": _enum("narration", "source", source, NARRATION_SOURCES),
        "voice_id": (
            str(narration["voice_id"]).strip()
            if narration.get("voice_id") else None
        ),
        "voice_recording": _clean_upload_record(
            narration.get("voice_recording"), section="narration.voice_recording"
        ),
    }
    config["narration"] = cleaned_narration

    captions = _section(raw, "captions", {"mode", "sync"})
    _sync = _enum(
        "captions", "sync", captions.get("sync", "word"),
        CAPTION_SYNCS_ACCEPTED,
    )
    config["captions"] = {
        "mode": _enum(
            "captions", "mode", captions.get("mode", "burn"), CAPTION_MODES
        ),
        # C-13: a submitted "syllable" is accepted and demoted to
        # the auto word sync — derived timing is an internal
        # renderer fallback, not a user choice.
        "sync": "word" if _sync == "syllable" else _sync,
    }
    return config


def preset_config(preset_id) -> dict:
    """The cleaned configuration a preset names (§14 bundles)."""
    if preset_id not in PRESETS:
        _refuse("invalid_config", f"Unknown preset: {preset_id!r}.")
    return clean_config({**PRESETS[preset_id]["config"], "preset": preset_id})


def matching_preset(config) -> str:
    """The preset a stored configuration still equals verbatim,
    or "custom" — a preset label is only claimed while the
    bundle's fields are exactly what will run."""
    for preset_id in PRESETS:
        bundle = preset_config(preset_id)
        if all(config.get(key) == bundle.get(key) for key in bundle
               if key not in ("version", "preset")):
            return preset_id
    return "custom"


def with_honest_preset_label(config) -> dict:
    """A copy of the configuration whose preset label states
    what the fields actually are: the named preset while the
    document equals its bundle verbatim, "custom" otherwise.
    Applied at every store/resolve point so a label can never
    claim a bundle the fields do not carry."""
    labelled = dict(config)
    labelled["preset"] = matching_preset(config)
    return labelled


# ---------------------------------------------------------------------
# Theme resolution: founder id / custom -> engine name + token dict
# ---------------------------------------------------------------------


def resolve_theme(config) -> dict:
    """Resolve the configuration's theme to the motion engine's
    terms: {founder_id, engine_name, tokens}. founder_id None
    means no theme was selected — the genre pack's own theme
    then applies, exactly as before Phase 9."""
    theme = (config or {}).get("theme") or {}
    founder_id = theme.get("id")
    if not founder_id:
        return {"founder_id": None, "engine_name": None, "tokens": None}
    if founder_id == "custom":
        base = theme.get("base") or "dark"
        base_engine = (
            themes_module.founder_theme_engine_name(base) or base
        )
        base_tokens = themes_module.resolve_theme(base_engine)
        tokens = dict(base_tokens)
        for key, value in (theme.get("tokens") or {}).items():
            tokens[key] = tuple(value)
        tokens["name"] = "custom"
        return {
            "founder_id": "custom",
            "engine_name": "custom",
            "tokens": tokens,
        }
    engine_name = themes_module.founder_theme_engine_name(founder_id)
    return {
        "founder_id": founder_id,
        "engine_name": engine_name,
        "tokens": themes_module.resolve_theme(engine_name),
    }


def theme_pack_override(config, genre_pack):
    """The genre pack copy the theme consumers must see: the
    pack unchanged when no theme is configured, else a shallow
    copy whose visual_theme is the resolved engine theme (a
    name, or the custom token dict — resolve_theme passes token
    dicts through). Every other pack field rides untouched, so
    composition affinities and template data are the pack's own."""
    resolved = resolve_theme(config)
    if resolved["founder_id"] is None:
        return genre_pack, resolved
    pack = dict(genre_pack or {})
    pack["visual_theme"] = (
        resolved["tokens"]
        if resolved["founder_id"] == "custom"
        else resolved["engine_name"]
    )
    return pack, resolved


# ---------------------------------------------------------------------
# Music library (Stage I catalogue — read through its own loader)
# ---------------------------------------------------------------------


def catalogue_path():
    raw = os.environ.get("CINEMORA_MUSIC_CATALOGUE", "").strip()
    return Path(raw) if raw else None


def load_library():
    """(catalogue, asset_root) or (None, None) when no library
    is configured. A configured-but-invalid catalogue raises the
    library's own CatalogueValidationError — a broken library is
    surfaced, never silently treated as an empty one."""
    path = catalogue_path()
    if path is None:
        return None, None
    from services import music_library

    catalogue = music_library.load_catalogue(path, asset_root=path.parent)
    return catalogue, path.parent


def library_state() -> dict:
    """The catalogue view for the configuration surface:
    whether a library exists at all, and its real tracks."""
    try:
        catalogue, _root = load_library()
    except Exception as error:
        return {
            "configured": catalogue_path() is not None,
            "available": False,
            "error": f"{type(error).__name__}: {error}",
            "tracks": [],
        }
    if catalogue is None:
        return {"configured": False, "available": False, "tracks": []}
    return {
        "configured": True,
        "available": not catalogue.empty,
        "tracks": [
            {
                "track_id": track.track_id,
                "title": track.title,
                "composer": track.composer,
                "moods": list(track.moods),
                "duration_seconds": track.duration_seconds,
                "licence_state": track.licence_state,
                "source_class": track.source_class,
            }
            for track in catalogue.tracks
        ],
    }


def resolve_library_track(config):
    """The library track the configuration selects, as
    (track_record, asset_path) — by track_id, else by mood
    (first catalogue track carrying the mood, catalogue order).
    (None, None) when the configuration selects no library
    track; raises ConfigError when a selection cannot be
    honoured."""
    music = (config or {}).get("music") or {}
    if music.get("source") != "library":
        return None, None
    catalogue, root = load_library()
    if catalogue is None or catalogue.empty:
        _refuse(
            "music_library_unavailable",
            "Music source is the library, but no music catalogue "
            "is configured on this deployment (CINEMORA_MUSIC_"
            "CATALOGUE is unset or the catalogue is empty).",
        )
    track = None
    if music.get("track_id"):
        track = catalogue.by_id(music["track_id"])
        if track is None:
            _refuse(
                "music_track_unknown",
                f"Library track {music['track_id']!r} is not in "
                "the configured catalogue.",
            )
    elif music.get("mood"):
        for candidate in catalogue.tracks:
            if music["mood"] in [m.lower() for m in candidate.moods]:
                track = candidate
                break
        if track is None:
            _refuse(
                "music_mood_unmatched",
                f"No library track carries the mood "
                f"{music['mood']!r}.",
            )
    if track is None:
        return None, None
    return track, root / track.assets.full


# ---------------------------------------------------------------------
# Flat projection: the document -> the option keys stages consume
# ---------------------------------------------------------------------


#: Flat keys ONLY the configuration projection writes. When a
#: configuration is saved or a run is resolved, these keys are
#: first cleared from the target options and then re-written
#: from the projection — otherwise a stale music_file (or theme
#: token set) from an earlier configuration would keep being
#: consumed after the user changed the setting that produced
#: it. Shared-vocabulary keys (voice_id, captions, music_volume,
#: the mix keys) are NOT in this set: the projection writes them
#: when the configuration names them and leaves any independent
#: value alone otherwise.
PROJECTION_OWNED_KEYS = (
    "music_file", "music_track_id", "music_sections",
    "visual_theme", "visual_theme_engine", "visual_theme_tokens",
    "motion_graphics_enabled", "motion_graphics_intensity",
    "music_placement",
)


def _apply_projection(options, config) -> dict:
    """Write a configuration + its flat projection onto an
    options dict (projection-owned keys cleared first)."""
    for key in PROJECTION_OWNED_KEYS:
        options.pop(key, None)
    options[CONFIG_KEY] = config
    options.update(flat_options(config))
    return options


def flat_options(config) -> dict:
    """Project the configuration onto the flat option keys the
    pipeline consumes. This is the ONLY translation between the
    structured document and the stages."""
    flat = {}
    mg = config["motion_graphics"]
    flat["motion_graphics_enabled"] = mg["enabled"]
    # FS-3: the flat key has no pipeline readers; it records
    # that intensity is auto-derived at run time, never a stored
    # user value — so the AUTO marker is written unconditionally,
    # whatever an uncleaned document might carry.
    flat["motion_graphics_intensity"] = AUTO_INTENSITY

    resolved_theme = resolve_theme(config)
    if resolved_theme["founder_id"] is not None:
        flat["visual_theme"] = resolved_theme["founder_id"]
        flat["visual_theme_engine"] = resolved_theme["engine_name"]
        if resolved_theme["founder_id"] == "custom":
            flat["visual_theme_tokens"] = {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in resolved_theme["tokens"].items()
            }

    music = config["music"]
    flat["music_enabled"] = music["enabled"]
    if music["enabled"]:
        if music["source"] == "upload" and music.get("upload"):
            flat["music_file"] = music["upload"]["file_path"]
        elif music["source"] == "library":
            # An unresolvable library selection projects no
            # file; validate_config reports the reason. The
            # projection itself never raises, so read views of a
            # stored configuration stay readable.
            try:
                track, path = resolve_library_track(config)
            except ConfigError:
                track, path = None, None
            if track is not None:
                flat["music_file"] = str(path)
                flat["music_track_id"] = track.track_id
    if music.get("volume") is not None:
        flat["music_volume"] = music["volume"]
    flat["music_placement"] = music["placement"]
    if music["placement"] == "sections":
        flat["music_sections"] = music["sections"]
    for key, value in (music.get("mix") or {}).items():
        flat[key] = value

    narration = config["narration"]
    if narration.get("voice_id"):
        flat["voice_id"] = narration["voice_id"]
    elif (narration.get("language") or "en") != "en":
        # Kokoro switch (2026-11-26): a production in a supported
        # non-English language with no explicit voice must still
        # narrate in ITS language — project the language's
        # primary installed voice rather than let the narration
        # default (English) read foreign text under that
        # language's label.
        from services import narration as _narration_service

        _default_voice = _narration_service.default_voice_for_language(
            narration.get("language") or "en"
        )
        if _default_voice:
            flat["voice_id"] = _default_voice
    flat["language"] = narration.get("language") or "en"
    flat["captions"] = config["captions"]["mode"]
    flat["caption_sync"] = config["captions"]["sync"]
    return flat


# ---------------------------------------------------------------------
# Graphics intensity — backend-auto (FS-3)
# ---------------------------------------------------------------------

#: Auto-derivation thresholds. "Dynamic" (the heavy bundle) is
#: for productions graphics genuinely carry: at least one in
#: four narration sentences needs a graphic, or the case bible's
#: timeline is dense enough (>= 8 dated events) that chronology
#: graphics are a structural part of the film.
GRAPHICS_DYNAMIC_TARGET_SHARE = 0.25
GRAPHICS_DYNAMIC_TIMELINE_EVENTS = 8

#: The three treatment bundles in the graphics stage's own
#: terms — the exact parameter sets the pre-FS-3 user selector
#: chose among, unchanged:
#: - subtle: balanced build, the stat-family cap tightened to
#:   1, and the Visual Director's composition layer OFF
#:   (graphics only where a beat needs one);
#: - moderate: the standard balance (balanced, the Stage A cap,
#:   compositions on);
#: - dynamic: heavy build (stat family uncapped), compositions
#:   on.
_GRAPHICS_BUNDLES = {
    "subtle": {"mode": "balanced", "max_stats": 1, "compositions": False},
    "moderate": {"mode": "balanced", "max_stats": None, "compositions": True},
    "dynamic": {"mode": "heavy", "max_stats": None, "compositions": True},
}


def _context_count(context, key) -> int:
    try:
        value = int((context or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def derive_graphics_intensity(context=None) -> str:
    """FS-3: choose the graphics-treatment bundle from the
    production's OWN context — never from a user-supplied value.

    The rule (deterministic; same context, same bundle; no
    randomness, no provider calls). Signals are counts the
    pipeline has already computed by the time the motion
    designer stage runs:

    - context is None (no production context available, e.g.
      the run-submission snapshot): "moderate", the standard
      balance — the bundle a default production always got.
    - graphic_target_count == 0 (no narration sentence needs a
      graphic — footage covered every beat): "subtle", the
      lightest bundle; there is nothing for a heavier treatment
      to serve.
    - graphic targets are >= 25% of all narration sentences,
      or the bible timeline carries >= 8 dated events:
      "dynamic" — graphics/chronology carry a structural share
      of this film, so the heavy, uncapped bundle applies.
    - otherwise (some beats need graphics, an ordinary share):
      "moderate".

    `context` may also carry descriptive keys (genre_pack_id,
    scene_count) which are recorded by callers for the
    production record; the derivation itself reads only the
    three demand counts above.
    """
    if context is None:
        return "moderate"
    targets = _context_count(context, "graphic_target_count")
    if targets <= 0:
        return "subtle"
    sentences = _context_count(context, "sentence_count")
    if (
        sentences > 0
        and targets / sentences >= GRAPHICS_DYNAMIC_TARGET_SHARE
    ):
        return "dynamic"
    if (
        _context_count(context, "timeline_event_count")
        >= GRAPHICS_DYNAMIC_TIMELINE_EVENTS
    ):
        return "dynamic"
    return "moderate"


def graphics_build_params(config, context=None) -> dict:
    """The motion-graphics treatment in the graphics stage's own
    terms (jobs' motion_designer stage consumes exactly this):

    - disabled (the preserved user on/off toggle): mode "off" —
      build_motion_graphics_assets returns [] before any chain
      runs, so no graphic is inserted, including as the T5
      coverage fallback (§13.1);
    - enabled: the bundle derive_graphics_intensity(context)
      selects — FS-3 backend-auto. Any "intensity" value stored
      on the config (legacy documents, preset bundles, old
      clients) is inert and never read here.

    The returned dict carries the derived bundle name under
    "intensity" and "intensity_source": "auto" so the
    production record shows what actually ran and why.
    """
    mg = (config or {}).get("motion_graphics") or {}
    if not mg.get("enabled", True):
        return {
            "enabled": False, "mode": "off", "max_stats": 0,
            "compositions": False,
            "intensity": None, "intensity_source": "auto",
        }
    intensity = derive_graphics_intensity(context)
    return {
        "enabled": True,
        "intensity": intensity,
        "intensity_source": "auto",
        **_GRAPHICS_BUNDLES[intensity],
    }


# ---------------------------------------------------------------------
# Pre-generation validation (§15): can the pipeline honour this?
# ---------------------------------------------------------------------


def validate_config(config) -> dict:
    """Validate a CLEANED configuration against the live system:
    the installed voice catalogue, the filesystem (uploaded
    tracks), and the configured music library. Returns
    {"errors": [{code, message}], "warnings": [{code, message}]}.
    Errors mean a run must not start; warnings are facts the
    user should see before generation (chiefly §13.1's coverage
    consequence of disabling motion graphics)."""
    errors = []
    warnings = []

    narration = config["narration"]
    # Item 6: a language with no installed narration voice must
    # stop the run BEFORE charging, with the honest reason.
    from services import languages as languages_service

    block_reason = languages_service.production_block_reason(
        narration.get("language")
    )
    if block_reason:
        errors.append(
            {
                "code": "language_narration_unsupported",
                "message": block_reason,
            }
        )
    if narration.get("voice_id"):
        from services import narration as narration_service

        _model, _cfg, _resolved, fell_back = narration_service.resolve_voice(
            narration["voice_id"]
        )
        if fell_back:
            errors.append({
                "code": "voice_not_installed",
                "message": (
                    f"Voice {narration['voice_id']!r} is not in "
                    "the installed voice catalogue; narration "
                    "would silently fall back to the default "
                    "voice. Choose an installed voice."
                ),
            })
        elif not _model.exists():
            errors.append({
                "code": "voice_model_missing",
                "message": (
                    f"The model files for voice "
                    f"{narration['voice_id']!r} are not installed "
                    "on this deployment."
                ),
            })
        else:
            # Kokoro switch (2026-11-26): voices now genuinely
            # belong to languages, so an installed voice that
            # speaks a DIFFERENT language than the production's
            # would produce foreign audio under the production
            # language's label — refuse it here, before charging,
            # with the plain reason.
            _voice_language = narration_service.voice_language(
                _resolved
            )
            _production_language = languages_service.normalize(
                narration.get("language")
            )
            if _voice_language != _production_language:
                errors.append({
                    "code": "voice_language_mismatch",
                    "message": (
                        f"Voice {narration['voice_id']!r} speaks "
                        f"{languages_service.name_of(_voice_language)}, "
                        "but the production language is "
                        f"{languages_service.name_of(_production_language)}. "
                        "Choose a voice that speaks the production "
                        "language."
                    ),
                })

    music = config["music"]
    if music["enabled"]:
        if music["source"] == "upload":
            upload = music.get("upload")
            if not upload:
                errors.append({
                    "code": "music_upload_missing",
                    "message": (
                        "Music source is an uploaded track, but "
                        "no track has been uploaded for this "
                        "project yet."
                    ),
                })
            elif not Path(upload["file_path"]).is_file():
                errors.append({
                    "code": "music_upload_missing",
                    "message": (
                        "The uploaded music track "
                        f"({upload.get('file_name') or upload['file_path']}) "
                        "is no longer on disk; upload it again or "
                        "choose another source."
                    ),
                })
        elif music["source"] == "library":
            try:
                track, path = resolve_library_track(config)
            except ConfigError as error:
                errors.append({"code": error.code, "message": error.message})
            else:
                if track is None:
                    errors.append({
                        "code": "music_track_unselected",
                        "message": (
                            "Music source is the library, but no "
                            "track or mood is selected."
                        ),
                    })
                elif not path.is_file():
                    errors.append({
                        "code": "music_track_file_missing",
                        "message": (
                            f"Library track {track.track_id!r} "
                            "names a file that is not on disk."
                        ),
                    })
        if music["placement"] == "sections" and not music["sections"]:
            errors.append({
                "code": "music_sections_empty",
                "message": (
                    "Music placement is 'sections' but no section "
                    "ranges are set."
                ),
            })

    if not config["motion_graphics"]["enabled"]:
        warnings.append({
            "code": "motion_graphics_disabled_coverage",
            "message": (
                "Motion graphics are disabled: no graphic will "
                "be inserted for any beat, including as a "
                "coverage fallback. Beats that footage retrieval "
                "cannot cover will be reported uncovered in the "
                "production record rather than filled."
            ),
        })
    return {"errors": errors, "warnings": warnings}


# ---------------------------------------------------------------------
# Run resolution (§14.2): the document -> the run's options snapshot
# ---------------------------------------------------------------------


def stored_config(project_options) -> dict | None:
    """The cleaned stored configuration from a project's (or a
    run's) options, or None when the project carries none. A
    stored document that no longer cleans (written by an older
    build) is treated as absent rather than crashing a read —
    but run submission re-cleans strictly (see below)."""
    raw = (project_options or {}).get(CONFIG_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return clean_config(raw)
    except ConfigError:
        return None


def resolve_run_options(project, merged_options) -> dict:
    """The run-submission gate. When the merged options carry a
    production_config document: clean it strictly (a malformed
    document refuses 422), validate it against the live system
    (an unhonourable configuration refuses 422 with every
    error), then stamp the flat projection + the resolved
    snapshot onto the options the run will carry. The snapshot
    is what the ledger/manifest — and therefore the Versioning
    configuration history — records for this run.

    `project` is accepted for symmetry with the other gates and
    future project-scoped checks; validation today needs only
    the document and the live system."""
    raw = (merged_options or {}).get(CONFIG_KEY)
    if not isinstance(raw, dict):
        # Item 6: even without a configuration document, a stored
        # flat language the narration catalogue cannot honour
        # refuses the run before anything is queued or charged.
        from services import languages as languages_service

        flat_language = (merged_options or {}).get("language")
        if flat_language is not None:
            if not languages_service.is_known(flat_language):
                raise ConfigError(
                    422,
                    "language_unknown",
                    f"Language {flat_language!r} is not a language "
                    "Cinemora supports.",
                )
            reason = languages_service.production_block_reason(
                flat_language
            )
            if reason:
                raise ConfigError(
                    422, "language_narration_unsupported", reason
                )
        return merged_options
    config = clean_config(raw)
    validation = validate_config(config)
    if validation["errors"]:
        raise ConfigError(
            422,
            "config_validation_failed",
            "The production configuration cannot be honoured: "
            + "; ".join(e["message"] for e in validation["errors"]),
        )
    config = with_honest_preset_label(config)
    resolved_theme = resolve_theme(config)
    merged = dict(merged_options)
    # The stored configuration is authoritative for the keys it
    # projects (§14.2): clear any projection-owned key the run
    # or project options carried, then write the projection.
    for key in PROJECTION_OWNED_KEYS:
        merged.pop(key, None)
    merged.update(flat_options(config))
    merged[CONFIG_KEY] = config
    merged["production_config_resolved"] = {
        "preset": matching_preset(config),
        "theme_engine": resolved_theme["engine_name"],
        "graphics": graphics_build_params(config),
        "warnings": validation["warnings"],
    }
    return merged


def project_config_view(project) -> dict:
    """The configuration surface's read model for one project:
    the stored document (or the resolved defaults view when the
    project carries none), its flat projection, and its live
    validation."""
    options = dict(project.options or {})
    config = stored_config(options)
    is_stored = config is not None
    if config is None:
        config = clean_config({})
    return {
        "project_id": project.id,
        "stored": is_stored,
        "config": config,
        "preset_match": matching_preset(config),
        "flat_options": flat_options(config),
        "validation": validate_config(config),
    }


def save_project_config(db, project, raw_config) -> dict:
    """Clean, validate and store a configuration on the project:
    the document plus its flat projection are written onto the
    project's options together, so no reader can see one without
    the other. Refuses (422) on cleaning or validation errors —
    a configuration the pipeline cannot honour is never stored
    as if it were runnable."""
    config = with_honest_preset_label(clean_config(raw_config))
    validation = validate_config(config)
    if validation["errors"]:
        raise ConfigError(
            422,
            "config_validation_failed",
            "The production configuration cannot be honoured: "
            + "; ".join(e["message"] for e in validation["errors"]),
        )
    options = _apply_projection(dict(project.options or {}), config)
    project.options = options
    db.commit()
    db.refresh(project)
    return project_config_view(project)


# ---------------------------------------------------------------------
# Per-user saved defaults (§15) — the preferences document pattern
# ---------------------------------------------------------------------


def load_defaults(db, user_id) -> dict | None:
    if not user_id:
        return None
    from models import UserPreference

    try:
        row = db.get(UserPreference, user_id)
    except Exception:
        # Fail-soft on a database without the preferences table
        # (legacy fixture DBs that create_all only the production
        # tables — the same contract services.plans documents for
        # resolve_effective_plan): no preferences system exists
        # there, so no saved defaults apply. Without this guard,
        # project creation 500s on such a database even though
        # defaults are only an optional seed.
        db.rollback()
        return None
    document = (row.document if row else None) or {}
    section = document.get(DEFAULTS_SECTION)
    if not isinstance(section, dict):
        return None
    raw = section.get("defaults")
    if not isinstance(raw, dict):
        return None
    try:
        return clean_config(raw)
    except ConfigError:
        return None


def save_defaults(db, user_id, raw_config) -> dict:
    if not user_id:
        raise ConfigError(
            403, "no_user", "Saved defaults need a user account."
        )
    from models import UserPreference

    config = with_honest_preset_label(clean_config(raw_config))
    row = db.get(UserPreference, user_id)
    document = dict((row.document if row else None) or {})
    document[DEFAULTS_SECTION] = {"defaults": config}
    if row is None:
        db.add(UserPreference(user_id=user_id, document=document))
    else:
        row.document = document
    db.commit()
    return config


def clear_defaults(db, user_id) -> None:
    if not user_id:
        return
    from models import UserPreference

    row = db.get(UserPreference, user_id)
    if row is None:
        return
    document = dict(row.document or {})
    section = document.get(DEFAULTS_SECTION)
    if isinstance(section, dict) and "defaults" in section:
        section = dict(section)
        section.pop("defaults")
        document[DEFAULTS_SECTION] = section
        row.document = document
        db.commit()


# ---------------------------------------------------------------------
# Uploads (§13.6): the user's own music + voice recordings
# ---------------------------------------------------------------------

UPLOAD_KINDS = ("music", "voice")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def store_config_upload(project_id, kind, filename, fileobj) -> dict:
    """Store one uploaded audio file as project media under the
    project's config_uploads/<kind>/ directory and return its
    record. The original is preserved exactly as uploaded
    (derived versions, where a stage makes them, live elsewhere
    — §15). The file is probed with ffprobe through the music
    service's audio probe: an unprobeable or audio-less file is
    refused 422 and removed — an upload the pipeline cannot
    read is never recorded as usable."""
    if kind not in UPLOAD_KINDS:
        _refuse("invalid_upload_kind", f"Unknown upload kind: {kind!r}.")
    from services import editor as editor_service
    from services import music as music_service

    safe = _SAFE_NAME.sub("_", Path(filename or "upload").name) or "upload"
    upload_id = uuid.uuid4().hex[:12]
    target_dir = media_paths.project_dir(project_id) / "config_uploads" / kind
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{upload_id}-{safe}"
    cap = editor_service.upload_max_bytes()
    written = 0
    with open(target, "wb") as handle:
        while True:
            chunk = fileobj.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > cap:
                handle.close()
                target.unlink(missing_ok=True)
                raise ConfigError(
                    413, "upload_too_large",
                    f"The upload exceeds the {cap}-byte limit.",
                )
            handle.write(chunk)
    probe = music_service.probe_audio_file(target)
    if not probe or not probe.get("codec"):
        target.unlink(missing_ok=True)
        _refuse(
            "upload_unprobeable",
            "The uploaded file could not be read as audio; "
            "nothing was stored.",
        )
    return {
        "id": upload_id,
        "file_name": filename,
        "file_path": str(target),
        "content_sha256": music_service.file_sha256(target),
        "duration_seconds": probe.get("duration_seconds"),
        "codec": probe.get("codec"),
        "uploaded_at": _now_iso(),
    }


def attach_upload(db, project, kind, record) -> dict:
    """Record a stored upload on the project's configuration:
    a music upload becomes the configuration's music source
    (§13.6: uploaded music is always distinguishable from
    library music — source "upload" with its own record); a
    voice upload becomes the recorded voice recording the
    voice-to-documentary flow transcribes (§13.4: the original
    recording stays as project media)."""
    options = dict(project.options or {})
    config = stored_config(options) or clean_config({})
    if kind == "music":
        config["music"]["source"] = "upload"
        config["music"]["upload"] = record
        config["music"]["enabled"] = True
    elif kind == "voice":
        config["narration"]["voice_recording"] = record
    else:
        _refuse("invalid_upload_kind", f"Unknown upload kind: {kind!r}.")
    project.options = _apply_projection(options, config)
    db.commit()
    db.refresh(project)
    return project_config_view(project)


def remove_music_upload(db, project) -> dict:
    """§13.6 replace/remove: drop the music upload RECORD from
    the configuration (the original file stays on disk as
    project media; the configuration simply no longer selects
    it) and fall back to no music source."""
    options = dict(project.options or {})
    config = stored_config(options) or clean_config({})
    config["music"]["upload"] = None
    if config["music"]["source"] == "upload":
        config["music"]["source"] = "none"
    project.options = _apply_projection(options, config)
    db.commit()
    db.refresh(project)
    return project_config_view(project)


# ---------------------------------------------------------------------
# The catalogue: everything the configuration surface may offer
# ---------------------------------------------------------------------


def catalogue() -> dict:
    """The configuration catalogue, generated from the live
    systems: founder themes with their real engine token
    swatches, presets as their resolved bundles, the narration
    adapter and its installed voices, caption modes, intensities
    and the music library's real state. Nothing is offered that
    the pipeline cannot consume."""
    from services import narration as narration_service

    themes = []
    for founder_id, engine_name in sorted(
        themes_module.FOUNDER_THEMES.items()
    ):
        tokens = themes_module.THEMES[engine_name]
        themes.append({
            "id": founder_id,
            "engine_name": engine_name,
            "swatch": {
                key: list(tokens[key]) for key in TOKEN_KEYS
            },
            "background_pattern": tokens.get("background_pattern"),
        })
    voices = []
    for voice_id, (model, _cfg) in sorted(
        narration_service.VOICE_CATALOGUE.items()
    ):
        voices.append({
            "id": voice_id,
            "is_default": voice_id == narration_service.DEFAULT_VOICE_ID,
            "installed": Path(model).exists(),
        })
    return {
        "themes": themes,
        "custom_theme": {
            "bases": sorted(themes_module.FOUNDER_THEMES),
            "token_keys": list(TOKEN_KEYS),
        },
        "presets": [
            {
                "id": preset_id,
                "label": preset["label"],
                "config": preset_config(preset_id),
            }
            for preset_id, preset in PRESETS.items()
        ],
        "caption_modes": list(CAPTION_MODES),
        "caption_syncs": list(CAPTION_SYNCS),
        "music_placements": list(MUSIC_PLACEMENTS),
        "music_library": library_state(),
        "narration": {
            "adapter": "services.narration",
            "provider": "kokoro",
            "voices": voices,
            "note": (
                "The pipeline talks to the narration adapter's "
                "stable interface (text + voice id); the provider "
                "behind it is Kokoro-82M, self-hosted. Narration "
                "runs at the provider's native rate — there is "
                "no speed setting."
            ),
        },
    }
