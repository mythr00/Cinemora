"""Verification hand-off seam of the former media_pipeline monolith
(Phase 5.4).

The post-discovery verification layer: candidate identity
(_candidate_id_for, _uve_candidate_key), URL hygiene hand-off
(_apply_url_hygiene), the metadata gate hand-off _uve_verify_all
(dedupe, eligibility gate, evidence scoring), visual-format
classification (_VISUAL_FORMAT_TERMS, classify_visual_format,
_uve_annotate), the Phase 1 frame-verification hand-off
(_apply_frame_check), content hashing (_uve_content_hash), and
_uve_fetch_first — the fetch hand-off that walks verified candidates,
applies the frame/transcript checks to each downloaded asset and
returns the first accepted one. The downloader itself is INJECTED by
the caller, which is what makes this seam cleanly movable.

Re-exported through media_pipeline; every historical
``media_pipeline.<name>`` reference keeps working unchanged.

CALL-SITE CONVENTION — read before editing: names that remain in
media_pipeline are resolved through its namespace at call time
(``_media_pipeline().<name>``): the eligibility gate gate_shot_need
and its matching helpers (_fold_name, _resolve_need_aliases,
result_title — the assignment seam, and the gate is patched on
media_pipeline by the verification-core tests, which count gate
executions through _uve_verify_all), _record_dedupe_drop (discovery
seam), _apply_transcript_check (stays with the transcript chain),
_FAILED_DOWNLOAD_URLS and DEFAULT_CLIP_DURATION (the failed-download
set is RunContext-bound state owned by media_pipeline's binder).
Unpatched, each resolves to exactly the object it always was.

WHAT STAYED in media_pipeline, and why (Phase 5.4 coupling note):
_uve_download_any_candidate and the two downloaders
(_uve_download_video_candidate / _uve_download_image_candidate) own
six RunContext-bound containers (_SEGMENT_BASIS / _SEGMENT_ATTEMPTS /
_SEGMENT_FAILURE_CAUSE / _SEGMENT_FILE_CACHE / _yt_segment_uses /
_FAILED_DOWNLOAD_URLS) and call the most-patched helpers in the
codebase (download_video_clip, validate_video,
_segment_start_detailed); _apply_transcript_check stays with the
transcript chain (_transcript_window_text is patched on
media_pipeline and reads the RunContext-bound YouTube transcript
caches). Moving either group would require rewriting their state
access through another namespace — a behaviour risk, not a move.
"""

import os

from services import download_taxonomy
from services import frame_verification
from services import pipeline_assignment
from services import provider_health
from services import rejection_diagnostics
from services import run_metrics
from services import story_understanding as story
from services import tier_ladder
from services import url_hygiene
from services import visual_identity

try:
    from services import source_cache
except ImportError:  # flat-layout fallback
    import source_cache

try:
    from services.canonical_media import (
        canonical_candidate_key,
        canonical_source_key,
    )
except ImportError:  # flat-layout fallback
    from canonical_media import (  # noqa: F401
        canonical_candidate_key,
        canonical_source_key,
    )


def _media_pipeline():
    """Lazy handle on media_pipeline (circular-import-safe; see the
    module docstring for why cross-seam calls resolve through it)."""
    try:
        from services import media_pipeline
    except ImportError:  # flat-layout fallback
        import media_pipeline
    return media_pipeline


def _apply_frame_check(asset, candidate, scenario, bible):
    """Run the Phase 1.1 frame-level visual verification on one
    downloaded asset (transformation plan §1.1).

    The ``visual`` verdict dimension was ``not_evaluated`` under a
    previous-era standing order (AM-028) because verification was
    metadata-only. The founder's takeover mandate (2026-11-21)
    supersedes that carve-out: the dimension is now evaluated on
    sampled frames of the downloaded file itself by
    services/frame_verification (deterministic detectors only —
    banned social/meme formats, transient channel cards,
    insufficient landscape fill, OCR geography contradiction;
    watermark presence is recorded, never rejected, per the
    founder's 2026-11-22 watermark ruling).

    Stamps the full record as ``frame_check`` on the asset AND on
    the candidate's verdict copy (the ledger's records are built
    from the candidate pool item), and overwrites the verdict's
    ``dimension_results["visual"]`` with the evaluated outcome —
    the metadata-time ``not_evaluated`` stays on candidates that
    were never downloaded, which is the truthful value for them.
    On a typed rejection the asset's verdict is demoted in place
    (accepted=False) exactly like the transcript demotion; the
    caller records the failure and moves to the next pick. Any
    internal failure leaves the asset untouched (fail-soft: the
    record itself says ``not_evaluated`` with the cause). This
    layer ONLY ever demotes. Returns the frame-check record, or
    None when it cannot run at all.
    """
    if not isinstance(asset, dict):
        return None
    try:
        record = frame_verification.verify_asset_frames(
            asset, scenario, bible
        )
    except Exception as error:  # a frame failure never crashes a pick
        print(f"FRAME CHECK SKIPPED (error): {error}")
        return None
    asset["frame_check"] = record
    outcome = record.get("outcome")
    visual_value = {
        "pass": "pass",
        "reject": "fail",
    }.get(outcome, "not_evaluated")

    def _stamp(verdict):
        if not isinstance(verdict, dict):
            return verdict
        stamped = dict(verdict)
        stamped["frame_check"] = record
        dimensions = dict(stamped.get("dimension_results") or {})
        dimensions["visual"] = visual_value
        stamped["dimension_results"] = dimensions
        if record.get("watermarked_preview"):
            # Record-only provenance (founder ruling 2026-11-22):
            # the same field the metadata path stamps, never a
            # rejection and never labelled clean.
            stamped[story.WATERMARKED_PREVIEW_FIELD] = True
        return stamped

    asset["verification"] = _stamp(asset.get("verification") or {})
    if isinstance(candidate, dict) and candidate.get("verification"):
        candidate["verification"] = _stamp(candidate["verification"])
    if outcome == "reject":
        reason = str(record.get("reason") or "frame_verification")
        verdict = dict(asset.get("verification") or {})
        verdict["accepted"] = False
        verdict["rejection_reason"] = reason
        asset["verification"] = verdict
        asset["reject_reason"] = reason
        asset["rejection_stage"] = "frame_verification"
    return record

