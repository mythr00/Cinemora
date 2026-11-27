"""Visual Director — Pass-2 reconciliation (Master Directive
Phase 6, AM-002).

The Visual Director is a PURE decision module: no I/O, no
retrieval, no LLM calls, no rendering. It consumes

* the Phase 2 `visual_plan.json` v1 (pre-retrieval intentions), and
* the Asset Manager stage's actual per-sentence outcomes,

and produces the two authoritative Pass-2 artefacts:

* `visual_plan.json` v2 — v1's rows, each carrying exactly one
  typed final treatment decision (footage / graphic / unresolved)
  with its reason, and
* `graphics_manifest.json` — the authoritative list of targeted
  graphics. Graphics generation consumes the manifest: no manifest
  entry, no graphic (AM-001/D1 — graphics exist only for a
  reconciled requirement, never from an untargeted sweep).

Decision rules (deterministic, recorded per beat):

* A sentence the asset stage covered with a verified visual is
  decided FOOTAGE, with the covering asset's tier and identity.
* A sentence the asset stage recorded as missing (starved) is
  decided GRAPHIC when the v1 plan permits a graphic for it — its
  `graphic_fallback.allowed` is true, or its primary visual intent
  is `graphic_explanation` — and a manifest entry is issued.
  Otherwise it is decided UNRESOLVED with the reason recorded; an
  unresolved beat is an honest gap, never a silent graphic.
* A sentence with no recorded asset outcome (legacy / partial
  data) is UNRESOLVED with reason `no_asset_outcome_recorded` —
  the Director never invents an outcome.

Composition (AM-011): a FOOTAGE beat may additionally carry ONE
composition decision, by precedence: split_screen (Phase 8 —
comparison text signal + split_screen the pack's strictly
favoured pattern + a second verified video in the scene);
lower_third_card (Phase 6 — labelled with the covering asset's
own recorded title, scene's first covered beat, one per scene);
footage_window (Phase 8 — the pack favours picture_in_picture
and the scene holds a second verified video with a recorded
clip window, one per scene). Every pattern is opt-in via pack
affinities and recorded material; the Director renders nothing —
the decision is data for the composition engine.

Fact basis (AM-009): a manifest entry is stamped
`verified_asset_metadata` ONLY when the caller supplies an
evidence record for the sentence — an `evidence_ref` naming a
verified asset (`asset_id`) plus the `verified_data` drawn from
that asset's recorded metadata. Anything else stays
`script_claim`; the Director never promotes a claim on its own.

Genre-pack consumers landed here (AM-008): `template_affinities`
orders a graphic beat's template candidates (recorded per entry);
`composition_affinities` gates the lower-third decision (recorded
per decision); `visual_theme` selects the production theme via
services.motion_engine.themes. `category_priors` and
`specificity_defaults` remain data-only — no consumer is invented
for them in this phase.
"""

import re

PLAN_V2_VERSION = 2
GRAPHICS_MANIFEST_VERSION = 1

TREATMENT_FOOTAGE = "footage"
TREATMENT_GRAPHIC = "graphic"
TREATMENT_UNRESOLVED = "unresolved"

FACT_BASIS_ASSET = "verified_asset_metadata"
FACT_BASIS_SCRIPT = "script_claim"
# Transformation Phase 2.3: a script fact corroborated by a
# recorded knowledge-base verdict (services.script_facts) — the
# stamp is honoured only with the KB source on the evidence ref.
FACT_BASIS_RESEARCH = "research_supported"

_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s?(?:%|percent\b)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_YEAR_RE = re.compile(r"\b(?:1[0-9]{3}|20[0-9]{2})\b")
_MONTH_RE = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\b",
    re.IGNORECASE,
)
_SEQUENCE_MARKERS = (
    "first,", "then,", "next,", "finally,", "after that", "followed by",
    "began", "begins", "ended with", "led to",
)


_PAIR_CUES = (
    "than", "versus", "vs", "compared", "while", "whereas",
)


