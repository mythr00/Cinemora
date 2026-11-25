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
import os
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from services import rejection_diagnostics
from services import visual_identity
from services.atomic_write import write_json_atomic

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


def build_asset_report(processed_scenes, timeline_result=None, renderer_audit=None, analysis_mode=None, run_failure=None, execution=None, soundtrack=None, genre_pack=None, final_audio=None, audio_provenance=None, planner=None, research=None) -> dict:
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
    soundtrack:       the Sound Designer stage's record (Stage 3H;
                      Stage H adds "state", "decided_by",
                      "source_type" and the track's "identity").
                      Emitted verbatim as the top-level "soundtrack"
                      key; None keeps the pre-3H behaviour for other
                      callers.
    final_audio:      Stage H (M9): loudness + true-peak measured
                      on the EXPORTED file (services.music
                      final_audio_record). Emitted verbatim as
                      "final_audio"; None when unmeasured.
    audio_provenance: Stage H (M10): the record-vs-audio
                      consistency assessment (services.music
                      assess_audio_provenance). Emitted verbatim as
                      "audio_provenance"; None when not assessed.
    """
    stats = Counter()
    accepted_by_path: dict[str, dict] = {}
    provider_funnel: dict[str, Counter] = {}
    kind_funnel: dict[str, Counter] = {}
    download_failures: list = []
    missing_visuals: list = []
    uncovered_unsearched: list = []
    rejection_reasons: Counter = Counter()
    discovery_failures: list = []
    discovery_failures_collected = False
    recovery_attempts: list = []
    coverage_passes: list = []
    variety_passes: list = []
    # Master Directive Phase 2 / B2: every tier-ladder attempt the
    # production recorded (from coverage passes and from
    # missing_visuals entries — the D-B3 unsearched entries carry
    # theirs only there), deduplicated by identity: a searchable
    # beat's attempts appear in both its coverage passes and its
    # missing entries as the same dicts.
    tier_attempts: list = []
    _tier_attempt_ids: set = set()

    def _collect_tier_attempts(attempts, scene_number):
        for attempt in attempts or []:
            if isinstance(attempt, dict) and id(attempt) not in _tier_attempt_ids:
                _tier_attempt_ids.add(id(attempt))
                tier_attempts.append(
                    {"scene": scene_number, **attempt}
                )

    candidate_records: list = []
    query_diagnostics: list = []
    beat_sufficiency: list = []
    # Run-11 fix 2: per-production provider download health. The
    # snapshot is cumulative across the production (not per
    # scene), so the LAST scene snapshot a pass stamped is the
    # production's final table.
    provider_health_snapshot: dict = {}

    for scene in processed_scenes or []:
        if not isinstance(scene, dict):
            continue
        scene_stats = scene.get("asset_stats") or {}
        if "discovery_failures" in scene_stats:
            discovery_failures_collected = True
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
        if scene_stats.get("provider_health"):
            provider_health_snapshot = scene_stats["provider_health"]
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
                merged = {"scene": scene.get("scene_number"), **record}
                # Stage 1 / R5: every event carries its stable id
                # (older records constructed without one get it here,
                # where the scene number is known).
                if not merged.get("event_id"):
                    merged["event_id"] = rejection_diagnostics.event_id_for(
                        merged.get("scene"),
                        merged.get("shot_index"),
                        merged.get("candidate_id"),
                    )
                candidate_records.append(merged)
        for attempt in scene_stats.get("recovery_attempts") or []:
            if isinstance(attempt, dict):
                recovery_attempts.append(
                    {"scene": scene.get("scene_number"), **attempt}
                )
        # Phase 3 / AM-021: per-beat query diagnostics, as recorded
        # by the retrieval passes (queries issued, their sources,
        # hits / candidates / verified per query).
        for diag in scene_stats.get("query_diagnostics") or []:
            if isinstance(diag, dict):
                query_diagnostics.append(
                    {"scene": scene.get("scene_number"), **diag}
                )
        # Phase 3 / FP-4: per-beat sufficiency records (distinct
        # fetched assets vs the beat's sentence count).
        for suff in scene_stats.get("beat_sufficiency") or []:
            if isinstance(suff, dict):
                beat_sufficiency.append(
                    {"scene": scene.get("scene_number"), **suff}
                )
        # Coverage fix: the coverage pass's per-shot record (draws
        # from the verified pool, blocks, exhaustion) rides beside
        # recovery_attempts, so a coverage outcome is explainable
        # from the report alone.
        for cov_pass in scene_stats.get("coverage_passes") or []:
            if isinstance(cov_pass, dict):
                coverage_passes.append(
                    {"scene": scene.get("scene_number"), **cov_pass}
                )
                _collect_tier_attempts(
                    cov_pass.get("tier_attempts"),
                    scene.get("scene_number"),
                )
        # Phase 5 / AM-003: the variety pass's per-shot record
        # (distinct counts before/after, draws, blocks) rides
        # beside coverage_passes, so a variety outcome is
        # explainable from the report alone.
        for var_pass in scene_stats.get("variety_passes") or []:
            if isinstance(var_pass, dict):
                variety_passes.append(
                    {"scene": scene.get("scene_number"), **var_pass}
                )
        # Sentences for which retrieval produced no verified visual.
        # Since the text-card fallback was removed, these are reported
        # explicitly here instead of being silently substituted.
        for missing in scene_stats.get("missing_visuals") or []:
            if isinstance(missing, dict):
                missing_visuals.append(
                    {"scene": scene.get("scene_number"), **missing}
                )
                _collect_tier_attempts(
                    missing.get("tier_attempts"),
                    scene.get("scene_number"),
                )
        # B-roll B0: sentences in unsearched-role beats, named by
        # the pipeline in scene_stats. Phase 2 / D-B3: since B2
        # these sentences ALSO enter missing_visuals (reason
        # "unsearched_role") when their T3 ladder entry leaves
        # them uncovered; this list remains the scene-level naming
        # of every unsearched-role sentence, covered or not.
        for unsearched in scene_stats.get("uncovered_unsearched") or []:
            if isinstance(unsearched, dict):
                uncovered_unsearched.append(
                    {"scene": scene.get("scene_number"), **unsearched}
                )
        for asset in list(scene.get("videos") or []) + list(scene.get("images") or []):
            if not isinstance(asset, dict):
                continue
            path = str(asset.get("file_path") or asset.get("path") or "")
            if path and path not in accepted_by_path:
                accepted_by_path[path] = asset

    # Stage 6 / C.A.3.4 + ASSET-017: the headline rejection_reasons
    # counter is derived FROM the candidate records (primary reasons
    # only) whenever records exist — the per-scene counters summed
    # above are a legacy fallback for record-less inputs only (old
    # stored shapes), never a parallel source that can drift.
    if candidate_records:
        rejection_reasons = Counter(
            rejection_diagnostics.primary_reason_counts(candidate_records)
        )
    # Attempt-level download failures, counted from the records
    # (one event per failed pick attempt). Distinct from the
    # shot-level "failed_downloads" scalar below; None — unknown,
    # not zero — when the run produced no candidate records.
    download_attempt_failures = (
        sum(
            1
            for r in candidate_records
            if r.get("outcome") == "download_failed"
        )
        if candidate_records
        else None
    )

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
    # Phase 5 / AM-017: repetition and the distinct headline are
    # resolved through services.visual_identity — the ONE
    # distinct-visual definition visual_quality and the production
    # report also use — never from file paths: the same source
    # image downloaded twice is ONE visual here too. (Stills
    # hash-unify from the files on disk at report time; an
    # unreadable file falls back to the canonical ladder.)
    _dict_items = [item for item in items if isinstance(item, dict)]
    _item_identities = visual_identity.resolve_asset_groups(
        _dict_items,
        hash_of=lambda _item: visual_identity.content_hash_for_file(
            str(_item.get("file_path") or _item.get("path") or "")
        ),
    )
    distinct_visuals_on_timeline = len(set(_item_identities))
    _identity_by_item = {
        id(_item): _identity
        for _item, _identity in zip(_dict_items, _item_identities)
    }
    seen_identities: set[str] = set()
    repeated = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("file_path") or item.get("path") or "")
        _identity = _identity_by_item.get(id(item))
        is_repeat = bool(
            _identity is not None and _identity in seen_identities
        )
        if _identity is not None:
            seen_identities.add(_identity)
        # Founder defect ruling 2026-11-23: the renderer no longer
        # loops a short source to fill its window (the historical
        # -stream_loop -1 repetition this counter also flagged);
        # a longer window now holds the source's last frame, which
        # is one continuous segment, not repeated footage. Only a
        # genuine identity repeat counts here.
        if is_repeat:
            repeated += 1

    # MA-35: positional (evidence-free) segment ASSIGNMENTS, counted
    # from the timeline items' own segment_evidence stamps — the
    # share of the finished film whose windows rest on an evenly
    # spaced guess rather than segment evidence. Kept distinct from
    # the per-asset class counts in verification_dimensions below:
    # one positional asset can serve several assignments.
    # (Phase 3 note: the tier_breakdown built below now also carries
    # the D-B1 band comparison — see its own block comment.)
    positional_assignments = sum(
        1
        for item in items
        if isinstance(item, dict)
        and item.get("segment_evidence") == "positional_no_contract"
    )

    # ---- B-roll tier breakdown (B0, plan §6.1/§6.2) ------------------
    # Items and runtime per tier, read from the timeline items' own
    # tier stamps. A timeline whose items carry no stamps (stored
    # pre-B0 productions) reports state "not_recorded" with a null
    # B-roll share — unknown, never zero. The B-roll share is
    # (activity_broll + atmospheric runtime) / total item runtime.
    # Phase 3 / D-B1 (the B3 remainder): the measured share is also
    # compared against the active pack's founder-set band
    # (expectations.broll_share) in the "band" sub-block below —
    # REPORT ONLY, never a gate: an over-band run reports
    # "over_band" and proceeds exactly as before.
    _tier_item_counts: Counter = Counter()
    _tier_runtime: Counter = Counter()
    _tier_per_sentence: list = []
    _tier_total_runtime = 0.0
    _unstamped_items = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        _duration = item.get("duration")
        _duration = float(_duration) if isinstance(_duration, (int, float)) else 0.0
        _tier_total_runtime += _duration
        _tier = item.get("tier")
        if _tier not in rejection_diagnostics.TIERS:
            _unstamped_items += 1
            continue
        _tier_item_counts[_tier] += 1
        _tier_runtime[_tier] += _duration
        _tier_per_sentence.append(
            {
                "scene_number": item.get("scene_number"),
                "sentence_index": item.get("sentence_index"),
                "tier": _tier,
            }
        )
    _broll_runtime = sum(
        _tier_runtime.get(tier, 0.0)
        for tier in rejection_diagnostics.BROLL_TIERS
    )
    tier_breakdown = {
        "state": (
            "recorded" if _tier_item_counts else "not_recorded"
        ),
        "by_tier": {
            tier: {
                "items": _tier_item_counts[tier],
                "runtime_seconds": round(_tier_runtime[tier], 6),
                "runtime_share": (
                    round(_tier_runtime[tier] / _tier_total_runtime, 6)
                    if _tier_total_runtime
                    else None
                ),
            }
            for tier in rejection_diagnostics.TIERS
            if _tier_item_counts.get(tier)
        },
        "broll_runtime_seconds": round(_broll_runtime, 6),
        "broll_share": (
            round(_broll_runtime / _tier_total_runtime, 6)
            if _tier_item_counts and _tier_total_runtime
            else None
        ),
        "unstamped_items": _unstamped_items,
        "per_sentence": _tier_per_sentence,
    }
    # Phase 3 / D-B1: the band comparison (report only).
    _band_pack_id = (
        genre_pack.get("id")
        if isinstance(genre_pack, dict) else None
    )
    _band = None
    if _band_pack_id:
        from services import genre_packs as _genre_packs

        _band = _genre_packs.broll_share_band(
            _genre_packs.load_pack(str(_band_pack_id))
        )
    if _band is None:
        tier_breakdown["band"] = {
            "state": "unknown",
            "genre_pack": _band_pack_id,
            "max": None,
            "basis": None,
            "evaluation": "unknown",
        }
    elif tier_breakdown["broll_share"] is None:
        tier_breakdown["band"] = {
            "state": "not_recorded",
            "genre_pack": _band_pack_id,
            "max": _band["max"],
            "basis": _band["basis"],
            "evaluation": "unknown",
        }
    else:
        tier_breakdown["band"] = {
            "state": "recorded",
            "genre_pack": _band_pack_id,
            "max": _band["max"],
            "basis": _band["basis"],
            "evaluation": (
                "within_band"
                if tier_breakdown["broll_share"] <= _band["max"]
                else "over_band"
            ),
        }

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
    # Phase 3 / AM-022b: reliance is computed for STILLS as well as
    # footage, and every figure states its base n beside it. The
    # >80% warning semantics are unchanged — now evaluated per kind
    # on its stated base. The legacy keys (warning / provider /
    # footage_share) keep their meaning for existing consumers:
    # warning fires when EITHER kind is over the line, and provider
    # names the kind that fired (footage first, as before).
    accepted_stills_by_provider = Counter()
    for asset in images:
        provider = str(asset.get("provider") or asset.get("source") or "unknown")
        accepted_stills_by_provider[provider] += 1
    total_stills = sum(accepted_stills_by_provider.values())
    stills_provider, stills_share = None, 0.0
    if total_stills:
        stills_provider, stills_count = accepted_stills_by_provider.most_common(1)[0]
        stills_share = stills_count / total_stills
    footage_reliance_warning = bool(total_footage and reliance_share > 0.8)
    stills_reliance_warning = bool(total_stills and stills_share > 0.8)
    reliance_warning = footage_reliance_warning or stills_reliance_warning
    reliance_warning_provider = (
        reliance_provider if footage_reliance_warning
        else (stills_provider if stills_reliance_warning else None)
    )

    # ---- discovery state (Phase 3 / AM-022, reporting only) ----------
    # "Discovery degraded: no search key" is a first-class run-level
    # fact, recorded ONLY when the run made zero Serper calls BECAUSE
    # no search key was configured. Zero calls for any other reason
    # (key configured but the run never needed Serper, cache hits,
    # ...) is a different fact and never wears this label; a run with
    # no execution record at all reports state "unknown". Key supply
    # itself is NOT an engineering change (DM-4 is the founder's) —
    # this block only reports.
    _serper_calls = None
    if isinstance(execution, dict):
        _serper_block = execution.get("serper_calls")
        if isinstance(_serper_block, dict):
            _serper_calls = _serper_block.get("total")
    _search_key_configured = bool(os.getenv("SERPER_API_KEY"))
    if _serper_calls is None:
        discovery = {
            "state": "unknown",
            "degraded": None,
            "reason": None,
            "serper_calls": None,
            "search_key_configured": _search_key_configured,
            "fact": None,
        }
    elif _serper_calls == 0 and not _search_key_configured:
        discovery = {
            "state": "degraded_no_search_key",
            "degraded": True,
            "reason": "no_search_key",
            "serper_calls": 0,
            "search_key_configured": False,
            "fact": "discovery degraded: no search key",
        }
    else:
        discovery = {
            "state": "recorded",
            "degraded": False,
            "reason": None if _serper_calls else "no_serper_calls",
            "serper_calls": _serper_calls,
            "search_key_configured": _search_key_configured,
            "fact": None,
        }

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

    # Phase 1 / AM-018: ONE coverage rule, applied here — the only
    # place both facts exist side by side. Retrieval records a
    # sentence in missing_visuals when its beat produced no verified
    # asset; the timeline records a sentence as uncovered when no
    # item covers its narration window. They are different facts,
    # and stored reports used to read as contradictions (a failed
    # run with missing_visuals=[] beside incomplete coverage; a
    # completed run whose rotation-filled gaps still read as bare
    # "missing"). The rule (defined in scalar_definitions below):
    # sentence-level non-coverage ALWAYS materialises into
    # missing_visuals, and every entry carries its coverage_outcome
    # — the timeline's verdict for that sentence. Entries the
    # timeline knows but retrieval never recorded are appended
    # with reason "no_timeline_coverage". The coverage GATE is
    # untouched: this is reporting semantics only. When the report
    # is built without a timeline, outcomes are None (unknown,
    # never a fabricated verdict). The appended-entry shape is the
    # base missing_visuals shape (scene / sentence_number / reason)
    # plus coverage_outcome and the uncovered window — Phase 2's
    # unsearched_role entries extend THIS shape, not a parallel one.
    if isinstance(coverage, dict):
        _uncovered = {
            entry.get("sentence_number"): entry
            for entry in coverage.get("uncovered_sentences") or []
            if isinstance(entry, dict)
        }
        # Scene sentence offsets from the timeline's own scene
        # records (each scene's sentence_timings slice is that
        # scene's sentences, in order), so a global sentence number
        # maps to the scene-local sentence_index timeline items
        # carry — the covering item's assignment then names HOW a
        # retrieval-missing sentence was covered.
        _scene_offsets = {}
        _offset = 0
        for _scene_result in timeline_result.get("scenes") or []:
            if not isinstance(_scene_result, dict):
                continue
            _scene_count = len(
                _scene_result.get("sentence_timings") or []
            )
            _scene_number = _scene_result.get("scene_number")
            if _scene_number is not None and _scene_count:
                _scene_offsets[_scene_number] = _offset
            _offset += _scene_count
        _covering_assignment = {}
        for _item in items:
            if not isinstance(_item, dict):
                continue
            _key = (_item.get("scene_number"), _item.get("sentence_index"))
            if _key not in _covering_assignment:
                _covering_assignment[_key] = _item.get("assignment")

        def _coverage_outcome(entry):
            number = entry.get("sentence_number")
            if number in _uncovered:
                return "uncovered"
            scene = entry.get("scene")
            if scene in _scene_offsets and number is not None:
                local = (number - 1) - _scene_offsets[scene]
                assignment = _covering_assignment.get((scene, local))
                if assignment == "rotation":
                    return "covered_by_rotation"
                if assignment == "motion_graphic":
                    return "covered_by_graphic"
                if assignment:
                    return "covered_by_scene_asset"
            return "covered_unidentified"

        _known_missing = set()
        for entry in missing_visuals:
            entry["coverage_outcome"] = _coverage_outcome(entry)
            _known_missing.add(entry.get("sentence_number"))
        for number, uncovered_entry in _uncovered.items():
            if number in _known_missing:
                continue
            missing_visuals.append(
                {
                    "scene": uncovered_entry.get("scene_number"),
                    "sentence_number": number,
                    "reason": "no_timeline_coverage",
                    "coverage_outcome": "uncovered",
                    "start": uncovered_entry.get("start"),
                    "end": uncovered_entry.get("end"),
                }
            )
    else:
        for entry in missing_visuals:
            entry["coverage_outcome"] = None

    # Phase 2 / B2 totals: what the ladder spent and what it
    # covered, from the attempt records alone (the production
    # ledger itself is runtime state and is not serialised; its
    # budget limits are the tier_ladder constants, named here so
    # the report is self-describing).
    from services import tier_ladder as _tier_ladder

    tier_escalation_totals = {
        "attempts": len(tier_attempts),
        "extra_searches": sum(
            len(attempt.get("queries") or []) for attempt in tier_attempts
        ),
        "by_tier": dict(
            Counter(
                str(attempt.get("tier") or "unknown")
                for attempt in tier_attempts
            )
        ),
        "outcomes": dict(
            Counter(
                str(attempt.get("outcome") or "unknown")
                for attempt in tier_attempts
            )
        ),
        "escalation_accepted": sum(
            int(cov_pass.get("escalation_accepted") or 0)
            for cov_pass in coverage_passes
        ) + sum(
            int(attempt.get("accepted") or 0)
            for attempt in tier_attempts
            if attempt.get("entry") == "unsearched_role"
        ),
        "search_budget": _tier_ladder.PRODUCTION_MAX_EXTRA_SEARCHES,
        "download_budget": _tier_ladder.PRODUCTION_MAX_EXTRA_DOWNLOADS,
    }

    # Transformation Phase 1.1 (OCR wiring): the engine that read
    # frame text for this run's geography sub-check, derived from
    # the frame-check records themselves (accepted assets and
    # candidate records alike) — the most frequent recorded
    # engine, or None when no OCR ran on any checked frame.
    _frame_ocr_engines = Counter(
        str(_check["ocr_engine"])
        for _check in [
            (a.get("frame_check") or {}) for a in real_assets
        ] + [
            (r.get("frame_check") or {}) for r in candidate_records
        ]
        if (_check or {}).get("ocr_engine")
    )
    _visual_frame_ocr_engine = (
        _frame_ocr_engines.most_common(1)[0][0]
        if _frame_ocr_engines else None
    )

    # Transformation Phase 4.2: the source-hint funnel, aggregated
    # FROM the AM-021 query diagnostics (the records of record —
    # never a parallel counter): every query issued under source
    # "source_hint" vs every baseline query, with hits /
    # candidates / verified summed per side. A production whose
    # pack carried no hints reports state "no_hints" with zeroed
    # hint counts — the absence is stated, not implied.
    def _query_side_totals(want_hint):
        totals = {"queries": 0, "hits": 0, "candidates": 0, "verified": 0}
        for _diag in query_diagnostics:
            for _q in _diag.get("queries") or []:
                if not isinstance(_q, dict):
                    continue
                if (_q.get("source") == "source_hint") != want_hint:
                    continue
                totals["queries"] += 1
                for _field in ("hits", "candidates", "verified"):
                    try:
                        totals[_field] += int(_q.get(_field) or 0)
                    except (TypeError, ValueError):
                        pass
        return totals

    _hint_totals = _query_side_totals(True)
    source_hints_summary = {
        "state": "recorded" if _hint_totals["queries"] else "no_hints",
        "genre_pack": (
            genre_pack.get("id")
            if isinstance(genre_pack, dict) else None
        ),
        "hints": _hint_totals,
        "baseline": _query_side_totals(False),
    }

    return {
        "analysis_mode": analysis_mode,
        # Master Directive Phase 7 / P0 (DM-1): the planner
        # self-audit block, verbatim from the analysis — mode,
        # planner version, degraded_reason, degraded sentence
        # counts and the measured plan quality. None when the
        # caller had no analysis (never fabricated); the
        # production report's q30 answers from this block.
        "planner": planner,
        # Phase 3b / C-07: the research pass's own stage outcome
        # (jobs._research_stage_record, derived from the bible's
        # research_meta) — outcome, degraded marker, bounded retry
        # accounting (attempts / failures / limit / errors). None
        # when the caller had no analysis; a degraded research pass
        # is visible here instead of silently folded into the
        # script_analysis stage's success.
        "research": research,
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
        # Phase 3 / AM-022: the run-level discovery fact — degraded
        # (no search key) is recorded distinctly from zero calls for
        # other reasons, and from no record at all (unknown).
        "discovery": discovery,
        # Stage 3H: soundtrack honesty record (see docstring).
        "soundtrack": soundtrack,
        # Stage H (M9/M10): final-file audio measurement and the
        # record-vs-audio provenance assessment (see docstring).
        "final_audio": final_audio,
        "audio_provenance": audio_provenance,
        # Stage 5A: the active genre pack for this run as
        # {"id", "source"} (services.genre_packs) — the pack shaped
        # research queries, graphics template order and engine
        # style; the report records which one was active. None when
        # the caller resolved no pack.
        "genre_pack": genre_pack,
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
            # Transformation Phase 1.1: the visual dimension,
            # EVALUATED on sampled frames of each downloaded file
            # (services/frame_verification) — the dimension the
            # metadata-only era recorded as not_evaluated under
            # AM-028, a carve-out the founder's takeover mandate
            # (2026-11-21) superseded. Derived from the candidate
            # records and accepted assets themselves, never a
            # parallel counter. Watermark presence is a provenance
            # signal only (founder ruling 2026-11-22): counted
            # here, never a rejection.
            "visual_frame": {
                "authority": (
                    "takeover_mandate_2026-11-21 / "
                    "transformation_plan_phase_1_1; supersedes AM-028"
                ),
                "evaluated_assets": sum(
                    1 for a in real_assets if a.get("frame_check")
                ),
                "rejected_events": sum(
                    1 for r in candidate_records
                    if r.get("stage") == "frame_verification"
                ),
                "watermarked_preview_detected": sum(
                    1 for a in real_assets
                    if (a.get("frame_check") or {}).get(
                        "watermarked_preview"
                    )
                ),
                # The OCR engine behind the geography sub-check
                # (frame_verification's provider chain; RapidOCR
                # is the installed primary). None when no frame
                # text was read this run.
                "ocr_engine": _visual_frame_ocr_engine,
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
        # Stage 6 / ASSET-017: truthful alias for the scalar above
        # (see scalar_definitions) — the legacy key keeps its value
        # for existing consumers.
        "candidates_evaluated": stats["candidates_considered"],
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
        # Stage 1 / R5: the structured rejection funnel, aggregated
        # FROM the event records above (primary reasons additive and
        # summing to rejected events; contributing reasons reported
        # separately and never summed in; distinct-candidate view
        # deduplicated by candidate_id with best-outcome precedence).
        "rejection_funnel": rejection_diagnostics.aggregate_funnel(
            candidate_records
        ),
        # Stage 1 / §2.6: per-accepted-asset format exposure so
        # visual_quality can join timeline items to the format each
        # accepted asset was classified as (footage / photograph /
        # document / map / diagram / graphic / uncertain) and produce
        # honest runtime-weighted coverage accounting.
        "accepted_asset_formats": [
            {
                "file_path": str(a.get("file_path") or a.get("path") or ""),
                "visual_format": _format_of(a),
            }
            for a in accepted
        ],
        "missing_visuals": missing_visuals,
        "missing_visual_count": len(missing_visuals),
        # B-roll B0 (§6.1): items/runtime per tier from the timeline
        # items' stamps, the B-roll share (T3+T4 runtime share) and
        # the per-sentence tier list. Phase 3 / D-B1: the block's
        # "band" sub-key carries the founder-band comparison
        # (report only).
        "tier_breakdown": tier_breakdown,
        # B-roll B0 (§3.3): sentences in unsearched-role beats,
        # named here at scene level. Phase 2 / D-B3: since B2 the
        # still-uncovered ones ALSO appear in missing_visuals with
        # reason "unsearched_role" (and a coverage_outcome); this
        # list remains the naming of ALL unsearched-role sentences.
        "uncovered_unsearched": uncovered_unsearched,
        "uncovered_unsearched_count": len(uncovered_unsearched),
        "repeated_footage": repeated,
        # Phase 5 / AM-017: THE canonical distinct-asset headline
        # (definition in scalar_definitions) — the same number
        # visual_quality reports as
        # unique_canonical_assets_on_timeline and the production
        # report answers as q08, from the same identity function.
        "distinct_visuals_on_timeline": distinct_visuals_on_timeline,
        "failed_downloads": stats["downloads_failed"],
        # Stage 6 / ASSET-017: attempt-level counterpart of the
        # shot-level scalar above (definitions in scalar_definitions).
        "download_attempt_failures": download_attempt_failures,
        # Stage 6 / ASSET-017: the stored-report scalars, defined in
        # one place. The C.A.6 recompute showed the historical
        # counters (provider-funnel discovered / candidate records /
        # total_retrieved_assets) disagreeing because their scopes
        # were never defined together; these definitions are the
        # reconciliation — values are unchanged, scopes are explicit.
        "scalar_definitions": {
            "distinct_visuals_on_timeline": (
                "Phase 5 / AM-017 — THE canonical distinct-asset "
                "headline, defined once in services.visual_identity "
                "and used identically by visual_quality "
                "(unique_canonical_assets_on_timeline) and the "
                "production report (q08): distinct visuals among "
                "timeline items at canonical SOURCE identity where "
                "a source URL exists (a video counts by its source "
                "— the clip is one visual however many of its "
                "windows are shown — and a still by its whole "
                "source), with content-hash unification where two "
                "stills' canonical identities differ but their "
                "downloaded bytes are identical, and a file-path "
                "fallback for identity-less items. DEFINITION "
                "CHANGE (Phase 5): pre-Phase-5 counts were "
                "file-path counts and overstated variety whenever "
                "one visual was downloaded twice (film 1: 5 files, "
                "4 visuals); historical comparisons shift by "
                "exactly those merged duplicates."
            ),
            "total_retrieved_assets": (
                "Legacy name for candidates_evaluated: the sum over "
                "scenes of candidates_considered — candidate rows "
                "that entered verification (the scored pools, after "
                "within-pass dedupe), counted once per (scene, pass) "
                "evaluation. NOT a count of distinct retrieved "
                "assets, and NOT the candidate_records event count "
                "(a candidate re-enters per shot it is retrieved "
                "for, and records also cover selection/download "
                "stages the scored pools do not)."
            ),
            "candidates_evaluated": (
                "Same value as total_retrieved_assets, under the "
                "name that matches its definition."
            ),
            "total_rejected_assets": (
                "Candidate rows in the scored pools carrying a "
                "gate/scoring rejection (per scene, per pass). "
                "Selection-stage and dedupe rejections are recorded "
                "as candidate_records events and counted in "
                "rejection_funnel, not here."
            ),
            "failed_downloads": (
                "SHOT-level count: (scene, pass) evaluations in "
                "which at least one pick existed but every download "
                "attempt failed, leaving the shot without an asset. "
                "NOT a count of failed download attempts — see "
                "download_attempt_failures."
            ),
            "download_attempt_failures": (
                "ATTEMPT-level count: candidate_records events with "
                "outcome 'download_failed' (one per failed pick "
                "attempt, including transcript-verification "
                "demotions recorded at that stage). Null when the "
                "run produced no candidate records."
            ),
            "source_reuse.distinct_sources": (
                "Distinct source/provider LABELS among accepted "
                "real assets (the asset's recorded source name, or "
                "its URL host when unnamed) — a provider-diversity "
                "count, NOT a count of distinct source URLs. See "
                "distinct_source_urls for the URL-level count."
            ),
            "source_reuse.distinct_source_urls": (
                "Distinct source URLs among accepted real assets."
            ),
            "source_reuse.max_uses_of_single_source_url": (
                "Largest number of accepted real assets drawn from "
                "one source URL."
            ),
            "tier_breakdown.broll_share": (
                "Share of total timeline-item runtime whose items "
                "are stamped activity_broll or atmospheric (the "
                "B-roll tiers T3+T4). Null when the timeline carries "
                "no tier stamps (pre-B0 productions): unknown, not "
                "zero. Phase 3 / D-B1: tier_breakdown.band compares "
                "this share against the active genre pack's "
                "founder-set expectations.broll_share band "
                "(within_band / over_band) — REPORT ONLY, never a "
                "gate; an over-band run is reported, not blocked."
            ),
            "discovery": (
                "Phase 3 / AM-022 — the run-level discovery fact. "
                "state degraded_no_search_key (with fact 'discovery "
                "degraded: no search key') ONLY when the run's "
                "Serper calls were zero AND no SERPER_API_KEY was "
                "configured; zero calls with a key configured is "
                "state recorded with reason no_serper_calls and is "
                "NOT degraded; no execution record is state unknown "
                "with degraded null."
            ),
            "query_diagnostics": (
                "Phase 3 / AM-021 — one entry per (scene, beat, "
                "pass) retrieval pass: the queries ACTUALLY issued, "
                "each with the source that supplied it (scenario / "
                "composed / intent_queries, plus the search's own "
                "format_shaped and targeted_institutional queries), "
                "its raw hit count, and how many of its candidates "
                "were scored and verifier-accepted. Tier-ladder "
                "searches are accounted in tier_attempts instead."
            ),
            "source_hints": (
                "Transformation Phase 4.2 — the institutional "
                "source-hint funnel, aggregated from the AM-021 "
                "query diagnostics: queries issued under source "
                "source_hint (the genre pack's Commons category / "
                "Archive advanced-search / site-scoped templates, "
                "formatted per shot need) vs all baseline queries, "
                "each side summed over hits / candidates / "
                "verified. state is no_hints when the run's pack "
                "carried no hints — an absence, stated, not a zero "
                "measured against hints that never ran."
            ),
            "beat_sufficiency": (
                "Phase 3 / FP-4 (measure only) — per beat: its "
                "sentence count, the distinct assets fetched for "
                "it, their ratio, and meets_half (distinct assets "
                "at least half the sentence count). beat_sufficiency"
                "_summary counts searchable beats below that line. "
                "Nothing draws differently because of this measure."
            ),
            "missing_visuals": (
                "Phase 1 / AM-018 — the ONE coverage rule: an entry "
                "for every sentence that retrieval left without a "
                "verified visual of its own, PLUS every sentence "
                "the timeline records as uncovered (appended with "
                "reason no_timeline_coverage) — sentence-level "
                "non-coverage always materialises here. Phase 2 / "
                "D-B3: sentences in unsearched-role beats whose T3 "
                "ladder entry left them uncovered appear here too, "
                "with reason unsearched_role and their tier "
                "attempts recorded. Each entry's coverage_outcome "
                "is the timeline's verdict for that sentence: "
                "uncovered, covered_by_rotation, covered_by_graphic, "
                "covered_by_scene_asset or covered_unidentified; "
                "null when the report was built without a timeline "
                "(unknown, not a verdict)."
            ),
            "tier_attempts": (
                "Phase 2 / B2 — one record per tier-ladder attempt: "
                "the tier, the queries it spent, its outcome "
                "(verified / covered / pool_exhausted / "
                "no_concepts / budget_exhausted) and, for budget "
                "stops, the stop_reason. Escalation fires only on "
                "a recorded coverage shortfall; a production whose "
                "beats all covered has an empty list."
            ),
            "tier_escalation": (
                "Phase 2 / B2 — ladder totals: attempts, extra "
                "searches spent, attempts by tier and outcome, "
                "assets accepted via escalation, and the D-B2 "
                "production budgets they were spent against."
            ),
        },
        "low_confidence_selections": len(low_confidence),
        "sentence_assignment_accuracy": assignment_accuracy,
        "timeline_items": total_items,
        "assignment_breakdown": dict(assignment_counts),
        "source_reuse": {
            "distinct_sources": len(source_counter),
            # Stage 6 / ASSET-017: the URL-level count the legacy
            # key's name implied (see scalar_definitions).
            "distinct_source_urls": len(url_counter),
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
            # Phase 3 / AM-022b: stills counted beside footage.
            "accepted_stills_by_provider": dict(accepted_stills_by_provider),
            "single_provider_reliance": {
                "warning": reliance_warning,
                "provider": reliance_warning_provider,
                "footage_share": round(reliance_share, 4) if total_footage else None,
                # Phase 3 / AM-022b: every reliance figure states
                # its base n; stills carry their own share/base.
                "footage_base": total_footage,
                "footage_provider": reliance_provider,
                "stills_share": round(stills_share, 4) if total_stills else None,
                "stills_base": total_stills,
                "stills_provider": stills_provider,
            },
        },
        "download_failures": download_failures,
        # Run-11 fix 2: measured per-provider-scope download
        # health for the production — attempts / succeeded /
        # failed / rate_limited / backed_off per scope (the fetch
        # host), recorded by services.provider_health at every
        # download attempt and every observed HTTP 429.
        "provider_health": provider_health_snapshot,
        "rejection_reasons": dict(rejection_reasons),
        "discovery_failures": discovery_failures,
        # Phase 3 zero-vs-unknown: "recorded" only when at least one
        # scene's asset_stats actually carried the discovery_failures
        # key (even empty); otherwise the collection never happened.
        "discovery_failures_state": (
            "recorded" if discovery_failures_collected else "not_collected"
        ),
        "recovery_attempts": recovery_attempts,
        "coverage_passes": coverage_passes,
        # Phase 5 / AM-003: per-shot variety-pass records.
        "variety_passes": variety_passes,
        # Master Directive Phase 2 / B2: the tier ladder's attempt
        # records (per coverage pass and per D-B3 unsearched entry)
        # and their totals — budget exhaustion is visible here,
        # never silent.
        "tier_attempts": tier_attempts,
        "tier_escalation": tier_escalation_totals,
        # Phase 3 / AM-021: per-beat, per-pass query diagnostics —
        # the queries actually issued at T1 (with the source that
        # supplied each) and their hits / candidates / verified
        # counts, aggregated from the scenes' stats.
        "query_diagnostics": query_diagnostics,
        # Transformation Phase 4.2: the institutional source-hint
        # funnel — hint-sourced queries vs baseline queries (hits /
        # candidates / verified each), aggregated from the AM-021
        # diagnostics above. state "no_hints" when the run's genre
        # pack carried no hints or none were issued.
        "source_hints": source_hints_summary,
        # Phase 3 / FP-4: per-beat sufficiency (distinct fetched
        # assets vs the beat's sentences) with the below-half count.
        "beat_sufficiency": beat_sufficiency,
        "beat_sufficiency_summary": {
            "beats": len(beat_sufficiency),
            "searchable_beats": sum(
                1 for s in beat_sufficiency if s.get("searchable", True)
            ),
            "below_half": sum(
                1 for s in beat_sufficiency
                if s.get("searchable", True) and not s.get("meets_half")
            ),
        },
        "coverage": coverage,
        "rights": rights,
    }


def write_asset_report(project_dir, report) -> Path:
    # Atomic (Stage 4 / DATA-101 class): the asset report is read
    # back as truth by the plan, certificate and report builders.
    # The directory argument is the artefact home chosen by the
    # caller — a run's artefact directory for pipeline runs
    # (DATA-004), the project directory for legacy callers.
    path = Path(project_dir) / "asset_report.json"
    write_json_atomic(path, report)
    return path


def _rights_warnings(entry):
    """Phase 1 / AM-014: the per-asset warnings for a rights
    manifest entry, derived ONLY from the entry's own recorded
    fields — a warning names a gap in the recorded evidence (an
    unknown licence, a missing licence URL, creator, retrieval
    timestamp or source URL). Nothing is inferred beyond the
    entry itself, and a fully evidenced entry warns about
    nothing."""
    warnings = []
    if not entry.get("license") or entry.get("license") == "unknown":
        warnings.append("unknown_licence")
    if not entry.get("license_url"):
        warnings.append("licence_url_not_recorded")
    if not entry.get("creator"):
        warnings.append("creator_not_recorded")
    if not entry.get("retrieved_at"):
        warnings.append("retrieved_at_not_recorded")
    if not entry.get("source_url"):
        warnings.append("source_url_not_recorded")
    return warnings


def build_rights_manifest(processed_scenes, soundtrack=None) -> list:
    """Per-asset rights evidence for a project (AUD-15).

    One entry per accepted asset: where it came from, who created it (when
    the provider reports it), its licence exactly as recorded — "unknown"
    stays "unknown" — and when it was retrieved. This manifest is the
    project's reuse evidence; nothing in it is inferred or upgraded.

    Phase 1 / AM-014: every entry also carries a "warnings" list
    naming the gaps in its own recorded evidence (see
    _rights_warnings).

    Stage H (M7): when the production's soundtrack record shows an
    APPLIED track, the manifest gains one "music" entry for it —
    the track is an asset of the film like any other, and its
    licence is "unknown" unless the record carries evidence (for
    operator/editor-supplied files it never does). Narration-only
    productions (every state other than "applied") gain no entry,
    so the manifest still covers exactly the accepted assets.
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
        entry = {
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
        entry["warnings"] = _rights_warnings(entry)
        manifest.append(entry)
    if isinstance(soundtrack, dict) and soundtrack.get("state") == "applied":
        identity = soundtrack.get("identity") or {}
        music_entry = {
            "file_path": identity.get("file_path")
            or soundtrack.get("requested_file")
            or "",
            "type": "music",
            "title": "",
            "source": soundtrack.get("source_type") or "",
            "source_url": "",
            "content_url": "",
            "creator": "",
            "license": identity.get("license") or "unknown",
            "license_url": "",
            "rights_status": "verify_before_publish",
            "retrieved_at": "",
            "sentence_numbers": [],
            "content_sha256": identity.get("content_sha256"),
        }
        music_entry["warnings"] = _rights_warnings(music_entry)
        manifest.append(music_entry)
    return manifest


def write_rights_manifest(project_dir, manifest) -> Path:
    # Atomic (Stage 4 / DATA-101 class): the rights manifest is the
    # provenance record readers reconcile against — never partially
    # written. Directory semantics as in write_asset_report.
    path = Path(project_dir) / "rights_manifest.json"
    write_json_atomic(path, manifest)
    return path