_VISUAL_FORMAT_TERMS = {
    "map": ("map", "karte", "atlas", "cartograph"),
    "document": (
        "document", "manuscript", "decree", "treaty", "newspaper",
        "front page", "court record", "proclamation",
    ),
    "diagram": ("diagram", "schematic", "infographic", "chart", "graph"),
    "photograph": ("photograph", "photo of"),
}


def classify_visual_format(asset):
    """Evidence-based visual-format classification (F-3).

    Correction rule: an asset is NEVER classified solely from the search
    query or the intended format. Classification uses the asset's own
    metadata and source context (provider title/snippet terms); when no
    evidence supports a specific kind, the format stays "uncertain" —
    the asset can still be a genuine verified image; kind and
    authenticity are separate properties. Returns (format, evidence).
    """
    asset_type = (asset.get("type") or asset.get("visual_type") or "").lower()
    if asset_type == "video":
        return "footage", "media_probe"
    if asset.get("generated"):
        return (asset.get("visual_format") or "graphic"), "generated_in_house"

    text = (
        f"{asset.get('title') or ''} {asset.get('snippet') or ''} "
        f"{asset.get('source_url') or ''}"
    ).lower()
    for fmt, terms in _VISUAL_FORMAT_TERMS.items():
        if any(term in text for term in terms):
            return fmt, "source_context"
    return "uncertain", "unknown"


def _uve_annotate(asset, scenario, scenario_index, sentence_text):
    asset.update({
        "scenario_index": scenario_index,
        "scenario_visual": scenario.get("visual", ""),
        "scenario_kind": scenario.get("kind", ""),
        "scenario_specificity": scenario.get("specificity", ""),
        "scenario_subject_id": scenario.get("subject_id"),
        "asset_scope": scenario.get("kind") or "context_broll",
        "visual_role": scenario.get("kind") or "context_broll",
        "matched_sentence": sentence_text,
        "rights_status": asset.get("rights_status", "verify_before_publish"),
    })
    # F-3: stamp the evidence-based visual format. The scenario's intent
    # is recorded only as a hint when the evidence is inconclusive —
    # intent alone never classifies an asset.
    if not asset.get("visual_format"):
        fmt, evidence = classify_visual_format(asset)
        asset["visual_format"] = fmt
        asset["visual_format_evidence"] = evidence
        if evidence == "unknown":
            asset["format_hint"] = scenario.get("format_intent") or "any"
    return asset

def _uve_candidate_key(candidate):
    """Dedup/selection key for a candidate.

    Quality Recovery Phase 2 (workstream C; canonical DM-16): the base
    is the CANONICAL source key (services.canonical_media), not the
    raw URL string — youtu.be/X and watch?v=X are one candidate, and
    tracking parameters never mint a "new" candidate. Segment identity
    stays distinct from source identity: a stamped clip_start makes
    the key segment-specific, and an unstamped YouTube candidate keeps
    the "|pending" form so the same source may be selected again for
    a different segment (the registry's overlap blocking then applies
    on the canonical source)."""
    if not isinstance(candidate, dict):
        return ""

    base = canonical_candidate_key(candidate)

    if not base:
        return ""

    # Search candidates do not have a timestamp yet.
    # The timestamp is assigned later by _youtube_start().
    # Do not use _yt_segment_uses here because it contains
    # a list of timestamps, not one timestamp.
    start = candidate.get("clip_start")

    if start is not None:
        try:
            start = int(float(start))
        except (TypeError, ValueError):
            start = str(start)

        return f"{base}|{start}"

    # Allow the same YouTube source to be selected again.
    # _youtube_start() will choose a different segment.
    if base.startswith("youtube:"):
        return f"{base}|pending"

    return base

