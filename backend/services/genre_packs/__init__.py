"""Genre packs as DATA (Stage 5A).

A genre pack is a JSON document in data/ — never an if/else branch
in pipeline code. Each pack declares:

- research_query_templates: "{name} {era} historical record"-style
  templates the Stage 3A research pass formats per bible entity,
- entity_handling: editorial care notes for the genre (for true
  crime: never invent evidence/dates/locations; victim dignity),
- format_preferences: format-intent priorities. These stay WITHIN
  the existing bounded rules — format intent remains a bounded
  ranking signal (max 0.10 x confidence in the unified selection);
  the pack declares priorities, it does not raise any cap,
- graphics_motifs: ordered Motion Engine template preferences for
  the targeted-graphics chain,
- style_family: the motion-engine style family for the genre
  (history -> "history", true_crime -> "true_crime", general ->
  None, meaning the historical per-template defaults apply),
- pacing: informational targets only (narration is not retimed),
- music_mood: a metadata label only. NO music library is
  integrated — that selection is founder-gated.
- mix_preset (Stage J, P4): the genre's default mix parameters
  for services.music.prepare_soundtrack — music_volume,
  fade_in_seconds, fade_out_seconds, duck_ratio. DATA only: the
  mix resolver (services.music.resolve_mix_config) treats them as
  defaults an explicit option or an env override beats, and the
  applied preset is recorded in the soundtrack record.
- expectations (Stage 6 / R6, Architecture Review C.G.2): the
  genre's measured-share expectations — moving_share, still_share,
  document_share, graphic_share, uncovered_share — each
  {"range": [lo, hi] | None, "basis": str}. Every pack ships
  range None with basis "uncalibrated": an uncalibrated
  expectation evaluates to UNKNOWN (services.calibration), never
  to a default number. Ranges are written ONLY from the R6
  calibration protocol (panel-accepted corpus films), with the
  calibration report as evidence — no number enters a pack
  without it.
- visual_profile + visual_theme (Stage 7 / R7, Architecture
  Review B.2.2): genre independence as DATA. The profile carries
  category_priors (Pass-1 expectations over the five visual
  categories), template_affinities (only templates with a
  production assembler may be named — the Phase 5 motif lint,
  applied from day one), composition_affinities (inert until
  Phase 6), specificity_defaults (may raise, never lower the
  evidence floor below "exact"), research_requirements (shapes
  the research pass; cannot change support labelling) and
  care_rules_ref. visual_theme names the pack's default motion
  style. Validation is load-time (validate_visual_profile /
  validate_visual_theme); a profile that fails validation is not
  loaded — resolution falls back to the general profile and
  records the failure (fail-closed to neutral). Consumers read
  the RESOLVED profile via resolve_visual_profile /
  resolve_genre_pack; pipeline code never branches on genre
  names.
- broll_vocabulary (Intelligent B-Roll B1, plan §4.3): a block
  under visual_profile — activities / settings / place_types /
  mood phrase lists a genre's B-roll searches may top up from.
  Genre DATA only: the ladder machinery that consumes it (B2) is
  genre-agnostic, pack phrases never enter direct-evidence
  searches, and a pack without the block resolves to the general
  pack's core by the same field-by-field fallback. Validation
  (validate_broll_vocabulary) is load-time like the rest of the
  profile; for true_crime it additionally enforces the §6.5 care
  rule — no re-enactment-shaped activity phrases (the genre's
  B-roll shows the investigation and the world around the story,
  never a simulated crime).

Resolution order in resolve_genre_pack: an explicit known genre in
the run options wins; otherwise a conservative script detector;
otherwise the "general" pack. The detector needs a clear margin —
an ambiguous script stays general rather than being forced into a
genre.
"""

import copy
import json
import re
from functools import lru_cache
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent / "data"

PACK_IDS = ("general", "history", "true_crime")


