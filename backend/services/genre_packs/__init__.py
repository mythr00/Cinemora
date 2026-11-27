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
  None, meaning the historical per-template defaults apply;
  engineering / industrial -> "editorial", the clean technical
  slate — Transformation Phase 2.1/2.2, 2026-11-22),
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
- source_hints (Transformation Phase 4.2): a top-level block of
  institutional discovery templates — Wikimedia Commons category
  searches, Commons free-text searches, Archive.org advanced-
  search (Lucene) fragments and SearXNG web searches aimed at
  institutional material (NTSB, NASA, national archives, port
  authorities, government footage) that generic web search
  surfaces poorly. Templates carry {subject} / {place} / {era} /
  {query} placeholders formatted per shot need; discovery
  (media_pipeline.build_shot_needs -> _uve_scenario_search)
  consumes the RESOLVED hints via resolve_source_hints /
  format_source_hint_queries, and every hint query is accounted
  in the AM-021 per-query diagnostics under source "source_hint"
  and aggregated in the asset report's source_hints block.
  Validation is load-time (validate_source_hints) like the rest
  of the pack; a block that fails validation resolves to no
  hints — hints are an aid, never a gate. Packs without the
  block simply have no hints (no cross-pack fallback: a hint is
  genre knowledge, and borrowing another genre's institutions
  would be fabrication, not neutrality).
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
the run options wins; otherwise the owning project's stored
category when it names a known pack; otherwise a conservative
script detector; otherwise the "general" pack. The detector needs
a clear margin — an ambiguous script stays general rather than
being forced into a genre.
"""

import copy
import json
import re
from functools import lru_cache
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent / "data"

PACK_IDS = (
    "general", "history", "true_crime", "engineering", "industrial",
    # Phase M (Templates completion, founder #15): biography,
    # science and investigative journalism join as data-authored
    # packs on this same system — the gap audit's three named
    # additions (doc 01 §5 / doc 06 Phase M).
    "biography", "science", "investigative",
)


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
#: template is a validation failure, not dead configuration — the
#: MG-104 failure mode, linted from day one. Wave 1 of the
#: Motion Graphics Engine plan landed
#: the assembled set below — including
#: document_presentation, whose assembler exists but yields a
#: render ONLY for a genuine retrieved document asset with
#: recorded provenance (without one the attempt records the
#: AM-010 refusal). The remaining registered templates
#: (editorial_panel, split_panel, scan_grid, photo_node_diagram,
#: cross_section, satellite_annotation) still have no targeted-
#: chain assembler — cross_section renders via the event
#: schematic family instead — per the Phase 6 audit table.
ASSEMBLED_TEMPLATES = frozenset({
    "bar_chart", "data_gauge", "ring_badges", "stat_comparison",
    # Wave 1 (Motion Graphics Engine plan §C).
    "line_chart", "pie_chart",
    "exploded_view", "mechanical_cutaway", "blueprint_section",
    "newspaper_montage", "profile_reveal",
    "document_presentation",
    "text_overlay", "text_highlight",
    "archival_title",
    # Wave 2 (Motion Graphics Engine plan §C Wave 2): the breadth
    # families — every one has a production assembler in the
    # targeted chain; classified_file's assembler yields a render
    # only for a genuine document asset, exactly like
    # document_presentation.
    "bubble_chart", "population_pyramid", "animated_dashboard",
    "checklist_graphic",
    "territory_expansion", "satellite_zoom", "heatmap",
    "classified_file", "newspaper_wall", "crime_scene_diagram",
    "construction_sequence", "molecular_diagram",
    "hud_overlay", "holographic_panel", "before_after",
    "layered_reveal", "object_labels",
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
# Source hints (Transformation Phase 4.2)
# ---------------------------------------------------------------------------

#: The discovery channels a pack's source_hints block may target.
#: wikimedia_category templates format into Commons CirrusSearch
#: strings (incategory:"..." plus anchor terms) run through the
#: Commons image/video searches; wikimedia_query templates are
#: Commons free-text searches; archive templates are Archive.org
#: advanced-search (Lucene) fragments — search_archive_videos
#: appends "AND mediatype:movies"; web templates are SearXNG
#: searches (videos + web + images).
SOURCE_HINT_CHANNELS = (
    "wikimedia_category", "wikimedia_query", "archive", "web",
)

#: Per-need execution caps per channel (formatted hints actually
#: stamped on a shot need). Discovery aid, bounded like every
#: other query expansion (cf. _MAX_NEED_QUERIES for intent
#: queries): hints widen the funnel, they must not flood it.
SOURCE_HINT_CAPS = {
    "wikimedia_category": 2,
    "wikimedia_query": 1,
    "archive": 2,
    "web": 1,
}

_SOURCE_HINT_MAX_TEMPLATES = 4
_SOURCE_HINT_MAX_TEMPLATE_CHARS = 200
_SOURCE_HINT_PLACEHOLDERS = ("{subject}", "{place}", "{era}", "{query}")


def validate_source_hints(hints):
    """Validate one pack's source_hints block (Phase 4.2).

    Returns a list of failure strings; empty means valid. An
    absent block (None) is legal — the pack simply has no hints.
    Rules: known channels only; each channel holds at most
    _SOURCE_HINT_MAX_TEMPLATES non-empty string templates of at
    most _SOURCE_HINT_MAX_TEMPLATE_CHARS characters; every
    template uses at least one known placeholder (a template
    with no anchor would fire the same query for every beat in
    the genre — a flood, not a hint); no unknown {placeholder}
    may appear (it would survive formatting as literal noise).
    """
    failures = []
    if hints is None:
        return failures
    if not isinstance(hints, dict):
        return ["source_hints_not_an_object"]
    unknown = sorted(set(hints) - set(SOURCE_HINT_CHANNELS))
    if unknown:
        failures.append(f"source_hints_unknown_channels:{','.join(unknown)}")
    for channel in sorted(set(hints) & set(SOURCE_HINT_CHANNELS)):
        templates = hints[channel]
        if not isinstance(templates, list):
            failures.append(f"source_hints_{channel}_not_a_list")
            continue
        if len(templates) > _SOURCE_HINT_MAX_TEMPLATES:
            failures.append(f"source_hints_{channel}_too_many")
        for template in templates:
            if not isinstance(template, str) or not template.strip():
                failures.append(f"source_hints_{channel}_template_invalid")
                break
            if len(template) > _SOURCE_HINT_MAX_TEMPLATE_CHARS:
                failures.append(f"source_hints_{channel}_template_too_long")
                break
            found = re.findall(r"\{[^{}]*\}", template)
            bad = [f for f in found if f not in _SOURCE_HINT_PLACEHOLDERS]
            if bad:
                failures.append(
                    f"source_hints_{channel}_unknown_placeholder:"
                    f"{','.join(sorted(set(bad)))}"
                )
                break
            if not found:
                failures.append(
                    f"source_hints_{channel}_template_unanchored"
                )
                break
    return failures


def resolve_source_hints(pack):
    """The pack's validated source_hints, normalised per channel.

    Returns {"hints": {channel: [templates]}, "failures": [...]}.
    A pack without the block — or with a block that fails
    validation — resolves to empty hints with the failures
    recorded (fail-closed to no-hints: discovery proceeds exactly
    as before Phase 4.2). The returned lists are fresh copies;
    the cached pack dicts are never mutated.
    """
    result = {"hints": {}, "failures": []}
    if not isinstance(pack, dict):
        return result
    hints = pack.get("source_hints")
    if hints is None:
        return result
    failures = validate_source_hints(hints)
    if failures:
        result["failures"] = failures
        return result
    for channel in SOURCE_HINT_CHANNELS:
        templates = hints.get(channel) or []
        if templates:
            result["hints"][channel] = list(templates)
    return result


def format_source_hint_queries(hints, *, subject="", place="", era="",
                               query=""):
    """Format a resolved hints block against one shot need's
    anchors.

    ``hints`` is the {channel: [templates]} mapping from
    resolve_source_hints. A template is SKIPPED when any
    placeholder it uses has an empty anchor (a hint that cannot
    name its subject is not issued half-formed). Formatted
    queries are whitespace-normalised, deduplicated within the
    channel, and capped per SOURCE_HINT_CAPS. Returns a list of
    {"query", "channel", "template"} in channel order — the
    record build_shot_needs stamps on the need and
    _uve_scenario_search executes.
    """
    anchors = {
        "{subject}": str(subject or "").strip(),
        "{place}": str(place or "").strip(),
        "{era}": str(era or "").strip(),
        "{query}": str(query or "").strip(),
    }
    formatted = []
    for channel in SOURCE_HINT_CHANNELS:
        seen = set()
        for template in (hints or {}).get(channel) or []:
            used = re.findall(r"\{[^{}]*\}", str(template))
            if any(not anchors.get(ph) for ph in used):
                continue
            text = str(template)
            for placeholder, value in anchors.items():
                text = text.replace(placeholder, value)
            text = " ".join(text.split()).strip()
            if not text or text.lower() in seen:
                continue
            seen.add(text.lower())
            formatted.append({
                "query": text,
                "channel": channel,
                "template": str(template),
            })
            if len([f for f in formatted if f["channel"] == channel]) >= (
                SOURCE_HINT_CAPS[channel]
            ):
                break
    return formatted


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
    "project_category" (the owning project's stored category —
    run-9 fix: jobs.py injects it as options["project_category"];
    a category that names a known pack outranks script detection,
    because the category is a deliberate classification while the
    detector is a heuristic), "detected" (the script detector
    picked a non-general genre) or "default" (the general pack —
    no option, or an unknown option string with an undetectable
    script; the requested value is still recorded verbatim in
    "requested").
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
    if isinstance(options, dict):
        category = options.get("project_category")
        if category:
            pack = load_pack(str(category).strip())
            if pack is not None:
                return _with_visual_profile({
                    "pack": pack,
                    "pack_id": pack["id"],
                    "source": "project_category",
                    "requested": str(category),
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