def _candidate_id_for(candidate):
    """Stable per-run candidate identifier (VA-08).

    Derived from the candidate's CANONICAL source identity (Phase 2,
    canonical DM-16), so the same source carries the same id through
    verification, selection, download and reporting within a run —
    however its URL was spelled by whichever search seam found it.
    """
    import hashlib

    key = canonical_candidate_key(candidate)
    return "cand_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _apply_url_hygiene(candidate):
    """Run-11 fix 1: resolve redirector-wrapper URLs on ONE
    candidate, in place, before it is identified, gated, scored
    or fetched.

    The candidate's primary URL (video_url / url / link /
    image_url, first non-empty) is the address every later stage
    would hand to yt-dlp or the image fetcher. When it is a
    redirector wrapper (services.url_hygiene), the recovered
    destination replaces EVERY field that carried the wrapper and
    the wrapper is preserved on ``url_resolved_from`` as
    provenance. When no destination can be recovered, the
    candidate is stamped ``_url_hygiene_failure`` and
    _uve_verify_all rejects it at the discovery stage with the
    typed UNRESOLVABLE_REDIRECTOR reason — no gate call, no
    scoring, no segment-evidence or download attempt is spent on
    the wrapper. The provenance page URL (source_url) is rewritten
    only when its own resolution succeeds; a failed source_url
    never condemns a fetchable candidate. Returns True when the
    candidate may proceed, False when it must be rejected."""
    if not isinstance(candidate, dict):
        return True
    primary = ""
    for field in ("video_url", "url", "link", "image_url"):
        value = str(candidate.get(field) or "").strip()
        if value:
            primary = value
            break
    if primary and url_hygiene.is_redirector_url(primary):
        resolved, status = url_hygiene.resolve_candidate_url(primary)
        candidate["_url_hygiene_status"] = status
        if not resolved:
            candidate["_url_hygiene_failure"] = (
                rejection_diagnostics.UNRESOLVABLE_REDIRECTOR_REASON
            )
            return False
        for field in ("video_url", "url", "link", "image_url"):
            if str(candidate.get(field) or "").strip() == primary:
                candidate[field] = resolved
        candidate["url_resolved_from"] = primary
    source_url = str(candidate.get("source_url") or "").strip()
    if (
        source_url
        and source_url != primary
        and url_hygiene.is_redirector_url(source_url)
    ):
        resolved, _status = url_hygiene.resolve_candidate_url(source_url)
        if resolved:
            candidate["source_url"] = resolved
    return True


def _speculative_verdict(verdict, absence_failures):
    """Stamp an ACCEPTED verdict as a metadata-absence bypass
    (run-6345 fix): the candidate's gate failures were absence-only
    and the scorer accepted it on positive evidence, so it is
    admitted CONDITIONALLY — score reduced by
    METADATA_ABSENCE_PENALTY, the bypass and the absence reasons
    recorded on the verdict, and acceptance explicitly conditioned
    on frame-level confirmation after download (enforced in
    _uve_fetch_first). This is not the retired AUD-16 relaxed pool:
    that admitted verifier-REJECTED candidates on score alone with
    no further check; a speculative candidate was verifier-ACCEPTED,
    is penalised and marked, is budget-capped, and is rejected at
    the frame stage unless the frame stage positively evaluates
    its frames."""
    stamped = dict(verdict)
    raw_score = stamped.get("score") or 0
    stamped["pre_penalty_score"] = raw_score
    stamped["score"] = round(
        max(0.0, raw_score - pipeline_assignment.METADATA_ABSENCE_PENALTY),
        3,
    )
    stamped["metadata_absence_bypass"] = True
    stamped["metadata_absence_penalty"] = (
        pipeline_assignment.METADATA_ABSENCE_PENALTY
    )
    stamped["absence_reasons"] = list(absence_failures)
    stamped["acceptance_basis"] = (
        "metadata_absence_bypass_frame_confirmation_required"
    )
    reasons = list(stamped.get("reasons") or [])
    reasons.append(
        "metadata absence bypass: gate failures "
        + ", ".join(absence_failures)
        + " are absence-only; admitted speculatively pending "
        "frame confirmation"
    )
    stamped["reasons"] = reasons
    return stamped


def _verdict_cache():
    """The ACTIVE run's verdict cache, or None.

    Gate 4 diagnosis (2026-11-26): coverage passes and recovery
    rounds re-run _uve_verify_all over largely the same candidates
    for the same needs; in the Hyatt run 3,443 of 5,916 candidate
    records were exact (URL, sentences, beat) re-evaluations, each
    paying a gate execution and a scorer call. The gate and the
    scorer are pure functions of (candidate metadata, gate need,
    scenario, bible), and the bible is fixed within a run, so a
    per-run cache keyed on exactly those inputs is sound.

    Scoped to a genuinely activated run context (jobs activates one
    per production): the DEFAULT context — unit tests, ad-hoc
    calls — gets no cache, so patched-gate counting tests (VA-11)
    and cross-test isolation are untouched."""
    try:
        from services import run_context
    except ImportError:  # flat-layout fallback
        import run_context
    try:
        ctx = run_context.current_context()
        if ctx is None or ctx is run_context.default_context():
            return None
        cache = getattr(ctx, "verdict_cache", None)
        return cache if isinstance(cache, dict) else None
    except Exception:
        return None