def _text_features(text, scenario=None):
    """Plan-level text features for template routing. Deliberately
    lightweight: the graphics pipeline's assemblers remain the
    authority on whether a candidate can actually be built, and a
    refusal there is recorded — the Director only orders the
    attempt.

    Wave 1 (§D.1) features: has_series (>= 3 measured numbers,
    bare years excluded), has_pair (two values joined by a
    comparison cue), has_places (>= 2 distinct places recorded
    for the beat: geography-dataset countries named in the text
    plus the scenario's place)."""
    text = text or ""
    numbers = _NUMBER_RE.findall(text)

    def _is_bare_year(token):
        if "," in token or "." in token:
            return False
        try:
            value = int(token)
        except ValueError:
            return False
        return 1000 <= value <= 2099

    measured = [n for n in numbers if not _is_bare_year(n)]
    lowered = text.lower()
    places = set()
    scenario = scenario or {}
    scenario_place = scenario.get("place") or scenario.get("location")
    if scenario_place:
        places.add(str(scenario_place).strip().lower())
    try:
        from services.motion_engine import geography

        for name in geography.country_names():
            if name.lower() in lowered:
                places.add(name.lower())
    except Exception:
        pass
    return {
        "has_percent": bool(_PERCENT_RE.search(text)),
        "has_comparison": len(numbers) >= 2,
        "has_date": bool(_YEAR_RE.search(text) or _MONTH_RE.search(text)),
        "has_sequence": any(m in lowered for m in _SEQUENCE_MARKERS),
        "has_number": bool(numbers),
        "has_series": len(measured) >= 3,
        "has_pair": len(measured) >= 2 and any(
            re.search(rf"\b{cue}\b", lowered) for cue in _PAIR_CUES
        ),
        "has_places": len(places) >= 2,
        "places": sorted(places),
    }


def outcomes_from_processed_scenes(processed_scenes):
    """Shape the Asset Manager stage's records into per-sentence
    outcomes for the Director. Pure data shaping over the processed
    scene dicts the pipeline already produced — the same records
    jobs._build_graphic_targets reads, extended with the covering
    assets' identity/tier for covered sentences.

    Returns {sentence_number: outcome} where outcome is:
      {sentence_number, scene_number, sentence_index, text,
       scenario, covered (bool), starved (bool),
       tier, asset_title, asset_file, scene_photo_count,
       secondary_video}
    `secondary_video` (Phase 8) is the scene's first accepted
    verified VIDEO visual that does not cover this sentence and
    carries a recorded clip window — the only material a
    footage_window / split_screen composition may draw on:
      {path, clip_start, clip_end, title} or None.
    A sentence is `covered` when an explicitly accepted verified
    visual is linked to it; `starved` when the scene's
    missing_visuals record names it. A sentence in neither set is
    absent from the map (the Director decides it unresolved)."""
    outcomes = {}
    for processed in processed_scenes or []:
        if not isinstance(processed, dict):
            continue
        scene = processed.get("scene") or {}
        scene_number = scene.get("scene_number")
        sentences = scene.get("sentences") or []
        stats = processed.get("asset_stats") or {}
        missing_numbers = {
            entry.get("sentence_number")
            for entry in stats.get("missing_visuals") or []
            if isinstance(entry, dict)
        }
        visuals = [
            v for v in list(processed.get("videos") or [])
            + list(processed.get("images") or [])
            if isinstance(v, dict)
        ]
        accepted = [
            v for v in visuals
            if (v.get("verification") or {}).get("accepted") is True
            and not v.get("motion_graphic")
        ]
        scene_photo_count = sum(
            1 for v in accepted
            if (v.get("type") or "").lower() in {"image", "photo", "still"}
            or v.get("image_url") or v.get("file_path", "").lower().endswith(
                (".jpg", ".jpeg", ".png", ".webp")
            )
        )
        # Wave 1 document unlock (§D.1 has_document): the scene's
        # genuine document asset — an accepted verified visual the
        # evidence layer classified as a document, with a file and
        # recorded provenance. Its presence is what lets the
        # Director propose document_presentation WITH a source.
        document_asset = None
        for visual in accepted:
            if visual.get("evidence_type") != "document":
                continue
            path = visual.get("file_path") or visual.get("path")
            provenance = (
                visual.get("provider") or visual.get("source") or "")
            url = visual.get("source_url") or visual.get("url") or ""
            if path and (provenance or url):
                document_asset = {
                    "path": path,
                    "title": visual.get("title"),
                    "source": provenance,
                    "url": url,
                }
                break
        scene_has_identity_photo = any(
            v.get("evidence_type") == "identity"
            or v.get("asset_role") == "real_person_photo"
            for v in accepted
        )
        for index, sentence in enumerate(sentences):
            if not isinstance(sentence, dict):
                continue
            number = sentence.get("sentence_number")
            if number is None:
                continue
            covering = None
            for visual in accepted:
                linked = visual.get("sentence_index")
                if linked is None and visual.get("sentence_number") is not None:
                    if visual.get("sentence_number") == number:
                        linked = index
                if linked == index:
                    covering = visual
                    break
            scenarios = sentence.get("scenarios") or []
            scenario = (
                scenarios[0]
                if scenarios and isinstance(scenarios[0], dict)
                else {}
            )
            secondary_video = None
            for visual in accepted:
                if visual is covering:
                    continue
                kind = (visual.get("type") or visual.get(
                    "visual_type") or "").lower()
                path = visual.get("file_path") or visual.get("path")
                clip_start = visual.get("clip_start")
                clip_end = visual.get("clip_end")
                if (
                    kind in {"video", "footage"}
                    and path
                    and isinstance(clip_start, (int, float))
                    and isinstance(clip_end, (int, float))
                    and clip_end > clip_start
                ):
                    secondary_video = {
                        "path": path,
                        "clip_start": clip_start,
                        "clip_end": clip_end,
                        "title": visual.get("title"),
                    }
                    break
            outcomes[number] = {
                "sentence_number": number,
                "scene_number": scene_number,
                "sentence_index": index,
                "text": sentence.get("text")
                or sentence.get("sentence_text") or "",
                "scenario": scenario,
                "covered": covering is not None,
                "starved": number in missing_numbers,
                "tier": (covering or {}).get("tier"),
                "asset_title": (covering or {}).get("title"),
                "asset_file": (covering or {}).get("file_path")
                or (covering or {}).get("path"),
                "scene_photo_count": scene_photo_count,
                "secondary_video": secondary_video,
                "document_asset": document_asset,
                "scene_has_identity_photo": scene_has_identity_photo,
            }
    return outcomes