@lru_cache(maxsize=None)
def load_pack(pack_id):
    """The pack dict for an id, or None when unknown. Returned
    dicts are shared — treat them as read-only."""
    if not pack_id:
        return None
    path = _DATA_DIR / f"{str(pack_id).strip().lower()}.json"
    if not path.exists():
        return None
    try:
        pack = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(pack, dict) or pack.get("id") != path.stem:
        return None
    return pack


def available_packs():
    return [load_pack(p) for p in PACK_IDS if load_pack(p) is not None]


def broll_share_band(pack):
    """The founder-set B-roll share band (D-B1, approved 2026-11-21)
    carried in a pack's expectations data: {"max": float, "basis":
    str}, or None when the pack carries no usable band.

    Deliberately NOT part of calibration.EXPECTATION_FIELDS (the
    C.G.2 five calibrated shares stay as they are): the band is a
    founder-set reporting reference, not a calibrated expectation.
    Consumers compare the measured tier B-roll share against it and
    REPORT the result — nothing gates on it."""
    entry = ((pack or {}).get("expectations") or {}).get("broll_share")
    if not isinstance(entry, dict):
        return None
    rng = entry.get("range")
    if (
        isinstance(rng, (list, tuple))
        and len(rng) == 2
        and isinstance(rng[1], (int, float))
        and not isinstance(rng[1], bool)
    ):
        return {"max": float(rng[1]), "basis": entry.get("basis") or "founder_band"}
    return None


# ---------------------------------------------------------------------------
# Visual profile (Stage 7 / R7 — Architecture Review B.2.2)
# ---------------------------------------------------------------------------

#: The five visual categories a pack's category_priors range over.
#: Four are verification categories (services.story_understanding);
#: explanatory_graphic is a planning expectation only — it never
#: classifies a beat against the beat's own signals.
PROFILE_CATEGORIES = (
    "evidence", "contextual", "environmental", "explanatory_graphic",
    "atmosphere",
)

#: Templates a pack's template_affinities may name TODAY: exactly
#: the templates with a production assembler in graphics_pipeline's
#: targeted-graphics chain. Naming a registered-but-unassembled
#: template (territory_map, evidence_board, ...) is a validation
#: failure, not dead configuration — the MG-104 failure mode, linted
#: from day one. This set grows when Stage 10 lands assemblers.
ASSEMBLED_TEMPLATES = frozenset({
    "bar_chart", "data_gauge", "ring_badges", "stat_comparison",
})

_PROFILE_FIELDS = frozenset({
    "profile_version", "category_priors", "template_affinities",
    "composition_affinities", "specificity_defaults",
    "research_requirements", "care_rules_ref", "broll_vocabulary",
})

#: B-roll vocabulary fields (B1, plan §4.3) and their size caps —
#: phrases, not keywords: at most 12 phrases per list, at most 6
#: words per phrase.
_BROLL_VOCABULARY_FIELDS = frozenset({
    "activities", "settings", "place_types", "mood",
})
_BROLL_MAX_PHRASES = 12
_BROLL_MAX_WORDS = 6

#: Re-enactment fragments barred from the true_crime vocabulary by
#: the §6.5 care rule (substring match on the lowercased phrase).
_REENACTMENT_FRAGMENTS = (
    "attack", "strangl", "stab", "shoot", "kill", "murder",
    "assault", "disposal", "reenact", "re-enact", "bludgeon",
    "suffocat", "drown", "poison",
)

#: Composition pattern vocabulary (Phase 6). Inert data until
#: Stage 11 — recorded from the start, per B.2.2.
_COMPOSITION_PATTERNS = frozenset({
    "lower_third_card", "side_panel", "picture_in_picture",
    "split_screen", "full_frame",
})

_SPECIFICITY_LEVELS = frozenset({"exact", "representative", "generic"})


def _engine_template_registry():
    """The motion-engine template registry, imported lazily so this
    package's import never depends on the engine's import graph."""
    from services.motion_engine.templates import TEMPLATES

    return TEMPLATES


def _engine_styles():
    from services.motion_engine.styles import STYLES

    return STYLES