def _verdict_cache_key(candidate, key, gate_need, verify_scenario):
    """Fingerprint of everything the gate + scorer read: the
    candidate's identity key and metadata text, the enriched gate
    need, and the verify scenario. Two evaluations with the same
    key are the same evaluation."""
    import hashlib
    import json

    meta = "|".join(
        str(candidate.get(k) or "")
        for k in ("title", "snippet", "description", "provider", "source")
    )
    payload = json.dumps(
        [meta, gate_need, verify_scenario], sort_keys=True, default=str
    )
    return (key, hashlib.sha256(payload.encode("utf-8")).hexdigest())


def _uve_verify_all(candidates, scenario, bible, shot_need=None):
    scored, seen = [], set()
    _cache = _verdict_cache()

    # Phase 1: enrich the gate's shot need (a COPY — the caller's dict
    # is never mutated) with the bible's other people/places so the
    # gate can judge positive contradictions where it has the data;
    # verify_candidate applies the same rules from the bible directly.
    gate_need = shot_need
    if shot_need is not None and bible:
        gate_need = dict(shot_need)
        _entities = bible.get("entities") or []
        _req_id = shot_need.get("required_subject_id")
        gate_need.setdefault("other_people", [
            e.get("canonical") for e in _entities
            if e.get("type") == "person" and e.get("id") != _req_id
            and e.get("canonical")
        ])
        _place_norm = str(shot_need.get("place") or "").strip().lower()
        gate_need.setdefault("other_places", [
            e.get("canonical") for e in _entities
            if e.get("type") in {"place", "location"}
            and e.get("canonical")
            and str(e.get("canonical")).strip().lower() != _place_norm
        ])
        # Transformation Phase 5: alias evidence for the gate's
        # subject/place matching, for needs that did not come
        # through build_shot_needs (which stamps the same fields).
        if (
            "subject_aliases" not in gate_need
            or "place_aliases" not in gate_need
        ):
            _subject_entity = None
            _by_id = bible.get("by_id") or {}
            if _req_id and isinstance(_by_id.get(_req_id), dict):
                _subject_entity = _by_id[_req_id]
            else:
                for e in _entities:
                    if _media_pipeline()._fold_name(e.get("canonical")) == _media_pipeline()._fold_name(
                        shot_need.get("required_subject")
                    ):
                        _subject_entity = e
                        break
            _subj_aliases, _place_aliases = _media_pipeline()._resolve_need_aliases(
                bible,
                _subject_entity,
                str(shot_need.get("required_subject") or ""),
                str(shot_need.get("place") or ""),
            )
            gate_need.setdefault("subject_aliases", _subj_aliases)
            gate_need.setdefault("place_aliases", _place_aliases)
            if isinstance(_subject_entity, dict):
                gate_need.setdefault(
                    "subject_entity_type",
                    str(_subject_entity.get("type") or "").strip().lower(),
                )

    # Stage 7 / R7: ONE category per shot across gate, scorer and
    # events. The shot NEED is the authoritative shot record (it
    # carries shot_type and the sentence's named place, which the
    # bare scenario may lack), so when a need exists the scorer
    # judges under the need's resolved category — stamped onto a
    # COPY of the scenario; the caller's dict is never mutated.
    verify_scenario = scenario
    if gate_need is not None:
        verify_scenario = dict(scenario or {})
        verify_scenario["visual_category"] = (
            story.resolve_visual_category(gate_need)
        )

    for candidate in candidates:
        # Run-11 fix 1 (URL hygiene): a redirector wrapper is
        # resolved to its real destination BEFORE the candidate is
        # identified, gated, scored or fetched — the wrapper
        # itself must never reach yt-dlp or the image fetcher. An
        # unresolvable wrapper is rejected here, at the discovery
        # stage, with its typed reason (run 11: 69 of 108 download
        # failures were segment-evidence deaths on google.com/goto
        # wrappers that were never real media addresses).
        if not _apply_url_hygiene(candidate):
            item = dict(candidate)
            item["candidate_id"] = _candidate_id_for(item)
            item["reject_reason"] = (
                rejection_diagnostics.UNRESOLVABLE_REDIRECTOR_REASON
            )
            item["rejection_stage"] = "discovery"
            item["verification"] = {
                "accepted": False,
                "score": 0,
            }
            item["universal_score"] = 0
            item["relevance_score"] = 0
            scored.append(item)
            print(
                "      REJECT "
                f"{rejection_diagnostics.UNRESOLVABLE_REDIRECTOR_REASON}: "
                f"{_media_pipeline().result_title(candidate)[:80]}"
            )
            continue

        candidate["candidate_id"] = _candidate_id_for(candidate)
        key = _uve_candidate_key(candidate)

        if not key or key in seen:
            # Stage 1 / R5: a dropped duplicate/keyless candidate is
            # a recorded rejection event (stage "dedupe"), not a
            # silent disappearance.
            _media_pipeline()._record_dedupe_drop(candidate, key)
            continue

        seen.add(key)

        # Gate 4: an identical (candidate, need, scenario)
        # evaluation already completed in this run is replayed from
        # the verdict cache — no second gate execution, no second
        # scorer call (see _verdict_cache). The replayed item is a
        # deep copy: downstream stages stamp their own fields on
        # the items they receive.
        _cache_key = None
        if _cache is not None:
            _cache_key = _verdict_cache_key(
                candidate, key, gate_need, verify_scenario
            )
            _hit = _cache.get(_cache_key)
            if _hit is not None:
                import copy

                scored.append(copy.deepcopy(_hit))
                continue

        def _remember(item):
            if _cache is not None and _cache_key is not None:
                import copy

                _cache[_cache_key] = copy.deepcopy(item)

        # Cheap metadata gate BEFORE expensive LLM verification.
        ok, reason = _media_pipeline().gate_shot_need(
            candidate,
            gate_need,
        )

        # Metadata-absence bypass (run-6345 fix, 2026-11-23): a
        # gate failure whose collect-all failure list is ABSENCE-
        # ONLY (rejection_diagnostics.failures_are_absence_only —
        # the metadata does not mention a required entity, but
        # nothing in it positively contradicts the need) is no
        # longer a hard rejection here. The candidate proceeds to
        # the scorer; if the scorer accepts it on positive
        # evidence it is admitted speculatively (penalty + marker,
        # see _speculative_verdict) and must still be confirmed by
        # frame-level verification after a budget-capped download.
        # Any contradiction in the list keeps the historical hard
        # rejection at the gate, byte for byte.
        absence_failures = None
        if not ok:
            # Stage 6 / R5: the gate's collect-all failure list,
            # stamped on the candidate by gate_shot_need itself —
            # from the SAME single gate execution (VA-11).
            gate_failures = list(
                candidate.get("_gate_failures") or [reason]
            )
            if rejection_diagnostics.failures_are_absence_only(
                gate_failures
            ):
                absence_failures = gate_failures
            else:
                item = dict(candidate)
                source_cache.bump("gate_rejects")
                item["reject_reason"] = reason
                item["rejection_stage"] = "metadata_gate"
                item["gate_failures"] = gate_failures
                item["verification"] = {
                    "accepted": False,
                    "score": 0,
                }
                item["universal_score"] = 0
                item["relevance_score"] = 0
                _remember(item)
                scored.append(item)

                print(
                    f"      REJECT {reason}: "
                    f"{_media_pipeline().result_title(candidate)[:80]}"
                )
                continue

        source_cache.bump("llm_verify_calls")
        verdict = story.verify_candidate(
            candidate,
            verify_scenario,
            bible,
        )

        item = dict(candidate)
        if absence_failures is not None:
            item["gate_failures"] = list(absence_failures)
            if verdict.get("accepted"):
                verdict = _speculative_verdict(
                    verdict, absence_failures
                )
                item["metadata_absence_bypass"] = True
                item["metadata_absence_penalty"] = (
                    pipeline_assignment.METADATA_ABSENCE_PENALTY
                )
                item["absence_reasons"] = list(absence_failures)
                print(
                    "      SPECULATIVE (metadata absence bypass: "
                    f"{', '.join(absence_failures)}): "
                    f"{_media_pipeline().result_title(candidate)[:80]}"
                )
            # A scorer REJECTION of an absence-bypassed candidate
            # stands exactly as recorded below (stage "scoring"):
            # the scorer's absence verdicts (no_topical_evidence /
            # missing_required_subject) and its threshold encode
            # "no positive evidence" / an explicit exact contract,
            # which the frame stage cannot truthfully rescue — see
            # rejection_diagnostics' classification comment.
        item["verification"] = verdict
        item["universal_score"] = verdict["score"]
        item["relevance_score"] = verdict["score"]
        if not verdict.get("accepted"):
            # VA-04/VA-08: scoring rejections used to leave NO record —
            # the verdict (score, contributions, reason) was computed
            # and then dropped. Stamp the reason and stage so the
            # review log and asset report can explain the decision.
            # Stage 6 / C.A.4: the retired score-below alias is
            # gone — verify_candidate's precedence chain is
            # total over rejections, so its rejection_reason is
            # always present; the fallback names the verifier's own
            # vocabulary string, never the alias.
            item["reject_reason"] = (
                verdict.get("rejection_reason") or "below_threshold"
            )
            item["rejection_stage"] = "scoring"
        _remember(item)
        scored.append(item)

    scored.sort(
        key=lambda c: c["universal_score"],
        reverse=True,
    )

    return scored