def _graphic_permitted(row):
    """A graphic is permitted for a beat when the v1 plan's own
    graphic_fallback allows it, or the beat's primary intent IS a
    graphic explanation. Anything else stays unresolved — the
    Director does not widen the plan's permission on its own."""
    fallback = row.get("graphic_fallback")
    if isinstance(fallback, dict) and fallback.get("allowed") is True:
        return True
    return row.get("primary_visual_intent") == "graphic_explanation"


def _template_candidates(row, outcome, pack):
    """Ordered engine-template candidates for a graphic beat, from
    plan-level features, ordered by the pack's template_affinities
    (AM-008 consumer: higher affinity first; unlisted templates
    keep feature order after listed ones). Returns (candidates,
    affinities_used).

    Wave 1 (§D.2): EVERY engine template is registered here under
    its recorded-evidence condition — closing the reachability
    gap is a wave acceptance criterion. document_presentation is
    proposed ONLY when the scene carries a genuine document asset
    (outcome["document_asset"], recorded provenance) — without
    one it stays refused, exactly as before (AM-010).

    Wave 2: the breadth templates register under the same
    discipline — text cues for the data families, recorded
    scenario data contracts (pyramid / checklist / stages /
    molecule / density points / expansion / scene markers /
    object labels / before-after pair) for the scenario-fed
    families, verified-photo counts for the photo treatments,
    and the genuine document asset for classified_file."""
    text = outcome.get("text") or ""
    scenario = outcome.get("scenario") or {}
    features = _text_features(text, scenario)
    lowered = text.lower()
    candidates = []
    if features["has_comparison"]:
        # GitHub Skills Programme (2026-11-25): for a pure data
        # comparison the Manim engine's growing-bar animation is
        # the stronger treatment, so it is proposed first; its
        # template refuses under 3 items (and whenever Manim is
        # unavailable on the host) and the chain falls through to
        # the PIL bar chart with the refusal recorded.
        candidates += ["manim_bar_chart", "mpl_bar_chart", "bar_chart", "stat_comparison"]
    if features["has_percent"]:
        candidates += ["data_gauge", "ring_badges"]
    # F1 charts: a measured series, a comparable pair.
    if features["has_series"]:
        # GitHub Skills Programme: same engine choice as the
        # comparison above — Manim's draw-on line first for a
        # recorded series, the PIL line chart as the recorded
        # fall-through (the Manim template refuses under 3 points
        # or when the engine is unavailable).
        candidates += ["manim_line_chart", "mpl_line_chart", "line_chart"]
    if features["has_pair"]:
        candidates.append("pie_chart")
    # Wave 2 (F1/F2 breadth): the dashboard, bubble chart and
    # holographic panel ride on the same recorded series cue.
    # (P0 removal, 2026-11-27: milestone_waveform candidacy on
    # the date cue removed with the template — completely.)
    if features["has_series"]:
        candidates += [
            "bubble_chart", "animated_dashboard", "holographic_panel"]
    place = scenario.get("place") or scenario.get("location")
    # (P0 removal, 2026-11-27: the F3 map family candidacy —
    # territory_map + border/route/trade_route/military variants
    # — removed with the renderers, completely. The geography
    # dataset survives; `place` is still read below for the
    # satellite_zoom cue.)
    # F5 schematic variants: only on caller-recorded layers.
    layers = scenario.get("layers")
    if isinstance(layers, list) and layers:
        candidates += [
            "exploded_view", "mechanical_cutaway", "blueprint_section"]
    # Wave 2 scenario-recorded data contracts (the layers
    # precedent): each of these templates is proposed ONLY when
    # the scenario itself records the data it renders — the
    # Director never proposes a graphic whose material is not
    # already on the record.
    pyramid = scenario.get("pyramid")
    if isinstance(pyramid, (list, dict)) and pyramid:
        candidates.append("population_pyramid")
    if isinstance(scenario.get("checklist"), list) \
            and scenario["checklist"]:
        candidates.append("checklist_graphic")
    if isinstance(scenario.get("stages"), list) \
            and len(scenario["stages"]) >= 2:
        candidates.append("construction_sequence")
    molecule = scenario.get("molecule")
    if isinstance(molecule, dict) and molecule.get("atoms"):
        candidates.append("molecular_diagram")
    if isinstance(scenario.get("density_points"), list) \
            and len(scenario["density_points"]) >= 3:
        candidates.append("heatmap")
    expansion = scenario.get("expansion")
    if isinstance(expansion, dict) \
            and isinstance(expansion.get("states"), list) \
            and len(expansion["states"]) >= 2:
        candidates.append("territory_expansion")
    if isinstance(scenario.get("scene_markers"), list) \
            and scenario["scene_markers"]:
        candidates.append("crime_scene_diagram")
    if scenario.get("before_image") and scenario.get("after_image"):
        candidates.append("before_after")
    photo_count = outcome.get("scene_photo_count") or 0
    # (P0 removal, 2026-11-27: evidence_board and red_string_board
    # candidacy removed with the templates — completely.)
    # F4 investigation variants on their recorded material.
    if outcome.get("document_asset"):
        candidates.append("document_presentation")
        # Wave 2: the classified presentation of the same
        # genuine document — proposed only with it, gated
        # identically in the pipeline.
        candidates.append("classified_file")
    if any(m in lowered for m in (
            "newspaper", "headline", "front page", "press reported")):
        candidates.append("newspaper_montage")
        # Wave 2: the wall is the montage's grid composition —
        # proposed after it (its assembler requires >= 3
        # recorded articles where the montage needs fewer).
        candidates.append("newspaper_wall")
    if outcome.get("scene_has_identity_photo") or (
        scenario.get("subject_type") == "person"
    ):
        candidates.append("profile_reveal")
    # F8/F6 photo & text treatments on recorded photos / quotes.
    if photo_count >= 1:
        # (P0 removal, 2026-11-27: photo_parallax removed from
        # this pair — template deleted completely. text_overlay
        # stays.)
        candidates += ["text_overlay"]
        # Wave 2 photo-backed breadth: the HUD needs recorded
        # numbers to read out; object labels need the scenario's
        # recorded anchors; the satellite zoom needs a recorded
        # place (or focus) plus an aerial cue in the text.
        if features["has_number"]:
            candidates.append("hud_overlay")
        if isinstance(scenario.get("object_labels"), list) \
                and scenario["object_labels"]:
            candidates.append("object_labels")
        if place and any(m in lowered for m in (
                "satellite", "aerial", "overhead", "from above")):
            candidates.append("satellite_zoom")
    if photo_count >= 3:
        # Wave 2: the layered reveal stacks the scene's recorded
        # photos — proposed only when at least three exist (its
        # assembler still refuses below two).
        candidates.append("layered_reveal")
    if outcome.get("sentence_index") == 0:
        candidates.append("archival_title")
    if '"' in text or "“" in text:
        candidates.append("text_highlight")
    # Dedupe, preserving feature order.
    candidates = list(dict.fromkeys(candidates))

    affinities = {}
    profile = (pack or {}).get("visual_profile") or {}
    raw = profile.get("template_affinities") or {}
    for name in candidates:
        if isinstance(raw.get(name), (int, float)):
            affinities[name] = raw[name]
    if affinities:
        def _key(item):
            index, name = item
            if name in affinities:
                return (0, -affinities[name], index)
            return (1, 0, index)

        candidates = [
            name for _, name in sorted(enumerate(candidates), key=_key)
        ]
    return candidates, affinities