def _is_reenactment_phrase(phrase):
    """True when a vocabulary phrase is re-enactment-shaped (§6.5):
    any word in the phrase STARTS with a barred fragment — so
    "strangling" matches "strangl" while "skilled" never matches
    "kill"."""
    words = re.findall(r"[a-z-]+", str(phrase or "").lower())
    return any(
        word.startswith(fragment)
        for word in words
        for fragment in _REENACTMENT_FRAGMENTS
    )


def validate_broll_vocabulary(vocabulary, pack_id=None):
    """Validate one pack's broll_vocabulary block (B1, plan §4.3).

    Returns a list of failure strings; empty means valid. An absent
    block (None) is legal — resolution falls back to the general
    pack's core. Rules: known keys only; each list holds at most
    12 non-empty string phrases of at most 6 words. For the
    true_crime pack the §6.5 care rule also applies: no
    re-enactment-shaped phrases in any list.
    """
    failures = []
    if vocabulary is None:
        return failures
    if not isinstance(vocabulary, dict):
        return ["broll_vocabulary_not_an_object"]
    unknown = sorted(set(vocabulary) - _BROLL_VOCABULARY_FIELDS)
    if unknown:
        failures.append(f"broll_vocabulary_unknown_fields:{','.join(unknown)}")
    for field in sorted(set(vocabulary) & _BROLL_VOCABULARY_FIELDS):
        phrases = vocabulary[field]
        if not isinstance(phrases, list):
            failures.append(f"broll_vocabulary_{field}_not_a_list")
            continue
        if len(phrases) > _BROLL_MAX_PHRASES:
            failures.append(f"broll_vocabulary_{field}_too_many")
        for phrase in phrases:
            if not isinstance(phrase, str) or not phrase.strip():
                failures.append(f"broll_vocabulary_{field}_phrase_invalid")
                break
            if len(phrase.split()) > _BROLL_MAX_WORDS:
                failures.append(f"broll_vocabulary_{field}_phrase_too_long")
                break
        if pack_id == "true_crime":
            for phrase in phrases:
                if isinstance(phrase, str) and _is_reenactment_phrase(phrase):
                    failures.append(
                        f"broll_vocabulary_reenactment:{phrase}"
                    )
    return failures


def validate_visual_profile(profile, pack_id=None):
    """Validate one pack's visual_profile object (B.2.2).

    Returns a list of failure strings; empty means valid. An absent
    profile (None) is legal — the general profile's values apply.
    Rules: known keys only; category_priors range over the five
    profile categories and sum to 1.0; every template named in
    template_affinities exists in the engine registry AND has a
    production assembler; specificity_defaults may raise but never
    lower the evidence floor below "exact"; research_requirements
    carries only its two known fields with a sane minimum;
    broll_vocabulary follows validate_broll_vocabulary (``pack_id``
    scopes the true_crime care rule).
    """
    failures = []
    if profile is None:
        return failures
    if not isinstance(profile, dict):
        return ["visual_profile_not_an_object"]
    unknown = sorted(set(profile) - _PROFILE_FIELDS)
    if unknown:
        failures.append(f"visual_profile_unknown_fields:{','.join(unknown)}")
    priors = profile.get("category_priors")
    if priors is not None:
        if not isinstance(priors, dict):
            failures.append("category_priors_not_an_object")
        else:
            bad = sorted(set(priors) - set(PROFILE_CATEGORIES))
            if bad:
                failures.append(f"category_priors_unknown:{','.join(bad)}")
            values = [
                v for v in priors.values()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            ]
            if len(values) != len(priors) or any(v < 0 for v in values):
                failures.append("category_priors_values_invalid")
            elif abs(sum(values) - 1.0) > 0.01:
                failures.append(f"category_priors_sum:{sum(values):.3f}")
    affinities = profile.get("template_affinities")
    if affinities is not None:
        if not isinstance(affinities, dict):
            failures.append("template_affinities_not_an_object")
        else:
            try:
                registry = _engine_template_registry()
            except Exception:
                registry = None
                failures.append("template_registry_unavailable")
            for name in affinities:
                if registry is not None and name not in registry:
                    failures.append(f"template_affinity_unknown:{name}")
                elif name not in ASSEMBLED_TEMPLATES:
                    failures.append(f"template_affinity_unassembled:{name}")
    composition = profile.get("composition_affinities")
    if composition is not None:
        if not isinstance(composition, dict):
            failures.append("composition_affinities_not_an_object")
        else:
            bad = sorted(set(composition) - _COMPOSITION_PATTERNS)
            if bad:
                failures.append(
                    f"composition_affinities_unknown:{','.join(bad)}"
                )
    defaults = profile.get("specificity_defaults")
    if defaults is not None:
        if not isinstance(defaults, dict):
            failures.append("specificity_defaults_not_an_object")
        else:
            for category, level in defaults.items():
                if category not in PROFILE_CATEGORIES:
                    failures.append(
                        f"specificity_default_unknown_category:{category}"
                    )
                elif level not in _SPECIFICITY_LEVELS:
                    failures.append(
                        f"specificity_default_unknown_level:{category}={level}"
                    )
            if "evidence" in defaults and defaults["evidence"] != "exact":
                # B.2.2: a profile may raise a floor, never lower the
                # evidence floor below exact.
                failures.append("specificity_default_evidence_below_exact")
    research = profile.get("research_requirements")
    if research is not None:
        if not isinstance(research, dict):
            failures.append("research_requirements_not_an_object")
        else:
            bad = sorted(
                set(research)
                - {"min_sources_per_primary_subject", "prefer_primary_sources"}
            )
            if bad:
                failures.append(
                    f"research_requirements_unknown:{','.join(bad)}"
                )
            minimum = research.get("min_sources_per_primary_subject")
            if minimum is not None and (
                not isinstance(minimum, int)
                or isinstance(minimum, bool)
                or minimum < 0
            ):
                failures.append("research_requirements_min_sources_invalid")
    failures.extend(
        validate_broll_vocabulary(profile.get("broll_vocabulary"), pack_id)
    )
    return failures


