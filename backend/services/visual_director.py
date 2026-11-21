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

Composition (AM-011): a FOOTAGE beat may additionally carry a
composition decision — a lower_third_card overlay labelled with
the covering asset's own recorded title — when the genre pack's
composition_affinities favour the pattern and the beat is its
scene's first covered beat (one label per scene entry, never a
blanket overlay). The decision is data for the composition engine;
the Director renders nothing.

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


def _text_features(text):
    """Plan-level text features for template routing. Deliberately
    lightweight: the graphics pipeline's assemblers remain the
    authority on whether a candidate can actually be built, and a
    refusal there is recorded — the Director only orders the
    attempt."""
    text = text or ""
    numbers = _NUMBER_RE.findall(text)
    return {
        "has_percent": bool(_PERCENT_RE.search(text)),
        "has_comparison": len(numbers) >= 2,
        "has_date": bool(_YEAR_RE.search(text) or _MONTH_RE.search(text)),
        "has_sequence": any(m in text.lower() for m in _SEQUENCE_MARKERS),
        "has_number": bool(numbers),
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
       tier, asset_title, asset_file, scene_photo_count}
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
    affinities_used). document_presentation is NEVER a candidate —
    it stays disabled (no genuine document source exists)."""
    features = _text_features(outcome.get("text") or "")
    scenario = outcome.get("scenario") or {}
    candidates = []
    if features["has_comparison"]:
        candidates += ["bar_chart", "stat_comparison"]
    if features["has_percent"]:
        candidates += ["data_gauge", "ring_badges"]
    place = scenario.get("place") or scenario.get("location")
    if place:
        candidates.append("territory_map")
    if (outcome.get("scene_photo_count") or 0) > 0:
        candidates.append("evidence_board")
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
    this beat, in order. The stat family rides on the pipeline's
    own extraction; chronology/process are included ONLY on a
    plan-level signal (D1: no unconditional renders)."""
    features = _text_features(outcome.get("text") or "")
    families = []
    if features["has_number"]:
        families.append("stat")
    if features["has_date"]:
        families.append("timeline")
    if features["has_sequence"] and (
        (production_context or {}).get("timeline_event_count") or 0
    ) >= 3:
        families.append("process")
    families.append("event_schematic")
    return families


def _composition_decision(row, outcome, pack, scene_labelled):
    """The lower-third composition decision for a covered beat, or
    None. Requires: the pack's composition_affinities favour
    lower_third_card (weight >= 1 and >= every other non-full-frame
    pattern), the beat is its scene's first covered beat, and the
    covering asset carries a recorded title to label with. The
    label is the asset's own title — never invented text."""
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
    label = (outcome.get("asset_title") or "").strip()
    if not label:
        return None
    scenario = outcome.get("scenario") or {}
    sublabel = scenario.get("place") or scenario.get("era") or None
    return {
        "pattern": "lower_third_card",
        "label": label,
        "sublabel": sublabel,
        "affinity": weight,
        "reason": "scene_entry_label",
    }


def reconcile_visual_plan(plan_v1, outcomes, genre_pack=None,
                          evidence=None, production_context=None):
    """Pass-2 reconciliation. Pure: takes plain dicts, returns
    (plan_v2, graphics_manifest) as plain dicts.

    `evidence` maps sentence_number -> {"evidence_ref": {...},
    "verified_data": {...}} for AM-009; an entry stamps its
    graphic verified ONLY when evidence_ref carries an asset_id
    and verified_data carries a display value."""
    from services.motion_engine import themes as _themes

    plan_v1 = plan_v1 if isinstance(plan_v1, dict) else {}
    outcomes = outcomes if isinstance(outcomes, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    pack = genre_pack if isinstance(genre_pack, dict) else None
    theme = _themes.theme_for_pack(pack)
    theme_name = theme.get("name") if theme else None

    rows = []
    manifest_entries = []
    counts = {TREATMENT_FOOTAGE: 0, TREATMENT_GRAPHIC: 0,
              TREATMENT_UNRESOLVED: 0}
    labelled_scenes = set()
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
            composition = _composition_decision(
                row, outcome, pack,
                scene_labelled=outcome.get("scene_number") in labelled_scenes,
            )
            if composition is not None:
                decision["composition"] = composition
                labelled_scenes.add(outcome.get("scene_number"))
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
                else:
                    evidence_ref = None
                    verified_data = None
                families = _family_candidates(
                    row, outcome, production_context
                )
                if fact_basis == FACT_BASIS_ASSET and "stat" not in families:
                    # The verified-data path IS the stat family: an
                    # evidence-bearing entry always permits it,
                    # whatever the sentence's own numbers look like.
                    families.insert(0, "stat")
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