def _family_candidates(row, outcome, production_context):
    """Non-engine graphic families the pipeline may attempt for
    this beat, in order. (P0 removal, 2026-11-27: the
    chronology/"timeline" family, the native stat family and the
    native process family were removed completely and are never
    candidates again — statistics are served by the engine chart
    templates above, fed by recorded data. Only the event
    schematic remains.)"""
    families = ["event_schematic"]
    return families


def _composition_decision(row, outcome, pack, scene_labelled,
                          bible_identifier=None):
    """The lower-third composition decision for a covered beat, or
    None. Requires: the pack's composition_affinities favour
    lower_third_card (weight >= 1 and >= every other non-full-frame
    pattern) and the beat is its scene's first covered beat.

    UV-03 (Unified Visual Audit): the label text prefers the
    beat's recorded BIBLE identifier — the scenario's place, else
    the date the bible timeline records for this sentence —
    over the covering asset's raw search title (a search-result
    string, never an identifier the production recorded for the
    beat). The other identifier becomes the sublabel. Only when
    the beat carries neither place nor date does the historical
    behaviour apply: the asset's own title as label, scenario
    place/era as sublabel. A label is a place/date identifier
    only — never a file name, a source label or a sentence —
    and never invented text: every candidate comes from a
    recorded field."""
    if scene_labelled:
        return None
    profile = (pack or {}).get("visual_profile") or {}
    affinities = profile.get("composition_affinities") or {}
    weight = affinities.get("lower_third_card")
    if not isinstance(weight, (int, float)) or weight < 1:
        return None
    others = [
        w for name, w in affinities.items()
        if name not in {"lower_third_card", "full_frame"}
        and isinstance(w, (int, float))
    ]
    if others and weight < max(others):
        return None
    scenario = outcome.get("scenario") or {}
    place = (scenario.get("place") or "").strip()
    date = (
        (bible_identifier or {}).get("date")
        or scenario.get("era")
        or scenario.get("date")
        or ""
    ).strip()
    asset_label = (outcome.get("asset_title") or "").strip()
    label = place or date or asset_label
    if not label:
        return None
    if place and label == place:
        sublabel = date or None
        label_source = "bible_identifier"
    elif date and label == date:
        sublabel = place or None
        label_source = "bible_identifier"
    else:
        sublabel = scenario.get("place") or scenario.get("era") or None
        if sublabel and sublabel.strip() == label:
            sublabel = None
        label_source = "asset_title"
    return {
        "pattern": "lower_third_card",
        "label": label,
        "sublabel": sublabel,
        "affinity": weight,
        "reason": "scene_entry_label",
        "label_source": label_source,
    }


