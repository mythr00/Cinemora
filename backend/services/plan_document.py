"""Production plan document + provenance certificate (Stage 4).

The plan is the studio's review surface: one row per script
sentence carrying the story understanding (subject, scene, proposed
visual from the sentence's scenarios) and — once a run has produced
one — the visual actually chosen, its segment-evidence class, its
rights classification and its coverage state.

Artefacts (all inside the project's media directory):

- ``plan.json`` — the canonical plan artefact, written from a fresh
  analysis by POST /api/v1/projects/{id}/plan/generate, by the
  plan-approval analysis phase, and by run_full_pipeline's analysis
  stage. GET plan prefers it.
- Without plan.json (runs produced before Stage 4), the plan is
  assembled best-effort from ``story_understanding_cache.json``
  (bible + per-sentence plans) overlaid with ``timeline.json`` /
  ``asset_report.json`` / ``rights_manifest.json``. Timeline items
  are matched to sentences by scene + position when the plan's own
  scene structure is known, else by sentence text in order. The
  response says which source it used ("plan_source") — the fallback
  is labelled, never disguised as the canonical artefact.

The media index and the provenance certificate are built ONLY from
recorded artefacts (ledger decisions, timeline, rights manifest,
pipeline result, plan decisions, and the pipeline's canonical
output filenames). Nothing is served or certified from a raw
filesystem guess.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path

from services import media_paths
from services import tier_ladder
from services.atomic_write import write_text_atomic

PLAN_FILENAME = "plan.json"
VISUAL_PLAN_FILENAME = "visual_plan.json"
DECISIONS_FILENAME = "plan_decisions.json"

# The pipeline's canonical output filenames (jobs.run_full_pipeline
# writes exactly these names into the project directory).
CANONICAL_OUTPUTS = {
    "film": "documentary_final_output.mp4",
    "captions_json": "captions.json",
    "captions_srt": "captions.srt",
    "captions_vtt": "captions.vtt",
    "asset_report": "asset_report.json",
    "rights_manifest": "rights_manifest.json",
    "run_manifest": "run_manifest.json",
    "narration": "narration.wav",
}


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------
# Plan construction from a fresh analysis
# ---------------------------------------------------------------------


def _entity_names(bible):
    names = {}
    for entity in (bible or {}).get("entities") or []:
        if isinstance(entity, dict) and entity.get("id"):
            names[entity["id"]] = entity.get("canonical") or entity["id"]
    return names


def _proposed_visual(sentence):
    scenarios = sentence.get("scenarios") or []
    if not scenarios or not isinstance(scenarios[0], dict):
        return None
    scenario = scenarios[0]
    return {
        "description": scenario.get("visual") or "",
        "kind": scenario.get("kind"),
        "specificity": scenario.get("specificity"),
        "format_intent": scenario.get("format_intent"),
        "search_terms": scenario.get("search_terms") or [],
    }


def plan_from_analysis(analysis, project_id):
    """Build the plan document dict from an analyze_script result
    (the same dict jobs.build_production_scenes returns pieces of).
    Pure function — no I/O."""
    bible = analysis.get("bible") or {}
    names = _entity_names(bible)
    sentences = analysis.get("sentences") or []

    scene_of = {}
    scene_rows = []
    for scene in analysis.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        numbers = []
        for record in scene.get("sentences") or []:
            if isinstance(record, dict) and record.get("sentence_number"):
                scene_of[record["sentence_number"]] = scene.get("scene_number")
                numbers.append(record["sentence_number"])
        if numbers:
            scene_rows.append(
                {"scene_number": scene.get("scene_number"), "sentence_numbers": numbers}
            )
    if not scene_rows:
        # No story structure (degraded analysis): one implicit scene.
        numbers = [s.get("sentence_number") for s in sentences if isinstance(s, dict)]
        scene_rows = [{"scene_number": 1, "sentence_numbers": numbers}]
        for number in numbers:
            scene_of[number] = 1

    rows = []
    for sentence in sentences:
        if not isinstance(sentence, dict):
            continue
        number = sentence.get("sentence_number")
        rows.append(
            {
                "sentence_number": number,
                "scene_number": scene_of.get(number),
                "text": sentence.get("text") or "",
                "role": sentence.get("role"),
                "subject_id": sentence.get("subject_id"),
                "subject": names.get(sentence.get("subject_id")),
                "entities": sentence.get("entities") or [],
                "proposed_visual": _proposed_visual(sentence),
                # B-roll B1 (§4.1): the sentence's fallback concept
                # set + its source, so the Visual Plan page can show
                # the B-roll the system intends before production.
                "fallback_concepts": sentence.get("fallback_concepts"),
                "concepts_source": sentence.get("concepts_source"),
                # Master Directive Phase 2 (§4.2): the sentence's
                # visual-intention record, beside its concepts.
                "visual_intents": sentence.get("visual_intents"),
            }
        )
    return {
        "version": 1,
        "project_id": str(project_id),
        "analysis_mode": analysis.get("mode"),
        "bible": {
            "title": bible.get("title"),
            "story_type": bible.get("story_type"),
            "time_period": bible.get("story_era") or bible.get("time_period"),
            "primary_subject_id": bible.get("primary_subject_id"),
            "entity_count": len(bible.get("entities") or []),
        },
        "scenes": scene_rows,
        "sentences": rows,
    }


def visual_plan_from_analysis(analysis, project_id, scene_governor=None):
    """visual_plan.json v1 (Master Directive Phase 2, §4.2): the
    PRE-RETRIEVAL visual plan — what the system intends to show for
    every sentence, recorded before any search runs. One row per
    sentence: its visual intentions, the concept set they were
    seeded from, and the tier-ladder entry tier its role implies
    (D-B3). Pure function — no I/O. Additive artefact: plan.json
    and every pre-Phase-2 reader are untouched, and runs that never
    write it (legacy paths) behave exactly as before."""
    base = plan_from_analysis(analysis, project_id)
    rows = []
    for row in base["sentences"]:
        intents = row.get("visual_intents") or {}
        rows.append(
            {
                "sentence_number": row.get("sentence_number"),
                "scene_number": row.get("scene_number"),
                "role": row.get("role"),
                "primary_visual_intent": intents.get(
                    "primary_visual_intent"
                ),
                "secondary_visual_intents": intents.get(
                    "secondary_visual_intents"
                ) or [],
                "intent_queries": intents.get("intent_queries") or {},
                "graphic_fallback": intents.get("graphic_fallback"),
                "preferred_media_type": intents.get(
                    "preferred_media_type"
                ),
                "specificity_requirement": intents.get(
                    "specificity_requirement"
                ),
                "visual_misrepresentation_risk": intents.get(
                    "visual_misrepresentation_risk"
                ),
                "intents_source": intents.get("intents_source"),
                "fallback_concepts": row.get("fallback_concepts"),
                "concepts_source": row.get("concepts_source"),
                "entry_tier": tier_ladder.entry_tier_for_role(
                    row.get("role")
                ),
            }
        )
    return {
        "version": 1,
        "project_id": str(project_id),
        "analysis_mode": base.get("analysis_mode"),
        "scene_governor": scene_governor,
        "sentences": rows,
    }


def write_visual_plan_document(project_dir, analysis, project_id,
                               scene_governor=None):
    """Persist visual_plan.json from a fresh analysis (same atomic
    pattern as plan.json). Fail-soft: returns the path or None."""
    from datetime import datetime, timezone

    try:
        document = visual_plan_from_analysis(
            analysis, project_id, scene_governor=scene_governor
        )
        document["generated_at"] = datetime.now(timezone.utc).isoformat()
        project_dir = Path(project_dir)
        target = project_dir / VISUAL_PLAN_FILENAME
        payload = json.dumps(document, indent=2, ensure_ascii=False)
        write_text_atomic(target, payload)
        return target
    except Exception as error:
        print(f"WARNING: visual plan document write failed: {error}")
        return None


def write_plan_document(project_dir, analysis, project_id):
    """Persist plan.json from a fresh analysis (atomic write via
    services.atomic_write, the single MA-11 pattern since Stage 4).
    Fail-soft: returns the path or None."""
    from datetime import datetime, timezone

    try:
        document = plan_from_analysis(analysis, project_id)
        document["generated_at"] = datetime.now(timezone.utc).isoformat()
        project_dir = Path(project_dir)
        target = project_dir / PLAN_FILENAME
        payload = json.dumps(document, indent=2, ensure_ascii=False)
        write_text_atomic(target, payload)
        return target
    except Exception as error:
        print(f"WARNING: plan document write failed: {error}")
        return None


# ---------------------------------------------------------------------
# Plan assembly for serving (plan.json preferred, cache fallback)
# ---------------------------------------------------------------------


def _plan_from_cache(project_dir):
    """Best-effort plan from story_understanding_cache.json (runs
    produced before plan.json existed). Sentences come from the
    cached per-sentence plans; scene structure is unknown, so scene
    numbers are filled in from the timeline overlay when possible."""
    cache = _read_json(Path(project_dir) / "story_understanding_cache.json")
    if not isinstance(cache, dict):
        return None
    bible = cache.get("bible") or {}
    names = _entity_names(bible)
    rows = []
    for index, plan in enumerate(cache.get("plans") or []):
        if not isinstance(plan, dict):
            continue
        sentence = {"scenarios": plan.get("scenarios") or []}
        rows.append(
            {
                "sentence_number": index + 1,
                "scene_number": None,
                "text": plan.get("sentence") or "",
                "role": plan.get("role"),
                "subject_id": plan.get("subject_id"),
                "subject": names.get(plan.get("subject_id")),
                "entities": plan.get("entities") or [],
                "proposed_visual": _proposed_visual(sentence),
                # B-roll B1: cached plans written before B1 carry no
                # concepts — None, honestly unstated.
                "fallback_concepts": plan.get("fallback_concepts"),
                "concepts_source": plan.get("concepts_source"),
                # Phase 2: same honesty for intentions — cached
                # plans written before Phase 2 carry none.
                "visual_intents": plan.get("visual_intents"),
            }
        )
    if not rows:
        return None
    return {
        "version": 1,
        "project_id": None,
        "analysis_mode": cache.get("mode"),
        "bible": {
            "title": bible.get("title"),
            "story_type": bible.get("story_type"),
            "time_period": bible.get("story_era") or bible.get("time_period"),
            "primary_subject_id": bible.get("primary_subject_id"),
            "entity_count": len(bible.get("entities") or []),
        },
        "scenes": [],
        "sentences": rows,
    }


def _timeline_items(project_dir):
    # Stage 4 (DATA-004): timeline.json is run-scoped for new runs;
    # resolve to the freshest run copy, legacy path as fallback.
    timeline = _read_json(
        media_paths.resolve_artefact(project_dir, "timeline.json")
    )
    if not isinstance(timeline, dict):
        return []
    items = timeline.get("items")
    if items:
        return [i for i in items if isinstance(i, dict)]
    collected = []
    for scene in timeline.get("scenes") or []:
        if isinstance(scene, dict):
            collected.extend(
                i for i in (scene.get("items") or []) if isinstance(i, dict)
            )
    return collected


def _coverage_for(item):
    """Coverage state for one timeline item, in the studio's
    vocabulary: verified / rotation / graphic."""
    if item is None:
        return None
    if item.get("assignment") == "rotation":
        return "rotation"
    if item.get("assignment") == "motion_graphic":
        return "graphic"
    if item.get("relevance_status") == "verified_for_sentence":
        return "verified"
    if item.get("assignment") == "sentence":
        return "verified"
    return "graphic" if item.get("type") == "graphic" else "verified"


def _chosen_visual(item, rights_by_path, project_id=None, project_dir=None):
    path = str(item.get("file_path") or item.get("path") or "")
    rights = rights_by_path.get(path)
    media_key = None
    if path and project_dir is not None:
        try:
            media_key = media_key_for(project_dir, path)
        except (ValueError, OSError):
            media_key = None
    return {
        "type": item.get("type"),
        "provider": item.get("source"),
        "source_url": item.get("source_url") or None,
        "title": item.get("title"),
        "file_name": Path(path).name if path else None,
        "assignment": item.get("assignment"),
        "relevance_status": item.get("relevance_status"),
        "media_key": media_key,
        "media_url": (
            f"/api/v1/projects/{project_id}/media/{media_key}"
            if media_key and project_id
            else None
        ),
    }, rights


def load_plan_decisions(project_dir):
    data = _read_json(Path(project_dir) / DECISIONS_FILENAME)
    if isinstance(data, dict) and isinstance(data.get("decisions"), list):
        return data["decisions"]
    return []


def build_plan(project_id, project_dir):
    """Assemble the servable plan for a project: the plan document
    (canonical or cache fallback) overlaid with the run's chosen
    visuals, evidence, rights, coverage and recorded decisions.
    Returns None when no analysis artefact exists at all."""
    project_dir = Path(project_dir)
    document = _read_json(project_dir / PLAN_FILENAME)
    source = "plan_json"
    if not isinstance(document, dict):
        document = _plan_from_cache(project_dir)
        source = "story_cache"
    if document is None:
        return None
    document["project_id"] = str(project_id)
    document["plan_source"] = source

    items = _timeline_items(project_dir)
    report = (
        _read_json(media_paths.resolve_artefact(project_dir, "asset_report.json"))
        or {}
    )
    missing = {
        entry.get("sentence_number")
        for entry in (report.get("missing_visuals") or [])
        if isinstance(entry, dict)
    }
    rights_by_path = {}
    for entry in (
        _read_json(
            media_paths.resolve_artefact(project_dir, "rights_manifest.json")
        )
        or []
    ):
        if isinstance(entry, dict) and entry.get("file_path"):
            rights_by_path[str(entry["file_path"])] = {
                "license": entry.get("license") or "unknown",
                "rights_status": entry.get("rights_status"),
                "creator": entry.get("creator"),
                "source": entry.get("source"),
            }

    # Position of each sentence within its scene (for timeline
    # matching when the plan's scene structure is known).
    position = {}
    for scene in document.get("scenes") or []:
        for index, number in enumerate(scene.get("sentence_numbers") or []):
            position[number] = (scene.get("scene_number"), index)
    items_by_position = {}
    items_by_text = {}
    for item in items:
        key = (item.get("scene_number"), item.get("sentence_index"))
        if key not in items_by_position:
            items_by_position[key] = item
        text = item.get("sentence")
        if text and text not in items_by_text:
            items_by_text[text] = item

    decisions = {}
    for decision in load_plan_decisions(project_dir):
        number = decision.get("sentence_number")
        if number is not None:
            decisions.setdefault(number, []).append(decision)

    summary = {"verified": 0, "rotation": 0, "graphic": 0, "missing": 0, "uncovered": 0}
    for row in document["sentences"]:
        number = row["sentence_number"]
        item = None
        if number in position:
            item = items_by_position.get(position[number])
            if item is not None and row.get("scene_number") is None:
                row["scene_number"] = position[number][0]
        if item is None:
            item = items_by_text.get(row["text"])
            if item is not None and row.get("scene_number") is None:
                row["scene_number"] = item.get("scene_number")
        if item is not None:
            chosen, rights = _chosen_visual(
                item, rights_by_path, project_id=project_id, project_dir=project_dir
            )
            row["chosen_visual"] = chosen
            row["evidence_class"] = item.get("segment_evidence")
            row["rights"] = rights
            row["coverage"] = _coverage_for(item)
        else:
            row["chosen_visual"] = None
            row["evidence_class"] = None
            row["rights"] = None
            row["coverage"] = "missing" if number in missing else "uncovered"
        summary[row["coverage"]] = summary.get(row["coverage"], 0) + 1
        row["decisions"] = decisions.get(number, [])
    document["summary"] = {
        "sentences": len(document["sentences"]),
        **summary,
        "decisions_recorded": sum(len(v) for v in decisions.values()),
    }
    return document


# ---------------------------------------------------------------------
# Media index — the ONLY source of servable files
# ---------------------------------------------------------------------


def media_key_for(project_dir, path):
    """Stable key for a file under the project dir: sha256 of the
    project-relative path. Keys reveal nothing about the filesystem
    layout beyond what the artefacts themselves record."""
    relative = Path(path).resolve().relative_to(Path(project_dir).resolve())
    return hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest()[:20]


def _revision_documents(project_dir):
    """The project's editor revision documents (Stage 6), read from
    disk: revision_<id>.json files in the project directory. The DB
    is truth for revision records; the documents are the recorded
    artefacts the media index is allowed to trust."""
    documents = []
    try:
        paths = sorted(Path(project_dir).glob("revision_*.json"))
    except OSError:
        return documents
    for path in paths:
        document = _read_json(path)
        if isinstance(document, dict):
            documents.append(document)
    return documents


def build_media_index(project_id, project_dir):
    """Map media key -> file facts, built exclusively from recorded
    artefacts: timeline items, the rights manifest, the pipeline
    result, recorded plan-decision replacements, the pipeline's
    canonical output files that exist on disk, the exports recorded
    in editor revision documents (Stage 6), the editor_media files
    recorded as visual-item assets in revision documents (IA-04:
    regenerated graphics live only there), the music files
    recorded in revision documents and in the production's
    soundtrack record (Stage J), and the files recorded in the
    project's upload index. Every indexed path is verified to live
    under the project directory."""
    project_dir = Path(project_dir).resolve()
    index = {}

    def _add(path, kind):
        if not path:
            return
        try:
            candidate = Path(str(path)).resolve()
            relative = candidate.relative_to(project_dir)
        except (ValueError, OSError):
            return
        if not candidate.is_file():
            return
        key = hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest()[:20]
        if key not in index:
            index[key] = {
                "key": key,
                "path": candidate,
                "file_name": candidate.name,
                "kind": kind,
                "content_type": mimetypes.guess_type(candidate.name)[0]
                or "application/octet-stream",
                "size_bytes": candidate.stat().st_size,
            }

    for item in _timeline_items(project_dir):
        _add(item.get("file_path") or item.get("path"), "timeline_visual")
    for entry in (
        _read_json(
            media_paths.resolve_artefact(project_dir, "rights_manifest.json")
        )
        or []
    ):
        if isinstance(entry, dict):
            _add(entry.get("file_path"), "rights_manifest")
    result = _read_json(
        media_paths.resolve_artefact(project_dir, "pipeline_result.json")
    )
    if isinstance(result, dict):
        for field in ("video", "captions", "narration", "timeline"):
            _add(result.get(field), "pipeline_output")
    for decision in load_plan_decisions(project_dir):
        replacement = decision.get("replacement") or {}
        _add(replacement.get("file_path"), "plan_replacement")
        refreshed = decision.get("refreshed_asset") or {}
        _add(refreshed.get("file_path"), "plan_replacement")
    # Editor artefacts (Stage 6): only paths RECORDED in a revision
    # document's exports list or in the upload index are servable —
    # the same recorded-artefact rule as everything above, so the
    # editor's preview/download keys (sha256 of the project-relative
    # path) resolve while unrecorded files still 404.
    for document in _revision_documents(project_dir):
        for export in document.get("exports") or []:
            if isinstance(export, dict):
                _add(export.get("file"), "revision_export")
        # IA-04: regenerated graphics (editor regenerate_graphic)
        # are written under editor_media/<revision_id>/ and are
        # recorded ONLY as visual-item assets in the revision
        # document — they appear in no timeline, manifest, export
        # list or upload index, so without this source they could
        # never be served. Only visual-item paths under
        # editor_media/ are trusted from this source: production
        # visuals in the same list are already indexed from their
        # own recorded artefacts, and trusting every visual path
        # would widen the index beyond the editor-created files
        # this source exists for. _add still applies the
        # under-project-dir guard, so traversal records and
        # cross-project records stay unserved, and a file merely
        # present under editor_media/ without a revision-document
        # record stays unindexed.
        tracks = document.get("tracks") or {}
        if isinstance(tracks, dict):
            for item in tracks.get("visual") or []:
                if not isinstance(item, dict):
                    continue
                asset = item.get("asset") or {}
                if not isinstance(asset, dict):
                    continue
                recorded = asset.get("file_path") or asset.get("path")
                if not recorded:
                    continue
                try:
                    relative = Path(str(recorded)).resolve().relative_to(
                        project_dir
                    )
                except (ValueError, OSError):
                    continue
                if not relative.parts or relative.parts[0] != "editor_media":
                    continue
                _add(recorded, "editor_media")
        # Stage J (P5): a revision's RECORDED music selection is
        # servable for editor preview, under the same
        # recorded-paths-only rule — the document's music track
        # file and its soundtrack record's identity. A music file
        # outside the project directory (an operator path the
        # editor accepted) is not under the project dir and stays
        # unserved; the UI reports preview unavailable for it.
        music_track = (document.get("tracks") or {}).get("music") or {}
        _add(music_track.get("file"), "revision_music")
        record_identity = (
            (music_track.get("soundtrack_record") or {}).get("identity")
            or {}
        )
        _add(record_identity.get("file_path"), "revision_music")
    # The production's applied music (Stage H identity recorded in
    # soundtrack.json) is likewise a recorded path.
    soundtrack_record = _read_json(project_dir / "soundtrack.json")
    if isinstance(soundtrack_record, dict):
        identity = soundtrack_record.get("identity") or {}
        _add(identity.get("file_path"), "music")
    uploads_index = _read_json(project_dir / "uploads" / "index.json")
    if isinstance(uploads_index, dict):
        for upload in uploads_index.get("uploads") or []:
            if isinstance(upload, dict):
                _add(upload.get("file_path"), "upload")
    for kind, filename in CANONICAL_OUTPUTS.items():
        # Stage 4 (DATA-004): canonical outputs that are run-scoped
        # (film, reports, run manifest) resolve to the latest run's
        # copy — the index's "latest run" view; shared outputs
        # (captions, narration) resolve to the project path itself.
        _add(
            media_paths.resolve_artefact(project_dir, filename),
            f"output_{kind}",
        )
    return index


# ---------------------------------------------------------------------
# Provenance certificate — generated from the ledger, nothing else
# ---------------------------------------------------------------------

CERTIFICATE_DISCLAIMERS = [
    "This certificate records what the production system recorded. "
    "It is not a legal opinion and not a licence.",
    "The project's rights policy is allow_all: assets whose licence "
    "is recorded as \"unknown\" are NOT cleared for publication — "
    "\"unknown\" means the licence was not determined, and each such "
    "asset must be reviewed before publishing.",
    "Generated media (motion graphics and other synthetic visuals) "
    "are labelled as generated in the decision records; they are not "
    "archival footage and are never certified as such.",
    "Verification outcomes are the pipeline's metadata/transcript "
    "verification verdicts, recorded verbatim. A \"verified\" outcome "
    "means the recorded checks passed; it is not a human review.",
]


def _verification_outcome(verification):
    if not isinstance(verification, dict):
        return None
    if "accepted" in verification:
        return {
            "accepted": bool(verification.get("accepted")),
            "reason": verification.get("rejection_reason")
            or verification.get("reason"),
            "transcript_check": verification.get("transcript_check"),
            # Phase 1 / AM-013: a swap applied despite a rejected
            # fresh verdict carries its override stamp in the
            # ledger's verification JSON; the certificate surfaces
            # it (None for every non-override row).
            "swap_override": verification.get("swap_override"),
        }
    return {"status": verification.get("status")}


def _music_section_from_decisions(decisions):
    """The certificate's music section (Stage H, M7), built from
    the ledger's music decision row — the LAST one recorded for
    the scope the caller selected. No music row means the run
    predates music recording (or its Sound Designer stage never
    recorded): the section says "not_recorded" rather than
    guessing a state the records do not contain."""
    music_rows = [
        d for d in decisions or []
        if isinstance(d, dict) and d.get("decision_type") == "music"
    ]
    if not music_rows:
        return {"state": "not_recorded"}
    row = music_rows[-1]
    rights = row.get("rights") or {}
    verification = row.get("verification") or {}
    asset_key = row.get("asset_key") or ""
    return {
        "state": verification.get("status") or "not_recorded",
        "file_name": Path(asset_key).name if asset_key else None,
        "content_sha256": row.get("content_hash"),
        "source_type": rights.get("source") or row.get("provider"),
        "license": rights.get("license") or "unknown",
        "rights_status": rights.get("rights_status"),
        "decided_by": verification.get("decided_by"),
        "music_volume": verification.get("music_volume"),
    }


def build_certificate(project, run, decisions, counts):
    """Assemble the provenance certificate from ledger records.
    `decisions` are run_ledger.list_decisions rows; `counts` is
    run_ledger.decision_counts for the same run. Claims nothing the
    records do not support."""
    rows = []
    for decision in decisions:
        if decision.get("decision_type") == "music":
            # Music is not a per-sentence visual asset; it is
            # certified in the dedicated "music" section below.
            continue
        rights = decision.get("rights") or {}
        verification = decision.get("verification")
        rows.append(
            {
                "sentence_number": decision.get("sentence_number"),
                "scene_number": decision.get("scene_number"),
                "decision_type": decision.get("decision_type"),
                # Phase 1 / AM-014: the asset's recorded title —
                # carried in the ledger's rights JSON for film
                # assets and in the verification JSON for
                # candidate-class rows (rejected /
                # verified_not_picked / download_failed); None when
                # the row predates the carry (never backfilled).
                "title": (
                    rights.get("title")
                    if isinstance(rights, dict) and rights.get("title")
                    else (
                        verification.get("title")
                        if isinstance(verification, dict)
                        else None
                    )
                ) or None,
                "asset_key": decision.get("asset_key"),
                "content_hash": decision.get("content_hash"),
                "provider": decision.get("provider"),
                "source_url": decision.get("source_url"),
                "interval": {
                    "start": decision.get("interval_start"),
                    "duration": decision.get("interval_duration"),
                },
                "evidence_class": decision.get("evidence_class"),
                # B-roll B0 (§6.1): the tier recorded in the
                # decision's verification JSON (None on pre-B0
                # ledger rows — unstated, never backfilled).
                "tier": (
                    decision.get("verification", {}).get("tier")
                    if isinstance(decision.get("verification"), dict)
                    else None
                ),
                "verification": _verification_outcome(
                    decision.get("verification")
                ),
                "rights": {
                    "license": rights.get("license", "unknown")
                    if isinstance(rights, dict)
                    else "unknown",
                    # Phase 1 / AM-014: the licence evidence URL and
                    # the retrieval timestamp ride with the licence
                    # (None on rows recorded before the carry).
                    "license_url": rights.get("license_url")
                    if isinstance(rights, dict)
                    else None,
                    "rights_status": rights.get("rights_status")
                    if isinstance(rights, dict)
                    else None,
                    "creator": rights.get("creator")
                    if isinstance(rights, dict)
                    else None,
                    "source": rights.get("source")
                    if isinstance(rights, dict)
                    else None,
                    "retrieved_at": rights.get("retrieved_at")
                    if isinstance(rights, dict)
                    else None,
                }
                if decision.get("decision_type")
                in ("selected", "still", "swapped_in", "rotated", "generated")
                else None,
                "previous_decision_id": decision.get("previous_decision_id"),
                "recorded_at": decision.get("created_at"),
            }
        )
    return {
        "certificate_version": 1,
        "project": {
            "id": project.get("id"),
            "title": project.get("title"),
            "category": project.get("category"),
        },

        "run": {
            "run_id": run.get("run_id") if run else None,
            "status": run.get("status") if run else None,
            "script_hash": run.get("script_hash") if run else None,
            "created_at": run.get("created_at") if run else None,
            "finished_at": run.get("finished_at") if run else None,
        },
        "summary": counts,
        "assets": rows,
        # Stage H (M7): what the film's music is, per the ledger —
        # state, track identity, source and licence ("unknown"
        # stays "unknown"; "not_recorded" for pre-Stage-H runs).
        "music": _music_section_from_decisions(decisions),
        "disclaimers": CERTIFICATE_DISCLAIMERS,
    }


# ---------------------------------------------------------------------
# Per-revision provenance certificate (Stage 6 editor)
# ---------------------------------------------------------------------

REVISION_CERTIFICATE_DISCLAIMER = (
    "This certificate covers an EDITED REVISION of the production. "
    "Its decision trail is the original run's decisions plus the "
    "decisions the revision chain appended (each tagged with its "
    "revision id); manual edits never rewrite the original records, "
    "and a manually replaced or uploaded asset carries only the "
    "evidence recorded for it at edit time — editing does not make "
    "an asset verified."
)


def build_revision_certificate(project, run, revision, decisions, counts,
                               final_assets, music=None):
    """Certificate for one editor revision, built on the run
    certificate: the trail is the source run's original decisions
    plus the revision chain's appended decisions (the caller
    selects them via run_ledger.list_decisions_for_revision), and
    `final_assets` is the revision document's own final asset set —
    what the exported revision actually contains, with each item's
    classification, evidence class, verification outcome and rights
    as recorded in the document. The original run certificate is
    unchanged by this builder.

    Stage H (M7/M8): the certificate's "music" section must
    describe the REVISION's music, not the production's — the
    caller passes `music` derived from the revision document (its
    own soundtrack record). When omitted, the section falls back
    to the latest music decision in the selected trail, which for
    an unedited chain is the production's own record."""
    certificate = build_certificate(project, run, decisions, counts)
    if music is not None:
        certificate["music"] = music
    certificate["certificate_version"] = 2
    certificate["revision"] = {
        "revision_id": revision.get("revision_id"),
        "revision_number": revision.get("revision_number"),
        "parent_revision_id": revision.get("parent_revision_id"),
        "chain_revision_ids": revision.get("chain_revision_ids") or [],
        "status": revision.get("status"),
        "source_run_id": run.get("run_id") if run else None,
    }
    certificate["final_revision_assets"] = final_assets
    certificate["disclaimers"] = (
        list(CERTIFICATE_DISCLAIMERS) + [REVISION_CERTIFICATE_DISCLAIMER]
    )
    return certificate
