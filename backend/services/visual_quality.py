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
        },
        "runtime_share": _runtime_block(dict(kind_totals), total_runtime),
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
        "uncovered_beats": _uncovered_beats(report, timeline),
        "rejection_reasons": _rejection_reasons(report),
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
    }


def measure_project(project_dir) -> dict:
    """Load a project's stored artefacts from disk and measure them."""
    project_dir = Path(project_dir)
    timeline = _load_json(project_dir / "timeline.json", {})
    report = _load_json(project_dir / "asset_report.json", {})
    manifest = _load_json(project_dir / "rights_manifest.json", [])
    mg = _load_json(project_dir / "motion_graphics" / "motion_graphics.json", {})
    return measure_visual_quality(
        timeline=timeline if isinstance(timeline, dict) else {},
        asset_report=report if isinstance(report, dict) else {},
        rights_manifest=manifest if isinstance(manifest, list) else [],
        motion_graphics=mg if isinstance(mg, dict) else {},
        run_manifest_present=(project_dir / "run_manifest.json").exists(),
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
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
