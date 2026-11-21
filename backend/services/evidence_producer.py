"""AM-009 live evidence producer (Master Directive Phase 8).

Phase 6 built the verified asset-data fact-basis path end to end
(Director evidence -> graphics manifest -> stat maker) but no
production caller assembled the evidence. This module is that
caller-side producer: it scans a production's processed scenes
for VERIFIED assets carrying recorded structured facts and maps
them onto the production's starved sentences, producing the
`evidence` argument `visual_director.reconcile_visual_plan`
consumes.

The rule is deliberately narrow — the register's guard is "stamp
VERIFIED only on an exact recorded ref":

* A fact is eligible only when it is RECORDED on a verified
  (verification.accepted) asset's own record, under
  `metadata_facts`: a list of {"display", "kind"} entries copied
  verbatim from provider-supplied structured metadata (the media
  pipeline passes a candidate's `metadata_facts` through onto the
  asset record untouched — it derives nothing). The producer
  copies an entry verbatim; it never paraphrases, formats or
  computes a value.
* The asset must sit in the SAME SCENE as the starved sentence
  (editorial adjacency: the graphic annotates the scene's own
  verified material, never a fact borrowed from another scene).
* The asset must carry a recorded identity (`candidate_id` or
  `asset_id`) — the evidence_ref names it exactly:
  {"asset_id", "source", "url"} drawn from the asset's record.
* At most one evidence entry per starved sentence (the first
  eligible fact in scene order).

What is deliberately NOT eligible: probed technical facts
(width/height/fps/duration — true statements about the FILE, not
about the sentence's subject; stamping them VERIFIED on a subject
graphic would mislead), asset titles, and any value extracted
from the sentence itself (that is the script_claim path).

The producer also returns a production record counting, per
starved sentence, what happened — produced / no verified asset
in scene / verified assets but no recorded fact — so the firing
rate is a measured production fact, never a silent zero. The
whole producer is fail-soft by contract: jobs calls it inside
the Director's existing try/except, and any unexpected shape
degrades to an empty map plus the record, never an exception
escaping into the pipeline (D2 record-and-continue).
"""


def _verified_assets_by_scene(processed_scenes):
    """{scene_number: [asset, ...]} over accepted, non-graphic
    visuals, in recorded order."""
    by_scene = {}
    for processed in processed_scenes or []:
        if not isinstance(processed, dict):
            continue
        scene = processed.get("scene") or {}
        scene_number = scene.get("scene_number")
        if scene_number is None:
            continue
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
        if accepted:
            by_scene.setdefault(scene_number, []).extend(accepted)
    return by_scene


def _asset_identity(asset):
    """The recorded identity an evidence_ref may name, or None.
    An asset without a recorded id cannot be referenced exactly,
    so it can never anchor a VERIFIED stamp."""
    asset_id = asset.get("candidate_id") or asset.get("asset_id")
    if not asset_id:
        return None
    return {
        "asset_id": asset_id,
        "source": asset.get("source") or asset.get("provider"),
        "url": asset.get("source_url") or asset.get("url"),
    }


def _recorded_fact(asset):
    """The asset's first recorded structured fact, verbatim, or
    None. Only the `metadata_facts` list qualifies — see the
    module docstring for what is deliberately excluded."""
    facts = asset.get("metadata_facts")
    if not isinstance(facts, list):
        return None
    for fact in facts:
        if (
            isinstance(fact, dict)
            and isinstance(fact.get("display"), str)
            and fact["display"].strip()
        ):
            return {
                "display": fact["display"],
                "kind": fact.get("kind") or "verified_value",
            }
    return None


def build_evidence(processed_scenes, outcomes):
    """Assemble the Director evidence map for one production.

    `outcomes` is the Director's own outcome map (only starved
    sentences can receive evidence — a covered beat needs no
    graphic and an unrecorded beat must not gain one). Returns
    (evidence, record): evidence maps sentence_number ->
    {"evidence_ref": {...}, "verified_data": {...}} in exactly
    the shape reconcile_visual_plan consumes; record counts the
    per-starved-sentence outcomes."""
    evidence = {}
    record = {
        "starved_sentences": 0,
        "produced": 0,
        "no_verified_asset_in_scene": 0,
        "no_recorded_fact": 0,
        "no_recorded_identity": 0,
    }
    try:
        by_scene = _verified_assets_by_scene(processed_scenes)
        for number, outcome in (outcomes or {}).items():
            if not isinstance(outcome, dict) or not outcome.get("starved"):
                continue
            record["starved_sentences"] += 1
            assets = by_scene.get(outcome.get("scene_number")) or []
            if not assets:
                record["no_verified_asset_in_scene"] += 1
                continue
            placed = False
            saw_fact_without_identity = False
            for asset in assets:
                fact = _recorded_fact(asset)
                if fact is None:
                    continue
                identity = _asset_identity(asset)
                if identity is None:
                    saw_fact_without_identity = True
                    continue
                evidence[number] = {
                    "evidence_ref": identity,
                    "verified_data": fact,
                }
                record["produced"] += 1
                placed = True
                break
            if not placed:
                if saw_fact_without_identity:
                    record["no_recorded_identity"] += 1
                else:
                    record["no_recorded_fact"] += 1
    except Exception:
        # Fail-soft (D2): evidence production must never break the
        # Director stage; the record shows what was assembled
        # before the failure, and the caller continues without
        # the remaining evidence.
        record["producer_error"] = True
    return evidence, record