_CONTENT_HASH_CACHE: dict = {}


def _uve_content_hash(path):
    """SHA-256 of a downloaded file's bytes (Phase 5 dedupe
    identity), cached per (path, size, mtime) for the process —
    a segment-reused asset shares the first download's file, so
    repeat stamps of one file cost one read. None when the file
    cannot be read: identity then falls back to the canonical
    ladder, never to a guessed hash."""
    if not path:
        return None
    try:
        stat = os.stat(path)
        cache_key = (str(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None
    if cache_key not in _CONTENT_HASH_CACHE:
        _CONTENT_HASH_CACHE[cache_key] = (
            visual_identity.content_hash_for_file(path)
        )
    return _CONTENT_HASH_CACHE[cache_key]


def _uve_fetch_first(candidates, downloader, scene_dir, sentence_number, asset_index, registry, used_keys, failures=None, transcript_scenario=None, transcript_bible=None):
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        candidate_url = str(
            candidate.get("url")
            or candidate.get("video_url")
            or candidate.get("image_url")
            or ""
        )
        # Phase 2: the failed-download guard keys on the CANONICAL
        # source identity — a failed candidate is never re-downloaded
        # within a run, however its URL is respelled.
        # Phase 3 / AM-016: when the candidate already carries a
        # segment identity (stamped by an earlier attempt), the guard
        # ALSO honours the segment key — that segment's failure skips
        # this candidate, while the source key alone no longer blocks
        # a candidate whose segment was never attempted.
        candidate_key = canonical_source_key(candidate_url)
        _segment_guard_key = (
            f"seg:{candidate['segment_id']}"
            if candidate.get("segment_id")
            else None
        )
        if candidate_url and (
            candidate_key in _media_pipeline()._FAILED_DOWNLOAD_URLS
            or (
                _segment_guard_key is not None
                and _segment_guard_key in _media_pipeline()._FAILED_DOWNLOAD_URLS
            )
        ):
            # Already failed once this run (throttled, dead, invalid):
            # never re-request it; record the skip as a failure too.
            if failures is not None:
                failures.append(download_taxonomy.annotate_failure(
                    {
                        "provider": candidate.get("provider")
                        or candidate.get("source")
                        or "unknown",
                        "url": candidate_url,
                        "reason": "previously_failed_this_run",
                        "candidate_id": candidate.get("candidate_id"),
                        "stage": "download",
                        "sentence_number": sentence_number,
                    }
                ))
            continue

        # Run-11 fix 2 (provider health): a provider scope backed
        # off for repeated HTTP 429s is refused BEFORE any request
        # is made. The refusal is recorded with its typed reason;
        # the candidate is NOT entered into the failed-download
        # guard (the scope is at fault, not the candidate), no
        # download attempt is counted (nothing was requested), and
        # the loop moves on to candidates from scopes that
        # actually deliver.
        _health_scope = provider_health.scope_for_url(candidate_url)
        if _health_scope and provider_health.is_backed_off(_health_scope):
            if failures is not None:
                failures.append(download_taxonomy.annotate_failure(
                    {
                        "provider": candidate.get("provider")
                        or candidate.get("source")
                        or "unknown",
                        "url": candidate_url,
                        "reason": (
                            rejection_diagnostics
                            .PROVIDER_BACKED_OFF_REASON
                        ),
                        "candidate_id": candidate.get("candidate_id"),
                        "stage": "download",
                        "sentence_number": sentence_number,
                    }
                ))
            continue

        # Metadata-absence bypass budget (run-6345 fix): a
        # speculative candidate's download spends from the
        # production's shared D-B2 extra-download account (the
        # tier_ladder ledger on the registry: escalation +
        # speculative ≤ 20 per production) and is capped per beat
        # (SPECULATIVE_PER_BEAT_DOWNLOAD_CAP). A candidate drawn by
        # tier escalation is already charged by the draw's own
        # spend_download, so it is not charged twice. The refusal
        # is recorded with its typed reason, no download attempt is
        # counted (nothing was requested), and the candidate is NOT
        # entered into the failed-download guard — the budget is at
        # fault, not the candidate (provider-backoff precedent).
        if (
            candidate.get("metadata_absence_bypass")
            and not candidate.get("tier_escalation")
        ):
            _spec_ledger = tier_ladder.ledger_for(registry)
            _spec_beat_key = (str(scene_dir), sentence_number)
            if not _spec_ledger.can_spend_speculative_download(
                beat_key=_spec_beat_key
            ):
                if failures is not None:
                    failures.append(download_taxonomy.annotate_failure(
                        {
                            "provider": candidate.get("provider")
                            or candidate.get("source")
                            or "unknown",
                            "url": candidate_url,
                            "reason": (
                                rejection_diagnostics
                                .SPECULATIVE_DOWNLOAD_BUDGET_REASON
                            ),
                            "candidate_id": candidate.get("candidate_id"),
                            "stage": "download",
                            "sentence_number": sentence_number,
                        }
                    ))
                continue
            _spec_ledger.spend_speculative_download(
                beat_key=_spec_beat_key
            )

        # Stage 0 metrics: every real download attempt passes
        # through this loop (previously-failed URLs skipped above are
        # not attempts — nothing is requested for them).
        run_metrics.download_attempted()
        asset = downloader(
            candidate,
            scene_dir,
            sentence_number,
            asset_index,
        )
        # Run-11 fix 2: the attempt's outcome is provider-health
        # evidence — per-scope download success is a measured fact
        # of the production, not an anecdote.
        provider_health.record_download_outcome(_health_scope, bool(asset))

        if not asset:
            # Phase 3 / AM-016: a failure from a segment-stamped
            # attempt is recorded under the SEGMENT key only — the
            # source's other segments stay retrievable. Attempts
            # without a segment identity (images, source-level
            # failures) keep the canonical source key, exactly as
            # before.
            _attempted_segment_id = candidate.get("_attempted_segment_id")
            if candidate_url:
                if _attempted_segment_id:
                    _media_pipeline()._FAILED_DOWNLOAD_URLS.add(f"seg:{_attempted_segment_id}")
                else:
                    _media_pipeline()._FAILED_DOWNLOAD_URLS.add(candidate_key)
            # MS-2: a failed download is recorded with its actual reason
            # (stashed on the candidate by the downloader) — never silent,
            # never substituted with unrelated footage.
            if failures is not None:
                failure = {
                    "provider": candidate.get("provider")
                    or candidate.get("source")
                    or "unknown",
                    "url": str(
                        candidate.get("url")
                        or candidate.get("video_url")
                        or candidate.get("image_url")
                        or ""
                    ),
                    "reason": candidate.get("_download_error")
                    or "download_failed",
                }
                if _attempted_segment_id:
                    failure["segment_id"] = _attempted_segment_id
                failure["candidate_id"] = candidate.get("candidate_id")
                failure.setdefault(
                    "stage", candidate.get("rejection_stage") or "download"
                )
                if candidate.get("_technical_rejection"):
                    failure["technical_rejection"] = (
                        candidate["_technical_rejection"]
                    )
                if candidate.get("_segment_failure_cause"):
                    failure["segment_failure_cause"] = (
                        candidate["_segment_failure_cause"]
                    )
                if candidate.get("_segment_attempts"):
                    failure["segment_attempts"] = candidate["_segment_attempts"]
                if candidate.get("rejection_stage"):
                    failure["stage"] = candidate["rejection_stage"]
                # Phase 3a / C-02: every download failure record
                # carries its taxonomy class — from the
                # downloader's own stamp when it made one
                # (it saw the HTTP status / timeout / stderr),
                # else classified here from the reason.
                _taxonomy = candidate.get("_download_failure")
                if isinstance(_taxonomy, dict) and _taxonomy.get("failure_class"):
                    failure["failure_class"] = _taxonomy["failure_class"]
                    failure["http_status"] = _taxonomy.get("http_status")
                    failure["check"] = _taxonomy.get("check")
                    failure["retryable"] = _taxonomy.get("retryable")
                    if _taxonomy.get("detail"):
                        failure["detail"] = _taxonomy["detail"]
                else:
                    failure = download_taxonomy.annotate_failure(
                        failure, sentence_number=sentence_number,
                    )
                failure.setdefault("sentence_number", sentence_number)
                failures.append(failure)
            continue

        # Stage 0 metrics: the download produced a file — count it
        # and its size as downloaded bytes (measured now, while the
        # file the downloader wrote is on disk). A segment-reused
        # asset (Stage 7 / R8) downloaded nothing: no metric.
        _dl_path = asset.get("file_path") or asset.get("path")
        _dl_bytes = 0
        try:
            if _dl_path and os.path.exists(_dl_path):
                _dl_bytes = os.path.getsize(_dl_path)
        except OSError:
            _dl_bytes = 0
        if not asset.get("segment_reused"):
            run_metrics.download_succeeded(_dl_bytes)

        key = _uve_candidate_key(asset)

        # Stage 7 / R8: a segment-reused asset's key is EXPECTED in
        # used_keys (the first beat registered the same segment) —
        # that is the reuse working, not a duplicate: the asset
        # proceeds (its own beat's transcript check and registry
        # accounting still apply) and the shared file is never
        # deleted here.
        if key and key in used_keys and not asset.get("segment_reused"):
            try:
                path = asset.get("file_path")
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
            continue

        # Phase 1.1 (transformation plan §1.1): frame-level visual
        # verification. The downloaded file's own sampled frames
        # are checked by deterministic detectors (banned social /
        # meme formats, transient channel cards, insufficient
        # landscape fill, OCR geography contradiction; watermarks
        # recorded, never rejected). A typed rejection DEMOTES the
        # candidate before registration, exactly like the Stage 3B
        # demotion below: the file is removed (unless segment-
        # reused), the rejection is recorded with its frame_check
        # evidence, and the loop moves to the next pick. The URL is
        # NOT added to the failed-download guard — the download
        # succeeded; the frames failed (transcript precedent).
        frame_record = _apply_frame_check(
            asset, candidate, transcript_scenario, transcript_bible
        )
        if frame_record is not None and frame_record.get("outcome") == "reject":
            print(
                f"      FRAME CHECK REJECTED (demoted): "
                f"{candidate_url} reason={frame_record.get('reason')}"
            )
            if failures is not None:
                failures.append(download_taxonomy.annotate_failure({
                    "provider": candidate.get("provider")
                    or candidate.get("source")
                    or "unknown",
                    "url": candidate_url,
                    "reason": str(
                        frame_record.get("reason") or "frame_verification"
                    ),
                    "candidate_id": candidate.get("candidate_id"),
                    "stage": "frame_verification",
                    "frame_check": frame_record,
                    "sentence_number": sentence_number,
                }))
            if not asset.get("segment_reused"):
                try:
                    path = asset.get("file_path")
                    if path and os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
            continue

        # Metadata-absence bypass confirmation (run-6345 fix): a
        # speculative asset's metadata already failed the gate, so
        # the frame stage is the only positive judgement it can
        # ever receive. An evaluated PASS confirms it. An
        # abstention — outcome not_evaluated (no frames sampled,
        # OCR unavailable, an internal error) or no record at all —
        # confirms nothing, so the asset is demoted here with the
        # typed frame-stage reason and the loop moves on: a
        # speculative asset is never silently accepted. (A frame
        # REJECT was handled by the block above, with the frame
        # stage's own reason.) Non-speculative assets keep the
        # historical fail-soft semantics — their metadata verdict
        # stands on its own.
        if candidate.get("metadata_absence_bypass"):
            if frame_record is None or frame_record.get("outcome") != "pass":
                print(
                    "      SPECULATIVE UNCONFIRMED (demoted): "
                    f"{candidate_url} frame outcome="
                    f"{(frame_record or {}).get('outcome')}"
                )
                if failures is not None:
                    failures.append(download_taxonomy.annotate_failure({
                        "provider": candidate.get("provider")
                        or candidate.get("source")
                        or "unknown",
                        "url": candidate_url,
                        "reason": (
                            rejection_diagnostics
                            .SPECULATIVE_FRAME_UNCONFIRMED_REASON
                        ),
                        "candidate_id": candidate.get("candidate_id"),
                        "stage": "frame_verification",
                        "frame_check": frame_record,
                        "sentence_number": sentence_number,
                    }))
                if not asset.get("segment_reused"):
                    try:
                        path = asset.get("file_path")
                        if path and os.path.exists(path):
                            os.remove(path)
                    except OSError:
                        pass
                continue
            asset["speculative_frame_confirmed"] = True

        # Stage 3B: transcript-interval verification. The asset was
        # accepted by verify_candidate and carries real segment
        # evidence; if the spoken words inside the chosen interval
        # contain none of the scenario's subject/topical tokens, the
        # candidate is DEMOTED (never upgraded) before it is
        # registered or returned: the downloaded file is removed, the
        # rejection is recorded, and the loop moves to the next pick.
        if transcript_scenario is not None:
            check = _media_pipeline()._apply_transcript_check(
                asset, transcript_scenario, transcript_bible
            )
            if check == "transcript_mismatch":
                print(
                    f"      TRANSCRIPT MISMATCH (demoted): "
                    f"{candidate_url} start={asset.get('clip_start')}"
                )
                if failures is not None:
                    failures.append(download_taxonomy.annotate_failure({
                        "provider": candidate.get("provider")
                        or candidate.get("source")
                        or "unknown",
                        "url": candidate_url,
                        "reason": "transcript_mismatch",
                        "candidate_id": candidate.get("candidate_id"),
                        "stage": "transcript_verification",
                        "sentence_number": sentence_number,
                    }))
                # A segment-reused asset's file is SHARED with the
                # beat that extracted it — demotion removes this
                # beat's use, never the shared file.
                if not asset.get("segment_reused"):
                    try:
                        path = asset.get("file_path")
                        if path and os.path.exists(path):
                            os.remove(path)
                    except OSError:
                        pass
                continue

        if key:
            used_keys.add(key)

        try:
            # Phase 2: registry identity is canonical (the registry
            # canonicalises internally too); the clip key is built on
            # the canonical source key so segment bookkeeping is
            # spelling-independent.
            registry_url = str(
                asset.get("video_url")
                or asset.get("url")
                or key
                or ""
            )

            clip_start = asset.get("clip_start")

            if clip_start is not None:
                try:
                    clip_start = int(float(clip_start))
                except (TypeError, ValueError):
                    clip_start = str(clip_start)

                clip_key = (
                    f"{canonical_source_key(registry_url)}|{clip_start}"
                )
            else:
                clip_key = key

            # AUD-20: pass the segment range so the registry's overlap
            # protection actually engages — register() supported start/end
            # all along, but this call never supplied them, leaving
            # used_segments permanently empty.
            segment_start = None
            segment_end = None
            if isinstance(clip_start, (int, float)):
                segment_start = float(clip_start)
                raw_duration = (
                    asset.get("duration_seconds")
                    or asset.get("clip_duration")
                    or _media_pipeline().DEFAULT_CLIP_DURATION
                )
                try:
                    segment_end = segment_start + float(raw_duration)
                except (TypeError, ValueError):
                    segment_end = None

            registry.register(
                path=asset.get("file_path"),
                url=registry_url,
                title=asset.get("title", ""),
                clip_key=clip_key,
                start=segment_start,
                end=segment_end,
            )
        except Exception:
            pass

        # Phase 5 / dedupe by source image: stamp the downloaded
        # bytes' hash on the asset. Where canonical identity
        # differs but the bytes are identical (the same source
        # image downloaded twice), this stamp is what lets every
        # distinctness consumer — coverage/variety arithmetic,
        # timeline dealing, visual_quality, the asset report —
        # count the visual ONCE (services.visual_identity).
        if isinstance(asset, dict) and not asset.get("content_hash"):
            _hash = _uve_content_hash(
                asset.get("file_path") or asset.get("path")
            )
            if _hash:
                asset["content_hash"] = _hash

        return asset

    return None
