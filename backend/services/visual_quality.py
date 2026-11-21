#!/usr/bin/env python3
"""Visual-quality measurement from stored production artefacts (Phase 5).

This module is MEASUREMENT ONLY. It reads artefacts a completed
production already left on disk and computes machine-readable
visual-variety metrics. It never runs a production, never calls a
provider, and never modifies the artefacts it reads.

Artefacts consumed (all optional on disk; a missing artefact is
reported as ``null`` / unknown, never invented):

- ``timeline.json``        — scenes -> items (the rendered timeline)
- ``asset_report.json``    — funnel, rejection reasons, missing visuals
- ``rights_manifest.json`` — one entry per accepted asset
- ``motion_graphics/motion_graphics.json`` — targeted-graphics record
- ``run_manifest.json``    — run manifest, if the project has one
  (the two stored E2E baselines predate it in their project dirs, so
  for them this field is reported as absent, not fabricated)

Metric definitions (every number in the output follows these exactly)
----------------------------------------------------------------------
Canonical asset identity:
    ``file_path`` of a timeline item, else its ``path``, else its
    ``source_url`` / ``video_url`` / ``image_url`` / ``url``.
    LIMITATION (also emitted in the output metadata): the stored
    timeline items carry no stable asset id, no content hash and no
    canonicalised URL. Identity is therefore the recorded path/URL
    STRING exactly as written. The same underlying content stored
    under two different paths, or reached via a redirect-wrapper URL
    vs its canonical URL, counts as two assets; identical paths
    always count as one. No content-hash or perceptual dedup is
    performed, because the artefacts do not carry the data for it.

Timeline items:
    Items are taken from ``timeline["scenes"][*]["items"]`` in scene
    order when scenes are present (the top-level ``timeline["items"]``
    list in the stored baselines is the same sequence with extra
    ``project_start``/``project_end`` fields, so using both would
    double-count). If there are no scenes, ``timeline["items"]`` is
    used. ``timeline_item_count`` is the number of items so obtained.

Duration / runtime:
    An item's duration is its numeric ``duration`` field, else
    ``end - start`` when both are numeric, else 0. Total runtime is
    the sum of item durations.

Unique assets / reuse:
    ``unique_canonical_assets_on_timeline`` = number of distinct
    canonical identities among timeline items. Items with no
    identifier at all are each given a distinct synthetic identity
    ``<missing_identifier:scene:item>`` (so they cannot silently
    merge) and are counted in ``missing_identifier_items``.
    Per asset: ``count`` = number of timeline items with that
    identity; ``runtime_seconds`` = sum of their durations;
    ``runtime_share`` = asset runtime / total runtime (0 if total
    runtime is 0). ``max_reuse_count`` / ``max_runtime_share`` are
    the maxima over assets (0 for an empty timeline).
    ``reuse_distribution`` maps a use-count (as a string, JSON keys)
    to the number of assets used exactly that many times.
    ``consecutive_repeat_boundaries`` = number of adjacent item
    pairs (in timeline order) sharing one canonical identity;
    ``max_consecutive_run`` = length of the longest run of one
    identity (0 for an empty timeline, 1 when no repeats).

Moving / still / graphics runtime share (PRIMARY, asset-kind):
    Each item is classified by what the asset IS, in this precedence:
      1. graphics — ``source == "cinemora_motion_graphics"``, or
         ``type`` in {"motion_graphic", "graphic"}, or the canonical
         identity contains ``/motion_graphics/``, or its basename
         starts with ``motion_graphic_``;
      2. moving — ``type == "video"``;
      3. still — ``type == "image"``;
      4. unknown — anything else (reported, never reclassified).
    Seconds and fraction (= seconds / total runtime) are reported
    per class. A motion-graphic FILE reused by rotation therefore
    counts as graphics in this primary view.
    ALTERNATIVE (for reconciliation with
    VISUAL_VARIETY_INVESTIGATION_2026-11-20.md, which classified by
    ASSIGNMENT): ``runtime_share_assignment_method`` classifies an
    item as graphics iff ``assignment == "motion_graphic"``, else
    moving iff ``type == "video"``, else still iff
    ``type == "image"``, else unknown. Under that method the two
    rotation reuses of Berlin's schematic file count as moving
    footage; under the primary method they count as graphics. Both
    views are emitted so the difference is explicit, not hidden.

Assignment breakdown:
    Count, runtime and runtime share grouped by the item's
    ``assignment`` string; a missing/empty assignment is grouped
    under ``"unknown"``.

Uncovered beats:
    Primary source: ``asset_report["missing_visuals"]``.
    ``count`` = ``len(missing_visuals)`` when that list is present,
    else ``asset_report["missing_visual_count"]`` when that is an
    int, else the length of ``timeline.coverage.uncovered_sentences``
    when present, else ``null`` (unknown — never invented).
    ``source_field`` names which source produced ``count``.
    The report's own ``missing_visual_count`` field and the
    timeline-coverage uncovered list are ALSO emitted separately,
    because they measure different things in the stored baselines:
    timeline coverage counts rotation-filled sentences as covered
    (0 uncovered in both baselines) while ``missing_visuals``
    records the sentences that never got a verified asset
    (Berlin 14, Kohberger 18).

Rejection-reason distribution:
    Primary source: ``asset_report["rejection_reasons"]`` when it is
    a non-empty dict — emitted verbatim with its sum as ``total``.
    Fallback: counts of ``reason`` over
    ``asset_report["candidate_records"]["records"]`` entries whose
    ``outcome == "rejected"`` and whose reason is a non-empty string.
    If neither source exists: ``distribution`` is ``null``,
    ``total`` is ``null`` and ``unknown`` is ``true``. Reasons are
    NEVER invented, merged or renamed.

Genre expectations (Stage 6 / C.G.2):
    ``genre_expectations`` evaluates the asset report's genre
    pack ``expectations`` data against this measurement's share
    vector (moving / still / document / graphic runtime shares and
    the uncovered sentence share). Every field carries its basis
    label; an uncalibrated expectation (range null in the pack)
    evaluates to ``UNKNOWN`` — never a pass/fail — and the block
    is report-only. No named pack -> ``state: "not_recorded"``.

Segment reuse (Stage 7 / R9, Architecture Review C.F.3):
    ``segment_reuse`` measures reuse at SEGMENT identity alongside
    the per-file numbers above (which are unchanged). A video
    item's segment is its stamped ``segment_id`` when present, else
    the identity reconstructed from its recorded source window
    (canonical source key + ``clip_start`` + duration — the
    reconstruction C.F.3 specifies for stored timelines, which
    predate the stamp); a still's segment is the whole-asset
    identity of its canonical source. Items with no derivable
    segment identity (graphics, identity-less records) are counted
    in ``items_without_segment_identity`` and excluded from the
    per-segment counts. ``segments_reused`` counts segments with
    more than one item; ``exact_segment_reuse_items`` counts the
    items beyond the first for those segments — exact-segment reuse,
    reported separately from source reuse
    (``distinct_segments_per_source`` shows how many distinct
    segments of one source the timeline used). This block is the
    measurement the timeline screen-time cap was rebound against at
    Stage 7: per-segment uses here are directly comparable to the
    cap value, per-file uses above are not.

B-roll tier accounting (Intelligent B-Roll B0, plan §6.1/§6.2):
    ``tier_accounting`` groups timeline items by their stamped
    ``tier`` (direct / related / activity_broll / atmospheric /
    graphic): count, runtime seconds and runtime share per tier,
    plus ``broll_share`` — the (activity_broll + atmospheric)
    runtime share of the whole timeline. Items without a stamp are
    counted in ``unstamped_items`` and excluded from the shares; a
    timeline with no stamped items at all reports
    ``state: "not_recorded"`` and a null ``broll_share`` (unknown,
    never zero). Measurement only — the founder-set band comparison
    is B3.

Sentence outcomes — profile buckets (Stage 7 / R7, C.B.3):
    A sentence whose direct ("sentence" assignment) item carries a
    stamped ``verification_category`` of contextual, environmental
    or atmosphere counts into ``direct_contextual_profile`` /
    ``direct_environmental_profile`` / ``direct_atmosphere_profile``
    instead of ``direct`` — an acceptance under a non-evidence
    profile is never merged into the evidence-grade ``direct``
    bucket, nor into ``contextual`` (which means rotation /
    not-verified-for-this-sentence). Items without the stamp
    (stored timelines) count as ``direct`` exactly as before.
    ``direct_assignment_share`` counts all four direct buckets.

Source / provider distribution:
    - ``timeline_source_distribution``: per timeline item ``source``
      (missing/empty -> ``"unknown"``): item count, runtime seconds,
      runtime share, share of timeline items.
    - ``accepted_source_distribution``: per rights-manifest entry
      ``source`` (missing/empty -> ``"unknown"``): accepted-asset
      count and share of accepted assets; plus
      ``accepted_type_distribution`` (count per manifest ``type``).
    - ``provider_funnel_distribution``: verbatim per-provider
      ``discovered/verified/rejected/downloaded/download_failed``
      counts from ``asset_report["providers"]["funnel"]`` when
      present, with summed totals; ``null`` when absent.
    - ``candidate_records_summary``: ``total``, ``by_outcome`` and
      ``by_stage`` verbatim from ``asset_report["candidate_records"]``
      when present; ``null`` when absent.

Targeted graphics:
    ``targets_requested`` from
    ``motion_graphics.targets.requested`` (dict form) or
    ``len(motion_graphics.targets)`` (list form), else ``null``.
    ``generated_assets`` = ``len(motion_graphics.assets)`` when the
    list is present, else ``null``.
    ``targeted_graphics_on_timeline`` = number of timeline items
    with ``assignment == "motion_graphic"`` whose canonical identity
    is one of the generated motion-graphics file paths.
    ``motion_graphic_assignment_items`` = all timeline items with
    ``assignment == "motion_graphic"``, regardless of file match.

CLI:
    ``python -m services.visual_quality <project_dir> [--output PATH]``
    prints the JSON measurement (and writes it to PATH if given).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from services import media_paths
from services import rejection_diagnostics
from services import segment_index
from services.atomic_write import write_text_atomic

GRAPHICS_SOURCE = "cinemora_motion_graphics"
UNKNOWN = "unknown"


def _load_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _item_duration(item: dict) -> float:
    if _is_number(item.get("duration")):
        return float(item["duration"])
    if _is_number(item.get("end")) and _is_number(item.get("start")):
        return float(item["end"]) - float(item["start"])
    return 0.0


def canonical_asset_id(item: dict, fallback: str) -> str:
    """Canonical identity of one timeline item (see module docstring)."""
    for key in ("file_path", "path", "source_url", "video_url", "image_url", "url"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return fallback


def timeline_items(timeline: dict) -> list:
    """Flatten timeline items without double-counting (see docstring)."""
    if not isinstance(timeline, dict):
        return []
    scenes = timeline.get("scenes")
    items: list = []
    if isinstance(scenes, list) and scenes:
        for scene in scenes:
            if isinstance(scene, dict):
                for item in scene.get("items") or []:
                    if isinstance(item, dict):
                        items.append(item)
        if items:
            return items
    raw = timeline.get("items")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def classify_asset_kind(item: dict, canonical_id: str) -> str:
    """Primary (asset-kind) classification: graphics > moving > still."""
    source = item.get("source")
    itype = item.get("type")
    basename = canonical_id.rsplit("/", 1)[-1]
    if (
        source == GRAPHICS_SOURCE
        or itype in ("motion_graphic", "graphic")
        or "/motion_graphics/" in canonical_id
        or basename.startswith("motion_graphic_")
    ):
        return "graphics"
    if itype == "video":
        return "moving"
    if itype == "image":
        return "still"
    return UNKNOWN


def classify_assignment_method(item: dict) -> str:
    """Alternative classification used by the 2026-11-20 investigation."""
    if item.get("assignment") == "motion_graphic":
        return "graphics"
    if item.get("type") == "video":
        return "moving"
    if item.get("type") == "image":
        return "still"
    return UNKNOWN


def _share(part: float, total: float) -> float:
    return (part / total) if total else 0.0


def _runtime_block(totals: dict, total_runtime: float) -> dict:
    return {
        kind: {
            "seconds": round(seconds, 6),
            "fraction": _share(seconds, total_runtime),
        }
        for kind, seconds in sorted(totals.items())
    }


def _rejection_reasons(report: dict) -> dict:
    reasons = report.get("rejection_reasons")
    if isinstance(reasons, dict) and reasons:
        distribution = {
            str(k): int(v) for k, v in reasons.items() if _is_number(v)
        }
        return {
            "source": "asset_report.rejection_reasons",
            "distribution": distribution,
            "total": sum(distribution.values()),
            "unknown": False,
        }
    records_container = report.get("candidate_records")
    records = (
        records_container.get("records")
        if isinstance(records_container, dict)
        else None
    )
    if isinstance(records, list) and records:
        counter: Counter = Counter()
        for record in records:
            if not isinstance(record, dict):
                continue
            if record.get("outcome") != "rejected":
                continue
            reason = record.get("reason")
            if isinstance(reason, str) and reason:
                counter[reason] += 1
        if counter:
            return {
                "source": "asset_report.candidate_records.records[rejected].reason",
                "distribution": dict(sorted(counter.items())),
                "total": sum(counter.values()),
                "unknown": False,
            }
    return {
        "source": None,
        "distribution": None,
        "total": None,
        "unknown": True,
    }


def _genre_expectations_block(
    report: dict,
    kind_totals: dict,
    total_runtime: float,
    runtime_share_by_format: dict,
    sentence_outcomes: dict,
) -> dict:
    """Stage 6 / R6 (Architecture Review C.G.2): the run's genre
    expectations, evaluated against this measurement.

    The genre pack is data; its expectations carry ranges only
    after R6 calibration, and every field evaluation carries its
    basis label. An uncalibrated expectation evaluates to UNKNOWN —
    never to a default number — and the block is report-only (it
    feeds no verdict). When the asset report names no pack, the
    block is an explicit not_recorded state."""
    pack_ref = report.get("genre_pack") if isinstance(report, dict) else None
    pack_id = pack_ref.get("id") if isinstance(pack_ref, dict) else None
    if not pack_id:
        return {
            "state": "not_recorded",
            "note": (
                "The asset report names no genre pack; genre "
                "expectations are not evaluated for this production."
            ),
        }
    from services import calibration, genre_packs

    pack = genre_packs.load_pack(str(pack_id))
    if pack is None:
        return {
            "state": "not_recorded",
            "genre_pack": str(pack_id),
            "note": "The named genre pack does not exist; expectations are not evaluated.",
        }

    def _kind_share(kind):
        if not total_runtime:
            return None
        return _share(kind_totals.get(kind, 0.0), total_runtime)

    document_share = None
    if runtime_share_by_format.get("state") == "recorded":
        document_share = (
            (runtime_share_by_format.get("by_format") or {})
            .get("document", {})
            .get("runtime_share")
        )
    measurements = {
        "moving_share": _kind_share("moving"),
        "still_share": _kind_share("still"),
        "document_share": document_share,
        "graphic_share": _kind_share("graphics"),
        "uncovered_share": (sentence_outcomes.get("shares") or {}).get(
            "uncovered"
        ),
    }
    return {
        "state": "recorded",
        "genre_pack": str(pack_id),
        "fields": calibration.evaluate_expectations(pack, measurements),
    }


def _uncovered_beats(report: dict, timeline: dict) -> dict:
    missing = report.get("missing_visuals")
    missing_field = report.get("missing_visual_count")
    coverage = timeline.get("coverage") if isinstance(timeline, dict) else None
    coverage_uncovered = (
        coverage.get("uncovered_sentences") if isinstance(coverage, dict) else None
    )
    sentence_numbers = None
    if isinstance(missing, list):
        count: object = len(missing)
        source_field: object = "asset_report.missing_visuals"
        sentence_numbers = [
            entry.get("sentence_number")
            for entry in missing
            if isinstance(entry, dict) and entry.get("sentence_number") is not None
        ]
    elif isinstance(missing_field, int) and not isinstance(missing_field, bool):
        count = missing_field
        source_field = "asset_report.missing_visual_count"
    elif isinstance(coverage_uncovered, list):
        count = len(coverage_uncovered)
        source_field = "timeline.coverage.uncovered_sentences"
    else:
        count = None
        source_field = None
    return {
        "count": count,
        "source_field": source_field,
        "missing_visual_count_field": missing_field
        if isinstance(missing_field, int) and not isinstance(missing_field, bool)
        else None,
        "missing_visuals_list_length": len(missing) if isinstance(missing, list) else None,
        "sentence_numbers": sentence_numbers,
        "timeline_coverage_uncovered_sentences": len(coverage_uncovered)
        if isinstance(coverage_uncovered, list)
        else None,
    }


def measure_visual_quality(
    *,
    timeline: dict,
    asset_report: dict,
    rights_manifest: list,
    motion_graphics: dict | None = None,
    run_manifest_present: bool | None = None,
    project_id: str | None = None,
    project_dir: str | None = None,
) -> dict:
    """Compute the visual-quality measurement from parsed artefacts.

    Pure function: all inputs are already-parsed JSON values. See the
    module docstring for the exact definition of every metric.
    """
    timeline = timeline if isinstance(timeline, dict) else {}
    report = asset_report if isinstance(asset_report, dict) else {}
    manifest = (
        [e for e in rights_manifest if isinstance(e, dict)]
        if isinstance(rights_manifest, list)
        else []
    )
    mg = motion_graphics if isinstance(motion_graphics, dict) else {}

    items = timeline_items(timeline)
    durations = [_item_duration(item) for item in items]
    total_runtime = sum(durations)

    canonical_ids: list[str] = []
    missing_identifier_items = 0
    for position, item in enumerate(items):
        fallback = (
            f"<missing_identifier:{item.get('scene_number')}:{position}>"
        )
        cid = canonical_asset_id(item, fallback)
        if cid == fallback:
            missing_identifier_items += 1
        canonical_ids.append(cid)

    per_asset: dict[str, dict] = {}
    for cid, duration in zip(canonical_ids, durations):
        entry = per_asset.setdefault(cid, {"count": 0, "runtime_seconds": 0.0})
        entry["count"] += 1
        entry["runtime_seconds"] += duration
    reuse = [
        {
            "canonical_asset_id": cid,
            "count": entry["count"],
            "runtime_seconds": round(entry["runtime_seconds"], 6),
            "runtime_share": _share(entry["runtime_seconds"], total_runtime),
        }
        for cid, entry in sorted(
            per_asset.items(), key=lambda kv: (-kv[1]["count"], kv[0])
        )
    ]
    reuse_distribution = Counter(e["count"] for e in per_asset.values())

    # Stage 7 / R9 (C.F.3): the same timeline measured at SEGMENT
    # identity — the measurement the timeline cap was rebound
    # against. Stamped segment_ids win; stored items are
    # reconstructed from their recorded source windows.
    per_segment: dict[str, dict] = {}
    segments_per_source: dict[str, set] = {}
    items_without_segment_identity = 0
    for item, duration in zip(items, durations):
        seg_id = segment_index.segment_id_for_item(item)
        if not seg_id:
            items_without_segment_identity += 1
            continue
        entry = per_segment.setdefault(
            seg_id, {"count": 0, "runtime_seconds": 0.0}
        )
        entry["count"] += 1
        entry["runtime_seconds"] += duration
        source_key = segment_index.source_key_for(item)
        if source_key:
            segments_per_source.setdefault(source_key, set()).add(seg_id)
    segment_reuse = {
        "state": "recorded",
        "items_with_segment_identity": sum(
            e["count"] for e in per_segment.values()
        ),
        "items_without_segment_identity": items_without_segment_identity,
        "unique_segments": len(per_segment),
        "max_segment_uses": max(
            (e["count"] for e in per_segment.values()), default=0
        ),
        "segments_reused": sum(
            1 for e in per_segment.values() if e["count"] > 1
        ),
        "exact_segment_reuse_items": sum(
            e["count"] - 1 for e in per_segment.values() if e["count"] > 1
        ),
        "reuse_per_segment": [
            {
                "segment_id": seg_id,
                "count": entry["count"],
                "runtime_seconds": round(entry["runtime_seconds"], 6),
                "runtime_share": _share(
                    entry["runtime_seconds"], total_runtime
                ),
            }
            for seg_id, entry in sorted(
                per_segment.items(), key=lambda kv: (-kv[1]["count"], kv[0])
            )
        ],
        "distinct_segments_per_source": {
            key: len(value)
            for key, value in sorted(segments_per_source.items())
        },
    }

    consecutive_repeats = 0
    max_run = 0
    current_run = 0
    previous = None
    for cid in canonical_ids:
        if cid == previous:
            consecutive_repeats += 1
            current_run += 1
        else:
            current_run = 1
            previous = cid
        max_run = max(max_run, current_run)

    kind_totals: Counter = Counter()
    assignment_method_totals = Counter()
    assignment_counts = Counter()
    assignment_runtime = Counter()
    source_counts = Counter()
    source_runtime = Counter()
    for item, duration, cid in zip(items, durations, canonical_ids):
        kind_totals[classify_asset_kind(item, cid)] += duration
        assignment_method_totals[classify_assignment_method(item)] += duration
        assignment = item.get("assignment")
        assignment_key = assignment if isinstance(assignment, str) and assignment else UNKNOWN
        assignment_counts[assignment_key] += 1
        assignment_runtime[assignment_key] += duration
        source = item.get("source")
        source_key = source if isinstance(source, str) and source else UNKNOWN
        source_counts[source_key] += 1
        source_runtime[source_key] += duration
    for kind in ("moving", "still", "graphics", UNKNOWN):
        kind_totals.setdefault(kind, 0.0)
        assignment_method_totals.setdefault(kind, 0.0)

    # Stage 1 / §2.6: FORMAT runtime accounting — an orthogonal
    # dimension to the kind totals above (whose definitions are
    # frozen). Timeline items are joined by file path to the format
    # each accepted asset was classified as in the asset report
    # (footage / photograph / map / document / diagram / graphic /
    # text_card / uncertain), so moving footage, stills and graphics
    # are accounted separately and a still is never counted as
    # footage. Reports produced before accepted_asset_formats existed
    # yield an honest not_recorded state, never a fabricated split.
    _formats = report.get("accepted_asset_formats")
    if isinstance(_formats, list) and _formats:
        _format_by_path = {
            str(e.get("file_path")): str(e.get("visual_format") or UNKNOWN)
            for e in _formats
            if isinstance(e, dict) and e.get("file_path")
        }
        _format_runtime: Counter = Counter()
        _format_counts: Counter = Counter()
        _joined = 0
        for _item, _duration in zip(items, durations):
            _path = str(_item.get("file_path") or _item.get("path") or "")
            _fmt = _format_by_path.get(_path)
            if _fmt is None:
                _fmt = UNKNOWN
            else:
                _joined += 1
            _format_runtime[_fmt] += _duration
            _format_counts[_fmt] += 1
        runtime_share_by_format = {
            "state": "recorded",
            "source": "asset_report.accepted_asset_formats joined on file_path",
            "joined_items": _joined,
            "total_items": len(items),
            "by_format": {
                fmt: {
                    "count": _format_counts[fmt],
                    "runtime_seconds": round(_format_runtime[fmt], 6),
                    "runtime_share": _share(_format_runtime[fmt], total_runtime),
                }
                for fmt in sorted(_format_counts)
            },
            "moving_footage_runtime_share": _share(
                _format_runtime.get("footage", 0.0), total_runtime
            ),
        }
    else:
        runtime_share_by_format = {
            "state": "not_recorded",
            "source": "asset_report.accepted_asset_formats",
            "note": (
                "This production's asset report does not expose "
                "per-asset formats; the format runtime split is "
                "unknown, not zero."
            ),
            "by_format": {},
            "moving_footage_runtime_share": None,
        }

    accepted_source_counts = Counter()
    accepted_type_counts = Counter()
    for entry in manifest:
        source = entry.get("source")
        accepted_source_counts[
            source if isinstance(source, str) and source else UNKNOWN
        ] += 1
        etype = entry.get("type")
        accepted_type_counts[
            etype if isinstance(etype, str) and etype else UNKNOWN
        ] += 1
    accepted_total = len(manifest)

    funnel = ((report.get("providers") or {}).get("funnel") or None) if isinstance(
        report.get("providers"), dict
    ) else None
    provider_funnel = None
    if isinstance(funnel, dict) and funnel:
        per_provider = {}
        totals = Counter()
        for provider, counts in sorted(funnel.items()):
            if not isinstance(counts, dict):
                continue
            cleaned = {}
            for field in (
                "discovered",
                "verified",
                "rejected",
                "downloaded",
                "download_failed",
            ):
                if _is_number(counts.get(field)):
                    cleaned[field] = int(counts[field])
                    totals[field] += int(counts[field])
            per_provider[str(provider)] = cleaned
        provider_funnel = {
            "source": "asset_report.providers.funnel",
            "provider_count": len(per_provider),
            "totals": dict(sorted(totals.items())),
            "per_provider": per_provider,
        }

    candidate_records = report.get("candidate_records")
    candidate_summary = None
    if isinstance(candidate_records, dict) and candidate_records:
        candidate_summary = {
            "source": "asset_report.candidate_records",
            "total": candidate_records.get("total"),
            "by_outcome": candidate_records.get("by_outcome"),
            "by_stage": candidate_records.get("by_stage"),
        }

    mg_assets = mg.get("assets") if isinstance(mg.get("assets"), list) else None
    generated_files = set()
    if mg_assets:
        for asset in mg_assets:
            if isinstance(asset, dict):
                for key in ("file_path", "path"):
                    if isinstance(asset.get(key), str) and asset[key]:
                        generated_files.add(asset[key])
    targets = mg.get("targets")
    if isinstance(targets, dict):
        targets_requested: object = (
            int(targets["requested"])
            if _is_number(targets.get("requested"))
            else None
        )
    elif isinstance(targets, list):
        targets_requested = len(targets)
    else:
        targets_requested = None
    mg_assignment_items = [
        item for item in items if item.get("assignment") == "motion_graphic"
    ]
    targeted_on_timeline = sum(
        1
        for item, cid in zip(items, canonical_ids)
        if item.get("assignment") == "motion_graphic" and cid in generated_files
    )

    # ---- Phase 3: diversity, sentence outcomes, relevance, integrity,
    # provider failures (reporting only; no existing field changes) ----
    uncovered_block = _uncovered_beats(report, timeline)
    rejection_block = _rejection_reasons(report)

    def _sentence_number_of(item):
        value = item.get("sentence_number")
        if _is_number(value):
            return int(value)
        index = item.get("sentence_index")
        if _is_number(index):
            return int(index) + 1
        return None

    item_sentence_numbers = [_sentence_number_of(item) for item in items]

    runtime_minutes = (total_runtime / 60.0) if total_runtime else 0.0
    unique_count = len(per_asset)
    seen_ids: set = set()
    repeated_items = 0
    for cid in canonical_ids:
        if cid in seen_ids:
            repeated_items += 1
        else:
            seen_ids.add(cid)
    item_count = len(items)
    diversity = {
        "d1_unique_assets": unique_count,
        "d1_unique_assets_per_minute": (
            unique_count / runtime_minutes if runtime_minutes else None
        ),
        "d1_counting_rule": (
            "timeline counting rule — distinct canonical identities among "
            "timeline items (the canonical D1 rule; supersedes the metrics "
            "document's asset-report arithmetic which counted looped items "
            "differently)"
        ),
        "d2_repeated_items": repeated_items,
        "d2_repeated_item_rate": (
            repeated_items / item_count if item_count else None
        ),
        "items_per_minute": (
            item_count / runtime_minutes if runtime_minutes else None
        ),
        "variety_ratio": (unique_count / item_count if item_count else None),
    }

    # Sentence count N: asset_report coverage first, then the
    # timeline's own sentence_count field, then the max seen.
    coverage_report = report.get("coverage")
    n_value = None
    if isinstance(coverage_report, dict) and _is_number(
        coverage_report.get("total_sentences")
    ):
        n_value = int(coverage_report["total_sentences"])
    elif _is_number(timeline.get("sentence_count")):
        n_value = int(timeline["sentence_count"])
    seen_numbers = [n for n in item_sentence_numbers if n is not None]
    missing_list = report.get("missing_visuals")
    if isinstance(missing_list, list):
        for entry in missing_list:
            if isinstance(entry, dict) and _is_number(entry.get("sentence_number")):
                seen_numbers.append(int(entry["sentence_number"]))
    max_seen = max(seen_numbers) if seen_numbers else 0
    if n_value is None:
        n_value = max_seen
    elif max_seen > n_value:
        n_value = max_seen
    sentence_count_n = int(n_value)

    items_by_sentence: dict = {}
    for item, number in zip(items, item_sentence_numbers):
        if number is not None:
            items_by_sentence.setdefault(number, []).append(item)
    buckets = {
        "direct": 0,
        "direct_contextual_profile": 0,
        "direct_environmental_profile": 0,
        "direct_atmosphere_profile": 0,
        "approved_graphic": 0,
        "contextual": 0,
        "editorial_holdover": 0,
        "uncovered": 0,
    }
    per_sentence = []
    for number in range(1, sentence_count_n + 1):
        group = items_by_sentence.get(number, [])
        assignments = {i.get("assignment") for i in group}
        if "sentence" in assignments:
            outcome = "direct"
            # Stage 7 / R7 (C.B.3): a direct item stamped with a
            # non-evidence verification category counts into its own
            # profile bucket — never merged into "direct", never
            # into "contextual" (rotation). Unstamped items (stored
            # timelines) stay "direct", exactly as before.
            for _item in group:
                if _item.get("assignment") != "sentence":
                    continue
                _category = _item.get("verification_category")
                if _category in (
                    "contextual", "environmental", "atmosphere",
                ):
                    outcome = f"direct_{_category}_profile"
                    break
        elif "motion_graphic" in assignments:
            outcome = "approved_graphic"
        elif "holdover" in assignments or any(i.get("holdover") for i in group):
            outcome = "editorial_holdover"
        elif "rotation" in assignments:
            outcome = "contextual"
        else:
            outcome = "uncovered"
        buckets[outcome] += 1
        per_sentence.append({"sentence_number": number, "outcome": outcome})
    sentence_outcomes = {
        "sentence_count": sentence_count_n,
        "buckets": buckets,
        "shares": {
            key: (value / sentence_count_n if sentence_count_n else None)
            for key, value in buckets.items()
        },
        "reconciles": sum(buckets.values()) == sentence_count_n,
        "sentences_without_verified_visual": uncovered_block.get("count"),
        "per_sentence": per_sentence,
    }

    relevance = {
        "direct_assignment_share": (
            (
                buckets["direct"]
                + buckets["direct_contextual_profile"]
                + buckets["direct_environmental_profile"]
                + buckets["direct_atmosphere_profile"]
            ) / sentence_count_n
            if sentence_count_n
            else None
        ),
        "pipeline_sentence_assignment_accuracy": report.get(
            "sentence_assignment_accuracy"
        ),
        "human_adjudicated_relevance": {
            "value": None,
            "state": "unknown",
            "basis": "PROVISIONAL",
            "note": (
                "No labelled dataset exists; human relevance is never "
                "inferred from pipeline scores."
            ),
        },
    }

    suspect_numbers = []
    suspect_count = 0
    for item, number in zip(items, item_sentence_numbers):
        status = item.get("relevance_status")
        is_suspect = item.get("assignment") == "rotation" or (
            isinstance(status, str)
            and bool(status)
            and status not in ("verified_for_sentence", "generated_graphic")
        )
        if is_suspect:
            suspect_count += 1
            if number is not None:
                suspect_numbers.append(number)
    rejection_distribution = rejection_block.get("distribution") or {}
    contradiction_rejections = {
        str(k): int(v)
        for k, v in rejection_distribution.items()
        if str(k).startswith("contradictory_")
    }
    integrity_categories = {
        "confirmed_contradiction": {
            "count": None,
            "state": "unknown",
            "note": (
                "No frame-level or human confirmation source is recorded "
                "in the stored artefacts; a metadata label is not a "
                "confirmed contradiction."
            ),
        },
        "metadata_suspected_mismatch": {
            "count": suspect_count,
            "state": "recorded",
            "basis": "metadata",
            "sentence_numbers": sorted(suspect_numbers),
        },
        "human_reviewed_metadata_classification": {
            "count": None,
            "state": "unknown",
            "note": "No human review classification is recorded.",
        },
        "frame_level_confirmation": {
            "count": None,
            "state": "not_performed",
            "note": (
                "VA-01 excluded by founder order; metadata labels are "
                "never frame proof."
            ),
        },
        "unreviewed": {"count": sentence_count_n, "state": "unreviewed"},
        "contradiction_rejections_recorded": contradiction_rejections,
    }

    if "discovery_failures" not in report:
        provider_failures = {
            "count": None,
            "state": "unknown",
            "note": (
                "discovery_failures is absent from the asset report; "
                "the collection state is unknown, not zero."
            ),
        }
    elif report.get("discovery_failures_state") == "recorded":
        failures_list = report.get("discovery_failures")
        provider_failures = {
            "count": len(failures_list) if isinstance(failures_list, list) else 0,
            "state": "recorded",
            "note": "Provider failures were collected for this production.",
        }
    else:
        failures_list = report.get("discovery_failures")
        provider_failures = {
            "count": len(failures_list) if isinstance(failures_list, list) else 0,
            "state": "unknown",
            "note": (
                "Legacy stored report: discovery_failures is present "
                "without discovery_failures_state, so an empty list "
                "cannot be distinguished from not-collected."
            ),
        }

    # B-roll tier accounting (B0, plan §6.1/§6.2): items and runtime
    # per tier from the timeline items' own tier stamps, and the
    # B-roll share — (activity_broll + atmospheric runtime) / total
    # runtime. A stored timeline whose items carry no stamps yields
    # state "not_recorded" and a null share: unknown, not zero. The
    # founder-set band comparison is B3; this block only measures.
    _tier_counts: Counter = Counter()
    _tier_runtime: Counter = Counter()
    _unstamped_items = 0
    for _item, _duration in zip(items, durations):
        _tier = _item.get("tier")
        if _tier not in rejection_diagnostics.TIERS:
            _unstamped_items += 1
            continue
        _tier_counts[_tier] += 1
        _tier_runtime[_tier] += _duration
    _broll_runtime = sum(
        _tier_runtime.get(tier, 0.0)
        for tier in rejection_diagnostics.BROLL_TIERS
    )
    tier_accounting = {
        "state": "recorded" if _tier_counts else "not_recorded",
        "by_tier": {
            tier: {
                "count": _tier_counts[tier],
                "runtime_seconds": round(_tier_runtime[tier], 6),
                "runtime_share": _share(_tier_runtime[tier], total_runtime),
            }
            for tier in rejection_diagnostics.TIERS
            if _tier_counts.get(tier)
        },
        "broll_runtime_seconds": round(_broll_runtime, 6),
        "broll_share": (
            _share(_broll_runtime, total_runtime) if _tier_counts else None
        ),
        "unstamped_items": _unstamped_items,
    }

    return {
        "schema": "cinemora.visual_quality/1",
        "project_id": project_id
        or (timeline.get("project_id") if isinstance(timeline, dict) else None),
        "project_dir": project_dir,
        "metadata": {
            "measurement_only": True,
            "canonical_identity": (
                "timeline item file_path, else path, else "
                "source_url/video_url/image_url/url (exact recorded string)"
            ),
            "canonical_identity_limitation": (
                "Stored timeline items carry no stable asset id and no "
                "content hash; identity is the recorded path/URL string "
                "only. The same content under two different paths/URLs "
                "counts as two assets, and no content-hash or perceptual "
                "dedup is performed."
            ),
            "artefacts": {
                "timeline_present": bool(timeline),
                "asset_report_present": bool(report),
                "rights_manifest_entries": accepted_total,
                "motion_graphics_present": bool(mg),
                "run_manifest_present": run_manifest_present,
            },
            "primary_runtime_classification": (
                "asset-kind (graphics file counts as graphics even when "
                "reused by rotation); see "
                "runtime_share_assignment_method for the investigation's "
                "assignment-based alternative"
            ),
        },
        "timeline": {
            "timeline_item_count": len(items),
            "total_runtime_seconds": round(total_runtime, 6),
            "duration_seconds_field": timeline.get("duration_seconds"),
            "sentence_count_field": timeline.get("sentence_count"),
            "unique_canonical_assets_on_timeline": len(per_asset),
            "missing_identifier_items": missing_identifier_items,
            "max_reuse_count": max((e["count"] for e in per_asset.values()), default=0),
            "max_runtime_share": max(
                (_share(e["runtime_seconds"], total_runtime) for e in per_asset.values()),
                default=0.0,
            ),
            "assets_used_once": sum(1 for e in per_asset.values() if e["count"] == 1),
            "assets_reused": sum(1 for e in per_asset.values() if e["count"] > 1),
            "reuse_distribution": {
                str(k): reuse_distribution[k] for k in sorted(reuse_distribution)
            },
            "consecutive_repeat_boundaries": consecutive_repeats,
            "max_consecutive_run": max_run,
            "reuse_per_asset": reuse,
            "segment_reuse": segment_reuse,
        },
        "runtime_share": _runtime_block(dict(kind_totals), total_runtime),
        "runtime_share_by_format": runtime_share_by_format,
        "tier_accounting": tier_accounting,
        "runtime_share_assignment_method": _runtime_block(
            dict(assignment_method_totals), total_runtime
        ),
        "assignment_breakdown": {
            key: {
                "count": assignment_counts[key],
                "runtime_seconds": round(assignment_runtime[key], 6),
                "runtime_share": _share(assignment_runtime[key], total_runtime),
                "item_share": _share(float(assignment_counts[key]), float(len(items))),
            }
            for key in sorted(assignment_counts)
        },
        "uncovered_beats": uncovered_block,
        "rejection_reasons": rejection_block,
        "diversity": diversity,
        "sentence_outcomes": sentence_outcomes,
        "relevance": relevance,
        "integrity_categories": integrity_categories,
        "provider_failures": provider_failures,
        "source_distribution": {
            "timeline": {
                key: {
                    "count": source_counts[key],
                    "runtime_seconds": round(source_runtime[key], 6),
                    "runtime_share": _share(source_runtime[key], total_runtime),
                    "item_share": _share(float(source_counts[key]), float(len(items))),
                }
                for key in sorted(source_counts)
            },
            "accepted_manifest": {
                key: {
                    "count": accepted_source_counts[key],
                    "share_of_accepted": _share(
                        float(accepted_source_counts[key]), float(accepted_total)
                    ),
                }
                for key in sorted(accepted_source_counts)
            },
            "accepted_type_distribution": {
                key: accepted_type_counts[key]
                for key in sorted(accepted_type_counts)
            },
            "provider_funnel": provider_funnel,
            "candidate_records": candidate_summary,
        },
        "targeted_graphics": {
            "targets_requested": targets_requested,
            "generated_assets": len(mg_assets) if mg_assets is not None else None,
            "motion_graphic_assignment_items": len(mg_assignment_items),
            "targeted_graphics_on_timeline": targeted_on_timeline
            if mg_assets is not None
            else None,
            "report_generated_graphics": report.get("generated_graphics"),
        },
        "report_fields": {
            "total_accepted_assets": report.get("total_accepted_assets"),
            "total_rejected_assets": report.get("total_rejected_assets"),
            "total_retrieved_assets": report.get("total_retrieved_assets"),
            "sentence_assignment_accuracy": report.get(
                "sentence_assignment_accuracy"
            ),
            "rotation_assignments": report.get("rotation_assignments"),
        },
        # Stage 6 / C.G.2: genre expectations vs this measurement
        # (report-only; uncalibrated expectations read UNKNOWN).
        "genre_expectations": _genre_expectations_block(
            report,
            dict(kind_totals),
            total_runtime,
            runtime_share_by_format,
            sentence_outcomes,
        ),
    }


def measure_project(project_dir) -> dict:
    """Load a project's stored artefacts from disk and measure them."""
    project_dir = Path(project_dir)
    # Stage 4 (DATA-004): derived artefacts are run-scoped for new
    # runs — resolve the freshest run copy, legacy project-level
    # files (e.g. the stored Berlin/Kohberger projects) as fallback.
    timeline = _load_json(
        media_paths.resolve_artefact(project_dir, "timeline.json"), {}
    )
    report = _load_json(
        media_paths.resolve_artefact(project_dir, "asset_report.json"), {}
    )
    manifest = _load_json(
        media_paths.resolve_artefact(project_dir, "rights_manifest.json"), []
    )
    mg = _load_json(project_dir / "motion_graphics" / "motion_graphics.json", {})
    return measure_visual_quality(
        timeline=timeline if isinstance(timeline, dict) else {},
        asset_report=report if isinstance(report, dict) else {},
        rights_manifest=manifest if isinstance(manifest, list) else [],
        motion_graphics=mg if isinstance(mg, dict) else {},
        run_manifest_present=media_paths.resolve_artefact(
            project_dir, "run_manifest.json"
        ).exists(),
        project_id=(timeline.get("project_id") if isinstance(timeline, dict) else None),
        project_dir=str(project_dir),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure visual quality from a project's stored artefacts."
    )
    parser.add_argument("project_dir", help="Project directory with stored artefacts")
    parser.add_argument("--output", help="Optional path to write the JSON measurement")
    args = parser.parse_args(argv)
    result = measure_project(args.project_dir)
    text = json.dumps(result, indent=2, sort_keys=False)
    if args.output:
        write_text_atomic(Path(args.output), text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