def _non_full_frame_affinities(pack):
    profile = (pack or {}).get("visual_profile") or {}
    affinities = profile.get("composition_affinities") or {}
    return {
        name: w for name, w in affinities.items()
        if name != "full_frame" and isinstance(w, (int, float))
    }


def _split_screen_decision(row, outcome, pack, scene_split_used):
    """The split_screen composition decision (Phase 8, AM-011
    completion), or None. The most conservative trigger in the
    system — ALL of these must hold:

    * the pack's composition_affinities name split_screen with
      weight >= 1 AND strictly greater than every other
      non-full-frame pattern (no current pack does — the pattern
      activates only when a pack is deliberately edited to
      favour it; that is a data decision, not a code default);
    * the beat's own text carries the comparison signal the
      Director already computes (>= 2 numbers — the beat is
      contrasting two quantities, the editorial case for showing
      two visuals side by side);
    * the scene holds a second verified video with a recorded
      clip window (the outcome's secondary_video);
    * the scene has no split decision yet (one per scene).
    """
    if scene_split_used:
        return None
    secondary = outcome.get("secondary_video")
    if not secondary:
        return None
    weights = _non_full_frame_affinities(pack)
    weight = weights.get("split_screen")
    if not isinstance(weight, (int, float)) or weight < 1:
        return None
    others = [w for name, w in weights.items() if name != "split_screen"]
    if others and weight <= max(others):
        return None
    if not _text_features(outcome.get("text") or "")["has_comparison"]:
        return None
    return {
        "pattern": "split_screen",
        "split_asset": {
            "path": secondary["path"],
            "clip_start": secondary["clip_start"],
            "clip_end": secondary["clip_end"],
            "side": "right",
        },
        "affinity": weight,
        "reason": "comparison_split_screen",
    }


