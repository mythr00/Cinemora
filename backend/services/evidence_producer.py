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

SCRIPT-FACT FALLBACK (Transformation Phase 2.3, 2026-11-22)
    When the caller supplies the production's structured fact
    table (services.script_facts, extracted at planning time from
    the script's own sentences), a starved sentence that no
    verified asset could serve may still rest its graphic on a
    fact extracted from ITS OWN sentence. The basis is recorded
    exactly: a plain script fact produces evidence stamped
    `script_claim`; a fact the knowledge base corroborated
    (basis research_supported, KB source recorded by the
    corroboration stage) produces `research_supported` evidence
    whose ref names the KB source. Asset evidence always wins —
    the fallback runs only for sentences the asset path left
    unserved — and the record counts the two fact paths
    separately (produced_script_fact / produced_kb_fact), so the
    firing rate per basis stays measurable.
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


def _facts_by_sentence(fact_table):
    """{sentence_number: [fact, ...]} from a script_facts table
    (the dict build_fact_table returns, or a bare fact list)."""
    if isinstance(fact_table, dict):
        facts = fact_table.get("facts")
    else:
        facts = fact_table
    by_sentence = {}
    for fact in facts or []:
        if isinstance(fact, dict) and fact.get("sentence_number") is not None:
            by_sentence.setdefault(fact["sentence_number"], []).append(fact)
    return by_sentence


def _fact_evidence(fact):
    """The Director evidence entry for one script fact, or None.

    The display the graphic presents: a quantity's own text, or a
    dated event's date. The ref names the fact exactly (fact_id +
    sentence) and, for a KB-corroborated fact, the KB source the
    corroboration stage recorded — the Director stamps
    research_supported only when that source is present."""
    if not isinstance(fact, dict) or not fact.get("fact_id"):
        return None
    if fact.get("kind") == "quantity":
        display = fact.get("display")
        kind = "script_quantity"
    elif fact.get("kind") in ("dated_event", "date"):
        display = fact.get("when") or fact.get("display")
        kind = "script_date"
    else:
        return None
    if not display or not str(display).strip():
        return None
    basis = fact.get("basis") or "script_claim"
    ref = {
        "fact_id": fact["fact_id"],
        "source": "script_facts",
        "sentence_number": fact.get("sentence_number"),
    }
    if basis == "research_supported":
        kb_source = (fact.get("source") or {}).get("source")
        if not kb_source:
            # A verified stamp without its recorded source is a
            # claim on trust — downgrade to the honest basis.
            basis = "script_claim"
        else:
            ref["kb_source"] = kb_source
            ref["kb_query"] = (fact.get("source") or {}).get("query")
    return {
        "evidence_ref": ref,
        "verified_data": {"display": str(display), "kind": kind},
        "fact_basis": basis,
    }


def _best_fact_evidence(facts):
    """The strongest evidence a sentence's own facts offer:
    KB-corroborated first, quantities before dates within a
    basis (a number is the stat family's natural headline)."""
    ranked = []
    for fact in facts or []:
        entry = _fact_evidence(fact)
        if entry is None:
            continue
        basis_rank = 0 if entry["fact_basis"] == "research_supported" else 1
        kind_rank = 0 if fact.get("kind") == "quantity" else 1
        ranked.append((basis_rank, kind_rank, entry))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]))
    return ranked[0][2]


def build_evidence(processed_scenes, outcomes, fact_table=None):
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
        "produced_script_fact": 0,
        "produced_kb_fact": 0,
        "no_verified_asset_in_scene": 0,
        "no_recorded_fact": 0,
        "no_recorded_identity": 0,
    }
    try:
        by_scene = _verified_assets_by_scene(processed_scenes)
        facts_by_sentence = _facts_by_sentence(fact_table)
        for number, outcome in (outcomes or {}).items():
            if not isinstance(outcome, dict) or not outcome.get("starved"):
                continue
            record["starved_sentences"] += 1
            assets = by_scene.get(outcome.get("scene_number")) or []
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
            if placed:
                continue
            # Phase 2.3 fallback: the sentence's own script facts
            # (asset evidence always wins — this runs only when
            # the asset path placed nothing).
            fact_entry = _best_fact_evidence(
                facts_by_sentence.get(number)
            )
            if fact_entry is not None:
                evidence[number] = fact_entry
                if fact_entry["fact_basis"] == "research_supported":
                    record["produced_kb_fact"] += 1
                else:
                    record["produced_script_fact"] += 1
                continue
            if not assets:
                record["no_verified_asset_in_scene"] += 1
            elif saw_fact_without_identity:
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