def validate_visual_theme(theme):
    """A pack's visual_theme must name a real motion-engine style
    (the six Phase 4 theme names join STYLES at Stage 9)."""
    if theme is None:
        return []
    try:
        styles = _engine_styles()
    except Exception:
        return ["style_registry_unavailable"]
    if theme not in styles:
        return [f"visual_theme_unknown:{theme}"]
    return []


def resolve_visual_profile(pack):
    """The effective visual profile for a pack (B.2.2).

    Field-by-field fallback: a field absent from the pack's profile
    takes the general profile's value for that field. A profile that
    FAILS validation is not loaded at all — resolution falls back to
    the general profile wholesale and records the failures
    (fail-closed to neutral). An invalid visual_theme likewise falls
    back to the general pack's theme, with the failure recorded.
    Returns {"profile", "profile_source", "profile_failures",
    "visual_theme", "theme_failures"}; the returned profile is a
    fresh dict — the cached pack dicts are never mutated.
    """
    general = load_pack("general") or {}
    general_profile = general.get("visual_profile") or {}
    result = {
        "profile": copy.deepcopy(general_profile),
        "profile_source": "general",
        "profile_failures": [],
        "visual_theme": general.get("visual_theme"),
        "theme_failures": validate_visual_theme(general.get("visual_theme")),
    }
    if not isinstance(pack, dict) or pack.get("id") == "general":
        return result
    failures = validate_visual_profile(
        pack.get("visual_profile"), pack_id=pack.get("id")
    )
    if failures:
        result["profile_failures"] = failures
        return result
    profile = copy.deepcopy(general_profile)
    for field, value in (pack.get("visual_profile") or {}).items():
        profile[field] = copy.deepcopy(value)
    result["profile"] = profile
    result["profile_source"] = pack.get("id")
    theme_failures = validate_visual_theme(pack.get("visual_theme"))
    result["theme_failures"] = theme_failures
    if not theme_failures and pack.get("visual_theme"):
        result["visual_theme"] = pack["visual_theme"]
    return result