def _footage_window_decision(row, outcome, pack, scene_window_used):
    """The footage_window composition decision (Phase 8, AM-011
    completion), or None. Trigger rule (documented, conservative):

    * the pack's composition_affinities favour the window pattern
      (packs name it `picture_in_picture`) with weight >= 1 and
      >= every other non-full-frame pattern except lower_third_card
      (the scene-entry label keeps its own rule and precedence);
    * the scene holds a second verified video with a recorded
      clip window (the outcome's secondary_video) — the window
      shows genuinely retrieved, verified footage, never a
      re-derived or placeholder source;
    * the scene has no window decision yet (one per scene), and
      the beat carries no other composition (one per beat —
      enforced by the caller's precedence chain).
    """
    if scene_window_used:
        return None
    secondary = outcome.get("secondary_video")
    if not secondary:
        return None
    weights = _non_full_frame_affinities(pack)
    weight = weights.get("picture_in_picture")
    if not isinstance(weight, (int, float)) or weight < 1:
        return None
    others = [
        w for name, w in weights.items()
        if name not in {"picture_in_picture", "lower_third_card"}
    ]
    if others and weight < max(others):
        return None
    return {
        "pattern": "footage_window",
        "window_asset": {
            "path": secondary["path"],
            "clip_start": secondary["clip_start"],
            "clip_end": secondary["clip_end"],
            "title": secondary.get("title"),
        },
        "affinity": weight,
        "reason": "related_verified_footage_window",
    }


