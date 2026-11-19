"""Machine-readable Asset Manager report (founder directive §E, AUD-16/23).

Every documentary produces one of these. It counts — from the actual
pipeline outputs, never from intentions — what was retrieved, accepted,
rejected, substituted and reused.

Reporting model (founder format correction, 2026-11-19): a documentary
is described by the QUALITY and COMPOSITION of its visuals, never
judged by its percentage of moving footage. Four properties are kept
strictly separate:
- authenticity: were accepted visuals verified genuine, or were any
  substituted (text cards, renderer placeholders, unverified assets)?
  Generated graphics and photographs are legitimate visuals and do NOT
  count against authenticity — they are reported in the format mix.
- format mix: how many accepted visuals are footage / photographs /
  documents / maps / diagrams / graphics / cards / uncertain.
- provenance: which providers supplied candidates and accepted assets,
  with a warning when one provider supplies >80% of accepted footage.
- rights: recorded licences vs honestly-unknown licensing. A genuine
  asset with unknown licensing is authentic but NOT rights-cleared;
  the two are never merged.

Honesty rules baked in:
- A text card is counted as a text card, never as an image.
- A placeholder clip is counted from the renderer's own audit; if the
  renderer audit is unavailable the count is None (unknown), NOT zero,
  and authenticity cannot be certified while it is unknown.
- An asset whose licence is missing or "unknown" is counted as unknown
  licensing — this module never upgrades it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

# The verifier's representative-specificity acceptance threshold
# (story_understanding.verify_candidate). Accepted assets below it are
# reported as low-confidence selections.
LOW_CONFIDENCE_SCORE = 0.34


def _is_card(asset: dict) -> bool:
    return (
        asset.get("asset_scope") == "fallback_card"
        or asset.get("source") == "text_card"
        or asset.get("type") == "text_card"
    )


def _is_graphic(asset: dict) -> bool:
    return bool(
        asset.get("motion_graphic")
        or asset.get("type") == "motion_graphic"
        or asset.get("source") == "cinemora_motion_graphics"
    )


def _is_verified(asset: dict) -> bool:
    return asset.get("verification_status") == "verified"


def _score(asset: dict):
    for key in ("universal_score", "verification_score", "relevance_score"):
        value = asset.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _source_name(asset: dict) -> str:
    source = str(asset.get("source") or "").strip()
    if source:
        return source
    url = str(asset.get("url") or asset.get("video_url") or "").strip()
    if url:
        return urlparse(url).netloc or url
    return "unknown"


def _timeline_items(timeline_result) -> list:
    if not isinstance(timeline_result, dict):
        return []
    items = list(timeline_result.get("items") or [])
    if items:
        return items
    for scene in timeline_result.get("scenes") or []:
        if isinstance(scene, dict):
            items.extend(scene.get("items") or [])
    return items


def build_asset_report(processed_scenes, timeline_result=None, renderer_audit=None, analysis_mode=None, run_failure=None, execution=None) -> dict:
    """Build the report from real pipeline outputs.

    processed_scenes: scene result dicts from the media pipeline.
    timeline_result:  the dict returned by build_project_timeline.
    renderer_audit:   parsed renderer_timing_audit.json, or None.
    analysis_mode:    "llm", "partial_llm" or "degraded" from the
                      story analysis (MA-27) — recorded verbatim so
                      every report states explicitly whether real
                      LLM analysis, a partial LLM/degraded mix, or
                      the heuristic degraded mode produced the
                      scenarios the assets were selected against.
    execution:        the run_metrics snapshot for this run (Stage 0
                      observability): provider call counts, download
                      totals, stage durations, wall time. Its
                      monetary_cost_usd is None unless a price table
                      is configured — unknown cost is never reported
                      as zero. None when the caller has no metrics.
    """
    stats = Counter()
    accepted_by_path: dict[str, dict] = {}
    provider_funnel: dict[str, Counter] = {}
    kind_funnel: dict[str, Counter] = {}
    download_failures: list = []
    missing_visuals: list = []
    rejection_reasons: Counter = Counter()
    discovery_failures: list = []
    recovery_attempts: list = []
    candidate_records: list = []

    for scene in processed_scenes or []:
        if not isinstance(scene, dict):
            continue
        scene_stats = scene.get("asset_stats") or {}
        for key in ("candidates_considered", "candidates_rejected", "downloads_failed"):
            try:
                stats[key] += int(scene_stats.get(key) or 0)
            except (TypeError, ValueError):
                pass
        for provider, counts in (scene_stats.get("providers") or {}).items():
            funnel = provider_funnel.setdefault(provider, Counter())
            for stage, count in (counts or {}).items():
                try:
                    funnel[stage] += int(count or 0)
                except (TypeError, ValueError):
                    pass
        for kind, counts in (scene_stats.get("kinds") or {}).items():
            funnel = kind_funnel.setdefault(kind, Counter())
            for stage, count in (counts or {}).items():
                try:
                    funnel[stage] += int(count or 0)
                except (TypeError, ValueError):
                    pass
        download_failures.extend(scene_stats.get("download_failures") or [])
        for reason, count in (scene_stats.get("rejection_reasons") or {}).items():
            try:
                rejection_reasons[str(reason)] += int(count or 0)
            except (TypeError, ValueError):
                pass
        for failure in scene_stats.get("discovery_failures") or []:
            if isinstance(failure, dict):
                discovery_failures.append(
                    {"scene": scene.get("scene_number"), **failure}
                )
        for record in scene_stats.get("candidate_records") or []:
            if isinstance(record, dict):
                candidate_records.append(
                    {"scene": scene.get("scene_number"), **record}
                )
        for attempt in scene_stats.get("recovery_attempts") or []:
            if isinstance(attempt, dict):
                recovery_attempts.append(
                    {"scene": scene.get("scene_number"), **attempt}
                )
        # Sentences for which retrieval produced no verified visual.
        # Since the text-card fallback was removed, these are reported
        # explicitly here instead of being silently substituted.
        for missing in scene_stats.get("missing_visuals") or []:
            if isinstance(missing, dict):
                missing_visuals.append(
                    {"scene": scene.get("scene_number"), **missing}
                )
        for asset in list(scene.get("videos") or []) + list(scene.get("images") or []):
            if not isinstance(asset, dict):
                continue
            path = str(asset.get("file_path") or asset.get("path") or "")
            if path and path not in accepted_by_path:
                accepted_by_path[path] = asset

    accepted = list(accepted_by_path.values())
    cards = [a for a in accepted if _is_card(a)]
    graphics = [a for a in accepted if _is_graphic(a) and not _is_card(a)]
    real_assets = [a for a in accepted if not _is_card(a) and not _is_graphic(a)]
    videos = [a for a in real_assets if (a.get("type") or a.get("visual_type")) == "video"]
    images = [a for a in real_assets if (a.get("type") or a.get("visual_type")) == "image"]

    verified_videos = [a for a in videos if _is_verified(a)]
    verified_images = [a for a in images if _is_verified(a)]
    unverified = [a for a in real_assets if not _is_verified(a)]
    unknown_licence = [
        a for a in real_assets if not a.get("license") or a.get("license") == "unknown"
    ]
    low_confidence = [
        a
        for a in real_assets
        if not _is_verified(a) or (_score(a) is not None and _score(a) < LOW_CONFIDENCE_SCORE)
    ]

    # ---- timeline-level measures -------------------------------------
    items = _timeline_items(timeline_result)
    assignment_counts = Counter(
        str(item.get("assignment") or "unmarked") for item in items if isinstance(item, dict)
    )
    total_items = sum(assignment_counts.values())
    assignment_accuracy = (
        round(assignment_counts.get("sentence", 0) / total_items, 4) if total_items else None
    )

    probed_duration = {
        path: asset.get("duration_seconds")
        for path, asset in accepted_by_path.items()
        if isinstance(asset.get("duration_seconds"), (int, float))
    }
    seen_paths: set[str] = set()
    repeated = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("file_path") or item.get("path") or "")
        is_repeat = bool(path and path in seen_paths)
        if path:
            seen_paths.add(path)
        loops = False
        real_duration = probed_duration.get(path)
        item_duration = item.get("duration")
        if isinstance(real_duration, (int, float)) and isinstance(item_duration, (int, float)):
            # The renderer loops a clip (-stream_loop -1) when the timeline
            # window is longer than the clip itself.
            loops = float(item_duration) > float(real_duration) + 0.05
        if is_repeat or loops:
            repeated += 1

    # MA-35: positional (evidence-free) segment ASSIGNMENTS, counted
    # from the timeline items' own segment_evidence stamps — the
    # share of the finished film whose windows rest on an evenly
    # spaced guess rather than segment evidence. Kept distinct from
    # the per-asset class counts in verification_dimensions below:
    # one positional asset can serve several assignments.
    positional_assignments = sum(
        1
        for item in items
        if isinstance(item, dict)
        and item.get("segment_evidence") == "positional_no_contract"
    )

    # ---- renderer placeholders / holdovers ---------------------------
    if isinstance(renderer_audit, dict) and renderer_audit.get("placeholder_count") is not None:
        placeholder_clips = int(renderer_audit.get("placeholder_count") or 0)
    else:
        placeholder_clips = None  # unknown — never claim zero
    holdover_clips = (
        int(renderer_audit.get("holdover_count") or 0)
        if isinstance(renderer_audit, dict)
        else 0
    )

    # ---- source reuse --------------------------------------------------
    source_counter = Counter(_source_name(a) for a in real_assets)
    url_counter = Counter(
        str(a.get("video_url") or a.get("url") or "") for a in real_assets
    )
    url_counter.pop("", None)

    substitutions = {
        "text_cards": len(cards),
        "placeholder_clips": placeholder_clips,
        "unverified_assets": len(unverified),
        "generated_graphics": len(graphics),
    }

    # ---- format mix (composition, reported neutrally) ------------------
    def _format_of(asset):
        fmt = str(asset.get("visual_format") or "").strip()
        if fmt:
            return fmt
        if _is_card(asset):
            return "text_card"
        if _is_graphic(asset):
            return "graphic"
        if (asset.get("type") or asset.get("visual_type")) == "video":
            return "footage"
        return "uncertain"  # genuine image, kind not evidence-classified

    format_mix = Counter(_format_of(a) for a in accepted)

    # ---- authenticity (honesty verdict — NOT a footage percentage) -----
    # Authentic = no placeholder clips (and the count is KNOWN), no text
    # cards standing in for visuals, no unverified assets. Photographs,
    # documents, maps and identified generated graphics are legitimate
    # documentary visuals and never count against authenticity.
    authentic = (
        placeholder_clips == 0
        and len(cards) == 0
        and len(unverified) == 0
    )
    authenticity = {
        "authentic": authentic,
        "verified_genuine_assets": len(verified_videos) + len(verified_images),
        "generated_visuals_identified": len(graphics),
        "substitutions": {
            "text_cards": len(cards),
            "placeholder_clips": placeholder_clips,
            "unverified_assets": len(unverified),
        },
    }

    # ---- provenance: provider funnel + single-provider reliance --------
    accepted_footage_by_provider = Counter()
    for asset in videos:
        provider = str(asset.get("provider") or asset.get("source") or "unknown")
        accepted_footage_by_provider[provider] += 1
        funnel = provider_funnel.setdefault(provider, Counter())
        funnel["accepted_footage"] += 1
    total_footage = sum(accepted_footage_by_provider.values())
    reliance_provider, reliance_share = None, 0.0
    if total_footage:
        reliance_provider, reliance_count = accepted_footage_by_provider.most_common(1)[0]
        reliance_share = reliance_count / total_footage
    reliance_warning = bool(total_footage and reliance_share > 0.8)

    # ---- rights (kept separate from authenticity) ----------------------
    rights = {
        "recorded_licences": len(real_assets) - len(unknown_licence),
        "unknown_licences": len(unknown_licence),
    }

    # ---- segment retrieval (original vs extracted, honestly) -----------
    segment_retrieval = [
        {
            "provider": _source_name(asset),
            "url": asset.get("video_url") or asset.get("url") or "",
            "source_duration_seconds": asset.get("source_duration_seconds"),
            "segment_start_seconds": asset.get("clip_start"),
            "segment_evidence": asset.get("segment_evidence"),
            "segment_evidence_basis": asset.get("segment_evidence_basis"),
            "extracted_duration_seconds": asset.get("duration_seconds"),
            "download_method": asset.get("download_method"),
            "partial_download": asset.get("partial_download"),
        }
        for asset in real_assets
        if (asset.get("type") or asset.get("visual_type")) == "video"
    ]

    coverage = None
    if isinstance(timeline_result, dict):
        coverage = timeline_result.get("coverage")

    return {
        "analysis_mode": analysis_mode,
        # VA-07: a failed run still produces this report (from partial
        # state); run_failure names the stage and error. None means
        # the run completed.
        "run_failure": run_failure,
        "run_completed": run_failure is None,
        # Stage 0 observability: what the run actually did — provider
        # calls, downloads, stage durations, wall time (see
        # services/run_metrics). Present on success AND failure
        # reports; None only when the caller collected no metrics.
        "execution": execution,
        # VA-09: the four verification dimensions, kept separate —
        # technical validity, metadata relevance, segment evidence
        # and rights are different judgements and are never merged
        # into a single "verified" claim.
        "verification_dimensions": {
            "technical_validity": {
                "accepted_with_technical_checks": sum(
                    1 for a in real_assets
                    if (a.get("technical_checks") or {}).get("passed")
                ),
                "accepted_total": len(real_assets),
            },
            "metadata_relevance": {
                "verified_for_sentence": assignment_counts.get("sentence", 0),
                "assigned_by_rotation": assignment_counts.get("rotation", 0),
                "low_confidence_selections": len(low_confidence),
            },
            "segment_evidence": {
                "by_class": dict(Counter(
                    a.get("segment_evidence") or "not_applicable"
                    for a in real_assets
                    if (a.get("type") or a.get("visual_type")) == "video"
                )),
                "positional_no_contract": sum(
                    1 for a in real_assets
                    if a.get("segment_evidence") == "positional_no_contract"
                ),
                # MA-35: assignment-level positional exposure (the
                # auditor's share limit applies to this, not to the
                # per-asset count above).
                "positional_assignments": positional_assignments,
                "positional_assignment_share": (
                    round(positional_assignments / total_items, 4)
                    if total_items else 0.0
                ),
            },
            "rights": {
                "recorded_licences": len(real_assets) - len(unknown_licence),
                "unknown_licences": len(unknown_licence),
            },
        },
        # VA-09: rights honesty. RIGHTS_POLICY=allow_all is the
        # founder's operating policy: assets are accepted on relevance
        # and technical validity. That is NOT legal clearance.
        "rights_note": (
            "RIGHTS_POLICY=allow_all: acceptance is based on relevance "
            "and technical validity only. A recorded licence is "
            "provider-supplied evidence; an unknown licence is NOT "
            "cleared for publication, and technical acceptance is not "
            "legal clearance."
        ),
        "provider_rights_policies": {
            "wikimedia_stills": (
                "Discovery pre-filters to public-domain / CC0 / CC-BY "
                "licences; other licences never become candidates."
            ),
            "wikimedia_videos": (
                "No licence pre-filter; the licence is labelled from "
                "provider metadata, including NC/ND."
            ),
            "archive_org": (
                "Licence mapped only from recognizable licence URLs; "
                "anything unrecognized stays unknown."
            ),
            "all_other_providers": (
                "Licence recorded as unknown unless the provider "
                "supplies evidence; unknown is never treated as cleared."
            ),
        },
        # VA-10: rotation assignments are counted explicitly — a
        # rotated visual was verified for a DIFFERENT sentence.
        "rotation_assignments": assignment_counts.get("rotation", 0),
        "total_retrieved_assets": stats["candidates_considered"],
        "total_accepted_assets": len(accepted),
        "total_rejected_assets": stats["candidates_rejected"],
        "verified_moving_footage": len(verified_videos),
        "verified_images": len(verified_images),
        "unverified_assets": len(unverified),
        "unknown_licensing": len(unknown_licence),
        "generated_graphics": len(graphics),
        "text_cards": len(cards),
        "placeholder_clips": placeholder_clips,
        "holdover_clips": holdover_clips,
        "segment_retrieval": segment_retrieval,
        # VA-08: per-candidate traceability. The aggregates are
        # computed FROM these records, so totals and individual
        # records cannot disagree.
        "candidate_records": {
            "total": len(candidate_records),
            "by_outcome": dict(
                Counter(r.get("outcome") for r in candidate_records)
            ),
            "by_stage": dict(
                Counter(r.get("stage") for r in candidate_records)
            ),
            "records": candidate_records,
        },
        "missing_visuals": missing_visuals,
        "missing_visual_count": len(missing_visuals),
        "repeated_footage": repeated,
        "failed_downloads": stats["downloads_failed"],
        "low_confidence_selections": len(low_confidence),
        "sentence_assignment_accuracy": assignment_accuracy,
        "timeline_items": total_items,
        "assignment_breakdown": dict(assignment_counts),
        "source_reuse": {
            "distinct_sources": len(source_counter),
            "max_uses_of_single_source_url": max(url_counter.values()) if url_counter else 0,
            "top_sources": [
                {"source": name, "assets": count}
                for name, count in source_counter.most_common(5)
            ],
        },
        "substitutions": substitutions,
        "authenticity": authenticity,
        "authentic": authentic,
        "format_mix": {
            "accepted": dict(format_mix),
            "by_kind": {name: dict(counts) for name, counts in sorted(kind_funnel.items())},
        },
        "providers": {
            "funnel": {name: dict(counts) for name, counts in sorted(provider_funnel.items())},
            "accepted_footage_by_provider": dict(accepted_footage_by_provider),
            "single_provider_reliance": {
                "warning": reliance_warning,
                "provider": reliance_provider if reliance_warning else None,
                "footage_share": round(reliance_share, 4) if total_footage else None,
            },
        },
        "download_failures": download_failures,
        "rejection_reasons": dict(rejection_reasons),
        "discovery_failures": discovery_failures,
        "recovery_attempts": recovery_attempts,
        "coverage": coverage,
        "rights": rights,
    }


def write_asset_report(project_dir, report) -> Path:
    path = Path(project_dir) / "asset_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def build_rights_manifest(processed_scenes) -> list:
    """Per-asset rights evidence for a project (AUD-15).

    One entry per accepted asset: where it came from, who created it (when
    the provider reports it), its licence exactly as recorded — "unknown"
    stays "unknown" — and when it was retrieved. This manifest is the
    project's reuse evidence; nothing in it is inferred or upgraded.
    """
    entries: dict[str, dict] = {}
    sentences_by_path: dict[str, list] = {}

    for scene in processed_scenes or []:
        if not isinstance(scene, dict):
            continue
        for asset in list(scene.get("videos") or []) + list(scene.get("images") or []):
            if not isinstance(asset, dict):
                continue
            path = str(asset.get("file_path") or asset.get("path") or "")
            if path and path not in entries:
                entries[path] = asset
        for sentence in scene.get("sentences") or []:
            if not isinstance(sentence, dict):
                continue
            number = sentence.get("sentence_number")
            for asset in list(sentence.get("videos") or []) + list(sentence.get("images") or []):
                if not isinstance(asset, dict):
                    continue
                path = str(asset.get("file_path") or asset.get("path") or "")
                if path and number is not None:
                    sentences_by_path.setdefault(path, [])
                    if number not in sentences_by_path[path]:
                        sentences_by_path[path].append(number)

    manifest = []
    for path, asset in entries.items():
        manifest.append(
            {
                "file_path": path,
                "type": asset.get("type") or asset.get("visual_type") or "",
                "title": asset.get("title") or "",
                "source": asset.get("source") or "",
                "source_url": asset.get("source_url") or asset.get("link") or "",
                "content_url": asset.get("video_url")
                or asset.get("image_url")
                or asset.get("url")
                or "",
                "creator": asset.get("creator") or asset.get("channel") or "",
                "license": asset.get("license") or "unknown",
                "license_url": asset.get("license_url") or "",
                "rights_status": asset.get("rights_status") or "verify_before_publish",
                "retrieved_at": asset.get("retrieved_at") or "",
                "sentence_numbers": sentences_by_path.get(path, []),
            }
        )
    return manifest


def write_rights_manifest(project_dir, manifest) -> Path:
    path = Path(project_dir) / "rights_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