def _with_visual_profile(result):
    resolved = resolve_visual_profile(result.get("pack"))
    result["visual_profile"] = resolved["profile"]
    result["visual_profile_source"] = resolved["profile_source"]
    result["visual_profile_failures"] = resolved["profile_failures"]
    result["visual_theme"] = resolved["visual_theme"]
    result["visual_theme_failures"] = resolved["theme_failures"]
    return result


# ---------------------------------------------------------------------------
# Conservative script-based detection
# ---------------------------------------------------------------------------

_TRUE_CRIME_CUES = [
    r"\bmurder(?:ed|s|ing)?\b", r"\bhomicide\b", r"\bvictims?\b",
    r"\bsuspects?\b", r"\barrested\b", r"\bcharged with\b",
    r"\bconvicted\b", r"\bguilty\b", r"\btrial\b", r"\bdetectives?\b",
    r"\binvestigat(?:ion|ors?|ed)\b", r"\bcrime scene\b",
    r"\bkidnapp(?:ed|ing)\b", r"\bdisappeared\b", r"\bmissing person\b",
    r"\bsentenced\b", r"\bbody was found\b", r"\bpolice said\b",
    r"\bforensic\b", r"\bdna\b",
]

_HISTORY_CUES = [
    r"\bcentury\b", r"\bempire\b", r"\bancient\b", r"\bdynasty\b",
    r"\btreaty\b", r"\brevolution\b", r"\bworld war\b", r"\bcivil war\b",
    r"\bkingdom\b", r"\bcolon(?:y|ial|ies)\b", r"\bmedieval\b",
    r"\bexpedition\b", r"\bfounded in\b", r"\bcenturies\b",
]

_YEAR_RE = re.compile(r"\b(1[5-9]\d\d|20[0-2]\d)\b")


def _cue_score(text, cues):
    return sum(1 for cue in cues if re.search(cue, text))


def detect_genre(script_text):
    """Score a script for the history / true_crime packs.

    Returns {"pack_id", "scores", "pre_1961_years"}. A genre wins
    only with >= 3 distinct cue hits AND a strictly higher score
    than the other genre; anything else is "general". History also
    earns up to +4 from distinct pre-1961 year mentions (a script
    anchored in the deep past is a history signal even with few
    genre words).
    """
    text = str(script_text or "").lower()
    crime = _cue_score(text, _TRUE_CRIME_CUES)
    history = _cue_score(text, _HISTORY_CUES)
    years = {int(y) for y in _YEAR_RE.findall(text)}
    pre_1961 = sorted(y for y in years if y <= 1960)
    history += min(4, len(pre_1961))
    scores = {"history": history, "true_crime": crime}
    winner = "general"
    if crime >= 3 and crime > history:
        winner = "true_crime"
    elif history >= 3 and history > crime:
        winner = "history"
    return {"pack_id": winner, "scores": scores, "pre_1961_years": pre_1961}


def resolve_genre_pack(options=None, script_text=""):
    """Resolve the active pack for a run.

    Returns {"pack", "pack_id", "source", "requested", "detection"}
    where source is "option" (explicit known genre in options),
    "detected" (the script detector picked a non-general genre) or
    "default" (the general pack — no option, or an unknown option
    string with an undetectable script; the requested value is
    still recorded verbatim in "requested").
    """
    requested = None
    if isinstance(options, dict):
        requested = options.get("genre") or options.get("genre_pack")
    if requested:
        pack = load_pack(str(requested))
        if pack is not None:
            return _with_visual_profile({
                "pack": pack,
                "pack_id": pack["id"],
                "source": "option",
                "requested": str(requested),
                "detection": None,
            })
    detection = detect_genre(script_text)
    if detection["pack_id"] != "general":
        pack = load_pack(detection["pack_id"])
        return _with_visual_profile({
            "pack": pack,
            "pack_id": pack["id"],
            "source": "detected",
            "requested": str(requested) if requested else None,
            "detection": detection,
        })
    pack = load_pack("general")
    return _with_visual_profile({
        "pack": pack,
        "pack_id": "general",
        "source": "default",
        "requested": str(requested) if requested else None,
        "detection": detection,
    })