def reconcile_visual_plan(plan_v1, outcomes, genre_pack=None,
                          evidence=None, production_context=None,
                          compositions=True, bible=None):
    """Pass-2 reconciliation. Pure: takes plain dicts, returns
    (plan_v2, graphics_manifest) as plain dicts.

    `evidence` maps sentence_number -> {"evidence_ref": {...},
    "verified_data": {...}} for AM-009; an entry stamps its
    graphic verified ONLY when evidence_ref carries an asset_id
    and verified_data carries a display value.

    `compositions` (Phase 9): when False, the composition
    decisions over COVERED beats (split screen, lower-third,
    footage window — the enhancement layer) are not issued at
    all: covered beats stay plain footage treatments. This is
    the Subtle motion-graphics intensity: graphics are still
    directed for starved beats (the coverage floor), but no
    decorative composition is layered over footage that already
    answers its sentence. Default True — the historical
    behaviour, unchanged for every existing caller."""
    from services.motion_engine import themes as _themes

    plan_v1 = plan_v1 if isinstance(plan_v1, dict) else {}
    outcomes = outcomes if isinstance(outcomes, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    pack = genre_pack if isinstance(genre_pack, dict) else None
    theme = _themes.theme_for_pack(pack)
    theme_name = theme.get("name") if theme else None

    # UV-03: the bible's per-sentence date identifiers — timeline
    # entries the fact table merged in carry their sentence
    # number; planner entries without one simply never match, and
    # callers that pass no bible get the historical behaviour.
    bible_identifiers = {}
    for _entry in (bible or {}).get("timeline") or []:
        if (
            isinstance(_entry, dict)
            and _entry.get("sentence_number") is not None
            and _entry.get("when")
        ):
            bible_identifiers[_entry["sentence_number"]] = {
                "date": _entry["when"],
            }

    rows = []
    manifest_entries = []
    counts = {TREATMENT_FOOTAGE: 0, TREATMENT_GRAPHIC: 0,
              TREATMENT_UNRESOLVED: 0}
    labelled_scenes = set()
    split_scenes = set()
    windowed_scenes = set()
    graphic_index = 0

    for row in plan_v1.get("sentences") or []:
        if not isinstance(row, dict):
            continue
        number = row.get("sentence_number")
        outcome = outcomes.get(number) or {}
        decided = dict(row)
        # The outcome's scene-local index rides onto the v2 row so
        # downstream consumers (composition stamping) can match
        # decisions to timeline items, which are keyed by
        # (scene_number, sentence_index).
        if outcome.get("sentence_index") is not None:
            decided["sentence_index"] = outcome["sentence_index"]
        if outcome.get("covered"):
            decision = {
                "treatment": TREATMENT_FOOTAGE,
                "reason": "verified_visual_assigned",
                "tier": outcome.get("tier"),
                "asset_file": outcome.get("asset_file"),
            }
            # Composition precedence per beat (Phase 8): the
            # strictest trigger first — split_screen (comparison
            # signal + strictly-favoured affinity), then the
            # scene-entry lower-third (Phase 6 rule, unchanged),
            # then the footage window. One composition per beat;
            # each pattern is capped at one per scene by its own
            # rule.
            scene_number = outcome.get("scene_number")
            composition = None
            if compositions:
                composition = _split_screen_decision(
                    row, outcome, pack,
                    scene_split_used=scene_number in split_scenes,
                )
                if composition is not None:
                    split_scenes.add(scene_number)
                if composition is None:
                    composition = _composition_decision(
                        row, outcome, pack,
                        scene_labelled=scene_number in labelled_scenes,
                        bible_identifier=bible_identifiers.get(number),
                    )
                    if composition is not None:
                        labelled_scenes.add(scene_number)
                if composition is None:
                    composition = _footage_window_decision(
                        row, outcome, pack,
                        scene_window_used=scene_number in windowed_scenes,
                    )
                    if composition is not None:
                        windowed_scenes.add(scene_number)
            if composition is not None:
                decision["composition"] = composition
            counts[TREATMENT_FOOTAGE] += 1
        elif outcome.get("starved"):
            if _graphic_permitted(row):
                graphic_index += 1
                manifest_id = f"gm_{graphic_index:04d}"
                candidates, affinities = _template_candidates(
                    row, outcome, pack
                )
                entry_evidence = evidence.get(number) or {}
                evidence_ref = entry_evidence.get("evidence_ref")
                verified_data = entry_evidence.get("verified_data")
                fact_basis = FACT_BASIS_SCRIPT
                if (
                    isinstance(evidence_ref, dict)
                    and evidence_ref.get("asset_id")
                    and isinstance(verified_data, dict)
                    and verified_data.get("display")
                ):
                    fact_basis = FACT_BASIS_ASSET
                elif (
                    isinstance(evidence_ref, dict)
                    and evidence_ref.get("fact_id")
                    and isinstance(verified_data, dict)
                    and verified_data.get("display")
                ):
                    # Phase 2.3: script-fact evidence (the
                    # sentence's own extracted fact, via the
                    # evidence producer's fact fallback). The data
                    # rides through with its recorded basis:
                    # research_supported ONLY when the ref carries
                    # the KB source that corroborated it — never on
                    # the producer's say-so alone — and script_claim
                    # otherwise. The stamp always says which.
                    if (
                        entry_evidence.get("fact_basis")
                        == FACT_BASIS_RESEARCH
                        and evidence_ref.get("kb_source")
                    ):
                        fact_basis = FACT_BASIS_RESEARCH
                else:
                    evidence_ref = None
                    verified_data = None
                families = _family_candidates(
                    row, outcome, production_context
                )
                # (P0 removal, 2026-11-27: the verified-data path
                # fed the native stat family, which is removed.
                # The entry still carries its evidence_ref /
                # verified_data record — the engine chart
                # assemblers read the same facts from the bible's
                # recorded fact table — but no stat family is
                # inserted: that family no longer exists.)
                manifest_entries.append({
                    "manifest_id": manifest_id,
                    "target": {
                        "scene_number": outcome.get("scene_number"),
                        "sentence_index": outcome.get("sentence_index"),
                        "sentence_number": number,
                    },
                    # The target sentence's own text + scenario ride
                    # on the entry: the graphics assemblers build
                    # ONLY from the sentence's recorded material.
                    "text": outcome.get("text") or "",
                    "scenario": outcome.get("scenario") or {},
                    "purpose": (
                        "graphic_explanation"
                        if row.get("primary_visual_intent")
                        == "graphic_explanation"
                        else "coverage_fallback"
                    ),
                    "primary_visual_intent": row.get(
                        "primary_visual_intent"
                    ),
                    "template": candidates[0] if candidates else None,
                    "template_candidates": candidates,
                    "family_candidates": families,
                    "affinities": affinities,
                    "theme": theme_name,
                    "fact_basis": fact_basis,
                    "evidence_ref": evidence_ref,
                    "verified_data": verified_data,
                    "reason": "starved_beat_graphic_permitted",
                })
                decision = {
                    "treatment": TREATMENT_GRAPHIC,
                    "reason": "starved_beat_graphic_permitted",
                    "manifest_id": manifest_id,
                }
                counts[TREATMENT_GRAPHIC] += 1
            else:
                decision = {
                    "treatment": TREATMENT_UNRESOLVED,
                    "reason": "graphic_not_permitted_by_plan",
                }
                counts[TREATMENT_UNRESOLVED] += 1
        else:
            decision = {
                "treatment": TREATMENT_UNRESOLVED,
                "reason": (
                    "starved_outcome_unrecorded"
                    if outcome
                    else "no_asset_outcome_recorded"
                ),
            }
            counts[TREATMENT_UNRESOLVED] += 1
        decided["decision"] = decision
        rows.append(decided)

    plan_v2 = {
        "version": PLAN_V2_VERSION,
        "project_id": plan_v1.get("project_id"),
        "analysis_mode": plan_v1.get("analysis_mode"),
        "scene_governor": plan_v1.get("scene_governor"),
        "reconciled_from": "visual_plan_v1",
        "theme": theme_name,
        "sentences": rows,
        "summary": {
            "treatments": counts,
            "graphics_manifested": len(manifest_entries),
            "compositions": sum(
                1 for r in rows
                if (r.get("decision") or {}).get("composition")
            ),
            # Phase 9: whether the composition layer was enabled
            # for this reconciliation (Subtle intensity disables
            # it); recorded so a zero count is explainable.
            "compositions_enabled": bool(compositions),
            "pack_fields_consumed": [
                "template_affinities", "composition_affinities",
                "visual_theme",
            ] if pack else [],
        },
    }
    manifest = {
        "version": GRAPHICS_MANIFEST_VERSION,
        "project_id": plan_v1.get("project_id"),
        "theme": theme_name,
        "entries": manifest_entries,
    }
    return plan_v2, manifest
